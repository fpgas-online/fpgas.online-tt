import asyncio
import io
import json
from pathlib import Path

import aiohttp
import pytest

from fpgas_tt import __version__, designs
from fpgas_tt.bridge import Bridge
from fpgas_tt.config import BoardConfig
from fpgas_tt.designs import ICE40_PREAMBLE
from fpgas_tt.server import (
    MULTIPART_NAME_MAX_BYTES,
    build_parser,
    create_app,
    main,
)

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


DEMOS = Path(__file__).parent / "data" / "demos"
FPGA_CFG = BoardConfig(slug="fpga-1", kind="fpga", switch=2, port=33, hostname="pi-sw2-p33")


@pytest.fixture
async def fpga_client(aiohttp_client, bridge, fake_repl, tmp_path):
    # the designs are the Pi's: the packaged demos, and uploads in a directory of this test's own
    return await aiohttp_client(create_app(bridge, FPGA_CFG, demos_dir=DEMOS, uploads_dir=tmp_path / "pi-uploads"))


async def test_fpga_routes_404_on_asic_board(client):
    routes = (("GET", "/designs"), ("POST", "/designs/x/enable"), ("POST", "/bitstream"))
    for method, path in routes:
        resp = await client.request(method, path)
        assert resp.status == 404
        assert (await resp.json())["error"] == "not an fpga board"


async def test_fpga_only_guard_is_checked_by_identity_not_truthiness(client, monkeypatch):
    # aiohttp's web.Response is a MutableMapping (for per-response state) and
    # starts with zero stored items, so len(resp) == 0. On aiohttp 3.8.4 --
    # Debian bookworm's version, what CI's test-bookworm job runs --
    # StreamResponse defines no __bool__, so Python falls back to __len__ and
    # bool(resp) is False even for a genuine 404 error response; newer
    # aiohttp added an explicit __bool__ that fixes this (so this repro only
    # showed up under bookworm, not under whatever aiohttp is installed
    # here). `if (err := _fpga_only(request)):` silently fell through on
    # 3.8.4 and ran the handler body anyway -- regression: GET /designs on an
    # asic board did a real REPL call and timed out into a 502 instead of a
    # 404. Guard against regressing to bare truthiness on *any* aiohttp
    # version by forcing a response whose __bool__ is hard-wired False
    # (rather than relying on the installed aiohttp's own __len__/__bool__
    # behaviour, which is exactly what let this slip through here before).
    import fpgas_tt.server as server_module

    class DeliberatelyFalsyResponse(server_module.web.Response):
        def __bool__(self):
            return False

    falsy_error = DeliberatelyFalsyResponse(status=404)
    assert not falsy_error  # sanity: this is the exact pathology being guarded against

    monkeypatch.setattr(server_module, "_fpga_only", lambda request: falsy_error)
    resp = await client.get("/designs")
    assert resp.status == 404


def board_tree(fake_repl):
    root = fake_repl.root
    return {str(f.relative_to(root)): (f.read_bytes() if f.is_file() else None) for f in sorted(root.rglob("*"))}


async def test_designs_list_enable_and_upload_flow_never_changes_a_file_on_the_board(fpga_client, fake_repl, tmp_path):
    (fake_repl.root / "bitstreams" / "custom.bin").write_bytes(b"left by an older loader")
    before = board_tree(fake_repl)
    body = await (await fpga_client.get("/designs")).json()
    assert [d["name"] for d in body["designs"]] == ["tt_um_demo_a", "tt_um_demo_b"]
    assert body["enabled"] is None

    resp = await fpga_client.post("/designs/tt_um_demo_a/enable", json={"clock_hz": 100})
    assert resp.status == 200
    assert await resp.json() == {"enabled": "tt_um_demo_a", "clock_hz": 100}
    assert (await fpga_client.post("/designs/nope/enable")).status == 404
    assert (await fpga_client.post("/designs/custom/enable")).status == 404  # on the board only: not a design

    data = ICE40_PREAMBLE + b"\x01" * 5000
    form = aiohttp.FormData()
    form.add_field("name", "my_design")
    form.add_field("file", data, filename="my_design.bin", content_type="application/octet-stream")
    resp = await fpga_client.post("/bitstream", data=form)
    assert resp.status == 201, await resp.text()
    assert await resp.json() == {"name": "my_design", "size": len(data), "evicted": []}
    assert (tmp_path / "pi-uploads" / "my_design.bin").read_bytes() == data  # kept on the Pi
    body = await (await fpga_client.get("/designs")).json()
    assert [d["name"] for d in body["designs"]] == ["my_design", "tt_um_demo_a", "tt_um_demo_b"]
    assert body["enabled"] == "tt_um_demo_a"

    resp = await fpga_client.post("/designs/my_design/enable")
    assert resp.status == 200, await resp.text()
    assert fake_repl.loaded[-1] == ("pi:my_design.bin", data)  # the SDK's loader got the upload's bytes
    assert (await (await fpga_client.get("/designs")).json())["enabled"] == "my_design"
    assert board_tree(fake_repl) == before


