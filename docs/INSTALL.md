# Installing /compute

/compute is one desktop app for Mac and Windows. It lets you chat with a shared LLM and, if you want, lend part of your computer's memory to the pool to earn FLOPs.

## Download

| Platform | File | Requirements |
|---|---|---|
| macOS (Apple Silicon) | `compute-mac.dmg` | macOS 12 or later |
| Windows 10/11 (64-bit) | `compute-windows.zip` | Microsoft Edge WebView2 Runtime (see below) |

The builds are **not code-signed yet**, so your computer will warn you the first time you open the app. The steps below get past that warning.

## macOS

1. Open `compute-mac.dmg` and drag **compute** into **Applications**.
2. The first time, **right-click (or Control-click) compute in Applications and choose Open**, then click **Open** in the dialog.
   - If macOS only offers "Move to Trash" or "Done": open **System Settings → Privacy & Security**, scroll to the message about "compute" and click **Open Anyway**, then confirm.
3. If macOS asks whether **python3.13** may **find devices on your local network**, click **Allow**. That's how it finds a pool on your LAN. /compute runs on its own copy of Python, so that's the name macOS shows, both in the question and under **System Settings → Privacy & Security → Local Network**.
4. If macOS asks whether compute may **accept incoming network connections**, click **Allow**. Pool members connect to each other directly.

## Windows

1. Right-click `compute-windows.zip` → **Extract All...** and pick a folder you keep (for example `C:\Users\you\Apps`). Don't run it from inside the zip.
2. Open the extracted `compute` folder and double-click **compute.exe**.
3. **SmartScreen** may say "Windows protected your PC". Click **More info**, then **Run anyway**.
4. **Windows Defender Firewall** will ask about network access. Allow **Private networks** (and Public only if your pool runs over a public network).
5. **WebView2**: the app window uses the Microsoft Edge WebView2 Runtime. It's already installed on Windows 11 and on most up-to-date Windows 10 PCs. If the window stays blank or the app says WebView2 is missing, install the "Evergreen Bootstrapper" from <https://developer.microsoft.com/microsoft-edge/webview2/> and open compute again.

## First run

1. **Connect**: nothing to do on a local network. The app finds the main server over mDNS and joins its pool by itself, and follows it if its address changes (for example when the host switches Wi-Fi), so everyone on the network is in the same pool. If the network has more than one main server, every app picks the one that started first, and the app won't start a second one. For a server off your network, type the address you were given (for example `192.168.1.10:8765`). That turns auto-join off until you click *Join this network's pool*.
2. **Account**: register (or sign in), then read and accept the DRAFT terms.
3. **Contribute** (optional): pick how much memory to lend, add the folders where you keep `.gguf` models, and click *Start contributing*.
   - The first time you contribute, the app **downloads llama.cpp** for your platform (tens of MB) into its data folder. This takes a minute; later starts are quick.
4. **Chat**: pick a model, set a FLOP budget for the session, and send a message. If the pool is busy you'll see your place in line.

Files you attach in chat are sent to other people's computers in the pool to answer your message. Don't attach anything you wouldn't share.

## Where things are stored

| | macOS | Windows |
|---|---|---|
| Settings, logs, downloaded llama.cpp | `~/.compute` | `%LOCALAPPDATA%\compute` |
| Logs | `~/.compute/logs` | `%LOCALAPPDATA%\compute\logs` |

To uninstall, delete the app (and the data folder above if you want to remove everything).

## Troubleshooting

- **"Port 8766 is in use"**: another copy of compute is still running. Quit it (check the menu bar / system tray or Activity Monitor / Task Manager) and open again.
- **Can't find the pool on the LAN**: make sure you allowed local network access (Mac) or private network access (Windows firewall), or type the server address by hand.
- **Contributing stops right away**: check that you're signed in and accepted the terms; details are in `logs/node.log`.
