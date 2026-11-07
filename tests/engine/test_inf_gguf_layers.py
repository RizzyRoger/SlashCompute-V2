from pathlib import Path

import pytest

from compute.engine.gguf import GGUFHeader, NeedMoreBytes, parse_header, read_header_file, shard_paths
from compute.engine.coordinator.layers import ModelLayout, build_layout, synthetic_layout
from inf_gguf_fixtures import dense_model, moe_model


def test_parses_metadata_and_tensors():
    h = parse_header(dense_model(n_layers=3))
    assert h.arch == 'llama'
    assert h.get('block_count') == 3
    names = [t.name for t in h.tensors]
    assert 'blk.2.ffn_down.weight' in names and 'output.weight' in names
    q = next(t for t in h.tensors if t.name == 'blk.0.attn_q.weight')
    assert q.nbytes == 256 * 256 // 32 * 34  # Q8_0: 34 bytes per 32 elements


def test_truncated_buffer_asks_for_more_bytes():
    buf = dense_model()
    with pytest.raises(NeedMoreBytes):
        parse_header(buf[: len(buf) // 2])


def test_header_json_round_trip():
    h = parse_header(dense_model())
    assert GGUFHeader.from_json(h.to_json()) == h


def test_dense_layer_table_active_equals_full_and_kv_profile():
    layout = build_layout('dense.gguf', [parse_header(dense_model(n_layers=4, emb=256, heads=8, kv_heads=2))])
    assert layout.n_layers == 4
    for l in layout.layers:
        assert l.active_bytes == l.full_bytes > 0
        # 2 kv heads x (32 + 32) dims x 2 bytes
        assert l.kv_bytes_per_token == 2 * 64 * 2
    # untied: embedding is held by the head but only the output layer is read per token
    assert layout.head_active_bytes < layout.head_full_bytes


def test_tied_embedding_counts_as_active():
    layout = build_layout('tied.gguf', [parse_header(dense_model(tied=True))])
    embd = next(t for t in parse_header(dense_model(tied=True)).tensors if t.name == 'token_embd.weight')
    assert layout.head_active_bytes >= embd.nbytes


def test_moe_active_bytes_scale_routed_experts_by_used_over_count():
    h = parse_header(moe_model(experts=8, used=2))
    layout = build_layout('moe.gguf', [h])
    assert layout.moe and layout.expert_count == 8 and layout.expert_used_count == 2
    blk0 = [t for t in h.tensors if t.name.startswith('blk.0.')]
    exps = sum(t.nbytes for t in blk0 if '_exps' in t.name)
    rest = sum(t.nbytes for t in blk0 if '_exps' not in t.name)
    assert layout.layers[0].full_bytes == exps + rest
    assert layout.layers[0].active_bytes == round(rest + exps * 2 / 8)


def test_layout_json_round_trip():
    layout = build_layout('moe.gguf', [parse_header(moe_model())])
    assert ModelLayout.from_json(layout.to_json()) == layout


def test_sharded_header_files(tmp_path: Path):
    for i in (1, 2):
        (tmp_path / f'm-0000{i}-of-00002.gguf').write_bytes(dense_model(n_layers=2))
    shards = shard_paths(tmp_path / 'm-00002-of-00002.gguf')
    assert [p.name for p in shards] == ['m-00001-of-00002.gguf', 'm-00002-of-00002.gguf']
    headers = [read_header_file(p, start=64) for p in shards]
    layout = build_layout('m-00001-of-00002.gguf', headers)
    assert layout.n_layers == 2


def test_synthetic_layout_totals():
    gb = 10 ** 9
    layout = synthetic_layout('x', n_layers=80, total_bytes=int(42.5 * gb), head_bytes=int(1.5 * gb))
    assert layout.n_layers == 80
    assert abs(layout.total_full_bytes - 42.5 * gb) < 80
    assert layout.kv_bytes(4096) == 80 * 4096 * 4096


def test_real_vocab_gguf_headers_parse_if_present():
    """Sanity check against real llama.cpp files on this machine (skipped elsewhere)."""
    files = sorted(Path.home().glob('.unsloth/llama.cpp/models/ggml-vocab-*.gguf'))[:5]
    if not files:
        pytest.skip('no local llama.cpp vocab GGUFs')
    for f in files:
        h = read_header_file(f)
        assert h.arch and h.version >= 2
