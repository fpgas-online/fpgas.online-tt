"""A fake MicroPython raw REPL on the FakeBoard pty: enough protocol for the daemon's tasks.

It runs *real* Python for the submitted code, with a sandboxed `os` whose
filesystem root is a tmp dir (so '/bitstreams/x.bin' maps to tmp/bitstreams/x.bin)
and a fake `tt` object exposing the FPGA shuttle API the daemon relies on:
`tt.shuttle.enable(design)` hands `design.file` to the fake of the SDK's loader
module (`ttboard.fpga.fabricfoxv2`), which reads it with that module's `open`,
128 bytes at a time, as the SDK's does. What it read is in `FakeRepl.loaded`.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

RAW_BANNER = b"raw REPL; CTRL-B to exit\r\n>"
FRIENDLY_BANNER = b"\r\nMicroPython v1.25 fake; FPGA\r\nType \"help()\" for more information.\r\n>>> "
# What a real board echoes for \r\x01 (CTRL_A) *before* the raw-REPL banner:
# readline's echo of the \r, a re-issued friendly prompt, and pyexec's own
# newline -- present on every real entry, not just an "interference" case.
DEFAULT_RAW_PREAMBLE = b"\r\n>>> \r\n"


class BoardWrite(AssertionError):
    """Something the daemon sent tried to change the board's filesystem. Nothing may (Tim, 2026-10-05)."""


class _FakeOs:
    """os.listdir/stat rooted under `root`, path strings as MicroPython sees them. Everything that would change
    the board's filesystem raises BoardWrite: no snippet of the daemon's may do it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.writes: list[str] = []  # every attempt, kept: a snippet could catch the exception

    def _refuse(self, what: str):
        self.writes.append(what)
        raise BoardWrite(what)

    def _p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def listdir(self, path: str = "/"):
        return sorted(os.listdir(self._p(path)))

    def stat(self, path: str):
        st = os.stat(self._p(path))
        # MicroPython returns a 10-tuple; index 6 = size, 8 = mtime
        return (st.st_mode, 0, 0, 0, 0, 0, st.st_size, int(st.st_atime), int(st.st_mtime), int(st.st_ctime))

    def remove(self, path: str):
        self._refuse(f"os.remove({path!r})")

    def rename(self, src: str, dst: str):
        self._refuse(f"os.rename({src!r}, {dst!r})")

    def mkdir(self, path: str):
        self._refuse(f"os.mkdir({path!r})")

    unlink = remove
    rmdir = remove


@dataclass
class FakeBitStream:
    name: str
    file: str
    project_index: int
    clock_hz: int = 100

    def enable(self, force: bool = False):
        return self._mux.enable(self, force)  # _mux is set by FakeShuttle


class FakeShuttle:
    """tt.shuttle: BitStreamIndex-like over the fake /bitstreams dir."""

    def __init__(self, fos: _FakeOs, loader=None) -> None:
        self._fos = fos
        self._loader = loader
        self._design_index = None
        self.enabled = None
        self.enable_log: list[str] = []

    def enable(self, design, force: bool = False):  # FPGAMux.enable in the real SDK
        self.enabled = design
        self.enable_log.append(design.name)
        if self._loader is not None:
            self._loader.spi_transferPIO(design.file)
        return True

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


# The SDK's loader, as far as the daemon relies on it: it opens the path with the name `open` as its own module
# sees it, reads 128 bytes at a time until an empty read, and (like the SDK's) prints an OSError rather than
# raising it.
FAKE_LOADER = """
def spi_transferPIO(filepath, freq=1_000_000):
    try:
        with open(filepath, 'rb') as f:
            data = b''
            while True:
                chunk = f.read(128)
                if not chunk:
                    break
                data += chunk
        _loaded.append((filepath, data))
    except OSError as e:
        print(f"Error accessing file: {e}")
