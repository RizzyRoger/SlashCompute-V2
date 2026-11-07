"""Agent side of the relay (see coordinator/relay.py).

- HeadProxy: a 127.0.0.1 listener per worker. llama-server's `--rpc` points at it, and every
  connection it accepts is tunnelled to that worker through the coordinator.
- bridge_stream: on the worker, connects a relayed stream to the local rpc-server.
All connections are outbound from the agent; nothing listens on a public interface.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)
CHUNK = 1 << 20


def ws_url(coordinator_url: str, path: str) -> str:
    base = coordinator_url.rstrip('/')
    if base.startswith('https://'):
        base = 'wss://' + base[len('https://'):]
    elif base.startswith('http://'):
        base = 'ws://' + base[len('http://'):]
    return base + path


def _connect(url: str, token: str):
    return connect(url, additional_headers={'Authorization': f'Bearer {token}'}, max_size=None,
                   compression=None, ping_interval=20, ping_timeout=60, open_timeout=20)


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ws) -> tuple[int, int]:
    """Copy bytes TCP <-> WebSocket until either side closes. Returns (sent, received)."""
    counts = [0, 0]

    async def tcp_to_ws():
        while chunk := await reader.read(CHUNK):
            await ws.send(chunk)
            counts[0] += len(chunk)

    async def ws_to_tcp():
        async for msg in ws:
            if isinstance(msg, str):
                continue
            writer.write(msg)
            await writer.drain()
            counts[1] += len(msg)

    tasks = [asyncio.create_task(tcp_to_ws()), asyncio.create_task(ws_to_tcp())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, ConnectionClosed, ConnectionError, Exception):
                await t
        with contextlib.suppress(Exception):
            await ws.close()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    return counts[0], counts[1]


class HeadProxy:
    """Local endpoint for one worker of a pipeline, as seen by the head's llama-server."""

    def __init__(self, coordinator_url: str, token: str, pipeline_id: str, worker_id: str):
        self.url = ws_url(coordinator_url, f'/relay/head/{pipeline_id}/{worker_id}')
        self.token = token
        self.worker_id = worker_id
        self.server: asyncio.base_events.Server | None = None
        self.port = 0
        self._conns: set[asyncio.Task] = set()

    async def start(self) -> str:
        self.server = await asyncio.start_server(self._on_conn, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return f'127.0.0.1:{self.port}'

    async def _on_conn(self, reader, writer) -> None:
        task = asyncio.current_task()
        self._conns.add(task)
        try:
            async with _connect(self.url, self.token) as ws:
                await pipe(reader, writer, ws)
        except Exception as e:  # noqa: BLE001 - llama-server sees a closed socket, which it reports
            log.warning('relay to worker %s failed: %s', self.worker_id, e)
            writer.close()
        finally:
            self._conns.discard(task)

    async def close(self) -> None:
        if self.server:
            self.server.close()
        for t in list(self._conns):
            t.cancel()


async def bridge_stream(coordinator_url: str, token: str, stream_id: str, local_endpoint: str) -> tuple[int, int]:
    """Worker side: attach the relayed stream to the local rpc-server."""
    host, port = local_endpoint.rsplit(':', 1)
    reader, writer = await asyncio.open_connection(host, int(port))
    try:
        async with _connect(ws_url(coordinator_url, f'/relay/worker/{stream_id}'), token) as ws:
            return await pipe(reader, writer, ws)
    except Exception:
        writer.close()
        raise
