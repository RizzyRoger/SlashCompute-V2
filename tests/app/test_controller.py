from __future__ import annotations

import json
import sys

import httpx
import pytest

from compute.app import controller as ctl_mod
from compute.app.controller import (
    CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, AppSettings, ControllerError, normalize_url, spawn_kwargs,
)
from compute.common.discovery import LanServer
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
    popen = Recorder(exit_code=2, log_text="compute server: a password is required\n")
    c = make_controller(popen=popen)
    with pytest.raises(ControllerError) as e:
        c.start_server("someone@example.com", "pw")
    assert "refused to start" in str(e.value)
    assert "a password is required" in str(e.value)
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


# ------------------------------------------------------------------ LAN pool

LATE = LanServer("http://10.0.0.2:8765", "compute-late", 200.0)
EARLY = LanServer("http://10.0.0.3:8765", "compute-early", 100.0)


def test_auto_join_defaults_on_and_round_trips(make_controller):
    c = make_controller()
    assert AppSettings().auto_join is True
    assert AppSettings.from_dict({"server_url": "h"}).auto_join is True   # installs from before auto-join
    assert c.update_settings({"auto_join": False}).auto_join is False
    assert c.load_settings().auto_join is False


def test_follow_pool_joins_the_server_that_started_first(make_controller):
    c = make_controller(lan=[LATE, EARLY])
    assert c.follow_pool() == EARLY.url
    assert c.load_settings().server_url == EARLY.url
    assert c.follow_pool() is None   # already there


def test_follow_pool_follows_the_pool_to_a_new_address(make_controller):
    seen = [EARLY]
    c = make_controller(lan=lambda: list(seen))
    c.follow_pool()
    seen[:] = [LanServer("http://10.0.0.9:8765", EARLY.name, EARLY.started)]
    assert c.follow_pool() == "http://10.0.0.9:8765"
    assert c.load_settings().server_url == "http://10.0.0.9:8765"


def test_follow_pool_keeps_a_typed_address(make_controller):
    c = make_controller(lan=[EARLY])
    c.update_settings({"server_url": "pool.example.com:9000", "auto_join": False})
    assert c.follow_pool() is None
    assert c.load_settings().server_url == "http://pool.example.com:9000"


def test_follow_pool_keeps_the_address_when_nothing_is_advertised(make_controller):
    c = make_controller(lan=[])
    c.update_settings({"server_url": "http://10.0.0.2:8765"})
    assert c.follow_pool() is None
    assert c.load_settings().server_url == "http://10.0.0.2:8765"


def node_moving_controller(make_controller, monkeypatch, node_url: str):
    popen, run = Recorder(), Recorder()
    c = make_controller(popen=popen, run=run, routes={"/auth/me": ok_me()}, lan=[EARLY])
    c.update_settings({"server_url": node_url, "session_token": "tok", "contribute": True})
    alive = {"pid": 999}
    monkeypatch.setattr(c, "node_pid", lambda: alive["pid"])
    monkeypatch.setattr(c, "node_url", lambda: node_url if alive["pid"] else None)
    return c, popen, run, alive


def test_running_node_drains_then_restarts_on_the_new_pool(make_controller, monkeypatch):
    c, popen, run, alive = node_moving_controller(make_controller, monkeypatch, LATE.url)
    assert c.follow_pool() == EARLY.url
    assert run.calls and "stop" in run.calls[0]["argv"]   # asked to drain and leave
    assert popen.calls == []                              # not restarted while the old one drains
    c.follow_pool()
    assert popen.calls == []
    alive["pid"] = None                                   # the old node has exited
    c.follow_pool()
    argv = popen.calls[0]["argv"]
    assert argv[argv.index("--url") + 1] == EARLY.url
    assert c.load_settings().contribute is True


def test_node_already_on_the_pool_is_left_alone(make_controller, monkeypatch):
    c, popen, run, _ = node_moving_controller(make_controller, monkeypatch, EARLY.url)
    c.follow_pool()
    assert run.calls == [] and popen.calls == []


