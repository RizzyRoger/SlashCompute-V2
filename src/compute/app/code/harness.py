"""One Code tab: its conversation with a pool model, the agent loop, approvals, and persistence.

A turn runs as an asyncio task owned by the session, not by an HTTP request, so it keeps going
while the UI switches tabs or reloads. The UI follows it through ``session_events`` (SSE).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

import httpx

from compute.app.code import commands, workspace as wsmod
from compute.app.code.commands import clip
from compute.app.code.policy import ASK, AUTO, DENY, EDIT_TOOLS, MANUAL, MODES, PLAN, Decision, decide
from compute.app.code.protocol import (
    STOP, ToolCall, history_text, parse_tool_call, strip_thinking, summarize, system_prompt, tool_result,
)
from compute.app.code.workspace import WorkspaceError

log = logging.getLogger(__name__)

MAX_STEPS = 30
MAX_TOKENS = 2048
HISTORY_CHARS = 40_000
RESULT_CHARS = 12_000
ENTRY_TEXT_CHARS = 4_000
SHRUNK_CHARS = 600
LISTING_CHARS = 3_000
HEARTBEAT_S = 15.0
COALESCE_S = 0.1
STREAM_TIMEOUT = httpx.Timeout(None, connect=5.0)
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
PFLOP = 1e15
CHECKPOINT_MAX_BYTES = 5 * 1024 * 1024
LABEL_CHARS = 80

IDLE, RUNNING, WAITING = "idle", "running", "waiting"
PLAN_ACTIONS = (AUTO, MANUAL, "revise")
EMPTY_REPLY_NUDGE = ("Your last reply was empty. Continue: make exactly one tool call in the format from the "
                     "instructions, or reply with your final answer.")

Llm = Callable[[dict], AsyncIterator[dict]]


class CodeError(Exception):
    """A request the UI made that can't be done; ``status`` becomes the HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class LlmError(Exception):
    """The pool couldn't answer; the message is shown in the transcript."""


# ------------------------------------------------------------- the pool


def response_detail(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return r.text or f"HTTP {r.status_code}"
    detail = data.get("detail", data) if isinstance(data, dict) else data
    return detail if isinstance(detail, str) else json.dumps(detail)


def pool_error(r: httpx.Response) -> str:
    detail = response_detail(r)
    if r.status_code == 401:
        return "Sign in first (Account section)."
    if r.status_code == 402 and "budget" in detail:
        return "This tab's FLOP budget is used up. Raise the budget above the transcript, then send again."
    if r.status_code == 402:
        return "Not enough FLOPs in your wallet. Contribute or buy (test) FLOPs, then send again."
    if r.status_code == 404:
        return f"The pool doesn't serve this model right now ({detail}). Pick another model."
    if r.status_code == 503:
        return f"No capacity in the pool for this model right now ({detail})."
    return f"The pool returned HTTP {r.status_code}: {detail}"


def auth_headers(settings: Any) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if settings.session_token:
        headers["authorization"] = f"Bearer {settings.session_token}"
    return headers


def server_url(settings: Any) -> str:
    if not settings.server_url:
        raise LlmError("Not connected to a main server. Connect first (Connect section).")
    return settings.server_url


def pool_llm(http: httpx.AsyncClient, settings: Callable[[], Any]) -> Llm:
    """Stream ``/v1/chat/completions`` from the main server as parsed SSE events."""

    async def stream(body: dict) -> AsyncIterator[dict]:
        current = settings()
        base = server_url(current)
        try:
            async with http.stream("POST", f"{base}/v1/chat/completions", json=body,
                                   headers=auth_headers(current), timeout=STREAM_TIMEOUT) as r:
                if r.status_code >= 400:
                    await r.aread()
                    raise LlmError(pool_error(r))
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        yield event
        except httpx.HTTPError as e:
            raise LlmError(f"Main server unreachable at {base}: {e}") from e

    return stream


# ------------------------------------------------------------ the history


def shrink(message: dict) -> dict:
    content = message["content"]
    if message["role"] == "user" and content.startswith("<tool_result"):
        match = re.match(r'<tool_result name="([^"]*)"', content)
        return {**message, "content": tool_result(match.group(1) if match else "tool", "[older output removed to save space]")}
    if len(content) > SHRUNK_CHARS:
        return {**message, "content": content[:SHRUNK_CHARS] + "\n[... older text removed to save space]"}
    return message


def compact(messages: list[dict], budget: int = HISTORY_CHARS) -> list[dict]:
    """Newest messages verbatim; once the budget is spent, older tool output and long text shrink.
    The first message (the task) always stays whole."""
    total, out = 0, []
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if i > 0 and total + len(message["content"]) > budget:
            message = shrink(message)
        total += len(message["content"])
        out.append(message)
    return out[::-1]


def merge_consecutive(messages: list[dict]) -> list[dict]:
    """Chat templates expect user and assistant to alternate (a Stop can leave two user messages)."""
    merged: list[dict] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1] = {**merged[-1], "content": merged[-1]["content"] + "\n\n" + m["content"]}
        else:
            merged.append(dict(m))
    return merged


