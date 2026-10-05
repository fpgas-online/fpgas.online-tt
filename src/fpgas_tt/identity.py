"""What the board on this Pi is, taken from the board itself.

Nothing here knows which switch port a board is plugged into, and no file
says which Pi carries which board: a board moved to another port is the same
board. Two things are read, both on this Pi, each time somebody asks:

- the USB serial number of the device behind ``/dev/ttboard`` (sysfs): the
  value on the board's label;
- the boot check's report (fpgas-verify writes ``/run/fpgas-online/verify.json``
  whole, by rename, when it has checked the boards on this Pi), which says for
  each board it found what the board said it is.

The board in the report with this USB serial gives the kind, by its
identity's ``chip`` (what rpi-hwid read from the board's own SDK): ``fpga``
for an FPGA demo board, anything else (a Tiny Tapeout chip) is ``other``.
The report's ``variant`` is not asked: the check gives every Raspberry Pi USB
device the variant ``tt-fpga`` before it has read anything from it
(fpgas.online-test-designs issue #124); once that is fixed the variant can be
believed here.

Until the report says what the board is, the kind is ``unknown``: no boot
check yet this boot, a board plugged in since, or a check that found the board
and could not read its identity (the report's own reason is passed on). The
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
# fpgas-verify's name for a Tiny Tapeout demo board, and what an FPGA demo board's identity gives as its chip.
TT_BOARD = "tt"
FPGA_CHIP = "fpga"
# The identity's fields that say why the board's own word was not read (fpgas-verify, boards/tt_fpga.py).
WHY_NOT_READ = ("tinytapeout_error", "tinytapeout_note")
# A report holds each test's output; this is many times the largest seen, and bounds what one request reads.
MAX_REPORT_BYTES = 4 * 1024 * 1024

FPGA = "fpga"
OTHER = "other"  # a Tiny Tapeout board that says it carries something other than the FPGA breakout
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Identity:
    kind: str  # FPGA, OTHER or UNKNOWN
    usb_serial: str | None
    chip: str | None  # what the board said it carries, from the report
    reason: str  # where the kind came from, or why it is not known; for /health and for people


def _reported_boards(report_path: Path) -> list[dict]:
    """The Tiny Tapeout boards in the boot check's report. Raises OSError when
    the file cannot be read and ValueError when it is not a report."""
    with open(report_path, "rb") as f:
        raw = f.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError(f"larger than {MAX_REPORT_BYTES} bytes")
    try:
        doc = json.loads(raw)  # a JSONDecodeError and a UnicodeDecodeError are both a ValueError
    except RecursionError as exc:
        raise ValueError("nested too deeply to be a report") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("boards"), list):
        raise ValueError("not an object with a 'boards' list")
    return [b for b in doc["boards"] if isinstance(b, dict) and b.get("board") == TT_BOARD]


def _part(board: dict, name: str) -> dict:
    value = board.get(name)
    return value if isinstance(value, dict) else {}


def _text(value) -> str | None:
    """A field of the report as text, without the space around it; None when it is not text or is empty."""
    return (value.strip() or None) if isinstance(value, str) else None


def _serial_of(board: dict) -> str | None:
    """The USB serial the report gives a board: where it was found, or its identity."""
    return _text(_part(board, "found").get("serial")) or _text(_part(board, "identity").get("usb_serial"))


def _why_not_read(board: dict) -> str:
    """The report's own reason a board's chip is not in it."""
    identity = _part(board, "identity")
    for key in WHY_NOT_READ:
        if _text(identity.get(key)):
            return _text(identity[key])
    return _text(board.get("reason")) or "the boot check did not read what the board carries"


_last_unusable: str | None = None


def _unusable(report_path: Path, exc: Exception) -> str:
    """Why the report cannot be used; logged when it changes, not at every request."""
    global _last_unusable
    reason = f"{report_path} cannot be used: {exc}"
    if reason != _last_unusable:
        log.warning("identity: %s", reason)
        _last_unusable = reason
    return reason


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
        return Identity(UNKNOWN, serial, None, _unusable(report_path, exc))
    global _last_unusable
    _last_unusable = None  # a report that reads again: the next one that does not is said again
    for board in boards:
        if _serial_of(board) != serial:
            continue
        chip = _text(_part(board, "identity").get("chip"))
        if chip is None:
            return Identity(UNKNOWN, serial, None,
                            f"{report_path} names board {serial} without what it carries: {_why_not_read(board)}")
        kind = FPGA if chip.lower() == FPGA_CHIP else OTHER
        return Identity(kind, serial, chip, f"{report_path} says board {serial} carries {chip}")
    return Identity(UNKNOWN, serial, None, f"{report_path} does not name board {serial}")
