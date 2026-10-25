import asyncio
import socket

import pytest

from cob.hfp import AgFeature, ATError, CallState, HandsFree, derive_call_state

CIND_TEST = (
    '+CIND: ("service",(0,1)),("call",(0,1)),("callsetup",(0-3)),'
    '("callheld",(0-2)),("signal",(0-5)),("roam",(0,1)),("battchg",(0-5))'
)
AG_FEATURES = int(
    AgFeature.THREE_WAY_CALLING
    | AgFeature.REJECT_CALL
    | AgFeature.ENHANCED_CALL_STATUS
    | AgFeature.EXTENDED_ERRORS
)


class FakePhone:
    """Minimal Audio Gateway that answers the commands a hands-free unit sends."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.received: list[str] = []
        self.calls: list[str] = []
        self.task = asyncio.create_task(self._serve())

    def send(self, *lines: str) -> None:
        for line in lines:
            self.writer.write(f"\r\n{line}\r\n".encode())

    async def _serve(self) -> None:
        buffer = b""
        while True:
            data = await self.reader.read(256)
            if not data:
                return
            buffer += data
            while b"\r" in buffer:
                raw, buffer = buffer.split(b"\r", 1)
                command = raw.decode()
                self.received.append(command)
                self._respond(command)
            await self.writer.drain()

    def _respond(self, command: str) -> None:
        if command.startswith("AT+BRSF="):
            self.send(f"+BRSF: {AG_FEATURES}", "OK")
        elif command == "AT+CIND=?":
            self.send(CIND_TEST, "OK")
        elif command == "AT+CIND?":
            self.send("+CIND: 1,0,0,0,4,0,3", "OK")
        elif command == "AT+CHLD=?":
            self.send("+CHLD: (0,1,1x,2,2x,3,4)", "OK")
        elif command == "AT+COPS?":
            self.send('+COPS: 0,0,"Test Mobile"', "OK")
        elif command == "AT+CLCC":
            self.send(*self.calls, "OK")
        elif command == "AT+BOGUS":
            self.send("ERROR")
        elif command == "AT+SILENT":
            pass
        else:
            self.send("OK")


async def connect(timeout: float = 2.0):
    hf_sock, ag_sock = socket.socketpair()
    hf_reader, hf_writer = await asyncio.open_connection(sock=hf_sock)
    ag_reader, ag_writer = await asyncio.open_connection(sock=ag_sock)
    phone = FakePhone(ag_reader, ag_writer)
    hf = HandsFree(hf_reader, hf_writer, timeout=timeout)
    events: list[tuple[str, dict]] = []
    hf.add_listener(lambda event, data: events.append((event, data)))
    await hf.start()
    return hf, phone, events


async def settle():
    for _ in range(10):
        await asyncio.sleep(0.01)


def names(events):
    return [event for event, _ in events]


def test_service_level_connection():
    async def scenario():
        hf, phone, events = await connect()
        assert phone.received[:5] == [
            "AT+BRSF=62",
            "AT+CIND=?",
            "AT+CIND?",
            "AT+CMER=3,0,0,1",
            "AT+CHLD=?",
        ]
        assert "AT+CLIP=1" in phone.received
        assert "AT+CCWA=1" in phone.received
        assert "AT+CMEE=1" in phone.received
        assert hf.connected
        assert hf.ag_features & AgFeature.THREE_WAY_CALLING
        assert hf.indicators["signal"] == 4
        assert hf.indicators["battchg"] == 3
        assert hf.chld_options == ["0", "1", "1x", "2", "2x", "3", "4"]
        assert hf.operator == "Test Mobile"
        assert hf.call_state == CallState.IDLE
        assert names(events)[-1] == "connected"
        await hf.close()

    asyncio.run(scenario())


def test_incoming_call_answer_and_hangup():
    async def scenario():
        hf, phone, events = await connect()
        phone.calls = ['+CLCC: 1,1,4,0,0,"+15551234",145']
        phone.send("+CIEV: 3,1")
        phone.send("RING", '+CLIP: "+15551234",145')
        await settle()
        assert hf.call_state == CallState.INCOMING
        assert hf.number == "+15551234"
        assert "ring" in names(events)
        assert ("caller_id", {"number": "+15551234"}) in events

        await hf.answer()
        assert phone.received[-1] == "ATA"
        phone.send("+CIEV: 2,1", "+CIEV: 3,0")
        await settle()
        assert hf.call_state == CallState.ACTIVE

        await hf.hangup()
        assert phone.received[-1] == "AT+CHUP"
        phone.send("+CIEV: 2,0")
        await settle()
        assert hf.call_state == CallState.IDLE
        assert hf.number is None
        states = [d["state"] for e, d in events if e == "call_state"]
        assert states == [CallState.INCOMING, CallState.ACTIVE, CallState.IDLE]
        await hf.close()

    asyncio.run(scenario())


def test_outgoing_call_and_dtmf():
    async def scenario():
        hf, phone, events = await connect()
        await hf.dial("+1 555-0100")
        assert phone.received[-1] == "ATD+15550100;"
        phone.send("+CIEV: 3,2")
        await settle()
        assert hf.call_state == CallState.DIALING
        phone.send("+CIEV: 3,3")
        await settle()
        assert hf.call_state == CallState.ALERTING
        phone.send("+CIEV: 2,1", "+CIEV: 3,0")
        await settle()
        assert hf.call_state == CallState.ACTIVE
        assert hf.number == "+15550100"

        await hf.send_dtmf("1#")
        assert phone.received[-2:] == ["AT+VTS=1", "AT+VTS=#"]
        with pytest.raises(ValueError):
            await hf.send_dtmf("x")
        with pytest.raises(ValueError):
            await hf.dial("call mom")
        await hf.close()

    asyncio.run(scenario())


def test_volume_and_unsolicited_indicators():
    async def scenario():
        hf, phone, events = await connect()
        phone.send("+VGS: 7", "+CIEV: 7,1", "+BSIR: 1")
        await settle()
        assert hf.speaker_volume == 7
        assert ("volume", {"target": "speaker", "level": 7}) in events
        assert hf.indicators["battchg"] == 1
        assert hf.inband_ring
        await hf.set_speaker_volume(99)
        assert phone.received[-1] == "AT+VGS=15"
        await hf.close()

    asyncio.run(scenario())


def test_errors_timeouts_and_disconnect():
    async def scenario():
        hf, phone, events = await connect(timeout=0.2)
        with pytest.raises(ATError):
            await hf.send("AT+BOGUS")
        with pytest.raises(asyncio.TimeoutError):
            await hf.send("AT+SILENT")
        assert await hf.raw("AT+CIND?") == ["1,0,0,0,4,0,3"]

        phone.writer.close()
        await asyncio.wait_for(hf.wait_closed(), 1)
        assert not hf.connected
        assert names(events)[-1] == "disconnected"
        with pytest.raises(ConnectionError):
            await hf.answer()
        await hf.close()

    asyncio.run(scenario())


def test_call_state_derivation():
    assert derive_call_state({}) == CallState.IDLE
    assert derive_call_state({"callsetup": 1}) == CallState.INCOMING
    assert derive_call_state({"call": 1, "callsetup": 1}, CallState.ACTIVE) == CallState.WAITING
    assert derive_call_state({"call": 1, "callsetup": 1}, CallState.INCOMING) == CallState.ACTIVE
    assert derive_call_state({"call": 1, "callheld": 1}) == CallState.ACTIVE
    assert derive_call_state({"call": 1, "callheld": 2}) == CallState.HELD
    assert derive_call_state({"callheld": 2}) == CallState.HELD
