from __future__ import annotations

import json
import sys

import httpx
import pytest

from compute.app import controller as ctl_mod
from compute.app.controller import (
    CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, AppSettings, ControllerError, normalize_url, spawn_kwargs,
)
from app_fakes import Recorder


def ok_me(accepted: bool = True):
    return lambda _r: httpx.Response(200, json={"id": "u1", "accepted_terms": accepted})


# ------------------------------------------------------------------ settings


def test_settings_default_when_missing(make_controller):
    s = make_controller().load_settings()
    assert s == AppSettings()
    assert s.memory_percent == 50


def test_settings_round_trip(make_controller, tmp_path):
    c = make_controller()
    c.save_settings(AppSettings(server_url="http://10.0.0.2:8765", memory_percent=70,
                                models_dirs=["/m1", "/m2"], contribute=True, session_token="tok"))
    assert c.load_settings() == AppSettings("http://10.0.0.2:8765", 70, ["/m1", "/m2"], True, "tok")
    assert json.loads((tmp_path / "app.json").read_text())["memory_percent"] == 70


def test_settings_clamp_and_clean(make_controller):
    c = make_controller()
    s = c.update_settings({"memory_percent": 400, "models_dirs": [" /a ", "", "/a", "/b"],
                           "server_url": "192.168.1.9", "bogus": 1})
    assert s.memory_percent == 100
    assert s.models_dirs == ["/a", "/b"]
    assert s.server_url == "http://192.168.1.9:8765"
    assert c.update_settings({"memory_percent": -5}).memory_percent == 1
    assert c.update_settings({"memory_percent": "abc"}).memory_percent == 50


def test_settings_corrupt_file_falls_back(make_controller, tmp_path):
    (tmp_path / "app.json").write_text("{not json")
    assert make_controller().load_settings() == AppSettings()


def test_normalize_url():
    assert normalize_url("host") == "http://host:8765"
    assert normalize_url("host:9000/") == "http://host:9000"
    assert normalize_url("https://pool.example.com/") == "https://pool.example.com"
    assert normalize_url("  ") == ""


# ---------------------------------------------------------------------- argv


def test_node_argv_uses_self_command(make_controller, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Apps/compute.app/Contents/MacOS/compute")
    c = make_controller()
    s = AppSettings(server_url="http://10.0.0.2:8765", memory_percent=30, models_dirs=["/a", "/b"])
    argv = c.node_argv(s)
    assert argv[:3] == ["/Apps/compute.app/Contents/MacOS/compute", "node", "start"]
    assert argv[3:9] == ["--url", "http://10.0.0.2:8765", "--memory-percent", "30", "--head", "--models-dir"]
    assert argv.count("--models-dir") == 2 and "/b" in argv


def test_node_argv_from_source(make_controller, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    argv = make_controller().node_argv(AppSettings(server_url="http://h:8765"))
    assert argv[:5] == [sys.executable, "-m", "compute", "node", "start"]


def test_server_argv_and_password_only_in_env(make_controller, monkeypatch, tmp_path):
    popen = Recorder()
    routes = {"/health": lambda _r: httpx.Response(200, json={"ok": True})}
    c = make_controller(popen=popen, routes=routes)
    monkeypatch.setattr(ctl_mod, "pid_alive", lambda pid: False)
    c.start_server("admin@example.com", "s3cret-pw")
    call = popen.calls[0]
    assert call["argv"][-6:] == ["server", "serve", "--admin-email", "admin@example.com", "--home", str(tmp_path)]
    assert "s3cret-pw" not in " ".join(call["argv"])
    assert call["env"]["COMPUTE_ADMIN_PASSWORD"] == "s3cret-pw"
    assert c.load_settings().server_url == "http://10.0.0.5:8765"


def test_server_early_exit_surfaces_message(make_controller, monkeypatch):
    popen = Recorder(exit_code=2, log_text="error: only the admin may run the server\n")
    c = make_controller(popen=popen)
    with pytest.raises(ControllerError) as e:
        c.start_server("someone@example.com", "pw")
    assert "Only the admin" in str(e.value)
    assert "only the admin may run the server" in str(e.value)
    assert not c.server_pid_path.exists()


def test_server_start_requires_credentials(make_controller):
    with pytest.raises(ControllerError):
        make_controller().start_server("", "pw")


# ---------------------------------------------------------------- spawning


def test_spawn_kwargs_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert spawn_kwargs() == {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}


def test_spawn_kwargs_posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert spawn_kwargs() == {"start_new_session": True}


def test_node_start_on_windows_passes_creationflags(make_controller, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    popen = Recorder()
    c = make_controller(popen=popen, routes={"/auth/me": ok_me()})
    c.update_settings({"server_url": "http://10.0.0.2:8765", "session_token": "tok-123"})
    c.start_node()
    call = popen.calls[0]
    assert call["creationflags"] == CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    assert "start_new_session" not in call
    assert call["env"]["COMPUTE_SESSION_TOKEN"] == "tok-123"
    assert "tok-123" not in call["argv"]
    assert c.load_settings().contribute is True


def test_node_start_needs_sign_in(make_controller):
    c = make_controller()
    c.update_settings({"server_url": "http://10.0.0.2:8765"})
    with pytest.raises(ControllerError, match="Sign in"):
        c.start_node()


def test_node_start_needs_terms(make_controller):
    c = make_controller(routes={"/auth/me": ok_me(accepted=False)})
    c.update_settings({"server_url": "http://10.0.0.2:8765", "session_token": "t"})
    with pytest.raises(ControllerError, match="terms"):
        c.start_node()


def test_node_early_exit_reports_log(make_controller):
    popen = Recorder(exit_code=1, log_text="403 terms_required\n")
    c = make_controller(popen=popen, routes={"/auth/me": ok_me()})
    c.update_settings({"server_url": "http://10.0.0.2:8765", "session_token": "t"})
    with pytest.raises(ControllerError, match="terms_required"):
        c.start_node()
    assert c.load_settings().contribute is False


def test_node_stop_runs_self_command(make_controller, monkeypatch):
    run = Recorder()
    c = make_controller(run=run)
    monkeypatch.setattr(c, "node_pid", lambda: 999)
    c.stop_node()
    argv = run.calls[0]["argv"]
    assert argv[argv.index("node"):argv.index("node") + 2] == ["node", "stop"]
    assert c.load_settings().contribute is False


def test_status_shape(make_controller):
    c = make_controller(routes={"/health": lambda _r: httpx.Response(200, json={"ok": True})})
    c.update_settings({"server_url": "http://10.0.0.2:8765"})
    st = c.status()
    assert st["server"]["reachable"] is True
    assert st["server"]["local_running"] is False
    assert st["node"] == {"running": False, "pid": None, "status": {}}
    assert st["settings"]["server_url"] == "http://10.0.0.2:8765"


def test_early_exit_shows_only_new_log_lines(make_controller, tmp_path):
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "server.log").write_text("old run: something unrelated\n")
    popen = Recorder(exit_code=1, log_text="new run: port in use\n")
    c = make_controller(popen=popen)
    with pytest.raises(ControllerError) as e:
        c.start_server("admin@example.com", "pw")
    assert "port in use" in str(e.value)
    assert "old run" not in str(e.value)


def test_child_env_puts_src_on_path_from_source(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    env = ctl_mod.child_env({"X": "1"})
    first, *rest = env["PYTHONPATH"].split(ctl_mod.os.pathsep)
    assert first.endswith("src") and rest == ["/elsewhere"]
    assert env["X"] == "1"


def test_child_env_frozen_untouched(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    assert "PYTHONPATH" not in ctl_mod.child_env({})
