"""Relay: llama.cpp RPC between pipeline members, through the coordinator, over WebSockets.

Lets devices on the public internet work together without a private network or open ports:
every agent only makes outbound connections, and each worker's RPC server listens on 127.0.0.1.

  head llama-server ─TCP─▶ head agent proxy ─WSS─▶ coordinator ─WSS─▶ worker agent ─TCP─▶ rpc-server
                       (127.0.0.1)              /relay/head/...      /relay/worker/...  (127.0.0.1)

Each TCP connection the head's llama-server opens becomes one relayed stream:
1. the head agent connects to `/relay/head/{pipeline}/{worker}`; only the head of that live
   pipeline may, and only to one of its members
2. the coordinator asks the worker agent (`open_stream` command) to connect to
   `/relay/worker/{stream}`; only that worker may
3. bytes are piped both ways until either side closes, or the pipeline stops or breaks
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

log = logging.getLogger(__name__)
ATTACH_TIMEOUT = 15.0
LIVE = ('starting', 'loading', 'active', 'draining')


@dataclass
class Stream:
    id: str
    pipeline_id: str
    head_id: str
    worker_id: str
    worker_ws: asyncio.Future
    done: asyncio.Event = field(default_factory=asyncio.Event)
    head_ws: WebSocket | None = None
    bytes_up: int = 0      # head -> worker
    bytes_down: int = 0    # worker -> head


class Relay:
    def __init__(self, bus, mgr, node_from_token):
        self.bus = bus
        self.mgr = mgr
        self.node_from_token = node_from_token   # token -> node row or None
        self.streams: dict[str, Stream] = {}
        self.bytes_by_pipeline: dict[str, int] = {}

    def _auth(self, ws: WebSocket):
        auth = ws.headers.get('authorization', '')
        token = auth[7:].strip() if auth.lower().startswith('bearer ') else ws.query_params.get('token', '')
        return self.node_from_token(token) if token else None

    async def head(self, ws: WebSocket, pipeline_id: str, worker_id: str) -> None:
        node = self._auth(ws)
        rt = self.mgr.runtimes.get(pipeline_id)
        if node is None:
            await ws.close(code=4401, reason='unknown node token')
            return
        if rt is None or rt.state not in LIVE or rt.head.node_id != node['id'] or worker_id not in rt.node_ids \
                or worker_id == node['id']:
            await ws.close(code=4403, reason='not the head of this pipeline, or not one of its workers')
            return
        await ws.accept()
        sid = 's-' + uuid.uuid4().hex[:12]
        stream = Stream(sid, pipeline_id, node['id'], worker_id, asyncio.get_running_loop().create_future(), head_ws=ws)
        self.streams[sid] = stream
        try:
            self.bus.post(worker_id, 'open_stream', {'stream_id': sid, 'pipeline_id': pipeline_id})
            try:
                worker_ws = await asyncio.wait_for(stream.worker_ws, ATTACH_TIMEOUT)
            except asyncio.TimeoutError:
                await ws.close(code=4408, reason='worker did not attach')
                return
            await self._pipe(stream, ws, worker_ws)
        finally:
            stream.done.set()
            self.streams.pop(sid, None)

    async def worker(self, ws: WebSocket, stream_id: str) -> None:
        node = self._auth(ws)
        stream = self.streams.get(stream_id)
        if node is None or stream is None or stream.worker_id != node['id'] or stream.worker_ws.done():
            await ws.close(code=4403, reason='unknown stream or not its worker')
            return
        await ws.accept()
        stream.worker_ws.set_result(ws)
        await stream.done.wait()  # the head side owns the pipe; keep this socket open until it ends

    async def _pipe(self, stream: Stream, head_ws: WebSocket, worker_ws: WebSocket) -> None:
        async def forward(src: WebSocket, dst: WebSocket, up: bool):
            while True:
                msg = await src.receive()
                if msg['type'] == 'websocket.disconnect':
                    return
                data = msg.get('bytes')
                if data is None:
                    continue
                await dst.send_bytes(data)
                if up:
                    stream.bytes_up += len(data)
                else:
                    stream.bytes_down += len(data)
                self.bytes_by_pipeline[stream.pipeline_id] = self.bytes_by_pipeline.get(stream.pipeline_id, 0) + len(data)

        tasks = [asyncio.create_task(forward(head_ws, worker_ws, True)),
                 asyncio.create_task(forward(worker_ws, head_ws, False))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError, Exception):
                    await t
            for s in (head_ws, worker_ws):
                with contextlib.suppress(Exception):
                    await s.close()
            log.info('relay stream %s (%s -> %s) closed: %.1f MB up, %.1f MB down', stream.id, stream.head_id,
                     stream.worker_id, stream.bytes_up / 1e6, stream.bytes_down / 1e6)

    async def close_pipeline(self, pipeline_id: str) -> None:
        """Pipeline stopped or broke: cut its relayed RPC connections."""
        for s in [s for s in self.streams.values() if s.pipeline_id == pipeline_id]:
            if s.head_ws is not None:
                with contextlib.suppress(Exception):
                    await s.head_ws.close(code=4410, reason='pipeline ended')
            if s.worker_ws.done():
                with contextlib.suppress(Exception):
                    await s.worker_ws.result().close(code=4410, reason='pipeline ended')
            s.done.set()
