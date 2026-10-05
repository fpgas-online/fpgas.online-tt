"""What the board is comes from its USB serial and the boot check's report, never from a port."""

import json
from pathlib import Path

import pytest

from fpgas_tt import identity
from fpgas_tt.identity import identify

DATA = Path(__file__).parent / "data"
# Reports read from Pis at Welland, each test's output left out: a passing FPGA demo board on 2026-10-05, and
# on 2026-10-04 a board the check found but whose own word it could not read (rpi-hwid was not installed).
REAL_FPGA = DATA / "verify-tt-fpga-2026-10-05.json"
REAL_NOT_READ = DATA / "verify-tt-no-rpi-hwid-2026-10-04.json"
SERIAL = "a2961e5cac65b25f"
OTHER_SERIAL = "4df39a7a6856f86f"


@pytest.fixture
def serial(monkeypatch):
    monkeypatch.setattr(identity, "usb_serial_for_tty", lambda device: SERIAL)


@pytest.fixture(autouse=True)
def _forget_last_warning(monkeypatch):
    monkeypatch.setattr(identity, "_last_unusable", None)


def _report(tmp_path, boards):
    path = tmp_path / "verify.json"
    path.write_text(json.dumps({"schema_version": 2, "result": "pass", "boards": boards}))
    return path


def _tt(serial=SERIAL, chip="fpga", result="pass", **more_identity):
    """A Tiny Tapeout board as fpgas-verify writes it: the variant is `tt-fpga` whatever the board is (the
    check sets it when it finds the USB device), and the identity holds what the board said."""
    ident = {"board": "tt", "kind": "tt", "variant": "tt-fpga", "serial": serial, "usb": "1-1.2",
             "usb_serial": serial, **({"chip": chip} if chip is not None else {}), **more_identity}
    return {"board": "tt", "variant": "tt-fpga", "found": {"variant": "tt-fpga", "usb": "1-1.2", "serial": serial},
            "identity": ident, "tests": [], "result": result}


def test_the_real_report_of_an_fpga_demo_board_is_fpga(serial):
    who = identify("/dev/ttboard", REAL_FPGA)
    assert (who.kind, who.usb_serial, who.chip) == ("fpga", SERIAL, "fpga")
    assert SERIAL in who.reason and "carries fpga" in who.reason


def test_the_real_report_of_a_board_that_was_not_read_is_unknown_with_the_checks_reason(monkeypatch):
    monkeypatch.setattr(identity, "usb_serial_for_tty", lambda device: OTHER_SERIAL)
    who = identify("/dev/ttboard", REAL_NOT_READ)
    assert (who.kind, who.usb_serial, who.chip) == ("unknown", OTHER_SERIAL, None)
    assert "rpi-hwid is not installed" in who.reason


def test_a_board_with_a_tiny_tapeout_chip_is_not_fpga_whatever_its_variant(tmp_path, serial):
    """The check calls every Raspberry Pi USB device `tt-fpga`: the chip the board named decides."""
    who = identify("/dev/ttboard", _report(tmp_path, [_tt(chip="asic", result="fail", shuttle="tt06")]))
    assert (who.kind, who.chip) == ("other", "asic")


@pytest.mark.parametrize("chip", [None, "", "  ", 7])
def test_a_variant_alone_does_not_make_a_board_fpga(tmp_path, serial, chip):
    who = identify("/dev/ttboard", _report(tmp_path, [_tt(chip=chip)]))
    assert who.kind == "unknown" and "without what it carries" in who.reason


def test_a_chip_that_is_null_in_the_report_is_unknown(tmp_path, serial):
    board = _tt(chip=None)
    board["identity"]["chip"] = None  # written as JSON null
    assert identify("/dev/ttboard", _report(tmp_path, [board])).kind == "unknown"


@pytest.mark.parametrize("chip", ["fpga", "FPGA", " fpga\n"])
def test_the_chip_is_read_whatever_its_case_or_the_space_around_it(tmp_path, serial, chip):
    assert identify("/dev/ttboard", _report(tmp_path, [_tt(chip=chip)])).kind == "fpga"


