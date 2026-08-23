import asyncio
import contextlib

import pytest

from fpgas_tt.bridge import Bridge
from fpgas_tt.repl import (
    DEFAULT_OVERALL_TIMEOUT,
    ReplBusy,
    ReplError,
    ReplNoBoard,
    ReplRunner,
    _default_overall,
    _Session,
)


async def wait_for(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture
async def bridge(fake_board):
    b = Bridge(str(fake_board.path), reopen_interval=0.05)
    await b.start()
    await wait_for(lambda: b.present)
    yield b
    await b.stop()


async def test_exec_returns_stdout_and_restores_friendly_repl(bridge, fake_repl):
    runner = ReplRunner(bridge)
    out = await runner.exec("print(1 + 1)")
    assert out == "2\r\n"
    # the session ends with Ctrl-B so viewers get their friendly REPL back --
    # the write is fire-and-forget from exec()'s point of view, so poll for it.
    await wait_for(lambda: fake_repl.transcript.endswith(b"\r\x02"))


async def test_exec_surfaces_board_exception_as_repl_error(bridge, fake_repl):
    runner = ReplRunner(bridge)
    with pytest.raises(ReplError) as ei:
        await runner.exec("raise ValueError('nope')")
    assert "ValueError: nope" in ei.value.detail
    await wait_for(lambda: fake_repl.transcript.endswith(b"\r\x02"))


async def test_exec_steps_runs_several_snippets_in_one_session(bridge, fake_repl):
    runner = ReplRunner(bridge)
    outs = await runner.exec_steps(["x = 40", "print(x + 2)"])
    assert outs == ["", "42\r\n"]
    assert fake_repl.transcript.count(b"\x01") == 1  # one raw-REPL entry


async def test_back_to_back_sessions_do_not_interfere(bridge, fake_repl):
    # A new session started the instant the previous one returns must not see
    # the outgoing session's "leaving raw REPL" bytes leak into its own read
    # stream (regression: the two clients raced for those bytes).
    runner = ReplRunner(bridge)
    assert await runner.exec("print(1)") == "1\r\n"
    assert await runner.exec("print(2)") == "2\r\n"
    assert await runner.exec("print(3)") == "3\r\n"


async def test_concurrent_tasks_are_refused_not_queued(bridge, fake_repl):
    runner = ReplRunner(bridge)
    first = asyncio.create_task(runner.exec("import time\nprint('slow')", timeout=5))
    await wait_for(lambda: runner.busy)
    with pytest.raises(ReplBusy):
        await runner.exec("print('second')")
    assert await first == "slow\r\n"


async def test_interference_fails_the_task_with_detail(bridge, fake_repl):
    fake_repl.echo_junk = b"someone typed this\r\n>>> "
    runner = ReplRunner(bridge)
    with pytest.raises(ReplError) as ei:
        await runner.exec("print(1)")
    assert "someone typed this" in ei.value.detail
    await wait_for(lambda: fake_repl.transcript.endswith(b"\r\x02"))


async def test_no_board(tmp_path):
    bridge = Bridge(str(tmp_path / "missing"), reopen_interval=0.05)
    await bridge.start()
    try:
        with pytest.raises(ReplNoBoard):
            await ReplRunner(bridge).exec("print(1)")
    finally:
        await bridge.stop()


async def test_timeout_when_board_is_silent(bridge, fake_board):
    # no FakeRepl running: nothing ever answers Ctrl-A
    runner = ReplRunner(bridge)
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(ReplError) as ei:
        await runner.exec("print(1)", timeout=0.3)
    assert "timed out" in str(ei.value)
    # enter() never confirmed raw mode, so cleanup must not also wait out
    # LEAVE_DRAIN_TIMEOUT for a friendly prompt that will never come.
    assert loop.time() - start < 0.8


async def test_client_released_even_if_outer_wait_for_cancels_it(bridge, fake_board):
    # no FakeRepl running: nothing ever answers, so exec() is still waiting
    # inside the raw-REPL session when an outer wait_for's own timeout fires
    # and cancels it with something other than BoardNotPresent -- the client
    # must still be released, not leaked on the bridge.
    runner = ReplRunner(bridge)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(runner.exec("print(1)"), 0.2)
    assert bridge.clients == 0


class _JunkClient:
    """A minimal Client stub that always echoes junk, never the raw-REPL
    banner -- used to test enter()'s preamble cap deterministically, without
    depending on how the real Bridge/pty happens to chunk reads."""

    dropped = False

    async def write(self, data: bytes) -> None:
        pass

    async def read(self) -> bytes:
        return b"x" * 100


async def test_enter_gives_up_after_too_much_pre_banner_preamble():
    # A real board echoes some preamble (readline/pyexec) before the actual
    # raw-REPL banner on entry (see test_exec_returns_stdout_and_restores_
    # friendly_repl and friends, which exercise that path via the fake's
    # default preamble) -- but a board that never sends the banner at all
    # must not make enter() buffer forever.
    session = _Session(_JunkClient(), timeout=5.0)
    with pytest.raises(ReplError) as ei:
        await session.enter()
    assert "raw REPL banner" in str(ei.value)


async def test_enter_scans_past_a_larger_than_usual_preamble(bridge, fake_repl):
    # The fake's default preamble already exercises the common case (every
    # other test in this file goes through it); this checks a longer one --
    # still under the cap -- is tolerated too, not just the exact default.
    fake_repl.raw_preamble = b"\r\n>>> \r\n" * 20
    runner = ReplRunner(bridge)
    assert await runner.exec("print(1)") == "1\r\n"


async def test_overall_deadline_fires_even_though_each_read_beats_its_own_timeout(bridge, fake_board):
    # A board that dribbles bytes just fast enough to keep beating the
    # per-read timeout must still be bounded by the overall session deadline.
    async def dribble():
        while True:
            await fake_board.send(b"x")
            await asyncio.sleep(0.05)

    task = asyncio.create_task(dribble())
    try:
        runner = ReplRunner(bridge)
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(ReplError) as ei:
            await runner.exec("print(1)", timeout=5.0, overall=0.3)
        assert "overall deadline" in str(ei.value)
        assert loop.time() - start < 2.0  # nowhere near the 5s per-read timeout
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_client_dropped_for_buffer_overrun_raises_replerror_not_noboard(bridge, fake_repl, monkeypatch):
    import fpgas_tt.bridge as bridge_module

    monkeypatch.setattr(bridge_module, "MAX_CLIENT_BUFFER", 8)  # tiny: any real reply overruns it
    runner = ReplRunner(bridge)
    with pytest.raises(ReplError) as ei:
        await runner.exec("print(1)")
    assert not isinstance(ei.value, ReplNoBoard)
    assert "overran the buffer" in str(ei.value)


def test_default_overall_scales_with_timeout_and_step_count():
    # enable_design's actual case: a single step at timeout=30 used to leave
    # entry and the SPI-load step sharing that same 30s overall budget.
    assert _default_overall(timeout=30.0, n_steps=1) == 60.0
    assert _default_overall(timeout=10.0, n_steps=3) == 40.0  # 10 * (3 + 1)
    # the floor still applies for small timeouts/step counts.
    assert _default_overall(timeout=1.0, n_steps=1) == DEFAULT_OVERALL_TIMEOUT
