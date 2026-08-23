# fpgas-tt phase 2 — FPGA designs API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Pi daemon the FPGA-board API from the spec — `GET /designs`, `POST /designs/<name>/enable`, `POST /bitstream`, `POST /demos/sync` — implemented as raw-REPL *tasks that are clients of the bridge* (the bridge stays the only owner of the serial port), with demo metadata merged from `/usr/share/fpgas-tt/demos/index.json`.

**Architecture:** A new `fpgas_tt/repl.py` runs one raw-REPL exchange at a time (asyncio lock) through a `Bridge.subscribe()` client: `Ctrl-C Ctrl-C Ctrl-A`, code, `Ctrl-D`, parse `OK<out>\x04<err>\x04>`, then `Ctrl-B` back to the friendly REPL so viewers' Commanders keep working. Any framing mismatch (someone else typing) fails the task with the captured bytes. `fpgas_tt/designs.py` builds the REPL snippets (list, enable, chunked base64 file write, stat) and merges board files with the demo index; `server.py` exposes the routes for `kind == "fpga"` (404 `{"error": …}` otherwise). Tests use a **fake MicroPython raw REPL** on the existing pty fixture.

**Tech Stack:** Python 3.11 (bookworm), aiohttp 3.8, pyserial-asyncio, pytest/pytest-aiohttp, ruff; nfpm deb (unchanged packaging, one new CLI flag + one new directory).

**Spec:** `fpgas.online-infra/docs/superpowers/specs/2026-08-22-tinytapeout-fpgas-online-design.md` §5.2, §5.3, §5.5, §9. Board-side facts verified on hardware 2026-08-23 (TT SDK 3.1.0, FPGA breakout): `tt.shuttle.projects.all` → list of `BitStream(name, file, project_index, clock_hz)`; `tt.shuttle.get(name_or_index).enable()` streams the `.bin` over SPI (~3 s, 104,090 bytes for the iCE40UP5K); `tt.shuttle.enabled` is the enabled `BitStream` or `None`; the index is built lazily once (`tt.shuttle._design_index`) so it must be reset to `None` after files change; bitstreams live in `/bitstreams/<name>.bin`; `tt.clock_project_PWM(hz)` sets the project clock; the REPL globals include `select_design`, `read_rom`, `set_clock_hz`.

## Global Constraints

