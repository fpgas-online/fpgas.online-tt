"""A fake MicroPython raw REPL on the FakeBoard pty: enough protocol for the daemon's tasks.

It runs *real* Python for the submitted code, with a sandboxed `os` whose
filesystem root is a tmp dir (so '/bitstreams/x.bin' maps to tmp/bitstreams/x.bin)
and a fake `tt` object exposing the FPGA shuttle API the daemon relies on.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import os
from dataclasses import dataclass, field
from pathlib import Path

RAW_BANNER = b"raw REPL; CTRL-B to exit\r\n>"
FRIENDLY_BANNER = b"\r\nMicroPython v1.25 fake; FPGA\r\nType \"help()\" for more information.\r\n>>> "


class _FakeOs:
    """os.listdir/stat/remove/mkdir rooted under `root`, path strings as MicroPython sees them."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def listdir(self, path: str = "/"):
        return sorted(os.listdir(self._p(path)))

    def stat(self, path: str):
        st = os.stat(self._p(path))
        # MicroPython returns a 10-tuple; index 6 = size, 8 = mtime
        return (st.st_mode, 0, 0, 0, 0, 0, st.st_size, int(st.st_atime), int(st.st_mtime), int(st.st_ctime))

    def remove(self, path: str):
        os.remove(self._p(path))

    def mkdir(self, path: str):
        os.mkdir(self._p(path))


@dataclass
class FakeBitStream:
    name: str
    file: str
    project_index: int
    clock_hz: int = 100

    def enable(self, force: bool = False):
        self._mux.enabled = self  # set by FakeShuttle.get
        self._mux.enable_log.append(self.name)
        return True


class FakeShuttle:
    """tt.shuttle: BitStreamIndex-like over the fake /bitstreams dir."""

    def __init__(self, fos: _FakeOs) -> None:
        self._fos = fos
        self._design_index = None
        self.enabled = None
        self.enable_log: list[str] = []

    @property
    def projects(self):
        if self._design_index is None:
            self._design_index = self._build()
        return self._design_index

    def _build(self):
        items = []
        try:
            names = [f for f in self._fos.listdir("/bitstreams") if f.endswith(".bin")]
        except OSError:
            names = []
        for i, f in enumerate(names):
            bs = FakeBitStream(f[:-4], f"/bitstreams/{f}", i)
            bs._mux = self
            items.append(bs)
        return items

    @property
    def all(self):  # mirrors BitStreamIndex.all via tt.shuttle.projects.all in the real SDK
        return self.projects

    def has(self, name: str) -> bool:
        return any(b.name == name for b in self.projects)

    def get(self, name):
        if isinstance(name, int) or (isinstance(name, str) and name.isdigit()):
            idx = int(name)
            for b in self.projects:
                if b.project_index == idx:
                    return b
            raise ValueError(f"Do not have a project {idx}")
        for b in self.projects:
            if b.name == name:
                return b
        raise ValueError(f'Do not have a project "{name}"')


class FakeTT:
    def __init__(self, fos: _FakeOs) -> None:
        self.shuttle = FakeShuttle(fos)
        self.clock_log: list[int] = []

    def clock_project_PWM(self, hz: int):
        self.clock_log.append(hz)


@dataclass
class FakeRepl:
    """Drive the pty master like a MicroPython board in raw-REPL mode."""

    master_fd: int
    root: Path
    echo_junk: bytes = b""  # bytes "someone else" injects after the banner (interference tests)
    transcript: bytearray = field(default_factory=bytearray)

    def __post_init__(self) -> None:
        self.fos = _FakeOs(self.root)
        (self.root / "bitstreams").mkdir(exist_ok=True)
        self.tt = FakeTT(self.fos)
        self._task: asyncio.Task | None = None

        def _open(path, mode="r", *a, **k):  # MicroPython's open() is relative to its own FS root
            return builtins.open(self.fos._p(path), mode, *a, **k)

        self._globals: dict = {
            "os": self.fos,
            "tt": self.tt,
            "open": _open,
            "__builtins__": builtins,
            "__name__": "__main__",
        }

    # -- lifecycle --
    def start(self) -> None:
        self._task = asyncio.create_task(self._serve(), name="fake-repl")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # -- protocol --
    async def _read(self, n: int = 4096) -> bytes:
        # A plain `run_in_executor(os.read, ...)` blocks a real OS thread
        # until data arrives; cancelling the awaiting task (as ``stop()``
        # does) detaches from that thread without stopping it, and it is
        # then never reclaimed until more data (or EOF) shows up on the fd
        # -- which can deadlock a test runner that waits for the executor to
        # drain before it lets other fixtures close that fd. `add_reader`
        # is cooperatively cancellable: cancellation just deregisters it.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bytes] = loop.create_future()

        def _on_readable() -> None:
            if fut.done():
                return
            try:
                fut.set_result(os.read(self.master_fd, n))
            except OSError as exc:
                fut.set_exception(exc)

        loop.add_reader(self.master_fd, _on_readable)
        try:
            return await fut
        finally:
            loop.remove_reader(self.master_fd)

    async def _write(self, data: bytes) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, os.write, self.master_fd, data)

    async def _serve(self) -> None:
        buf = b""
        raw = False
        while True:
            data = await self._read()
            if not data:
                return
            self.transcript += data
            buf += data
            while buf:
                if not raw:
                    if b"\x01" in buf:
                        buf = buf.split(b"\x01", 1)[1]
                        raw = True
                        await self._write(RAW_BANNER + self.echo_junk)
                        continue
                    buf = b""  # friendly mode: swallow (Ctrl-C, newlines, ...)
                    break
                # raw mode
                if buf.startswith(b"\x02"):
                    raw = False
                    buf = buf[1:]
                    await self._write(FRIENDLY_BANNER)
                    continue
                if b"\x04" not in buf:
                    break  # wait for the rest of the snippet
                code, buf = buf.split(b"\x04", 1)
                code = code.replace(b"\r", b"")
                out, err = self._run(code.decode("utf-8", "replace"))
                await self._write(b"OK" + out.encode() + b"\x04" + err.encode() + b"\x04>")

    def _run(self, code: str) -> tuple[str, str]:
        # A real board keeps one running interpreter: globals set by one
        # Ctrl-D execution are still there for the next one in the same (or
        # a later) raw-REPL session, so `self._globals` persists across
        # calls rather than being rebuilt each time.
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                exec(code, self._globals)  # noqa: S102 - test double
        except Exception as exc:  # noqa: BLE001
            stderr.write(
                f'Traceback (most recent call last):\r\n  File "<stdin>", line 1, in <module>\r\n'
                f"{type(exc).__name__}: {exc}\r\n"
            )
        return stdout.getvalue().replace("\n", "\r\n"), stderr.getvalue()
