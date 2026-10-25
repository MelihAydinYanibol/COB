"""Linux backend: registers with the BlueZ daemon over D-Bus.

BlueZ handles pairing, SDP and RFCOMM. When a phone connects it passes us the
RFCOMM socket through ``org.bluez.Profile1.NewConnection``. Call audio arrives
on kernel SCO sockets, which with CVSD carry plain 8 kHz PCM.
"""

import asyncio
import logging
import socket
import struct
import threading
from typing import Callable

from dbus_fast import BusType, DBusError, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

from ..audio import AudioBridge
from ..hfp import HfFeature, sdp_features
from . import Backend, BackendListener

log = logging.getLogger(__name__)

HFP_HF_UUID = "0000111e-0000-1000-8000-00805f9b34fb"
HFP_AG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"
PROFILE_PATH = "/org/cob/handsfree"
HFP_VERSION = 0x0107

SOL_BLUETOOTH = 274
BT_VOICE = 11
BT_VOICE_CVSD_16BIT = 0x0060
BDADDR_ANY = "00:00:00:00:00:00"


def address_from_path(path: str) -> str:
    """``/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF`` -> ``AA:BB:CC:DD:EE:FF``."""
    return path.rsplit("/", 1)[-1].removeprefix("dev_").replace("_", ":").upper()


def device_path(adapter: str, address: str) -> str:
    return f"/org/bluez/{adapter}/dev_{address.upper().replace(':', '_')}"


class HandsFreeProfile(ServiceInterface):
    """``org.bluez.Profile1``: BlueZ calls this when a phone connects."""

    def __init__(
        self,
        on_connection: Callable[[str, int], None],
        on_disconnect_request: Callable[[str], None],
    ):
        super().__init__("org.bluez.Profile1")
        self._on_connection = on_connection
        self._on_disconnect_request = on_disconnect_request

    @method()
    def Release(self):
        log.info("BlueZ released the profile")

    @method()
    def NewConnection(self, device: "o", fd: "h", properties: "a{sv}"):
        log.info("new HFP connection from %s", device)
        self._on_connection(device, fd)

    @method()
    def RequestDisconnection(self, device: "o"):
        log.info("BlueZ asked to disconnect %s", device)
        self._on_disconnect_request(device)


def _sco_socket() -> socket.socket:
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_SCO)
    try:
        sock.setsockopt(SOL_BLUETOOTH, BT_VOICE, struct.pack("H", BT_VOICE_CVSD_16BIT))
    except OSError:
        pass  # older kernels only do CVSD anyway
    return sock


class BlueZBackend(Backend):
    def __init__(self, listener: BackendListener, features: HfFeature, adapter: str = "hci0"):
        super().__init__(listener, features)
        self.adapter = adapter
        self._bus: MessageBus | None = None
        self._profile: HandsFreeProfile | None = None
        self._sco_listener: socket.socket | None = None
        self._accept_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._connections: dict[str, asyncio.StreamWriter] = {}
        self._sco: socket.socket | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def describe(self) -> str:
        return f"BlueZ ({self.adapter})"

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        self._profile = HandsFreeProfile(self._on_new_connection, self._on_disconnect_request)
        self._bus.export(PROFILE_PATH, self._profile)
        manager = await self._interface("/org/bluez", "org.bluez.ProfileManager1")
        options = {
            "Name": Variant("s", "COB Hands-Free"),
            "Version": Variant("q", HFP_VERSION),
            "Features": Variant("q", sdp_features(self.features)),
            "RequireAuthentication": Variant("b", True),
            "AutoConnect": Variant("b", True),
        }
        await manager.call_register_profile(PROFILE_PATH, HFP_HF_UUID, options)

        sock = _sco_socket()
        sock.bind(BDADDR_ANY.encode())
        sock.listen(1)
        sock.setblocking(False)
        self._sco_listener = sock
        self._accept_task = asyncio.create_task(self._accept_sco())

    async def stop(self) -> None:
        await self.close_audio()
        if self._accept_task is not None:
            self._accept_task.cancel()
        if self._sco_listener is not None:
            self._sco_listener.close()
        for writer in self._connections.values():
            writer.close()
        if self._bus is not None:
            try:
                manager = await self._interface("/org/bluez", "org.bluez.ProfileManager1")
                await manager.call_unregister_profile(PROFILE_PATH)
            except DBusError as exc:
                log.debug("unregister failed: %s", exc)
            self._bus.disconnect()

    async def connect(self, address: str) -> None:
        device = await self._interface(device_path(self.adapter, address), "org.bluez.Device1")
        await device.call_connect_profile(HFP_AG_UUID)

    async def open_audio(self, address: str) -> None:
        sock = _sco_socket()
        try:
            sock.bind(BDADDR_ANY.encode())
            sock.setblocking(False)
            await asyncio.get_running_loop().sock_connect(sock, address.encode())
        except BaseException:
            sock.close()
            raise
        self._start_audio(sock, address)

    async def close_audio(self) -> None:
        sock, self._sco = self._sco, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    # -- D-Bus -------------------------------------------------------------

    async def _interface(self, path: str, name: str):
        assert self._bus is not None
        introspection = await self._bus.introspect("org.bluez", path)
        return self._bus.get_proxy_object("org.bluez", path, introspection).get_interface(name)

    async def _alias(self, path: str) -> str | None:
        try:
            device = await self._interface(path, "org.bluez.Device1")
            return await device.get_alias()
        except Exception:
            return None

    def _on_new_connection(self, path: str, fd: int) -> None:
        sock = socket.socket(fileno=fd)
        sock.setblocking(False)
        task = asyncio.create_task(self._hand_over(path, sock))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _hand_over(self, path: str, sock: socket.socket) -> None:
        address = address_from_path(path)
        name = await self._alias(path)
        reader, writer = await asyncio.open_connection(sock=sock)
        if self.listener.control_connected(address, name, reader, writer):
            self._connections[address] = writer
        else:
            writer.close()

    def _on_disconnect_request(self, path: str) -> None:
        writer = self._connections.pop(address_from_path(path), None)
        if writer is not None:
            writer.close()

    # -- SCO audio ---------------------------------------------------------

    async def _accept_sco(self) -> None:
        loop = asyncio.get_running_loop()
        assert self._sco_listener is not None
        while True:
            conn, address = await loop.sock_accept(self._sco_listener)
            if isinstance(address, bytes):
                address = address.decode()
            log.info("SCO connection from %s", address)
            self._start_audio(conn, address.upper())

    def _start_audio(self, sock: socket.socket, address: str) -> None:
        bridge = self.listener.audio_connected(address)
        if bridge is None:
            sock.close()
            return
        if self._sco is not None:
            self._sco.close()
        self._sco = sock
        sock.setblocking(True)
        sock.settimeout(0.5)
        threading.Thread(
            target=self._pump, args=(sock, bridge), name="sco-audio", daemon=True
        ).start()

    def _pump(self, sock: socket.socket, bridge: AudioBridge) -> None:
        """Move packets between the SCO socket and the bridge until the link drops."""
        try:
            while True:
                try:
                    packet = sock.recv(1024)
                except socket.timeout:
                    continue
                if not packet:
                    break
                sock.send(bridge.exchange(packet))
        except OSError as exc:
            log.info("SCO link closed: %s", exc)
        finally:
            sock.close()
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._audio_closed, sock)

    def _audio_closed(self, sock: socket.socket) -> None:
        if self._sco is sock:
            self._sco = None
        if self._sco is None:
            self.listener.audio_disconnected()
