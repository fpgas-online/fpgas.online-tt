"""What the board on this Pi is, taken from the board itself.

Nothing here knows which switch port a board is plugged into, and no file
says which Pi carries which board: a board moved to another port is the same
board. Two things are read, both on this Pi, each time somebody asks:

- the USB serial number of the device behind ``/dev/ttboard`` (sysfs): the
  value on the board's label;
- the boot check's report (fpgas-verify writes ``/run/fpgas-online/verify.json``
  whole, by rename, when it has checked the boards on this Pi), which says for
  each board it found what it is.

The board in the report with this USB serial gives the kind: an FPGA demo
board (variant ``tt-fpga``) is ``fpga``. Until the report names the board
(no boot check yet this boot, a board plugged in since, a board the check does
not know, such as one with a Tiny Tapeout chip) the kind is ``unknown``: the
serial bridge works as always and the design routes say why they do not.

The daemon never asks the board what it is: the boot check does that, with the
port to itself, and a second prober would get in a visitor's way.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from fpgas_tt.usbinfo import usb_serial_for_tty

log = logging.getLogger(__name__)

REPORT_DEFAULT = Path("/run/fpgas-online/verify.json")
# fpgas-verify's name for a Tiny Tapeout demo board, and the variants of it that carry an FPGA.
TT_BOARD = "tt"
FPGA_VARIANTS = frozenset({"tt-fpga"})
# A report holds each test's output; this is many times the largest seen, and bounds what one request reads.
MAX_REPORT_BYTES = 4 * 1024 * 1024

FPGA = "fpga"
OTHER = "other"  # a Tiny Tapeout board the report names that is not an FPGA demo board
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Identity:
    kind: str  # FPGA, OTHER or UNKNOWN
    usb_serial: str | None
    variant: str | None  # the report's variant for this board, when it names the board
    reason: str  # where the kind came from, or why it is not known; for /health and for people


def _reported_boards(report_path: Path) -> list[dict]:
    """The Tiny Tapeout boards in the boot check's report. Raises OSError when
    the file cannot be read and ValueError when it is not a report."""
    with open(report_path, "rb") as f:
        raw = f.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError(f"larger than {MAX_REPORT_BYTES} bytes")
    doc = json.loads(raw)  # a JSONDecodeError and a UnicodeDecodeError are both a ValueError
    if not isinstance(doc, dict) or not isinstance(doc.get("boards"), list):
        raise ValueError("not an object with a 'boards' list")
    return [b for b in doc["boards"] if isinstance(b, dict) and b.get("board") == TT_BOARD]


def _serial_of(board: dict) -> str | None:
    """The USB serial the report gives a board: where it was found, or its identity."""
    for holder, key in (("found", "serial"), ("identity", "usb_serial")):
        value = board.get(holder)
        if isinstance(value, dict) and isinstance(value.get(key), str) and value[key]:
            return value[key]
    return None


def identify(device: str, report_path: Path | str = REPORT_DEFAULT) -> Identity:
    """What the board behind *device* is, from its USB serial and the boot check's report."""
    report_path = Path(report_path)
    serial = usb_serial_for_tty(device)
    if serial is None:
        return Identity(UNKNOWN, None, None, f"no USB device with a serial number is behind {device}")
    try:
        boards = _reported_boards(report_path)
    except FileNotFoundError:
        return Identity(UNKNOWN, serial, None, f"the boot check has not written {report_path} yet")
    except (OSError, ValueError) as exc:
        log.warning("identity: %s cannot be used: %s", report_path, exc)
        return Identity(UNKNOWN, serial, None, f"{report_path} cannot be used: {exc}")
    for board in boards:
        if _serial_of(board) != serial:
            continue
        variant = board.get("variant")
        if not isinstance(variant, str) or not variant:
            return Identity(UNKNOWN, serial, None, f"{report_path} names board {serial} without saying what it is")
        kind = FPGA if variant in FPGA_VARIANTS else OTHER
        return Identity(kind, serial, variant, f"{report_path} says board {serial} is {variant}")
    return Identity(UNKNOWN, serial, None, f"{report_path} does not name board {serial}")
