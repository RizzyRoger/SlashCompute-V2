"""Download the pinned llama.cpp release for this machine: ``uv run python scripts/fetch_llama.py [--home DIR]``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))   # runs without an installed package

from compute.engine.node.binaries import LLAMA_BUILD, LlamaDownloadError, ensure_llama  # noqa: E402


def _progress(name: str, done: int, total: int) -> None:
    pct = f"{done * 100 // total:3d}%" if total else f"{done >> 20} MiB"
    print(f"\r{name}: {pct}", end="", file=sys.stderr, flush=True)
    if total and done >= total:
        print(file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=f"Fetch llama.cpp {LLAMA_BUILD} (llama-server + rpc-server)")
    ap.add_argument("--home", type=Path, default=None, help="State directory (default: the app home)")
    args = ap.parse_args()
    try:
        server, rpc = ensure_llama(args.home, progress=_progress)
    except LlamaDownloadError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"llama-server: {server}\nrpc-server:   {rpc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
