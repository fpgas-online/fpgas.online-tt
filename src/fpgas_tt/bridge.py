"""The one and only owner of the demo board's serial port.

One reader task fans every chunk out to all subscribed clients; one writer
accepts bytes from any client. There is deliberately no locking or
arbitration: WebSocket viewers and (in later phases) internal tasks such as
bitstream upload are all just clients of this bridge.

Back-pressure policy: a client whose unread bytes exceed ``MAX_CLIENT_BUFFER``
is dropped (``Client.dropped = True``, ``read()`` returns ``None``). The serial
reader is never blocked by a slow consumer.

Loss policy: if the serial device disappears, every client is closed
(``read()`` returns ``None``) and the bridge retries opening the device every
``reopen_interval`` seconds forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import serial
import serial_asyncio

log = logging.getLogger(__name__)

MAX_CLIENT_BUFFER = 256 * 1024
READ_CHUNK = 4096


class BoardNotPresent(RuntimeError):
    """Raised by ``Bridge.write`` when no serial device is open."""


def _in_waiting(writer: asyncio.StreamWriter) -> int:
    """Bytes still sitting in the port's OS receive buffer, or 0 if unknown."""
    try:
        return writer.transport.serial.in_waiting
    except (OSError, AttributeError):
        return 0


class Client:
    """A subscriber of the bridge. Obtain via ``Bridge.subscribe()``."""

    def __init__(self, bridge: "Bridge") -> None:
        self._bridge = bridge
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.buffered = 0
        self.dropped = False
        self.closed = False

    async def read(self) -> bytes | None:
        """Next available bytes from the board, or ``None`` once this client
        is closed.

        Like any byte stream, chunk boundaries are not preserved: the serial
        reader may deliver a single board write as several small internal
        reads, so this coalesces everything already queued at the time of
        the call into one return value instead of handing back one physical
        read at a time.
        """
        item = await self._queue.get()
        if item is None:
            self.closed = True
            return None
        self.buffered -= len(item)
        chunks = [item]
        while True:
            try:
                nxt = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if nxt is None:
                self._queue.put_nowait(None)  # leave the sentinel for next read()
                break
            self.buffered -= len(nxt)
            chunks.append(nxt)
        return chunks[0] if len(chunks) == 1 else b"".join(chunks)

    async def write(self, data: bytes) -> None:
        await self._bridge.write(data)

    def close(self) -> None:
        self._bridge._unsubscribe(self)

    # -- internal, called by Bridge --
    def _push(self, data: bytes) -> bool:
        if self.buffered + len(data) > MAX_CLIENT_BUFFER:
            return False
        self.buffered += len(data)
        self._queue.put_nowait(data)
        return True

    def _end(self) -> None:
        if not self.closed:
            # Discard any unread data still queued: a closed/dropped client
            # must observe read() -> None immediately, not drain stale bytes
            # first.
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(None)


class Bridge:
    def __init__(self, device: str, *, baudrate: int = 115200, reopen_interval: float = 1.0) -> None:
        self.device = device
        self.baudrate = baudrate
        self.reopen_interval = reopen_interval
        self._clients: set[Client] = set()
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self.present = False

    # -- lifecycle --
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fpgas-tt-bridge")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._close_clients()

    # -- client API --
    @property
    def clients(self) -> int:
        return len(self._clients)

    def subscribe(self) -> Client:
        client = Client(self)
        self._clients.add(client)
        return client

    def _unsubscribe(self, client: Client) -> None:
        if client in self._clients:
            self._clients.discard(client)
            client._end()

    async def write(self, data: bytes) -> None:
        writer = self._writer
        if writer is None:
            raise BoardNotPresent(self.device)
        writer.write(data)
        await writer.drain()

    # -- internals --
    async def _run(self) -> None:
        while True:
            try:
                reader, writer = await serial_asyncio.open_serial_connection(url=self.device, baudrate=self.baudrate)
            except (OSError, serial.SerialException) as exc:
                log.debug("bridge: cannot open %s: %s", self.device, exc)
                await asyncio.sleep(self.reopen_interval)
                continue

            self._writer = writer
            self.present = True
            log.info("bridge: opened %s at %d baud", self.device, self.baudrate)
            try:
                while True:
                    data = await reader.read(READ_CHUNK)
                    if not data:
                        log.warning("bridge: EOF on %s", self.device)
                        break
                    # The OS may deliver one board write as several small
                    # physical reads. Drain everything already sitting in
                    # the port's receive buffer before fanning out, so a
                    # burst reaches clients as one chunk instead of many.
                    chunks = [data]
                    while _in_waiting(writer) > 0:
                        more = await reader.read(READ_CHUNK)
                        if not more:
                            break
                        chunks.append(more)
                    self._fanout(chunks[0] if len(chunks) == 1 else b"".join(chunks))
            except (OSError, serial.SerialException) as exc:
                log.warning("bridge: lost %s: %s", self.device, exc)
            finally:
                self.present = False
                self._writer = None
                writer.close()
                self._close_clients()
            await asyncio.sleep(self.reopen_interval)

    def _fanout(self, data: bytes) -> None:
        for client in list(self._clients):
            if not client._push(data):
                log.warning("bridge: dropping slow client (%d bytes buffered)", client.buffered)
                client.dropped = True
                self._unsubscribe(client)

    def _close_clients(self) -> None:
        for client in list(self._clients):
            self._unsubscribe(client)
