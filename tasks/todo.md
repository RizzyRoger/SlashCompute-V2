# /compute v2 — 2-hour build

Plan: ~/.claude/plans/okay-can-we-are-distributed-gosling.md · Contract: docs/API.md · Demo: docs/DEMO.md

- [x] A. Scaffold `compute/`, port engine (no MLX) — 60 engine tests green
- [x] A. Waitlist gate hook, session_id budget param, admin authorize hook, `compute` CLI, docs/API.md
- [x] B1. Engine cross-platform: llama.cpp b12176 downloader (sha256-checked), psutil, Windows flags, memory %, CPU head, mmproj pairing, auto per-link transport
- [x] B2. Main server: admin-only start, accounts, FLOP ledger, starter, test checkout, sessions/budget, waitlist, leaderboard, DRAFT terms, admin API
- [x] B3. App shell + barebones UI + text/image attachments + /admin page + PyInstaller spec + build scripts + INSTALL.md
- [x] C. Full pytest: 160 passed (engine 82, server 30, app 48)
- [x] C. Real 2-node split on this Mac (Qwen3.5-0.8B, real llama.cpp b12176 RPC)
- [x] C. Rebuild + launch `.dmg` (28.5 MB): packaged app connected to the pool, signed in, chatted ("51"), wallet and session updated
- [ ] Windows build (`scripts/build_windows.ps1`) + 27B demo — needs your Windows PC (docs/DEMO.md)

Deferred: 300B planner rewrite, PDF/Office attachments.

## Review

Real split on one Mac (server :8775, nodes A + B at 1.6 GiB each, model needs both):
- Pipeline formed with 2 members: node A layers 0–23 (head), node B the rest, over auto transport.
- Ledger is 1:1: Carol spent 9.95e11 FLOPs = node A owner earned 6.59e11 + node B owner earned 3.36e11. Leaderboard and session budget updated.
- Waitlist: a second concurrent chat got `queued` events (position 1, ETA 2.8 s → 0.8 s) and then ran.
- Found and fixed: Qwen3.5/3.8 are reasoning models whose first tokens arrive as `reasoning_content`. The UI showed an empty reply, so it now shows the thinking until the answer starts.
- Fixed during merge: server terms/admin paths now resolve inside PyInstaller builds; `/inference/status` goes through the shared-token/admin gate; `spent_total` no longer shows `-0.0`.

Known gaps:
- Image requests aren't routed specifically to a head that has the mmproj. Fine for the demo, where the Mac holds the model and is the only possible head.
- Auto-mode latency for pairs that can't reach each other directly uses a default, not a measured relay RTT.
- Windows code paths are unit-tested only; they haven't run on real Windows.
- Builds are unsigned.
- The server and node keep running after the app window closes. That's deliberate so contributing continues; quitting them is a product decision for later.

# Code harness (file editing with Plan / Auto / Manual)

Plan: ~/.claude/plans/okay-can-you-add-jiggly-dragonfly.md · Contract: docs/API.md ("Code harness") · PRD: coding-assistant section

- [x] `app/code/`: workspace (safe paths, previewed edits), commands (risk classifier, runner), policy (mode matrix), protocol (text tool calls), harness (agent loop, approvals, persistence), routes
- [x] Shell wiring (`/api/code/*`, SHELL_GENERATION 8) and the Code section UI (`static/code.js`): session tabs, mode switch, approval card with diff, plan approval, Stop, budget, reload-safe
- [x] File-change checkpoints: per-turn undo of the assistant's edits (PRD safeguard)
- [x] Tests: workspace, commands, policy matrix, harness loop, routes (scripted model + MockTransport)
- [x] Browser check against a scripted pool; real run on a local pool with gemma-4-12B
- [ ] Windows: command classification covers cmd names, but the runner hasn't run on real Windows
