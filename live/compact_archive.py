from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import shutil
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .archive_settings import resolve_wcl_sync_root
from .encounter_tracker import parse_combat_datetime, parse_log_line
from .fact_extractor import DAMAGE_EVENTS, _advanced_target_hp, _event_amount, _spell_info
from .io_utils import atomic_write_json
from .rawlog import open_raw_text, resolve_raw
from .reset_policy import is_reset_pull, reset_classification, is_early_wipe, reset_metadata
from .versions import APP_BUILD, ENGINE_VERSION
from .verdict_projection import mechanic_failure_slots, build_presentation_verdict
from .boss_mechanics import analyze_events as analyze_boss_mechanics_events, link_review_to_b31, normalize_compact_core
from .prelive_audit import write_kithix_observation
from .boss_mechanics.engine import RULES_VERSION as BOSS_MECHANIC_RULES_VERSION
from .boss_registry import boss_slug

CAPSULE_SCHEMA_VERSION = 1
SUMMARY_SCHEMA_VERSION = 4
INDEX_SCHEMA_VERSION = 3

REACTION_HOSTILE = 0x40
AFFILIATION_MINE = 0x1
AFFILIATION_PARTY = 0x2
AFFILIATION_RAID = 0x4

DEATH_EVENTS = {"UNIT_DIED", "UNIT_DESTROYED", "SPELL_INSTAKILL"}
DISPEL_EVENTS = {"SPELL_DISPEL", "SPELL_STOLEN"}
INTERRUPT_EVENTS = {"SPELL_INTERRUPT"}
PLAYER_CAST_EVENTS = {"SPELL_CAST_START", "SPELL_CAST_SUCCESS", "SPELL_CAST_FAILED", "SPELL_CAST_FAILED_QUIET"}
SUMMON_EVENTS = {"SPELL_SUMMON", "SPELL_CREATE"}
MISS_EVENTS = {"SPELL_MISSED", "RANGE_MISSED", "SWING_MISSED"}


def archive_pull_key(encounter: dict | None) -> str:
    """Stable logical pull identity independent of capture completeness.

    ``sourceKey`` intentionally includes the raw SHA and therefore distinguishes
    a truncated Live capture from a complete Historical capture of the same pull.
    B37.3.4 adds this raw-independent key for cross-folder/cross-archive
    reconciliation while preserving sourceKey as the immutable source identity.
    """
    row = encounter or {}
    return "|".join([
        str(row.get("encounterID") or ""),
        str(row.get("difficultyID") or row.get("difficulty") or ""),
        str(row.get("startTimestamp") or ""),
    ])


def _archive_kind_from_date_dir(date_dir: Path) -> str:
    try:
        return str(date_dir.parent.name)
    except Exception:
        return ""


def _exact_source_quality(row: dict) -> tuple:
    """Choose the canonical folder for byte-identical sourceKey duplicates."""
    try:
        cap = int(row.get("capsuleBytes") or 0)
    except Exception:
        cap = 0
    canonical_path_bonus = int(bool(row.get("canonicalBossPath")))
    kind_bonus = int(str(row.get("archiveKind") or "").lower() == "historical")
    return (kind_bonus, canonical_path_bonus, cap)


def _archive_quality(row: dict) -> tuple:
    """Rank competing captures of one logical pull.

    Normal completed pulls beat abort/truncated captures; when both are complete,
    the longer/more complete capture wins. Historical is only a tie-breaker, so a
    genuinely fuller Sessions capture is never discarded merely because of kind.
    """
    result = str(row.get("result") or "").strip().upper()
    try:
        duration = float(row.get("durationSec")) if row.get("durationSec") is not None else -1.0
    except Exception:
        duration = -1.0
    try:
        cap = int(row.get("capsuleBytes") or 0)
    except Exception:
        cap = 0
    completed = int(result in {"KILL", "WIPE"} and duration >= 0.0)
    kind_bonus = int(str(row.get("archiveKind") or "").lower() == "historical")
    canonical_path_bonus = int(bool(row.get("canonicalBossPath")))
    return (completed, round(duration, 3), cap, kind_bonus, canonical_path_bonus)


def _summary_row(path: Path, date_dir: Path, archive_kind: str) -> dict | None:
    doc = _read_json(path)
    if not doc:
        return None
    enc = doc.get("encounter") or {}
    analysis = doc.get("analysis") or {}
    first = analysis.get("firstMistake") or {}
    if not isinstance(first, dict):
        first = {}
    audit = analysis.get("resetAudit") if isinstance(analysis.get("resetAudit"), dict) else {}
    pull_key = str(doc.get("pullKey") or archive_pull_key(enc))
    expected_dir = f"{int(enc.get('encounterID') or 0)}_{_boss_slug(enc)}_{_difficulty_token(enc)}"
    # Preserve the old D233 archive directory as a canonical historical path.
    # The new M token applies to future names only; do not migrate saved capsules.
    legacy_d233_dir = (
        f"{int(enc.get('encounterID') or 0)}_{_boss_slug(enc)}_D233"
        if str(enc.get("difficultyID")) == "233" else None
    )
    return {
        "sourceKey": doc.get("sourceKey"),
        "pullKey": pull_key,
        "encounterID": enc.get("encounterID"),
        "name": enc.get("name"),
        "difficultyID": enc.get("difficultyID"),
        "difficulty": enc.get("difficultyName") or enc.get("difficultyID"),
        "sourceTryNo": enc.get("tryNo"),
        "tryNo": enc.get("tryNo"),
        "result": enc.get("result"),
        "startTimestamp": enc.get("startTimestamp"),
        "durationSec": enc.get("durationSec"),
        "firstMistake": first,
        "deathCount": audit.get("deathCount"),
        "hostileDamageEventCount": audit.get("hostileDamageEventCount"),
        "hostileDamageTotal": audit.get("hostileDamageTotal"),
        "earlyWipe": bool(audit.get("earlyWipe")),
        "resetAudit": audit,
        "summary": str(path.relative_to(date_dir)).replace("\\", "/"),
        "capsule": (doc.get("capsule") or {}).get("file"),
        "capsuleBytes": (doc.get("capsule") or {}).get("bytes"),
        "archiveKind": archive_kind,
        "canonicalBossPath": path.parent.name in {expected_dir, legacy_d233_dir},
        "_path": path,
    }


