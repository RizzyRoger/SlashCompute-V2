# /compute

Pool computers (Apple Silicon Macs and Windows PCs) into one shared LLM. Contributors lend a percentage of their GPU/unified memory and earn FLOPs; anyone spends FLOPs to chat with models too big for their own machine, split layer-by-layer across the pool with llama.cpp RPC.

See `docs/` for the API contract, install notes and draft terms.

## Secrets and settings (.env)

Copy `.env.example` to `.env` and fill in `COMPUTE_ADMIN_EMAIL` (required on the server machine), and optionally `COMPUTE_ADMIN_PASSWORD` and `COMPUTE_INF_TOKEN`. `.env` is git-ignored. The packaged app reads `~/.compute/.env` (Mac) or `%LOCALAPPDATA%\compute\.env` (Windows). Real environment variables override the file.

## Run from source (Mac)

```bash
uv sync --group dev
PYTHONPATH=src uv run python -m compute            # desktop app
uv run pytest -q                                   # 160 tests, no GPU needed
```

`PYTHONPATH=src` is needed on this Mac because uv marks `.venv` hidden and Python 3.13 skips hidden `.pth` files.

Roles (the packaged app runs the same subcommands on itself):

```bash
compute server serve --admin-email "$COMPUTE_ADMIN_EMAIL"   # main server, admin only (email + password from .env)
compute node start --url http://<server>:8765 --memory-percent 50 --models-dir ~/models  # contribute
```

Build: `scripts/build_mac.sh` → `dist/compute-mac.dmg`; on Windows `scripts/build_windows.ps1` → `dist/compute-windows.zip`.
