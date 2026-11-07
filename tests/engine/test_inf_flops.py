from types import SimpleNamespace

import pytest

from inf_gguf_fixtures import dense_model, moe_model
from compute.engine.flops import (
    ModelFlops, estimate_flops, gen_tps, gen_weight, prompt_tps, request_flops, span_flops,
)
from compute.engine.gguf import parse_header


def member(node_id, role, start, end):
    return SimpleNamespace(node_id=node_id, role=role, layer_start=start, layer_end=end)


def brute(mf, start, end, positions, head):
    total = 0.0
    for pos in positions:
        for i in range(start, end):
            total += 2 * mf.layer_params[i] + 2 * pos * mf.hidden
        if head:
            total += 2 * mf.head_params
    return total


def test_dense_parameter_counts_from_tensor_shapes():
    emb, ff, heads, kv, vocab = 256, 512, 8, 2, 1000
    mf = ModelFlops.from_headers([parse_header(dense_model(n_layers=3, emb=emb, ff=ff, heads=heads, kv_heads=kv,
                                                           vocab=vocab))])
    hd = emb // heads
    per_layer = emb + emb * emb + 2 * emb * kv * hd + emb * emb + 2 * emb * ff
    assert mf.layer_params == (per_layer,) * 3
    assert mf.head_params == emb * vocab        # untied: the output matrix
    assert mf.hidden == emb


def test_tied_embeddings_count_as_the_head():
    mf = ModelFlops.from_headers([parse_header(dense_model(n_layers=2, emb=128, vocab=500, tied=True))])
    assert mf.head_params == 128 * 500


def test_moe_experts_count_at_the_routed_fraction():
    emb, ff, experts, used = 256, 128, 8, 2
    mf = ModelFlops.from_headers([parse_header(moe_model(n_layers=2, emb=emb, ff=ff, experts=experts, used=used))])
    dense_part = emb * emb + 2 * emb * 64 + emb * emb + emb * experts
    expert_part = 2 * emb * ff * experts * used / experts
    assert mf.layer_params[0] == pytest.approx(dense_part + expert_part)


def test_span_matches_brute_force_including_attention_growth():
    mf = ModelFlops((10.0, 20.0, 30.0, 40.0), head_params=7.0, hidden=3)
    assert span_flops(mf, 1, 3, 5, 4, head=False) == pytest.approx(brute(mf, 1, 3, range(5, 9), False))
    assert span_flops(mf, 0, 4, 0, 6, head=True) == pytest.approx(brute(mf, 0, 4, range(0, 6), True))
    assert span_flops(mf, 0, 4, 0, 0, head=True) == 0.0


def test_request_split_adds_up_and_weights_generation():
    mf = ModelFlops(tuple(float(100 + i) for i in range(8)), head_params=50.0, hidden=4)
    members = [member("w", "worker", 0, 5), member("h", "head", 5, 8)]
    per = request_flops(mf, members, cache_n=3, prompt_n=10, predicted_n=6, gen_weight=1.0)
    whole = span_flops(mf, 0, 8, 3, 10, True) + span_flops(mf, 0, 8, 13, 6, True)
    assert sum(per.values()) == pytest.approx(whole)
    # only the head runs the output layer
    assert per["h"] == pytest.approx(brute(mf, 5, 8, range(3, 19), True))
    weighted = request_flops(mf, members, cache_n=3, prompt_n=10, predicted_n=6, gen_weight=10.0)
    gen_only = span_flops(mf, 0, 8, 13, 6, True)
    assert sum(weighted.values()) == pytest.approx(whole + 9 * gen_only)


def test_estimate_is_an_upper_bound_for_the_same_weight():
    mf = ModelFlops((100.0,) * 4, head_params=10.0, hidden=8)
    actual = sum(request_flops(mf, [member("h", "head", 0, 4)], 0, 20, 5, 3.0).values())
    assert estimate_flops(mf, 20, 16, 3.0) >= actual


def test_generation_weight_is_clamped_and_falls_back():
    assert gen_weight(None, 20.0, default=10.0, maximum=50.0) == 10.0
    assert gen_weight(400.0, None, default=10.0, maximum=50.0) == 10.0
    assert gen_weight(400.0, 20.0, default=10.0, maximum=50.0) == 20.0
    assert gen_weight(10_000.0, 20.0, default=10.0, maximum=50.0) == 50.0
    assert gen_weight(10.0, 20.0, default=10.0, maximum=50.0) == 1.0     # never below face value


def test_speeds_from_llama_server_timings():
    assert prompt_tps({"prompt_per_second": 812.5}) == 812.5
    assert prompt_tps({"prompt_n": 100, "prompt_ms": 500}) == 200.0
    assert gen_tps({"predicted_n": 30, "predicted_ms": 1000}) == 30.0
    assert prompt_tps({"prompt_n": 0, "prompt_ms": 0}) is None
