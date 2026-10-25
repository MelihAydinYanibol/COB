import asyncio
import hashlib
import json
import ssl
from urllib.parse import parse_qs, urlparse

import pytest

from cob import wifi
from cob.hfp import CallState


class FakeApp:
    """Speaks the companion app's side of the protocol, with certificate pinning."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, host, port, fingerprint):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # pinned below instead
        reader, writer = await asyncio.open_connection(host, port, ssl=context)
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        assert hashlib.sha256(der).hexdigest() == fingerprint, "certificate pin mismatch"
        return cls(reader, writer)

    def send(self, message):
        self.writer.write(json.dumps(message).encode() + b"\n")

    async def receive(self):
        line = await asyncio.wait_for(self.reader.readline(), 2)
        return json.loads(line) if line else None

    def close(self):
        self.writer.close()


def parse_pairing(uri):
    parsed = urlparse(uri)
    assert parsed.scheme == "cob" and parsed.netloc == "pair"
    return {k: v[0] for k, v in parse_qs(parsed.query).items()}


async def start_server(tmp_path):
    phones = []
    server = wifi.WifiServer(phones.append, tmp_path, port=0, bind="127.0.0.1", name="test-pc")
    await server.start()
    return server, phones


async def wait_for(predicate, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


async def pair(server, phones, name="Pixel"):
    info = parse_pairing(server.start_pairing("127.0.0.1"))
    app = await FakeApp.connect(info["h"], int(info["p"]), info["f"])
    app.send({"type": "pair", "token": info["t"], "name": name})
    reply = await app.receive()
    await wait_for(lambda: phones)
    phone = phones[-1]
    events = []
    phone.add_listener(lambda event, data: events.append((event, data)))
    phone.start()
    return app, reply, phone, events


def test_pairing_and_reconnect(tmp_path):
    async def scenario():
        server, phones = await start_server(tmp_path)
        app, reply, phone, events = await pair(server, phones)
        assert reply["type"] == "paired" and reply["name"] == "test-pc"
        assert phone.name == "Pixel"
        assert ("connected", {}) in events
        stored = server.devices.devices[reply["device_id"]]
        assert reply["secret"] not in json.dumps(stored)  # only the hash is kept
        app.close()
        await asyncio.wait_for(phone.wait_closed(), 2)
        assert events[-1][0] == "disconnected"

        # Reconnect with the device secret.
        app = await FakeApp.connect("127.0.0.1", server.port, server.fingerprint)
        app.send({"type": "hello", "device_id": reply["device_id"], "secret": reply["secret"], "name": "Pixel"})
        assert (await app.receive())["type"] == "welcome"
        await wait_for(lambda: len(phones) == 2)

        # The pairing token was single use, and the store survives a restart.
        assert wifi.DeviceStore(tmp_path / "wifi-devices.json").verify(reply["device_id"], reply["secret"])
        app.close()
        await server.close()

    asyncio.run(scenario())


def test_refuses_bad_credentials(tmp_path):
    async def scenario():
        server, phones = await start_server(tmp_path)
        info = parse_pairing(server.start_pairing("127.0.0.1"))

        app = await FakeApp.connect("127.0.0.1", server.port, server.fingerprint)
        app.send({"type": "pair", "token": "wrong", "name": "Mallory"})
        assert (await app.receive())["type"] == "error"

        app = await FakeApp.connect("127.0.0.1", server.port, server.fingerprint)
        app.send({"type": "hello", "device_id": "nope", "secret": "nope"})
        assert (await app.receive())["type"] == "error"

        # The real token still works once, and only once.
        app = await FakeApp.connect("127.0.0.1", server.port, server.fingerprint)
        app.send({"type": "pair", "token": info["t"], "name": "Pixel"})
        assert (await app.receive())["type"] == "paired"
        app2 = await FakeApp.connect("127.0.0.1", server.port, server.fingerprint)
        app2.send({"type": "pair", "token": info["t"], "name": "Pixel"})
        assert (await app2.receive())["type"] == "error"
        assert len(phones) == 1
        await server.close()

    asyncio.run(scenario())


def test_pairing_window_expires(tmp_path, monkeypatch):
    async def scenario():
        server, phones = await start_server(tmp_path)
        info = parse_pairing(server.start_pairing("127.0.0.1"))
        now = wifi.time.monotonic()
        monkeypatch.setattr(wifi.time, "monotonic", lambda: now + wifi.PAIRING_TTL + 1)
        app = await FakeApp.connect("127.0.0.1", server.port, server.fingerprint)
        app.send({"type": "pair", "token": info["t"], "name": "Pixel"})
        assert (await app.receive())["type"] == "error"
        await server.close()

    asyncio.run(scenario())


def test_incoming_call_and_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(wifi, "RING_INTERVAL", 0.05)

    async def scenario():
        server, phones = await start_server(tmp_path)
        app, _reply, phone, events = await pair(server, phones)

        app.send({"type": "call", "state": "ringing", "number": "+905551112233"})
        await wait_for(lambda: sum(e == "ring" for e, _ in events) >= 2)  # repeats
        assert phone.call_state == CallState.INCOMING
        assert ("caller_id", {"number": "+905551112233"}) in events

        answer = asyncio.create_task(phone.answer())
        command = await app.receive()
        assert command["type"] == "command" and command["action"] == "answer"
        app.send({"type": "result", "id": command["id"], "ok": True})
        await answer

        app.send({"type": "call", "state": "offhook", "number": "+905551112233"})
        await wait_for(lambda: phone.call_state == CallState.ACTIVE)
        rings = sum(e == "ring" for e, _ in events)
        await asyncio.sleep(0.15)
        assert sum(e == "ring" for e, _ in events) == rings  # stopped ringing

        hangup = asyncio.create_task(phone.hangup())
        command = await app.receive()
        app.send({"type": "result", "id": command["id"], "ok": False, "error": "permission denied"})
        with pytest.raises(wifi.WifiCommandError, match="permission denied"):
            await hangup

        app.send({"type": "call", "state": "idle"})
        await wait_for(lambda: phone.call_state == CallState.IDLE)
        assert phone.number is None

        dial = asyncio.create_task(phone.dial("+90 555 000"))
        command = await app.receive()
        assert command["action"] == "dial" and command["number"] == "+90555000"
        app.send({"type": "result", "id": command["id"], "ok": True})
        await dial
        redial = asyncio.create_task(phone.redial())
        assert (await app.receive())["number"] == "+90555000"
        redial.cancel()

        app.send({"type": "ping"})
        assert (await app.receive())["type"] == "pong"
        app.send({"type": "status", "battery": 81, "charging": True, "signal": 3, "operator": "Turkcell"})
        await wait_for(lambda: phone.status.get("battery") == 81)
        assert phone.status["operator"] == "Turkcell"
        app.close()
        await server.close()

    asyncio.run(scenario())


def test_discovery(tmp_path):
    async def scenario():
        server, _phones = await start_server(tmp_path)
        replies = []

        class Probe(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                replies.append(json.loads(data))

        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            Probe, remote_addr=("127.0.0.1", server.port)
        )
        transport.sendto(wifi.DISCOVERY_REQUEST)
        await wait_for(lambda: replies)
        assert replies[0] == {
            "cob": 1,
            "port": server.port,
            "fp": server.fingerprint[:16],
            "name": "test-pc",
        }
        transport.close()
        await server.close()

    asyncio.run(scenario())


def test_certificate_is_reused(tmp_path):
    _, _, first = wifi.ensure_certificate(tmp_path)
    _, _, second = wifi.ensure_certificate(tmp_path)
    assert first == second and len(first) == 64


def test_unpair(tmp_path):
    store = wifi.DeviceStore(tmp_path / "devices.json")
    device_id, secret = store.add("Pixel")
    assert store.verify(device_id, secret) == "Pixel"
    assert store.verify(device_id, "wrong") is None
    assert store.remove("pixel") == "Pixel"
    assert store.verify(device_id, secret) is None


def test_qr_renders():
    text = wifi.render_qr("cob://pair?h=1.2.3.4")
    assert text.count("\n") > 10
