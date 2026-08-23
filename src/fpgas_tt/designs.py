"""FPGA-board tasks: list/enable/upload bitstreams, sync demos -- all raw-REPL snippets run through ReplRunner.

Board-side facts (TT SDK 3.1.0, FPGA breakout): bitstreams live in /bitstreams/<name>.bin;
``tt.shuttle.projects.all`` lists them; ``tt.shuttle.get(name).enable()`` loads one;
``tt.shuttle.enabled`` is the loaded one; the index is cached in ``tt.shuttle._design_index``
and must be reset after the directory changes; ``tt.clock_project_PWM(hz)`` sets the clock.

Demo sync tracks a sha1 manifest at /bitstreams/.demos.json ({name: sha1 hex}) rather than
just comparing sizes -- same-size content changes (common: all of one part's iCE40 bitstreams
share a length) would otherwise never be noticed. write_bitstream's overall REPL deadline
scales with payload size (60s + 1s/8KiB) on top of ReplRunner's default. Writes go up in
CHUNK=1024-byte raw steps (1368 base64 chars each on the wire) -- fewer round trips than the
original 256.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from pathlib import Path

from fpgas_tt.repl import ReplError, ReplNoBoard, ReplRunner

log = logging.getLogger(__name__)

DEMOS_DIR_DEFAULT = Path("/usr/share/fpgas-tt/demos")
MAX_BITSTREAM_BYTES = 256 * 1024
MAX_UPLOADS = 16
NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")
ICE40_PREAMBLE = b"\x7e\xaa\x99\x7e"
PREAMBLE_WINDOW = 64
CHUNK = 1024  # raw bytes per REPL write step (1368 base64 chars on the wire)
META_FIELDS = ("title", "author", "description", "docs_url", "repo_url")
DEMOS_MANIFEST_PATH = "/bitstreams/.demos.json"
# write_bitstream's overall REPL-session deadline: a flat floor plus a
# per-byte allowance, so a legitimately large (near MAX_BITSTREAM_BYTES)
# upload isn't bounded by the same deadline as a tiny one.
WRITE_OVERALL_BASE = 60.0
WRITE_OVERALL_PER_BYTE = 1.0 / 8192
# enable_design's own overall deadline: the site proxy in front of this
# daemon has its own read timeouts (30s/45s) -- the default overall this
# call would otherwise get (60s, from ReplRunner's step-scaled default for
# a single timeout=30.0 step) exceeds the shorter of those, so the client
# would see a raw connection reset instead of a clean REPL-task-exceeded-
# its-deadline error. Comfortably under both.
ENABLE_OVERALL_TIMEOUT = 25.0


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


def _parse_int(out: str) -> int:
    try:
        return int(out.strip())
    except ValueError as exc:
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
    if len(data) > MAX_BITSTREAM_BYTES:
        raise ValidationError(f"bitstream too large ({len(data)} bytes, limit {MAX_BITSTREAM_BYTES})")
    if ICE40_PREAMBLE not in data[:PREAMBLE_WINDOW + len(ICE40_PREAMBLE)]:
        raise ValidationError("not an iCE40 bitstream (preamble 7E AA 99 7E missing in the first 64 bytes)")


# -- snippets --
# os.listdir('/bitstreams') is guarded everywhere: a freshly-flashed board
# may not have the directory yet, and that must read as "no files", not
# crash the task.
LIST_CODE = (
    "import os, json\n"
    "try:\n"
    "    _files = os.listdir('/bitstreams')\n"
    "except OSError:\n"
    "    _files = []\n"
    "names = sorted(f[:-4] for f in _files if f.endswith('.bin'))\n"
    "en = tt.shuttle.enabled\n"
    "print(json.dumps({'names': names, 'enabled': en.name if en else None}))\n"
)

STAT_CODE = (
    "import os, json\n"
    "out = {}\n"
    "try:\n"
    "    _files = os.listdir('/bitstreams')\n"
    "except OSError:\n"
    "    _files = []\n"
    "for f in _files:\n"
    "    if f.endswith('.bin'):\n"
    "        st = os.stat('/bitstreams/' + f)\n"
    "        out[f[:-4]] = [st[6], st[8]]\n"
    "print(json.dumps(out))\n"
)

REFRESH_CODE = "tt.shuttle._design_index = None\n"

# Sweeps stale *.tmp left by a write that crashed or dropped mid-transfer,
# and makes sure /bitstreams exists (sync_demos may be the very first thing
# to touch a freshly-flashed board). Closes whatever the global `f` happens
# to reference first: an aborted write leaves it open, and some filesystems
# dislike removing (or overwriting) a file that's still open.
CLEAN_TMP_CODE = (
    "try:\n"
    "    f.close()\n"
    "except Exception:\n"
    "    pass\n"
    "import os\n"
    "try:\n"
    "    os.mkdir('/bitstreams')\n"
    "except OSError:\n"
    "    pass\n"
    "try:\n"
    "    _files = os.listdir('/bitstreams')\n"
    "except OSError:\n"
    "    _files = []\n"
    "for f in _files:\n"
    "    if f.endswith('.tmp'):\n"
    "        try:\n"
    "            os.remove('/bitstreams/' + f)\n"
    "        except OSError:\n"
    "            pass\n"
    "print('ok')\n"
)

READ_MANIFEST_CODE = (
    "try:\n"
    f"    with open({DEMOS_MANIFEST_PATH!r}) as _mf:\n"
    "        _manifest = _mf.read()\n"
    "except OSError:\n"
    "    _manifest = '{}'\n"
    "print(_manifest)\n"
)


def _write_manifest_code(manifest: dict[str, str], stale: list[str] = ()) -> str:
    """Writes the new manifest, first removing any `stale` demo .bin (a name
    that was in the old manifest but isn't in this run's -- dropped from
    index.json) so it doesn't linger and show up as an "upload"."""
    lines = []
    if stale:
        lines.append("import os\n")
        for n in stale:
            path = f"/bitstreams/{n}.bin"
            lines.append(f"try:\n    os.remove({path!r})\nexcept OSError:\n    pass\n")
        lines.append(REFRESH_CODE)
    body = json.dumps(manifest)
    lines.append(f"with open({DEMOS_MANIFEST_PATH!r}, 'w') as _mf:\n")
    lines.append(f"    _mf.write({body!r})\n")
    lines.append("print('ok')\n")
    return "".join(lines)


