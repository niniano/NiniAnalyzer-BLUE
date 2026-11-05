"""Passive pre-live encounter observation capture.

This module deliberately does *not* make mechanic judgments.  It records the
first real combat-log shape for pre-live catalog bosses so later rule work can
use observed encounter IDs, localized spell names, event types, timing and
phase-transition evidence without changing first-mistake/verdict semantics.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .boss_mechanics import normalize_compact_core
from .io_utils import atomic_write_json
from .skill_tips import boss_spells

AUDIT_SCHEMA_VERSION = 1
KITHIX_BOSS_KEY = "kithix"
KITHIX_NAME_ALIASES = ("키식스", "kithix", "kith'ix", "kith’ix")
KITHIX_LAST_LIGHT = 1305427
KITHIX_EIGHTFOLD_ECLIPSE = 1301479


def _norm_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "".join(ch for ch in text if ch.isalnum() or ("가" <= ch <= "힣"))


def _name_matches_kithix(value: Any) -> bool:
    norm = _norm_text(value)
    if not norm:
        return False
    return any(_norm_text(alias) in norm or norm in _norm_text(alias) for alias in KITHIX_NAME_ALIASES)


def _catalog_rows() -> list[dict]:
    return boss_spells(KITHIX_BOSS_KEY)


def _catalog_id_map() -> dict[int, dict]:
    out: dict[int, dict] = {}
    for row in _catalog_rows():
        for raw in [row.get("spellID"), *(row.get("relatedSpellIDs") or [])]:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                continue
            out.setdefault(sid, row)
    return out


def _safe_sec(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _is_npc_source(row: dict) -> bool:
    guid = str(row.get("sourceGUID") or "")
    if guid.startswith(("Creature-", "Vehicle-")):
        return True
    return str(row.get("sourceType") or "").strip().lower() in {"npc", "creature", "vehicle", "boss"}


def _spell_observations(rows: Iterable[dict], catalog: dict[int, dict]) -> tuple[list[dict], list[dict], set[str]]:
    buckets: dict[int, dict] = {}
    known_source_guids: set[str] = set()
    for row in rows or []:
        try:
            sid = int(row.get("spellID"))
        except (TypeError, ValueError):
            continue
        known = sid in catalog
        if not known and not _is_npc_source(row):
            continue
        if known:
            guid = str(row.get("sourceGUID") or "")
            if guid.startswith(("Creature-", "Vehicle-")):
                known_source_guids.add(guid)
        item = buckets.setdefault(sid, {
            "spellID": sid,
            "logNames": Counter(),
            "eventTypes": Counter(),
            "sourceNames": Counter(),
            "sourceGUIDs": set(),
            "destNames": set(),
            "firstSec": None,
            "lastSec": None,
            "count": 0,
        })
        name = str(row.get("spellName") or "").strip('" ')
        event = str(row.get("event") or "").strip()
        source_name = str(row.get("sourceName") or "").strip('" ')
        source_guid = str(row.get("sourceGUID") or "")
        dest_name = str(row.get("destName") or "").strip('" ')
        sec = _safe_sec(row.get("sec"))
        if name:
            item["logNames"][name] += 1
        if event:
            item["eventTypes"][event] += 1
        if source_name:
            item["sourceNames"][source_name] += 1
        if source_guid:
            item["sourceGUIDs"].add(source_guid)
        if dest_name:
            item["destNames"].add(dest_name)
        if sec is not None:
            item["firstSec"] = sec if item["firstSec"] is None else min(item["firstSec"], sec)
            item["lastSec"] = sec if item["lastSec"] is None else max(item["lastSec"], sec)
        item["count"] += 1

    def finish(sid: int, item: dict) -> dict:
        cat = catalog.get(sid) or {}
        log_name = item["logNames"].most_common(1)[0][0] if item["logNames"] else None
        return {
            "spellID": sid,
            "catalogNameKO": cat.get("nameKO") if cat else None,
            "catalogNameEN": cat.get("nameEN") if cat else None,
            "logSpellName": log_name,
            "nameMatchesCatalog": bool(log_name and cat and _norm_text(log_name) == _norm_text(cat.get("nameKO"))),
            "eventTypes": dict(sorted(item["eventTypes"].items())),
            "firstSec": item["firstSec"],
            "lastSec": item["lastSec"],
            "count": item["count"],
            "sourceNames": [x for x, _n in item["sourceNames"].most_common()],
            "sourceGUIDs": sorted(item["sourceGUIDs"]),
            "sampleDestNames": sorted(item["destNames"])[:12],
        }

    known_rows = [finish(sid, buckets[sid]) for sid in sorted(buckets, key=lambda x: (buckets[x]["firstSec"] is None, buckets[x]["firstSec"] or 10**9, x)) if sid in catalog]
    unknown_rows = [finish(sid, buckets[sid]) for sid in sorted(buckets, key=lambda x: (buckets[x]["firstSec"] is None, buckets[x]["firstSec"] or 10**9, x)) if sid not in catalog]
    return known_rows, unknown_rows, known_source_guids


def _first_sec(rows: list[dict], spell_id: int) -> float | None:
    for row in rows:
        if int(row.get("spellID") or 0) == int(spell_id):
            return _safe_sec(row.get("firstSec"))
    return None


def _boss_hp25_candidate(hp_samples: Iterable[dict], encounter: dict, known_source_guids: set[str]) -> float | None:
    candidates = []
    encounter_name = str((encounter or {}).get("name") or "")
    for row in hp_samples or []:
        try:
            pct = float(row.get("hpPct"))
            sec = float(row.get("sec"))
        except (TypeError, ValueError):
            continue
        if pct > 25.0:
            continue
        guid = str(row.get("guid") or "")
        name = str(row.get("name") or "")
        if guid in known_source_guids or _name_matches_kithix(name) or (_name_matches_kithix(encounter_name) and _norm_text(name) == _norm_text(encounter_name)):
            candidates.append(sec)
    return round(min(candidates), 3) if candidates else None


def build_kithix_observation(encounter: dict, core_rows: Iterable, *, hp_samples: Iterable[dict] | None = None) -> dict | None:
    """Return a passive Kith'ix observation document or ``None`` when unmatched.

    Match policy is intentionally conservative: an encounter-name alias is enough;
    otherwise at least two distinct pre-live catalog spell IDs must be observed.
    """
    catalog = _catalog_id_map()
    normalized = normalize_compact_core(core_rows or [], encounter or {})
    known, unknown_npc, known_source_guids = _spell_observations(normalized, catalog)
    observed_known_ids = sorted({int(row["spellID"]) for row in known})
    name_match = _name_matches_kithix((encounter or {}).get("name"))
    spell_match = len(observed_known_ids) >= 2
    if not (name_match or spell_match):
        return None

    last_light = _first_sec(known, KITHIX_LAST_LIGHT)
    eclipse = _first_sec(known, KITHIX_EIGHTFOLD_ECLIPSE)
    hp25 = _boss_hp25_candidate(hp_samples or [], encounter or {}, known_source_guids)
    if last_light is not None:
        selected, basis = last_light, "last_light_1305427"
    elif hp25 is not None:
        selected, basis = hp25, "boss_hp_at_or_below_25"
    elif eclipse is not None:
        selected, basis = eclipse, "eightfold_eclipse_1301479"
    else:
        selected, basis = None, None

    return {
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "purpose": "prelive_observation_only",
        "bossKey": KITHIX_BOSS_KEY,
        "judgmentPolicy": "no mechanic status/evidence/verdict is produced by this file",
        "match": {
            "encounterNameAlias": name_match,
            "catalogSpellOverlap": spell_match,
            "observedCatalogSpellIDs": observed_known_ids,
            "observedCatalogSpellCount": len(observed_known_ids),
        },
        "encounter": {
            "encounterID": (encounter or {}).get("encounterID"),
            "name": (encounter or {}).get("name"),
            "difficultyID": (encounter or {}).get("difficultyID") or (encounter or {}).get("difficulty"),
            "difficultyName": (encounter or {}).get("difficultyName"),
            "tryNo": (encounter or {}).get("tryNo"),
            "result": (encounter or {}).get("result"),
            "startTimestamp": (encounter or {}).get("startTimestamp"),
            "durationSec": (encounter or {}).get("durationSec"),
        },
        "catalog": {
            "spellCount": len(_catalog_rows()),
            "spellIDs": sorted(catalog),
        },
        "observedCatalogSpells": known,
        "observedUnknownNpcSpells": unknown_npc,
        "phaseCandidates": {
            "lastLightFirstSec": last_light,
            "eightfoldEclipseFirstSec": eclipse,
            "bossHp25FirstSec": hp25,
            "selectedP2CandidateSec": selected,
            "selectedP2Basis": basis,
        },
    }


def write_kithix_observation(path: Path | str, encounter: dict, core_rows: Iterable, *, hp_samples: Iterable[dict] | None = None) -> Path | None:
    doc = build_kithix_observation(encounter, core_rows, hp_samples=hp_samples)
    if doc is None:
        return None
    return atomic_write_json(Path(path), doc)
