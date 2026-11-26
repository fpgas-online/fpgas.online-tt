import asyncio
import hashlib
import io
import json
import os
from pathlib import Path

import aiohttp
import pytest

from fpgas_tt import __version__, designs, identity, library
from fpgas_tt.bridge import Bridge
from fpgas_tt.designs import ICE40_PREAMBLE
from fpgas_tt.identity import Identity
from fpgas_tt.server import (
    MULTIPART_NAME_MAX_BYTES,
    build_parser,
    create_app,
    main,
)
from tests.test_library import ENTRIES

SERIAL = "a2961e5cac65b25f"
# What identity.identify() gives for a board the boot check's report does not name, one that told the check it
# carries the FPGA breakout, and one that told it it carries a chip.
NOT_NAMED = Identity(identity.UNKNOWN, SERIAL, None, "verify.json does not name board " + SERIAL)
IS_FPGA = Identity(identity.FPGA, SERIAL, "fpga", "verify.json says board " + SERIAL + " carries fpga")
IS_OTHER = Identity(identity.OTHER, SERIAL, "asic", "verify.json says board " + SERIAL + " carries asic")


def fpga_app(bridge, **kwargs):
    return create_app(bridge, identify=lambda: IS_FPGA, **kwargs)


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
    return await aiohttp_client(create_app(bridge, identify=lambda: NOT_NAMED))


async def test_health_reports_board_and_identity(client):
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["board"]["present"] is True
    assert body["board"]["device"].endswith("ttboard")
    assert body["kind"] == "unknown"
    assert body["kind_reason"] == NOT_NAMED.reason
    assert body["board"]["usb_serial"] == SERIAL
    assert body["board"]["chip"] is None
    # nothing says where the board is plugged in
    assert not {"slug", "switch", "port", "hostname"} & set(body)
    assert body["board"]["vid_pid"] is None  # a pty has no USB identity
    assert body["clients"] == 0
    assert body["version"] == __version__
    assert isinstance(body["uptime_s"], int)


async def test_health_when_board_absent(aiohttp_client, tmp_path):
    bridge = Bridge(str(tmp_path / "missing"), reopen_interval=0.05)
    await bridge.start()
    try:
        c = await aiohttp_client(create_app(bridge, identify=lambda: NOT_NAMED))
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
        c = await aiohttp_client(create_app(bridge, identify=lambda: NOT_NAMED))
        async with c.ws_connect("/serial") as ws:
            first = json.loads((await ws.receive(timeout=2)).data)
            assert first["present"] is False
            await ws.send_bytes(b"x")
            err = json.loads((await ws.receive(timeout=2)).data)
            assert err == {"event": "error", "error": "board not present"}
    finally:
        await bridge.stop()


FPGA_BOARD = {"board": "tt", "variant": "tt-fpga", "found": {"serial": SERIAL},
              "identity": {"usb_serial": SERIAL, "chip": "fpga"}}


def _report(tmp_path, boards):
    path = tmp_path / "verify.json"
    path.write_text(json.dumps({"schema_version": 2, "result": "pass", "boards": boards}))
    return path


