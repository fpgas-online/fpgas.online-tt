# fpgas-tt daemon (phase 1: bridge + health + packaging) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and package the Pi-side `fpgas-tt` daemon that owns the Tiny Tapeout demo board's serial port and exposes it as a fan-out WebSocket bridge plus a `/health` endpoint, installable from the fpgas.online apt repo into the shared Pi NFS root.

**Architecture:** One asyncio process per Pi. A `Bridge` owns `/dev/ttboard` (one reader task fanning bytes out to every subscribed client, one writer), an aiohttp app exposes `WS /serial` and `GET /health`, and the daemon discovers its own board identity (slug/kind) from its hostname `pi-sw<s>-p<p>` plus the baked `/etc/fpgas-online/tt-boards.yaml`. Packaged with nfpm as an `arch: all` deb; nothing is fetched or generated after boot. Tasks from spec §5.3 (`/designs`, `/bitstream`, `/kianv/boot`, demo sync) are **later phases** and are not in this plan.

**Tech Stack:** Python 3.11 (Debian bookworm), `aiohttp`, `pyserial-asyncio`, `PyYAML`; `pytest` + `pytest-aiohttp`; `ruff`; `uv`; nfpm; GitHub Actions.

**Spec:** `fpgas.online-infra/docs/superpowers/specs/2026-08-22-tinytapeout-fpgas-online-design.md` (§5, §7.3, §9, §10, §12).

## Global Constraints

- Repo: `fpgas-online/fpgas.online-tt` (new). Package name `fpgas-online-tt`; Python package `fpgas_tt`; CLI `fpgas-tt`; systemd unit `fpgas-tt.service`; listens on `0.0.0.0:8765`.
- Python `>=3.11` (bookworm). Runtime deps must be Debian bookworm packages: `python3-aiohttp`, `python3-serial-asyncio`, `python3-yaml`. No pip at runtime.
- Deb is `arch: all`, built by nfpm in GitHub Actions, published to `fpgas-online/apt` via `repository_dispatch` `receive-deb` (exactly like `fpgas.online-setup-pi`).
- Exactly one owner of the serial port (the `Bridge`); every consumer is a client. No arbitration, no locking.
- Slow WebSocket client (> 256 KiB queued) is dropped; the serial reader is never blocked.
- Serial loss: all clients closed with code 1011 reason `board disconnected`; re-open retried every 1 s.
- Configuration is discovered (hostname + baked YAML), never generated. Unknown hostname ⇒ `kind="asic"`, `slug=<hostname>`.
- Errors are never swallowed: every failure path logs and/or returns `{error, detail}`.
- Licence Apache-2.0; ISO dates; `uv` for all Python invocations locally; every change via PR with CI green; feature branches in `.worktrees/` (gitignored); never force-push.
- Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01UdRpsg6jY8PbcQxxX6txKE
  ```

---

## File structure

```
fpgas.online-tt/
├── pyproject.toml                 hatchling; deps; dev deps; ruff config
├── ruff.toml                      line-length 120, rules E/F/W/I (matches sibling repos)
├── README.md                      what/why/how, API table, dev loop
├── CLAUDE.md                      repo context + conventions (sibling-repo style)
├── LICENSE                        Apache-2.0
├── .gitignore                     .venv/, dist/, .worktrees/, __pycache__/, tmp/
├── nfpm.yaml                      deb definition (arch all)
├── bin/fpgas-tt                   shell wrapper: exec python3 -m fpgas_tt "$@"
├── debian/fpgas-tt.service        systemd unit
├── debian/60-fpgas-tt.rules       udev: RP2040/RP2350 CDC -> /dev/ttboard
├── debian/postinstall.sh          systemctl daemon-reload; udevadm control --reload
├── src/fpgas_tt/__init__.py       __version__
├── src/fpgas_tt/__main__.py       python -m fpgas_tt
├── src/fpgas_tt/config.py         hostname parsing, boards YAML, discover()
├── src/fpgas_tt/bridge.py         Bridge, Client, BoardNotPresent
├── src/fpgas_tt/server.py         create_app(), /health, /serial, main()
├── tests/conftest.py              pty fixtures (fake board), event-loop helpers
├── tests/test_config.py
├── tests/test_bridge.py
├── tests/test_server.py
├── .github/workflows/ci.yml       ruff + pytest (+ nfpm build on PRs, artifact only)
├── .github/workflows/build-deb.yml  on v* tags: nfpm build + dispatch to fpgas-online/apt
└── docs/superpowers/plans/2026-08-22-fpgas-tt-daemon-phase1.md   (this file)
```

Responsibilities: `config.py` knows nothing about serial or HTTP; `bridge.py` knows nothing about HTTP or YAML; `server.py` glues and holds the CLI. Tests talk to a fake board through a pty — no hardware.

---

### Task 1: Repository scaffold, CI lint, GitHub repo

**Files:**
- Create: `pyproject.toml`, `ruff.toml`, `.gitignore`, `LICENSE`, `README.md`, `CLAUDE.md`, `src/fpgas_tt/__init__.py`, `src/fpgas_tt/__main__.py`, `tests/__init__.py`, `.github/workflows/ci.yml`
- Plan already present: `docs/superpowers/plans/2026-08-22-fpgas-tt-daemon-phase1.md`

**Interfaces:**
- Produces: `fpgas_tt.__version__ = "0.1.0"`; `python -m fpgas_tt` entry (calls `fpgas_tt.server.main` — added in Task 4; until then `__main__` imports lazily and the module simply exists).

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "fpgas-online-tt"
version = "0.1.0"
description = "Pi-side Tiny Tapeout demo-board bridge daemon for fpgas.online"
readme = "README.md"
license = "Apache-2.0"
requires-python = ">=3.11"
dependencies = [
    "aiohttp>=3.8",
    "pyserial-asyncio>=0.6",
    "PyYAML>=6",
]

[project.scripts]
fpgas-tt = "fpgas_tt.server:main"

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-aiohttp>=1.0",
    "pytest-asyncio>=0.23",
    "ruff>=0.6",
]

[tool.hatch.build.targets.wheel]
packages = ["src/fpgas_tt"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Write `ruff.toml`, `.gitignore`, `LICENSE`**

`ruff.toml`:
```toml
line-length = 120
target-version = "py311"

[lint]
select = ["E", "F", "W", "I"]
```

`.gitignore`:
```
.venv/
dist/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
# git worktrees for feature branches (superpowers:using-git-worktrees)
.worktrees/
# local scratch
tmp/
```

`LICENSE`: the full Apache License 2.0 text (copy from `fpgas.online-infra/LICENSE`, which is Apache-2.0 — verify the first line reads "Apache License" before copying).

- [ ] **Step 3: Write the package skeleton**

`src/fpgas_tt/__init__.py`:
```python
"""fpgas-tt: Pi-side Tiny Tapeout demo-board bridge daemon for fpgas.online."""

