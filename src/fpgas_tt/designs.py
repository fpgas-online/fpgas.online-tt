"""FPGA-board designs: list them, load one into the FPGA, keep a visitor's upload.

Nothing here writes to the demo board's filesystem (Tim, 2026-10-05: nothing at all may write to the Tiny
Tapeout boards). Every design is a file on the Pi: the demos in the package's demos directory, and uploads in
the daemon's own uploads directory. Loading one sends its bytes over the REPL into a buffer in the RP2350's
memory and has the SDK's own loader clock that buffer into the iCE40, so the board's state afterwards is what
the SDK's ``tt.shuttle.<design>.enable()`` leaves (``tt.shuttle.enabled`` names the design), and no file is
involved. The files an older daemon copied to the board's /bitstreams are left alone: not read, not removed.

Board-side facts (TT SDK 3.1.0, FPGA breakout): ``tt.shuttle.enable(design)`` resets the pins, records
``tt.shuttle.enabled`` and calls ``ttboard.fpga.fabricfoxv2.spi_transferPIO(design.file)``, which opens that
path with the module's ``open`` and reads 128 bytes at a time; ``tt.clock_project_PWM(hz)`` sets the clock. The
SDK's ``tt`` object exists only after the board's own main.py has run: a raw-REPL soft reset (mpremote, the
boot check) leaves the board without it. Loading then does what the Commander does when the SDK does not
answer: it soft-resets the board from the friendly REPL, which runs main.py.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from fpgas_tt.repl import ReplError, ReplRunner

log = logging.getLogger(__name__)

DEMOS_DIR_DEFAULT = Path("/usr/share/fpgas-tt/demos")
# Uploads live on the Pi, in the service's StateDirectory (debian/fpgas-tt.service).
UPLOADS_DIR_DEFAULT = Path("/var/lib/fpgas-tt/uploads")
MAX_BITSTREAM_BYTES = 256 * 1024
MAX_UPLOADS = 16
NAME_RE = re.compile(r"^[a-z0-9_]{1,40}\Z")  # \Z, not $: a trailing newline is not part of a name
ICE40_PREAMBLE = b"\x7e\xaa\x99\x7e"
PREAMBLE_WINDOW = 64
CHUNK = 1024  # raw bytes per REPL step (1368 base64 chars on the wire)
META_FIELDS = ("title", "author", "description", "docs_url", "repo_url")
# enable_design's overall REPL-session deadline: the site proxy in front of this daemon has its own read
# timeouts (30s/45s), so the whole load (sending the bitstream, the SPI transfer, the clock) has to fail
# cleanly before the shorter of those would reset the connection.
ENABLE_OVERALL_TIMEOUT = 25.0
# The name the SDK is given for the design the daemon itself streams into a board nobody is using (idle.py).
# It is not a design of the Pi's gallery, so no upload may take it: a page marks as running the listed design
# whose name the SDK gives.
IDLE_NAME = "idle_display"

class ValidationError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class DesignNotFound(Exception):
    pass


# -- board output parsing (a stray print, interference, or a corrupt board
# reply must surface as a REPL failure, never an unhandled ValueError) --
def _parse_json(out: str):
    try:
        return json.loads(out)
    except (ValueError, TypeError) as exc:
        raise ReplError("board returned unparseable output", out) from exc


# -- demo index --
def load_demo_index(demos_dir: Path) -> dict[str, dict]:
    path = Path(demos_dir) / "index.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        log.warning("demos: %s is unreadable (%s); treating the demo index as empty", path, exc)
        return {}
    out: dict[str, dict] = {}
    for d in doc.get("demos", []) if isinstance(doc, dict) else []:
        if not isinstance(d, dict):
            continue
        name = d.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            log.warning("demos: %s: dropping entry with an invalid name %r", path, name)
            continue
        out[name] = d
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
    if name == IDLE_NAME:
        raise ValidationError(f"{name} is the daemon's own name for its idle display; pick another", 409)
    if len(data) > MAX_BITSTREAM_BYTES:
        raise ValidationError(f"bitstream too large ({len(data)} bytes, limit {MAX_BITSTREAM_BYTES})")
    if ICE40_PREAMBLE not in data[:PREAMBLE_WINDOW + len(ICE40_PREAMBLE)]:
        raise ValidationError("not an iCE40 bitstream (preamble 7E AA 99 7E missing in the first 64 bytes)")


# -- the Pi's own store --
_STORE_LOCK = threading.Lock()


def _demo_file(demos_dir: Path, name: str) -> Path:
    return Path(demos_dir) / f"{name}.bin"


def _upload_file(uploads_dir: Path, name: str) -> Path:
    return Path(uploads_dir) / f"{name}.bin"


def _upload_names(uploads_dir: Path) -> list[str]:
    try:
        files = os.listdir(uploads_dir)
    except FileNotFoundError:
        return []
    return sorted(f[:-4] for f in files if f.endswith(".bin") and NAME_RE.match(f[:-4]))


def design_names(demos_dir: Path, uploads_dir: Path) -> list[str]:
    """Every design this Pi can load: a demo whose bitstream is here, and every upload."""
    demos = load_demo_index(demos_dir)
    here = {n for n in demos if _demo_file(demos_dir, n).is_file()}
    missing = sorted(set(demos) - here)
    if missing:
        log.warning("demos: listed in index.json but the bitstream is missing: %s", ", ".join(missing))
    # the same rule as design_file: an upload is hidden only by a demo that is really here
    return sorted(here | set(_upload_names(uploads_dir)))


def design_file(demos_dir: Path, uploads_dir: Path, name: str) -> Path:
    """The file of design `name` on this Pi. A demo wins over an upload of the same name (uploads are refused
    a demo's name, so that is only a file put there by hand)."""
    if not NAME_RE.match(name or ""):
        raise DesignNotFound(name)
    if name in load_demo_index(demos_dir) and _demo_file(demos_dir, name).is_file():
        return _demo_file(demos_dir, name)
    if _upload_file(uploads_dir, name).is_file():
        return _upload_file(uploads_dir, name)
    raise DesignNotFound(name)


def store_upload(uploads_dir: Path, name: str, data: bytes, keep: int = MAX_UPLOADS) -> list[str]:
    """Keep upload `name` on the Pi, and at most `keep` uploads in all: the names of the oldest ones removed
    to make room. The file appears under its name whole or not at all."""
    uploads_dir = Path(uploads_dir)
    with _STORE_LOCK:  # one upload at a time: two making room at once would remove each other's files
        uploads_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=uploads_dir, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, _upload_file(uploads_dir, name))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        others = [n for n in _upload_names(uploads_dir) if n != name]
        others.sort(key=lambda n: (_upload_file(uploads_dir, n).stat().st_mtime, n))
        evicted = others[: max(0, len(others) - (keep - 1))]
        for n in evicted:
            _upload_file(uploads_dir, n).unlink(missing_ok=True)
        return evicted


