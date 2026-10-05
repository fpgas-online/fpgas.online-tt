"""What the board is comes from its USB serial and the boot check's report, never from a port."""

import json

import pytest

from fpgas_tt import identity
from fpgas_tt.identity import identify

SERIAL = "a2961e5cac65b25f"
OTHER_SERIAL = "4df39a7a6856f86f"


@pytest.fixture
def serial(monkeypatch):
    monkeypatch.setattr(identity, "usb_serial_for_tty", lambda device: SERIAL)


def _report(tmp_path, boards):
    path = tmp_path / "verify.json"
    path.write_text(json.dumps({"schema_version": 2, "result": "pass", "boards": boards}))
    return path


def _tt(serial=SERIAL, variant="tt-fpga"):
    # the shape fpgas-verify writes for a Tiny Tapeout board (a report read from a Pi on 2026-10-04)
    return {"board": "tt", "variant": variant, "found": {"variant": variant, "usb": "1-1.2", "serial": serial},
            "tests": [], "result": "pass"}


def test_an_fpga_demo_board_is_fpga(tmp_path, serial):
    who = identify("/dev/ttboard", _report(tmp_path, [_tt()]))
    assert (who.kind, who.usb_serial, who.variant) == ("fpga", SERIAL, "tt-fpga")
    assert SERIAL in who.reason and "tt-fpga" in who.reason


def test_the_board_is_found_by_its_serial_among_several(tmp_path, serial):
    boards = [{"board": "acorn", "variant": "cle-215+", "found": {"serial": SERIAL}}, _tt(OTHER_SERIAL), _tt()]
    assert identify("/dev/ttboard", _report(tmp_path, boards)).kind == "fpga"


def test_the_serial_may_be_in_the_identity(tmp_path, serial):
    board = {"board": "tt", "variant": "tt-fpga", "identity": {"usb_serial": SERIAL}}
    assert identify("/dev/ttboard", _report(tmp_path, [board])).kind == "fpga"


def test_another_tiny_tapeout_variant_is_not_fpga(tmp_path, serial):
    who = identify("/dev/ttboard", _report(tmp_path, [_tt(variant="tt-asic")]))
    assert (who.kind, who.variant) == ("other", "tt-asic")


def test_unknown_before_the_boot_check_has_written_its_report(tmp_path, serial):
    who = identify("/dev/ttboard", tmp_path / "verify.json")
    assert (who.kind, who.usb_serial, who.variant) == ("unknown", SERIAL, None)
    assert "has not written" in who.reason


def test_unknown_when_the_report_names_another_board(tmp_path, serial):
    who = identify("/dev/ttboard", _report(tmp_path, [_tt(OTHER_SERIAL)]))
    assert who.kind == "unknown" and "does not name board " + SERIAL in who.reason


def test_unknown_when_the_report_does_not_say_what_the_board_is(tmp_path, serial):
    board = {"board": "tt", "found": {"serial": SERIAL}, "result": "error"}
    who = identify("/dev/ttboard", _report(tmp_path, [board]))
    assert who.kind == "unknown" and "without saying what it is" in who.reason


def test_unknown_without_a_usb_serial(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "usb_serial_for_tty", lambda device: None)
    who = identify("/dev/ttboard", _report(tmp_path, [_tt()]))
    assert (who.kind, who.usb_serial) == ("unknown", None)


@pytest.mark.parametrize(
    "text",
    ["", "{", "[]", '{"boards": {}}', '{"result": "pass"}', "\xff\xfe"],
)
def test_a_report_that_cannot_be_used_is_unknown_not_a_crash(tmp_path, serial, text, caplog):
    path = tmp_path / "verify.json"
    path.write_bytes(text.encode("latin-1"))
    who = identify("/dev/ttboard", path)
    assert who.kind == "unknown" and "cannot be used" in who.reason
    assert "cannot be used" in caplog.text


def test_entries_that_are_not_boards_are_passed_over(tmp_path, serial):
    boards = ["x", None, {"board": "tt"}, {"board": "tt", "found": "y", "identity": 3}, _tt()]
    assert identify("/dev/ttboard", _report(tmp_path, boards)).kind == "fpga"


def test_a_report_that_is_too_large_is_not_read(tmp_path, serial, monkeypatch):
    monkeypatch.setattr(identity, "MAX_REPORT_BYTES", 64)
    who = identify("/dev/ttboard", _report(tmp_path, [_tt()]))
    assert who.kind == "unknown" and "larger than" in who.reason


def test_a_report_that_cannot_be_opened_is_unknown(tmp_path, serial):
    who = identify("/dev/ttboard", tmp_path)  # a directory
    assert who.kind == "unknown" and "cannot be used" in who.reason
