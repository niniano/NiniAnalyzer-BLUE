from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_json
from .mechanics import is_fake_pull
from .rawlog import resolve_raw
from .reset_policy import is_reset_pull
from .versions import ENGINE_VERSION, INCIDENT_SCHEMA_VERSION, MODULE_VERSION
from .boss_mechanics import supports_encounter as supports_boss_mechanic
from .boss_mechanics.engine import ENGINE_VERSION as BOSS_MECHANIC_ENGINE_VERSION, SCHEMA_VERSION as BOSS_MECHANIC_SCHEMA_VERSION, RULES_VERSION as BOSS_MECHANIC_RULES_VERSION

CURRENT_RAID_REVIEW_SCHEMA = 5
CURRENT_RAID_SERIES_SCHEMA = 3


def _load_json(path: Path | str | None) -> dict:
    if not path:
        return {}
    try:
        p = Path(path)
        if not p.exists():
            return {}
        value = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


# B38 SPEED1: the freshness check only needs a few version fields from each
# result file.  Parsing every full raid_facts.json (up to ~1 MB each) on every
# start made startup scale with the whole archive.  Extracted fields are cached
# per file (path + mtime + size); any change to the file re-reads it.
_FIELD_KEYS = {
    "review": ("engineVersion", "schemaVersion", "moduleVersion"),
    "facts": ("incidentSchemaVersion", "engineVersion"),
    "mechanic": ("schemaVersion", "engineVersion", "rulesVersion"),
    "series": ("schemaVersion", "moduleVersion"),
}
_FIELD_CACHE: dict[str, dict] = {}
_FIELD_CACHE_DIRTY = False
import threading as _threading
_FIELD_LOCK = _threading.Lock()
FIELD_CACHE_NAME = "inspect_fields_v1.json"


def _file_sig(p: Path):
    try:
        st = p.stat()
    except OSError:
        return None
    return [int(st.st_mtime_ns), int(st.st_size)]


def _load_fields(path: Path | str | None, kind: str) -> dict:
    """Same emptiness semantics as ``_load_json`` but returns only needed keys."""
    global _FIELD_CACHE_DIRTY
    if not path:
        return {}
    p = Path(path)
    sig = _file_sig(p)
    if sig is None:
        return {}
    key = f"{kind}|{p}"
    with _FIELD_LOCK:
        hit = _FIELD_CACHE.get(key)
    if hit is not None and hit.get("sig") == sig:
        return dict(hit.get("fields") or {})
    data = _load_json(p)
    fields: dict = {}
    if data:
        fields["__present__"] = True
        for k in _FIELD_KEYS[kind]:
            if k in data:
                fields[k] = data.get(k)
        if kind == "facts" and "pullStart" in data:
            fields["pullStart"] = True
    with _FIELD_LOCK:
        _FIELD_CACHE[key] = {"sig": sig, "fields": fields}
        _FIELD_CACHE_DIRTY = True
    return dict(fields)


def load_field_cache(storage_root: Path | str) -> None:
    """Merge the on-disk field cache (best effort; never required)."""
    try:
        p = Path(storage_root) / "_cache" / FIELD_CACHE_NAME
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == 1:
            with _FIELD_LOCK:
                for k, v in (data.get("entries") or {}).items():
                    if isinstance(v, dict) and k not in _FIELD_CACHE:
                        _FIELD_CACHE[k] = v
    except Exception:
        pass


def save_field_cache(storage_root: Path | str) -> None:
    global _FIELD_CACHE_DIRTY
    if not _FIELD_CACHE_DIRTY:
        return
    try:
        folder = Path(storage_root) / "_cache"
        folder.mkdir(parents=True, exist_ok=True)
        with _FIELD_LOCK:
            snapshot = {k: dict(v) for k, v in _FIELD_CACHE.items()}
            _FIELD_CACHE_DIRTY = False
        atomic_write_json(folder / FIELD_CACHE_NAME, {"version": 1, "entries": snapshot})
    except Exception:
        pass


def _review_skipped_by_current_engine(data: dict) -> bool:
    """B38 SPEED1: a pull the current pipeline itself skipped (e.g. < 5 s, so
    process_try_dir writes no raid_review.json) is not 'missing'.  Without this
    the same short pulls were fully rebuilt on every start."""
    return (str(data.get("raidReviewStatus") or "").lower() == "skipped"
            and str(data.get("lastReanalyzedEngineVersion") or "") == ENGINE_VERSION)


def _resolved(row: dict, key: str, default_name: str | None = None) -> Path | None:
    value = (row or {}).get(key)
    storage = (row or {}).get("storageDir")
    if value:
        p = Path(str(value))
        if not p.is_absolute() and storage:
            p = Path(storage) / p
        return p
    if storage and default_name:
        return Path(storage) / default_name
    return None


