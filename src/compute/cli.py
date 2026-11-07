"""One entry point for every role, so a frozen app (PyInstaller) can re-launch itself.

    compute               open the desktop app (default)
    compute app           same
    compute server ...    the main server (admin only)  -> compute.server.main
    compute node ...      a contributor node            -> compute.engine.node.__main__
"""

from __future__ import annotations

import sys


def self_command(*args: str) -> list[str]:
    """argv that re-runs this program with a subcommand: the frozen exe, or ``python -m compute``."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "compute", *args]


def ensure_stdio(role: str) -> None:
    """A windowed PyInstaller exe on Windows starts with ``sys.stdout``/``sys.stderr`` set to None, and
    uvicorn/typer/logging crash on the first write or ``isatty()``. Point the missing streams at a log file."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from compute.common.config import default_home

    log_dir = default_home() / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / f"{role}-console.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        import os

        stream = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def main(argv: list[str] | None = None) -> None:
    from compute.common.env import load_env

    load_env()   # .env secrets/settings (admin email, passwords, pool token); real env vars win
    args = list(sys.argv[1:] if argv is None else argv)
    role = args[0] if args else "app"
    rest = args[1:]
    ensure_stdio(role if role in ("app", "server", "node") else "app")
    if role == "server":
        from compute.server.main import app as server_app
        server_app(rest, prog_name="compute server")
    elif role == "node":
        from compute.engine.node.__main__ import app as node_app
        node_app(rest, prog_name="compute node")
    elif role == "app":
        from compute.app.main import main as app_main
        app_main()
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
