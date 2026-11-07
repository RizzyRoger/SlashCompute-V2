"""``compute server``: run the main server. Only the admin account (``config.admin_email()``) may start it."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from compute.common.config import EngineConfig, admin_email
from compute.common.logging import setup_logging

app = typer.Typer(no_args_is_help=True, help="/compute main server (admin only)")
EXIT_REFUSED = 2


def check_admin(cfg: EngineConfig, email: str, password: str) -> Optional[str]:
    """Create the admin on first start, verify the password afterwards. Returns an error message or None."""
    from compute.server.auth import Auth, AuthError, normalize_email
    from compute.server.db import Database, database_path

    if not admin_email():
        return "no admin account is configured: set COMPUTE_ADMIN_EMAIL in .env (see .env.example)"
    if normalize_email(email) != admin_email():
        return "only the admin account can start the main server"
    if not password:
        return "the admin password is required (--admin-password or COMPUTE_ADMIN_PASSWORD)"
    db = Database(database_path(cfg.home))
    try:
        Auth(db).ensure_admin(email, password)
    except AuthError as e:
        return str(e)
    finally:
        db.close()
    return None


@app.command()
def serve(
    admin_email_opt: str = typer.Option(..., "--admin-email", help="The admin account's email"),
    admin_password: str = typer.Option("", "--admin-password", envvar="COMPUTE_ADMIN_PASSWORD",
                                       help="The admin password (set on first start, checked afterwards)"),
    host: Optional[str] = typer.Option(None, help="Bind address (default 0.0.0.0)"),
    port: Optional[int] = typer.Option(None, help="Port (default 8765)"),
    home: Optional[Path] = typer.Option(None, help="State directory (default ~/.compute)"),
    mdns: bool = typer.Option(True, help="Advertise on the LAN via mDNS"),
):
    """Run the main server."""
    cfg = EngineConfig.from_env(coordinator_host=host, coordinator_port=port, home=home)
    error = check_admin(cfg, admin_email_opt, admin_password)
    if error:
        typer.echo(f"compute server: {error}", err=True)
        raise typer.Exit(EXIT_REFUSED)

    import uvicorn

    from compute.server.app import create_app

    setup_logging("server")
    uvicorn.run(create_app(cfg, advertise=mdns), host=cfg.coordinator_host, port=cfg.coordinator_port,
                log_level="warning", ws_ping_interval=20, ws_max_size=64 * 1024 * 1024)


@app.command()
def version():
    """Print the server version."""
    from compute.server.app import VERSION

    typer.echo(VERSION)


if __name__ == "__main__":
    app()
