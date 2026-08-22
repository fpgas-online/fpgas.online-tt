"""HTTP/WebSocket front end for the bridge, and the ``fpgas-tt`` CLI.

Endpoints (phase 1):
  GET /health   JSON status used by the site's status pill
  WS  /serial   the bridge: binary frames <-> board bytes; text frames from the
                server are JSON events; text frames from the client are written
                to the board as UTF-8 bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
import time

from aiohttp import WSMsgType, web

from fpgas_tt import __version__
from fpgas_tt.bridge import BoardNotPresent, Bridge
from fpgas_tt.config import BoardConfig, discover, parse_hostname
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


def create_app(
    bridge: Bridge,
    config: BoardConfig,
    *,
    version: str = __version__,
    config_error: str | None = None,
) -> web.Application:
    app = web.Application()
    app["bridge"] = bridge
    app["config"] = config
    app["version"] = version
    app["config_error"] = config_error
    app["started"] = time.monotonic()
    app["websockets"] = set()
    app.add_routes([web.get("/health", health), web.get("/serial", serial_ws)])
    app.on_shutdown.append(close_websockets)
    return app


async def close_websockets(app: web.Application) -> None:
    """Say goodbye on restart instead of dropping every socket on the floor."""
    for ws in list(app["websockets"]):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                ws.close(code=CLOSE_GOING_AWAY, message=b"server shutdown"), CLOSE_REPLY_TIMEOUT
            )


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
    app = create_app(bridge, config, config_error=config_error)

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
