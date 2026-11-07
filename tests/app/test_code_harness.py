from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from code_fakes import BUGGY_APP, FIX_ADD, ScriptedLlm, answer, make_session, run_turn, tool, wait_pending
from compute.app.code import commands, harness
from compute.app.code.harness import (
    IDLE, CodeError, CodeManager, LlmError, compact, merge_consecutive, pool_llm, session_events,
)
from compute.app.code.policy import AUTO, MANUAL, PLAN
from compute.app.code.protocol import history_text, parse_tool_call, system_prompt
from compute.app.controller import AppSettings
from app_fakes import sse


@pytest.fixture(autouse=True)
def plain_path(monkeypatch: pytest.MonkeyPatch):
    """Don't start a login shell to find PATH in tests."""
    monkeypatch.setattr(commands, "login_path", lambda: None)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = (tmp_path / "proj").resolve()
    root.mkdir()
    (root / "app.py").write_text(BUGGY_APP)
    return root


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "home" / "code"


def kinds(session) -> list[str]:
    return [e["kind"] for e in session.entries]


def tools(session) -> list[dict]:
    return [e for e in session.entries if e["kind"] == "tool"]


# ------------------------------------------------------------- the modes


async def test_auto_mode_applies_edits_without_asking(project, store):
    llm = ScriptedLlm([FIX_ADD, "Fixed add() to return a + b."])
    session = make_session(store, project, llm, AUTO)
    await run_turn(session, "add() is wrong, fix it")

    assert (project / "app.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert kinds(session) == ["user", "assistant", "tool", "assistant"]
    edit = tools(session)[0]
    assert edit["status"] == "auto" and edit["target"] == "app.py" and "+    return a + b" in edit["diff"]
    assert session.entries[-1]["text"] == "Fixed add() to return a + b."
    assert session.state == IDLE and session.spent_flops == pytest.approx(2e12)
    first = llm.bodies[0]
    assert first["stop"] == ["</tool>"] and first["stream"] is True and first["model"] == "qwen-test"
    assert first["messages"][0]["role"] == "system" and "AUTO MODE" in first["messages"][0]["content"]
    assert llm.last_user(1).startswith('<tool_result name="edit_file">') and "Edited app.py" in llm.last_user(1)
    assert llm.bodies[1]["messages"][2]["content"].endswith("</tool>")   # cut stop sequence restored


async def test_manual_mode_waits_for_approval_then_applies(project, store):
    llm = ScriptedLlm([FIX_ADD, "Done."])
    session = make_session(store, project, llm, MANUAL)
    session.start("fix add")
    pending = await wait_pending(session)
    assert session.state == "waiting" and pending["tool"] == "edit_file"
    assert "+    return a + b" in pending["diff"] and "manual mode" in pending["reason"]
    assert "a - b" in (project / "app.py").read_text()   # nothing written yet
    session.resolve_approval(pending["id"], True)
    await session.task
    assert "a + b" in (project / "app.py").read_text()
    assert tools(session)[0]["status"] == "approved"


async def test_manual_reject_keeps_the_file_and_tells_the_model(project, store):
    llm = ScriptedLlm([FIX_ADD, "OK, I'll leave it."])
    session = make_session(store, project, llm, MANUAL)
    session.start("fix add")
    await answer(session, approve=False, note="use operator.add instead")
    await session.task
    assert (project / "app.py").read_text() == BUGGY_APP
    assert tools(session)[0]["status"] == "rejected"
    assert "rejected" in llm.last_user(1) and "use operator.add instead" in llm.last_user(1)


async def test_manual_mode_asks_even_for_read_only_commands(project, store):
    llm = ScriptedLlm([tool("run_command", command="ls"), "Listed."])
    session = make_session(store, project, llm, MANUAL)
    session.start("what's here?")
    pending = await answer(session, approve=True)
    await session.task
    assert pending["tool"] == "run_command" and pending["target"] == "ls"
    assert "app.py" in tools(session)[0]["output"]


async def test_plan_mode_is_read_only_then_approving_switches_to_auto(project, store):
    llm = ScriptedLlm([
        tool("read_file", path="app.py"),
        tool("write_file", path="app.py", content="oops\n"),
        tool("run_command", command="touch hacked.txt"),
        tool("run_command", command="git status 2>/dev/null || ls"),
        "Plan below.\n" + tool("propose_plan", plan="1. Change - to + in app.py\n2. Run the tests"),
        FIX_ADD,
        "Implemented the plan.",
    ])
    session = make_session(store, project, llm, PLAN)
    await run_turn(session, "fix add")

    statuses = [(t["tool"], t["status"]) for t in tools(session)]
    assert statuses == [("read_file", "auto"), ("write_file", "denied"), ("run_command", "denied"),
                        ("run_command", "auto")]
    assert (project / "app.py").read_text() == BUGGY_APP and not (project / "hacked.txt").exists()
    assert session.plan.startswith("1. Change - to +") and session.entries[-1]["kind"] == "plan"
    assert "PLAN MODE" in llm.bodies[0]["messages"][0]["content"]
    assert "plan mode is read-only" in llm.last_user(2)

    session.answer_plan(AUTO, "keep it small")
    await session.task
    assert session.mode == AUTO and session.plan == ""
    assert "a + b" in (project / "app.py").read_text()
    assert "approved your plan" in llm.bodies[5]["messages"][-1]["content"]
    assert "keep it small" in llm.bodies[5]["messages"][-1]["content"]
    assert "AUTO MODE" in llm.bodies[5]["messages"][0]["content"]
    assert [e["kind"] for e in session.entries[-5:]] == ["mode", "user", "assistant", "tool", "assistant"]


async def test_plan_revise_keeps_planning(project, store):
    llm = ScriptedLlm([tool("propose_plan", plan="1. Rewrite everything"),
                       tool("propose_plan", plan="1. Only fix the sign")])
    session = make_session(store, project, llm, PLAN)
    await run_turn(session, "fix add")
    session.answer_plan("revise", "smaller please")
    await session.task
    assert session.mode == PLAN and session.plan == "1. Only fix the sign"
    assert "smaller please" in llm.bodies[1]["messages"][-1]["content"]
    with pytest.raises(CodeError):
        session.answer_plan("ship-it")


async def test_auto_mode_asks_for_powerful_commands(project, store):
    llm = ScriptedLlm([tool("run_command", command="rm app.py"), "I won't delete it."])
    session = make_session(store, project, llm, AUTO)
    session.start("clean up")
    pending = await answer(session, approve=False)
    await session.task
    assert pending["risk"] == "powerful" and "rm deletes files" in pending["reason"]
    assert (project / "app.py").exists()


async def test_auto_mode_asks_before_running_programs_by_default(project, store):
    llm = ScriptedLlm([tool("run_command", command="echo hello > out.txt && cat out.txt"), "Wrote out.txt."])
    session = make_session(store, project, llm, AUTO)
    session.start("write a file")
    pending = await answer(session, approve=False)
    await session.task
    assert pending["tool"] == "run_command" and "unless this tab allows it" in pending["reason"]
    assert not (project / "out.txt").exists() and tools(session)[0]["status"] == "rejected"


async def test_auto_mode_runs_normal_commands_when_the_tab_allows_it(project, store):
    llm = ScriptedLlm([tool("run_command", command="echo hello > out.txt && cat out.txt"), "Wrote out.txt."])
    session = make_session(store, project, llm, AUTO)
    session.run_commands = True
    await run_turn(session, "write a file")
    assert (project / "out.txt").read_text() == "hello\n"
    result = tools(session)[0]
    assert result["status"] == "auto" and result["output"].startswith("[exit code 0]") and "hello" in result["output"]


async def test_auto_mode_asks_before_deleting(project, store):
    llm = ScriptedLlm([tool("delete_file", path="app.py"), "Deleted."])
    session = make_session(store, project, llm, AUTO)
    session.start("delete app.py")
    pending = await answer(session, approve=True)
    await session.task
    assert pending["tool"] == "delete_file" and "-    return a - b" in pending["diff"]
    assert not (project / "app.py").exists()


async def test_mode_switch_applies_to_the_next_tool_call(project, store):
    def switch_then_edit(body: dict) -> str:
        session.set_mode(AUTO)
        return FIX_ADD

    llm = ScriptedLlm([switch_then_edit, "Done."])
    session = make_session(store, project, llm, MANUAL)
    await run_turn(session, "fix add")
    assert tools(session)[0]["status"] == "auto"
    assert "a + b" in (project / "app.py").read_text()


# --------------------------------------------------------- errors, limits


async def test_edit_goes_stale_while_waiting_for_approval(project, store):
    llm = ScriptedLlm([FIX_ADD, "I'll re-read it."])
    session = make_session(store, project, llm, MANUAL)
    session.start("fix add")
    pending = await wait_pending(session)
    (project / "app.py").write_text(BUGGY_APP + "# edited meanwhile\n")
    session.resolve_approval(pending["id"], True)
    await session.task
    assert tools(session)[0]["status"] == "error" and "changed since" in tools(session)[0]["output"]
    assert "# edited meanwhile" in (project / "app.py").read_text()


async def test_malformed_and_unknown_tool_calls_get_feedback(project, store):
    llm = ScriptedLlm(['<tool name="edit_file"><path>app.py</path>', tool("format_disk"), "Giving up."])
    session = make_session(store, project, llm, AUTO)
    await run_turn(session, "fix add")
    assert [t["status"] for t in tools(session)] == ["error", "error"]
    assert "missing <old>, <new>" in llm.last_user(1)
    assert "no tool named 'format_disk'" in llm.last_user(2)


async def test_step_cap_ends_a_runaway_turn(project, store, monkeypatch):
    monkeypatch.setattr(harness, "MAX_STEPS", 3)
    llm = ScriptedLlm([], default=tool("list_files", path="."))
    session = make_session(store, project, llm, AUTO)
    await run_turn(session, "loop forever")
    assert len(llm.bodies) == 3 and session.entries[-1]["kind"] == "error"
    assert "Stopped after 3 tool calls" in session.entries[-1]["text"]


async def test_pool_errors_end_the_turn_cleanly(project, store):
    llm = ScriptedLlm([LlmError("This tab's FLOP budget is used up.")])
    session = make_session(store, project, llm, AUTO)
    await run_turn(session, "hi")
    assert session.entries[-1] == {**session.entries[-1], "kind": "error", "text": "This tab's FLOP budget is used up."}
    assert session.state == IDLE and not session.running


async def test_stop_kills_a_running_command(project, store):
    llm = ScriptedLlm([tool("run_command", command="sleep 30"), "never reached"])
    session = make_session(store, project, llm, AUTO)
    session.start("wait a while")
    for _ in range(200):
        if session.activity.startswith("running"):
            break
        await asyncio.sleep(0.02)
    started = time.monotonic()
    session.stop()
    with pytest.raises(asyncio.CancelledError):
        await session.task
    assert time.monotonic() - started < 3
    assert session.entries[-1]["text"] == "Stopped." and session.state == IDLE
    assert "pressed Stop" in session.messages[-1]["content"]
    # The next turn still works, with the stopped step in the history.
    llm.replies = ["Ready."]
    await run_turn(session, "carry on")
    assert session.entries[-1]["text"] == "Ready."


async def test_stop_while_waiting_for_approval(project, store):
    llm = ScriptedLlm([FIX_ADD])
    session = make_session(store, project, llm, MANUAL)
    session.start("fix add")
    await wait_pending(session)
    session.stop()
    with pytest.raises(asyncio.CancelledError):
        await session.task
    assert session.pending is None and (project / "app.py").read_text() == BUGGY_APP
    with pytest.raises(CodeError, match="Nothing is waiting"):
        session.resolve_approval("x", True)


async def test_busy_tab_refuses_a_second_message(project, store):
    llm = ScriptedLlm([FIX_ADD])
    session = make_session(store, project, llm, MANUAL)
    session.start("fix add")
    with pytest.raises(CodeError) as err:
        session.start("and another thing")
    assert err.value.status == 409
    session.stop()
    with pytest.raises(asyncio.CancelledError):
        await session.task


# --------------------------------------------------------------- events


async def test_session_events_follow_a_turn_to_the_end(project, store):
    llm = ScriptedLlm([FIX_ADD, "Done."])
    session = make_session(store, project, llm, MANUAL)
    session.start("fix add")
    seen: list[dict] = []

    async def collect():
        async for event in session_events(session, 0):
            seen.append(event)

    follower = asyncio.ensure_future(collect())
    pending = await wait_pending(session)
    for _ in range(300):   # like a person: approve only once the card has been shown
        if any(e.get("state", {}).get("pending") for e in seen):
            break
        await asyncio.sleep(0.01)
    session.resolve_approval(pending["id"], True)
    await session.task
    await asyncio.wait_for(follower, 5)
    entries = [e["entry"]["kind"] for e in seen if "entry" in e]
    assert entries == ["user", "assistant", "tool", "assistant"]
    assert any(e.get("state", {}).get("pending") for e in seen)
    assert seen[-1] == {"done": True}


async def test_events_catch_up_when_the_turn_ends_mid_stream(project, store):
    """Regression: the stream used to send `done` without the entries added while it was yielding."""
    llm = ScriptedLlm([FIX_ADD, "Done."])
    session = make_session(store, project, llm, AUTO)
    session.start("fix add")
    await asyncio.sleep(0)                      # the turn commits the user entry, then waits on the model
    events = session_events(session, 0)
    seen = [await events.__anext__()]           # paused right after the first entry...
    await session.task                          # ...while the whole turn finishes
    seen += [event async for event in events]
    assert [e["entry"]["kind"] for e in seen if "entry" in e] == ["user", "assistant", "tool", "assistant"]
    assert seen[-1] == {"done": True}


async def test_queue_and_partial_reply_are_visible_while_streaming(project, store):
    queued, writing = asyncio.Event(), asyncio.Event()

    def llm(body: dict):
        async def stream():
            yield {"queued": {"queue_id": "q-9", "position": 2, "eta_s": 3.0}}
            await queued.wait()
            yield {"choices": [{"delta": {"reasoning_content": "Let me think."}}]}
            yield {"choices": [{"delta": {"content": "Hello"}}]}
            await writing.wait()
            yield {"choices": [{"delta": {"content": " there."}}]}
        return stream()

    session = make_session(store, project, llm, AUTO)
    session.start("hi")
    seen: list[dict] = []

    async def collect():
        async for event in session_events(session, 0):
            seen.append(event)

    follower = asyncio.ensure_future(collect())

    async def until(check):
        for _ in range(300):
            if check():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition never met")

    await until(lambda: any((e.get("state") or {}).get("queue") for e in seen))
    assert session.activity == "waiting in line" and session.queue["position"] == 2
    queued.set()
    await until(lambda: any((e.get("partial") or {}).get("text") == "Hello" for e in seen))
    assert session.queue is None and session.partial["thinking"] == "Let me think."
    writing.set()
    await session.task
    await asyncio.wait_for(follower, 5)
    assert session.entries[-1]["text"] == "Hello there." and session.entries[-1]["thinking"] == "Let me think."
    assert session.partial == {"text": "", "thinking": ""}


# ---------------------------------------------------------- persistence


def manager(home: Path, llm, routes=None) -> CodeManager:
    def handler(request: httpx.Request) -> httpx.Response:
        fn = (routes or {}).get(request.url.path)
        return fn(request) if fn else httpx.Response(404, json={"detail": "not found"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = AppSettings(server_url="http://pool:8765", session_token="tok")
    return CodeManager(home, lambda: settings, http, llm=llm)


async def test_tabs_survive_a_restart_and_close_deletes_them(project, tmp_path):
    home = tmp_path / "home"
    llm = ScriptedLlm([FIX_ADD, "Done."])
    first = manager(home, llm)
    session = await first.create({"root": str(project), "model": "qwen-test", "mode": "auto"})
    await run_turn(session, "fix add")

    again = manager(home, llm)
    restored = again.get(session.id)
    assert restored.entries == session.entries and restored.messages == session.messages
    assert restored.mode == AUTO and restored.state == IDLE and restored.ws.root == project
    assert [m["id"] for m in again.list()] == [session.id]

    again.close(session.id)
    assert again.list() == [] and not (home / "code" / f"{session.id}.json").exists()
    with pytest.raises(CodeError) as err:
        again.get(session.id)
    assert err.value.status == 404


async def test_pending_plan_survives_a_restart(project, tmp_path):
    home = tmp_path / "home"
    llm = ScriptedLlm([tool("propose_plan", plan="1. fix it")])
    first = manager(home, llm)
    session = await first.create({"root": str(project), "model": "m", "mode": "plan"})
    await run_turn(session, "fix add")
    assert manager(home, llm).get(session.id).plan == "1. fix it"


async def test_create_validates_and_opens_a_flop_session(project, tmp_path):
    home = tmp_path / "home"
    seen = {}

    def sessions(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "srv-1", "model": "m", "max_flops": 2e15, "spent": 0})

    mgr = manager(home, ScriptedLlm([]), {"/sessions": sessions})
    session = await mgr.create({"root": str(project), "model": "m", "mode": "manual", "budget_pflops": 2})
    assert session.flop_session_id == "srv-1" and session.budget_flops == 2e15
    assert seen == {"body": {"model": "m", "max_flops": 2e15}, "auth": "Bearer tok"}
    assert session.entries[0]["kind"] == "notice"

    for body, message in [
        ({"root": str(project), "model": ""}, "Pick a model"),
        ({"root": str(project), "model": "m", "mode": "yolo"}, "Mode must be"),
        ({"root": str(project), "model": "m", "budget_pflops": -1}, "negative"),
        ({"root": str(home), "model": "m"}, "own settings"),
        ({"root": "relative", "model": "m"}, "full path"),
    ]:
        with pytest.raises(CodeError, match=message):
            await mgr.create(body)


# ------------------------------------------------------------ the pool


async def test_pool_llm_streams_events_with_auth():
    captured = {}

    def chat(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=sse({"queued": {"position": 1}},
                                               {"choices": [{"delta": {"content": "hi"}}]},
                                               ": keep-alive", "[DONE]"),
                              headers={"content-type": "text/event-stream"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: chat(r)))
    stream = pool_llm(http, lambda: AppSettings(server_url="http://pool:8765", session_token="tok"))
    events = [e async for e in stream({"model": "m", "messages": []})]
    assert events == [{"queued": {"position": 1}}, {"choices": [{"delta": {"content": "hi"}}]}]
    assert captured["auth"] == "Bearer tok"


@pytest.mark.parametrize("status, detail, message", [
    (402, "budget_exhausted", "budget is used up"),
    (402, "insufficient FLOPs", "Not enough FLOPs"),
    (401, "missing", "Sign in first"),
    (503, "no capacity", "No capacity"),
    (404, "unknown model", "doesn't serve this model"),
])
async def test_pool_llm_errors_are_explained(status, detail, message):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"detail": detail})))
    stream = pool_llm(http, lambda: AppSettings(server_url="http://pool:8765"))
    with pytest.raises(LlmError, match=message):
        [e async for e in stream({})]


