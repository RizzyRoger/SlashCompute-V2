"""Coordinator + 5 fake nodes + 3 models, 100 mixed requests, one node unplugged mid-run."""
import asyncio
import random

import httpx
import pytest

from inf_harness import FakeNode, fast_settings, start_harness
from compute.engine.coordinator.layers import synthetic_layout

GB = 10 ** 9
A = 'Qwen3.8-27B-UD-Q4_K_XL.gguf'                                     # 17.6 GB dense: fits one node
B = 'phi-4-F16.gguf'                                                  # 29.3 GB dense: needs two
C = 'NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16-00001-of-00002.gguf'  # 65.9 GB MoE: needs three

NODES = [
    FakeNode('pc', 32, gen_score=1.5, may_be_head=True, files=(A,)),
    FakeNode('mini', 32, gen_score=1.1, may_be_head=True, files=(B,)),
    FakeNode('studio', 32, gen_score=1.0, may_be_head=True, files=(C,)),
    FakeNode('mbp', 32, gen_score=0.8),
    FakeNode('laptop', 32, gen_score=0.4),
]


def layouts():
    return [
        synthetic_layout(A, n_layers=64, total_bytes=int(17.56 * GB), head_bytes=1 * GB, kv_bytes_per_token_per_layer=4096),
        synthetic_layout(B, n_layers=40, total_bytes=int(29.32 * GB), head_bytes=1 * GB, kv_bytes_per_token_per_layer=5120),
        synthetic_layout(C, n_layers=52, total_bytes=int(65.85 * GB), head_bytes=int(1.5 * GB),
                         kv_bytes_per_token_per_layer=2048, expert_fraction=0.95, expert_count=128,
                         expert_used_count=6),
    ]


async def ask(client, model, i, max_tokens):
    body = {'model': model, 'max_tokens': max_tokens,
            'messages': [{'role': 'user', 'content': f'request {i}: explain pipelines'}]}
    return await client.post('/v1/chat/completions', json=body)


def pipeline_nodes(h, pid):
    return [r['node_id'] for r in h.conn.execute(
        'SELECT node_id FROM pipeline_members WHERE pipeline_id=? ORDER BY position', (pid,))]


async def test_mixed_load_with_a_node_dropped_mid_run():
    h = await start_harness(fast_settings(PIPELINE_IDLE_SECONDS=600), time_scale=0.05)
    try:
        for layout in layouts():
            h.add_model(layout)
        await h.add_nodes(NODES, default_ms=2.0)
        async with httpx.AsyncClient(base_url=h.url, timeout=60) as client:
            # warm up in a fixed order so placement is deterministic
            first = {}
            for model in (A, B, C):
                r = await ask(client, model, -1, 8)
                assert r.status_code == 200, r.text
                first[model] = r.json()['network']['pipeline_id']
            assert len(pipeline_nodes(h, first[A])) == 1
            assert len(pipeline_nodes(h, first[B])) == 2
            assert len(pipeline_nodes(h, first[C])) == 3
            assert h.ids['mbp'] in pipeline_nodes(h, first[C])

            rng = random.Random(42)
            plan = [(rng.choice((A, B, C)), rng.randint(8, 24)) for _ in range(100)]
            sem = asyncio.Semaphore(10)
            done = 0
            dropped = asyncio.Event()

            async def one(i, model, n):
                nonlocal done
                async with sem:
                    r = await ask(client, model, i, n)
                done += 1
                if done == 40 and not dropped.is_set():
                    dropped.set()
                    await h.drop('mbp')  # unplug a member of the MoE pipeline mid-run
                return r

            results = await asyncio.gather(*[one(i, m, n) for i, (m, n) in enumerate(plan)])

        conn = h.conn
        # 1. every request completed (a request whose first attempt failed was retried once)
        assert all(r.status_code == 200 for r in results), [r.text for r in results if r.status_code != 200]
        failed = conn.execute("SELECT * FROM jobs WHERE state='failed'").fetchall()
        assert failed, 'dropping a node should have failed at least one in-flight job'
        for f in failed:
            assert f['attempt'] == 1 and f['retryable'] == 1
            retry = conn.execute('SELECT * FROM jobs WHERE retry_of=?', (f['id'],)).fetchone()
            assert retry is not None and retry['state'] == 'done' and retry['attempt'] == 2
        done_jobs = conn.execute("SELECT * FROM jobs WHERE state='done'").fetchall()
        assert len(done_jobs) == 103

        # the MoE pipeline broke and was re-planned without the dropped node
        assert conn.execute('SELECT state FROM pipelines WHERE id=?', (first[C],)).fetchone()['state'] == 'broken'
        live_c = conn.execute("SELECT id FROM pipelines WHERE model_id=? AND state='active'", (C,)).fetchall()
        assert live_c and all(h.ids['mbp'] not in pipeline_nodes(h, p['id']) for p in live_c)

        # 2. shares add up to 1 for every pipeline
        for p in conn.execute('SELECT id FROM pipelines').fetchall():
            shares = [float(r['share']) for r in conn.execute('SELECT share FROM pipeline_members WHERE pipeline_id=?',
                                                               (p['id'],))]
            assert sum(shares) == pytest.approx(1.0, abs=1e-9)

        # 3. every finished job was credited once, in FLOPs, split exactly over its pipeline's members
        records = h.svc.accounting.records
        assert len(records) == len(done_jobs) == 103
        for j in done_jobs:
            assert j['flops'] > 0 and j['gen_weight'] >= 1.0
        assert sum(sum(r['per_node'].values()) for r in records) == pytest.approx(sum(j['flops'] for j in done_jobs))
        for r in records:
            assert all(v > 0 for v in r['per_node'].values())

        # 4. nobody (in particular the dropped node) is credited for failed attempts: the dropped
        #    node only appears in records of jobs that finished on a pipeline it belonged to
        mbp = h.ids['mbp']
        mbp_credits = [r for r in records if mbp in r['per_node']]
        member_sets = [set(pipeline_nodes(h, j['pipeline_id'])) for j in done_jobs]
        assert len(mbp_credits) == sum(1 for m in member_sets if mbp in m)
        rel = conn.execute('SELECT reliability FROM nodes WHERE id=?', (h.ids['mbp'],)).fetchone()['reliability']
        assert rel < 1.0
    finally:
        await h.stop()
