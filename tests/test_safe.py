"""The safe state of a chip board (issue #181 of fpgas.online-test-designs): set once in RAM, nothing written."""

from __future__ import annotations

import pytest

from fpgas_tt import idle, safe
from fpgas_tt.bridge import Bridge
from fpgas_tt.repl import ReplBusy, ReplError
from fpgas_tt.server import create_app
from tests.fakerepl import PIN_IN, PIN_OUT, PULL_DOWN
from tests.test_designs import board_tree, wait_for
from tests.test_server import DEMOS, IS_FPGA, IS_OTHER, NOT_NAMED

SET = (
    "set: clock stopped (was 10 Hz; its pin released), ui_in driven to 0 (read {read}), "
    "uio released (uio_oe_pico was {oe})"
)


@pytest.fixture
async def bridge(fake_board):
    b = Bridge(str(fake_board.path), reopen_interval=0.05)
    await b.start()
    await wait_for(lambda: b.present)
    yield b
    await b.stop()


@pytest.fixture
def settled(monkeypatch):
    """No quiet time after the board is opened: most tests are about what happens once it has passed."""
    monkeypatch.setattr(safe, "SETTLE", 0.0)


def an_app(bridge, tmp_path, who):
    return create_app(bridge, identify=who, demos_dir=DEMOS, uploads_dir=tmp_path / "pi-uploads")


def chip_sdk_started(fake_repl) -> None:
    """A chip board as SDK 2.0.4 leaves it at every start, with its default config.ini: the factory test, ui_in = 1,
    clocked at 10 Hz, in ASIC_RP_CONTROL, the RP2040 driving ui_in."""
    tt = fake_repl.tt
    tt.shuttle.enabled = type("Design", (), {"name": idle.SDK_START_DESIGN})()
    tt.ui_in.value = 1
    tt.clock_hz = 10


def driven(tt) -> list[int]:
    return [k for k in range(8) if tt.ui_pin(k).mode == PIN_OUT]


async def test_a_chip_board_in_the_sdks_start_state_is_made_safe_once_and_nothing_is_written(
    bridge, fake_repl, tmp_path, settled
):
    chip_sdk_started(fake_repl)
    fake_repl.tt.uio_oe_pico.value = 0x0F
    before = board_tree(fake_repl)
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER))
    state = await start.step()
    assert state == SET.format(read=1, oe=15)
    tt = fake_repl.tt
    assert (tt.clock_hz, tt.ui_in.value, tt.uio_oe_pico.value, driven(tt)) == (0, 0, 0, list(range(8)))
    assert tt.shuttle.enabled.name == idle.SDK_START_DESIGN and tt.mode_str == "ASIC_RP_CONTROL"
    assert fake_repl.fos.writes == [] and board_tree(fake_repl) == before  # RAM only
    said = bytes(fake_repl.transcript)
    tt.ui_in.value = 1  # a visitor sets it again: the daemon does not undo it for this opening of the board
    assert await start.step() == state and bytes(fake_repl.transcript) == said and tt.ui_in.value == 1


async def test_after_the_boot_check_the_ui_in_pins_are_driven_again(bridge, fake_repl, tmp_path, settled):
    """fpgas.online-test-designs issue #196: the wiring test leaves ui_in as inputs with no pull, ui_in[0] floating
    high, so the factory test drives its counter onto uio, and so onto ui_in[1:3] through the HAT."""
    chip_sdk_started(fake_repl)
    tt = fake_repl.tt
    tt.released_by_the_boot_check()
    assert tt.ui_in.value == 0b0111  # as read on board de641070db746f27 on 8 Oct 2026
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER))
    assert await start.step() == SET.format(read=7, oe=0)
    assert (tt.ui_in.value, driven(tt)) == (0, list(range(8)))
    assert all(tt.ui_pin(k).pull == PULL_DOWN for k in range(8))


async def test_a_dip_switch_that_is_on_is_not_driven_against_and_is_said(bridge, fake_repl, tmp_path, settled):
    chip_sdk_started(fake_repl)
    tt = fake_repl.tt
    tt.released_by_the_boot_check()
    tt.dip_on = {4}
    state = await safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER)).step()
    assert state.endswith("; ui_in[4] held high on the demo board (a DIP switch that is on?), left an input")
    assert tt.ui_pin(4).mode == PIN_IN and driven(tt) == [0, 1, 2, 3, 5, 6, 7]


async def test_ui_in0_held_high_is_not_a_safe_state(bridge, fake_repl, tmp_path, settled):
    """With ui_in[0] high the chip still drives uio: a failure, said loudly, not a "set"."""
    chip_sdk_started(fake_repl)
    tt = fake_repl.tt
    tt.released_by_the_boot_check()
    tt.dip_on = {0}
    assert (await safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER)).step()).startswith(
        "failed: the board did not take the safe state"
    )


async def test_the_board_is_asked_only_after_it_has_been_quiet_since_it_was_opened(bridge, fake_repl, tmp_path):
    chip_sdk_started(fake_repl)
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER))
    assert await start.step() == "waiting" and fake_repl.transcript == b""  # its SDK may still be starting
    start._seen = (start._seen[0], start._seen[1] - safe.SETTLE)
    assert (await start.step()).startswith("set: ")