def _dedupe_rows_by_identity(rows: list[dict], *, reset: bool = False) -> tuple[list[dict], list[dict]]:
    """Collapse exact-source and logical-pull duplicates.

    Reset rows are audit-only, but they still need the same global identity rules so
    old/new boss slugs and Sessions/Historical overlap do not multiply entries in
    ``excludedResets``.
    """
    suppressed: list[dict] = []
    source_reason = "duplicate_reset_sourceKey" if reset else "duplicate_sourceKey"
    pull_reason = "duplicate_reset_pull" if reset else "duplicate_pull"

    by_source: dict[str, dict] = {}
    source_less: list[dict] = []
    for row in rows:
        sk = str(row.get("sourceKey") or "")
        if not sk:
            source_less.append(row)
            continue
        prev = by_source.get(sk)
        if prev is None or _exact_source_quality(row) > _exact_source_quality(prev):
            if prev is not None:
                old = dict(prev); old["suppressedReason"] = source_reason; suppressed.append(old)
            by_source[sk] = row
        else:
            old = dict(row); old["suppressedReason"] = source_reason; suppressed.append(old)

    candidates = list(by_source.values()) + source_less
    by_pull: dict[str, dict] = {}
    no_pull: list[dict] = []
    for row in candidates:
        pk = str(row.get("pullKey") or "")
        if not pk or pk == "||":
            no_pull.append(row)
            continue
        prev = by_pull.get(pk)
        if prev is None or _archive_quality(row) > _archive_quality(prev):
            if prev is not None:
                old = dict(prev); old["suppressedReason"] = pull_reason; suppressed.append(old)
            by_pull[pk] = row
        else:
            old = dict(row); old["suppressedReason"] = pull_reason; suppressed.append(old)
    return list(by_pull.values()) + no_pull, suppressed


