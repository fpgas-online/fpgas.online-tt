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
ENTER_RAW = b"\r\x03\x03"  # interrupt anything running (twice, like mpremote)
CTRL_A = b"\r\x01"
CTRL_B = b"\r\x02"
CTRL_D = b"\x04"
DETAIL_LIMIT = 2000


class ReplError(Exception):
    def __init__(self, message: str, detail: bytes | str = b"") -> None:
        super().__init__(message)
        self.detail = detail.decode("utf-8", "replace") if isinstance(detail, bytes) else detail
        self.detail = self.detail[-DETAIL_LIMIT:]


class ReplBusy(ReplError):
    pass


class ReplNoBoard(ReplError):
    pass


class ReplRunner:
    def __init__(self, bridge: Bridge) -> None:
        self._bridge = bridge
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def exec(self, code: str, *, timeout: float = 10.0) -> str:
        return (await self.exec_steps([code], timeout=timeout))[0]

    async def exec_steps(self, steps: list[str], *, timeout: float = 10.0) -> list[str]:
        """Enter raw REPL once, run each snippet (Ctrl-D per step), leave with Ctrl-B."""
        if self._lock.locked():
            raise ReplBusy("another task is running")
        if not self._bridge.present:
            raise ReplNoBoard("board not present")
        async with self._lock:
            client = self._bridge.subscribe()
            session = _Session(client, timeout)
            try:
                await session.enter()
                return [await session.run(step) for step in steps]
            except BoardNotPresent as exc:
                raise ReplNoBoard("board not present") from exc
            finally:
                try:
                    await client.write(CTRL_B)
                    # Give the transport a moment to actually flush the bytes
                    # before we close the client out from under it -- there is
                    # no reply to wait for, so nothing else forces this.
                    await asyncio.sleep(0.05)
                except BoardNotPresent:
                    log.warning("repl: board went away before the session could be closed")
                client.close()


class _Session:
    def __init__(self, client: Client, timeout: float) -> None:
        self._client = client
        self._timeout = timeout
        self._buf = b""
        self._seen = b""  # everything read this session, for error detail

    async def enter(self) -> None:
        await self._client.write(ENTER_RAW)
        await asyncio.sleep(0.05)
        self._buf = b""  # discard whatever the interrupt produced
        await self._client.write(CTRL_A)
        await self._expect(RAW_BANNER, "raw REPL banner")

    async def run(self, code: str) -> str:
        await self._client.write(code.encode("utf-8") + CTRL_D)
        await self._expect(b"OK", "OK after snippet")
        out = await self._read_until(CTRL_D, "end of stdout")
        err = await self._read_until(CTRL_D, "end of stderr")
        await self._expect(b">", "raw prompt")
        if err:
            raise ReplError("REPL task failed", err)
        return out.decode("utf-8", "replace")

    # -- framing helpers --
    async def _fill(self, what: str) -> None:
        try:
            data = await asyncio.wait_for(self._client.read(), self._timeout)
        except asyncio.TimeoutError as exc:
            raise ReplError(f"REPL task timed out waiting for {what}", self._seen) from exc
        if data is None:
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