async def test_a_visitor_who_comes_while_it_settles_has_the_board_as_they_found_it(bridge, fake_repl, tmp_path):
    chip_sdk_started(fake_repl)
    app = an_app(bridge, tmp_path, lambda: IS_OTHER)
    start = safe.SafeStart(app)
    assert await start.step() == "waiting"
    app["websockets"].add(object())  # a Commander, or a terminal
    assert await start.step() == "left: the board was in use first"
    app["websockets"].clear()
    start._seen = (start._seen[0], start._seen[1] - safe.SETTLE)
    assert await start.step() == "left: the board was in use first"
    assert fake_repl.transcript == b"" and fake_repl.tt.clock_hz == 10


async def test_a_board_that_comes_back_is_made_safe_again(bridge, fake_repl, tmp_path, settled):
    chip_sdk_started(fake_repl)
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER))
    assert (await start.step()).startswith("set: ")
    chip_sdk_started(fake_repl)
    bridge.opens += 1  # unplugged and plugged in again, or reset: its SDK starts with the board's config.ini
    assert (await start.step()).startswith("set: ") and fake_repl.tt.ui_in.value == 0


async def test_an_fpga_board_is_left_alone(bridge, fake_repl, tmp_path, settled):
    chip_sdk_started(fake_repl)
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_FPGA))
    assert await start.step() == "not a chip board"
    assert fake_repl.transcript == b"" and fake_repl.tt.ui_in.value == 1


async def test_a_board_not_known_yet_is_looked_at_again(bridge, fake_repl, tmp_path, settled):
    chip_sdk_started(fake_repl)
    who = [NOT_NAMED]
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: who[0]))
    assert (await start.step()).startswith("waiting: ") and fake_repl.transcript == b""
    who[0] = IS_OTHER  # the boot check's report is there now
    assert (await start.step()).startswith("set: ")


@pytest.mark.parametrize("busy", ["repl", "taken"])
async def test_a_board_some_task_has_is_in_use(bridge, fake_repl, tmp_path, settled, busy):
    chip_sdk_started(fake_repl)
    app = an_app(bridge, tmp_path, lambda: IS_OTHER)
    if busy == "taken":
        app["taken"].take()
    else:
        await app["repl"]._lock.acquire()
    assert await safe.SafeStart(app).step() == "left: the board was in use first"
    assert fake_repl.transcript == b""


@pytest.mark.parametrize(
    "change,state",
    [
        (lambda tt, g: g.pop("tt"), "left: the SDK is not running"),
        (lambda tt, g: setattr(tt.shuttle, "enabled", type("D", (), {"name": "tt_um_other"})()),
         "left: tt_um_other is enabled"),  # fmt: skip
        (lambda tt, g: setattr(tt.shuttle, "enabled", None), "left: no project is enabled"),
        (lambda tt, g: setattr(tt, "mode_str", "ASIC_MANUAL_INPUTS"), "left: the board is in ASIC_MANUAL_INPUTS mode"),
        (lambda tt, g: setattr(tt, "clock_hz", 0),
         "left: the factory test is not being clocked (not the SDK's start state)"),  # fmt: skip
    ],
)
async def test_only_the_sdks_start_state_is_changed(bridge, fake_repl, tmp_path, settled, change, state):
    chip_sdk_started(fake_repl)
    tt = fake_repl.tt
    change(tt, fake_repl._globals)
    clock = tt.clock_hz
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER))
    assert await start.step() == state
    assert (tt.clock_hz, tt.ui_in.value, driven(tt)) == (clock, 1, list(range(8)))


async def test_a_failure_is_tried_again_later_or_when_the_board_comes_back(
    bridge, fake_repl, tmp_path, settled, monkeypatch
):
    chip_sdk_started(fake_repl)
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER))
    with monkeypatch.context() as m:
        m.setattr(type(fake_repl.tt), "clock_project_stop", lambda self: None)  # the clock keeps running
        assert (await start.step()).startswith("failed: the board did not take the safe state")
        said = bytes(fake_repl.transcript)
        assert (await start.step()).startswith("failed: ") and bytes(fake_repl.transcript) == said  # not at once
    chip_sdk_started(fake_repl)
    bridge.opens += 1  # the board came back: tried again now, not after FAILED_WAIT
    assert (await start.step()).startswith("set: ")


async def test_a_runner_that_is_busy_leaves_it_for_the_next_look(bridge, fake_repl, tmp_path, settled, monkeypatch):
    chip_sdk_started(fake_repl)
    app = an_app(bridge, tmp_path, lambda: IS_OTHER)

    async def busy(*args, **kwargs):
        raise ReplBusy("another task is running")

    start = safe.SafeStart(app)
    with monkeypatch.context() as m:
        m.setattr(app["repl"], "exec", busy)
        assert await start.step() == "waiting" and not app["taken"].taken
    assert (await start.step()).startswith("set: ")


@pytest.mark.parametrize(
    "reply",
    [
        None,
        [],
        {"sdk": "yes"},
        {},
        {"sdk": True, "enabled": "tt_um_factory_test", "mode": "ASIC_RP_CONTROL", "clock_was": 10},  # no pin lists
    ],
)
def test_a_reply_that_is_not_one_is_an_error(reply):
    with pytest.raises(ReplError):
        safe.outcome(reply)


async def test_the_health_endpoint_says_what_the_safe_start_did(bridge, fake_repl, tmp_path, aiohttp_client):
    app = an_app(bridge, tmp_path, lambda: IS_OTHER)
    app["safe"] = safe.SafeStart(app)
    client = await aiohttp_client(app)
    body = await (await client.get("/health")).json()
    assert body["safe_start"] == {"state": "waiting"}
