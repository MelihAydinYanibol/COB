"""Wi-Fi link to the COB Android companion app.

The phone app connects to COB over the local network and gives call control
(ringing, caller ID, answer, reject, hang up, dial) plus battery and signal
status. Android doesn't let apps carry call audio, so over Wi-Fi the
conversation stays on the phone. Bluetooth is still used for audio when
it's connected.

Security:

* Everything runs over TLS. COB generates a self-signed certificate on first
  use, and the phone pins its SHA-256 fingerprint, so nobody on the network
  can read or impersonate the link.
* Pairing: ``pair`` shows a QR code holding the PC's address, the certificate
  fingerprint and a one-time random token (valid for 10 minutes). The app
  scans it, connects, and trades the token for a random per-device secret.
  COB stores only a hash of that secret.
* Later connections authenticate with the device secret inside the pinned
  TLS session.

Wire protocol: one JSON object per line, in both directions.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import secrets
import socket
import ssl
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from .hfp import CallState

log = logging.getLogger(__name__)

DEFAULT_PORT = 7480
DISCOVERY_REQUEST = b"COB-DISCOVER"
PROTOCOL_VERSION = 1
PAIRING_TTL = 600  # seconds
HANDSHAKE_TIMEOUT = 15
IDLE_TIMEOUT = 75  # the app pings every 25 s
COMMAND_TIMEOUT = 10
RING_INTERVAL = 4
MAX_LINE = 64 * 1024


class WifiCommandError(Exception):
    pass


class NotSupportedOverWifi(Exception):
    pass


# -- certificate -----------------------------------------------------------


def ensure_certificate(directory: Path) -> tuple[Path, Path, str]:
    """Return (cert, key, sha256 fingerprint), creating a certificate if needed."""
    cert_path = directory / "wifi-cert.pem"
    key_path = directory / "wifi-key.pem"
    if not (cert_path.exists() and key_path.exists()):
        _create_certificate(cert_path, key_path)
    return cert_path, key_path, certificate_fingerprint(cert_path.read_bytes())


def certificate_fingerprint(cert_pem: bytes) -> str:
    der = ssl.PEM_cert_to_DER_cert(cert_pem.decode())
    return hashlib.sha256(der).hexdigest()


def _create_certificate(cert_path: Path, key_path: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "COB")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path.touch(mode=0o600)
    key_path.write_bytes(key_bytes)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


# -- paired devices --------------------------------------------------------


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class DeviceStore:
    """Paired phones, kept in a JSON file. Only secret hashes are stored."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.devices: dict[str, dict] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.devices = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600)
        self.path.write_text(json.dumps(self.devices, indent=2))

    def add(self, name: str) -> tuple[str, str]:
        device_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        self.devices[device_id] = {
            "name": name,
            "secret_sha256": _hash_secret(secret),
            "paired": datetime.date.today().isoformat(),
        }
        self._save()
        return device_id, secret

    def verify(self, device_id: str, secret: str) -> str | None:
        device = self.devices.get(device_id)
        if device is None:
            _hash_secret(secret)  # keep timing similar for unknown ids
            return None
        if hmac.compare_digest(device["secret_sha256"], _hash_secret(secret)):
            return device["name"]
        return None

    def remove(self, key: str) -> str | None:
        """Remove by device id or (case-insensitive) name. Returns the name."""
        for device_id, device in list(self.devices.items()):
            if key == device_id or key.lower() == device["name"].lower():
                del self.devices[device_id]
                self._save()
                return device["name"]
        return None


# -- pairing helpers -------------------------------------------------------


