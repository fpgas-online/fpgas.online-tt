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
import logging
import socket
import time
import warnings

from aiohttp import WSMsgType, web
from aiohttp.web_exceptions import NotAppKeyWarning

from fpgas_tt import __version__
from fpgas_tt.bridge import BoardNotPresent, Bridge
from fpgas_tt.config import BoardConfig, discover

log = logging.getLogger(__name__)

CLOSE_BOARD_LOST = 1011
CLOSE_CLIENT_SLOW = 1008


def create_app(bridge: Bridge, config: BoardConfig, *, version: str = __version__) -> web.Application:
    app = web.Application()
    # aiohttp recommends web.AppKey instances for app-storage keys, but our
    # tests (and this module's own handlers) address app state by plain
    # string for simplicity; silence the resulting NotAppKeyWarning rather
    # than switch keys, since AppKey lookups aren't string-addressable.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotAppKeyWarning)
        app["bridge"] = bridge
        app["config"] = config
        app["version"] = version
        app["started"] = time.monotonic()
    app.add_routes([web.get("/health", health), web.get("/serial", serial_ws)])
    return app


async def health(request: web.Request) -> web.Response:
    bridge: Bridge = request.app["bridge"]
    config: BoardConfig = request.app["config"]
    return web.json_response(
        {
            "board": {"present": bridge.present, "device": bridge.device},
            "kind": config.kind,
            "slug": config.slug,
            "switch": config.switch,
            "port": config.port,
            "hostname": config.hostname,
            "clients": bridge.clients,
            "uptime_s": int(time.monotonic() - request.app["started"]),
            "version": request.app["version"],
        }
    )


async def serial_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    bridge: Bridge = request.app["bridge"]
    client = bridge.subscribe()
    peer = request.remote
    log.info("serial: client %s connected (%d total)", peer, bridge.clients)
    await ws.send_json({"event": "board", "present": bridge.present, "device": bridge.device})

    async def pump_board_to_ws() -> None:
        while True:
            data = await client.read()
            if data is None:
                if client.dropped:
                    await ws.close(code=CLOSE_CLIENT_SLOW, message=b"client too slow")
                else:
                    await ws.close(code=CLOSE_BOARD_LOST, message=b"board disconnected")
                return
            await ws.send_bytes(data)

    pump = asyncio.create_task(pump_board_to_ws(), name="fpgas-tt-pump")
    try:
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
        client.close()
        pump.cancel()
        try:
            await pump
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown path, already closing
            pass
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
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s")

    config = discover(args.hostname, args.boards)
    log.info("fpgas-tt %s: %s kind=%s slug=%s device=%s", __version__, config.hostname, config.kind, config.slug,
             args.device)

    bridge = Bridge(args.device, baudrate=args.baudrate)
    app = create_app(bridge, config)

    async def on_startup(_app: web.Application) -> None:
        await bridge.start()

    async def on_cleanup(_app: web.Application) -> None:
        await bridge.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0
