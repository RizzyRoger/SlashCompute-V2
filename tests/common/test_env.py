"""The .env loader keeps secrets out of git without overriding real environment variables."""

from __future__ import annotations

import os

from compute.common.env import load_env, parse


def test_parse_handles_comments_quotes_and_export():
    text = """
# admin
COMPUTE_ADMIN_EMAIL=Boss@Example.test
export COMPUTE_ADMIN_PASSWORD="pa ss # not a comment"
COMPUTE_INF_TOKEN='abc123'
COMPUTE_STARTER_FLOPS=4e15  # trailing comment
=nokey
not a pair
"""
    assert parse(text) == {
        "COMPUTE_ADMIN_EMAIL": "Boss@Example.test",
        "COMPUTE_ADMIN_PASSWORD": "pa ss # not a comment",
        "COMPUTE_INF_TOKEN": "abc123",
        "COMPUTE_STARTER_FLOPS": "4e15",
    }


def test_load_env_never_overrides_real_variables(tmp_path, monkeypatch):
    first, second = tmp_path / "a.env", tmp_path / "b.env"
    first.write_text("COMPUTE_TEST_A=from-a\nCOMPUTE_TEST_B=from-a\n")
    second.write_text("COMPUTE_TEST_B=from-b\nCOMPUTE_TEST_C=from-b\n")
    monkeypatch.setenv("COMPUTE_TEST_A", "real")
    for key in ("COMPUTE_TEST_B", "COMPUTE_TEST_C"):
        monkeypatch.delenv(key, raising=False)

    loaded = load_env([first, tmp_path / "missing.env", second])

    assert loaded == [first, second]
    assert os.environ["COMPUTE_TEST_A"] == "real"      # real env wins
    assert os.environ["COMPUTE_TEST_B"] == "from-a"    # first file wins
    assert os.environ["COMPUTE_TEST_C"] == "from-b"
    for key in ("COMPUTE_TEST_B", "COMPUTE_TEST_C"):
        monkeypatch.delenv(key)
