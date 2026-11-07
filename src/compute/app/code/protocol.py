"""What the model is told, and how its tool calls are read back.

Pool models are llama.cpp models without a reliable native tool-call format, so the harness uses
plain text: one ``<tool name="...">`` block per reply, with XML-style parameters (code needs no
escaping). Requests carry ``stop: ["</tool>"]``, so generation ends right after the call.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Optional

from compute.app.code.policy import AUTO, MANUAL, PLAN

STOP = "</tool>"
TOOL_PARAMS: dict[str, tuple[str, ...]] = {
    "list_files": ("path",),
    "read_file": ("path", "start", "end"),
    "search": ("pattern", "path"),
    "write_file": ("path", "content"),
    "edit_file": ("path", "old", "new"),
    "delete_file": ("path",),
    "run_command": ("command",),
    "propose_plan": ("plan",),
}
REQUIRED: dict[str, tuple[str, ...]] = {
    "read_file": ("path",),
    "search": ("pattern",),
    "write_file": ("path", "content"),
    "edit_file": ("path", "old", "new"),
    "delete_file": ("path",),
    "run_command": ("command",),
    "propose_plan": ("plan",),
}
RAW_PARAMS = frozenset({"content", "old", "new"})   # keep inner whitespace exactly
# Single-value tools: a body without parameter tags is that value (models often write it this way).
BARE_PARAM = {"propose_plan": "plan", "run_command": "command", "read_file": "path", "list_files": "path",
              "delete_file": "path", "search": "pattern"}
OPEN_TAG = re.compile(r"<tool\s+name\s*=\s*[\"']?([A-Za-z_]+)[\"']?\s*/?>")
THINK = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL)
TRAILING_TAGS = re.compile(r"(?:\s*</[A-Za-z_]+>)+\s*$")   # stray closers such as </tool_result>

MODE_RULES = {
    PLAN: (
        "PLAN MODE (read-only). Explore with list_files, read_file, search and read-only commands "
        "(ls, cat, grep, git status/diff/log). You cannot change files or run other commands yet. "
        "If something about the task is unclear, ask the user (reply without a tool call). When you "
        "understand the task, call propose_plan with a short numbered plan: which files change, how, "
        "and how to check the result. The user approves the plan or asks for changes."
    ),
    MANUAL: (
        "MANUAL MODE. Reading is free. Every file change and every command is shown to the user, who "
        "approves or rejects it. If a step is rejected, read the user's note and adjust."
    ),
    AUTO: (
        "AUTO MODE. Reads and file edits happen immediately. Commands run immediately too, except "
        "risky ones (deleting, installing, network, git push, paths outside the folder), which the "
        "user must approve. Work step by step and check your work (run the tests if there are any)."
    ),
}


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, str] = field(default_factory=dict)
    error: str = ""
    narration: str = ""   # what the model wrote before the call


def strip_thinking(text: str) -> str:
    return THINK.sub("", text or "")


def shell_name() -> str:
    return "cmd.exe" if sys.platform == "win32" else "/bin/sh"


def os_name() -> str:
    return {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, "Linux")


def system_prompt(mode: str, root: str, listing: str) -> str:
    return f"""You are a coding assistant working in the project folder {root} on {os_name()}. You read and \
change files only through the tools below.

To use a tool, write one short sentence about what you are doing, then exactly one tool call, then stop:
<tool name="read_file"><path>src/app.py</path></tool>
The result comes back in the next message inside <tool_result>. One tool call per reply. Paths are relative to the \
project folder.