- Repo `fpgas-online/fpgas.online-tt`; feature branch in `.worktrees/` (gitignored); PRs to `main`; CI (`ci.yml`: lint, test, test-bookworm, deb) green before merge. Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01UdRpsg6jY8PbcQxxX6txKE
  ```
- Python ≥ 3.11 and **aiohttp 3.8.4** (bookworm) must keep working: no aiohttp ≥ 3.9-only API, no `web.AppKey`. `pytest` runs with `filterwarnings = error`.
- The bridge remains the single owner of the port: tasks use `Bridge.subscribe()`/`Client.write()`/`Client.read()` only; never open the device.
- Wire contract (consumed by the Commander fork and the Django site — do not deviate):
  - `GET /designs` → 200 `{"enabled": "<name>|null", "designs": [{"name","title","author","description","docs_url","repo_url","clock_hz","pinout","source"}]}` sorted by `name`; `source` ∈ `"demo"|"upload"`; non-demo fields default to `""`/`null`/`[]` (`clock_hz` default `null`, `pinout` default `{}`).
  - `POST /designs/<name>/enable` (JSON body `{"clock_hz": int}` optional) → 200 `{"enabled": "<name>", "clock_hz": int|null}`; 404 `{"error": "no such design", "detail": ""}`.
  - `POST /bitstream` multipart fields `name`, `file` → 201 `{"name", "size", "evicted": [names]}`; 400 for validation errors (`{"error": "<message>", "detail": ""}`); 409 if `name` is a demo name.
  - `POST /demos/sync` → 200 `{"synced": [names], "skipped": [names]}`.
  - All four → 404 `{"error": "not an fpga board", "detail": ""}` when `config.kind != "fpga"`; 503 `{"error": "board not present", "detail": ""}` when the bridge has no port; 409 `{"error": "another task is running", "detail": ""}` when the task lock is busy (we do not queue); 502 `{"error": "REPL task failed", "detail": "<captured bytes, utf-8 errors replaced, ≤ 2000 chars>"}` on protocol mismatch / board exception.
- Validation constants: upload ≤ `MAX_BITSTREAM_BYTES = 256 * 1024`; iCE40 preamble `b"\x7e\xaa\x99\x7e"` must appear within the first 64 bytes; `NAME_RE = ^[a-z0-9_]{1,40}$`; `MAX_UPLOADS = 16` (oldest `source: upload` by board mtime evicted first); demos dir default `/usr/share/fpgas-tt/demos` (`--demos-dir`), index file `index.json` with shape `{"demos": [{"name","title","author","description","docs_url","repo_url","clock_hz","pinout":{"ui_in":[8 strings],"uo_out":[8],"uio":[8]}}]}`; a missing index ⇒ no demos (not an error).
- REPL framing (raw mode): send `b"\r\x03\x03"`, then `b"\r\x01"`, expect `b"raw REPL; CTRL-B to exit\r\n>"`; send `code + b"\x04"`; expect `b"OK"`; read stdout until `b"\x04"`; read stderr until `b"\x04"`; expect `b">"`; finally send `b"\r\x02"` (friendly REPL) whether or not the task succeeded. Per-step timeout 10 s, file write steps 60 s.
- ISO dates; `uv run …`; `/tmp` never (use `tmp_path`).

---

## File structure

```
src/fpgas_tt/repl.py          raw-REPL task runner over a bridge Client (lock, framing, timeouts)
src/fpgas_tt/designs.py       snippets + merge logic: list/enable/write/stat/sync, demo index loader, validation
src/fpgas_tt/server.py        (modify) routes, --demos-dir, auto-sync on first board open
tests/fakerepl.py             fake MicroPython raw REPL driving the pty master (fake os/tt over tmp_path)
tests/test_repl.py            framing, lock, interference, timeouts
tests/test_designs.py         list/merge, validation, enable, upload (+eviction), sync
tests/data/demos/index.json   fixture demo index + two .bin files
README.md                     (modify) API table
```

---

### Task 1: `repl.py` — raw-REPL task runner as a bridge client

**Files:**
- Create: `src/fpgas_tt/repl.py`
- Create: `tests/fakerepl.py`, `tests/test_repl.py`

**Interfaces:**
- Produces: `class ReplRunner(bridge)`, `async def ReplRunner.exec(code: str, *, timeout: float = 10.0) -> str` (stdout of the snippet; raises `ReplError(message, detail)`), `async def ReplRunner.exec_steps(steps: list[str], timeout=…)` (one raw-REPL session, several `Ctrl-D` executions — used by the chunked file write), `class ReplError(Exception)` with `.detail: str`, `class ReplBusy(ReplError)`, `class ReplNoBoard(ReplError)`.

- [ ] **Step 1: The fake raw REPL (test helper)** — `tests/fakerepl.py`:
```python
"""A fake MicroPython raw REPL on the FakeBoard pty: enough protocol for the daemon's tasks.

It runs *real* Python for the submitted code, with a sandboxed `os` whose
filesystem root is a tmp dir (so '/bitstreams/x.bin' maps to tmp/bitstreams/x.bin)
and a fake `tt` object exposing the FPGA shuttle API the daemon relies on.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import os
import contextlib
import posixpath
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
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, os.read, self.master_fd, n)

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
        g = {"os": self.fos, "tt": self.tt, "__builtins__": builtins, "__name__": "__main__"}
        # MicroPython's open() is relative to its own FS root
        def _open(path, mode="r", *a, **k):
            return builtins.open(self.fos._p(path), mode, *a, **k)
        g["open"] = _open
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                exec(code, g)  # noqa: S102 - test double
        except Exception as exc:  # noqa: BLE001
            stderr.write(f"Traceback (most recent call last):\r\n  File \"<stdin>\", line 1, in <module>\r\n{type(exc).__name__}: {exc}\r\n")
        return stdout.getvalue().replace("\n", "\r\n"), stderr.getvalue()
```
and a fixture in `tests/conftest.py` (append):
```python
@pytest.fixture
async def fake_repl(fake_board, tmp_path):
    from tests.fakerepl import FakeRepl  # noqa: PLC0415 - test helper

    repl = FakeRepl(master_fd=fake_board.master, root=tmp_path / "board")
    (tmp_path / "board").mkdir()
    repl.__post_init__()
    repl.start()
    yield repl
    await repl.stop()
```
(`fake_board` keeps owning the fds; `FakeRepl` only reads/writes the master. `tests/__init__.py` exists so `from tests.fakerepl import …` resolves with `testpaths = ["tests"]`.)

- [ ] **Step 2: Failing tests** — `tests/test_repl.py`:
```python
import asyncio

import pytest

from fpgas_tt.bridge import Bridge
from fpgas_tt.repl import ReplBusy, ReplError, ReplNoBoard, ReplRunner


async def wait_for(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture
async def bridge(fake_board):
    b = Bridge(str(fake_board.path), reopen_interval=0.05)
    await b.start()
    await wait_for(lambda: b.present)
    yield b
    await b.stop()


async def test_exec_returns_stdout_and_restores_friendly_repl(bridge, fake_repl):
    runner = ReplRunner(bridge)
    out = await runner.exec("print(1 + 1)")
    assert out == "2\r\n"
    # the session ends with Ctrl-B so viewers get their friendly REPL back
    assert fake_repl.transcript.endswith(b"\r\x02")


async def test_exec_surfaces_board_exception_as_repl_error(bridge, fake_repl):
    runner = ReplRunner(bridge)
    with pytest.raises(ReplError) as ei:
        await runner.exec("raise ValueError('nope')")
    assert "ValueError: nope" in ei.value.detail
    assert fake_repl.transcript.endswith(b"\r\x02")


async def test_exec_steps_runs_several_snippets_in_one_session(bridge, fake_repl):
    runner = ReplRunner(bridge)
    outs = await runner.exec_steps(["x = 40", "print(x + 2)"])
    assert outs == ["", "42\r\n"]
    assert fake_repl.transcript.count(b"\x01") == 1  # one raw-REPL entry


async def test_concurrent_tasks_are_refused_not_queued(bridge, fake_repl):
    runner = ReplRunner(bridge)
    first = asyncio.create_task(runner.exec("import time\nprint('slow')", timeout=5))
    await wait_for(lambda: runner.busy)
    with pytest.raises(ReplBusy):
        await runner.exec("print('second')")
    assert await first == "slow\r\n"


async def test_interference_fails_the_task_with_detail(bridge, fake_repl):
    fake_repl.echo_junk = b"someone typed this\r\n>>> "
    runner = ReplRunner(bridge)
    with pytest.raises(ReplError) as ei:
        await runner.exec("print(1)")
    assert "someone typed this" in ei.value.detail
    assert fake_repl.transcript.endswith(b"\r\x02")


async def test_no_board(tmp_path):
    bridge = Bridge(str(tmp_path / "missing"), reopen_interval=0.05)
    await bridge.start()
    try:
        with pytest.raises(ReplNoBoard):
            await ReplRunner(bridge).exec("print(1)")
    finally:
        await bridge.stop()


async def test_timeout_when_board_is_silent(bridge, fake_board):
    # no FakeRepl running: nothing ever answers Ctrl-A
    runner = ReplRunner(bridge)
    with pytest.raises(ReplError) as ei:
        await runner.exec("print(1)", timeout=0.3)
    assert "timed out" in str(ei.value)
```

- [ ] **Step 3: Run to verify failure** — `uv run pytest tests/test_repl.py -q` → ImportError (`fpgas_tt.repl`).

- [ ] **Step 4: Implement** — `src/fpgas_tt/repl.py`:
```python
"""Run MicroPython raw-REPL snippets through the bridge — as just another client.

