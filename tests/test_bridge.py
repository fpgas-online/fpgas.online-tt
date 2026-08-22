import asyncio

import pytest

from fpgas_tt.bridge import MAX_CLIENT_BUFFER, BoardNotPresent, Bridge


async def wait_for(predicate, timeout=2.0, interval=0.01):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


@pytest.fixture
async def bridge(fake_board):
    b = Bridge(str(fake_board.path), reopen_interval=0.05)
    await b.start()
    await wait_for(lambda: b.present)
    yield b
    await b.stop()


async def test_board_to_client(fake_board, bridge):
    client = bridge.subscribe()
    await fake_board.send(b"hello\r\n")
    assert await asyncio.wait_for(client.read(), 2) == b"hello\r\n"
    client.close()


async def test_client_to_board(fake_board, bridge):
    client = bridge.subscribe()
    await client.write(b"print(1)\r\n")
    assert await fake_board.recv_exactly(len(b"print(1)\r\n")) == b"print(1)\r\n"
    client.close()


async def test_fanout_to_all_clients(fake_board, bridge):
    a, b = bridge.subscribe(), bridge.subscribe()
    assert bridge.clients == 2
    await fake_board.send(b"x")
    assert await asyncio.wait_for(a.read(), 2) == b"x"
    assert await asyncio.wait_for(b.read(), 2) == b"x"
    a.close()
    b.close()
    assert bridge.clients == 0


async def test_slow_client_is_dropped_not_reader(fake_board, bridge):
    slow = bridge.subscribe()
    fast = bridge.subscribe()
    chunk = b"A" * 4096
    for _ in range(MAX_CLIENT_BUFFER // len(chunk) + 2):
        await fake_board.send(chunk)
        # the fast client keeps draining; the slow one never reads
        await asyncio.wait_for(fast.read(), 2)
    await wait_for(lambda: slow.dropped)
    assert await asyncio.wait_for(slow.read(), 2) is None  # closed
    assert bridge.clients == 1  # fast client still attached
    await fake_board.send(b"still alive")
    assert await asyncio.wait_for(fast.read(), 2) == b"still alive"
    fast.close()


async def test_write_without_board_raises(fake_board):
    b = Bridge(str(fake_board.path), reopen_interval=0.05)
    # not started: no board
    with pytest.raises(BoardNotPresent):
        await b.write(b"x")


async def test_unplug_closes_clients_and_replug_recovers(fake_board, bridge):
    client = bridge.subscribe()
    fake_board.unplug()
    await wait_for(lambda: not bridge.present)
    assert await asyncio.wait_for(client.read(), 2) is None  # closed on loss
    assert client.dropped is False
    assert bridge.clients == 0

    fake_board.replug()
    await wait_for(lambda: bridge.present)
    client2 = bridge.subscribe()
    await fake_board.send(b"back")
    assert await asyncio.wait_for(client2.read(), 2) == b"back"
    client2.close()


async def test_write_after_unplug_raises_board_not_present(fake_board, bridge):
    client = bridge.subscribe()
    fake_board.unplug()
    await wait_for(lambda: not bridge.present)
    with pytest.raises(BoardNotPresent):
        await bridge.write(b"x")
    with pytest.raises(BoardNotPresent):
        await client.write(b"x")
    client.close()


async def test_write_during_closing_window_raises_board_not_present(fake_board, bridge):
    # Between the transport detecting a port error and the read loop's
    # `finally` clearing `present`, `self._writer` is still set but
    # `writer.is_closing()` is already True. Reproducing that race against
    # the real pty is non-deterministic (it depends on exact event-loop
    # scheduling between the transport's read callback and our read loop's
    # exception handling), so this drives the exact branch directly instead.
    class ClosingWriter:
        def is_closing(self):
            return True

    bridge._writer = ClosingWriter()
    with pytest.raises(BoardNotPresent):
        await bridge.write(b"x")


async def test_start_without_device_keeps_retrying(tmp_path):
    b = Bridge(str(tmp_path / "missing"), reopen_interval=0.05)
    await b.start()
    await asyncio.sleep(0.2)
    assert b.present is False
    await b.stop()  # must stop cleanly while retrying
