"""``compute app``: a native window (pywebview) around the local shell on 127.0.0.1:8766."""

from __future__ import annotations

import html
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import uvicorn

from compute.app.shell import SHELL_GENERATION, SHELL_HOST, SHELL_PORT, create_shell

WINDOW_TITLE = "/compute"
WINDOW_W = 1200
WINDOW_H = 820


def shell_url() -> str:
    return f"http://{SHELL_HOST}:{SHELL_PORT}"


def port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def running_generation(url: str) -> int:
    """Generation of a /compute shell already on the port (0 = not ours / not answering)."""
    try:
        r = httpx.get(f"{url}/api/shell", timeout=0.6)
        data = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return 0
    if not isinstance(data, dict) or data.get("app") != "compute":
        return 0
    try:
        return int(data.get("generation", 0))
    except (TypeError, ValueError):
        return 0


def serve() -> None:
    uvicorn.run(create_shell(), host=SHELL_HOST, port=SHELL_PORT, log_level="warning")


def ensure_shell(wait_s: float = 10.0) -> str:
    """Start the shell in a background thread unless an up-to-date one already answers."""
    url = shell_url()
    if running_generation(url) >= SHELL_GENERATION:
        return url
    if port_open(SHELL_HOST, SHELL_PORT):
        raise SystemExit(f"Port {SHELL_PORT} is in use (an older /compute?). Quit it and open again.")
    threading.Thread(target=serve, daemon=True, name="compute-shell").start()
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if running_generation(url) >= SHELL_GENERATION:
            return url
        time.sleep(0.1)
    raise SystemExit(f"The app shell did not start at {url}")


def folder_chooser(window: Any) -> Callable[[str], Optional[str]]:
    """``pywebview.api.choose_folder(start)`` for the UI: the system folder picker (Finder on macOS,
    File Explorer on Windows). Returns the chosen folder's full path, or None if cancelled."""

    def choose_folder(start: str = "") -> Optional[str]:
        import webview

        kind = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
        # No start folder: the OS opens where the user last was.
        picked = window.create_file_dialog(kind, directory=start if start and Path(start).is_dir() else "")
        return str(picked[0]) if picked else None

    return choose_folder


def open_window(url: str) -> None:
    import webview

    window = webview.create_window(WINDOW_TITLE, url, width=WINDOW_W, height=WINDOW_H)
    window.expose(folder_chooser(window))
    webview.start()


def show_error(message: str) -> None:
    """A windowed build has no terminal: show startup errors in a small window instead."""
    import webview

    body = f"<p style='font-family: system-ui; padding: 1rem'>{html.escape(message)}</p>"
    webview.create_window(WINDOW_TITLE, html=body, width=520, height=200)
    webview.start()


def main() -> None:
    try:
        url = ensure_shell()
    except SystemExit as e:
        show_error(str(e))
        raise
    open_window(url)


if __name__ == "__main__":
    main()