def _remove_code(*filenames: str) -> str:
    """Best-effort ``os.remove`` for one or more board paths under /bitstreams.

    Closes whatever the global `f` currently references first (best-effort):
    an aborted write leaves it open, and some filesystems dislike removing an
    open file."""
    lines = ["try:\n", "    f.close()\n", "except Exception:\n", "    pass\n", "import os\n"]
    for n in filenames:
        path = f"/bitstreams/{n}"
        lines.append(f"try:\n    os.remove({path!r})\nexcept OSError:\n    pass\n")
    lines.append("print('ok')\n")
    return "".join(lines)


def _enable_code(name: str, clock_hz: int | None) -> str:
    code = f"tt.shuttle.get({name!r}).enable()\n"
    if clock_hz is not None:
        code += f"tt.clock_project_PWM({int(clock_hz)})\n"
    code += "print('enabled')\n"
    return code


async def _board_names(runner: ReplRunner) -> tuple[list[str], str | None]:
    out = _parse_json(await runner.exec(LIST_CODE))
    return out["names"], out["enabled"]


async def list_designs(runner: ReplRunner, demos_dir: Path) -> dict:
    names, enabled = await _board_names(runner)
    demos = load_demo_index(demos_dir)
    return {"enabled": enabled, "designs": [_meta(n, demos) for n in names]}


async def enable_design(runner: ReplRunner, name: str, clock_hz: int | None) -> dict:
    if not NAME_RE.match(name):
        # Not a name the board could ever have -- don't even ask it (and
        # don't let an unvalidated name anywhere near board code). The HTTP
        # layer already checks this before calling in; this is
        # defense-in-depth for any other caller.
        raise DesignNotFound(name)
    names, _ = await _board_names(runner)
    if name not in names:
        raise DesignNotFound(name)
    # timeout (per-read) stays 30s -- the SPI load takes a few seconds and
    # any single read waiting on it is normal; overall is intentionally
    # tighter (see ENABLE_OVERALL_TIMEOUT) so the whole call still fails
    # cleanly before the site proxy would have reset the connection anyway.
    await runner.exec(_enable_code(name, clock_hz), timeout=30.0, overall=ENABLE_OVERALL_TIMEOUT)
    return {"enabled": name, "clock_hz": clock_hz}


def _write_steps(name: str, data: bytes) -> list[str]:
    # Write to a temp name and rename onto the real one as the very last act:
    # a crash or dropped connection mid-write must never leave a truncated
    # bitstream visible under its real name (list_designs/enable would treat
    # it as a valid, complete design).
    tmp_path = f"/bitstreams/{name}.bin.tmp"
    final_path = f"/bitstreams/{name}.bin"
    steps = ["import binascii\n" + f"f = open({tmp_path!r}, 'wb')\n"]
    for i in range(0, len(data), CHUNK):
        b64 = base64.b64encode(data[i:i + CHUNK]).decode("ascii")
        steps.append(f"f.write(binascii.a2b_base64({b64!r}))\n")
    steps.append(
        "f.close()\n"
        "import os\n"
        "import errno\n"
        # MicroPython's VfsFat os.rename() raises EEXIST when the
        # destination already exists (LFS2 doesn't) -- remove it first so
        # re-uploading/re-syncing the same name works on either. Only ENOENT
        # (it simply didn't exist yet -- the common case) is swallowed; any
        # other failure (permissions, I/O, ...) is re-raised so the real
        # cause reaches the user instead of a confusing EEXIST from the
        # rename that follows.
        "try:\n"
        f"    os.remove({final_path!r})\n"
        "except OSError as e:\n"
        "    if e.errno != errno.ENOENT:\n"
        "        raise\n"
        f"os.rename({tmp_path!r}, {final_path!r})\n"
        f"print(os.stat({final_path!r})[6])\n"
        + REFRESH_CODE
    )
    return steps


