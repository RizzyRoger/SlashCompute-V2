"""Plan / Auto / Manual: may a tool call run now, must the user approve it first, or is it refused?

| tool                         | plan  | manual | auto  |
|------------------------------|-------|--------|-------|
| list_files, read_file, search| allow | allow  | allow |
| read a secrets file          | ask   | ask    | ask   |
| write_file, edit_file        | deny  | ask    | allow (ask for secrets files) |
| delete_file                  | deny  | ask    | ask   |
| run_command: read-only       | allow | ask    | allow |
| run_command: normal          | deny  | ask    | ask (allow if the tab opted in: run_commands) |
| run_command: powerful        | deny  | ask    | ask   |
| propose_plan                 | allow | deny   | deny  |

Paths outside the project folder and writes inside ``.git`` are refused in every mode.

The model's replies are generated on other people's computers in the pool, so Auto mode only runs
programs (normal commands) without asking when the user turned that on for the tab.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from compute.app.code.commands import NORMAL, POWERFUL, READ_ONLY, classify
from compute.app.code.workspace import Workspace, WorkspaceError, in_git_dir, is_sensitive

PLAN, AUTO, MANUAL = "plan", "auto", "manual"
MODES = (PLAN, AUTO, MANUAL)
ALLOW, ASK, DENY = "allow", "ask", "deny"

READ_TOOLS = frozenset({"list_files", "read_file", "search"})
EDIT_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})
TOOLS = READ_TOOLS | EDIT_TOOLS | {"run_command", "propose_plan"}


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    risk: Optional[str] = None


def decide_read(name: str, args: dict, ws: Workspace) -> Decision:
    path = ws.resolve(args.get("path"))
    if name == "read_file" and is_sensitive(path):
        return Decision(ASK, f"{ws.rel(path)} may hold secrets, and its text would be sent to the pool")
    return Decision(ALLOW, "reading is always allowed")


def decide_edit(mode: str, name: str, args: dict, ws: Workspace) -> Decision:
    path = ws.resolve(args.get("path"))
    if in_git_dir(ws, path):
        return Decision(DENY, "files inside .git are never edited")
    if mode == PLAN:
        return Decision(DENY, "plan mode is read-only; finish exploring and call propose_plan")
    if mode == MANUAL:
        return Decision(ASK, "manual mode: every change needs your approval")
    if name == "delete_file":
        return Decision(ASK, "deleting a file always needs your approval")
    if is_sensitive(path):
        return Decision(ASK, f"{ws.rel(path)} may hold secrets")
    return Decision(ALLOW, "auto mode applies edits")


def decide_command(mode: str, args: dict, ws: Workspace, run_commands: bool = False) -> Decision:
    command = (args.get("command") or "").strip()
    if not command:
        return Decision(DENY, "run_command needs a <command>")
    risk = classify(command, ws.root)
    if mode == PLAN:
        if risk.level == READ_ONLY:
            return Decision(ALLOW, risk.reason, risk.level)
        return Decision(DENY, f"plan mode only runs read-only commands ({risk.reason})", risk.level)
    if mode == MANUAL:
        return Decision(ASK, f"manual mode: every command needs your approval ({risk.reason})", risk.level)
    if risk.level == POWERFUL:
        return Decision(ASK, f"powerful command: {risk.reason}", risk.level)
    if risk.level == NORMAL and not run_commands:
        return Decision(ASK, f"auto mode asks before running programs unless this tab allows it ({risk.reason})",
                        risk.level)
    return Decision(ALLOW, risk.reason, risk.level)


def decide(mode: str, name: str, args: dict, ws: Workspace, run_commands: bool = False) -> Decision:
    if mode not in MODES:
        return Decision(DENY, f"unknown mode {mode!r}")
    if name not in TOOLS:
        return Decision(DENY, f"unknown tool {name!r}")
    if name == "propose_plan":
        if mode == PLAN:
            return Decision(ALLOW, "shows the plan to the user")
        return Decision(DENY, "you are not in plan mode; carry out the work instead of proposing a plan")
    try:
        if name in READ_TOOLS:
            return decide_read(name, args, ws)
        if name in EDIT_TOOLS:
            return decide_edit(mode, name, args, ws)
        return decide_command(mode, args, ws, run_commands)
    except WorkspaceError as e:
        return Decision(DENY, str(e))