def local_ip() -> str:
    """Best guess at this PC's address on the local network."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # no packets are sent for UDP connect
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def pairing_uri(host: str, port: int, fingerprint: str, token: str, name: str) -> str:
    query = urlencode({"h": host, "p": port, "f": fingerprint, "t": token, "n": name})
    return f"cob://pair?{query}"


def render_qr(text: str) -> str:
    import io

    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(text)
    out = io.StringIO()
    qr.print_ascii(out=out, invert=True)
    return out.getvalue()


# -- the phone, as seen over Wi-Fi ----------------------------------------


class WifiPhone:
    """A connected companion app. Mirrors the call control API of HandsFree.

    Events: ``connected``, ``disconnected``, ``ring``, ``caller_id``,
    ``call_state``, ``status``.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        device_id: str,
        name: str,
    ):
        self._reader = reader
        self._writer = writer
        self.device_id = device_id
        self.name = name
        self.address = (writer.get_extra_info("peername") or ("?",))[0]
        self.call_state = CallState.IDLE
        self.number: str | None = None
        self.status: dict = {}
        self.connected = True
        self.last_dialed: str | None = None
        self._listeners: list[Callable[[str, dict], None]] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._closed = asyncio.Event()
        self._ring_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None

    def add_listener(self, listener: Callable[[str, dict], None]) -> None:
        self._listeners.append(listener)

    def _emit(self, event: str, **data) -> None:
        for listener in list(self._listeners):
            try:
                listener(event, data)
            except Exception:
                log.exception("listener failed on %s", event)

    def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())
        self._emit("connected")

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        self._writer.close()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

    def _send(self, message: dict) -> None:
        if not self._writer.is_closing():
            self._writer.write(json.dumps(message).encode() + b"\n")

    # -- incoming messages --------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await asyncio.wait_for(self._reader.readline(), IDLE_TIMEOUT)
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    log.info("bad message from %s", self.name)
                    continue
                if isinstance(message, dict):
                    self._handle(message)
        except (asyncio.TimeoutError, ConnectionError, OSError, ValueError) as exc:
            log.info("Wi-Fi link to %s closed: %s", self.name, exc)
        finally:
            self.connected = False
            self._closed.set()
            self._set_ringing(False)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("phone disconnected"))
            self._pending.clear()
            self._writer.close()
            self._emit("disconnected")

    def _handle(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "ping":
            self._send({"type": "pong"})
        elif kind == "result":
            future = self._pending.pop(message.get("id"), None)
            if future is not None and not future.done():
                if message.get("ok"):
                    future.set_result(message)
                else:
                    future.set_exception(WifiCommandError(message.get("error") or "failed"))
        elif kind == "call":
            self._on_call(str(message.get("state", "")), message.get("number") or None)
        elif kind == "status":
            self.status = {k: v for k, v in message.items() if k != "type"}
            self._emit("status", **self.status)

    def _on_call(self, state: str, number: str | None) -> None:
        if number and number != self.number:
            self.number = number
            if state == "ringing":
                self._emit("caller_id", number=number)
        if state == "ringing":
            new = CallState.INCOMING
        elif state == "offhook":
            new = CallState.ACTIVE
        else:
            new = CallState.IDLE
        if new != self.call_state:
            previous, self.call_state = self.call_state, new
            if new == CallState.IDLE:
                self.number = None
            self._emit("call_state", state=new, previous=previous, number=self.number)
        self._set_ringing(new == CallState.INCOMING)

    def _set_ringing(self, ringing: bool) -> None:
        """Android reports ringing once; repeat it like a hands-free RING."""
        if ringing and self._ring_task is None:
            self._ring_task = asyncio.create_task(self._ring())
        elif not ringing and self._ring_task is not None:
            self._ring_task.cancel()
            self._ring_task = None

    async def _ring(self) -> None:
        while True:
            self._emit("ring", number=self.number)
            await asyncio.sleep(RING_INTERVAL)

    # -- commands ------------------------------------------------------------

    async def _request(self, action: str, **params) -> dict:
        if self.closed:
            raise ConnectionError("not connected")
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._send({"type": "command", "id": request_id, "action": action, **params})
        try:
            return await asyncio.wait_for(future, COMMAND_TIMEOUT)
        finally:
            self._pending.pop(request_id, None)

    async def answer(self) -> None:
        await self._request("answer")

    async def hangup(self) -> None:
        await self._request("hangup")

    async def dial(self, number: str) -> None:
        from .at import is_dialable

        number = number.replace(" ", "").replace("-", "")
        if not is_dialable(number):
            raise ValueError(f"not a dialable number: {number!r}")
        await self._request("dial", number=number)
        self.last_dialed = self.number = number

    async def redial(self) -> None:
        if not self.last_dialed:
            raise NotSupportedOverWifi("nothing dialed from COB yet")
        await self.dial(self.last_dialed)

    async def refresh_status(self) -> dict:
        await self._request("status")
        return self.status


# -- server ------------------------------------------------------------------


class _DiscoveryResponder(asyncio.DatagramProtocol):
    def __init__(self, reply: bytes):
        self._reply = reply
        self._transport = None

    def connection_made(self, transport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if data.strip() == DISCOVERY_REQUEST and self._transport is not None:
            self._transport.sendto(self._reply, addr)


class WifiServer:
    """Accepts encrypted connections from the companion app."""

    def __init__(
        self,
        on_phone: Callable[[WifiPhone], None],
        directory: Path,
        port: int = DEFAULT_PORT,
        bind: str = "0.0.0.0",
        name: str | None = None,
    ):
        self._on_phone = on_phone
        self.directory = directory
        self.port = port
        self.bind = bind
        self.name = name or socket.gethostname()
        self.devices = DeviceStore(directory / "wifi-devices.json")
        self.fingerprint = ""
        self._server: asyncio.base_events.Server | None = None
        self._discovery = None
        self._pairing_token: str | None = None
        self._pairing_expires = 0.0

    async def start(self) -> None:
        cert, key, self.fingerprint = ensure_certificate(self.directory)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        self._server = await asyncio.start_server(
            self._handle, self.bind, self.port, ssl=context, limit=MAX_LINE
        )
        self.port = self._server.sockets[0].getsockname()[1]
        reply = json.dumps(
            {"cob": PROTOCOL_VERSION, "port": self.port, "fp": self.fingerprint[:16], "name": self.name}
        ).encode()
        try:
            self._discovery, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _DiscoveryResponder(reply), local_addr=(self.bind, self.port)
            )
        except OSError as exc:
            log.warning("Wi-Fi discovery unavailable: %s", exc)

    async def close(self) -> None:
        if self._discovery is not None:
            self._discovery.close()
        if self._server is not None:
            self._server.close()

    def start_pairing(self, host: str | None = None) -> str:
        """Open a 10 minute pairing window. Returns the URI for the QR code."""
        self._pairing_token = secrets.token_urlsafe(16)
        self._pairing_expires = time.monotonic() + PAIRING_TTL
        return pairing_uri(host or local_ip(), self.port, self.fingerprint, self._pairing_token, self.name)

    def _take_pairing_token(self, token: str) -> bool:
        expected = self._pairing_token
        if not expected or time.monotonic() > self._pairing_expires:
            return False
        if not hmac.compare_digest(expected, token):
            return False
        self._pairing_token = None  # single use
        return True

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            line = await asyncio.wait_for(reader.readline(), HANDSHAKE_TIMEOUT)
            message = json.loads(line) if line else {}
            if not isinstance(message, dict):
                raise ValueError("not an object")
        except (asyncio.TimeoutError, ValueError, ConnectionError, OSError) as exc:
            log.info("Wi-Fi handshake with %s failed: %s", peer, exc)
            writer.close()
            return

        phone_name = str(message.get("name") or "Android phone")[:64]
        reply: dict
        if message.get("type") == "pair":
            if not self._take_pairing_token(str(message.get("token", ""))):
                await self._refuse(writer, "Pairing code is wrong or expired. Run 'pair' on the PC again.")
                return
            device_id, secret = self.devices.add(phone_name)
            reply = {"type": "paired", "device_id": device_id, "secret": secret}
            log.info("paired %s (%s)", phone_name, device_id)
        elif message.get("type") == "hello":
            device_id = str(message.get("device_id", ""))
            stored_name = self.devices.verify(device_id, str(message.get("secret", "")))
            if stored_name is None:
                await self._refuse(writer, "This phone is not paired with the PC. Pair it again.")
                return
            reply = {"type": "welcome"}
        else:
            await self._refuse(writer, "Unexpected message.")
            return

        reply.update(name=self.name, version=PROTOCOL_VERSION)
        writer.write(json.dumps(reply).encode() + b"\n")
        phone = WifiPhone(reader, writer, device_id, phone_name)
        self._on_phone(phone)

    async def _refuse(self, writer: asyncio.StreamWriter, text: str) -> None:
        log.warning("refused Wi-Fi connection from %s: %s", writer.get_extra_info("peername"), text)
        writer.write(json.dumps({"type": "error", "text": text}).encode() + b"\n")
        try:
            await writer.drain()
        finally:
            writer.close()
