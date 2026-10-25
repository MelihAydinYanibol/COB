"""Hands-Free (HF) side of the Bluetooth Hands-Free Profile.

The phone is the Audio Gateway (AG). This module speaks the AT command
protocol on the RFCOMM control channel: it sets up the Service Level
Connection (SLC), tracks the phone's indicators, works out the call state,
and sends call control commands (answer, hang up, dial, ...).

It only needs an asyncio stream pair, so it can be tested without Bluetooth.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import Enum, IntFlag
from typing import Callable

from . import at

log = logging.getLogger(__name__)


class HfFeature(IntFlag):
    """Hands-Free supported features, sent in ``AT+BRSF``."""

    EC_NR = 1 << 0
    THREE_WAY_CALLING = 1 << 1
    CLI_PRESENTATION = 1 << 2
    VOICE_RECOGNITION = 1 << 3
    REMOTE_VOLUME = 1 << 4
    ENHANCED_CALL_STATUS = 1 << 5
    ENHANCED_CALL_CONTROL = 1 << 6
    CODEC_NEGOTIATION = 1 << 7
    HF_INDICATORS = 1 << 8
    ESCO_S4 = 1 << 9


class AgFeature(IntFlag):
    """Audio Gateway supported features, received in ``+BRSF``."""

    THREE_WAY_CALLING = 1 << 0
    EC_NR = 1 << 1
    VOICE_RECOGNITION = 1 << 2
    INBAND_RING = 1 << 3
    VOICE_TAG = 1 << 4
    REJECT_CALL = 1 << 5
    ENHANCED_CALL_STATUS = 1 << 6
    ENHANCED_CALL_CONTROL = 1 << 7
    EXTENDED_ERRORS = 1 << 8
    CODEC_NEGOTIATION = 1 << 9
    HF_INDICATORS = 1 << 10
    ESCO_S4 = 1 << 11


# Codec negotiation is left out on purpose: without it the phone uses CVSD,
# which the kernel turns into plain 8 kHz 16-bit PCM on the SCO socket.
DEFAULT_HF_FEATURES = (
    HfFeature.THREE_WAY_CALLING
    | HfFeature.CLI_PRESENTATION
    | HfFeature.VOICE_RECOGNITION
    | HfFeature.REMOTE_VOLUME
    | HfFeature.ENHANCED_CALL_STATUS
)


def sdp_features(features: HfFeature) -> int:
    """Map ``AT+BRSF`` feature bits to the SDP SupportedFeatures attribute."""
    value = int(features) & 0x1F
    if features & HfFeature.CODEC_NEGOTIATION:
        value |= 1 << 5  # wide band speech
    return value


class CallState(str, Enum):
    IDLE = "idle"
    INCOMING = "incoming"
    WAITING = "waiting"
    DIALING = "dialing"
    ALERTING = "alerting"
    ACTIVE = "active"
    HELD = "held"


class ATError(Exception):
    def __init__(self, command: str, response: str):
        super().__init__(f"{command!r} failed: {response}")
        self.command = command
        self.response = response


Listener = Callable[[str, dict], None]


@dataclass
class _Pending:
    command: str
    prefixes: tuple[str, ...]
    future: asyncio.Future
    lines: list[str] = field(default_factory=list)


def derive_call_state(
    indicators: dict[str, int], previous: CallState = CallState.IDLE
) -> CallState:
    call = indicators.get("call", 0)
    setup = indicators.get("callsetup", 0)
    held = indicators.get("callheld", 0)
    if setup == 1:
        if not call:
            return CallState.INCOMING
        # Answering sends call=1 before callsetup=0; that's not a second call.
        return CallState.ACTIVE if previous == CallState.INCOMING else CallState.WAITING
    if setup == 2:
        return CallState.DIALING
    if setup == 3:
        return CallState.ALERTING
    if call:
        return CallState.HELD if held == 2 else CallState.ACTIVE
    if held:
        return CallState.HELD
    return CallState.IDLE


class HandsFree:
    """One Service Level Connection to a phone.

    Listeners get ``(event, data)`` callbacks. Events:

    ``connected``     SLC is up. data: ag_features, operator
    ``disconnected``  the RFCOMM link closed
    ``ring``          the phone is ringing. data: number
    ``caller_id``     caller number known. data: number
    ``call_waiting``  second incoming call. data: number
    ``call_state``    data: state, previous, number
    ``indicator``     data: name, value
    ``volume``        data: target ("speaker" or "microphone"), level (0-15)
    ``inband_ring``   data: enabled
    ``voice_recognition`` data: enabled
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        features: HfFeature = DEFAULT_HF_FEATURES,
        timeout: float = 10.0,
    ):
        self._reader = reader
        self._writer = writer
        self.features = HfFeature(features)
        self.timeout = timeout

        self.ag_features = AgFeature(0)
        self.indicator_names: list[str] = []
        self.indicators: dict[str, int] = {}
        self.chld_options: list[str] = []
        self.call_state = CallState.IDLE
        self.number: str | None = None
        self.operator: str | None = None
        self.speaker_volume = 12
        self.mic_volume = 12
        self.inband_ring = False
        self.connected = False

        self._listeners: list[Listener] = []
        self._lock = asyncio.Lock()
        self._pending: _Pending | None = None
        self._reader_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closed = asyncio.Event()

    # -- listeners ---------------------------------------------------------

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def _emit(self, event: str, **data) -> None:
        for listener in list(self._listeners):
            try:
                listener(event, data)
            except Exception:
                log.exception("listener failed on %s", event)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start reading and set up the Service Level Connection."""
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            await self._establish_slc()
        except BaseException:
            await self.close()
            raise
        self.connected = True
        await self._after_slc()
        self._emit("connected", ag_features=self.ag_features, operator=self.operator)

    async def close(self) -> None:
        self._writer.close()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        for task in list(self._tasks):
            task.cancel()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # -- AT command transport ---------------------------------------------

    async def send(self, command: str, expect: str | tuple[str, ...] = ()) -> list[str]:
        """Send one AT command and wait for its final result code.

        Returns the argument strings of the response lines whose name is in
        ``expect`` (e.g. ``"+CIND"``). Raises :class:`ATError` on an error
        result and :class:`asyncio.TimeoutError` if the phone doesn't answer.
        """
        prefixes = (expect,) if isinstance(expect, str) else tuple(expect)
        async with self._lock:
            if self.closed:
                raise ConnectionError("not connected")
            pending = _Pending(command, prefixes, asyncio.get_running_loop().create_future())
            self._pending = pending
            try:
                log.debug("> %s", command)
                self._writer.write((command + "\r").encode())
                await self._writer.drain()
                return await asyncio.wait_for(pending.future, self.timeout)
            finally:
                self._pending = None

    async def _try(self, command: str, expect: str | tuple[str, ...] = ()) -> list[str] | None:
        try:
            return await self.send(command, expect)
        except (ATError, asyncio.TimeoutError) as exc:
            log.info("phone did not accept %s: %s", command, exc)
            return None

    async def _read_loop(self) -> None:
        buffer = b""
        try:
            while True:
                data = await self._reader.read(1024)
                if not data:
                    break
                buffer += data
                *lines, buffer = re.split(rb"[\r\n]", buffer)
                for raw in lines:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        self._handle_line(line)
        except (ConnectionError, OSError) as exc:
            log.info("control channel error: %s", exc)
        finally:
            self.connected = False
            self._closed.set()
            pending = self._pending
            if pending is not None and not pending.future.done():
                pending.future.set_exception(ConnectionError("connection closed"))
            self._emit("disconnected")

    def _handle_line(self, line: str) -> None:
        log.debug("< %s", line)
        name, args = at.parse_line(line)
        pending = self._pending
        if pending is not None and not pending.future.done():
            if name == "OK":
                pending.future.set_result(pending.lines)
                return
            if name in at.ERROR_RESULTS:
                pending.future.set_exception(ATError(pending.command, line))
                return
            if name in pending.prefixes:
                pending.lines.append(args)
                return
        self._handle_unsolicited(name, args, line)

    # -- Service Level Connection -----------------------------------------

    async def _establish_slc(self) -> None:
        brsf = _first(await self.send(f"AT+BRSF={int(self.features)}", "+BRSF"))
        self.ag_features = AgFeature(at.parse_int_list(brsf or "0")[0])
        log.info("phone features: %s", self.ag_features)

        cind_test = _first(await self.send("AT+CIND=?", "+CIND"))
        self.indicator_names = at.parse_cind_test(cind_test)

        cind = _first(await self.send("AT+CIND?", "+CIND"))
        values = at.parse_int_list(cind)
        self.indicators = dict(zip(self.indicator_names, values))
        self._update_call_state()

        await self.send("AT+CMER=3,0,0,1")

        if self._both(HfFeature.THREE_WAY_CALLING, AgFeature.THREE_WAY_CALLING):
            self.chld_options = at.parse_chld_test(_first(await self.send("AT+CHLD=?", "+CHLD")))

    async def _after_slc(self) -> None:
        if self.features & HfFeature.CLI_PRESENTATION:
            await self._try("AT+CLIP=1")
        if self._both(HfFeature.THREE_WAY_CALLING, AgFeature.THREE_WAY_CALLING):
            await self._try("AT+CCWA=1")
        if self.ag_features & AgFeature.EXTENDED_ERRORS:
            await self._try("AT+CMEE=1")
        if self.ag_features & AgFeature.INBAND_RING:
            self.inband_ring = True
        await self.query_operator()
        if self.features & HfFeature.REMOTE_VOLUME:
            await self._try(f"AT+VGS={self.speaker_volume}")
            await self._try(f"AT+VGM={self.mic_volume}")
        if self.call_state != CallState.IDLE:
            await self._refresh_calls()

    def _both(self, hf: HfFeature, ag: AgFeature) -> bool:
        return bool(self.features & hf) and bool(self.ag_features & ag)

    # -- unsolicited results ----------------------------------------------

    def _handle_unsolicited(self, name: str, args: str, line: str) -> None:
        if name == "RING":
            self._emit("ring", number=self.number)
        elif name == "+CLIP":
            number = at.parse_clip(args)
            if number and number != self.number:
                self.number = number
                self._emit("caller_id", number=number)
        elif name == "+CCWA":
            self._emit("call_waiting", number=at.parse_clip(args))
        elif name == "+CIEV":
            values = at.parse_int_list(args)
            if len(values) >= 2 and 0 < values[0] <= len(self.indicator_names):
                indicator = self.indicator_names[values[0] - 1]
                self.indicators[indicator] = values[1]
                self._emit("indicator", name=indicator, value=values[1])
                if indicator in ("call", "callsetup", "callheld"):
                    self._update_call_state()
        elif name in ("+VGS", "+VGM"):
            level = _clamp_volume(at.parse_int_list(args)[0] if args else 0)
            if name == "+VGS":
                self.speaker_volume = level
                self._emit("volume", target="speaker", level=level)
            else:
                self.mic_volume = level
                self._emit("volume", target="microphone", level=level)
        elif name == "+BSIR":
            self.inband_ring = args.strip() == "1"
            self._emit("inband_ring", enabled=self.inband_ring)
        elif name == "+BVRA":
            self._emit("voice_recognition", enabled=args.strip().startswith("1"))
        else:
            log.debug("unhandled result: %s", line)

    def _update_call_state(self) -> None:
        state = derive_call_state(self.indicators, self.call_state)
        if state == self.call_state:
            return
        previous, self.call_state = self.call_state, state
        if state == CallState.IDLE:
            self.number = None
        self._emit("call_state", state=state, previous=previous, number=self.number)
        if self.connected and state != CallState.IDLE:
            self._spawn(self._refresh_calls())

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _refresh_calls(self) -> None:
        """Fill in the number of the current call from ``AT+CLCC`` if we can."""
        if not self._both(HfFeature.ENHANCED_CALL_STATUS, AgFeature.ENHANCED_CALL_STATUS):
            return
        try:
            calls = await self.list_calls()
        except (ATError, asyncio.TimeoutError, ConnectionError):
            return
        wanted = {
            CallState.INCOMING: "incoming",
            CallState.WAITING: "waiting",
            CallState.DIALING: "dialing",
            CallState.ALERTING: "alerting",
            CallState.ACTIVE: "active",
            CallState.HELD: "held",
        }.get(self.call_state)
        for call in calls:
            if call.status == wanted and call.number and call.number != self.number:
                self.number = call.number
                self._emit("caller_id", number=call.number)
                return

    # -- call control ------------------------------------------------------

    async def answer(self) -> None:
        await self.send("ATA")

    async def hangup(self) -> None:
        """Hang up the active call, or reject an incoming one."""
        await self.send("AT+CHUP")

    async def dial(self, number: str) -> None:
        number = number.replace(" ", "").replace("-", "")
        if not at.is_dialable(number):
            raise ValueError(f"not a dialable number: {number!r}")
        await self.send(f"ATD{number};")
        self.number = number

    async def dial_memory(self, slot: int) -> None:
        await self.send(f"ATD>{int(slot)};")

    async def redial(self) -> None:
        await self.send("AT+BLDN")

    async def send_dtmf(self, digits: str) -> None:
        for digit in digits:
            if digit not in "0123456789*#ABCDabcd":
                raise ValueError(f"not a DTMF digit: {digit!r}")
            await self.send(f"AT+VTS={digit.upper()}")

    async def call_hold(self, action: str) -> None:
        """Three-way calling control (``AT+CHLD``): 0, 1, 1x, 2, 2x, 3, 4."""
        await self.send(f"AT+CHLD={action}")

    async def voice_recognition(self, enabled: bool) -> None:
        await self.send(f"AT+BVRA={1 if enabled else 0}")

    async def set_speaker_volume(self, level: int) -> None:
        self.speaker_volume = _clamp_volume(level)
        await self.send(f"AT+VGS={self.speaker_volume}")

    async def set_mic_volume(self, level: int) -> None:
        self.mic_volume = _clamp_volume(level)
        await self.send(f"AT+VGM={self.mic_volume}")

    async def list_calls(self) -> list[at.Call]:
        lines = await self.send("AT+CLCC", "+CLCC")
        return [call for call in map(at.parse_clcc, lines) if call is not None]

    async def query_operator(self) -> str | None:
        if await self._try("AT+COPS=3,0") is None:
            return None
        lines = await self._try("AT+COPS?", "+COPS")
        if lines:
            self.operator = at.parse_cops(lines[0])
        return self.operator

    async def raw(self, command: str) -> list[str]:
        """Send an arbitrary AT command, returning its matching response lines."""
        match = re.match(r"AT(\+\w+)", command, re.IGNORECASE)
        expect = (match.group(1).upper(),) if match else ()
        return await self.send(command, expect)


def _first(lines: list[str]) -> str:
    return lines[0] if lines else ""


def _clamp_volume(level: int) -> int:
    return max(0, min(15, int(level)))