def top_listing(ws: wsmod.Workspace) -> str:
    try:
        return clip(wsmod.list_files(ws, ".", depth=1), LISTING_CHARS)
    except WorkspaceError as e:
        return f"(can't list the folder: {e})"


# ------------------------------------------------------------ checkpoints


def snapshot_file(path: Path) -> dict:
    """What a file held before the assistant first changed it in a turn (bytes, so binaries too)."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {"existed": False, "data": None, "after": None}
    if len(data) > CHECKPOINT_MAX_BYTES:
        return {"existed": True, "data": None, "too_large": True, "after": None}
    return {"existed": True, "data": base64.b64encode(data).decode("ascii"), "after": None}


def restore_file(path: Path, snap: dict) -> None:
    if snap["existed"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(snap["data"]))
    else:
        path.unlink(missing_ok=True)


def checkpoint_view(cp: dict) -> dict:
    return {"turn": cp["turn"], "label": cp["label"], "files": sorted(cp["files"]), "reverted": cp["reverted"]}


@dataclass(frozen=True)
class Reply:
    text: str
    thinking: str
    flops: float


# ------------------------------------------------------------ one tab


class CodeSession:
    def __init__(self, *, id: str, root: str, model: str, mode: str, title: str, created: float,
                 store: Path, llm: Llm, flop_session_id: str = "", budget_flops: float = 0.0,
                 spent_flops: float = 0.0, messages: Optional[list] = None, entries: Optional[list] = None,
                 plan: str = "", checkpoints: Optional[list] = None, run_commands: bool = False) -> None:
        self.id = id
        self.ws = wsmod.Workspace(Path(root))
        self.model = model
        self.mode = mode
        self.run_commands = run_commands   # Auto mode runs normal commands without asking (opt-in)
        self.title = title
        self.created = created
        self.store = store
        self.llm = llm
        self.flop_session_id = flop_session_id
        self.budget_flops = budget_flops
        self.spent_flops = spent_flops
        self.messages: list[dict] = list(messages or [])
        self.entries: list[dict] = list(entries or [])
        self.plan = plan
        self.checkpoints: list[dict] = list(checkpoints or [])
        self.turn: Optional[dict] = None   # {"turn": seq of the user entry, "label": ...} while a turn runs
        self.partial = {"text": "", "thinking": ""}
        self.state = IDLE
        self.activity = ""
        self.queue: Optional[dict] = None
        self.pending: Optional[dict] = None
        self._decision: Optional[asyncio.Future] = None
        self.task: Optional[asyncio.Task] = None
        self.version = 0
        self._waiters: list[asyncio.Future] = []
        self.closed = False

    # ---------------------------------------------------------- persistence

    @property
    def path(self) -> Path:
        return self.store / f"{self.id}.json"

    def record(self) -> dict:
        return {
            "id": self.id, "root": str(self.ws.root), "model": self.model, "mode": self.mode,
            "title": self.title, "created": self.created, "flop_session_id": self.flop_session_id,
            "budget_flops": self.budget_flops, "spent_flops": self.spent_flops, "plan": self.plan,
            "messages": self.messages, "entries": self.entries, "checkpoints": self.checkpoints,
            "run_commands": self.run_commands,
        }

    @classmethod
    def from_record(cls, rec: dict, store: Path, llm: Llm) -> "CodeSession":
        mode = rec.get("mode") if rec.get("mode") in MODES else PLAN
        return cls(id=str(rec["id"]), root=str(rec["root"]), model=str(rec.get("model", "")), mode=mode,
                   title=str(rec.get("title") or Path(str(rec["root"])).name), created=float(rec.get("created", 0)),
                   store=store, llm=llm, flop_session_id=str(rec.get("flop_session_id") or ""),
                   budget_flops=float(rec.get("budget_flops") or 0), spent_flops=float(rec.get("spent_flops") or 0),
                   messages=rec.get("messages") or [], entries=rec.get("entries") or [], plan=str(rec.get("plan") or ""),
                   checkpoints=rec.get("checkpoints") or [], run_commands=bool(rec.get("run_commands")))

    def save(self) -> None:
        if self.closed:   # a cancelled turn finishing after its tab closed must not bring the file back
            return
        self.store.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.record()))
        if sys.platform != "win32":
            tmp.chmod(0o600)   # holds project code
        os.replace(tmp, self.path)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)

    # ------------------------------------------------------------- watching

    def notify(self) -> None:
        self.version += 1
        waiters, self._waiters = self._waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)

    async def wait_change(self, since: int, timeout: float) -> bool:
        if self.version > since:
            return True
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            await asyncio.wait_for(fut, timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            if fut in self._waiters:
                self._waiters.remove(fut)

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def meta(self) -> dict:
        return {
            "id": self.id, "root": str(self.ws.root), "model": self.model, "mode": self.mode,
            "title": self.title, "created": self.created, "state": self.state, "activity": self.activity,
            "spent_flops": self.spent_flops, "budget_flops": self.budget_flops,
            "flop_session_id": self.flop_session_id, "pending": self.pending is not None,
            "plan": bool(self.plan), "entries": len(self.entries), "run_commands": self.run_commands,
        }

    def state_view(self) -> dict:
        return {"state": self.state, "activity": self.activity, "mode": self.mode, "model": self.model,
                "run_commands": self.run_commands,
                "title": self.title, "pending": self.pending, "plan": self.plan, "queue": self.queue,
                "spent_flops": self.spent_flops, "budget_flops": self.budget_flops,
                "checkpoints": [checkpoint_view(cp) for cp in self.checkpoints]}

    def view(self, after: int = 0) -> dict:
        return {"session": self.meta(), "entries": self.entries[max(0, after):], "partial": dict(self.partial),
                **self.state_view()}

    def commit(self, kind: str, **data: Any) -> dict:
        entry = {"seq": len(self.entries) + 1, "kind": kind, "ts": time.time(), **data}
        self.entries.append(entry)
        self.save()
        self.notify()
        return entry

    # -------------------------------------------------------------- control

    def start(self, text: str, display: Optional[str] = None) -> None:
        text = (text or "").strip()
        if self.running:
            raise CodeError(409, "This tab is still working. Wait for it or press Stop.")
        if not text:
            raise CodeError(400, "Type a message first.")
        self.plan = ""
        self.state, self.activity = RUNNING, "thinking"
        self.task = asyncio.get_running_loop().create_task(self.run_turn(text, display or text))
        self.notify()

    def stop(self) -> None:
        if self.running:
            self.task.cancel()

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise CodeError(400, f"Mode must be one of {', '.join(MODES)}.")
        if mode == self.mode:
            return
        self.mode = mode
        self.commit("mode", mode=mode)

    def resolve_approval(self, approval_id: str, approve: bool, note: str = "") -> None:
        if self.pending is None or self._decision is None or self._decision.done():
            raise CodeError(409, "Nothing is waiting for approval.")
        if approval_id and approval_id != self.pending["id"]:
            raise CodeError(409, "That approval is out of date.")
        self._decision.set_result((bool(approve), (note or "").strip()))

    def answer_plan(self, action: str, note: str = "") -> None:
        note = (note or "").strip()
        if self.running:
            raise CodeError(409, "This tab is still working.")
        if not self.plan:
            raise CodeError(409, "There is no plan waiting for approval.")
        if action not in PLAN_ACTIONS:
            raise CodeError(400, f"action must be one of {', '.join(PLAN_ACTIONS)}.")
        extra = f" Their note: {note}" if note else ""
        if action == "revise":
            text = f"The user wants changes to the plan: {note}" if note else "Keep planning and refine the plan."
            self.start(text, f"Keep planning. {note}".strip())
            return
        self.set_mode(action)
        self.start(f"The user approved your plan. You are now in {action} mode: implement it step by step.{extra}",
                   f"Approved the plan and switched to {action} mode.{(' ' + note) if note else ''}")

    # ------------------------------------------------------------ the turn

    async def run_turn(self, text: str, display: str) -> None:
        self.messages.append({"role": "user", "content": text})
        entry = self.commit("user", text=display)
        self.turn = {"turn": entry["seq"], "label": clip(display, LABEL_CHARS).replace("\n", " ")}
        inflight: Optional[ToolCall] = None
        nudged = False
        try:
            for _ in range(MAX_STEPS):
                inflight = None
                reply = await self.reply()
                if not strip_thinking(reply.text).strip() and not nudged:
                    # Small models sometimes go quiet after an error; ask once before ending the turn.
                    nudged = True
                    self.messages.append({"role": "assistant", "content": "(empty reply)"})
                    self.messages.append({"role": "user", "content": EMPTY_REPLY_NUDGE})
                    continue
                call = parse_tool_call(reply.text)
                self.messages.append({"role": "assistant", "content": history_text(reply.text) or "(empty reply)"})
                shown = call.narration if call else strip_thinking(reply.text).strip()
                if shown or reply.thinking or call is None:
                    self.commit("assistant", text=clip(shown, ENTRY_TEXT_CHARS * 4),
                                thinking=clip(reply.thinking, ENTRY_TEXT_CHARS), flops=reply.flops)
                if call is None:
                    return
                inflight = call
                if call.error:
                    self.add_result(call, "error", f"Error: {call.error}", call.error)
                    continue
                if call.name == "propose_plan" and self.mode == PLAN:
                    self.plan = call.args["plan"]
                    self.messages.append({"role": "user", "content": tool_result(
                        call.name, "The plan is shown to the user. Wait for their decision.")})
                    inflight = None
                    self.commit("plan", text=self.plan)
                    return
                await self.handle(call)
            inflight = None
            self.commit("error", text=f"Stopped after {MAX_STEPS} tool calls in one turn. Send a message to continue.")
        except LlmError as e:
            self.commit("error", text=str(e))
        except asyncio.CancelledError:
            if inflight is not None:
                self.messages.append({"role": "user", "content": tool_result(
                    inflight.name, "The user pressed Stop; this step did not finish.")})
            self.commit("notice", text="Stopped.")
            raise
        except Exception as e:  # noqa: BLE001  never leave a tab stuck in "running"
            log.exception("code turn failed")
            self.commit("error", text=f"Something went wrong: {e}")
        finally:
            self.state, self.activity, self.queue = IDLE, "", None
            self.partial = {"text": "", "thinking": ""}
            self.pending, self._decision = None, None
            self.turn = None
            self.save()
            self.notify()

    def request_messages(self) -> list[dict]:
        system = {"role": "system", "content": system_prompt(self.mode, str(self.ws.root), top_listing(self.ws))}
        return [system, *merge_consecutive(compact(self.messages))]

    async def reply(self) -> Reply:
        body = {"model": self.model, "messages": self.request_messages(), "max_tokens": MAX_TOKENS,
                "stream": True, "stop": [STOP]}
        if self.flop_session_id:
            body["session_id"] = self.flop_session_id
        text: list[str] = []
        thinking: list[str] = []
        flops = 0.0
        self.partial, self.activity = {"text": "", "thinking": ""}, "thinking"
        self.notify()
        async for event in self.llm(body):
            if event.get("queued"):
                self.queue, self.activity = event["queued"], "waiting in line"
                self.notify()
                continue
            if self.queue is not None:
                self.queue, self.activity = None, "thinking"
            error = event.get("error")
            if error:
                message = error.get("message") if isinstance(error, dict) else str(error)
                raise LlmError(f"The pool returned an error: {message or 'unknown error'}")
            grew = False
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    thinking.append(delta["reasoning_content"])
                    grew = True
                if delta.get("content"):
                    text.append(delta["content"])
                    grew = True
            if grew:
                self.partial = {"text": "".join(text), "thinking": "".join(thinking)}
                self.notify()
            network = event.get("network")
            if isinstance(network, dict):
                flops = float(network.get("flops") or 0)
        self.spent_flops += flops
        self.partial = {"text": "", "thinking": ""}
        return Reply("".join(text), "".join(thinking), flops)

    def add_result(self, call: ToolCall, status: str, result: str, reason: str, diff: str = "",
                   risk: Optional[str] = None) -> None:
        self.messages.append({"role": "user", "content": tool_result(call.name, clip(result, RESULT_CHARS))})
        self.commit("tool", tool=call.name, target=summarize(call), status=status, reason=reason,
                    output=clip(result, ENTRY_TEXT_CHARS), diff=diff, risk=risk)

    async def handle(self, call: ToolCall) -> None:
        decision = decide(self.mode, call.name, call.args, self.ws, self.run_commands)
        if decision.action == DENY:
            self.add_result(call, "denied", f"Not allowed: {decision.reason}.", decision.reason, risk=decision.risk)
            return
        edit = None
        if call.name in EDIT_TOOLS:
            try:
                edit = await asyncio.to_thread(self.preview, call)
            except WorkspaceError as e:
                self.add_result(call, "error", f"Error: {e}", str(e))
                return
        diff = edit.diff if edit else ""
        status = "auto"
        if decision.action == ASK:
            approved, note = await self.ask(call, decision, diff)
            if not approved:
                hint = f" Their note: {note}" if note else " Try another approach, or ask what they want."
                self.add_result(call, "rejected", f"The user rejected this step.{hint}", note or decision.reason,
                                diff, decision.risk)
                return
            status = "approved"
        target = summarize(call)
        self.activity = f"running {target}" if call.name == "run_command" else f"{call.name} {target}"
        self.notify()
        try:
            result = await self.execute(call, edit)
        except WorkspaceError as e:
            self.add_result(call, "error", f"Error: {e}", str(e), diff, decision.risk)
            return
        finally:
            self.activity = "thinking"
        self.add_result(call, status, result, decision.reason, diff, decision.risk)

    def preview(self, call: ToolCall) -> wsmod.Edit:
        a = call.args
        if call.name == "write_file":
            return wsmod.plan_write(self.ws, a.get("path"), a.get("content"))
        if call.name == "edit_file":
            return wsmod.plan_replace(self.ws, a.get("path"), a.get("old"), a.get("new"))
        return wsmod.plan_delete(self.ws, a.get("path"))

    async def execute(self, call: ToolCall, edit: Optional[wsmod.Edit]) -> str:
        a = call.args
        if call.name == "list_files":
            return await asyncio.to_thread(wsmod.list_files, self.ws, a.get("path") or ".")
        if call.name == "read_file":
            return await asyncio.to_thread(wsmod.read_file, self.ws, a.get("path"), a.get("start"), a.get("end"))
        if call.name == "search":
            return await asyncio.to_thread(wsmod.search, self.ws, a.get("pattern"), a.get("path") or ".")
        if call.name in EDIT_TOOLS and edit is not None:
            return await asyncio.to_thread(self.apply_tracked, edit)
        if call.name == "run_command":
            if not self.ws.root.is_dir():
                raise WorkspaceError(f"The project folder {self.ws.root} no longer exists.")
            return (await commands.run(a["command"], self.ws.root)).text()
        raise WorkspaceError(f"{call.name} can't run here.")

    def apply_tracked(self, edit: wsmod.Edit) -> str:
        """Apply an edit, remembering the file's earlier contents in this turn's checkpoint."""
        before = snapshot_file(edit.path)
        result = wsmod.apply(edit)
        if self.turn is None:
            return result
        cp = next((c for c in self.checkpoints if c["turn"] == self.turn["turn"]), None)
        if cp is None:
            cp = {**self.turn, "files": {}, "reverted": False}
            self.checkpoints.append(cp)
        snap = cp["files"].setdefault(edit.rel, before)
        snap["after"] = wsmod.file_hash(edit.path)
        return result

    def revert(self, turn: int) -> dict:
        """Put every file the assistant changed from ``turn`` onward back the way it was, newest first.
        Changes made by shell commands aren't tracked."""
        if self.running:
            raise CodeError(409, "Wait for the tab to finish (or press Stop) before undoing.")
        targets = sorted((c for c in self.checkpoints if c["turn"] >= turn and not c["reverted"]),
                         key=lambda c: c["turn"], reverse=True)
        if not targets:
            raise CodeError(409, "There are no file changes to undo from that point.")
        restored, changed_since, skipped = set(), set(), set()
        for cp in targets:
            for rel, snap in cp["files"].items():
                try:
                    path = self.ws.resolve(rel)
                except WorkspaceError:
                    skipped.add(rel)
                    continue
                if snap.get("too_large"):
                    skipped.add(rel)
                    continue
                if snap.get("after") != wsmod.file_hash(path):
                    changed_since.add(rel)
                restore_file(path, snap)
                restored.add(rel)
            cp["reverted"] = True
        files = sorted(restored)
        self.messages.append({"role": "user", "content": (
            "(The user undid the file changes you made from an earlier point. These files are back to how they "
            f"were before: {', '.join(files) or 'none'}. Read them again before editing.)")})
        text = f"Undid file changes in {len(files)} file{'s' if len(files) != 1 else ''}: {', '.join(files)}."
        if changed_since:
            text += f" Also overwrote later edits to {', '.join(sorted(changed_since))}."
        if skipped:
            text += f" Could not restore {', '.join(sorted(skipped))} (too large or outside the folder)."
        return self.commit("revert", turn=turn, files=files, changed_since=sorted(changed_since),
                           skipped=sorted(skipped), text=text)

    async def ask(self, call: ToolCall, decision: Decision, diff: str) -> tuple[bool, str]:
        self._decision = asyncio.get_running_loop().create_future()
        self.pending = {"id": uuid.uuid4().hex[:10], "tool": call.name, "target": summarize(call),
                        "reason": decision.reason, "risk": decision.risk, "diff": diff}
        self.state, self.activity = WAITING, "waiting for your approval"
        self.notify()
        try:
            return await self._decision
        finally:
            self.pending, self._decision = None, None
            self.state, self.activity = RUNNING, "thinking"
            self.notify()


