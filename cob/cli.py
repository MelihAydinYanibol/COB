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
from .config import config_dir
from .hfp import DEFAULT_HF_FEATURES, AgFeature, ATError, CallState, HandsFree
from .wifi import (
    DEFAULT_PORT,
    NotSupportedOverWifi,
    WifiCommandError,
    WifiPhone,
    WifiServer,
    render_qr,
)

log = logging.getLogger("cob")

HELP = """\
Commands:
  a, answer            answer the incoming call
  h, hangup            hang up, or reject an incoming call
  d, dial <number>     call a number
  r, redial            call the last dialed number again
  s, status            connection and phone status
  connect <address>    connect to a paired phone over Bluetooth
  pair                 show a QR code to pair the COB Android app (Wi-Fi)
  devices              list phones paired over Wi-Fi
  unpair <name|id>     forget a phone paired over Wi-Fi
  q, quit              exit
Bluetooth only:
  t, dtmf <digits>     send key presses during a call (0-9 * #)
  x, audio             move call audio between the PC and the phone
  m, mute              mute / unmute the PC microphone
  vol <0-15>           speaker volume
  mic <0-15>           microphone gain reported to the phone
  hold <0|1|2|3|1x|2x> three-way calling (1 = end active and accept other,
                       2 = swap / hold, 3 = merge into conference)
  vr [on|off]          start / stop the phone's voice assistant
  calls                list the phone's current calls
  at <command>         send a raw AT command, e.g. "at AT+CBC?"
"""