__version__ = "0.1.0"
```

`src/fpgas_tt/__main__.py`:
```python
from fpgas_tt.server import main

if __name__ == "__main__":
    raise SystemExit(main())
```

`tests/__init__.py`: empty file.

`src/fpgas_tt/server.py` (placeholder that Task 4 replaces — it exists only so `__main__` imports):
```python
"""HTTP/WebSocket front end for the bridge (filled in by Task 4)."""


def main(argv=None) -> int:
    raise SystemExit("fpgas-tt: server not implemented yet")
```

- [ ] **Step 4: Write `README.md` and `CLAUDE.md`**

`README.md`:
````markdown
# fpgas.online-tt

Pi-side daemon that owns a Tiny Tapeout demo board's USB serial port and
exposes it to the [tinytapeout.fpgas.online](https://tinytapeout.fpgas.online)
web front end as a fan-out WebSocket bridge.

Part of the [fpgas.online](https://fpgas.online) platform. Design:
`fpgas.online-infra/docs/superpowers/specs/2026-08-22-tinytapeout-fpgas-online-design.md`.

## What it does

- Opens `/dev/ttboard` (udev symlink for the demo board's RP2040/RP2350 USB-CDC
  port) at 115200 baud and keeps retrying every second until a board appears.
- `WS /serial` — every connected client receives the same bytes from the board
  and may write bytes to it. No locking. A client that falls more than 256 KiB
  behind is dropped; the board reader is never blocked.
- `GET /health` — `{"board": {"present": bool, "device": str}, "kind": str,
  "slug": str, "switch": int|null, "port": int|null, "clients": int,
  "uptime_s": int, "version": str}`.
- Discovers which board it is from its hostname (`pi-sw<switch>-p<port>`) and
  `/etc/fpgas-online/tt-boards.yaml` (baked into the Pi NFS root by
  fpgas.online-infra). Unknown hostname ⇒ plain `asic` bridge.

Later phases add bitstream upload / design listing (FPGA boards) and the
KianV boot macro; they are tasks that go *through* the bridge as clients —
there is only ever one owner of the serial port.

## Install (on the Pi NFS root — done by fpgas.online-infra)

```bash
apt install fpgas-online-tt
systemctl enable fpgas-tt.service
```

Listens on `0.0.0.0:8765`; only the gateway can reach it (per-port VLANs).

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run fpgas-tt --device /dev/ttyACM0 --boards tests/data/tt-boards.yaml --hostname pi-sw1-p6
```

Tests use a pseudo-terminal as a fake board; no hardware needed.

## Packaging

`nfpm.yaml` builds an `arch: all` deb in CI (`.github/workflows/build-deb.yml`)
on `v*` tags and publishes it to <https://fpgas-online.github.io/apt>.

## License

Apache-2.0
````

`CLAUDE.md`:
````markdown
## Background