def test_the_reason_a_board_was_not_read_is_passed_on(tmp_path, serial):
    board = _tt(chip=None, result="error", tinytapeout_error="the demo board's main.py is not the SDK's own")
    who = identify("/dev/ttboard", _report(tmp_path, [board]))
    assert who.kind == "unknown" and "main.py is not the SDK's own" in who.reason


def test_a_check_that_crashed_is_unknown_with_its_reason(tmp_path, serial):
    """runner.py's report for a board whose check raised: found and a base identity, no chip."""
    board = {"board": "tt", "found": {"variant": "tt-fpga", "usb": "1-1.2", "serial": SERIAL}, "result": "error",
             "reason": "the check crashed: KeyError: 'x'",
             "identity": {"board": "tt", "kind": "tt", "variant": "tt-fpga", "usb_serial": SERIAL}}
    who = identify("/dev/ttboard", _report(tmp_path, [board]))
    assert who.kind == "unknown" and "the check crashed" in who.reason


@pytest.mark.parametrize("result", ["pass", "fail", "error", "changed"])
def test_the_kind_does_not_depend_on_how_the_tests_went(tmp_path, serial, result):
    assert identify("/dev/ttboard", _report(tmp_path, [_tt(result=result)])).kind == "fpga"


def test_the_board_is_found_by_its_serial_among_several(tmp_path, serial):
    boards = [{"board": "acorn", "variant": "cle-215+", "found": {"serial": SERIAL}, "identity": {"chip": "fpga"}},
              _tt(OTHER_SERIAL, chip="asic"), _tt()]
    assert identify("/dev/ttboard", _report(tmp_path, boards)).kind == "fpga"


def test_the_serial_may_be_in_the_identity_alone(tmp_path, serial):
    board = {"board": "tt", "identity": {"usb_serial": SERIAL, "chip": "fpga"}}
    assert identify("/dev/ttboard", _report(tmp_path, [board])).kind == "fpga"


def test_unknown_before_the_boot_check_has_written_its_report(tmp_path, serial):
    who = identify("/dev/ttboard", tmp_path / "verify.json")
    assert (who.kind, who.usb_serial, who.chip) == ("unknown", SERIAL, None)
    assert "has not written" in who.reason


def test_unknown_when_the_report_names_another_board(tmp_path, serial):
    """A board plugged in since the check ran: the report is about the one that was there."""
    who = identify("/dev/ttboard", _report(tmp_path, [_tt(OTHER_SERIAL)]))
    assert who.kind == "unknown" and "does not name board " + SERIAL in who.reason


def test_unknown_without_a_usb_serial(monkeypatch):
    monkeypatch.setattr(identity, "usb_serial_for_tty", lambda device: None)
    who = identify("/dev/ttboard", REAL_FPGA)
    assert (who.kind, who.usb_serial) == ("unknown", None)


@pytest.mark.parametrize(
    "text",
    ["", "{", "[]", '{"boards": {}}', '{"result": "pass"}', "\xff\xfe", "[" * 100_000],
)
def test_a_report_that_cannot_be_used_is_unknown_not_a_crash(tmp_path, serial, text, caplog):
    path = tmp_path / "verify.json"
    path.write_bytes(text.encode("latin-1"))
    for _ in range(3):
        who = identify("/dev/ttboard", path)
        assert who.kind == "unknown" and "cannot be used" in who.reason
    assert caplog.text.count("cannot be used") == 1  # said once, not at every request
    # ... and said again when it happens after a report that could be read
    path.write_text(json.dumps({"boards": []}))
    assert identify("/dev/ttboard", path).reason.endswith("does not name board " + SERIAL)
    path.write_bytes(text.encode("latin-1"))
    identify("/dev/ttboard", path)
    assert caplog.text.count("cannot be used") == 2


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