async def test_starting_the_daemon_sends_nothing_to_the_board(fpga_client, fake_repl):
    """It used to copy every demo to the board's /bitstreams when it started."""
    await asyncio.sleep(0.3)
    assert fake_repl.transcript == b"" and board_tree(fake_repl) == {"bitstreams": None}


async def test_an_upload_does_not_touch_the_board(fpga_client, fake_repl):
    form = aiohttp.FormData()
    form.add_field("name", "my_design")
    form.add_field("file", ICE40_PREAMBLE + b"\x02" * 100, filename="my_design.bin")
    assert (await fpga_client.post("/bitstream", data=form)).status == 201
    assert fake_repl.transcript == b""


async def test_an_upload_that_cannot_be_kept_is_a_json_500(fpga_client, monkeypatch):
    def no_room(uploads_dir, name, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(designs, "store_upload", no_room)
    form = aiohttp.FormData()
    form.add_field("name", "my_design")
    form.add_field("file", ICE40_PREAMBLE + b"\x02" * 100, filename="my_design.bin")
    resp = await fpga_client.post("/bitstream", data=form)
    assert resp.status == 500 and resp.content_type == "application/json"
    assert await resp.json() == {"error": "the upload could not be kept on the Pi", "detail": "OSError"}


async def test_upload_validation_errors(fpga_client):
    form = aiohttp.FormData()
    form.add_field("name", "Bad Name")
    form.add_field("file", ICE40_PREAMBLE, filename="x.bin")
    resp = await fpga_client.post("/bitstream", data=form)
    assert resp.status == 400
    assert "name" in (await resp.json())["error"]
    resp = await fpga_client.post("/bitstream", data=aiohttp.FormData())  # no fields at all
    assert resp.status == 400


async def test_the_demo_sync_route_is_gone(fpga_client):
    assert (await fpga_client.post("/demos/sync")).status in (404, 405)


async def test_board_absent_gives_503(aiohttp_client, tmp_path):
    bridge = Bridge(str(tmp_path / "missing"), reopen_interval=0.05)
    await bridge.start()
    try:
        c = await aiohttp_client(create_app(bridge, FPGA_CFG, demos_dir=tmp_path, uploads_dir=tmp_path / "up"))
        resp = await c.get("/designs")
        assert resp.status == 503
        assert (await resp.json())["error"] == "board not present"
    finally:
        await bridge.stop()


def test_parser_demos_and_uploads_dir_defaults():
    args = build_parser().parse_args([])
    assert args.demos_dir == "/usr/share/fpgas-tt/demos"
    assert args.uploads_dir == "/var/lib/fpgas-tt/uploads"  # the service's StateDirectory: on the Pi


async def test_board_returning_unparseable_output_gives_502_json_not_500_text(fpga_client, fake_repl, monkeypatch):
    # A stray print (leftover debug output, board-side interference) before
    # ENABLED_CODE's own json.dumps corrupts its stdout so json.loads can't
    # parse it -- this must still surface as a clean 502 JSON error, not an
    # unhandled 500 text/plain crash.
    monkeypatch.setattr(designs, "ENABLED_CODE", "print('x')\n" + designs.ENABLED_CODE)
    resp = await fpga_client.get("/designs")
    assert resp.status == 502
    assert resp.content_type == "application/json"
    body = await resp.json()
    assert body["error"] == "REPL task failed"


async def test_unexpected_exception_in_task_gives_500_json_not_default_text(fpga_client, monkeypatch):
    async def boom(runner, demos_dir, uploads_dir):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(designs, "list_designs", boom)
    resp = await fpga_client.get("/designs")
    assert resp.status == 500
    assert resp.content_type == "application/json"
    body = await resp.json()
    assert body["error"] == "internal error"
    assert body["detail"] == "RuntimeError"


async def test_replerror_detail_is_sanitized_before_it_reaches_http(fpga_client, monkeypatch):
    from fpgas_tt.repl import ReplError

    async def boom(runner, demos_dir, uploads_dir):
        raise ReplError("x", "\x1b[31mRED\x1b[0m\x01\x02bad\nline")

    monkeypatch.setattr(designs, "list_designs", boom)
    resp = await fpga_client.get("/designs")
    assert resp.status == 502
    body = await resp.json()
    assert body["detail"] == "REDbad\nline"


async def test_oversized_multipart_body_rejected_before_touching_repl(fpga_client, fake_repl):
    # aiohttp can compute this request's total size upfront (every part is
    # an in-memory bytes/BytesIO payload of known length), so this is
    # actually caught by the Content-Length pre-check, before the parser
    # ever runs -- 413, same as the aggregate running-total check below,
    # since both represent the same "whole body too big" condition.
    form = aiohttp.FormData()
    form.add_field("name", "too_big")
    big = ICE40_PREAMBLE + b"\x01" * (1024 * 1024)  # well over MULTIPART_MAX_BYTES
    # io.BytesIO, not raw bytes: aiohttp warns (ResourceWarning, fatal under
    # this repo's filterwarnings=error) about sending a large body as raw
    # bytes and recommends exactly this.
    form.add_field("file", io.BytesIO(big), filename="x.bin", content_type="application/octet-stream")
    resp = await fpga_client.post("/bitstream", data=form)
    assert resp.status == 413
    assert fake_repl.transcript == b""  # never touched the board


async def test_oversized_chunked_name_part_rejected_before_touching_repl(fpga_client, fake_repl):
    # chunked=True forces no Content-Length header, so the pre-check above
    # can't apply here regardless of body size -- and this body is well
    # under MULTIPART_MAX_BYTES anyway, so the running-total check wouldn't
    # catch it either: only bounding the 'name' part specifically does.
    form = aiohttp.FormData()
    form.add_field("name", io.BytesIO(b"x" * (MULTIPART_NAME_MAX_BYTES + 1)))
    form.add_field("file", io.BytesIO(ICE40_PREAMBLE + b"\x01" * 100), filename="x.bin",
                    content_type="application/octet-stream")
    resp = await fpga_client.post("/bitstream", data=form, chunked=True)
    assert resp.status == 400
    assert (await resp.json())["error"] == "name too long"
    assert fake_repl.transcript == b""  # never touched the board


async def test_many_small_junk_multipart_parts_bounded_by_running_total(fpga_client, fake_repl):
    # No single part is anywhere near either per-field limit, and none is
    # named 'name' or 'file' -- only a running total across *every* part in
    # the request catches an unbounded number of small "junk" parts holding
    # the handler open / growing memory forever. chunked=True forces no
    # Content-Length header (aiohttp can otherwise compute one upfront for
    # an all-known-length body like this one, which would let the earlier
    # pre-check catch it first instead of exercising this running total).
    form = aiohttp.FormData()
    junk = b"j" * 70_000
    for i in range(6):  # 6 * 70_000 = 420_000 > MULTIPART_MAX_BYTES (327_680)
        form.add_field(f"junk{i}", io.BytesIO(junk))
    resp = await fpga_client.post("/bitstream", data=form, chunked=True)
    assert resp.status == 413
    assert fake_repl.transcript == b""  # never touched the board


async def test_enable_rejects_non_matching_name_before_touching_repl(fpga_client, fake_repl):
    resp = await fpga_client.post("/designs/not a valid name!/enable")
    assert resp.status == 404
    assert fake_repl.transcript == b""  # never touched the board


@pytest.mark.parametrize(
    "clock_hz",
    [True, "100", 0, -1, 200_000_001, 3.5],
)
async def test_enable_rejects_invalid_clock_hz(fpga_client, clock_hz):
    resp = await fpga_client.post("/designs/some_name/enable", json={"clock_hz": clock_hz})
    assert resp.status == 400
    assert "clock_hz" in (await resp.json())["error"]
