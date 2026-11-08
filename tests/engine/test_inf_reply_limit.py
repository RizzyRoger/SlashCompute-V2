"""fit_max_tokens: a reply's limit shrinks to what the user can spend instead of the request being refused."""

from __future__ import annotations

import math

from compute.engine.coordinator.service import MIN_REPLY_TOKENS, fit_max_tokens


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


def test_small_limits_are_left_alone():
    assert fit_max_tokens(cost, 8, spendable=0.0) == 8
    assert fit_max_tokens(cost, MIN_REPLY_TOKENS, spendable=0.0) == MIN_REPLY_TOKENS
