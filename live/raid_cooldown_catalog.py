from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

# B36.5.1 keeps the auto raid catalog separate from the per-character selector
# catalog.  The selector catalog answers "what should Live save for this player";
# this catalog answers "what shield-relevant major cooldown is normally used by
# this specialization in a verified reference sample".
CATALOG_SCHEMA_VERSION = 2
DEFAULT_MAJOR_WINDOW_BEFORE = 12.0
DEFAULT_MAJOR_WINDOW_AFTER = 10.0
DEFAULT_MIN_REFERENCE_SAMPLES = 2
DEFAULT_MIN_HIT_RATE = 0.67

# B36.5.1 catalog learning is for specialization/class cooldowns.  Bloodlust is
# already judged by the dedicated B36.5 blood slot, and the Ula'tek heart is
# equipment rather than a specialization ability, so neither belongs in the
# automatic spec catalog.
AUTO_SPEC_EXCLUDED_SPELL_IDS = {80353, 1297761}


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def catalog_path() -> Path:
    return _root() / "data" / "raid_cooldown_catalog.json"


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _pkey(name) -> str:
    text = str(name or "").strip()
    return text.split("-")[0].casefold() if text else ""


def _event(row: dict) -> str:
    return str(row.get("event") or "").strip().lower()


def _spell_id(row: dict):
    return _safe_int(row.get("spellID") if row.get("spellID") is not None else row.get("abilityGameID"))


def _completed_cast(row: dict) -> bool:
    return _event(row) in {"cast", "spell_cast_success", "cast_success"}


def resolve_roster_specs(rows: Iterable[dict]) -> dict:
    """Resolve player name -> specID from normalized raw/WCL rows.

    Local raw COMBATANT_INFO carries GUID + specID but no name, while ordinary
    spell rows carry the same GUID + name.  WCL fixtures may instead attach
    sourceSpecID/targetSpecID directly.  Keep all three forms and expose coverage
    so a missing spec never turns into a false cooldown failure.
    """
    names_by_guid: dict[str, str] = {}
    spec_by_guid: dict[str, int] = {}
    direct: dict[str, tuple[str, int]] = {}

    for row in rows or []:
        sguid = str(row.get("sourceGUID") or "")
        tguid = str(row.get("destGUID") or row.get("targetGUID") or "")
        sname = row.get("sourceName")
        tname = row.get("targetName") or row.get("destName")
        if sguid.startswith("Player-") and sname:
            names_by_guid.setdefault(sguid, str(sname))
        if tguid.startswith("Player-") and tname:
            names_by_guid.setdefault(tguid, str(tname))

        sid = _safe_int(row.get("specID"))
        if sid is not None:
            if sguid.startswith("Player-"):
                spec_by_guid[sguid] = sid
            elif sname:
                direct[_pkey(sname)] = (str(sname), sid)
        ssid = _safe_int(row.get("sourceSpecID"))
        if ssid is not None:
            if sguid.startswith("Player-"):
                spec_by_guid[sguid] = ssid
            elif sname:
                direct[_pkey(sname)] = (str(sname), ssid)
        tsid = _safe_int(row.get("targetSpecID"))
        if tsid is not None:
            if tguid.startswith("Player-"):
                spec_by_guid[tguid] = tsid
            elif tname:
                direct[_pkey(tname)] = (str(tname), tsid)

    resolved: dict[str, dict] = {}
    for guid, spec_id in spec_by_guid.items():
        name = names_by_guid.get(guid)
        if not name:
            continue
        resolved[_pkey(name)] = {"name": name, "specID": spec_id, "guid": guid, "source": "combatant_info"}
    for key, (name, spec_id) in direct.items():
        resolved.setdefault(key, {"name": name, "specID": spec_id, "guid": None, "source": "normalized_spec"})
    return {
        "players": resolved,
        "resolvedCount": len(resolved),
        "specIDs": sorted({int(x["specID"]) for x in resolved.values()}),
        "source": "combatant_info_or_normalized_spec" if resolved else "unavailable",
    }


