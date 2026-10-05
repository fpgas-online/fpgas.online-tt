"""USB identity of a tty, read from sysfs.

``/health`` reports it so the site can tell "no board" apart from "some other
USB serial device answered to /dev/ttboard", and the board's USB serial number
is how the daemon finds its board in the boot check's report (identity.py).
Best effort only: anything missing or unreadable is reported as ``None``,
never as an error.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SYSFS_TTY_ROOT = "/sys/class/tty"
# /sys/class/tty/<name>/device points at the USB *interface*; idVendor and
# idProduct live on the USB device above it -- one level up for a plain tty,
# two for a CDC ACM whose interface sits under an association.
MAX_PARENT_LEVELS = 3


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError as exc:
        log.debug("usbinfo: cannot read %s: %s", path, exc)
        return None


def _usb_device(device_path: str) -> tuple[Path, str, str] | None:
    """The sysfs directory of the USB device behind a tty, with its vendor and
    product ids; ``None`` if the tty is not a USB one.

    *device_path* may be a udev symlink; it is resolved first.
    """
    name = os.path.basename(os.path.realpath(device_path))
    if not name:
        return None
    link = Path(SYSFS_TTY_ROOT) / name / "device"
    try:
        node = Path(os.path.realpath(link))
    except OSError as exc:  # pragma: no cover - realpath rarely raises
        log.debug("usbinfo: cannot resolve %s: %s", link, exc)
        return None
    if not node.exists():
        log.debug("usbinfo: no sysfs device for %s", device_path)
        return None
    for _ in range(MAX_PARENT_LEVELS):
        node = node.parent
        vid = _read(node / "idVendor")
        pid = _read(node / "idProduct")
        if vid and pid:
            return node, vid.lower(), pid.lower()
    log.debug("usbinfo: no idVendor/idProduct above %s", link)
    return None


def vid_pid_for_tty(device_path: str) -> str | None:
    """``/dev/ttboard`` → ``"2e8a:0005"``, or ``None`` if it is not a USB tty."""
    found = _usb_device(device_path)
    if found is None:
        return None
    _, vid, pid = found
    return f"{vid}:{pid}"


def usb_serial_for_tty(device_path: str) -> str | None:
    """``/dev/ttboard`` → the USB serial number of the device behind it (the
    value on the board's label), or ``None`` if it is not a USB tty or the
    device gives none."""
    found = _usb_device(device_path)
    if found is None:
        return None
    return _read(found[0] / "serial")