BLUETOOTH_ONLY = {"t", "dtmf", "x", "audio", "m", "mute", "vol", "mic", "hold", "vr", "calls", "at"}

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
        self.wifi_server: WifiServer | None = None
        self.wifi: WifiPhone | None = None
        self.bridge: AudioBridge | None = None
        self.muted = False
        self._tasks: set[asyncio.Task] = set()
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()

    @property
    def phone(self) -> HandsFree | WifiPhone | None:
        """The link used for call control: Bluetooth when it's up, else Wi-Fi."""
        if self.hf is not None and self.hf.connected:
            return self.hf
        if self.wifi is not None and self.wifi.connected:
            return self.wifi
        return None

    # -- main loop ---------------------------------------------------------

    async def run(self) -> None:
        await self.backend.start()
        say(f"COB {__version__} ready on {self.backend.describe()}.")
        if self.args.wifi:
            self.wifi_server = WifiServer(
                self._on_wifi_phone, config_dir(), self.args.wifi_port, self.args.wifi_bind
            )
            await self.wifi_server.start()
            say(f"Wi-Fi link open on port {self.wifi_server.port}. Type 'pair' to pair the COB Android app.")
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
        if self.wifi is not None:
            await self.wifi.close()
        if self.wifi_server is not None:
            await self.wifi_server.close()
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
        hf.add_listener(lambda event, data: self._on_event("bluetooth", event, data))
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

    # -- Wi-Fi -------------------------------------------------------------

    def _on_wifi_phone(self, phone: WifiPhone) -> None:
        old = self.wifi
        if old is not None and not old.closed:
            self._spawn(old.close())  # the same phone reconnected, or a new one took over
        self.wifi = phone

        def on_event(event: str, data: dict) -> None:
            if phone is self.wifi:  # ignore a replaced connection winding down
                self._on_event("wifi", event, data)

        phone.add_listener(on_event)
        phone.start()

    def _pair(self) -> None:
        if self.wifi_server is None:
            say("The Wi-Fi link is off. Restart COB with --wifi.")
            return
        uri = self.wifi_server.start_pairing(self.args.wifi_host)
        print(render_qr(uri), end="")
        say("In the COB app, tap 'Pair with PC' and scan this code (valid for 10 minutes).")
        say(f"Or paste this into the app: {uri}")

    def _list_devices(self) -> None:
        if self.wifi_server is None:
            say("The Wi-Fi link is off. Restart COB with --wifi.")
            return
        devices = self.wifi_server.devices.devices
        if not devices:
            say("No phones paired over Wi-Fi. Use 'pair'.")
        for device_id, device in devices.items():
            online = self.wifi is not None and self.wifi.connected and self.wifi.device_id == device_id
            say(f"{device['name']} [{device_id}] paired {device['paired']}" + (", connected" if online else ""))

    def _unpair(self, key: str) -> None:
        if self.wifi_server is None:
            say("The Wi-Fi link is off. Restart COB with --wifi.")
            return
        name = self.wifi_server.devices.remove(key)
        if name is None:
            say(f"No paired phone called {key!r}. See 'devices'.")
            return
        wifi = self.wifi
        if wifi is not None and wifi.device_id not in self.wifi_server.devices.devices:
            self._spawn(wifi.close())
        say(f"Forgot {name}.")

    # -- phone events ------------------------------------------------------

    def _who(self, link) -> str:
        return (link.number if link else None) or "unknown number"

    def _on_event(self, source: str, event: str, data: dict) -> None:
        bluetooth_up = self.hf is not None and self.hf.connected
        if source == "wifi" and bluetooth_up and event in ("ring", "caller_id", "call_state"):
            return  # Bluetooth is connected and reports the same call
        link = self.hf if source == "bluetooth" else self.wifi
        if event == "connected":
            if source == "bluetooth":
                details = f" ({data['operator']})" if data.get("operator") else ""
                say(f"Connected to {self.phone_name}{details} over Bluetooth.")
            else:
                say(f"Connected to {self.wifi.name} over Wi-Fi ({self.wifi.address}).")
        elif event == "disconnected":
            if source == "bluetooth":
                say(f"{self.phone_name} disconnected from Bluetooth.")
                self._stop_bridge()
                if self.wifi is not None and self.wifi.connected:
                    say("Still connected over Wi-Fi: call control works, audio stays on the phone.")
            else:
                say(f"{link.name} disconnected from Wi-Fi.")
        elif event == "ring":
            say(f"Incoming call from {self._who(link)}. 'a' to answer, 'h' to reject.")
            inband = source == "bluetooth" and self.hf.inband_ring
            if self.args.ringtone and self.bridge is None and not inband:
                try:
                    play_ringtone(self.args.output_device)
                except Exception:
                    log.debug("ringtone failed", exc_info=True)
        elif event == "caller_id":
            if link and link.call_state in (CallState.INCOMING, CallState.WAITING):
                say(f"Caller: {data['number']}")
        elif event == "call_state":
            template = STATE_MESSAGES.get(data["state"])
            if source == "wifi" and data["state"] == CallState.ACTIVE:
                template = "Call in progress with {who} (audio on the phone)."
            if template:
                say(template.format(who=self._who(link)))
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
        if name == "pair":
            self._pair()
            return True
        if name == "devices":
            self._list_devices()
            return True
        if name == "unpair":
            if not args:
                say("Usage: unpair <name|id>")
            else:
                self._unpair(" ".join(args))
            return True

        phone = self.phone
        if phone is None:
            say("No phone connected.")
            return True
        if isinstance(phone, WifiPhone) and name in BLUETOOTH_ONLY:
            say(f"'{name}' needs the Bluetooth connection. Over Wi-Fi COB can answer, "
                "reject, hang up, dial and redial.")
            return True
        try:
            await self._phone_command(phone, name, args)
        except ATError as exc:
            say(f"The phone refused that: {exc.response}")
        except WifiCommandError as exc:
            say(f"The phone refused that: {exc}")
        except NotSupportedOverWifi as exc:
            say(f"Not possible over Wi-Fi: {exc}")
        except asyncio.TimeoutError:
            say("The phone did not answer.")
        except (ValueError, IndexError) as exc:
            say(f"Invalid input: {exc}")
        except Exception as exc:
            say(f"Failed: {exc}")
        return True

    async def _phone_command(self, phone, name: str, args: list[str]) -> None:
        if name in ("a", "answer"):
            await phone.answer()
        elif name in ("h", "hangup"):
            await phone.hangup()
        elif name in ("d", "dial"):
            await phone.dial("".join(args))
        elif name in ("r", "redial"):
            await phone.redial()
        elif name in ("s", "status"):
            await self._print_status()
        else:
            await self._bluetooth_command(phone, name, args)

    async def _bluetooth_command(self, hf: HandsFree, name: str, args: list[str]) -> None:
        if name in ("t", "dtmf"):
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

    async def _print_status(self) -> None:
        hf, wifi = self.hf, self.wifi
        if hf is not None and hf.connected:
            self._print_bluetooth_status(hf)
        else:
            say("Bluetooth: not connected.")
        if wifi is not None and wifi.connected:
            try:
                await wifi.refresh_status()
            except (WifiCommandError, asyncio.TimeoutError, ConnectionError):
                pass
            self._print_wifi_status(wifi, show_call=not (hf is not None and hf.connected))
        elif self.wifi_server is not None:
            say("Wi-Fi: no phone connected.")

    def _print_bluetooth_status(self, hf: HandsFree) -> None:
        ind = hf.indicators
        say(f"Bluetooth: {self.phone_name} [{self.phone_address}]")
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

    def _print_wifi_status(self, wifi: WifiPhone, show_call: bool) -> None:
        say(f"Wi-Fi: {wifi.name} [{wifi.address}]")
        status = wifi.status
        parts = []
        if status.get("operator"):
            parts.append(str(status["operator"]))
        if "service" in status:
            parts.append("network " + ("available" if status["service"] else "unavailable"))
        if status.get("signal") is not None:
            parts.append(f"signal {status['signal']}/4")
        if status.get("battery") is not None:
            charging = ", charging" if status.get("charging") else ""
            parts.append(f"battery {status['battery']}%{charging}")
        if parts:
            say(", ".join(parts))
        if show_call:
            call = wifi.call_state.value + (f" ({wifi.number})" if wifi.number else "")
            say(f"Call: {call}; audio on the phone")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cob",
        description="CallOverBluetooth: use your PC as a hands-free kit for your phone.",
    )
    parser.add_argument("--version", action="version", version=f"cob {__version__}")
    parser.add_argument(
        "--backend",
        choices=("bluez", "bumble", "none"),
        default=default_backend(),
        help="Bluetooth stack: 'bluez' (Linux), 'bumble' (Windows, or Linux with a "
        "dedicated USB dongle) or 'none' (Wi-Fi only). Default: %(default)s",
    )
    parser.add_argument("-d", "--device", help="Bluetooth address of a paired phone to connect to")
    parser.add_argument("--adapter", default="hci0", help="BlueZ adapter (default: %(default)s)")
    parser.add_argument(
        "--transport",
        help="Bumble HCI transport (default: usb:0+sco=0, the first USB dongle with SCO audio)",
    )
    parser.add_argument("--name", help="Bluetooth name shown to the phone (Bumble backend)")
    parser.add_argument("--keystore", help="file for pairing keys (Bumble backend)")
    parser.add_argument(
        "--wifi", action="store_true", help="accept the COB Android app over the local network"
    )
    parser.add_argument(
        "--wifi-port", type=int, default=DEFAULT_PORT, help="Wi-Fi link port (default: %(default)s)"
    )
    parser.add_argument(
        "--wifi-bind", default="0.0.0.0", help="address to listen on (default: all interfaces)"
    )
    parser.add_argument(
        "--wifi-host", help="address the phone should use, if COB guesses this PC's address wrong"
    )
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
    if args.backend == "none" and not args.wifi:
        print("With --backend none, add --wifi to use the COB Android app.", file=sys.stderr)
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
