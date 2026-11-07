# /compute

Pool computers (Apple Silicon Macs and Windows PCs) into one shared LLM. Contributors lend a percentage of their GPU/unified memory and earn FLOPs; anyone spends FLOPs to chat with models too big for their own machine, split layer-by-layer across the pool with llama.cpp RPC.

See `docs/` for the API contract, install notes and draft terms.

## Run from source (Mac)

```bash
uv sync --group dev
PYTHONPATH=src uv run python -m compute            # desktop app
uv run pytest -q                                   # 160 tests, no GPU needed
```

`PYTHONPATH=src` is needed on this Mac because uv marks `.venv` hidden and Python 3.13 skips hidden `.pth` files.

Roles (the packaged app runs the same subcommands on itself):

```bash
COMPUTE_ADMIN_PASSWORD=... compute server serve --admin-email admin@example.com   # main server, admin only
compute node start --url http://<server>:8765 --memory-percent 50 --models-dir ~/models  # contribute
```

Build: `scripts/build_mac.sh` → `dist/compute-mac.dmg`; on Windows `scripts/build_windows.ps1` → `dist/compute-windows.zip`.
