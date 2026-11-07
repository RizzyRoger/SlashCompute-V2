from __future__ import annotations

import logging
import os
import sys


def setup_logging(name: str) -> logging.Logger:
    level = os.environ.get("COMPUTE_LOG", "INFO").upper()
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname).1s [%(name)s] %(message)s", "%H:%M:%S")
        )
        root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(name)
