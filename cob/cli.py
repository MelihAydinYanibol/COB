"""COB command line app: take your phone calls on your PC."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shlex
import sys
import threading

from . import __version__
from .audio import AudioBridge, list_devices, play_ringtone
from .backends import Backend, create_backend, default_backend
from .hfp import DEFAULT_HF_FEATURES, AgFeature, ATError, CallState, HandsFree

log = logging.getLogger("cob")

HELP = """\
Commands:
  a, answer            answer the incoming call
  h, hangup            hang up, or reject an incoming call
  d, dial <number>     call a number
  r, redial            call the last dialed number again
  t, dtmf <digits>     send key presses during a call (0-9 * #)
  x, audio             move call audio between the PC and the phone
  m, mute              mute / unmute the PC microphone
  vol <0-15>           speaker volume
  mic <0-15>           microphone gain reported to the phone
  hold <0|1|2|3|1x|2x> three-way calling (1 = end active and accept other,
                       2 = swap / hold, 3 = merge into conference)
  vr [on|off]          start / stop the phone's voice assistant
  calls                list the phone's current calls
  s, status            connection and phone status
  connect <address>    connect to a paired phone
  at <command>         send a raw AT command, e.g. "at AT+CBC?"
  q, quit              exit
"""

STATE_MESSAGES = {
    CallState.DIALING: "Dialing {who}...",
    CallState.ALERTING: "Ringing {who}...",
    CallState.ACTIVE: "Call active with {who}.",
    CallState.HELD: "Call with {who} is on hold.",
    CallState.WAITING: "Another call is waiting from {who}. 'hold 1' to switch, 'hold 0' to reject.",
    CallState.IDLE: "Call ended.",
}


def say(message: str) -> None:
    print(f"* {message}", flush=True)


class App:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.features = DEFAULT_HF_FEATURES
        self.backend: Backend = create_backend(args.backend, self, self.features, args)
        self.hf: HandsFree | None = None
        self.phone_address: str | None = None
        self.phone_name: str | None = None
        self.bridge: AudioBridge | None = None
        self.muted = False
        self._tasks: set[asyncio.Task] = set()
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()

    # -- main loop ---------------------------------------------------------

    async def run(self) -> None:
        await self.backend.start()
        say(f"COB {__version__} ready on {self.backend.describe()}.")
        if self.args.device:
            self._spawn(self._connect(self.args.device))
        else:
            say("Waiting for a phone. Connect from the phone's Bluetooth settings, or use 'connect <address>'.")
        say("Type 'help' for commands.")
        self._start_input_thread()
        try:
            while True:
                line = await self._lines.get()
                if line is None:
                    break
                if not await self._command(line):
                    break
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        self._stop_bridge()
        if self.hf is not None:
            await self.hf.close()
        await self.backend.stop()

    def _start_input_thread(self) -> None:
        loop = asyncio.get_running_loop()

        def read_lines() -> None:
            for line in sys.stdin:
                loop.call_soon_threadsafe(self._lines.put_nowait, line)
            loop.call_soon_threadsafe(self._lines.put_nowait, None)

        threading.Thread(target=read_lines, name="stdin", daemon=True).start()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _connect(self, address: str) -> None:
        say(f"Connecting to {address}...")
        try:
            await self.backend.connect(address)
        except Exception as exc:
            say(f"Could not connect to {address}: {exc}")

    # -- BackendListener ---------------------------------------------------

    def control_connected(self, address, name, reader, writer) -> bool:
        if self.hf is not None and not self.hf.closed:
            log.info("refusing %s: already connected to %s", address, self.phone_address)
            return False
        hf = HandsFree(reader, writer, self.features)
        hf.add_listener(self._on_event)
        self.hf = hf
        self.phone_address = address
        self.phone_name = name or address
        self._spawn(self._run_hf(hf))
        return True

    async def _run_hf(self, hf: HandsFree) -> None:
        try:
            await hf.start()
        except (ATError, asyncio.TimeoutError, ConnectionError) as exc:
            say(f"Could not set up the hands-free link with {self.phone_name}: {exc}")
            return
        await hf.wait_closed()

    def audio_connected(self, address: str) -> AudioBridge | None:
        if address != self.phone_address:
            log.info("refusing audio from %s", address)
            return None
        self._stop_bridge()
        bridge = AudioBridge(self.args.input_device, self.args.output_device, self.args.audio_rate)
        try:
            bridge.start()
        except Exception as exc:
            say(f"Could not open the PC's audio devices: {exc}")
            return None
        if self.hf is not None:
            bridge.set_volume(self.hf.speaker_volume)
        bridge.muted = self.muted
        self.bridge = bridge
        say("Call audio is on the PC.")
        return bridge

    def audio_disconnected(self) -> None:
        if self.bridge is not None:
            self._stop_bridge()
            say("Call audio is on the phone.")

    def _stop_bridge(self) -> None:
        bridge, self.bridge = self.bridge, None
        if bridge is not None:
            bridge.stop()

    # -- phone events ------------------------------------------------------

    def _who(self) -> str:
        return (self.hf.number if self.hf else None) or "unknown number"

    def _on_event(self, event: str, data: dict) -> None:
        hf = self.hf
        if event == "connected":
            details = f" ({data['operator']})" if data.get("operator") else ""
            say(f"Connected to {self.phone_name}{details}.")
        elif event == "disconnected":
            say(f"{self.phone_name} disconnected.")
            self._stop_bridge()
        elif event == "ring":
            say(f"Incoming call from {self._who()}. 'a' to answer, 'h' to reject.")
            if self.args.ringtone and self.bridge is None and not (hf and hf.inband_ring):
                try:
                    play_ringtone(self.args.output_device)
                except Exception:
                    log.debug("ringtone failed", exc_info=True)
        elif event == "caller_id":
            if hf and hf.call_state in (CallState.INCOMING, CallState.WAITING):
                say(f"Caller: {data['number']}")
        elif event == "call_state":
            template = STATE_MESSAGES.get(data["state"])
            if template:
                say(template.format(who=self._who()))
        elif event == "volume" and data["target"] == "speaker":
            if self.bridge is not None:
                self.bridge.set_volume(data["level"])

    # -- commands ----------------------------------------------------------

    async def _command(self, line: str) -> bool:
        try:
            words = shlex.split(line)
        except ValueError:
            words = line.split()
        if not words:
            return True
        name, args = words[0].lower(), words[1:]
        if name in ("q", "quit", "exit"):
            return False
        if name in ("help", "?"):
            print(HELP, end="")
            return True
        if name == "connect":
            if not args:
                say("Usage: connect <address>")
            else:
                self._spawn(self._connect(args[0]))
            return True

        hf = self.hf
        if hf is None or not hf.connected:
            say("No phone connected.")
            return True
        try:
            await self._phone_command(hf, name, args)
        except ATError as exc:
            say(f"The phone refused that: {exc.response}")
        except asyncio.TimeoutError:
            say("The phone did not answer.")
        except (ValueError, IndexError) as exc:
            say(f"Invalid input: {exc}")
        except Exception as exc:
            say(f"Failed: {exc}")
        return True

    async def _phone_command(self, hf: HandsFree, name: str, args: list[str]) -> None:
        if name in ("a", "answer"):
            await hf.answer()
        elif name in ("h", "hangup"):
            await hf.hangup()
        elif name in ("d", "dial"):
            await hf.dial("".join(args))
        elif name in ("r", "redial"):
            await hf.redial()
        elif name in ("t", "dtmf"):
            await hf.send_dtmf("".join(args))
        elif name in ("x", "audio"):
            await self._toggle_audio()
        elif name in ("m", "mute"):
            self.muted = not self.muted
            if self.bridge is not None:
                self.bridge.muted = self.muted
            say("Microphone muted." if self.muted else "Microphone on.")
        elif name == "vol":
            await hf.set_speaker_volume(int(args[0]))
            if self.bridge is not None:
                self.bridge.set_volume(hf.speaker_volume)
        elif name == "mic":
            await hf.set_mic_volume(int(args[0]))
        elif name == "hold":
            await hf.call_hold(args[0])
        elif name == "vr":
            await hf.voice_recognition(not args or args[0].lower() in ("on", "1", "start"))
        elif name == "calls":
            calls = await hf.list_calls()
            if not calls:
                say("No calls.")
            for call in calls:
                direction = "outgoing" if call.outgoing else "incoming"
                conference = ", conference" if call.multiparty else ""
                say(f"#{call.index} {call.number or 'unknown'}: {call.status}, {direction}{conference}")
        elif name in ("s", "status"):
            self._print_status(hf)
        elif name == "at":
            for response in await hf.raw(" ".join(args)):
                say(response)
            say("OK")
        else:
            say(f"Unknown command {name!r}. Type 'help'.")

    async def _toggle_audio(self) -> None:
        if self.bridge is not None:
            await self.backend.close_audio()
        else:
            assert self.phone_address is not None
            await self.backend.open_audio(self.phone_address)

    def _print_status(self, hf: HandsFree) -> None:
        ind = hf.indicators
        say(f"Phone: {self.phone_name} [{self.phone_address}]")
        if hf.operator:
            say(f"Operator: {hf.operator}")
        parts = []
        if "service" in ind:
            parts.append("network " + ("available" if ind["service"] else "unavailable"))
        if "signal" in ind:
            parts.append(f"signal {ind['signal']}/5")
        if "battchg" in ind:
            parts.append(f"battery {ind['battchg']}/5")
        if ind.get("roam"):
            parts.append("roaming")
        if parts:
            say(", ".join(parts))
        call = hf.call_state.value + (f" ({hf.number})" if hf.number else "")
        say(f"Call: {call}; audio on {'PC' if self.bridge else 'phone'}"
            + ("; mic muted" if self.muted else ""))
        say(f"Volume: speaker {hf.speaker_volume}/15, mic {hf.mic_volume}/15")
        if hf.ag_features & AgFeature.INBAND_RING:
            say("Phone sends its own ringtone over Bluetooth.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cob",
        description="CallOverBluetooth: use your PC as a Bluetooth hands-free kit for your phone.",
    )
    parser.add_argument("--version", action="version", version=f"cob {__version__}")
    parser.add_argument(
        "--backend",
        choices=("bluez", "bumble"),
        default=default_backend(),
        help="Bluetooth stack: 'bluez' (Linux) or 'bumble' (Windows, or Linux with a "
        "dedicated USB dongle). Default: %(default)s",
    )
    parser.add_argument("-d", "--device", help="Bluetooth address of a paired phone to connect to")
    parser.add_argument("--adapter", default="hci0", help="BlueZ adapter (default: %(default)s)")
    parser.add_argument(
        "--transport",
        help="Bumble HCI transport (default: usb:0+sco=0, the first USB dongle with SCO audio)",
    )
    parser.add_argument("--name", help="Bluetooth name shown to the phone (Bumble backend)")
    parser.add_argument("--keystore", help="file for pairing keys (Bumble backend)")
    parser.add_argument("--input-device", help="microphone (index or name, see --list-audio)")
    parser.add_argument("--output-device", help="speakers (index or name, see --list-audio)")
    parser.add_argument(
        "--audio-rate",
        type=int,
        default=48000,
        help="sample rate for the PC audio devices, a multiple of 8000 (default: %(default)s)",
    )
    parser.add_argument("--list-audio", action="store_true", help="list audio devices and exit")
    parser.add_argument(
        "--no-ringtone", dest="ringtone", action="store_false", help="don't ring on the PC"
    )
    parser.add_argument("-v", "--verbose", action="count", default=0, help="more logging (-vv for AT traffic)")
    return parser


def _device_arg(value: str | None):
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = (logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)]
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s: %(message)s")

    if args.list_audio:
        print(list_devices())
        return 0
    if args.audio_rate <= 0 or args.audio_rate % 8000:
        print("--audio-rate must be a multiple of 8000", file=sys.stderr)
        return 2
    if args.backend == "bluez" and not sys.platform.startswith("linux"):
        print("The bluez backend only works on Linux; use --backend bumble.", file=sys.stderr)
        return 2
    args.input_device = _device_arg(args.input_device)
    args.output_device = _device_arg(args.output_device)

    async def run() -> None:
        await App(args).run()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log.debug("fatal error", exc_info=True)
        print(f"cob: {exc}", file=sys.stderr)
        return 1
    return 0