async def session_events(session: CodeSession, after: int = 0) -> AsyncIterator[dict]:
    """What the UI needs to follow a tab: new transcript entries, the reply being written, and the
    state (mode, approval card, plan, queue). Ends once the tab is idle; heartbeats while waiting."""
    sent, last_state, last_partial = max(0, after), None, None
    while True:
        version = session.version
        for entry in session.entries[sent:]:
            yield {"entry": entry}
            sent = entry["seq"]
        state = session.state_view()
        if state != last_state:
            yield {"state": state}
            last_state = state
        partial = dict(session.partial)
        if partial != last_partial:
            yield {"partial": partial}
            last_partial = partial
        if session.version != version:
            continue   # the turn moved on while we were sending; catch up before deciding we're done
        if session.state == IDLE and not session.running and sent >= len(session.entries):
            yield {"done": True}
            return
        if await session.wait_change(version, HEARTBEAT_S):
            await asyncio.sleep(COALESCE_S)
        else:
            yield {"ping": True}


# ------------------------------------------------------------ all tabs


def parse_pflops(value: Any) -> float:
    try:
        pflops = float(value or 0)
    except (TypeError, ValueError) as e:
        raise CodeError(400, "The budget must be a number of PFLOPs.") from e
    if pflops < 0:
        raise CodeError(400, "The budget can't be negative.")
    return pflops


