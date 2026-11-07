# /compute

Pool computers (Apple Silicon Macs and Windows PCs) into one shared LLM. Contributors lend a percentage of their GPU/unified memory and earn FLOPs; anyone spends FLOPs to chat with models too big for their own machine, split layer-by-layer across the pool with llama.cpp RPC.

See `docs/` for the API contract, install notes and draft terms.

## Secrets and settings (.env)

Copy `.env.example` to `.env` and optionally fill in `COMPUTE_ADMIN_EMAIL`, `COMPUTE_ADMIN_PASSWORD` and `COMPUTE_INF_TOKEN`. `.env` is git-ignored. The packaged app reads `~/.compute/.env` (Mac) or `%LOCALAPPDATA%\compute\.env` (Windows). Real environment variables override the file.

## Code tabs

The **Code** section is a coding assistant on the pool: open a tab on a project folder, pick a model and a permission mode, and describe the change. **Plan** explores read-only and proposes a plan for you to approve; **Manual** asks before every file change and command (with a diff); **Auto** edits and runs commands itself and asks only for deletions and powerful commands (`rm`, `git push`, installs, network...). Each tab has an optional FLOP budget, a Stop button, and per-turn undo for the files it changed. The tools run on your computer; what the model reads goes to the pool. Details: `docs/API.md` ("Code harness").

## Run from source (Mac)

```bash
uv sync --group dev
PYTHONPATH=src uv run python -m compute            # desktop app
uv run pytest -q                                   # 337 tests, no GPU needed
```

`PYTHONPATH=src` is needed on this Mac because uv marks `.venv` hidden and Python 3.13 skips hidden `.pth` files.

Roles (the packaged app runs the same subcommands on itself):

```bash
compute server serve --admin-email "$COMPUTE_ADMIN_EMAIL"   # host a pool: you become its admin
compute node start --url http://<server>:8765 --memory-percent 50 --models-dir ~/models  # contribute
```

Build: `scripts/build_mac.sh` → `dist/compute-mac.dmg`; on Windows `scripts/build_windows.ps1` → `dist/compute-windows.zip`. The Mac app carries uv's portable Python and runs on it instead of PyInstaller: macOS never gives an ad-hoc signed PyInstaller app Local Network access, but it does give it to the bundled Python, which System Settings lists as python3.13.