def test_main_takes_the_kind_from_the_report(monkeypatch, tmp_path, caplog):
    """main() wires argv → create_app → run_app (stubbed); the app asks the report what the board is."""
    captured = {}

    def fake_run_app(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr("fpgas_tt.server.web.run_app", fake_run_app)
    monkeypatch.setattr(identity, "usb_serial_for_tty", lambda device: SERIAL)
    report = _report(tmp_path, [FPGA_BOARD])
    with caplog.at_level("INFO", logger="fpgas_tt.server"):
        rc = main(["--device", "/dev/null", "--report", str(report), "--port", "9999"])
    assert rc == 0
    assert "kind=fpga" in caplog.text
    assert captured["kwargs"]["port"] == 9999
    assert captured["kwargs"]["host"] == "0.0.0.0"
    assert captured["kwargs"]["shutdown_timeout"] == 5.0
    assert captured["kwargs"]["access_log"] is None
    assert captured["app"]["bridge"].device == "/dev/null"
    assert captured["app"]["identify"]().kind == "fpga"
    # the report is read again at each request: the boot check may write it after the daemon starts
    report.write_text(json.dumps({"boards": []}))
    assert captured["app"]["identify"]().kind == "unknown"


def test_no_option_names_a_port_or_a_board_map():
    parser = build_parser()
    for gone in ("--boards", "--hostname"):
        with pytest.raises(SystemExit):
            parser.parse_args([gone, "x"])


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


def test_log_level_is_restricted():
    parser = build_parser()
    assert parser.parse_args(["--log-level", "DEBUG"]).log_level == "DEBUG"
    with pytest.raises(SystemExit):
        parser.parse_args(["--log-level", "chatty"])


DEMOS = Path(__file__).parent / "data" / "demos"


@pytest.fixture
async def fpga_client(aiohttp_client, bridge, fake_repl, tmp_path):
    # the designs are the Pi's: the packaged demos, and uploads in a directory of this test's own
    return await aiohttp_client(
        fpga_app(bridge, demos_dir=DEMOS, uploads_dir=tmp_path / "pi-uploads", library_dir=tmp_path / "pi-library")
    )


ROUTES = (
    ("GET", "/designs"), ("POST", "/designs/x/enable"), ("POST", "/bitstream"), ("GET", "/library"),
    ("POST", "/library/x"),
)  # fmt: skip


async def test_fpga_routes_503_until_the_board_is_identified(client):
    for method, path in ROUTES:
        resp = await client.request(method, path)
        assert resp.status == 503
        assert await resp.json() == {"error": "board not identified yet", "detail": NOT_NAMED.reason}


async def test_fpga_routes_404_on_a_board_that_is_not_an_fpga_board(aiohttp_client, bridge):
    c = await aiohttp_client(create_app(bridge, identify=lambda: IS_OTHER))
    for method, path in ROUTES:
        resp = await c.request(method, path)
        assert resp.status == 404
        assert await resp.json() == {"error": "not an fpga board", "detail": IS_OTHER.reason}


async def test_the_kind_follows_the_report_without_a_restart(aiohttp_client, bridge, fake_repl, tmp_path, monkeypatch):
    """The boot check finishes after the daemon has started: the design routes work from then on."""
    monkeypatch.setattr(identity, "usb_serial_for_tty", lambda device: SERIAL)
    report = tmp_path / "verify.json"
    c = await aiohttp_client(create_app(bridge, report=report, demos_dir=DEMOS, uploads_dir=tmp_path / "up"))
    assert (await c.get("/designs")).status == 503
    assert (await (await c.get("/health")).json())["kind"] == "unknown"
    _report(tmp_path, [FPGA_BOARD])
    assert (await c.get("/designs")).status == 200
    health = await (await c.get("/health")).json()
    assert health["kind"] == "fpga" and health["board"]["chip"] == "fpga"


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
        c = await aiohttp_client(fpga_app(bridge, demos_dir=tmp_path, uploads_dir=tmp_path / "up"))
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
    async def boom(runner, demos_dir, uploads_dir, **kwargs):
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

    async def boom(runner, demos_dir, uploads_dir, **kwargs):
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


async def test_nothing_stubbed_sysfs_and_a_real_report(aiohttp_client, bridge, fake_repl, tmp_path, monkeypatch):
    """/health through the real identify(): a sysfs tree for the board's tty, and a report read from a Pi."""
    from fpgas_tt import usbinfo
    from tests.test_usbinfo import _fake_sysfs

    tty = Path(bridge.device).resolve().name
    root = _fake_sysfs(tmp_path, tty=tty, pid="0005", serial=SERIAL)
    monkeypatch.setattr(usbinfo, "SYSFS_TTY_ROOT", str(root))
    real = Path(__file__).parent / "data" / "verify-tt-fpga-2026-10-05.json"
    c = await aiohttp_client(create_app(bridge, report=real, demos_dir=DEMOS, uploads_dir=tmp_path / "up"))
    body = await (await c.get("/health")).json()
    assert body["board"] == {"present": True, "device": bridge.device, "vid_pid": "2e8a:0005",
                             "usb_serial": SERIAL, "chip": "fpga"}
    assert body["kind"] == "fpga"
    assert (await c.get("/designs")).status == 200


# -- the VGA library: the site pushes entries, the Pi keeps them, a load reads them --

LIB_ID, LIB_SHA, LIB_DATA = ENTRIES[1]  # ttsky25a_vga_example, 4096 bytes
LIB_META = {"id": LIB_ID, "bin_sha256": LIB_SHA, "bin_bytes": len(LIB_DATA)}
BOTH_PARTS = "multipart form with parts 'meta' and 'file' required"


def push_form(meta=LIB_META, file=LIB_DATA) -> aiohttp.FormData:
    form = aiohttp.FormData()
    if meta is not None:
        form.add_field("meta", meta if isinstance(meta, str) else json.dumps(meta), content_type="application/json")
    if file is not None:
        form.add_field("file", io.BytesIO(file), filename="x.bin", content_type="application/octet-stream")
    return form


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_FLIPPED = LIB_DATA[:100] + b"\x55" + LIB_DATA[101:]
_NOT_ICE40 = b"\x00" * 4096
# One case per row of the spec's check table (section 2, POST /library/{id}), first failure first.
PUSH_CHECKS = {
    "not_an_fpga_board": dict(who=IS_OTHER, status=404, error="not an fpga board"),
    "not_identified_yet": dict(who=NOT_NAMED, status=503, error="board not identified yet"),
    "id_not_a_name": dict(url="Bad-Name", status=404, error="no such design"),
    "id_too_long": dict(url="a" * 41, status=404, error="no such design"),
    "not_multipart": dict(body="json", status=400, error=BOTH_PARTS),
    "no_file_part": dict(file=None, status=400, error=BOTH_PARTS),
    "no_meta_part": dict(meta=None, status=400, error=BOTH_PARTS),
    "body_too_large": dict(file=ICE40_PREAMBLE + b"\x01" * (1024 * 1024), status=413, error="request body too large"),
    "file_too_large": dict(file=ICE40_PREAMBLE + b"\x01" * (256 * 1024), status=400, error="bitstream too large"),
    "meta_too_long": dict(meta="x" * 4097, status=400, error="meta too long"),
    "meta_not_json": dict(meta="{not json", status=400, error="meta: not JSON"),
    # RecursionError on Python 3.11 ("not JSON"); a 3.14 parser reads it as a list ("must be a JSON object")
    "meta_nested_too_deep": dict(meta="[" * 2000 + "]" * 2000, status=400, error="meta: "),
    "meta_nan": dict(meta=json.dumps(LIB_META)[:-1].replace("4096", "NaN") + "}", status=400, error="meta: not JSON"),
    "meta_not_an_object": dict(meta="[1, 2]", status=400, error="meta: must be a JSON object"),
    "meta_extra_key": dict(meta={**LIB_META, "title": ""}, status=400, error="meta: keys must be exactly"),
    "meta_bool": dict(meta={**LIB_META, "bin_bytes": True}, status=400, error="meta: bin_bytes must be an integer"),
    "meta_upper_hex": dict(meta={**LIB_META, "bin_sha256": LIB_SHA.upper()}, status=400,
                           error="meta: bin_sha256 must be 64 lowercase hex digits"),
    "meta_id_not_the_url": dict(url="other_name", status=400, error="meta.id does not match the URL"),
    "a_demo_name": dict(url="tt_um_demo_a", meta={**LIB_META, "id": "tt_um_demo_a"}, status=409,
                        error="tt_um_demo_a is a demo name; pick another"),
    "the_idle_name": dict(url="idle_display", meta={**LIB_META, "id": "idle_display"}, status=409,
                          error="idle_display is the daemon's own name for its idle display; pick another"),
    "size_not_meta": dict(file=LIB_DATA[:-1], status=400, error="bitstream is 4095 bytes, meta says 4096"),
    "hash_not_meta": dict(file=_FLIPPED, status=400, error="bitstream does not match bin_sha256"),
    "not_ice40": dict(file=_NOT_ICE40, meta={**LIB_META, "bin_sha256": _sha(_NOT_ICE40)}, status=400,
                      error="not an iCE40 bitstream (preamble 7E AA 99 7E missing in the first 64 bytes)"),
}  # fmt: skip


def library_app(bridge, tmp_path, who=IS_FPGA, **kwargs):
    return create_app(bridge, identify=lambda: who, demos_dir=DEMOS, uploads_dir=tmp_path / "pi-uploads",
                      library_dir=tmp_path / "pi-library", **kwargs)  # fmt: skip


@pytest.mark.parametrize("case", list(PUSH_CHECKS), ids=list(PUSH_CHECKS))
async def test_push_checks(aiohttp_client, bridge, fake_repl, tmp_path, case):
    c = dict(PUSH_CHECKS[case])
    client = await aiohttp_client(library_app(bridge, tmp_path, who=c.get("who", IS_FPGA)))
    url = f"/library/{c.get('url', LIB_ID)}"
    if c.get("body") == "json":
        resp = await client.post(url, json=LIB_META)
    else:
        resp = await client.post(url, data=push_form(c.get("meta", LIB_META), c.get("file", LIB_DATA)))
    assert resp.status == c["status"], await resp.text()
    error = (await resp.json())["error"]
    assert error.startswith(c["error"]), error
    assert library.entries(tmp_path / "pi-library") == []  # nothing kept
    assert fake_repl.transcript == b""  # and the board never asked


async def test_push_created_then_idempotent_200(fpga_client, fake_repl, tmp_path):
    lib = tmp_path / "pi-library"
    resp = await fpga_client.post(f"/library/{LIB_ID}", data=push_form())
    assert resp.status == 201, await resp.text()
    body = {"id": LIB_ID, "bin_bytes": 4096, "bin_sha256": LIB_SHA, "evicted": [], "hides_upload": False}
    assert await resp.json() == body
    assert library.bin_path(lib, LIB_ID).read_bytes() == LIB_DATA
    os.utime(lib / f"{LIB_ID}.json", (1000, 1000))
    resp = await fpga_client.post(f"/library/{LIB_ID}", data=push_form())
    assert resp.status == 200 and await resp.json() == body
    assert (lib / f"{LIB_ID}.json").stat().st_mtime > 1000  # the LRU time is refreshed
    assert fake_repl.transcript == b""  # a push never touches the board


async def test_push_evicts_the_oldest_and_says_so(aiohttp_client, bridge, fake_repl, tmp_path):
    c = await aiohttp_client(library_app(bridge, tmp_path, library_max_bytes=4096))
    first, second = ENTRIES[0], ENTRIES[1]
    assert (await c.post(f"/library/{first[0]}", data=push_form(
        {"id": first[0], "bin_sha256": first[1], "bin_bytes": 4096}, first[2]))).status == 201  # fmt: skip
    resp = await c.post(f"/library/{LIB_ID}", data=push_form())
    assert resp.status == 201 and (await resp.json())["evicted"] == [first[0]]
    assert second[0] == LIB_ID and library.names(tmp_path / "pi-library") == {LIB_ID}


async def test_a_push_that_cannot_be_kept_is_a_json_500(fpga_client, monkeypatch):
    def no_room(*args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(library, "store", no_room)
    resp = await fpga_client.post(f"/library/{LIB_ID}", data=push_form())
    assert resp.status == 500 and resp.content_type == "application/json"
    assert await resp.json() == {"error": "the library entry could not be kept on the Pi", "detail": "OSError"}


async def test_push_reports_hides_upload(fpga_client, tmp_path):
    form = aiohttp.FormData()
    form.add_field("name", LIB_ID)
    form.add_field("file", ICE40_PREAMBLE + b"an upload made before the index existed", filename="x.bin")
    assert (await fpga_client.post("/bitstream", data=form)).status == 201
    resp = await fpga_client.post(f"/library/{LIB_ID}", data=push_form())
    assert resp.status == 201 and (await resp.json())["hides_upload"] is True
    listed = [d for d in (await (await fpga_client.get("/designs")).json())["designs"] if d["name"] == LIB_ID]
    assert [d["source"] for d in listed] == ["library"]  # listed once, as what a load would load
    # and a new upload may not take a cached library id
    form = aiohttp.FormData()
    form.add_field("name", LIB_ID)
    form.add_field("file", ICE40_PREAMBLE + b"another", filename="x.bin")
    resp = await fpga_client.post("/bitstream", data=form)
    assert resp.status == 409
    assert (await resp.json())["error"] == f"{LIB_ID} is a VGA library design; pick another"


async def test_push_counts_as_activity(aiohttp_client, bridge, fake_repl, tmp_path):
    app = library_app(bridge, tmp_path)
    c = await aiohttp_client(app)
    before = app["activity"].last
    await asyncio.sleep(0.01)
    assert (await c.post(f"/library/{LIB_ID}", data=push_form())).status == 201
    assert app["activity"].last > before


async def test_get_library(aiohttp_client, bridge, fake_repl, tmp_path):
    app = library_app(bridge, tmp_path, library_max_bytes=1 << 20)
    c = await aiohttp_client(app)
    assert await (await c.get("/library")).json() == {"max_bytes": 1 << 20, "used_bytes": 0, "entries": []}
    library.store(tmp_path / "pi-library", LIB_ID, LIB_SHA, LIB_DATA, 1 << 20)
    app["taken"].take()  # the idle display has the board: /library does not wait for it
    try:
        resp = await asyncio.wait_for(c.get("/library"), 2)
    finally:
        app["taken"].release()
    body = await resp.json()
    assert body["max_bytes"] == 1 << 20 and body["used_bytes"] == 4096
    [entry] = body["entries"]
    assert set(entry) == {"id", "bin_sha256", "bin_bytes", "used_at"}
    assert (entry["id"], entry["bin_sha256"], entry["bin_bytes"]) == (LIB_ID, LIB_SHA, 4096)
    assert fake_repl.transcript == b""  # never touches the REPL


async def test_designs_has_top_level_library_key(aiohttp_client, bridge, fake_repl, tmp_path):
    c = await aiohttp_client(library_app(bridge, tmp_path, library_max_bytes=1 << 20))
    body = await (await c.get("/designs")).json()
    assert list(body) == ["enabled", "library", "designs"]
    assert body["library"] == {"max_bytes": 1 << 20, "used_bytes": 0}
    assert (await c.post(f"/library/{LIB_ID}", data=push_form())).status == 201
    body = await (await c.get("/designs")).json()
    assert body["library"] == {"max_bytes": 1 << 20, "used_bytes": 4096}
    assert body["designs"][-1] == {
        "name": LIB_ID, "title": "", "author": "", "description": "", "docs_url": "", "repo_url": "",
        "clock_hz": None, "pinout": {}, "source": "library", "size": 4096,
    }  # fmt: skip
    resp = await c.post(f"/designs/{LIB_ID}/enable", json={"clock_hz": 25_175_000})
    assert resp.status == 200, await resp.text()
    assert fake_repl.loaded[-1] == (f"pi:{LIB_ID}.bin", LIB_DATA)
    assert (await (await c.get("/designs")).json())["enabled"] == LIB_ID


async def test_enable_of_an_evicted_library_entry_is_404(aiohttp_client, bridge, fake_repl, tmp_path):
    c = await aiohttp_client(library_app(bridge, tmp_path, library_max_bytes=4096))
    assert (await c.post(f"/library/{LIB_ID}", data=push_form())).status == 201
    other = ENTRIES[2]
    meta = {"id": other[0], "bin_sha256": other[1], "bin_bytes": 4096}
    assert (await (await c.post(f"/library/{other[0]}", data=push_form(meta, other[2]))).json())["evicted"] == [LIB_ID]
    resp = await c.post(f"/designs/{LIB_ID}/enable")
    assert resp.status == 404 and (await resp.json())["error"] == "no such design"


def test_cli_library_flags(monkeypatch, tmp_path):
    monkeypatch.delenv(library.MAX_BYTES_ENV, raising=False)
    args = build_parser().parse_args([])
    assert args.library_dir == "/var/lib/fpgas-tt/library"  # the service's StateDirectory, beside the uploads
    assert args.library_max_bytes is None  # main() asks library.max_bytes_default()
    args = build_parser().parse_args(["--library-dir", str(tmp_path), "--library-max-bytes", "4096"])
    assert args.library_dir == str(tmp_path) and args.library_max_bytes == 4096
    for bad in ("0", "lots", "-1"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--library-max-bytes", bad])

    captured = {}
    monkeypatch.setattr("fpgas_tt.server.web.run_app", lambda app, **kwargs: captured.update(app=app))
    monkeypatch.setattr(identity, "usb_serial_for_tty", lambda device: SERIAL)
    report = _report(tmp_path, [FPGA_BOARD])
    argv = ["--device", "/dev/null", "--report", str(report), "--idle-design", ""]
    assert main(argv) == 0
    assert captured["app"]["library_dir"] == Path("/var/lib/fpgas-tt/library")
    assert captured["app"]["library_max_bytes"] == 16777216
    monkeypatch.setenv(library.MAX_BYTES_ENV, "1048576")  # the infra drop-in's Environment=
    assert main(argv) == 0 and captured["app"]["library_max_bytes"] == 1048576
    assert main([*argv, "--library-max-bytes", "8192", "--library-dir", str(tmp_path)]) == 0  # the flag wins
    assert captured["app"]["library_max_bytes"] == 8192 and captured["app"]["library_dir"] == tmp_path
    monkeypatch.setenv(library.MAX_BYTES_ENV, "lots")
    with pytest.raises(SystemExit):
        main(argv)


def test_create_app_library_defaults(monkeypatch):
    monkeypatch.setenv(library.MAX_BYTES_ENV, "65536")
    app = create_app(Bridge("/dev/null"), identify=lambda: IS_FPGA)
    assert app["library_dir"] == Path("/var/lib/fpgas-tt/library") and app["library_max_bytes"] == 65536


# The upload route's limits, run again now that it reads its parts through the helper it shares with the push.
UPLOAD_LIMITS = {
    "body_over_the_total": dict(fields=[("name", b"too_big"), ("file", ICE40_PREAMBLE + b"\x01" * (1024 * 1024))],
                                chunked=False, status=413, error="request body too large"),
    "chunked_name_too_long": dict(fields=[("name", b"x" * (MULTIPART_NAME_MAX_BYTES + 1)),
                                          ("file", ICE40_PREAMBLE + b"\x01" * 100)],
                                  chunked=True, status=400, error="name too long"),
    "junk_parts_over_the_total": dict(fields=[(f"junk{i}", b"j" * 70_000) for i in range(6)], chunked=True,
                                      status=413, error="request body too large"),
    "file_over_its_cap": dict(fields=[("name", b"my_design"), ("file", ICE40_PREAMBLE + b"\x01" * (256 * 1024))],
                              chunked=True, status=400, error="bitstream too large (limit 262144 bytes)"),
    "no_fields": dict(fields=[], chunked=False, status=400,
                      error="multipart form with fields 'name' and 'file' required"),
    "no_file": dict(fields=[("name", b"my_design")], chunked=False, status=400,
                    error="fields 'name' and 'file' are required"),
}  # fmt: skip


@pytest.mark.parametrize("case", list(UPLOAD_LIMITS), ids=list(UPLOAD_LIMITS))
async def test_bitstream_upload_unchanged_after_part_reader_move(fpga_client, fake_repl, tmp_path, case):
    c = UPLOAD_LIMITS[case]
    form = aiohttp.FormData()
    for name, value in c["fields"]:
        form.add_field(name, io.BytesIO(value), filename="x.bin" if name == "file" else None)
    resp = await fpga_client.post("/bitstream", data=form, chunked=c["chunked"] or None)
    assert resp.status == c["status"], await resp.text()
    assert (await resp.json())["error"].startswith(c["error"])
    assert not (tmp_path / "pi-uploads").exists() or list((tmp_path / "pi-uploads").iterdir()) == []
    assert fake_repl.transcript == b""
