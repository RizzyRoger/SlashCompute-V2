# /compute v2: API contract

The three parts of the app talk over HTTP. This file is the contract between them. Keep it in sync with the code.

```
desktop window (pywebview) → app shell 127.0.0.1:8766 (/api/*) → main server :8765 (everything else)
                                                                ↑
                                       nodes (compute node) register and heartbeat here
```

Units: every FLOPs amount is a **float of weighted FLOPs** (FLOP-equivalents; see `engine/flops.py`). 1 PFLOP = 1e15.
Auth: the main server accepts `Authorization: Bearer <session token>` (desktop app and nodes) or the `compute_session` cookie (the `/admin` page in a browser). Errors look like `{"detail": "..."}` with a real HTTP status.

---

## Main server (`compute server serve`, default port 8765)

### Start (anybody can host)
`compute server serve --admin-email E --admin-password P [--host 0.0.0.0 --port 8765 --home DIR]`
(the password may also come from env `COMPUTE_ADMIN_PASSWORD`).
- Anybody can host: `E` becomes this server's admin (it overrides any `COMPUTE_ADMIN_EMAIL` in `.env`). A missing email or password, or the wrong password for an existing account, exits with code 2 **before binding**.
- On first start it creates the admin account with `P`. Later starts must match the stored password.

### Health
`GET /health` → `{"ok": true, "server": "compute", "version": "0.2.0", "inference_nodes": int}` (never reveals the admin email)

