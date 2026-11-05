from __future__ import annotations

import json
import os
import string
from pathlib import Path
from typing import Iterable

from .io_utils import atomic_write_json

ENV_SYNC_ROOT = "NINI_WCL_SYNC_ROOT"
ENV_SETTINGS_HOME = "NINI_WCL_SETTINGS_HOME"
SETTINGS_NAME = "archive_settings.json"


def _norm(p: Path | str | None) -> Path | None:
    if not p:
        return None
    try:
        return Path(str(p)).expanduser()
    except Exception:
        return None


def _nini_root(candidate: Path) -> Path | None:
    """Resolve NiniWoWSync from a user-selected path or a common parent.

    Accepted inputs include NiniWoWSync itself, its WCLAnalyzer child, a Drive
    root, or a parent that contains common Drive Desktop nesting such as
    ``Google Drive/My Drive/NiniWoWSync``.
    """
    try:
        candidate = candidate.expanduser()
    except Exception:
        return None

    if candidate.name.lower() == "niniwowsync" and candidate.exists():
        return candidate
    if (
        candidate.name.lower() == "wclanalyzer"
        and candidate.parent.name.lower() == "niniwowsync"
        and candidate.parent.exists()
    ):
        return candidate.parent

    rels = (
        ("NiniWoWSync",),
        ("My Drive", "NiniWoWSync"),
        ("내 드라이브", "NiniWoWSync"),
        ("Google Drive", "NiniWoWSync"),
        ("Google Drive", "My Drive", "NiniWoWSync"),
        ("Google Drive", "내 드라이브", "NiniWoWSync"),
        ("GoogleDrive", "NiniWoWSync"),
        ("GoogleDrive", "My Drive", "NiniWoWSync"),
        ("GoogleDrive", "내 드라이브", "NiniWoWSync"),
    )
    for rel in rels:
        hit = candidate.joinpath(*rel)
        if hit.exists():
            return hit
    return None


def resolve_nini_candidate(candidate: Path | str | None) -> Path | None:
    p = _norm(candidate)
    return _nini_root(p) if p is not None else None


def _candidate_roots() -> Iterable[Path]:
    # Explicit environment variable always wins and is also convenient for tests.
    env = os.environ.get(ENV_SYNC_ROOT)
    if env:
        yield Path(env)

    home = Path.home()
    for base in (
        home,
        home / "Google Drive",
        home / "내 드라이브",
        home / "My Drive",
        home / "GoogleDrive",
    ):
        yield base

    # Drive for desktop commonly mounts a drive letter on Windows.  Existence
    # checks are cheap; no recursive scan is performed.
    if os.name == "nt":
        for letter in string.ascii_uppercase[2:]:
            root = Path(f"{letter}:\\")
            for rel in ("", "내 드라이브", "My Drive", "Google Drive", "GoogleDrive"):
                yield root / rel if rel else root


def global_settings_path() -> Path:
    override = os.environ.get(ENV_SETTINGS_HOME)
    if override:
        return Path(override).expanduser() / SETTINGS_NAME
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "NiniWCLAnalyzer" / SETTINGS_NAME
    return Path.home() / ".config" / "nini-wcl-analyzer" / SETTINGS_NAME


def _read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _legacy_setting_paths(workdir: Path | str) -> Iterable[Path]:
    """Bounded lookup for settings left beside a previous FULL_CLEAN install."""
    wd = Path(workdir).resolve()
    roots = [wd.parent]
    home = Path.home()
    roots.extend([home / "Desktop", home / "Downloads", home / "Documents"])
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            candidates = list(root.glob("WCLAnalyzer*/data/archive_settings.json"))
            candidates += list(root.glob("*WCLAnalyzer*/data/archive_settings.json"))
        except Exception:
            continue
        for path in candidates:
            try:
                key = str(path.resolve()).lower()
            except Exception:
                key = str(path).lower()
            if key in seen or path == settings_path(wd):
                continue
            seen.add(key)
            yield path


def settings_path(workdir: Path | str) -> Path:
    return Path(workdir) / "data" / SETTINGS_NAME


def load_settings(workdir: Path | str) -> dict:
    local = _read_settings(settings_path(workdir))
    if local:
        return local
    return _read_settings(global_settings_path())


def save_settings(workdir: Path | str, *, drive_sync_root: Path | str | None) -> Path:
    path = settings_path(workdir)
    payload = {
        "schemaVersion": 2,
        "driveSyncRoot": str(drive_sync_root) if drive_sync_root else None,
        "note": "Path may point to NiniWoWSync itself, WCLAnalyzer under it, or a parent that contains it.",
    }
    local_written = atomic_write_json(path, payload)
    try:
        atomic_write_json(global_settings_path(), payload)
    except Exception:
        # Local settings remain sufficient; global persistence is best-effort.
        pass
    return local_written


def _configured_roots(workdir: Path | str) -> Iterable[Path]:
    for path in (settings_path(workdir), global_settings_path(), *_legacy_setting_paths(workdir)):
        cfg = _read_settings(path)
        configured = _norm(cfg.get("driveSyncRoot"))
        if configured is not None:
            yield configured


def resolve_nini_sync_root(workdir: Path | str) -> Path | None:
    """Return the local Drive-for-desktop NiniWoWSync folder, if visible.

    Resolution order: env -> local/global/legacy saved settings -> bounded common
    Drive Desktop mount roots.  No Google API/OAuth credentials are used.
    """
    env = os.environ.get(ENV_SYNC_ROOT)
    if env:
        hit = _nini_root(Path(env))
        if hit is not None:
            return hit

    seen = set()
    for configured in _configured_roots(workdir):
        key = str(configured).lower()
        if key in seen:
            continue
        seen.add(key)
        hit = _nini_root(configured)
        if hit is not None:
            return hit

    for candidate in _candidate_roots():
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        hit = _nini_root(candidate)
        if hit is not None:
            return hit
    return None


def resolve_wcl_sync_root(workdir: Path | str) -> Path | None:
    nini = resolve_nini_sync_root(workdir)
    if nini is None:
        return None
    return nini / "WCLAnalyzer"
