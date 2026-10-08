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

The safe state: the project clock stopped (its pin released), the RP2040's own ``uio`` pins released
(``uio_oe_pico`` 0, all inputs), and ``ui_in`` driven to 0 by the RP2040, so the factory test leaves ``uio`` as
inputs and copies it to ``uo_out``. Then no net of the HAT has two drivers: the daemon drives none of the Pi's
GPIOs. ``ui_in``'s direction is set too, not only its value: the boot check's wiring test leaves the ui_in pins as
inputs with no pull (fpgas.online-test-designs issue #196), behind the SDK's back (its pins keep the mode it last
set), and the SDK's ``ui_in.value`` only sets the output register. As the SDK does when it starts (its contention
guard for the DIP switches), each ui_in pin is made an input with the SDK's pull-down, whatever the SDK believes it
is, and is driven only if it then reads low, ``ui_in[0]`` first (with it low the chip lets go of
``uio``, and so of ui_in[1:3] through the HAT). A pin still held high, by a DIP switch that is on, is left an
input and said. Which pins are driven is read back from the RP2040's GPIO_OE register, not from the SDK. Nothing
is written to a file on the board (Tim, 2026-10-05); the state lasts until the board's SDK
starts again.

The rules:

* Only on a board the boot check's report says carries a Tiny Tapeout chip (`identity.OTHER`). An FPGA board,
  and a board whose kind is not known yet, are left alone; the second is looked at again.
* Once each time the board is opened: at the daemon's start (which follows every boot check, which stops and
  starts the daemon) and when the board comes back after it was unplugged, reset or power-cycled; and only after
  ``SETTLE`` quiet seconds from then, so a board whose SDK is still starting is not interrupted.
* Only when nobody else is using the board. A visitor who connects in that time, or first, has the board as they
  find it: the daemon does not change it for that opening of the board.
* Only the SDK's start state is changed: ``tt_um_factory_test`` enabled, in ASIC_RP_CONTROL mode, and clocked.
  Any other project or mode, a factory test that is not being clocked (a visitor's, or a board already made safe),
  or a board without the SDK's ``tt`` object, is left as it is.
* An exchange that failed is tried again after ``FAILED_WAIT`` seconds, or at once when the board is opened again.
  That try acts on a factory test that is not being clocked too, because the failed try may have stopped the clock
  itself. A visitor who connected in the meantime still has the board as they found it.
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
SETTLE = 30.0  # quiet seconds after the board is opened before it is asked
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
    "    _fo_r['clock_was'] = _fo_tt.auto_clocking_freq if _fo_tt.is_auto_clocking else 0\n"
    f"    if _fo_r['enabled'] == {idle.SDK_START_DESIGN!r} and _fo_r['mode'] == {SDK_MODE!r}"
    " and (_fo_r['clock_was'] or __RETRY__):\n"
    "        import time\n"
    "        from machine import Pin\n"
    "        _fo_r['ui_in_was'] = int(_fo_tt.ui_in.value)\n"
    "        _fo_r['uio_oe_was'] = int(_fo_tt.uio_oe_pico.value)\n"
    "        _fo_tt.clock_project_stop()\n"
    "        _fo_tt.uio_oe_pico.value = 0\n"
    "        _fo_tt.ui_in.value = 0\n"  # the output register: a pin made an output below starts low
    "        import machine\n"
    "        _fo_r['held'] = []\n"
    "        for _fo_i in range(8):\n"
    "            _fo_p = getattr(_fo_tt.pins, 'ui_in%d' % _fo_i)\n"
    # Whatever the SDK believes: its pin keeps the mode it last set, and the boot check changed the pins behind
    # it (issue #196). An input with the pull-down first; driven only if it then reads low.
    "            _fo_p.mode = Pin.IN\n"
    "            _fo_p.pull = Pin.PULL_DOWN\n"
    "            time.sleep_ms(5)\n"
    "            if _fo_p():\n"
    "                _fo_r['held'].append(_fo_i)\n"
    "                continue\n"
    "            _fo_p.mode = Pin.OUT\n"
    "        time.sleep_ms(5)\n"
    "        _fo_oe = machine.mem32[0xd0000020]\n"  # SIO GPIO_OE: which pins the RP2040 drives, from the hardware
    "        _fo_r['clock'] = _fo_tt.auto_clocking_freq if _fo_tt.is_auto_clocking else 0\n"
    "        _fo_r['ui_in'] = int(_fo_tt.ui_in.value)\n"
    "        _fo_r['uio_oe'] = int(_fo_tt.uio_oe_pico.value)\n"
    "        _fo_r['driven'] = [_fo_i for _fo_i in range(8)\n"
    "                           if _fo_oe >> getattr(_fo_tt.pins, 'ui_in%d' % _fo_i).gpio_num & 1]\n"
    "print(json.dumps(_fo_r))\n"
)