# -- snippets --
# What the board's SDK has loaded. None of this touches a file. `tt` is missing after a raw-REPL soft reset
# until the board's main.py has run again, and is None when a start of the SDK was cut short (its main.py sets
# `tt = None` before it builds the board): both read as "nothing loaded", not as a failure.
ENABLED_CODE = (
    "import json\n"
    "_fo_tt = globals().get('tt')\n"
    "_fo_en = _fo_tt.shuttle.enabled if _fo_tt is not None else None\n"
    "print(json.dumps({'enabled': _fo_en.name if _fo_en else None}))\n"
)

# Asked first when loading: is the SDK's `tt` object there?
SDK_CODE = "print('sdk' if globals().get('tt') is not None else 'nosdk')\n"
# The SDK's last boot line (its main.py prints it once the board object is built).
SDK_STARTED = "tt.sdk_version="
# Starting the SDK: the board's own main.py takes about 2 s on a v3 demo board.
SDK_START_TIMEOUT = 10.0

# Before the buffer is made: whatever an earlier load that was cut short left behind is let go first, so two
# buffers never have to fit in the board's memory at once.
BUFFER_CODE = """\
_fo_buf = None
_fo_reader = None
_fo_d = None
import gc, binascii
gc.collect()
_fo_buf = bytearray(__SIZE__)
_fo_n = 0
print('buffer')
"""

# The SDK's loader reads its bitstream with `open(path, 'rb')` and `.read(128)`. For the one load, the loader
# module's own `open` is this reader over the buffer in memory; it is taken away again whatever happens
# (deleting a module attribute that is not there is a KeyError on MicroPython, an AttributeError on CPython). The
# buffer is checked against the Pi's SHA-256 before anything is loaded. The SDK names the design as enabled
# before it has transferred it, and its loader prints an OSError instead of raising it: so a load that failed
# once the FPGA was touched, or did not read every byte, leaves `tt.shuttle.enabled` as None, never the design's
# name. A load refused before that (an incomplete or damaged buffer) leaves the running design, and its name,
# as they were.
LOAD_CODE = """\
import gc, binascii, hashlib
import ttboard.fpga.fabricfoxv2 as _fo_loader
from ttboard.fpga.fpga_mux import BitStream as _fo_BitStream
class _fo_Reader:
    def __init__(self, buf):
        self.buf = memoryview(buf)
        self.at = 0
    def read(self, n):
        data = bytes(self.buf[self.at:self.at + n])
        self.at += len(data)
        return data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
try:
    del _fo_loader.open
except (AttributeError, KeyError):
    pass
_fo_ok = False
_fo_touched = False
try:
    if _fo_n != __SIZE__:
        raise ValueError('bitstream incomplete: %d of __SIZE__ bytes' % _fo_n)
    if binascii.hexlify(hashlib.sha256(_fo_buf).digest()).decode() != __SHA256__:
        raise ValueError('the bitstream did not arrive as it was sent')
    _fo_reader = _fo_Reader(_fo_buf)
    _fo_loader.open = lambda path, mode='rb': _fo_reader
    _fo_touched = True
    tt.shuttle.enable(_fo_BitStream(tt.shuttle, __PATH__, __NAME__, -1))
    if _fo_reader.at != __SIZE__:
        raise ValueError('the loader read %d of __SIZE__ bytes' % _fo_reader.at)
    _fo_ok = True
finally:
    try:
        del _fo_loader.open
    except (AttributeError, KeyError):
        pass
    _fo_buf = None
    _fo_reader = None
    gc.collect()
    if _fo_touched and not _fo_ok:
        tt.shuttle.enabled = None
__CLOCK__print('enabled')
"""


