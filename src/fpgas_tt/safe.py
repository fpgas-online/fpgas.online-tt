"""Put a board with a Tiny Tapeout chip into a safe state, in its RAM, when the daemon starts and when the board
comes back.

fpgas.online-test-designs issue #181: at every start the SDK (2.0.4) loads its default project, the chip's
``tt_um_factory_test``, with the board's own ``config.ini`` section for it: ``clock_frequency = 10`` and
``ui_in = 1``, in ASIC_RP_CONTROL mode, where the RP2040 drives ``ui_in``. With ``ui_in[0]`` high the factory test
drives its counter onto ``uio`` (``uio_oe = 0xff``). The Pi's Pmod HAT makes one net of each of ui_in[1:3] and
uio[1:3] (HAT JA2-4 and JB2-4 are the same Pi GPIOs 10, 9 and 11), so the RP2040 driving ``ui_in[1:3]`` low and
the chip driving its counter bits fight on those three nets, from power-on. Tim, 2026-10-08 (tt-6): "Both (b) and
(c)": (b) each chip board's ``config.ini`` is changed (his word for that write, done by hand, board by board) and
(c) this: the daemon sets a safe state in RAM.

The safe state: the project clock stopped, ``ui_in`` driven to 0 (so the factory test leaves ``uio`` as inputs
and copies it to ``uo_out``), and the RP2040's own ``uio`` pins released (``uio_oe_pico`` 0, all inputs). Then
no net of the HAT has two drivers: the daemon drives none of the Pi's GPIOs. Nothing is written to a file on the
board (Tim, 2026-10-05); the state lasts until the board's SDK starts again.

The rules:

* Only on a board the boot check's report says carries a Tiny Tapeout chip (`identity.OTHER`). An FPGA board,
  and a board whose kind is not known yet, are left alone; the second is looked at again.
* Once each time the board is opened: at the daemon's start (which follows every boot check, which stops and
  starts the daemon) and when the board comes back after it was unplugged, reset or power-cycled.
* Only when nobody else is using the board. A visitor who connects first has the board as they find it: the
  daemon does not change it for that opening of the board.
* Only the SDK's start state is changed: ``tt_um_factory_test`` enabled, in ASIC_RP_CONTROL mode. Any other
  project or mode, or a board without the SDK's ``tt`` object, is left as it is.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time

from aiohttp import web

from fpgas_tt import designs, identity, idle
from fpgas_tt.repl import ReplBusy, ReplError, ReplNoBoard

log = logging.getLogger(__name__)

POLL = 2.0
ASK_TIMEOUT = 5.0
TAKEN_LIMIT = 10.0
# After an exchange that failed, the board is left alone this long before the next try.
FAILED_WAIT = 600.0
SDK_MODE = "ASIC_RP_CONTROL"  # the mode in which the SDK applies the factory test's `ui_in = 1`
MODE_RE = re.compile(r"[A-Z_]{1,32}")

SAFE_CODE = (
    "import json\n"
    "_fo_tt = globals().get('tt')\n"
    "_fo_r = {'sdk': _fo_tt is not None}\n"
    "if _fo_tt is not None:\n"
    "    _fo_en = _fo_tt.shuttle.enabled\n"
    "    _fo_r['enabled'] = _fo_en.name if _fo_en else None\n"
    "    _fo_r['mode'] = _fo_tt.mode_str\n"
    f"    if _fo_r['enabled'] == {idle.SDK_START_DESIGN!r} and _fo_r['mode'] == {SDK_MODE!r}:\n"
    "        _fo_r['clock_was'] = _fo_tt.auto_clocking_freq if _fo_tt.is_auto_clocking else 0\n"
    "        _fo_r['ui_in_was'] = int(_fo_tt.ui_in.value)\n"
    "        _fo_r['uio_oe_was'] = int(_fo_tt.uio_oe_pico.value)\n"
    "        _fo_tt.clock_project_stop()\n"
    "        _fo_tt.ui_in.value = 0\n"
    "        _fo_tt.uio_oe_pico.value = 0\n"
    "        _fo_r['clock'] = _fo_tt.auto_clocking_freq if _fo_tt.is_auto_clocking else 0\n"
    "        _fo_r['ui_in'] = int(_fo_tt.ui_in.value)\n"
    "        _fo_r['uio_oe'] = int(_fo_tt.uio_oe_pico.value)\n"
    "print(json.dumps(_fo_r))\n"
)


def _number(value) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def outcome(reply) -> str:
    """What the board's reply to SAFE_CODE says happened, for /health and the log. Raises ReplError when it is
    not such a reply, or when the board did not end in the safe state."""
    if not isinstance(reply, dict) or not isinstance(reply.get("sdk"), bool):
        raise ReplError("the board did not say what state it is in", repr(reply))
    if not reply["sdk"]:
        return "left: the SDK is not running"
    enabled = idle.named(reply.get("enabled"))
    if enabled != idle.SDK_START_DESIGN:
        return f"left: {enabled or 'no project'} is enabled"
    mode = reply.get("mode")
    if mode != SDK_MODE:
        return f"left: the board is in {mode if isinstance(mode, str) and MODE_RE.fullmatch(mode) else 'another'} mode"
    after = {k: _number(reply.get(k)) for k in ("clock", "ui_in", "uio_oe")}
    if after != {"clock": 0, "ui_in": 0, "uio_oe": 0}:
        raise ReplError("the board did not take the safe state", repr(reply))
    was = {k: _number(reply.get(k + "_was")) for k in ("clock", "ui_in", "uio_oe")}
    return (
        f"set: clock stopped (was {was['clock']} Hz), ui_in 0 (was {was['ui_in']}), "
        f"uio released (uio_oe_pico was {was['uio_oe']})"
    )


class SafeStart:
    def __init__(self, app: web.Application) -> None:
        self._app = app
        # What /health says: "waiting", "board not present", "waiting: <why the kind is not known>",
        # "not a chip board", "left: <why>", "set: <what changed>", "failed: <why>".
        self.state = "waiting"
        self._done_for: int | None = None  # the opening of the board (bridge.opens) this is settled for
        self._retry_at = 0.0
        self._task: asyncio.Task | None = None

    def health(self) -> dict:
        return {"state": self.state}

    def _look(self) -> int | None:
        """The opening of the board to set the safe state for now, or None with `self.state` saying why not.
        Nothing is awaited here, so what it saw still holds when it returns."""
        app = self._app
        bridge = app["bridge"]
        if not bridge.present:
            self.state = "board not present"
            return None
        opens = bridge.opens
        if self._done_for == opens or time.monotonic() < self._retry_at:
            return None
        who = app["identify"]()
        if who.kind == identity.UNKNOWN:
            self.state = f"waiting: {who.reason}"
            return None
        if who.kind != identity.OTHER:
            self._done_for = opens
            self.state = "not a chip board"
            return None
        if app["websockets"] or app["repl"].busy or app["taken"].taken:
            self._done_for = opens  # the board is a visitor's as they found it
            self.state = "left: the board was in use first"
            return None
        return opens

    async def step(self) -> str:
        """Look once, and set the safe state if this is the moment for it. Returns `self.state`."""
        app = self._app
        opens = self._look()
        if opens is None:
            return self.state
        taken = app["taken"]
        taken.take()
        try:
            async with asyncio.timeout(TAKEN_LIMIT):
                out = await app["repl"].exec(SAFE_CODE, timeout=ASK_TIMEOUT, overall=ASK_TIMEOUT)
            self.state = outcome(designs._parse_json(out))
            self._done_for = opens
            log.info("safe start: %s", self.state)
        except (ReplBusy, ReplNoBoard):
            self.state = "board not present" if not app["bridge"].present else "waiting"
        except TimeoutError:
            self._failed(ReplError("the board did not answer in time"))
        except ReplError as exc:
            self._failed(exc)
        finally:
            taken.release()
        return self.state

    def _failed(self, exc: ReplError) -> None:
        self._retry_at = time.monotonic() + FAILED_WAIT
        self.state = f"failed: {exc}"
        log.warning("safe start: %s: %s", exc, idle._printable(exc.detail[-200:]))

    async def _run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A bug here must not take the daemon's serial bridge down with it.
                log.exception("safe start: unexpected error")
            await asyncio.sleep(POLL)

    async def start(self, _app: web.Application | None = None) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fpgas-tt-safe-start")

    async def stop(self, _app: web.Application | None = None) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._app["taken"].release()
