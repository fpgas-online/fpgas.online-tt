"""Run MicroPython raw-REPL snippets through the bridge -- as just another client.

One task at a time (``ReplBusy`` otherwise, never queued: a viewer who clicks
twice should see "busy", not a growing backlog). The exchange is framed exactly
as MicroPython does it; any deviation -- typically another viewer typing into
the shared REPL -- fails the task with the captured bytes so the UI can say
"retry, someone else may be typing". Every session ends with Ctrl-B so the
friendly REPL everyone else is looking at comes back.
"""

from __future__ import annotations

import asyncio
import logging

from fpgas_tt.bridge import BoardNotPresent, Bridge, Client

log = logging.getLogger(__name__)

RAW_BANNER = b"raw REPL; CTRL-B to exit\r\n>"
FRIENDLY_PROMPT = b">>> "  # every MicroPython friendly-REPL banner ends with this
ENTER_RAW = b"\r\x03\x03"  # interrupt anything running (twice, like mpremote)
CTRL_A = b"\r\x01"
CTRL_B = b"\r\x02"
CTRL_D = b"\x04"
DETAIL_LIMIT = 2000
LEAVE_DRAIN_TIMEOUT = 1.0  # bound on waiting for the friendly prompt on the way out
# A real board echoes readline/pyexec preamble (a re-issued friendly prompt,
# an echoed \r, ...) before the raw-REPL banner when CTRL_A is sent from the
# friendly prompt -- enter() scans past it rather than assuming the banner is
# the very first thing on the wire; this bounds how much can be discarded
# while looking for it, so a board that never sends the banner still fails
# fast instead of buffering forever.
RAW_BANNER_PREAMBLE_CAP = 4096
DEFAULT_OVERALL_TIMEOUT = 30.0  # floor for exec_steps' whole-session deadline


class ReplError(Exception):
    def __init__(self, message: str, detail: bytes | str = b"") -> None:
        super().__init__(message)
        self.detail = detail.decode("utf-8", "replace") if isinstance(detail, bytes) else detail
        self.detail = self.detail[-DETAIL_LIMIT:]


class ReplBusy(ReplError):
    pass


class ReplNoBoard(ReplError):
    pass


def _default_overall(timeout: float, n_steps: int) -> float:
    """The whole-session deadline used when `overall` isn't given explicitly:
    scaled by step count (+1 for entry) so a single big-timeout step (e.g.
    enable_design's SPI load) doesn't have to share that one timeout's worth
    of budget between entry and the step itself."""
    return max(DEFAULT_OVERALL_TIMEOUT, timeout * (n_steps + 1))


class ReplRunner:
    def __init__(self, bridge: Bridge) -> None:
        self._bridge = bridge
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def exec(self, code: str, *, timeout: float = 10.0, overall: float | None = None) -> str:
        return (await self.exec_steps([code], timeout=timeout, overall=overall))[0]

    async def exec_steps(self, steps: list[str], *, timeout: float = 10.0, overall: float | None = None) -> list[str]:
        """Enter raw REPL once, run each snippet (Ctrl-D per step), leave with Ctrl-B.

        `timeout` bounds each individual read; `overall` additionally bounds
        the whole session (entry through the last step) so a board that
        dribbles bytes just fast enough to keep beating the per-read timeout
        can't hang a task forever. See `_default_overall` for the default
        when not given explicitly."""
        if self._lock.locked():
            raise ReplBusy("another task is running")
        if not self._bridge.present:
            raise ReplNoBoard("board not present")
        deadline = overall if overall is not None else _default_overall(timeout, len(steps))
        async with self._lock:
            client = self._bridge.subscribe()
            session = _Session(client, timeout)
            try:
                async with asyncio.timeout(deadline):
                    await session.enter()
                    return [await session.run(step) for step in steps]
            except TimeoutError as exc:
                raise ReplError("REPL task exceeded its overall deadline", session._seen) from exc
            except BoardNotPresent as exc:
                raise ReplNoBoard("board not present") from exc
            finally:
                try:
                    await client.write(CTRL_B)
                    if session.raw:
                        # We know the board is actually in raw REPL (enter()
                        # confirmed the banner), so it will genuinely reply
                        # to this Ctrl-B. Wait (briefly) for that friendly
                        # prompt before releasing this client: the very next
                        # task may subscribe its own client immediately
                        # afterwards (e.g. sync/eviction loops issue several
                        # sessions back to back), and if these leftover
                        # "leaving raw REPL" bytes are still in flight when
                        # it does, they land in the new session's read
                        # stream and are misread as interference.
                        # Best-effort only -- a failure here must never fail
                        # the task, which has already succeeded or raised by
                        # this point. When we never confirmed raw mode (no
                        # board, silent board, interference before the
                        # banner) there is nothing to wait for -- a reply
                        # may never come -- so Ctrl-B stays fire-and-forget.
                        await session.drain_to_friendly_prompt()
                except BoardNotPresent:
                    log.warning("repl: board went away before the session could be closed")
                finally:
                    # However the cleanup above went -- delivered, timed out,
                    # BoardNotPresent, or some other exception (e.g. an outer
                    # wait_for's CancelledError) -- this client must never
                    # leak: unsubscribe it from the bridge no matter what.
                    client.close()


