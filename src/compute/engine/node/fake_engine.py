"""FakeEngine: simulates a llama.cpp RPC pipeline so the whole system runs on one laptop, no GPU.

Per-token time = sum over members (share_i x out_s / gen_score_i) + hops x one-way delay, scaled by
`time_scale` (0 = instant, for tests). Output is a deterministic function of the prompt, so audits
agree across pipelines, unless a member is marked byzantine. If any member of the pipeline is
dropped from the `FakeCluster`, the head raises mid-stream like llama-server losing an RPC worker.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import AsyncIterator

from compute.engine.devices import Device
from compute.engine.node.engine import EngineError, head_layout

WORDS = ('the', 'model', 'runs', 'split', 'across', 'several', 'devices', 'each', 'holding', 'a', 'block',
         'of', 'layers', 'and', 'every', 'token', 'passes', 'through', 'them', 'in', 'order', 'so', 'it', 'fits')


class FakeCluster:
    """Shared state between fake nodes in one process: liveness, byzantine nodes, latency."""

    def __init__(self, latency_ms: dict | None = None):
        self.dead: set[str] = set()
        self.byzantine: set[str] = set()
        self.latency_ms: dict[tuple[str, str], float] = dict(latency_ms or {})
        self._port = 50051

    def alive(self, node_id: str) -> bool:
        return node_id not in self.dead

    def next_port(self) -> int:
        self._port += 1
        return self._port

    def one_way(self, a: str, b: str, default: float = 1.0) -> float:
        if a == b:
            return 0.0
        return self.latency_ms.get((a, b), self.latency_ms.get((b, a), default))


def fake_tokens(body: dict, n: int) -> list[str]:
    seed = json.dumps(body.get('messages', body.get('prompt', '')), sort_keys=True).encode()
    out = []
    h = hashlib.sha256(seed).digest()
    while len(out) < n:
        h = hashlib.sha256(h).digest()
        out.extend(WORDS[b % len(WORDS)] for b in h[:8])
    return [(' ' if i else '') + w for i, w in enumerate(out[:n])]


def fake_prompt_tokens(body: dict) -> int:
    text = json.dumps(body.get('messages', ''))
    return max(8, len(text) // 4)


class FakeEngine:
    def __init__(self, node_id: str, cluster: FakeCluster, gen_score: float = 1.0, prompt_score: float = 1.0,
                 time_scale: float = 0.0, ip: str = '100.64.0.1', load_seconds: float = 2.0):
        self.node_id = node_id
        self.cluster = cluster
        self.gen_score, self.prompt_score = gen_score, prompt_score
        self.time_scale = time_scale
        self.ip = ip
        self.load_seconds = load_seconds
        self.workers: dict[str, str] = {}
        self.heads: dict[str, dict] = {}

    async def start_worker(self, pipeline_id: str, spec: dict) -> dict:
        endpoint = f'{self.ip}:{self.cluster.next_port()}'
        self.workers[pipeline_id] = endpoint
        return {'endpoint': endpoint}

    async def stop_worker(self, pipeline_id: str) -> None:
        self.workers.pop(pipeline_id, None)

    async def start_head(self, pipeline_id: str, spec: dict) -> dict:
        devices = [Device(f'RPC{i}', w['endpoint'], 65536, 65000) for i, w in enumerate(spec['workers'])]
        devices.append(Device('MTL0', f'Fake GPU ({self.node_id})', 65536, 65000))
        layout = head_layout(devices, spec, self.node_id) if spec['workers'] else \
            {'devices': [], 'tensor_split': [], 'order': [self.node_id]}
        await asyncio.sleep(self.load_seconds * self.time_scale)
        self.heads[pipeline_id] = spec
        return {**layout, 'load_seconds': self.load_seconds}

    async def stop_head(self, pipeline_id: str) -> None:
        self.heads.pop(pipeline_id, None)

    def _check_members(self, spec: dict) -> None:
        for m in spec['sim']['members']:
            if not self.cluster.alive(m['node_id']):
                raise EngineError(f"rpc worker {m['node_id']} lost", pipeline_broken=True)

    def _per_token_s(self, spec: dict) -> tuple[float, float]:
        sim = spec['sim']
        members = sim['members']
        gen = sum(m['share'] * sim['out_s'] / max(m['gen_score'], 1e-6) for m in members)
        prompt = sum(m['share'] * sim['in_s'] / max(m.get('prompt_score', 1.0), 1e-6) for m in members)
        if len(members) > 1:
            ids = [m['node_id'] for m in members]
            hops = [self.cluster.one_way(a, b) for a, b in zip(ids, ids[1:] + ids[:1])]
            gen += len(members) * sum(hops) / len(hops) / 1000
        return prompt, gen

    async def complete(self, pipeline_id: str, body: dict) -> AsyncIterator[dict]:
        spec = self.heads.get(pipeline_id)
        if spec is None:
            raise EngineError(f'no head for pipeline {pipeline_id}')
        self._check_members(spec)
        prompt_s, gen_s = self._per_token_s(spec)
        prompt_n = fake_prompt_tokens(body)
        max_tokens = int(body.get('max_tokens') or body.get('max_completion_tokens') or 64)
        tokens = fake_tokens(body, max_tokens)
        if any(m['node_id'] in self.cluster.byzantine for m in spec['sim']['members']):
            tokens = [t[::-1] for t in tokens]  # wrong work: different output
        await asyncio.sleep(prompt_n * prompt_s * self.time_scale)
        created = int(time.time())
        cid = 'chatcmpl-' + hashlib.sha1(f'{pipeline_id}{time.time()}'.encode()).hexdigest()[:12]
        for i, tok in enumerate(tokens):
            self._check_members(spec)
            if self.time_scale:
                await asyncio.sleep(gen_s * self.time_scale)
            elif i % 16 == 0:
                await asyncio.sleep(0)  # let other tasks (e.g. heartbeats, node drops) run
            yield {'type': 'chunk', 'data': {
                'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': spec['model'],
                'choices': [{'index': 0, 'delta': {'content': tok},
                             'finish_reason': 'length' if i == len(tokens) - 1 else None}],
            }}
        timings = {
            'cache_n': 0, 'prompt_n': prompt_n, 'prompt_ms': prompt_n * prompt_s * 1000,
            'predicted_n': len(tokens), 'predicted_ms': len(tokens) * gen_s * 1000,
        }
        yield {'type': 'final', 'timings': timings, 'finish_reason': 'length',
               'usage': {'prompt_tokens': prompt_n, 'completion_tokens': len(tokens),
                         'total_tokens': prompt_n + len(tokens)}}

    async def benchmark(self) -> dict:
        return {'prompt_score': self.prompt_score, 'gen_score': self.gen_score}

    async def stop_all(self) -> None:
        self.workers.clear()
        self.heads.clear()
