from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Union

from compute.app.code.harness import CodeSession, LlmError
from compute.app.code.protocol import STOP

Script = Union[str, Exception, Callable[[dict], str]]

BUGGY_APP = "def add(a, b):\n    return a - b\n"
FIX_ADD = ('Fixing the sign.\n<tool name="edit_file"><path>app.py</path>'
           '<old>return a - b</old><new>return a + b</new></tool>')


def tool(name: str, **params: str) -> str:
    inner = "".join(f"<{k}>{v}</{k}>" for k, v in params.items())
    return f'<tool name="{name}">{inner}</tool>'


class ScriptedLlm:
    """Plays back replies like a pool model would stream them (cut at the stop sequence)."""

    def __init__(self, replies: list[Script], flops: float = 1e12, default: str = "All done.") -> None:
        self.replies = list(replies)
        self.flops = flops
        self.default = default
        self.bodies: list[dict] = []

    def __call__(self, body: dict):
        self.bodies.append(body)
        script = self.replies.pop(0) if self.replies else self.default

        async def stream():
            if isinstance(script, Exception):
                raise script
            text = script(body) if callable(script) else script
            text = text.split(STOP)[0]
            yield {"queued": {"queue_id": "q-1", "position": 1, "eta_s": 0.1}}
            for i in range(0, len(text), 7):
                yield {"choices": [{"delta": {"content": text[i:i + 7]}}]}
            yield {"choices": [], "usage": {}, "network": {"flops": self.flops}}

        return stream()

    def last_user(self, index: int = -1) -> str:
        messages = self.bodies[index]["messages"]
        return next(m["content"] for m in reversed(messages) if m["role"] == "user")


def make_session(store: Path, root: Path, llm: Any, mode: str) -> CodeSession:
    return CodeSession(id="tab1", root=str(root), model="qwen-test", mode=mode, title=root.name,
                       created=0.0, store=store, llm=llm)


async def run_turn(session: CodeSession, text: str) -> None:
    session.start(text)
    await session.task


async def wait_pending(session: CodeSession, timeout: float = 5.0) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while session.pending is None:
        if loop.time() > deadline:
            raise AssertionError("no approval request appeared")
        await asyncio.sleep(0.01)
    return session.pending


async def answer(session: CodeSession, approve: bool, note: str = "") -> dict:
    pending = await wait_pending(session)
    session.resolve_approval(pending["id"], approve, note)
    return pending


__all__ = ["BUGGY_APP", "FIX_ADD", "LlmError", "ScriptedLlm", "answer", "make_session", "run_turn", "tool",
           "wait_pending"]
