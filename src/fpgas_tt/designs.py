"""FPGA-board tasks: list/enable/upload bitstreams, sync demos -- all raw-REPL snippets run through ReplRunner.

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
        code = (
            "import os\n"
            + "".join(f"os.remove('/bitstreams/{n}.bin')\n" for n in victims)
            + REFRESH_CODE
            + "print('ok')\n"
        )
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
