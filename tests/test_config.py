from pathlib import Path

import pytest

from fpgas_tt.config import BoardConfig, discover, load_boards, parse_hostname

DATA = Path(__file__).parent / "data" / "tt-boards.yaml"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("pi-sw1-p7", (1, 7)),
        ("pi-sw2-p48", (2, 48)),
        ("pi-sw1-p7.fpgas.welland.mithis.com", None),  # callers pass the short name
        ("raspberrypi", None),
        ("pi-sw-p7", None),
        ("", None),
    ],
)
def test_parse_hostname(name, expected):
    assert parse_hostname(name) == expected


def test_load_boards_returns_list():
    boards = load_boards(DATA)
    assert [b["slug"] for b in boards] == ["tt06", "tt03", "fpga-1", "kianv-1", "sw2-thing"]


def test_load_boards_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_boards("/nonexistent/tt-boards.yaml")


def test_load_boards_wrong_shape_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("- just: a list\n")
    with pytest.raises(ValueError, match="tt_boards"):
        load_boards(p)


def test_discover_known_board():
    assert discover("pi-sw1-p6", DATA) == BoardConfig(slug="tt06", kind="asic", switch=1, port=6, hostname="pi-sw1-p6")


def test_discover_fpga_board():
    cfg = discover("pi-sw1-p12", DATA)
    assert (cfg.slug, cfg.kind) == ("fpga-1", "fpga")


def test_discover_respects_switch():
    assert discover("pi-sw2-p1", DATA).slug == "sw2-thing"
    assert discover("pi-sw1-p1", DATA).slug == "pi-sw1-p1"  # no such board on switch 1


def test_discover_disabled_board_falls_back():
    cfg = discover("pi-sw1-p3", DATA)
    assert cfg == BoardConfig(slug="pi-sw1-p3", kind="asic", switch=1, port=3, hostname="pi-sw1-p3")


def test_discover_unknown_hostname():
    cfg = discover("raspberrypi", DATA)
    assert cfg == BoardConfig(slug="raspberrypi", kind="asic", switch=None, port=None, hostname="raspberrypi")


def test_discover_without_boards_file():
    cfg = discover("pi-sw1-p6", "/nonexistent.yaml")
    assert cfg == BoardConfig(slug="pi-sw1-p6", kind="asic", switch=1, port=6, hostname="pi-sw1-p6")


def test_discover_rejects_unknown_kind(tmp_path):
    p = tmp_path / "tt-boards.yaml"
    p.write_text("tt_boards:\n  - {slug: x, port: 1, kind: banana}\n")
    with pytest.raises(ValueError, match="kind"):
        discover("pi-sw1-p1", p)
