"""The safe state of a chip board (issue #181 of fpgas.online-test-designs): set once in RAM, nothing written."""

from __future__ import annotations

from pathlib import Path

import pytest

from fpgas_tt import idle, rpregs, safe
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


CHIPS = ["rp2040", "rp2350"]


@pytest.fixture(params=CHIPS)
def chip(request) -> str:
    return request.param


@pytest.fixture
async def fake_repl(fake_repl, chip):
    """Every test here that has a board runs with it as each chip. Only this file does: the shared fixture is
    an RP2350, and the other files do not look at the chip."""
    fake_repl.chip = chip
    return fake_repl


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
    return [k for k in range(8) if tt.ui_pin(k).hw_mode == PIN_OUT]


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
    high, so the factory test drives its counter onto uio, and so onto ui_in[1:3] through the HAT. It does that
    behind the SDK's back: the SDK's pins still say they are outputs (live run 4, 8 Oct 2026, 21:15)."""
    chip_sdk_started(fake_repl)
    tt = fake_repl.tt
    tt.released_by_the_boot_check()
    assert tt.ui_in.value == 0b0111  # as read on board de641070db746f27 on 8 Oct 2026
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER))
    assert await start.step() == SET.format(read=7, oe=0)
    assert (tt.ui_in.value, driven(tt)) == (0, list(range(8)))
    assert all(tt.ui_pin(k).hw_pull == PULL_DOWN for k in range(8))


async def test_a_dip_switch_that_is_on_is_not_driven_against_and_is_said(bridge, fake_repl, tmp_path, settled):
    chip_sdk_started(fake_repl)
    tt = fake_repl.tt
    tt.released_by_the_boot_check()
    tt.dip_on = {4}
    state = await safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER)).step()
    assert state.endswith("; ui_in[4] held high on the demo board (a DIP switch that is on?), left an input")
    assert tt.ui_pin(4).hw_mode == PIN_IN and driven(tt) == [0, 1, 2, 3, 5, 6, 7]


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


async def test_ui_in_is_never_driven_high_and_ui_in0_is_driven_first(bridge, fake_repl, tmp_path, settled):
    """Review 2: the output register is 0 before any ui_in pin is made an output (no high pulse onto the HAT), and
    ui_in[0] goes first (with it low the chip lets go of uio, and so of ui_in[1:3])."""
    chip_sdk_started(fake_repl)
    tt = fake_repl.tt
    tt.released_by_the_boot_check()
    assert (await safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER)).step()).startswith("set: ")
    assert tt.high_pulses == [] and tt.made_outputs == list(range(8))


async def test_a_retry_after_a_failure_finishes_what_the_failed_try_began(bridge, fake_repl, tmp_path, settled):
    """Review 2: the failed try stopped the clock itself, so the retry is not told it is "not the start state"."""
    chip_sdk_started(fake_repl)
    tt = fake_repl.tt
    tt.released_by_the_boot_check()
    tt.dip_on = {0}
    start = safe.SafeStart(an_app(bridge, tmp_path, lambda: IS_OTHER))
    assert (await start.step()).startswith("failed: the board did not take the safe state")
    assert tt.clock_hz == 0  # the failed try stopped it
    tt.dip_on = set()  # the switch is set off
    start._retry = (start._retry[0], 0.0)  # FAILED_WAIT has passed
    assert (await start.step()) == (
        "set: clock stopped (was 0 Hz; its pin released), ui_in driven to 0 (read 0), uio released (uio_oe_pico was 0)"
    )
    assert tt.ui_in.value == 0 and driven(tt) == list(range(8)) and start._retry is None


def test_only_a_retry_acts_on_a_factory_test_that_is_not_clocked():
    assert "and (_fo_r['clock_was'] or False)" in safe.safe_code(False)
    assert "and (_fo_r['clock_was'] or True)" in safe.safe_code(True)
    left = {"sdk": True, "enabled": "tt_um_factory_test", "mode": "ASIC_RP_CONTROL", "clock_was": 0}
    assert safe.outcome(left).startswith("left: the factory test is not being clocked")


GPIO_OE_LINE = "        _fo_oe = machine.mem32[0xd0000020]\n"
GPIO_OE_EXPR_LINE = (
    "        _fo_oe = machine.mem32[(0xd0000030 if (machine.mem32[0x40000000] >> 12) & 0xffff == 4"
    " else 0xd0000020)]\n"
)


class _Machine:
    def __init__(self, chip_id: int) -> None:
        self.mem32 = {0x40000000: chip_id}


@pytest.mark.parametrize(("part", "address"), [(2, 0xD0000020), (4, 0xD0000030)])
def test_gpio_oe_expr_by_chip(part, address):
    machine = _Machine((part << 12) | 0x927)
    assert eval(rpregs.GPIO_OE_EXPR, {"machine": machine}) == address  # noqa: S307 - the board-side expression


def test_safe_code_uses_the_expression():
    code = safe.safe_code(False)
    assert f"machine.mem32[{rpregs.GPIO_OE_EXPR}]" in code
    assert GPIO_OE_EXPR_LINE in code
    assert "import sys" not in code
    assert "mem32[0xd0000020]" not in code


def test_safe_code_structure_unchanged():
    before = (Path(__file__).parent / "data" / "safe_code_before_ag.txt").read_text().splitlines(keepends=True)
    after = safe.safe_code(False).splitlines(keepends=True)
    assert before.count(GPIO_OE_LINE) == 1
    assert [GPIO_OE_EXPR_LINE if line == GPIO_OE_LINE else line for line in before] == after


def test_an_rp2350_reply_whose_driven_pins_are_the_ones_made_outputs_is_set():
    reply = {
        "sdk": True, "enabled": idle.SDK_START_DESIGN, "mode": safe.SDK_MODE, "clock_was": 10,
        "held": [], "driven": list(range(8)), "clock": 0, "ui_in": 0, "uio_oe": 0,
        "ui_in_was": 1, "uio_oe_was": 0x0F,
    }  # fmt: skip
    assert safe.outcome(reply).startswith("set: ")
