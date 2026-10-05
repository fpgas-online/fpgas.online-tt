"""The idle display: an FPGA board nobody is using gets a moving design, and a visitor is never disturbed."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from fpgas_tt import designs, idle
from fpgas_tt.bridge import Bridge
from fpgas_tt.server import build_parser, create_app
from tests.test_designs import PRE, board_tree, wait_for
from tests.test_server import DEMOS, IS_FPGA, IS_OTHER, NOT_NAMED

MOVING = PRE + b"a design that animates the display from the FPGA's own oscillator" * 40


@pytest.fixture
async def bridge(fake_board):
    b = Bridge(str(fake_board.path), reopen_interval=0.05)
    await b.start()
    await wait_for(lambda: b.present)
    yield b
    await b.stop()


@pytest.fixture
def design(tmp_path) -> Path:
    path = tmp_path / "tt_fpga_platform.bin"
    path.write_bytes(MOVING)
    return path


def an_app(bridge, tmp_path, who=IS_FPGA):
    return create_app(bridge, identify=lambda: who, demos_dir=DEMOS, uploads_dir=tmp_path / "pi-uploads")


def quiet_for(app, seconds: float) -> None:
    """As if nobody had used the board for `seconds`."""
    app["activity"].last -= seconds


def sdk_started(fake_repl) -> None:
    """The board as SDK 3.1.0 leaves it at every start: its default project loaded from its own file."""
    fake_repl.tt.shuttle.enabled = type("BitStream", (), {"name": idle.SDK_START_DESIGN})()


@pytest.mark.parametrize(
    "state,quiet,replace_after,expected",
    [
        ({"sdk": True, "enabled": "tt_um_factory_test"}, 60, None, True),
        ({"sdk": True, "enabled": "tt_um_factory_test"}, 59, None, False),
        ({"sdk": True, "enabled": None}, 60, None, True),  # the SDK runs and nothing is loaded
        ({"sdk": False, "enabled": None}, 9999, 1800, False),  # as the boot check leaves it: its design runs
        ({"sdk": True, "enabled": "idle_display"}, 9999, 1800, False),  # already there
        ({"sdk": True, "enabled": "my_upload"}, 9999, None, False),  # a visitor's design: never, unless asked
        ({"sdk": True, "enabled": "my_upload"}, 1800, 1800, True),
        ({"sdk": True, "enabled": "my_upload"}, 1799, 1800, False),
    ],
)
def test_which_boards_get_the_idle_design(state, quiet, replace_after, expected):
    assert idle.wanted(state, quiet, 60, replace_after) is expected


async def test_a_board_left_in_the_sdks_start_state_gets_the_idle_design_and_nothing_is_written_to_it(
    bridge, fake_repl, tmp_path, design
):
    sdk_started(fake_repl)
    before = board_tree(fake_repl)
    app = an_app(bridge, tmp_path)
    show = idle.IdleDisplay(app, design=design, after=60)
    assert await show.step() == "waiting" and fake_repl.transcript == b""  # not yet: the board is not even asked
    quiet_for(app, 60)
    assert await show.step() == "loaded"
    assert fake_repl.loaded == [("pi:idle_display.bin", MOVING)]  # the Pi's file, through the SDK's loader
    assert fake_repl.tt.shuttle.enabled.name == designs.IDLE_NAME
    assert fake_repl.tt.clock_log == []  # no clock asked for: the design has its own
    assert fake_repl.soft_resets == 0
    assert board_tree(fake_repl) == before
    assert show.health() == {"design": str(design), "state": "loaded"}


async def test_the_board_is_asked_once_per_quiet_time_and_again_after_somebody_used_it(
    bridge, fake_repl, tmp_path, design
):
    sdk_started(fake_repl)
    app = an_app(bridge, tmp_path)
    show = idle.IdleDisplay(app, design=design, after=60)
    quiet_for(app, 60)
    await show.step()
    said = bytes(fake_repl.transcript)
    for _ in range(3):
        assert await show.step() == "loaded"
    assert bytes(fake_repl.transcript) == said  # nothing more typed at the board
    sdk_started(fake_repl)  # a Commander came, soft-reset the board, and left
    app["activity"].touch()
    assert await show.step() == "waiting"
    quiet_for(app, 60)
    assert await show.step() == "loaded" and len(fake_repl.loaded) == 2


async def test_nothing_is_typed_while_a_client_is_connected_or_a_task_runs(bridge, fake_repl, tmp_path, design):
    sdk_started(fake_repl)
    app = an_app(bridge, tmp_path)
    show = idle.IdleDisplay(app, design=design, after=60)
    quiet_for(app, 3600)
    app["websockets"].add(object())  # a Commander, or a terminal
    assert await show.step() == "in use"
    app["websockets"].clear()
    async with app["repl"]._lock:  # a Run in progress
        assert await show.step() == "in use"
    assert fake_repl.transcript == b"" and fake_repl.loaded == []


async def test_a_client_that_connects_while_the_board_is_asked_keeps_the_board(
    bridge, fake_repl, tmp_path, design, monkeypatch
):
    sdk_started(fake_repl)
    app = an_app(bridge, tmp_path)
    show = idle.IdleDisplay(app, design=design, after=60)
    quiet_for(app, 60)
    ask = app["repl"].exec

    async def ask_while_somebody_connects(code, **kwargs):
        out = await ask(code, **kwargs)
        app["websockets"].add(object())
        return out

    monkeypatch.setattr(app["repl"], "exec", ask_while_somebody_connects)
    assert await show.step() == "in use" and fake_repl.loaded == []
    assert not app["taken"].taken  # and the board is given up at once


async def test_a_client_that_connects_while_the_file_is_read_finds_nothing_typed_at_the_board(
    bridge, fake_repl, tmp_path, design, monkeypatch
):
    sdk_started(fake_repl)
    app = an_app(bridge, tmp_path)
    show = idle.IdleDisplay(app, design=design, after=60)
    quiet_for(app, 60)
    read = show._read

    def read_while_somebody_connects():
        app["websockets"].add(object())
        return read()

    monkeypatch.setattr(show, "_read", read_while_somebody_connects)
    assert await show.step() == "in use"
    assert fake_repl.transcript == b""  # not a byte


async def test_a_client_that_connects_while_the_idle_design_is_streamed_is_held_and_sees_none_of_it(
    aiohttp_client, bridge, fake_repl, tmp_path, design, monkeypatch
):
    sdk_started(fake_repl)
    app = an_app(bridge, tmp_path)
    app["idle"] = show = idle.IdleDisplay(app, design=design, after=60)
    c = await aiohttp_client(app)
    quiet_for(app, 60)
    load = designs.load_design
    arrived: list = []

    async def load_while_somebody_connects(*args, **kwargs):
        arrived.append(asyncio.ensure_future(c.ws_connect("/serial")))
        await wait_for(lambda: app["websockets"])  # the server has the socket, and holds it
        assert bridge.clients == 0  # not bridged to the board
        await load(*args, **kwargs)

    monkeypatch.setattr(designs, "load_design", load_while_somebody_connects)
    assert await show.step() == "loaded"
    ws = await arrived[0]
    assert (await ws.receive_json())["event"] == "board"
    await wait_for(lambda: bridge.clients == 1)  # bridged now
    assert len(fake_repl.transcript) > len(MOVING)  # the load did go over the wire
    with pytest.raises(asyncio.TimeoutError):  # and not a byte of it, nor of the board's replies, came here
        await ws.receive(timeout=0.3)
    await ws.close()


async def test_a_run_that_arrives_while_the_idle_design_is_streamed_waits_and_then_runs(
    aiohttp_client, bridge, fake_repl, tmp_path, design, monkeypatch
):
    sdk_started(fake_repl)
    app = an_app(bridge, tmp_path)
    app["idle"] = show = idle.IdleDisplay(app, design=design, after=60)
    c = await aiohttp_client(app)
    quiet_for(app, 60)
    load = designs.load_design
    runs: list = []

    async def load_while_somebody_runs(*args, **kwargs):
        if not runs:
            runs.append(asyncio.ensure_future(c.post("/designs/tt_um_demo_a/enable")))
            await asyncio.sleep(0.05)
        await load(*args, **kwargs)

    monkeypatch.setattr(designs, "load_design", load_while_somebody_runs)
    assert await show.step() == "loaded"
    assert (await runs[0]).status == 200  # not "409 another task is running"
    assert [name for name, _ in fake_repl.loaded] == ["pi:idle_display.bin", "pi:tt_um_demo_a.bin"]


async def test_a_board_that_comes_back_starts_a_new_quiet_time_and_gets_the_idle_design_again(
    bridge, fake_board, fake_repl, tmp_path, design
):
    sdk_started(fake_repl)
    app = an_app(bridge, tmp_path)
    show = idle.IdleDisplay(app, design=design, after=60)
    quiet_for(app, 60)
    assert await show.step() == "loaded"
    bridge.opens += 1  # unplugged and plugged in again, or reset: it starts its SDK, with the still display
    sdk_started(fake_repl)
    assert await show.step() == "waiting"  # a minute for the board to start
    quiet_for(app, 60)
    assert await show.step() == "loaded" and len(fake_repl.loaded) == 2


async def test_a_board_that_is_not_there_is_said_to_be_not_there(bridge, fake_repl, tmp_path, design):
    app = an_app(bridge, tmp_path)
    show = idle.IdleDisplay(app, design=design, after=60)
    quiet_for(app, 60)
    bridge.present = False
    assert await show.step() == "board not present"


async def test_a_name_a_visitor_made_up_at_the_repl_is_not_repeated_in_health_or_the_log(
    bridge, fake_repl, tmp_path, design, caplog
):
    fake_repl.tt.shuttle.enabled = type("BitStream", (), {"name": "<script>alert(1)</script>\n" * 50})()
    app = an_app(bridge, tmp_path)
    show = idle.IdleDisplay(app, design=design, after=60)
    quiet_for(app, 60)
    with caplog.at_level(logging.INFO, logger="fpgas_tt.idle"):
        assert await show.step() == "left: a design"
    assert "script" not in caplog.text


@pytest.mark.parametrize(
    "after,replace_after",
    [(0, None), (-1, None), (float("nan"), None), (float("inf"), None), (60, 30), (60, float("nan"))],
)
def test_quiet_times_that_make_no_sense_are_refused(after, replace_after):
    with pytest.raises(ValueError):
        idle.IdleDisplay({"bridge": type("B", (), {"opens": 0})()}, after=after, replace_after=replace_after)


async def test_a_board_without_the_sdk_is_left_as_the_boot_check_left_it(bridge, fake_repl, tmp_path, design):
    del fake_repl._globals["tt"]  # as after the boot check's raw-REPL soft reset: its moving design is running
    app = an_app(bridge, tmp_path)
    show = idle.IdleDisplay(app, design=design, after=60, replace_after=1800)
    quiet_for(app, 7200)
    assert await show.step() == "left: the SDK is not running"
    assert fake_repl.soft_resets == 0 and fake_repl.loaded == []


async def test_a_visitors_design_is_left_unless_replacing_it_was_asked_for(bridge, fake_repl, tmp_path, design):
    app = an_app(bridge, tmp_path)
    await designs.enable_design(app["repl"], "tt_um_demo_a", None, DEMOS, tmp_path / "pi-uploads")
    show = idle.IdleDisplay(app, design=design, after=60)
    quiet_for(app, 86400)
    assert await show.step() == "left: tt_um_demo_a"
    assert [name for name, _ in fake_repl.loaded] == ["pi:tt_um_demo_a.bin"]

    asked = idle.IdleDisplay(app, design=design, after=60, replace_after=86400)
    app["activity"].touch()
    quiet_for(app, 3600)
    assert await asked.step() == "left: tt_um_demo_a"  # an hour is not a day
    quiet_for(app, 86400)
    assert await asked.step() == "loaded"
    assert [name for name, _ in fake_repl.loaded] == ["pi:tt_um_demo_a.bin", "pi:idle_display.bin"]


@pytest.mark.parametrize("who,state", [(IS_OTHER, "not an fpga board"), (NOT_NAMED, "not an fpga board")])
async def test_a_board_that_is_not_known_to_be_an_fpga_board_is_never_touched(
    bridge, fake_repl, tmp_path, design, who, state
):
    sdk_started(fake_repl)
    app = an_app(bridge, tmp_path, who)
    show = idle.IdleDisplay(app, design=design, after=60)
    quiet_for(app, 3600)
    assert await show.step() == state and fake_repl.transcript == b""


async def test_a_missing_or_wrong_file_is_said_once_in_the_log_and_in_health_and_the_board_is_not_asked(
    bridge, fake_repl, tmp_path, caplog
):
    sdk_started(fake_repl)
    app = an_app(bridge, tmp_path)
    show = idle.IdleDisplay(app, design=tmp_path / "not-in-this-root.bin", after=60)
    quiet_for(app, 60)
    with caplog.at_level(logging.WARNING, logger="fpgas_tt.idle"):
        for _ in range(3):
            assert await show.step() == "file missing"
    assert len([r for r in caplog.records if "file missing" in r.getMessage()]) == 1
    assert fake_repl.transcript == b""

    show.design.write_bytes(b"not a bitstream")
    assert await show.step() == "file is not an iCE40 bitstream" and fake_repl.transcript == b""
    show.design.write_bytes(MOVING)  # a root update brought the file: no restart needed
    assert await show.step() == "loaded"


async def test_a_load_that_fails_is_said_and_not_tried_again_until_somebody_has_used_the_board(
    bridge, fake_repl, tmp_path, design, caplog
):
    sdk_started(fake_repl)
    app = an_app(bridge, tmp_path)
    show = idle.IdleDisplay(app, design=design, after=60)
    quiet_for(app, 60)
    fake_repl.tt.shuttle.enable = None  # the SDK's loader is broken
    with caplog.at_level(logging.WARNING, logger="fpgas_tt.idle"):
        assert (await show.step()).startswith("failed: ")
    said = bytes(fake_repl.transcript)
    quiet_for(app, 60)  # the usual quiet time is not enough after a failure
    assert (await show.step()).startswith("failed: ") and bytes(fake_repl.transcript) == said
    del fake_repl.tt.shuttle.enable  # the loader works again
    quiet_for(app, idle.FAILED_WAIT)
    assert await show.step() == "loaded"


async def test_health_says_what_the_idle_display_did_and_a_run_or_a_client_starts_the_quiet_time_again(
    aiohttp_client, bridge, fake_repl, tmp_path, design
):
    app = an_app(bridge, tmp_path)
    app["idle"] = idle.IdleDisplay(app, design=design, after=60)
    c = await aiohttp_client(app)
    quiet_for(app, 3600)
    assert (await c.post("/designs/tt_um_demo_a/enable")).status == 200
    assert await app["idle"].step() == "waiting"  # the Run was a moment ago
    quiet_for(app, 3600)
    ws = await c.ws_connect("/serial")
    await ws.receive_json()
    assert await app["idle"].step() == "in use"
    await ws.close()
    await wait_for(lambda: not app["websockets"])
    assert await app["idle"].step() == "waiting"  # the client left a moment ago
    assert (await (await c.get("/health")).json())["idle_display"] == {"design": str(design), "state": "waiting"}


async def test_health_of_a_daemon_that_runs_no_idle_display_says_none(aiohttp_client, bridge, tmp_path):
    c = await aiohttp_client(an_app(bridge, tmp_path))
    assert (await (await c.get("/health")).json())["idle_display"] is None


async def test_an_upload_may_not_take_the_idle_designs_name(aiohttp_client, bridge, fake_repl, tmp_path):
    with pytest.raises(designs.ValidationError) as refused:
        designs.validate_bitstream(designs.IDLE_NAME, PRE, set())
    assert refused.value.status == 409


async def test_the_task_runs_steps_and_survives_a_bug(bridge, fake_repl, tmp_path, design, monkeypatch):
    sdk_started(fake_repl)
    app = an_app(bridge, tmp_path)
    show = idle.IdleDisplay(app, design=design, after=0.01)
    monkeypatch.setattr(idle, "POLL", 0.01)
    calls = []
    step = show.step

    async def step_with_a_bug_first():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("a bug")
        return await step()

    monkeypatch.setattr(show, "step", step_with_a_bug_first)
    await show.start()
    await wait_for(lambda: show.state == "loaded")
    await show.stop()
    await asyncio.sleep(0.05)
    assert show._task is None


def test_the_daemons_defaults_are_the_boot_checks_file_a_minute_and_no_replacing_of_a_visitors_design():
    args = build_parser().parse_args([])
    assert args.idle_design == str(idle.IDLE_DESIGN_DEFAULT)
    assert str(idle.IDLE_DESIGN_DEFAULT).endswith("/tt-display-tt-fpga/tt_fpga_platform.bin")
    assert args.idle_after == 60 and args.idle_replace_after is None