Tools:
- list_files: <path>folder</path> -- files and folders, two levels deep.
- read_file: <path>file</path>, optional <start>N</start><end>M</end> -- numbered lines, 400 at a time.
- search: <pattern>regex</pattern>, optional <path>folder</path> -- matching lines with file and line number.
- write_file: <path>file</path><content>the complete file</content> -- create a file or replace all of it.
- edit_file: <path>file</path><old>exact existing text</old><new>replacement</new> -- the old text must appear \
exactly once; read the file first and copy it exactly, indentation included. Prefer this for small changes.
- delete_file: <path>file</path>
- run_command: <command>a shell command</command> -- runs in the project folder with {shell_name()}, \
2 minute limit, no input.
- propose_plan: <plan>numbered steps</plan> -- plan mode only.

When the task is done, reply with a short summary of what changed and no tool call.

{MODE_RULES.get(mode, MODE_RULES[MANUAL])}

Project folder (top level):
{listing}"""


def find_param(body: str, name: str, last: bool) -> Optional[str]:
    """Text between <name> and </name>; the last declared parameter takes the last closing tag,
    so file content that itself contains the tag survives."""
    open_tag, close_tag = f"<{name}>", f"</{name}>"
    start = body.find(open_tag)
    if start < 0:
        return None
    start += len(open_tag)
    end = body.rfind(close_tag) if last else body.find(close_tag, start)
    return body[start:] if end < start else body[start:end]


def clean(name: str, value: str) -> str:
    if name not in RAW_PARAMS:
        return value.strip()
    if value.startswith("\n"):
        value = value[1:]
    if value.endswith("\n") and name != "content":
        value = value[:-1]
    return value


def parse_tool_call(text: str) -> Optional[ToolCall]:
    """The first tool call in a reply, or None when the reply is a final answer."""
    visible = strip_thinking(text)
    match = OPEN_TAG.search(visible)
    if not match:
        return None
    narration = visible[:match.start()].strip()
    narration = re.sub(r"```[a-z]*\s*$", "", narration).strip()
    name = match.group(1)
    if name not in TOOL_PARAMS:
        known = ", ".join(TOOL_PARAMS)
        return ToolCall(name, error=f"There is no tool named {name!r}. Use one of: {known}.", narration=narration)
    body = visible[match.end():]
    stop = body.find(STOP)
    body = body if stop < 0 else body[:stop]
    params = TOOL_PARAMS[name]
    present = [p for p in params if f"<{p}>" in body]
    args = {}
    for p in params:
        value = find_param(body, p, last=bool(present) and p == present[-1])
        if value is not None:
            args[p] = clean(p, value)
    bare = TRAILING_TAGS.sub("", body).strip()
    if not present and name in BARE_PARAM and bare:
        args[BARE_PARAM[name]] = bare   # e.g. <tool name="propose_plan">1. ...</tool>
    missing = [p for p in REQUIRED.get(name, ()) if p not in args]
    if missing:
        tags = ", ".join(f"<{p}>" for p in missing)
        return ToolCall(name, args, error=f"{name} is missing {tags}. Write it exactly like this: {example(name)}",
                        narration=narration)
    return ToolCall(name, args, narration=narration)


def example(name: str) -> str:
    inner = "".join(f"<{p}>...</{p}>" for p in REQUIRED.get(name, TOOL_PARAMS[name][:1]))
    return f'<tool name="{name}">{inner}</tool>'


def history_text(text: str) -> str:
    """The reply as kept in the conversation: closing tag restored after the stop sequence cut it."""
    body = strip_thinking(text).rstrip()
    if OPEN_TAG.search(body) and not body.endswith(STOP):
        body += STOP
    return body


def tool_result(name: str, text: str) -> str:
    return f'<tool_result name="{name}">\n{text}\n</tool_result>'


def summarize(call: ToolCall) -> str:
    """One line for the transcript and the approval card."""
    a = call.args
    if call.name == "run_command":
        return a.get("command", "")
    if call.name == "search":
        return f"{a.get('pattern', '')!r} in {a.get('path') or '.'}"
    if call.name == "read_file" and (a.get("start") or a.get("end")):
        return f"{a.get('path', '')} lines {a.get('start') or 1}-{a.get('end') or ''}"
    return a.get("path") or "."
