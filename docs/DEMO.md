# Demo checklist: Qwen3.8-27B split across a Mac and a Windows PC

Goal: prove one model too big for either machine runs split across both, with FLOPs moving correctly.

| Machine | Role | Holds |
|---|---|---|
| Mac (M1 Pro, 16 GB) | main server + node (head) | `Qwen3.8-27B-UD-Q4_K_XL.gguf` (17.6 GB) + `mmproj-F16.gguf` |
| Windows PC | node (worker) | nothing (receives layers over RPC) |

The 27B model is 17.6 GB, so neither a 16 GB Mac nor a typical gaming GPU can hold it alone. The planner must split it.

## 0. Before the demo
- [ ] Both machines on the same Wi-Fi/LAN (Ethernet is better). Internet joining also works, via relay, but slowly.
- [ ] Mac: `compute-mac.dmg` installed (right-click → Open the first time).
- [ ] Windows: unzip `compute-windows.zip` and run `compute\compute.exe` (SmartScreen: More info → Run anyway). Allow it through Windows Firewall on **private** networks when asked.
- [ ] Both machines: first launch downloads llama.cpp b12176 (Mac: Metal; Windows: CUDA if `nvidia-smi` works, otherwise Vulkan or CPU). Wait for it to finish.

## 1. Mac: start the main server (admin)
1. Open the app → **Connect** → *Start main server*.
2. Sign in with `admin@example.com` and the admin password you choose (any other email is refused).
3. **Account**: you're signed in as admin; **Wallet** shows *unlimited*.
4. **Contribute**: add the models folder
   `~/.cache/huggingface/hub/models--unsloth--Qwen3.8-27B-GGUF/snapshots/4ca720788d1e01f1bff70c033e0d0028fd02e502`
   then set memory to about **60%**, accept the terms, and click *Start contributing*.

## 2. Windows: join
1. Open the app → **Connect** → *Find on LAN* (or type `http://<mac-ip>:8765`).
2. **Account** → register a normal account. It starts with the starter balance of about 20 replies.
3. Accept the terms → **Contribute** → set memory to about **70%** → *Start contributing*.

## 3. Chat
1. On either machine → **Chat** → model `Qwen3.8-27B-UD-Q4_K_XL` (it shows as vision-capable).
2. Set a session budget, e.g. 1 PFLOP. Ask something short.
3. Expected: the first reply takes a while (both machines load layers). The final line shows the FLOPs cost.
4. On the Mac, open `http://localhost:8765/admin` → **Pipelines** shows **2 members** (Mac head + Windows worker) with a layer split.
5. Attach an image (`.png`/`.jpg`) and ask about it. You get a one-time privacy notice; then the reply describes the image.

## 4. Check the FLOPs moved
- [ ] The Windows user's **Wallet**: balance went down by the reply's FLOPs.
- [ ] Each contributor's **earned** went up in proportion to the layers they served, and the totals match (1:1).
- [ ] **Leaderboard** (all-time / this week) lists both contributors.
- [ ] Admin page: credit the Windows user 10 PFLOP, and their balance updates.
- [ ] Wallet → TEST MODE buy 5 PFLOP, and the balance updates (no real payment).

## 5. Waitlist
- [ ] Send two chats at once (Mac and Windows). The second shows *queued, position 1, ETA …* and can be cancelled.

## If it goes wrong
- **"no capacity" / no pipeline:** neither node has enough memory lent. Raise the memory %, or check that the Mac's node lists the 27B model (admin page → models).
- **Windows can't be reached directly:** the link falls back to relay through the main server automatically, which is slower but works.
- **Logs:** `~/.compute/logs/` (Mac) and `%LOCALAPPDATA%\compute\logs\` (Windows).