class _Session:
    def __init__(self, client: Client, timeout: float) -> None:
        self._client = client
        self._timeout = timeout
        self._buf = b""
        self._seen = b""  # everything read this session, for error detail
        self.raw = False  # set once enter() has confirmed the raw REPL banner

    async def enter(self) -> None:
        await self._client.write(ENTER_RAW)
        await asyncio.sleep(0.05)
        self._buf = b""  # discard whatever the interrupt produced
        await self._client.write(CTRL_A)
        # A real board, moving from the friendly prompt into raw REPL, first
        # echoes readline/pyexec preamble (e.g. "\r\n>>> \r\n" -- an echo of
        # the \r, a re-issued prompt, pyexec's newline) *before* the actual
        # "raw REPL; CTRL-B to exit\r\n>" banner. Scan for the banner instead
        # of assuming it's the first thing on the wire, discarding (but
        # keeping in `_seen`) anything before it; bounded so a board that
        # never sends the banner still fails instead of buffering forever.
        while RAW_BANNER not in self._buf:
            if len(self._buf) > RAW_BANNER_PREAMBLE_CAP:
                raise ReplError("REPL protocol mismatch waiting for raw REPL banner", self._seen)
            await self._fill("raw REPL banner")
        _, self._buf = self._buf.split(RAW_BANNER, 1)
        self.raw = True

    async def run(self, code: str) -> str:
        await self._client.write(code.encode("utf-8") + CTRL_D)
        await self._expect(b"OK", "OK after snippet")
        out = await self._read_until(CTRL_D, "end of stdout")
        err = await self._read_until(CTRL_D, "end of stderr")
        await self._expect(b">", "raw prompt")
        if err:
            raise ReplError("REPL task failed", err)
        return out.decode("utf-8", "replace")

    async def drain_to_friendly_prompt(self) -> None:
        """Best-effort: consume bytes until the friendly ``>>> `` prompt is
        seen (or a bounded timeout elapses), so the board has actually left
        raw REPL before this client is released. Never raises."""
        buf = b""  # start fresh: anything already in self._buf predates the
        # Ctrl-B just sent and must not be mistaken for its reply.
        try:
            async with asyncio.timeout(LEAVE_DRAIN_TIMEOUT):
                while FRIENDLY_PROMPT not in buf:
                    data = await self._client.read()
                    if data is None:
                        return
                    buf += data
        except (TimeoutError, BoardNotPresent):
            pass

    # -- framing helpers --
    async def _fill(self, what: str) -> None:
        try:
            data = await asyncio.wait_for(self._client.read(), self._timeout)
        except asyncio.TimeoutError as exc:
            raise ReplError(f"REPL task timed out waiting for {what}", self._seen) from exc
        if data is None:
            if self._client.dropped:
                # The bridge dropped us for falling too far behind (buffer
                # overrun) -- the board is still there, our stream just got
                # cut. Distinct from ReplNoBoard, which means no board at
                # all; conflating the two would have callers retry a task
                # that's actually fine, waiting on a board that's fine too.
                raise ReplError("REPL task lost its stream (board output overran the buffer)", self._seen)
            raise ReplNoBoard("board not present")
        self._buf += data
        self._seen += data

    async def _expect(self, token: bytes, what: str) -> None:
        while len(self._buf) < len(token):
            await self._fill(what)
        if not self._buf.startswith(token):
            # anything else at this point means the REPL is not where we think it is
            # (somebody typed, or the board is not in raw mode)
            raise ReplError(f"REPL protocol mismatch waiting for {what}", self._seen)
        self._buf = self._buf[len(token) :]

    async def _read_until(self, token: bytes, what: str) -> bytes:
        while token not in self._buf:
            await self._fill(what)
        out, self._buf = self._buf.split(token, 1)
        return out