async def write_bitstream(runner: ReplRunner, name: str, data: bytes) -> None:
    tmp = f"{name}.bin.tmp"
    overall = WRITE_OVERALL_BASE + len(data) * WRITE_OVERALL_PER_BYTE
    try:
        outs = await runner.exec_steps(_write_steps(name, data), timeout=60.0, overall=overall)
    except ReplNoBoard:
        raise  # nothing we can do -- there's no board to run cleanup on
    except ReplError:
        # A failed write must not leave a straggling .tmp behind for later
        # attempts to trip over. Best-effort only: this must never mask (or
        # replace) the original failure.
        try:
            await runner.exec(_remove_code(tmp))
        except Exception:  # deliberately broad: cleanup is best-effort, must never raise
            log.warning("write_bitstream: best-effort cleanup of %s failed", tmp)
        raise
    size = _parse_int(outs[-1])
    if size != len(data):
        raise ReplError(f"board reports {size} bytes after writing {len(data)}", "")


async def evict_uploads(runner: ReplRunner, demo_names: set[str], keep: int = MAX_UPLOADS - 1) -> list[str]:
    """Delete the oldest uploads so that at most `keep` remain (demos are never touched)."""
    stats = _parse_json(await runner.exec(STAT_CODE))
    uploads = sorted(
        (mtime, n) for n, (_size, mtime) in stats.items() if n not in demo_names and NAME_RE.match(n)
    )
    victims = [n for _m, n in uploads[: max(0, len(uploads) - keep)]]
    if victims:
        lines = ["import os\n"]
        for n in victims:
            path = f"/bitstreams/{n}.bin"
            lines.append(f"try:\n    os.remove({path!r})\nexcept OSError:\n    pass\n")
        code = "".join(lines) + REFRESH_CODE + "print('ok')\n"
        await runner.exec(code)
    return victims


async def sync_demos(runner: ReplRunner, demos_dir: Path) -> dict:
    demos = load_demo_index(demos_dir)
    if not demos:
        # Nothing to do: skip the board round trip entirely so an empty (or
        # absent) demos dir never contends the one-task-at-a-time REPL lock.
        return {"synced": [], "skipped": []}
    await runner.exec(CLEAN_TMP_CODE)
    manifest_raw = await runner.exec(READ_MANIFEST_CODE)
    try:
        manifest = json.loads(manifest_raw)
        if not isinstance(manifest, dict):
            manifest = {}
    except (ValueError, TypeError):
        # Missing, corrupt, or otherwise unreadable: treat as empty rather
        # than failing the sync -- this just forces everything to re-sync,
        # which is self-healing (a fresh, correct manifest comes out the
        # other end of this call).
        log.warning("demos: manifest at %s is unreadable; treating it as empty (full re-sync)", DEMOS_MANIFEST_PATH)
        manifest = {}
    synced, skipped, new_manifest = [], [], {}
    for name in sorted(demos):
        if not NAME_RE.match(name):
            continue  # defense in depth -- load_demo_index already filters these
        src = Path(demos_dir) / f"{name}.bin"
        if not src.exists():
            log.warning("demos: %s listed in index.json but %s is missing", name, src)
            if name in manifest:
                # Still listed in the index -- just can't verify it right
                # now. Carry the old entry forward unchanged so it's neither
                # re-synced (we have no bytes to sync) nor, worse, evicted as
                # "stale" by the pass below (which must only ever act on a
                # demo actually dropped from the index, not a transiently
                # unreadable file for one still in it).
                new_manifest[name] = manifest[name]
            continue
        data = src.read_bytes()
        digest = hashlib.sha1(data).hexdigest()  # noqa: S324 - identity check, not a security digest
        if manifest.get(name) == digest:
            skipped.append(name)
        else:
            await write_bitstream(runner, name, data)
            synced.append(name)
        new_manifest[name] = digest
    # Written once, last, only if every write above actually succeeded (a
    # failure partway through leaves the previous manifest in place, so the
    # untouched-by-this-run entries still correctly compare equal next time,
    # and whatever *did* fail is retried instead of wrongly marked synced),
    # and only if anything actually changed -- a repeat sync with nothing to
    # do must not flash-write the manifest every time it happens to run.
    # `stale` is keyed off the *index* (demos), not new_manifest: a name
    # missing from new_manifest only because its file was momentarily
    # unreadable this run (handled above) must never be treated the same as
    # one genuinely dropped from index.json -- only the latter is stale.
    if new_manifest != manifest:
        stale = sorted(n for n in manifest if n not in demos and NAME_RE.match(n))
        await runner.exec(_write_manifest_code(new_manifest, stale))
    return {"synced": synced, "skipped": skipped}
