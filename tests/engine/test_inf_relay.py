"""Relay transport: llama.cpp RPC tunnelled through the coordinator over WebSockets."""
import asyncio
import os

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from inf_harness import FakeNode, chat, fast_settings, start_harness
from compute.engine.coordinator.layers import synthetic_layout
from compute.engine.node.fake_engine import FakeEngine
from compute.engine.node.relay import ws_url

GB = 10 ** 9
QWEN = 'Qwen3.8-27B-UD-Q4_K_XL.gguf'


class EchoEngine(FakeEngine):
    """FakeEngine whose 'RPC server' is a real local TCP echo server."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.servers = {}

    async def start_worker(self, pipeline_id, spec):
        async def echo(reader, writer):
            while data := await reader.read(1 << 16):
                writer.write(data)
                await writer.drain()
            writer.close()
        server = await asyncio.start_server(echo, '127.0.0.1', 0)
        self.servers[pipeline_id] = server
        return {'endpoint': f'127.0.0.1:{server.sockets[0].getsockname()[1]}'}

    async def stop_worker(self, pipeline_id):
        server = self.servers.pop(pipeline_id, None)
        if server:
            server.close()


@pytest.fixture
async def relay_cluster():
    h = await start_harness(fast_settings(TRANSPORT='relay'))
    h.add_model(synthetic_layout(QWEN, n_layers=64, total_bytes=int(17.56 * GB), head_bytes=1 * GB))
    await h.add_nodes([FakeNode('head', 16, may_be_head=True, files=(QWEN,)), FakeNode('worker', 16),
                       FakeNode('outsider', 16)], engine_cls=EchoEngine)
    yield h
    await h.stop()


async def test_agents_run_in_relay_mode_and_measure_rtt(relay_cluster):
    h = relay_cluster
    assert all(a.transport == 'relay' for a in h.agents.values())
    assert all(a.engine.bind_ip == '127.0.0.1' for a in h.agents.values() if hasattr(a.engine, 'bind_ip'))
    r = await chat(h, QWEN, max_tokens=4)
    assert r.status_code == 200, r.text
    members = {m['node'] for m in r.json()['network']['members']}
    assert 'head' in members and len(members) == 2
    # relay latency: (rtt_a + rtt_b) / 2 stored for every pair
    rows = h.conn.execute('SELECT COUNT(*) AS n FROM latency').fetchone()['n']
    assert rows >= 2


async def test_bytes_round_trip_through_the_relay(relay_cluster):
    h = relay_cluster
    r = await chat(h, QWEN, max_tokens=4)
    pid = r.json()['network']['pipeline_id']
    worker = next(m['node'] for m in r.json()['network']['members'] if m['node'] != 'head')
    assert h.conn.execute('SELECT endpoint FROM pipeline_members WHERE pipeline_id=? AND node_id=?',
                          (pid, h.ids[worker])).fetchone()['endpoint'] == f'relay:{h.ids[worker]}'
    proxy = h.agents['head'].proxies[pid][0]
    payload = os.urandom(20 * 1024 * 1024)
    reader, writer = await asyncio.open_connection('127.0.0.1', proxy.port)

    async def send():
        for i in range(0, len(payload), 1 << 20):
            writer.write(payload[i:i + (1 << 20)])
            await writer.drain()

    sender = asyncio.create_task(send())
    got = await asyncio.wait_for(reader.readexactly(len(payload)), 30)
    await sender
    assert got == payload  # every byte, in order, through head proxy -> coordinator -> worker -> echo
    assert h.svc.relay.bytes_by_pipeline[pid] >= 2 * len(payload)

    # stopping the pipeline cuts the relayed connection
    await h.svc.mgr.stop_pipeline(pid, 'test')
    assert await asyncio.wait_for(reader.read(), 10) == b''
    writer.close()


async def test_relay_rejects_nodes_outside_the_pipeline(relay_cluster):
    h = relay_cluster
    r = await chat(h, QWEN, max_tokens=4)
    pid = r.json()['network']['pipeline_id']
    worker_id = next(m['node_id'] for m in r.json()['network']['members'] if m['node'] != 'head')
    outsider = h.agents['outsider']
    for token in (outsider.token, 'not-a-token'):
        with pytest.raises(InvalidStatus):
            async with connect(ws_url(h.node_url, f'/relay/head/{pid}/{worker_id}'),
                               additional_headers={'Authorization': f'Bearer {token}'}):
                pass
    with pytest.raises(InvalidStatus):
        async with connect(ws_url(h.node_url, '/relay/worker/s-unknown'),
                           additional_headers={'Authorization': f'Bearer {h.agents["head"].token}'}):
            pass
