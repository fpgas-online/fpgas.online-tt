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
            raw_port = board.get("port")
            if raw_port is None:  # a reserved/"coming soon" entry
                continue
            # YAML quoting must not change identity: `port: "6"` is port 6.
            if (int(board.get("switch", 1)), int(raw_port)) != (switch, port):
                continue
            kind = board.get("kind", "asic")
            if kind not in KINDS:
                raise ValueError(f"{boards_path}: board {board.get('slug')!r} has unknown kind {kind!r}")
            if "slug" not in board:
                raise ValueError(f"{boards_path}: board on switch {switch} port {port} has no 'slug'")
            return BoardConfig(slug=str(board["slug"]), kind=kind, switch=switch, port=port, hostname=hostname)

    return BoardConfig(slug=hostname, kind="asic", switch=switch, port=port, hostname=hostname)