async def test_pool_llm_needs_a_server():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(LlmError, match="Not connected"):
        [e async for e in pool_llm(http, lambda: AppSettings())({})]


# ----------------------------------------------------- protocol + history


def test_parse_tool_call_variants():
    assert parse_tool_call("All done, add() is fixed.") is None
    call = parse_tool_call('Let me look.\n```xml\n<tool name="read_file"><path> src/app.py </path>')
    assert call.name == "read_file" and call.args == {"path": "src/app.py"} and call.narration == "Let me look."
    write = parse_tool_call('<tool name="write_file"><path>a.html</path><content>\n<p>keep </content> inside</p>\n</content>')
    assert write.args["content"] == "<p>keep </content> inside</p>\n"
    edit = parse_tool_call("<tool name='edit_file'><path>a.py</path><old>\n    x = 1\n</old><new>\n    x = 2\n</new></tool>")
    assert edit.args == {"path": "a.py", "old": "    x = 1", "new": "    x = 2"}
    thought = parse_tool_call('<think>maybe <tool name="delete_file"><path>a</path></tool></think>Done.')
    assert thought is None
    missing = parse_tool_call('<tool name="run_command"></tool>').error
    assert missing.startswith("run_command is missing <command>.")
    assert '<tool name="run_command"><command>...</command></tool>' in missing


