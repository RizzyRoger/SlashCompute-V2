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

# Landing-page restyle (app + admin)

Plan: ~/.claude/plans/https-midnight-mac-showcase-lovable-app-tranquil-flurry.md · Design source: github.com/revantashokraj-cmyk/midnight-mac-showcase (`public/reference/restyled-native-app.html`, commit 5c7f056)

- [x] `static/app.css`: the design team's stylesheet verbatim (it targets our `index.html` as of `ff5b730`), texture served from `/static`, plus dark rules for the folder picker and the Code folder field, which landed after their snapshot
- [x] Bundled fonts: Space Grotesk + JetBrains Mono variable woff2 (latin + latin-ext, OFL 1.1, licences in `fonts/`) from `@fontsource-variable/*@5.3.0`; works offline
- [x] Admin: inline styles moved to `server/admin/admin.css` in the same design (stat tiles for the overview, sideways-scrolling tables); own copy of fonts and texture because `/admin/static` only serves `server/admin/`
- [x] No HTML/JS/Python changes apart from the admin `<link>`; `SHELL_GENERATION` unchanged
- [x] Tests: 383 passed
- [x] Browser check on a scratch pool (own `COMPUTE_HOME`, ports 8775/8776): every app section signed in, the in-app folder picker, admin sign-in, overview, tables and upload form, at 1024 px and 600 px; fonts load and no console errors
- [ ] Not seen inside the pywebview window or a rebuilt `.dmg`/Windows build

## Review

- The marketing website stays in the Lovable repo. Its copy still says file editing isn't available yet, which Code tabs have since changed.
- When markup changes, add matching rules to `app.css` / `admin.css` in the same style instead of new light-theme rules.

# One pool per network (auto-join over mDNS)

Plan: ~/.claude/plans/okay-on-the-main-atomic-flamingo.md · Builds on `1c232c6` (one section per tab, already on main) and #4

- [x] `common/discovery.py`: `LanServer`, `choose_pool` (earliest `started`, then name), `LanBrowser` (long-lived mDNS browser), TXT `started`/`version`; `COMPUTE_MDNS_TYPE` override for test pools
- [x] Server advert via AsyncZeroconf inside the server's loop (the blocking one raised `EventLoopBlocked` there, so no goodbye was ever sent), 120 s TTL, re-advertises when the host changes network
- [x] App: `auto_join` setting (default on), `follow_pool()` on every `/api/status`, node drains and restarts on the current pool, app refuses to start a second server on the network, browser restarts on a network change; `SHELL_GENERATION` 10
- [x] Connect tab: "Join this network's pool", "(this network's pool)" / "(set by hand)"; account, models and leaderboard reload once whenever the server changes
- [x] Tests: 406 passed
- [x] Real mDNS on this Mac (test service type, scratch homes, ports 8775/8785/8776): auto-join with no clicks; two servers → app stays on the earlier; stopping it → app moves at once; second server refused in the UI; typed address stays; rejoin works; the Mac changed networks mid-test, which is how the re-advertise gap was found; after the fix the app followed the server to its new address
- [ ] Not seen: two physical machines, Windows (mDNS/firewall), the pywebview window or a rebuilt `.dmg`; a real node moving pools (unit-tested only)

## Review

- Everyone on the network needs this version for auto-join; older apps can still use the button (same service type), and older servers (no `started`) are still found, sorted last.
- zeroconf's "No route to host" on a VPN tunnel is harmless, but on the Wi-Fi/Ethernet address it means macOS Local Network access is off for the app: nothing on the LAN works. The app now detects this (`local_network_blocked`) and says how to fix it.

# Host warning when its server isn't visible

- [x] `status().server.advertised` (own server in own mDNS browse, 10 s grace) and a red Connect-tab warning with the Mac/Windows fix and the address to type
- [x] Tests: 410 passed; browser check with a scratch server, mDNS off (warning shows) then on (warning clears)