def test_stop_contributing_cancels_a_pending_move(make_controller, monkeypatch):
    c, popen, _, alive = node_moving_controller(make_controller, monkeypatch, LATE.url)
    c.follow_pool()
    alive["pid"] = None
    c.stop_node()   # the user clicked Stop contributing while it drained
    c.follow_pool()
    assert popen.calls == []


def test_node_url_reads_the_running_nodes_url(make_controller, monkeypatch):
    c = make_controller()
    monkeypatch.setattr(c, "node_pid", lambda: 4242)

    class Proc:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            return ["compute", "node", "start", "--url", "http://10.0.0.3:8765", "--head"]

    monkeypatch.setattr(ctl_mod.psutil, "Process", Proc)
    assert c.node_url() == "http://10.0.0.3:8765"
    monkeypatch.setattr(c, "node_pid", lambda: None)
    assert c.node_url() is None


def test_discover_turns_auto_join_back_on(make_controller):
    c = make_controller(lan=[LATE, EARLY])
    c.update_settings({"server_url": "http://pool.example.com:9000", "auto_join": False})
    assert c.discover(timeout=0) == EARLY.url
    s = c.load_settings()
    assert s.auto_join is True and s.server_url == EARLY.url


def test_discover_finds_nothing(make_controller):
    assert make_controller(lan=[]).discover(timeout=0) is None


def test_server_start_refused_when_the_network_has_a_pool(make_controller, monkeypatch):
    popen = Recorder()
    c = make_controller(popen=popen, lan=[EARLY])
    monkeypatch.setattr(ctl_mod, "pid_alive", lambda pid: False)
    with pytest.raises(ControllerError) as e:
        c.start_server("admin@example.com", "pw")
    assert f"already has a pool at {EARLY.url}" in str(e.value)
    assert popen.calls == []


def test_server_start_ignores_its_own_advert(make_controller, monkeypatch):
    own = LanServer("http://10.0.0.5:8765", "compute-me", 1.0)
    popen = Recorder()
    c = make_controller(popen=popen, lan=[own],
                        routes={"/health": lambda _r: httpx.Response(200, json={"ok": True})})
    c.update_settings({"auto_join": False})
    monkeypatch.setattr(ctl_mod, "pid_alive", lambda pid: False)
    c.start_server("admin@example.com", "pw")
    assert popen.calls
    s = c.load_settings()
    assert s.server_url == own.url and s.auto_join is True


def test_status_reports_auto_join_and_lan_servers(make_controller):
    st = make_controller(lan=[EARLY]).status()
    assert st["server"]["auto_join"] is True
    assert st["server"]["lan"] == [{"url": EARLY.url, "name": EARLY.name}]


# ------------------------------------------------- is our own server visible?

def running_server(c, monkeypatch, age_s: float):
    c.server_pid_path.write_text("4242\n")
    then = __import__("time").time() - age_s
    __import__("os").utime(c.server_pid_path, (then, then))
    monkeypatch.setattr(ctl_mod, "pid_alive", lambda pid: True)


def test_own_server_seen_on_the_network_is_advertised(make_controller, monkeypatch):
    c = make_controller(lan=[LanServer("http://10.0.0.5:8765", "compute-me", 1.0)])
    running_server(c, monkeypatch, age_s=60)
    assert c.status()["server"]["advertised"] is True


def test_own_server_missing_from_the_network_is_flagged_after_a_grace_period(make_controller, monkeypatch):
    c = make_controller(lan=[EARLY])   # only someone else's server is visible
    running_server(c, monkeypatch, age_s=1)
    assert c.status()["server"]["advertised"] is None    # just started: too early to tell
    running_server(c, monkeypatch, age_s=60)
    assert c.status()["server"]["advertised"] is False


def test_advertised_is_none_without_a_local_server(make_controller):
    assert make_controller(lan=[EARLY]).status()["server"]["advertised"] is None