def test_single_value_tools_accept_a_bare_body():
    """Seen with gemma-4-12B: the plan written straight inside the tool tag."""
    plan = parse_tool_call('Here is the plan.\n\n<tool name="propose_plan">\n1. Change + to *\n2. Run the tests</tool>')
    assert plan.error == "" and plan.args == {"plan": "1. Change + to *\n2. Run the tests"}
    assert parse_tool_call('<tool name="run_command">python3 -m pytest -q</tool>').args == {"command": "python3 -m pytest -q"}
    assert parse_tool_call('<tool name="read_file">calc.py</tool>').args == {"path": "calc.py"}
    stray = parse_tool_call('<tool name="propose_plan">1. Edit calc.py\n2. Run the tests\n</tool_result>\n</tool>')
    assert stray.args == {"plan": "1. Edit calc.py\n2. Run the tests"}   # also seen with gemma-4-12B
    assert parse_tool_call('<tool name="edit_file">app.py</tool>').error.startswith("edit_file is missing <path>")


def test_history_text_restores_the_stop_sequence():
    assert history_text('Reading.\n<tool name="read_file"><path>a</path>') == \
        'Reading.\n<tool name="read_file"><path>a</path></tool>'
    assert history_text("<think>hmm</think>Final answer.") == "Final answer."


