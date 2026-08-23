import asyncio

import pytest

from fpgas_tt.bridge import Bridge
from fpgas_tt.repl import ReplBusy, ReplError, ReplNoBoard, ReplRunner


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
    with pytest.raises(ReplError) as ei:
        await runner.exec("print(1)", timeout=0.3)
    assert "timed out" in str(ei.value)


async def test_client_released_even_if_outer_wait_for_cancels_it(bridge, fake_board):
    # no FakeRepl running: nothing ever answers, so exec() is still waiting
    # inside the raw-REPL session when an outer wait_for's own timeout fires
    # and cancels it with something other than BoardNotPresent -- the client
    # must still be released, not leaked on the bridge.
    runner = ReplRunner(bridge)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(runner.exec("print(1)"), 0.2)
    assert bridge.clients == 0
