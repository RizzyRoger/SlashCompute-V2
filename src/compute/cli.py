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


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    role = args[0] if args else "app"
    rest = args[1:]
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