def test_system_prompt_names_the_mode_and_folder():
    prompt = system_prompt(PLAN, "/work/proj", "app.py")
    assert "PLAN MODE" in prompt and "/work/proj" in prompt and "propose_plan" in prompt and "app.py" in prompt


def test_compact_keeps_recent_and_first_messages():
    big = "x" * 30_000
    messages = [
        {"role": "user", "content": "the task " + big},
        {"role": "assistant", "content": '<tool name="read_file"><path>a</path></tool>'},
        {"role": "user", "content": f'<tool_result name="read_file">\n{big}\n</tool_result>'},
        {"role": "assistant", "content": '<tool name="read_file"><path>b</path></tool>'},
        {"role": "user", "content": f'<tool_result name="read_file">\n{big}\n</tool_result>'},
    ]
    out = compact(messages, budget=40_000)
    assert out[0] == messages[0] and out[4] == messages[4]
    assert "older output removed" in out[2]["content"] and out[2]["content"].startswith('<tool_result name="read_file">')


def test_merge_consecutive_joins_same_role():
    merged = merge_consecutive([{"role": "user", "content": "a"}, {"role": "user", "content": "b"},
                                {"role": "assistant", "content": "c"}])
    assert merged == [{"role": "user", "content": "a\n\nb"}, {"role": "assistant", "content": "c"}]


