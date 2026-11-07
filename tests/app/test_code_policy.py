from __future__ import annotations

from pathlib import Path

import pytest

from compute.app.code.policy import ALLOW, ASK, AUTO, DENY, MANUAL, PLAN, decide
from compute.app.code.workspace import Workspace


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    root = (tmp_path / "proj").resolve()
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    (root / ".env").write_text("KEY=1\n")
    return Workspace(root)


READ = ("read_file", {"path": "app.py"})
LIST = ("list_files", {"path": "."})
SEARCH = ("search", {"pattern": "x"})
SECRET_READ = ("read_file", {"path": ".env"})
WRITE = ("write_file", {"path": "new.py", "content": "y = 2\n"})
EDIT = ("edit_file", {"path": "app.py", "old": "x = 1", "new": "x = 2"})
SECRET_EDIT = ("edit_file", {"path": ".env", "old": "KEY=1", "new": "KEY=2"})
DELETE = ("delete_file", {"path": "app.py"})
CMD_READ = ("run_command", {"command": "git status"})
CMD_NORMAL = ("run_command", {"command": "pytest -q"})
CMD_POWERFUL = ("run_command", {"command": "rm -rf build"})
PLAN_CALL = ("propose_plan", {"plan": "1. do it"})

MATRIX = [
    # call,         plan,  manual, auto
    (READ,          ALLOW, ALLOW, ALLOW),
    (LIST,          ALLOW, ALLOW, ALLOW),
    (SEARCH,        ALLOW, ALLOW, ALLOW),
    (SECRET_READ,   ASK,   ASK,   ASK),
    (WRITE,         DENY,  ASK,   ALLOW),
    (EDIT,          DENY,  ASK,   ALLOW),
    (SECRET_EDIT,   DENY,  ASK,   ASK),
    (DELETE,        DENY,  ASK,   ASK),
    (CMD_READ,      ALLOW, ASK,   ALLOW),
    (CMD_NORMAL,    DENY,  ASK,   ASK),
    (CMD_POWERFUL,  DENY,  ASK,   ASK),
    (PLAN_CALL,     ALLOW, DENY,  DENY),
]


@pytest.mark.parametrize("call, plan, manual, auto", MATRIX, ids=[f"{c[0]}:{list(c[1].values())[0]}" for c, *_ in MATRIX])
def test_permission_matrix(ws: Workspace, call, plan, manual, auto):
    name, args = call
    for mode, expected in ((PLAN, plan), (MANUAL, manual), (AUTO, auto)):
        decision = decide(mode, name, args, ws)
        assert decision.action == expected, f"{mode} {name} {args}: {decision}"
        assert decision.reason


@pytest.mark.parametrize("mode", [PLAN, MANUAL, AUTO])
def test_outside_paths_and_git_are_denied_everywhere(ws: Workspace, mode: str):
    assert decide(mode, "read_file", {"path": "../../etc/passwd"}, ws).action == DENY
    assert decide(mode, "write_file", {"path": "/tmp/x", "content": ""}, ws).action == DENY
    git = decide(mode, "write_file", {"path": ".git/hooks/pre-commit", "content": ""}, ws)
    assert git.action == DENY and ".git" in git.reason


def test_unknown_tool_mode_and_empty_command(ws: Workspace):
    assert decide(AUTO, "format_disk", {}, ws).action == DENY
    assert decide("yolo", "read_file", {"path": "app.py"}, ws).action == DENY
    assert decide(AUTO, "run_command", {"command": "  "}, ws).action == DENY


def test_auto_runs_programs_only_when_the_tab_opts_in(ws: Workspace):
    name, args = CMD_NORMAL
    asked = decide(AUTO, name, args, ws)
    assert asked.action == ASK and "unless this tab allows it" in asked.reason
    assert decide(AUTO, name, args, ws, run_commands=True).action == ALLOW
    assert decide(AUTO, *CMD_READ, ws).action == ALLOW
    assert decide(AUTO, *CMD_POWERFUL, ws, run_commands=True).action == ASK
    assert decide(PLAN, name, args, ws, run_commands=True).action == DENY
    assert decide(MANUAL, name, args, ws, run_commands=True).action == ASK


@pytest.mark.parametrize("command", ["rg --pre bash x", 'git -c core.fsmonitor="touch p" status',
                                     "GIT_EXTERNAL_DIFF=./x.sh git diff", "printenv", "cat .env"])
def test_plan_mode_refuses_commands_that_only_look_read_only(ws: Workspace, command: str):
    assert decide(PLAN, "run_command", {"command": command}, ws).action == DENY
    assert decide(AUTO, "run_command", {"command": command}, ws, run_commands=True).action == ASK


def test_command_decisions_carry_the_risk(ws: Workspace):
    decision = decide(AUTO, "run_command", {"command": "git push"}, ws)
    assert decision.risk == "powerful" and "git push" in decision.reason
    assert "read-only" in decide(PLAN, "run_command", {"command": "pytest"}, ws).reason
