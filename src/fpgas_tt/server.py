"""HTTP/WebSocket front end for the bridge, and the ``fpgas-tt`` CLI.

Endpoints:
  GET  /health                  JSON status used by the site's status pill; says what the board is, and what
                                 the idle display (idle.py) last did
  WS   /serial                  the bridge: binary frames <-> board bytes; text
                                 frames from the server are JSON events; text
                                 frames from the client are written to the board
                                 as UTF-8 bytes.
  GET  /designs                 (fpga only) list the designs on this Pi + which is loaded
  POST /designs/{name}/enable   (fpga only) load a design into the FPGA, optional clock_hz
  POST /bitstream               (fpga only) upload a bitstream (multipart: name, file)

Nothing here writes to the demo board's filesystem: designs are files on the Pi (the packaged demos, and
uploads in --uploads-dir), and loading one streams it into the FPGA through the board's memory (designs.py).

What the board is comes from the board, never from where it is plugged in (identity.py): its USB serial and
the boot check's report on this Pi, read at each request. The three design/bitstream routes return 404
``{"error": "not an fpga board", "detail": why}`` on a board that told the check it carries a chip, 503
``{"error": "board not identified yet", "detail": why}`` on one the report does not say that for yet, and map
``ReplRunner``/``designs`` exceptions onto the wire contract: 503 board not
present, 409 another task is running (or a demo-name collision on upload),
404 no such design (including a name POSTed to /designs/{name}/enable that
could never be valid -- rejected before the REPL is touched), 502 REPL task
failed (with detail -- ANSI/non-printable bytes stripped, \n kept), 400
validation errors, 500 internal error (an unexpected exception, logged with
a traceback; always JSON, never aiohttp's default text/plain).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

from aiohttp import WSMsgType, web

from fpgas_tt import __version__, designs, identity, idle
from fpgas_tt.bridge import BoardNotPresent, Bridge
from fpgas_tt.designs import DEMOS_DIR_DEFAULT, UPLOADS_DIR_DEFAULT, DesignNotFound, ValidationError
from fpgas_tt.identity import REPORT_DEFAULT, Identity
from fpgas_tt.repl import ReplBusy, ReplError, ReplNoBoard, ReplRunner
from fpgas_tt.usbinfo import vid_pid_for_tty

log = logging.getLogger(__name__)

CLOSE_GOING_AWAY = 1001
CLOSE_BOARD_LOST = 1011
CLOSE_INTERNAL_ERROR = 1011
CLOSE_CLIENT_SLOW = 1008
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# How long to wait for a peer's reply to a close frame we had to finish
# ourselves; a vanished client must not hold up shutdown.
CLOSE_REPLY_TIMEOUT = 2.0
SHUTDOWN_TIMEOUT = 5.0
# A little over the bitstream cap so multipart framing overhead never trips
# this before designs.validate_bitstream gets to give a proper 400. aiohttp
# >=3.9's request.multipart() honours Application's client_max_size on its
# own; 3.8.4 (Debian bookworm) does not, so bitstream_upload also checks
# Content-Length up front and bounds the 'file' part's own read below --
# belt and braces, since either aiohttp version is in play depending on how
# this daemon is packaged.
MULTIPART_MAX_BYTES = designs.MAX_BITSTREAM_BYTES + 64 * 1024
MULTIPART_CHUNK = 8192
MULTIPART_NAME_MAX_BYTES = 256  # far more than NAME_RE's own 40-char cap ever needs
CLOCK_HZ_MIN, CLOCK_HZ_MAX = 1, 200_000_000
# ANSI escapes and other non-printable bytes (board output, possibly
# corrupted by interference) must not reach an HTTP client verbatim; \n is
# kept so multi-line detail is still readable.
_UNSAFE_DETAIL_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|[^\x20-\x7e\n]")


def create_app(
    bridge: Bridge,
    *,
    version: str = __version__,
    report: Path | str = REPORT_DEFAULT,
    identify: Callable[[], Identity] | None = None,
    demos_dir: Path | str = DEMOS_DIR_DEFAULT,
    uploads_dir: Path | str = UPLOADS_DIR_DEFAULT,
) -> web.Application:
    # See MULTIPART_MAX_BYTES: this only fully protects non-multipart bodies
    # and aiohttp >=3.9's multipart parsing; bitstream_upload has its own
    # belt-and-braces check for 3.8.4.
    app = web.Application(client_max_size=MULTIPART_MAX_BYTES)
    app["bridge"] = bridge
    # Asked at each request, never kept: the boot check may finish after this daemon starts, and a board may
    # be swapped under it. *identify* is for the tests; the daemon reads the board's USB serial and *report*.
    app["identify"] = identify or (lambda: identity.identify(bridge.device, report))
    app["version"] = version
    app["started"] = time.monotonic()
    app["websockets"] = set()
    app["demos_dir"] = Path(demos_dir)
    app["uploads_dir"] = Path(uploads_dir)
    app["repl"] = ReplRunner(bridge)
    app["activity"] = idle.Activity()  # when somebody last used the board through this daemon
    app["idle"] = None  # the idle display (idle.py), when the daemon runs one
    app["taken"] = idle.Taken()  # set while the idle display has the board: clients and Runs wait for it
    app.add_routes(
        [
            web.get("/health", health),
            web.get("/serial", serial_ws),
            web.get("/designs", designs_list),
            web.post("/designs/{name}/enable", designs_enable),
            web.post("/bitstream", bitstream_upload),
        ]
    )
    app.on_shutdown.append(close_websockets)
    return app


async def close_websockets(app: web.Application) -> None:
    """Say goodbye on restart instead of dropping every socket on the floor."""
    for ws in list(app["websockets"]):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                ws.close(code=CLOSE_GOING_AWAY, message=b"server shutdown"), CLOSE_REPLY_TIMEOUT
            )


def _json_error(status: int, error: str, detail: str = "") -> web.Response:
    return web.json_response({"error": error, "detail": detail}, status=status)


def _fpga_only(request: web.Request) -> web.Response | None:
    who: Identity = request.app["identify"]()
    if who.kind == identity.FPGA:
        return None
    if who.kind == identity.UNKNOWN:
        return _json_error(503, "board not identified yet", who.reason)
    return _json_error(404, "not an fpga board", who.reason)


def _sanitize_detail(text: str) -> str:
    """Strip ANSI escapes and other non-printable bytes from board output
    before it reaches an HTTP client; keeps newlines for readability."""
    return _UNSAFE_DETAIL_RE.sub("", text)


async def _run(request: web.Request, coro) -> web.Response:
    """Map task exceptions onto the wire contract."""
    try:
        return await coro
    except ReplNoBoard:
        return _json_error(503, "board not present")
    except ReplBusy:
        return _json_error(409, "another task is running")
    except DesignNotFound:
        return _json_error(404, "no such design")
    except ReplError as exc:
        log.warning("task failed: %s: %s", exc, exc.detail[-200:])
        return _json_error(502, "REPL task failed", _sanitize_detail(exc.detail))
    except Exception as exc:
        # Belt and braces: anything that reaches here is a bug (a board
        # output parsing gap the designs helpers didn't already catch, or
        # something else entirely) -- it must still come back as clean JSON,
        # not aiohttp's default 500 text/plain, and it must be logged with a
        # traceback since nothing upstream of this point expected it.
        log.exception("task failed with an unexpected exception")
        return _json_error(500, "internal error", type(exc).__name__)


async def designs_list(request: web.Request) -> web.Response:
    if (err := _fpga_only(request)) is not None:
        return err

    async def go():
        app = request.app
        return web.json_response(await designs.list_designs(app["repl"], app["demos_dir"], app["uploads_dir"]))

    await request.app["taken"].free()
    return await _run(request, go())


async def designs_enable(request: web.Request) -> web.Response:
    if (err := _fpga_only(request)) is not None:
        return err
    if not designs.NAME_RE.match(request.match_info["name"]):
        # Not a name the board could ever have -- don't even ask it (and
        # don't let an unvalidated path segment anywhere near board code).
        return _json_error(404, "no such design")
    clock_hz = None
    if request.can_read_body:
        try:
            body = await request.json()
        except ValueError:
            return _json_error(400, "body must be JSON")
        clock_hz = body.get("clock_hz") if isinstance(body, dict) else None
        if clock_hz is not None:
            # bool is a subclass of int in Python -- {"clock_hz": true} must
            # not silently become clock_hz=1.
            if isinstance(clock_hz, bool) or not isinstance(clock_hz, int):
                return _json_error(400, "clock_hz must be an integer")
            if not (CLOCK_HZ_MIN <= clock_hz <= CLOCK_HZ_MAX):
                return _json_error(400, f"clock_hz must be between {CLOCK_HZ_MIN} and {CLOCK_HZ_MAX}")

    async def go():
        app = request.app
        return web.json_response(
            await designs.enable_design(
                app["repl"], request.match_info["name"], clock_hz, app["demos_dir"], app["uploads_dir"]
            )
        )

    request.app["activity"].touch()
    await request.app["taken"].free()  # the idle display gives the board up when it sees somebody has come
    try:
        return await _run(request, go())
    finally:
        request.app["activity"].touch()  # the quiet time starts when the load has ended


async def bitstream_upload(request: web.Request) -> web.Response:
    if (err := _fpga_only(request)) is not None:
        return err
    if not request.content_type.startswith("multipart/"):
        return _json_error(400, "multipart form with fields 'name' and 'file' required")
    request.app["activity"].touch()  # somebody is here, about to load it
    # aiohttp 3.8.4's request.multipart() does not honour Application's
    # client_max_size (fixed in later aiohttp) -- a declared oversized body
    # is rejected here before the parser ever runs; a body with no (or a
    # lying) Content-Length is instead bounded below by reading every part
    # in chunks against a running total across the *whole* request, not
    # just the 'name'/'file' fields -- an unbounded number of otherwise-tiny
    # unrecognized ("junk") parts must not be able to hold the handler open
    # or grow memory forever either.
    if request.content_length is not None and request.content_length > MULTIPART_MAX_BYTES:
        return _json_error(413, f"request body too large (limit {MULTIPART_MAX_BYTES} bytes)")
    name_bytes = bytearray()
    data = bytearray()
    total = 0
    reader = await request.multipart()
    async for part in reader:
        while chunk := await part.read_chunk(MULTIPART_CHUNK):
            total += len(chunk)
            if total > MULTIPART_MAX_BYTES:
                return _json_error(413, f"request body too large (limit {MULTIPART_MAX_BYTES} bytes)")
            if part.name == "name":
                # BodyPartReader.read()/.text() accumulate the whole part
                # unbounded -- same risk as 'file' below (worse: the
                # Content-Length pre-check above doesn't cover a chunked
                # request), so this is bounded the same way.
                name_bytes.extend(chunk)
                if len(name_bytes) > MULTIPART_NAME_MAX_BYTES:
                    return _json_error(400, "name too long")
            elif part.name == "file":
                data.extend(chunk)
                if len(data) > designs.MAX_BITSTREAM_BYTES:
                    return _json_error(400, f"bitstream too large (limit {designs.MAX_BITSTREAM_BYTES} bytes)")
            # any other part name: still counted in `total` above (and thus
            # still bounded), just not otherwise kept.
    name = name_bytes.decode("utf-8", "replace").strip()
    data = bytes(data)
    if not name or not data:
        return _json_error(400, "fields 'name' and 'file' are required")
    demos = designs.load_demo_index(request.app["demos_dir"])
    try:
        designs.validate_bitstream(name, data, set(demos))
    except ValidationError as exc:
        return _json_error(exc.status, str(exc))

    request.app["activity"].touch()
    # Kept on the Pi: the board is not involved until somebody loads it.
    try:
        evicted = await asyncio.to_thread(designs.store_upload, request.app["uploads_dir"], name, data)
    except OSError as exc:
        log.exception("upload: could not keep %s", name)
        return _json_error(500, "the upload could not be kept on the Pi", type(exc).__name__)
    return web.json_response({"name": name, "size": len(data), "evicted": evicted}, status=201)


async def health(request: web.Request) -> web.Response:
    bridge: Bridge = request.app["bridge"]
    who: Identity = request.app["identify"]()
    return web.json_response(
        {
            "board": {
                "present": bridge.present,
                "device": bridge.device,
                "vid_pid": vid_pid_for_tty(bridge.device),
                "usb_serial": who.usb_serial,
                "chip": who.chip,
            },
            "kind": who.kind,
            "kind_reason": who.reason,
            "clients": bridge.clients,
            "idle_display": request.app["idle"].health() if request.app["idle"] is not None else None,
            "uptime_s": int(time.monotonic() - request.app["started"]),
            "version": request.app["version"],
        }
    )


# Reachable only from the gateway (per-port VLANs); no Origin check by design.
async def serial_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    bridge: Bridge = request.app["bridge"]
    websockets: set = request.app["websockets"]
    websockets.add(ws)
    peer = request.remote
    client = None
    pump = None
    goodbye: tuple[int, bytes] | None = None

    async def pump_board_to_ws() -> None:
        nonlocal goodbye
        try:
            while True:
                data = await client.read()
                if data is None:
                    if client.dropped:
                        goodbye = (CLOSE_CLIENT_SLOW, b"client too slow")
                    else:
                        goodbye = (CLOSE_BOARD_LOST, b"board disconnected")
                    await ws.close(code=goodbye[0], message=goodbye[1])
                    return
                await ws.send_bytes(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("serial: pump failed for %s", peer)
            goodbye = (CLOSE_INTERNAL_ERROR, b"internal error")
            await ws.close(code=goodbye[0], message=goodbye[1])

    try:
        # The socket is in `websockets` from the start, so the idle display sees that somebody has come and
        # gives the board up; until it has, this client is held: accepted, and not yet bridged to the REPL,
        # so a visitor never shares the REPL with the daemon's own load and never sees its bytes.
        request.app["activity"].touch()
        await request.app["taken"].free()
        # Subscribe inside the try: anything that fails from here on must
        # still unsubscribe, or the Client outlives its socket forever.
        client = bridge.subscribe()
        log.info("serial: client %s connected (%d total)", peer, bridge.clients)
        await ws.send_json({"event": "board", "present": bridge.present, "device": bridge.device})
        pump = asyncio.create_task(pump_board_to_ws(), name="fpgas-tt-pump")
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                payload = msg.data
            elif msg.type == WSMsgType.TEXT:
                payload = msg.data.encode("utf-8")
            else:
                continue
            try:
                await bridge.write(payload)
            except BoardNotPresent:
                await ws.send_json({"event": "error", "error": "board not present"})
    finally:
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            # ws.close() called from the pump task parks until this receive
            # loop yields, so the cancel above can land before the close frame
            # is written. Finish the close the pump asked for.
            if goodbye is not None and not ws.closed:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        ws.close(code=goodbye[0], message=goodbye[1]), CLOSE_REPLY_TIMEOUT
                    )
        if client is not None:
            client.close()
        websockets.discard(ws)
        request.app["activity"].touch()  # the quiet time starts when the last client has left
        log.info("serial: client %s disconnected (%d total)", peer, bridge.clients)
    return ws


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fpgas-tt", description="Tiny Tapeout demo-board bridge daemon")
    p.add_argument("--device", default="/dev/ttboard", help="serial device (udev symlink) of the demo board")
    p.add_argument("--report", default=str(REPORT_DEFAULT), help="the boot check's report on this Pi (JSON)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--baudrate", type=int, default=115200)
    p.add_argument("--log-level", default="INFO", choices=list(LOG_LEVELS))
    p.add_argument("--demos-dir", default=str(DEMOS_DIR_DEFAULT), help="directory of demo bitstreams + index.json")
    p.add_argument("--uploads-dir", default=str(UPLOADS_DIR_DEFAULT), help="directory, on this Pi, that keeps uploads")
    p.add_argument(
        "--idle-design",
        default=str(idle.IDLE_DESIGN_DEFAULT),
        help="bitstream, on this Pi, streamed into an FPGA board nobody is using so its display moves; "
        "empty for none",
    )
    p.add_argument(
        "--idle-after",
        type=float,
        default=idle.IDLE_AFTER_DEFAULT,
        help="seconds without a client, a Run or an upload before a board left in the SDK's start state gets it",
    )
    p.add_argument(
        "--idle-replace-after",
        type=float,
        default=None,
        help="seconds without a client, a Run or an upload before a design a visitor loaded is replaced by it "
        "(default: never)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s")

    who = identity.identify(args.device, args.report)
    log.info("fpgas-tt %s: device=%s kind=%s (%s)", __version__, args.device, who.kind, who.reason)

    bridge = Bridge(args.device, baudrate=args.baudrate)
    app = create_app(bridge, report=args.report, demos_dir=args.demos_dir, uploads_dir=args.uploads_dir)

    async def on_startup(_app: web.Application) -> None:
        await bridge.start()

    async def on_cleanup(_app: web.Application) -> None:
        await bridge.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    if args.idle_design:
        try:
            app["idle"] = idle.IdleDisplay(
                app, design=args.idle_design, after=args.idle_after, replace_after=args.idle_replace_after
            )
        except ValueError as exc:
            build_parser().error(str(exc))
        app.on_startup.append(app["idle"].start)
        app.on_cleanup.insert(0, app["idle"].stop)  # before the bridge it talks through
    web.run_app(
        app,
        host=args.host,
        port=args.port,
        print=None,
        access_log=None,
        shutdown_timeout=SHUTDOWN_TIMEOUT,
    )
    return 0