"""


class _MicroPythonModule(types.ModuleType):
    """A module as MicroPython has it in one respect that matters here: deleting an attribute that is not
    there raises KeyError (seen on a demo board, 2026-10-05), where CPython raises AttributeError."""

    def __delattr__(self, name):
        if name not in self.__dict__:
            raise KeyError(name)
        super().__delattr__(name)


class FakeMuxBitStream:
    """ttboard.fpga.fpga_mux.BitStream."""

    def __init__(self, loader, filepath, name, project_index=0, clock_hz=100):
        self._loader, self.file, self.name = loader, filepath, name
        self.project_index, self.clock_hz = project_index, clock_hz

    def enable(self, force: bool = False):
        self._loader.enable(self, force)


class FakeTT:
    def __init__(self, fos: _FakeOs, loader=None) -> None:
        self.shuttle = FakeShuttle(fos, loader)
        self.clock_log: list[int] = []

    def clock_project_PWM(self, hz: int):
        self.clock_log.append(hz)


@dataclass
class FakeRepl:
    """Drive the pty master like a MicroPython board in raw-REPL mode."""

    master_fd: int
    root: Path
    echo_junk: bytes = b""  # bytes "someone else" injects after the banner (interference tests)
    raw_preamble: bytes = DEFAULT_RAW_PREAMBLE  # pre-banner echo a real board sends; tests may override/blow it up
    transcript: bytearray = field(default_factory=bytearray)

    def __post_init__(self) -> None:
        self.fos = _FakeOs(self.root)
        (self.root / "bitstreams").mkdir(exist_ok=True)
        self._task: asyncio.Task | None = None
        self.soft_resets = 0

        def _open(path, mode="r", *a, **k):  # MicroPython's open() is relative to its own FS root
            if mode not in ("r", "rb", "rt"):
                self.fos._refuse(f"open({path!r}, {mode!r})")
            return builtins.open(self.fos._p(path), mode, *a, **k)

        self.board_writes = self.fos.writes

        # The SDK's modules a snippet may import. The loader module has no `open` of its own: it finds the
        # board's through its builtins, as on the board.
        self.loaded: list[tuple[str, bytes]] = []
        loader = _MicroPythonModule("ttboard.fpga.fabricfoxv2")
        loader.__dict__.update({"__builtins__": {**vars(builtins), "open": _open}, "_loaded": self.loaded})
        exec(FAKE_LOADER, loader.__dict__)  # noqa: S102 - test double
        fpga_mux = types.ModuleType("ttboard.fpga.fpga_mux")
        fpga_mux.BitStream = FakeMuxBitStream
        fpga = types.ModuleType("ttboard.fpga")
        fpga.fabricfoxv2, fpga.fpga_mux = loader, fpga_mux
        ttboard = types.ModuleType("ttboard")
        ttboard.fpga = fpga
        self.loader = loader
        self._modules = {"ttboard": ttboard, "ttboard.fpga": fpga, "ttboard.fpga.fabricfoxv2": loader,
                         "ttboard.fpga.fpga_mux": fpga_mux}  # fmt: skip
        self.tt = FakeTT(self.fos, loader)

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
                        await self._write(self.raw_preamble + RAW_BANNER + self.echo_junk)
                        continue
                    if b"\x04" in buf:
                        # Ctrl-D at the friendly prompt: a soft reset. The interpreter starts afresh (its
                        # globals are gone) and runs the board's main.py, whose output goes to the port.
                        buf = buf.split(b"\x04", 1)[1]
                        self.soft_resets += 1
                        out, err = self.soft_reset()
                        await self._write(b"\r\nMPY: soft reboot\r\n" + out.encode() + err.encode() + FRIENDLY_BANNER)
                        continue
                    buf = b""  # friendly mode: swallow (Ctrl-C, newlines, ...)
                    break
                # raw mode
                if buf.startswith(b"\r\x02") or buf.startswith(b"\x02"):
                    # ReplRunner always sends CTRL_B as b"\r\x02" (the leading
                    # \r is a defensive "clear any partial line" byte, as with
                    # ENTER_RAW); a bare \x02 is also accepted.
                    n = 2 if buf.startswith(b"\r\x02") else 1
                    raw = False
                    buf = buf[n:]
                    await self._write(FRIENDLY_BANNER)
                    continue
                if b"\x04" not in buf:
                    break  # wait for the rest of the snippet
                code, buf = buf.split(b"\x04", 1)
                code = code.replace(b"\r", b"")
                out, err = self._run(code.decode("utf-8", "replace"))
                await self._write(b"OK" + out.encode() + b"\x04" + err.encode() + b"\x04>")

    def soft_reset(self) -> tuple[str, str]:
        """A fresh interpreter: only what the firmware provides survives (here: the sandboxed os and open, and
        `_the_sdk`, a test's stand-in for what the SDK's main.py builds). Then main.py, if the board has one."""
        kept = {k: v for k, v in self._globals.items() if k in ("os", "open", "__builtins__", "__name__", "_the_sdk")}
        self._globals.clear()
        self._globals.update(kept)
        main = self.root / "main.py"
        return self._run(main.read_text()) if main.exists() else ("", "")

    def _run(self, code: str) -> tuple[str, str]:
        # A real board keeps one running interpreter: globals set by one
        # Ctrl-D execution are still there for the next one in the same (or
        # a later) raw-REPL session, so `self._globals` persists across
        # calls rather than being rebuilt each time.
        stdout, stderr = io.StringIO(), io.StringIO()
        # The daemon's snippets do `import os` (real board behaviour); make
        # that resolve to the sandboxed `self.fos` rather than the host's
        # real `os` module, which would otherwise shadow our `os` global
        # and let the snippet see (and touch) the real filesystem.
        sentinel = object()
        mine = {"os": self.fos, **self._modules}
        prior = {name: sys.modules.get(name, sentinel) for name in mine}
        sys.modules.update(mine)
        try:
            with contextlib.redirect_stdout(stdout):
                exec(code, self._globals)  # noqa: S102 - test double
        except Exception as exc:  # noqa: BLE001
            stderr.write(
                f'Traceback (most recent call last):\r\n  File "<stdin>", line 1, in <module>\r\n'
                f"{type(exc).__name__}: {exc}\r\n"
            )
        finally:
            for name, was in prior.items():
                if was is sentinel:
                    del sys.modules[name]
                else:
                    sys.modules[name] = was
        return stdout.getvalue().replace("\n", "\r\n"), stderr.getvalue()
