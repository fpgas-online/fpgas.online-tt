"""Test helpers: a pseudo-terminal stands in for the demo board's USB serial port.

``fake_board`` yields an object whose ``path`` the Bridge opens (via a symlink,
so tests can swap the underlying pty to simulate unplug/replug) and whose
``master`` fd is "the board": bytes written to it arrive at the Bridge, bytes
the Bridge writes can be read from it.
"""

from __future__ import annotations

import asyncio
import os
import termios
import tty
from dataclasses import dataclass, field
from pathlib import Path

import pytest


@dataclass
class FakeBoard:
    path: Path  # symlink the Bridge opens
    master: int
    slave: int
    _link_dir: Path = field(repr=False)

    async def send(self, data: bytes) -> None:
        """Board → bridge."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, os.write, self.master, data)

    async def recv(self, n: int = 4096, timeout: float = 2.0) -> bytes:
        """Bridge → board. Raises TimeoutError if nothing arrives."""
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.run_in_executor(None, os.read, self.master, n), timeout)

    async def recv_exactly(self, n: int, timeout: float = 2.0) -> bytes:
        buf = b""
        while len(buf) < n:
            buf += await self.recv(n - len(buf), timeout)
        return buf

    def unplug(self) -> None:
        """Close the master: the slave now returns EIO, like a yanked USB cable."""
        os.close(self.master)
        self.master = -1

    def replug(self) -> None:
        """Create a fresh pty and repoint the symlink at it (a new /dev/ttyACM0)."""
        master, slave = _open_raw_pty()
        self.master, self.slave = master, slave
        tmp = self._link_dir / "ttboard.new"
        os.symlink(os.ttyname(slave), tmp)
        os.replace(tmp, self.path)


def _open_raw_pty() -> tuple[int, int]:
    master, slave = os.openpty()
    tty.setraw(slave)
    tty.setraw(master)
    # Keep the slave open on our side too; pyserial opens its own fd by path.
    attrs = termios.tcgetattr(slave)
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    return master, slave


@pytest.fixture
def fake_board(tmp_path: Path) -> FakeBoard:
    master, slave = _open_raw_pty()
    link = tmp_path / "ttboard"
    os.symlink(os.ttyname(slave), link)
    board = FakeBoard(path=link, master=master, slave=slave, _link_dir=tmp_path)
    yield board
    for fd in (board.master, board.slave):
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
