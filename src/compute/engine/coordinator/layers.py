"""Per-layer table of a GGUF model, built from its header (never from config.json).

For each `blk.<n>` layer we record:
  full_bytes    bytes the device must hold (every expert)
  active_bytes  bytes actually read per token (routed experts x used/count)
  kv_bytes_per_token  KV-cache bytes per token of context (f16), capped at the sliding window
Everything outside `blk.*` (token embedding, output head, norms) is held by the pipeline head.
Plan memory with full bytes; plan speed and shares with active bytes.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from compute.engine.gguf import GGUFHeader

_BLK = re.compile(r'^blk\.(\d+)\.')


@dataclass(frozen=True)
class LayerInfo:
    index: int
    full_bytes: int
    active_bytes: int
    kv_bytes_per_token: int = 0
    kv_window: int | None = None  # sliding-window layers only keep this many tokens of KV

    def kv_bytes(self, ctx: int) -> int:
        tokens = min(ctx, self.kv_window) if self.kv_window else ctx
        return self.kv_bytes_per_token * tokens


@dataclass(frozen=True)
class ModelLayout:
    name: str
    arch: str
    layers: tuple[LayerInfo, ...]
    head_full_bytes: int      # embedding + output + misc tensors, held by the head
    head_active_bytes: int    # output (+ tied embedding) read per token by the head
    moe: bool = False
    expert_count: int = 0
    expert_used_count: int = 0
    file_bytes: int = 0
    max_ctx: int | None = None
    source: str = 'gguf'      # gguf | synthetic

    @property
    def n_layers(self) -> int:
        return len(self.layers)

    @property
    def total_full_bytes(self) -> int:
        return sum(l.full_bytes for l in self.layers) + self.head_full_bytes

    @property
    def total_active_bytes(self) -> int:
        return sum(l.active_bytes for l in self.layers) + self.head_active_bytes

    def kv_bytes(self, ctx: int) -> int:
        return sum(l.kv_bytes(ctx) for l in self.layers)

    def to_json(self) -> dict:
        d = asdict(self)
        d['layers'] = [asdict(l) for l in self.layers]
        return d

    @classmethod
    def from_json(cls, d: dict) -> 'ModelLayout':
        return cls(**{**d, 'layers': tuple(LayerInfo(**l) for l in d['layers'])})


def _per_layer(value, n: int) -> list:
    if isinstance(value, list):
        return list(value) + [value[-1] if value else 0] * max(0, n - len(value))
    return [value] * n


def _kv_profile(h: GGUFHeader, n_layers: int, has_attn: list[bool], kv_elem_bytes: int):
    """KV bytes per token and sliding window per layer."""
    hq = _per_layer(h.get('attention.head_count', 0) or 0, n_layers)
    hkv = _per_layer(h.get('attention.head_count_kv', None), n_layers)
    emb = h.get('embedding_length', 0) or 0
    lora = h.get('attention.kv_lora_rank')
    window = h.get('attention.sliding_window')
    pattern = h.get('attention.sliding_window_pattern')
    full_interval = h.get('full_attention_interval')
    per_tok, windows = [], []
    for i in range(n_layers):
        attn = has_attn[i]
        if full_interval:
            attn = attn and (i + 1) % full_interval == 0
        n_kv = hkv[i] if hkv[i] is not None else hq[i]
        if not attn or (not lora and not n_kv):
            per_tok.append(0)
            windows.append(None)
            continue
        if lora:  # MLA caches the compressed latent + rope part
            rope = h.get('rope.dimension_count', 64) or 64
            per_tok.append((lora + rope) * kv_elem_bytes)
        else:
            head_dim = (emb // hq[i]) if hq[i] else 128
            k_len = h.get('attention.key_length', head_dim) or head_dim
            v_len = h.get('attention.value_length', k_len) or k_len
            per_tok.append(n_kv * (k_len + v_len) * kv_elem_bytes)
        is_swa = False
        if window:
            if isinstance(pattern, list):
                is_swa = bool(pattern[i]) if i < len(pattern) else False
            elif isinstance(pattern, int) and pattern > 1:
                is_swa = (i + 1) % pattern != 0
        windows.append(window if is_swa else None)
    return per_tok, windows


def build_layout(name: str, headers: list[GGUFHeader], file_bytes: int = 0,
                 kv_elem_bytes: int = 2) -> ModelLayout:
    """Layer table from the header(s) of a model (one header per shard; metadata from the first)."""
    first = headers[0]
    tensors = [t for h in headers for t in h.tensors]
    n_layers = first.get('block_count', 0) or 0
    blk_ids = [int(m.group(1)) for t in tensors if (m := _BLK.match(t.name))]
    n_layers = max(n_layers, max(blk_ids) + 1 if blk_ids else 0)

    n_exp = first.get('expert_count', 0) or 0
    n_used = first.get('expert_used_count', 0) or 0
    moe = bool(n_exp and n_used)
    frac = n_used / n_exp if moe else 1.0

    full = [0] * n_layers
    active = [0.0] * n_layers
    has_attn = [False] * n_layers
    head_full = 0
    head_active = 0
    has_output = any(t.name.startswith('output.') for t in tensors)
    for t in tensors:
        m = _BLK.match(t.name)
        if m:
            i = int(m.group(1))
            full[i] += t.nbytes
            if 'nextn' in t.name:
                pass  # multi-token-prediction layer: held but not run during normal decode
            elif '_exps' in t.name and moe:
                active[i] += t.nbytes * frac
            else:
                active[i] += t.nbytes
            if 'attn_' in t.name:
                has_attn[i] = True
            continue
        head_full += t.nbytes
        if t.name.startswith('token_embd'):
            if not has_output:  # tied embeddings double as the output head
                head_active += t.nbytes
        elif t.name.startswith('per_layer'):
            pass  # per-layer embedding lookups read one row
        else:
            head_active += t.nbytes

    per_tok, windows = _kv_profile(first, n_layers, has_attn, kv_elem_bytes)
    layers = tuple(
        LayerInfo(index=i, full_bytes=full[i], active_bytes=int(round(active[i])),
                  kv_bytes_per_token=per_tok[i], kv_window=windows[i])
        for i in range(n_layers)
    )
    return ModelLayout(
        name=name, arch=first.arch, layers=layers, head_full_bytes=head_full,
        head_active_bytes=head_active, moe=moe, expert_count=n_exp, expert_used_count=n_used,
        file_bytes=file_bytes or sum(t.nbytes for t in tensors), max_ctx=first.get('context_length'),
        source='gguf',
    )


def synthetic_layout(name: str, n_layers: int, total_bytes: int, head_bytes: int,
                     kv_bytes_per_token_per_layer: int = 4096, expert_fraction: float = 0.0,
                     expert_count: int = 0, expert_used_count: int = 0,
                     head_active_bytes: int | None = None, arch: str = 'synthetic') -> ModelLayout:
    """Uniform layer table for tests and the fake-node demo.

    `expert_fraction` is the share of each layer's bytes that are routed experts (MoE only).
    """
    per_layer = (total_bytes - head_bytes) // n_layers
    moe = bool(expert_count and expert_used_count)
    used = expert_used_count / expert_count if moe else 1.0
    active = int(per_layer * (1 - expert_fraction) + per_layer * expert_fraction * used) if moe else per_layer
    layers = tuple(LayerInfo(i, per_layer, active, kv_bytes_per_token_per_layer) for i in range(n_layers))
    return ModelLayout(
        name=name, arch=arch, layers=layers, head_full_bytes=head_bytes,
        # by default half the head bytes are the output layer (read per token), half the embedding
        head_active_bytes=head_bytes // 2 if head_active_bytes is None else head_active_bytes,
        moe=moe, expert_count=expert_count, expert_used_count=expert_used_count,
        file_bytes=total_bytes, source='synthetic',
    )