def _dedupe_archive_rows(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Return canonical normal rows, unique excluded resets, and suppressed duplicates."""
    reset_rows: list[dict] = []
    normal_rows: list[dict] = []
    for row in rows:
        cls = reset_classification(row)
        if is_reset_pull(row):
            rr = dict(row)
            rr["excludedReason"] = str(cls.get("reason") or "reset_pull")
            rr["resetAudit"] = dict(row.get("resetAudit") or cls)
            rr["deathCount"] = cls.get("deathCount")
            rr["hostileDamageEventCount"] = cls.get("hostileDamageEventCount")
            rr["earlyWipe"] = False
            reset_rows.append(rr)
        else:
            nr = dict(row)
            nr["earlyWipe"] = bool(cls.get("earlyWipe"))
            nr["resetAudit"] = dict(row.get("resetAudit") or cls)
            if nr.get("deathCount") is None:
                nr["deathCount"] = cls.get("deathCount")
            if nr.get("hostileDamageEventCount") is None:
                nr["hostileDamageEventCount"] = cls.get("hostileDamageEventCount")
            normal_rows.append(nr)

    selected, suppressed = _dedupe_rows_by_identity(normal_rows, reset=False)
    unique_resets, reset_suppressed = _dedupe_rows_by_identity(reset_rows, reset=True)
    suppressed.extend(reset_suppressed)
    return selected, unique_resets, suppressed


def _strip_internal(row: dict) -> dict:
    out = {k: v for k, v in row.items() if not k.startswith("_") and k != "canonicalBossPath"}
    return out


def _assign_canonical_try_numbers(rows: list[dict]) -> list[dict]:
    rows = sorted(rows, key=lambda x: (
        parse_combat_datetime(str(x.get("startTimestamp") or "")) or datetime.min,
        int(x.get("sourceTryNo") or 0),
    ))
    counters: dict[tuple, int] = defaultdict(int)
    out = []
    for row in rows:
        r = dict(row)
        key = (r.get("encounterID"), r.get("difficultyID") or r.get("difficulty"))
        counters[key] += 1
        r["tryNo"] = counters[key]
        out.append(r)
    return out


def _flag_int(value: object) -> int:
    try:
        text = str(value or "0").strip()
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except Exception:
        return 0


def _is_player(guid: object) -> bool:
    return str(guid or "").startswith("Player-")


def _is_npc(guid: object) -> bool:
    text = str(guid or "")
    return text.startswith(("Creature-", "Vehicle-", "GameObject-"))


def _is_hostile(flags: object) -> bool:
    return bool(_flag_int(flags) & REACTION_HOSTILE)


def _is_raid_affiliated(flags: object) -> bool:
    f = _flag_int(flags)
    return bool(f & (AFFILIATION_MINE | AFFILIATION_PARTY | AFFILIATION_RAID)) and not bool(f & REACTION_HOSTILE)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}



def _capsule_reset_audit(capsule_path: Path, encounter: dict | None = None) -> dict:
    """Derive conservative reset/early-wipe evidence without rewriting the capsule.

    Compact archives drop raw player->NPC damage rows but preserve a derived
    ``raid_dps_1s.json`` stream before dropping them.  Use that derived stream
    plus player death events from ``events_core`` so legacy short pulls can be
    distinguished from true reconnect/reset records.
    """
    encounter = dict(encounter or {})
    deaths = 0
    hostile_damage_events = 0
    hostile_damage_total = 0.0
    hostile_damage_seconds = 0
    try:
        with zipfile.ZipFile(capsule_path, "r") as zf:
            try:
                with zf.open("source/events_core.jsonl.gz", "r") as raw_gz:
                    with gzip.GzipFile(fileobj=raw_gz, mode="rb") as gz:
                        for raw in gz:
                            try:
                                item = json.loads(raw.decode("utf-8"))
                            except Exception:
                                continue
                            if not isinstance(item, list) or len(item) < 2 or not isinstance(item[1], list):
                                continue
                            fields = item[1]
                            event = str(fields[0] if fields else "")
                            if event in DEATH_EVENTS:
                                dest_guid = fields[5] if len(fields) > 5 else ""
                                if str(dest_guid or "").startswith("Player-"):
                                    deaths += 1
            except KeyError:
                pass
            try:
                dps_doc = json.loads(zf.read("source/raid_dps_1s.json").decode("utf-8"))
                samples = dps_doc.get("samples") if isinstance(dps_doc, dict) else []
                if not isinstance(samples, list):
                    samples = []
                hostile_damage_seconds = len(samples)
                for row in samples:
                    if not isinstance(row, dict):
                        continue
                    try:
                        hostile_damage_events += max(0, int(row.get("events") or 0))
                    except Exception:
                        pass
                    try:
                        hostile_damage_total += max(0.0, float(row.get("damage") or 0.0))
                    except Exception:
                        pass
            except (KeyError, json.JSONDecodeError):
                pass
    except Exception:
        return reset_classification(encounter)

    enriched = dict(encounter)
    enriched["deathCount"] = deaths
    enriched["hostileDamageEventCount"] = hostile_damage_events
    cls = reset_classification(enriched)
    cls.update({
        "deathCount": deaths,
        "hostileDamageEventCount": hostile_damage_events,
        "hostileDamageTotal": round(hostile_damage_total, 3),
        "hostileDamageSeconds": hostile_damage_seconds,
        "auditSource": "compact_capsule",
    })
    return cls


# B37.3.5 compatibility: archive_refresh imports this historical name.
# Use the existing compact-capsule reset audit without changing its logic.
_reset_evidence_from_capsule = _capsule_reset_audit


def _try_dir_reset_audit(try_dir: Path, meta: dict) -> dict:
    """Best available reset evidence while the local raw/live try still exists."""
    enriched = dict(meta or {})
    raid = _read_json(try_dir / "raid_facts.json")
    if enriched.get("deathCount") is None and isinstance(raid.get("deaths"), list):
        enriched["deathCount"] = len(raid.get("deaths") or [])
    cls = reset_classification(enriched)
    cls["auditSource"] = "live_meta"
    return cls


def _load_capsule_for_rejudge(capsule_path: Path, fallback_encounter: dict) -> tuple[dict, list, dict]:
    """Load immutable capsule metadata, normalized BossMechanic events, and light supplemental samples."""
    with zipfile.ZipFile(capsule_path, "r") as zf:
        meta = {}
        try:
            meta = json.loads(zf.read("source/meta.json").decode("utf-8"))
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        encounter = dict(meta)
        # The readable summary may have a newer/canonical name/tryNo; preserve it.
        for key, value in (fallback_encounter or {}).items():
            if value is not None:
                encounter[key] = value
        core_rows = []
        with zf.open("source/events_core.jsonl.gz", "r") as raw_gz:
            with gzip.GzipFile(fileobj=raw_gz, mode="rb") as gz:
                for raw in gz:
                    try:
                        item = json.loads(raw.decode("utf-8"))
                    except Exception:
                        continue
                    core_rows.append(item)

        # B37.4.11: archive re-judgment must also recover the full raw rows
        # intentionally preserved around previous analysis anchors.  P2 Coiled
        # Altar Gloombomb->Soulcoiler validation depends on player->NPC damage
        # ratios, which are deliberately omitted from events_core for size.
        # Ignoring evidence_windows therefore degraded a live verified 1/2 or
        # 2/2 verdict to "unknown" on a later force-refresh.
        evidence_rows = []
        try:
            with zf.open("source/evidence_windows.jsonl.gz", "r") as raw_gz:
                with gzip.GzipFile(fileobj=raw_gz, mode="rb") as gz:
                    for raw in gz:
                        try:
                            item = json.loads(raw.decode("utf-8"))
                        except Exception:
                            continue
                        if not isinstance(item, (list, tuple)) or len(item) < 2:
                            continue
                        timestamp, payload = item[0], item[1]
                        fields = None
                        if isinstance(payload, list):
                            fields = payload
                        elif isinstance(payload, str):
                            parsed = parse_log_line(payload)
                            if parsed:
                                timestamp = parsed.get("timestamp") or timestamp
                                fields = parsed.get("fields")
                        if isinstance(fields, list) and fields:
                            evidence_rows.append([timestamp, fields])
        except KeyError:
            pass

        # Exact core events are also present inside an evidence window.  Keep one
        # copy so cast/aura counts cannot be inflated by the richer rejudge path.
        merged_rows = []
        seen_rows = set()
        for item in list(core_rows) + evidence_rows:
            try:
                key = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                key = repr(item)
            if key in seen_rows:
                continue
            seen_rows.add(key)
            merged_rows.append(item)
        supplemental = {}
        for member, key in (("source/hp_1s.json", "hpSamples"), ("source/raid_dps_1s.json", "raidDpsSamples")):
            try:
                doc = json.loads(zf.read(member).decode("utf-8"))
                if isinstance(doc, dict) and isinstance(doc.get("samples"), list):
                    supplemental[key] = doc.get("samples") or []
            except Exception:
                pass
    return encounter, normalize_compact_core(merged_rows, encounter), supplemental


def _b31_from_summary(summary: dict) -> dict:
    analysis = summary.get("analysis") if isinstance(summary.get("analysis"), dict) else {}
    causal = analysis.get("raidCausalVerdict") or analysis.get("raidVerdict") or {}
    return {
        "schemaVersion": analysis.get("raidWipeReviewSchema"),
        "verdict": causal if isinstance(causal, dict) else {},
        "primaryFinding": analysis.get("raidPrimaryFinding") if isinstance(analysis.get("raidPrimaryFinding"), dict) else {},
        "collapse": analysis.get("collapse") if isinstance(analysis.get("collapse"), dict) else {},
    }


def refresh_summary_from_capsule(summary_path: Path | str, *, force: bool = False) -> dict:
    """Re-judge one derived summary from its immutable ``.wcla.zip`` source.

    The capsule bytes are never modified.  Only the sibling summary is replaced,
    allowing B37.3.5+ verdict projection to be applied to pre-B37.3.3 archives.
    """
    summary_path = Path(summary_path)
    doc = _read_json(summary_path)
    if not doc:
        return {"status": "skipped_invalid_summary", "summary": str(summary_path)}
    analysis = doc.get("analysis") if isinstance(doc.get("analysis"), dict) else {}
    verdict = analysis.get("mechanicVerdict") if isinstance(analysis.get("mechanicVerdict"), dict) else {}
    first = analysis.get("firstMistake") if isinstance(analysis.get("firstMistake"), dict) else {}
    needs_projection = (
        str(verdict.get("status") or "").lower() == "failed"
        and str(verdict.get("evidenceLevel") or "").lower() == "verified"
        and not first
    )
    rules_current = str(analysis.get("mechanicRulesVersion") or "") == str(BOSS_MECHANIC_RULES_VERSION)
    if not force and str(analysis.get("appBuild") or "") == APP_BUILD and rules_current and not needs_projection:
        return {"status": "up_to_date", "summary": str(summary_path)}

    capsule_name = str((doc.get("capsule") or {}).get("file") or "")
    capsule_path = summary_path.parent / capsule_name if capsule_name else None
    if capsule_path is None or not capsule_path.exists():
        return {"status": "skipped_missing_capsule", "summary": str(summary_path)}

    before_stat = (capsule_path.stat().st_size, capsule_path.stat().st_mtime_ns)
    encounter = dict(doc.get("encounter") or {})
    try:
        encounter_for_analysis, events, supplemental = _load_capsule_for_rejudge(capsule_path, encounter)
        mech = analyze_boss_mechanics_events(events, encounter_for_analysis, source_kind="archive", supplemental=supplemental)
    except Exception as exc:
        return {"status": "error", "summary": str(summary_path), "error": str(exc)}

    if not mech:
        # Unsupported boss: keep verdict content intact, but attach reset audit so
        # Canonical can still classify short pulls conservatively.
        new_doc = dict(doc)
        new_analysis = dict(analysis)
        new_analysis["engineVersion"] = ENGINE_VERSION
        new_analysis["appBuild"] = APP_BUILD
        new_analysis["mechanicRulesVersion"] = BOSS_MECHANIC_RULES_VERSION
        new_analysis["resetAudit"] = _capsule_reset_audit(capsule_path, encounter)
        new_analysis["rejudgedFromCapsule"] = {
            "status": "unsupported_boss",
            "appBuild": APP_BUILD,
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
        }
        new_doc["analysis"] = new_analysis
        new_doc["schemaVersion"] = SUMMARY_SCHEMA_VERSION
        new_doc["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        atomic_write_json(summary_path, new_doc)
        return {"status": "refreshed_audit_only", "summary": str(summary_path)}

    b31 = _b31_from_summary(doc)
    mech = link_review_to_b31(mech, b31) or mech
    slots = mechanic_failure_slots(mech)
    presentation = build_presentation_verdict(mech, b31)
    old_build = str(analysis.get("appBuild") or "")
    new_analysis = dict(analysis)
    new_analysis.update({
        "engineVersion": ENGINE_VERSION,
        "appBuild": APP_BUILD,
        "mechanicReviewSchema": mech.get("schemaVersion"),
        "mechanicRulesVersion": BOSS_MECHANIC_RULES_VERSION,
        "mechanicVerdict": mech.get("verdict"),
        "firstMistake": slots.get("firstMistake") or mech.get("firstMistake") or (mech.get("observations") or {}).get("firstVerifiedFailure"),
        "majorIncident": slots.get("majorIncident") or mech.get("majorIncident"),
        "collapseDirect": slots.get("collapseDirect") or mech.get("collapseDirect"),
        "raidVerdict": presentation or b31.get("verdict") or analysis.get("raidVerdict"),
        "raidCausalVerdict": b31.get("verdict") or analysis.get("raidCausalVerdict"),
        "resetAudit": _capsule_reset_audit(capsule_path, encounter),
        "rejudgedFromCapsule": {
            "status": "refreshed",
            "fromAppBuild": old_build or None,
            "appBuild": APP_BUILD,
            "sourceKind": "archive",
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
        },
    })
    new_doc = dict(doc)
    new_doc["schemaVersion"] = SUMMARY_SCHEMA_VERSION
    new_doc["analysis"] = new_analysis
    new_doc["updatedAt"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(summary_path, new_doc)

    after_stat = (capsule_path.stat().st_size, capsule_path.stat().st_mtime_ns)
    if after_stat != before_stat:
        raise RuntimeError(f"immutable capsule changed during summary refresh: {capsule_path}")
    return {
        "status": "refreshed",
        "summary": str(summary_path),
        "capsule": str(capsule_path),
        "fromAppBuild": old_build or None,
        "firstMistake": new_analysis.get("firstMistake"),
    }


def refresh_archive_summaries(
    local_root: Path | str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
) -> dict:
    """Batch refresh derived summaries and rebuild daily/Canonical indexes."""
    local_root = Path(local_root)
    stats = defaultdict(int)
    details: list[dict] = []
    touched_dates: set[str] = set()
    for kind in ("Sessions", "Historical"):
        root = local_root / kind
        if not root.is_dir():
            continue
        for date_dir in sorted(x for x in root.iterdir() if x.is_dir()):
            date = date_dir.name
            if start_date and date < start_date:
                continue
            if end_date and date > end_date:
                continue
            for summary_path in sorted(date_dir.rglob("*.summary.json")):
                result = refresh_summary_from_capsule(summary_path, force=force)
                status = str(result.get("status") or "unknown")
                stats[status] += 1
                if status.startswith("refreshed"):
                    touched_dates.add(date)
                details.append(result)

    rebuilt = rebuild_archive_indexes(local_root)
    return {
        "appBuild": APP_BUILD,
        "startDate": start_date,
        "endDate": end_date,
        "force": bool(force),
        "stats": dict(sorted(stats.items())),
        "touchedDates": sorted(touched_dates),
        "indexesRebuilt": len(rebuilt),
        "details": details,
    }


def _difficulty_token(meta: dict) -> str:
    try:
        diff = int(meta.get("difficultyID"))
    except Exception:
        diff = None
    if diff in {5, 16, 233}:
        return "M"
    if diff in {4, 15}:
        return "H"
    return f"D{diff}" if diff is not None else "DUNK"


def _result_token(meta: dict) -> str:
    result = str(meta.get("result") or "END").upper()
    if "KILL" in result:
        return "KILL"
    if "WIPE" in result:
        return "WIPE"
    if result in {"ABORT", "ABORTED", "중단"}:
        return "ABORT"
    return "END"


def _boss_slug(meta: dict) -> str:
    try:
        eid = int(meta.get("encounterID"))
    except Exception:
        eid = 0
    return boss_slug(eid)


def capsule_basename(meta: dict) -> str:
    try:
        eid = int(meta.get("encounterID") or 0)
    except Exception:
        eid = 0
    try:
        no = int(meta.get("tryNo") or 0)
    except Exception:
        no = 0
    return f"{eid}_{_boss_slug(meta)}_{_difficulty_token(meta)}_Try{no:03d}_{_result_token(meta)}"


def _relative_source(value: object) -> str | None:
    if not value:
        return None
    # Normalize Windows paths even when the archive is inspected on Linux/macOS.
    text = str(value).replace("\\", "/")
    parts = [x for x in text.split("/") if x]
    lower = [x.lower() for x in parts]
    for needle in ("warcraftlogsarchive", "logs"):
        if needle in lower:
            i = lower.index(needle)
            # Logs itself is not useful; keep the archive marker when present.
            if needle == "logs" and i + 1 < len(parts) and parts[i + 1].lower() == "warcraftlogsarchive":
                i += 1
            return "/".join(parts[i:])
    return parts[-1] if parts else text


def _relative_sec(timestamp: str, start_dt: datetime | None) -> float | None:
    if start_dt is None:
        return None
    dt = parse_combat_datetime(str(timestamp or ""), now=start_dt)
    if dt is None:
        return None
    sec = (dt - start_dt).total_seconds()
    if sec < -43200:
        sec += 86400
    return round(sec, 4)


def _keep_core_event(event: str, fields: list[str]) -> bool:
    if event.startswith("ENCOUNTER_") or event == "COMBATANT_INFO":
        return True
    source_guid = fields[1] if len(fields) > 1 else ""
    source_flags = fields[3] if len(fields) > 3 else "0"
    dest_guid = fields[5] if len(fields) > 5 else ""
    dest_flags = fields[7] if len(fields) > 7 else "0"
    src_player = _is_player(source_guid)
    dst_player = _is_player(dest_guid)
    src_npc = _is_npc(source_guid)
    dst_npc = _is_npc(dest_guid)
    src_hostile = _is_hostile(source_flags)
    dst_hostile = _is_hostile(dest_flags)

    if src_player and event in PLAYER_CAST_EVENTS:
        return True
    if event in DEATH_EVENTS | DISPEL_EVENTS | INTERRUPT_EVENTS | {"SPELL_ABSORBED", "SPELL_RESURRECT"}:
        return True
    if src_npc and event in PLAYER_CAST_EVENTS | SUMMON_EVENTS:
        return True
    if (src_npc or dst_npc or src_hostile or dst_hostile) and "AURA" in event:
        return True
    if (src_npc or src_hostile) and dst_player and (event in DAMAGE_EVENTS or event in MISS_EVENTS):
        return True
    return False


def _analysis_anchors(try_dir: Path, meta: dict) -> list[float]:
    anchors: set[float] = set()
    dur = meta.get("durationSec")
    try:
        if dur is not None:
            anchors.add(max(0.0, float(dur)))
    except Exception:
        pass

    def add(value):
        try:
            x = float(value)
        except Exception:
            return
        if math.isfinite(x) and x >= 0:
            anchors.add(round(x, 3))

    for name in ("mechanic_review.json", "raid_wipe_review.json", "raid_review.json"):
        doc = _read_json(try_dir / name)
        if not doc:
            continue
        # Explicitly useful semantic anchors; avoid retaining every timestamp in a large review.
        for key in ("firstMistake", "majorIncident", "collapseDirect", "primaryFinding", "verdict", "collapse"):
            row = doc.get(key)
            if isinstance(row, dict):
                for tkey in ("sec", "time", "startSec", "rootTime"):
                    if tkey in row:
                        add(row.get(tkey))
        chain = doc.get("collapseChain")
        if isinstance(chain, dict):
            add(chain.get("rootTime"))
    return sorted(anchors)


def _in_windows(sec: float | None, anchors: list[float], radius: float = 5.0) -> bool:
    if sec is None:
        return False
    return any(abs(float(sec) - a) <= radius for a in anchors)


def _gzip_jsonl(path: Path, rows: list[Any]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=6, newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _source_payload(try_dir: Path, meta: dict, raw_path: Path) -> tuple[dict, dict[str, Any]]:
    start_dt = parse_combat_datetime(str(meta.get("startTimestamp") or ""))
    sha = hashlib.sha256()
    core_rows: list[Any] = []
    hp_by_sec: dict[tuple[int, str], dict] = {}
    dps_by_sec: dict[tuple[int, str], dict] = {}
    event_counts = defaultdict(int)
    kept_counts = defaultdict(int)
    line_count = 0
    advanced_hp_rows = 0
    raid_damage_rows = 0
    anchors = _analysis_anchors(try_dir, meta)
    evidence_rows: list[Any] = []

    with open_raw_text(raw_path) as fh:
        for raw in fh:
            line_count += 1
            encoded = raw.encode("utf-8", errors="replace")
            sha.update(encoded)
            parsed = parse_log_line(raw)
            if not parsed:
                continue
            event = str(parsed.get("event") or "")
            fields = parsed.get("fields") or []
            event_counts[event] += 1
            sec = _relative_sec(parsed.get("timestamp") or "", start_dt)

            source_guid = fields[1] if len(fields) > 1 else ""
            source_name = fields[2] if len(fields) > 2 else ""
            source_flags = fields[3] if len(fields) > 3 else "0"
            dest_guid = fields[5] if len(fields) > 5 else ""
            dest_name = fields[6] if len(fields) > 6 else ""
            dest_flags = fields[7] if len(fields) > 7 else "0"

            if event in DAMAGE_EVENTS:
                _, _, extra_start = _spell_info(event, fields)
                # Keep 1-second hostile-target HP samples BEFORE dropping player damage rows.
                if dest_guid and _is_hostile(dest_flags):
                    hp, max_hp = _advanced_target_hp(fields, extra_start)
                    if hp is not None and max_hp not in (None, 0) and sec is not None:
                        advanced_hp_rows += 1
                        bucket = int(math.floor(max(0.0, float(sec))))
                        hp_by_sec[(bucket, dest_guid)] = {
                            "sec": bucket,
                            "guid": dest_guid,
                            "name": str(dest_name or "").strip('"'),
                            "currentHP": hp,
                            "maxHP": max_hp,
                            "hpPct": round(float(hp) / float(max_hp) * 100.0, 3),
                        }

                    # Raid damage per target per second. Include players and raid-affiliated pets/guardians.
                    if _is_player(source_guid) or _is_raid_affiliated(source_flags):
                        amount = _event_amount(event, fields, extra_start)
                        if amount is not None and sec is not None:
                            raid_damage_rows += 1
                            bucket = int(math.floor(max(0.0, float(sec))))
                            key = (bucket, dest_guid)
                            row = dps_by_sec.setdefault(key, {
                                "sec": bucket,
                                "targetGUID": dest_guid,
                                "targetName": str(dest_name or "").strip('"'),
                                "damage": 0.0,
                                "events": 0,
                            })
                            row["damage"] += float(amount)
                            row["events"] += 1

            if _keep_core_event(event, fields):
                kept_counts[event] += 1
                # Compact but future-proof: exact parsed CSV fields are retained,
                # so unknown spell IDs can be reinterpreted by a later engine.
                core_rows.append([parsed.get("timestamp"), fields])

            if _in_windows(sec, anchors):
                # Evidence windows intentionally retain full raw rows, including
                # player damage/heal that is excluded from events_core.
                evidence_rows.append([parsed.get("timestamp"), raw.rstrip("\r\n")])

    source_hash = sha.hexdigest()
    source_key = "|".join([
        str(meta.get("encounterID") or ""),
        str(meta.get("difficultyID") or ""),
        str(meta.get("startTimestamp") or ""),
        source_hash,
    ])
    roster_doc = _read_json(try_dir / "raid_facts.json")
    roster = roster_doc.get("roster") if isinstance(roster_doc.get("roster"), list) else []

    coverage = {
        "schemaVersion": CAPSULE_SCHEMA_VERSION,
        "rawLineCount": line_count,
        "coreEventCount": len(core_rows),
        "evidenceWindowLineCount": len(evidence_rows),
        "eventCounts": dict(sorted(event_counts.items())),
        "coreEventCounts": dict(sorted(kept_counts.items())),
        "advancedLogging": {
            "detected": bool(advanced_hp_rows),
            "hostileTargetHpRows": advanced_hp_rows,
            "raidDamageRows": raid_damage_rows,
            "note": "false may mean Advanced Combat Logging was disabled or the fight lacked parseable v22 target HP fields",
        },
        "filterPolicy": {
            "mode": "event_kind_v1",
            "keeps": [
                "all hostile NPC casts/auras/summons",
                "hostile NPC to player damage/misses/debuff auras",
                "deaths/interrupts/dispels/resurrects/absorbs",
                "all player spell casts",
                "encounter/combatant metadata",
            ],
            "dropsRaw": ["player-to-NPC damage", "healing", "routine player buff refreshes"],
            "derivedBeforeDrop": ["hostile target HP 1s samples", "raid damage per target per second"],
        },
    }

    payload = {
        "sourceHash": source_hash,
        "sourceKey": source_key,
        "core": core_rows,
        "evidence": evidence_rows,
        "hp": sorted(hp_by_sec.values(), key=lambda x: (x["sec"], x["name"], x["guid"])),
        "dps": sorted(dps_by_sec.values(), key=lambda x: (x["sec"], x["targetName"], x["targetGUID"])),
        "roster": roster,
        "coverage": coverage,
        "anchors": anchors,
    }
    return payload, coverage


def _summary_from_reviews(try_dir: Path, meta: dict, source_key: str, capsule_name: str, capsule_size: int) -> dict:
    mech = _read_json(try_dir / "mechanic_review.json")
    raid = _read_json(try_dir / "raid_wipe_review.json") or _read_json(try_dir / "raid_review.json")

    def pick(doc: dict, *keys):
        for key in keys:
            row = doc.get(key)
            if row:
                return row
        return None

    slots = mechanic_failure_slots(mech)
    presentation = raid.get("presentationVerdict") or build_presentation_verdict(mech, raid)
    reset_audit = _try_dir_reset_audit(try_dir, meta)

    return {
        "schemaVersion": SUMMARY_SCHEMA_VERSION,
        "sourceKey": source_key,
        "pullKey": archive_pull_key(meta),
        "encounter": {
            "encounterID": meta.get("encounterID"),
            "name": meta.get("name"),
            "difficultyID": meta.get("difficultyID"),
            "difficultyName": meta.get("difficultyName"),
            "tryNo": meta.get("tryNo"),
            "result": meta.get("result"),
            "startTimestamp": meta.get("startTimestamp"),
            "endTimestamp": meta.get("endTimestamp"),
            "durationSec": meta.get("durationSec"),
            "deathCount": reset_audit.get("deathCount"),
            "hostileDamageEventCount": reset_audit.get("hostileDamageEventCount"),
            "earlyWipe": reset_audit.get("earlyWipe"),
        },
        "analysis": {
            "engineVersion": ENGINE_VERSION,
            "appBuild": APP_BUILD,
            "mechanicReviewSchema": mech.get("schemaVersion"),
            "mechanicRulesVersion": BOSS_MECHANIC_RULES_VERSION,
            "raidWipeReviewSchema": raid.get("schemaVersion"),
            "mechanicVerdict": mech.get("verdict"),
            "firstMistake": slots.get("firstMistake") or pick(mech, "firstMistake", "firstVerifiedFailure"),
            "majorIncident": slots.get("majorIncident") or mech.get("majorIncident"),
            "collapseDirect": slots.get("collapseDirect") or mech.get("collapseDirect"),
            "raidVerdict": presentation or raid.get("verdict"),
            "raidCausalVerdict": raid.get("verdict"),
            "raidPrimaryFinding": raid.get("primaryFinding"),
            "collapse": raid.get("collapse"),
            "resetAudit": reset_audit,
        },
        "capsule": {
            "file": capsule_name,
            "bytes": int(capsule_size),
            "schemaVersion": CAPSULE_SCHEMA_VERSION,
        },
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }


def _collect_date_rows(date_dir: Path, archive_kind: str | None = None) -> list[dict]:
    rows: list[dict] = []
    if not date_dir.is_dir():
        return rows
    kind = str(archive_kind or _archive_kind_from_date_dir(date_dir))
    for p in sorted(date_dir.rglob("*.summary.json")):
        row = _summary_row(p, date_dir, kind)
        if row:
            rows.append(row)
    return rows


def _write_daily_index(date_dir: Path) -> Path:
    kind = _archive_kind_from_date_dir(date_dir)
    rows, resets, suppressed = _dedupe_archive_rows(_collect_date_rows(date_dir, kind))
    rows = _assign_canonical_try_numbers(rows)
    clean_rows = [_strip_internal(x) for x in rows]
    clean_resets = [_strip_internal(x) for x in sorted(resets, key=lambda x: str(x.get("startTimestamp") or ""))]
    clean_suppressed = [_strip_internal(x) for x in suppressed]
    return atomic_write_json(date_dir / "index.json", {
        "schemaVersion": INDEX_SCHEMA_VERSION,
        "date": date_dir.name,
        "archiveKind": kind,
        "tries": clean_rows,
        "excludedResets": clean_resets,
        "suppressedDuplicates": clean_suppressed,
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    })


def _write_canonical_index(local_root: Path, date: str) -> Path:
    all_rows: list[dict] = []
    all_resets: list[dict] = []
    all_suppressed: list[dict] = []
    for kind in ("Sessions", "Historical"):
        date_dir = local_root / kind / date
        selected, resets, suppressed = _dedupe_archive_rows(_collect_date_rows(date_dir, kind))
        all_rows.extend(selected)
        all_resets.extend(resets)
        all_suppressed.extend(suppressed)

    selected, resets2, suppressed2 = _dedupe_archive_rows(all_rows)
    all_resets.extend(resets2)
    all_suppressed.extend(suppressed2)
    # Reset rows never enter ``all_rows`` above, so reconcile them explicitly
    # across archive kinds as audit metadata too.
    _, all_resets, reset_suppressed = _dedupe_archive_rows(all_resets)
    all_suppressed.extend(reset_suppressed)
    selected = _assign_canonical_try_numbers(selected)
    out_dir = local_root / "Canonical" / date
    out_dir.mkdir(parents=True, exist_ok=True)

    def canonical_path(row: dict, key: str):
        value = str(row.get(key) or "")
        if not value:
            return value
        kind = str(row.get("archiveKind") or "")
        if key == "summary":
            return f"../../{kind}/{date}/{value}"
        # capsule is basename only; place it beside the summary's boss folder.
        summary = str(row.get("summary") or "")
        parent = str(Path(summary).parent).replace("\\", "/")
        return f"../../{kind}/{date}/{parent}/{value}" if parent not in {"", "."} else f"../../{kind}/{date}/{value}"

    clean = []
    for row in selected:
        r = _strip_internal(row)
        r["summary"] = canonical_path(row, "summary")
        r["capsule"] = canonical_path(row, "capsule")
        clean.append(r)
    return atomic_write_json(out_dir / "index.json", {
        "schemaVersion": INDEX_SCHEMA_VERSION,
        "date": date,
        "archiveKind": "Canonical",
        "selectionPolicy": "same sourceKey across archive kinds prefers Historical; same pullKey uses completed/longer/larger capture then Historical; reset only when very-short + no deaths + near-zero engagement (legacy fallback sub-second); other <=30s pulls kept as earlyWipe",
        "tries": clean,
        "excludedResets": [_strip_internal(x) for x in all_resets],
        "suppressedDuplicates": [_strip_internal(x) for x in all_suppressed],
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    })


def rebuild_archive_indexes(local_root: Path | str) -> list[Path]:
    local_root = Path(local_root)
    dates: set[str] = set()
    for kind in ("Sessions", "Historical"):
        kind_dir = local_root / kind
        if not kind_dir.is_dir():
            continue
        for date_dir in kind_dir.iterdir():
            if date_dir.is_dir():
                dates.add(date_dir.name)
    out: list[Path] = []
    for date in sorted(dates):
        for kind in ("Sessions", "Historical"):
            date_dir = local_root / kind / date
            if date_dir.is_dir():
                out.append(_write_daily_index(date_dir))
        out.append(_write_canonical_index(local_root, date))
    return out


def _copy_if_changed(src: Path, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dst.exists() and src.stat().st_size == dst.stat().st_size:
            # Summary/index may keep same size while content changes; only capsule
            # is immutable. Caller decides whether this optimization is acceptable.
            if src.suffix == ".zip":
                return False
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return True
    except Exception:
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[name-defined]
        except Exception:
            pass
        raise


def _sync_tree(files: list[Path], local_root: Path, workdir: Path) -> tuple[str, list[str]]:
    sync_root = resolve_wcl_sync_root(workdir)
    if sync_root is None:
        return "pending_no_drive_folder", []
    synced = []
    try:
        for src in files:
            rel = src.relative_to(local_root)
            dst = sync_root / rel
            _copy_if_changed(src, dst)
            synced.append(str(dst))
        return "synced_local_drive_folder", synced
    except Exception as exc:
        return f"pending_sync_error:{exc}", synced


def build_compact_capsule(
    try_dir: Path | str,
    *,
    workdir: Path | str | None = None,
    archive_kind: str = "Sessions",
    force: bool = False,
) -> dict:
    """Build a compact, re-analysable source capsule plus readable summary/index.

    The capsule is immutable for a sourceKey. Derived review output lives in the
    sibling summary and may be refreshed by later engines without changing the
    source capsule or dedupe key.
    """
    try_dir = Path(try_dir)
    workdir = Path(workdir or Path.cwd())
    meta = _read_json(try_dir / "meta.json")
    if not meta:
        raise FileNotFoundError(f"meta.json missing: {try_dir}")
    raw_path = resolve_raw(try_dir)
    if not raw_path.exists():
        raise FileNotFoundError(f"raw.log/raw.log.gz missing: {try_dir}")

    date = str(meta.get("sessionDate") or "")
    if not date:
        dt = parse_combat_datetime(str(meta.get("startTimestamp") or ""))
        date = dt.strftime("%Y-%m-%d") if dt else datetime.now().strftime("%Y-%m-%d")
    boss_dir_name = f"{int(meta.get('encounterID') or 0)}_{_boss_slug(meta)}_{_difficulty_token(meta)}"
    local_root = workdir / "data" / "archive_outbox"
    local_root.mkdir(parents=True, exist_ok=True)

    # B37.3.5: true resets still do not get a long-term capsule, but write a
    # lightweight derived summary so excludedResets remains auditable (duration,
    # death count, engagement evidence, reason) and never becomes invisible.
    if is_reset_pull(meta):
        out_dir = local_root / archive_kind / date / boss_dir_name
        out_dir.mkdir(parents=True, exist_ok=True)
        base = capsule_basename(meta)
        summary_path = out_dir / f"{base}.summary.json"
        payload, _coverage = _source_payload(try_dir, meta, raw_path)
        source_key = payload["sourceKey"]
        summary = _summary_from_reviews(try_dir, meta, source_key, "", 0)
        atomic_write_json(summary_path, summary)
        index_path = _write_daily_index(local_root / archive_kind / date)
        canonical_index_path = _write_canonical_index(local_root, date)
        sync_status, sync_paths = _sync_tree([summary_path, index_path, canonical_index_path], local_root, workdir)
        result = {
            "archiveStatus": "excluded_reset_summary",
            "archiveKind": archive_kind,
            "archiveSourceKey": source_key,
            "archivePullKey": archive_pull_key(meta),
            "archiveSummary": str(summary_path),
            "archiveIndex": str(index_path),
            "archiveCanonicalIndex": str(canonical_index_path),
            "archiveSyncStatus": sync_status,
            "archiveSyncPaths": sync_paths,
        }
        local_meta = dict(meta)
        local_meta.update(reset_metadata(meta))
        local_meta.update({k: v for k, v in result.items() if k != "archiveSyncPaths"})
        try:
            atomic_write_json(try_dir / "meta.json", local_meta)
        except Exception:
            pass
        return result

    out_dir = local_root / archive_kind / date / boss_dir_name
    out_dir.mkdir(parents=True, exist_ok=True)
    base = capsule_basename(meta)
    capsule_path = out_dir / f"{base}.wcla.zip"
    summary_path = out_dir / f"{base}.summary.json"

    payload, coverage = _source_payload(try_dir, meta, raw_path)
    source_key = payload["sourceKey"]

    # B37.3.4: sourceKey dedupe is date-global, not folder-local.  This prevents
    # the pre/post boss-slug rename case from writing the exact same immutable
    # pull into both encounter_3497 and lost_explorers directories.
    existing_matches = []
    kind_date_dir = local_root / archive_kind / date
    if kind_date_dir.is_dir():
        for existing_summary in kind_date_dir.rglob("*.summary.json"):
            doc = _read_json(existing_summary)
            if str(doc.get("sourceKey") or "") != str(source_key):
                continue
            row = _summary_row(existing_summary, kind_date_dir, archive_kind)
            if row:
                existing_matches.append(row)
    if existing_matches:
        chosen = max(existing_matches, key=_exact_source_quality)
        chosen_summary = Path(chosen["_path"])
        chosen_doc = _read_json(chosen_summary)
        chosen_capsule_name = str((chosen_doc.get("capsule") or {}).get("file") or "")
        chosen_capsule = chosen_summary.parent / chosen_capsule_name if chosen_capsule_name else None
        if chosen_capsule is not None and chosen_capsule.exists():
            summary_path = chosen_summary
            capsule_path = chosen_capsule
            out_dir = chosen_summary.parent
            base = capsule_path.name.removesuffix(".wcla.zip")

    # Never overwrite a capsule that already declares the same immutable source.
    existing_source_key = None
    if capsule_path.exists():
        try:
            with zipfile.ZipFile(capsule_path, "r") as zf:
                existing = json.loads(zf.read("source/manifest.json").decode("utf-8"))
                existing_source_key = existing.get("sourceKey")
        except Exception:
            existing_source_key = None

    if force or not capsule_path.exists() or existing_source_key != source_key:
        with tempfile.TemporaryDirectory(prefix="wcla_") as td:
            t = Path(td)
            source = t / "source"
            source.mkdir()
            manifest = {
                "schemaVersion": CAPSULE_SCHEMA_VERSION,
                "format": "WCLAnalyzer Compact Archive",
                "sourceKey": source_key,
                "sourceHash": payload["sourceHash"],
                "createdAt": datetime.now().isoformat(timespec="seconds"),
                "sourceLogRelative": _relative_source(meta.get("sourceLog")),
                "encounterID": meta.get("encounterID"),
                "difficultyID": meta.get("difficultyID"),
                "startTimestamp": meta.get("startTimestamp"),
                "filterPolicy": "event_kind_v1",
            }
            sanitized_meta = dict(meta)
            sanitized_meta.pop("sourceLog", None)
            sanitized_meta["sourceLogRelative"] = manifest["sourceLogRelative"]
            # Do not leak local absolute artifact paths into the long-term capsule.
            for key in list(sanitized_meta):
                if key.lower().endswith(("json", "txt", "log", "dir")) and isinstance(sanitized_meta.get(key), str):
                    val = str(sanitized_meta[key])
                    if Path(val).is_absolute():
                        sanitized_meta.pop(key, None)
            atomic_write_json(source / "manifest.json", manifest)
            atomic_write_json(source / "meta.json", sanitized_meta)
            atomic_write_json(source / "coverage.json", coverage)
            atomic_write_json(source / "roster.json", {"roster": payload["roster"]})
            atomic_write_json(source / "hp_1s.json", {"samples": payload["hp"]})
            atomic_write_json(source / "raid_dps_1s.json", {"samples": payload["dps"]})
            _gzip_jsonl(source / "events_core.jsonl.gz", payload["core"])
            _gzip_jsonl(source / "evidence_windows.jsonl.gz", payload["evidence"])

            tmp_zip = capsule_path.with_suffix(capsule_path.suffix + ".tmp")
            with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for p in sorted(source.rglob("*")):
                    if p.is_file():
                        zf.write(p, p.relative_to(t).as_posix())
            os.replace(tmp_zip, capsule_path)

    summary = _summary_from_reviews(try_dir, meta, source_key, capsule_path.name, capsule_path.stat().st_size)
    atomic_write_json(summary_path, summary)

    # B37.5.0.3 pre-live observation capture: derived/audit-only.  It never
    # feeds BossMechanic verdicts and a failure here must not block archiving.
    prelive_audit_path = None
    try:
        prelive_audit_path = write_kithix_observation(
            out_dir / f"{base}.kithix_observation.json",
            meta,
            payload.get("core") or [],
            hp_samples=payload.get("hp") or [],
        )
    except Exception:
        prelive_audit_path = None

    index_path = _write_daily_index(local_root / archive_kind / date)
    canonical_index_path = _write_canonical_index(local_root, date)

    sync_files = [capsule_path, summary_path, index_path, canonical_index_path]
    if prelive_audit_path is not None:
        sync_files.append(prelive_audit_path)
    sync_status, sync_paths = _sync_tree(sync_files, local_root, workdir)
    result = {
        "archiveStatus": "ready",
        "archiveKind": archive_kind,
        "archiveSourceKey": source_key,
        "archivePullKey": archive_pull_key(meta),
        "archiveCapsule": str(capsule_path),
        "archiveSummary": str(summary_path),
        "archiveIndex": str(index_path),
        "archiveCanonicalIndex": str(canonical_index_path),
        "archiveCapsuleBytes": int(capsule_path.stat().st_size),
        "archiveSyncStatus": sync_status,
        "archiveSyncPaths": sync_paths,
        "archiveAdvancedLogging": bool((coverage.get("advancedLogging") or {}).get("detected")),
        "archivePreliveAudit": str(prelive_audit_path) if prelive_audit_path is not None else None,
    }
    # Local meta may evolve; capsule meta remains immutable.
    local_meta = dict(meta)
    local_meta.update({k: v for k, v in result.items() if k != "archiveSyncPaths"})
    try:
        atomic_write_json(try_dir / "meta.json", local_meta)
    except Exception:
        pass
    return result


def sync_pending_archives(workdir: Path | str) -> dict:
    workdir = Path(workdir)
    local_root = workdir / "data" / "archive_outbox"
    if not local_root.is_dir():
        return {"status": "nothing_to_sync", "files": 0}
    # Rebuild legacy indexes before every retry.  This immediately repairs old
    # duplicate sourceKey rows and creates the cross-kind Canonical index without
    # rewriting immutable capsules.
    rebuilt = rebuild_archive_indexes(local_root)
    files = [p for p in local_root.rglob("*") if p.is_file() and not p.name.endswith(".tmp")]
    status, paths = _sync_tree(files, local_root, workdir)
    return {"status": status, "files": len(paths), "paths": paths, "indexesRebuilt": len(rebuilt)}