One task at a time (``ReplBusy`` otherwise, never queued: a viewer who clicks
twice should see "busy", not a growing backlog). The exchange is framed exactly
as MicroPython does it; any deviation — typically another viewer typing into
the shared REPL — fails the task with the captured bytes so the UI can say
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
                with_suppress = True
                try:
                    await client.write(CTRL_B)
                except BoardNotPresent:
                    with_suppress = False
                client.close()
                if not with_suppress:
                    log.warning("repl: board went away before the session could be closed")


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
        self._buf = self._buf[len(token):]

    async def _read_until(self, token: bytes, what: str) -> bytes:
        while token not in self._buf:
            await self._fill(what)
        out, self._buf = self._buf.split(token, 1)
        return out
```
Note on `_expect`: when the buffer holds *more* than the token that is fine (the rest is the next field). Interference shows up as a prefix mismatch.

- [ ] **Step 5: Run tests** — `uv run pytest tests/test_repl.py -q` → all pass; `uv run ruff check .`.

- [ ] **Step 6: Commit** — `git add src/fpgas_tt/repl.py tests/fakerepl.py tests/test_repl.py tests/conftest.py && git commit -m "feat(repl): raw-REPL task runner as a bridge client (one task at a time, framing checks, Ctrl-B restore)"` + trailer.

---

### Task 2: `designs.py` — snippets, demo index, validation, merge

**Files:**
- Create: `src/fpgas_tt/designs.py`, `tests/test_designs.py`, `tests/data/demos/index.json`, `tests/data/demos/tt_um_demo_a.bin`, `tests/data/demos/tt_um_demo_b.bin`

**Interfaces:**
- Consumes: `ReplRunner` (Task 1).
- Produces: `load_demo_index(demos_dir: Path) -> dict[str, dict]` (name → metadata, `{}` if no index); `validate_bitstream(name: str, data: bytes, demo_names: set[str]) -> None` (raises `ValidationError(message, status)` with status 400/409); `async list_designs(runner, demos_dir) -> dict` (the `GET /designs` body); `async enable_design(runner, name, clock_hz) -> dict`; `async write_bitstream(runner, name, data) -> None`; `async evict_uploads(runner, demo_names, keep=MAX_UPLOADS-1) -> list[str]`; `async sync_demos(runner, demos_dir) -> dict`; constants `MAX_BITSTREAM_BYTES`, `MAX_UPLOADS`, `NAME_RE`, `ICE40_PREAMBLE`, `DEMOS_DIR_DEFAULT`.

- [ ] **Step 1: Fixture data** — `tests/data/demos/index.json`:
```json
{"demos": [
  {"name": "tt_um_demo_a", "title": "Demo A", "author": "fpgas.online", "description": "First demo",
   "docs_url": "https://example.org/a", "repo_url": "https://github.com/fpgas-online/tinytapeout-fpga-demos",
   "clock_hz": 1000, "pinout": {"ui_in": ["a0","a1","a2","a3","a4","a5","a6","a7"], "uo_out": ["o0","o1","o2","o3","o4","o5","o6","o7"], "uio": ["","","","","","","",""]}},
  {"name": "tt_um_demo_b", "title": "Demo B", "author": "fpgas.online", "description": "Second demo",
   "docs_url": "", "repo_url": "", "clock_hz": null, "pinout": {}}
]}
```
`tt_um_demo_a.bin` / `tt_um_demo_b.bin`: 300 bytes each, starting with `b"\xff\x00" + b"\x7e\xaa\x99\x7e"` then `b"A"`/`b"B"` repeated (create with a tiny script in the step; commit the binaries).

- [ ] **Step 2: Failing tests** — `tests/test_designs.py`:
```python
import asyncio
import json
from pathlib import Path