def _normal_review_expected(row: dict) -> bool:
    result = str((row or {}).get("result") or "").upper()
    if result not in {"KILL", "WIPE"}:
        return False
    if is_fake_pull(row):
        return False
    if is_reset_pull(row):
        return False
    # B37.3.5: duration alone no longer decides whether a wipe is reviewable.
    # A 3-12s pull with deaths/real engagement can be genuine progression and
    # must remain eligible for the same factual/causal pipeline.
    try:
        return float((row or {}).get("durationSec") or 0.0) > 0.0
    except Exception:
        return False


def inspect_saved_row(row: dict | None) -> dict:
    """Lightweight saved-session freshness check.

    This never parses raw.log.  It only reads the small generated JSON files so
    startup can immediately flag a B2/B3 review without blocking the Tk thread.
    """
    data = dict(row or {})
    personal_stale = bool(
        data.get("playerIdentityReanalysisRequired")
        or str(data.get("factsStatus") or "").lower() == "stale-player"
        or str(data.get("deathReviewStatus") or "").lower() == "stale-player"
    )

    expected = _normal_review_expected(data)
    review_path = _resolved(data, "raidReviewJson", "raid_review.json")
    review = _load_fields(review_path, "review")
    review_engine = review.get("engineVersion")
    review_schema = review.get("schemaVersion")
    review_module = review.get("moduleVersion")
    raid_review_stale = False
    raid_reason = None
    if expected:
        if not review and _review_skipped_by_current_engine(data):
            pass
        elif not review:
            raid_review_stale = True
            raid_reason = "missing-review"
        else:
            try:
                schema_ok = int(review_schema or 0) >= CURRENT_RAID_REVIEW_SCHEMA
            except Exception:
                schema_ok = False
            engine_ok = str(review_engine or "") == ENGINE_VERSION
            module_ok = str(review_module or "") == MODULE_VERSION
            if not (schema_ok and engine_ok and module_ok):
                raid_review_stale = True
                raid_reason = "legacy-review"

    raid_facts_path = _resolved(data, "raidFactsJson", "raid_facts.json")
    raid_facts = _load_fields(raid_facts_path, "facts")
    raid_facts_stale = False
    reset_facts_refresh = bool(is_reset_pull(data))
    if (expected or reset_facts_refresh) and raid_facts:
        try:
            incident_ok = int(raid_facts.get("incidentSchemaVersion") or 0) == int(INCIDENT_SCHEMA_VERSION)
        except Exception:
            incident_ok = False
        engine_ok = str(raid_facts.get("engineVersion") or "") == ENGINE_VERSION
        pull_start_ok = ("pullStart" in raid_facts) if reset_facts_refresh else True
        raid_facts_stale = not (incident_ok and engine_ok and pull_start_ok)
    elif reset_facts_refresh and not raid_facts:
        # Reset/fake pulls are excluded from causal review, but their raid-wide
        # factual layer is still useful for deaths + first observed attack.
        raid_facts_stale = True
    elif expected and not raid_facts:
        # A current raid_review.json is sufficient for display.  Missing factual
        # artifacts alone should not force a raw rebuild (older hand-copied test/
        # archive folders may intentionally keep only the final review).
        raid_facts_stale = bool(raid_review_stale)

    mechanic_review_stale = False
    mechanic_review_state = "unsupported"
    if supports_boss_mechanic(data) and not expected:
        # B37.4.7: reset/fake pulls are audit records, not success/failure
        # samples.  Do not schedule a raw rebuild merely because an old mechanic
        # sidecar is missing/stale, and never treat absence in such a pull as
        # verified success.
        mechanic_review_state = "excluded"
    elif supports_boss_mechanic(data):
        mechanic_path = _resolved(data, "mechanicReviewJson", "mechanic_review.json")
        mechanic = _load_fields(mechanic_path, "mechanic")
        if not mechanic:
            mechanic_review_stale = True
            mechanic_review_state = "missing"
        else:
            try:
                mech_schema_ok = int(mechanic.get("schemaVersion") or 0) >= int(BOSS_MECHANIC_SCHEMA_VERSION)
            except Exception:
                mech_schema_ok = False
            mech_engine_ok = str(mechanic.get("engineVersion") or "") == str(BOSS_MECHANIC_ENGINE_VERSION)
            # B37.3.7.1: rules can change without a schema bump.  Older mechanic
            # reviews did not stamp rulesVersion, so the engine-version bump above
            # makes them stale once; new reviews additionally carry this audit key.
            stored_rules = str(mechanic.get("rulesVersion") or "")
            rules_ok = (not stored_rules) or stored_rules == str(BOSS_MECHANIC_RULES_VERSION)
            mechanic_review_stale = not (mech_schema_ok and mech_engine_ok and rules_ok)
            mechanic_review_state = "legacy" if mechanic_review_stale else "current"

    reasons = []
    if raid_reason:
        reasons.append(raid_reason)
    if raid_facts_stale:
        reasons.append("reset-raid-facts" if reset_facts_refresh and not expected else "stale-raid-facts")
    if personal_stale:
        reasons.append("stale-player")
    if mechanic_review_stale:
        reasons.append("stale-mechanic")

    state = "excluded" if not expected else "legacy" if raid_review_stale else "current"
    return {
        "raidReviewState": state,
        "raidReviewStale": bool(raid_review_stale),
        "raidFactsStale": bool(raid_facts_stale),
        "personalReviewStale": bool(personal_stale),
        "mechanicReviewStale": bool(mechanic_review_stale),
        "mechanicReviewState": mechanic_review_state,
        "reanalysisRequired": bool(reasons),
        "reanalysisReasons": reasons,
        "storedReviewEngineVersion": review_engine,
        "storedReviewSchemaVersion": review_schema,
    }


