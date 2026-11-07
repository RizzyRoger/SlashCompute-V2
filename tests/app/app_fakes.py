from __future__ import annotations

import json
from typing import Any, Optional


class FakeProc:
    """Stands in for subprocess.Popen; ``exit_code`` None means still running."""

    def __init__(self, pid: int = 4242, exit_code: Optional[int] = None) -> None:
        self.pid = pid
        self.exit_code = exit_code

    def poll(self) -> Optional[int]:
        return self.exit_code


class Recorder:
    def __init__(self, exit_code: Optional[int] = None, log_text: str = "") -> None:
        self.calls: list[dict[str, Any]] = []
        self.exit_code = exit_code
        self.log_text = log_text

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProc:
        self.calls.append({"argv": argv, **kwargs})
        if self.log_text:
            kwargs["stdout"].write(self.log_text.encode())
            kwargs["stdout"].flush()
        return FakeProc(exit_code=self.exit_code)



def sse(*events: Any) -> bytes:
    lines = [f"data: {json.dumps(e) if not isinstance(e, str) else e}\n\n" for e in events]
    return "".join(lines).encode()