import pytest

from fpgas_tt import designs
from fpgas_tt.bridge import Bridge
from fpgas_tt.designs import ValidationError, validate_bitstream
from fpgas_tt.repl import ReplRunner

DEMOS = Path(__file__).parent / "data" / "demos"
PRE = b"\x7e\xaa\x99\x7e"


async def wait_for(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture
async def runner(fake_board, fake_repl):
    b = Bridge(str(fake_board.path), reopen_interval=0.05)
    await b.start()
    await wait_for(lambda: b.present)
    yield ReplRunner(b)
    await b.stop()


def board_file(fake_repl, name: str) -> Path:
    return fake_repl.root / "bitstreams" / f"{name}.bin"


def test_demo_index_loads_and_missing_dir_is_empty(tmp_path):
    idx = designs.load_demo_index(DEMOS)
    assert set(idx) == {"tt_um_demo_a", "tt_um_demo_b"}
    assert idx["tt_um_demo_a"]["clock_hz"] == 1000
    assert designs.load_demo_index(tmp_path / "nope") == {}


@pytest.mark.parametrize(
    "name,data,status,msg",
    [
        ("Bad Name", PRE + b"x" * 10, 400, "name"),
        ("a" * 41, PRE + b"x" * 10, 400, "name"),
        ("ok_name", b"\x00" * 70 + PRE, 400, "preamble"),
        ("ok_name", PRE + b"x" * (256 * 1024), 400, "large"),
        ("tt_um_demo_a", PRE + b"x" * 10, 409, "demo"),
    ],
)
def test_validate_bitstream_rejects(name, data, status, msg):
    with pytest.raises(ValidationError) as ei:
        validate_bitstream(name, data, {"tt_um_demo_a"})
    assert ei.value.status == status
    assert msg in str(ei.value).lower()


def test_validate_bitstream_accepts_preamble_anywhere_in_first_64_bytes():
    validate_bitstream("my_design", b"\xff" * 60 + PRE + b"\x00" * 100, set())


async def test_list_designs_merges_board_files_with_demo_index(runner, fake_repl):
    board_file(fake_repl, "tt_um_demo_a").write_bytes(PRE)
    board_file(fake_repl, "my_upload").write_bytes(PRE)
    body = await designs.list_designs(runner, DEMOS)
    assert body["enabled"] is None
    names = [d["name"] for d in body["designs"]]
    assert names == ["my_upload", "tt_um_demo_a"]
    a, u = body["designs"][1], body["designs"][0]
    assert a["source"] == "demo" and a["title"] == "Demo A" and a["clock_hz"] == 1000
    assert u["source"] == "upload" and u["title"] == "" and u["clock_hz"] is None and u["pinout"] == {}


async def test_enable_design_and_report_enabled(runner, fake_repl):
    board_file(fake_repl, "tt_um_demo_a").write_bytes(PRE)
    out = await designs.enable_design(runner, "tt_um_demo_a", clock_hz=1000)
    assert out == {"enabled": "tt_um_demo_a", "clock_hz": 1000}
    assert fake_repl.tt.shuttle.enable_log == ["tt_um_demo_a"]
    assert fake_repl.tt.clock_log == [1000]
    body = await designs.list_designs(runner, DEMOS)
    assert body["enabled"] == "tt_um_demo_a"


async def test_enable_unknown_design_raises_not_found(runner, fake_repl):
    with pytest.raises(designs.DesignNotFound):
        await designs.enable_design(runner, "nope", clock_hz=None)


async def test_write_bitstream_round_trips_and_refreshes_index(runner, fake_repl):
    data = PRE + bytes(range(256)) * 3  # 772 bytes: several chunks, last one partial
    await designs.write_bitstream(runner, "my_upload", data)
    assert board_file(fake_repl, "my_upload").read_bytes() == data
    # the shuttle index is rebuilt: the new design is enable-able
    await designs.enable_design(runner, "my_upload", clock_hz=None)


async def test_evict_uploads_keeps_newest_and_never_touches_demos(runner, fake_repl):
    import os
    import time

    for i in range(designs.MAX_UPLOADS):
        p = board_file(fake_repl, f"u{i:02d}")
        p.write_bytes(PRE)
        os.utime(p, (time.time() - 1000 + i, time.time() - 1000 + i))
    board_file(fake_repl, "tt_um_demo_a").write_bytes(PRE)  # demo, oldest mtime possible
    evicted = await designs.evict_uploads(runner, {"tt_um_demo_a"}, keep=designs.MAX_UPLOADS - 1)
    assert evicted == ["u00"]
    assert not board_file(fake_repl, "u00").exists()
    assert board_file(fake_repl, "tt_um_demo_a").exists()


async def test_sync_demos_copies_missing_and_skips_same_size(runner, fake_repl):
    board_file(fake_repl, "tt_um_demo_b").write_bytes((DEMOS / "tt_um_demo_b.bin").read_bytes())
    out = await designs.sync_demos(runner, DEMOS)
    assert out == {"synced": ["tt_um_demo_a"], "skipped": ["tt_um_demo_b"]}
    assert board_file(fake_repl, "tt_um_demo_a").read_bytes() == (DEMOS / "tt_um_demo_a.bin").read_bytes()
```

- [ ] **Step 3: Run to verify failure** — `uv run pytest tests/test_designs.py -q` → ImportError.

- [ ] **Step 4: Implement** — `src/fpgas_tt/designs.py`:
```python
"""FPGA-board tasks: list/enable/upload bitstreams, sync demos — all raw-REPL snippets run through ReplRunner.

Board-side facts (TT SDK 3.1.0, FPGA breakout): bitstreams live in /bitstreams/<name>.bin;
``tt.shuttle.projects.all`` lists them; ``tt.shuttle.get(name).enable()`` loads one;
``tt.shuttle.enabled`` is the loaded one; the index is cached in ``tt.shuttle._design_index``
and must be reset after the directory changes; ``tt.clock_project_PWM(hz)`` sets the clock.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

from fpgas_tt.repl import ReplError, ReplRunner

log = logging.getLogger(__name__)

DEMOS_DIR_DEFAULT = Path("/usr/share/fpgas-tt/demos")
MAX_BITSTREAM_BYTES = 256 * 1024
MAX_UPLOADS = 16
NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")
ICE40_PREAMBLE = b"\x7e\xaa\x99\x7e"
PREAMBLE_WINDOW = 64
CHUNK = 256  # raw bytes per REPL write step (344 base64 chars on the wire)
META_FIELDS = ("title", "author", "description", "docs_url", "repo_url")


class ValidationError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class DesignNotFound(Exception):
    pass


# -- demo index --
def load_demo_index(demos_dir: Path) -> dict[str, dict]:
    path = Path(demos_dir) / "index.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    out: dict[str, dict] = {}
    for d in doc.get("demos", []):
        if "name" in d:
            out[d["name"]] = d
    return out


def _meta(name: str, demos: dict[str, dict]) -> dict:
    d = demos.get(name)
    if d is None:
        return {"name": name, "title": "", "author": "", "description": "", "docs_url": "", "repo_url": "",
                "clock_hz": None, "pinout": {}, "source": "upload"}
    return {"name": name, **{k: d.get(k, "") for k in META_FIELDS}, "clock_hz": d.get("clock_hz"),
            "pinout": d.get("pinout") or {}, "source": "demo"}


# -- validation --
def validate_bitstream(name: str, data: bytes, demo_names: set[str]) -> None:
    if not NAME_RE.match(name or ""):
        raise ValidationError("name must match ^[a-z0-9_]{1,40}$")
    if name in demo_names:
        raise ValidationError(f"{name} is a demo name; pick another", 409)
    if len(data) > MAX_BITSTREAM_BYTES:
        raise ValidationError(f"bitstream too large ({len(data)} bytes, limit {MAX_BITSTREAM_BYTES})")
    if ICE40_PREAMBLE not in data[:PREAMBLE_WINDOW + len(ICE40_PREAMBLE)]:
        raise ValidationError("not an iCE40 bitstream (preamble 7E AA 99 7E missing in the first 64 bytes)")


# -- snippets --
LIST_CODE = (
    "import os, json\n"
    "names = sorted(f[:-4] for f in os.listdir('/bitstreams') if f.endswith('.bin'))\n"
    "en = tt.shuttle.enabled\n"
    "print(json.dumps({'names': names, 'enabled': en.name if en else None}))\n"
)

STAT_CODE = (
    "import os, json\n"
    "out = {}\n"
    "for f in os.listdir('/bitstreams'):\n"
    "    if f.endswith('.bin'):\n"
    "        st = os.stat('/bitstreams/' + f)\n"
    "        out[f[:-4]] = [st[6], st[8]]\n"
    "print(json.dumps(out))\n"
)

REFRESH_CODE = "tt.shuttle._design_index = None\n"


def _enable_code(name: str, clock_hz: int | None) -> str:
    code = f"tt.shuttle.get({name!r}).enable()\n"
    if clock_hz:
        code += f"tt.clock_project_PWM({int(clock_hz)})\n"
    code += "print('enabled')\n"
    return code


async def _board_names(runner: ReplRunner) -> tuple[list[str], str | None]:
    out = json.loads(await runner.exec(LIST_CODE))
    return out["names"], out["enabled"]


async def list_designs(runner: ReplRunner, demos_dir: Path) -> dict:
    names, enabled = await _board_names(runner)
    demos = load_demo_index(demos_dir)
    return {"enabled": enabled, "designs": [_meta(n, demos) for n in names]}


async def enable_design(runner: ReplRunner, name: str, clock_hz: int | None) -> dict:
    names, _ = await _board_names(runner)
    if name not in names:
        raise DesignNotFound(name)
    await runner.exec(_enable_code(name, clock_hz), timeout=30.0)  # the SPI load takes a few seconds
    return {"enabled": name, "clock_hz": clock_hz}


def _write_steps(name: str, data: bytes) -> list[str]:
    steps = ["import binascii\n" f"f = open('/bitstreams/{name}.bin', 'wb')\n"]
    for i in range(0, len(data), CHUNK):
        b64 = base64.b64encode(data[i:i + CHUNK]).decode("ascii")
        steps.append(f"f.write(binascii.a2b_base64('{b64}'))\n")
    steps.append(
        "f.close()\n"
        "import os\n"
        f"print(os.stat('/bitstreams/{name}.bin')[6])\n"
        + REFRESH_CODE
    )
    return steps


async def write_bitstream(runner: ReplRunner, name: str, data: bytes) -> None:
    outs = await runner.exec_steps(_write_steps(name, data), timeout=60.0)
    size = int(outs[-1].strip() or -1)
    if size != len(data):
        raise ReplError(f"board reports {size} bytes after writing {len(data)}", "")


async def evict_uploads(runner: ReplRunner, demo_names: set[str], keep: int) -> list[str]:
    """Delete the oldest uploads so that at most `keep` remain (demos are never touched)."""
    stats = json.loads(await runner.exec(STAT_CODE))
    uploads = sorted((mtime, n) for n, (_size, mtime) in stats.items() if n not in demo_names)
    victims = [n for _m, n in uploads[: max(0, len(uploads) - keep)]]
    if victims:
        code = "import os\n" + "".join(f"os.remove('/bitstreams/{n}.bin')\n" for n in victims) + REFRESH_CODE + "print('ok')\n"
        await runner.exec(code)
    return victims


async def sync_demos(runner: ReplRunner, demos_dir: Path) -> dict:
    demos = load_demo_index(demos_dir)
    stats = json.loads(await runner.exec(STAT_CODE))
    synced, skipped = [], []
    for name in sorted(demos):
        src = Path(demos_dir) / f"{name}.bin"
        if not src.exists():
            log.warning("demos: %s listed in index.json but %s is missing", name, src)
            continue
        data = src.read_bytes()
        if name in stats and stats[name][0] == len(data):
            skipped.append(name)
            continue
        await write_bitstream(runner, name, data)
        synced.append(name)
    return {"synced": synced, "skipped": skipped}
```

- [ ] **Step 5: Run tests** — `uv run pytest tests/test_designs.py tests/test_repl.py -q`; `uv run ruff check .`.

- [ ] **Step 6: Commit** — `git add src/fpgas_tt/designs.py tests/test_designs.py tests/data/demos && git commit -m "feat(designs): list/enable/upload/evict/sync FPGA bitstreams via raw-REPL tasks; demo index merge; validation"` + trailer.

---

### Task 3: HTTP routes + `--demos-dir` + auto-sync

**Files:**
- Modify: `src/fpgas_tt/server.py`, `tests/test_server.py`, `README.md`

**Interfaces:**
- Consumes: `ReplRunner`, `designs.*`.
- Produces: the four routes (contract in Global Constraints); `create_app(..., demos_dir: Path = DEMOS_DIR_DEFAULT)`; `app["repl"]`; CLI `--demos-dir`; on startup for `kind == "fpga"`: a background task that waits for `bridge.present`, runs `sync_demos` once (errors logged, retried every 30 s until it succeeds once), then exits.

- [ ] **Step 1: Failing tests** — append to `tests/test_server.py`:
```python
from pathlib import Path

from fpgas_tt.designs import ICE40_PREAMBLE

DEMOS = Path(__file__).parent / "data" / "demos"
FPGA_CFG = BoardConfig(slug="fpga-1", kind="fpga", switch=2, port=33, hostname="pi-sw2-p33")


@pytest.fixture
async def fpga_client(aiohttp_client, bridge, fake_repl, tmp_path):
    # an empty demos dir: auto-sync has nothing to do and the tests control the board contents
    empty = tmp_path / "nodemos"
    empty.mkdir()
    return await aiohttp_client(create_app(bridge, FPGA_CFG, demos_dir=empty))


async def test_fpga_routes_404_on_asic_board(client):
    for method, path in (("GET", "/designs"), ("POST", "/designs/x/enable"), ("POST", "/bitstream"), ("POST", "/demos/sync")):
        resp = await client.request(method, path)
        assert resp.status == 404
        assert (await resp.json())["error"] == "not an fpga board"


async def test_designs_list_enable_and_upload_flow(fpga_client, fake_repl):
    (fake_repl.root / "bitstreams" / "tt_um_factory_test.bin").write_bytes(ICE40_PREAMBLE)
    body = await (await fpga_client.get("/designs")).json()
    assert [d["name"] for d in body["designs"]] == ["tt_um_factory_test"]
    assert body["enabled"] is None

    resp = await fpga_client.post("/designs/tt_um_factory_test/enable", json={"clock_hz": 100})
    assert resp.status == 200
    assert await resp.json() == {"enabled": "tt_um_factory_test", "clock_hz": 100}
    assert (await fpga_client.post("/designs/nope/enable")).status == 404

    data = ICE40_PREAMBLE + b"\x01" * 500
    form = aiohttp.FormData()
    form.add_field("name", "my_design")
    form.add_field("file", data, filename="my_design.bin", content_type="application/octet-stream")
    resp = await fpga_client.post("/bitstream", data=form)
    assert resp.status == 201, await resp.text()
    assert await resp.json() == {"name": "my_design", "size": len(data), "evicted": []}
    assert (fake_repl.root / "bitstreams" / "my_design.bin").read_bytes() == data
    names = [d["name"] for d in (await (await fpga_client.get("/designs")).json())["designs"]]
    assert names == ["my_design", "tt_um_factory_test"]


async def test_upload_validation_errors(fpga_client):
    form = aiohttp.FormData()
    form.add_field("name", "Bad Name")
    form.add_field("file", ICE40_PREAMBLE, filename="x.bin")
    resp = await fpga_client.post("/bitstream", data=form)
    assert resp.status == 400
    assert "name" in (await resp.json())["error"]
    resp = await fpga_client.post("/bitstream", data=aiohttp.FormData())  # no fields at all
    assert resp.status == 400


async def test_demos_sync_route_and_auto_sync_on_start(aiohttp_client, bridge, fake_repl):
    c = await aiohttp_client(create_app(bridge, FPGA_CFG, demos_dir=DEMOS))
    await wait_for(lambda: (fake_repl.root / "bitstreams" / "tt_um_demo_b.bin").exists(), timeout=5)
    resp = await c.post("/demos/sync")
    assert resp.status == 200
    assert await resp.json() == {"synced": [], "skipped": ["tt_um_demo_a", "tt_um_demo_b"]}


async def test_board_absent_gives_503(aiohttp_client, tmp_path):
    bridge = Bridge(str(tmp_path / "missing"), reopen_interval=0.05)
    await bridge.start()
    try:
        c = await aiohttp_client(create_app(bridge, FPGA_CFG, demos_dir=tmp_path))
        resp = await c.get("/designs")
        assert resp.status == 503
        assert (await resp.json())["error"] == "board not present"
    finally:
        await bridge.stop()


def test_parser_demos_dir_default():
    assert build_parser().parse_args([]).demos_dir == "/usr/share/fpgas-tt/demos"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_server.py -q` → `create_app() got an unexpected keyword argument 'demos_dir'`.

- [ ] **Step 3: Implement** — in `server.py`:
```python
from pathlib import Path

from fpgas_tt import designs
from fpgas_tt.designs import DEMOS_DIR_DEFAULT, DesignNotFound, ValidationError
from fpgas_tt.repl import ReplBusy, ReplError, ReplNoBoard, ReplRunner

DEMO_SYNC_RETRY = 30.0


def create_app(bridge, config, *, version=__version__, config_error=None, demos_dir: Path | str = DEMOS_DIR_DEFAULT):
    ...
    app["demos_dir"] = Path(demos_dir)
    app["repl"] = ReplRunner(bridge)
    app.add_routes([
        web.get("/health", health), web.get("/serial", serial_ws),
        web.get("/designs", designs_list), web.post("/designs/{name}/enable", designs_enable),
        web.post("/bitstream", bitstream_upload), web.post("/demos/sync", demos_sync),
    ])
    app.on_shutdown.append(close_websockets)
    if config.kind == "fpga":
        app.on_startup.append(start_demo_sync)
        app.on_cleanup.append(stop_demo_sync)
    return app


def _json_error(status: int, error: str, detail: str = "") -> web.Response:
    return web.json_response({"error": error, "detail": detail}, status=status)


def _fpga_only(request: web.Request) -> web.Response | None:
    if request.app["config"].kind != "fpga":
        return _json_error(404, "not an fpga board")
    return None


async def _run(request: web.Request, coro):
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


async def designs_list(request):
    if (err := _fpga_only(request)):
        return err
    async def go():
        return web.json_response(await designs.list_designs(request.app["repl"], request.app["demos_dir"]))
    return await _run(request, go())


async def designs_enable(request):
    if (err := _fpga_only(request)):
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
        return web.json_response(await designs.enable_design(request.app["repl"], request.match_info["name"], clock_hz))
    return await _run(request, go())


async def bitstream_upload(request):
    if (err := _fpga_only(request)):
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


async def demos_sync(request):
    if (err := _fpga_only(request)):
        return err
    async def go():
        return web.json_response(await designs.sync_demos(request.app["repl"], request.app["demos_dir"]))
    return await _run(request, go())


async def start_demo_sync(app):
    async def loop():
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
            await asyncio.sleep(0.2)
    app["demo_sync_task"] = asyncio.create_task(loop(), name="fpgas-tt-demo-sync")


async def stop_demo_sync(app):
    task = app.get("demo_sync_task")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
```
Note `evict_uploads(keep=MAX_UPLOADS - 1)` runs *before* the write so the new upload is the 16th at most. `build_parser()` gains `--demos-dir` (default `str(DEMOS_DIR_DEFAULT)`), and `main()` passes `demos_dir=args.demos_dir`. Update the module docstring and README API table (the four routes + error shapes + `--demos-dir`).

- [ ] **Step 4: Run all tests** — `uv run pytest -q`; `uv run ruff check .`. Also run the bookworm job locally if docker is available: `docker run --rm -v $PWD:/src -w /src debian:bookworm sh -c 'apt-get update -qq && apt-get install -y -qq python3-aiohttp python3-serial-asyncio python3-yaml python3-pytest python3-pytest-aiohttp python3-pytest-asyncio >/dev/null && python3 -m pytest -q'` (CI runs the equivalent `test-bookworm`).

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(server): /designs, /designs/<name>/enable, /bitstream, /demos/sync for fpga boards; --demos-dir; demo auto-sync"` + trailer.

---

### Task 4: Packaging + docs + PR

**Files:**
- Modify: `nfpm.yaml` (add the empty directory `/usr/share/fpgas-tt/demos` owned by the package so `--demos-dir` always exists: `contents: - dst: /usr/share/fpgas-tt/demos type: dir`), `README.md` (done in Task 3, re-check), `debian/fpgas-tt.service` (unchanged — the default path is compiled in).

- [ ] **Step 1: nfpm** — add the `type: dir` entry; `uv run pytest tests/test_packaging.py -q` (it builds the deb with nfpm if present; keep green).
- [ ] **Step 2: Lint + full tests** — `uv run ruff check . && uv run pytest -q`.
- [ ] **Step 3: Commit, push, PR** — branch `fpga-designs-api`; PR title `fpgas-tt: FPGA designs API (list/enable/upload/sync) as bridge-client REPL tasks`; wait for CI (lint, test, test-bookworm, deb) green; merge; confirm `build-deb.yml` uploads `fpgas-online-tt_0.0.postN_all.deb` to the `v0.0` release.

---

## Self-review

- Spec coverage: §5.3 rows `/designs`, `/designs/<name>/enable`, `/bitstream` (size, preamble, name, demo-name, 16-cap eviction, size verify), `/demos/sync` (+ auto-run after first open), error shape `{error, detail}` (§9 "task corrupted by concurrent typing" → 502 with detail), §5.2 single owner (tasks are clients). `/kianv/boot` and `/build` are out of scope (phases 3/4).
- Placeholders: none; every step carries code.
- Type consistency: `ReplRunner.exec/exec_steps/busy`, `ReplError.detail`, `ValidationError.status`, `DesignNotFound`, `designs.list_designs/enable_design/write_bitstream/evict_uploads/sync_demos/load_demo_index/validate_bitstream`, `create_app(demos_dir=)` used identically across tasks.
