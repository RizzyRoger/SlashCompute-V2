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

### Start (admin only)
`compute server serve --admin-email E --admin-password P [--host 0.0.0.0 --port 8765 --home DIR]`
(the password may also come from env `COMPUTE_ADMIN_PASSWORD`).
- If `E` is not `config.admin_email()` (default `admin@example.com`, env `COMPUTE_ADMIN_EMAIL`), it exits with code 2 **before binding**.
- On first start it creates the admin account with `P`. Later starts must match the stored password.

### Health
`GET /health` → `{"ok": true, "server": "compute", "version": "0.2.0", "inference_nodes": int, "admin_email": str}`

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
- `POST /v1/chat/completions`, body `{model, messages, max_tokens, stream: true, session_id?}`
  - `messages[].content` may be a string, or a list of OpenAI content parts (`{"type":"text"}`, `{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}`). Images only work on `vision: true` models.
  - Errors before streaming:
    - 402 `insufficient FLOPs`
    - 402 `budget_exhausted` (the session's `max_flops` can't cover the estimate)
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
| GET/POST | `/api/settings` | `{server_url, memory_percent, models_dirs: [], contribute: bool, session_token}` |
| GET | `/api/status` | `{server: {url, reachable, local_running}, node: {running, status...}, settings}` |
| POST | `/api/discover` | finds a main server on the LAN (mDNS) and saves its URL |
| POST | `/api/server/start` | `{email, password}`: starts a local main server (admin only; error text comes from the server) |
| POST | `/api/server/stop` | – |
| POST | `/api/node/start` / `/api/node/stop` | start/stop contributing (needs a session token whose terms are accepted) |
| ANY | `/api/coord/{path}` | proxy to the main server, forwarding the auth header/cookie |
| POST | `/api/chat` | streaming proxy to `/v1/chat/completions` (SSE passthrough, including `queued` events) |
| POST | `/api/attach` | multipart `file` → `{kind: "text"\|"image", name, size, text? , data_url?}`. Extraction runs locally; nothing is sent to the pool until the user sends a message. |