def _number(value) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def safe_code(retry: bool) -> str:
    """SAFE_CODE for this look. `retry`: the daemon's own earlier try, on this opening of the board, failed; it may
    have stopped the clock itself, so a factory test that is not being clocked is no sign of a visitor then."""
    return SAFE_CODE.replace("__RETRY__", "True" if retry else "False")


def outcome(reply, retry: bool = False) -> str:
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
    if not _number(reply.get("clock_was")) and not retry:
        return "left: the factory test is not being clocked (not the SDK's start state)"
    held, driven = reply.get("held"), reply.get("driven")
    if not (isinstance(held, list) and isinstance(driven, list) and all(isinstance(i, int) for i in held + driven)):
        raise ReplError("the board did not say which ui_in pins it drives", repr(reply))
    reads = _number(reply.get("ui_in"))
    held_mask = sum(1 << i for i in held)
    after = {k: _number(reply.get(k)) for k in ("clock", "uio_oe")}
    if (
        after != {"clock": 0, "uio_oe": 0}
        or sorted(held + driven) != list(range(8))
        or 0 in held  # with ui_in[0] high the chip still drives uio: that is not a safe state
        or reads is None
        or reads & ~held_mask & 0xFF
    ):
        raise ReplError("the board did not take the safe state", repr(reply))
    was = {k: _number(reply.get(k + "_was")) for k in ("clock", "ui_in", "uio_oe")}
    text = (
        f"set: clock stopped (was {was['clock']} Hz; its pin released), ui_in driven to 0 (read {was['ui_in']}), "
        f"uio released (uio_oe_pico was {was['uio_oe']})"
    )
    if held:
        names = ", ".join(f"ui_in[{i}]" for i in held)
        text += f"; {names} held high on the demo board (a DIP switch that is on?), left an input"
    return text


class SafeStart:
    def __init__(self, app: web.Application) -> None:
        self._app = app
        # What /health says: "waiting", "board not present", "waiting: <why the kind is not known>",
        # "not a chip board", "left: <why>", "set: <what changed>", "failed: <why>".
        self.state = "waiting"
        self._done_for: int | None = None  # the opening of the board (bridge.opens) this is settled for
        self._seen: tuple[int, float] | None = None  # the opening last seen, and when it was first seen
        self._retry: tuple[int, float] | None = None  # after a failure: the opening, and when to try again
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
        now = time.monotonic()
        if self._seen is None or self._seen[0] != opens:
            self._seen = (opens, now)
        if self._done_for == opens or (self._retry is not None and self._retry[0] == opens and now < self._retry[1]):
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
        if now - self._seen[1] < SETTLE:
            self.state = "waiting"
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
                retry = self._retry is not None and self._retry[0] == opens
                out = await app["repl"].exec(safe_code(retry), timeout=ASK_TIMEOUT, overall=ASK_TIMEOUT)
            self.state = outcome(designs._parse_json(out), retry)
            self._done_for = opens
            self._retry = None
            log.info("safe start: %s", self.state)
        except (ReplBusy, ReplNoBoard):
            self.state = "board not present" if not app["bridge"].present else "waiting"
        except TimeoutError:
            self._failed(opens, ReplError("the board did not answer in time"))
        except ReplError as exc:
            self._failed(opens, exc)
        finally:
            taken.release()
        return self.state

    def _failed(self, opens: int, exc: ReplError) -> None:
        self._retry = (opens, time.monotonic() + FAILED_WAIT)
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
