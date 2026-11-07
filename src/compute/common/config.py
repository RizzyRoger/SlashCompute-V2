"""App-wide defaults. Every value can be overridden with ``COMPUTE_<FIELD>`` (upper-case)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

MDNS_SERVICE_TYPE = "_compute._tcp.local."


def admin_email() -> str:
    """The one admin account, from ``COMPUTE_ADMIN_EMAIL`` (set it in ``.env``; see ``.env.example``).

    Empty when unset: then nobody is admin and the main server refuses to start."""
    return (os.environ.get("COMPUTE_ADMIN_EMAIL") or "").strip().lower()


def default_home() -> Path:
    """``%LOCALAPPDATA%\\compute`` on Windows, ``~/.compute`` elsewhere."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "compute"
    return Path.home() / ".compute"


@dataclass
class EngineConfig:
    home: Path = field(default_factory=default_home)
    coordinator_host: str = "0.0.0.0"
    coordinator_port: int = 8765

    # Set on a server reachable over the internet; a LAN host leaves these alone.
    public_pool: bool = False
    public_url: str = ""

    @classmethod
    def from_env(cls, **overrides) -> "EngineConfig":
        cfg = cls()
        for f in fields(cls):
            env = os.environ.get(f"COMPUTE_{f.name.upper()}")
            if env is None:
                continue
            cur = getattr(cfg, f.name)
            if isinstance(cur, bool):
                val = env.lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, Path):
                val = Path(env).expanduser()
            else:
                val = type(cur)(env)
            setattr(cfg, f.name, val)
        for k, v in overrides.items():
            if v is not None:
                setattr(cfg, k, v)
        return cfg

    @property
    def coordinator_dir(self) -> Path:
        return self.home / "server"