def _load_steps(name: str, data: bytes, clock_hz: int | None) -> list[str]:
    """The REPL steps that put `data` into a buffer in the board's memory and have the SDK load it."""
    steps = [BUFFER_CODE.replace("__SIZE__", str(len(data)))]
    for i in range(0, len(data), CHUNK):
        b64 = base64.b64encode(data[i : i + CHUNK]).decode("ascii")
        steps.append(
            f"_fo_d = binascii.a2b_base64({b64!r})\n_fo_buf[_fo_n:_fo_n + len(_fo_d)] = _fo_d\n_fo_n += len(_fo_d)\n"
        )
    clock = f"tt.clock_project_PWM({int(clock_hz)})\n" if clock_hz is not None else ""
    steps.append(
        LOAD_CODE.replace("__SIZE__", str(len(data)))
        .replace("__SHA256__", repr(hashlib.sha256(data).hexdigest()))
        .replace("__PATH__", repr(f"pi:{name}.bin"))  # not a path on the board: the reader never looks at it
        .replace("__NAME__", repr(name))
        .replace("__CLOCK__", clock)
    )
    return steps


def _listing(demos_dir: Path, uploads_dir: Path) -> list[dict]:
    demos = load_demo_index(demos_dir)
    return [_meta(n, demos) for n in design_names(demos_dir, uploads_dir)]


def _read_design(demos_dir: Path, uploads_dir: Path, name: str) -> bytes:
    try:
        return design_file(demos_dir, uploads_dir, name).read_bytes()
    except FileNotFoundError as exc:  # an upload removed to make room, between finding it and reading it
        raise DesignNotFound(name) from exc


async def ensure_sdk(runner: ReplRunner) -> None:
    """Have the board's SDK running, the way the Commander does it (Tim, 2026-10-05, answer tt-07: "DO the same
    thing the commander app does"): ask whether the SDK is there, and only if it is not, soft-reset the board
    from the friendly REPL (Ctrl-C twice, Ctrl-B, Ctrl-D), which runs the board's own boot.py and main.py.
    Raises ReplError when main.py does not start the SDK."""
    if (await runner.exec(SDK_CODE)).strip() == "sdk":
        return
    said = await runner.soft_reset(timeout=SDK_START_TIMEOUT)
    if SDK_STARTED not in said or (await runner.exec(SDK_CODE)).strip() != "sdk":
        raise ReplError("the board's main.py did not start the Tiny Tapeout SDK", said)


async def list_designs(runner: ReplRunner, demos_dir: Path, uploads_dir: Path) -> dict:
    out = _parse_json(await runner.exec(ENABLED_CODE))
    # The Pi's root is on NFS: its files are read off the event loop, which also serves the serial bridge.
    return {"enabled": out["enabled"], "designs": await asyncio.to_thread(_listing, demos_dir, uploads_dir)}


async def enable_design(
    runner: ReplRunner, name: str, clock_hz: int | None, demos_dir: Path, uploads_dir: Path, *, waited: float = 0.0
) -> dict:
    """Load design `name`. `waited` is what the caller already spent waiting for the board: it comes out of
    the deadline, which is the caller's whole request's."""
    # design_file refuses a name the Pi could never have before anything goes near the board.
    data = await asyncio.to_thread(_read_design, demos_dir, uploads_dir, name)
    started = time.monotonic() - waited
    await ensure_sdk(runner)
    # What starting the SDK took comes out of the load's deadline.
    await load_design(runner, name, data, clock_hz, overall=ENABLE_OVERALL_TIMEOUT - (time.monotonic() - started))
    return {"enabled": name, "clock_hz": clock_hz}


async def load_design(
    runner: ReplRunner, name: str, data: bytes, clock_hz: int | None, *, overall: float = ENABLE_OVERALL_TIMEOUT
) -> None:
    """Stream `data` into the FPGA through the SDK that is running on the board, under the name `name`. The
    SDK is not started here: a board without it fails the load."""
    # timeout (per-read) stays 30s -- the SPI load takes a few seconds and any single read waiting on it is
    # normal; overall is intentionally tighter (see ENABLE_OVERALL_TIMEOUT).
    outs = await runner.exec_steps(_load_steps(name, data, clock_hz), timeout=30.0, overall=max(5.0, overall))
    if outs[-1].strip().splitlines()[-1:] != ["enabled"]:
        raise ReplError("the board did not confirm the load", outs[-1])