This repo is part of the [fpgas.online](https://fpgas.online) FPGA-as-a-Service
platform. It provides the Pi-side daemon for the Tiny Tapeout front end
(`tinytapeout.fpgas.online`): a single-owner serial bridge to the TT demo
board plus a health endpoint. Design spec:
`fpgas.online-infra/docs/superpowers/specs/2026-08-22-tinytapeout-fpgas-online-design.md`.

## Repository Overview

- `src/fpgas_tt/config.py` — hostname → (switch, port); boards YAML; `discover()`
- `src/fpgas_tt/bridge.py` — `Bridge` (one serial owner) and `Client`
- `src/fpgas_tt/server.py` — aiohttp app (`/health`, `/serial`) and CLI `main()`
- `tests/` — pytest; a pty stands in for the board
- `nfpm.yaml`, `debian/`, `bin/` — deb packaging (arch all)

Invariants: exactly one owner of the serial port; every consumer (WebSocket
viewers and future tasks) is a bridge client; no arbitration; slow clients are
dropped, the reader never blocks; nothing is fetched or generated after boot.

## Conventions

- **Python**: Use `uv` for all Python commands (`uv run`, `uv pip`). Never use bare `python` or `pip`.
- **Dates**: ISO 8601 (YYYY-MM-DD) or day-first. Never month-first.
- **Commits**: small, discrete commits; every change via PR; CI green before merge.
- **License**: Apache 2.0.
- **Linting**: ruff (blocking). Tests: pytest (blocking).
- **No force push**.

## Related Repos

| Repo | Purpose |
|------|---------|
| [fpgas.online-infra](https://github.com/fpgas-online/fpgas.online-infra) | Ansible; bakes this deb into the Pi NFS root |
| [fpgas.online-site](https://github.com/fpgas-online/fpgas.online-site) | Django site (the `ttsite` app proxies to this daemon) |
| [tt-commander-app (fork)](https://github.com/fpgas-online/tt-commander-app) | Web Commander that connects to `/serial` |
| [apt](https://github.com/fpgas-online/apt) | APT repo this deb is published to |
````

- [ ] **Step 5: Write the CI workflow**

`.github/workflows/ci.yml`:
```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/ruff-action@v3

  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.11"
      - run: uv sync
      - run: uv run pytest -v
```

- [ ] **Step 6: Lock, lint, and verify the skeleton runs**

Run: `cd /home/tim/github/fpgas-online/fpgas.online-tt && uv sync && uv run ruff check . && uv run python -c "import fpgas_tt; print(fpgas_tt.__version__)"`
Expected: `uv.lock` created, ruff clean, prints `0.1.0`.

- [ ] **Step 7: Commit the scaffold on `main` (the only direct commit — the repo does not exist on GitHub yet)**

```bash
git add -A
git commit -F - <<'EOF'
chore: scaffold fpgas.online-tt (pyproject, ruff, CI, README, plan)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UdRpsg6jY8PbcQxxX6txKE
EOF
```

- [ ] **Step 8: Create the GitHub repo with the `github-setup` skill**

Invoke `Skill: github-setup` and follow it for a new **public** repo `fpgas-online/fpgas.online-tt` (description: "Pi-side Tiny Tapeout demo-board bridge daemon for fpgas.online", Apache-2.0). It applies the org-standard settings (branch protection on `main` requiring PRs + green status checks, merge options, security scanning, tag rulesets). Push `main`:
```bash
git remote add origin git@github.com:fpgas-online/fpgas.online-tt.git
git push -u origin main
```
Then confirm CI is green: `gh run list --repo fpgas-online/fpgas.online-tt --limit 1` → `completed success`. **From here on, every change is a feature branch in `.worktrees/<branch>` → PR → CI green → merge.**

- [ ] **Step 9: Tell Tim about the one manual secret**

The `build-deb.yml` publish job (Task 5) needs repo secret `APT_REPO_TOKEN` (a token allowed to `repository_dispatch` into `fpgas-online/apt`, same as the one `fpgas.online-setup-pi` uses). Ask Tim to add it — do not guess or create tokens.

---

### Task 2: `config.py` — hostname parsing, boards YAML, discovery

**Files:**
- Create: `src/fpgas_tt/config.py`, `tests/test_config.py`, `tests/data/tt-boards.yaml`

**Interfaces:**
- Produces:
  - `KINDS = ("asic", "kianv", "fpga")`
  - `parse_hostname(name: str) -> tuple[int, int] | None` — `"pi-sw1-p7" → (1, 7)`, else `None`.
  - `load_boards(path: str | Path) -> list[dict]` — returns the `tt_boards` list from the YAML mapping; `FileNotFoundError` propagates; `ValueError` if the top-level key is missing.
  - `@dataclass(frozen=True) BoardConfig(slug: str, kind: str, switch: int | None, port: int | None, hostname: str)`
  - `discover(hostname: str, boards_path: str | Path) -> BoardConfig`

- [ ] **Step 1: Write the test data file `tests/data/tt-boards.yaml`**

```yaml
# Same shape as the file fpgas.online-infra renders from host_vars `tt_boards`.
tt_boards:
  - {slug: tt06, port: 6, kind: asic, shuttle: tt06, title: "Tiny Tapeout 6"}
  - {slug: tt03, port: 3, kind: asic, shuttle: tt03, title: "Tiny Tapeout 3", enabled: false}
  - {slug: fpga-1, port: 12, kind: fpga, title: "TT FPGA emulation board 1"}
  - {slug: kianv-1, port: null, kind: kianv, shuttle: tt06, title: "KianV uLinux SoC (TT06)"}
  - {slug: sw2-thing, switch: 2, port: 1, kind: asic, title: "On switch two"}
```

- [ ] **Step 2: Write the failing tests `tests/test_config.py`**

```python
from pathlib import Path

import pytest

from fpgas_tt.config import BoardConfig, discover, load_boards, parse_hostname

DATA = Path(__file__).parent / "data" / "tt-boards.yaml"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("pi-sw1-p7", (1, 7)),
        ("pi-sw2-p48", (2, 48)),
        ("pi-sw1-p7.fpgas.welland.mithis.com", None),  # callers pass the short name
        ("raspberrypi", None),
        ("pi-sw-p7", None),
        ("", None),
    ],
)
def test_parse_hostname(name, expected):
    assert parse_hostname(name) == expected


def test_load_boards_returns_list():
    boards = load_boards(DATA)
    assert [b["slug"] for b in boards] == ["tt06", "tt03", "fpga-1", "kianv-1", "sw2-thing"]


def test_load_boards_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_boards("/nonexistent/tt-boards.yaml")


def test_load_boards_wrong_shape_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("- just: a list\n")
    with pytest.raises(ValueError, match="tt_boards"):
        load_boards(p)


def test_discover_known_board():
    assert discover("pi-sw1-p6", DATA) == BoardConfig(slug="tt06", kind="asic", switch=1, port=6, hostname="pi-sw1-p6")


def test_discover_fpga_board():
    cfg = discover("pi-sw1-p12", DATA)
    assert (cfg.slug, cfg.kind) == ("fpga-1", "fpga")


def test_discover_respects_switch():
    assert discover("pi-sw2-p1", DATA).slug == "sw2-thing"
    assert discover("pi-sw1-p1", DATA).slug == "pi-sw1-p1"  # no such board on switch 1


def test_discover_disabled_board_falls_back():
    cfg = discover("pi-sw1-p3", DATA)
    assert cfg == BoardConfig(slug="pi-sw1-p3", kind="asic", switch=1, port=3, hostname="pi-sw1-p3")


def test_discover_unknown_hostname():
    cfg = discover("raspberrypi", DATA)
    assert cfg == BoardConfig(slug="raspberrypi", kind="asic", switch=None, port=None, hostname="raspberrypi")


def test_discover_without_boards_file():
    cfg = discover("pi-sw1-p6", "/nonexistent.yaml")
    assert cfg == BoardConfig(slug="pi-sw1-p6", kind="asic", switch=1, port=6, hostname="pi-sw1-p6")


def test_discover_rejects_unknown_kind(tmp_path):
    p = tmp_path / "tt-boards.yaml"
    p.write_text("tt_boards:\n  - {slug: x, port: 1, kind: banana}\n")
    with pytest.raises(ValueError, match="kind"):
        discover("pi-sw1-p1", p)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'fpgas_tt.config'`.

- [ ] **Step 4: Implement `src/fpgas_tt/config.py`**

```python
"""Board identity discovery: hostname + baked tt-boards.yaml → BoardConfig.

The Pi NFS root is shared by every Pi, so nothing here is per-Pi on disk.
A Pi learns which Tiny Tapeout board it carries from its DHCP hostname
(``pi-sw<switch>-p<port>``, assigned per switch port by the gateway) and the
site-wide ``tt-boards.yaml`` that fpgas.online-infra bakes into the image.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

KINDS = ("asic", "kianv", "fpga")

_HOSTNAME_RE = re.compile(r"^pi-sw(\d+)-p(\d+)$")


@dataclass(frozen=True)
class BoardConfig:
    slug: str
    kind: str
    switch: int | None
    port: int | None
    hostname: str


def parse_hostname(name: str) -> tuple[int, int] | None:
    """``pi-sw1-p7`` → ``(1, 7)``; anything else → ``None``."""
    m = _HOSTNAME_RE.match(name)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def load_boards(path: str | Path) -> list[dict]:
    """Return the ``tt_boards`` list from the YAML file at *path*.

    Raises ``FileNotFoundError`` if the file is missing and ``ValueError`` if
    it is not a mapping with a ``tt_boards`` list.
    """
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict) or not isinstance(doc.get("tt_boards"), list):
        raise ValueError(f"{path}: expected a mapping with a 'tt_boards' list")
    return doc["tt_boards"]


def discover(hostname: str, boards_path: str | Path) -> BoardConfig:
    """Work out which board this Pi carries.

    Falls back to a plain ``asic`` bridge named after the hostname when the
    hostname is not of the ``pi-sw<s>-p<p>`` form, the boards file is absent,
    or no enabled entry matches this (switch, port).
    """
    sp = parse_hostname(hostname)
    switch, port = sp if sp else (None, None)

    boards: list[dict] = []
    if Path(boards_path).exists():
        boards = load_boards(boards_path)

    if sp is not None:
        for board in boards:
            if not board.get("enabled", True):
                continue
            if (int(board.get("switch", 1)), board.get("port")) != (switch, port):
                continue
            kind = board.get("kind", "asic")
            if kind not in KINDS:
                raise ValueError(f"{boards_path}: board {board.get('slug')!r} has unknown kind {kind!r}")
            return BoardConfig(slug=str(board["slug"]), kind=kind, switch=switch, port=port, hostname=hostname)

    return BoardConfig(slug=hostname, kind="asic", switch=switch, port=port, hostname=hostname)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v && uv run ruff check .`
Expected: 13 passed; ruff clean.

- [ ] **Step 6: Commit, push, PR, CI**

```bash
git worktree add .worktrees/config -b config main   # (do this BEFORE step 1 if starting fresh)
git add src/fpgas_tt/config.py tests/test_config.py tests/data/tt-boards.yaml
git commit -F - <<'EOF'
feat(config): discover board identity from hostname + tt-boards.yaml

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UdRpsg6jY8PbcQxxX6txKE
EOF
git push -u origin config
gh pr create --fill --base main
gh pr checks --watch
```
Merge when green (`gh pr merge --squash --delete-branch` if protection allows self-merge; otherwise request Tim's review). Remove the worktree afterwards (`git worktree remove .worktrees/config`).

---

### Task 3: `bridge.py` — single-owner serial bridge with fan-out clients

**Files:**
- Create: `src/fpgas_tt/bridge.py`, `tests/conftest.py`, `tests/test_bridge.py`

**Interfaces:**
- Produces:
  - `class BoardNotPresent(RuntimeError)`
  - `MAX_CLIENT_BUFFER = 256 * 1024`
  - `class Client`: `async read() -> bytes | None` (`None` = closed), `async write(data: bytes) -> None`, `close() -> None`, attributes `dropped: bool`, `buffered: int`.
  - `class Bridge(device: str, *, baudrate: int = 115200, reopen_interval: float = 1.0)`: `async start()`, `async stop()`, `subscribe() -> Client`, `async write(data: bytes)` (raises `BoardNotPresent`), properties `present: bool`, `clients: int`, `device: str`.
- Consumes: nothing from Task 2 (bridge is config-agnostic).

- [ ] **Step 1: Write the pty fixtures `tests/conftest.py`**

```python
"""Test helpers: a pseudo-terminal stands in for the demo board's USB serial port.

``fake_board`` yields an object whose ``path`` the Bridge opens (via a symlink,
so tests can swap the underlying pty to simulate unplug/replug) and whose
``master`` fd is "the board": bytes written to it arrive at the Bridge, bytes
the Bridge writes can be read from it.
"""

from __future__ import annotations

import asyncio
import os
import termios
import tty
from dataclasses import dataclass, field
from pathlib import Path

import pytest


@dataclass
class FakeBoard:
    path: Path  # symlink the Bridge opens
    master: int
    slave: int
    _link_dir: Path = field(repr=False)

    async def send(self, data: bytes) -> None:
        """Board → bridge."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, os.write, self.master, data)

    async def recv(self, n: int = 4096, timeout: float = 2.0) -> bytes:
        """Bridge → board. Raises TimeoutError if nothing arrives."""
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.run_in_executor(None, os.read, self.master, n), timeout)

    async def recv_exactly(self, n: int, timeout: float = 2.0) -> bytes:
        buf = b""
        while len(buf) < n:
            buf += await self.recv(n - len(buf), timeout)
        return buf

    def unplug(self) -> None:
        """Close the master: the slave now returns EIO, like a yanked USB cable."""
        os.close(self.master)
        self.master = -1

    def replug(self) -> None:
        """Create a fresh pty and repoint the symlink at it (a new /dev/ttyACM0)."""
        master, slave = _open_raw_pty()
        self.master, self.slave = master, slave
        tmp = self._link_dir / "ttboard.new"
        os.symlink(os.ttyname(slave), tmp)
        os.replace(tmp, self.path)


def _open_raw_pty() -> tuple[int, int]:
    master, slave = os.openpty()
    tty.setraw(slave)
    tty.setraw(master)
    # Keep the slave open on our side too; pyserial opens its own fd by path.
    attrs = termios.tcgetattr(slave)
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    return master, slave


@pytest.fixture
def fake_board(tmp_path: Path) -> FakeBoard:
    master, slave = _open_raw_pty()
    link = tmp_path / "ttboard"
    os.symlink(os.ttyname(slave), link)
    board = FakeBoard(path=link, master=master, slave=slave, _link_dir=tmp_path)
    yield board
    for fd in (board.master, board.slave):
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
```

- [ ] **Step 2: Write the failing tests `tests/test_bridge.py`**

```python
import asyncio

import pytest

from fpgas_tt.bridge import MAX_CLIENT_BUFFER, BoardNotPresent, Bridge


async def wait_for(predicate, timeout=2.0, interval=0.01):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


@pytest.fixture
async def bridge(fake_board):
    b = Bridge(str(fake_board.path), reopen_interval=0.05)
    await b.start()
    await wait_for(lambda: b.present)
    yield b
    await b.stop()


async def test_board_to_client(fake_board, bridge):
    client = bridge.subscribe()
    await fake_board.send(b"hello\r\n")
    assert await asyncio.wait_for(client.read(), 2) == b"hello\r\n"
    client.close()


async def test_client_to_board(fake_board, bridge):
    client = bridge.subscribe()
    await client.write(b"print(1)\r\n")
    assert await fake_board.recv_exactly(len(b"print(1)\r\n")) == b"print(1)\r\n"
    client.close()


async def test_fanout_to_all_clients(fake_board, bridge):
    a, b = bridge.subscribe(), bridge.subscribe()
    assert bridge.clients == 2
    await fake_board.send(b"x")
    assert await asyncio.wait_for(a.read(), 2) == b"x"
    assert await asyncio.wait_for(b.read(), 2) == b"x"
    a.close()
    b.close()
    assert bridge.clients == 0


async def test_slow_client_is_dropped_not_reader(fake_board, bridge):
    slow = bridge.subscribe()
    fast = bridge.subscribe()
    chunk = b"A" * 4096
    for _ in range(MAX_CLIENT_BUFFER // len(chunk) + 2):
        await fake_board.send(chunk)
        # the fast client keeps draining; the slow one never reads
        await asyncio.wait_for(fast.read(), 2)
    await wait_for(lambda: slow.dropped)
    assert await asyncio.wait_for(slow.read(), 2) is None  # closed
    assert bridge.clients == 1  # fast client still attached
    await fake_board.send(b"still alive")
    assert await asyncio.wait_for(fast.read(), 2) == b"still alive"
    fast.close()


async def test_write_without_board_raises(fake_board):
    b = Bridge(str(fake_board.path), reopen_interval=0.05)
    # not started: no board
    with pytest.raises(BoardNotPresent):
        await b.write(b"x")


async def test_unplug_closes_clients_and_replug_recovers(fake_board, bridge):
    client = bridge.subscribe()
    fake_board.unplug()
    await wait_for(lambda: not bridge.present)
    assert await asyncio.wait_for(client.read(), 2) is None  # closed on loss
    assert client.dropped is False
    assert bridge.clients == 0

    fake_board.replug()
    await wait_for(lambda: bridge.present)
    client2 = bridge.subscribe()
    await fake_board.send(b"back")
    assert await asyncio.wait_for(client2.read(), 2) == b"back"
    client2.close()


async def test_start_without_device_keeps_retrying(tmp_path):
    b = Bridge(str(tmp_path / "missing"), reopen_interval=0.05)
    await b.start()
    await asyncio.sleep(0.2)
    assert b.present is False
    await b.stop()  # must stop cleanly while retrying
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_bridge.py -v`
Expected: `ModuleNotFoundError: No module named 'fpgas_tt.bridge'`.

- [ ] **Step 4: Implement `src/fpgas_tt/bridge.py`**

```python
"""The one and only owner of the demo board's serial port.

One reader task fans every chunk out to all subscribed clients; one writer
accepts bytes from any client. There is deliberately no locking or
arbitration: WebSocket viewers and (in later phases) internal tasks such as
bitstream upload are all just clients of this bridge.

Back-pressure policy: a client whose unread bytes exceed ``MAX_CLIENT_BUFFER``
is dropped (``Client.dropped = True``, ``read()`` returns ``None``). The serial
reader is never blocked by a slow consumer.

Loss policy: if the serial device disappears, every client is closed
(``read()`` returns ``None``) and the bridge retries opening the device every
``reopen_interval`` seconds forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import serial
import serial_asyncio

log = logging.getLogger(__name__)

MAX_CLIENT_BUFFER = 256 * 1024
READ_CHUNK = 4096


class BoardNotPresent(RuntimeError):
    """Raised by ``Bridge.write`` when no serial device is open."""


class Client:
    """A subscriber of the bridge. Obtain via ``Bridge.subscribe()``."""

    def __init__(self, bridge: "Bridge") -> None:
        self._bridge = bridge
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.buffered = 0
        self.dropped = False
        self.closed = False

    async def read(self) -> bytes | None:
        """Next chunk from the board, or ``None`` once this client is closed."""
        item = await self._queue.get()
        if item is None:
            self.closed = True
            return None
        self.buffered -= len(item)
        return item

    async def write(self, data: bytes) -> None:
        await self._bridge.write(data)

    def close(self) -> None:
        self._bridge._unsubscribe(self)

    # -- internal, called by Bridge --
    def _push(self, data: bytes) -> bool:
        if self.buffered + len(data) > MAX_CLIENT_BUFFER:
            return False
        self.buffered += len(data)
        self._queue.put_nowait(data)
        return True

    def _end(self) -> None:
        if not self.closed:
            self._queue.put_nowait(None)


class Bridge:
    def __init__(self, device: str, *, baudrate: int = 115200, reopen_interval: float = 1.0) -> None:
        self.device = device
        self.baudrate = baudrate
        self.reopen_interval = reopen_interval
        self._clients: set[Client] = set()
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self.present = False

    # -- lifecycle --
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fpgas-tt-bridge")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._close_clients()

    # -- client API --
    @property
    def clients(self) -> int:
        return len(self._clients)

    def subscribe(self) -> Client:
        client = Client(self)
        self._clients.add(client)
        return client

    def _unsubscribe(self, client: Client) -> None:
        if client in self._clients:
            self._clients.discard(client)
            client._end()

    async def write(self, data: bytes) -> None:
        writer = self._writer
        if writer is None:
            raise BoardNotPresent(self.device)
        writer.write(data)
        await writer.drain()

    # -- internals --
    async def _run(self) -> None:
        while True:
            try:
                reader, writer = await serial_asyncio.open_serial_connection(url=self.device, baudrate=self.baudrate)
            except (OSError, serial.SerialException) as exc:
                log.debug("bridge: cannot open %s: %s", self.device, exc)
                await asyncio.sleep(self.reopen_interval)
                continue

            self._writer = writer
            self.present = True
            log.info("bridge: opened %s at %d baud", self.device, self.baudrate)
            try:
                while True:
                    data = await reader.read(READ_CHUNK)
                    if not data:
                        log.warning("bridge: EOF on %s", self.device)
                        break
                    self._fanout(data)
            except (OSError, serial.SerialException) as exc:
                log.warning("bridge: lost %s: %s", self.device, exc)
            finally:
                self.present = False
                self._writer = None
                writer.close()
                self._close_clients()
            await asyncio.sleep(self.reopen_interval)

    def _fanout(self, data: bytes) -> None:
        for client in list(self._clients):
            if not client._push(data):
                log.warning("bridge: dropping slow client (%d bytes buffered)", client.buffered)
                client.dropped = True
                self._unsubscribe(client)

    def _close_clients(self) -> None:
        for client in list(self._clients):
            self._unsubscribe(client)
```

Note for the implementer: `serial_asyncio.open_serial_connection` is a coroutine; on a pty that has been unplugged the slave read raises `OSError(EIO)` which `serial.SerialException` wraps — both are caught. On EOF `reader.read` returns `b""`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_bridge.py -v && uv run ruff check .`
Expected: 7 passed; ruff clean. If `test_unplug_closes_clients_and_replug_recovers` flakes on reopen timing, raise `reopen_interval` in the fixture to `0.1` — do not loosen the assertions.

- [ ] **Step 6: Commit, push, PR, CI**

```bash
git worktree add .worktrees/bridge -b bridge main
git add src/fpgas_tt/bridge.py tests/conftest.py tests/test_bridge.py
git commit -F - <<'EOF'
feat(bridge): single-owner serial bridge with fan-out clients

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UdRpsg6jY8PbcQxxX6txKE
EOF
git push -u origin bridge && gh pr create --fill --base main && gh pr checks --watch
```
Merge when green; remove the worktree.

---

### Task 4: `server.py` — `/health`, `/serial` WebSocket, CLI `main()`

**Files:**
- Modify: `src/fpgas_tt/server.py` (replace the Task 1 placeholder)
- Create: `tests/test_server.py`

**Interfaces:**
- Consumes: `Bridge`, `Client`, `BoardNotPresent` (Task 3); `BoardConfig`, `discover` (Task 2); `fpgas_tt.__version__`.
- Produces:
  - `create_app(bridge: Bridge, config: BoardConfig, *, version: str = __version__) -> aiohttp.web.Application`
  - `main(argv: list[str] | None = None) -> int` (CLI: `--device`, `--boards`, `--hostname`, `--host`, `--port`, `--baudrate`, `--log-level`)
  - WebSocket protocol on `/serial`: server→client **binary** frames = board bytes; server→client **text** frames = JSON events (`{"event":"board","present":bool,"device":str}` on connect; `{"event":"error","error":str}`); client→server binary *or* text frames are written to the board verbatim (text is UTF-8 encoded). Close code `1011` reason `board disconnected` on serial loss, `1008` reason `client too slow` when dropped.

- [ ] **Step 1: Write the failing tests `tests/test_server.py`**

```python
import asyncio
import json

import aiohttp
import pytest

from fpgas_tt import __version__
from fpgas_tt.bridge import Bridge
from fpgas_tt.config import BoardConfig
from fpgas_tt.server import create_app, main

CFG = BoardConfig(slug="tt06", kind="asic", switch=1, port=6, hostname="pi-sw1-p6")


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


@pytest.fixture
async def client(aiohttp_client, bridge):
    return await aiohttp_client(create_app(bridge, CFG))


async def test_health_reports_board_and_identity(client):
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["board"]["present"] is True
    assert body["board"]["device"].endswith("ttboard")
    assert body["kind"] == "asic"
    assert body["slug"] == "tt06"
    assert body["switch"] == 1 and body["port"] == 6
    assert body["clients"] == 0
    assert body["version"] == __version__
    assert isinstance(body["uptime_s"], int)


async def test_health_when_board_absent(aiohttp_client, tmp_path):
    bridge = Bridge(str(tmp_path / "missing"), reopen_interval=0.05)
    await bridge.start()
    try:
        c = await aiohttp_client(create_app(bridge, CFG))
        body = await (await c.get("/health")).json()
        assert body["board"]["present"] is False
    finally:
        await bridge.stop()


async def test_serial_ws_roundtrip(client, fake_board):
    async with client.ws_connect("/serial") as ws:
        first = await ws.receive(timeout=2)
        assert first.type == aiohttp.WSMsgType.TEXT
        assert json.loads(first.data) == {"event": "board", "present": True, "device": str(fake_board.path)}

        await ws.send_bytes(b"print(1)\r\n")
        assert await fake_board.recv_exactly(len(b"print(1)\r\n")) == b"print(1)\r\n"

        await ws.send_str("\x03")  # text frames are written verbatim too
        assert await fake_board.recv_exactly(1) == b"\x03"

        await fake_board.send(b"1\r\n>>> ")
        msg = await ws.receive(timeout=2)
        assert msg.type == aiohttp.WSMsgType.BINARY
        assert msg.data == b"1\r\n>>> "

        health = await (await client.get("/health")).json()
        assert health["clients"] == 1


async def test_serial_ws_fanout(client, fake_board):
    async with client.ws_connect("/serial") as a, client.ws_connect("/serial") as b:
        await a.receive(timeout=2)  # board events
        await b.receive(timeout=2)
        await fake_board.send(b"ping")
        assert (await a.receive(timeout=2)).data == b"ping"
        assert (await b.receive(timeout=2)).data == b"ping"


async def test_serial_ws_closed_on_board_loss(client, fake_board, bridge):
    async with client.ws_connect("/serial") as ws:
        await ws.receive(timeout=2)  # board event
        fake_board.unplug()
        msg = await ws.receive(timeout=2)
        assert msg.type == aiohttp.WSMsgType.CLOSE
        assert msg.data == 1011
        assert msg.extra == "board disconnected"
    await wait_for(lambda: bridge.clients == 0)


async def test_serial_ws_write_when_board_absent_reports_error(aiohttp_client, tmp_path):
    bridge = Bridge(str(tmp_path / "missing"), reopen_interval=0.05)
    await bridge.start()
    try:
        c = await aiohttp_client(create_app(bridge, CFG))
        async with c.ws_connect("/serial") as ws:
            first = json.loads((await ws.receive(timeout=2)).data)
            assert first["present"] is False
            await ws.send_bytes(b"x")
            err = json.loads((await ws.receive(timeout=2)).data)
            assert err == {"event": "error", "error": "board not present"}
    finally:
        await bridge.stop()


def test_main_parses_args_and_discovers(monkeypatch, tmp_path):
    """main() wires argv → discover() → create_app → run_app; we stub run_app."""
    captured = {}

    def fake_run_app(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr("fpgas_tt.server.web.run_app", fake_run_app)
    boards = tmp_path / "tt-boards.yaml"
    boards.write_text("tt_boards:\n  - {slug: fpga-1, port: 12, kind: fpga}\n")
    rc = main(["--device", "/dev/null", "--boards", str(boards), "--hostname", "pi-sw1-p12", "--port", "9999"])
    assert rc == 0
    assert captured["kwargs"]["port"] == 9999
    assert captured["kwargs"]["host"] == "0.0.0.0"
    assert captured["app"]["config"].slug == "fpga-1"
    assert captured["app"]["bridge"].device == "/dev/null"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_server.py -v`
Expected: `ImportError: cannot import name 'create_app' from 'fpgas_tt.server'`.

- [ ] **Step 3: Implement `src/fpgas_tt/server.py`**

```python
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

from aiohttp import WSMsgType, web

from fpgas_tt import __version__
from fpgas_tt.bridge import BoardNotPresent, Bridge
from fpgas_tt.config import BoardConfig, discover

log = logging.getLogger(__name__)

CLOSE_BOARD_LOST = 1011
CLOSE_CLIENT_SLOW = 1008


def create_app(bridge: Bridge, config: BoardConfig, *, version: str = __version__) -> web.Application:
    app = web.Application()
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all tests (config + bridge + server) pass; ruff clean. If aiohttp's `ws.receive()` for the CLOSE frame exposes the reason under a different attribute than `extra` on the installed version, check `aiohttp.WSMessage` docs and adjust the assertion — not the server.

- [ ] **Step 5: Smoke-run the CLI against the pty (manual, optional but recommended)**

Run in one terminal: `uv run python - <<'EOF'` … (use `tests/conftest.py`'s `_open_raw_pty` approach to print a pty path) — or simpler: `socat -d -d pty,raw,echo=0 pty,raw,echo=0` prints two `/dev/pts/N`; then `uv run fpgas-tt --device /dev/pts/N --boards tests/data/tt-boards.yaml --hostname pi-sw1-p6 --port 8765` and `curl -s localhost:8765/health`. Expected JSON with `present: true`.

- [ ] **Step 6: Commit, push, PR, CI**

```bash
git worktree add .worktrees/server -b server main
git add src/fpgas_tt/server.py tests/test_server.py
git commit -F - <<'EOF'
feat(server): /health and /serial WebSocket front end, fpgas-tt CLI

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UdRpsg6jY8PbcQxxX6txKE
EOF
git push -u origin server && gh pr create --fill --base main && gh pr checks --watch
```
Merge when green; remove the worktree.

---

### Task 5: Debian packaging (nfpm), systemd unit, udev rule, release workflow

> **Superseded:** the `APT_REPO_TOKEN` / `repository_dispatch` publish design below was
> replaced by a token-less rolling GitHub Release — see README.md "Releases (rolling)".
> Kept here for history.

**Files:**
- Create: `nfpm.yaml`, `bin/fpgas-tt`, `debian/fpgas-tt.service`, `debian/60-fpgas-tt.rules`, `debian/postinstall.sh`, `.github/workflows/build-deb.yml`, `tests/test_packaging.py`
- Modify: `.github/workflows/ci.yml` (add a `deb` job that builds the package on PRs and uploads it as an artifact — no publish)

**Interfaces:**
- Produces: deb `fpgas-online-tt_<ver>_all.deb` installing `/usr/lib/python3/dist-packages/fpgas_tt/`, `/usr/bin/fpgas-tt`, `/usr/lib/systemd/system/fpgas-tt.service`, `/etc/udev/rules.d/60-fpgas-tt.rules`; depends `python3 (>= 3.11), python3-aiohttp, python3-serial-asyncio, python3-yaml, udev`.
- Consumes: `src/fpgas_tt/*` from Tasks 2–4.

- [ ] **Step 1: Write the failing packaging test `tests/test_packaging.py`**

This test checks the packaging *inputs* are consistent (the deb itself is built in CI); it runs everywhere without nfpm.

```python
"""Consistency checks between pyproject, nfpm.yaml and the unit/udev files."""

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_nfpm_version_tracks_pyproject():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    nfpm = yaml.safe_load((ROOT / "nfpm.yaml").read_text())
    assert nfpm["name"] == "fpgas-online-tt"
    assert nfpm["arch"] == "all"
    # nfpm takes VERSION from the environment in CI; the fallback must match pyproject.
    assert nfpm["version"] == "${VERSION:-%s}" % pyproject["project"]["version"]


def test_nfpm_depends_on_bookworm_packages_only():
    nfpm = yaml.safe_load((ROOT / "nfpm.yaml").read_text())
    assert set(nfpm["depends"]) == {
        "python3 (>= 3.11)",
        "python3-aiohttp",
        "python3-serial-asyncio",
        "python3-yaml",
        "udev",
    }


def test_nfpm_ships_package_unit_rule_and_wrapper():
    nfpm = yaml.safe_load((ROOT / "nfpm.yaml").read_text())
    dsts = {c["dst"] for c in nfpm["contents"]}
    assert "/usr/lib/python3/dist-packages/fpgas_tt" in dsts
    assert "/usr/bin/fpgas-tt" in dsts
    assert "/usr/lib/systemd/system/fpgas-tt.service" in dsts
    assert "/etc/udev/rules.d/60-fpgas-tt.rules" in dsts
    for c in nfpm["contents"]:
        assert (ROOT / c["src"]).exists(), c["src"]


def test_service_runs_daemon_as_pi_with_restart():
    unit = (ROOT / "debian" / "fpgas-tt.service").read_text()
    assert "ExecStart=/usr/bin/fpgas-tt" in unit
    assert re.search(r"^User=pi$", unit, re.M)
    assert re.search(r"^Restart=always$", unit, re.M)
    assert re.search(r"^RestartSec=1$", unit, re.M)


def test_udev_rule_symlinks_rp2040_and_rp2350_cdc():
    rule = (ROOT / "debian" / "60-fpgas-tt.rules").read_text()
    assert 'SYMLINK+="ttboard"' in rule
    assert 'ATTRS{idVendor}=="2e8a"' in rule
    assert 'ATTRS{idProduct}=="0005"' in rule  # RP2040 MicroPython CDC
    assert 'ATTRS{idProduct}=="000f"' in rule  # RP2350 MicroPython CDC
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_packaging.py -v`
Expected: `FileNotFoundError: .../nfpm.yaml`.

- [ ] **Step 3: Write the packaging files**

`nfpm.yaml`:
```yaml
name: fpgas-online-tt
arch: all
platform: linux
version: "${VERSION:-0.1.0}"
maintainer: "fpgas.online <fpgas@fpgas.online>"
description: "Tiny Tapeout demo-board serial bridge daemon for fpgas.online Pi nodes"
homepage: https://github.com/fpgas-online/fpgas.online-tt
license: Apache-2.0
depends:
  - python3 (>= 3.11)
  - python3-aiohttp
  - python3-serial-asyncio
  - python3-yaml
  - udev
contents:
  - src: src/fpgas_tt
    dst: /usr/lib/python3/dist-packages/fpgas_tt
  - src: bin/fpgas-tt
    dst: /usr/bin/fpgas-tt
    file_info:
      mode: 0755
  - src: debian/fpgas-tt.service
    dst: /usr/lib/systemd/system/fpgas-tt.service
  - src: debian/60-fpgas-tt.rules
    dst: /etc/udev/rules.d/60-fpgas-tt.rules
    type: config
scripts:
  postinstall: debian/postinstall.sh
```

`bin/fpgas-tt`:
```sh
#!/bin/sh
# Thin launcher so the package needs no pip-installed console script.
exec /usr/bin/python3 -m fpgas_tt "$@"
```

`debian/fpgas-tt.service`:
```ini
[Unit]
Description=fpgas.online Tiny Tapeout demo-board bridge
Documentation=https://github.com/fpgas-online/fpgas.online-tt
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=pi
SupplementaryGroups=dialout
ExecStart=/usr/bin/fpgas-tt --device /dev/ttboard --boards /etc/fpgas-online/tt-boards.yaml
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
```

`debian/60-fpgas-tt.rules`:
```
# Tiny Tapeout demo board (RP2040 / RP2350 running the TT MicroPython SDK):
# stable /dev/ttboard symlink for fpgas-tt, readable by the dialout group.
SUBSYSTEM=="tty", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="0005", SYMLINK+="ttboard", GROUP="dialout", MODE="0660"
SUBSYSTEM=="tty", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="000f", SYMLINK+="ttboard", GROUP="dialout", MODE="0660"
```

`debian/postinstall.sh`:
```sh
#!/bin/sh
set -e
systemctl daemon-reload || true
udevadm control --reload || true
```

- [ ] **Step 4: Write the release workflow and extend CI**

`.github/workflows/build-deb.yml`:
```yaml
name: Build and publish deb

on:
  push:
    tags: ["v*"]
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install nfpm
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh release download --repo goreleaser/nfpm --pattern 'nfpm_*_Linux_x86_64.tar.gz' --dir .
          tar xzf nfpm_*.tar.gz nfpm
          chmod +x nfpm

      - name: Build deb (arch all)
        run: |
          VERSION="${GITHUB_REF_NAME#v}"
          export VERSION
          mkdir -p dist
          ./nfpm package --packager deb --target dist/
          ls -l dist/

      - uses: actions/upload-artifact@v4
        with:
          name: deb-package
          path: dist/*.deb

  publish:
    needs: build
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-latest
    steps:
      - name: Trigger apt repo update
        uses: peter-evans/repository-dispatch@v3
        with:
          token: ${{ secrets.APT_REPO_TOKEN }}
          repository: fpgas-online/apt
          event-type: receive-deb
          client-payload: |
            {
              "package_name": "fpgas-online-tt",
              "package_version": "${{ github.ref_name }}",
              "run_id": "${{ github.run_id }}",
              "source_repo": "${{ github.repository }}"
            }
```

Append to `.github/workflows/ci.yml` (same file, new job — builds the deb on every PR so packaging breakage fails CI; no publish):
```yaml
  deb:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install nfpm
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh release download --repo goreleaser/nfpm --pattern 'nfpm_*_Linux_x86_64.tar.gz' --dir .
          tar xzf nfpm_*.tar.gz nfpm
          chmod +x nfpm
      - name: Build deb
        run: |
          mkdir -p dist && ./nfpm package --packager deb --target dist/
          dpkg-deb --info dist/*.deb
          dpkg-deb --contents dist/*.deb
      - uses: actions/upload-artifact@v4
        with:
          name: deb-package-pr
          path: dist/*.deb
```

- [ ] **Step 5: Run the tests, lint, and (if nfpm is available locally) a local build**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all pass.
Optional local build: `nfpm package --packager deb --target tmp/ && dpkg-deb --contents tmp/*.deb` (install nfpm per its README; `tmp/` is gitignored; delete afterwards).

- [ ] **Step 6: Commit, push, PR, CI**

```bash
git worktree add .worktrees/packaging -b packaging main
git add nfpm.yaml bin/fpgas-tt debian/ .github/workflows/build-deb.yml .github/workflows/ci.yml tests/test_packaging.py
git commit -F - <<'EOF'
build: nfpm deb (arch all), systemd unit, udev rule, release workflow

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UdRpsg6jY8PbcQxxX6txKE
EOF
git push -u origin packaging && gh pr create --fill --base main && gh pr checks --watch
```
Merge when green (the `deb` job must be green too); remove the worktree.

---

### Task 6: Register the package in `fpgas-online/apt` and cut `v0.1.0`

> **Superseded:** the `APT_REPO_TOKEN` / `repository_dispatch` registration flow below was
> replaced by the apt repo enumerating GitHub Releases directly — see README.md "Releases
> (rolling)". Also note: the tag ruleset only admits two-component `vX.Y` tags, so
> `v0.1.0` below would not be creatable today; use `v0.1`. Kept here for history.

**Files:**
- Modify (in the `fpgas-online/apt` repo, in a worktree there): `tools/package_sources.toml`, `README.md` ("Hosted Packages" list)

**Interfaces:**
- Consumes: the `receive-deb` dispatch payload from Task 5 (`package_name: fpgas-online-tt`).

- [ ] **Step 1: Clone/worktree the apt repo and add the source mapping**

```bash
cd /home/tim/github/fpgas-online && git clone git@github.com:fpgas-online/apt.git 2>/dev/null || true
cd apt && git fetch origin && git worktree add .worktrees/add-fpgas-online-tt -b add-fpgas-online-tt origin/main
```
(If `.worktrees/` is not gitignored there, add it to `.gitignore` in this same PR.)
In `tools/package_sources.toml`, add (match the existing entries' exact format — read the file first):
```toml
[packages.fpgas-online-tt]
repo = "fpgas-online/fpgas.online-tt"
```
In `README.md` under "Hosted Packages" add:
```
- **fpgas-online-tt** -- Tiny Tapeout demo-board serial bridge daemon for tinytapeout.fpgas.online.
```
and under "Related Repositories":
```
- [fpgas.online-tt](https://github.com/fpgas-online/fpgas.online-tt) -- Source for the `fpgas-online-tt` package
```

- [ ] **Step 2: Run the apt repo's own tests**

Run: `uv run --python 3.12 python -m unittest tools.test_build_site`
Expected: OK. (If the test asserts the exact package list, extend it.)

- [ ] **Step 3: Commit, push, PR, CI, merge**

```bash
git add tools/package_sources.toml README.md
git commit -F - <<'EOF'
feat: register fpgas-online-tt package source

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UdRpsg6jY8PbcQxxX6txKE
EOF
git push -u origin add-fpgas-online-tt && gh pr create --fill --base main && gh pr checks --watch
```

- [ ] **Step 4: Tag `v0.1.0` on `fpgas.online-tt` and watch it publish**

Only after Tim confirms `APT_REPO_TOKEN` is set (Task 1 step 9):
```bash
cd /home/tim/github/fpgas-online/fpgas.online-tt && git checkout main && git pull --ff-only
git tag -a v0.1.0 -m "fpgas-online-tt 0.1.0: serial bridge + /health"
git push origin v0.1.0
gh run watch --repo fpgas-online/fpgas.online-tt   # build + publish jobs
gh run list --repo fpgas-online/apt --limit 1      # receive-deb run, then pages deploy
curl -s https://fpgas-online.github.io/apt/dists/bookworm/main/binary-all/Packages | grep -A3 '^Package: fpgas-online-tt'
```
Expected: the `Packages` index lists `fpgas-online-tt` version `0.1.0`. If `binary-all` is not a path the apt repo generates (check `update-repo.sh`), look under `binary-arm64`/`binary-armhf` — `arch: all` debs are listed in every architecture's index.

---

## Self-review against the spec

- §5.1 packaging/runtime (deps, arch all, udev, user pi, discovery not generation) → Tasks 2, 5. ✔
- §5.2 bridge single owner, fan-out, slow-client drop at 256 KiB, close 1011 `board disconnected`, 1 s reopen → Task 3 (+ Task 4 close codes). ✔
- §5.3 `GET /health` shape and `WS /serial` → Task 4. `/designs`, `/bitstream`, `/enable`, `/kianv/boot`, `/demos/sync`, `/build` → **deliberately out of this plan (phases 2–4)**; the bridge API (`subscribe/read/write`) is what those tasks will build on.
- §5.5 tests vs fake REPL pty → conftest pty fixture; raw-REPL framing tests arrive with phase-2 tasks. ✔
- §7.3 "apt install fpgas-online-tt … enable fpgas-tt.service" → deb + unit here; the Ansible side is plan D. ✔
- §9 errors surfaced (log + `{event:error}`) → Tasks 3–4. ✔
- §10/§12 CI gates, PR-only, worktrees → every task's last step; `.worktrees/` ignored. ✔
- Placeholder scan: none. Type check: `BoardConfig` fields, `Bridge(device, *, baudrate, reopen_interval)`, `Client.read/write/close/dropped`, `create_app(bridge, config, *, version)` used consistently across Tasks 2–5. ✔
