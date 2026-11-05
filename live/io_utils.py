from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> Path:
    """Write a text file atomically in the same directory.

    GUI polling can observe generated review files several times per second.  Writing
    to a sibling temporary file and replacing the destination prevents readers from
    seeing a partially-written JSON/TXT file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    return path


def atomic_write_json(path: Path | str, value: Any, *, indent: int = 2) -> Path:
    return atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=indent),
        encoding="utf-8",
    )
