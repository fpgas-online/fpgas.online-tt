"""Keep the display of an FPGA board nobody is using moving.

Tim, 2026-10-05: "The tiny tapeout verify should leave the tiny tapeout 8 segment LED dispays doing something
interesting. Currently the Tiny Tapeout FPGA boards end up with a static". The boot check ends by streaming a
design that animates the display from the FPGA's own oscillator. That lasts until the board's SDK next starts
(a Commander that connects to a board without ``tt``, or a Run from the page, soft-resets the board): at every
start SDK 3.1.0 loads its default project ``tt_um_factory_test`` and, on an FPGA board, forces the mode in
which that project's ``ui_in = 1`` is not applied and the RP2350 does not drive ``ui_in``. The factory test
counts only when ``ui_in`` bit 0 is high, so what the display then shows is decided by pins nobody sets (the
DIP switches, the Pi's Pmod HAT lines): a raw binary count on the segments, or a still pattern. On board
4df39a7a6856f86f on 2026-10-05 ``ui_in`` read 00001001 and it counted.

So when nobody is using the board the daemon streams the boot check's own design again (the same file on the
Pi, the same way the page's Run loads a design: through the board's memory, nothing written to the board). It
changes no pin and no mode of the SDK: the design ignores its clock, reset and inputs.

The rules:

* Never while a serial client (a Commander, a terminal) is connected, and never when one has just connected:
  while a client is there the board is the visitor's, a still display included. The quiet time starts when the
  last client leaves, when a Run or an upload ends, and when the daemon starts.
* Only on a board the boot check's report says carries the FPGA breakout. A board with a Tiny Tapeout chip is
  left alone (its SDK's default configuration applies ``ui_in = 1`` to the factory test itself).
* A board without the SDK's ``tt`` object is left alone: that is the state the boot check leaves, with its
  moving design already in the FPGA, and starting the SDK would replace it with the factory test.
* After ``after`` quiet seconds a board in the SDK's start state (the factory test, or nothing) gets the idle
  design. A design a visitor loaded is replaced only after ``replace_after`` quiet seconds, and that is off
  unless asked for: the daemon cannot see a visitor who is watching the camera or working on the Pi, and
  taking their design away unannounced would end their session without the page's warning.
* The board is asked what it has loaded once per quiet time (once more when ``replace_after`` is reached), not
  polled: asking interrupts whatever program was left running at the prompt.
* While the daemon is asking or streaming, the board is taken (``Taken``): a serial client that connects then
  is accepted and held, not bridged, until the daemon has finished or given up, and a Run waits the same way.
  So a visitor never shares the REPL with the daemon's own load and never sees its bytes. A client that
  arrives while the board is being asked makes the daemon give up before it streams anything. The daemon
  keeps the board for a bounded time (``TAKEN_LIMIT``), whatever the board or its serial port does.
* A board that was unplugged, reset or power-cycled starts a new quiet time, as does a load that failed (a
  longer one), so a board that comes back in the SDK's start state gets the idle design again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from pathlib import Path

from aiohttp import web

from fpgas_tt import designs, identity
from fpgas_tt.repl import ReplBusy, ReplError, ReplNoBoard

log = logging.getLogger(__name__)

# The design the boot check leaves running (fpgas-online-tt-fpga-bitstreams, which the check itself needs).
# A file on the Pi; its directory is named apart so that no text of the daemon reads like the board's own
# bitstream directory (tests/test_designs.py's tripwire).
IDLE_DESIGN_DEFAULT = (
    Path("/usr/share/fpgas-online/tt-fpga") / "bitstreams" / "tt-display-tt-fpga" / "tt_fpga_platform.bin"
)
IDLE_AFTER_DEFAULT = 60.0
# What the SDK loads at every start (src/config.ini at v3.1.0: `project = tt_um_factory_test`).
SDK_START_DESIGN = "tt_um_factory_test"
POLL = 5.0
# After a load that failed, the board is left alone this long before the next try.
FAILED_WAIT = 600.0
# How long the daemon may keep the board to itself: the question, the load, and in all. Whoever arrives
# meanwhile is held that long at most, and a Run that waited has the rest of its own deadline left
# (designs.ENABLE_OVERALL_TIMEOUT), so the site's proxy still gets its answer in time.
ASK_TIMEOUT = 5.0
LOAD_TIMEOUT = 12.0
TAKEN_LIMIT = 20.0

STATE_CODE = (
    "import json\n"
    "_fo_tt = globals().get('tt')\n"
    "_fo_en = _fo_tt.shuttle.enabled if _fo_tt is not None else None\n"
    "print(json.dumps({'sdk': _fo_tt is not None, 'enabled': _fo_en.name if _fo_en else None}))\n"
)


class Activity:
    """When somebody last used the board through this daemon."""

    def __init__(self) -> None:
        self.last = time.monotonic()

    def touch(self) -> None:
        self.last = time.monotonic()


class Taken:
    """The board is the daemon's for a moment (the idle display is asking it, or streaming into it). Whoever
    else wants the board waits in `free()` first."""

    def __init__(self) -> None:
        self._free: asyncio.Event | None = None

    @property
    def taken(self) -> bool:
        return self._free is not None

    def take(self) -> None:
        if self._free is None:  # taken twice is taken once: nobody waiting is left behind
            self._free = asyncio.Event()

    def release(self) -> None:
        if self._free is not None:
            self._free.set()
            self._free = None

    async def free(self) -> None:
        while self._free is not None:
            await self._free.wait()


def named(enabled) -> str | None:
    """The name of the loaded design as the board gave it, if it is one this daemon would say aloud. A visitor
    at the REPL can call a design anything, and this goes into /health and the log."""
    if enabled is None:
        return None
    return enabled if isinstance(enabled, str) and designs.NAME_RE.match(enabled) else "a design"


def wanted(state: dict, quiet: float, after: float, replace_after: float | None) -> bool:
    """Whether a board that said `state`, unused for `quiet` seconds, gets the idle design."""
    if not state["sdk"] or state["enabled"] == designs.IDLE_NAME:
        return False
    if state["enabled"] in (None, SDK_START_DESIGN):
        return quiet >= after
    return replace_after is not None and quiet >= replace_after


class IdleDisplay:
    def __init__(
        self,
        app: web.Application,
        *,
        design: Path | str = IDLE_DESIGN_DEFAULT,
        after: float = IDLE_AFTER_DEFAULT,
        replace_after: float | None = None,
    ) -> None:
        if not (math.isfinite(after) and after > 0):
            raise ValueError("the quiet time before the idle display must be a number of seconds above 0")
        if replace_after is not None and not (math.isfinite(replace_after) and replace_after >= after):
            raise ValueError("a visitor's design can be replaced no sooner than the quiet time of the idle display")
        self._app = app
        self.design = Path(design)
        self.after = after
        self.replace_after = replace_after
        # What /health says: "waiting", "in use", "board not present", "not an fpga board", "file missing",
        # "file is not an iCE40 bitstream", "left: <the design a visitor loaded>", "left: the SDK is not
        # running", "loaded", "failed: <why>".
        self.state = "waiting"
        self._asked: tuple[float, int] | None = None  # the quiet time, and its stage, the board was asked in
        self._opens = app["bridge"].opens  # the board, as last seen: it comes back as another one
        self._file_warned = False
        self._task: asyncio.Task | None = None

    def health(self) -> dict:
        return {"design": str(self.design), "state": self.state}

    def _read(self) -> bytes | None:
        """The idle design, or None (said once in the log, and in /health until it is there)."""
        try:
            data = self.design.read_bytes()
        except OSError as exc:
            self._file_problem("file missing", f"{self.design}: {exc}")
            return None
        head = data[: designs.PREAMBLE_WINDOW + len(designs.ICE40_PREAMBLE)]
        if len(data) > designs.MAX_BITSTREAM_BYTES or designs.ICE40_PREAMBLE not in head:
            self._file_problem("file is not an iCE40 bitstream", f"{self.design}: {len(data)} bytes")
            return None
        self._file_warned = False
        return data

    def _file_problem(self, state: str, detail: str) -> None:
        self.state = state
        if not self._file_warned:
            log.warning("idle display: %s (%s); the display of an unused board is left as it is", state, detail)
            self._file_warned = True

    def _look(self) -> tuple[float, int] | None:
        """Whether this is a moment to ask the board: the quiet time and its stage, or None with `self.state`
        saying why not. Nothing is awaited here, so what it saw still holds when it returns."""
        app = self._app
        bridge = app["bridge"]
        if bridge.opens != self._opens:  # unplugged, reset or power-cycled: whatever it had loaded is gone
            self._opens = bridge.opens
            self._asked = None
            app["activity"].touch()
        if not bridge.present:
            self.state = "board not present"
            return None
        if app["websockets"] or app["repl"].busy or app["taken"].taken:
            self.state = "in use"
            return None
        if app["identify"]().kind != identity.FPGA:
            self.state = "not an fpga board"
            return None
        stamp = app["activity"].last
        quiet = time.monotonic() - stamp
        stage = 2 if self.replace_after is not None and quiet >= self.replace_after else 1 if quiet >= self.after else 0
        if self._asked is not None and self._asked[0] == stamp and self._asked[1] >= stage:
            return None  # already settled for this quiet time: the state stays what that left
        if stage == 0:
            if not self.state.startswith("failed: "):  # a failed load is said until it is tried again
                self.state = "waiting"
            return None
        return stamp, stage

    async def step(self) -> str:
        """Look once, and stream the idle design if this is the moment for it. Returns `self.state`."""
        app = self._app
        if self._look() is None:
            return self.state
        # The Pi's root is on NFS: read off the event loop, which also serves the serial bridge. Somebody may
        # come meanwhile, so the look is taken again afterwards, and from there to taking the board nothing
        # is awaited.
        data = await asyncio.to_thread(self._read)
        if data is None:
            return self.state
        look = self._look()
        if look is None:
            return self.state
        stamp, stage = look
        taken = app["taken"]
        taken.take()
        try:
            # In all, bounded: a serial port that stops taking bytes must not keep every visitor out.
            async with asyncio.timeout(TAKEN_LIMIT):
                return await self._ask_and_load(stamp, stage, data)
        except TimeoutError:
            self._failed(ReplError("the board did not finish in time"))
        finally:
            taken.release()
        return self.state

    def _failed(self, exc: ReplError) -> None:
        """Tried again after a new, longer, quiet time: a board that was still starting gets its display."""
        activity = self._app["activity"]
        self._asked = None
        activity.last = max(activity.last, time.monotonic() + FAILED_WAIT - self.after)
        self.state = f"failed: {exc}"
        log.warning("idle display: %s: %s", exc, _printable(exc.detail[-200:]))

    async def _ask_and_load(self, stamp: float, stage: int, data: bytes) -> str:
        app = self._app
        try:
            out = await app["repl"].exec(STATE_CODE, timeout=ASK_TIMEOUT, overall=ASK_TIMEOUT)
            state = designs._parse_json(out)
            if not isinstance(state, dict) or not isinstance(state.get("sdk"), bool):
                raise ReplError("the board did not say what it has loaded", out)
            enabled = state["enabled"] = named(state.get("enabled"))
            self._asked = (stamp, stage)
            if not wanted(state, time.monotonic() - stamp, self.after, self.replace_after):
                if enabled == designs.IDLE_NAME:
                    self.state = "loaded"
                else:
                    self.state = f"left: {enabled if state['sdk'] else 'the SDK is not running'}"
                return self.state
            if app["websockets"] or app["activity"].last != stamp:  # somebody came while the board was asked
                self._asked = None
                self.state = "in use"
                return self.state
            await designs.load_design(app["repl"], designs.IDLE_NAME, data, None, overall=LOAD_TIMEOUT)
        except (ReplBusy, ReplNoBoard):
            self._asked = None
            self.state = "in use" if app["bridge"].present else "board not present"
        except ReplError as exc:
            self._failed(exc)
        else:
            self.state = "loaded"
            log.info("idle display: streamed %s into the unused board (was: %s)", self.design, enabled)
        return self.state

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(POLL)
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A bug here must not take the daemon's serial bridge down with it.
                log.exception("idle display: unexpected error")

    async def start(self, _app: web.Application | None = None) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fpgas-tt-idle")

    async def stop(self, _app: web.Application | None = None) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._app["taken"].release()


def _printable(text: str) -> str:
    """Board output for one line of the log."""
    return "".join(c if " " <= c <= "~" else " " for c in text)
