"""End-to-end test of the Bumble backend against Bumble's own Audio Gateway.

Two virtual controllers share an in-process link: one runs COB's backend, the
other plays the phone with ``bumble.hfp.AgProtocol``.
"""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("bumble")

from bumble import hci, hfp, rfcomm  # noqa: E402
from bumble.controller import Controller  # noqa: E402
from bumble.core import PhysicalTransport  # noqa: E402
from bumble.device import Device, DeviceConfiguration  # noqa: E402
from bumble.host import Host  # noqa: E402
from bumble.link import LocalLink  # noqa: E402
from bumble.transport.common import AsyncPipeSink  # noqa: E402

from cob.backends import bumble as bumble_backend  # noqa: E402
from cob.hfp import DEFAULT_HF_FEATURES, CallState, HandsFree  # noqa: E402

HF_ADDRESS = "F0:F1:F2:F3:F4:F5"
AG_ADDRESS = "A0:A1:A2:A3:A4:A5"


class Listener:
    def __init__(self):
        self.hf: HandsFree | None = None
        self.events: list[str] = []
        self.ready = asyncio.Event()

    def control_connected(self, address, name, reader, writer):
        self.address = address
        self.hf = HandsFree(reader, writer, DEFAULT_HF_FEATURES, timeout=5)
        self.hf.add_listener(lambda event, data: self.events.append(event))
        asyncio.ensure_future(self._start())
        return True

    async def _start(self):
        await self.hf.start()
        self.ready.set()

    def audio_connected(self, address):
        self.audio_address = address
        return self.bridge

    bridge = None
    audio_closed = False

    def audio_disconnected(self):
        self.audio_closed = True


def test_phone_connects_and_rings(monkeypatch):
    async def scenario():
        link = LocalLink()
        hf_controller = Controller("HF", link=link, public_address=HF_ADDRESS)

        async def fake_open_transport(name):
            async def close():
                pass

            return SimpleNamespace(
                source=hf_controller, sink=AsyncPipeSink(hf_controller), close=close
            )

        monkeypatch.setattr(bumble_backend, "open_transport", fake_open_transport)
        listener = Listener()
        backend = bumble_backend.BumbleBackend(listener, DEFAULT_HF_FEATURES)
        await backend.start()

        ag_controller = Controller("AG", link=link, public_address=AG_ADDRESS)
        ag_device = Device(
            config=DeviceConfiguration(classic_enabled=True, le_enabled=False, address=AG_ADDRESS),
            host=Host(ag_controller, AsyncPipeSink(ag_controller)),
        )
        await ag_device.power_on()
        connection = await ag_device.connect(HF_ADDRESS, transport=PhysicalTransport.BR_EDR)
        channel, _version, _features = await hfp.find_hf_sdp_record(connection)
        multiplexer = await rfcomm.Client(connection).start()
        dlc = await multiplexer.open_dlc(channel)
        ag = hfp.AgProtocol(
            dlc,
            hfp.AgConfiguration(
                supported_ag_features=[
                    hfp.AgFeature.REJECT_CALL,
                    hfp.AgFeature.ENHANCED_CALL_STATUS,
                ],
                supported_ag_indicators=[
                    hfp.AgIndicatorState.service(),
                    hfp.AgIndicatorState.call(),
                    hfp.AgIndicatorState.callsetup(),
                    hfp.AgIndicatorState.callheld(),
                    hfp.AgIndicatorState.signal(),
                    hfp.AgIndicatorState.roam(),
                    hfp.AgIndicatorState.battchg(),
                ],
                supported_hf_indicators=[],
                supported_ag_call_hold_operations=[],
                supported_audio_codecs=[hfp.AudioCodec.CVSD],
            ),
        )
        answered = asyncio.Event()
        ag.on("answer", answered.set)

        await asyncio.wait_for(listener.ready.wait(), 5)
        hf = listener.hf
        assert listener.address == AG_ADDRESS
        assert hf.connected
        assert "callsetup" in hf.indicator_names

        ag.update_ag_indicator(hfp.AgIndicator.CALL_SETUP, 1)
        ag.send_ring()
        for _ in range(50):
            if hf.call_state == CallState.INCOMING and "ring" in listener.events:
                break
            await asyncio.sleep(0.02)
        assert hf.call_state == CallState.INCOMING
        assert "ring" in listener.events

        await hf.answer()
        await asyncio.wait_for(answered.wait(), 2)

        # Pull the call audio to the PC: packets from the phone go through the
        # bridge, and the bridge's reply goes back to the phone.
        class EchoBridge:
            received = []

            def exchange(self, packet):
                self.received.append(packet)
                return bytes(reversed(packet))

        listener.bridge = EchoBridge()
        ag_links = []

        def on_ag_sco_request(connection, link_type):
            params = hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_CVSD_S1]
            asyncio.ensure_future(
                ag_device.send_command(
                    hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command(
                        bd_addr=connection.peer_address, **params.asdict()
                    )
                )
            )

        ag_device.on("sco_request", on_ag_sco_request)
        ag_device.on("sco_connection", ag_links.append)
        hf_host = backend._device.host
        # The virtual controller implements the enhanced command without advertising it.
        monkeypatch.setattr(hf_host, "supports_command", lambda op: True)
        await backend.open_audio(AG_ADDRESS)
        for _ in range(50):
            if ag_links and backend._sco is not None:
                break
            await asyncio.sleep(0.02)
        assert listener.audio_address == AG_ADDRESS

        # The virtual controller doesn't carry SCO data, so deliver a packet to
        # COB's link directly and capture what COB sends back.
        sent = []
        monkeypatch.setattr(hf_host, "send_sco_sdu", lambda handle, sdu: sent.append((handle, sdu)))
        link = backend._sco
        link.sink(
            hci.HCI_SynchronousDataPacket(
                connection_handle=link.handle,
                packet_status=hci.HCI_SynchronousDataPacket.Status.CORRECTLY_RECEIVED_DATA,
                data_total_length=4,
                data=b"\x01\x02\x03\x04",
            )
        )
        assert EchoBridge.received == [b"\x01\x02\x03\x04"]
        assert sent == [(link.handle, b"\x04\x03\x02\x01")]

        await backend.close_audio()
        for _ in range(50):
            if backend._sco is None:
                break
            await asyncio.sleep(0.02)
        assert listener.audio_closed

        await hf.close()
        await backend.stop()

    asyncio.run(scenario())
