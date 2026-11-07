"""Turn an attached file into a chat content part, locally. Nothing leaves this computer here."""

from __future__ import annotations

import base64
from pathlib import PurePath
from typing import Any

TEXT_MAX_BYTES = 200 * 1024
IMAGE_MAX_BYTES = 8 * 1024 * 1024

TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".log", ".xml", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".html", ".htm", ".css", ".py", ".js", ".ts", ".tsx", ".jsx", ".java",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".sh", ".ps1",
    ".sql", ".r", ".tex",
})
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class AttachmentError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def extension(name: str) -> str:
    return PurePath(name or "").suffix.lower()


def max_bytes_for(name: str) -> int:
    """How many bytes to read before rejecting (the caller reads one more to detect overflow)."""
    return IMAGE_MAX_BYTES if extension(name) in IMAGE_TYPES else TEXT_MAX_BYTES


def human_size(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} MB" if n >= 1024 * 1024 else f"{n // 1024} KB"


def extract(name: str, data: bytes) -> dict[str, Any]:
    ext = extension(name)
    if ext in TEXT_EXTENSIONS:
        return text_part(name, data)
    if ext in IMAGE_TYPES:
        return image_part(name, data, IMAGE_TYPES[ext])
    raise AttachmentError(
        415, f"Can't attach {ext or 'this'} files yet. Attach text files (.txt, .md, .csv, .json, "
             f".log, code) or images (.png, .jpg, .webp, .gif). PDF and Office files are coming later.",
    )


def text_part(name: str, data: bytes) -> dict[str, Any]:
    if len(data) > TEXT_MAX_BYTES:
        raise AttachmentError(413, f"{name} is too large: text files can be up to {human_size(TEXT_MAX_BYTES)}.")
    text = data.decode("utf-8", errors="replace")
    return {"kind": "text", "name": name, "size": len(data), "text": text}


def image_part(name: str, data: bytes, mime: str) -> dict[str, Any]:
    if len(data) > IMAGE_MAX_BYTES:
        raise AttachmentError(413, f"{name} is too large: images can be up to {human_size(IMAGE_MAX_BYTES)}.")
    encoded = base64.b64encode(data).decode("ascii")
    return {"kind": "image", "name": name, "size": len(data), "data_url": f"data:{mime};base64,{encoded}"}
