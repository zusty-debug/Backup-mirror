from __future__ import annotations

import hashlib
import re
from pathlib import Path


def safe_filename(name: str | None, fallback: str) -> str:
    candidate = (name or fallback).replace("\\", "_").replace("/", "_").strip()
    candidate = candidate.replace("..", "")
    candidate = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", candidate)
    candidate = candidate.lstrip("._")
    return candidate[:180] or fallback


def readable_bytes(value: int | float) -> str:
    number = float(value)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if abs(number) < 1024 or unit == units[-1]:
            return f"{number:.1f} {unit}" if unit != "B" else f"{int(number)} B"
        number /= 1024
    return f"{number:.1f} PB"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def truncate(value: str, length: int = 64) -> str:
    flattened = " ".join(value.split())
    return flattened if len(flattened) <= length else f"{flattened[: length - 1]}…"
