import os

from fpgas_tt import usbinfo
from fpgas_tt.usbinfo import vid_pid_for_tty


def _fake_sysfs(tmp_path, tty="ttyACM0", vid="2e8a", pid="0005", depth=1):
    """Mirror the real shape: /sys/class/tty/<tty>/device is a symlink to the
    USB *interface* directory, and idVendor/idProduct live on its parent(s).
    """
    usbdev = tmp_path / "devices" / "usb1" / "1-1"
    iface = usbdev
    for i in range(depth):
        iface = iface / f"1-1:1.{i}"
    iface.mkdir(parents=True)
    (usbdev / "idVendor").write_text(vid + "\n")
    (usbdev / "idProduct").write_text(pid + "\n")
    root = tmp_path / "class" / "tty"
    (root / tty).mkdir(parents=True)
    os.symlink(iface, root / tty / "device")
    return root


def test_vid_pid_for_tty_reads_sysfs(tmp_path, monkeypatch):
    root = _fake_sysfs(tmp_path)
    monkeypatch.setattr(usbinfo, "SYSFS_TTY_ROOT", str(root))
    assert vid_pid_for_tty("/dev/ttyACM0") == "2e8a:0005"


def test_vid_pid_for_tty_walks_up_for_nested_cdc(tmp_path, monkeypatch):
    root = _fake_sysfs(tmp_path, vid="2E8A", pid="000F", depth=2)
    monkeypatch.setattr(usbinfo, "SYSFS_TTY_ROOT", str(root))
    assert vid_pid_for_tty("/dev/ttyACM0") == "2e8a:000f"


def test_vid_pid_for_tty_follows_symlink(tmp_path, monkeypatch):
    root = _fake_sysfs(tmp_path)
    monkeypatch.setattr(usbinfo, "SYSFS_TTY_ROOT", str(root))
    link = tmp_path / "ttboard"
    os.symlink("/dev/ttyACM0", link)
    assert vid_pid_for_tty(str(link)) == "2e8a:0005"


def test_vid_pid_for_tty_none_for_pty(fake_board):
    assert vid_pid_for_tty(str(fake_board.path)) is None


def test_vid_pid_for_tty_none_when_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(usbinfo, "SYSFS_TTY_ROOT", str(tmp_path / "nope"))
    assert vid_pid_for_tty("/dev/ttyACM0") is None
