"""Inference FLOPs, credited in the same unit as training (``metering/flops.py``).

Counts are for the logical model (parameter counts from the GGUF tensor shapes), so the same work
earns the same FLOPs regardless of quantization. Per token at context position ``pos``, one
transformer layer costs ``2 * layer_params + 2 * pos * hidden``; the output head (on the pipeline's
head node) adds ``2 * head_params``. MoE expert tensors count at ``expert_used / expert_count``.

Prompt tokens are compute-bound and count at face value. Generated tokens are memory-bound: each
costs the same FLOPs as a prompt token but takes far longer, so they are multiplied by a weight
``w = prompt tok/s / generation tok/s`` (clamped). Credits for generation are therefore
FLOP-equivalents: an hour of hosting earns about what an hour of compute-bound work does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from compute.engine.gguf import GGUFHeader

_BLK = re.compile(r"^blk\.(\d+)\.")


@dataclass(frozen=True)
class ModelFlops:
    layer_params: tuple[float, ...]   # active parameters per layer
    head_params: float                # output head (or tied embedding), run once per token on the head
    hidden: int

    @property
    def n_layers(self) -> int:
        return len(self.layer_params)

    def to_json(self) -> dict:
        return {"layer_params": list(self.layer_params), "head_params": self.head_params, "hidden": self.hidden}

    @classmethod
    def from_json(cls, d: dict) -> "ModelFlops":
        return cls(tuple(float(x) for x in d["layer_params"]), float(d["head_params"]), int(d["hidden"]))

    @classmethod
    def from_headers(cls, headers: list[GGUFHeader]) -> "ModelFlops":
        """Parameter counts from the GGUF header(s) (one per shard; metadata from the first)."""
        first = headers[0]
        tensors = [t for h in headers for t in h.tensors]
        blk_ids = [int(m.group(1)) for t in tensors if (m := _BLK.match(t.name))]
        n_layers = max(first.get("block_count", 0) or 0, max(blk_ids) + 1 if blk_ids else 0)
        n_exp = first.get("expert_count", 0) or 0
        n_used = first.get("expert_used_count", 0) or 0
        frac = n_used / n_exp if n_exp and n_used else 1.0
        layers = [0.0] * n_layers
        output = embd = 0.0
        for t in tensors:
            m = _BLK.match(t.name)
            if m:
                if "nextn" in t.name:
                    continue  # multi-token-prediction layer: held but not run during normal decode
                layers[int(m.group(1))] += t.n_elements * (frac if "_exps" in t.name else 1.0)
            elif t.name.startswith("output."):
                output += t.n_elements
            elif t.name.startswith("token_embd"):
                embd += t.n_elements
        hidden = int(first.get("embedding_length", 0) or 0)
        return cls(tuple(layers), output or embd, hidden)

    @classmethod
    def from_layout(cls, layout, bytes_per_param: float = 2.0, hidden: int = 4096) -> "ModelFlops":
        """Estimate from a layer table when no header is available (synthetic test models)."""
        return cls(tuple(l.active_bytes / bytes_per_param for l in layout.layers),
                   layout.head_active_bytes / bytes_per_param, hidden)


def _pos_sum(start: int, n: int) -> float:
    """Sum of context positions start, start+1, ..., start+n-1."""
    return n * (2 * start + n - 1) / 2 if n > 0 else 0.0


def span_flops(mf: ModelFlops, layer_start: int, layer_end: int, start: int, n: int, head: bool) -> float:
    """FLOPs for ``n`` tokens at positions ``start..start+n-1`` through layers [layer_start, layer_end)."""
    if n <= 0:
        return 0.0
    params = sum(mf.layer_params[layer_start:layer_end])
    out = 2.0 * params * n + 2.0 * (layer_end - layer_start) * mf.hidden * _pos_sum(start, n)
    if head:
        out += 2.0 * mf.head_params * n
    return out


def request_flops(mf: ModelFlops, members: Iterable, cache_n: int, prompt_n: int, predicted_n: int,
                  gen_weight: float) -> dict[str, float]:
    """Per-node FLOP credit for one request. ``members`` have node_id, role, layer_start, layer_end."""
    gen_start = cache_n + prompt_n
    out: dict[str, float] = {}
    for m in members:
        head = m.role == "head"
        prompt = span_flops(mf, m.layer_start, m.layer_end, cache_n, prompt_n, head)
        gen = span_flops(mf, m.layer_start, m.layer_end, gen_start, predicted_n, head)
        out[m.node_id] = out.get(m.node_id, 0.0) + prompt + gen * gen_weight
    return out


def estimate_flops(mf: ModelFlops, prompt_n: int, max_tokens: int, gen_weight: float) -> float:
    """Upper bound for a request before it runs (used to reserve credits)."""
    prompt = span_flops(mf, 0, mf.n_layers, 0, prompt_n, True)
    gen = span_flops(mf, 0, mf.n_layers, prompt_n, max_tokens, True)
    return prompt + gen * gen_weight


def prompt_tps(timings: dict) -> Optional[float]:
    if timings.get("prompt_per_second"):
        return float(timings["prompt_per_second"])
    n, ms = timings.get("prompt_n") or 0, timings.get("prompt_ms") or 0
    return n / (ms / 1000) if n and ms else None


def gen_tps(timings: dict) -> Optional[float]:
    if timings.get("predicted_per_second"):
        return float(timings["predicted_per_second"])
    n, ms = timings.get("predicted_n") or 0, timings.get("predicted_ms") or 0
    return n / (ms / 1000) if n and ms else None


def gen_weight(ema_prompt_tps: Optional[float], generation_tps: Optional[float], default: float,
               maximum: float) -> float:
    """Time weight for generated tokens: prompt speed / generation speed, clamped to [1, maximum]."""
    if not ema_prompt_tps or not generation_tps:
        return default
    return min(max(ema_prompt_tps / generation_tps, 1.0), maximum)
