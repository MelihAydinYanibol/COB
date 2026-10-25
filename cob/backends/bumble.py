"""Windows (and Linux) backend built on Google's Bumble Bluetooth stack.

Windows doesn't let applications use SCO audio links or act as a Hands-Free
device, so on Windows COB brings its own Bluetooth host: Bumble talks HCI
straight to a USB Bluetooth dongle through libusb. That dongle has to use the
WinUSB driver (install it with Zadig), and while it does, Windows itself
can't use it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from bumble import hci, rfcomm
from bumble import hfp as bumble_hfp
from bumble.core import PhysicalTransport
from bumble.device import Device, DeviceConfiguration, ScoLink
from bumble.transport import open_transport

from ..hfp import HfFeature
from . import Backend, BackendListener

log = logging.getLogger(__name__)

# Service class "Audio" + major class "Audio/Video" + minor class "Hands-free".
CLASS_OF_DEVICE_HANDSFREE = 0x200408
SDP_HANDLE = 0x00010001
DEFAULT_TRANSPORT = "usb:0+sco=0"


@hci.HCI_Command.command
@dataclasses.dataclass
class HCI_Setup_Synchronous_Connection_Command(hci.HCI_AsyncCommand):
    """Bluetooth spec 7.1.26, for controllers without the enhanced variant.

    Bumble only ships the enhanced command; older dongles need this one.
    """

    connection_handle: int = dataclasses.field(metadata=hci.metadata(2))
    transmit_bandwidth: int = dataclasses.field(metadata=hci.metadata(4))
    receive_bandwidth: int = dataclasses.field(metadata=hci.metadata(4))
    max_latency: int = dataclasses.field(metadata=hci.metadata(2))
    voice_setting: int = dataclasses.field(metadata=hci.metadata(2))
    retransmission_effort: int = dataclasses.field(metadata=hci.metadata(1))
    packet_type: int = dataclasses.field(metadata=hci.metadata(2))


# CVSD over SCO or eSCO with whatever packet types the link supports.
LEGACY_SCO_PARAMETERS = dict(
    transmit_bandwidth=8000,
    receive_bandwidth=8000,
    max_latency=0xFFFF,
    voice_setting=0x0060,
    retransmission_effort=0xFF,
    packet_type=0x03FF,
)


class DlcWriter:
    """Makes an RFCOMM DLC look like an asyncio StreamWriter to HandsFree."""

    def __init__(self, dlc: rfcomm.DLC):
        self._dlc = dlc
        self._closed = False

    def write(self, data: bytes) -> None:
        if not self._closed:
            self._dlc.write(data)

    async def drain(self) -> None:
        await self._dlc.drain()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = asyncio.ensure_future(self._dlc.disconnect())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())


class BumbleBackend(Backend):
    def __init__(
        self,
        listener: BackendListener,
        features: HfFeature,
        transport: str | None = None,
        device_name: str | None = None,
        keystore: str | None = None,
    ):
        super().__init__(listener, features)
        self.transport_name = transport or DEFAULT_TRANSPORT
        self.device_name = device_name or "COB Hands-Free"
        self.keystore = keystore
        self._transport = None
        self._device: Device | None = None
        self._sco: ScoLink | None = None
        self._tasks: set[asyncio.Task] = set()

    def describe(self) -> str:
        return f"Bumble ({self.transport_name})"

    async def start(self) -> None:
        self._transport = await open_transport(self.transport_name)
        config = DeviceConfiguration(
            name=self.device_name,
            class_of_device=CLASS_OF_DEVICE_HANDSFREE,
            classic_enabled=True,
            le_enabled=False,
            keystore="JsonKeyStore" + (f":{self.keystore}" if self.keystore else ""),
        )
        device = Device.from_config_with_hci(config, self._transport.source, self._transport.sink)
        self._device = device

        channel = rfcomm.Server(device).listen(self._on_dlc)
        configuration = bumble_hfp.HfConfiguration(
            supported_hf_features=[f for f in bumble_hfp.HfFeature if f & int(self.features)],
            supported_hf_indicators=[],
            supported_audio_codecs=[bumble_hfp.AudioCodec.CVSD],
        )
        device.sdp_service_records = {
            SDP_HANDLE: bumble_hfp.make_hf_sdp_records(
                SDP_HANDLE, channel, configuration, bumble_hfp.ProfileVersion.V1_7
            )
        }
        device.on("sco_request", self._on_sco_request)
        device.on("sco_connection", self._on_sco_connection)

        await device.power_on()
        await device.set_discoverable(True)
        await device.set_connectable(True)
        log.info("Bumble device %s is up as %r", device.public_address, self.device_name)

    async def stop(self) -> None:
        await self.close_audio()
        if self._transport is not None:
            await self._transport.close()

    async def connect(self, address: str) -> None:
        device = self._require_device()
        connection = await device.connect(address, transport=PhysicalTransport.BR_EDR)
        await connection.authenticate()
        await connection.encrypt()
        record = await bumble_hfp.find_ag_sdp_record(connection)
        if record is None:
            raise RuntimeError(f"{address} does not offer the Hands-Free Audio Gateway service")
        channel = record[0]
        multiplexer = await rfcomm.Client(connection).start()
        dlc = await multiplexer.open_dlc(channel)
        self._on_dlc(dlc)

    async def open_audio(self, address: str) -> None:
        device = self._require_device()
        connection = device.find_connection_by_bd_addr(
            hci.Address(address), transport=PhysicalTransport.BR_EDR
        )
        if connection is None:
            raise RuntimeError(f"not connected to {address}")
        if device.host.supports_command(hci.HCI_ENHANCED_SETUP_SYNCHRONOUS_CONNECTION_COMMAND):
            params = bumble_hfp.ESCO_PARAMETERS[bumble_hfp.DefaultCodecParameters.ESCO_CVSD_S1]
            command = hci.HCI_Enhanced_Setup_Synchronous_Connection_Command(
                connection_handle=connection.handle, **params.asdict()
            )
        else:
            command = HCI_Setup_Synchronous_Connection_Command(
                connection_handle=connection.handle, **LEGACY_SCO_PARAMETERS
            )
        await device.send_command(command, check_result=True)

    async def close_audio(self) -> None:
        link = self._sco  # its disconnection handler reports the change
        if link is not None:
            try:
                await link.disconnect()
            except Exception:
                log.debug("SCO disconnect failed", exc_info=True)

    # -- internals ---------------------------------------------------------

    def _require_device(self) -> Device:
        if self._device is None:
            raise RuntimeError("backend not started")
        return self._device

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_dlc(self, dlc: rfcomm.DLC) -> None:
        connection = dlc.multiplexer.l2cap_channel.connection
        address = str(connection.peer_address).split("/")[0].upper()
        reader = asyncio.StreamReader()
        dlc.sink = reader.feed_data
        dlc.on("close", reader.feed_eof)
        writer = DlcWriter(dlc)
        if not self.listener.control_connected(address, connection.peer_name, reader, writer):
            writer.close()

    def _on_sco_request(self, connection, link_type: int) -> None:
        device = self._require_device()
        if device.host.supports_command(
            hci.HCI_ENHANCED_ACCEPT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND
        ):
            codec = (
                bumble_hfp.DefaultCodecParameters.ESCO_CVSD_S1
                if link_type == hci.HCI_Connection_Complete_Event.LinkType.ESCO
                else bumble_hfp.DefaultCodecParameters.SCO_CVSD_D1
            )
            command = hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command(
                bd_addr=connection.peer_address, **bumble_hfp.ESCO_PARAMETERS[codec].asdict()
            )
        else:
            command = hci.HCI_Accept_Synchronous_Connection_Request_Command(
                bd_addr=connection.peer_address, **LEGACY_SCO_PARAMETERS
            )
        self._spawn(device.send_command(command))

    def _on_sco_connection(self, link: ScoLink) -> None:
        address = str(link.acl_connection.peer_address).split("/")[0].upper()
        bridge = self.listener.audio_connected(address)
        if bridge is None:
            self._spawn(link.disconnect())
            return
        if self._sco is not None and self._sco is not link:
            self._spawn(self._sco.disconnect())
        self._sco = link
        host = self._require_device().host

        def on_packet(packet: hci.HCI_SynchronousDataPacket) -> None:
            host.send_sco_sdu(link.handle, bridge.exchange(packet.data))

        def on_disconnection(*_args) -> None:
            if self._sco is link:
                self._sco = None
                self.listener.audio_disconnected()

        link.sink = on_packet
        link.on("disconnection", on_disconnection)
