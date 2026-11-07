"""Join benchmark: the basket model alone on this node (512 input / 128 output tokens, 3 runs).

Gives `prompt_score` and `gen_score` = this node's tokens/s / the reference machine's tokens/s.
Used for planning only, never for pay.
"""
from __future__ import annotations

import random
import statistics
from pathlib import Path

import httpx

from compute.engine.node import head, rpc_worker

PROMPT_TOKENS = 512
OUTPUT_TOKENS = 128
RUNS = 3


async def run_benchmark(llama_server: str, basket_path: Path, ref_prompt_tps: float, ref_gen_tps: float) -> dict:
    if not basket_path.is_file():
        raise FileNotFoundError(f'basket model not found: {basket_path} (see scripts/fetch_smoke_model.sh)')
    port = head.free_port()
    proc = await head.start_server(llama_server, str(basket_path), [], [], 2048, port)
    try:
        await head.wait_health(port, proc, timeout=300)
        prompt_tps, gen_tps = [], []
        rng = random.Random(0)
        async with httpx.AsyncClient(timeout=300) as client:
            for _ in range(RUNS):
                tokens = [rng.randint(100, 5000) for _ in range(PROMPT_TOKENS)]
                r = await client.post(f'http://127.0.0.1:{port}/completion', json={
                    'prompt': tokens, 'n_predict': OUTPUT_TOKENS, 'ignore_eos': True, 'cache_prompt': False,
                    'temperature': 0,
                })
                r.raise_for_status()
                t = r.json()['timings']
                prompt_tps.append(t['prompt_n'] / (t['prompt_ms'] / 1000))
                gen_tps.append(t['predicted_n'] / (t['predicted_ms'] / 1000))
        p, g = statistics.median(prompt_tps), statistics.median(gen_tps)
        return {'prompt_score': p / ref_prompt_tps, 'gen_score': g / ref_gen_tps,
                'prompt_tps': p, 'gen_tps': g, 'runs': RUNS}
    finally:
        await rpc_worker.terminate(proc)