def load_catalog(path: Path | None = None) -> dict:
    path = Path(path or catalog_path())
    if not path.exists():
        return {
            "schemaVersion": CATALOG_SCHEMA_VERSION,
            "source": "missing",
            "path": str(path),
            "specCatalogs": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {
            "schemaVersion": CATALOG_SCHEMA_VERSION,
            "source": "read_error",
            "path": str(path),
            "specCatalogs": {},
        }
    if not isinstance(payload, dict):
        payload = {}
    schema = _safe_int(payload.get("schemaVersion")) or 0
    if schema < CATALOG_SCHEMA_VERSION:
        return {
            "schemaVersion": CATALOG_SCHEMA_VERSION,
            "source": "stale_schema",
            "staleSchemaVersion": schema,
            "path": str(path),
            "specCatalogs": {},
        }
    payload.setdefault("schemaVersion", CATALOG_SCHEMA_VERSION)
    payload.setdefault("specCatalogs", {})
    payload["path"] = str(path)
    return payload


def save_catalog(payload: dict, path: Path | None = None) -> Path:
    path = Path(path or catalog_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def _median(values: list[float]):
    return round(float(statistics.median(values)), 3) if values else None


def learn_catalog_from_samples(
    samples: Iterable[dict],
    *,
    major_spell_names: dict[int, str] | None = None,
    min_samples: int = DEFAULT_MIN_REFERENCE_SAMPLES,
    min_hit_rate: float = DEFAULT_MIN_HIT_RATE,
    before_sec: float = DEFAULT_MAJOR_WINDOW_BEFORE,
    after_sec: float = DEFAULT_MAJOR_WINDOW_AFTER,
    source_label: str = "reference_samples",
) -> dict:
    """Learn spec/shield major-cooldown expectations from successful samples.

    Each sample is ``{"events": [...], "shieldCycles": [...]}`` (a full mechanic
    review may be provided under ``review`` instead).  Only spell IDs in
    ``major_spell_names`` are eligible.  This intentionally discovers *timing
    habits*, not class mechanics: a spell is promoted only after it repeatedly
    appears near the same shield index for players of the same spec.
    """
    major_spell_names = {
        int(k): str(v) for k, v in (major_spell_names or {}).items()
        if int(k) not in AUTO_SPEC_EXCLUDED_SPELL_IDS
    }
    opp: dict[tuple[int, int, int], int] = defaultdict(int)
    hit: dict[tuple[int, int, int], int] = defaultdict(int)
    offsets: dict[tuple[int, int, int], list[float]] = defaultdict(list)
    sample_ids: dict[tuple[int, int, int], set[str]] = defaultdict(set)

    normalized_samples = []
    for idx, sample in enumerate(samples or []):
        events = list(sample.get("events") or [])
        review = sample.get("review") or {}
        cycles = list(sample.get("shieldCycles") or ((review.get("observations") or {}).get("shieldCycles") or []))
        if not events or not cycles:
            continue
        roster = resolve_roster_specs(events).get("players") or {}
        if not roster:
            continue
        sample_id = str(sample.get("sampleID") or sample.get("label") or idx + 1)
        normalized_samples.append(sample_id)
        casts_by_player: dict[str, list[dict]] = defaultdict(list)
        for row in events:
            sid = _spell_id(row)
            if sid not in major_spell_names or not _completed_cast(row):
                continue
            name = row.get("sourceName")
            key = _pkey(name)
            if key and key in roster:
                casts_by_player[key].append(row)

        for cyc in cycles:
            try:
                shield_index = int(cyc.get("index"))
                start = float(cyc.get("startSec"))
            except (TypeError, ValueError):
                continue
            for pkey, pinfo in roster.items():
                spec_id = _safe_int(pinfo.get("specID"))
                if spec_id is None:
                    continue
                player_casts = casts_by_player.get(pkey) or []
                # One player contributes at most one hit per spell per shield.
                # WCL can surface a single action twice a few milliseconds apart
                # (Power Infusion is a verified example), and counting both makes
                # hitRate exceed 1.0.  Keep the cast closest to shield start.
                best_by_spell: dict[int, float] = {}
                for row in player_casts:
                    sid = _spell_id(row)
                    if sid is None:
                        continue
                    sec = float(row.get("sec") or 0)
                    delta = sec - start
                    if -float(before_sec) <= delta <= float(after_sec):
                        prev = best_by_spell.get(sid)
                        if prev is None or abs(delta) < abs(prev):
                            best_by_spell[sid] = delta
                for sid, delta in best_by_spell.items():
                    key = (spec_id, shield_index, sid)
                    hit[key] += 1
                    offsets[key].append(delta)
                    sample_ids[key].add(sample_id)

    # Denominator: for each promoted candidate spell, every resolved player of
    # that spec at that shield in every usable sample is an opportunity.
    candidates = list(hit)
    for idx, sample in enumerate(samples or []):
        events = list(sample.get("events") or [])
        review = sample.get("review") or {}
        cycles = list(sample.get("shieldCycles") or ((review.get("observations") or {}).get("shieldCycles") or []))
        roster = resolve_roster_specs(events).get("players") or {}
        if not roster or not cycles:
            continue
        for cyc in cycles:
            shield_index = _safe_int(cyc.get("index"))
            if shield_index is None or cyc.get("startSec") is None:
                continue
            by_spec = defaultdict(int)
            for pinfo in roster.values():
                sid = _safe_int(pinfo.get("specID"))
                if sid is not None:
                    by_spec[sid] += 1
            for spec_id, si, spell_id in candidates:
                if si == shield_index:
                    opp[(spec_id, si, spell_id)] += by_spec.get(spec_id, 0)

    spec_catalogs: dict[str, dict] = {}
    for key in sorted(candidates):
        spec_id, shield_index, spell_id = key
        opportunities = int(opp.get(key) or 0)
        hits = int(hit.get(key) or 0)
        references = len(sample_ids.get(key) or set())
        ratio = (hits / opportunities) if opportunities else 0.0
        if references < int(min_samples) or ratio < float(min_hit_rate):
            continue
        values = offsets.get(key) or []
        expected = _median(values)
        deviations = [abs(x - float(expected or 0)) for x in values]
        # Never make learned tolerance narrower than the existing B36.5 manual
        # alignment defaults.  Top-log variation can widen it, not overfit it.
        learned_tol = max(5.0, (_median(deviations) or 0.0) + 2.0)
        block = spec_catalogs.setdefault(str(spec_id), {"specID": spec_id, "entries": []})
        block["entries"].append({
            "spellID": spell_id,
            "name": major_spell_names.get(spell_id) or str(spell_id),
            "shieldIndex": shield_index,
            "expectedOffsetSec": expected,
            "toleranceBeforeSec": round(learned_tol, 3),
            "toleranceAfterSec": round(learned_tol, 3),
            "evidence": {
                "referenceSampleCount": references,
                "opportunityCount": opportunities,
                "hitCount": hits,
                "hitRate": round(ratio, 4),
                "source": source_label,
            },
        })

    return {
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "source": source_label,
        "specIDContract": "wow_global_specialization_id",
        "learningContract": {
            "minReferenceSamples": int(min_samples),
            "minHitRate": float(min_hit_rate),
            "windowBeforeSec": float(before_sec),
            "windowAfterSec": float(after_sec),
            "excludedSpellIDs": sorted(AUTO_SPEC_EXCLUDED_SPELL_IDS),
            "note": "외부/성공 표본에서 같은 전문화가 같은 보호막에 반복 사용한 주요 쿨기만 자동 카탈로그로 승격. 기본 판정 모드는 observe이며 raidPlan이 judge로 올리기 전에는 원인 A에 사용하지 않음.",
        },
        "specCatalogs": spec_catalogs,
    }


def entries_for(catalog: dict, spec_id: int, shield_index: int) -> list[dict]:
    block = ((catalog or {}).get("specCatalogs") or {}).get(str(int(spec_id))) or {}
    return [dict(x) for x in (block.get("entries") or []) if _safe_int(x.get("shieldIndex")) == int(shield_index)]