# ---------------------------------------------------------- checkpoints


def user_seqs(session) -> list[int]:
    return [e["seq"] for e in session.entries if e["kind"] == "user"]


async def two_turns(project: Path, store: Path):
    llm = ScriptedLlm([
        FIX_ADD,
        tool("write_file", path="pkg/new.py", content="VALUE = 1\n"),
        "Turn one done.",
        tool("edit_file", path="app.py", old="return a + b", new="return b + a"),
        tool("edit_file", path="app.py", old="return b + a", new="return (b + a)"),
        "Turn two done.",
    ])
    session = make_session(store, project, llm, AUTO)
    await run_turn(session, "fix add and add a constant")
    await run_turn(session, "swap the operands")
    return session, llm


async def test_undo_the_last_turn(project, store):
    session, llm = await two_turns(project, store)
    first, second = user_seqs(session)
    views = session.state_view()["checkpoints"]
    assert views == [
        {"turn": first, "label": "fix add and add a constant", "files": ["app.py", "pkg/new.py"], "reverted": False},
        {"turn": second, "label": "swap the operands", "files": ["app.py"], "reverted": False},
    ]
    assert "data" not in json.dumps(views)   # file contents stay out of the UI state

    entry = session.revert(second)
    assert (project / "app.py").read_text() == "def add(a, b):\n    return a + b\n"   # before turn two, not mid-turn
    assert (project / "pkg" / "new.py").exists()
    assert entry["kind"] == "revert" and entry["files"] == ["app.py"] and entry["changed_since"] == []
    assert "undid the file changes" in session.messages[-1]["content"]
    assert session.state_view()["checkpoints"][1]["reverted"] is True
    with pytest.raises(CodeError, match="no file changes to undo"):
        session.revert(second)

    # The next request tells the model, merged into the user's next message.
    llm.replies = ["Noted."]
    await run_turn(session, "thanks")
    last = llm.bodies[-1]["messages"][-1]
    assert last["role"] == "user" and "undid the file changes" in last["content"] and last["content"].endswith("thanks")


