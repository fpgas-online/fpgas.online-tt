import asyncio
import json

import aiohttp
import pytest

from fpgas_tt import __version__
from fpgas_tt.bridge import Bridge
from fpgas_tt.config import BoardConfig
from fpgas_tt.server import build_parser, create_app, main

CFG = BoardConfig(slug="tt06", kind="asic", switch=1, port=6, hostname="pi-sw1-p6")


async def wait_for(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture
async def bridge(fake_board):
    b = Bridge(str(fake_board.path), reopen_interval=0.05)
    await b.start()
    await wait_for(lambda: b.present)
    yield b
    await b.stop()


@pytest.fixture
async def client(aiohttp_client, bridge):
    return await aiohttp_client(create_app(bridge, CFG))


async def test_health_reports_board_and_identity(client):
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["board"]["present"] is True
    assert body["board"]["device"].endswith("ttboard")
    assert body["kind"] == "asic"
    assert body["slug"] == "tt06"
    assert body["switch"] == 1 and body["port"] == 6
    assert body["board"]["vid_pid"] is None  # a pty has no USB identity
    assert body["clients"] == 0
    assert body["version"] == __version__
    assert body["config_error"] is None
    assert isinstance(body["uptime_s"], int)


async def test_health_when_board_absent(aiohttp_client, tmp_path):
    bridge = Bridge(str(tmp_path / "missing"), reopen_interval=0.05)
    await bridge.start()
    try:
        c = await aiohttp_client(create_app(bridge, CFG))
        body = await (await c.get("/health")).json()
        assert body["board"]["present"] is False
    finally:
        await bridge.stop()


async def test_serial_ws_roundtrip(client, fake_board, bridge):
    async with client.ws_connect("/serial") as ws:
        first = await ws.receive(timeout=2)
        assert first.type == aiohttp.WSMsgType.TEXT
        assert json.loads(first.data) == {"event": "board", "present": True, "device": str(fake_board.path)}

        await ws.send_bytes(b"print(1)\r\n")
        assert await fake_board.recv_exactly(len(b"print(1)\r\n")) == b"print(1)\r\n"

        await ws.send_str("\x03")  # text frames are written verbatim too
        assert await fake_board.recv_exactly(1) == b"\x03"

        await fake_board.send(b"1\r\n>>> ")
        msg = await ws.receive(timeout=2)
        assert msg.type == aiohttp.WSMsgType.BINARY
        assert msg.data == b"1\r\n>>> "

        health = await (await client.get("/health")).json()
        assert health["clients"] == 1
    await wait_for(lambda: bridge.clients == 0)  # no orphaned Client


async def test_serial_ws_fanout(client, fake_board, bridge):
    async with client.ws_connect("/serial") as a, client.ws_connect("/serial") as b:
        await a.receive(timeout=2)  # board events
        await b.receive(timeout=2)
        await fake_board.send(b"ping")
        assert (await a.receive(timeout=2)).data == b"ping"
        assert (await b.receive(timeout=2)).data == b"ping"
    await wait_for(lambda: bridge.clients == 0)  # no orphaned Clients


async def test_serial_ws_closed_on_board_loss(client, fake_board, bridge):
    async with client.ws_connect("/serial") as ws:
        await ws.receive(timeout=2)  # board event
        fake_board.unplug()
        msg = await ws.receive(timeout=2)
        assert msg.type == aiohttp.WSMsgType.CLOSE
        assert msg.data == 1011
        assert msg.extra == "board disconnected"
    await wait_for(lambda: bridge.clients == 0)


async def test_serial_ws_write_when_board_absent_reports_error(aiohttp_client, tmp_path):
    bridge = Bridge(str(tmp_path / "missing"), reopen_interval=0.05)
    await bridge.start()
    try:
        c = await aiohttp_client(create_app(bridge, CFG))
        async with c.ws_connect("/serial") as ws:
            first = json.loads((await ws.receive(timeout=2)).data)
            assert first["present"] is False
            await ws.send_bytes(b"x")
            err = json.loads((await ws.receive(timeout=2)).data)
            assert err == {"event": "error", "error": "board not present"}
    finally:
        await bridge.stop()


def test_main_parses_args_and_discovers(monkeypatch, tmp_path):
    """main() wires argv → discover() → create_app → run_app; we stub run_app."""
    captured = {}

    def fake_run_app(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr("fpgas_tt.server.web.run_app", fake_run_app)
    boards = tmp_path / "tt-boards.yaml"
    boards.write_text("tt_boards:\n  - {slug: fpga-1, port: 12, kind: fpga}\n")
    rc = main(["--device", "/dev/null", "--boards", str(boards), "--hostname", "pi-sw1-p12", "--port", "9999"])
    assert rc == 0
    assert captured["kwargs"]["port"] == 9999
    assert captured["kwargs"]["host"] == "0.0.0.0"
    assert captured["app"]["config"].slug == "fpga-1"
    assert captured["app"]["bridge"].device == "/dev/null"


async def test_websockets_closed_on_server_shutdown(client, bridge):
    """A restart must say goodbye (1001) instead of dropping sockets."""
    async with client.ws_connect("/serial") as ws:
        await ws.receive(timeout=2)  # board event
        # shutdown() waits for our on_shutdown hook, which waits for this
        # client's close reply -- so it cannot be awaited inline.
        shutdown = asyncio.create_task(client.app.shutdown())
        msg = await ws.receive(timeout=2)
        assert msg.type == aiohttp.WSMsgType.CLOSE
        assert msg.data == 1001
        assert msg.extra == "server shutdown"
        await asyncio.wait_for(shutdown, 5)
    await wait_for(lambda: bridge.clients == 0)


async def test_health_reports_config_error(aiohttp_client, bridge):
    c = await aiohttp_client(create_app(bridge, CFG, config_error="tt-boards.yaml: boom"))
    body = await (await c.get("/health")).json()
    assert body["config_error"] == "tt-boards.yaml: boom"


def test_main_falls_back_when_boards_file_is_invalid(monkeypatch, tmp_path, caplog):
    """A broken tt-boards.yaml must not put the unit in a restart loop."""
    captured = {}

    def fake_run_app(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr("fpgas_tt.server.web.run_app", fake_run_app)
    boards = tmp_path / "tt-boards.yaml"
    boards.write_text("tt_boards:\n  - {slug: [oops\n")  # malformed YAML
    with caplog.at_level("ERROR", logger="fpgas_tt.server"):
        rc = main(["--device", "/dev/null", "--boards", str(boards), "--hostname", "pi-sw1-p6"])
    assert rc == 0
    assert "falling back" in caplog.text
    app = captured["app"]
    assert app["config"] == BoardConfig(slug="pi-sw1-p6", kind="asic", switch=1, port=6, hostname="pi-sw1-p6")
    assert app["config_error"]
    assert captured["kwargs"]["shutdown_timeout"] == 5.0
    assert captured["kwargs"]["access_log"] is None


def test_log_level_is_restricted():
    parser = build_parser()
    assert parser.parse_args(["--log-level", "DEBUG"]).log_level == "DEBUG"
    with pytest.raises(SystemExit):
        parser.parse_args(["--log-level", "chatty"])