### Accounts
| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/auth/register` | `{email, password, name}` | `{user, token}` and sets the cookie. New users get the `starter` grant. |
| POST | `/auth/login` | `{email, password}` | `{user, token}` and sets the cookie. 403 if banned. |
| POST | `/auth/logout` | – | `{ok}` |
| GET | `/auth/me` | – | `user` |
| PATCH | `/auth/me` | `{name}` | `user` (display name is the only editable field) |
| GET | `/terms` | – | `{version, text}` (DRAFT terms, `version` = hash of the text) |
| POST | `/auth/terms` | `{version}` | `user` (the version must match the current one) |

`user` = `{id, email, name, is_admin, banned, flagged, accepted_terms: bool, terms_version}`

A node registering with a session token whose user hasn't accepted the current terms gets **403 `terms_required`**. Banned users get 403 everywhere.

### Wallet (FLOPs)
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/wallet` | – | `{balance, unlimited, reserved, earned_total, earned_week, spent_total, purchased_total}`. For the admin: `unlimited: true` and `balance: null`. |
| GET | `/wallet/transactions?limit=50` | – | `[{id, kind, flops, ts, note}]`, newest first. `kind` ∈ `starter, earn, spend, purchase, admin_credit, refund`. |
| GET | `/wallet/live` | – | `{earning_flops_per_s, earned_total, nodes: [{node_id, name, online, flops_total}]}` (the user's own nodes; for the live monitor) |
| POST | `/wallet/checkout` | `{pflops}` | `{receipt: {id, pflops, flops, price_usd, test_mode: true}, balance}`. **Fake checkout, TEST MODE**: no external service. |

Rules:
- Earning is 1:1: the weighted FLOPs each node computes go to its owner as `earn`.
- The requester is charged the same total as `spend`.
- The admin is never charged, but contributors still earn from the admin's requests.
- `STARTER_FLOPS` (env `COMPUTE_STARTER_FLOPS`) ≈ 20 short replies on Qwen3.8-27B.

### Chat sessions (FLOP budget per session)
| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/sessions` | `{model, max_flops}` | `{id, model, max_flops, spent, created}` |
| GET | `/sessions/{id}` | – | same shape |

### Chat (OpenAI-compatible, mounted by the engine)
- `GET /v1/models` → `{data: [{id, arch, moe, size_gb, heads, vision}]}`
- `POST /v1/chat/completions`, body `{model, messages, max_tokens?, stream: true, session_id?}`
  - `messages[].content` may be a string, or a list of OpenAI content parts (`{"type":"text"}`, `{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}`). Images only work on `vision: true` models.
  - `max_tokens` is the most the reply may generate, and the server reserves FLOPs for all of it. Leave it out for no limit: the reply may then use the rest of its context window, the smallest context (4096, doubling) that leaves it at least 1024 tokens. That's about 4,000 tokens for a new chat. When your balance or the session's `max_flops` can't cover the limit, the server shortens it to the largest value they can cover, but not below 256. The desktop app sends no `max_tokens`, so reasoning models can think and then answer; its Stop button ends a reply early.
  - Errors before streaming:
    - 402 `insufficient FLOPs` (can't cover even a 256-token reply)
    - 402 `budget_exhausted` (the session's `max_flops` can't cover even a 256-token reply)
    - 403 banned
    - 404 unknown model
    - 503 no capacity
  - SSE stream, one `data:` JSON per line:
    - `{"queued": {"queue_id", "position", "eta_s"}}`: zero or more while waiting (position 1 = next)
    - an OpenAI `chat.completion.chunk` with `choices[0].delta.content`
    - final: `{"choices": [], "usage": {prompt_tokens, completion_tokens}, "network": {job_id, flops, members: [...], ...}}`
    - error: `{"error": {"message", "retryable"}}`
    - `[DONE]`

### Waitlist
- `GET /queue` → `{"models": {model_id: [{queue_id, position, eta_s, mine: bool}]}}`
- `DELETE /queue/{queue_id}`: cancel your own queued request (the admin can cancel any). The SSE stream then ends with an error event, `"cancelled"`.

### Leaderboard
`GET /leaderboard?window=all|week` → `[{rank, name, flops}]` (sum of `earn`; display names only, never emails)

### Admin (admin session required; 403 otherwise)
- `GET /admin` → the HTML page (files in `src/compute/server/admin/`)
- `GET /admin/api/users` → `[{id, email, name, balance, earned_total, spent_total, flagged, flag_reason, banned, created}]`
- `POST /admin/api/users/{id}/credit` `{flops, note}` → `{ok, balance}` (an `admin_credit` transaction)
- `POST /admin/api/users/{id}/ban` `{banned: bool}` → `{ok}`
- `POST /admin/api/users/{id}/flag` `{flagged: bool, reason}` → `{ok}`
- `GET /admin/api/overview` → `{users, nodes_online, flops_earned_total, flops_purchased_total, queue_length}`
- Engine routes also accept the admin session (as well as the shared token):
  - `GET /inference/status`
  - `POST /inference/models/upload?name=`
  - `DELETE /inference/models/{name}`
  - `POST /inference/pipelines/{id}/stop`

### Engine (nodes ↔ server, unchanged from SlashCompute)
`/inference/nodes/register|heartbeat|commitment|benchmark|models|latency`, `/inference/agent/commands`, `/inference/agent/jobs/{id}/stream`, relay WebSockets `/inference/relay/*`. See `engine/coordinator/service.py`.

---

## Node (`compute node`)
`compute node start --url http://HOST:8765 --memory-percent 50 --models-dir DIR [--models-dir DIR2] --session-token T [--head/--no-head]`
- `--memory-percent` = percent of GPU memory (Windows GPU), unified memory (Mac) or RAM (CPU only) lent to the pool.
- On first run the node downloads the pinned llama.cpp (`b12176`) for its platform into `<home>/llama.cpp/`.
- `compute node status` / `compute node stop` work as before. Stop drains the current request, then exits.

---

## App shell (`compute app`, 127.0.0.1:8766; serves `app/static/`)
| Method | Path | Notes |
|---|---|---|
| GET | `/` | barebones UI |
| GET/POST | `/api/settings` | `{server_url, memory_percent, models_dirs: [], contribute: bool, session_token, auto_join: bool}`. `auto_join` (default true) follows this network's pool; the UI sets it false when you type an address. |
| GET | `/api/status` | `{server: {url, reachable, local_running, lan_url, advertised, health, auto_join, lan: [{url, name}]}, node: {running, status...}, settings, last_error}`. Each call also joins this network's pool (see below). `advertised`: whether this computer's own main server shows up in its own mDNS browse (true/false; null when none is running or it started under 10 s ago). The Connect tab warns when it's false. |
| POST | `/api/discover` | turns `auto_join` back on and joins this network's pool, waiting up to 5 s for an advert; 404 if none |
| POST | `/api/server/start` | `{email, password}`: starts a local main server; whoever starts it is its admin (error text comes from the server). Refused when another main server is already advertised on the LAN: one pool per network. |
| POST | `/api/server/stop` | – |
| POST | `/api/node/start` / `/api/node/stop` | start/stop contributing (needs a session token whose terms are accepted) |
| ANY | `/api/coord/{path}` | proxy to the main server, forwarding the auth header/cookie |
| POST | `/api/chat` | streaming proxy to `/v1/chat/completions` (SSE passthrough, including `queued` events) |
| POST | `/api/attach` | multipart `file` → `{kind: "text"\|"image", name, size, text? , data_url?}`. Extraction runs locally; nothing is sent to the pool until the user sends a message. |
| GET | `/api/folders?path=&hidden=` | Fallback folder picker. In the app window, "Choose folder..." (Code tab project folder, model folders) opens the system dialog (Finder / File Explorer) through `pywebview.api.choose_folder(start)` (`app/main.py`); in a plain browser the UI browses with this endpoint instead: `{path, name, parent, crumbs: [{name, path}], folders: [{name, path}], truncated, places: [{name, path}]}`. Sub-folders only (no files), sorted by name, hidden ones (dot-folders, OS-hidden) only with `hidden=true`, at most 2000. No `path` = the home folder. 400 relative path, 403 no permission, 404 not a folder. |

**One pool per network.** The main server advertises `_compute._tcp.local.` over mDNS with TXT `started` (unix seconds) and `version`. The shell browses for it all the time, and on every `/api/status` with `auto_join` on it switches `server_url` to the advertised server that started first (ties: by service name; adverts without `started` sort last). Every app on the network therefore picks the same pool. If nothing is advertised it keeps the saved address. Adverts use a 120 s TTL and the server sends goodbyes when it stops, so apps drop a stopped server at once and a crashed one within about 2 minutes. When a computer's LAN address changes (another Wi-Fi, a hotspot), the server re-advertises on the new network every 5 s check with the same `started`, and the app's browser starts over on its new network. A running node whose `--url` differs from `server_url` is asked to stop and, once it has exited, started again on the current pool. Set `COMPUTE_MDNS_TYPE` (e.g. `_compute-test._tcp.local.`) on a test server and app to keep a test pool apart from everyone else's.

---

## Code harness (`/api/code/*` on the app shell)

A Code tab lets a pool model read and edit files in a folder you choose. The tools run **on this computer** (`src/compute/app/code/`); only the model's prompts (including file text it reads and command output) go to the pool. Each tab is a session with its own folder, model, permission mode, optional FLOP budget and transcript, saved in `<home>/code/<id>.json` (tabs survive a restart).

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/code/sessions` | – | `{sessions: [meta]}` |
| POST | `/api/code/sessions` | `{root, model, mode?, budget_pflops?, title?, run_commands?}` | `meta`. `root` must be an existing folder (not `/`, the home folder, or the /compute home). `budget_pflops > 0` opens a server FLOP session (`POST /sessions`) that caps the tab's spending. |
| GET | `/api/code/sessions/{id}?after=N` | – | snapshot: `{session: meta, entries[seq > N], partial, state, activity, mode, pending, plan, queue, spent_flops, budget_flops, checkpoints}` |
| PATCH | `/api/code/sessions/{id}` | `{mode?, title?, model?, budget_pflops?, run_commands?}` | `meta`. A mode change applies to the next tool call. `run_commands` (default false) lets Auto run normal commands without asking. |
| DELETE | `/api/code/sessions/{id}` | – | `{ok}` (stops the turn, deletes the transcript) |
| POST | `/api/code/sessions/{id}/messages` | `{text}` | `meta`; starts a turn (409 while one runs) |
| POST | `/api/code/sessions/{id}/approval` | `{id, approve, note?}` | `{ok}`; answers the pending approval (409 if none or out of date) |
| POST | `/api/code/sessions/{id}/plan` | `{action: auto\|manual\|revise, note?}` | `meta`; approves the proposed plan (switching mode) or asks for changes |
| POST | `/api/code/sessions/{id}/revert` | `{turn}` | the `revert` entry; puts files the assistant changed from that message onward back (409 while running) |
| POST | `/api/code/sessions/{id}/stop` | – | `meta`; cancels the turn (kills a running command) |
| GET | `/api/code/sessions/{id}/events?after=N` | – | SSE: `{entry}`, `{partial: {text, thinking}}`, `{state: {...}}`, `{ping}` while waiting, `{done}` once the tab is idle |

`meta` = `{id, root, model, mode, title, created, state: idle|running|waiting, activity, spent_flops, budget_flops, flop_session_id, pending: bool, plan: bool, entries}`.
Entry kinds: `user`, `assistant {text, thinking, flops}`, `tool {tool, target, status: auto|approved|rejected|denied|error, reason, output, diff, risk}`, `plan`, `mode`, `revert`, `notice`, `error`.

### Permission modes

| Tool | Plan | Manual | Auto |
|---|---|---|---|
| `list_files`, `read_file`, `search` | allow | allow | allow |
| read a secrets file (`.env*`, `*.pem`, `*.key`, `id_*` keys) | ask | ask | ask |
| `write_file`, `edit_file` | deny | ask (diff shown) | allow (ask for secrets files) |
| `delete_file` | deny | ask | ask |
| `run_command`, read-only (`ls`, `cat`, `grep`, `git status/diff/log`...) | allow | ask | allow |
| `run_command`, normal (`pytest`, `npm test`, `mkdir`, `python x.py`...) | deny | ask | ask; allow if the tab has `run_commands` on |
| `run_command`, powerful | deny | ask | ask |
| `propose_plan` | ends the turn, shows the plan | deny | deny |

Always refused: paths outside the folder (symlinks included) and writes inside `.git`. **Powerful** commands (`code/commands.py`): deleting (`rm`, `del`), `sudo`, permission changes, killing processes, network tools (`curl`, `ssh`, `rsync`...), containers, opening apps or changing system settings, `git push / reset --hard / clean / rebase / checkout -- / branch -D`, installing or publishing packages, `$(...)` and backticks, `eval`/`xargs`, piping into a shell or interpreter, inline code (`python -c`), background jobs (`&`), any argument that points outside the folder, `VAR=value` prefixes, `printenv` and bare `env`, arguments naming a secrets file, options that make a read-only tool start a program (`rg --pre`, `fd -x`, `sort --compress-program`, `git -c` / `--config-env` / `--exec-path`, `git grep -O`), and git network commands (`fetch`, `pull`, `clone`, `ls-remote`, `archive`). The model's replies come from pool computers, so Auto only runs normal commands without asking when the tab opts in. A command line is split on `&& || ; |` and its most severe part decides. Commands run in the folder with a 2-minute limit, no input, a login-shell `PATH`, and without `COMPUTE_*` variables.

### Tool-call protocol

The model writes one call per reply as text, so any pool model can use it:

```
Fixing the sign.
<tool name="edit_file"><path>app.py</path><old>return a - b</old><new>return a + b</new></tool>
```

Requests carry `stop: ["</tool>"]` (llama-server stops right after the call), `max_tokens: 2048` and the tab's `session_id`. Results come back as a user message `<tool_result name="...">...</tool_result>`. Single-value tools (`propose_plan`, `run_command`, `read_file`, `list_files`, `delete_file`, `search`) also accept the value written straight inside the tool tag; a malformed call gets an error showing the exact format. A reply without a call ends the turn (an empty reply first gets one nudge); a turn stops after 30 tool calls. Older tool output is shortened once the history passes about 40k characters.

### Checkpoints

Every edit, new file or deletion made through the tools is recorded per turn (the file's earlier bytes). `revert` restores everything changed from a given message onward, newest first, reports files that were edited outside the assistant in the meantime, and tells the model in its next request. Changes made by shell commands aren't tracked.