async def test_undo_from_an_earlier_turn_rewinds_everything_after_it(project, store):
    session, _ = await two_turns(project, store)
    first, _second = user_seqs(session)
    entry = session.revert(first)
    assert (project / "app.py").read_text() == BUGGY_APP
    assert not (project / "pkg" / "new.py").exists()
    assert entry["files"] == ["app.py", "pkg/new.py"] and entry["changed_since"] == []
    assert all(cp["reverted"] for cp in session.state_view()["checkpoints"])


async def test_undo_reports_edits_made_since(project, store):
    session, _ = await two_turns(project, store)
    (project / "app.py").write_text("# my own edit\n")
    entry = session.revert(user_seqs(session)[1])
    assert entry["changed_since"] == ["app.py"] and "overwrote later edits to app.py" in entry["text"]
    assert (project / "app.py").read_text() == "def add(a, b):\n    return a + b\n"


async def test_undo_restores_a_deleted_binary_file(project, store):
    (project / "logo.png").write_bytes(b"\x89PNG\0\0\1\2")
    llm = ScriptedLlm([tool("delete_file", path="logo.png"), "Deleted."])
    session = make_session(store, project, llm, MANUAL)
    session.start("remove the logo")
    await answer(session, approve=True)
    await session.task
    assert not (project / "logo.png").exists()
    session.revert(user_seqs(session)[0])
    assert (project / "logo.png").read_bytes() == b"\x89PNG\0\0\1\2"