def _series_module_is_current(value: Any) -> bool:
    # B31 series schema v3 was introduced before the module label moved from
    # RaidWipeReview-v0.3-B31 to v0.4-B31.  Both are B31 semantics.  Reject
    # B2/B3-era artifacts, but do not invalidate the canonical B31 fixture or
    # users' already-correct v0.3-B31 series merely because of the label bump.
    module = str(value or "")
    return module == MODULE_VERSION or module.endswith("-B31")


def inspect_series(path: Path | str | None) -> dict:
    series = _load_fields(path, "series")
    if not series:
        return {"seriesReviewStale": True, "seriesReviewState": "missing"}
    try:
        schema_ok = int(series.get("schemaVersion") or 0) >= CURRENT_RAID_SERIES_SCHEMA
    except Exception:
        schema_ok = False
    module_ok = _series_module_is_current(series.get("moduleVersion"))
    stale = not (schema_ok and module_ok)
    return {
        "seriesReviewStale": stale,
        "seriesReviewState": "legacy" if stale else "current",
        "storedSeriesSchemaVersion": series.get("schemaVersion"),
        "storedSeriesModuleVersion": series.get("moduleVersion"),
    }


def backup_try_results(storage_dir: Path | str) -> Path | None:
    """RC4.1: zip the current result files of one Try before they are rebuilt.

    Nothing is deleted.  The backup is a single zip next to meta.json so the
    session scanner (which rglobs meta.json) never mistakes it for a Try.
    raw.log / raw.log.gz are not copied: reanalysis only reads them.
    """
    import zipfile
    folder = Path(storage_dir)
    files = [p for p in sorted(folder.iterdir())
             if p.is_file() and p.suffix.lower() in (".json", ".txt")]
    if not files:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = folder / f"reanalysis_backup_{stamp}.zip"
    n = 1
    while target.exists():
        n += 1
        target = folder / f"reanalysis_backup_{stamp}_{n}.zip"
    tmp = target.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=p.name)
    tmp.replace(target)
    return target


def rebuild_saved_row(row: dict, storage_root: Path | str, raid_only: bool = False) -> dict:
    """Re-run the current Live Review pipeline for one persisted Try.

    Player identity must already be migrated before this function is called.  It
    intentionally reuses the exact live pipeline so old B2 files cannot drift
    from newly captured B31 results.
    """
    from .raid_review import _extract_live_reviews

    storage_dir = Path(str((row or {}).get("storageDir") or ""))
    if not storage_dir.is_dir():
        raise FileNotFoundError(storage_dir)
    meta_path = storage_dir / "meta.json"
    meta = _load_json(meta_path)
    if not meta:
        raise ValueError(f"meta.json 읽기 실패: {meta_path}")

    raw = resolve_raw(storage_dir)
    if not raw.exists():
        raise FileNotFoundError(raw)

    player = str(meta.get("factsPlayer") or row.get("factsPlayer") or row.get("livePlayer") or "").strip()
    # Backup first; if it fails the old results stay untouched (no rebuild).
    backup = backup_try_results(storage_dir)
    info = _extract_live_reviews(raw, meta, player, Path(storage_root), progress=None, raid_only=raid_only)

    # The component writers already persist their paths/module versions.  Finish
    # the migration by clearing the stale-player marker and recording statuses so
    # a later startup can trust the rebuilt artifacts without another raw scan.
    fresh_meta = _load_json(meta_path) or meta
    for key in (
        "factsStatus", "deathReviewStatus", "raidFactsStatus", "raidReviewStatus",
        "raidSeriesReviewStatus", "analysisExcluded", "resetPull", "autoResetPull",
        "resetReason", "resetMaxDurationSec", "factsPlayer", "factsSpecID",
        "deathCount", "pullStart",
    ):
        if key in info:
            fresh_meta[key] = info.get(key)
    if (not raid_only) and str(info.get("factsStatus") or "").lower() == "ready":
        fresh_meta["playerIdentityReanalysisRequired"] = False
    fresh_meta["lastReanalyzedEngineVersion"] = ENGINE_VERSION
    fresh_meta["lastReanalyzedAt"] = datetime.now().isoformat(timespec="seconds")
    if backup is not None:
        fresh_meta["lastReanalysisBackup"] = backup.name
    atomic_write_json(meta_path, fresh_meta)

    merged = dict(row or {})
    merged.update(fresh_meta)
    merged.update(info)
    merged["storageDir"] = str(storage_dir)
    merged["metaJson"] = str(meta_path)
    merged.update(inspect_saved_row(merged))
    return merged
