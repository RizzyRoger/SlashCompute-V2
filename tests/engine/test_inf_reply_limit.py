"""Reply limits: no max_tokens fills the context (fill_context), and a limit the user can't afford shrinks
to what they can spend (fit_max_tokens) instead of the request being refused."""

from __future__ import annotations

import math

from compute.engine.config import InferenceSettings
from compute.engine.coordinator.service import (
    CTX_MARGIN_TOKENS, MIN_REPLY_TOKENS, REPLY_ROOM_TOKENS, ctx_for, estimate_prompt_tokens, fill_context,
    fit_max_tokens,
)


def cost(tokens: int) -> float:
    return 1000.0 + 10.0 * tokens   # prompt + per generated token, like estimate_flops


def test_a_limit_the_user_can_afford_is_kept():
    assert fit_max_tokens(cost, 4096, spendable=cost(4096)) == 4096
    assert fit_max_tokens(cost, 4096, spendable=math.inf) == 4096


def test_a_limit_over_budget_shrinks_to_the_largest_that_fits():
    assert fit_max_tokens(cost, 4096, spendable=cost(1900)) == 1900
    assert fit_max_tokens(cost, 4096, spendable=cost(1900) + 9.0) == 1900   # 1901 would cost 1 FLOP too many


def test_never_fitted_below_the_floor():
    assert fit_max_tokens(cost, 4096, spendable=cost(MIN_REPLY_TOKENS)) == MIN_REPLY_TOKENS
    # Can't even afford the floor: keep the request as asked, so the reservation refuses it as before.
    assert fit_max_tokens(cost, 4096, spendable=cost(MIN_REPLY_TOKENS) - 1) == 4096
    assert fit_max_tokens(cost, 4096, spendable=0.0) == 4096


def test_no_limit_fills_the_default_context():
    s = InferenceSettings()
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert fill_context(body, s) == s.DEFAULT_CTX - estimate_prompt_tokens(body) - CTX_MARGIN_TOKENS
    assert ctx_for({**body, "max_tokens": fill_context(body, s)}, s) == s.DEFAULT_CTX   # no extra memory


def test_no_limit_grows_the_context_only_to_leave_the_reply_room():
    s = InferenceSettings()
    long = {"messages": [{"role": "user", "content": "x" * 4 * 3500}]}   # ~3,500 prompt tokens
    room = fill_context(long, s)
    assert room >= REPLY_ROOM_TOKENS
    assert room == 2 * s.DEFAULT_CTX - estimate_prompt_tokens(long) - CTX_MARGIN_TOKENS
    assert ctx_for({**long, "max_tokens": room}, s) == 2 * s.DEFAULT_CTX


def test_small_limits_are_left_alone():
    assert fit_max_tokens(cost, 8, spendable=0.0) == 8
    assert fit_max_tokens(cost, MIN_REPLY_TOKENS, spendable=0.0) == MIN_REPLY_TOKENS