async def test_rejected_and_failed_edits_leave_no_checkpoint(project, store):
    llm = ScriptedLlm([FIX_ADD, tool("edit_file", path="app.py", old="nope", new="x"), "OK."])
    session = make_session(store, project, llm, MANUAL)
    session.start("fix add")
    await answer(session, approve=False)
    await session.task
    assert session.checkpoints == []
    with pytest.raises(CodeError):
        session.revert(1)


async def test_undo_waits_for_a_running_turn(project, store):
    llm = ScriptedLlm([FIX_ADD])
    session = make_session(store, project, llm, MANUAL)
    session.start("fix add")
    await wait_pending(session)
    with pytest.raises(CodeError, match="Wait for the tab"):
        session.revert(1)
    session.stop()
    with pytest.raises(asyncio.CancelledError):
        await session.task


async def test_checkpoints_survive_a_restart(project, tmp_path):
    home = tmp_path / "home"
    llm = ScriptedLlm([FIX_ADD, "Done."])
    session = await manager(home, llm).create({"root": str(project), "model": "m", "mode": "auto"})
    await run_turn(session, "fix add")
    restored = manager(home, llm).get(session.id)
    restored.revert(user_seqs(restored)[0])
    assert (project / "app.py").read_text() == BUGGY_APP


async def test_an_empty_reply_gets_one_nudge(project, store):
    llm = ScriptedLlm(["", FIX_ADD, "Fixed."])
    session = make_session(store, project, llm, AUTO)
    await run_turn(session, "fix add")
    assert "a + b" in (project / "app.py").read_text()
    assert llm.bodies[1]["messages"][-1]["content"].startswith("Your last reply was empty")
    assert kinds(session) == ["user", "assistant", "tool", "assistant"]


async def test_a_second_empty_reply_ends_the_turn(project, store):
    llm = ScriptedLlm(["", ""])
    session = make_session(store, project, llm, AUTO)
    await run_turn(session, "fix add")
    assert len(llm.bodies) == 2 and kinds(session) == ["user", "assistant"]
    assert session.entries[-1]["text"] == "" and session.state == IDLE


async def test_plan_with_a_bare_body_is_shown(project, store):
    llm = ScriptedLlm(['<tool name="propose_plan">1. Change - to +\n2. Run the test</tool>'])
    session = make_session(store, project, llm, PLAN)
    await run_turn(session, "fix add")
    assert session.plan == "1. Change - to +\n2. Run the test" and session.entries[-1]["kind"] == "plan"
