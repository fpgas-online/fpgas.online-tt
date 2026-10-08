"""The safe state of a chip board (issue #181 of fpgas.online-test-designs): set once in RAM, nothing written."""

from __future__ import annotations

import pytest

from fpgas_tt import idle, safe
from fpgas_tt.bridge import Bridge
from fpgas_tt.repl import ReplError
from fpgas_tt.server import create_app
from tests.test_designs import board_tree, wait_for
from tests.test_server import DEMOS, IS_FPGA, IS_OTHER, NOT_NAMED


@pytest.fixture
async def bridge(fake_board):
    b = Bridge(str(fake_board.path), reopen_interval=0.05)
    await b.start()
    await wait_for(lambda: b.present)
    yield b
    await b.stop()


def an_app(bridge, tmp_path, who):
    app = create_app(bridge, identify=lambda: who(), demos_dir=DEMOS, uploads_dir=tmp_path / "pi-uploads")
    return app


def chip_sdk_started(fake_repl) -> None:
    """A chip board as SDK 2.0.4 leaves it at every start, with its default config.ini: the factory test, ui_in = 1,
    clocked at 10 Hz, in ASIC_RP_CONTROL."""
    tt = fake_repl.tt
    tt.shuttle.enabled = type("Design", (), {"name": idle.SDK_START_DESIGN})()
    tt.ui_in.value = 1
    tt.clock_hz = 10


async def test_a_chip_board_in_the_sdks_start_state_is_made_safe_once_and_nothing_is_written(
    bridge, fake_repl, tmp_path
):
    chip_sdk_started(fake_repl)
    fake_repl.tt.uio_oe_pico.value = 0x0F
    before = board_tree(fake_repl)
    app = an_app(bridge, tmp_path, lambda: IS_OTHER)
    start = safe.SafeStart(app)
    state = await start.step()
    assert state == "set: clock stopped (was 10 Hz), ui_in 0 (was 1), uio released (uio_oe_pico was 15)"
    tt = fake_repl.tt
    assert (tt.clock_hz, tt.ui_in.value, tt.uio_oe_pico.value) == (0, 0, 0)
    assert tt.shuttle.enabled.name == idle.SDK_START_DESIGN and tt.mode_str == "ASIC_RP_CONTROL"
    assert fake_repl.fos.writes == [] and board_tree(fake_repl) == before  # RAM only
    said = bytes(fake_repl.transcript)
    tt.ui_in.value = 1  # a visitor sets it again: the daemon does not undo it for this opening of the board
    assert await start.step() == state and bytes(fake_repl.transcript) == said and tt.ui_in.value == 1


async def test_a_board_that_comes_back_is_made_safe_again(bridge, fake_repl, tmp_path):
    chip_sdk_started(fake_repl)
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER))
    assert (await start.step()).startswith("set: ")
    chip_sdk_started(fake_repl)
    bridge.opens += 1  # unplugged and plugged in again, or reset: its SDK starts with the board's config.ini
    assert (await start.step()).startswith("set: ") and fake_repl.tt.ui_in.value == 0


async def test_an_fpga_board_is_left_alone(bridge, fake_repl, tmp_path):
    chip_sdk_started(fake_repl)
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_FPGA))
    assert await start.step() == "not a chip board"
    assert fake_repl.transcript == b"" and fake_repl.tt.ui_in.value == 1


async def test_a_board_not_known_yet_is_looked_at_again(bridge, fake_repl, tmp_path):
    chip_sdk_started(fake_repl)
    who = [NOT_NAMED]
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: who[0]))
    assert (await start.step()).startswith("waiting: ") and fake_repl.transcript == b""
    who[0] = IS_OTHER  # the boot check's report is there now
    assert (await start.step()).startswith("set: ")


async def test_a_visitor_who_came_first_has_the_board_as_they_found_it(bridge, fake_repl, tmp_path):
    chip_sdk_started(fake_repl)
    app = an_app(bridge, tmp_path, lambda: IS_OTHER)
    app["websockets"].add(object())  # a Commander, or a terminal
    start = safe.SafeStart(app)
    assert await start.step() == "left: the board was in use first"
    app["websockets"].clear()
    assert await start.step() == "left: the board was in use first"
    assert fake_repl.transcript == b"" and fake_repl.tt.ui_in.value == 1


@pytest.mark.parametrize(
    "change,state",
    [
        (lambda tt, g: g.pop("tt"), "left: the SDK is not running"),
        (
            lambda tt, g: setattr(tt.shuttle, "enabled", type("D", (), {"name": "tt_um_other"})()),
            "left: tt_um_other is enabled",
        ),  # fmt: skip
        (lambda tt, g: setattr(tt.shuttle, "enabled", None), "left: no project is enabled"),
        (lambda tt, g: setattr(tt, "mode_str", "ASIC_MANUAL_INPUTS"), "left: the board is in ASIC_MANUAL_INPUTS mode"),
    ],
)
async def test_only_the_sdks_start_state_is_changed(bridge, fake_repl, tmp_path, change, state):
    chip_sdk_started(fake_repl)
    tt = fake_repl.tt
    change(tt, fake_repl._globals)
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER))
    assert await start.step() == state
    assert (tt.clock_hz, tt.ui_in.value) == (10, 1)


async def test_a_board_that_does_not_take_the_safe_state_is_a_failure_tried_again_later(
    bridge, fake_repl, tmp_path, monkeypatch
):
    chip_sdk_started(fake_repl)
    monkeypatch.setattr(type(fake_repl.tt), "clock_project_stop", lambda self: None)  # the clock keeps running
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER))
    assert (await start.step()).startswith("failed: the board did not take the safe state")
    said = bytes(fake_repl.transcript)
    assert (await start.step()).startswith("failed: ") and bytes(fake_repl.transcript) == said  # not at once


@pytest.mark.parametrize("reply", [None, [], {"sdk": "yes"}, {}])
def test_a_reply_that_is_not_one_is_an_error(reply):
    with pytest.raises(ReplError):
        safe.outcome(reply)


def test_the_health_endpoint_says_what_the_safe_start_did():
    assert safe.SafeStart({"bridge": None}).health() == {"state": "waiting"}
