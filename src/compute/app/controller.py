"""Settings plus start/stop of the local main server and the contributor node.

Every role is spawned with ``compute.cli.self_command`` so a frozen build re-launches itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import psutil

from compute.cli import self_command
from compute.common.config import EngineConfig, default_home
from compute.common.discovery import discover, lan_ip

SERVER_START_WAIT_S = 20.0
NODE_EARLY_EXIT_WAIT_S = 1.5
LOG_TAIL_CHARS = 1500
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


class ControllerError(Exception):
    """User-facing start/stop failure; the message is shown in the UI."""


@dataclass
class AppSettings:
    server_url: str = ""
    memory_percent: int = 50
    models_dirs: list[str] = field(default_factory=list)
    contribute: bool = False
    session_token: str = ""

    @classmethod
    def from_dict(cls, raw: Any) -> "AppSettings":
        if not isinstance(raw, dict):
            return cls()
        dirs = raw.get("models_dirs", [])
        return cls(
            server_url=normalize_url(str(raw.get("server_url") or "")),
            memory_percent=clamp_percent(raw.get("memory_percent", 50)),
            models_dirs=clean_dirs(dirs if isinstance(dirs, list) else []),
            contribute=bool(raw.get("contribute", False)),
            session_token=str(raw.get("session_token") or "").strip(),
        )


def clamp_percent(value: Any, default: int = 50) -> int:
    try:
        return max(1, min(100, int(float(value))))
    except (TypeError, ValueError):
        return default


def clean_dirs(dirs: list[Any]) -> list[str]:
    cleaned = [str(d).strip() for d in dirs if str(d or "").strip()]
    return list(dict.fromkeys(cleaned))


def normalize_url(url: str, port: int = 8765) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        host = u.split("/")[0]
        u = f"http://{u}" if ":" in host else f"http://{host}:{port}{u[len(host):]}"
    return u.rstrip("/")


def spawn_kwargs() -> dict[str, Any]:
    """Detach children from the app: own process group, no console window on Windows."""
    if sys.platform == "win32":
        return {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}
    return {"start_new_session": True}


def run_kwargs() -> dict[str, Any]:
    """For short helper commands (``compute node stop``): no console flash on Windows."""
    if sys.platform == "win32":
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def child_env(extra: dict[str, str]) -> dict[str, str]:
    """Environment for a spawned role. From source, make ``python -m compute`` importable even when
    the editable install's .pth is skipped (macOS + Python 3.13 ignore hidden .pth files)."""
    env = {**os.environ, **extra}
    if getattr(sys, "frozen", False):
        return env
    src = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    return env


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        if not psutil.pid_exists(pid):
            return False
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def tail(path: Path, since: int = 0, chars: int = LOG_TAIL_CHARS) -> str:
    """The end of what a process wrote to its log after byte offset ``since``."""
    try:
        with open(path, "rb") as fh:
            fh.seek(since)
            return fh.read().decode(errors="replace")[-chars:].strip()
    except OSError:
        return ""


def exit_message(code: Optional[int], log_tail: str) -> str:
    if code == 2:
        head = "Only the admin account can start the main server."
    else:
        head = f"The main server stopped (exit code {code})."
    return f"{head}\n{log_tail}" if log_tail else head