class CodeManager:
    def __init__(self, home: Path, settings: Callable[[], Any], http: httpx.AsyncClient,
                 llm: Optional[Llm] = None) -> None:
        self.home = Path(home)
        self.dir = self.home / "code"
        self.settings = settings
        self.http = http
        self.llm = llm or pool_llm(http, settings)
        self.sessions: dict[str, CodeSession] = {s.id: s for s in self.load()}

    def load(self) -> list[CodeSession]:
        sessions = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                sessions.append(CodeSession.from_record(json.loads(path.read_text()), self.dir, self.llm))
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("skipping unreadable code session %s", path)
        return sessions

    def list(self) -> list[dict]:
        return [s.meta() for s in sorted(self.sessions.values(), key=lambda s: s.created)]

    def get(self, sid: str) -> CodeSession:
        session = self.sessions.get(sid)
        if session is None:
            raise CodeError(404, "No such tab (it may have been closed).")
        return session

    async def flop_session(self, model: str, pflops: float) -> str:
        """A server chat session that caps this tab's spending (POST /sessions)."""
        current = self.settings()
        if not current.server_url:
            raise CodeError(503, "Connect to a main server first.")
        try:
            r = await self.http.post(f"{current.server_url}/sessions", json={"model": model, "max_flops": pflops * PFLOP},
                                     headers=auth_headers(current), timeout=REQUEST_TIMEOUT)
        except httpx.HTTPError as e:
            raise CodeError(502, f"Main server unreachable at {current.server_url}: {e}") from e
        if r.status_code >= 400:
            raise CodeError(r.status_code if r.status_code < 500 else 502, response_detail(r))
        return str(r.json()["id"])

    async def create(self, body: dict) -> CodeSession:
        try:
            root = wsmod.validate_root(str(body.get("root") or ""), forbidden=[self.home])
        except WorkspaceError as e:
            raise CodeError(400, str(e)) from e
        model = str(body.get("model") or "").strip()
        if not model:
            raise CodeError(400, "Pick a model (Refresh models if the list is empty).")
        mode = str(body.get("mode") or PLAN)
        if mode not in MODES:
            raise CodeError(400, f"Mode must be one of {', '.join(MODES)}.")
        pflops = parse_pflops(body.get("budget_pflops"))
        flop_session_id = await self.flop_session(model, pflops) if pflops > 0 else ""
        session = CodeSession(id=uuid.uuid4().hex[:12], root=str(root), model=model, mode=mode,
                              title=str(body.get("title") or root.name).strip() or root.name,
                              created=time.time(), store=self.dir, llm=self.llm,
                              flop_session_id=flop_session_id, budget_flops=pflops * PFLOP,
                              run_commands=bool(body.get("run_commands")))
        self.sessions[session.id] = session
        session.commit("notice", text=f"Opened {root} with {model} in {mode} mode.")
        return session

    async def update(self, sid: str, body: dict) -> CodeSession:
        session = self.get(sid)
        if "mode" in body:
            session.set_mode(str(body["mode"]))
        if "run_commands" in body and bool(body["run_commands"]) != session.run_commands:
            session.run_commands = bool(body["run_commands"])
            session.commit("notice", text="Auto mode will run commands without asking in this tab."
                           if session.run_commands else "Auto mode will ask before running commands in this tab.")
        if "title" in body and str(body["title"]).strip():
            session.title = str(body["title"]).strip()
        if "model" in body and str(body["model"]).strip() and str(body["model"]).strip() != session.model:
            session.model = str(body["model"]).strip()
            session.commit("notice", text=f"Switched to {session.model}.")
        if "budget_pflops" in body:
            pflops = parse_pflops(body["budget_pflops"])
            session.flop_session_id = await self.flop_session(session.model, pflops) if pflops > 0 else ""
            session.budget_flops, session.spent_flops = pflops * PFLOP, 0.0   # spent counts against this budget
            session.commit("notice", text=f"New budget: {pflops:g} PFLOPs." if pflops else "Budget removed (no cap).")
        session.save()
        session.notify()
        return session

    def close(self, sid: str) -> None:
        session = self.get(sid)
        session.closed = True
        session.stop()
        del self.sessions[sid]
        session.delete()
