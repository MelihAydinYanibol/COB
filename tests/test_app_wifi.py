"""The command line app driving a phone over the Wi-Fi link (Bluetooth off)."""

import asyncio

from cob import cli
from test_wifi import FakeApp, parse_pairing, wait_for


def test_wifi_only_app(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "config_dir", lambda: tmp_path)

    async def scenario():
        args = cli.build_parser().parse_args(
            ["--backend", "none", "--wifi", "--wifi-port", "0", "--wifi-bind", "127.0.0.1",
             "--wifi-host", "127.0.0.1", "--no-ringtone"]
        )
        app = cli.App(args)
        app._start_input_thread = lambda: None
        run = asyncio.create_task(app.run())
        await wait_for(lambda: app.wifi_server is not None and app.wifi_server.port)

        await app._command("pair")
        out = capsys.readouterr().out
        uri = next(word for word in out.split() if word.startswith("cob://pair?"))
        info = parse_pairing(uri)
        assert info["h"] == "127.0.0.1"

        phone = await FakeApp.connect(info["h"], int(info["p"]), info["f"])
        phone.send({"type": "pair", "token": info["t"], "name": "Pixel"})
        assert (await phone.receive())["type"] == "paired"
        await wait_for(lambda: app.phone is not None)
        assert "Connected to Pixel over Wi-Fi" in capsys.readouterr().out

        phone.send({"type": "call", "state": "ringing", "number": "+905551112233"})
        await wait_for(lambda: "Incoming call from +905551112233" in capsys.readouterr().out)

        answer = asyncio.create_task(app._command("a"))
        command = await phone.receive()
        assert command["action"] == "answer"
        phone.send({"type": "result", "id": command["id"], "ok": True})
        await answer
        phone.send({"type": "call", "state": "offhook", "number": "+905551112233"})
        await wait_for(lambda: "Call in progress with +905551112233" in capsys.readouterr().out)

        await app._command("x")
        assert "needs the Bluetooth connection" in capsys.readouterr().out

        status = asyncio.create_task(app._command("status"))
        command = await phone.receive()
        assert command["action"] == "status"
        phone.send({"type": "status", "battery": 64, "charging": False, "signal": 2, "operator": "Turkcell"})
        phone.send({"type": "result", "id": command["id"], "ok": True})
        await status
        out = capsys.readouterr().out
        assert "Bluetooth: not connected." in out
        assert "Turkcell, signal 2/4, battery 64%" in out

        await app._command("devices")
        out = capsys.readouterr().out
        assert "Pixel [" in out and out.rstrip().endswith(", connected")
        await app._command("unpair pixel")
        assert "Forgot Pixel." in capsys.readouterr().out
        await wait_for(lambda: app.phone is None)
        assert not app.wifi_server.devices.devices

        app._lines.put_nowait("q")
        await asyncio.wait_for(run, 5)

    asyncio.run(scenario())


def test_bluetooth_wins_when_both_are_connected(capsys):
    args = cli.build_parser().parse_args(["--backend", "none", "--wifi", "--no-ringtone"])
    app = cli.App(args)

    class Link:
        connected = True
        number = "+905551112233"
        call_state = cli.CallState.INCOMING
        inband_ring = False
        name = "Pixel"
        address = "192.168.1.20"

    app.hf, app.wifi = Link(), Link()
    app.phone_name = "Pixel"
    assert app.phone is app.hf

    app._on_event("wifi", "ring", {"number": "+905551112233"})
    assert capsys.readouterr().out == ""  # duplicate of the Bluetooth ring
    app._on_event("bluetooth", "ring", {"number": "+905551112233"})
    assert "Incoming call from +905551112233" in capsys.readouterr().out

    app.hf.connected = False
    assert app.phone is app.wifi
    app._on_event("wifi", "ring", {"number": "+905551112233"})
    assert "Incoming call from +905551112233" in capsys.readouterr().out