class Controller:
    def __init__(
        self,
        home: Optional[Path] = None,
        popen: Callable[..., Any] = subprocess.Popen,
        run: Callable[..., Any] = subprocess.run,
        http: Optional[httpx.Client] = None,
        discover_fn: Callable[[float], Optional[str]] = discover,
        lan_ip_fn: Callable[[], str] = lan_ip,
        server_wait_s: float = SERVER_START_WAIT_S,
        node_wait_s: float = NODE_EARLY_EXIT_WAIT_S,
    ) -> None:
        self.home = Path(home) if home is not None else default_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.server_port = EngineConfig.from_env().coordinator_port
        self._popen = popen
        self._run = run
        self._http = http or httpx.Client(timeout=2.0)
        self._discover = discover_fn
        self._lan_ip = lan_ip_fn
        self._server_wait_s = server_wait_s
        self._node_wait_s = node_wait_s
        self._server_proc: Any = None
        self._log_start: dict[str, int] = {}
        self.last_error = ""

    # ------------------------------------------------------------------ paths

    @property
    def settings_path(self) -> Path:
        return self.home / "app.json"

    @property
    def log_dir(self) -> Path:
        path = self.home / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def server_pid_path(self) -> Path:
        return self.home / "server.pid"

    @property
    def node_pid_path(self) -> Path:
        return self.home / "inference" / "node.pid"

    @property
    def node_status_path(self) -> Path:
        return self.home / "inference" / "status.json"

    # --------------------------------------------------------------- settings

    def load_settings(self) -> AppSettings:
        try:
            return AppSettings.from_dict(json.loads(self.settings_path.read_text()))
        except (OSError, ValueError):
            return AppSettings()

    def save_settings(self, settings: AppSettings) -> AppSettings:
        clean = AppSettings.from_dict(asdict(settings))
        self.settings_path.write_text(json.dumps(asdict(clean), indent=2) + "\n")
        if sys.platform != "win32":
            self.settings_path.chmod(0o600)   # holds the session token
        return clean

    def update_settings(self, changes: dict[str, Any]) -> AppSettings:
        known = {k: v for k, v in changes.items() if k in AppSettings.__dataclass_fields__}
        return self.save_settings(AppSettings.from_dict({**asdict(self.load_settings()), **known}))

    # ----------------------------------------------------------------- argv

    def _home_args(self) -> list[str]:
        return [] if self.home == default_home() else ["--home", str(self.home)]

    def server_argv(self, email: str) -> list[str]:
        return self_command("server", "serve", "--admin-email", email, "--home", str(self.home))

    def node_argv(self, settings: AppSettings) -> list[str]:
        dirs = [arg for d in settings.models_dirs for arg in ("--models-dir", d)]
        return self_command(
            "node", "start", "--url", settings.server_url,
            "--memory-percent", str(settings.memory_percent), "--head", *dirs, *self._home_args(),
        )

    def node_stop_argv(self) -> list[str]:
        return self_command("node", "stop", *self._home_args())

    # -------------------------------------------------------------- spawning

    def _spawn(self, argv: list[str], log_name: str, extra_env: dict[str, str]) -> Any:
        env = child_env(extra_env)
        log_path = self.log_dir / log_name
        self._log_start[log_name] = log_size(log_path)
        with open(log_path, "ab") as fh:
            try:
                return self._popen(argv, stdout=fh, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, env=env, **spawn_kwargs())
            except OSError as e:
                raise ControllerError(f"Could not start {argv[0]}: {e}") from e

    def _new_log(self, log_name: str) -> str:
        return tail(self.log_dir / log_name, since=self._log_start.get(log_name, 0))

    # ----------------------------------------------------------------- health

    def health(self, url: str) -> Optional[dict]:
        if not url:
            return None
        try:
            r = self._http.get(f"{url}/health", timeout=1.5)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def local_server_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def lan_server_url(self) -> str:
        return f"http://{self._lan_ip()}:{self.server_port}"

    # ------------------------------------------------------------ main server

    def server_pid(self) -> Optional[int]:
        pid = read_pid(self.server_pid_path)
        return pid if pid_alive(pid) else None

    def start_server(self, email: str, password: str) -> dict:
        email, password = (email or "").strip(), password or ""
        if not email or not password:
            raise ControllerError("Enter the admin email and password.")
        if self.server_pid() is not None:
            return self.status()
        proc = self._spawn(self.server_argv(email), "server.log",
                           {"COMPUTE_ADMIN_PASSWORD": password})
        self._server_proc = proc
        self.server_pid_path.write_text(f"{proc.pid}\n")
        self._wait_for_server(proc)
        self.update_settings({"server_url": self.lan_server_url()})
        return self.status()

    def _wait_for_server(self, proc: Any) -> None:
        deadline = time.monotonic() + self._server_wait_s
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                self.server_pid_path.unlink(missing_ok=True)
                raise ControllerError(exit_message(code, self._new_log("server.log")))
            if self.health(self.local_server_url()):
                return
            time.sleep(0.2)
        self.last_error = "The main server is still starting; check logs/server.log if it never answers."

    def stop_server(self) -> dict:
        pid = self.server_pid()
        if pid is not None:
            terminate(pid)
        self.server_pid_path.unlink(missing_ok=True)
        self._server_proc = None
        return self.status()

    # ------------------------------------------------------------------- node

    def node_pid(self) -> Optional[int]:
        pid = read_pid(self.node_pid_path)
        return pid if pid_alive(pid) else None

    def check_account(self, settings: AppSettings) -> None:
        """Fail early, with a clear message, when the node would be refused for terms or auth."""
        try:
            r = self._http.get(f"{settings.server_url}/auth/me",
                               headers={"Authorization": f"Bearer {settings.session_token}"})
        except httpx.HTTPError as e:
            raise ControllerError(f"Main server unreachable at {settings.server_url}: {e}") from e
        if r.status_code == 401:
            raise ControllerError("Your session has expired. Sign in again.")
        if r.status_code >= 400:
            raise ControllerError(detail_of(r))
        try:
            user = r.json()
        except ValueError:
            user = {}
        if isinstance(user, dict) and user.get("accepted_terms") is False:
            raise ControllerError("Accept the terms (Account section) before contributing.")

    def start_node(self, check: bool = True) -> dict:
        settings = self.load_settings()
        if not settings.server_url:
            raise ControllerError("Connect to a main server first.")
        if not settings.session_token:
            raise ControllerError("Sign in first so this computer earns FLOPs for your account.")
        if self.node_pid() is not None:
            return self.status()
        if check:
            self.check_account(settings)
        proc = self._spawn(self.node_argv(settings), "node.log",
                           {"COMPUTE_SESSION_TOKEN": settings.session_token})
        self.update_settings({"contribute": True})
        self._watch_early_exit(proc, "node.log", "The node stopped right away")
        return self.status()

    def _watch_early_exit(self, proc: Any, log_name: str, what: str) -> None:
        deadline = time.monotonic() + self._node_wait_s
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                self.update_settings({"contribute": False})
                log_tail = self._new_log(log_name)
                raise ControllerError(f"{what} (exit code {code}).\n{log_tail}".strip())
            time.sleep(0.1)

    def stop_node(self) -> dict:
        self.update_settings({"contribute": False})
        if self.node_pid() is None:
            return self.status()
        try:
            self._run(self.node_stop_argv(), capture_output=True, text=True, timeout=30,
                      stdin=subprocess.DEVNULL, env=child_env({}), **run_kwargs())
        except (OSError, subprocess.SubprocessError) as e:
            raise ControllerError(f"Could not stop the node: {e}") from e
        return self.status()

    def node_status(self) -> dict:
        pid = self.node_pid()
        if pid is None:
            return {"running": False, "pid": None, "status": {}}
        try:
            data = json.loads(self.node_status_path.read_text())
        except (OSError, ValueError):
            data = {}
        return {"running": True, "pid": pid, "status": data if isinstance(data, dict) else {}}

    # ----------------------------------------------------------------- status

    def discover(self, timeout: float = 5.0) -> Optional[str]:
        found = self._discover(timeout)
        if not found:
            return None
        url = normalize_url(found)
        self.update_settings({"server_url": url})
        return url

    def status(self) -> dict:
        settings = self.load_settings()
        local_running = self.server_pid() is not None
        health = self.health(settings.server_url)
        if health is None and local_running:
            health = self.health(self.local_server_url())
        return {
            "server": {
                "url": settings.server_url,
                "reachable": health is not None,
                "local_running": local_running,
                "lan_url": self.lan_server_url() if local_running else "",
                "health": health or {},
            },
            "node": self.node_status(),
            "settings": asdict(settings),
            "last_error": self.last_error,
        }


def terminate(pid: int, wait_s: float = 5.0) -> None:
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=wait_s)
    except psutil.TimeoutExpired:
        proc.kill()
    except psutil.Error:
        return


def detail_of(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return r.text or f"HTTP {r.status_code}"
    detail = data.get("detail", data) if isinstance(data, dict) else data
    return detail if isinstance(detail, str) else json.dumps(detail)
