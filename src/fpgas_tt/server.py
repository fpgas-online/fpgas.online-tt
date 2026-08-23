"""HTTP/WebSocket front end for the bridge, and the ``fpgas-tt`` CLI.

Endpoints:
  GET  /health                  JSON status used by the site's status pill
  WS   /serial                  the bridge: binary frames <-> board bytes; text
                                 frames from the server are JSON events; text
                                 frames from the client are written to the board
                                 as UTF-8 bytes.
  GET  /designs                 (fpga only) list bitstreams + which is enabled
  POST /designs/{name}/enable   (fpga only) load a bitstream, optional clock_hz
  POST /bitstream               (fpga only) upload a bitstream (multipart: name, file)
  POST /demos/sync              (fpga only) (re)sync the on-disk demo set onto the board;
                                 waits up to ~1 s for a running task before answering 409

The four design/bitstream/demo routes return 404
``{"error": "not an fpga board", "detail": ""}`` on non-fpga boards, and map
``ReplRunner``/``designs`` exceptions onto the wire contract: 503 board not
present, 409 another task is running (or a demo-name collision on upload),
404 no such design, 502 REPL task failed (with detail), 400 validation
errors. On startup, fpga boards get a background task that waits for the
board to be present and runs ``designs.sync_demos`` once, retrying every 30 s
on failure.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from fpgas_tt import __version__, designs
from fpgas_tt.bridge import BoardNotPresent, Bridge
from fpgas_tt.config import BoardConfig, discover, parse_hostname
from fpgas_tt.designs import DEMOS_DIR_DEFAULT, DesignNotFound, ValidationError
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
DEMO_SYNC_RETRY = 30.0
# POST /demos/sync riding out a startup auto-sync still in flight: bounded
# retries * delay = ~1s worst case before it finally surfaces 409.
DEMOS_SYNC_BUSY_RETRIES = 10
DEMOS_SYNC_BUSY_RETRY_DELAY = 0.1


def create_app(
    bridge: Bridge,
    config: BoardConfig,
    *,
    version: str = __version__,
    config_error: str | None = None,
    demos_dir: Path | str = DEMOS_DIR_DEFAULT,
) -> web.Application:
    # A little over the bitstream cap so multipart framing overhead never
    # trips this before designs.validate_bitstream gets to give a proper 400.
    app = web.Application(client_max_size=designs.MAX_BITSTREAM_BYTES + 64 * 1024)
    app["bridge"] = bridge
    app["config"] = config
    app["version"] = version
    app["config_error"] = config_error
    app["started"] = time.monotonic()
    app["websockets"] = set()
    app["demos_dir"] = Path(demos_dir)
    app["repl"] = ReplRunner(bridge)
    app.add_routes(
        [
            web.get("/health", health),
            web.get("/serial", serial_ws),
            web.get("/designs", designs_list),
            web.post("/designs/{name}/enable", designs_enable),
            web.post("/bitstream", bitstream_upload),
            web.post("/demos/sync", demos_sync),
        ]
    )
    app.on_shutdown.append(close_websockets)
    if config.kind == "fpga":
        app.on_startup.append(start_demo_sync)
        app.on_cleanup.append(stop_demo_sync)
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
    if request.app["config"].kind != "fpga":
        return _json_error(404, "not an fpga board")
    return None


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
        return _json_error(502, "REPL task failed", exc.detail)


async def designs_list(request: web.Request) -> web.Response:
    if (err := _fpga_only(request)) is not None:
        return err

    async def go():
        return web.json_response(await designs.list_designs(request.app["repl"], request.app["demos_dir"]))

    return await _run(request, go())


async def designs_enable(request: web.Request) -> web.Response:
    if (err := _fpga_only(request)) is not None:
        return err
    clock_hz = None
    if request.can_read_body:
        try:
            body = await request.json()
        except ValueError:
            return _json_error(400, "body must be JSON")
        clock_hz = body.get("clock_hz") if isinstance(body, dict) else None
        if clock_hz is not None and not isinstance(clock_hz, int):
            return _json_error(400, "clock_hz must be an integer")

    async def go():
        return web.json_response(
            await designs.enable_design(request.app["repl"], request.match_info["name"], clock_hz)
        )

    return await _run(request, go())


async def bitstream_upload(request: web.Request) -> web.Response:
    if (err := _fpga_only(request)) is not None:
        return err
    if not request.content_type.startswith("multipart/"):
        return _json_error(400, "multipart form with fields 'name' and 'file' required")
    name, data = "", b""
    reader = await request.multipart()
    async for part in reader:
        if part.name == "name":
            name = (await part.text()).strip()
        elif part.name == "file":
            data = await part.read(decode=False)
            if len(data) > designs.MAX_BITSTREAM_BYTES:
                return _json_error(400, f"bitstream too large (limit {designs.MAX_BITSTREAM_BYTES} bytes)")
    if not name or not data:
        return _json_error(400, "fields 'name' and 'file' are required")
    demos = designs.load_demo_index(request.app["demos_dir"])
    try:
        designs.validate_bitstream(name, data, set(demos))
    except ValidationError as exc:
        return _json_error(exc.status, str(exc))

    async def go():
        repl = request.app["repl"]
        evicted = await designs.evict_uploads(repl, set(demos), keep=designs.MAX_UPLOADS - 1)
        await designs.write_bitstream(repl, name, data)
        return web.json_response({"name": name, "size": len(data), "evicted": evicted}, status=201)

    return await _run(request, go())


async def demos_sync(request: web.Request) -> web.Response:
    if (err := _fpga_only(request)) is not None:
        return err

    async def go():
        out = await _sync_demos_retrying_busy(request.app["repl"], request.app["demos_dir"])
        return web.json_response(out)

    return await _run(request, go())


async def _sync_demos_retrying_busy(repl: ReplRunner, demos_dir: Path) -> dict:
    """Sync is idempotent and often called right as the daemon boots, when
    the startup auto-sync (``start_demo_sync``) may still be finishing its
    own run. Rather than bounce that overlap straight to a 409 a caller has
    to retry themselves, ride out a short, bounded window of ``ReplBusy``
    before giving up -- the final attempt still surfaces 409 through `_run`
    exactly as any other genuinely-busy REPL task does."""
    for _ in range(DEMOS_SYNC_BUSY_RETRIES - 1):
        try:
            return await designs.sync_demos(repl, demos_dir)
        except ReplBusy:
            await asyncio.sleep(DEMOS_SYNC_BUSY_RETRY_DELAY)
    return await designs.sync_demos(repl, demos_dir)  # last attempt: let ReplBusy propagate to `_run`


async def start_demo_sync(app: web.Application) -> None:
    async def loop() -> None:
        bridge: Bridge = app["bridge"]
        while True:
            if bridge.present:
                try:
                    out = await designs.sync_demos(app["repl"], app["demos_dir"])
                    log.info("demos: synced=%s skipped=%s", out["synced"], out["skipped"])
                    return
                except ReplError as exc:
                    log.warning("demos: sync failed (%s); retrying in %ss", exc, DEMO_SYNC_RETRY)
                    await asyncio.sleep(DEMO_SYNC_RETRY)
                    continue
                except Exception:
                    # Must never die silently: an unexpected bug here would
                    # otherwise leave the board without demos forever, with
                    # nothing in the logs pointing at why.
                    log.exception("demos: sync task raised an unexpected exception; retrying in %ss", DEMO_SYNC_RETRY)
                    await asyncio.sleep(DEMO_SYNC_RETRY)
                    continue
            await asyncio.sleep(0.2)

    app["demo_sync_task"] = asyncio.create_task(loop(), name="fpgas-tt-demo-sync")


async def stop_demo_sync(app: web.Application) -> None:
    task = app.get("demo_sync_task")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def health(request: web.Request) -> web.Response:
    bridge: Bridge = request.app["bridge"]
    config: BoardConfig = request.app["config"]
    return web.json_response(
        {
            "board": {
                "present": bridge.present,
                "device": bridge.device,
                "vid_pid": vid_pid_for_tty(bridge.device),
            },
            "kind": config.kind,
            "slug": config.slug,
            "switch": config.switch,
            "port": config.port,
            "hostname": config.hostname,
            "clients": bridge.clients,
            "uptime_s": int(time.monotonic() - request.app["started"]),
            "version": request.app["version"],
            "config_error": request.app["config_error"],
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
        log.info("serial: client %s disconnected (%d total)", peer, bridge.clients)
    return ws


def _short_hostname() -> str:
    return socket.gethostname().split(".")[0]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fpgas-tt", description="Tiny Tapeout demo-board bridge daemon")
    p.add_argument("--device", default="/dev/ttboard", help="serial device (udev symlink) of the demo board")
    p.add_argument("--boards", default="/etc/fpgas-online/tt-boards.yaml", help="site-wide board map (YAML)")
    p.add_argument("--hostname", default=_short_hostname(), help="override this Pi's short hostname")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--baudrate", type=int, default=115200)
    p.add_argument("--log-level", default="INFO", choices=list(LOG_LEVELS))
    p.add_argument("--demos-dir", default=str(DEMOS_DIR_DEFAULT), help="directory of demo bitstreams + index.json")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s")

    config_error: str | None = None
    try:
        config = discover(args.hostname, args.boards)
    except ValueError as exc:
        # A broken board map must not put the unit in a Restart=always loop:
        # serve the port as a plain asic bridge and report why in /health.
        log.error("fpgas-tt: invalid boards file %s: %s — falling back to plain asic bridge", args.boards, exc)
        config_error = str(exc) or None
        sp = parse_hostname(args.hostname)
        switch, port = sp if sp else (None, None)
        config = BoardConfig(slug=args.hostname, kind="asic", switch=switch, port=port, hostname=args.hostname)

    log.info("fpgas-tt %s: %s kind=%s slug=%s device=%s", __version__, config.hostname, config.kind, config.slug,
             args.device)

    bridge = Bridge(args.device, baudrate=args.baudrate)
    app = create_app(bridge, config, config_error=config_error, demos_dir=args.demos_dir)

    async def on_startup(_app: web.Application) -> None:
        await bridge.start()

    async def on_cleanup(_app: web.Application) -> None:
        await bridge.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(
        app,
        host=args.host,
        port=args.port,
        print=None,
        access_log=None,
        shutdown_timeout=SHUTDOWN_TIMEOUT,
    )
    return 0
