from __future__ import annotations

import bisect
import copy
import json
import math
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from ..encounter_tracker import parse_log_line, parse_combat_datetime, safe_int
from ..io_utils import atomic_write_json, atomic_write_text
from ..verdict_projection import mechanic_failure_slots, build_presentation_verdict
from ..mechanics import TANK_SPECS
from ..raid_cooldown_catalog import load_catalog as load_raid_cooldown_catalog, entries_for as raid_cooldown_entries_for, resolve_roster_specs
from ..rawlog import open_raw_text
from ..boss_registry import boss_key as registry_boss_key
from .contracts import (
    EVIDENCE_CANDIDATE,
    EVIDENCE_UNAVAILABLE,
    EVIDENCE_VERIFIED,
    evidence_label,
    load_all_rules,
    load_rule,
    load_strategy,
    make_check,
)

ENGINE_VERSION = "boss-mechanic-v1.4.14"
SCHEMA_VERSION = 22
RULES_VERSION = "B37.5.0.5"

# Discovery-only localized labels. They NEVER decide success/failure and are not
# part of rule files. They exist only to surface candidate spell IDs to the probe
# until those IDs are verified and promoted into a rule JSON.
_DISCOVERY_CONDENSED_VENOM_NAMES = {"응결된 맹독", "Condensed Venom", "Coalesced Venom"}
_DISCOVERY_GUILLOTINE_NAMES = {"단두대", "냉혹한 단두대", "Guillotine"}
_DISCOVERY_TOXIC_RUPTURE_NAMES = {"맹독성 파열", "Toxic Rupture", "Venom Rupture"}


def _rule_for_encounter(encounter: dict) -> dict | None:
    enc_id = safe_int((encounter or {}).get("encounterID") or (encounter or {}).get("encounterId"))
    name = str((encounter or {}).get("name") or (encounter or {}).get("encounterName") or "").strip()

    # B37.3.9: stable encounter identity comes from one canonical registry.
    # Rule JSON files continue to own spell/mechanic data, but storage/UI/rule
    # dispatch no longer maintain separate encounter->boss alias tables.
    key = registry_boss_key(enc_id) if enc_id is not None else None
    if key:
        rule = load_rule(key)
        if rule:
            return rule

    # Compatibility for unregistered/legacy encounters while the registry grows.
    for rule in load_all_rules():
        ids = {safe_int(x) for x in (rule.get("encounterIDs") or []) if safe_int(x) is not None}
        if enc_id is not None and enc_id in ids:
            return rule
    # Temporary fallback only when the encounter ID itself is not yet known.
    for rule in load_all_rules():
        if name and name in set(rule.get("encounterNameFallbacks") or []):
            return rule
    return None


def supports_encounter(encounter: dict) -> bool:
    return _rule_for_encounter(encounter) is not None


def _sec(ts: str, start_dt):
    dt = parse_combat_datetime(ts, now=start_dt)
    if not dt or not start_dt:
        return None
    value = (dt - start_dt).total_seconds()
    if value < -43200:
        value += 86400
    return round(value, 4)


def _guid_npc_id(guid) -> int | None:
    text = str(guid or "")
    if not text.startswith(("Creature-", "Vehicle-")):
        return None
    parts = text.split("-")
    if len(parts) < 6:
        return None
    return safe_int(parts[5])


def _spell_fields(parsed: dict):
    fields = parsed.get("fields") or []
    event = str(parsed.get("event") or "")
    if event.startswith("SWING_"):
        return None
    if len(fields) < 12 or not event.startswith("SPELL_"):
        return None
    source_guid = fields[1] if len(fields) > 1 else None
    dest_guid = fields[5] if len(fields) > 5 else None
    out = {
        "event": event,
        "sourceGUID": source_guid,
        "sourceName": fields[2] if len(fields) > 2 else None,
        "sourceType": "NPC" if str(source_guid or "").startswith(("Creature-", "Vehicle-")) else "Player" if str(source_guid or "").startswith("Player-") else None,
        "sourceGameID": _guid_npc_id(source_guid),
        "destGUID": dest_guid,
        "destName": fields[6] if len(fields) > 6 else None,
        "targetType": "Player" if str(dest_guid or "").startswith("Player-") else "NPC" if str(dest_guid or "").startswith(("Creature-", "Vehicle-")) else None,
        "targetGameID": _guid_npc_id(dest_guid),
        "spellID": safe_int(fields[9]) if len(fields) > 9 else None,
        "spellName": fields[10] if len(fields) > 10 else None,
    }
    # SPELL_INTERRUPT carries the interrupted ability after the interrupting spell.
    if event == "SPELL_INTERRUPT" and len(fields) > 13:
        out["extraSpellID"] = safe_int(fields[12])
        out["extraSpellName"] = fields[13]

    # B36.5: shield-cycle evidence needs the variable aura amount carried by
    # advanced CombatLog aura rows.  For Twilight Veil (1286912), the apply
    # amount is the initial absorb pool and the remove row reaches 0.
    if event.startswith("SPELL_AURA_") and len(fields) > 12:
        out["auraType"] = fields[12]
        if len(fields) > 13:
            out["auraAmount"] = safe_int(fields[13])
        # Aura dose rows carry the stack count immediately after auraType in
        # the retail CombatLog payload. Keep it separate from auraAmount so
        # absorb-aura accounting (B36.5) and stack mechanics (B37) cannot
        # accidentally reinterpret one another.
        if event in {"SPELL_AURA_APPLIED_DOSE", "SPELL_AURA_REMOVED_DOSE"} and len(fields) > 13:
            out["auraStack"] = safe_int(fields[13])

    # SPELL_ABSORBED has two retail payload forms.  Spell damage includes the
    # attacking spell before the absorbing aura; white/swing-style sources jump
    # directly to the absorbing actor/aura.  Preserve the absorber ID and the
    # *first* numeric amount after the absorb aura: on the supplied advanced raw
    # log, those amounts sum exactly to the initial Twilight Veil pool.
    if event == "SPELL_ABSORBED":
        has_attacking_spell = len(fields) > 9 and safe_int(fields[9]) is not None
        base = 12 if has_attacking_spell else 9
        # In the no-attacking-spell form, fields[9] is a GUID. Do not expose it
        # as the attacking spell ID/name.
        if not has_attacking_spell:
            out["spellID"] = None
            out["spellName"] = None
        if len(fields) > base + 7:
            out["absorbingSourceGUID"] = fields[base]
            out["absorbingSourceName"] = fields[base + 1]
            out["absorbingSpellID"] = safe_int(fields[base + 4])
            out["absorbingSpellName"] = fields[base + 5]
            out["absorbedAmount"] = safe_int(fields[base + 7])
            if len(fields) > base + 8:
                out["absorbedOriginalAmount"] = safe_int(fields[base + 8])

    # Advanced Combat Logging appends a unit snapshot after the normal spell
    # payload. The snapshot may describe the destination (damage/heal rows) or
    # the source (many cast rows). Preserve both forms so local spatial analysis
    # can use player/ghost positions without guessing from names.
    if len(fields) > 15:
        info_guid = str(fields[12] or "")
        cur = safe_int(fields[14])
        max_hp = safe_int(fields[15])
        try:
            snap_x = float(fields[26])
            snap_y = float(fields[27])
        except (TypeError, ValueError, IndexError):
            snap_x = snap_y = None

        if info_guid == str(dest_guid or ""):
            if cur is not None and max_hp is not None and max_hp > 0:
                out["targetCurrentHP"] = cur
                out["targetMaxHP"] = max_hp
                out["targetHealthPct"] = round(100.0 * float(cur) / float(max_hp), 4)
            if snap_x is not None and snap_y is not None:
                out["targetX"] = snap_x
                out["targetY"] = snap_y
            # Retail advanced SPELL_DAMAGE payloads expose the final amount
            # followed by the pre-mitigation amount for the damaged target.
            if event in {"SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE"}:
                try:
                    out["amount"] = float(fields[31])
                    out["unmitigatedAmount"] = float(fields[32])
                except (TypeError, ValueError, IndexError):
                    pass
        elif info_guid == str(source_guid or ""):
            if cur is not None and max_hp is not None and max_hp > 0:
                out["sourceCurrentHP"] = cur
                out["sourceMaxHP"] = max_hp
                out["sourceHealthPct"] = round(100.0 * float(cur) / float(max_hp), 4)
            if snap_x is not None and snap_y is not None:
                out["sourceX"] = snap_x
                out["sourceY"] = snap_y
    return out


FEIGN_DEATH_SPELL_ID = 5384
FEIGN_DEATH_WINDOW_SEC = 3.0
FEIGN_CONFIRM_SEC = 30.0
_FEIGN_CAST_EVIDENCE = frozenset({"SPELL_CAST_SUCCESS", "SPELL_CAST_START"})
_FEIGN_MELEE_EVIDENCE = frozenset({"SWING_DAMAGE", "SWING_MISSED", "RANGE_DAMAGE", "RANGE_MISSED"})
_FEIGN_MELEE_MIN_GAP_SEC = 1.5


def _drop_feign_deaths(rows: list[dict]) -> list[dict]:
    """RC4.1: drop a player death only with positive Feign Death evidence.

    A UNIT_DIED within FEIGN_DEATH_WINDOW_SEC after Feign Death (5384) is a
    candidate.  It is removed only when the same player acts again (cast, or
    melee/auto-shot after a short gap) within FEIGN_CONFIRM_SEC and nobody
    resurrected them in between.  Anything uncertain stays a death.
    """
    last_fd: dict[str, float] = {}
    candidates: dict[str, tuple[int, float]] = {}
    drop: set[int] = set()
    for idx, row in enumerate(rows):
        ev = str(row.get("event") or "")
        try:
            sec = float(row.get("sec"))
        except (TypeError, ValueError):
            sec = None
        if sec is None:
            continue
        src = str(row.get("sourceGUID") or "")
        dst = str(row.get("destGUID") or "")
        if candidates:
            if ev == "SPELL_RESURRECT" and dst in candidates:
                candidates.pop(dst, None)
            cand = candidates.get(src)
            if cand is not None:
                gap = sec - cand[1]
                if gap > FEIGN_CONFIRM_SEC:
                    candidates.pop(src, None)
                elif ((ev in _FEIGN_CAST_EVIDENCE and gap > 0.0)
                        or (ev in _FEIGN_MELEE_EVIDENCE and gap >= _FEIGN_MELEE_MIN_GAP_SEC)):
                    drop.add(cand[0])
                    candidates.pop(src, None)
        if safe_int(row.get("spellID")) == FEIGN_DEATH_SPELL_ID:
            if ev == "SPELL_CAST_SUCCESS":
                guid = src
            elif ev in ("SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH"):
                guid = dst
            else:
                guid = ""
            if guid.startswith("Player-"):
                last_fd[guid] = sec
        if ev in ("UNIT_DIED", "UNIT_DESTROYED") and dst.startswith("Player-"):
            fd = last_fd.get(dst)
            if fd is not None and 0.0 <= sec - fd <= FEIGN_DEATH_WINDOW_SEC and dst not in candidates:
                candidates[dst] = (idx, sec)
                last_fd.pop(dst, None)
    if not drop:
        return rows
    return [row for i, row in enumerate(rows) if i not in drop]


def _normalize_raw(raw_path: Path, encounter: dict) -> list[dict]:
    start = parse_combat_datetime(str(encounter.get("startTimestamp") or ""))
    rows = []
    with open_raw_text(raw_path) as fh:
        for line in fh:
            parsed = parse_log_line(line)
            if not parsed:
                continue
            event = str(parsed.get("event") or "")
            fields = parsed.get("fields") or []
            if event == "COMBATANT_INFO" and len(fields) > 1:
                guid = fields[1]
                spec_id = None
                # Retail COMBATANT_INFO has variable stat prefixes.  The first
                # talent array is stable enough to anchor the preceding specID,
                # which is the same rule used by raid_facts.py.
                for i, value in enumerate(fields):
                    if str(value).lstrip().startswith("["):
                        candidate = safe_int(fields[i - 1]) if i else None
                        if candidate is not None:
                            spec_id = candidate
                        break
                rows.append({
                    "event": "combatant_info",
                    "sec": _sec(parsed.get("timestamp"), start),
                    "sourceGUID": guid,
                    "sourceType": "Player" if str(guid or "").startswith("Player-") else None,
                    "specID": spec_id,
                })
                continue
            sf = _spell_fields(parsed)
            if sf is None:
                if event in {"UNIT_DIED", "UNIT_DESTROYED"} and len(fields) >= 9:
                    dest_guid = fields[5]
                    sf = {
                        "event": event,
                        "sourceGUID": fields[1] if len(fields) > 1 else None,
                        "sourceName": fields[2] if len(fields) > 2 else None,
                        "destGUID": dest_guid,
                        "destName": fields[6] if len(fields) > 6 else None,
                        "targetType": "Player" if str(dest_guid or "").startswith("Player-") else "NPC" if str(dest_guid or "").startswith(("Creature-", "Vehicle-")) else None,
                        "targetGameID": _guid_npc_id(dest_guid),
                    }
                else:
                    continue
            sf["sec"] = _sec(parsed.get("timestamp"), start)
            rows.append(sf)
    return _drop_feign_deaths(rows)


def normalize_compact_core(core_rows: Iterable, encounter: dict) -> list[dict]:
    """Normalize compact-archive ``events_core`` rows for BossMechanic re-judgment.

    The compact capsule stores exact parsed CSV fields for all event categories
    needed by the mechanic layer.  It intentionally omits heavy player->NPC
    damage/heal rows, so archive re-judgment is treated as a complete semantic
    stream for retained mechanic events but does not claim raw-only spatial
    coverage.
    """
    start = parse_combat_datetime(str((encounter or {}).get("startTimestamp") or ""))
    rows: list[dict] = []
    for item in core_rows or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        timestamp, fields = item[0], item[1]
        if not isinstance(fields, list) or not fields:
            continue
        event = str(fields[0] or "")
        if event == "COMBATANT_INFO" and len(fields) > 1:
            guid = fields[1]
            spec_id = None
            for i, value in enumerate(fields):
                if str(value).lstrip().startswith("["):
                    candidate = safe_int(fields[i - 1]) if i else None
                    if candidate is not None:
                        spec_id = candidate
                    break
            rows.append({
                "event": "combatant_info",
                "sec": _sec(str(timestamp or ""), start),
                "sourceGUID": guid,
                "sourceType": "Player" if str(guid or "").startswith("Player-") else None,
                "specID": spec_id,
            })
            continue
        parsed = {"timestamp": timestamp, "event": event, "fields": fields}
        sf = _spell_fields(parsed)
        if sf is None:
            if event in {"UNIT_DIED", "UNIT_DESTROYED"} and len(fields) >= 9:
                dest_guid = fields[5]
                sf = {
                    "event": event,
                    "sourceGUID": fields[1] if len(fields) > 1 else None,
                    "sourceName": fields[2] if len(fields) > 2 else None,
                    "destGUID": dest_guid,
                    "destName": fields[6] if len(fields) > 6 else None,
                    "targetType": "Player" if str(dest_guid or "").startswith("Player-") else "NPC" if str(dest_guid or "").startswith(("Creature-", "Vehicle-")) else None,
                    "targetGameID": _guid_npc_id(dest_guid),
                }
            else:
                continue
        sf["sec"] = _sec(str(timestamp or ""), start)
        rows.append(sf)
    return _drop_feign_deaths(rows)


def _event_name(row: dict) -> str:
    return str(row.get("event") or "").strip().lower()


def _is_creature_source(row: dict) -> bool:
    guid = str(row.get("sourceGUID") or "")
    if guid.startswith(("Creature-", "Vehicle-")):
        return True
    stype = str(row.get("sourceType") or row.get("actorType") or "").strip().lower()
    return stype in {"npc", "creature", "vehicle", "boss"}



def _is_playerish_source(row: dict) -> bool:
    return str(row.get("sourceType") or "").strip().lower() in {"player", "pet"}


def _is_player_target(row: dict) -> bool:
    ttype = str(row.get("targetType") or "").strip().lower()
    if ttype == "player":
        return True
    guid = str(row.get("destGUID") or "")
    return guid.startswith("Player-")


def _is_completed_cast(row: dict) -> bool:
    return _event_name(row) in {"spell_cast_success", "cast"}


def _is_aura_apply(row: dict) -> bool:
    return _event_name(row) in {"spell_aura_applied", "applydebuff", "applybuff"}


def _is_aura_remove(row: dict) -> bool:
    return _event_name(row) in {"spell_aura_removed", "removedebuff", "removebuff"}


def _is_cast_start(row: dict) -> bool:
    return _event_name(row) in {"spell_cast_start", "begincast", "cast_start", "caststart"}


def _is_interrupt(row: dict) -> bool:
    return _event_name(row) in {"spell_interrupt", "interrupt"}


def _is_damage(row: dict) -> bool:
    return _event_name(row) in {"spell_damage", "spell_periodic_damage", "damage"}


def _target_key(row: dict):
    guid = row.get("destGUID")
    if guid:
        return guid
    tid = row.get("targetID")
    inst = row.get("targetInstance") or row.get("targetInstanceID") or row.get("targetInstanceId")
    if tid not in (None, ""):
        return (tid, inst)
    return row.get("destName") or row.get("targetName")


def _player_identity(row: dict, *, side: str):
    """Return a stable player identity for raid-size/death eligibility guards."""
    if side == "source":
        guid = str(row.get("sourceGUID") or "")
        ptype = str(row.get("sourceType") or "").strip().lower()
        if ptype != "player" and not guid.startswith("Player-"):
            return None
        if guid.startswith("Player-"):
            return ("guid", guid)
        pid = row.get("sourceID")
        if pid not in (None, ""):
            return ("id", pid)
        name = row.get("sourceName")
        return ("name", str(name)) if name else None

    guid = str(row.get("destGUID") or "")
    ptype = str(row.get("targetType") or "").strip().lower()
    if ptype != "player" and not guid.startswith("Player-"):
        return None
    if guid.startswith("Player-"):
        return ("guid", guid)
    pid = row.get("targetID")
    if pid not in (None, ""):
        return ("id", pid)
    name = row.get("targetName") or row.get("destName")
    return ("name", str(name)) if name else None


def _coiled_survival_context(rows: list[dict]) -> dict:
    """Build a conservative raid roster/death timeline for judgment exclusion.

    Current WCL probe streams include deaths but not explicit combat-resurrection
    events.  The alive count is therefore used only as a coarse guard (below
    half the raid), while the primary exclusion anchor remains the verified
    4-deaths-in-1s collapse cluster.
    """
    roster = set()
    death_times: dict[object, float] = {}
    for row in rows:
        for side in ("source", "target"):
            key = _player_identity(row, side=side)
            if key is not None:
                roster.add(key)
        if _event_name(row) in {"death", "unit_died"}:
            key = _player_identity(row, side="target")
            if key is not None:
                sec = float(row.get("sec") or 0)
                prior = death_times.get(key)
                if prior is None or sec < prior:
                    death_times[key] = sec
    return {
        "raidSize": len(roster),
        "roster": roster,
        "firstDeathByPlayer": death_times,
    }


def _coiled_judgment_guard(
    sec: float,
    collapse_snapshot: dict | None,
    survival_context: dict | None,
    strategy: dict | None,
) -> dict:
    """Mark post-collapse / severely depleted mechanics as descriptive only."""
    cfg = ((strategy or {}).get("orbAccounting") or {}) if isinstance(strategy, dict) else {}
    exclude_after_collapse = bool(cfg.get("excludeAfterCollapse", True))
    try:
        alive_fraction_min = float(cfg.get("excludeBelowAliveFraction", 0.5))
    except (TypeError, ValueError):
        alive_fraction_min = 0.5
    try:
        min_raid_size = int(cfg.get("minRaidSizeForAliveGuard", 10))
    except (TypeError, ValueError):
        min_raid_size = 10

    significant = bool((collapse_snapshot or {}).get("significantCluster"))
    collapse_sec = (collapse_snapshot or {}).get("collapseSec")
    after_collapse = False
    if exclude_after_collapse and significant and collapse_sec is not None:
        after_collapse = float(sec) >= float(collapse_sec)

    survival_context = survival_context or {}
    raid_size = int(survival_context.get("raidSize") or 0)
    deaths = survival_context.get("firstDeathByPlayer") or {}
    # Strictly before this mechanic. A death produced by the mechanic itself must
    # not retroactively make that same mechanic "post-collapse".
    dead_before = sum(1 for t in deaths.values() if float(t) < float(sec))
    alive = max(0, raid_size - dead_before) if raid_size else None
    below_half = bool(
        raid_size >= min_raid_size
        and alive is not None
        and float(alive) < float(raid_size) * alive_fraction_min
    )

    reasons = []
    if after_collapse:
        reasons.append("after_significant_collapse")
    if below_half:
        reasons.append("alive_below_half")
    excluded = bool(reasons)
    if after_collapse:
        label = "붕괴 후 브레스 · 판정 제외"
    elif below_half:
        label = "생존 인원 절반 미만 · 판정 제외"
    else:
        label = None
    return {
        "judgmentExcluded": excluded,
        "judgmentEligible": not excluded,
        "judgmentExclusionReasons": reasons,
        "judgmentExclusionLabel": label,
        "raidSize": raid_size or None,
        "alivePlayers": alive,
        "deadPlayersObserved": dead_before if raid_size else None,
        "aliveFraction": (round(alive / raid_size, 3) if raid_size and alive is not None else None),
        "collapseSec": collapse_sec if significant else None,
    }


def _group_times(rows: list[dict], *, max_gap: float) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for row in sorted(rows, key=lambda x: float(x.get("sec") or 0)):
        sec = float(row.get("sec") or 0)
        if not groups or sec - float(groups[-1][-1].get("sec") or 0) > max_gap:
            groups.append([row])
        else:
            groups[-1].append(row)
    return groups


def _mythic(encounter: dict) -> bool:
    return safe_int(encounter.get("difficultyID") or encounter.get("difficulty")) in {5, 16, 233}


def _fmt_time(sec) -> str:
    try:
        value = round(max(0.0, float(sec)), 1)
    except (TypeError, ValueError):
        return "-"
    mm = int(value // 60)
    ss = value - mm * 60
    return f"{mm}:{ss:04.1f}"


def _strategy_slots_by_guillotine(breaths: list[dict], orb_waves: list[dict], guillotine_markers: list[dict]) -> dict[int, dict]:
    """Map raid-strategy slots to the last two breaths before Guillotine 1/2.

    The strategy wording itself is Guillotine-anchored. For each of the first
    two Guillotines, use the latest orb wave in that Guillotine window and the
    last two verified breaths after that wave. This maps 23/40s to Guillotine 1
    and 108/125s to Guillotine 2 in all three reference mythic kills.
    """
    if not guillotine_markers:
        return {}
    slot_pairs = [
        ("wave1_breath1", "wave1_breath2"),
        ("wave2_breath1", "wave2_breath2"),
    ]
    out: dict[int, dict] = {}
    prior_guillotine_sec = float("-inf")
    for gi, marker in enumerate(guillotine_markers[:2], start=1):
        marker_sec = float(marker.get("sec") or 0)
        waves = [w for w in orb_waves if prior_guillotine_sec < float(w.get("sec") or 0) < marker_sec]
        if not waves:
            prior_guillotine_sec = marker_sec
            continue
        anchor_wave = waves[-1]
        wave_sec = float(anchor_wave.get("sec") or 0)
        candidates = [
            (bi, b) for bi, b in enumerate(breaths)
            if wave_sec <= float(b.get("sec") or 0) < marker_sec
        ]
        if len(candidates) >= 2:
            chosen = candidates[-2:]
            for pos, (bi, _breath) in enumerate(chosen):
                out[bi] = {
                    "slot": slot_pairs[gi - 1][pos],
                    "guillotineIndex": gi,
                    "guillotineSec": round(marker_sec, 3),
                    "anchorOrbWaveIndex": int(anchor_wave.get("index") or 0) or None,
                    "anchorOrbWaveSec": round(wave_sec, 3),
                    "positionBeforeGuillotine": pos + 1,
                }
        prior_guillotine_sec = marker_sec
    return out


def _coiled_orb_waves(rows: list[dict], rule: dict) -> list[dict]:
    actors = rule.get("actors") or {}
    normal_ids = set((actors.get("condensedVenomTracker") or {}).get("npcIDs") or [])
    mutation_ids = set((actors.get("virulentMutation") or {}).get("npcIDs") or [])
    all_ids = normal_ids | mutation_ids
    summons = []
    for row in rows:
        if _event_name(row) not in {"spell_summon", "summon"}:
            continue
        npc_id = safe_int(row.get("targetGameID"))
        if npc_id is None:
            npc_id = _guid_npc_id(row.get("destGUID"))
        if npc_id in all_ids:
            summons.append(row)
    waves = []
    for idx, group in enumerate(_group_times(summons, max_gap=3.0), start=1):
        # WCL can reuse targetInstance values for separate summon events inside
        # the same wave. The semantic event stream is already deduplicated, so
        # count summon *events* rather than unique (targetID, instance) pairs.
        normal_count = 0
        mutation_count = 0
        event_keys = []
        for pos, row in enumerate(group, start=1):
            npc_id = safe_int(row.get("targetGameID"))
            if npc_id is None:
                npc_id = _guid_npc_id(row.get("destGUID"))
            if npc_id in normal_ids:
                normal_count += 1
            if npc_id in mutation_ids:
                mutation_count += 1
            key = row.get("destGUID") or (row.get("targetID"), row.get("targetInstance"))
            event_keys.append(f"{npc_id}:{key}@{round(float(row.get('sec') or 0), 4)}#{pos}")
        waves.append({
            "index": idx,
            "sec": round(float(group[0].get("sec") or 0), 3),
            "lastSec": round(float(group[-1].get("sec") or 0), 3),
            "summonCount": len(group),
            "coalescedCount": normal_count,
            "mutationCount": mutation_count,
            "orbKeys": event_keys[:48],
            "evidenceLevel": EVIDENCE_VERIFIED,
            "npcIDs": sorted(all_ids),
        })
    return waves


def _coiled_valid_breath(row: dict, rule: dict) -> bool:
    spec = ((rule.get("mechanics") or {}).get("cleaveBreath") or {})
    cast_ids = {safe_int(x) for x in (spec.get("spellIDs") or []) if safe_int(x) is not None}
    aura_ids = {safe_int(x) for x in (spec.get("auraMarkerSpellIDs") or []) if safe_int(x) is not None}
    sid = safe_int(row.get("spellID") if row.get("spellID") is not None else row.get("abilityGameID"))
    if not _is_creature_source(row):
        return False
    boss_npcs = {safe_int(x) for x in (((rule.get("actors") or {}).get("boss") or {}).get("npcIDs") or []) if safe_int(x) is not None}
    if boss_npcs:
        source_id = safe_int(row.get("sourceGameID"))
        if source_id not in boss_npcs:
            return False
    if sid in cast_ids and _is_completed_cast(row):
        return True
    # WCL's hostile cast stream was missing in early probes, but the Sever aura
    # marker (1301690) is fight-scoped and lines up at the same timestamps in
    # all three reference mythic kills. It is therefore a verified fallback
    # marker, still guarded by the boss NPC source condition.
    if sid in aura_ids and _is_aura_apply(row):
        return True
    return False


def _coiled_breaths(rows: list[dict], rule: dict) -> list[dict]:
    candidates = [x for x in rows if _coiled_valid_breath(x, rule)]
    breaths = []
    for group in _group_times(candidates, max_gap=0.40):
        # Prefer a completed cast when both cast and aura marker are present.
        preferred = next((x for x in group if _is_completed_cast(x)), group[0])
        breaths.append(preferred)
    return breaths


def _coiled_player_deaths(rows: list[dict], *, end_sec: float | None = None) -> dict[tuple, list[dict]]:
    out: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        sec = float(row.get("sec") or 0)
        if end_sec is not None and sec >= float(end_sec):
            continue
        if _event_name(row) not in {"death", "unit_died", "unit_destroyed", "spell_instakill"}:
            continue
        if not _is_player_target(row):
            continue
        key = _target_key(row)
        if key is not None:
            out[key].append(row)
    return out


def _coiled_rupture_intervals(rows: list[dict], rupture_ids: set[int], *, end_sec: float | None = None) -> dict[tuple, list[tuple[float, float | None]]]:
    """Build inclusive Venom Rupture aura intervals for Glaive-overlap review.

    Aura removal and a final damage event can share the same timestamp.  The
    interval is therefore treated as inclusive at the removal boundary; this
    preserves the actual Try 24 case where the last Glaive tick and rupture
    removal are both logged at 108.416s.
    """
    active: dict[tuple, float] = {}
    intervals: dict[tuple, list[tuple[float, float | None]]] = defaultdict(list)
    for row in rows:
        sec = float(row.get("sec") or 0)
        if end_sec is not None and sec >= float(end_sec):
            break
        sid = safe_int(row.get("spellID") or row.get("abilityGameID"))
        if sid not in rupture_ids or not _is_player_target(row):
            continue
        key = _target_key(row)
        if key is None:
            continue
        if _is_aura_apply(row) or _event_name(row) in {"applydebuffstack", "spell_aura_applied_dose", "refreshdebuff", "spell_aura_refresh"}:
            active.setdefault(key, sec)
        elif _is_aura_remove(row):
            start = active.pop(key, None)
            if start is not None:
                intervals[key].append((start, sec))
    for key, start in active.items():
        intervals[key].append((start, None))
    return intervals


def _coiled_interval_active(intervals: dict[tuple, list[tuple[float, float | None]]], key: tuple | None, sec: float) -> bool:
    if key is None:
        return False
    for start, end in intervals.get(key, []):
        if float(start) <= sec and (end is None or sec <= float(end) + 0.01):
            return True
    return False


def _coiled_p1_execution(
    rows: list[dict], rule: dict, strategy: dict, *, phase2_entry_sec: float | None
) -> dict:
    """Verified P1 execution mistakes independent of orb-count strategy.

    B37.4.15 separates direct player execution evidence from raid orb-plan
    deviations.  P1 tank-breath non-tank hits are verified from 1299684 damage
    plus tank spec resolution.  Glaive is promoted only when a repeated
    1285017 hit sequence is directly followed by that player's death; ordinary
    non-fatal Glaive contacts remain observations.
    """
    end_sec = float(phase2_entry_sec) if phase2_entry_sec is not None else (max((float(x.get("sec") or 0) for x in rows), default=0.0) + 0.001)
    phase2_plan = (((strategy or {}).get("raidPlan") or {}).get("phase2") or {})
    tank_resolution = _coiled_tank_resolution(rows, phase2_plan.get("tankPlayers") or [])
    tank_players = list(tank_resolution.get("effective") or [])
    tank_keys = {_player_name_key(x) for x in tank_players if _player_name_key(x)}
    deaths = _coiled_player_deaths(rows, end_sec=end_sec)

    breath_spec = ((rule.get("mechanics") or {}).get("cleaveBreath") or {})
    breath_ids = {safe_int(x) for x in (breath_spec.get("spellIDs") or []) if safe_int(x) is not None}
    boss_ids = {safe_int(x) for x in ((((rule.get("actors") or {}).get("boss") or {}).get("npcIDs") or [])) if safe_int(x) is not None}
    breath_hits = []
    non_tank_hits = []
    for row in rows:
        sec = float(row.get("sec") or 0)
        if sec >= end_sec:
            continue
        sid = safe_int(row.get("spellID") or row.get("abilityGameID"))
        if sid not in breath_ids or not _is_damage(row) or not _is_player_target(row):
            continue
        if boss_ids and safe_int(row.get("sourceGameID")) not in boss_ids:
            continue
        player = _coiled_player_name(row)
        key = _target_key(row)
        death = next((d for d in deaths.get(key, []) if 0.0 <= float(d.get("sec") or 0) - sec <= 0.30), None)
        item = {
            "sec": round(sec, 3), "player": player,
            "amount": int(float(row.get("amount") or 0)),
            "spellID": sid,
            "linkedDeath": bool(death),
            "deathSec": round(float(death.get("sec") or 0), 3) if death is not None else None,
            "deathDelaySec": round(float(death.get("sec") or 0) - sec, 3) if death is not None else None,
            "evidenceLevel": EVIDENCE_VERIFIED,
            "supplementalResource": bool(row.get("supplementalResource")),
        }
        breath_hits.append(item)
        if tank_keys and _player_name_key(player) not in tank_keys:
            non_tank_hits.append(item)

    glaive_spec = ((rule.get("mechanics") or {}).get("axegrinderGlaive") or {})
    glaive_ids = {safe_int(x) for x in (glaive_spec.get("spellIDs") or [1285017]) if safe_int(x) is not None}
    glaive_source_ids = {safe_int(x) for x in (glaive_spec.get("sourceNpcIDs") or [260474]) if safe_int(x) is not None}
    rupture_ids = {safe_int(x) for x in (((rule.get("mechanics") or {}).get("toxicRupture") or {}).get("spellIDs") or [1299838]) if safe_int(x) is not None}
    rupture_intervals = _coiled_rupture_intervals(rows, rupture_ids, end_sec=end_sec)
    by_player: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        sec = float(row.get("sec") or 0)
        if sec >= end_sec:
            continue
        sid = safe_int(row.get("spellID") or row.get("abilityGameID"))
        if sid not in glaive_ids or not _is_damage(row) or not _is_player_target(row):
            continue
        if glaive_source_ids and safe_int(row.get("sourceGameID")) not in glaive_source_ids:
            continue
        key = _target_key(row)
        if key is None:
            continue
        by_player[key].append({
            "sec": sec,
            "player": _coiled_player_name(row),
            "amount": int(float(row.get("amount") or 0)),
            "spellID": sid,
            "ruptureActive": _coiled_interval_active(rupture_intervals, key, sec),
        })

    glaive_episodes = []
    for key, hits in by_player.items():
        hits = sorted(hits, key=lambda x: float(x.get("sec") or 0))
        groups = []
        for hit in hits:
            if not groups or float(hit["sec"]) - float(groups[-1][-1]["sec"]) > 1.20:
                groups.append([hit])
            else:
                groups[-1].append(hit)
        for group in groups:
            last_sec = float(group[-1]["sec"])
            death = next((d for d in deaths.get(key, []) if 0.0 <= float(d.get("sec") or 0) - last_sec <= 0.50), None)
            glaive_episodes.append({
                "startSec": round(float(group[0]["sec"]), 3),
                "lastHitSec": round(last_sec, 3),
                "player": group[0].get("player"),
                "hitCount": len(group),
                "totalAmount": sum(int(x.get("amount") or 0) for x in group),
                "ruptureOverlapHitCount": sum(1 for x in group if x.get("ruptureActive")),
                "ruptureOverlap": any(bool(x.get("ruptureActive")) for x in group),
                "linkedDeath": bool(death),
                "deathSec": round(float(death.get("sec") or 0), 3) if death is not None else None,
                "deathDelaySec": round(float(death.get("sec") or 0) - last_sec, 3) if death is not None else None,
                "evidenceLevel": EVIDENCE_VERIFIED,
            })

    failures = []
    for hit in non_tank_hits:
        summary = f"{hit.get('player') or '?'} · {int(hit.get('amount') or 0):,} 피해"
        if hit.get("linkedDeath"):
            summary += f" · {float(hit.get('deathDelaySec') or 0):.2f}초 뒤 사망"
        failures.append({
            "kind": "p1_tank_breath_non_tank_hit",
            "sec": hit.get("sec"),
            "label": "P1 탱 브레스 오피격",
            "summary": summary,
            "players": [hit.get("player")] if hit.get("player") else [],
            "linkedDeathCount": 1 if hit.get("linkedDeath") else 0,
            "linkedDeaths": ([{"player": hit.get("player"), "deathSec": hit.get("deathSec")} ] if hit.get("linkedDeath") else []),
            "evidenceLevel": EVIDENCE_VERIFIED,
            "status": "failed",
            "directSpellIDs": [hit.get("spellID") or 1299684],
        })
    for ep in glaive_episodes:
        if not ep.get("linkedDeath"):
            continue
        summary = f"{ep.get('player') or '?'} · {int(ep.get('hitCount') or 0)}연타 · {float(ep.get('deathDelaySec') or 0):.2f}초 뒤 사망"
        if ep.get("ruptureOverlap"):
            summary += f" · 맹독성 파열 중 {int(ep.get('ruptureOverlapHitCount') or 0)}타 겹침"
        failures.append({
            "kind": "p1_glaive_death",
            "sec": ep.get("lastHitSec"),
            "label": "P1 글레이브 피격 사망",
            "summary": summary,
            "players": [ep.get("player")] if ep.get("player") else [],
            "linkedDeathCount": 1,
            "linkedDeaths": [{"player": ep.get("player"), "deathSec": ep.get("deathSec")}],
            "ruptureOverlap": bool(ep.get("ruptureOverlap")),
            "evidenceLevel": EVIDENCE_VERIFIED,
            "status": "failed",
            "directSpellIDs": sorted(glaive_ids),
        })
    failures.sort(key=lambda x: float(x.get("sec") or 0))
    return {
        "tankBreath": {
            "tankPlayersConfigured": sorted(tank_resolution.get("configured") or []),
            "tankPlayersInferred": sorted(tank_resolution.get("inferred") or []),
            "tankPlayersEffective": sorted(tank_players),
            "tankResolutionSource": tank_resolution.get("source"),
            "hitCount": len(breath_hits),
            "nonTankHitCount": len(non_tank_hits),
            "nonTankHits": non_tank_hits,
            "linkedDeathCount": sum(1 for x in non_tank_hits if x.get("linkedDeath")),
            "evidenceLevel": EVIDENCE_VERIFIED if tank_players and breath_hits else EVIDENCE_UNAVAILABLE,
        },
        "glaive": {
            "episodeCount": len(glaive_episodes),
            "episodes": glaive_episodes,
            "linkedDeathCount": sum(1 for x in glaive_episodes if x.get("linkedDeath")),
            "ruptureOverlapDeathCount": sum(1 for x in glaive_episodes if x.get("linkedDeath") and x.get("ruptureOverlap")),
            "evidenceLevel": EVIDENCE_VERIFIED if glaive_episodes else EVIDENCE_UNAVAILABLE,
        },
        "failures": failures,
        "firstMistake": failures[0] if failures else None,
        "evidenceLevel": EVIDENCE_VERIFIED if failures else EVIDENCE_UNAVAILABLE,
        "note": "P1 구슬 개수 전략과 분리한 직접 실행 판정. 비탱 브레스 피격은 1299684 직접 피해, 글레이브는 1285017 연속 피격 직후 동일 플레이어 사망만 실패로 승격.",
    }


def _discovery_candidates(rows: list[dict], names: set[str]) -> list[dict]:
    # Diagnostic-only; never used for a rule verdict. Exposes candidate IDs so a
    # future probe can promote them to ID-based rule JSON.
    out = []
    for row in rows:
        if str(row.get("spellName") or "") in names:
            out.append(row)
    return out


def _candidate_condensed_pulses(rows: list[dict], breath_sec: float) -> list[dict]:
    candidates = [
        x for x in rows
        if str(x.get("spellName") or "") in _DISCOVERY_CONDENSED_VENOM_NAMES
        and _is_damage(x)
        and breath_sec - 0.10 <= float(x.get("sec") or 0) <= breath_sec + 4.0
    ]
    pulses = []
    # Discovery only. A pulse is simultaneous raid damage, not yet an orb count.
    for group in _group_times(candidates, max_gap=0.12):
        targets = []
        for row in group:
            key = _target_key(row)
            if key is not None and key not in targets:
                targets.append(key)
        sec = float(group[0].get("sec") or 0)
        ids = sorted({safe_int(x.get("spellID") or x.get("abilityGameID")) for x in group if safe_int(x.get("spellID") or x.get("abilityGameID")) is not None})
        pulses.append({
            "sec": round(sec, 3),
            "afterBreathSec": round(sec - breath_sec, 3),
            "eventCount": len(group),
            "targetCount": len(targets),
            "spellIDs": ids,
            "evidenceLevel": EVIDENCE_CANDIDATE,
        })
    return pulses


def _candidate_venom_rupture_signal(rows: list[dict], breath_sec: float, rule: dict) -> dict | None:
    spec = ((rule.get("mechanics") or {}).get("toxicRupture") or {})
    ids = {safe_int(x) for x in (spec.get("spellIDs") or []) if safe_int(x) is not None}
    hits = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids
        and _event_name(x) in {"applydebuff", "applydebuffstack", "refreshdebuff", "spell_aura_applied", "spell_aura_applied_dose", "spell_aura_refresh"}
        and breath_sec - 0.12 <= float(x.get("sec") or 0) <= breath_sec + 0.18
    ]
    if not hits:
        return None
    per_target = Counter(_target_key(x) for x in hits if _target_key(x) is not None)
    if not per_target:
        return None
    counts = sorted(int(v) for v in per_target.values())
    freq = Counter(counts)
    top_freq = max(freq.values())
    modes = sorted(k for k, v in freq.items() if v == top_freq)
    median = counts[len(counts)//2] if len(counts) % 2 else (counts[len(counts)//2 - 1] + counts[len(counts)//2]) / 2
    return {
        "eventCount": len(hits),
        "targetCount": len(per_target),
        "minPerTarget": min(counts),
        "medianPerTarget": median,
        "maxPerTarget": max(counts),
        "modePerTarget": modes,
        "spellIDs": sorted(ids),
        "evidenceLevel": EVIDENCE_CANDIDATE,
        "note": "브레스 직후 맹독성 파열 aura/stack 반복. 제거 구슬 수와의 1:1 대응은 아직 미검증.",
    }


def _verified_venom_rupture_orb_count(
    rows: list[dict],
    breath_sec: float,
    rule: dict,
    *,
    source_kind: str,
    strategy: dict | None = None,
) -> dict:
    """Return verified orb removals from Venom Rupture aura applications.

    B35.4 validation across the 37 supplied mythic wipes established that the
    number of 1299838 apply/apply-stack events per player in the first second
    after a verified Sever breath equals the number of orbs destroyed by that
    breath. Refresh events are deliberately excluded.

    A value is accepted only when every observed player has the same count.
    For complete WCL/raw streams, no qualifying aura event in the 1s window is
    a verified zero-orb breath. Synthetic/unknown streams do not infer zero
    from absence.
    """
    spec = ((rule.get("mechanics") or {}).get("toxicRupture") or {})
    ids = {safe_int(x) for x in (spec.get("spellIDs") or []) if safe_int(x) is not None}
    sig_cfg = (spec.get("orbCountSignal") or {}) if isinstance(spec, dict) else {}
    strategy_cfg = ((strategy or {}).get("orbAccounting") or {}) if isinstance(strategy, dict) else {}
    try:
        window_sec = float(sig_cfg.get("windowSec", strategy_cfg.get("countWindowSec", 1.0)))
    except (TypeError, ValueError):
        window_sec = 1.0
    try:
        min_targets = int(sig_cfg.get("minConsensusTargets", strategy_cfg.get("minConsensusTargets", 4)))
    except (TypeError, ValueError):
        min_targets = 4

    allowed = {
        "applydebuff", "applydebuffstack",
        "spell_aura_applied", "spell_aura_applied_dose",
    }
    hits = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids
        and _event_name(x) in allowed
        and breath_sec <= float(x.get("sec") or 0) <= breath_sec + window_sec
        and (_is_player_target(x) or (not x.get("targetType") and _target_key(x) is not None))
    ]
    per_target = Counter(_target_key(x) for x in hits if _target_key(x) is not None)
    counts = sorted(int(v) for v in per_target.values())

    complete_stream = str(source_kind or "").strip().lower() in {"wcl", "raw", "archive"}
    if not counts:
        if complete_stream:
            return {
                "removedOrbs": 0,
                "consistent": True,
                "eventCount": 0,
                "targetCount": 0,
                "minPerTarget": 0,
                "maxPerTarget": 0,
                "windowSec": window_sec,
                "spellIDs": sorted(ids),
                "countedEvents": sorted(allowed),
                "ignoredRefresh": True,
                "zeroByAbsence": True,
                "evidenceLevel": EVIDENCE_VERIFIED,
                "note": "브레스 후 1초 내 파열 적용/중첩 이벤트 0건 → 제거 구슬 0개. refresh는 제외.",
            }
        return {
            "removedOrbs": None,
            "consistent": False,
            "eventCount": 0,
            "targetCount": 0,
            "minPerTarget": None,
            "maxPerTarget": None,
            "windowSec": window_sec,
            "spellIDs": sorted(ids),
            "countedEvents": sorted(allowed),
            "ignoredRefresh": True,
            "zeroByAbsence": False,
            "evidenceLevel": EVIDENCE_UNAVAILABLE,
            "note": "완전한 WCL/raw 스트림이 아니므로 파열 이벤트 부재를 0개로 환산하지 않음.",
        }

    consistent = min(counts) == max(counts)
    enough_targets = len(per_target) >= min_targets
    verified = consistent and enough_targets
    removed = counts[0] if verified else None
    return {
        "removedOrbs": removed,
        "consistent": bool(consistent),
        "consensusEnoughTargets": bool(enough_targets),
        "eventCount": len(hits),
        "targetCount": len(per_target),
        "minPerTarget": min(counts),
        "maxPerTarget": max(counts),
        "windowSec": window_sec,
        "spellIDs": sorted(ids),
        "countedEvents": sorted(allowed),
        "ignoredRefresh": True,
        "zeroByAbsence": False,
        "evidenceLevel": EVIDENCE_VERIFIED if verified else EVIDENCE_UNAVAILABLE,
        "note": (
            f"플레이어 {len(per_target)}명 모두 {removed}중첩 → 제거 구슬 {removed}개. refresh 제외."
            if verified else
            f"플레이어별 파열 적용/중첩 수 불일치 또는 표본 부족: min={min(counts)}, max={max(counts)}, targets={len(per_target)}. 제거 수 확인 불가."
        ),
    }


def _coiled_orb_accounting(
    observations: list[dict],
    orb_waves: list[dict],
    guillotine_markers: list[dict],
    strategy: dict,
    *,
    collapse_snapshot: dict | None = None,
    survival_context: dict | None = None,
) -> dict:
    """Build a verified running pool of active orbs.

    Spawn totals and verified breath removals remain in the raw accounting even
    after a wipe has already begun, so the timeline stays truthful.  However,
    mechanics at/after the first significant 4-deaths-in-1s collapse cluster,
    or while fewer than half of a normal-sized raid remains alive, are marked
    descriptive-only and excluded from strategy/risk aggregates.
    """
    cfg = (strategy or {}).get("orbAccounting") or {}
    try:
        high_burst_min = int(cfg.get("highBurstMin", 7))
    except (TypeError, ValueError):
        high_burst_min = 7

    events = []
    for wave in orb_waves:
        events.append((float(wave.get("sec") or 0), 0, "spawn", wave))
    for row in observations:
        events.append((float(row.get("sec") or 0), 1, "breath", row))
    for marker in guillotine_markers:
        events.append((float(marker.get("sec") or 0), 2, "guillotine", marker))
    events.sort(key=lambda x: (x[0], x[1]))

    active = 0
    known = bool(orb_waves)
    ledger = []
    wave_states = []
    guillotine_states = []
    mismatch_count = 0
    flags = []
    raw_flags = []
    excluded_events = []
    after_second_guillotine = False

    def add_raw_flag(code: str, sec: float, **extra):
        item = {"code": code, "sec": round(sec, 3), **extra}
        raw_flags.append(item)
        return item

    def add_flag(code: str, sec: float, **extra):
        item = {"code": code, "sec": round(sec, 3), **extra}
        flags.append(item)
        return item

    for sec, _priority, kind, item in events:
        guard = (
            item.get("judgmentGuard")
            if kind == "breath" and isinstance(item.get("judgmentGuard"), dict)
            else _coiled_judgment_guard(sec, collapse_snapshot, survival_context, strategy)
        )
        excluded = bool((guard or {}).get("judgmentExcluded"))

        if kind == "spawn":
            before = active if known else None
            added = int(item.get("summonCount") or 0)
            carry_in = before if before is not None else None
            if known:
                active += added
                after = active
            else:
                after = None
            state = {
                "kind": "spawn",
                "sec": round(sec, 3),
                "waveIndex": item.get("index"),
                "activeBefore": before,
                "carryInOrbs": carry_in,
                "spawnedOrbs": added,
                "coalescedCount": item.get("coalescedCount"),
                "mutationCount": item.get("mutationCount"),
                "activeAfter": after,
                "judgmentExcluded": excluded,
                "judgmentGuard": guard,
                "evidenceLevel": EVIDENCE_VERIFIED if known else EVIDENCE_UNAVAILABLE,
            }
            if carry_in is not None and carry_in > 0:
                if after_second_guillotine:
                    # B36.5 schema 13: the first post-G2 residual is not an
                    # ordinary failed carryover. It is the raidPlan orb-handoff
                    # / P2 pre-cleanup amount. Preserve the raw
                    # number but keep it out of wave_carryover failure totals.
                    state["carryover"] = False
                    state["carryoverKind"] = "tank_handoff"
                    state["tankHandoffCarryInOrbs"] = carry_in
                    add_raw_flag("tank_handoff_carry_in", sec, waveIndex=item.get("index"), remainingOrbs=carry_in, judgmentExcluded=excluded)
                    if not excluded:
                        add_flag("tank_handoff_carry_in", sec, waveIndex=item.get("index"), remainingOrbs=carry_in)
                else:
                    state["carryover"] = True
                    state["carryoverKind"] = "wave_carryover"
                    add_raw_flag("wave_carryover", sec, waveIndex=item.get("index"), remainingOrbs=carry_in, judgmentExcluded=excluded)
                    if not excluded:
                        add_flag("wave_carryover", sec, waveIndex=item.get("index"), remainingOrbs=carry_in)
            else:
                state["carryover"] = False
                state["carryoverKind"] = None
            if excluded:
                excluded_events.append({"kind": "spawn", "sec": round(sec, 3), "reason": guard.get("judgmentExclusionLabel")})
            wave_states.append(state)
            ledger.append(state)
            continue

        if kind == "breath":
            before = active if known else None
            removed = item.get("destroyedOrbs")
            strategy_judge = bool(item.get("strategyJudgmentEnabled", item.get("strategyApplies")))
            even_before_guillotine = ((item.get("strategyAssignment") or {}).get("positionBeforeGuillotine") == 2)
            after = None
            arithmetic_mismatch = False
            if removed is None:
                known = False
            elif known:
                candidate_after = int(active) - int(removed)
                if candidate_after < 0:
                    arithmetic_mismatch = True
                    mismatch_count += 1
                    known = False
                else:
                    active = candidate_after
                    after = active
            item["activeOrbsBefore"] = before
            item["activeOrbsAfter"] = after
            item["orbArithmeticMismatch"] = arithmetic_mismatch
            item["judgmentExcluded"] = excluded
            item["judgmentEligible"] = not excluded
            item["judgmentGuard"] = guard
            item["judgmentExclusionLabel"] = guard.get("judgmentExclusionLabel")

            raw_row_flags = []
            if removed is not None:
                if int(removed) == 0:
                    raw_row_flags.append("zero_breath")
                    add_raw_flag("zero_breath", sec, breathIndex=item.get("index"), removedOrbs=0, judgmentExcluded=excluded)
                    # B37.4.15 raid plan: only the even breath immediately before
                    # Guillotine is a promoted stack-risk check. Odd/unassigned
                    # breaths preserve the raw fact without becoming a failure.
                    if strategy_judge and even_before_guillotine and not excluded:
                        add_flag("zero_breath", sec, breathIndex=item.get("index"), removedOrbs=0)
                if int(removed) >= high_burst_min:
                    raw_row_flags.append("high_burst")
                    add_raw_flag("high_burst", sec, breathIndex=item.get("index"), removedOrbs=int(removed), judgmentExcluded=excluded)
                    if strategy_judge and even_before_guillotine and not excluded:
                        add_flag("high_burst", sec, breathIndex=item.get("index"), removedOrbs=int(removed))
                if item.get("strategyApplies") and item.get("goalMin") is not None and int(removed) < int(item.get("goalMin")):
                    if ((item.get("strategyAssignment") or {}).get("positionBeforeGuillotine") == 1):
                        raw_row_flags.append("first_breath_short")
                        add_raw_flag(
                            "first_breath_short", sec, breathIndex=item.get("index"),
                            removedOrbs=int(removed), goalMin=int(item.get("goalMin")),
                            strategySlot=item.get("strategySlot"), judgmentExcluded=excluded,
                        )
                        if strategy_judge and not excluded:
                            add_flag(
                                "first_breath_short", sec, breathIndex=item.get("index"),
                                removedOrbs=int(removed), goalMin=int(item.get("goalMin")),
                                strategySlot=item.get("strategySlot"),
                            )
            if arithmetic_mismatch:
                raw_row_flags.append("orb_arithmetic_mismatch")
                add_raw_flag("orb_arithmetic_mismatch", sec, breathIndex=item.get("index"), removedOrbs=removed, activeBefore=before, judgmentExcluded=excluded)
                if not excluded:
                    add_flag("orb_arithmetic_mismatch", sec, breathIndex=item.get("index"), removedOrbs=removed, activeBefore=before)

            item["rawRiskFlags"] = raw_row_flags
            promoted_row_codes = {x.get("code") for x in flags if round(float(x.get("sec") or -999), 3) == round(sec, 3)}
            item["riskFlags"] = [] if excluded else [x for x in raw_row_flags if x in promoted_row_codes]
            if item.get("strategyApplies"):
                raw_status = item.get("rawStatus") or item.get("status") or "unknown"
                item["rawStatus"] = raw_status
                if excluded:
                    item["status"] = "excluded"
            state = {
                "kind": "breath",
                "sec": round(sec, 3),
                "breathIndex": item.get("index"),
                "strategySlot": item.get("strategySlot"),
                "activeBefore": before,
                "removedOrbs": removed,
                "activeAfter": after,
                "riskFlags": item.get("riskFlags") or [],
                "rawRiskFlags": raw_row_flags,
                "status": item.get("status"),
                "rawStatus": item.get("rawStatus"),
                "judgmentExcluded": excluded,
                "judgmentGuard": guard,
                "evidenceLevel": (item.get("orbRemovalSignal") or {}).get("evidenceLevel", EVIDENCE_UNAVAILABLE),
            }
            if excluded:
                excluded_events.append({"kind": "breath", "sec": round(sec, 3), "breathIndex": item.get("index"), "reason": guard.get("judgmentExclusionLabel")})
            ledger.append(state)
            continue

        before = active if known else None
        guillotine_index = safe_int(item.get("index"))
        if guillotine_index == 2:
            after_second_guillotine = True
        raw_status = "unknown"
        if before is not None:
            if before == 0:
                raw_status = "success"
            else:
                # B37.4.18: a residual orb count is verified pressure context,
                # not automatic proof of the wipe mistake.  Keep it as warning
                # until a separate direct death/collapse contract can link it.
                raw_status = "warning"
        status = "excluded" if excluded else raw_status
        state = {
            "kind": "guillotine",
            "sec": round(sec, 3),
            "guillotineIndex": item.get("index"),
            "remainingOrbs": before,
            "status": status,
            "rawStatus": raw_status,
            "judgmentExcluded": excluded,
            "judgmentGuard": guard,
            "evidenceLevel": EVIDENCE_VERIFIED if before is not None else EVIDENCE_UNAVAILABLE,
        }
        if before is not None and before > 0:
            add_raw_flag("guillotine_remaining", sec, guillotineIndex=item.get("index"), remainingOrbs=before, judgmentExcluded=excluded)
            if not excluded:
                add_flag("guillotine_remaining_observed", sec, guillotineIndex=item.get("index"), remainingOrbs=before)
                if guillotine_index == 2:
                    add_flag("guillotine2_remaining_observed", sec, guillotineIndex=item.get("index"), remainingOrbs=before)
        if excluded:
            excluded_events.append({"kind": "guillotine", "sec": round(sec, 3), "guillotineIndex": item.get("index"), "reason": guard.get("judgmentExclusionLabel")})
        guillotine_states.append(state)
        ledger.append(state)

    # B37.4.16: odd breaths remain observation-only, but preserve them as
    # non-causal context for a later *even* breath / Guillotine failure.  This
    # lets the raid lead see why load may have shifted into the dangerous even
    # breath without promoting the earlier odd breath to firstMistake.
    odd_context_by_guillotine: dict[int, dict] = {}
    for ob in observations:
        assignment = ob.get("strategyAssignment") or {}
        gi = safe_int(assignment.get("guillotineIndex"))
        pos = safe_int(assignment.get("positionBeforeGuillotine"))
        if gi and pos == 1:
            odd_context_by_guillotine[int(gi)] = {
                "sec": ob.get("sec"),
                "breathIndex": ob.get("index"),
                "strategySlot": ob.get("strategySlot"),
                "removedOrbs": ob.get("destroyedOrbs"),
                "goalMin": ob.get("goalMin"),
                "goalMax": ob.get("goalMax"),
                "status": "context_only",
                "evidenceLevel": (ob.get("orbRemovalSignal") or {}).get("evidenceLevel", EVIDENCE_UNAVAILABLE),
                "note": "직전 홀수 브레스 처리량. 실패로 승격하지 않고 뒤 짝수 브레스/단두대 실패의 선행 맥락으로만 사용.",
            }

    for ob in observations:
        assignment = ob.get("strategyAssignment") or {}
        gi = safe_int(assignment.get("guillotineIndex"))
        pos = safe_int(assignment.get("positionBeforeGuillotine"))
        if gi and pos == 2 and int(gi) in odd_context_by_guillotine:
            ob["precedingOddContext"] = dict(odd_context_by_guillotine[int(gi)])

    for gs in guillotine_states:
        gi = safe_int(gs.get("guillotineIndex"))
        if gi and int(gi) in odd_context_by_guillotine:
            gs["precedingOddContext"] = dict(odd_context_by_guillotine[int(gi)])

    strategic = [x for x in observations if x.get("strategyApplies") and x.get("strategyJudgmentEnabled", True) and not x.get("judgmentExcluded")]
    eligible_guillotines = [x for x in guillotine_states if not x.get("judgmentExcluded")]
    required_guillotines = [x for x in eligible_guillotines if safe_int(x.get("guillotineIndex")) != 2]
    direct_fail = any(x.get("status") == "failed" for x in strategic)
    guill_fail = any(x.get("status") == "failed" for x in required_guillotines)
    warning_seen = any(x.get("status") == "warning" for x in strategic) or any(x.get("status") == "warning" for x in eligible_guillotines)
    known_strategic = [x for x in strategic if x.get("destroyedOrbs") is not None]
    if direct_fail or guill_fail:
        overall_status = "failed"
    elif warning_seen:
        overall_status = "warning"
    elif required_guillotines and all(x.get("status") == "success" for x in required_guillotines) and strategic and len(known_strategic) == len(strategic):
        overall_status = "success"
    else:
        overall_status = "unknown"

    by_code = Counter(x.get("code") for x in flags)
    raw_by_code = Counter(x.get("code") for x in raw_flags)
    return {
        "ledger": ledger,
        "waveStates": wave_states,
        "guillotineStates": guillotine_states,
        "currentActiveOrbs": active if known else None,
        "poolKnown": bool(known),
        "mismatchCount": mismatch_count,
        "highBurstMin": high_burst_min,
        "flags": flags,
        "flagCounts": dict(by_code),
        "rawFlags": raw_flags,
        "rawFlagCounts": dict(raw_by_code),
        "excludedEvents": excluded_events,
        "excludedEventCount": len(excluded_events),
        "excludedBreathCount": sum(1 for x in excluded_events if x.get("kind") == "breath"),
        "overallStatus": overall_status,
        "oddBreathContextByGuillotine": {str(k): dict(v) for k, v in sorted(odd_context_by_guillotine.items())},
        "evidenceLevel": EVIDENCE_VERIFIED if known else EVIDENCE_UNAVAILABLE,
        "note": "active_orbs 원시 계산은 끝까지 유지하되, 2차 단두대 뒤 잔존은 호환 필드 tank_handoff로 분리하고 일반 wave_carryover 실패에서 제외합니다. 실제 처리 담당자는 별도 근거 없이는 단정하지 않습니다. 의미 있는 붕괴 이후 또는 생존 인원 절반 미만의 기믹은 전략/원인 집계에서 제외합니다. 홀수 브레스 처리량은 실패로 승격하지 않고 뒤 짝수 브레스/단두대 실패의 선행 맥락으로만 보존합니다.",
    }



def _coiled_orb_failure_events(observations: list[dict], orb_accounting: dict) -> list[dict]:
    """Verified P1 orb failures eligible for chronological firstMistake.

    Odd breaths are observation-only by strategy contract and therefore never
    appear here.  Even breaths become failures only at the verified high-burst
    threshold (7+ with the current plan).  Guillotine residuals stay warning
    observations until a separate direct death/collapse contract links them.
    Preceding odd-breath load is attached as context only.
    """
    failures: list[dict] = []
    for ob in observations or []:
        if ob.get("judgmentExcluded"):
            continue
        if not ob.get("strategyApplies") or not ob.get("strategyJudgmentEnabled", True):
            continue
        if str(ob.get("status") or "") != "failed" or ob.get("sec") is None:
            continue
        removed = ob.get("destroyedOrbs")
        goal_min, goal_max = ob.get("goalMin"), ob.get("goalMax")
        summary = f"{int(removed)}개" if removed is not None else "처리량 확인"
        if goal_min is not None and goal_max is not None:
            summary += f" · 목표 {int(goal_min)}~{int(goal_max)}"
        ctx = ob.get("precedingOddContext") or {}
        if ctx.get("removedOrbs") is not None:
            summary += f" · 선행 홀수 {int(ctx.get('removedOrbs'))}개"
            if ctx.get("goalMin") is not None and ctx.get("goalMax") is not None:
                summary += f"(계획 {int(ctx.get('goalMin'))}~{int(ctx.get('goalMax'))})"
            summary += " · 선행은 실패 판정 아님"
        failures.append({
            "kind": "p1_orb_failure",
            "subkind": "even_breath_out_of_range",
            "sec": round(float(ob.get("sec")), 3),
            "label": "P1 구슬 처리 실패",
            "summary": summary,
            "evidenceLevel": EVIDENCE_VERIFIED,
            "status": "failed",
            "directSpellIDs": [safe_int(ob.get("spellID"))] if safe_int(ob.get("spellID")) is not None else [],
            "breathIndex": ob.get("index"),
            "strategySlot": ob.get("strategySlot"),
            "precedingOddContext": dict(ctx) if ctx else None,
        })
    for gs in (orb_accounting or {}).get("guillotineStates") or []:
        if gs.get("judgmentExcluded") or str(gs.get("status") or "") != "failed" or gs.get("sec") is None:
            continue
        gi = safe_int(gs.get("guillotineIndex"))
        if gi == 2:  # second Guillotine residual is observation/warning by contract
            continue
        remain = gs.get("remainingOrbs")
        summary = f"{gi or '?'}차 단두대 직전 잔존 {remain if remain is not None else '?'}개"
        ctx = gs.get("precedingOddContext") or {}
        if ctx.get("removedOrbs") is not None:
            summary += f" · 선행 홀수 {int(ctx.get('removedOrbs'))}개"
            if ctx.get("goalMin") is not None and ctx.get("goalMax") is not None:
                summary += f"(계획 {int(ctx.get('goalMin'))}~{int(ctx.get('goalMax'))})"
            summary += " · 선행은 실패 판정 아님"
        failures.append({
            "kind": "p1_orb_failure",
            "subkind": "guillotine_residual",
            "sec": round(float(gs.get("sec")), 3),
            "label": "P1 구슬 처리 실패",
            "summary": summary,
            "evidenceLevel": EVIDENCE_VERIFIED,
            "status": "failed",
            "directSpellIDs": [],
            "guillotineIndex": gi,
            "precedingOddContext": dict(ctx) if ctx else None,
        })
    failures.sort(key=lambda x: float(x.get("sec") or 0))
    return failures

def _coiled_orb_causal_chain(
    observations: list[dict],
    orb_accounting: dict,
    collapse_snapshot: dict | None,
    rule: dict,
) -> dict | None:
    """Link verified orb handling to a rupture-led collapse as a candidate chain.

    Orb counts are verified; causality to deaths remains candidate until the B31
    downstream chain explicitly links it.
    """
    if not collapse_snapshot:
        return None
    collapse_sec = float(collapse_snapshot.get("collapseSec") or 0)
    rupture_ids = {safe_int(x) for x in ((((rule.get("mechanics") or {}).get("toxicRupture") or {}).get("spellIDs") or [])) if safe_int(x) is not None}
    pressure = collapse_snapshot.get("raidWidePressureCandidate") or {}
    if safe_int(pressure.get("spellID")) not in rupture_ids:
        return None
    high_min = int((orb_accounting or {}).get("highBurstMin") or 7)
    eligible = [
        x for x in observations
        if not x.get("judgmentExcluded")
        and x.get("destroyedOrbs") is not None
        and int(x.get("destroyedOrbs") or 0) >= high_min
        and 0 <= collapse_sec - float(x.get("sec") or 0) <= 5.0
    ]
    if not eligible:
        return None
    breath = eligible[-1]
    breath_sec = float(breath.get("sec") or 0)
    prior_zero = [x for x in observations if not x.get("judgmentExcluded") and float(x.get("sec") or 0) < breath_sec and x.get("destroyedOrbs") == 0]
    carry = [
        x for x in (orb_accounting.get("waveStates") or [])
        if not x.get("judgmentExcluded") and float(x.get("sec") or 0) < breath_sec and (x.get("carryInOrbs") or 0) > 0
    ]
    steps = []
    if prior_zero:
        z = prior_zero[-1]
        steps.append({
            "kind": "zero_breath", "sec": z.get("sec"), "removedOrbs": 0,
            "evidenceLevel": EVIDENCE_VERIFIED,
            "text": f"{_fmt_time(z.get('sec'))} 브레스 구슬 0개 처리",
        })
    if carry:
        c = carry[-1]
        steps.append({
            "kind": "carryover", "sec": c.get("sec"), "remainingOrbs": c.get("carryInOrbs"),
            "evidenceLevel": EVIDENCE_VERIFIED,
            "text": f"{_fmt_time(c.get('sec'))} 새 웨이브 시작 시 이전 구슬 {c.get('carryInOrbs')}개 잔존",
        })
    steps.append({
        "kind": "high_burst", "sec": breath.get("sec"), "removedOrbs": breath.get("destroyedOrbs"),
        "evidenceLevel": EVIDENCE_VERIFIED,
        "text": f"{_fmt_time(breath.get('sec'))} 브레스에서 {breath.get('destroyedOrbs')}개 동시 제거",
    })
    steps.append({
        "kind": "rupture_collapse", "sec": collapse_snapshot.get("collapseSec"),
        "spellID": pressure.get("spellID"), "targetCount": pressure.get("targetCount"),
        "evidenceLevel": EVIDENCE_CANDIDATE,
        "text": f"{_fmt_time(collapse_snapshot.get('collapseSec'))} 맹독성 파열 공대 압박 뒤 붕괴",
    })
    return {
        "status": "candidate",
        "evidenceLevel": EVIDENCE_CANDIDATE,
        "breathSec": breath.get("sec"),
        "removedOrbs": breath.get("destroyedOrbs"),
        "collapseSec": collapse_snapshot.get("collapseSec"),
        "steps": steps,
        "summary": " → ".join(str(x.get("text") or "") for x in steps),
        "note": "구슬 제거/잔존 수는 검증됨. 해당 고중첩이 실제 전멸 원인이라는 인과 연결은 B31 downstream 확인 전까지 후보.",
    }


def _candidate_venom_rupture_bursts(rows: list[dict], breath_sec: float, rule: dict) -> list[dict]:
    """Candidate raid-wide Venom Rupture moments after a breath.

    These bursts are diagnostic only. They are used to test whether the total
    number of rupture moments matches total spawned orbs (normal + mutation),
    or whether mutation orbs contribute extra moments. No orb count is inferred
    from this signal until reference logs validate the relationship.
    """
    spec = ((rule.get("mechanics") or {}).get("toxicRupture") or {})
    ids = {safe_int(x) for x in (spec.get("spellIDs") or []) if safe_int(x) is not None}
    hits = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids
        and _event_name(x) in {"applydebuff", "applydebuffstack", "refreshdebuff", "spell_aura_applied", "spell_aura_applied_dose", "spell_aura_refresh"}
        and breath_sec - 0.12 <= float(x.get("sec") or 0) <= breath_sec + 4.0
    ]
    bursts = []
    for group in _group_times(hits, max_gap=0.06):
        targets = {_target_key(x) for x in group if _target_key(x) is not None}
        if len(targets) < 4:
            continue
        sec = float(group[0].get("sec") or 0)
        raw_stacks = []
        for row in group:
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
            value = raw.get("stack") if raw else None
            try:
                if value is not None:
                    raw_stacks.append(int(value))
            except Exception:
                pass
        bursts.append({
            "sec": round(sec, 3),
            "afterBreathSec": round(sec - breath_sec, 3),
            "eventCount": len(group),
            "targetCount": len(targets),
            "stackValues": sorted(set(raw_stacks))[:16],
            "spellIDs": sorted(ids),
            "evidenceLevel": EVIDENCE_CANDIDATE,
        })
    return bursts



def _damage_amount(row: dict) -> float:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    for key in ("amount", "unmitigatedAmount"):
        value = row.get(key)
        if value is None:
            value = raw.get(key)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            pass
    return 0.0



def _coiled_health_resource(row: dict, *, actor_game_id: int) -> dict | None:
    """Return a normalized HP snapshot for one actor from raw/WCL resources.

    Local advanced-combat rows expose targetCurrentHP/targetMaxHP.  B36 WCL
    probes also normalize sourceResources/targetResources when includeResources
    is enabled on the targeted boss stream.
    """
    candidates = []
    if safe_int(row.get("targetGameID")) == int(actor_game_id):
        candidates.append((row.get("targetCurrentHP"), row.get("targetMaxHP"), row.get("targetHealthPct"), "target"))
    if safe_int(row.get("sourceGameID")) == int(actor_game_id):
        candidates.append((row.get("sourceCurrentHP"), row.get("sourceMaxHP"), row.get("sourceHealthPct"), "source"))
    for cur, max_hp, pct, side in candidates:
        try:
            cur_i = int(cur) if cur is not None else None
            max_i = int(max_hp) if max_hp is not None else None
            pct_f = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            continue
        if pct_f is None and cur_i is not None and max_i and max_i > 0:
            pct_f = 100.0 * float(cur_i) / float(max_i)
        if pct_f is None:
            continue
        return {
            "sec": round(float(row.get("sec") or 0), 3),
            "currentHP": cur_i,
            "maxHP": max_i,
            "healthPct": round(pct_f, 3),
            "side": side,
            "evidenceLevel": EVIDENCE_VERIFIED,
            "supplementalResource": bool(row.get("supplementalResource")),
        }
    return None


def _coiled_boss_health_snapshot(rows: list[dict], checkpoint_sec: float, *, boss_npc_id: int = 257911, max_distance_sec: float = 2.0) -> dict:
    snaps = []
    for row in rows:
        snap = _coiled_health_resource(row, actor_game_id=boss_npc_id)
        if snap is not None:
            snaps.append(snap)
    if not snaps:
        return {
            "checkpointSec": round(float(checkpoint_sec), 3),
            "available": False,
            "evidenceLevel": EVIDENCE_UNAVAILABLE,
            "note": "HP resource snapshot 없음. 로컬 고급 전투 기록 또는 B36 WCL includeResources 스트림이 필요합니다.",
        }
    nearest = min(snaps, key=lambda x: abs(float(x.get("sec") or 0) - float(checkpoint_sec)))
    distance = abs(float(nearest.get("sec") or 0) - float(checkpoint_sec))
    if distance > float(max_distance_sec):
        return {
            "checkpointSec": round(float(checkpoint_sec), 3),
            "available": False,
            "nearestSnapshotSec": nearest.get("sec"),
            "nearestDistanceSec": round(distance, 3),
            "evidenceLevel": EVIDENCE_UNAVAILABLE,
            "note": f"체크포인트 ±{max_distance_sec:.1f}s 안에 HP snapshot이 없습니다.",
        }
    return {
        "checkpointSec": round(float(checkpoint_sec), 3),
        "available": True,
        **nearest,
        "distanceSec": round(distance, 3),
    }


def _coiled_phase1_transition(rows: list[dict], rule: dict, strategy: dict) -> dict:
    cfg = (strategy or {}).get("phase1Transition") or {}
    target_min = float(cfg.get("targetMinSec", 165.0))
    target_max = float(cfg.get("targetMaxSec", 170.0))
    # B37.4.33: raid-plan clock targets are human-readable tenths/whole seconds.
    # Treat sub-tenth capture jitter at either boundary as inside the target
    # instead of rendering e.g. 170.040s as "2:50.0" while classifying it late.
    boundary_tolerance = max(0.0, min(0.5, float(cfg.get("boundaryToleranceSec", 0.1))))
    checkpoint = float(cfg.get("healthCheckpointSec", target_min))
    hp_checkpoint = _coiled_boss_health_snapshot(rows, checkpoint, boss_npc_id=257911)

    hp_snaps = []
    for row in rows:
        snap = _coiled_health_resource(row, actor_game_id=257911)
        if snap is not None:
            hp_snaps.append(snap)
    native_zero = next((x for x in sorted(hp_snaps, key=lambda x: float(x.get("sec") or 0)) if float(x.get("healthPct") or 0) <= 0.1 and not x.get("supplementalResource")), None)
    zero = native_zero or next((x for x in sorted(hp_snaps, key=lambda x: float(x.get("sec") or 0)) if float(x.get("healthPct") or 0) <= 0.1), None)

    activation_spec = ((rule.get("mechanics") or {}).get("phase2Activation") or {})
    activation_ids = {safe_int(x) for x in (activation_spec.get("spellIDs") or []) if safe_int(x) is not None}
    activation_sources = {safe_int(x) for x in (activation_spec.get("sourceNpcIDs") or [259854]) if safe_int(x) is not None}
    activation_rows = [
        x for x in _fast_rows_spell(rows, activation_ids)
        if safe_int(x.get("sourceGameID")) in activation_sources
        and _is_completed_cast(x)
    ]
    activation_sec = round(float(activation_rows[0].get("sec") or 0), 3) if activation_rows else None

    if zero is not None:
        entry_sec = float(zero.get("sec") or 0)
        if entry_sec < target_min - boundary_tolerance:
            status, label = "early", "목표보다 빠른 P2 진입"
        elif entry_sec <= target_max + boundary_tolerance:
            status, label = "success", "P2 진입 목표 구간"
        else:
            status, label = "late", "목표보다 늦은 P2 진입"
        evidence = EVIDENCE_VERIFIED
        basis = "zuljan_hp_zero"
    else:
        entry_sec = None
        status, label = "unknown", "P2 진입 시각 확인 불가"
        evidence = EVIDENCE_UNAVAILABLE
        basis = "health_resource_unavailable"

    return {
        "targetMinSec": target_min,
        "targetMaxSec": target_max,
        "targetBoundaryToleranceSec": boundary_tolerance,
        "entrySec": round(entry_sec, 3) if entry_sec is not None else None,
        "status": status,
        "label": label,
        "basis": basis,
        "healthAtCheckpoint": hp_checkpoint,
        "malacrassActivationSec": activation_sec,
        "malacrassActivationEvidenceLevel": EVIDENCE_VERIFIED if activation_sec is not None else EVIDENCE_UNAVAILABLE,
        "evidenceLevel": evidence,
        "note": "P1→P2 실제 진입은 줄잔 HP 0 snapshot으로 판정. 말라크라스 Dreadful Presence는 P2 분석 시작 앵커이며 진입 시각 자체로 등치하지 않습니다.",
    }


def _coiled_phase2_end_sec(rows: list[dict], rule: dict) -> float | None:
    spec = ((rule.get("mechanics") or {}).get("soulbinding") or {})
    ids = {safe_int(x) for x in (spec.get("spellIDs") or []) if safe_int(x) is not None}
    sources = {safe_int(x) for x in (spec.get("sourceNpcIDs") or [259854]) if safe_int(x) is not None}
    hits = [x for x in _fast_rows_spell(rows, ids) if safe_int(x.get("sourceGameID")) in sources and _is_completed_cast(x)]
    return round(float(hits[0].get("sec") or 0), 3) if hits else None


# B36.5 packaged catalog.  These are only *recognizers*; whether they are
# required for a shield window is controlled by raidPlan.phase2.shieldWindows.
# This keeps the default package descriptive until the raid leader explicitly
# assigns a cooldown requirement.
_BLOODLUST_FAMILY_IDS = {
    2825,      # Bloodlust
    32182,     # Heroism
    80353,     # Time Warp
    90355,     # Ancient Hysteria
    160452,    # Netherwinds
    178207,    # Drums / older variants seen in logs
    264667,
    390386,    # Fury of the Aspects
}
_COMBAT_POTION_IDS = {
    1236616,
    1236994,
}


def _row_spell_id(row: dict) -> int | None:
    return safe_int(row.get("spellID") or row.get("abilityGameID"))


# V5.2.4: 똬리 분석은 같은 rows(트라이 하나 약 47만 줄)를 주문·이벤트별로 수십 번 다시 훑었다.
# 분석하는 동안만 주문 ID·이벤트 이름 → 줄 번호 색인을 만들어 두고 그 줄만 본다.
# 원래 순서 그대로 돌려주므로 결과는 같다. 다른 목록(잘라낸 rows 등)이 오면 예전처럼 전부 훑는다.
_FAST_ROWS = threading.local()


def _fast_begin(rows: list) -> None:
    _FAST_ROWS.state = {"rows": rows, "n": len(rows), "spell": None, "event": None, "roster": None,
                        "secs": None, "tanks": {}}


def _fast_end() -> None:
    _FAST_ROWS.state = None


def _fast_state(rows):
    st = getattr(_FAST_ROWS, "state", None)
    if st is None or st["rows"] is not rows or st["n"] != len(rows):
        return None
    return st


def _fast_positions(rows, st: dict, kind: str, keys) -> list:
    index = st[kind]
    if index is None:
        index = defaultdict(list)
        if kind == "spell":
            for i, x in enumerate(rows):
                index[safe_int(x.get("spellID") or x.get("abilityGameID"))].append(i)
        else:
            for i, x in enumerate(rows):
                index[str(x.get("event") or "").strip().lower()].append(i)
        st[kind] = index
    pos = []
    for k in set(keys):
        pos.extend(index.get(k, ()))
    pos.sort()
    return pos


def _fast_rows_spell(rows, ids) -> list:
    """[x for x in rows if _row_spell_id(x) in ids] 와 같은 결과 (같은 순서)."""
    st = _fast_state(rows)
    if st is None:
        return [x for x in rows if _row_spell_id(x) in ids]
    return [rows[i] for i in _fast_positions(rows, st, "spell", ids)]


def _fast_rows_between(rows, lo: float, hi: float, *, hi_open: bool = False) -> list:
    """rows 가운데 lo <= sec <= hi (hi_open 이면 < hi) 인 줄, 원래 순서. 색인이 없거나 rows 가 시각순이 아니면 전부 훑는다."""
    st = _fast_state(rows)
    secs = None
    if st is not None:
        secs = st.get("secs")
        if secs is None:
            secs = [float(x.get("sec") or 0) for x in rows]
            if any(secs[i] > secs[i + 1] for i in range(len(secs) - 1)):
                secs = False
            st["secs"] = secs
    if not secs:
        if hi_open:
            return [x for x in rows if lo <= float(x.get("sec") or 0) < hi]
        return [x for x in rows if lo <= float(x.get("sec") or 0) <= hi]
    a = bisect.bisect_left(secs, lo)
    b = bisect.bisect_left(secs, hi) if hi_open else bisect.bisect_right(secs, hi)
    return rows[a:b]


def _fast_rows_event(rows, names) -> list:
    """[x for x in rows if _event_name(x) in names] 와 같은 결과 (같은 순서)."""
    st = _fast_state(rows)
    if st is None:
        return [x for x in rows if _event_name(x) in names]
    return [rows[i] for i in _fast_positions(rows, st, "event", names)]


def _row_extra_spell_id(row: dict) -> int | None:
    return safe_int(row.get("extraSpellID") or row.get("extraAbilityGameID"))


def _coiled_player_roster(rows: list[dict]) -> dict:
    """Return realm-preserving player names plus first observed death times.

    The event stream may be a WCL subset rather than a full raw log, so this is
    deliberately described as the *observed* roster.  It is sufficient for the
    shield-window potion denominator when player streams are present, and the
    output exposes the count/source so the UI never pretends it is omniscient.
    """
    st = _fast_state(rows)
    if st is not None and st.get("roster") is not None:
        cached = st["roster"]
        return {"namesByKey": dict(cached["namesByKey"]), "names": list(cached["names"]),
                "deathByKey": dict(cached["deathByKey"]), "observedCount": cached["observedCount"]}
    names: dict[str, str] = {}
    death_by_key: dict[str, float] = {}

    def remember(row: dict, side: str):
        if side == "source":
            guid = str(row.get("sourceGUID") or "")
            typ = str(row.get("sourceType") or "").strip().lower()
            name = row.get("sourceName")
            pid = row.get("sourceID")
        else:
            guid = str(row.get("destGUID") or "")
            typ = str(row.get("targetType") or "").strip().lower()
            name = row.get("targetName") or row.get("destName")
            pid = row.get("targetID")
        is_player = typ == "player" or guid.startswith("Player-")
        if not is_player or not name:
            return None
        key = _player_name_key(name)
        if not key:
            return None
        names.setdefault(key, str(name))
        return key

    for row in rows:
        remember(row, "source")
        tkey = remember(row, "target")
        if _event_name(row) in {"death", "unit_died", "unit_destroyed"} and tkey:
            sec = float(row.get("sec") or 0)
            if tkey not in death_by_key or sec < death_by_key[tkey]:
                death_by_key[tkey] = sec
    out = {
        "namesByKey": names,
        "names": list(names.values()),
        "deathByKey": death_by_key,
        "observedCount": len(names),
    }
    if st is not None:
        st["roster"] = {"namesByKey": dict(names), "names": list(out["names"]),
                        "deathByKey": dict(death_by_key), "observedCount": len(names)}
    return out


def _coiled_alive_players_at(rows: list[dict], sec: float) -> dict:
    roster = _coiled_player_roster(rows)
    dead_keys = {
        key for key, death_sec in (roster.get("deathByKey") or {}).items()
        if float(death_sec) < float(sec)
    }
    names_by_key = roster.get("namesByKey") or {}
    alive = [name for key, name in names_by_key.items() if key not in dead_keys]
    dead = [name for key, name in names_by_key.items() if key in dead_keys]
    return {
        "alive": alive,
        "deadExcluded": dead,
        "aliveCount": len(alive),
        "deadExcludedCount": len(dead),
        "observedRosterCount": int(roster.get("observedCount") or 0),
        "source": "observed_event_roster",
    }


def _coiled_match_player_spell(
    rows: list[dict],
    *,
    spell_ids: set[int],
    center_sec: float,
    before_sec: float,
    after_sec: float,
    player: str | None = None,
) -> list[dict]:
    if not spell_ids:
        return []
    pkey = _player_name_key(player) if player else ""
    out = []
    lo = float(center_sec) - max(0.0, float(before_sec))
    hi = float(center_sec) + max(0.0, float(after_sec))
    for row in rows:
        sec = float(row.get("sec") or 0)
        if sec < lo or sec > hi:
            continue
        if _row_spell_id(row) not in spell_ids:
            continue
        # Completed casts are preferred. Aura apply is also accepted for raid
        # haste effects because several log sources expose the buff but omit a
        # player cast row.
        if not (_is_completed_cast(row) or _is_aura_apply(row)):
            continue
        source_name = row.get("sourceName")
        if pkey and _player_name_key(source_name) != pkey:
            continue
        if source_name and (str(row.get("sourceType") or "").lower() == "player" or str(row.get("sourceGUID") or "").startswith("Player-")):
            out.append(row)
        elif not player and _is_aura_apply(row):
            # Bloodlust-family auras may be attributed differently depending on
            # stream. Keep as observational evidence only.
            out.append(row)
    return out



def _coiled_auto_spec_entry_policy(cfg: dict, *, spec_id: int, shield_index: int, spell_id: int) -> tuple[str, dict | None]:
    """Return per-entry raidPlan policy, falling back to legacy global mode.

    B36.5.2 makes the learned catalog a candidate layer only.  A matching
    raidPlan ``entryPolicies`` row wins over the legacy/global mode.  Unlisted
    catalog entries use ``defaultMode`` (or the old ``judgmentMode``) so older
    user strategy files continue to work.
    """
    for row in (cfg.get("entryPolicies") or []):
        if not isinstance(row, dict):
            continue
        sid = safe_int(row.get("specID"))
        sh = safe_int(row.get("shieldIndex"))
        sp = safe_int(row.get("spellID"))
        if sid == int(spec_id) and sh == int(shield_index) and sp == int(spell_id):
            mode = str(row.get("mode") or "observe").strip().lower()
            if mode not in {"judge", "observe", "ignore"}:
                mode = "observe"
            return mode, row
    fallback = str(cfg.get("judgmentMode") or cfg.get("defaultMode") or "observe").strip().lower()
    if fallback not in {"judge", "observe", "ignore"}:
        fallback = "observe"
    return fallback, None


def _coiled_auto_spec_cooldown_alignment(
    rows: list[dict],
    *,
    shield_index: int,
    shield_start_sec: float,
    strategy: dict,
) -> dict:
    """Evaluate learned specialization cooldowns against raidPlan entry policy.

    B36.5.2 keeps automatic discovery and actual raid obligation separate:
    * catalog-only entries are observations unless the fallback/global mode says otherwise;
    * exact raidPlan ``entryPolicies`` may promote one spec+shield+spell to judge;
    * promoted judge rows remain usable even if the learned catalog later goes stale,
      because their timing/tolerance is copied into the raidPlan policy itself.
    """
    phase2_plan = (((strategy or {}).get("raidPlan") or {}).get("phase2") or {})
    cfg = phase2_plan.get("autoSpecCooldowns") or {}
    enabled = bool(cfg.get("enabled", True))
    fallback_mode = str(cfg.get("judgmentMode") or cfg.get("defaultMode") or "observe").strip().lower()
    if fallback_mode not in {"observe", "judge", "ignore"}:
        fallback_mode = "observe"
    if not enabled:
        return {
            "enabled": False, "status": "unknown", "judgmentMode": fallback_mode,
            "effectivePolicyMode": "none", "requirements": [], "reason": "disabled",
            "hasJudgeRequirements": False, "evidenceLevel": EVIDENCE_UNAVAILABLE,
        }

    catalog = load_raid_cooldown_catalog()
    spec_catalogs = catalog.get("specCatalogs") or {}
    policies = [x for x in (cfg.get("entryPolicies") or []) if isinstance(x, dict)]
    if not spec_catalogs and not policies:
        return {
            "enabled": True, "status": "unknown", "judgmentMode": fallback_mode,
            "effectivePolicyMode": "none", "requirements": [], "reason": "catalog_unavailable",
            "catalogSource": catalog.get("source"), "catalogPath": catalog.get("path"),
            "hasJudgeRequirements": False, "evidenceLevel": EVIDENCE_UNAVAILABLE,
        }

    player_cast_rows = [
        x for x in rows
        if _is_completed_cast(x)
        and (str(x.get("sourceType") or "").strip().lower() == "player" or str(x.get("sourceGUID") or "").startswith("Player-"))
        and x.get("sourceName")
    ]
    if not player_cast_rows:
        return {
            "enabled": True, "status": "unknown", "judgmentMode": fallback_mode,
            "effectivePolicyMode": "none", "requirements": [], "reason": "friendly_cast_stream_unavailable",
            "catalogSource": catalog.get("source"), "catalogPath": catalog.get("path"),
            "hasJudgeRequirements": False, "evidenceLevel": EVIDENCE_UNAVAILABLE,
        }

    alive_ctx = _coiled_alive_players_at(rows, shield_start_sec)
    alive_names = list(alive_ctx.get("alive") or [])
    roster_specs = resolve_roster_specs(rows)
    by_name = roster_specs.get("players") or {}

    def fnum(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    min_samples = int(fnum(cfg.get("minimumReferenceSamples"), 2))
    min_rate = fnum(cfg.get("minimumHitRate"), 0.67)
    cfg_before = fnum(cfg.get("toleranceBeforeSec"), 5.0)
    cfg_after = fnum(cfg.get("toleranceAfterSec"), 5.0)
    default_extended = max(cfg_before, cfg_after, fnum(cfg.get("lateSearchSec"), 18.0))

    reqs = []
    unknown_spec_players = []
    eligible_players = set()
    for player in alive_names:
        info = by_name.get(_player_name_key(player))
        spec_id = safe_int((info or {}).get("specID"))
        if spec_id is None:
            unknown_spec_players.append(player)
            continue

        catalog_entries = {safe_int(x.get("spellID")): dict(x) for x in raid_cooldown_entries_for(catalog, spec_id, shield_index) if safe_int(x.get("spellID")) is not None}
        policy_entries = {
            safe_int(x.get("spellID")): dict(x)
            for x in policies
            if safe_int(x.get("specID")) == spec_id
            and safe_int(x.get("shieldIndex")) == int(shield_index)
            and safe_int(x.get("spellID")) is not None
        }
        spell_ids = sorted(set(catalog_entries) | set(policy_entries))
        for sid in spell_ids:
            entry = catalog_entries.get(sid) or {}
            policy_mode, policy = _coiled_auto_spec_entry_policy(
                cfg, spec_id=spec_id, shield_index=shield_index, spell_id=sid
            )
            if policy_mode == "ignore":
                continue

            ev = entry.get("evidence") or {}
            explicit_policy = policy is not None
            refs = safe_int(ev.get("referenceSampleCount")) or 0
            rate = float(ev.get("hitRate") or 0)
            # Learned-only candidates still obey learning quality thresholds.
            # A promoted raidPlan entry is authoritative and may survive a stale
            # or temporarily unavailable catalog because its timing was copied
            # into the policy at promotion time.
            if not explicit_policy and (refs < min_samples or rate < min_rate):
                continue

            eligible_players.add(_player_name_key(player))
            expected_offset = fnum((policy or {}).get("expectedOffsetSec"), entry.get("expectedOffsetSec") if entry.get("expectedOffsetSec") is not None else 0.0)
            before = fnum((policy or {}).get("toleranceBeforeSec"), entry.get("toleranceBeforeSec") if entry.get("toleranceBeforeSec") is not None else cfg_before)
            after = fnum((policy or {}).get("toleranceAfterSec"), entry.get("toleranceAfterSec") if entry.get("toleranceAfterSec") is not None else cfg_after)
            extended = fnum((policy or {}).get("lateSearchSec"), default_extended)
            label = (policy or {}).get("label") or entry.get("name") or str(sid)
            center = float(shield_start_sec) + expected_offset
            target_player = str((policy or {}).get("targetPlayer") or "").strip()
            target_required = bool((policy or {}).get("targetAssignmentRequired"))

            def target_ok(row):
                if not target_player:
                    return True
                actual = row.get("targetName") or row.get("destName")
                return _player_name_key(actual) == _player_name_key(target_player)

            matches = [x for x in _coiled_match_player_spell(
                rows, spell_ids={sid}, center_sec=center,
                before_sec=before, after_sec=after, player=player,
            ) if target_ok(x)]
            nearest = None
            if not matches:
                broader = [x for x in _coiled_match_player_spell(
                    rows, spell_ids={sid}, center_sec=center,
                    before_sec=extended, after_sec=extended, player=player,
                ) if target_ok(x)]
                if broader:
                    nearest = min(broader, key=lambda x: abs(float(x.get("sec") or 0) - center))

            target_plan_missing = target_required and not target_player
            if matches:
                status = "success"
                chosen = min(matches, key=lambda x: abs(float(x.get("sec") or 0) - center))
            elif nearest is not None:
                status = "late"
                chosen = nearest
            else:
                status = "missing"
                chosen = None
            # Targeted external buffs are kept observational until a recipient
            # assignment exists.  If somebody explicitly promotes such an entry
            # to judge without a target, do not create a false raid failure.
            if policy_mode == "judge" and target_plan_missing:
                status = "unknown"
                chosen = None

            cast_sec = round(float(chosen.get("sec") or 0), 3) if chosen else None
            reqs.append({
                "kind": "auto_spec", "player": player, "specID": spec_id,
                "spellID": sid, "label": label,
                "shieldIndex": int(shield_index), "status": status,
                "policyMode": policy_mode,
                "policySource": (policy or {}).get("source") or ("raidPlan_entry_policy" if explicit_policy else "catalog_default"),
                "policyNote": (policy or {}).get("note"),
                "policyRole": (policy or {}).get("role"),
                "targetAssignmentRequired": target_required,
                "targetPlayer": target_player or None,
                "targetPlanMissing": target_plan_missing,
                "expectedOffsetSec": round(expected_offset, 3),
                "expectedSec": round(center, 3), "castSec": cast_sec,
                "deviationSec": round(cast_sec - center, 3) if cast_sec is not None else None,
                "toleranceBeforeSec": before, "toleranceAfterSec": after,
                "referenceEvidence": ev,
                "catalogPresent": bool(entry),
            })

    judged = [x for x in reqs if x.get("policyMode") == "judge"]
    observed = [x for x in reqs if x.get("policyMode") == "observe"]

    def counts(group):
        return {
            "denominator": len(group),
            "success": sum(1 for x in group if x.get("status") == "success"),
            "late": sum(1 for x in group if x.get("status") == "late"),
            "missing": sum(1 for x in group if x.get("status") == "missing"),
            "unknown": sum(1 for x in group if x.get("status") == "unknown"),
        }

    jc = counts(judged)
    oc = counts(observed)
    if jc["denominator"]:
        if jc["late"] or jc["missing"]:
            judge_status = "failed"
        elif jc["unknown"]:
            judge_status = "unknown"
        else:
            judge_status = "success"
    else:
        judge_status = "unknown"
    if oc["denominator"]:
        if oc["late"] or oc["missing"]:
            observe_status = "warning"
        elif oc["unknown"]:
            observe_status = "unknown"
        else:
            observe_status = "success"
    else:
        observe_status = "unknown"

    if jc["denominator"]:
        status = judge_status
        reason = "raidplan_entry_policy"
    elif oc["denominator"]:
        status = observe_status
        reason = "catalog_observation"
    else:
        status = "unknown"
        reason = "no_catalog_entries_for_current_roster"

    effective = "mixed" if jc["denominator"] and oc["denominator"] else "judge" if jc["denominator"] else "observe" if oc["denominator"] else "none"
    total = counts(reqs)
    return {
        "enabled": True,
        "status": status,
        "judgmentMode": fallback_mode,
        "effectivePolicyMode": effective,
        "hasJudgeRequirements": bool(jc["denominator"]),
        "judgeStatus": judge_status,
        "observeStatus": observe_status,
        "requirements": reqs,
        "judgedRequirements": judged,
        "observedRequirements": observed,
        "denominator": total["denominator"],
        "successCount": total["success"],
        "lateCount": total["late"],
        "missingCount": total["missing"],
        "unknownCount": total["unknown"],
        "judgedDenominator": jc["denominator"],
        "judgedSuccessCount": jc["success"],
        "judgedLateCount": jc["late"],
        "judgedMissingCount": jc["missing"],
        "judgedUnknownCount": jc["unknown"],
        "observedDenominator": oc["denominator"],
        "observedSuccessCount": oc["success"],
        "observedLateCount": oc["late"],
        "observedMissingCount": oc["missing"],
        "observedUnknownCount": oc["unknown"],
        "eligiblePlayerCount": len(eligible_players),
        "alivePlayerCount": len(alive_names),
        "unknownSpecPlayers": unknown_spec_players,
        "reason": reason,
        "catalogSource": catalog.get("source"),
        "catalogPath": catalog.get("path"),
        "catalogUpdatedAt": catalog.get("updatedAt"),
        "scope": (
            f"범위: 현재 트라이 · 보호막 {int(shield_index)} · "
            f"raidPlan judge n={jc['denominator']} + 관측 n={oc['denominator']}"
        ),
        "evidenceLevel": EVIDENCE_VERIFIED if reqs else EVIDENCE_UNAVAILABLE,
        "note": "B36.5.2: 카탈로그는 후보, raidPlan entryPolicies의 명시 judge만 보호막 실패 원인 A에 합류. observe 항목은 미사용/지연을 보여도 원인으로 승격하지 않음.",
    }

def _coiled_shield_cooldown_alignment(
    rows: list[dict],
    *,
    shield_index: int,
    shield_start_sec: float,
    phase2_anchor_sec: float | None,
    strategy: dict,
) -> dict:
    phase2_plan = (((strategy or {}).get("raidPlan") or {}).get("phase2") or {})
    windows = [x for x in (phase2_plan.get("shieldWindows") or []) if isinstance(x, dict)]
    plan = next((x for x in windows if safe_int(x.get("index")) == int(shield_index)), {})
    defaults = phase2_plan.get("cooldownDefaults") or {}

    def fnum(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    default_before = fnum(defaults.get("toleranceBeforeSec"), 3.0)
    default_after = fnum(defaults.get("toleranceAfterSec"), 5.0)
    if plan.get("toleranceSec") is not None:
        default_before = default_after = fnum(plan.get("toleranceSec"), 5.0)
    default_before = fnum(plan.get("toleranceBeforeSec"), default_before)
    default_after = fnum(plan.get("toleranceAfterSec"), default_after)

    blood_ids = {safe_int(x) for x in (defaults.get("bloodlustSpellIDs") or _BLOODLUST_FAMILY_IDS) if safe_int(x) is not None}
    potion_ids = {safe_int(x) for x in (defaults.get("combatPotionSpellIDs") or _COMBAT_POTION_IDS) if safe_int(x) is not None}

    requirements = []
    failures = []
    observations = []

    blood_req = plan.get("blood")
    if blood_req is True:
        matches = _coiled_match_player_spell(
            rows, spell_ids=blood_ids, center_sec=shield_start_sec,
            before_sec=default_before, after_sec=default_after,
        )
        ok = bool(matches)
        item = {
            "kind": "bloodlust", "required": True, "status": "success" if ok else "failed",
            "matches": [{"sec": round(float(x.get("sec") or 0), 3), "player": x.get("sourceName"), "spellID": _row_spell_id(x)} for x in matches],
            "toleranceBeforeSec": default_before, "toleranceAfterSec": default_after,
        }
        requirements.append(item)
        if not ok:
            failures.append(item)
    elif blood_req is False:
        observations.append({"kind": "bloodlust", "required": False, "status": "not_required"})

    potion_req = plan.get("potion")
    if potion_req is True:
        alive_ctx = _coiled_alive_players_at(rows, shield_start_sec)
        alive_names = list(alive_ctx.get("alive") or [])
        per_player = []
        used = []
        missing = []
        for name in alive_names:
            matches = _coiled_match_player_spell(
                rows, spell_ids=potion_ids, center_sec=shield_start_sec,
                before_sec=default_before, after_sec=default_after, player=name,
            )
            if matches:
                used.append(name)
            else:
                missing.append(name)
            per_player.append({
                "player": name,
                "status": "success" if matches else "failed",
                "matches": [{"sec": round(float(x.get("sec") or 0), 3), "spellID": _row_spell_id(x)} for x in matches],
            })
        # A zero observed roster is not evidence of raid-wide potion failure.
        if alive_names:
            pstatus = "success" if not missing else "failed"
        else:
            pstatus = "unknown"
        item = {
            "kind": "potion", "required": True, "status": pstatus,
            "usedCount": len(used), "denominator": len(alive_names),
            "aliveAtShield": alive_names, "missingPlayers": missing,
            "deadExcluded": alive_ctx.get("deadExcluded") or [],
            "deadExcludedCount": int(alive_ctx.get("deadExcludedCount") or 0),
            "rosterSource": alive_ctx.get("source"),
            "perPlayer": per_player,
            "toleranceBeforeSec": default_before, "toleranceAfterSec": default_after,
        }
        requirements.append(item)
        if pstatus == "failed":
            failures.append(item)
    elif potion_req is False:
        observations.append({"kind": "potion", "required": False, "status": "not_required"})

    for assigned in (plan.get("assignedCooldowns") or []):
        if not isinstance(assigned, dict):
            continue
        sid = safe_int(assigned.get("spellID"))
        player = str(assigned.get("player") or "").strip()
        if sid is None or not player:
            continue
        before = fnum(assigned.get("toleranceBeforeSec"), assigned.get("toleranceSec") if assigned.get("toleranceSec") is not None else default_before)
        after = fnum(assigned.get("toleranceAfterSec"), assigned.get("toleranceSec") if assigned.get("toleranceSec") is not None else default_after)
        matches = _coiled_match_player_spell(
            rows, spell_ids={sid}, center_sec=shield_start_sec,
            before_sec=before, after_sec=after, player=player,
        )
        ok = bool(matches)
        item = {
            "kind": "assigned", "required": True, "player": player, "spellID": sid,
            "label": assigned.get("label"), "status": "success" if ok else "failed",
            "matches": [{"sec": round(float(x.get("sec") or 0), 3), "spellID": _row_spell_id(x)} for x in matches],
            "toleranceBeforeSec": before, "toleranceAfterSec": after,
        }
        requirements.append(item)
        if not ok:
            failures.append(item)

    if requirements:
        if any(x.get("status") == "failed" for x in requirements):
            status = "failed"
        elif any(x.get("status") == "unknown" for x in requirements):
            status = "unknown"
        else:
            status = "success"
    else:
        status = "unknown"

    auto_spec = _coiled_auto_spec_cooldown_alignment(
        rows, shield_index=shield_index, shield_start_sec=shield_start_sec, strategy=strategy
    )
    auto_judges = bool(auto_spec.get("hasJudgeRequirements"))
    if auto_judges:
        if auto_spec.get("status") == "failed":
            status = "failed"
        elif auto_spec.get("status") == "success" and not requirements:
            status = "success"
        elif auto_spec.get("status") == "unknown" and not requirements:
            status = "unknown"

    target_offset = plan.get("targetOffsetSec")
    try:
        target_offset = float(target_offset) if target_offset is not None else None
    except (TypeError, ValueError):
        target_offset = None
    p2_rel = round(float(shield_start_sec) - float(phase2_anchor_sec), 3) if phase2_anchor_sec is not None else None
    deviation = round(p2_rel - target_offset, 3) if p2_rel is not None and target_offset is not None else None
    return {
        "status": status,
        "manualRequirementsConfigured": bool(requirements),
        "requirementsConfigured": bool(requirements) or bool(auto_spec.get("hasJudgeRequirements")),
        "requirements": requirements,
        "failures": failures,
        "observations": observations,
        "autoSpecCooldowns": auto_spec,
        "targetOffsetSec": target_offset,
        "p2RelativeSec": p2_rel,
        "scheduleDeviationSec": deviation,
        "scheduleToleranceSec": plan.get("scheduleToleranceSec"),
        "evidenceLevel": EVIDENCE_VERIFIED if (requirements or int(auto_spec.get("denominator") or 0) > 0) else EVIDENCE_UNAVAILABLE,
        "note": "실제 보호막 시작 시각 기준으로 raidPlan 지정 블러드/물약/개별 쿨기와 B36.5.2 자동 전문화 카탈로그/raidPlan entry policy를 분리 매칭. 명시 judge만 실패 원인 A에 합류하며 NSRT 목표 시각 편차는 별도 표시.",
    }


def _coiled_observed_nightfall_cast_duration(rows: list[dict], cast_ids: set[int], source_ids: set[int]) -> float | None:
    starts = [
        x for x in _fast_rows_spell(rows, cast_ids)
        if (not source_ids or safe_int(x.get("sourceGameID")) in source_ids) and _is_cast_start(x)
    ]
    successes = [
        x for x in _fast_rows_spell(rows, cast_ids)
        if (not source_ids or safe_int(x.get("sourceGameID")) in source_ids) and _is_completed_cast(x)
    ]
    durations = []
    for success in successes:
        ssec = float(success.get("sec") or 0)
        candidates = [x for x in starts if 0 <= ssec - float(x.get("sec") or 0) <= 30.0]
        if candidates:
            start = candidates[-1]
            duration = ssec - float(start.get("sec") or 0)
            if duration > 0:
                durations.append(duration)
    if not durations:
        return None
    durations.sort()
    mid = len(durations) // 2
    value = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) / 2.0
    return round(float(value), 3)


def _coiled_shield_cycles(
    rows: list[dict],
    rule: dict,
    strategy: dict,
    encounter: dict,
    *,
    phase2_anchor_sec: float | None,
    phase2_end_sec: float | None,
) -> list[dict]:
    spec = ((rule.get("mechanics") or {}).get("eternalNightfall") or {})
    shield_ids = {safe_int(x) for x in (spec.get("shieldAuraSpellIDs") or [1286912]) if safe_int(x) is not None}
    nightfall_aura_ids = {safe_int(x) for x in (spec.get("auraSpellIDs") or [1310752]) if safe_int(x) is not None}
    cast_ids = {safe_int(x) for x in (spec.get("castSpellIDs") or spec.get("spellIDs") or [1286918]) if safe_int(x) is not None}
    source_ids = {safe_int(x) for x in (spec.get("sourceNpcIDs") or [259854]) if safe_int(x) is not None}
    cast_duration = _coiled_observed_nightfall_cast_duration(rows, cast_ids, source_ids)

    applies = [
        x for x in _fast_rows_spell(rows, shield_ids)
        if _is_aura_apply(x)
        and (not source_ids or safe_int(x.get("sourceGameID")) in source_ids or safe_int(x.get("targetGameID")) in source_ids)
    ]
    applies.sort(key=lambda x: float(x.get("sec") or 0))
    out = []
    encounter_end = max([float(x.get("sec") or 0) for x in rows] + [float(encounter.get("durationSec") or 0)])
    result = str(encounter.get("result") or ("Kill" if encounter.get("kill") else "Wipe")).strip().lower()
    is_wipe = result not in {"kill", "success", "1", "true"}

    for idx, apply in enumerate(applies, 1):
        start = float(apply.get("sec") or 0)
        next_start = float(applies[idx].get("sec") or 0) if idx < len(applies) else (float(phase2_end_sec) if phase2_end_sec is not None and float(phase2_end_sec) > start else encounter_end + 0.001)
        cycle_rows = _fast_rows_between(rows, start - 0.25, next_start, hi_open=True)
        removes = [x for x in cycle_rows if _row_spell_id(x) in shield_ids and _is_aura_remove(x)]
        shield_remove = removes[0] if removes else None
        break_sec = float(shield_remove.get("sec") or 0) if shield_remove else None

        starts = [x for x in cycle_rows if _row_spell_id(x) in cast_ids and _is_cast_start(x)]
        cast_start = starts[0] if starts else None
        cast_start_sec = float(cast_start.get("sec") or 0) if cast_start else None
        successes = [x for x in cycle_rows if _row_spell_id(x) in cast_ids and _is_completed_cast(x)]
        cast_success = successes[0] if successes else None
        cast_success_sec = float(cast_success.get("sec") or 0) if cast_success else None
        interrupts = [x for x in cycle_rows if _is_interrupt(x) and _row_extra_spell_id(x) in cast_ids]
        interrupt = interrupts[0] if interrupts else None
        interrupt_sec = float(interrupt.get("sec") or 0) if interrupt else None

        aura_applies = [x for x in cycle_rows if _row_spell_id(x) in nightfall_aura_ids and _is_aura_apply(x)]
        aura_removes = [x for x in cycle_rows if _row_spell_id(x) in nightfall_aura_ids and _is_aura_remove(x)]

        # Shield throughput/remaining must be measured at the decisive shield
        # deadline, not across the entire phase.  In a failed cycle the boss can
        # continue taking hits after Eternal Nightfall completes; counting those
        # later absorbs would make a failed 12% shield incorrectly look like 0%.
        shield_measure_end_sec = None
        decisive_candidates = [x for x in (break_sec, cast_success_sec) if x is not None]
        if decisive_candidates:
            shield_measure_end_sec = min(decisive_candidates)
        elif interrupt_sec is not None:
            shield_measure_end_sec = interrupt_sec
        else:
            shield_measure_end_sec = min(next_start - 0.001, encounter_end)

        initial_absorb = safe_int(apply.get("auraAmount"))
        absorb_rows = [
            x for x in cycle_rows
            if _event_name(x) == "spell_absorbed"
            and safe_int(x.get("absorbingSpellID")) in shield_ids
            and (not source_ids or safe_int(x.get("targetGameID")) in source_ids or safe_int(x.get("sourceGameID")) in source_ids)
            and float(x.get("sec") or 0) <= float(shield_measure_end_sec) + 0.001
        ]
        absorbed_total = sum(int(x.get("absorbedAmount") or 0) for x in absorb_rows if x.get("absorbedAmount") is not None)
        remaining = None
        remaining_basis = None
        if initial_absorb is not None:
            remaining = max(0, int(initial_absorb) - int(absorbed_total))
            remaining_basis = "initial_minus_spell_absorbed"
        elif shield_remove is not None and shield_remove.get("auraAmount") is not None:
            remaining = max(0, int(shield_remove.get("auraAmount") or 0))
            remaining_basis = "aura_remove_amount"
        remaining_pct = round(100.0 * float(remaining) / float(initial_absorb), 1) if initial_absorb not in (None, 0) and remaining is not None else None

        unresolved_end = min(next_start - 0.001, encounter_end)
        wiped_during = bool(
            is_wipe
            and cast_success_sec is None
            and interrupt_sec is None
            and encounter_end >= start
            and encounter_end <= unresolved_end + 0.01
        )

        if cast_success_sec is not None:
            status = "failed"
            nightfall_status = "failed"
            shield_status = "failed" if break_sec is None or break_sec > cast_success_sec else "success"
        elif wiped_during:
            status = "unknown"
            nightfall_status = "unknown"
            shield_status = "unknown" if break_sec is None else "success"
        elif break_sec is not None and interrupt_sec is not None and break_sec <= interrupt_sec:
            status = "success"
            shield_status = "success"
            nightfall_status = "success"
        elif break_sec is not None:
            status = "warning"
            shield_status = "success"
            nightfall_status = "unknown"
        elif interrupt_sec is not None:
            status = "warning"
            shield_status = "unknown"
            nightfall_status = "success"
        else:
            status = "unknown"
            shield_status = "unknown"
            nightfall_status = "unknown"

        margin = None
        if cast_duration is not None and cast_start_sec is not None and break_sec is not None:
            margin = round((cast_start_sec + cast_duration) - break_sec, 3)

        # resolutionSec answers when the mechanic outcome was known, while
        # windowDurationSec remains the shield-damage window used for throughput.
        if status == "failed" and cast_success_sec is not None:
            resolution_sec = cast_success_sec
        elif status == "success" and interrupt_sec is not None:
            resolution_sec = interrupt_sec
        else:
            resolution_sec = break_sec if break_sec is not None else interrupt_sec if interrupt_sec is not None else encounter_end
        window_duration = max(0.0, float(shield_measure_end_sec) - float(start)) if shield_measure_end_sec is not None else None
        absorbed_per_sec = (
            round(float(absorbed_total) / float(window_duration), 3)
            if absorb_rows and window_duration not in (None, 0) else None
        )

        alive_ctx = _coiled_alive_players_at(rows, start)
        deaths_during = []
        roster = _coiled_player_roster(rows)
        for key, death_sec in (roster.get("deathByKey") or {}).items():
            end_for_death = shield_measure_end_sec if shield_measure_end_sec is not None else next_start
            if start <= float(death_sec) <= float(end_for_death):
                deaths_during.append((roster.get("namesByKey") or {}).get(key, key))

        alignment = _coiled_shield_cooldown_alignment(
            rows,
            shield_index=idx,
            shield_start_sec=start,
            phase2_anchor_sec=phase2_anchor_sec,
            strategy=strategy,
        )
        if status == "failed" and alignment.get("status") == "failed":
            cause = "A"
            cause_label = "쿨기 정렬 미달"
        elif status == "failed" and deaths_during:
            cause = "C"
            cause_label = "세부 실행 손실 확인"
        elif status == "failed" and alignment.get("status") == "success":
            cause = "B"
            cause_label = "쿨기 정렬 정상 · 보호막 처리량 부족"
        else:
            cause = None
            cause_label = "원인 분류 보류"

        out.append({
            "index": idx,
            "status": status,
            "shieldStatus": shield_status,
            "nightfallStatus": nightfall_status,
            "startSec": round(start, 3),
            "breakSec": round(break_sec, 3) if break_sec is not None else None,
            "breakDurationSec": round(break_sec - start, 3) if break_sec is not None else None,
            "castStartSec": round(cast_start_sec, 3) if cast_start_sec is not None else None,
            "castSuccessSec": round(cast_success_sec, 3) if cast_success_sec is not None else None,
            "interruptSec": round(interrupt_sec, 3) if interrupt_sec is not None else None,
            "interruptPlayer": interrupt.get("sourceName") if interrupt else None,
            "nightfallAuraApplySec": round(float(aura_applies[0].get("sec") or 0), 3) if aura_applies else None,
            "nightfallAuraRemoveSec": round(float(aura_removes[0].get("sec") or 0), 3) if aura_removes else None,
            "initialAbsorb": initial_absorb,
            "absorbedTotal": absorbed_total if absorb_rows else 0 if initial_absorb is not None else None,
            "remainingAbsorb": remaining,
            "remainingPct": remaining_pct,
            "remainingBasis": remaining_basis,
            "absorbEventCount": len(absorb_rows),
            "resolutionSec": round(resolution_sec, 3) if resolution_sec is not None else None,
            "windowDurationSec": round(window_duration, 3) if window_duration is not None else None,
            "absorbedPerSec": absorbed_per_sec,
            "castDurationObservedSec": cast_duration,
            "breakMarginSec": margin,
            "wipeDuringCycle": wiped_during,
            "aliveAtStart": alive_ctx,
            "deathsDuring": deaths_during,
            "cooldownAlignment": alignment,
            "causeClass": cause,
            "causeLabel": cause_label,
            "p2RelativeSec": alignment.get("p2RelativeSec"),
            "targetOffsetSec": alignment.get("targetOffsetSec"),
            "scheduleDeviationSec": alignment.get("scheduleDeviationSec"),
            "evidenceLevel": EVIDENCE_VERIFIED,
            "ids": {
                "shieldAuraSpellIDs": sorted(shield_ids),
                "nightfallAuraSpellIDs": sorted(nightfall_aura_ids),
                "nightfallCastSpellIDs": sorted(cast_ids),
            },
            "note": "보호막 파괴 + 영원한 일몰 실제 차단을 함께 성공으로 판정. 전멸/로그 종료로 미완료된 시전은 성공으로 추정하지 않음.",
        })
    return out


def _coiled_shield_observation_plan(
    rows: list[dict],
    strategy: dict,
    encounter: dict,
    shield_cycles: list[dict],
    *,
    phase2_anchor_sec: float | None,
    phase2_anchor_basis: str | None,
) -> dict:
    """B37.4.16 readiness slots for first real shield/nightfall reach.

    The 70/155 second values are raid-plan/NSRT expectations, never a success
    threshold.  Before the mechanic is reached we expose how far the pull ended
    from the expected window; when it is reached the existing B36.5 shield
    analyzer supplies absorb, cast, interrupt, and schedule-deviation evidence.
    """
    phase2_plan = (((strategy or {}).get("raidPlan") or {}).get("phase2") or {})
    windows = [x for x in (phase2_plan.get("shieldWindows") or []) if isinstance(x, dict)]
    encounter_end = max([float(x.get("sec") or 0) for x in rows] + [float(encounter.get("durationSec") or 0)])
    by_index = {safe_int(x.get("index")): x for x in (shield_cycles or []) if safe_int(x.get("index")) is not None}

    slots = []
    for raw in windows:
        idx = safe_int(raw.get("index"))
        offset = raw.get("targetOffsetSec")
        tolerance = raw.get("scheduleToleranceSec")
        try:
            offset_f = float(offset)
        except (TypeError, ValueError):
            offset_f = None
        try:
            tolerance_f = float(tolerance)
        except (TypeError, ValueError):
            tolerance_f = None
        target_sec = (float(phase2_anchor_sec) + offset_f) if phase2_anchor_sec is not None and offset_f is not None else None
        remaining = (target_sec - encounter_end) if target_sec is not None else None
        observed = by_index.get(idx) if idx is not None else None
        if observed:
            state = "observed"
        elif target_sec is None:
            state = "anchor_unavailable"
        elif remaining is not None and remaining > 0:
            state = "awaiting"
        else:
            state = "target_passed_unobserved"
        slots.append({
            "index": idx,
            "targetOffsetSec": offset_f,
            "scheduleToleranceSec": tolerance_f,
            "targetSec": round(target_sec, 3) if target_sec is not None else None,
            "encounterEndSec": round(encounter_end, 3),
            "timeToTargetAtEndSec": round(remaining, 3) if remaining is not None else None,
            "state": state,
            "observed": bool(observed),
            "actualStartSec": (observed or {}).get("startSec") if observed else None,
            "actualP2RelativeSec": (observed or {}).get("p2RelativeSec") if observed else None,
            "scheduleDeviationSec": (observed or {}).get("scheduleDeviationSec") if observed else None,
            "evidenceLevel": EVIDENCE_VERIFIED if observed else EVIDENCE_UNAVAILABLE,
        })

    return {
        "anchorSec": round(float(phase2_anchor_sec), 3) if phase2_anchor_sec is not None else None,
        "anchorBasis": phase2_anchor_basis,
        "slots": slots,
        "expectedOffsetsSec": [x.get("targetOffsetSec") for x in slots if x.get("targetOffsetSec") is not None],
        "observedCount": sum(1 for x in slots if x.get("observed")),
        "verificationFields": [
            "shieldAura 1286912 initialAbsorb",
            "shield breakSec / breakDurationSec / remainingPct",
            "Eternal Nightfall 1286918 castStartSec / observed cast duration",
            "interruptSec / interruptPlayer 또는 castSuccessSec",
            "P2 상대시각 및 NSRT targetOffsetSec(70/155) 편차",
        ],
        "note": "70/155초는 raidPlan/NSRT 관측 목표. 실제 첫 도달 전에는 실패 판정하지 않으며, 도달 즉시 흡수량·일몰 시전/차단·P2 상대시각을 기록해 추후 기준을 검증합니다.",
    }


def _coiled_post_g2_cleanup_signal(
    rows: list[dict],
    rule: dict,
    orb_accounting: dict,
    phase1_transition: dict,
    phase2: dict,
    strategy: dict,
    *,
    source_kind: str,
    collapse_snapshot: dict | None = None,
) -> dict:
    """Verified cleanup immediately before P2 from a raid-wide Venom Rupture cluster.

    The 42/44/47 WCL samples show a final 1299838 apply/apply-stack cluster
    after the last Sever breath and before Malacrass activation.  Per-player
    counts are 10/12/10 respectively and exactly match the active-orb ledger.
    Some WCL pulls split one logical burst across rows a few hundredths of a
    second apart, so the signal is clustered before per-player consensus is
    calculated.

    This proves the *cleanup result* (active orbs reach zero), but not who
    physically performed it.  The raidPlan keeps ``role=tank_handoff`` only as a compatibility label;
    actual execution is reported separately as ``bulk_rupture`` and the
    responsible player is not inferred from this signal.
    """
    plan = ((strategy or {}).get("raidPlan") or {}).get("orbHandoff") or {}
    g2 = next((x for x in (orb_accounting.get("guillotineStates") or []) if safe_int(x.get("guillotineIndex")) == 2), None)
    active = safe_int(orb_accounting.get("currentActiveOrbs"))
    if not g2 or active is None:
        return {
            "verified": False, "remainingOrbs": None, "removedOrbs": None,
            "status": "unknown", "reason": "orb_ledger_unavailable",
            "evidenceLevel": EVIDENCE_UNAVAILABLE,
        }

    entry_sec = phase1_transition.get("entrySec")
    entry_basis = "zuljan_hp_zero"
    if entry_sec is None:
        entry_sec = phase1_transition.get("malacrassActivationSec")
        entry_basis = "malacrass_activation_fallback"
    if entry_sec is None:
        entry_sec = (phase2 or {}).get("activationSec")
        entry_basis = "malacrass_activation_fallback"
    if entry_sec is None:
        return {
            "verified": False, "remainingOrbs": None, "removedOrbs": None,
            "status": "unknown", "reason": "p2_anchor_unavailable",
            "evidenceLevel": EVIDENCE_UNAVAILABLE,
        }

    if collapse_snapshot and collapse_snapshot.get("significantCluster"):
        csec = collapse_snapshot.get("collapseSec")
        if csec is not None and float(csec) <= float(entry_sec):
            return {
                "verified": False, "remainingOrbs": None, "removedOrbs": None,
                "status": "unknown", "reason": "collapse_before_p2_entry",
                "entrySec": round(float(entry_sec), 3), "entryBasis": entry_basis,
                "evidenceLevel": EVIDENCE_UNAVAILABLE,
            }

    # If verified breath arithmetic already reaches zero, no extra cleanup
    # signal is required.
    if active == 0:
        return {
            "verified": True, "remainingOrbs": 0, "removedOrbs": 0,
            "status": "success", "method": "orb_ledger_zero",
            "entrySec": round(float(entry_sec), 3), "entryBasis": entry_basis,
            "evidenceLevel": EVIDENCE_VERIFIED,
            "summary": "P2 진입 전 검증된 구슬 장부가 이미 0개",
        }

    try:
        grace = float(plan.get("graceSec", plan.get("graceWindowSec", 2.0)))
    except (TypeError, ValueError):
        grace = 2.0
    try:
        cluster_gap = float(plan.get("cleanupClusterGapSec", 0.15))
    except (TypeError, ValueError):
        cluster_gap = 0.15

    last_breath_sec = max(
        [float(x.get("sec") or 0) for x in (orb_accounting.get("ledger") or []) if x.get("kind") == "breath"]
        or [float(g2.get("sec") or 0)]
    )
    start_sec = max(float(g2.get("sec") or 0), last_breath_sec) + 0.20
    end_sec = float(entry_sec) + max(0.0, grace)

    spec = ((rule.get("mechanics") or {}).get("toxicRupture") or {})
    ids = {safe_int(x) for x in (spec.get("spellIDs") or []) if safe_int(x) is not None}
    sig_cfg = spec.get("orbCountSignal") or {}
    try:
        min_targets = int(sig_cfg.get("minConsensusTargets", ((strategy or {}).get("orbAccounting") or {}).get("minConsensusTargets", 4)))
    except (TypeError, ValueError):
        min_targets = 4
    allowed = {"applydebuff", "applydebuffstack", "spell_aura_applied", "spell_aura_applied_dose"}
    hits = [
        x for x in _fast_rows_spell(rows, ids)
        if _event_name(x) in allowed
        and start_sec <= float(x.get("sec") or 0) <= end_sec
        and (_is_player_target(x) or (not x.get("targetType") and _target_key(x) is not None))
    ]

    candidates = []
    for group in _group_times(hits, max_gap=cluster_gap):
        per_target = Counter(_target_key(x) for x in group if _target_key(x) is not None)
        if len(per_target) < min_targets:
            continue
        counts = sorted(int(v) for v in per_target.values())
        if not counts or min(counts) != max(counts):
            continue
        removed = counts[0]
        candidates.append({
            "sec": round(float(group[0].get("sec") or 0), 3),
            "endSec": round(float(group[-1].get("sec") or 0), 3),
            "removedOrbs": int(removed),
            "targetCount": len(per_target),
            "eventCount": len(group),
            "clusterGapSec": cluster_gap,
        })

    if not candidates:
        return {
            "verified": False, "remainingOrbs": None, "removedOrbs": None,
            "status": "unknown", "reason": "cleanup_signal_unavailable",
            "entrySec": round(float(entry_sec), 3), "entryBasis": entry_basis,
            "windowStartSec": round(start_sec, 3), "windowEndSec": round(end_sec, 3),
            "evidenceLevel": EVIDENCE_UNAVAILABLE,
        }

    signal = candidates[-1]
    removed = int(signal["removedOrbs"])
    if removed > int(active):
        return {
            **signal,
            "verified": False, "remainingOrbs": None,
            "status": "unknown", "reason": "cleanup_exceeds_active_ledger",
            "activeBeforeCleanup": int(active),
            "entrySec": round(float(entry_sec), 3), "entryBasis": entry_basis,
            "evidenceLevel": EVIDENCE_UNAVAILABLE,
        }

    remaining = int(active) - removed
    return {
        **signal,
        "verified": True,
        "status": "success" if remaining == 0 else "failed",
        "method": "bulk_rupture",
        "activeBeforeCleanup": int(active),
        "remainingOrbs": int(remaining),
        "entrySec": round(float(entry_sec), 3), "entryBasis": entry_basis,
        "windowStartSec": round(start_sec, 3), "windowEndSec": round(end_sec, 3),
        "evidenceLevel": EVIDENCE_VERIFIED,
        "summary": f"{signal['sec']:.1f} 일괄 파열 {removed}개 · P2 진입 잔존 {remaining}개",
        "note": "1299838 apply/apply-stack을 0.15초 클러스터로 묶고 플레이어별 공통값으로 합산. 실제 처리 담당자는 이 신호만으로 단정하지 않음.",
    }


def _coiled_tank_handoff(
    orb_accounting: dict,
    phase1_transition: dict,
    phase2: dict,
    strategy: dict,
    *,
    source_kind: str,
    cleanup_signal: dict | None = None,
) -> dict:
    plan = ((strategy or {}).get("raidPlan") or {}).get("orbHandoff") or {}
    g2 = next((x for x in (orb_accounting.get("guillotineStates") or []) if safe_int(x.get("guillotineIndex")) == 2), None)
    residual = (g2 or {}).get("remainingOrbs")
    max_value = safe_int(plan.get("tankHandoffMax")) if plan.get("tankHandoffMax") is not None else None
    if residual is None:
        load_status = "unknown"
    elif max_value is not None and int(residual) > int(max_value):
        load_status = "warning"
    else:
        load_status = "info"

    p2_entry_sec = phase1_transition.get("entrySec")
    p2_entry_basis = "zuljan_hp_zero" if p2_entry_sec is not None else None
    if p2_entry_sec is None:
        p2_entry_sec = phase1_transition.get("malacrassActivationSec") or (phase2 or {}).get("activationSec")
        if p2_entry_sec is not None:
            p2_entry_basis = "malacrass_activation_fallback"
    p2_reached = p2_entry_sec is not None or bool((phase2 or {}).get("reached"))
    signal = cleanup_signal or {}

    if not p2_reached:
        cleanup_status = "unknown"
        p2_active = None
        cleanup_summary = "P2 미진입 · P2 진입 시 활성 구슬 판정 불가"
    elif signal.get("verified"):
        cleanup_status = str(signal.get("status") or "unknown")
        p2_active = safe_int(signal.get("remainingOrbs"))
        if cleanup_status == "success":
            cleanup_summary = f"P2 진입 시 활성 구슬 {int(p2_active or 0)}개"
            if signal.get("method") == "bulk_rupture":
                cleanup_summary += f" · {float(signal.get('sec')):.1f} 일괄 파열 {int(signal.get('removedOrbs') or 0)}개"
        elif cleanup_status == "failed" and p2_active is not None:
            cleanup_summary = f"P2 진입 시 활성 구슬 {int(p2_active)}개 · 정리 미완료"
        else:
            cleanup_summary = str(signal.get("summary") or "P2 진입 잔여 구슬 판정 불가")
    else:
        cleanup_status = "unknown"
        p2_active = None
        reason = str(signal.get("reason") or "cleanup_signal_unavailable")
        if reason == "collapse_before_p2_entry":
            cleanup_summary = "P2 진입 전 공대 붕괴 · 잔여 구슬 판정 제외"
        else:
            cleanup_summary = "P2 도달 · P2 진입 전 구슬 정리 신호 확인 불가"

    return {
        "enabled": bool(plan.get("enabled", True)),
        "role": plan.get("role") or "tank_handoff",
        "secondGuillotineResidual": residual,
        "tankHandoffMax": max_value,
        "handoffLoadStatus": load_status,
        "p2Reached": p2_reached,
        "p2EntrySec": round(float(p2_entry_sec), 3) if p2_entry_sec is not None else None,
        "p2EntryBasis": p2_entry_basis,
        "p2EntryCleanupStatus": cleanup_status,
        "p2EntryActiveOrbs": p2_active,
        "directCleanupSignalVerified": bool(signal.get("verified")),
        "cleanupMethod": signal.get("method"),
        "cleanupSignal": signal,
        "cleanupSummary": cleanup_summary,
        "sourceKind": source_kind,
        "evidenceLevel": EVIDENCE_VERIFIED if residual is not None else EVIDENCE_UNAVAILABLE,
        "note": "2차 단두대 잔존은 인계/정리 정보값. tankHandoffMax 초과만 경고하며, P2 진입 잔존은 검증된 최종 파열/장부 신호가 있을 때만 ✓/✗로 판정. 실제 처리 담당자는 별도 raidPlan과 로그 근거가 필요.",
    }

def _coiled_player_name(row: dict, *, side: str = "target") -> str | None:
    if side == "source":
        return row.get("sourceName")
    return row.get("targetName") or row.get("destName")


def _player_name_key(name) -> str:
    """Realm-insensitive comparison key for raidPlan/roster player names."""
    text = str(name or "").strip()
    if not text:
        return ""
    return text.split("-")[0].casefold()


def _coiled_tank_resolution(rows: list[dict], configured_players: Iterable[str] | None) -> dict:
    """Resolve effective tanks with raidPlan taking precedence over roster specs.

    Manual raidPlan names are authoritative.  When they are absent, local raw
    COMBATANT_INFO (or a normalized fixture/event carrying specID) is used to
    infer tank names from the shared TANK_SPECS table.  The result deliberately
    exposes both configured and inferred names so the UI can explain which
    source was used instead of silently guessing.
    """
    configured = [str(x).strip() for x in (configured_players or []) if str(x or "").strip()]
    st = _fast_state(rows)
    memo_key = tuple(configured)
    if st is not None and memo_key in st["tanks"]:
        return copy.deepcopy(st["tanks"][memo_key])
    out = _coiled_tank_resolution_uncached(rows, configured)
    if st is not None:
        st["tanks"][memo_key] = copy.deepcopy(out)
    return out


def _coiled_tank_resolution_uncached(rows: list[dict], configured: list[str]) -> dict:
    names_by_guid: dict[str, str] = {}
    specs_by_guid: dict[str, int] = {}
    direct_spec_names: dict[str, int] = {}
    for row in rows:
        sguid = str(row.get("sourceGUID") or "")
        tguid = str(row.get("destGUID") or "")
        sname = row.get("sourceName")
        tname = row.get("targetName") or row.get("destName")
        if sguid.startswith("Player-") and sname:
            names_by_guid.setdefault(sguid, str(sname))
        if tguid.startswith("Player-") and tname:
            names_by_guid.setdefault(tguid, str(tname))

        spec_id = safe_int(row.get("specID"))
        if spec_id is not None:
            if sguid.startswith("Player-"):
                specs_by_guid[sguid] = spec_id
            elif sname:
                direct_spec_names[str(sname)] = spec_id
        source_spec = safe_int(row.get("sourceSpecID"))
        if source_spec is not None:
            if sguid.startswith("Player-"):
                specs_by_guid[sguid] = source_spec
            elif sname:
                direct_spec_names[str(sname)] = source_spec
        target_spec = safe_int(row.get("targetSpecID"))
        if target_spec is not None:
            if tguid.startswith("Player-"):
                specs_by_guid[tguid] = target_spec
            elif tname:
                direct_spec_names[str(tname)] = target_spec

    inferred = []
    inferred_specs = {}
    for guid, spec_id in specs_by_guid.items():
        if spec_id not in TANK_SPECS:
            continue
        name = names_by_guid.get(guid)
        if name and _player_name_key(name) not in {_player_name_key(x) for x in inferred}:
            inferred.append(name)
            inferred_specs[name] = spec_id
    for name, spec_id in direct_spec_names.items():
        if spec_id in TANK_SPECS and _player_name_key(name) not in {_player_name_key(x) for x in inferred}:
            inferred.append(name)
            inferred_specs[name] = spec_id

    if configured:
        effective = configured
        source = "manual_raidplan"
    elif inferred:
        effective = inferred
        source = "roster_spec"
    else:
        effective = []
        source = "unavailable"
    return {
        "configured": configured,
        "inferred": inferred,
        "effective": effective,
        "source": source,
        "inferredSpecs": inferred_specs,
    }


def _coiled_add_instance_key(row: dict, *, side: str) -> tuple | None:
    if side == "source":
        aid = row.get("sourceID")
        inst = row.get("sourceInstance") or row.get("sourceInstanceID") or row.get("sourceInstanceId")
        guid = row.get("sourceGUID")
    else:
        aid = row.get("targetID")
        inst = row.get("targetInstance") or row.get("targetInstanceID") or row.get("targetInstanceId")
        guid = row.get("destGUID")
    if guid:
        return ("guid", str(guid))
    if aid not in (None, ""):
        return ("id", aid, inst)
    return None


def _coiled_gravebound_states(rows: list[dict], bomb_targets: list[dict], explosion_sec: float | None, deadline_sec: float) -> list[dict]:
    if explosion_sec is None:
        return []
    target_keys = {_target_key(x) for x in bomb_targets if _target_key(x) is not None}
    names = {_target_key(x): _coiled_player_name(x) for x in bomb_targets if _target_key(x) is not None}
    out = []
    frag_rows = _fast_rows_spell(rows, {GRAVEBOUND_FRAGMENT_DAMAGE_ID})
    death_rows = [x for x in _fast_rows_event(rows, {"death", "unit_died"})]
    last_sec = max((float(x.get("sec") or 0) for x in rows), default=0)
    duration = gravebound_duration_from_rows(rows)
    for key in target_keys:
        rel = [
            x for x in _fast_rows_spell(rows, {1286837})
            if _target_key(x) == key
            and float(explosion_sec) - 0.15 <= float(x.get("sec") or 0) <= float(explosion_sec) + float(deadline_sec) + 0.3
        ]
        applied = next((x for x in rel if _event_name(x) in {"applydebuff", "spell_aura_applied"}), None)
        removed = next((x for x in rel if _event_name(x) in {"removedebuff", "spell_aura_removed"}), None)
        stack_rows = [x for x in rel if _event_name(x) in {"removedebuffstack", "spell_aura_removed_dose"}]
        deadline = float(explosion_sec) + float(deadline_sec)
        if removed is not None and float(removed.get("sec") or 0) <= deadline:
            status = "success"
        elif applied is not None and max((float(x.get("sec") or 0) for x in rows), default=0) >= deadline:
            status = "failed"
        else:
            status = "unknown"
        # V5.2.3: 무덤구속은 영혼 3개(중첩 3)로 걸리고 11초 뒤 끝나면 사망. 영혼 파편을 밟을 때마다 1308330 피해와 중첩 감소가
        # 찍히고, 세 번째 파편에서 디버프가 풀린다 (10/25·9/29 신화 실측: 완료 169번 모두 10.4초 안 · 시간 초과 37번 모두 10.9~11.0초).
        # 그래서 「풀린 시각에 파편 피해가 같이 있음」 = 완료, 「11초에 풀림」 = 시간 초과, 「일찍 풀렸는데 그때 죽음」 = 다른 원인 사망.
        outcome, outcome_note = _coiled_gravebound_outcome(key, applied, removed, len(stack_rows),
                                                           frag_rows, death_rows, last_sec, duration)
        out.append({
            "player": names.get(key),
            "appliedSec": round(float(applied.get("sec") or 0), 3) if applied else None,
            "removedSec": round(float(removed.get("sec") or 0), 3) if removed else None,
            "removedStackEvents": len(stack_rows),
            "status": status,
            "deadlineSec": round(deadline, 3),
            "evidenceLevel": EVIDENCE_VERIFIED if status in {"success", "failed"} else EVIDENCE_UNAVAILABLE,
            "outcome": outcome,
            "outcomeNote": outcome_note,
            "durationSec": duration,
        })
    return sorted(out, key=lambda x: str(x.get("player") or ""))


GRAVEBOUND_DEBUFF_ID = 1286837
GRAVEBOUND_FRAGMENT_DAMAGE_ID = 1308330
GRAVEBOUND_DURATION_SEC = 11.0
GRAVEBOUND_DURATION_SEC_HOTFIX = 13.0   # 2026-11-06 신화 핫픽스: 영혼 파편이 사라지기까지 2초 늘어남


def gravebound_duration_from_rows(rows: list[dict]) -> float:
    """무덤구속 지속시간: 같은 판에서 11.5초 넘게 유지된 디버프가 하나라도 있으면 핫픽스 뒤(13초), 아니면 11초.
    (11초 판에서는 11.5초를 넘길 수 없음 · 예전 기록은 그대로 11초)"""
    open_at: dict = {}
    for x in _fast_rows_spell(rows, {GRAVEBOUND_DEBUFF_ID}):
        key, ev, sec = _target_key(x), _event_name(x), float(x.get("sec") or 0)
        if ev in {"applydebuff", "spell_aura_applied"}:
            open_at[key] = sec
        elif ev in {"removedebuff", "spell_aura_removed"} and key in open_at:
            if sec - open_at.pop(key) > GRAVEBOUND_DURATION_SEC + 0.5:
                return GRAVEBOUND_DURATION_SEC_HOTFIX
    return GRAVEBOUND_DURATION_SEC


def _coiled_gravebound_outcome(key, applied: dict | None, removed: dict | None, stack_events: int,
                               frag_rows: list[dict], death_rows: list[dict], last_sec: float,
                               duration: float = GRAVEBOUND_DURATION_SEC) -> tuple[str, str]:
    """V5.2.3: 무덤구속 한 명의 결과 = complete(영혼 3개 완료) / expired(시간 초과 · 11초, 11/6 핫픽스 뒤 13초) / died / unknown."""
    if applied is None:
        return "unknown", "걸린 시각 없음"
    a_sec = float(applied.get("sec") or 0)
    if removed is None:
        # 풀린 기록 없이 죽은 경우: 11초 무렵이면 시간 초과 사망, 그보다 이르면 다른 원인 사망
        d = sorted(float(x.get("sec") or 0) for x in death_rows if _target_key(x) == key and float(x.get("sec") or 0) >= a_sec - 0.1)
        if d:
            held = d[0] - a_sec
            if held >= duration - 0.5:
                return "expired", f"{held:.1f}초에 시간 초과 사망"
            return "died", f"{held:.1f}초에 다른 원인으로 사망"
        if last_sec >= a_sec + duration + 0.5:
            return "expired", f"풀린 기록 없이 {duration:.0f}초 지남"
        return "unknown", "전투가 먼저 끝남"
    r_sec = float(removed.get("sec") or 0)
    hit_at_remove = any(_target_key(x) == key and abs(float(x.get("sec") or 0) - r_sec) <= 0.15
                        and _event_name(x) in {"damage", "spell_damage", "absorbed", "spell_absorbed", "missed", "spell_missed"}
                        for x in frag_rows)
    held = r_sec - a_sec
    if hit_at_remove:
        return "complete", f"{held:.1f}초에 영혼 3개 완료"
    if held >= duration - 0.5:
        return "expired", f"{held:.1f}초 시간 초과"
    died = any(_target_key(x) == key and -0.2 <= float(x.get("sec") or 0) - r_sec <= 0.3 for x in death_rows)
    if died:
        return "died", f"{held:.1f}초에 다른 원인으로 사망"
    if not frag_rows and stack_events >= 2:
        # 보관본(.wcla)처럼 파편 피해가 없는 자료: 중첩 2번 감소 + 일찍 풀림이면 완료로 본다
        return "complete", f"{held:.1f}초에 풀림 (중첩 감소 {stack_events}번)"
    return "unknown", f"{held:.1f}초에 풀림 (파편 피해 없음)"


def _coiled_gravebound_summary(targets: list[dict]) -> dict:
    cnt = Counter(str(t.get("outcome") or "unknown") for t in targets)
    judged = cnt["complete"] + cnt["expired"]
    status = "failed" if cnt["expired"] else ("success" if judged and cnt["unknown"] == 0 else "unknown")
    return {"completeCount": cnt["complete"], "expiredCount": cnt["expired"], "diedCount": cnt["died"],
            "unknownCount": cnt["unknown"], "status": status,
            "expiredPlayers": [t.get("player") for t in targets if t.get("outcome") == "expired"],
            "evidenceLevel": EVIDENCE_VERIFIED if judged else EVIDENCE_UNAVAILABLE}


def _coiled_soulcoiler_vulnerability_states(
    rows: list[dict],
    add_instances: set[tuple],
    *,
    start_sec: float,
    explosion_sec: float | None,
    next_sec: float,
    pre_window_sec: float = 2.0,
    post_window_sec: float = 2.0,
    min_samples: int = 2,
) -> list[dict]:
    """Infer Gloombomb success from the Soulcoiler's mythic 99% DR transition.

    Retail logs do not reliably emit 1310883 as direct damage to the add.  The
    robust signal is the add's aggregate final/pre-mitigation damage ratio:
    protected ~=1%, vulnerable ~=100%.  Only a tight window around the bomb
    explosion is used, and ratios are computed from sums rather than individual
    hits so armor/absorbs/proc outliers cannot flip the verdict.
    """
    if explosion_sec is None:
        return []
    out = []
    significant_min = 200.0
    explosion_sec = float(explosion_sec)
    pre_lo = max(float(start_sec), explosion_sec - max(0.25, float(pre_window_sec)))
    post_hi = min(float(next_sec), explosion_sec + max(0.25, float(post_window_sec)))

    def aggregate(samples: list[tuple[float, float, float]]) -> tuple[float | None, float, float]:
        if not samples:
            return None, 0.0, 0.0
        final_sum = sum(max(0.0, x[1]) for x in samples)
        raw_sum = sum(max(0.0, x[2]) for x in samples)
        return ((final_sum / raw_sum) if raw_sum > 0 else None), final_sum, raw_sum

    window_rows = _fast_rows_between(rows, pre_lo, post_hi)  # V5.2.4: 창 밖 줄은 어차피 건너뜀
    for inst in sorted(add_instances, key=str):
        samples = []
        for row in window_rows:
            if not _is_damage(row) or safe_int(row.get("targetGameID")) != 261521:
                continue
            if _coiled_add_instance_key(row, side="target") != inst:
                continue
            sec = float(row.get("sec") or 0)
            if sec < pre_lo or sec > post_hi:
                continue
            try:
                amount = float(row.get("amount"))
                raw_amount = float(row.get("unmitigatedAmount"))
            except (TypeError, ValueError):
                continue
            if raw_amount < significant_min or amount < 0:
                continue
            samples.append((sec, amount, raw_amount))

        pre = [x for x in samples if pre_lo <= x[0] < explosion_sec]
        post = [x for x in samples if explosion_sec <= x[0] <= post_hi]
        pre_ratio, pre_final, pre_raw = aggregate(pre)
        post_ratio, post_final, post_raw = aggregate(post)
        pre_protected = len(pre) >= min_samples and pre_ratio is not None and pre_ratio <= 0.05
        post_vulnerable = len(post) >= min_samples and post_ratio is not None and post_ratio >= 0.50
        post_protected = len(post) >= min_samples and post_ratio is not None and post_ratio <= 0.05
        if pre_protected and post_vulnerable:
            status = "success"
        elif pre_protected and post_protected:
            status = "failed"
        else:
            status = "unknown"

        first_vulnerable = next((sec for sec, amount, raw in sorted(post) if raw > 0 and amount / raw >= 0.50), None)
        out.append({
            "instance": list(inst),
            "status": status,
            "explosionSec": round(explosion_sec, 3),
            "preWindowSec": round(float(pre_window_sec), 3),
            "postWindowSec": round(float(post_window_sec), 3),
            "preSampleCount": len(pre),
            "postSampleCount": len(post),
            "preAggregateDamageRatio": round(pre_ratio, 4) if pre_ratio is not None else None,
            "postAggregateDamageRatio": round(post_ratio, 4) if post_ratio is not None else None,
            "preFinalDamageSum": round(pre_final, 1),
            "preUnmitigatedDamageSum": round(pre_raw, 1),
            "postFinalDamageSum": round(post_final, 1),
            "postUnmitigatedDamageSum": round(post_raw, 1),
            "firstVulnerableSampleSec": round(first_vulnerable, 3) if first_vulnerable is not None else None,
            "transitionDelaySec": round(first_vulnerable - explosion_sec, 3) if first_vulnerable is not None else None,
            "evidenceLevel": EVIDENCE_VERIFIED if status in {"success", "failed"} else EVIDENCE_UNAVAILABLE,
            "note": "암영폭탄 폭발 전/후 짧은 창의 합산 실피해÷감소전피해가 ~1%→정상으로 전환되는지 판정.",
        })
    return out



def _coiled_player_position_index(rows: list[dict]) -> dict[str, list[tuple[float, float, float]]]:
    """Collect local raw player position samples from source and target snapshots."""
    out: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for row in rows:
        sec = float(row.get("sec") or 0)
        if str(row.get("sourceType") or "").lower() == "player" and row.get("sourceName") and row.get("sourceX") is not None and row.get("sourceY") is not None:
            out[str(row.get("sourceName"))].append((sec, float(row.get("sourceX")), float(row.get("sourceY"))))
        if _is_player_target(row) and _coiled_player_name(row) and row.get("targetX") is not None and row.get("targetY") is not None:
            out[str(_coiled_player_name(row))].append((sec, float(row.get("targetX")), float(row.get("targetY"))))
    for name, samples in out.items():
        # Deduplicate the many identical snapshots produced by simultaneous log rows.
        seen = set()
        clean = []
        for sample in sorted(samples):
            key = (round(sample[0], 3), round(sample[1], 3), round(sample[2], 3))
            if key in seen:
                continue
            seen.add(key)
            clean.append(sample)
        out[name] = clean
    return dict(out)


def _coiled_position_near(samples: list[tuple[float, float, float]], sec: float, *, max_age_sec: float = 1.5, max_future_sec: float = 0.20):
    if not samples:
        return None
    sec = float(sec)
    eligible = [x for x in samples if sec - max_age_sec <= x[0] <= sec + max_future_sec]
    if not eligible:
        return None
    # Prefer a sample at/before the event; a tiny future allowance covers the
    # damage snapshot emitted milliseconds after the aura application.
    prior = [x for x in eligible if x[0] <= sec + 0.02]
    chosen = max(prior, key=lambda x: x[0]) if prior else min(eligible, key=lambda x: abs(x[0] - sec))
    return {"sec": round(chosen[0], 3), "x": round(chosen[1], 3), "y": round(chosen[2], 3), "ageSec": round(sec - chosen[0], 3)}


def _coiled_movement_summary(samples: list[tuple[float, float, float]], start_sec: float, end_sec: float) -> dict:
    pts = [x for x in samples if float(start_sec) <= x[0] <= float(end_sec)]
    if len(pts) < 2:
        return {"sampleCount": len(pts), "pathDistance": None, "displacement": None, "evidenceLevel": EVIDENCE_UNAVAILABLE}
    # Collapse consecutive equal positions before summing path distance.
    compact = [pts[0]]
    for p in pts[1:]:
        if p[1] == compact[-1][1] and p[2] == compact[-1][2]:
            continue
        compact.append(p)
    if len(compact) < 2:
        return {"sampleCount": len(pts), "pathDistance": 0.0, "displacement": 0.0, "evidenceLevel": EVIDENCE_CANDIDATE}
    import math
    path = sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(compact, compact[1:]))
    disp = math.hypot(compact[-1][1] - compact[0][1], compact[-1][2] - compact[0][2])
    return {
        "sampleCount": len(pts),
        "pathDistance": round(path, 3),
        "displacement": round(disp, 3),
        "startSec": round(compact[0][0], 3),
        "endSec": round(compact[-1][0], 3),
        "evidenceLevel": EVIDENCE_CANDIDATE,
    }


def _coiled_active_fixation_players(rows: list[dict], fixation_ids: set[int], sec: float) -> set[str]:
    counts: Counter[str] = Counter()
    for row in sorted(_fast_rows_spell(rows, fixation_ids), key=lambda x: float(x.get("sec") or 0)):
        rsec = float(row.get("sec") or 0)
        if rsec > float(sec) + 0.001:
            break
        if safe_int(row.get("spellID") or row.get("abilityGameID")) not in fixation_ids or not _is_player_target(row):
            continue
        player = _coiled_player_name(row)
        if not player:
            continue
        ev = _event_name(row)
        if ev in {"spell_aura_applied", "applydebuff", "applybuff"}:
            counts[player] += 1
        elif ev in {"spell_aura_removed", "removedebuff", "removebuff"}:
            counts[player] = max(0, counts[player] - 1)
    return {name for name, count in counts.items() if count > 0}



def _coiled_distribution_stats(values: list[float]) -> dict:
    vals = sorted(float(x) for x in values if x is not None)
    if not vals:
        return {"count": 0, "min": None, "p10": None, "median": None, "p90": None, "max": None}

    def pct(q: float) -> float:
        if len(vals) == 1:
            return vals[0]
        pos = (len(vals) - 1) * float(q)
        lo = int(pos)
        hi = min(len(vals) - 1, lo + 1)
        frac = pos - lo
        return vals[lo] * (1.0 - frac) + vals[hi] * frac

    return {
        "count": len(vals),
        "min": round(vals[0], 3),
        "p10": round(pct(0.10), 3),
        "median": round(pct(0.50), 3),
        "p90": round(pct(0.90), 3),
        "max": round(vals[-1], 3),
    }


def _coiled_manifest_safe_spatial_baseline(
    rows: list[dict],
    fixation_ids: set[int],
    position_index: dict[str, list[tuple[float, float, float]]],
    *,
    start_sec: float,
    end_sec: float,
    resonance_secs: list[float],
    sample_step_sec: float = 0.5,
    resonance_exclusion_sec: float = 1.0,
) -> dict:
    """Candidate baseline from the same local wipe while fixation is active but no collision occurs.

    This deliberately uses *player* positions only as a proxy for trailing ghosts.  It is
    useful for comparing collision windows with ordinary movement in the same raid/pull,
    but it must not create a hard safe-distance rule.
    """
    if not position_index or end_sec <= start_sec:
        return {
            "source": "same_pull_non_resonance_fixation_windows",
            "safeSampleCount": 0,
            "hardThresholdPromoted": False,
            "evidenceLevel": EVIDENCE_UNAVAILABLE,
            "note": "local raw 위치 좌표가 없어 정상 구간 공간 기준을 계산할 수 없음.",
        }

    # Build active-fixation state segments once, rather than rescanning every row per sample.
    active = Counter({name: 1 for name in _coiled_active_fixation_players(rows, fixation_ids, start_sec)})
    changes = [
        x for x in _fast_rows_spell(rows, fixation_ids)
        if start_sec < float(x.get("sec") or 0) <= end_sec
        and _is_player_target(x)
        and _event_name(x) in {"spell_aura_applied", "applydebuff", "applybuff", "spell_aura_removed", "removedebuff", "removebuff"}
    ]
    changes.sort(key=lambda x: float(x.get("sec") or 0))

    segments: list[tuple[float, float, list[str]]] = []
    cursor = float(start_sec)
    i = 0
    while i < len(changes):
        sec = float(changes[i].get("sec") or 0)
        if sec > cursor:
            segments.append((cursor, sec, sorted(name for name, count in active.items() if count > 0)))
        while i < len(changes) and abs(float(changes[i].get("sec") or 0) - sec) <= 0.001:
            row = changes[i]
            player = _coiled_player_name(row)
            if player:
                ev = _event_name(row)
                if ev in {"spell_aura_applied", "applydebuff", "applybuff"}:
                    active[player] += 1
                elif ev in {"spell_aura_removed", "removedebuff", "removebuff"}:
                    active[player] = max(0, active[player] - 1)
            i += 1
        cursor = sec
    if cursor < end_sec:
        segments.append((cursor, float(end_sec), sorted(name for name, count in active.items() if count > 0)))

    import itertools
    import math

    sample_step_sec = max(0.20, float(sample_step_sec))
    exclusion = max(0.0, float(resonance_exclusion_sec))
    resonance_secs = [float(x) for x in resonance_secs]
    nearest_distances: list[float] = []
    all_pair_distances: list[float] = []
    samples = []

    for seg_start, seg_end, players in segments:
        if len(players) < 2 or seg_end - seg_start < 0.02:
            continue
        t = seg_start
        while t < seg_end - 0.001:
            if not any(abs(t - rs) <= exclusion for rs in resonance_secs):
                pos = {p: _coiled_position_near(position_index.get(p, []), t) for p in players}
                pairs = []
                for a, b in itertools.combinations(players, 2):
                    pa, pb = pos.get(a), pos.get(b)
                    if not pa or not pb:
                        continue
                    d = math.hypot(pa["x"] - pb["x"], pa["y"] - pb["y"])
                    pairs.append((d, a, b))
                    all_pair_distances.append(d)
                if pairs:
                    d, a, b = min(pairs, key=lambda x: x[0])
                    nearest_distances.append(d)
                    if len(samples) < 40:
                        samples.append({
                            "sec": round(t, 3),
                            "activeFixationCount": len(players),
                            "nearestPairPlayers": [a, b],
                            "nearestPairDistance": round(d, 3),
                        })
            t += sample_step_sec

    return {
        "source": "same_pull_non_resonance_fixation_windows",
        "sampleStepSec": round(sample_step_sec, 3),
        "resonanceExclusionSec": round(exclusion, 3),
        "safeSampleCount": len(nearest_distances),
        "nearestPairDistanceStats": _coiled_distribution_stats(nearest_distances),
        "allPairDistanceStats": _coiled_distribution_stats(all_pair_distances),
        "samplePreview": samples,
        "hardThresholdPromoted": False,
        "evidenceLevel": EVIDENCE_CANDIDATE if nearest_distances else EVIDENCE_UNAVAILABLE,
        "note": "같은 로컬 와이프에서 집착 활성 + Malevolent Resonance 비발생 구간을 정상 비교군으로 사용. 플레이어 좌표는 유령 proxy이므로 안전거리 임계값/개인 책임은 확정하지 않음.",
    }


def _coiled_collision_spatial_context(
    rows: list[dict],
    fixation_ids: set[int],
    position_index: dict[str, list[tuple[float, float, float]]],
    *,
    sec: float,
    players: list[str],
    trace_window_sec: float = 5.0,
) -> dict:
    """Candidate-only spatial context for Manifestation collisions.

    Malevolent Resonance identifies the affected players, but the ghosts lag
    behind their owners. Player coordinates are therefore useful for explaining
    movement into a collision, not for declaring a hard safe-distance threshold.
    """
    active = sorted(_coiled_active_fixation_players(rows, fixation_ids, sec))
    active_set = set(active)
    pair = list(players) if len(players) == 2 and all(p in active_set for p in players) else []
    positions = {p: _coiled_position_near(position_index.get(p, []), sec) for p in players}
    movement = {p: _coiled_movement_summary(position_index.get(p, []), sec - trace_window_sec, sec) for p in players}
    distance = None
    pair_distance_trace = []
    if len(pair) == 2 and positions.get(pair[0]) and positions.get(pair[1]):
        import math
        distance = math.hypot(
            positions[pair[0]]["x"] - positions[pair[1]]["x"],
            positions[pair[0]]["y"] - positions[pair[1]]["y"],
        )
        # Trace the two fixation owners for the preceding few seconds.  This is
        # the player-proxy equivalent of "did their ghost lanes converge?"
        # and stays candidate-only because ghosts trail their owners.
        step = 0.5
        t = float(sec) - float(trace_window_sec)
        while t <= float(sec) + 0.001:
            p1 = _coiled_position_near(position_index.get(pair[0], []), t, max_age_sec=0.9, max_future_sec=0.15)
            p2 = _coiled_position_near(position_index.get(pair[1], []), t, max_age_sec=0.9, max_future_sec=0.15)
            if p1 and p2:
                d = math.hypot(p1["x"] - p2["x"], p1["y"] - p2["y"])
                pair_distance_trace.append({"sec": round(t, 3), "beforeContactSec": round(float(sec) - t, 3), "distance": round(d, 3)})
            t += step
        # The 0.0s point must match playerDistanceAtContact, which may use the
        # resonance event's freshest target snapshot.  Earlier trace points
        # intentionally use a tighter age window.
        if distance is not None:
            final_point = {"sec": round(float(sec), 3), "beforeContactSec": 0.0, "distance": round(distance, 3)}
            if pair_distance_trace and abs(float(pair_distance_trace[-1].get("beforeContactSec") or 0.0)) <= 0.001:
                pair_distance_trace[-1] = final_point
            else:
                pair_distance_trace.append(final_point)
    likely = None
    if len(pair) == 2:
        m1 = movement.get(pair[0], {}).get("pathDistance")
        m2 = movement.get(pair[1], {}).get("pathDistance")
        if m1 is not None and m2 is not None:
            if m1 >= m2 * 1.5 and m1 - m2 >= 3.0:
                likely = pair[0]
            elif m2 >= m1 * 1.5 and m2 - m1 >= 3.0:
                likely = pair[1]
            elif m1 >= 3.0 and m2 >= 3.0:
                likely = "both"
            else:
                likely = "undetermined"
    return {
        "activeFixationPlayers": active,
        "pairPlayers": pair,
        "pairResolved": bool(pair),
        "playerPositionsAtContact": positions,
        "playerDistanceAtContact": round(distance, 3) if distance is not None else None,
        "traceWindowSec": float(trace_window_sec),
        "pairDistanceTrace": pair_distance_trace,
        "pairDistanceTraceStart": pair_distance_trace[0]["distance"] if pair_distance_trace else None,
        "pairDistanceTraceMin": min((x["distance"] for x in pair_distance_trace), default=None),
        "pairDistanceTraceEnd": pair_distance_trace[-1]["distance"] if pair_distance_trace else None,
        "movement": movement,
        "likelyMoverCandidate": likely,
        "safeDistanceThreshold": None,
        "evidenceLevel": EVIDENCE_CANDIDATE if players else EVIDENCE_UNAVAILABLE,
        "note": "플레이어 좌표는 유령 위치의 대리값. 유령이 뒤따라오므로 거리/이동은 원인 설명 후보이며 개인 책임 확정 근거가 아님. 같은 트라이의 비접촉 fixation 구간과 비교만 하며 hard 안전거리/개인 책임 기준은 미설정.",
    }



def _coiled_p2_failure_events(cycle: dict) -> list[dict]:
    """Return only verified P2 failures that are safe to use in causal ordering.

    B36.4 intentionally excludes target-count diagnostics plus the still-unpromoted
    mind-control / Gravebound interpretations.  Those signals remain visible in the
    cycle payload, but they cannot become "첫 실수" until their success/failure
    contract is explicitly promoted.
    """
    failures: list[dict] = []
    idx = int(cycle.get("index") or 0)
    gb = cycle.get("gloombomb") or {}
    bomb_parts = []
    bomb_subkinds = []
    removed = int(gb.get("soulcoilerVulnerabilityRemovedCount") or 0)
    expected = int(gb.get("soulcoilerExpectedCount") or 0)
    retained = int(gb.get("soulcoilerVulnerabilityRetainedCount") or 0)
    extra = int(gb.get("extraHitCount") or 0)
    if retained > 0 or "gloombomb_soulcoiler_hit_missing" in (cycle.get("flags") or []):
        bomb_parts.append(f"영혼살무사 뎀감 해제 {removed}/{expected or '?'}")
        bomb_subkinds.append("gloombomb_soulcoiler_hit_missing")
    if extra > 0:
        bomb_parts.append(f"추가 피격 {extra}명")
        bomb_subkinds.append("gloombomb_extra_hit")
    if gb.get("firstInterruptOrderOk") is False:
        delta = gb.get("firstInterruptToDamageSec")
        bomb_parts.append(f"1차단 전 폭발 ({float(delta):+.1f}초)" if delta is not None else "1차단 전 폭발")
        bomb_subkinds.append("gloombomb_before_first_interrupt")
    if bomb_parts:
        sec = gb.get("damageSec") if gb.get("damageSec") is not None else gb.get("applySec")
        if sec is not None:
            failures.append({
                "kind": "gloombomb_failure",
                "subkinds": bomb_subkinds,
                "cycleIndex": idx,
                "sec": round(float(sec), 3),
                "label": "암영폭탄 처리 실패",
                "summary": " · ".join(bomb_parts),
                "evidenceLevel": EVIDENCE_VERIFIED,
                # Only splash/extra-hit can directly deal 1310883 raid damage.
                # A retained 99% DR state by itself is not a direct-death proof.
                "directSpellIDs": [1310883] if extra > 0 else [],
            })

    for ep in (cycle.get("wail") or {}).get("failureEpisodes") or []:
        if ep.get("sec") is None:
            continue
        failures.append({
            "kind": "wail_interrupt_failure",
            "subkinds": ["wail_interrupt_failure"],
            "cycleIndex": idx,
            "sec": round(float(ep.get("sec")), 3),
            "label": "공포의 통곡 미차단",
            "summary": f"플레이어 {int(ep.get('hitCount') or 0)}명 공포",
            "players": list(ep.get("players") or []),
            "evidenceLevel": EVIDENCE_VERIFIED,
            "directSpellIDs": [],
        })

    for ep in (cycle.get("manifestCollision") or {}).get("episodes") or []:
        if ep.get("sec") is None:
            continue
        deaths = int(ep.get("linkedDeathCount") or 0)
        failures.append({
            "kind": "manifest_collision",
            "subkinds": ["manifest_collision"],
            "cycleIndex": idx,
            "sec": round(float(ep.get("sec")), 3),
            "label": "현신 접촉",
            "summary": f"대상 {int(ep.get('targetCount') or 0)}명" + (f" · 직후 사망 {deaths}명" if deaths else ""),
            "players": list(ep.get("players") or []),
            "linkedDeaths": list(ep.get("linkedDeaths") or []),
            "linkedDeathCount": deaths,
            "evidenceLevel": EVIDENCE_VERIFIED,
            "directSpellIDs": [1310744, 1312132],
        })

    ss = cycle.get("soulSever") or {}
    if int(ss.get("nonTankHitCount") or 0) > 0 and ss.get("castSec") is not None:
        failures.append({
            "kind": "soul_sever_non_tank_hit",
            "subkinds": ["soul_sever_non_tank_hit"],
            "cycleIndex": idx,
            "sec": round(float(ss.get("castSec")), 3),
            "label": "Soul Sever 비탱커 피격",
            "summary": ", ".join(ss.get("nonTankHitPlayers") or []),
            "players": list(ss.get("nonTankHitPlayers") or []),
            "evidenceLevel": EVIDENCE_VERIFIED,
            "directSpellIDs": [1286620],
        })

    overlap = cycle.get("priorAddOverlap") or {}
    if overlap.get("detected"):
        secs = [float(x) for x in (overlap.get("wailSecs") or []) if x is not None]
        if secs:
            failures.append({
                "kind": "prior_add_overlap",
                "subkinds": ["prior_add_overlap"],
                "cycleIndex": idx,
                "sec": round(min(secs), 3),
                "label": "이전 차단쫄 다음 웨이브 중첩",
                "summary": "이전 Soulcoiler가 다음 웨이브 이후에도 통곡 시전 시작",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "directSpellIDs": [],
            })

    # Stable tie-break keeps one bomb event together and makes output deterministic.
    priority = {
        "gloombomb_failure": 10,
        "wail_interrupt_failure": 20,
        "manifest_collision": 30,
        "soul_sever_non_tank_hit": 40,
        "prior_add_overlap": 50,
    }
    failures.sort(key=lambda x: (float(x.get("sec") or 0), priority.get(str(x.get("kind")), 99)))
    return failures


def _coiled_p2_cycle_line(cycle: dict) -> str:
    """Compact WWF-style one-line status using only promoted P2 judgments."""
    parts = [f"P2 {int(cycle.get('index') or 0)}사이클"]
    gb = cycle.get("gloombomb") or {}
    expected = int(gb.get("soulcoilerExpectedCount") or 0)
    known = int(gb.get("soulcoilerVulnerabilityKnownCount") or 0)
    removed = int(gb.get("soulcoilerVulnerabilityRemovedCount") or 0)
    retained = int(gb.get("soulcoilerVulnerabilityRetainedCount") or 0)
    if retained > 0:
        parts.append(f"폭탄 {removed}/{expected or '?'} ❌")
    elif expected > 0 and known >= expected:
        parts.append(f"폭탄 {removed}/{expected} {'✅' if removed == expected else '❌'}")
    else:
        parts.append("폭탄 확인 불가")
    extra = int(gb.get("extraHitCount") or 0)
    if extra:
        parts.append(f"추가피격 {extra}명 ❌")
    if gb.get("firstInterruptToDamageSec") is not None:
        delta = float(gb.get("firstInterruptToDamageSec"))
        parts.append(f"1차단→폭탄 {delta:+.1f}초 {'✅' if gb.get('firstInterruptOrderOk') is not False else '❌'}")

    wail = cycle.get("wail") or {}
    wfail = int(wail.get("failureEpisodeCount") or 0)
    if wfail:
        parts.append(f"통곡 미차단 {wfail}회 ❌")
    elif int(wail.get("castCount") or 0) > 0 or int(wail.get("interruptCount") or 0) > 0:
        parts.append("통곡 ✅")
    else:
        parts.append("통곡 확인 불가")

    collision = cycle.get("manifestCollision") or {}
    cc = int(collision.get("episodeCount") or 0)
    if cc:
        deaths = int(collision.get("linkedDeathCount") or 0)
        parts.append(f"현신 접촉 이벤트 {cc}회 ❌" + (f" · 연계사망 {deaths}명" if deaths else ""))
    elif (cycle.get("soulSever") or {}).get("castSec") is not None or cycle.get("nextCycleSec") is not None:
        parts.append("현신 접촉 없음 ✅")
    else:
        parts.append("현신 확인 불가")

    if (cycle.get("priorAddOverlap") or {}).get("detected"):
        parts.append("이전쫄 중첩 ❌")
    return " · ".join(parts)


def _coiled_p2_collapse_link(cycles: list[dict], collapse_snapshot: dict | None, cfg: dict) -> dict:
    """Separate temporal first-failure ordering from evidence-backed collapse linkage."""
    failures = [f for c in cycles for f in ((c.get("judgment") or {}).get("failures") or [])]
    failures.sort(key=lambda x: float(x.get("sec") or 0))
    first = failures[0] if failures else None
    if not collapse_snapshot or not collapse_snapshot.get("significantCluster"):
        return {
            "firstMistake": first,
            "collapseDirect": None,
            "collapsePreceding": None,
            # The generic snapshot keeps a fallback first-death time for other
            # diagnostics, but B36.4's "붕괴 직결" exists only for the explicit
            # >=4 deaths / 1 sec collapse contract.  Do not leak that fallback
            # death into the P2 causal summary.
            "collapseSec": None,
            "fallbackDeathSec": (collapse_snapshot or {}).get("collapseSec"),
            "collapseDeathCount1s": int((collapse_snapshot or {}).get("collapseCluster1s") or 0),
            "status": "unavailable",
            "evidenceLevel": EVIDENCE_UNAVAILABLE,
            "reason": "no_significant_4_deaths_in_1s",
            "sameEvent": False,
        }

    collapse_sec = float(collapse_snapshot.get("collapseSec") or 0)
    lead_spell = safe_int(((collapse_snapshot.get("collapseLeadFinalHit") or {}).get("spellID")))
    max_direct = float(cfg.get("collapseDirectMaxLookbackSec", 6.0))
    max_preceding = float(cfg.get("collapsePrecedingMaxLookbackSec", 3.0))
    eligible = [f for f in failures if f.get("sec") is not None and 0 <= collapse_sec - float(f.get("sec")) <= max_direct]

    direct = None
    collapse_deaths = int(collapse_snapshot.get("collapseCluster1s") or 0)

    # B36.4.2 deliberately requires evidence for the *collapse cluster*, not
    # merely the lead death.  One player dying to a mechanic immediately before
    # a 4-death/1s cluster proves chronology but does not prove that mechanic
    # caused the raid collapse.  A direct-spell match therefore needs a same-
    # spell raid burst covering at least the observed collapse-death count.
    if lead_spell is not None and collapse_deaths > 0:
        burst_support = any(
            safe_int(x.get("spellID")) == lead_spell
            and int(x.get("targetCount") or 0) >= collapse_deaths
            for x in (collapse_snapshot.get("collapseBurstChainCandidates") or [])
        )
        if burst_support:
            matches = [f for f in eligible if lead_spell in {safe_int(x) for x in (f.get("directSpellIDs") or [])}]
            if matches:
                direct = max(matches, key=lambda x: float(x.get("sec") or 0))

    # Resonance provides player-level death links.  Promote it as collapse-direct
    # only when the same collision episode directly accounts for the full 1-second
    # collapse cluster.  Partial overlap remains "직전 실패" (temporal evidence).
    if direct is None and collapse_deaths > 0:
        death_lo, death_hi = collapse_sec - 0.05, collapse_sec + 1.05
        collision_matches = []
        for f in eligible:
            if f.get("kind") != "manifest_collision":
                continue
            linked = [
                x for x in (f.get("linkedDeaths") or [])
                if death_lo <= float(x.get("deathSec") or -999) <= death_hi
            ]
            if len(linked) >= collapse_deaths:
                collision_matches.append(f)
        if collision_matches:
            direct = max(collision_matches, key=lambda x: float(x.get("sec") or 0))

    preceding = None
    prior = [f for f in failures if f.get("sec") is not None and 0 <= collapse_sec - float(f.get("sec")) <= max_preceding]
    if prior:
        preceding = max(prior, key=lambda x: float(x.get("sec") or 0))

    same = bool(first and direct and first.get("kind") == direct.get("kind") and first.get("sec") == direct.get("sec"))
    return {
        "firstMistake": first,
        "collapseDirect": direct,
        "collapsePreceding": preceding,
        "collapseSec": round(collapse_sec, 3),
        "collapseDeathCount1s": int(collapse_snapshot.get("collapseCluster1s") or 0),
        "collapseLeadSpellID": lead_spell,
        "status": "linked" if direct else "unavailable",
        "evidenceLevel": EVIDENCE_VERIFIED if direct else EVIDENCE_UNAVAILABLE,
        "reason": None if direct else "no_direct_causal_evidence",
        "sameEvent": same,
        "note": "첫 실수는 시간 순서. 붕괴 직결은 1초 붕괴 군집 전체를 설명하는 직접 피해/사망 연결 근거가 있을 때만 승격하며, 일부 사망만 겹치면 직전 실패(선후관계)로만 표시.",
    }


def _coiled_p2_summary(cycles: list[dict], collapse_snapshot: dict | None, cfg: dict) -> dict:
    link = _coiled_p2_collapse_link(cycles, collapse_snapshot, cfg)
    first = link.get("firstMistake")
    direct = link.get("collapseDirect")
    primary_cycle = None
    if direct:
        primary_cycle = next((c for c in cycles if int(c.get("index") or 0) == int(direct.get("cycleIndex") or -1)), None)
    if primary_cycle is None and first:
        primary_cycle = next((c for c in cycles if int(c.get("index") or 0) == int(first.get("cycleIndex") or -1)), None)
    if primary_cycle is None and cycles:
        primary_cycle = cycles[0]
    return {
        "cycleLines": [c.get("summaryLine") for c in cycles if c.get("summaryLine")],
        "primaryCycleIndex": (primary_cycle or {}).get("index"),
        "oneLine": (primary_cycle or {}).get("summaryLine") or "P2 사이클 판정 자료 없음",
        "firstMistake": first,
        "collapseDirect": direct,
        "collapsePreceding": link.get("collapsePreceding"),
        "collapseSec": link.get("collapseSec"),
        "collapseDeathCount1s": link.get("collapseDeathCount1s"),
        "collapseStatus": link.get("status"),
        "sameEvent": bool(link.get("sameEvent")),
        "evidenceLevel": EVIDENCE_VERIFIED if first or direct else EVIDENCE_UNAVAILABLE,
        "note": link.get("note") or "첫 실수와 붕괴 직결을 분리해 표시.",
    }


def _coiled_p2_cycles(rows: list[dict], rule: dict, strategy: dict, *, phase2_activation_sec: float | None, phase2_end_sec: float | None, source_kind: str, collapse_snapshot: dict | None = None) -> dict:
    cfg = (strategy or {}).get("phase2Analysis") or {}
    expected_mc = int(cfg.get("expectedMindControlTargets", 4))
    expected_adds = int(cfg.get("expectedSoulcoilersPerCycle", 2))
    expected_bombs = int(cfg.get("expectedGloombombTargets", 4))
    deadline = float(cfg.get("graveboundDeadlineSec", 15.0))
    fixation_window = float(cfg.get("manifestFixationRemovalWindowSec", 0.25))
    overlap_grace = float(cfg.get("priorAddOverlapGraceSec", 0.05))
    bomb_pre_window = float(cfg.get("soulcoilerVulnerabilityPreWindowSec", 2.0))
    bomb_post_window = float(cfg.get("soulcoilerVulnerabilityPostWindowSec", 2.0))
    bomb_min_samples = int(cfg.get("soulcoilerVulnerabilityMinSamples", 2))
    collision_trace_window = float(cfg.get("manifestCollisionTraceWindowSec", 5.0))
    safe_sample_step = float(cfg.get("manifestSafeSampleStepSec", 0.5))
    safe_collision_exclusion = float(cfg.get("manifestSafeCollisionExclusionSec", 1.0))

    mechanics = rule.get("mechanics") or {}
    soulcoiler_damage_complete = str(source_kind or "").lower() in {"raw", "archive"} or any(
        _event_name(x) == "probe_stream_coverage"
        and x.get("coverage") == "soulcoiler_damage_taken"
        and x.get("coverageVersion") == "damage_taken_source_filter_v2"
        for x in rows
    )
    spirit = mechanics.get("spiritcackle") or {}
    add_summon_ids = {safe_int(x) for x in (spirit.get("summonSpellIDs") or []) if safe_int(x) is not None}
    add_npcs = {safe_int(x) for x in (spirit.get("npcIDs") or [261521]) if safe_int(x) is not None}
    summon_rows = [x for x in _fast_rows_event(rows, {"summon", "spell_summon"}) if safe_int(x.get("spellID") or x.get("abilityGameID")) in add_summon_ids and safe_int(x.get("targetGameID")) in add_npcs]
    if phase2_activation_sec is not None:
        summon_rows = [x for x in summon_rows if float(x.get("sec") or 0) >= float(phase2_activation_sec) - 0.5]
    if phase2_end_sec is not None:
        summon_rows = [x for x in summon_rows if float(x.get("sec") or 0) < float(phase2_end_sec)]
    summon_groups = _group_times(summon_rows, max_gap=0.35)

    dread_spec = mechanics.get("dreadmarch") or {}
    dread_ids = {safe_int(x) for x in (dread_spec.get("debuffSpellIDs") or []) if safe_int(x) is not None}
    bomb_spec = mechanics.get("gloombomb") or {}
    bomb_debuff_ids = {safe_int(x) for x in (bomb_spec.get("debuffSpellIDs") or []) if safe_int(x) is not None}
    bomb_damage_ids = {safe_int(x) for x in (bomb_spec.get("damageSpellIDs") or []) if safe_int(x) is not None}
    wail_ids = {safe_int(x) for x in ((mechanics.get("wailOfTerror") or {}).get("spellIDs") or []) if safe_int(x) is not None}
    soul_ids = {safe_int(x) for x in ((mechanics.get("soulSever") or {}).get("spellIDs") or []) if safe_int(x) is not None}
    fixation_ids = {safe_int(x) for x in ((mechanics.get("manifestationOfDread") or {}).get("fixationSpellIDs") or []) if safe_int(x) is not None}
    resonance_spec = mechanics.get("malevolentResonance") or {}
    resonance_aura_ids = {safe_int(x) for x in (resonance_spec.get("auraSpellIDs") or []) if safe_int(x) is not None}
    resonance_damage_ids = {safe_int(x) for x in (resonance_spec.get("damageSpellIDs") or []) if safe_int(x) is not None}
    resonance_death_link_window = float(cfg.get("manifestCollisionDeathLinkWindowSec", 0.75))
    phase2_plan = (((strategy or {}).get("raidPlan") or {}).get("phase2") or {})
    tank_resolution = _coiled_tank_resolution(rows, phase2_plan.get("tankPlayers") or [])
    tank_players = list(tank_resolution.get("effective") or [])
    tank_keys = {_player_name_key(x) for x in tank_players if _player_name_key(x)}
    position_index = _coiled_player_position_index(rows) if str(source_kind or "").lower() == "raw" else {}

    cycles = []
    for ci, group in enumerate(summon_groups, start=1):
        start = min(float(x.get("sec") or 0) for x in group)
        next_start = min(float(x.get("sec") or 0) for x in summon_groups[ci]) if ci < len(summon_groups) else (float(phase2_end_sec) if phase2_end_sec is not None else max((float(x.get("sec") or 0) for x in rows), default=start) + 0.001)
        add_instances = {_coiled_add_instance_key(x, side="target") for x in group if _coiled_add_instance_key(x, side="target") is not None}
        ordered_add_instances = []
        for summon in sorted(group, key=lambda x: (float(x.get("sec") or 0), str(_coiled_add_instance_key(x, side="target") or ""))):
            inst = _coiled_add_instance_key(summon, side="target")
            if inst is not None and inst not in ordered_add_instances:
                ordered_add_instances.append(inst)
        add_slot_by_instance = {inst: i + 1 for i, inst in enumerate(ordered_add_instances)}

        dread_candidates = [x for x in _fast_rows_spell(rows, dread_ids) if _is_aura_apply(x) and start - 15.0 <= float(x.get("sec") or 0) <= start]
        dread_groups = _group_times(dread_candidates, max_gap=0.25)
        dread_group = dread_groups[-1] if dread_groups else []
        mc_targets = sorted({_coiled_player_name(x) for x in dread_group if _coiled_player_name(x)})

        bomb_candidates = [x for x in _fast_rows_spell(rows, bomb_debuff_ids) if _is_aura_apply(x) and start <= float(x.get("sec") or 0) < next_start]
        bomb_groups = _group_times(bomb_candidates, max_gap=0.25)
        bomb_group = bomb_groups[0] if bomb_groups else []
        bomb_sec = min((float(x.get("sec") or 0) for x in bomb_group), default=None)
        bomb_targets = sorted({_coiled_player_name(x) for x in bomb_group if _coiled_player_name(x)})
        bomb_target_keys = {_target_key(x) for x in bomb_group if _target_key(x) is not None}

        damage_group = []
        if bomb_sec is not None:
            dmg_candidates = [x for x in _fast_rows_spell(rows, bomb_damage_ids) if _is_damage(x) and _is_player_target(x) and bomb_sec + 3.0 <= float(x.get("sec") or 0) <= bomb_sec + 7.0]
            dmg_groups = _group_times(dmg_candidates, max_gap=0.12)
            if dmg_groups:
                damage_group = min(dmg_groups, key=lambda g: abs(float(g[0].get("sec") or 0) - (bomb_sec + 5.0)))
        damage_sec = min((float(x.get("sec") or 0) for x in damage_group), default=None)
        damage_keys = {_target_key(x) for x in damage_group if _target_key(x) is not None}
        damage_targets = sorted({_coiled_player_name(x) for x in damage_group if _coiled_player_name(x)})
        extra_keys = damage_keys - bomb_target_keys
        extra_names = sorted({_coiled_player_name(x) for x in damage_group if _target_key(x) in extra_keys and _coiled_player_name(x)})
        bomb_add_hits = []
        if bomb_sec is not None:
            bomb_add_hits = [
                x for x in _fast_rows_spell(rows, bomb_damage_ids)
                if _is_damage(x)
                and safe_int(x.get("targetGameID")) == 261521
                and bomb_sec + 3.0 <= float(x.get("sec") or 0) <= bomb_sec + 7.0
            ]
        bomb_add_instances = {_coiled_add_instance_key(x, side="target") for x in bomb_add_hits if _coiled_add_instance_key(x, side="target") is not None}
        vulnerability_states = _coiled_soulcoiler_vulnerability_states(
            rows, add_instances, start_sec=start, explosion_sec=damage_sec, next_sec=next_start,
            pre_window_sec=bomb_pre_window, post_window_sec=bomb_post_window, min_samples=bomb_min_samples,
        )
        for state in vulnerability_states:
            inst = tuple(state.get("instance") or [])
            state["soulcoilerIndex"] = add_slot_by_instance.get(inst)
        vulnerability_success = [x for x in vulnerability_states if x.get("status") == "success"]
        vulnerability_failed = [x for x in vulnerability_states if x.get("status") == "failed"]
        vulnerability_known = [x for x in vulnerability_states if x.get("status") in {"success", "failed"}]

        wail_casts = [x for x in _fast_rows_spell(rows, wail_ids) if _event_name(x) in {"begincast", "spell_cast_start"} and start <= float(x.get("sec") or 0) < next_start]
        wail_successes = [x for x in _fast_rows_spell(rows, wail_ids) if _is_completed_cast(x) and start <= float(x.get("sec") or 0) < next_start]
        wail_debuffs = [x for x in _fast_rows_spell(rows, wail_ids) if _is_aura_apply(x) and _is_player_target(x) and start <= float(x.get("sec") or 0) < next_start]
        fear_groups = _group_times(wail_debuffs, max_gap=0.20)
        fear_episodes = []
        for fg in fear_groups:
            fear_episodes.append({
                "sec": round(min(float(x.get("sec") or 0) for x in fg), 3),
                "hitCount": len({_target_key(x) for x in fg if _target_key(x) is not None}),
                "players": sorted({_coiled_player_name(x) for x in fg if _coiled_player_name(x)}),
                "evidenceLevel": EVIDENCE_VERIFIED,
            })

        interrupts = [
            x for x in _fast_rows_event(rows, {"interrupt", "spell_interrupt"})
            if safe_int(x.get("extraSpellID") or x.get("extraAbilityGameID")) in wail_ids
            and start <= float(x.get("sec") or 0) < next_start
        ]
        interrupt_rows = [{
            "sec": round(float(x.get("sec") or 0), 3),
            "player": x.get("sourceName"),
            "target": x.get("targetName") or x.get("destName"),
            "targetInstance": x.get("targetInstance") or x.get("targetInstanceID") or x.get("targetInstanceId"),
            "interruptSpellID": safe_int(x.get("spellID") or x.get("abilityGameID")),
            "interruptedSpellID": safe_int(x.get("extraSpellID") or x.get("extraAbilityGameID")),
            "evidenceLevel": EVIDENCE_VERIFIED,
        } for x in interrupts]
        first_interrupt = min(interrupt_rows, key=lambda x: float(x.get("sec") or 0)) if interrupt_rows else None
        first_interrupt_sec = float(first_interrupt.get("sec")) if first_interrupt and first_interrupt.get("sec") is not None else None
        bomb_after_first_interrupt_sec = (round(float(damage_sec) - first_interrupt_sec, 3) if damage_sec is not None and first_interrupt_sec is not None else None)
        bomb_first_interrupt_order_ok = (bomb_after_first_interrupt_sec >= -0.02) if bomb_after_first_interrupt_sec is not None else None

        # Mythic Manifestation collision. 1310744 is the collision aura and
        # 1312132 is its 1-second damage tick.  WCL attributes these rows to
        # Malacrass rather than the Manifestation NPC, so spell IDs are the
        # authoritative signal.
        resonance_applies = [
            x for x in _fast_rows_spell(rows, resonance_aura_ids)
            if _is_aura_apply(x) and _is_player_target(x)
            and start <= float(x.get("sec") or 0) < next_start
        ]
        resonance_damage = [
            x for x in _fast_rows_spell(rows, resonance_damage_ids)
            if _is_damage(x) and _is_player_target(x)
            and start <= float(x.get("sec") or 0) < next_start
        ]
        resonance_groups = _group_times(resonance_applies, max_gap=0.25)
        resonance_episodes = []
        death_rows = [x for x in _fast_rows_event(rows, {"death", "unit_died"}) if _is_player_target(x) and start <= float(x.get("sec") or 0) < next_start]
        for rg in resonance_groups:
            rsec = min(float(x.get("sec") or 0) for x in rg)
            rkeys = {_target_key(x) for x in rg if _target_key(x) is not None}
            rplayers = sorted({_coiled_player_name(x) for x in rg if _coiled_player_name(x)})
            rdmg = [
                x for x in resonance_damage
                if _target_key(x) in rkeys and rsec - 0.05 <= float(x.get("sec") or 0) <= rsec + 5.5
            ]
            linked = []
            for key in rkeys:
                pdmg = [x for x in rdmg if _target_key(x) == key]
                if not pdmg:
                    continue
                last_dmg = max(float(x.get("sec") or 0) for x in pdmg)
                death = next((x for x in death_rows if _target_key(x) == key and 0 <= float(x.get("sec") or 0) - last_dmg <= resonance_death_link_window), None)
                if death is not None:
                    linked.append({
                        "player": _coiled_player_name(death),
                        "deathSec": round(float(death.get("sec") or 0), 3),
                        "lastResonanceDamageSec": round(last_dmg, 3),
                    })
            spatial = _coiled_collision_spatial_context(
                rows, fixation_ids, position_index, sec=rsec, players=rplayers,
                trace_window_sec=collision_trace_window,
            ) if position_index else {
                "activeFixationPlayers": [], "pairPlayers": [], "pairResolved": False,
                "playerPositionsAtContact": {}, "playerDistanceAtContact": None,
                "traceWindowSec": collision_trace_window, "pairDistanceTrace": [],
                "pairDistanceTraceStart": None, "pairDistanceTraceMin": None, "pairDistanceTraceEnd": None, "movement": {},
                "likelyMoverCandidate": None, "safeDistanceThreshold": None,
                "evidenceLevel": EVIDENCE_UNAVAILABLE,
                "note": "WCL reference events에는 local raw 위치 좌표가 없어 공간 판정 불가.",
            }
            resonance_episodes.append({
                "sec": round(rsec, 3),
                "players": rplayers,
                "targetCount": len(rkeys),
                "damageEventCount": len(rdmg),
                "linkedDeaths": linked,
                "linkedDeathCount": len(linked),
                "spatial": spatial,
                "evidenceLevel": EVIDENCE_VERIFIED,
            })

        manifest_spatial_baseline = _coiled_manifest_safe_spatial_baseline(
            rows, fixation_ids, position_index,
            start_sec=start, end_sec=next_start,
            resonance_secs=[float(x.get("sec") or 0) for x in resonance_episodes],
            sample_step_sec=safe_sample_step,
            resonance_exclusion_sec=safe_collision_exclusion,
        ) if position_index else {
            "source": "same_pull_non_resonance_fixation_windows",
            "safeSampleCount": 0, "hardThresholdPromoted": False,
            "evidenceLevel": EVIDENCE_UNAVAILABLE,
            "note": "WCL reference events에는 local raw 위치 좌표가 없어 정상 구간 공간 기준을 계산할 수 없음.",
        }

        safe_median = ((manifest_spatial_baseline.get("nearestPairDistanceStats") or {}).get("median"))
        for episode in resonance_episodes:
            spatial = episode.get("spatial") or {}
            contact = spatial.get("playerDistanceAtContact")
            comparison = {
                "safeNearestPairMedian": safe_median,
                "contactPlayerProxyDistance": contact,
                "contactVsSafeMedianRatio": round(float(contact) / float(safe_median), 3) if contact is not None and safe_median not in (None, 0) else None,
                "evidenceLevel": EVIDENCE_CANDIDATE if contact is not None and safe_median not in (None, 0) else EVIDENCE_UNAVAILABLE,
                "note": "접촉 순간 player-proxy 거리와 같은 pull 정상 구간의 최근접 fixation-pair 중앙값 비교. hard threshold 아님.",
            }
            spatial["samePullBaselineComparison"] = comparison

        soul_casts = [x for x in _fast_rows_spell(rows, soul_ids) if _is_completed_cast(x) and start <= float(x.get("sec") or 0) < next_start]
        soul = soul_casts[0] if soul_casts else None
        soul_sec = float(soul.get("sec") or 0) if soul else None
        fixation_removed = []
        if soul_sec is not None:
            fixation_removed = [x for x in _fast_rows_spell(rows, fixation_ids) if _event_name(x) in {"removedebuff", "spell_aura_removed"} and soul_sec - 0.05 <= float(x.get("sec") or 0) <= soul_sec + fixation_window]
        soul_player_hits = [
            x for x in _fast_rows_spell(rows, {1286620})
            if _is_damage(x) and _is_player_target(x)
            and start <= float(x.get("sec") or 0) < next_start
        ]
        soul_hit_players = sorted({_coiled_player_name(x) for x in soul_player_hits if _coiled_player_name(x)})
        non_tank_hits = sorted([
            x for x in soul_hit_players
            if tank_keys and _player_name_key(x) not in tank_keys
        ])

        gravebound = _coiled_gravebound_states(rows, bomb_group, damage_sec, deadline)

        overlap_rows = []
        if ci < len(summon_groups):
            boundary = next_start + overlap_grace
            overlap_rows = [x for x in wail_casts if float(x.get("sec") or 0) >= boundary and _coiled_add_instance_key(x, side="source") in add_instances]
            # wail_casts was clipped at next_start, so explicitly search beyond the boundary.
            overlap_rows = [
                x for x in _fast_rows_spell(rows, wail_ids)
                if _event_name(x) in {"begincast", "spell_cast_start"}
                and _coiled_add_instance_key(x, side="source") in add_instances
                and boundary <= float(x.get("sec") or 0) < min(next_start + 8.0, float(summon_groups[ci][0].get("sec") or next_start) + 8.0)
            ]

        flags = []
        if len(group) != expected_adds:
            flags.append("soulcoiler_count")
        if dread_group and len(mc_targets) != expected_mc:
            flags.append("mind_control_target_count")
        if bomb_group and len(bomb_targets) != expected_bombs:
            flags.append("gloombomb_target_count")
        if extra_keys:
            flags.append("gloombomb_extra_hit")
        if bomb_first_interrupt_order_ok is False:
            flags.append("gloombomb_before_first_interrupt")
        if bomb_group:
            if vulnerability_known and len(vulnerability_failed) > 0:
                flags.append("gloombomb_soulcoiler_hit_missing")
            elif not vulnerability_known and soulcoiler_damage_complete and bomb_add_hits and len(bomb_add_instances) < min(expected_adds, len(group)):
                # Legacy/direct-damage fallback. Do not treat an empty 1310883
                # add-hit stream as a miss: observed retail logs encode the
                # mechanic as a 99% DR transition instead.
                flags.append("gloombomb_soulcoiler_hit_missing")
        if fear_episodes:
            flags.append("wail_interrupt_failure")
        if resonance_episodes:
            flags.append("manifest_collision")
        if non_tank_hits:
            flags.append("soul_sever_non_tank_hit")
        if overlap_rows:
            flags.append("prior_add_overlap")
        grave_failed = [x for x in gravebound if x.get("status") == "failed"]
        if grave_failed:
            flags.append("gravebound_incomplete")

        # B36.4: keep diagnostics separate from promoted pass/fail signals.
        # Mind-control target count, Gloombomb target-count truncation and
        # Gravebound/"영혼 3개" are still collected but cannot become first
        # mistake or cycle failure until their contracts are explicitly promoted.
        promoted_failure_flags = {
            "gloombomb_extra_hit",
            "gloombomb_before_first_interrupt",
            "gloombomb_soulcoiler_hit_missing",
            "wail_interrupt_failure",
            "manifest_collision",
            "soul_sever_non_tank_hit",
            "prior_add_overlap",
        }
        judgment_flags = [x for x in flags if x in promoted_failure_flags]
        diagnostic_flags = [x for x in flags if x not in promoted_failure_flags]
        complete_observation = bool(len(group) == expected_adds and bomb_group and damage_group and soul is not None)
        bomb_judged = bool(
            min(expected_adds, len(group)) > 0
            and len(vulnerability_known) >= min(expected_adds, len(group))
        )
        cycle_mature = bool(soul is not None or ci < len(summon_groups))
        if judgment_flags:
            status = "failed"
        elif cycle_mature and bomb_judged:
            status = "success"
        else:
            status = "unknown"

        cycle_payload = {
            "index": ci,
            "startSec": round(start, 3),
            "nextCycleSec": round(next_start, 3) if ci < len(summon_groups) else None,
            "soulcoilerCount": len(group),
            "soulcoilerInstances": [list(x) for x in sorted(add_instances, key=str)],
            "mindControl": {
                "sec": round(min((float(x.get("sec") or 0) for x in dread_group), default=0), 3) if dread_group else None,
                "targetCount": len(mc_targets), "players": mc_targets, "expected": expected_mc,
                "evidenceLevel": EVIDENCE_CANDIDATE if dread_group else EVIDENCE_UNAVAILABLE,
                "judgmentEnabled": False, "judgmentStatus": "unknown",
                "note": "B36.4에서는 정배 성공/실패 계약 미확정으로 대상 신호만 수집하고 종합 판정에는 사용하지 않음.",
            },
            "gloombomb": {
                "applySec": round(bomb_sec, 3) if bomb_sec is not None else None,
                "targetCount": len(bomb_targets), "players": bomb_targets, "expected": expected_bombs,
                "damageSec": round(damage_sec, 3) if damage_sec is not None else None,
                "damageEventCount": len(damage_group), "damageUniqueTargetCount": len(damage_keys), "damagePlayers": damage_targets,
                "extraHitCount": len(extra_keys), "extraHitPlayers": extra_names,
                "soulcoilerHitCount": len(vulnerability_success) if vulnerability_known else len(bomb_add_instances),
                "soulcoilerHitInstances": [x.get("instance") for x in vulnerability_success] if vulnerability_known else [list(x) for x in sorted(bomb_add_instances, key=str)],
                "soulcoilerExpectedCount": min(expected_adds, len(group)),
                "soulcoilerVulnerabilityStates": vulnerability_states,
                "soulcoilerVulnerabilityKnownCount": len(vulnerability_known),
                "soulcoilerVulnerabilityRemovedCount": len(vulnerability_success),
                "soulcoilerVulnerabilityRetainedCount": len(vulnerability_failed),
                "soulcoilerVulnerabilityRemovedIndices": [int(x.get("soulcoilerIndex")) for x in vulnerability_success if x.get("soulcoilerIndex") is not None],
                "soulcoilerVulnerabilityRetainedIndices": [int(x.get("soulcoilerIndex")) for x in vulnerability_failed if x.get("soulcoilerIndex") is not None],
                "soulcoilerDamageStreamComplete": bool(soulcoiler_damage_complete),
                "soulcoilerHitEvidenceLevel": EVIDENCE_VERIFIED if vulnerability_known else (EVIDENCE_VERIFIED if bomb_add_hits else EVIDENCE_UNAVAILABLE),
                "firstInterruptSec": round(first_interrupt_sec, 3) if first_interrupt_sec is not None else None,
                "firstInterruptPlayer": (first_interrupt or {}).get("player") if first_interrupt else None,
                "firstInterruptToDamageSec": bomb_after_first_interrupt_sec,
                "firstInterruptOrderOk": bomb_first_interrupt_order_ok,
                "firstInterruptOrderRequired": bool(cfg.get("gloombombFirstInterruptOrderRequired", True)),
                "firstInterruptTimingEvidenceLevel": EVIDENCE_VERIFIED if bomb_after_first_interrupt_sec is not None else EVIDENCE_UNAVAILABLE,
                "evidenceLevel": EVIDENCE_VERIFIED if bomb_group else EVIDENCE_UNAVAILABLE,
            },
            "wail": {
                "castCount": len(wail_casts),
                "completedCastCount": len(wail_successes),
                "completedCastSecs": [round(float(x.get("sec") or 0), 3) for x in wail_successes],
                "failureEpisodes": fear_episodes, "failureEpisodeCount": len(fear_episodes),
                "interrupts": interrupt_rows, "interruptCount": len(interrupt_rows), "evidenceLevel": EVIDENCE_VERIFIED
            },
            "manifestCollision": {
                "episodeCount": len(resonance_episodes),
                "episodes": resonance_episodes,
                "damageEventCount": len(resonance_damage),
                "uniqueTargets": sorted({_coiled_player_name(x) for x in resonance_damage if _coiled_player_name(x)}),
                "linkedDeathCount": sum(int(x.get("linkedDeathCount") or 0) for x in resonance_episodes),
                "samePullSpatialBaseline": manifest_spatial_baseline,
                "evidenceLevel": EVIDENCE_VERIFIED if resonance_episodes else EVIDENCE_UNAVAILABLE,
                "note": "Malevolent Resonance 1310744/1312132 = 두려움의 현신 접촉 직접 신호.",
            },
            "gravebound": {
                "deadlineSec": deadline, "targets": gravebound, "failedCount": len(grave_failed),
                "evidenceLevel": EVIDENCE_CANDIDATE if gravebound else EVIDENCE_UNAVAILABLE,
                "judgmentEnabled": False, "judgmentStatus": "unknown",
                "outcome": _coiled_gravebound_summary(gravebound),
                "note": "V5.2.3: outcome = 영혼 3개 완료/11초 시간 초과/다른 원인 사망 (카드 표시용). 첫 실수 승격은 아직 안 함.",
            },
            "soulSever": {
                "castSec": round(soul_sec, 3) if soul_sec is not None else None,
                "fixationRemovedCandidateCount": len({_coiled_add_instance_key(x, side="source") for x in fixation_removed if _coiled_add_instance_key(x, side="source") is not None}),
                "playerHitCount": len(soul_hit_players),
                "playerHitPlayers": soul_hit_players,
                "tankPlayersConfigured": sorted(tank_resolution.get("configured") or []),
                "tankPlayersInferred": sorted(tank_resolution.get("inferred") or []),
                "tankPlayersEffective": sorted(tank_players),
                "tankResolutionSource": tank_resolution.get("source"),
                "tankInferredSpecs": tank_resolution.get("inferredSpecs") or {},
                "nonTankHitCount": len(non_tank_hits),
                "nonTankHitPlayers": non_tank_hits,
                "nonTankHitEvidenceLevel": EVIDENCE_VERIFIED if tank_players else EVIDENCE_UNAVAILABLE,
                "evidenceLevel": EVIDENCE_VERIFIED if soul is not None else EVIDENCE_UNAVAILABLE,
                "cleanupEvidenceLevel": EVIDENCE_CANDIDATE,
                "note": "Soul Sever 플레이어 피격은 직접 기록. 탱커는 raidPlan 수동 명단을 최우선으로 쓰고, 비어 있으면 COMBATANT_INFO/roster specID의 탱 전문화로 자동 추론하여 비탱커 피격을 verified 판정. fixation 제거 수는 정확한 처치 수가 아닌 보조 후보 신호.",
            },
            "priorAddOverlap": {
                "detected": bool(overlap_rows),
                "wailSecs": [round(float(x.get("sec") or 0), 3) for x in overlap_rows[:8]],
                "evidenceLevel": EVIDENCE_VERIFIED if overlap_rows else EVIDENCE_UNAVAILABLE,
                "note": "이전 차단쫄이 다음 쫄 소환 뒤에도 Wail을 시작했다는 직접 신호. 왜 제거되지 않았는지는 별도 판정하지 않음.",
            },
            "flags": flags,
            "judgmentFlags": judgment_flags,
            "diagnosticFlags": diagnostic_flags,
            "status": status,
            "evidenceLevel": EVIDENCE_VERIFIED if judgment_flags or (cycle_mature and bomb_judged) else EVIDENCE_UNAVAILABLE,
        }
        failures = _coiled_p2_failure_events(cycle_payload)
        cycle_payload["judgment"] = {
            "failures": failures,
            "firstMistake": failures[0] if failures else None,
            "firstMistakeEvidenceLevel": EVIDENCE_VERIFIED if failures else EVIDENCE_UNAVAILABLE,
            "mindControlStatus": "unknown",
            "graveboundStatus": "unknown",
            "note": "정배/영혼3개는 확인 불가로 유지. 첫 실수는 promoted verified failure만 시간순으로 선택.",
        }
        cycle_payload["summaryLine"] = _coiled_p2_cycle_line(cycle_payload)
        cycles.append(cycle_payload)

    diagnostic_flag_counts = Counter(flag for cycle in cycles for flag in cycle.get("flags") or [])
    flag_counts = Counter(flag for cycle in cycles for flag in cycle.get("judgmentFlags") or [])
    p2_summary = _coiled_p2_summary(cycles, collapse_snapshot, cfg)
    direct = p2_summary.get("collapseDirect") or {}
    for cycle in cycles:
        cjud = cycle.get("judgment") or {}
        if direct and int(direct.get("cycleIndex") or -1) == int(cycle.get("index") or 0):
            cjud["collapseDirect"] = direct
        else:
            cjud["collapseDirect"] = None
        cycle["judgment"] = cjud

    return {
        "reached": bool(phase2_activation_sec is not None or cycles),
        "activationSec": round(float(phase2_activation_sec), 3) if phase2_activation_sec is not None else None,
        "endSec": round(float(phase2_end_sec), 3) if phase2_end_sec is not None else None,
        "cycleCount": len(cycles),
        "cycles": cycles,
        "flagCounts": dict(flag_counts),
        "diagnosticFlagCounts": dict(diagnostic_flag_counts),
        "summary": p2_summary,
        "failedCycleCount": sum(1 for x in cycles if x.get("status") == "failed"),
        "wailFailureEpisodeCount": sum((x.get("wail") or {}).get("failureEpisodeCount", 0) for x in cycles),
        "manifestCollisionCycleCount": sum(1 for x in cycles if (x.get("manifestCollision") or {}).get("episodeCount", 0) > 0),
        "manifestCollisionEpisodeCount": sum((x.get("manifestCollision") or {}).get("episodeCount", 0) for x in cycles),
        "manifestCollisionLinkedDeathCount": sum((x.get("manifestCollision") or {}).get("linkedDeathCount", 0) for x in cycles),
        "soulSeverNonTankHitCount": sum((x.get("soulSever") or {}).get("nonTankHitCount", 0) for x in cycles),
        "gloombombExtraHitCycleCount": sum(1 for x in cycles if (x.get("gloombomb") or {}).get("extraHitCount", 0) > 0),
        "gloombombBeforeFirstInterruptCycleCount": sum(1 for x in cycles if "gloombomb_before_first_interrupt" in (x.get("flags") or [])),
        "gloombombTimingSamples": [
            {
                "cycleIndex": x.get("index"),
                "firstInterruptSec": (x.get("gloombomb") or {}).get("firstInterruptSec"),
                "bombDamageSec": (x.get("gloombomb") or {}).get("damageSec"),
                "deltaSec": (x.get("gloombomb") or {}).get("firstInterruptToDamageSec"),
                "orderOk": (x.get("gloombomb") or {}).get("firstInterruptOrderOk"),
                "evidenceLevel": (x.get("gloombomb") or {}).get("firstInterruptTimingEvidenceLevel", EVIDENCE_UNAVAILABLE),
            }
            for x in cycles
            if (x.get("gloombomb") or {}).get("firstInterruptToDamageSec") is not None
        ],
        "gloombombTimingObservedCount": sum(1 for x in cycles if (x.get("gloombomb") or {}).get("firstInterruptToDamageSec") is not None),
        "gloombombSoulcoilerMissCycleCount": sum(1 for x in cycles if "gloombomb_soulcoiler_hit_missing" in (x.get("flags") or [])),
        "priorAddOverlapCycleCount": sum(1 for x in cycles if (x.get("priorAddOverlap") or {}).get("detected")),
        "graveboundIncompleteCount": sum((x.get("gravebound") or {}).get("failedCount", 0) for x in cycles),
        "evidenceLevel": EVIDENCE_VERIFIED if (phase2_activation_sec is not None or cycles) else EVIDENCE_UNAVAILABLE,
    }


def _coiled_plan_stage(
    sec: float,
    strategy: dict,
    *,
    phase2_entry_sec: float | None = None,
    phase2_end_sec: float | None = None,
) -> dict:
    """Classify a wipe position against P1 anchors and observed P2 activation.

    P1 labels use the validated reference schedule.  Once Malacrass activation
    is actually observed, later deaths are labeled P2 rather than being left in
    the old catch-all "after Guillotine 2" bucket.
    """
    if phase2_end_sec is not None and sec >= float(phase2_end_sec):
        return {
            "key": "phase2_5",
            "label": "사이페(영혼결속) 이후",
            "sec": round(float(sec), 3),
            "phase2EntrySec": round(float(phase2_entry_sec), 3) if phase2_entry_sec is not None else None,
            "phase2EndSec": round(float(phase2_end_sec), 3),
            "evidenceLevel": EVIDENCE_VERIFIED,
            "note": "실제 P2/Soulbinding 로그 앵커 기반 진행 구간. 성공·실패 판정에는 직접 사용하지 않음.",
        }
    if phase2_entry_sec is not None and sec >= float(phase2_entry_sec):
        return {
            "key": "phase2",
            "label": "2페 · 말라크라스",
            "sec": round(float(sec), 3),
            "phase2EntrySec": round(float(phase2_entry_sec), 3),
            "evidenceLevel": EVIDENCE_VERIFIED,
            "note": "말라크라스 Dreadful Presence 첫 cast 이후를 P2로 분류.",
        }

    ref = (strategy or {}).get("referenceTimeline") or {}
    breaths = [float(x) for x in (ref.get("breathSecs") or [23, 40, 60, 77, 108, 125, 145, 162])]
    guills = [float(x) for x in (ref.get("guillotineSecs") or [43, 128])]
    b1 = breaths[0] if breaths else 23.0
    g1 = guills[0] if guills else 43.0
    b5 = breaths[4] if len(breaths) > 4 else 108.0
    g2 = guills[1] if len(guills) > 1 else 128.0
    if sec < b1:
        key, label = "before_first_breath", "1차 브레스 전"
    elif sec < g1:
        key, label = "guillotine1_pair", "단두대 1 전 브레스 구간"
    elif sec < b5:
        key, label = "after_guillotine1", "단두대 1 이후 · 중간 P1"
    elif sec < g2:
        key, label = "guillotine2_pair", "단두대 2 전 브레스 구간"
    else:
        key, label = "after_guillotine2", "단두대 2 이후 · P1"
    return {
        "key": key,
        "label": label,
        "sec": round(float(sec), 3),
        "referenceBreathSecs": breaths,
        "referenceGuillotineSecs": guills,
        "evidenceLevel": EVIDENCE_VERIFIED,
        "note": "진행 구간 분류용 기준점. 성공/실패 판정에는 사용하지 않음.",
    }

def _coiled_collapse_snapshot(
    rows: list[dict],
    strategy: dict,
    *,
    phase2_entry_sec: float | None = None,
    phase2_end_sec: float | None = None,
) -> dict | None:
    """Describe the earliest meaningful death collapse without over-claiming causality.

    A single recoverable death should not automatically become the wipe root.
    We therefore keep the literal first death, but anchor the collapse snapshot
    to the first 1-second window containing at least four player deaths. If no
    such window exists, the first death is used as a fallback.

    Two candidate signals are exposed around the collapse anchor:
    1) the last hostile damage event on the lead dead player, and
    2) the strongest raid-wide pressure event in the preceding 0.7s.
    Both stay candidate until B31's downstream chain links them.
    """
    deaths = [
        x for x in _fast_rows_event(rows, {"death", "unit_died"})
        if str(x.get("targetType") or "").strip().lower() == "player"
    ]
    if not deaths:
        return None

    first = deaths[0]
    first_t = float(first.get("sec") or 0)
    first_cluster = [x for x in deaths if first_t <= float(x.get("sec") or 0) <= first_t + 1.0]

    collapse_lead = first
    collapse_cluster = first_cluster
    significant = False
    for death in deaths:
        start = float(death.get("sec") or 0)
        cluster = [x for x in deaths if start <= float(x.get("sec") or 0) <= start + 1.0]
        if len(cluster) >= 4:
            collapse_lead = death
            collapse_cluster = cluster
            significant = True
            break

    t0 = float(collapse_lead.get("sec") or 0)
    lead_target = _target_key(collapse_lead)
    next_cluster = [x for x in deaths if t0 <= float(x.get("sec") or 0) <= t0 + 3.0]

    final_hits = [
        x for x in rows
        if _is_damage(x)
        and _target_key(x) == lead_target
        and t0 - 0.30 <= float(x.get("sec") or 0) <= t0 + 0.05
        and not _is_playerish_source(x)
    ]
    final = final_hits[-1] if final_hits else None

    pressure_rows = [
        x for x in rows
        if _is_damage(x)
        and str(x.get("targetType") or "").strip().lower() == "player"
        and t0 - 0.70 <= float(x.get("sec") or 0) <= t0 + 0.05
        and not _is_playerish_source(x)
    ]
    buckets: dict[tuple, dict] = {}
    for row in pressure_rows:
        sid = safe_int(row.get("spellID") if row.get("spellID") is not None else row.get("abilityGameID"))
        key = (sid, safe_int(row.get("sourceGameID")), str(row.get("spellName") or ""))
        item = buckets.setdefault(key, {
            "spellID": sid,
            "spellName": str(row.get("spellName") or sid or ""),
            "sourceName": row.get("sourceName"),
            "sourceGameID": safe_int(row.get("sourceGameID")),
            "targets": set(),
            "eventCount": 0,
            "totalDamage": 0.0,
            "maxHit": 0.0,
            "firstSec": float(row.get("sec") or 0),
            "lastSec": float(row.get("sec") or 0),
        })
        target = _target_key(row)
        if target is not None:
            item["targets"].add(target)
        amount = _damage_amount(row)
        item["eventCount"] += 1
        item["totalDamage"] += amount
        item["maxHit"] = max(item["maxHit"], amount)
        item["firstSec"] = min(item["firstSec"], float(row.get("sec") or 0))
        item["lastSec"] = max(item["lastSec"], float(row.get("sec") or 0))

    ranked = sorted(
        buckets.values(),
        key=lambda x: (len(x["targets"]), x["totalDamage"], x["maxHit"]),
        reverse=True,
    )
    raid_pressure = ranked[0] if ranked else None

    def pack_damage(row):
        if not row:
            return None
        return {
            "sec": round(float(row.get("sec") or 0), 3),
            "spellID": safe_int(row.get("spellID") if row.get("spellID") is not None else row.get("abilityGameID")),
            "spellName": row.get("spellName"),
            "sourceName": row.get("sourceName"),
            "sourceGameID": safe_int(row.get("sourceGameID")),
            "amount": round(_damage_amount(row)),
            "evidenceLevel": EVIDENCE_CANDIDATE,
        }

    packed_pressure = None
    if raid_pressure:
        packed_pressure = {
            "spellID": raid_pressure["spellID"],
            "spellName": raid_pressure["spellName"],
            "sourceName": raid_pressure["sourceName"],
            "sourceGameID": raid_pressure["sourceGameID"],
            "targetCount": len(raid_pressure["targets"]),
            "eventCount": raid_pressure["eventCount"],
            "totalDamage": round(raid_pressure["totalDamage"]),
            "maxHit": round(raid_pressure["maxHit"]),
            "firstSec": round(raid_pressure["firstSec"], 3),
            "lastSec": round(raid_pressure["lastSec"], 3),
            "evidenceLevel": EVIDENCE_CANDIDATE,
        }

    chain_rows = [
        x for x in rows
        if _is_damage(x)
        and str(x.get("targetType") or "").strip().lower() == "player"
        and t0 - 0.12 <= float(x.get("sec") or 0) <= t0 + 0.45
        and not _is_playerish_source(x)
    ]
    chain_bursts = []
    by_spell: dict[tuple, list[dict]] = defaultdict(list)
    for row in chain_rows:
        sid = safe_int(row.get("spellID") if row.get("spellID") is not None else row.get("abilityGameID"))
        by_spell[(sid, str(row.get("spellName") or ""), safe_int(row.get("sourceGameID")))].append(row)
    for (sid, name, source_game_id), spell_rows in by_spell.items():
        for group in _group_times(sorted(spell_rows, key=lambda x: float(x.get("sec") or 0)), max_gap=0.08):
            targets = {_target_key(x) for x in group if _target_key(x) is not None}
            if len(targets) < 4:
                continue
            sec = min(float(x.get("sec") or 0) for x in group)
            amounts = [_damage_amount(x) for x in group]
            chain_bursts.append({
                "sec": round(sec, 3),
                "afterCollapseSec": round(sec - t0, 3),
                "spellID": sid,
                "spellName": name or str(sid or ""),
                "sourceGameID": source_game_id,
                "targetCount": len(targets),
                "eventCount": len(group),
                "totalDamage": round(sum(amounts)),
                "maxHit": round(max(amounts) if amounts else 0),
                "evidenceLevel": EVIDENCE_CANDIDATE,
            })
    chain_bursts.sort(key=lambda x: (float(x.get("sec") or 0), -(x.get("targetCount") or 0)))

    return {
        "firstDeathSec": round(first_t, 3),
        "firstDeathPlayer": first.get("targetName"),
        "firstDeathCluster1s": len(first_cluster),
        "collapseSec": round(t0, 3),
        "collapseLeadPlayer": collapse_lead.get("targetName"),
        "collapseCluster1s": len(collapse_cluster),
        "collapseCluster3s": len(next_cluster),
        "significantCluster": significant,
        "collapseDeaths": [
            {"sec": round(float(x.get("sec") or 0), 3), "player": x.get("targetName")}
            for x in next_cluster[:12]
        ],
        "stage": _coiled_plan_stage(t0, strategy, phase2_entry_sec=phase2_entry_sec, phase2_end_sec=phase2_end_sec),
        "collapseLeadFinalHit": pack_damage(final),
        "raidWidePressureCandidate": packed_pressure,
        "collapseBurstChainCandidates": chain_bursts[:8],
        "evidenceLevel": EVIDENCE_CANDIDATE,
        "note": "첫 4명/1초 사망 군집을 우선한 붕괴 후보. B31 downstream 연결 전에는 전멸 원인으로 확정하지 않음.",
    }


def _analyze_coiled(events: Iterable[dict], encounter: dict, rule: dict, *, source_kind: str, supplemental: dict | None = None) -> dict:
    rows = sorted((dict(x) for x in events if x.get("sec") is not None), key=lambda x: float(x.get("sec") or 0))
    # B37.4.17: compact capsules keep 1s hostile HP samples separately.  Merge
    # those snapshots into the Coiled resource stream so archive rejudge
    # preserves the live P1->P2 HP-zero timestamp instead of regressing to
    # "entry unknown".  These synthetic rows are observation-only resources.
    for hp in list((supplemental or {}).get("hpSamples") or []):
        try:
            sec = float(hp.get("sec"))
        except (TypeError, ValueError):
            continue
        guid = str(hp.get("guid") or "")
        game_id = _guid_npc_id(guid)
        if game_id not in {257911, 259854}:
            continue
        rows.append({
            "sec": sec, "event": "RESOURCE_SNAPSHOT",
            "targetGUID": guid, "destGUID": guid, "targetGameID": game_id,
            "targetName": hp.get("name"), "destName": hp.get("name"),
            "targetType": "NPC",
            "targetCurrentHP": hp.get("currentHP"), "targetMaxHP": hp.get("maxHP"),
            "targetHealthPct": hp.get("hpPct"),
            "supplementalResource": True,
        })
    rows.sort(key=lambda x: float(x.get("sec") or 0))
    _fast_begin(rows)  # V5.2.4: 이 rows 로 하는 주문·이벤트 조회는 색인으로 (analyze_events 끝에서 해제)
    strategy, strategy_source = load_strategy("coiled_altar")
    goals = (strategy or {}).get("breathGoals") or {}
    survival_context = _coiled_survival_context(rows)
    phase1_transition = _coiled_phase1_transition(rows, rule, strategy)
    phase2_activation_sec = phase1_transition.get("malacrassActivationSec")
    # B36.5 NSRT protection windows are P2-entry relative.  Use the true Zul'jan
    # HP-zero transition when available; only fall back to the later Malacrass
    # activation anchor when resource snapshots are absent.
    phase2_schedule_anchor_sec = phase1_transition.get("entrySec")
    phase2_schedule_anchor_basis = "zuljan_hp_zero" if phase2_schedule_anchor_sec is not None else None
    if phase2_schedule_anchor_sec is None:
        phase2_schedule_anchor_sec = phase2_activation_sec
        if phase2_schedule_anchor_sec is not None:
            phase2_schedule_anchor_basis = "malacrass_activation_fallback"
    phase2_end_sec = _coiled_phase2_end_sec(rows, rule)
    collapse_snapshot = _coiled_collapse_snapshot(
        rows, strategy, phase2_entry_sec=phase2_activation_sec, phase2_end_sec=phase2_end_sec
    )
    if collapse_snapshot is not None:
        collapse_snapshot["raidSizeObserved"] = survival_context.get("raidSize")

    phase2 = _coiled_p2_cycles(
        rows, rule, strategy, phase2_activation_sec=phase2_activation_sec, phase2_end_sec=phase2_end_sec,
        source_kind=source_kind, collapse_snapshot=collapse_snapshot,
    )
    _coiled_apply_split_evidence(phase2, [], rule, source_kind)
    p1_execution = _coiled_p1_execution(
        rows, rule, strategy, phase2_entry_sec=phase1_transition.get("entrySec")
    )

    breaths = _coiled_breaths(rows, rule)
    orb_waves = _coiled_orb_waves(rows, rule)
    same_name_cleaves = [x for x in rows if str(x.get("spellName") or "") in {"절단", "Sever"} and _is_completed_cast(x)]
    ignored_same_name = [x for x in same_name_cleaves if not _coiled_valid_breath(x, rule)]

    diagnostic_guillotine = _discovery_candidates(rows, _DISCOVERY_GUILLOTINE_NAMES)
    diagnostic_rupture = _discovery_candidates(rows, _DISCOVERY_TOXIC_RUPTURE_NAMES)

    guillotine_spec = ((rule.get("mechanics") or {}).get("guillotine") or {})
    guillotine_marker_ids = {safe_int(x) for x in (guillotine_spec.get("markerSpellIDs") or guillotine_spec.get("spellIDs") or []) if safe_int(x) is not None}
    guillotine_rows = [
        x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in guillotine_marker_ids
        and _is_creature_source(x) and _is_aura_apply(x)
        and safe_int(x.get("sourceGameID")) in {257911}
    ]
    guillotine_markers = []
    for gi, group in enumerate(_group_times(guillotine_rows, max_gap=0.40), start=1):
        guillotine_markers.append({
            "index": gi, "sec": round(float(group[0].get("sec") or 0), 3),
            "spellID": safe_int(group[0].get("spellID") or group[0].get("abilityGameID")),
            "evidenceLevel": EVIDENCE_VERIFIED,
        })

    mythic_rule_applied = _mythic(encounter)
    strategy_assignments = _strategy_slots_by_guillotine(breaths, orb_waves, guillotine_markers) if mythic_rule_applied else {}

    timeline = []
    for wave in orb_waves:
        timeline.append({
            "kind": "orb_wave",
            "sec": wave["sec"],
            "title": f"응결된 맹독 구슬 웨이브 {wave['index']}",
            "summary": (
                f"응결 {wave.get('coalescedCount', 0)} · 변이 {wave.get('mutationCount', 0)} · 총 {wave['summonCount']}개 소환"
                if wave.get('mutationCount') else f"추적자 {wave['summonCount']}개 소환"
            ),
            "evidenceLevel": EVIDENCE_VERIFIED,
            "ids": {"npcIDs": wave.get("npcIDs") or []},
        })

    for gm in guillotine_markers:
        timeline.append({
            "kind": "guillotine",
            "sec": gm["sec"],
            "title": f"단두대 {gm['index']}차",
            "summary": f"줄잔 단두대 marker spellID {gm['spellID']}",
            "evidenceLevel": EVIDENCE_VERIFIED,
            "ids": {"spellIDs": [gm["spellID"]]},
        })

    observations = []
    per_wave_counter: dict[int, int] = {}
    for idx, breath in enumerate(breaths, start=1):
        sec = float(breath.get("sec") or 0)
        prior_waves = [w for w in orb_waves if float(w.get("sec") or 0) <= sec]
        wave = prior_waves[-1] if prior_waves else None
        wave_index = int(wave["index"]) if wave else None
        if wave_index is not None:
            per_wave_counter[wave_index] = per_wave_counter.get(wave_index, 0) + 1
            breath_in_wave = per_wave_counter[wave_index]
        else:
            breath_in_wave = None
        assignment = strategy_assignments.get(idx - 1)
        slot = (assignment or {}).get("slot")
        strat = goals.get(slot) if mythic_rule_applied and slot else None
        pulses = _candidate_condensed_pulses(rows, sec)
        if mythic_rule_applied:
            orb_signal = _verified_venom_rupture_orb_count(
                rows, sec, rule, source_kind=source_kind, strategy=strategy
            )
        else:
            candidate_signal = _candidate_venom_rupture_signal(rows, sec, rule)
            orb_signal = {
                "removedOrbs": None,
                "consistent": False,
                "eventCount": (candidate_signal or {}).get("eventCount", 0),
                "targetCount": (candidate_signal or {}).get("targetCount", 0),
                "minPerTarget": (candidate_signal or {}).get("minPerTarget"),
                "maxPerTarget": (candidate_signal or {}).get("maxPerTarget"),
                "windowSec": 1.0,
                "spellIDs": ((rule.get("mechanics") or {}).get("toxicRupture") or {}).get("spellIDs") or [],
                "ignoredRefresh": True,
                "zeroByAbsence": False,
                "evidenceLevel": EVIDENCE_UNAVAILABLE,
                "note": "구슬 제거 수 1:1 검증은 신화 규칙에서만 적용. 비신화는 동일 신호를 참고 수집만 함.",
            }
        rupture_bursts = _candidate_venom_rupture_bursts(rows, sec, rule)
        removed_orbs = orb_signal.get("removedOrbs")
        guard = _coiled_judgment_guard(sec, collapse_snapshot, survival_context, strategy)
        strategy_mode = str((strat or {}).get("judgmentMode") or "judge").strip().lower() if strat else "none"
        strategy_judgment_enabled = bool(strat and strategy_mode != "observe")
        raw_status = "unknown"
        status = "unknown"
        if strategy_judgment_enabled and removed_orbs is not None:
            removed_i = int(removed_orbs)
            goal_min = int(strat.get("min"))
            goal_max = int(strat.get("max"))
            raw_status = "success" if goal_min <= removed_i <= goal_max else "failed"
            # B37.4.18: target-range deviation is coaching context unless the
            # judged even breath reaches the verified high-burst threshold.
            # This prevents survivable 5~6 orb first-even breaths from stealing
            # firstMistake while keeping 7+ as a promoted failure.
            if slot in {"wave1_breath2", "wave2_breath2"}:
                high_burst_min = int(((strategy or {}).get("orbAccounting") or {}).get("highBurstMin", 7))
                if raw_status == "success":
                    status = "success"
                elif removed_i >= high_burst_min:
                    status = "failed"
                else:
                    status = "warning"
            else:
                status = raw_status
        if strategy_judgment_enabled and guard.get("judgmentExcluded"):
            status = "excluded"
        fallback_label = (
            f"구슬 웨이브 {wave_index} · {breath_in_wave}차 브레스 · 전략 없음 · 참고"
            if wave_index and breath_in_wave and mythic_rule_applied else f"브레스 {idx}"
        )
        row = {
            "index": idx,
            "label": (strat or {}).get("label") or fallback_label,
            "strategySlot": slot,
            "strategyAssignment": assignment,
            "strategyApplies": bool(strat),
            "strategyJudgmentMode": strategy_mode,
            "strategyJudgmentEnabled": strategy_judgment_enabled,
            "strategyUnit": "총 구슬",
            "sec": round(sec, 3),
            "spellID": safe_int(breath.get("spellID") or breath.get("abilityGameID")),
            "sourceName": breath.get("sourceName"),
            "sourceType": breath.get("sourceType"),
            "sourceGameID": breath.get("sourceGameID"),
            "orbWaveIndex": wave_index,
            "breathInWave": breath_in_wave,
            "orbWaveSummonCount": wave.get("summonCount") if wave else None,
            "goalMin": (strat or {}).get("min"),
            "goalMax": (strat or {}).get("max"),
            "goalNote": (strat or {}).get("note"),
            "destroyedOrbs": removed_orbs,
            "status": status,
            "rawStatus": raw_status,
            "judgmentExcluded": bool(guard.get("judgmentExcluded")),
            "judgmentEligible": bool(guard.get("judgmentEligible")),
            "judgmentGuard": guard,
            "judgmentExclusionLabel": guard.get("judgmentExclusionLabel"),
            "evidenceLevel": EVIDENCE_VERIFIED,
            "orbRemovalSignal": orb_signal,
            # Compatibility alias for older B35.1~B35.3 readers.  The signal is
            # no longer candidate when evidenceLevel=verified.
            "candidateVenomRuptureSignal": orb_signal,
            "candidateCondensedVenomPulseCount": len(pulses),
            "candidateCondensedVenomPulses": pulses,
            "candidateVenomRuptureBurstCount": len(rupture_bursts),
            "candidateVenomRuptureBursts": rupture_bursts,
        }
        observations.append(row)
        removal_text = (
            f" · 구슬 {removed_orbs}개 제거 [검증됨]"
            if removed_orbs is not None else " · 구슬 제거 수 확인 불가"
        )
        timeline.append({
            "kind": "breath",
            "sec": row["sec"],
            "title": row["label"],
            "summary": f"줄잔 절단 spellID {row['spellID']} 완료 시전{removal_text}",
            "evidenceLevel": EVIDENCE_VERIFIED,
            "ids": {"spellIDs": [row["spellID"]]},
        })
        for pulse in pulses:
            timeline.append({
                "kind": "candidate_pulse",
                "sec": pulse["sec"],
                "title": "응결된 맹독 광역 펄스 후보",
                "summary": f"공대 대상 {pulse['targetCount']}명 · 제거 구슬 수 판정에는 사용하지 않음",
                "evidenceLevel": EVIDENCE_CANDIDATE,
                "ids": {"spellIDs": pulse.get("spellIDs") or []},
            })

    # P1 transition / P2 mechanic timeline.  The visual P1->P2 transition is
    # HP-based; Malacrass Dreadful Presence is a later, stable P2 analysis anchor.
    if phase1_transition.get("entrySec") is not None:
        timeline.append({
            "kind": "phase2_entry",
            "sec": phase1_transition.get("entrySec"),
            "title": "P2 진입",
            "summary": f"줄잔 HP 0 · 목표 {int(phase1_transition.get('targetMinSec', 165))}~{int(phase1_transition.get('targetMaxSec', 170))}초",
            "evidenceLevel": EVIDENCE_VERIFIED,
            "ids": {"npcIDs": [257911]},
        })
    if phase2_activation_sec is not None:
        timeline.append({
            "kind": "phase2_activation",
            "sec": phase2_activation_sec,
            "title": "P2 분석 앵커 · 말라크라스 활성",
            "summary": "Dreadful Presence 1288624 · 실제 P2 진입 시각과는 별도",
            "evidenceLevel": EVIDENCE_VERIFIED,
            "ids": {"spellIDs": [1288624], "npcIDs": [259854]},
        })
    for cyc in phase2.get("cycles") or []:
        if (cyc.get("mindControl") or {}).get("sec") is not None:
            timeline.append({
                "kind": "p2_mind_control", "sec": cyc["mindControl"]["sec"],
                "title": f"P2 {cyc.get('index')}사이클 · 정배",
                "summary": f"대상 {(cyc.get('mindControl') or {}).get('targetCount')}명 (기준 {(cyc.get('mindControl') or {}).get('expected')})",
                "evidenceLevel": (cyc.get("mindControl") or {}).get("evidenceLevel", EVIDENCE_UNAVAILABLE),
                "ids": {"spellIDs": [1297445]},
            })
        timeline.append({
            "kind": "p2_soulcoilers", "sec": cyc.get("startSec"),
            "title": f"P2 {cyc.get('index')}사이클 · 차단쫄",
            "summary": f"원한에 찬 영혼살무사 {cyc.get('soulcoilerCount')}마리 소환",
            "evidenceLevel": EVIDENCE_VERIFIED,
            "ids": {"spellIDs": [1286498], "npcIDs": [261521]},
        })
        gb = cyc.get("gloombomb") or {}
        if gb.get("applySec") is not None:
            extra = int(gb.get("extraHitCount") or 0)
            timeline.append({
                "kind": "p2_gloombomb", "sec": gb.get("applySec"),
                "title": f"P2 {cyc.get('index')}사이클 · 암영폭탄",
                "summary": f"대상 {gb.get('targetCount')}명 · 폭발 고유 피격 {gb.get('damageUniqueTargetCount')}명" + (f" · 추가 피격 {extra}명" if extra else ""),
                "evidenceLevel": gb.get("evidenceLevel", EVIDENCE_UNAVAILABLE),
                "ids": {"spellIDs": [1310881, 1310883]},
            })
        for ep in (cyc.get("wail") or {}).get("failureEpisodes") or []:
            timeline.append({
                "kind": "p2_wail_failure", "sec": ep.get("sec"),
                "title": f"P2 {cyc.get('index')}사이클 · 공포의 통곡 차단 실패",
                "summary": f"플레이어 {ep.get('hitCount')}명에게 1286399 공포 적용",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "ids": {"spellIDs": [1286399]},
            })
        ss = cyc.get("soulSever") or {}
        if ss.get("castSec") is not None:
            timeline.append({
                "kind": "p2_soul_sever", "sec": ss.get("castSec"),
                "title": f"P2 {cyc.get('index')}사이클 · 영혼 절단",
                "summary": f"현신 fixation 제거 후보 {ss.get('fixationRemovedCandidateCount')}개",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "ids": {"spellIDs": [1286620]},
            })

    for failure in p1_execution.get("failures") or []:
        timeline.append({
            "kind": failure.get("kind"),
            "sec": failure.get("sec"),
            "title": failure.get("label"),
            "label": failure.get("label"),
            "summary": failure.get("summary"),
            "evidenceLevel": EVIDENCE_VERIFIED,
            "status": "failed",
            "ids": {"spellIDs": failure.get("directSpellIDs") or []},
        })

    orb_accounting = _coiled_orb_accounting(
        observations, orb_waves, guillotine_markers, strategy,
        collapse_snapshot=collapse_snapshot, survival_context=survival_context,
    )
    orb_failures = _coiled_orb_failure_events(observations, orb_accounting)
    for failure in orb_failures:
        timeline.append({
            "kind": failure.get("kind"), "sec": failure.get("sec"),
            "title": failure.get("label"), "label": failure.get("label"),
            "summary": failure.get("summary"), "evidenceLevel": EVIDENCE_VERIFIED,
            "status": "failed", "ids": {"spellIDs": failure.get("directSpellIDs") or []},
        })
    orb_causal_chain = _coiled_orb_causal_chain(observations, orb_accounting, collapse_snapshot, rule)
    cleanup_signal = _coiled_post_g2_cleanup_signal(
        rows, rule, orb_accounting, phase1_transition, phase2, strategy,
        source_kind=source_kind, collapse_snapshot=collapse_snapshot,
    )
    tank_handoff = _coiled_tank_handoff(
        orb_accounting, phase1_transition, phase2, strategy, source_kind=source_kind,
        cleanup_signal=cleanup_signal,
    )
    shield_cycles = _coiled_shield_cycles(
        rows, rule, strategy, encounter,
        phase2_anchor_sec=phase2_schedule_anchor_sec, phase2_end_sec=phase2_end_sec,
    )
    shield_observation_plan = _coiled_shield_observation_plan(
        rows, strategy, encounter, shield_cycles,
        phase2_anchor_sec=phase2_schedule_anchor_sec,
        phase2_anchor_basis=phase2_schedule_anchor_basis,
    )

    for shield in shield_cycles:
        timeline.append({
            "kind": "p2_shield_start",
            "sec": shield.get("startSec"),
            "title": f"P2 보호막 {shield.get('index')}",
            "summary": (
                f"영원한 일몰 시작 · P2 {shield.get('p2RelativeSec'):+.1f}s"
                if shield.get("p2RelativeSec") is not None else "영원한 일몰 보호막 시작"
            ),
            "evidenceLevel": EVIDENCE_VERIFIED,
            "ids": shield.get("ids") or {},
        })
        if shield.get("breakSec") is not None:
            remain = shield.get("remainingPct")
            remain_text = f" · 잔여 {remain:.1f}%" if remain is not None else ""
            timeline.append({
                "kind": "p2_shield_break",
                "sec": shield.get("breakSec"),
                "title": f"P2 보호막 {shield.get('index')} 파괴",
                "summary": f"{shield.get('breakDurationSec'):.3f}초{remain_text}" if shield.get("breakDurationSec") is not None else f"보호막 제거{remain_text}",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "ids": shield.get("ids") or {},
            })
        if shield.get("interruptSec") is not None:
            timeline.append({
                "kind": "p2_nightfall_interrupt",
                "sec": shield.get("interruptSec"),
                "title": "영원한 일몰 차단",
                "summary": str(shield.get("interruptPlayer") or "차단자 확인 불가"),
                "evidenceLevel": EVIDENCE_VERIFIED,
                "ids": shield.get("ids") or {},
            })
        if shield.get("castSuccessSec") is not None:
            timeline.append({
                "kind": "p2_nightfall_success",
                "sec": shield.get("castSuccessSec"),
                "title": "영원한 일몰 시전 완료",
                "summary": "보호막 처리 실패",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "ids": shield.get("ids") or {},
            })

    checks = []
    if not mythic_rule_applied:
        checks.append(make_check(
            key="difficulty_guard", title="신화 규칙 적용", status="unknown",
            evidence_level=EVIDENCE_VERIFIED,
            summary="신화 난이도가 아니므로 신화 공대 전략/성공·실패 판정은 적용하지 않고 기믹 신호만 수집합니다.",
        ))
    elif breaths:
        known_counts = [x for x in observations if x.get("destroyedOrbs") is not None and not x.get("judgmentExcluded")]
        failed_breaths = [x for x in observations if x.get("strategyApplies") and not x.get("judgmentExcluded") and x.get("status") == "failed"]
        guill_failures = [x for x in (orb_accounting.get("guillotineStates") or []) if not x.get("judgmentExcluded") and x.get("status") == "failed"]
        overall = orb_accounting.get("overallStatus") or "unknown"
        if overall == "failed":
            parts = []
            context_bits = []
            if failed_breaths:
                parts.append("고중첩 실패 " + ", ".join(
                    f"{_fmt_time(x.get('sec'))} {x.get('destroyedOrbs')}개(목표 {x.get('goalMin')}~{x.get('goalMax')})"
                    for x in failed_breaths[:4]
                ))
                for x in failed_breaths:
                    ctx = x.get("precedingOddContext") or {}
                    if ctx.get("removedOrbs") is not None:
                        context_bits.append(
                            f"직전 홀수 {_fmt_time(ctx.get('sec'))} {ctx.get('removedOrbs')}개"
                            + (f"(계획 {ctx.get('goalMin')}~{ctx.get('goalMax')})" if ctx.get("goalMin") is not None else "")
                        )
            if guill_failures:
                parts.append("단두대 전 잔존 " + ", ".join(
                    f"{x.get('guillotineIndex')}차 {x.get('remainingOrbs')}개" for x in guill_failures[:2]
                ))
                for x in guill_failures:
                    ctx = x.get("precedingOddContext") or {}
                    if ctx.get("removedOrbs") is not None:
                        context_bits.append(
                            f"직전 홀수 {_fmt_time(ctx.get('sec'))} {ctx.get('removedOrbs')}개"
                            + (f"(계획 {ctx.get('goalMin')}~{ctx.get('goalMax')})" if ctx.get("goalMin") is not None else "")
                        )
            if context_bits:
                seen = []
                for bit in context_bits:
                    if bit not in seen:
                        seen.append(bit)
                parts.append("선행 맥락: " + ", ".join(seen[:3]) + " · 실패 판정 아님")
            summary = " · ".join(parts) or "검증된 구슬 처리 이상 관측"
        elif overall == "warning":
            warning_breaths = [x for x in observations if x.get("strategyApplies") and not x.get("judgmentExcluded") and x.get("status") == "warning"]
            warning_guillotines = [x for x in (orb_accounting.get("guillotineStates") or []) if not x.get("judgmentExcluded") and x.get("status") == "warning"]
            parts = []
            if warning_breaths:
                parts.append("위험 관측 " + ", ".join(
                    f"{_fmt_time(x.get('sec'))} {x.get('destroyedOrbs')}개(계획 {x.get('goalMin')}~{x.get('goalMax')})"
                    for x in warning_breaths[:4]
                ))
            if warning_guillotines:
                parts.append("단두대 전 잔존 " + ", ".join(
                    f"{x.get('guillotineIndex')}차 {x.get('remainingOrbs')}개" for x in warning_guillotines[:2]
                ) + " · 단독 실패 판정 아님")
            summary = " · ".join(parts) or "구슬 처리 위험 관측 · 직접 실패 근거 없음"
        elif overall == "success":
            summary = "관측된 전략 브레스가 목표 범위이고 1차 단두대 진입 잔존 0개 조건을 충족했습니다. 2차 단두대 잔존은 P2 전 정리 정보값으로 별도 판정합니다."
        else:
            summary = f"브레스 {len(breaths)}회 중 제거 수 검증 {len(known_counts)}회. 트라이가 조기 종료됐거나 일부 구간이 미관측이라 전체 판정은 보류합니다."
        checks.append(make_check(
            key="orb_processing",
            title="브레스 구슬 처리",
            status=overall,
            evidence_level=EVIDENCE_VERIFIED if known_counts else EVIDENCE_UNAVAILABLE,
            summary=summary,
            ids={
                "breathSpellIDs": ((rule.get("mechanics") or {}).get("cleaveBreath") or {}).get("spellIDs") or [],
                "ruptureSpellIDs": ((rule.get("mechanics") or {}).get("toxicRupture") or {}).get("spellIDs") or [],
                "orbNpcIDs": sorted(set(
                    (((rule.get("actors") or {}).get("condensedVenomTracker") or {}).get("npcIDs") or []) +
                    (((rule.get("actors") or {}).get("virulentMutation") or {}).get("npcIDs") or [])
                )),
            },
        ))
    else:
        checks.append(make_check(
            key="orb_processing", title="브레스 구슬 처리", status="unknown",
            evidence_level=EVIDENCE_UNAVAILABLE, summary="검증된 브레스 spellID 시전을 찾지 못했습니다."
        ))
    if mythic_rule_applied:
        p1hp = phase1_transition.get("healthAtCheckpoint") or {}
        entry_sec = phase1_transition.get("entrySec")
        if entry_sec is not None:
            p1_status = "success" if phase1_transition.get("status") == "success" else "failed"
            hp_text = f" · 2:45 줄잔 {p1hp.get('healthPct')}%" if p1hp.get("available") else " · 2:45 줄잔 HP 확인 불가"
            checks.append(make_check(
                key="phase1_transition", title="P1→P2 진입 타이밍", status=p1_status,
                evidence_level=EVIDENCE_VERIFIED,
                summary=f"{_fmt_time(entry_sec)} P2 진입 · 목표 2:45~2:50{hp_text}",
                details=[phase1_transition.get("note")],
                ids={"bossNpcID": 257911},
            ))
        elif phase2.get("reached"):
            hp_text = f"2:45 줄잔 {p1hp.get('healthPct')}%" if p1hp.get("available") else "2:45 줄잔 HP 확인 불가"
            checks.append(make_check(
                key="phase1_transition", title="P1→P2 진입 타이밍", status="unknown",
                evidence_level=EVIDENCE_UNAVAILABLE,
                summary=f"P2 도달은 확인됨 · 실제 HP 0 진입 시각은 resource snapshot 부재로 확인 불가 · {hp_text}",
                details=[f"말라크라스 활성 앵커 {_fmt_time(phase2_activation_sec)}" if phase2_activation_sec is not None else "말라크라스 활성 앵커 미관측"],
                ids={"bossNpcID": 257911, "phase2ActivationSpellID": 1288624},
            ))

        if phase2.get("reached"):
            p2_flags = phase2.get("flagCounts") or {}
            failed_cycles = int(phase2.get("failedCycleCount") or 0)
            if failed_cycles:
                parts = []
                if p2_flags.get("manifest_collision"):
                    parts.append(f"현신 접촉 이벤트 {phase2.get('manifestCollisionEpisodeCount', 0)}회 · 연계사망 {phase2.get('manifestCollisionLinkedDeathCount', 0)}명")
                if p2_flags.get("wail_interrupt_failure"):
                    parts.append(f"통곡 차단 실패 {phase2.get('wailFailureEpisodeCount', 0)}회")
                if p2_flags.get("soul_sever_non_tank_hit"):
                    parts.append(f"Soul Sever 비탱커 피격 {phase2.get('soulSeverNonTankHitCount', 0)}명")
                if p2_flags.get("gloombomb_extra_hit"):
                    parts.append(f"암영폭탄 추가 피격 {phase2.get('gloombombExtraHitCycleCount', 0)}사이클")
                if p2_flags.get("gloombomb_before_first_interrupt"):
                    parts.append(f"암영폭탄 1차단 전 적중 {phase2.get('gloombombBeforeFirstInterruptCycleCount', 0)}사이클")
                if p2_flags.get("gloombomb_soulcoiler_hit_missing"):
                    parts.append(f"암영폭탄 차단쫄 비적중 {phase2.get('gloombombSoulcoilerMissCycleCount', 0)}사이클")
                if p2_flags.get("prior_add_overlap"):
                    parts.append(f"이전 차단쫄 다음 웨이브 중첩 {phase2.get('priorAddOverlapCycleCount', 0)}사이클")
                if p2_flags.get("gravebound_incomplete"):
                    parts.append(f"영혼 3개/15초 미완료 {phase2.get('graveboundIncompleteCount', 0)}명")
                checks.append(make_check(
                    key="phase2_opening", title="P2 정배·차단쫄·암영폭탄", status="failed",
                    evidence_level=EVIDENCE_VERIFIED,
                    summary=" · ".join(parts) or f"P2 {failed_cycles}개 사이클에서 검증된 처리 이상",
                    details=["차단 성공→현신 일시 노출은 공략 명시. 현신 실제 시야 노출 자체는 전투 로그로 직접 판정하지 않음."],
                    ids={"wail": 1286399, "manifestCollision": [1310744, 1312132], "gloombomb": [1310881, 1310883], "soulSever": 1286620, "soulcoilerNpcID": 261521},
                ))
            elif phase2.get("cycleCount"):
                all_success = all(x.get("status") == "success" for x in (phase2.get("cycles") or []))
                checks.append(make_check(
                    key="phase2_opening", title="P2 정배·차단쫄·암영폭탄", status="success" if all_success else "unknown",
                    evidence_level=EVIDENCE_VERIFIED if all_success else EVIDENCE_UNAVAILABLE,
                    summary=(f"관측된 P2 {phase2.get('cycleCount')}사이클에서 현신 접촉/차단 실패/암영폭탄 추가 피격/암영폭탄 차단쫄 비적중/이전 쫄 중첩 미관측" if all_success else f"P2 {phase2.get('cycleCount')}사이클 관측 · 완전 판정 데이터 부족"),
                    ids={"wail": 1286399, "manifestCollision": [1310744, 1312132], "gloombomb": [1310881, 1310883], "soulSever": 1286620, "soulcoilerNpcID": 261521},
                ))

    if mythic_rule_applied:
        residual = tank_handoff.get("secondGuillotineResidual")
        hmax = tank_handoff.get("tankHandoffMax")
        load_status = tank_handoff.get("handoffLoadStatus")
        if residual is None:
            handoff_status = "unknown"
            handoff_summary = "2차 단두대 잔존 수 확인 불가"
        elif load_status == "warning":
            handoff_status = "warning"
            handoff_summary = f"2차 단두대 잔존 {int(residual)}개 · P2 전 정리 대상 · raidPlan 상한 {int(hmax)}개 초과"
        else:
            handoff_status = "unknown"
            limit_text = f" · 상한 {int(hmax)}개 이내" if hmax is not None else ""
            handoff_summary = f"2차 단두대 잔존 {int(residual)}개 · P2 전 정리 정보값{limit_text}"
        checks.append(make_check(
            key="tank_handoff", title="2단두대 이후 잔여 구슬 정리량", status=handoff_status,
            evidence_level=tank_handoff.get("evidenceLevel", EVIDENCE_UNAVAILABLE),
            summary=handoff_summary,
            details=[tank_handoff.get("note")],
            ids={"guillotineMarkerSpellID": 1283485},
        ))

        cleanup_status = str(tank_handoff.get("p2EntryCleanupStatus") or "unknown")
        if cleanup_status == "success":
            entry_status = "success"
        elif cleanup_status == "failed":
            entry_status = "failed"
        else:
            entry_status = "unknown"
        cleanup_signal = tank_handoff.get("cleanupSignal") or {}
        details = [tank_handoff.get("note")]
        if cleanup_signal.get("note"):
            details.append(cleanup_signal.get("note"))
        checks.append(make_check(
            key="p2_entry_orbs", title="P2 진입 잔여 구슬", status=entry_status,
            evidence_level=(EVIDENCE_VERIFIED if cleanup_status in {"success", "failed"} else EVIDENCE_UNAVAILABLE),
            summary=tank_handoff.get("cleanupSummary") or "P2 진입 잔여 구슬 판정 불가",
            details=details,
            ids={"ruptureSpellID": 1299838, "phase2ActivationSpellID": 1288624},
        ))

        if shield_cycles:
            statuses = [str(x.get("status") or "unknown") for x in shield_cycles]
            if "failed" in statuses:
                shield_status = "failed"
            elif "warning" in statuses:
                shield_status = "warning"
            elif statuses and all(x == "success" for x in statuses):
                shield_status = "success"
            else:
                shield_status = "unknown"
            shield_bits = []
            for x in shield_cycles:
                bit = f"#{x.get('index')} {x.get('status')}"
                if x.get("breakDurationSec") is not None:
                    bit += f" {x.get('breakDurationSec'):.1f}s"
                if x.get("remainingPct") is not None and x.get("status") == "failed":
                    bit += f" · 잔여 {x.get('remainingPct'):.1f}%"
                shield_bits.append(bit)
            checks.append(make_check(
                key="eternal_sunset", title="P2 보호막 · 영원한 일몰", status=shield_status,
                evidence_level=EVIDENCE_VERIFIED,
                summary=" · ".join(shield_bits),
                details=["보호막 파괴와 1286918 실제 차단을 함께 성공으로 판정. 시전 완료 전 전멸은 성공 처리하지 않음."],
                ids={"shieldAuraSpellID": 1286912, "nightfallCastSpellID": 1286918, "nightfallAuraSpellID": 1310752},
            ))
        elif phase2_schedule_anchor_sec is not None:
            first_slot = next((x for x in (shield_observation_plan.get("slots") or []) if safe_int(x.get("index")) == 1), {})
            remaining = first_slot.get("timeToTargetAtEndSec")
            if first_slot.get("state") == "awaiting" and remaining is not None:
                summary = (
                    f"첫 보호막 관측 대기 · NSRT P2+{int(first_slot.get('targetOffsetSec') or 70)}초 목표까지 "
                    f"약 {float(remaining):.1f}초 전 트라이 종료"
                )
            elif first_slot.get("state") == "target_passed_unobserved":
                summary = "NSRT 첫 보호막 목표 시각은 지났으나 1286912 적용 신호 미관측 · 이벤트 스트림/실제 일정 확인 필요"
            else:
                summary = "P2 도달 · 보호막 1286912 적용 신호 미관측(첫 보호막 도달 전 종료 또는 해당 이벤트 스트림 미수집)"
            checks.append(make_check(
                key="eternal_sunset", title="P2 보호막 · 영원한 일몰", status="unknown",
                evidence_level=EVIDENCE_UNAVAILABLE,
                summary=summary,
                details=[
                    "관측 대기: 보호막 1286912 초기 흡수량/파괴시간/잔여량",
                    "관측 대기: 영원한 일몰 1286918 시전 시작·실제 시전시간·차단/완료",
                    "NSRT 70/155초는 P2 기준 예상 시각으로 기록만 하며 충분한 실제 표본 전에는 경고 임계값으로 사용하지 않음.",
                ],
                ids={"shieldAuraSpellID": 1286912, "nightfallCastSpellID": 1286918, "nightfallAuraSpellID": 1310752},
            ))

        configured_alignments = [x.get("cooldownAlignment") or {} for x in shield_cycles if (x.get("cooldownAlignment") or {}).get("requirementsConfigured")]
        if configured_alignments:
            align_statuses = [str(x.get("status") or "unknown") for x in configured_alignments]
            align_status = "failed" if "failed" in align_statuses else "unknown" if "unknown" in align_statuses else "success"
            fail_count = sum(
                len(x.get("failures") or [])
                + int(((x.get("autoSpecCooldowns") or {}).get("judgedLateCount") or 0))
                + int(((x.get("autoSpecCooldowns") or {}).get("judgedMissingCount") or 0))
                for x in configured_alignments
            )
            checks.append(make_check(
                key="shield_cooldown_alignment", title="보호막 쿨기 정렬", status=align_status,
                evidence_level=EVIDENCE_VERIFIED,
                summary=f"raidPlan 지정 보호막 {len(configured_alignments)}회 · 미사용/지연 항목 {fail_count}개",
                details=["물약 분모는 보호막 시작 시점 관측 생존자. 자동 전문화 쿨기는 entryPolicies의 judge 항목만 이 집계와 원인 A에 합류."],
            ))

    # B37.4.33: priorityChecks are fallbacks for mechanics without a concrete
    # analyzer check.  Never append a generic placeholder when the same key was
    # already produced from actual evidence (notably eternal_sunset).
    concrete_check_keys = {str(x.get("key") or "") for x in checks if isinstance(x, dict)}
    for item in (rule.get("priorityChecks") or []) if mythic_rule_applied else []:
        key = item.get("key") or "unknown"
        if key in concrete_check_keys:
            continue
        if key == "second_guillotine" and len(guillotine_markers) >= 2:
            g2_state = next((x for x in (orb_accounting.get("guillotineStates") or []) if int(x.get("guillotineIndex") or 0) == 2), None)
            if g2_state and g2_state.get("remainingOrbs") is not None:
                remain = int(g2_state.get("remainingOrbs") or 0)
                hmax = tank_handoff.get("tankHandoffMax")
                over = hmax is not None and remain > int(hmax)
                checks.append(make_check(
                    key=key, title=item.get("title") or "두 번째 단두대",
                    status="warning" if over else "success" if hmax is not None else "unknown",
                    evidence_level=EVIDENCE_VERIFIED,
                    summary=(
                        f"{_fmt_time(guillotine_markers[1]['sec'])} · 2차 단두대 잔존 {remain}개 = P2 전 정리 대상"
                        + (f" · raidPlan 상한 {int(hmax)}개 초과" if over else f" · 상한 {int(hmax)}개 이내" if hmax is not None else "")
                    ),
                    ids={"markerSpellIDs": sorted(guillotine_marker_ids)},
                ))
            else:
                checks.append(make_check(
                    key=key, title=item.get("title") or "두 번째 단두대", status="unknown",
                    evidence_level=EVIDENCE_VERIFIED,
                    summary=f"{_fmt_time(guillotine_markers[1]['sec'])} · 2차 단두대 기준점 관측 · active_orbs 계산은 중간에 unknown이 되어 잔존 수 판정 보류",
                    ids={"markerSpellIDs": sorted(guillotine_marker_ids)},
                ))
        else:
            checks.append(make_check(
                key=key,
                title=item.get("title") or "기믹",
                status="unknown",
                evidence_level=EVIDENCE_UNAVAILABLE,
                summary=(f"로그 판정 규칙 미확정 · 공개 와이프 표 참고 {item.get('referenceRatePct')}%" if item.get("referenceRatePct") is not None else "로그 판정 규칙 미확정"),
            ))

    if not mythic_rule_applied:
        verdict = {
            "severity": "INFO", "status": "unknown", "evidenceLevel": EVIDENCE_VERIFIED,
            "primary": "신화 규칙 미적용 · 신호만 수집",
            "why": f"난이도 ID {encounter.get('difficultyID') or encounter.get('difficulty')}는 신화 규칙 적용 대상이 아닙니다. 브레스 {len(breaths)}회와 구슬 웨이브 {len(orb_waves)}회는 참고 신호로만 표시합니다.",
            "nextAction": "신화 공대 전략 수치와 성공/실패 판정은 적용하지 않습니다.",
        }
    elif breaths:
        counts = [x.get("destroyedOrbs") for x in observations]
        flag_counts = orb_accounting.get("flagCounts") or {}
        overall = orb_accounting.get("overallStatus") or "unknown"
        if overall == "failed":
            severity = "HIGH" if (flag_counts.get("zero_breath") or flag_counts.get("guillotine_remaining")) else "WARN"
            verdict = {
                "severity": severity,
                "status": "failed",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "primary": "똬리 신화 구슬 처리 이상 감지",
                "why": (
                    f"브레스별 제거 구슬 {counts}. "
                    f"첫 브레스 부족 {flag_counts.get('first_breath_short', 0)}회 · "
                    f"0개 브레스 {flag_counts.get('zero_breath', 0)}회 · "
                    f"7개 이상 동시 제거 {flag_counts.get('high_burst', 0)}회 · "
                    f"단두대 전 잔존 {flag_counts.get('guillotine_remaining', 0)}회."
                ),
                "nextAction": "첫 브레스에 목표 수량을 먼저 넣고, 1차 단두대 직전 잔존 0개를 확인합니다. 2차 단두대 잔존은 P2 전 정리량으로 별도 확인합니다.",
            }
        elif overall == "warning":
            warning_breaths = [x for x in observations if x.get("strategyApplies") and not x.get("judgmentExcluded") and x.get("status") == "warning"]
            warning_residuals = [x for x in (orb_accounting.get("guillotineStates") or []) if not x.get("judgmentExcluded") and x.get("status") == "warning"]
            verdict = {
                "severity": "WARN",
                "status": "warning",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "primary": "P1 구슬 처리 위험 관측 · 직접 실패 미확정",
                "why": (
                    f"브레스별 제거 구슬 {counts}. "
                    f"목표 범위 밖이지만 7개 미만인 짝수 브레스 {len(warning_breaths)}회 · "
                    f"단두대 전 잔존 관측 {len(warning_residuals)}회. "
                    "이 값만으로 firstMistake를 만들지 않습니다."
                ),
                "nextAction": "짝수 브레스 처리량과 단두대 직전 잔존을 계속 관측하고, 실제 사망/붕괴 연결이 있는 경우만 실패로 승격합니다.",
            }
        elif overall == "success":
            verdict = {
                "severity": "INFO",
                "status": "success",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "primary": "관측된 구슬 처리 전략 충족",
                "why": f"브레스별 제거 구슬 {counts}. 관측된 전략 브레스 범위와 단두대 전 잔존 0개 조건을 충족했습니다.",
                "nextAction": "구슬 처리 외의 붕괴 신호를 확인합니다.",
            }
        else:
            verdict = {
                "severity": "INFO",
                "status": "unknown",
                "evidenceLevel": EVIDENCE_VERIFIED if any(x is not None for x in counts) else EVIDENCE_UNAVAILABLE,
                "primary": "구슬 제거 수 확인 · 전체 전략 판정은 진행 중",
                "why": f"브레스별 제거 구슬 {counts}. 트라이 종료 시점 때문에 단두대 기준 전체 쌍을 모두 확인하지 못했습니다.",
                "nextAction": "도달한 브레스의 제거 수와 active_orbs 이월을 확인하고, 다음 단두대까지 이어지는 트라이에서 잔존 0개 여부를 봅니다.",
            }
    else:
        verdict = {
            "severity": "INFO", "status": "unknown", "evidenceLevel": EVIDENCE_UNAVAILABLE,
            "primary": "보스 브레스 미관측",
            "why": "NPC source가 완료 시전한 검증 spellID 1299684를 찾지 못했습니다.",
            "nextAction": "조기 전멸인지 확인하고, 신화 전용 기술/단두대 spellID를 probe에서 먼저 식별합니다.",
        }

    # Once P2 is reached, promoted P2 failures take precedence over an older P1
    # verdict. B36.4 keeps temporal "첫 실수" separate from evidence-backed
    # "붕괴 직결" so a plausible sequence is never presented as proven causality.
    if mythic_rule_applied and phase2.get("reached") and int(phase2.get("failedCycleCount") or 0) > 0:
        p2f = phase2.get("flagCounts") or {}
        p2_summary = phase2.get("summary") or {}
        first = p2_summary.get("firstMistake") or {}
        direct = p2_summary.get("collapseDirect") or {}
        why_parts = []
        if p2f.get("manifest_collision"):
            why_parts.append(f"현신 접촉 이벤트 {phase2.get('manifestCollisionEpisodeCount', 0)}회 · resonance 연계사망 {phase2.get('manifestCollisionLinkedDeathCount', 0)}명")
        if p2f.get("wail_interrupt_failure"):
            why_parts.append(f"공포의 통곡 차단 실패 {phase2.get('wailFailureEpisodeCount', 0)}회")
        if p2f.get("soul_sever_non_tank_hit"):
            why_parts.append(f"Soul Sever 비탱커 피격 {phase2.get('soulSeverNonTankHitCount', 0)}명")
        if p2f.get("gloombomb_extra_hit"):
            why_parts.append(f"암영폭탄 추가 피격 {phase2.get('gloombombExtraHitCycleCount', 0)}사이클")
        if p2f.get("gloombomb_before_first_interrupt"):
            why_parts.append(f"암영폭탄 1차단 전 적중 {phase2.get('gloombombBeforeFirstInterruptCycleCount', 0)}사이클")
        if p2f.get("gloombomb_soulcoiler_hit_missing"):
            why_parts.append(f"암영폭탄 차단쫄 비적중 {phase2.get('gloombombSoulcoilerMissCycleCount', 0)}사이클")
        if p2f.get("prior_add_overlap"):
            why_parts.append(f"이전 차단쫄이 다음 웨이브까지 생존 {phase2.get('priorAddOverlapCycleCount', 0)}사이클")

        if direct:
            p2_primary = f"P2 붕괴 직결 · {direct.get('label') or '검증된 실패'}"
            p2_severity = "CRITICAL" if direct.get("kind") == "manifest_collision" and int(direct.get("linkedDeathCount") or 0) > 0 else "HIGH"
        elif first:
            p2_primary = f"P2 첫 실수 · {first.get('label') or '검증된 실패'}"
            # A verified Manifestation contact followed by a directly linked death
            # remains CRITICAL even when it was not the *first* mistake and the
            # generic 4-deaths/1s raid-collapse threshold was not reached.
            if int(phase2.get("manifestCollisionLinkedDeathCount") or 0) > 0:
                p2_severity = "CRITICAL"
            else:
                p2_severity = "HIGH" if first.get("kind") in {"manifest_collision", "wail_interrupt_failure", "soul_sever_non_tank_hit"} else "WARN"
        else:
            p2_primary = "P2 처리 이상 감지"
            p2_severity = "HIGH"

        ordered = []
        if p2_summary.get("oneLine"):
            ordered.append(str(p2_summary.get("oneLine")))
        if first:
            ordered.append(f"첫 실수 {_fmt_time(first.get('sec'))} {first.get('label')}: {first.get('summary') or ''}".rstrip())
        if direct:
            ordered.append(f"붕괴 직결 {_fmt_time(direct.get('sec'))} {direct.get('label')} → 1초 내 {int(p2_summary.get('collapseDeathCount1s') or 0)}명 사망")
        elif p2_summary.get("collapseSec") is not None:
            preceding = p2_summary.get("collapsePreceding") or {}
            if preceding:
                ordered.append(f"붕괴 직결 근거 없음 · 직전 실패 {_fmt_time(preceding.get('sec'))} {preceding.get('label')}")
            else:
                ordered.append("붕괴 직결 근거 없음")
        else:
            ordered.append("붕괴 직결: 1초 내 4명 이상 사망 군집 미관측")

        if why_parts:
            ordered.append("검증 실패: " + " · ".join(why_parts))
        verdict = {
            "severity": p2_severity,
            "status": "failed",
            "evidenceLevel": EVIDENCE_VERIFIED,
            "primary": p2_primary,
            "why": " / ".join(ordered),
            "nextAction": "첫 실수와 붕괴 직결을 따로 확인합니다. 현신 접촉/통곡/암영폭탄/차단쫄 중첩 중 직접 근거가 있는 항목만 원인 화살표로 연결합니다.",
        }

    p1_exec_first = p1_execution.get("firstMistake") or {}
    p1_orb_first = orb_failures[0] if orb_failures else {}
    p2_first_overall = ((phase2.get("summary") or {}).get("firstMistake") or {})
    overall_first_candidates = [x for x in (p1_exec_first, p1_orb_first, p2_first_overall) if isinstance(x, dict) and x.get("sec") is not None]
    first_verified_failure = min(overall_first_candidates, key=lambda x: float(x.get("sec") or 0)) if overall_first_candidates else None
    if first_verified_failure and first_verified_failure.get("kind") in {"p1_tank_breath_non_tank_hit", "p1_glaive_death", "p1_orb_failure"}:
        p1_first = first_verified_failure
        verdict = {
            "severity": "HIGH" if p1_first.get("kind") != "p1_orb_failure" else "WARN",
            "status": "failed",
            "evidenceLevel": EVIDENCE_VERIFIED,
            "primary": f"P1 첫 실수 · {p1_first.get('label') or '검증된 실행 실패'}",
            "why": f"첫 실수 {_fmt_time(p1_first.get('sec'))} {p1_first.get('label')}: {p1_first.get('summary') or ''}".rstrip(),
            "nextAction": (
                "다음 풀: 줄잔 브레스 전방은 탱커만 맞고 비탱커 오피격을 0회로 만드세요."
                if p1_first.get("kind") == "p1_tank_breath_non_tank_hit"
                else "다음 풀: 글레이브 연속 피격을 0회로 줄이고, 특히 맹독성 파열 디버프 중에는 글레이브를 최우선으로 피하세요."
                if p1_first.get("kind") == "p1_glaive_death"
                else "다음 풀: 짝수 브레스 처리량을 낮추고 다음 단두대 직전 잔존 0개를 확인하세요."
            ),
        }

    discovery_ids = {
        "guillotineSpellIDs": sorted({safe_int(x.get("spellID") or x.get("abilityGameID")) for x in diagnostic_guillotine if safe_int(x.get("spellID") or x.get("abilityGameID")) is not None}),
        "toxicRuptureSpellIDs": sorted({safe_int(x.get("spellID") or x.get("abilityGameID")) for x in diagnostic_rupture if safe_int(x.get("spellID") or x.get("abilityGameID")) is not None}),
        "condensedVenomCandidateSpellIDs": sorted({sid for ob in observations for p in ob.get("candidateCondensedVenomPulses") or [] for sid in (p.get("spellIDs") or [])}),
    }

    calibration = []
    if mythic_rule_applied:
        for gi in (1, 2):
            assigned = [x for x in observations if ((x.get("strategyAssignment") or {}).get("guillotineIndex") == gi)]
            if not assigned:
                continue
            anchor_wave_index = (assigned[0].get("strategyAssignment") or {}).get("anchorOrbWaveIndex")
            wave = next((w for w in orb_waves if int(w.get("index") or 0) == int(anchor_wave_index or -1)), None)
            known = [x for x in assigned if x.get("destroyedOrbs") is not None]
            removed_sum = sum(int(x.get("destroyedOrbs") or 0) for x in known) if len(known) == len(assigned) else None
            gstate = next((x for x in (orb_accounting.get("guillotineStates") or []) if int(x.get("guillotineIndex") or 0) == gi), None)
            calibration.append({
                "guillotineIndex": gi,
                "anchorOrbWaveIndex": anchor_wave_index,
                "spawnedOrbCount": (wave or {}).get("summonCount"),
                "coalescedCount": (wave or {}).get("coalescedCount"),
                "mutationCount": (wave or {}).get("mutationCount"),
                "strategySlots": [x.get("strategySlot") for x in assigned],
                "verifiedRemovedOrbs": [x.get("destroyedOrbs") for x in assigned],
                "verifiedRemovedSum": removed_sum,
                "remainingOrbsAtGuillotine": (gstate or {}).get("remainingOrbs"),
                # Kept for compatibility with B35.2/B35.3 readers; no longer
                # used as the orb-count signal.
                "candidateRuptureBurstCount": sum(int(x.get("candidateVenomRuptureBurstCount") or 0) for x in assigned),
                "evidenceLevel": EVIDENCE_VERIFIED if removed_sum is not None else EVIDENCE_UNAVAILABLE,
                "note": "브레스 후 1초 apply/apply-stack 플레이어 공통값을 제거 구슬 수로 사용. 파열 순간 묶음 수는 판정에 사용하지 않음.",
            })

    _coiled_apply_split_evidence(phase2, timeline, rule, source_kind)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "engineVersion": ENGINE_VERSION,
        "rulesVersion": RULES_VERSION,
        "bossKey": "coiled_altar",
        "sourceKind": source_kind,
        "encounter": {
            "name": encounter.get("name") or encounter.get("encounterName") or rule.get("displayName"),
            "encounterID": encounter.get("encounterID"),
            "difficultyID": encounter.get("difficultyID") or encounter.get("difficulty"),
            "tryNo": encounter.get("tryNo"),
            "result": encounter.get("result") or ("Kill" if encounter.get("kill") else "Wipe"),
            "durationSec": encounter.get("durationSec"),
            "startTimestamp": encounter.get("startTimestamp"),
            "endTimestamp": encounter.get("endTimestamp"),
        },
        "rule": {"bossKey": rule.get("bossKey"), "schemaVersion": rule.get("schemaVersion"), "idBased": True, "mythicRuleApplied": mythic_rule_applied},
        "strategy": {
            "source": strategy_source, "values": strategy, "userOverridePath": "data/boss_strategy.json",
            "applied": mythic_rule_applied,
            "assignmentBasis": "two breaths before Guillotine 1/2",
            "countBasis": "응결된 맹독 + 악성 돌연변이 포함",
            "raidPlan": (strategy or {}).get("raidPlan") or {},
            "referenceTimeline": (strategy or {}).get("referenceTimeline") or {},
        },
        "checks": checks,
        "timeline": sorted(timeline, key=lambda x: float(x.get("sec") or 0)),
        "observations": {
            "breaths": observations,
            "breathCount": len(breaths),
            "orbWaves": orb_waves,
            "orbWaveCount": len(orb_waves),
            "guillotineMarkers": guillotine_markers,
            "guillotineCount": len(guillotine_markers),
            "sameNameCleaveCompletedCount": len(same_name_cleaves),
            "ignoredSameNameCleaveCount": len(ignored_same_name),
            "discoveryOnly": discovery_ids,
            "ruptureBurstCalibration": calibration,
            "orbAccounting": orb_accounting,
            "orbFailures": orb_failures,
            "orbCausalChainCandidate": orb_causal_chain,
            "phase1Transition": phase1_transition,
            "p1Execution": p1_execution,
            "firstVerifiedFailure": first_verified_failure,
            "phase2": phase2,
            "phase2Cycles": phase2.get("cycles") or [],
            "tankHandoff": tank_handoff,
            "shieldCycles": shield_cycles,
            "shieldObservationPlan": shield_observation_plan,
            "shieldScheduleAnchorSec": phase2_schedule_anchor_sec,
            "shieldScheduleAnchorBasis": phase2_schedule_anchor_basis,
            "shieldScheduleAnchorBasis": "P2_entry" if phase1_transition.get("entrySec") is not None else "malacrass_activation_fallback",
            "collapseSnapshot": collapse_snapshot,
            "survivalContext": {"raidSizeObserved": survival_context.get("raidSize")},
        },
        "firstMistake": first_verified_failure,
        "verdict": verdict,
        "collapseLink": {"status": "unknown", "evidenceLevel": EVIDENCE_UNAVAILABLE, "summary": "B31 붕괴 체인과 아직 연결되지 않음"},
        "limitations": [
            "기믹 판정 규칙은 spellID/NPC ID/source 조건만 사용합니다. 이름 매칭은 probe용 ID 발견에만 사용하며 성공/실패 판정에는 쓰지 않습니다.",
            "줄잔 boss NPC ID 257911, Sever aura marker 1301690, Guillotine marker 1283485, Venom Rupture 1299838은 3개 신화 킬의 fight-scoped 이벤트에서 교차 확인했습니다.",
            "브레스 후 1초 안의 맹독성 파열 1299838 apply/apply-stack 이벤트를 플레이어별로 세고 전원 공통값일 때 제거 구슬 수로 사용합니다. refresh 이벤트는 제외합니다.",
            "구슬 소환 총량과 검증된 제거 수로 active_orbs를 시간순 누적하며, 제거 수가 확인 불가가 되는 순간 이후 잔존 수는 추측하지 않습니다.",
            "1초 내 4명 이상 사망한 의미 있는 붕괴 시점 이후, 또는 관측 생존 인원이 공대의 절반 미만인 시점의 브레스/구슬 상태는 원시 수치만 보존하고 전략·원인 집계에서 제외합니다.",
            "신화 공대 전략은 각 단두대 직전의 마지막 두 브레스에만 연결하며, 그 외 브레스는 전략 없음/참고로 둡니다.",
            "공대 전략 개수는 응결된 맹독과 악성 돌연변이를 모두 포함합니다.",
            "공대 전략 값은 data/boss_strategy.json 사용자 override가 있으면 우선합니다.",
            "P1→P2 실제 진입 시각은 줄잔 HP resource snapshot이 0이 되는 시점으로 판정합니다. 말라크라스 1288624 활성은 P2 분석 앵커일 뿐 진입 시각으로 등치하지 않습니다.",
            "2:45 줄잔 HP는 로컬 고급 전투 기록 snapshot 또는 B36 WCL includeResources 데이터가 있을 때만 표시합니다. 없는 기존 WCL ZIP에서는 확인 불가로 둡니다.",
            "P1 탱 브레스 1299684의 플레이어 직접 피해는 로스터/raidPlan 탱커 판정과 대조해 비탱커 피격을 verified 실행 실패로 기록합니다. 글레이브 1285017은 연속 피격 직후 동일 플레이어 사망이 직접 연결될 때만 실패로 승격하며, 맹독성 파열 1299838 활성 중 겹친 타격을 별도 표시합니다.",
            "B37.4.18부터 짝수 브레스의 계획 범위 이탈만으로는 실패를 만들지 않습니다. 7개 이상 동시 제거는 verified 고중첩 실패로 유지하고, 7개 미만 범위 이탈 및 단두대 잔존은 위험/관측으로 보존합니다. 잔존은 별도 직접 사망/붕괴 계약이 생길 때만 실패로 승격합니다.",
            "P2 공포의 통곡 1286399가 플레이어에게 적용되면 차단 실패로 verified 판정합니다. 차단 성공 시 현신 일시 노출은 공략 명시지만 시야 노출 자체는 전투 로그로 직접 확인하지 않습니다.",
            "암영폭탄은 공대 운영 기준상 1차 공포의 통곡 차단을 확인한 뒤 영혼살무사에 적중합니다. 로그에서는 첫 차단→1310883 폭발 순서와 시간차를 기록하고, 폭발이 첫 차단보다 앞선 경우만 verified 순서 실패로 승격합니다. '바로'의 최대 지연 시간은 별도 임계값으로 단정하지 않습니다.",
            "암영폭탄 대상 4명보다 1310883 고유 플레이어 피격자가 많으면 추가 피격으로 판정합니다. 차단쫄 적중 성공은 폭발 직후 1~2초의 영혼살무사 실제피해/감소전피해 합계 비율이 약 1% 보호 상태에서 정상 비율로 전환되는지를 우선 사용합니다. 직접 1310883 add-hit 0건만으로 실패를 만들지 않습니다.",
            "B36.4 종합 판정의 첫 실수는 promoted verified 실패 중 시간상 첫 사건이며, 붕괴 직결은 1초 내 4명 이상 사망 군집에 직접 피해/사망 연결 근거가 있을 때만 별도 승격합니다. 단순 선후관계에는 원인 화살표를 붙이지 않습니다.",
            "Soul Sever 탱커 판정은 raidPlan.phase2.tankPlayers 수동 입력을 최우선으로 사용하고, 비어 있으면 COMBATANT_INFO/roster specID의 탱 전문화 목록으로 자동 추론합니다.",
            "정배(Dreadmarch)와 Gravebound/영혼 3개 신호는 B36.4에서 수집만 하고 성공·실패 계약이 확정되기 전까지 '확인 불가'로 유지하며 첫 실수/붕괴 원인에 사용하지 않습니다.",
            "이전 차단쫄이 다음 차단쫄 웨이브 소환 뒤에도 Wail을 시작하면 '이전 쫄 생존/웨이브 중첩'으로만 판정하며, 제거 실패의 원인은 단정하지 않습니다.",
            "차단 순번 위반은 data/boss_strategy.json raidPlan.phase2.interruptAssignments에 담당 순서가 입력되고 Interrupts 이벤트가 있을 때만 판정할 수 있습니다.",
            "B36.5에서 2차 단두대 잔존은 인계/정리 정보값으로 취급합니다. 마지막 브레스 뒤~P2 앵커 사이 1299838 일괄 파열을 0.15초 클러스터로 묶어 플레이어별 공통값이 active_orbs와 일치할 때 P2 진입 잔존을 verified 판정합니다. 이 신호만으로 실제 처리 담당자를 단정하지 않습니다.",
            "P2 보호막은 황혼의 장막 1286912 적용/제거, SPELL_ABSORBED, 영원한 일몰 1286918 시작/완료/차단을 묶어 ShieldCycle로 판정합니다. 시전 완료 전 전멸/로그 종료는 성공으로 추정하지 않습니다.",
            "B36.5.2는 자동 전문화 카탈로그를 후보층으로 유지하고 raidPlan entryPolicies에서 spec+보호막+spell 단위로 judge/observe를 결정합니다. 명시 judge만 보호막 실패 원인 A에 합류하며 observe는 미사용/지연을 보여도 원인으로 단정하지 않습니다.",
        ],
    }


def _twin_spell_ids(rule: dict, key: str, field: str = "spellIDs") -> set[int]:
    spec = ((rule.get("mechanics") or {}).get(key) or {})
    return {safe_int(x) for x in (spec.get(field) or []) if safe_int(x) is not None}


def _twin_aura_stack(row: dict) -> int | None:
    for key in ("auraStack", "stack", "stacks", "stackCount", "stack_count"):
        value = safe_int(row.get(key))
        if value is not None:
            return value
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    for key in ("stack", "stacks", "stackCount", "stack_count", "amount"):
        value = safe_int(raw.get(key))
        if value is not None:
            return value
    # Local SPELL_AURA_APPLIED_DOSE stores the stack in auraAmount. Do not use
    # generic auraAmount for non-dose rows because B36.5 absorb auras use it as
    # a numeric shield pool rather than a stack count.
    if _event_name(row) in {"spell_aura_applied_dose", "spell_aura_removed_dose", "applydebuffstack"}:
        return safe_int(row.get("auraAmount"))
    return None


def _twin_player_label(row: dict) -> str:
    return str(row.get("targetName") or row.get("destName") or row.get("destGUID") or row.get("targetID") or "?").split("-")[0]


def _twin_source_key(row: dict):
    guid = row.get("sourceGUID")
    if guid:
        return guid
    source_id = row.get("sourceID")
    source_instance = row.get("sourceInstance")
    if source_id is not None or source_instance is not None:
        return (source_id, source_instance)
    return row.get("sourceName")


def _twin_eternal_venom(rows: list[dict], rule: dict, strategy: dict) -> dict:
    ids = _twin_spell_ids(rule, "eternalVenom")
    raid_plan = (strategy.get("raidPlan") or {}) if isinstance(strategy, dict) else {}
    threshold = int(raid_plan.get("eternalVenomDeathAt") or 10)
    warning_at = int(raid_plan.get("eternalVenomWarningAt") or max(1, threshold - 1))
    current: dict[object, int] = {}
    names: dict[object, str] = {}
    max_by_player: dict[object, dict] = {}
    threshold_events = []
    stack_signal_count = 0
    for row in rows:
        sid = safe_int(row.get("spellID") if row.get("spellID") is not None else row.get("abilityGameID"))
        if sid not in ids or not _is_player_target(row):
            continue
        ev = _event_name(row)
        key = _target_key(row)
        if key is None:
            continue
        names[key] = _twin_player_label(row)
        if ev in {"spell_aura_removed", "removedebuff", "spell_aura_removed_dose", "removedebuffstack"}:
            stack = _twin_aura_stack(row)
            current[key] = max(0, int(stack if stack is not None else 0))
            continue
        if ev not in {
            "spell_aura_applied", "applydebuff", "spell_aura_applied_dose", "applydebuffstack",
            "refreshdebuff", "spell_aura_refresh",
        }:
            continue
        stack = _twin_aura_stack(row)
        if stack is None:
            if ev in {"spell_aura_applied", "applydebuff"}:
                stack = 1
            elif ev in {"spell_aura_applied_dose", "applydebuffstack"}:
                stack = int(current.get(key, 1)) + 1
        if stack is None:
            continue
        stack_signal_count += 1
        current[key] = int(stack)
        prior = max_by_player.get(key)
        if prior is None or int(stack) > int(prior.get("maxStack") or 0):
            max_by_player[key] = {
                "player": names[key], "maxStack": int(stack), "sec": round(float(row.get("sec") or 0), 3)
            }
        if int(stack) >= threshold:
            threshold_events.append({
                "sec": round(float(row.get("sec") or 0), 3), "player": names[key], "stack": int(stack),
            })
    maxima = sorted(max_by_player.values(), key=lambda x: (-int(x.get("maxStack") or 0), float(x.get("sec") or 0), str(x.get("player") or "")))
    max_stack = max((int(x.get("maxStack") or 0) for x in maxima), default=None)
    warning_players = [x for x in maxima if int(x.get("maxStack") or 0) >= warning_at]
    return {
        "threshold": threshold,
        "warningAt": warning_at,
        "maxStack": max_stack,
        "maxima": maxima,
        "warningPlayers": warning_players,
        "thresholdEvents": threshold_events,
        "stackSignalCount": stack_signal_count,
        "available": bool(stack_signal_count),
    }


def _twin_interrupt_summary(rows: list[dict], rule: dict, mechanic_key: str) -> dict:
    ids = _twin_spell_ids(rule, mechanic_key)
    starts = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids and _is_cast_start(x)]
    completes = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids and _is_completed_cast(x)]
    interrupts = [x for x in rows if _is_interrupt(x) and safe_int(x.get("extraSpellID") or x.get("extraAbilityGameID")) in ids]
    return {"starts": starts, "completes": completes, "interrupts": interrupts}


def _twin_visceral(rows: list[dict], rule: dict) -> dict:
    cast_ids = _twin_spell_ids(rule, "visceralBurstCast")
    effect_ids = _twin_spell_ids(rule, "visceralBurstEffect")
    starts = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in cast_ids and _is_cast_start(x)]
    completes = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in cast_ids and _is_completed_cast(x)]
    interrupts = [x for x in rows if _is_interrupt(x) and safe_int(x.get("extraSpellID") or x.get("extraAbilityGameID")) in cast_ids]
    effects = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in effect_ids and (_is_damage(x) or _is_aura_apply(x))]

    # A successful cast and its raid-wide effect are the same failed broodling.
    # Merge by short time group + source so twenty damage rows never become
    # twenty separate failures.
    failure_rows = completes + effects
    episodes = []
    for group in _group_times(failure_rows, max_gap=0.75):
        source_keys = {_twin_source_key(x) for x in group if _twin_source_key(x) is not None}
        episodes.append({
            "sec": round(min(float(x.get("sec") or 0) for x in group), 3),
            "failedBroodlings": max(1, len(source_keys)),
            "effectTargets": len({_target_key(x) for x in group if _is_player_target(x) and _target_key(x) is not None}),
            "sourceNames": sorted({str(x.get("sourceName") or "?") for x in group}),
        })
    return {
        "startCount": len(starts), "completeCount": len(completes), "interruptCount": len(interrupts),
        "effectEventCount": len(effects), "failureEpisodes": episodes,
        "interrupts": [
            {"sec": round(float(x.get("sec") or 0), 3), "player": str(x.get("sourceName") or "?").split("-")[0]}
            for x in interrupts
        ],
    }


def _twin_tainted_burst(rows: list[dict], rule: dict) -> dict:
    blood_ids = _twin_spell_ids(rule, "taintedBlood")
    burst_ids = _twin_spell_ids(rule, "taintedBurst")
    blood = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in blood_ids]
    burst_rows = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in burst_ids and (_is_damage(x) or _is_aura_apply(x) or _is_completed_cast(x))]
    episodes = []
    for group in _group_times(burst_rows, max_gap=0.5):
        episodes.append({
            "sec": round(min(float(x.get("sec") or 0) for x in group), 3),
            "uniqueTargets": len({_target_key(x) for x in group if _is_player_target(x) and _target_key(x) is not None}),
            "eventCount": len(group),
        })
    return {"taintedBloodEventCount": len(blood), "burstEpisodes": episodes, "burstEventCount": len(burst_rows)}


def _twin_observed_evidence(source_kind: str) -> str:
    """Twin Fangs IDs are candidate until observed in a real pull.

    Synthetic/fixture rows exercise the behavior but do not promote journal/web
    IDs to verified evidence. A real local raw or WCL pull does.
    """
    return EVIDENCE_VERIFIED if str(source_kind or "").strip().lower() in {"raw", "wcl", "archive"} else EVIDENCE_CANDIDATE


def _twin_ravenous_stream_complete(rows: list[dict], source_kind: str) -> tuple[bool, str]:
    kind = str(source_kind or "").strip().lower()
    if kind in {"raw", "archive"}:
        return True, "local raw/archive"
    if kind != "wcl":
        return False, "synthetic/unknown stream"

    markers = [x for x in rows if _event_name(x) == "probe_stream_coverage"]
    needed = ("friendlies_debuffs", "twin_fangs_damage_done")
    grouped = {name: [x for x in markers if str(x.get("coverage") or "") == name] for name in needed}
    missing = [name for name in needed if not grouped.get(name)]
    if missing:
        return False, "coverage marker missing: " + ", ".join(missing)
    capped = [name for name in needed if any(bool(x.get("cappedWithoutNext")) for x in grouped.get(name) or [])]
    if capped:
        return False, "WCL cappedWithoutNext: " + ", ".join(capped)
    return True, "WCL Debuffs+Twin DamageDone complete"


def _twin_direct_feast_impact(row: dict) -> bool:
    """Only the first direct impact is a Ravenous Feast soak signal.

    Refresh rows and periodic/tick damage are follow-up effects and must never
    form their own soak episode.
    """
    ev = _event_name(row)
    if ev in {"refreshdebuff", "refreshbuff", "spell_aura_refresh", "spell_periodic_damage", "periodic_damage"}:
        return False
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    if bool(raw.get("tick")):
        return False
    return ev in {"damage", "spell_damage", "applydebuff", "applybuff", "spell_aura_applied"}


def _twin_ravenous_feast(rows: list[dict], rule: dict, strategy: dict, *, source_kind: str) -> dict:
    cast_ids = _twin_spell_ids(rule, "ravenousFeast")
    impact_ids = _twin_spell_ids(rule, "ravenousFeast", "impactSpellIDs")
    min_targets = int(((strategy.get("raidPlan") or {}).get("ravenousFeastMinTargets") or 4))
    judgment = strategy.get("judgment") or {}
    cluster_gap = float(judgment.get("ravenousImpactClusterGapSec") or 0.35)
    impact_window = float(judgment.get("ravenousImpactWindowSec") or 4.0)

    completed = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in cast_ids and _is_completed_cast(x)]
    starts = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in cast_ids and _is_cast_start(x)]
    anchor_rows = completed if completed else starts
    casts = [g[0] for g in _group_times(anchor_rows, max_gap=0.75)]

    impacts = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in impact_ids
        and _is_player_target(x)
        and _twin_direct_feast_impact(x)
    ]
    stream_complete, completeness_reason = _twin_ravenous_stream_complete(rows, source_kind)
    evidence = _twin_observed_evidence(source_kind)

    strikes = []
    used_ids: set[int] = set()
    for cast in casts:
        cast_sec = float(cast.get("sec") or 0)
        candidates = [
            x for x in impacts
            if id(x) not in used_ids and cast_sec <= float(x.get("sec") or 0) <= cast_sec + impact_window
        ]
        if not candidates:
            strikes.append({
                "sec": round(cast_sec, 3), "castSec": round(cast_sec, 3),
                "targetCount": None, "targets": [], "status": "unknown",
                "evidenceLevel": EVIDENCE_UNAVAILABLE,
                "streamComplete": stream_complete,
                "note": "시전 뒤 직접 첫 적중 신호 없음",
            })
            continue
        first_sec = min(float(x.get("sec") or 0) for x in candidates)
        group = [x for x in candidates if first_sec <= float(x.get("sec") or 0) <= first_sec + cluster_gap]
        for x in group:
            used_ids.add(id(x))
        targets = sorted({_twin_player_label(x) for x in group})
        count = len(targets)
        if count >= min_targets:
            status = "success"
            ev_level = evidence
        elif stream_complete:
            status = "failed"
            ev_level = evidence
        else:
            status = "unknown"
            ev_level = EVIDENCE_CANDIDATE if count else EVIDENCE_UNAVAILABLE
        strikes.append({
            "sec": round(first_sec, 3), "castSec": round(cast_sec, 3),
            "targetCount": count, "targets": targets, "status": status,
            "evidenceLevel": ev_level, "streamComplete": stream_complete,
            "note": None if status != "unknown" else f"불완전 스트림이라 {count}명 관측을 실패로 승격하지 않음",
        })

    unanchored = [x for x in impacts if id(x) not in used_ids]
    return {
        "castCount": len(casts), "castStartCount": len(starts), "castCompleteCount": len(completed),
        "impactEventCount": len(impacts), "minTargets": min_targets,
        "impactWindowSec": impact_window, "clusterGapSec": cluster_gap,
        "streamComplete": stream_complete, "streamCompletenessReason": completeness_reason,
        "strikes": strikes,
        "underSoaks": [x for x in strikes if x.get("status") == "failed"],
        "unknownSoaks": [x for x in strikes if x.get("status") == "unknown"],
        "unanchoredImpactEventCount": len(unanchored),
        "ignoredEventKinds": ["refreshdebuff", "spell_aura_refresh", "spell_periodic_damage", "periodic/tick damage"],
    }

def _twin_ravenous_mass_hits(rows: list[dict], rule: dict, *, source_kind: str) -> dict:
    """Detect the directly observed Ravenous Feast raid-wide failure shape.

    B37.4.20 calibration from real Mythic capsules:
    - both kills split 1290662 into short groups of 5-8 players;
    - 2026-10-05 Try 5 produced one 20-player 1290662 group followed by
      17 player deaths within a fraction of a second.

    We do not infer *why* the soak failed.  The verified statement is only the
    directly observed mass-hit + immediate mass-death shape.
    """
    spec = ((rule.get("mechanics") or {}).get("ravenousFeast") or {})
    ids = {safe_int(x) for x in (spec.get("directDamageSpellIDs") or []) if safe_int(x) is not None}
    min_targets = int(spec.get("raidwideFailureMinTargets") or 15)
    min_deaths = int(spec.get("raidwideFailureMinDeaths") or 4)
    death_window = float(spec.get("raidwideFailureDeathWindowSec") or 0.5)
    hits = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids
        and _is_player_target(x)
        and (_is_damage(x) or _event_name(x) in {"miss", "spell_missed"})
    ]
    deaths = [
        x for x in rows
        if _event_name(x) in {"unit_died", "death"}
        and str(x.get("targetType") or "").lower() == "player"
    ]
    id_ev = _observed_evidence_for_rule(rule, "ravenousFeast", source_kind)
    meaning_ev = str(spec.get("raidwideFailureMeaningEvidenceLevel") or EVIDENCE_CANDIDATE)
    evidence = _combined_evidence(id_ev, meaning_ev)
    episodes = []
    for group in _group_times(hits, max_gap=0.35):
        sec = min(float(x.get("sec") or 0) for x in group)
        last_sec = max(float(x.get("sec") or 0) for x in group)
        targets = sorted({_twin_player_label(x) for x in group})
        target_set = set(targets)
        linked = []
        for death in deaths:
            ds = float(death.get("sec") or 0)
            if last_sec <= ds <= last_sec + death_window and _twin_player_label(death) in target_set:
                linked.append({"player": _twin_player_label(death), "deathSec": round(ds, 3)})
        fatal = len(targets) >= min_targets and len(linked) >= min_deaths
        episodes.append({
            "sec": round(sec, 3), "lastSec": round(last_sec, 3),
            "targetCount": len(targets), "targets": targets,
            "linkedDeathCount": len(linked), "linkedDeaths": linked,
            "fatalMassHit": fatal, "evidenceLevel": evidence if fatal else id_ev,
            "spellIDs": sorted(ids),
        })
    return {
        "spellIDs": sorted(ids), "episodeCount": len(episodes), "episodes": episodes,
        "fatalEpisodes": [x for x in episodes if x.get("fatalMassHit")],
        "maxTargets": max((int(x.get("targetCount") or 0) for x in episodes), default=0),
        "minFailureTargets": min_targets, "minFailureDeaths": min_deaths,
        "deathWindowSec": death_window,
    }

def _twin_brood_waves(rows: list[dict], rule: dict, strategy: dict) -> dict:
    rouse_ids = _twin_spell_ids(rule, "rouseTheBrood")
    brood_ids = set((((rule.get("actors") or {}).get("broodlingOfIthraz") or {}).get("npcIDs") or []))
    expected = int(((strategy.get("raidPlan") or {}).get("rouseBroodExpectedCount") or 10))
    window = float(((strategy.get("judgment") or {}).get("broodSummonWindowSec") or 8.0))
    anchors = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in rouse_ids and _is_completed_cast(x)]
    if not anchors:
        anchors = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in rouse_ids and _is_cast_start(x)]
    anchors = [g[0] for g in _group_times(anchors, max_gap=1.0)]
    summons = []
    for row in rows:
        if _event_name(row) not in {"spell_summon", "summon"}:
            continue
        npc = safe_int(row.get("targetGameID"))
        if npc is None:
            npc = _guid_npc_id(row.get("destGUID"))
        if npc in brood_ids:
            summons.append(row)
    waves = []
    for idx, anchor in enumerate(anchors, start=1):
        sec = float(anchor.get("sec") or 0)
        group = [x for x in summons if sec - 0.5 <= float(x.get("sec") or 0) <= sec + window]
        keys = {x.get("destGUID") or (x.get("targetID"), x.get("targetInstance")) for x in group}
        waves.append({
            "index": idx, "sec": round(sec, 3), "summonCount": len(keys), "expectedCount": expected,
            "completeSummonSignal": bool(group),
        })
    return {"rouseCount": len(anchors), "summonCount": len(summons), "waves": waves, "expectedCount": expected}


def _twin_direct_player_episodes(rows: list[dict], rule: dict, mechanic_key: str, fields: tuple[str, ...], *, source_kind: str, max_gap: float = 0.35) -> dict:
    spec = ((rule.get("mechanics") or {}).get(mechanic_key) or {})
    ids = set()
    for field in fields:
        ids.update(safe_int(x) for x in (spec.get(field) or []) if safe_int(x) is not None)
    hits = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids
        and _is_player_target(x)
        and (_is_damage(x) or _is_aura_apply(x))
    ]
    episodes = []
    for group in _group_times(hits, max_gap=max_gap):
        targets = sorted({_twin_player_label(x) for x in group})
        spell_ids = sorted({safe_int(x.get("spellID") or x.get("abilityGameID")) for x in group if safe_int(x.get("spellID") or x.get("abilityGameID")) is not None})
        episodes.append({
            "sec": round(min(float(x.get("sec") or 0) for x in group), 3),
            "uniqueTargets": len(targets),
            "targets": targets,
            "eventCount": len(group),
            "spellIDs": spell_ids,
            "idEvidenceLevel": _observed_evidence_for_rule(rule, mechanic_key, source_kind),
            "meaningEvidenceLevel": _meaning_evidence_for_rule(rule, mechanic_key),
            "evidenceLevel": _meaning_evidence_for_rule(rule, mechanic_key),
        })
    return {"eventCount": len(hits), "episodes": episodes, "spellIDs": sorted(ids)}


def _twin_b3731_direct_failures(rows: list[dict], rule: dict, *, source_kind: str) -> dict:
    globule = _twin_direct_player_episodes(rows, rule, "causticGlobule", ("ruptureDamageSpellIDs",), source_kind=source_kind, max_gap=0.5)
    stone = _twin_direct_player_episodes(rows, rule, "stoneBreaker", ("reverbSpellIDs",), source_kind=source_kind, max_gap=0.5)
    waves = _twin_direct_player_episodes(rows, rule, "stirTheDepths", ("impactSpellIDs",), source_kind=source_kind, max_gap=0.35)
    spit = _twin_direct_player_episodes(rows, rule, "corrosiveSpit", ("impactSpellIDs",), source_kind=source_kind, max_gap=0.25)
    # Corrosive Spit deliberately targets one player. One direct victim is therefore
    # not enough to call a mistake; only collateral/multi-hit clusters are surfaced.
    spit["multiHitEpisodes"] = [x for x in spit.get("episodes") or [] if int(x.get("uniqueTargets") or 0) >= 2]
    return {"causticGlobuleRupture": globule, "stoneBreakerReverb": stone, "stirWaveHits": waves, "corrosiveSpit": spit}


def _analyze_twin_fangs(events: Iterable[dict], encounter: dict, rule: dict, *, source_kind: str) -> dict:
    rows = sorted((dict(x) for x in events if x.get("sec") is not None), key=lambda x: float(x.get("sec") or 0))
    strategy, strategy_source = load_strategy("twin_fangs")
    runtime_evidence = _twin_observed_evidence(source_kind)
    # B37.5.0.2: Eternal Venom is lethal at 10 stacks.  Real raw/WCL/archive
    # observations therefore use the same verified runtime evidence as the other
    # directly observed Twin Fangs mechanics; synthetic fixtures remain candidate.
    venom_evidence = runtime_evidence
    venom = _twin_eternal_venom(rows, rule, strategy)
    visceral = _twin_visceral(rows, rule)
    tainted = _twin_tainted_burst(rows, rule)
    feast = _twin_ravenous_feast(rows, rule, strategy, source_kind=source_kind)
    feast_mass = _twin_ravenous_mass_hits(rows, rule, source_kind=source_kind)
    brood = _twin_brood_waves(rows, rule, strategy)
    bulwark = _twin_interrupt_summary(rows, rule, "barbedBulwark")
    direct = _twin_b3731_direct_failures(rows, rule, source_kind=source_kind)

    checks = []
    timeline = []
    failures = []
    warnings = []

    if venom.get("available"):
        if venom.get("thresholdEvents"):
            first = venom["thresholdEvents"][0]
            checks.append(make_check(
                key="eternal_venom", title="영원의 맹독 중첩", status="failed", evidence_level=venom_evidence,
                summary=f"{_fmt_time(first.get('sec'))} · {first.get('player')} {first.get('stack')}중첩 · 10중첩 즉사",
                ids={"spellIDs": sorted(_twin_spell_ids(rule, "eternalVenom"))},
            ))
            failures.append({"sec": first["sec"], "kind": "eternal_venom", "label": "영원의 맹독 10중첩 즉사", "summary": f"{first['player']} {first['stack']}중첩 · 10중첩 즉사 조건 도달", "evidenceLevel": venom_evidence})
        elif venom.get("maxStack") is not None and int(venom.get("maxStack") or 0) >= int(venom.get("warningAt") or 0):
            top = venom["warningPlayers"][0]
            checks.append(make_check(
                key="eternal_venom", title="영원의 맹독 중첩", status="warning", evidence_level=venom_evidence,
                summary=f"최고 {top.get('maxStack')}중첩 · {top.get('player')} · 10중첩 즉사 직전",
                ids={"spellIDs": sorted(_twin_spell_ids(rule, "eternalVenom"))},
            ))
            warnings.append({"sec": top.get("sec"), "kind": "eternal_venom_high", "label": "영원의 맹독 9중첩 경고", "summary": f"{top.get('player')} {top.get('maxStack')}중첩 · 다음 1중첩에서 즉사", "evidenceLevel": venom_evidence})
        else:
            checks.append(make_check(
                key="eternal_venom", title="영원의 맹독 중첩", status="success", evidence_level=venom_evidence,
                summary=f"관측 최고 {venom.get('maxStack') or 0}중첩 · 10중첩 즉사 미도달",
                ids={"spellIDs": sorted(_twin_spell_ids(rule, "eternalVenom"))},
            ))
    else:
        checks.append(make_check(key="eternal_venom", title="영원의 맹독 중첩", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE, summary="중첩 수치 신호를 찾지 못했습니다."))

    if visceral.get("failureEpisodes"):
        visceral_failures = list(visceral.get("failureEpisodes") or [])
        first_ep = visceral_failures[0]
        total_failed = sum(int(x.get("failedBroodlings") or 0) for x in visceral_failures)
        checks.append(make_check(
            key="visceral_burst", title="내장 파열 차단", status="failed", evidence_level=runtime_evidence,
            summary=(
                f"{_fmt_time(first_ep.get('sec'))} · 첫 미차단/완료 {first_ep.get('failedBroodlings')}마리"
                f" · 전체 실패 {len(visceral_failures)}회/{total_failed}마리"
                f" · 첫 공대 효과 {first_ep.get('effectTargets')}명"
            ),
            ids={"castSpellIDs": sorted(_twin_spell_ids(rule, "visceralBurstCast")), "effectSpellIDs": sorted(_twin_spell_ids(rule, "visceralBurstEffect"))},
        ))
        # B37.4.25: preserve every missed/completed Visceral Burst episode.
        # This lets role projection distinguish the wipe-driving miss from later
        # verified misses, matching Caustic Globule and Stone Breaker behavior.
        for ep in visceral_failures:
            failures.append({
                "sec": ep["sec"], "kind": "visceral_burst", "label": "내장 파열 미차단",
                "summary": f"{ep.get('failedBroodlings')}마리 완료 · 공대 효과 {ep.get('effectTargets')}명",
                "evidenceLevel": runtime_evidence,
                "failedBroodlings": ep.get("failedBroodlings"),
                "targetCount": ep.get("effectTargets"),
                "spellIDs": sorted(_twin_spell_ids(rule, "visceralBurstEffect")),
            })
    elif visceral.get("startCount") or visceral.get("interruptCount"):
        status = "success" if visceral.get("startCount") and int(visceral.get("interruptCount") or 0) >= int(visceral.get("startCount") or 0) else "unknown"
        ev = runtime_evidence if status == "success" else EVIDENCE_CANDIDATE
        checks.append(make_check(
            key="visceral_burst", title="내장 파열 차단", status=status, evidence_level=ev,
            summary=f"시작 {visceral.get('startCount')} · 실제 차단 {visceral.get('interruptCount')} · 완료 효과 0",
            ids={"castSpellIDs": sorted(_twin_spell_ids(rule, "visceralBurstCast"))},
        ))
    else:
        checks.append(make_check(key="visceral_burst", title="내장 파열 차단", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE, summary="내장 파열 시전/차단 신호 미관측"))

    if tainted.get("burstEpisodes"):
        ep = tainted["burstEpisodes"][0]
        checks.append(make_check(
            key="tainted_blood", title="오염된 피 처리", status="failed", evidence_level=runtime_evidence,
            summary=f"{_fmt_time(ep.get('sec'))} · 오염된 폭발 발생 · 공대 피격 {ep.get('uniqueTargets')}명",
            ids={"burstSpellIDs": sorted(_twin_spell_ids(rule, "taintedBurst"))},
        ))
        failures.append({"sec": ep["sec"], "kind": "tainted_burst", "label": "오염된 피 처리 실패", "summary": f"오염된 폭발 · {ep.get('uniqueTargets')}명 피격"})
    elif tainted.get("taintedBloodEventCount"):
        checks.append(make_check(
            key="tainted_blood", title="오염된 피 처리", status="success", evidence_level=runtime_evidence,
            summary=f"오염된 피 신호 {tainted.get('taintedBloodEventCount')}건 · 오염된 폭발 0회",
            ids={"taintedBloodSpellIDs": sorted(_twin_spell_ids(rule, "taintedBlood")), "burstSpellIDs": sorted(_twin_spell_ids(rule, "taintedBurst"))},
        ))
    else:
        checks.append(make_check(key="tainted_blood", title="오염된 피 처리", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE, summary="오염된 피/폭발 신호 미관측"))

    globule_eps = (direct.get("causticGlobuleRupture") or {}).get("episodes") or []
    if globule_eps:
        first_ep = globule_eps[0]
        first_meaning = first_ep.get("meaningEvidenceLevel") or EVIDENCE_CANDIDATE
        first_ev = _combined_evidence(first_ep.get("idEvidenceLevel") or EVIDENCE_CANDIDATE, first_meaning)
        check = make_check(
            key="caustic_globule_rupture", title="부식성 구체 미흡수", status="failed", evidence_level=first_ev,
            summary=(
                f"{_fmt_time(first_ep.get('sec'))} · 미흡수 구체 파열 {len(globule_eps)}회 관측"
                f" · 첫 파열 {first_ep.get('uniqueTargets')}명 직접 관측"
            ),
            ids={"ruptureDamageSpellIDs": (direct.get("causticGlobuleRupture") or {}).get("spellIDs") or []},
        )
        check["idEvidenceLevel"] = first_ep.get("idEvidenceLevel") or EVIDENCE_CANDIDATE
        check["meaningEvidenceLevel"] = first_ev
        checks.append(check)
        # B37.3.9.16: every real rupture episode is a distinct verified failure.
        # Keeping only episode[0] hid later wipe-driving ruptures in long Mythic pulls.
        for ep in globule_eps:
            meaning_ev = ep.get("meaningEvidenceLevel") or EVIDENCE_CANDIDATE
            ev = _combined_evidence(ep.get("idEvidenceLevel") or EVIDENCE_CANDIDATE, meaning_ev)
            if ev == EVIDENCE_VERIFIED:
                failures.append({
                    "sec": ep.get("sec"), "kind": "caustic_globule_rupture",
                    "label": "부식성 구체 미흡수", "summary": "Caustic Globule 파열",
                    "evidenceLevel": ev, "targetCount": ep.get("uniqueTargets"),
                    "spellIDs": ep.get("spellIDs") or [],
                })
    else:
        checks.append(make_check(key="caustic_globule_rupture", title="부식성 구체 미흡수", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE, summary="구체 파열 직접 신호 미관측"))

    stone_eps = (direct.get("stoneBreakerReverb") or {}).get("episodes") or []
    if stone_eps:
        first_ep = stone_eps[0]
        first_meaning = first_ep.get("meaningEvidenceLevel") or EVIDENCE_CANDIDATE
        first_ev = _combined_evidence(first_ep.get("idEvidenceLevel") or EVIDENCE_CANDIDATE, first_meaning)
        check = make_check(
            key="stone_breaker_reverb", title="돌 파괴자 소크", status="failed", evidence_level=first_ev,
            summary=(
                f"{_fmt_time(first_ep.get('sec'))} · 플랫폼 반향 {len(stone_eps)}회 관측"
                f" · 첫 반향 {first_ep.get('uniqueTargets')}명 직접 관측"
            ),
            ids={"reverbSpellIDs": (direct.get("stoneBreakerReverb") or {}).get("spellIDs") or []},
        )
        check["idEvidenceLevel"] = first_ep.get("idEvidenceLevel") or EVIDENCE_CANDIDATE
        check["meaningEvidenceLevel"] = first_ev
        checks.append(check)
        # B37.3.9.16: retain all reverberations so a later fatal miss can own
        # major-incident/collapse-direct even when an earlier miss already occurred.
        for ep in stone_eps:
            meaning_ev = ep.get("meaningEvidenceLevel") or EVIDENCE_CANDIDATE
            ev = _combined_evidence(ep.get("idEvidenceLevel") or EVIDENCE_CANDIDATE, meaning_ev)
            if ev == EVIDENCE_VERIFIED:
                failures.append({
                    "sec": ep.get("sec"), "kind": "stone_breaker_reverb",
                    "label": "돌 파괴자 미소크", "summary": "플랫폼 반향 발생",
                    "evidenceLevel": ev, "targetCount": ep.get("uniqueTargets"),
                    "spellIDs": ep.get("spellIDs") or [],
                })
    else:
        checks.append(make_check(key="stone_breaker_reverb", title="돌 파괴자 소크", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE, summary="플랫폼 반향 직접 신호 미관측"))

    wave_eps = (direct.get("stirWaveHits") or {}).get("episodes") or []
    if wave_eps:
        first = wave_eps[0]
        meaning_ev = first.get("meaningEvidenceLevel") or EVIDENCE_CANDIDATE
        ev = _combined_evidence(first.get("idEvidenceLevel") or EVIDENCE_CANDIDATE, meaning_ev)
        total_targets = sum(int(x.get("uniqueTargets") or 0) for x in wave_eps)
        check = make_check(
            key="stir_wave_hits", title="심연 휘젓기 파도", status="warning", evidence_level=ev,
            summary=f"파도 직접 피격 {len(wave_eps)}회 묶음 · 누적 고유피격 합 {total_targets} · Kill에서도 발생 가능하므로 붕괴 원인 아님",
            ids={"impactSpellIDs": (direct.get("stirWaveHits") or {}).get("spellIDs") or []},
        )
        check["idEvidenceLevel"] = first.get("idEvidenceLevel") or EVIDENCE_CANDIDATE
        check["meaningEvidenceLevel"] = meaning_ev
        checks.append(check)
        for ep in wave_eps:
            timeline.append({"kind":"stir_wave_hit","sec":ep.get("sec"),"title":"파도 피격","summary":f"{ep.get('uniqueTargets')}명","status":"warning","idEvidenceLevel":ep.get("idEvidenceLevel"),"meaningEvidenceLevel":meaning_ev,"evidenceLevel":ev})
        if ev == EVIDENCE_VERIFIED:
            warnings.append({"sec":first.get("sec"),"kind":"stir_wave_hit","label":"심연 휘젓기 파도 피격","summary":f"{len(wave_eps)}회 묶음 · {total_targets}명-피격 합","evidenceLevel":ev})
    else:
        checks.append(make_check(key="stir_wave_hits", title="심연 휘젓기 파도", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE, summary="파도 직접 피격 신호 미관측"))

    spit_multi = (direct.get("corrosiveSpit") or {}).get("multiHitEpisodes") or []
    spit_all = (direct.get("corrosiveSpit") or {}).get("episodes") or []
    if spit_multi:
        first = spit_multi[0]
        meaning_ev = first.get("meaningEvidenceLevel") or EVIDENCE_CANDIDATE
        ev = _combined_evidence(first.get("idEvidenceLevel") or EVIDENCE_CANDIDATE, meaning_ev)
        check = make_check(
            key="corrosive_spit_cleave", title="부식성 침 추가 피격", status="warning", evidence_level=ev,
            summary=f"{_fmt_time(first.get('sec'))} · 동일 침 타격 {first.get('uniqueTargets')}명 · 표적 외 추가 피격",
            ids={"impactSpellIDs": (direct.get("corrosiveSpit") or {}).get("spellIDs") or []},
        )
        check["idEvidenceLevel"] = first.get("idEvidenceLevel") or EVIDENCE_CANDIDATE
        check["meaningEvidenceLevel"] = meaning_ev
        checks.append(check)
        timeline.append({"kind":"corrosive_spit_cleave","sec":first.get("sec"),"title":"부식성 침 추가 피격","summary":f"{first.get('uniqueTargets')}명 피격","status":"warning","idEvidenceLevel":first.get("idEvidenceLevel"),"meaningEvidenceLevel":meaning_ev,"evidenceLevel":ev})
        if ev == EVIDENCE_VERIFIED:
            warnings.append({"sec":first.get("sec"),"kind":"corrosive_spit_cleave","label":"부식성 침 추가 피격","summary":f"{first.get('uniqueTargets')}명 피격","evidenceLevel":ev})
    elif spit_all:
        id_ev = spit_all[0].get("idEvidenceLevel") or EVIDENCE_CANDIDATE
        success_meaning_ev = _meaning_evidence_for_rule(rule, "corrosiveSpit", success=True)
        success_ev = _combined_evidence(id_ev, success_meaning_ev)
        checks.append(make_check(
            key="corrosive_spit_cleave", title="부식성 침 추가 피격", status="success", evidence_level=success_ev,
            summary=f"직접 타격 {len(spit_all)}회 묶음 · 관측 묶음은 모두 1명 · 표적 외 추가 피격 0회",
            ids={"impactSpellIDs": (direct.get("corrosiveSpit") or {}).get("spellIDs") or []},
        ))
    else:
        checks.append(make_check(key="corrosive_spit_cleave", title="부식성 침 추가 피격", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE, summary="부식성 침 직접 피격 신호 미관측"))

    mass_failures = feast_mass.get("fatalEpisodes") or []
    if mass_failures:
        ep = mass_failures[0]
        ev = ep.get("evidenceLevel") or EVIDENCE_CANDIDATE
        checks.append(make_check(
            key="ravenous_feast", title="걸신들린 탐식 인원", status="failed", evidence_level=ev,
            summary=(f"{_fmt_time(ep.get('sec'))} · 동시 피격 {ep.get('targetCount')}명"
                     f" · {ep.get('linkedDeathCount')}명 {feast_mass.get('deathWindowSec')}초 내 사망"),
            ids={"directDamageSpellIDs": feast_mass.get("spellIDs") or []},
        ))
        if ev == EVIDENCE_VERIFIED:
            failures.append({
                "sec": ep.get("sec"), "kind": "ravenous_feast_mass_hit",
                "label": "걸신들린 탐식 대규모 피격",
                "summary": f"동시 피격 {ep.get('targetCount')}명 · 즉시 사망 {ep.get('linkedDeathCount')}명",
                "evidenceLevel": ev, "targetCount": ep.get("targetCount"),
                "linkedDeathCount": ep.get("linkedDeathCount"),
                "linkedDeaths": ep.get("linkedDeaths") or [],
                "spellIDs": ep.get("spellIDs") or [],
            })
    elif feast_mass.get("episodeCount"):
        checks.append(make_check(
            key="ravenous_feast", title="걸신들린 탐식 인원", status="success", evidence_level=runtime_evidence,
            summary=(f"직접 피해 묶음 {feast_mass.get('episodeCount')}회 · 최대 동시 {feast_mass.get('maxTargets')}명"
                     " · 대규모 즉시 사망 0회"),
            ids={"directDamageSpellIDs": feast_mass.get("spellIDs") or []},
        ))
    elif feast.get("strikes"):
        under = feast.get("underSoaks") or []
        unknown = feast.get("unknownSoaks") or []
        if under:
            ep = under[0]
            checks.append(make_check(
                key="ravenous_feast", title="걸신들린 탐식 인원", status="failed", evidence_level=runtime_evidence,
                summary=f"{_fmt_time(ep.get('sec'))} · 시전 직후 첫 적중 {ep.get('targetCount')}명 < 최소 {feast.get('minTargets')}명",
                ids={"impactSpellIDs": sorted(_twin_spell_ids(rule, "ravenousFeast", "impactSpellIDs"))},
            ))
            failures.append({"sec": ep["sec"], "kind": "ravenous_feast", "label": "걸신들린 탐식 인원 부족", "summary": f"{ep.get('targetCount')}/{feast.get('minTargets')}명"})
        elif unknown:
            counts = [x.get("targetCount") for x in unknown if x.get("targetCount") is not None]
            observed = "/".join(str(x) for x in counts) if counts else "직접 적중 없음"
            checks.append(make_check(
                key="ravenous_feast", title="걸신들린 탐식 인원", status="unknown", evidence_level=EVIDENCE_CANDIDATE,
                summary=f"시전 {feast.get('castCount')}회 · 첫 적중 {observed} · {feast.get('streamCompletenessReason')} → 실패 판정 보류",
                ids={"impactSpellIDs": sorted(_twin_spell_ids(rule, "ravenousFeast", "impactSpellIDs"))},
            ))
        else:
            checks.append(make_check(
                key="ravenous_feast", title="걸신들린 탐식 인원", status="success", evidence_level=runtime_evidence,
                summary=f"시전 기준 첫 적중 {len(feast.get('strikes') or [])}타 모두 최소 {feast.get('minTargets')}명 이상",
                ids={"impactSpellIDs": sorted(_twin_spell_ids(rule, "ravenousFeast", "impactSpellIDs"))},
            ))
    elif feast.get("castCount"):
        checks.append(make_check(
            key="ravenous_feast", title="걸신들린 탐식 인원", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE,
            summary=f"탐식 시전 {feast.get('castCount')}회 · 시전 직후 직접 첫 적중 신호 없음 · {feast.get('streamCompletenessReason')}",
        ))
    else:
        checks.append(make_check(
            key="ravenous_feast", title="걸신들린 탐식 인원", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE,
            summary=f"탐식 시전/직접 적중 신호 미확정 · 현재 신화 최소 {feast.get('minTargets')}명",
        ))

    if bulwark.get("interrupts"):
        checks.append(make_check(
            key="barbed_bulwark", title="가시 보루 차단", status="success", evidence_level=runtime_evidence,
            summary=f"가시 보루 실제 차단 {len(bulwark.get('interrupts') or [])}회 · 미차단 수는 스트림만으로 단정하지 않음",
            ids={"spellIDs": sorted(_twin_spell_ids(rule, "barbedBulwark"))},
        ))
    else:
        checks.append(make_check(
            key="barbed_bulwark", title="가시 보루 차단", status="unknown", evidence_level=EVIDENCE_CANDIDATE if (bulwark.get('starts') or bulwark.get('completes')) else EVIDENCE_UNAVAILABLE,
            summary=f"시작 {len(bulwark.get('starts') or [])} · 차단 0 · 실패 의미는 직접 판정하지 않음",
        ))

    if brood.get("waves"):
        observed = [int(x.get("summonCount") or 0) for x in brood.get("waves") or []]
        known = [x for x in observed if x > 0]
        summary = f"새끼 깨우기 {brood.get('rouseCount')}회 · 소환 관측 " + ("/".join(map(str, known)) if known else "?") + f" · 현재 기준 {brood.get('expectedCount')}마리"
        checks.append(make_check(
            key="rouse_brood", title="새끼 깨우기", status="success" if known and all(x == int(brood.get('expectedCount') or 0) for x in known) else "unknown",
            evidence_level=runtime_evidence if known else EVIDENCE_CANDIDATE, summary=summary,
            ids={"spellIDs": sorted(_twin_spell_ids(rule, "rouseTheBrood")), "broodlingNpcIDs": (((rule.get('actors') or {}).get('broodlingOfIthraz') or {}).get('npcIDs') or [])},
        ))
    else:
        checks.append(make_check(key="rouse_brood", title="새끼 깨우기", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE, summary="새끼 깨우기 신호 미관측"))

    # Keep major cast anchors visible without treating ordinary boss casts as failures.
    anchor_specs = [
        ("causticDeluge", "부식성 폭우"), ("venomousEmergence", "맹독성 출현"),
        ("stirTheDepths", "심연 휘젓기"), ("vileFlood", "끔찍한 홍수"),
        ("rouseTheBrood", "새끼 깨우기"), ("ravenousFeast", "걸신들린 탐식"),
        ("sanguineStorm", "핏빛 폭풍"), ("submerge", "잠수"),
    ]
    for key, label in anchor_specs:
        ids = _twin_spell_ids(rule, key)
        anchors = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids and (_is_cast_start(x) or _is_completed_cast(x))]
        for group in _group_times(anchors, max_gap=1.0):
            timeline.append({"kind": key, "sec": round(float(group[0].get("sec") or 0), 3), "title": label, "summary": "기믹 기준점", "evidenceLevel": runtime_evidence, "status": "success"})
    for failure in failures:
        timeline.append({"kind": failure["kind"], "sec": failure["sec"], "title": failure["label"], "summary": failure.get("summary") or "", "evidenceLevel": failure.get("evidenceLevel") or runtime_evidence, "status": "failed"})
    for warning in warnings:
        timeline.append({"kind": warning["kind"], "sec": warning.get("sec"), "title": warning["label"], "summary": warning.get("summary") or "", "evidenceLevel": warning.get("evidenceLevel") or runtime_evidence, "status": "warning"})
    timeline.sort(key=lambda x: (float(x.get("sec") or 0), 0 if x.get("status") == "failed" else 1, str(x.get("title") or "")))

    failures.sort(key=lambda x: float(x.get("sec") or 0))
    warnings.sort(key=lambda x: float(x.get("sec") or 0))
    for item in failures + warnings:
        item.setdefault("evidenceLevel", runtime_evidence)
    decisive_failures = [x for x in failures if (x.get("evidenceLevel") or runtime_evidence) == EVIDENCE_VERIFIED]
    decisive_warnings = [x for x in warnings if (x.get("evidenceLevel") or runtime_evidence) == EVIDENCE_VERIFIED]
    if decisive_failures:
        first = decisive_failures[0]
        verdict = {
            "severity": "HIGH", "status": "failed", "evidenceLevel": runtime_evidence,
            "primary": first["label"],
            "why": f"{_fmt_time(first.get('sec'))} · {first.get('summary') or first.get('label')} · 직접 로그 신호로 확인됐습니다.",
            "nextAction": {
                "eternal_venom": "다음 풀: 영원의 맹독이 10중첩에 도달하지 않도록 탐식으로 중첩을 줄이고 추가 피격을 피하세요.",
                "visceral_burst": "다음 풀: 새끼의 내장 파열 차단 누락을 0회로 만드세요.",
                "tainted_burst": "다음 풀: 탐식 뒤 오염된 피 웅덩이를 만료 전에 전부 흡수하세요.",
                "caustic_globule_rupture": "다음 풀: 부식성 구체가 파열되기 전에 전부 흡수하세요.",
                "stone_breaker_reverb": "다음 풀: 돌 파괴자 타격을 반드시 받아 플랫폼 반향을 0회로 만드세요.",
                "ravenous_feast": f"다음 풀: 탐식 한 타마다 최소 {feast.get('minTargets')}명 적중을 확인하세요.",
                "ravenous_feast_mass_hit": "다음 풀: 걸신들린 탐식의 공대 대규모 동시 피격과 즉시 다수 사망을 0회로 만드세요.",
            }.get(first.get("kind"), "다음 풀: 첫 검증 실패를 우선 제거하세요."),
        }
    elif decisive_warnings:
        first = decisive_warnings[0]
        verdict = {
            "severity": "WARN", "status": "warning", "evidenceLevel": runtime_evidence,
            "primary": first["label"], "why": f"{_fmt_time(first.get('sec'))} · {first.get('summary')}",
            "nextAction": {
                "stir_wave_hit": "다음 풀: 심연 휘젓기 파도 직접 피격을 줄이세요. 킬 가능 여부와 별개로 피할 수 있는 추가 피해입니다.",
                "corrosive_spit_cleave": "다음 풀: 부식성 침 대상자 외 인원이 직선상에 겹치지 않게 하세요.",
                "eternal_venom_high": "다음 풀: 영원의 맹독 9중첩은 즉사 직전입니다. 탐식으로 중첩을 줄이고 추가 피격을 피하세요.",
            }.get(first.get("kind"), "다음 풀: 검증된 경고 신호를 줄이세요."),
        }
    else:
        observed_any = any(x.get("evidenceLevel") == EVIDENCE_VERIFIED and x.get("status") in {"success", "failed"} for x in checks)
        verdict = {
            "severity": "INFO", "status": "success" if observed_any else "unknown",
            "evidenceLevel": EVIDENCE_VERIFIED if observed_any else EVIDENCE_UNAVAILABLE,
            "primary": "검증된 치명 실패 미관측" if observed_any else "쌍둥이 송곳니 판정 신호 부족",
            "why": "현재 스트림에서 승격 가능한 치명 실패 신호를 찾지 못했습니다." if observed_any else "기믹 이벤트 스트림이 충분하지 않습니다.",
            "nextAction": "다음 풀에서도 영원의 맹독·내장 파열·오염된 피·탐식 적중을 우선 추적합니다.",
        }

    return {
        "schemaVersion": SCHEMA_VERSION,
        "engineVersion": ENGINE_VERSION,
        "rulesVersion": RULES_VERSION,
        "bossKey": "twin_fangs",
        "sourceKind": source_kind,
        "encounter": {
            "name": encounter.get("name") or encounter.get("encounterName") or rule.get("displayName"),
            "encounterID": encounter.get("encounterID"),
            "difficultyID": encounter.get("difficultyID") or encounter.get("difficulty"),
            "tryNo": encounter.get("tryNo"),
            "result": encounter.get("result") or ("Kill" if encounter.get("kill") else "Wipe"),
            "durationSec": encounter.get("durationSec"),
            "startTimestamp": encounter.get("startTimestamp"),
        },
        "rule": {"bossKey": rule.get("bossKey"), "schemaVersion": rule.get("schemaVersion"), "idBased": True},
        "strategy": {"source": strategy_source, "values": strategy, "applied": _mythic(encounter)},
        "checks": checks,
        "timeline": timeline,
        "observations": {
            "eternalVenom": venom, "visceralBurst": visceral, "taintedBlood": tainted,
            "ravenousFeast": feast, "ravenousFeastMassHit": feast_mass, "barbedBulwark": {
                "startCount": len(bulwark.get("starts") or []), "completeCount": len(bulwark.get("completes") or []),
                "interruptCount": len(bulwark.get("interrupts") or []),
                "interrupts": [{"sec": round(float(x.get("sec") or 0), 3), "player": str(x.get("sourceName") or "?").split("-")[0]} for x in (bulwark.get("interrupts") or [])],
            },
            "brood": brood,
            "directFailuresB3731": direct,
            "verifiedFailures": [dict(x) for x in decisive_failures],
            "firstVerifiedFailure": decisive_failures[0] if decisive_failures else None,
        },
        "verdict": verdict,
        "collapseLink": {"status": "unknown", "evidenceLevel": EVIDENCE_UNAVAILABLE, "summary": "B31 붕괴 체인과 아직 연결되지 않음"},
        "limitations": [
            "공략/저널에서 고정한 spellID와 현재 Blizzard hotfix 기준을 사용하지만, 실제 트라이에 없는 이벤트를 성공/실패로 꾸며 판정하지 않습니다.",
            "가시 보루는 실제 차단을 확인할 수 있으나, 시작 수 대비 부족만으로 미차단 실패를 단정하지 않습니다.",
            "새끼 10마리는 현재 신화 기준이지만 summon 스트림이 부분 수집이면 부족으로 실패 처리하지 않습니다.",
            "걸신들린 탐식은 각 시전 직후 첫 직접 적중 묶음만 셉니다. refresh/periodic은 제외하고, WCL 스트림이 capped/불완전하면 4명 미만 관측도 실패로 승격하지 않습니다.",
            "B37.3.1 신규 직접 실패(구체 파열/돌 파괴자 반향/파도 피격/부식성 침 다중 피격)는 ID 관측과 실패 의미를 분리합니다. 실제 raw/WCL은 ID만 verified가 될 수 있고, 의미가 candidate이면 UI는 ?로 투영합니다.",
            "부식성 침은 한 명을 향하는 기술이므로 단일 피격 자체를 실패로 판정하지 않습니다. 같은 짧은 타격 묶음에서 2명 이상 맞은 경우만 추가 피격 후보로 표시합니다.",
        ],
    }


def _sentinel_blast_episodes(rows: list[dict], rule: dict) -> list[dict]:
    spec = ((rule.get("mechanics") or {}).get("toxicBlast") or {})
    ids = {safe_int(x) for x in (spec.get("spellIDs") or []) if safe_int(x) is not None}
    hits = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids and _is_damage(x)]
    episodes = []
    # A missed globule creates one raid-wide blast. Multiple missed globules land
    # within milliseconds, so counting temporal groups alone would undercount.
    # Count repeated hits per same target inside one short episode; the maximum
    # hit count among players equals the number of raid-wide blast pulses.
    for idx, group in enumerate(_group_times(hits, max_gap=1.0), start=1):
        per_target = Counter(_target_key(x) for x in group if _target_key(x) is not None)
        pulse_count = max(per_target.values()) if per_target else 0
        first_sec = min(float(x.get("sec") or 0) for x in group)
        last_sec = max(float(x.get("sec") or 0) for x in group)
        episodes.append({
            "index": idx,
            "sec": round(first_sec, 3),
            "lastSec": round(last_sec, 3),
            "missedOrbCount": int(pulse_count),
            "blastPulseCount": int(pulse_count),
            "uniqueTargets": len(per_target),
            "eventCount": len(group),
            "spellIDs": sorted(ids),
            "evidenceLevel": EVIDENCE_VERIFIED,
        })
    return episodes



def _rule_spell_ids(rule: dict, mechanic_key: str, field: str = "spellIDs") -> set[int]:
    spec = ((rule.get("mechanics") or {}).get(mechanic_key) or {})
    return {safe_int(x) for x in (spec.get(field) or []) if safe_int(x) is not None}


def _observed_evidence_for_rule(rule: dict, mechanic_key: str, source_kind: str) -> str:
    """Return ID/event-shape evidence only.

    B37.2.1 keeps the B37.1.1 contract explicit: observing a spell/event in a real
    raw/WCL pull can verify that the ID and event shape are real, but it does not
    verify that the occurrence means success or failure. Judgment meaning is tracked
    separately by *_meaning_evidence_for_rule().
    """
    spec = ((rule.get("mechanics") or {}).get(mechanic_key) or {})
    configured = str(spec.get("idEvidenceLevel") or spec.get("evidenceLevel") or "")
    if configured == EVIDENCE_VERIFIED:
        return EVIDENCE_VERIFIED
    if str(source_kind or "").strip().lower() in {"raw", "wcl", "archive"}:
        return EVIDENCE_VERIFIED
    return EVIDENCE_CANDIDATE


def _meaning_evidence_for_rule(rule: dict, mechanic_key: str, *, success: bool = False) -> str:
    """Return evidence for the *meaning* of an observed signal.

    A real raw/WCL row never promotes this by itself. Promotion requires the rule to
    be updated after kill/wipe comparison or equivalent local validation.
    """
    spec = ((rule.get("mechanics") or {}).get(mechanic_key) or {})
    field = "successMeaningEvidenceLevel" if success else "failureMeaningEvidenceLevel"
    configured = str(spec.get(field) or spec.get("meaningEvidenceLevel") or EVIDENCE_CANDIDATE)
    if configured == EVIDENCE_VERIFIED:
        return EVIDENCE_VERIFIED
    if configured == EVIDENCE_UNAVAILABLE:
        return EVIDENCE_UNAVAILABLE
    return EVIDENCE_CANDIDATE


def _combined_evidence(id_evidence: str, meaning_evidence: str) -> str:
    """A verified judgment needs both a real observed ID/event shape and verified meaning."""
    if meaning_evidence == EVIDENCE_UNAVAILABLE:
        return EVIDENCE_UNAVAILABLE
    if id_evidence == EVIDENCE_VERIFIED and meaning_evidence == EVIDENCE_VERIFIED:
        return EVIDENCE_VERIFIED
    return EVIDENCE_CANDIDATE


def _nekzali_stream_complete(rows: list[dict], source_kind: str) -> tuple[bool, str]:
    kind=str(source_kind or "").strip().lower()
    if kind in {"raw", "archive"}:
        return True, "local raw/archive"
    if kind != "wcl":
        return False, "synthetic/unknown stream"
    markers=[x for x in rows if _event_name(x)=="probe_stream_coverage"]
    needed=("enemy_casts","friendly_interrupts")
    missing=[name for name in needed if not any(str(x.get("coverage") or "")==name for x in markers)]
    if missing:
        return False, "coverage marker missing: " + ", ".join(missing)
    capped=[name for name in needed if any(str(x.get("coverage") or "")==name and bool(x.get("cappedWithoutNext")) for x in markers)]
    if capped:
        return False, "WCL cappedWithoutNext: " + ", ".join(capped)
    return True, "WCL enemy Casts + friendly Interrupts complete"


def _nekzali_cast_resolution(rows: list[dict], rule: dict, mechanic_key: str, *, source_kind: str, cast_window_sec: float = 10.5) -> dict:
    ids = _rule_spell_ids(rule, mechanic_key)
    starts = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids and _is_cast_start(x)]
    completes = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids and _is_completed_cast(x)]
    interrupts = [x for x in rows if _is_interrupt(x) and safe_int(x.get("extraSpellID") or x.get("extraAbilityGameID")) in ids]
    starts = [g[0] for g in _group_times(starts, max_gap=0.5)]
    outcomes=[]
    for st in starts:
        sec=float(st.get("sec") or 0)
        done=[x for x in completes if sec <= float(x.get("sec") or 0) <= sec+cast_window_sec]
        kicked=[x for x in interrupts if sec <= float(x.get("sec") or 0) <= sec+cast_window_sec]
        if done:
            first=min(done,key=lambda x:float(x.get("sec") or 0))
            id_ev=_observed_evidence_for_rule(rule, mechanic_key, source_kind)
            meaning_ev=_meaning_evidence_for_rule(rule, mechanic_key, success=False)
            outcomes.append({"sec":round(sec,3),"endSec":round(float(first.get("sec") or 0),3),"status":"failed","result":"completed","evidenceLevel":_combined_evidence(id_ev, meaning_ev),"idEvidenceLevel":id_ev,"meaningEvidenceLevel":meaning_ev})
        elif kicked:
            first=min(kicked,key=lambda x:float(x.get("sec") or 0))
            id_ev=_observed_evidence_for_rule(rule, mechanic_key, source_kind)
            meaning_ev=_meaning_evidence_for_rule(rule, mechanic_key, success=True)
            outcomes.append({"sec":round(sec,3),"endSec":round(float(first.get("sec") or 0),3),"status":"success","result":"interrupted","player":str(first.get("sourceName") or "?").split("-")[0],"evidenceLevel":_combined_evidence(id_ev, meaning_ev),"idEvidenceLevel":id_ev,"meaningEvidenceLevel":meaning_ev})
        else:
            outcomes.append({"sec":round(sec,3),"status":"unknown","result":"unresolved","evidenceLevel":EVIDENCE_UNAVAILABLE})
    # Some WCL exports can omit cast-start while retaining completion/interrupt.
    # Surface those as observed facts, but never infer an absent counterpart.
    if not starts:
        for x in completes:
            id_ev=_observed_evidence_for_rule(rule, mechanic_key, source_kind)
            meaning_ev=_meaning_evidence_for_rule(rule, mechanic_key, success=False)
            outcomes.append({"sec":round(float(x.get("sec") or 0),3),"endSec":round(float(x.get("sec") or 0),3),"status":"failed","result":"completed_without_start","evidenceLevel":_combined_evidence(id_ev, meaning_ev),"idEvidenceLevel":id_ev,"meaningEvidenceLevel":meaning_ev})
        for x in interrupts:
            id_ev=_observed_evidence_for_rule(rule, mechanic_key, source_kind)
            meaning_ev=_meaning_evidence_for_rule(rule, mechanic_key, success=True)
            outcomes.append({"sec":round(float(x.get("sec") or 0),3),"endSec":round(float(x.get("sec") or 0),3),"status":"success","result":"interrupt_without_start","player":str(x.get("sourceName") or "?").split("-")[0],"evidenceLevel":_combined_evidence(id_ev, meaning_ev),"idEvidenceLevel":id_ev,"meaningEvidenceLevel":meaning_ev})
    outcomes.sort(key=lambda x:float(x.get("sec") or 0))
    return {"startCount":len(starts),"completeCount":len(completes),"interruptCount":len(interrupts),"outcomes":outcomes}


def _analyze_nekzali(events: Iterable[dict], encounter: dict, rule: dict, *, source_kind: str) -> dict:
    rows=sorted((dict(x) for x in events if x.get("sec") is not None), key=lambda x:float(x.get("sec") or 0))
    strategy,strategy_source=load_strategy("nekzali")
    grasp_ids=_rule_spell_ids(rule,"graspingDepths")
    echo_npcs={safe_int(x) for x in ((((rule.get("actors") or {}).get("drownedEcho") or {}).get("npcIDs") or [])) if safe_int(x) is not None}
    grasp=[x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in grasp_ids and (_is_completed_cast(x) or _is_aura_apply(x))]
    echo_summons=[]
    for x in rows:
        if _event_name(x) not in {"spell_summon","summon"}:
            continue
        npc=safe_int(x.get("targetGameID")) or _guid_npc_id(x.get("destGUID"))
        if npc in echo_npcs:
            echo_summons.append(x)
    curse=_nekzali_cast_resolution(rows,rule,"soulcoilersCurse",source_kind=source_kind,cast_window_sec=10.5)
    curse_stream_complete, curse_stream_reason = _nekzali_stream_complete(rows, source_kind)
    curse["streamComplete"] = curse_stream_complete
    curse["streamCompletenessReason"] = curse_stream_reason
    exhaustion_ids=_rule_spell_ids(rule,"soulExhaustion")
    exhaustion=[x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in exhaustion_ids and _is_aura_apply(x) and _is_player_target(x)]
    exhaustion_by=Counter(str(x.get("destName") or x.get("targetName") or x.get("destGUID") or "?").split("-")[0] for x in exhaustion)
    rite_ids=_rule_spell_ids(rule,"soulcoilRite")
    rites=[x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in rite_ids and (_is_completed_cast(x) or _is_damage(x) or _is_aura_apply(x))]
    rite_eps=_group_times(rites,max_gap=0.75)
    invoke_ids=_rule_spell_ids(rule,"invoke")
    invokes=[x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in invoke_ids and (_is_cast_start(x) or _is_completed_cast(x))]
    corpse_ids=_rule_spell_ids(rule,"corpseBlight")
    corpse=[x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in corpse_ids and (_is_damage(x) or _is_aura_apply(x))]
    corpse_eps=_group_times(corpse,max_gap=0.75)
    ritual_burn_ids=_rule_spell_ids(rule,"ritualBurn")
    ritual_burn=[x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ritual_burn_ids]
    haunted_cast_ids=_rule_spell_ids(rule,"hauntedSalvoCast")
    haunted_cast_starts=[x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in haunted_cast_ids and _is_cast_start(x)]
    haunted_cast_completes=[x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in haunted_cast_ids and _is_completed_cast(x)]
    haunted_ids=_rule_spell_ids(rule,"hauntedSalvo")
    haunted=[x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in haunted_ids]
    void_strike_ids=_rule_spell_ids(rule,"voidStrike")
    void_strike=[x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in void_strike_ids]
    essence_ids=_rule_spell_ids(rule,"essenceShatter")
    essence=[x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in essence_ids]

    checks=[]; timeline=[]; failures=[]
    for o in curse["outcomes"]:
        if o.get("status")=="failed":
            ev=o.get("evidenceLevel") or EVIDENCE_CANDIDATE
            item={"kind":"soulcoilers_curse","sec":o.get("endSec") or o.get("sec"),"label":"영혼살무사의 저주 미차단","summary":"Drowned Echo 차단 대상 시전 완료","evidenceLevel":ev,"status":"failed"}
            timeline.append(item)
            if ev==EVIDENCE_VERIFIED: failures.append(item)
        elif o.get("status")=="success":
            timeline.append({"kind":"soulcoilers_curse","sec":o.get("endSec") or o.get("sec"),"label":"영혼살무사의 저주 차단","summary":f"{o.get('player') or '?'} 차단","evidenceLevel":o.get("evidenceLevel"),"status":"success"})
    failed=[x for x in curse["outcomes"] if x.get("status")=="failed"]
    succeeded=[x for x in curse["outcomes"] if x.get("status")=="success"]
    unresolved=[x for x in curse["outcomes"] if x.get("status")=="unknown"]
    if failed:
        ev=EVIDENCE_VERIFIED if any(x.get("evidenceLevel")==EVIDENCE_VERIFIED for x in failed) else EVIDENCE_CANDIDATE
        checks.append(make_check(key="soulcoilers_curse",title="영혼살무사의 저주 차단",status="failed",evidence_level=ev,summary=f"시전 완료 {len(failed)}회 · 차단 {len(succeeded)}회" + (f" · 미해결 {len(unresolved)}회" if unresolved else ""),ids={"spellIDs":sorted(_rule_spell_ids(rule,'soulcoilersCurse'))}))
    elif succeeded and not unresolved and curse_stream_complete:
        ev=EVIDENCE_VERIFIED if all(x.get("evidenceLevel")==EVIDENCE_VERIFIED for x in succeeded) else EVIDENCE_CANDIDATE
        checks.append(make_check(key="soulcoilers_curse",title="영혼살무사의 저주 차단",status="success",evidence_level=ev,summary=f"관측된 시전 {len(succeeded)}회 모두 차단 · {curse_stream_reason}",ids={"spellIDs":sorted(_rule_spell_ids(rule,'soulcoilersCurse'))}))
    else:
        reason = curse_stream_reason if succeeded and not curse_stream_complete else "차단 대상 시전의 시작/종료/차단 신호가 충분하지 않음"
        checks.append(make_check(key="soulcoilers_curse",title="영혼살무사의 저주 차단",status="unknown",evidence_level=EVIDENCE_CANDIDATE if succeeded else EVIDENCE_UNAVAILABLE,summary=f"판정 보류 · {reason}",ids={"spellIDs":sorted(_rule_spell_ids(rule,'soulcoilersCurse'))}))

    checks.append(make_check(key="soul_exhaustion",title="우물 진입 팀 · 영혼 탈진",status="unknown",evidence_level=_observed_evidence_for_rule(rule,'soulExhaustion',source_kind) if exhaustion else EVIDENCE_UNAVAILABLE,summary=(f"적용 {len(exhaustion)}회 · 대상 {len(exhaustion_by)}명 · 반복 최대 {max(exhaustion_by.values()) if exhaustion_by else 0}회" if exhaustion else "적용 신호 없음 · 배정 정보 없이는 성공/실패 판정 안 함"),ids={"spellIDs":sorted(exhaustion_ids)}))
    checks.append(make_check(key="soulcoil_rite",title="영혼똬리 의식",status="unknown",evidence_level=_observed_evidence_for_rule(rule,'soulcoilRite',source_kind) if rite_eps else EVIDENCE_UNAVAILABLE,summary=(f"의식 신호 {len(rite_eps)}회 · 정상 킬에서도 반복되는 공대 압박이라 단독 실패 판정 금지" if rite_eps else "의식 신호 없음"),ids={"spellIDs":sorted(rite_ids)}))

    failures.sort(key=lambda x:float(x.get("sec") or 0))
    first=failures[0] if failures else None
    if first:
        verdict={"severity":"HIGH","status":"failed","evidenceLevel":EVIDENCE_VERIFIED,"primary":first.get("label"),"why":f"{_fmt_time(first.get('sec'))}에 실제 신화 로그에서 차단 대상 시전 완료가 확인됐습니다.","nextAction":"다음 풀: 우물 담당조의 Drowned Echo 차단 순번을 확인하고 영혼살무사의 저주 완료를 0회로 만드세요."}
    else:
        candidate_fail=next((x for x in timeline if x.get("status")=="failed"),None)
        real_stream = str(source_kind or "").lower() in {"raw", "archive", "wcl"} and bool(rites or haunted or essence or void_strike)
        verdict={"severity":"WARN" if candidate_fail else "INFO","status":"unknown","evidenceLevel":EVIDENCE_CANDIDATE if candidate_fail else EVIDENCE_UNAVAILABLE,"primary":("영혼살무사의 저주 완료 후보" if candidate_fail else "네크잘리 verified 실행 실패 미관측"),"why":("차단 대상 시전 완료 후보가 관측됐지만 ID/의미 근거가 모두 verified인 경우에만 실행 실패로 확정합니다." if candidate_fail else ("실제 로그에서 네크잘리 전투 신호는 확인됐고, 영혼살무사의 저주는 실제 신화 표본으로 성공/실패 의미가 검증됐습니다. 이 트라이에서는 verified 완료 실패가 관측되지 않았습니다." if real_stream else "현재 입력에서 verified 네크잘리 실행 실패 신호가 없습니다.")),"nextAction":("다음 풀: Drowned Echo의 영혼살무사의 저주 완료를 0회로 유지하고, 붕괴 시 포화 직접 사망 여부를 별도로 확인하세요." if real_stream else "실제 신화 raw/WCL이 들어오면 저주 시전·차단과 포화 붕괴 문맥을 재검증하세요.")}

    return {
      "schemaVersion":SCHEMA_VERSION,"engineVersion":ENGINE_VERSION, "rulesVersion":RULES_VERSION,"bossKey":"nekzali","sourceKind":source_kind,
      "encounter":{"name":encounter.get("name") or encounter.get("encounterName") or rule.get("displayName"),"encounterID":encounter.get("encounterID"),"difficultyID":encounter.get("difficultyID") or encounter.get("difficulty"),"tryNo":encounter.get("tryNo"),"result":encounter.get("result") or ("Kill" if encounter.get("kill") else "Wipe"),"durationSec":encounter.get("durationSec")},
      "rule":{"bossKey":rule.get("bossKey"),"schemaVersion":rule.get("schemaVersion"),"idBased":True,"provenance":"mixed-actual-mythic-verified"},
      "strategy":{"source":strategy_source,"values":strategy},"checks":checks,"timeline":sorted(timeline,key=lambda x:float(x.get("sec") or 0)),
      "observations":{"graspingDepths":{"signalCount":len(grasp),"drownedEchoSummonCount":len(echo_summons)},"soulcoilersCurse":curse,"soulExhaustion":{"applyCount":len(exhaustion),"uniquePlayers":len(exhaustion_by),"byPlayer":dict(exhaustion_by)},"soulcoilRite":{"episodeCount":len(rite_eps),"eventCount":len(rites),"ambientRaidPressure":True},"ritualBurn":{"eventCount":len(ritual_burn)},"hauntedSalvoCast":{"startCount":len(haunted_cast_starts),"completeCount":len(haunted_cast_completes),"starts":[round(float(x.get("sec") or 0),3) for x in haunted_cast_starts],"completes":[round(float(x.get("sec") or 0),3) for x in haunted_cast_completes]},"hauntedSalvo":{"eventCount":len(haunted)},"voidStrike":{"eventCount":len(void_strike)},"essenceShatter":{"eventCount":len(essence)},"invoke":{"eventCount":len(invokes)},"corpseBlight":{"episodeCount":len(corpse_eps)},"verifiedFailures":[dict(x) for x in failures],"firstVerifiedFailure":first},
      "verdict":verdict,"collapseLink":{"status":"unknown","evidenceLevel":EVIDENCE_UNAVAILABLE,"summary":"B31 붕괴와 동일 기믹의 직접 인과가 검증될 때만 승격"},
      "limitations":["영혼살무사의 저주(1300238)는 2026-10-04 실제 신화에서 전부 차단된 Kill과 시전 완료가 발생한 Kill을 함께 비교해 차단 성공/완료 실패 의미를 verified로 고정합니다.","Soulcoil Rite와 귀신 들린 포화는 정상 Kill에서도 반복되는 전투 패턴이므로 발생 자체를 플레이어 실수로 판정하지 않습니다.","Soul Exhaustion은 우물 담당조가 정상적으로 나올 때도 적용되므로 배정 정보 없이 성공/실패를 판정하지 않습니다.","Immortal Coil / Vessel of Awakening은 현재 확보한 실제 신화 캡슐에서 검증되지 않아 candidate를 유지합니다.","귀신 들린 포화 다수 직접 사망은 붕괴 메커니즘만 확정하며 탱커 방향·유령 경로·개인 책임은 별도 근거 없이 단정하지 않습니다."]
    }



def _lost_player_label(row: dict) -> str:
    return str(row.get("destName") or row.get("targetName") or row.get("destGUID") or "?").split("-")[0]


def _lost_direct_episodes(rows: list[dict], rule: dict, mechanic_key: str, *, source_kind: str, max_gap: float = 0.45) -> list[dict]:
    ids = _rule_spell_ids(rule, mechanic_key)
    signal = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids
        and (_is_damage(x) or _is_aura_apply(x) or _is_completed_cast(x))
    ]
    out = []
    id_evidence = _observed_evidence_for_rule(rule, mechanic_key, source_kind)
    meaning_evidence = _meaning_evidence_for_rule(rule, mechanic_key)
    for group in _group_times(signal, max_gap=max_gap):
        targets = sorted({_lost_player_label(x) for x in group if _is_player_target(x)})
        out.append({
            "sec": round(min(float(x.get("sec") or 0) for x in group), 3),
            "lastSec": round(max(float(x.get("sec") or 0) for x in group), 3),
            "uniqueTargets": len(targets),
            "targets": targets,
            "eventCount": len(group),
            "spellIDs": sorted(ids),
            "idEvidenceLevel": id_evidence,
            "meaningEvidenceLevel": meaning_evidence,
            "evidenceLevel": meaning_evidence,
        })
    return out


def _lost_elemental_aura_context(rows: list[dict], rule: dict, explosion_eps: list[dict]) -> list[dict]:
    burning = _rule_spell_ids(rule, "frostfireVolley", "burningFlamesSpellIDs")
    piercing = _rule_spell_ids(rule, "frostfireVolley", "piercingFrostSpellIDs")
    tracked = burning | piercing
    if not tracked or not explosion_eps:
        return []
    active: dict[object, dict] = {}
    out = []
    eps = sorted(explosion_eps, key=lambda x: float(x.get("sec") or 0))
    ei = 0
    ordered = sorted(rows, key=lambda x: float(x.get("sec") or 0))
    for row in ordered:
        sec = float(row.get("sec") or 0)
        while ei < len(eps) and float(eps[ei].get("sec") or 0) < sec - 1e-6:
            ep = dict(eps[ei])
            ep["activeElementalCarriers"] = sorted(
                [
                    {"player": v.get("player"), "spellID": v.get("spellID"), "appliedSec": v.get("appliedSec")}
                    for v in active.values()
                ],
                key=lambda x: (str(x.get("player") or ""), int(x.get("spellID") or 0)),
            )
            out.append(ep)
            ei += 1
        sid = safe_int(row.get("spellID") or row.get("abilityGameID"))
        if sid not in tracked or not _is_player_target(row):
            continue
        key = _target_key(row)
        if key is None:
            continue
        ev = _event_name(row)
        if ev in {"spell_aura_applied", "applydebuff", "spell_aura_applied_dose", "applydebuffstack", "refreshdebuff", "spell_aura_refresh"}:
            active[key] = {"player": _lost_player_label(row), "spellID": sid, "appliedSec": round(sec, 3)}
        elif ev in {"spell_aura_removed", "removedebuff", "spell_aura_removed_dose", "removedebuffstack"}:
            active.pop(key, None)
    while ei < len(eps):
        ep = dict(eps[ei])
        ep["activeElementalCarriers"] = sorted(
            [
                {"player": v.get("player"), "spellID": v.get("spellID"), "appliedSec": v.get("appliedSec")}
                for v in active.values()
            ],
            key=lambda x: (str(x.get("player") or ""), int(x.get("spellID") or 0)),
        )
        out.append(ep)
        ei += 1
    return out


def _lost_final_ascension(rows: list[dict], rule: dict, *, source_kind: str) -> dict:
    """Collect Final Ascension cycle evidence without assigning failure meaning.

    B37.4.6 real Mythic Kill/Wipe comparison disproved the old assumption that
    completion/damage is itself a wipe mechanic: 1292780 occurs repeatedly in a
    successful kill.  Preserve the cycle as observation data only.
    """
    cast_ids = _rule_spell_ids(rule, "finalAscension", "castSpellIDs")
    damage_ids = _rule_spell_ids(rule, "finalAscension", "damageSpellIDs")
    charge_ids = _rule_spell_ids(rule, "finalAscension", "chargeAuraSpellIDs")
    channel_ids = _rule_spell_ids(rule, "finalAscension", "channelAuraSpellIDs")
    fish_ids = _rule_spell_ids(rule, "fishyFeedback", "fishUseSpellIDs") or _rule_spell_ids(rule, "fishyFeedback")
    command_ids = _rule_spell_ids(rule, "fishyFeedback", "commandSpellIDs")
    starts = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in cast_ids and _is_cast_start(x)]
    completes = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in cast_ids and _is_completed_cast(x)]
    damage = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in damage_ids and _is_damage(x)]
    charge_starts = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in charge_ids and _is_aura_apply(x)]
    charge_ends = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in charge_ids and _event_name(x) in {"spell_aura_removed", "removebuff", "removedebuff"}]
    channel_starts = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in channel_ids and _is_aura_apply(x)]
    commands = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in command_ids and _is_completed_cast(x)]
    fish = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in fish_ids
            and str(x.get("sourceType") or "") == "Player" and str(x.get("targetType") or "") == "NPC"
            and (_is_aura_apply(x) or _is_completed_cast(x))]
    starts = [g[0] for g in _group_times(starts, max_gap=0.5)]
    charge_starts = [g[0] for g in _group_times(charge_starts, max_gap=0.5)]
    charge_ends = [g[0] for g in _group_times(charge_ends, max_gap=0.5)]
    channel_starts = [g[0] for g in _group_times(channel_starts, max_gap=0.5)]
    commands = [g[0] for g in _group_times(commands, max_gap=0.5)]
    fish_eps = [
        {
            "sec": round(min(float(x.get("sec") or 0) for x in g), 3),
            "uniqueTargets": len({_target_key(x) for x in g if _is_player_target(x) and _target_key(x) is not None}),
            "eventCount": len(g),
            "players": sorted({str(x.get("sourceName") or "?").split("-")[0] for x in g}),
            "targets": sorted({str(x.get("destName") or x.get("targetName") or "?").split("-")[0] for x in g}),
            "idEvidenceLevel": _observed_evidence_for_rule(rule, "fishyFeedback", source_kind),
            "evidenceLevel": _observed_evidence_for_rule(rule, "fishyFeedback", source_kind),
        }
        for g in _group_times(fish, max_gap=0.5)
    ]
    damage_eps = [
        {
            "sec": round(min(float(x.get("sec") or 0) for x in g), 3),
            "uniqueTargets": len({_target_key(x) for x in g if _is_player_target(x) and _target_key(x) is not None}),
            "eventCount": len(g),
            "idEvidenceLevel": _observed_evidence_for_rule(rule, "finalAscension", source_kind),
            "evidenceLevel": _observed_evidence_for_rule(rule, "finalAscension", source_kind),
        }
        for g in _group_times(damage, max_gap=0.6)
    ]
    outcomes = []
    for st in starts:
        sec = float(st.get("sec") or 0)
        next_start = min((float(x.get("sec") or 0) for x in starts if float(x.get("sec") or 0) > sec), default=float("inf"))
        fish_after = [x for x in fish_eps if sec <= float(x.get("sec") or 0) < next_start]
        dmg_after = [x for x in damage_eps if sec <= float(x.get("sec") or 0) < next_start]
        done_after = [x for x in completes if sec <= float(x.get("sec") or 0) < next_start]
        end_candidates = [float(x.get("sec") or 0) for x in fish_after + dmg_after + done_after]
        end_sec = min(end_candidates, default=None)
        result = "cycle_observed" if end_sec is not None else "start_only"
        row = {
            "sec": round(sec, 3), "status": "observation", "result": result,
            "idEvidenceLevel": _observed_evidence_for_rule(rule, "finalAscension", source_kind),
            "evidenceLevel": _observed_evidence_for_rule(rule, "finalAscension", source_kind),
        }
        if end_sec is not None:
            row["endSec"] = round(end_sec, 3)
        outcomes.append(row)
    if not starts:
        for ep in damage_eps:
            outcomes.append({
                "sec": ep["sec"], "endSec": ep["sec"], "status": "observation", "result": "damage_without_start",
                "idEvidenceLevel": ep.get("idEvidenceLevel") or _observed_evidence_for_rule(rule, "finalAscension", source_kind),
                "evidenceLevel": ep.get("idEvidenceLevel") or ep.get("evidenceLevel") or EVIDENCE_CANDIDATE,
            })
        for x in completes:
            sec = round(float(x.get("sec") or 0), 3)
            outcomes.append({
                "sec": sec, "endSec": sec, "status": "observation", "result": "complete_without_start",
                "idEvidenceLevel": _observed_evidence_for_rule(rule, "finalAscension", source_kind),
                "evidenceLevel": _observed_evidence_for_rule(rule, "finalAscension", source_kind),
            })
    outcomes.sort(key=lambda x: float(x.get("endSec") or x.get("sec") or 0))

    # B37.5.0.3: model the actual fish cycle from the ~60 sec charge aura.
    # A fish may be thrown before the visible Final Ascension cast, during the
    # cast, or even late enough that a successful kill takes a few channel
    # ticks.  Timing is therefore observation/coaching data, not a failure by
    # itself.  Only a separate B31 direct-death collapse link may promote it.
    fish_cycles = []
    charge_points = [float(x.get("sec") or 0) for x in charge_starts]
    for i, charge_row in enumerate(charge_starts):
        cycle_start = float(charge_row.get("sec") or 0)
        next_start = charge_points[i + 1] if i + 1 < len(charge_points) else float("inf")
        end_rows = [x for x in charge_ends if cycle_start <= float(x.get("sec") or 0) < next_start]
        deadline = float(end_rows[0].get("sec") or 0) if end_rows else None
        feed_rows = [x for x in fish_eps if cycle_start <= float(x.get("sec") or 0) < next_start]
        feed = feed_rows[0] if feed_rows else None
        cast_rows = [x for x in starts if cycle_start <= float(x.get("sec") or 0) < next_start]
        done_rows = [x for x in completes if cycle_start <= float(x.get("sec") or 0) < next_start]
        channel_rows = [x for x in channel_starts if cycle_start <= float(x.get("sec") or 0) < next_start]
        damage_rows = [x for x in damage_eps if cycle_start <= float(x.get("sec") or 0) < next_start]
        command_rows = [x for x in commands if cycle_start <= float(x.get("sec") or 0) < next_start]
        cast_start_sec = float(cast_rows[0].get("sec") or 0) if cast_rows else None
        cast_done_sec = float(done_rows[0].get("sec") or 0) if done_rows else None
        channel_sec = float(channel_rows[0].get("sec") or 0) if channel_rows else None
        feed_sec = float(feed.get("sec") or 0) if feed else None
        command_after_feed = [x for x in command_rows if feed_sec is None or float(x.get("sec") or 0) >= feed_sec]
        command_sec = float(command_after_feed[0].get("sec") or 0) if command_after_feed else None
        damage_event_count = sum(int(x.get("eventCount") or 0) for x in damage_rows)
        if feed is None:
            timing = "no_feed"
        elif damage_rows:
            timing = "feed_with_ascension_damage"
        elif cast_done_sec is not None:
            timing = "feed_before_damage_after_cast"
        elif cast_start_sec is not None and feed_sec is not None and feed_sec >= cast_start_sec:
            timing = "feed_during_cast"
        else:
            timing = "feed_before_cast"
        cyc = {
            "cycle": i + 1,
            "chargeStartSec": round(cycle_start, 3),
            "deadlineSec": round(deadline, 3) if deadline is not None else None,
            "feedSec": round(feed_sec, 3) if feed_sec is not None else None,
            "feedPlayer": (feed.get("players") or [None])[0] if feed else None,
            "feedTarget": (feed.get("targets") or [None])[0] if feed else None,
            "castStartSec": round(cast_start_sec, 3) if cast_start_sec is not None else None,
            "castCompleteSec": round(cast_done_sec, 3) if cast_done_sec is not None else None,
            "channelStartSec": round(channel_sec, 3) if channel_sec is not None else None,
            "commandSec": round(command_sec, 3) if command_sec is not None else None,
            "ascensionDamageEpisodeCount": len(damage_rows),
            "ascensionDamageEventCount": damage_event_count,
            "timing": timing,
            "status": "observation",
            "evidenceLevel": EVIDENCE_VERIFIED if str(source_kind or "").lower() in {"raw", "archive"} else EVIDENCE_CANDIDATE,
        }
        if deadline is not None and feed_sec is not None:
            cyc["feedBeforeDeadlineSec"] = round(deadline - feed_sec, 3)
        if cast_start_sec is not None and feed_sec is not None:
            cyc["feedAfterCastStartSec"] = round(feed_sec - cast_start_sec, 3)
        if feed_sec is not None and command_sec is not None:
            cyc["commandAfterFeedSec"] = round(command_sec - feed_sec, 3)
        fish_cycles.append(cyc)

    return {
        "startCount": len(starts), "completeCount": len(completes), "damageEpisodeCount": len(damage_eps),
        "chargeCycleCount": len(charge_starts), "chargeStarts": [round(float(x.get("sec") or 0), 3) for x in charge_starts],
        "chargeEnds": [round(float(x.get("sec") or 0), 3) for x in charge_ends],
        "fishResetCount": len(fish_eps), "fishEpisodes": fish_eps, "damageEpisodes": damage_eps, "outcomes": outcomes,
        "fishCycles": fish_cycles,
    }


def _lost_interrupt_resolution(rows: list[dict], rule: dict, mechanic_key: str, *, source_kind: str, cast_window_sec: float = 8.0) -> dict:
    ids = _rule_spell_ids(rule, mechanic_key, "castSpellIDs") or _rule_spell_ids(rule, mechanic_key)
    starts = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids and _is_cast_start(x)]
    completes = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids and _is_completed_cast(x)]
    interrupts = [x for x in rows if _is_interrupt(x) and safe_int(x.get("extraSpellID") or x.get("extraAbilityGameID")) in ids]
    starts = [g[0] for g in _group_times(starts, max_gap=0.5)]
    outcomes=[]
    used_done=set(); used_kick=set()
    for st in starts:
        sec=float(st.get("sec") or 0)
        done=[(i,x) for i,x in enumerate(completes) if i not in used_done and sec <= float(x.get("sec") or 0) <= sec+cast_window_sec]
        kicked=[(i,x) for i,x in enumerate(interrupts) if i not in used_kick and sec <= float(x.get("sec") or 0) <= sec+cast_window_sec]
        candidates=[]
        if done: candidates.append((float(done[0][1].get("sec") or 0),"failed",done[0]))
        if kicked: candidates.append((float(kicked[0][1].get("sec") or 0),"success",kicked[0]))
        if candidates:
            _t,status,(idx,x)=min(candidates,key=lambda z:z[0])
            if status=="failed": used_done.add(idx)
            else: used_kick.add(idx)
            row={"sec":round(sec,3),"endSec":round(float(x.get("sec") or 0),3),"status":status,
                 "result":"completed" if status=="failed" else "interrupted",
                 "evidenceLevel":_observed_evidence_for_rule(rule,mechanic_key,source_kind)}
            if status=="failed":
                row["target"] = str(x.get("destName") or x.get("targetName") or "?").split("-")[0]
            else:
                row["player"] = str(x.get("sourceName") or "?").split("-")[0]
            outcomes.append(row)
        else:
            outcomes.append({"sec":round(sec,3),"status":"unknown","result":"unresolved","evidenceLevel":EVIDENCE_UNAVAILABLE})
    outcomes.sort(key=lambda x:float(x.get("sec") or 0))
    return {"startCount":len(starts),"completeCount":len(completes),"interruptCount":len(interrupts),"outcomes":outcomes}


def _lost_stream_complete(source_kind: str) -> bool:
    """Compact archive/local raw preserve Relic Rupture damage events."""
    return str(source_kind or "").strip().lower() in {"raw", "archive"}

def _analyze_lost_explorers(events: Iterable[dict], encounter: dict, rule: dict, *, source_kind: str) -> dict:
    rows = sorted((dict(x) for x in events if x.get("sec") is not None), key=lambda x: float(x.get("sec") or 0))
    strategy, strategy_source = load_strategy("lost_explorers")
    asc = _lost_final_ascension(rows, rule, source_kind=source_kind)
    icebound = _lost_interrupt_resolution(rows, rule, "iceboundFlames", source_kind=source_kind, cast_window_sec=6.0)
    relic = _lost_direct_episodes(rows, rule, "relicRupture", source_kind=source_kind)
    explosion = _lost_direct_episodes(rows, rule, "elementalExplosion", source_kind=source_kind)
    explosion = _lost_elemental_aura_context(rows, rule, explosion)
    spreading = _lost_direct_episodes(rows, rule, "spreadingFlames", source_kind=source_kind)

    thud_target_ids = _rule_spell_ids(rule, "mightyThud", "targetSpellIDs")
    thud_impact_ids = _rule_spell_ids(rule, "mightyThud", "impactSpellIDs")
    thud_targets = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in thud_target_ids and (_is_aura_apply(x) or _is_damage(x))]
    thud_impacts = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in thud_impact_ids and _is_damage(x)]
    thud_eps = []
    for g in _group_times(thud_impacts, max_gap=0.35):
        thud_eps.append({
            "sec": round(min(float(x.get("sec") or 0) for x in g), 3),
            "uniqueTargets": len({_target_key(x) for x in g if _is_player_target(x) and _target_key(x) is not None}),
            "eventCount": len(g),
            "idEvidenceLevel": _observed_evidence_for_rule(rule, "mightyThud", source_kind),
            "evidenceLevel": _observed_evidence_for_rule(rule, "mightyThud", source_kind),
        })

    defense_ids = _rule_spell_ids(rule, "unitedDefense")
    defense = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in defense_ids and (_is_aura_apply(x) or _is_completed_cast(x))]

    checks: list[dict] = []
    timeline: list[dict] = []
    verified_failures: list[dict] = []

    # B37.4.6: real Mythic kill proves Final Ascension completion/damage is a
    # normal encounter cycle, not a direct-failure signal.  Show what happened
    # without feeding firstMistake/majorIncident/collapse slots.
    asc_signal_count = (int(asc.get("chargeCycleCount") or 0) + int(asc.get("startCount") or 0)
                        + int(asc.get("completeCount") or 0) + int(asc.get("damageEpisodeCount") or 0)
                        + int(asc.get("fishResetCount") or 0))
    if asc_signal_count:
        asc_check = make_check(
            key="final_ascension", title="최후의 승천 · 사이클 관측", status="unknown",
            evidence_level=EVIDENCE_VERIFIED if str(source_kind or '').lower() in {'raw','archive'} else EVIDENCE_CANDIDATE,
            summary=(f"충전 사이클 {asc.get('chargeCycleCount') or 0} · 승천 시작 {asc.get('startCount') or 0} · "
                     f"완료 {asc.get('completeCount') or 0} · 공대 피해 묶음 {asc.get('damageEpisodeCount') or 0} · "
                     f"물고기 전달 {asc.get('fishResetCount') or 0} · "
                     "전달 시각/승천 노출은 관측용이며 발생 자체는 실패로 판정하지 않음"),
            ids={"castSpellIDs": sorted(_rule_spell_ids(rule, 'finalAscension', 'castSpellIDs')),
                 "damageSpellIDs": sorted(_rule_spell_ids(rule, 'finalAscension', 'damageSpellIDs'))},
        )
        asc_check["informational"] = True
        asc_check["rawStatus"] = "observed"
        asc_check["displayValue"] = "관측"
        checks.append(asc_check)
        for o in asc.get("outcomes") or []:
            timeline.append({
                "kind": "final_ascension_cycle", "sec": o.get("endSec") or o.get("sec"),
                "label": "최후의 승천 사이클", "summary": str(o.get("result") or "cycle_observed"),
                "status": "observation", "evidenceLevel": o.get("evidenceLevel") or EVIDENCE_CANDIDATE,
                "failureEligible": False,
            })
    else:
        checks.append(make_check(
            key="final_ascension", title="최후의 승천 · 사이클 관측", status="unknown",
            evidence_level=EVIDENCE_UNAVAILABLE, summary="최후의 승천 관련 신호 미관측",
        ))

    # B37.5.0.3: expose the actual Disgusting Fish hand-off on the mechanic
    # timeline.  This is coaching/observation data only.  A late hand-off, a
    # completed Final Ascension cast, or even a few 1292780 damage ticks can
    # occur in successful Mythic kills, so none of those facts alone is a
    # verified failure.
    for cycle in asc.get("fishCycles") or []:
        feed_sec = cycle.get("feedSec")
        if feed_sec is None:
            continue
        player = str(cycle.get("feedPlayer") or "?")
        target = str(cycle.get("feedTarget") or "?")
        before = cycle.get("feedBeforeDeadlineSec")
        if before is None:
            timing_text = "승천 마감 시각 미관측"
        else:
            try:
                delta = float(before)
            except (TypeError, ValueError):
                delta = 0.0
            if delta >= 0:
                timing_text = f"승천 마감 {delta:.1f}초 전"
            else:
                timing_text = f"승천 마감 {abs(delta):.1f}초 후"
        damage_count = int(cycle.get("ascensionDamageEventCount") or 0)
        if damage_count > 0:
            timing_text += f" · 승천 피해 노출 {damage_count}건"
        timeline.append({
            "kind": "fish_feed",
            "sec": feed_sec,
            "label": "역겨운 물고기 전달",
            "title": "역겨운 물고기 전달",
            "summary": f"{player} → {target} · {timing_text}",
            "status": "observation",
            "evidenceLevel": cycle.get("evidenceLevel") or EVIDENCE_CANDIDATE,
            "failureEligible": False,
            "cycle": cycle.get("cycle"),
            "player": player,
            "target": target,
            "deadlineSec": cycle.get("deadlineSec"),
            "feedBeforeDeadlineSec": cycle.get("feedBeforeDeadlineSec"),
            "feedAfterCastStartSec": cycle.get("feedAfterCastStartSec"),
            "ascensionDamageEventCount": damage_count,
        })

    def direct_check(key: str, mechanic_key: str, title: str, episodes: list[dict], fail_label: str, next_summary: str, *, allow_verified_absence: bool = False):
        meaning_ev = _meaning_evidence_for_rule(rule, mechanic_key)
        if episodes:
            first = episodes[0]
            id_ev = first.get("idEvidenceLevel") or _observed_evidence_for_rule(rule, mechanic_key, source_kind)
            combined_ev = _combined_evidence(id_ev, meaning_ev)
            check = make_check(
                key=key, title=title, status="failed", evidence_level=combined_ev,
                summary=f"{_fmt_time(first.get('sec'))} · {next_summary} · 공대 피격 {first.get('uniqueTargets') or 0}명",
                ids={"spellIDs": first.get("spellIDs") or []},
            )
            check["idEvidenceLevel"] = id_ev
            check["meaningEvidenceLevel"] = meaning_ev
            checks.append(check)
            for ep in episodes:
                ep_id_ev = ep.get("idEvidenceLevel") or id_ev
                ep_combined = _combined_evidence(ep_id_ev, meaning_ev)
                item = {
                    "kind": key, "sec": ep.get("sec"), "label": fail_label,
                    "summary": f"공대 피격 {ep.get('uniqueTargets') or 0}명",
                    "status": "failed", "idEvidenceLevel": ep_id_ev,
                    "meaningEvidenceLevel": meaning_ev, "evidenceLevel": ep_combined,
                }
                timeline.append(item)
                if ep_combined == EVIDENCE_VERIFIED:
                    verified_failures.append(item)
        else:
            rule_mech = ((rule.get("mechanics") or {}).get(mechanic_key) or {})
            id_ev = str(rule_mech.get("idEvidenceLevel") or EVIDENCE_CANDIDATE)
            combined_ev = _combined_evidence(id_ev, meaning_ev)
            if allow_verified_absence and _lost_stream_complete(source_kind) and combined_ev == EVIDENCE_VERIFIED:
                check = make_check(
                    key=key, title=title, status="success", evidence_level=EVIDENCE_VERIFIED,
                    summary=f"직접 실패 신호 0회 · 완전한 {str(source_kind).lower()} 스트림에서 미관측",
                )
                check["idEvidenceLevel"] = id_ev
                check["meaningEvidenceLevel"] = meaning_ev
                checks.append(check)
            else:
                check = make_check(
                    key=key, title=title, status="unknown", evidence_level=EVIDENCE_UNAVAILABLE,
                    summary=f"{title} 직접 실패 신호 미관측 · 현재 표본만으로 성공 판정하지 않음",
                )
                check["idEvidenceLevel"] = id_ev if id_ev == EVIDENCE_VERIFIED else EVIDENCE_UNAVAILABLE
                check["meaningEvidenceLevel"] = meaning_ev
                checks.append(check)

    # Icebound Flames: actual Kill/Wipe comparison verifies completion as an interrupt failure.
    ice_failed = [x for x in (icebound.get("outcomes") or []) if x.get("status") == "failed"]
    ice_ok = [x for x in (icebound.get("outcomes") or []) if x.get("status") == "success"]
    ice_unknown = [x for x in (icebound.get("outcomes") or []) if x.get("status") == "unknown"]
    ice_meaning = _meaning_evidence_for_rule(rule, "iceboundFlames")
    ice_success_meaning = _meaning_evidence_for_rule(rule, "iceboundFlames", success=True)
    ice_id = _observed_evidence_for_rule(rule, "iceboundFlames", source_kind) if int(icebound.get("startCount") or 0) else EVIDENCE_UNAVAILABLE
    if ice_failed:
        first_ice = ice_failed[0]
        ice_ev = _combined_evidence(ice_id, ice_meaning)
        checks.append(make_check(
            key="icebound_flames", title="얼음결속 불꽃 · 차단", status="failed", evidence_level=ice_ev,
            summary=f"{_fmt_time(first_ice.get('endSec') or first_ice.get('sec'))} · 시전 완료 {len(ice_failed)}회 · 차단 {len(ice_ok)}회" + (f" · 대상 {first_ice.get('target')}" if first_ice.get('target') and first_ice.get('target') != '?' else ""),
            ids={"castSpellIDs": sorted(_rule_spell_ids(rule, 'iceboundFlames', 'castSpellIDs'))},
        ))
        for ep in ice_failed:
            item={"kind":"icebound_flames","sec":ep.get("endSec") or ep.get("sec"),"label":"얼음결속 불꽃 미차단",
                  "summary":f"시전 완료" + (f" · 대상 {ep.get('target')}" if ep.get('target') and ep.get('target') != '?' else ""),
                  "status":"failed","idEvidenceLevel":ice_id,"meaningEvidenceLevel":ice_meaning,"evidenceLevel":ice_ev}
            timeline.append(item)
            if ice_ev == EVIDENCE_VERIFIED: verified_failures.append(item)
    elif int(icebound.get("startCount") or 0) and not ice_unknown and _lost_stream_complete(source_kind):
        ice_ev = _combined_evidence(ice_id, ice_success_meaning)
        checks.append(make_check(
            key="icebound_flames", title="얼음결속 불꽃 · 차단", status="success", evidence_level=ice_ev,
            summary=f"관측된 시전 {icebound.get('startCount') or 0}회 모두 차단 · 완료 0회",
            ids={"castSpellIDs": sorted(_rule_spell_ids(rule, 'iceboundFlames', 'castSpellIDs'))},
        ))
    else:
        checks.append(make_check(
            key="icebound_flames", title="얼음결속 불꽃 · 차단", status="unknown",
            evidence_level=ice_id if int(icebound.get("startCount") or 0) else EVIDENCE_UNAVAILABLE,
            summary=(f"시작 {icebound.get('startCount') or 0} · 차단 {len(ice_ok)} · 완료 0 · 미해결 {len(ice_unknown)}" if int(icebound.get('startCount') or 0) else "차단 대상 시전 미관측"),
            ids={"castSpellIDs": sorted(_rule_spell_ids(rule, 'iceboundFlames', 'castSpellIDs'))},
        ))

    direct_check(
        "relic_rupture", "relicRupture", "유물 25초 방치 · 유물 파열", relic,
        "유물 파열", "Relic Rupture 발생", allow_verified_absence=True,
    )
    direct_check(
        "elemental_explosion", "elementalExplosion", "서리불꽃 중첩 · 원소 폭발", explosion,
        "원소 폭발", "Elemental Explosion 발생", allow_verified_absence=True,
    )

    if explosion:
        for ep in explosion:
            carriers = ep.get("activeElementalCarriers") or []
            if carriers:
                timeline.append({
                    "kind": "elemental_aura_context", "sec": ep.get("sec"), "label": "원소 폭발 당시 잔존 디버프",
                    "summary": ", ".join(f"{x.get('player')}({x.get('spellID')})" for x in carriers[:8]),
                    "status": "observation", "evidenceLevel": ep.get("idEvidenceLevel") or EVIDENCE_CANDIDATE,
                    "failureEligible": False,
                })

    # Spreading Flames was wipe-only in the 77-pull corpus, but the observed
    # hit itself is not enough to prove the upstream positioning/ground-failure
    # boundary.  Surface it explicitly as a candidate observation and never
    # feed it into firstMistake/majorIncident/collapse slots.
    if spreading:
        sp_first = spreading[0]
        sp_check = make_check(
            key="spreading_flames", title="번지는 불길 · 후보 관측", status="unknown",
            evidence_level=EVIDENCE_CANDIDATE,
            summary=(f"피격 묶음 {len(spreading)}회 · 첫 관측 {_fmt_time(sp_first.get('sec'))} · "
                     "77표본에서 Wipe에만 관측됐지만 실패 경계 미확정"),
            ids={"spellIDs": sorted(_rule_spell_ids(rule, 'spreadingFlames'))},
        )
        sp_check["informational"] = True
        sp_check["rawStatus"] = "observed"
        sp_check["displayValue"] = "후보 관측"
        sp_check["idEvidenceLevel"] = sp_first.get("idEvidenceLevel") or EVIDENCE_VERIFIED
        sp_check["meaningEvidenceLevel"] = EVIDENCE_CANDIDATE
        checks.append(sp_check)
        for ep in spreading:
            timeline.append({
                "kind": "spreading_flames_candidate", "sec": ep.get("sec"),
                "label": "번지는 불길 · 후보 관측",
                "summary": f"피격 {ep.get('uniqueTargets') or 0}명 · 실패 조건 미확정",
                "status": "observation", "idEvidenceLevel": ep.get("idEvidenceLevel") or EVIDENCE_VERIFIED,
                "meaningEvidenceLevel": EVIDENCE_CANDIDATE, "evidenceLevel": EVIDENCE_CANDIDATE,
                "failureEligible": False,
            })
    else:
        checks.append(make_check(
            key="spreading_flames", title="번지는 불길 · 후보 관측", status="unknown",
            evidence_level=EVIDENCE_UNAVAILABLE, summary="번지는 불길 직접 피격 신호 미관측 · 후보 규칙 유지",
            ids={"spellIDs": sorted(_rule_spell_ids(rule, 'spreadingFlames'))},
        ))

    # Mighty Thud occurs repeatedly in successful Mythic kills.  Treat the
    # observed impact cycle as neutral context until a distinct empty-soak
    # event shape is proven.
    thud_count = len(thud_targets) + len(thud_eps)
    thud_check = make_check(
        key="mighty_thud", title="강력한 후려치기 · 관측", status="unknown",
        evidence_level=_observed_evidence_for_rule(rule, 'mightyThud', source_kind) if thud_count else EVIDENCE_UNAVAILABLE,
        summary=(f"대상 신호 {len(thud_targets)} · impact 묶음 {len(thud_eps)} · Kill에서도 반복 관측되어 발생 자체는 실패로 판정하지 않음" if thud_count else "Mighty Thud 신호 미관측"),
        ids={"targetSpellIDs": sorted(thud_target_ids), "impactSpellIDs": sorted(thud_impact_ids)},
    )
    if thud_count:
        thud_check["informational"] = True
        thud_check["rawStatus"] = "observed"
        thud_check["displayValue"] = "관측"
    checks.append(thud_check)

    defense_check = make_check(
        key="united_defense", title="합동 방어 · 관측", status="unknown",
        evidence_level=_observed_evidence_for_rule(rule, 'unitedDefense', source_kind) if defense else EVIDENCE_UNAVAILABLE,
        summary=(f"United Defense 적용/시전 {len(defense)}건 · Kill에서도 관측되는 정상 문맥 신호라 발생 자체는 실패로 판정하지 않음" if defense else "United Defense 직접 신호 미관측"),
        ids={"spellIDs": sorted(defense_ids)},
    )
    if defense:
        defense_check["informational"] = True
        defense_check["rawStatus"] = "observed"
        defense_check["displayValue"] = "관측"
    checks.append(defense_check)

    verified_failures.sort(key=lambda x: float(x.get("sec") or 0))
    timeline.sort(key=lambda x: float(x.get("sec") or 0))
    first = verified_failures[0] if verified_failures else None
    candidate_fail = next((x for x in timeline if x.get("status") == "failed" and x.get("evidenceLevel") == EVIDENCE_CANDIDATE), None)
    if first:
        action_by_kind = {
            "icebound_flames": "다음 풀: 얼음결속 불꽃 차단 순번을 지켜 시전 완료를 0회로 만드세요.",
            "relic_rupture": "다음 풀: 유물을 제한 시간 안에 처리해 Relic Rupture를 0회로 만드세요.",
            "elemental_explosion": "다음 풀: Frostfire Volley 전후 디버프 겹침을 해소하고 Elemental Explosion을 0회로 만드세요.",
        }
        verdict = {
            "severity": "HIGH", "status": "failed", "evidenceLevel": EVIDENCE_VERIFIED,
            "primary": first.get("label"),
            "why": f"{_fmt_time(first.get('sec'))}에 ID/event-shape와 실패 의미가 모두 검증된 직접 실패가 확인됐습니다.",
            "nextAction": action_by_kind.get(first.get("kind"), "다음 풀에서 해당 직접 실패 신호를 0회로 만드세요."),
        }
    elif candidate_fail:
        verdict = {
            "severity": "WARN", "status": "unknown", "evidenceLevel": EVIDENCE_CANDIDATE,
            "primary": candidate_fail.get("label") or "탐험가 실패 후보",
            "why": "실패 후보 신호는 관측됐지만 의미가 아직 candidate라 firstMistake/붕괴 원인으로 승격하지 않습니다.",
            "nextAction": "candidate 신호는 다음 Kill/Wipe 표본과 계속 대조하세요.",
        }
    else:
        verdict = {
            "severity": "INFO", "status": "unknown", "evidenceLevel": EVIDENCE_UNAVAILABLE,
            "primary": "탐험가 verified 실패 신호 미관측",
            "why": "현재 입력에서 의미까지 검증된 직접 실패 신호가 없습니다. Relic Rupture는 완전한 raw/archive에서 0회면 해당 카드가 정상으로 표시됩니다.",
            "nextAction": "다음 표본에서도 Relic Rupture와 Elemental Explosion 0회를 유지하고, 후보/관측 신호는 전멸 원인과 분리해 계속 검증하세요.",
        }

    return {
        "schemaVersion": SCHEMA_VERSION, "engineVersion": ENGINE_VERSION,
        "rulesVersion": RULES_VERSION, "bossKey": "lost_explorers", "sourceKind": source_kind,
        "encounter": {"name": encounter.get("name") or encounter.get("encounterName") or rule.get("displayName"),
                      "encounterID": encounter.get("encounterID"), "difficultyID": encounter.get("difficultyID") or encounter.get("difficulty"),
                      "tryNo": encounter.get("tryNo"), "result": encounter.get("result") or ("Kill" if encounter.get("kill") else "Wipe"),
                      "durationSec": encounter.get("durationSec")},
        "rule": {"bossKey": rule.get("bossKey"), "schemaVersion": rule.get("schemaVersion"), "idBased": True,
                 "provenance": "B37.4.28-lost-explorers-finish-77-pull-corpus"},
        "strategy": {"source": strategy_source, "values": strategy},
        "checks": checks, "timeline": timeline,
        "observations": {"finalAscension": asc, "iceboundFlames": icebound, "relicRupture": {"episodeCount": len(relic), "episodes": relic},
                         "elementalExplosion": {"episodeCount": len(explosion), "episodes": explosion},
                         "spreadingFlames": {"episodeCount": len(spreading), "episodes": spreading, "failureMeaning": "candidate"},
                         "mightyThud": {"targetSignalCount": len(thud_targets), "impactEpisodes": thud_eps},
                         "unitedDefense": {"signalCount": len(defense)},
                         "verifiedFailures": [dict(x) for x in verified_failures],
                         "firstVerifiedFailure": first},
        "verdict": verdict,
        "collapseLink": {"status": "unknown", "evidenceLevel": EVIDENCE_UNAVAILABLE, "summary": "B31 동일 기믹 직접 사망과의 exact link 대기"},
        "limitations": [
            "Relic Rupture 1310027은 신화 Historical 77표본에서 13킬 0회, 64와이프 중 12회 관측되어 직접 실패 의미를 재검증했습니다.",
            "Relic Rupture 발생 와이프는 최대 20명 동시 피격과 다수 즉시 사망으로 이어졌으며, 완전한 archive/raw에서 0회면 정상으로 표시합니다.",
            "Final Ascension 1292779/1292780은 성공 Kill에서도 완료/피해가 반복 관측되어 발생 자체를 실패로 판정하지 않습니다.",
            "Mighty Thud와 United Defense도 성공 Kill에서 반복 관측되므로 관측 전용입니다. 별도 empty-soak/이격 실패 event-shape가 확인되기 전에는 실패로 승격하지 않습니다.",
            "Icebound Flames 1286922는 77표본에서 627회 시작·564회 차단·27회 완료가 관측됐습니다. 완료는 verified 실패지만 킬에서도 2회 생존했으므로 B31 연결 없이 전멸 원인으로 단정하지 않습니다.",
            "실제 물고기 전달 1296535는 13/13 킬과 50/64 와이프에서 관측되어 정상 진행 문맥으로 유지합니다.",
            "Elemental Explosion 1295952는 신화 Historical 77표본에서 13킬 0회, 64와이프 중 3회(16/20/20명 피격) 관측되어 ID/event-shape와 실패 의미를 모두 verified로 잠갔습니다.",
        ],
    }

def _vashnik_damage_episodes(
    rows: list[dict],
    rule: dict,
    mechanic_key: str,
    fields: tuple[str, ...],
    *,
    source_kind: str,
    max_gap: float = 0.35,
) -> list[dict]:
    """Group Vashnik raid/player damage into pulses without over-counting targets.

    One raid-wide mechanic creates one damage row per player. A cluster with two
    simultaneous explosions therefore contains roughly twice as many rows, but
    the mechanic count is the maximum repeated hit count on the same target.
    """
    ids: set[int] = set()
    for field in fields:
        ids |= _rule_spell_ids(rule, mechanic_key, field)
    hits = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids
        and _is_damage(x)
        and _is_player_target(x)
    ]
    out = []
    for idx, group in enumerate(_group_times(hits, max_gap=max_gap), start=1):
        per_target = Counter(_target_key(x) for x in group if _target_key(x) is not None)
        pulse_count = max(per_target.values()) if per_target else 0
        first_sec = min(float(x.get("sec") or 0) for x in group)
        last_sec = max(float(x.get("sec") or 0) for x in group)
        out.append({
            "index": idx,
            "sec": round(first_sec, 3),
            "lastSec": round(last_sec, 3),
            "pulseCount": int(pulse_count),
            "uniqueTargets": len(per_target),
            "eventCount": len(group),
            "spellIDs": sorted({safe_int(x.get("spellID") or x.get("abilityGameID")) for x in group if safe_int(x.get("spellID") or x.get("abilityGameID")) is not None}),
            "idEvidenceLevel": _observed_evidence_for_rule(rule, mechanic_key, source_kind),
            "meaningEvidenceLevel": _meaning_evidence_for_rule(rule, mechanic_key),
            "evidenceLevel": _meaning_evidence_for_rule(rule, mechanic_key),
        })
    return out


def _vashnik_observed_count(rows: list[dict], rule: dict, mechanic_key: str, fields: tuple[str, ...], *, source_kind: str) -> dict:
    ids: set[int] = set()
    for field in fields:
        ids |= _rule_spell_ids(rule, mechanic_key, field)
    matches = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids]
    player_applies = [x for x in matches if _is_player_target(x) and _is_aura_apply(x)]
    return {
        "eventCount": len(matches),
        "playerApplyCount": len(player_applies),
        "uniquePlayers": len({_target_key(x) for x in player_applies if _target_key(x) is not None}),
        "spellIDs": sorted(ids),
        "idEvidenceLevel": _observed_evidence_for_rule(rule, mechanic_key, source_kind) if matches else EVIDENCE_UNAVAILABLE,
    }


def _vashnik_absence_stream_complete(source_kind: str) -> bool:
    """Whether a missing direct-failure event is meaningful for Vashnik.

    Local raw and compact archive preserve the damage/aura classes used by the
    two verified direct-failure rules.  Generic normalized/fixture/WCL subsets
    are not assumed complete here.
    """
    return str(source_kind or "").strip().lower() in {"raw", "archive"}


def _nymrissa_stream_complete(source_kind: str) -> bool:
    return str(source_kind or "").strip().lower() in {"raw", "archive", "fixture"}


def _nymrissa_unique_player_hits(rows: list[dict], spell_ids: set[int], *, gap_sec: float = 0.25) -> list[dict]:
    """Deduplicate DAMAGE/ABSORBED rows that describe one avoidable hit."""
    candidates = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in spell_ids
        and _is_player_target(x)
        and _event_name(x) in {"spell_damage", "spell_absorbed", "spell_missed", "damage"}
    ]
    by_target: dict[object, list[dict]] = defaultdict(list)
    for row in candidates:
        by_target[_target_key(row)].append(row)
    out: list[dict] = []
    for target, target_rows in by_target.items():
        target_rows.sort(key=lambda x: float(x.get("sec") or 0))
        cluster: list[dict] = []
        for row in target_rows:
            sec = float(row.get("sec") or 0)
            if not cluster or sec - float(cluster[-1].get("sec") or 0) <= gap_sec:
                cluster.append(row)
            else:
                first = cluster[0]
                out.append({
                    "sec": round(float(first.get("sec") or 0), 3),
                    "player": first.get("targetName") or first.get("destName") or str(target or "?"),
                    "eventCount": len(cluster),
                    "spellID": safe_int(first.get("spellID") or first.get("abilityGameID")),
                })
                cluster = [row]
        if cluster:
            first = cluster[0]
            out.append({
                "sec": round(float(first.get("sec") or 0), 3),
                "player": first.get("targetName") or first.get("destName") or str(target or "?"),
                "eventCount": len(cluster),
                "spellID": safe_int(first.get("spellID") or first.get("abilityGameID")),
            })
    out.sort(key=lambda x: float(x.get("sec") or 0))
    return out


def _nymrissa_spell_episodes(rows: list[dict], spell_ids: set[int], *, gap_sec: float = 0.35) -> list[dict]:
    hits = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in spell_ids
        and _event_name(x) in {"spell_damage", "spell_absorbed", "spell_missed", "damage"}
    ]
    out = []
    for group in _group_times(hits, max_gap=gap_sec):
        targets = {_target_key(x) for x in group if _is_player_target(x) and _target_key(x) is not None}
        out.append({
            "sec": round(min(float(x.get("sec") or 0) for x in group), 3),
            "lastSec": round(max(float(x.get("sec") or 0) for x in group), 3),
            "eventCount": len(group),
            "uniqueTargets": len(targets),
            "spellIDs": sorted({safe_int(x.get("spellID") or x.get("abilityGameID")) for x in group if safe_int(x.get("spellID") or x.get("abilityGameID")) is not None}),
        })
    return out


def _analyze_nymrissa(events: Iterable[dict], encounter: dict, rule: dict, *, source_kind: str) -> dict:
    rows = sorted((dict(x) for x in events if x.get("sec") is not None), key=lambda x: float(x.get("sec") or 0))
    stream_complete = _nymrissa_stream_complete(source_kind)
    checks: list[dict] = []
    timeline: list[dict] = []
    verified_failures: list[dict] = []

    # 1) Murloc leak -> Bubblefin Berserker.  The summon itself is the execution
    # failure; Pulsing Tides is retained as downstream damage/collapse evidence.
    leak_spec = ((rule.get("mechanics") or {}).get("berserkerLeak") or {})
    summon_ids = {safe_int(x) for x in (leak_spec.get("summonSpellIDs") or []) if safe_int(x) is not None}
    berserker_ids = {safe_int(x) for x in (leak_spec.get("berserkerNpcIDs") or []) if safe_int(x) is not None}
    pulse_ids = {safe_int(x) for x in (leak_spec.get("pulsingTidesSpellIDs") or []) if safe_int(x) is not None}
    summons = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in summon_ids
        and (not berserker_ids or safe_int(x.get("targetGameID")) in berserker_ids)
        and _event_name(x) in {"spell_summon", "summon"}
    ]
    spawn_eps = []
    for group in _group_times(summons, max_gap=1.0):
        guids = {str(x.get("destGUID") or x.get("targetID") or x.get("destName") or "") for x in group}
        guids.discard("")
        spawn_eps.append({
            "sec": round(min(float(x.get("sec") or 0) for x in group), 3),
            "spawnCount": len(guids) or len(group),
            "targets": sorted(guids),
        })
    pulse_eps = _nymrissa_spell_episodes(rows, pulse_ids, gap_sec=0.45)
    leak_id_ev = _observed_evidence_for_rule(rule, "berserkerLeak", source_kind) if summons else str(leak_spec.get("idEvidenceLevel") or EVIDENCE_VERIFIED)
    leak_meaning_ev = _meaning_evidence_for_rule(rule, "berserkerLeak")
    if spawn_eps:
        first = spawn_eps[0]
        combined = _combined_evidence(leak_id_ev, leak_meaning_ev)
        total = sum(int(x.get("spawnCount") or 0) for x in spawn_eps)
        checks.append(make_check(
            key="berserker_leak", title="멀록 유출 · 광전사 생성", status="failed", evidence_level=combined,
            summary=f"{_fmt_time(first.get('sec'))} · 광전사 {total}마리 생성 · 고동치는 해일 pulse {len(pulse_eps)}회",
            ids={"summonSpellIDs": sorted(summon_ids), "pulsingTidesSpellIDs": sorted(pulse_ids)},
        ))
        for ep in spawn_eps:
            item = {
                "kind": "berserker_leak", "sec": ep.get("sec"), "label": "멀록 유출 · 광전사 생성",
                "title": "멀록 유출 · 광전사 생성", "summary": f"광전사 {ep.get('spawnCount') or 0}마리 생성",
                "status": "failed", "evidenceLevel": combined,
                "idEvidenceLevel": leak_id_ev, "meaningEvidenceLevel": leak_meaning_ev,
                "spawnCount": ep.get("spawnCount"),
            }
            timeline.append(item)
            if combined == EVIDENCE_VERIFIED:
                verified_failures.append(dict(item))
    elif stream_complete:
        checks.append(make_check(
            key="berserker_leak", title="멀록 유출 · 광전사 생성", status="success", evidence_level=EVIDENCE_VERIFIED,
            summary="광전사 생성 0마리 · 완전한 raw/archive 스트림에서 미관측",
            ids={"summonSpellIDs": sorted(summon_ids), "pulsingTidesSpellIDs": sorted(pulse_ids)},
        ))
    else:
        checks.append(make_check(
            key="berserker_leak", title="멀록 유출 · 광전사 생성", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE,
            summary="광전사 생성 신호 미관측 · 현재 입력 범위로 성공 판정하지 않음",
        ))

    # 2) Frost Orb Shatter.  Failure meaning is locked, but absence remains
    # neutral until the ID itself has local/real-stream evidence.
    shatter_ids = _rule_spell_ids(rule, "frostOrbShatter", "spellIDs")
    shatter_eps = _nymrissa_spell_episodes(rows, shatter_ids, gap_sec=0.45)
    shatter_spec = ((rule.get("mechanics") or {}).get("frostOrbShatter") or {})
    shatter_id_cfg = str(shatter_spec.get("idEvidenceLevel") or EVIDENCE_CANDIDATE)
    shatter_meaning = _meaning_evidence_for_rule(rule, "frostOrbShatter")
    if shatter_eps:
        id_ev = _observed_evidence_for_rule(rule, "frostOrbShatter", source_kind)
        ev = _combined_evidence(id_ev, shatter_meaning)
        first = shatter_eps[0]
        checks.append(make_check(
            key="frost_orb_shatter", title="서리 구체 미처리 · Shatter", status="failed", evidence_level=ev,
            summary=f"{_fmt_time(first.get('sec'))} · Shatter {len(shatter_eps)}회",
            ids={"spellIDs": sorted(shatter_ids)},
        ))
        for ep in shatter_eps:
            item = {
                "kind": "frost_orb_shatter", "sec": ep.get("sec"), "label": "서리 구체 미처리 · Shatter",
                "title": "서리 구체 미처리 · Shatter", "summary": f"공대 피해 대상 {ep.get('uniqueTargets') or 0}명",
                "status": "failed", "evidenceLevel": ev, "idEvidenceLevel": id_ev, "meaningEvidenceLevel": shatter_meaning,
            }
            timeline.append(item)
            if ev == EVIDENCE_VERIFIED:
                verified_failures.append(dict(item))
    else:
        checks.append(make_check(
            key="frost_orb_shatter", title="서리 구체 미처리 · Shatter", status="unknown",
            evidence_level=EVIDENCE_CANDIDATE if shatter_id_cfg == EVIDENCE_CANDIDATE else EVIDENCE_UNAVAILABLE,
            summary="Shatter 0회 관측 · 로컬 실패 표본으로 ID가 아직 확정되지 않아 성공 판정은 보류",
            ids={"spellIDs": sorted(shatter_ids)},
        ))

    # 3) Swirling Whirlpools direct player hit.  DAMAGE and ABSORBED can be
    # duplicated for the same target/timestamp, so collapse to player episodes.
    whirl_ids = _rule_spell_ids(rule, "swirlingWhirlpools", "hitSpellIDs")
    whirl_hits = _nymrissa_unique_player_hits(rows, whirl_ids)
    whirl_meaning = _meaning_evidence_for_rule(rule, "swirlingWhirlpools")
    whirl_success = _meaning_evidence_for_rule(rule, "swirlingWhirlpools", success=True)
    if whirl_hits:
        id_ev = _observed_evidence_for_rule(rule, "swirlingWhirlpools", source_kind)
        ev = _combined_evidence(id_ev, whirl_meaning)
        first = whirl_hits[0]
        checks.append(make_check(
            key="whirlpool_hit", title="휘몰아치는 소용돌이 · 직접 피격", status="failed", evidence_level=ev,
            summary=f"{_fmt_time(first.get('sec'))} · 피격 {len(whirl_hits)}회/{len({x.get('player') for x in whirl_hits})}명",
            ids={"spellIDs": sorted(whirl_ids)},
        ))
        for hit in whirl_hits:
            item = {
                "kind": "whirlpool_hit", "sec": hit.get("sec"), "label": "휘몰아치는 소용돌이 피격",
                "title": "휘몰아치는 소용돌이 피격", "summary": str(hit.get("player") or "?"),
                "player": hit.get("player"), "status": "failed", "evidenceLevel": ev,
                "idEvidenceLevel": id_ev, "meaningEvidenceLevel": whirl_meaning,
            }
            timeline.append(item)
            if ev == EVIDENCE_VERIFIED:
                verified_failures.append(dict(item))
    elif stream_complete:
        checks.append(make_check(
            key="whirlpool_hit", title="휘몰아치는 소용돌이 · 직접 피격", status="success", evidence_level=whirl_success,
            summary="직접 피격 0회 · 완전한 raw/archive 스트림",
            ids={"spellIDs": sorted(whirl_ids)},
        ))
    else:
        checks.append(make_check(
            key="whirlpool_hit", title="휘몰아치는 소용돌이 · 직접 피격", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE,
            summary="직접 피격 신호 미관측 · 입력 범위 불완전",
        ))

    # 4) Core cycle observation.
    cycle_spec = ((rule.get("mechanics") or {}).get("raidCycle") or {})
    rain_ids = {safe_int(x) for x in (cycle_spec.get("abyssalRainSpellIDs") or []) if safe_int(x) is not None}
    chill_ids = {safe_int(x) for x in (cycle_spec.get("chillingFrostSpellIDs") or []) if safe_int(x) is not None}
    orb_ids = {safe_int(x) for x in (cycle_spec.get("frostOrbSpellIDs") or []) if safe_int(x) is not None}
    def cast_count(ids):
        return len([x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids and _is_completed_cast(x)])
    rain_count, chill_count = cast_count(rain_ids), cast_count(chill_ids)
    orb_apply_count = len([x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in orb_ids and _is_aura_apply(x)])
    cycle = make_check(
        key="raid_cycle", title="심연의 비 · 오싹한 서리 · 구체", status="unknown",
        evidence_level=EVIDENCE_VERIFIED if (rain_count or chill_count or orb_apply_count) else EVIDENCE_UNAVAILABLE,
        summary=f"심연의 비 {rain_count}회 · 오싹한 서리 {chill_count}회 · 서리 구체 오라 {orb_apply_count}회",
    )
    cycle["informational"] = True
    cycle["rawStatus"] = "observed" if (rain_count or chill_count or orb_apply_count) else "unobserved"
    cycle["displayValue"] = f"Rain {rain_count} · Frost {chill_count} · Orb {orb_apply_count}"
    checks.append(cycle)

    # 5) Mythic-only observations.  These never enter verified failure slots.
    frost_burst_ids = _rule_spell_ids(rule, "frostBurst", "spellIDs")
    frost_burst_eps = _nymrissa_spell_episodes(rows, frost_burst_ids, gap_sec=0.45) if _mythic(encounter) else []
    fb = make_check(
        key="frost_burst", title="서리 폭발 · 구체 처리 (M)", status="unknown",
        evidence_level=EVIDENCE_VERIFIED if frost_burst_eps else (EVIDENCE_UNAVAILABLE if _mythic(encounter) else EVIDENCE_UNAVAILABLE),
        summary=(f"관측 {len(frost_burst_eps)}회 · 정상 구체 처리에서도 발생 · 연쇄 실패 기준 미확정" if _mythic(encounter) else "신화 전용 관측"),
        ids={"spellIDs": sorted(frost_burst_ids)},
    )
    fb["informational"] = True
    fb["rawStatus"] = "observed" if frost_burst_eps else "unobserved"
    fb["displayValue"] = f"관측 {len(frost_burst_eps)}회" if _mythic(encounter) else "해당 없음"
    checks.append(fb)

    water_jet_ids = _rule_spell_ids(rule, "waterJet", "castSpellIDs")
    water_jet_count = cast_count(water_jet_ids) if _mythic(encounter) else 0
    wj = make_check(
        key="water_jet", title="물 분출 · 탱커 처리 (M)", status="unknown",
        evidence_level=EVIDENCE_VERIFIED if water_jet_count else EVIDENCE_UNAVAILABLE,
        summary=(f"시전 {water_jet_count}회 · Lingering Frost 제거 개수는 현재 capsule로 직접 판정하지 않음" if _mythic(encounter) else "신화 전용 관측"),
        ids={"castSpellIDs": sorted(water_jet_ids)},
    )
    wj["informational"] = True
    wj["rawStatus"] = "observed" if water_jet_count else "unobserved"
    wj["displayValue"] = f"시전 {water_jet_count}회" if _mythic(encounter) else "해당 없음"
    checks.append(wj)

    shield_ids = _rule_spell_ids(rule, "waterfogShield", "selfAuraSpellIDs") | _rule_spell_ids(rule, "waterfogShield", "allyAuraSpellIDs")
    shield_apps = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in shield_ids and _is_aura_apply(x)] if _mythic(encounter) else []
    shield_sources = {str(x.get("sourceGUID") or x.get("sourceName") or "") for x in shield_apps if x.get("sourceGUID") or x.get("sourceName")}
    ws = make_check(
        key="waterfog_shield", title="서리비늘 · 물안개 보호막 (M)", status="unknown",
        evidence_level=EVIDENCE_VERIFIED if shield_apps else EVIDENCE_UNAVAILABLE,
        summary=(f"보호막 적용 {len(shield_apps)}건 · 서리비늘 source {len(shield_sources)}개 · 등장 자체는 실패 아님" if _mythic(encounter) else "신화 전용 관측"),
        ids={"spellIDs": sorted(shield_ids)},
    )
    ws["informational"] = True
    ws["rawStatus"] = "observed" if shield_apps else "unobserved"
    ws["displayValue"] = f"관측 {len(shield_apps)}건" if _mythic(encounter) else "해당 없음"
    checks.append(ws)

    timeline.sort(key=lambda x: float(x.get("sec") or 0))
    verified_failures.sort(key=lambda x: float(x.get("sec") or 0))
    first_failure = verified_failures[0] if verified_failures else None
    result = str(encounter.get("result") or ("Kill" if encounter.get("kill") else "Wipe")).upper()
    if first_failure:
        recovered = result == "KILL"
        verdict = {
            "severity": "WARN" if recovered else "HIGH",
            "status": "failed", "evidenceLevel": EVIDENCE_VERIFIED,
            "primary": first_failure.get("label") or first_failure.get("title"),
            "why": (
                f"{_fmt_time(first_failure.get('sec'))}에 검증된 기믹 실패가 확인됐습니다. "
                + ("처치에는 성공해 회복된 실수로 남깁니다." if recovered else "와이프에서는 B31 붕괴와 동일 기믹인지 별도 연결합니다.")
            ),
            "nextAction": "다음 풀: 멀록 유출/광전사 생성과 소용돌이 직접 피격을 우선 0회로 만드세요.",
        }
    else:
        verdict = {
            "severity": "INFO", "status": "unknown", "evidenceLevel": EVIDENCE_UNAVAILABLE,
            "primary": "님리사 verified 실패 신호 미관측",
            "why": "현재 입력에서 검증된 실행 실패가 확인되지 않았습니다.",
            "nextAction": "멀록 유출, Shatter, 소용돌이 직접 피격을 계속 확인하세요.",
        }

    return {
        "schemaVersion": SCHEMA_VERSION,
        "engineVersion": ENGINE_VERSION,
        "rulesVersion": RULES_VERSION,
        "bossKey": "nymrissa",
        "sourceKind": source_kind,
        "encounter": {
            "name": encounter.get("name") or encounter.get("encounterName") or rule.get("displayName"),
            "encounterID": encounter.get("encounterID"),
            "difficultyID": encounter.get("difficultyID") or encounter.get("difficulty"),
            "tryNo": encounter.get("tryNo"),
            "result": encounter.get("result") or ("Kill" if encounter.get("kill") else "Wipe"),
            "durationSec": encounter.get("durationSec"),
        },
        "rule": {"bossKey": rule.get("bossKey"), "schemaVersion": rule.get("schemaVersion"), "idBased": True, "provenance": "actual-2026-10-04-plus-addon-guide-calibration"},
        "strategy": {"source": "encounter-rule", "values": {}},
        "checks": checks,
        "timeline": timeline,
        "observations": {
            "berserkerLeak": {"spawnCount": sum(int(x.get("spawnCount") or 0) for x in spawn_eps), "spawnEpisodes": spawn_eps, "pulsingTidesEpisodes": pulse_eps},
            "frostOrbShatter": {"episodeCount": len(shatter_eps), "episodes": shatter_eps},
            "whirlpoolHits": {"hitCount": len(whirl_hits), "hits": whirl_hits},
            "raidCycle": {"abyssalRainCount": rain_count, "chillingFrostCount": chill_count, "frostOrbAuraApplyCount": orb_apply_count},
            "frostBurst": {"episodeCount": len(frost_burst_eps), "episodes": frost_burst_eps, "mythicApplies": _mythic(encounter)},
            "waterJet": {"castCount": water_jet_count, "mythicApplies": _mythic(encounter)},
            "waterfogShield": {"applyCount": len(shield_apps), "sourceCount": len(shield_sources), "mythicApplies": _mythic(encounter)},
            "verifiedFailures": verified_failures,
            "firstVerifiedFailure": first_failure,
        },
        "verdict": verdict,
        "collapseLink": {"status": "unknown", "evidenceLevel": EVIDENCE_UNAVAILABLE, "summary": "B31 동일 기믹 직접 사망과 exact link 대기"},
        "limitations": [
            "Berserker 생성은 실제 2026-10-04 d233 Wipe/Kill 대조와 downstream Pulsing Tides로 verified 실행 실패를 판정합니다.",
            "Shatter는 실패 의미는 확정됐지만 현재 로컬 표본에서 실제 발생 ID를 보지 못해 미관측만으로 성공 판정하지 않습니다.",
            "Mythic Frost Burst, Water Jet, Waterfog Shield는 관측 전용이며 발생 자체를 실패로 보지 않습니다.",
        ],
    }



def _analyze_vashnik(events: Iterable[dict], encounter: dict, rule: dict, *, source_kind: str) -> dict:
    rows = sorted((dict(x) for x in events if x.get("sec") is not None), key=lambda x: float(x.get("sec") or 0))
    strategy, strategy_source = load_strategy("vashnik")
    judgment = (strategy.get("judgment") or {}) if isinstance(strategy, dict) else {}

    malignant_burst = _vashnik_damage_episodes(
        rows, rule, "malignantBurst", ("damageSpellIDs", "areaSpellIDs"), source_kind=source_kind,
        max_gap=float(judgment.get("raidWidePulseGapSec") or 0.35),
    )
    catalytic_bile = _vashnik_damage_episodes(
        rows, rule, "catalyticBile", ("missRaidDamageSpellIDs",), source_kind=source_kind,
        max_gap=float(judgment.get("raidWidePulseGapSec") or 0.35),
    )
    plague_wave = _vashnik_damage_episodes(
        rows, rule, "plagueWave", ("waveSpellIDs",), source_kind=source_kind,
        max_gap=float(judgment.get("raidWidePulseGapSec") or 0.35),
    )
    malignance = _vashnik_damage_episodes(
        rows, rule, "malignance", ("damageSpellIDs",), source_kind=source_kind,
        max_gap=float(judgment.get("raidWidePulseGapSec") or 0.35),
    ) if _mythic(encounter) else []

    infection_overlap_sec = float(judgment.get("explodingInfectionOverlapWindowSec") or 1.0)
    infection_warn_at = max(2, int(judgment.get("explodingInfectionWarningPulses") or 2))
    exploding_infection = _vashnik_damage_episodes(
        rows, rule, "explodingInfection", ("explosionSpellIDs",), source_kind=source_kind,
        max_gap=infection_overlap_sec,
    )
    caustic_overlap_sec = float(judgment.get("causticSurgeOverlapWindowSec") or 3.0)
    caustic_warn_at = max(2, int(judgment.get("causticSurgeWarningPulses") or 2))
    caustic_surge = _vashnik_damage_episodes(
        rows, rule, "causticSurge", ("spellIDs",), source_kind=source_kind,
        max_gap=caustic_overlap_sec,
    )

    exploding_debuff = _vashnik_observed_count(rows, rule, "explodingInfection", ("debuffSpellIDs",), source_kind=source_kind)
    stygian = _vashnik_observed_count(rows, rule, "stygianInfection", ("debuffSpellIDs", "burstSpellIDs"), source_kind=source_kind)
    siphoning = _vashnik_observed_count(rows, rule, "siphoningInfection", ("debuffSpellIDs", "burstSpellIDs"), source_kind=source_kind)
    catalyst = _vashnik_observed_count(rows, rule, "malignantCatalyst", ("castSpellIDs", "damageSpellIDs"), source_kind=source_kind)

    checks: list[dict] = []
    timeline: list[dict] = []
    verified_failures: list[dict] = []

    def add_direct_failure(key: str, mechanic_key: str, title: str, label: str, episodes: list[dict], next_text: str):
        meaning_ev = _meaning_evidence_for_rule(rule, mechanic_key)
        if episodes:
            first = episodes[0]
            id_ev = first.get("idEvidenceLevel") or _observed_evidence_for_rule(rule, mechanic_key, source_kind)
            combined_ev = _combined_evidence(id_ev, meaning_ev)
            check = make_check(
                key=key, title=title, status="failed", evidence_level=combined_ev,
                summary=f"{_fmt_time(first.get('sec'))} · 직접 피해 신호 {len(episodes)}회 · 첫 묶음 {first.get('uniqueTargets') or 0}명",
                ids={"spellIDs": first.get("spellIDs") or []},
            )
            check["idEvidenceLevel"] = id_ev
            check["meaningEvidenceLevel"] = meaning_ev
            checks.append(check)
            for ep in episodes:
                ep_id_ev = ep.get("idEvidenceLevel") or id_ev
                ep_combined = _combined_evidence(ep_id_ev, meaning_ev)
                item = {
                    "kind": key, "sec": ep.get("sec"), "title": label, "label": label,
                    "summary": f"플레이어 {ep.get('uniqueTargets') or 0}명 · pulse {ep.get('pulseCount') or 0}",
                    "status": "failed", "idEvidenceLevel": ep_id_ev,
                    "meaningEvidenceLevel": meaning_ev, "evidenceLevel": ep_combined,
                }
                timeline.append(item)
                if ep_combined == EVIDENCE_VERIFIED:
                    verified_failures.append(item)
        else:
            # B37.4.4: for the two rules whose ID *and* failure meaning are now
            # globally verified, a complete local raw/archive stream supports a
            # real zero-failure success.  Candidate mechanics still never gain
            # absence-based success.
            id_ev = str((((rule.get("mechanics") or {}).get(mechanic_key) or {}).get("idEvidenceLevel") or EVIDENCE_CANDIDATE))
            combined_ev = _combined_evidence(id_ev, meaning_ev)
            if _vashnik_absence_stream_complete(source_kind) and combined_ev == EVIDENCE_VERIFIED:
                check = make_check(
                    key=key, title=title, status="success", evidence_level=EVIDENCE_VERIFIED,
                    summary=f"직접 실패 신호 0회 · 완전한 {str(source_kind).lower()} 스트림에서 미관측",
                )
                check["idEvidenceLevel"] = id_ev
                check["meaningEvidenceLevel"] = meaning_ev
                checks.append(check)
            else:
                check = make_check(
                    key=key, title=title, status="unknown", evidence_level=EVIDENCE_UNAVAILABLE,
                    summary=f"{title} 직접 실패 신호 미관측 · 현재 입력 범위만으로 성공 판정하지 않음",
                )
                check["idEvidenceLevel"] = id_ev if id_ev == EVIDENCE_VERIFIED else EVIDENCE_UNAVAILABLE
                check["meaningEvidenceLevel"] = meaning_ev
                checks.append(check)

    add_direct_failure(
        "malignant_burst", "malignantBurst", "맹독 쫄 중앙 도달 · 악의 파열", "악의 파열",
        malignant_burst, "중앙 도달 쫄을 0회로 유지",
    )
    add_direct_failure(
        "catalytic_bile_miss", "catalyticBile", "촉매성 담즙 · 미흡수 공대피해", "촉매성 담즙 미흡수",
        catalytic_bile, "Catalytic Bile 미흡수 공대피해를 0회로 유지",
    )
    add_direct_failure(
        "plague_wave_hit", "plagueWave", "역병 파동 · 플레이어 피격", "역병 파동 피격",
        plague_wave, "Plague Wave 직접 피격을 0회로 유지",
    )
    if _mythic(encounter):
        add_direct_failure(
            "malignance", "malignance", "악성 · 토템 완료 피해", "악성(Malignance) 완료",
            malignance, "Malignant Totem의 Malignance 완료 전에 토템 처리",
        )
    else:
        checks.append(make_check(
            key="malignance", title="악성 · 토템 완료 피해", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE,
            summary="신화 전용 판정 · 현재 난이도에서는 적용하지 않음",
        ))

    def add_overlap_warning(key: str, mechanic_key: str, title: str, label: str, episodes: list[dict], warn_at: int):
        risky = [x for x in episodes if int(x.get("pulseCount") or 0) >= warn_at]
        if risky:
            first = risky[0]
            meaning_ev = first.get("meaningEvidenceLevel") or _meaning_evidence_for_rule(rule, mechanic_key)
            check = make_check(
                key=key, title=title, status="warning", evidence_level=meaning_ev,
                summary=f"{_fmt_time(first.get('sec'))} · {first.get('pulseCount')}회 겹침 후보 · 단일 발생은 실패로 보지 않음",
                ids={"spellIDs": first.get("spellIDs") or []},
            )
            check["idEvidenceLevel"] = first.get("idEvidenceLevel") or EVIDENCE_CANDIDATE
            check["meaningEvidenceLevel"] = meaning_ev
            checks.append(check)
            for ep in risky:
                timeline.append({
                    "kind": key, "sec": ep.get("sec"), "title": label, "label": label,
                    "summary": f"짧은 창에 pulse {ep.get('pulseCount')}회 · 플레이어 {ep.get('uniqueTargets') or 0}명",
                    "status": "warning", "idEvidenceLevel": ep.get("idEvidenceLevel"),
                    "meaningEvidenceLevel": meaning_ev, "evidenceLevel": meaning_ev,
                })
        elif episodes:
            id_ev = episodes[0].get("idEvidenceLevel") or EVIDENCE_CANDIDATE
            observed_pulses = sum(int(x.get('pulseCount') or 0) for x in episodes)
            check = make_check(
                key=key, title=title, status="unknown", evidence_level=id_ev,
                summary=f"총 {observed_pulses}회 관측 · {warn_at}회 이상 겹침 없음 · 단일 발생은 관측 전용",
                ids={"spellIDs": sorted({sid for ep in episodes for sid in (ep.get('spellIDs') or [])})},
            )
            check["informational"] = True
            check["rawStatus"] = "observed"
            check["displayValue"] = f"관측 {observed_pulses}회"
            checks.append(check)
        else:
            checks.append(make_check(
                key=key, title=title, status="unknown", evidence_level=EVIDENCE_UNAVAILABLE,
                summary=f"{title} 직접 피해 신호 미관측",
            ))

    add_overlap_warning(
        "exploding_infection_overlap", "explodingInfection", "폭발 전염병 · 동시 해제/폭발", "폭발 전염병 겹침",
        exploding_infection, infection_warn_at,
    )
    add_overlap_warning(
        "caustic_surge_overlap", "causticSurge", "부식성 쇄도 · 쫄 처치 겹침", "부식성 쇄도 겹침",
        caustic_surge, caustic_warn_at,
    )

    infection_summary = (
        f"폭발 전염병 적용 {exploding_debuff.get('playerApplyCount') or 0}회/{exploding_debuff.get('uniquePlayers') or 0}명 · "
        f"Stygian 신호 {stygian.get('eventCount') or 0} · Siphoning 신호 {siphoning.get('eventCount') or 0}"
    )
    infection_check = make_check(
        key="infection_observation", title="감염 디버프 · 관측", status="unknown",
        evidence_level=max(
            (x.get("idEvidenceLevel") for x in (exploding_debuff, stygian, siphoning) if x.get("idEvidenceLevel") == EVIDENCE_VERIFIED),
            default=EVIDENCE_CANDIDATE if any(int(x.get("eventCount") or 0) for x in (exploding_debuff, stygian, siphoning)) else EVIDENCE_UNAVAILABLE,
        ),
        summary=infection_summary,
    )
    if any(int(x.get("eventCount") or 0) for x in (exploding_debuff, stygian, siphoning)):
        infection_check["informational"] = True
        infection_check["rawStatus"] = "observed"
        infection_check["displayValue"] = "관측"
    checks.append(infection_check)

    catalyst_check = make_check(
        key="malignant_catalyst", title="악성 촉매 · 관측", status="unknown",
        evidence_level=catalyst.get("idEvidenceLevel") or EVIDENCE_UNAVAILABLE,
        summary=(f"시전/피해 관련 신호 {catalyst.get('eventCount') or 0}건 · 발생 자체는 정상 사이클일 수 있어 실패 판정 안 함" if catalyst.get("eventCount") else "악성 촉매 신호 미관측"),
        ids={"castSpellIDs": sorted(_rule_spell_ids(rule, "malignantCatalyst", "castSpellIDs")), "damageSpellIDs": sorted(_rule_spell_ids(rule, "malignantCatalyst", "damageSpellIDs"))},
    )
    if catalyst.get("eventCount"):
        catalyst_check["informational"] = True
        catalyst_check["rawStatus"] = "observed"
        catalyst_check["displayValue"] = "관측"
    checks.append(catalyst_check)

    timeline.sort(key=lambda x: float(x.get("sec") or 0))
    verified_failures.sort(key=lambda x: float(x.get("sec") or 0))
    first = verified_failures[0] if verified_failures else None
    candidate_incident = next((x for x in timeline if x.get("status") in {"failed", "warning"}), None)
    if first:
        verdict = {
            "severity": "HIGH", "status": "failed", "evidenceLevel": EVIDENCE_VERIFIED,
            "primary": first.get("label") or first.get("title"),
            "why": f"{_fmt_time(first.get('sec'))}에 ID/event-shape와 실패 의미가 모두 검증된 직접 실패가 확인됐습니다.",
            "nextAction": "다음 풀에서는 해당 직접 실패 신호를 0회로 만드세요.",
        }
    elif candidate_incident:
        verdict = {
            "severity": "WARN", "status": "unknown", "evidenceLevel": EVIDENCE_CANDIDATE,
            "primary": candidate_incident.get("label") or candidate_incident.get("title") or "Vashnik 실패 후보",
            "why": "직접 위험 신호는 관측됐지만 B37.4.4 증거 계약상 ID/event-shape 검증과 성공/실패 의미 검증을 분리하므로 아직 verified 실패로 승격하지 않습니다.",
            "nextAction": "candidate 기믹은 단독 실패로 승격하지 말고 다음 표본과 붕괴 문맥을 계속 대조하세요.",
        }
    else:
        verdict = {
            "severity": "INFO", "status": "unknown", "evidenceLevel": EVIDENCE_UNAVAILABLE,
            "primary": "바쉬니크 verified 실패 신호 미관측",
            "why": "현재 입력에서 verified 직접 실패 신호가 없습니다. 검증된 두 직접 실패 항목은 완전한 raw/archive에서 0회면 각 카드가 정상으로 표시됩니다.",
            "nextAction": "실제 Kill/Wipe 표본과 다음 트라이를 누적해 직접 실패 신호의 의미를 검증하세요.",
        }

    return {
        "schemaVersion": SCHEMA_VERSION,
        "engineVersion": ENGINE_VERSION,
        "rulesVersion": RULES_VERSION,
        "bossKey": "vashnik",
        "sourceKind": source_kind,
        "encounter": {
            "name": encounter.get("name") or encounter.get("encounterName") or rule.get("displayName"),
            "encounterID": encounter.get("encounterID"),
            "difficultyID": encounter.get("difficultyID") or encounter.get("difficulty"),
            "tryNo": encounter.get("tryNo"),
            "result": encounter.get("result") or ("Kill" if encounter.get("kill") else "Wipe"),
            "durationSec": encounter.get("durationSec"),
        },
        "rule": {
            "bossKey": rule.get("bossKey"), "schemaVersion": rule.get("schemaVersion"), "idBased": True,
            "provenance": "id-observation-separated-from-meaning-validation",
        },
        "strategy": {"source": strategy_source, "values": strategy},
        "checks": checks,
        "timeline": timeline,
        "observations": {
            "malignantBurst": {"episodeCount": len(malignant_burst), "episodes": malignant_burst},
            "catalyticBile": {"missEpisodeCount": len(catalytic_bile), "missEpisodes": catalytic_bile},
            "plagueWave": {"hitEpisodeCount": len(plague_wave), "hitEpisodes": plague_wave},
            "malignance": {"episodeCount": len(malignance), "episodes": malignance, "mythicApplies": _mythic(encounter)},
            "explodingInfection": {"debuff": exploding_debuff, "explosionEpisodes": exploding_infection, "overlapWarningAt": infection_warn_at},
            "stygianInfection": stygian,
            "siphoningInfection": siphoning,
            "causticSurge": {"episodes": caustic_surge, "overlapWarningAt": caustic_warn_at},
            "malignantCatalyst": catalyst,
            "firstVerifiedFailure": first,
        },
        "verdict": verdict,
        "collapseLink": {"status": "unknown", "evidenceLevel": EVIDENCE_UNAVAILABLE, "summary": "B31 붕괴 체인과 직접 인과 연결은 Vashnik 실제 표본 검증 후 승격"},
        "limitations": [
            "B37.4.4은 실제 Kill/Wipe 대조로 악의 파열과 촉매성 담즙 미흡수의 ID/event-shape와 실패 의미를 verified로 잠갔습니다.",
            "완전한 local raw/archive에서는 악의 파열·촉매성 담즙 미흡수 0회도 해당 항목의 verified success로 표시합니다. Plague Wave와 Malignance는 candidate를 유지합니다.",
            "Exploding Infection과 Caustic Surge는 단일 발생이 정상 처리 과정일 수 있어 발생 자체를 실패로 판정하지 않습니다. 짧은 시간 다중 pulse만 위험 후보로 표시합니다.",
            "absence-based success는 ID와 실패 의미가 모두 verified이고 입력 스트림이 완전한 두 직접 실패 규칙에만 제한합니다.",
        ],
    }


def _sszorak_player_label(row: dict) -> str:
    return str(row.get("targetName") or row.get("destName") or row.get("destGUID") or row.get("targetID") or "?")


def _sszorak_stream_complete(rows: list[dict], source_kind: str, coverage: str = "friendlies_debuffs") -> tuple[bool, str]:
    kind = str(source_kind or "").strip().lower()
    if kind in {"raw", "archive"}:
        return True, "local raw/archive"
    if kind == "fixture":
        return True, "synthetic fixture"
    if kind != "wcl":
        return False, "normalized/unknown stream"
    markers = [x for x in rows if _event_name(x) == "probe_stream_coverage" and str(x.get("coverage") or "") == coverage]
    if not markers:
        return False, f"coverage marker missing: {coverage}"
    if any(bool(x.get("cappedWithoutNext")) for x in markers):
        return False, f"WCL cappedWithoutNext: {coverage}"
    return True, f"WCL {coverage} complete"


def _sszorak_mutilate(rows: list[dict], rule: dict, strategy: dict, encounter: dict, *, source_kind: str) -> dict:
    spec = ((rule.get("mechanics") or {}).get("mutilate") or {})
    cast_ids = _rule_spell_ids(rule, "mutilate", "castSpellIDs")
    impact_ids = _rule_spell_ids(rule, "mutilate", "impactSpellIDs")
    gash_ids = _rule_spell_ids(rule, "mutilate", "gashSpellIDs")
    judgment = strategy.get("judgment") or {}
    raid_plan = strategy.get("raidPlan") or {}
    unsafe_at_or_below = int(raid_plan.get("mythicMutilateUnsafeAtOrBelow") or 5)
    window = float(judgment.get("mutilateImpactWindowSec") or 4.0)
    gap = float(judgment.get("mutilateClusterGapSec") or 0.45)

    completed = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in cast_ids and _is_completed_cast(x)]
    starts = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in cast_ids and _is_cast_start(x)]
    anchors = [g[0] for g in _group_times(completed if completed else starts, max_gap=0.75)]
    signals = [
        x for x in rows
        if _is_player_target(x)
        and (
            (safe_int(x.get("spellID") or x.get("abilityGameID")) in gash_ids and _is_aura_apply(x))
            or (safe_int(x.get("spellID") or x.get("abilityGameID")) in impact_ids and _is_damage(x) and _event_name(x) not in {"spell_periodic_damage", "periodic_damage"})
        )
    ]
    stream_complete, stream_reason = _sszorak_stream_complete(rows, source_kind, "friendlies_debuffs")
    id_ev = _observed_evidence_for_rule(rule, "mutilate", source_kind) if (anchors or signals) else EVIDENCE_UNAVAILABLE
    meaning_ev = _meaning_evidence_for_rule(rule, "mutilate")
    success_ev = _meaning_evidence_for_rule(rule, "mutilate", success=True)

    strikes: list[dict] = []
    used: set[int] = set()
    for anchor_row in anchors:
        sec = float(anchor_row.get("sec") or 0)
        candidates = [x for x in signals if id(x) not in used and sec - 0.15 <= float(x.get("sec") or 0) <= sec + window]
        if not candidates:
            strikes.append({
                "sec": round(sec, 3), "castSec": round(sec, 3), "targetCount": None, "targets": [],
                "status": "unknown", "idEvidenceLevel": id_ev, "meaningEvidenceLevel": EVIDENCE_UNAVAILABLE,
                "evidenceLevel": EVIDENCE_UNAVAILABLE, "streamComplete": stream_complete,
                "note": "Mutilate 뒤 직접 적중/Gash 신호 없음",
            })
            continue
        first = min(float(x.get("sec") or 0) for x in candidates)
        group = [x for x in candidates if first <= float(x.get("sec") or 0) <= first + gap]
        for x in group:
            used.add(id(x))
        targets = sorted({_sszorak_player_label(x) for x in group})
        count = len(targets)
        if not _mythic(encounter):
            status = "observed"
            ev = id_ev
        elif count <= unsafe_at_or_below and stream_complete:
            status = "failed"
            ev = meaning_ev
        elif count > unsafe_at_or_below and stream_complete:
            status = "success"
            ev = success_ev
        else:
            status = "unknown"
            ev = EVIDENCE_CANDIDATE if count else EVIDENCE_UNAVAILABLE
        strikes.append({
            "sec": round(first, 3), "castSec": round(sec, 3), "targetCount": count, "targets": targets,
            "status": status, "idEvidenceLevel": id_ev, "meaningEvidenceLevel": ev, "evidenceLevel": ev,
            "streamComplete": stream_complete,
            "note": None if stream_complete else f"불완전 스트림이라 {count}명 관측을 실패/성공으로 승격하지 않음",
        })

    # Some streams expose the Gash/impact rows but omit the boss cast. Preserve those
    # as unanchored observed strikes so the user still sees the actual soak count.
    leftovers = [x for x in signals if id(x) not in used]
    for group in _group_times(leftovers, max_gap=gap):
        targets = sorted({_sszorak_player_label(x) for x in group})
        count = len(targets)
        if not _mythic(encounter):
            status, ev = "observed", id_ev
        elif count <= unsafe_at_or_below and stream_complete:
            status, ev = "failed", meaning_ev
        elif count > unsafe_at_or_below and stream_complete:
            status, ev = "success", success_ev
        else:
            status, ev = "unknown", EVIDENCE_CANDIDATE if count else EVIDENCE_UNAVAILABLE
        strikes.append({
            "sec": round(min(float(x.get("sec") or 0) for x in group), 3), "castSec": None,
            "targetCount": count, "targets": targets, "status": status, "idEvidenceLevel": id_ev,
            "meaningEvidenceLevel": ev, "evidenceLevel": ev, "streamComplete": stream_complete,
            "note": "cast anchor 없음 · 적중/Gash 묶음 기준",
        })

    strikes.sort(key=lambda x: float(x.get("sec") or 0))
    return {
        "castCount": len(anchors), "signalEventCount": len(signals), "strikes": strikes,
        "underSoaks": [x for x in strikes if x.get("status") == "failed"],
        "adequateSoaks": [x for x in strikes if x.get("status") == "success"],
        "unknownSoaks": [x for x in strikes if x.get("status") == "unknown"],
        "unsafeAtOrBelow": unsafe_at_or_below, "safeObservedMin": unsafe_at_or_below + 1,
        "streamComplete": stream_complete, "streamCompletenessReason": stream_reason,
        "spellIDs": {"cast": sorted(cast_ids), "impact": sorted(impact_ids), "gash": sorted(gash_ids)},
        "thresholdContract": "verified-mythic-fewer-than-5; actual-kill/wipe calibrated",
    }


def _sszorak_cyst_bursts(rows: list[dict], rule: dict, strategy: dict, *, source_kind: str) -> dict:
    ids = _rule_spell_ids(rule, "viscousCyst", "burstSpellIDs")
    cfg = strategy.get("judgment") or {}
    window = float(cfg.get("cystChainWindowSec") or 1.0)
    warning_at = max(2, int(cfg.get("cystChainWarningPulses") or 2))
    death_window = float(cfg.get("cystDeathLinkWindowSec") or 3.0)
    # 1287205 also owns the 1-second periodic toxin tick. Only the initial direct
    # damage rows can represent a cyst bursting; periodic rows must never inflate pulseCount.
    hits = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids
        and _is_player_target(x)
        and _event_name(x) in {"damage", "spell_damage"}
        and not bool((x.get("raw") or {}).get("tick") if isinstance(x.get("raw"), dict) else False)
    ]
    deaths = [x for x in rows if _event_name(x) in {"death", "unit_died"} and _is_player_target(x)]
    id_ev = _observed_evidence_for_rule(rule, "viscousCyst", source_kind) if hits else EVIDENCE_UNAVAILABLE
    meaning_ev = _meaning_evidence_for_rule(rule, "viscousCyst")
    episodes = []
    for idx, group in enumerate(_group_times(hits, max_gap=window), start=1):
        per_target = Counter(_target_key(x) for x in group if _target_key(x) is not None)
        pulse_count = max(per_target.values()) if per_target else 0
        sec = min(float(x.get("sec") or 0) for x in group)
        last = max(float(x.get("sec") or 0) for x in group)
        linked_deaths = [x for x in deaths if sec <= float(x.get("sec") or 0) <= last + death_window]
        episodes.append({
            "index": idx, "sec": round(sec, 3), "lastSec": round(last, 3), "pulseCount": int(pulse_count),
            "uniqueTargets": len(per_target), "eventCount": len(group),
            "chainCandidate": bool(pulse_count >= warning_at),
            "deathLinkCount": len({_target_key(x) for x in linked_deaths if _target_key(x) is not None}),
            "deathLinks": sorted({_sszorak_player_label(x) for x in linked_deaths}),
            "idEvidenceLevel": id_ev, "meaningEvidenceLevel": meaning_ev,
            "evidenceLevel": meaning_ev if pulse_count >= warning_at else id_ev,
            "spellIDs": sorted(ids),
        })
    return {
        "eventCount": len(hits), "episodes": episodes,
        "chainEpisodes": [x for x in episodes if x.get("chainCandidate")],
        "chainWarningAt": warning_at, "chainWindowSec": window, "deathLinkWindowSec": death_window,
        "spellIDs": sorted(ids), "periodicTicksExcluded": True,
    }


def _sszorak_crosswinds(rows: list[dict], rule: dict, strategy: dict, *, source_kind: str) -> dict:
    ids = _rule_spell_ids(rule, "ragingCrosswinds", "turbulentGustSpellIDs")
    cfg = strategy.get("judgment") or {}
    long_at = float(cfg.get("crosswindLongDurationSec") or 9.5)
    max_aura = float(cfg.get("crosswindMaxAuraSec") or 10.5)
    applies = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids and _is_player_target(x) and _is_aura_apply(x)]
    removes = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids and _is_player_target(x) and _is_aura_remove(x)]
    deaths = [x for x in rows if _event_name(x) in {"death", "unit_died"} and _is_player_target(x)]
    id_ev = _observed_evidence_for_rule(rule, "ragingCrosswinds", source_kind) if applies else EVIDENCE_UNAVAILABLE
    meaning_ev = _meaning_evidence_for_rule(rule, "ragingCrosswinds")
    episodes = []
    for app in applies:
        key = _target_key(app)
        sec = float(app.get("sec") or 0)
        rem = next((x for x in removes if _target_key(x) == key and sec <= float(x.get("sec") or 0) <= sec + max_aura + 1.0), None)
        death = next((x for x in deaths if _target_key(x) == key and sec <= float(x.get("sec") or 0) <= sec + max_aura + 1.0), None)
        rem_sec = float(rem.get("sec") or 0) if rem else None
        death_sec = float(death.get("sec") or 0) if death else None
        duration = round(rem_sec - sec, 3) if rem_sec is not None else None
        died_active = death_sec is not None and (rem_sec is None or death_sec <= rem_sec + 0.05)
        # B37.4.29: death while the aura is active is not sufficient evidence.
        # It occurs in actual Mythic kills and can be caused by unrelated damage.
        # Only near-natural-expiry persistence remains a pair-miss candidate.
        long_lived = duration is not None and duration >= long_at
        candidate = bool(long_lived)
        episodes.append({
            "sec": round(sec, 3), "player": _sszorak_player_label(app), "removedSec": round(rem_sec, 3) if rem_sec is not None else None,
            "durationSec": duration, "deathSec": round(death_sec, 3) if death_sec is not None else None,
            "diedWhileActive": died_active, "longLived": long_lived, "pairMissCandidate": candidate,
            "idEvidenceLevel": id_ev, "meaningEvidenceLevel": meaning_ev,
            "evidenceLevel": meaning_ev if candidate else id_ev,
        })
    return {
        "applyCount": len(applies), "removeCount": len(removes), "episodes": episodes,
        "pairMissEpisodes": [x for x in episodes if x.get("pairMissCandidate")],
        "deathWhileActiveEpisodes": [x for x in episodes if x.get("diedWhileActive")],
        "longDurationSec": long_at, "maxAuraSec": max_aura, "spellIDs": sorted(ids),
        "failureBoundary": "candidate-only when Gust persists >= longDurationSec; death-while-active alone is context only",
    }

def _sszorak_serpents_fury(rows: list[dict], rule: dict, *, source_kind: str, encounter: dict) -> dict:
    fury_ids = _rule_spell_ids(rule, "serpentsFury", "spellIDs")
    slaughter_ids = _rule_spell_ids(rule, "serpentsFury", "toTheSlaughterSpellIDs")
    ferocity_ids = _rule_spell_ids(rule, "serpentsFury", "unboundFerocitySpellIDs")
    fury = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in fury_ids]
    slaughter = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in slaughter_ids]
    ferocity = [x for x in rows if safe_int(x.get("spellID") or x.get("abilityGameID")) in ferocity_ids]
    fury_applies = [x for x in fury if _is_aura_apply(x)]
    fury_removes = [x for x in fury if _is_aura_remove(x)]
    slaughter_groups = _group_times(slaughter, max_gap=0.8) if slaughter else []
    ferocity_groups = _group_times(ferocity, max_gap=0.8) if ferocity else []
    id_ev = _observed_evidence_for_rule(rule, "serpentsFury", source_kind) if (fury or slaughter or ferocity) else EVIDENCE_UNAVAILABLE
    meaning_ev = _meaning_evidence_for_rule(rule, "serpentsFury")
    success_ev = _meaning_evidence_for_rule(rule, "serpentsFury", success=True)
    stream_complete = str(source_kind or "").lower() in {"raw", "archive", "fixture"}
    successful_pops = []
    for rem in fury_removes:
        sec = float(rem.get("sec") or 0)
        sg = next((g for g in slaughter_groups if 0.0 <= float(g[0].get("sec") or 0) - sec <= 1.5), None)
        if sg:
            successful_pops.append({
                "furyRemovedSec": round(sec, 3),
                "slaughterSec": round(float(sg[0].get("sec") or 0), 3),
                "evidenceLevel": EVIDENCE_VERIFIED if stream_complete else success_ev,
            })
    return {
        "mythicApplies": _mythic(encounter),
        "furyEventCount": len(fury), "furyApplyCount": len(fury_applies), "furyRemoveCount": len(fury_removes),
        "toTheSlaughterEventCount": len(slaughter), "toTheSlaughterEpisodeCount": len(slaughter_groups),
        "successfulPopCount": len(successful_pops), "successfulPops": successful_pops,
        "unboundFerocityEventCount": len(ferocity), "unboundFerocityEpisodeCount": len(ferocity_groups),
        "unboundFirstSec": round(min(float(x.get("sec") or 0) for x in ferocity), 3) if ferocity else None,
        "idEvidenceLevel": id_ev, "meaningEvidenceLevel": meaning_ev, "successMeaningEvidenceLevel": success_ev,
        "streamComplete": stream_complete,
        "spellIDs": {"fury": sorted(fury_ids), "toTheSlaughter": sorted(slaughter_ids), "unboundFerocity": sorted(ferocity_ids)},
    }

def _analyze_sszorak(events: Iterable[dict], encounter: dict, rule: dict, *, source_kind: str) -> dict:
    rows = sorted((dict(x) for x in events if x.get("sec") is not None), key=lambda x: float(x.get("sec") or 0))
    strategy, strategy_source = load_strategy("sszorak")
    mutilate = _sszorak_mutilate(rows, rule, strategy, encounter, source_kind=source_kind)
    cyst = _sszorak_cyst_bursts(rows, rule, strategy, source_kind=source_kind)
    cross = _sszorak_crosswinds(rows, rule, strategy, source_kind=source_kind)
    fury = _sszorak_serpents_fury(rows, rule, source_kind=source_kind, encounter=encounter)

    checks: list[dict] = []
    timeline: list[dict] = []
    verified_failures: list[dict] = []

    under = mutilate.get("underSoaks") or []
    if under:
        first = under[0]
        ev = first.get("meaningEvidenceLevel") or EVIDENCE_CANDIDATE
        check = make_check(
            key="mutilate_under_soak", title="절단(Mutilate) 소크 인원", status="failed", evidence_level=ev,
            summary=f"{_fmt_time(first.get('sec'))} · {first.get('targetCount')}명 적중 · 신화 실패선 <5명",
            ids=mutilate.get("spellIDs") or {},
        )
        check["idEvidenceLevel"] = first.get("idEvidenceLevel") or EVIDENCE_CANDIDATE
        check["meaningEvidenceLevel"] = ev
        checks.append(check)
        for ep in under:
            item = {
                "kind":"mutilate_under_soak", "sec":ep.get("sec"), "title":"Mutilate 소크 인원 부족", "label":"Mutilate 소크 인원 부족",
                "summary":f"직접 적중 {ep.get('targetCount')}명 · 신화 실패선 <5명",
                "status":"failed", "idEvidenceLevel":ep.get("idEvidenceLevel"), "meaningEvidenceLevel":ev, "evidenceLevel":ev,
            }
            timeline.append(item)
            if ev == EVIDENCE_VERIFIED:
                verified_failures.append(dict(item))
    elif mutilate.get("adequateSoaks"):
        best = min((mutilate.get("adequateSoaks") or []), key=lambda x: int(x.get("targetCount") or 999))
        ev = best.get("meaningEvidenceLevel") or EVIDENCE_CANDIDATE
        check = make_check(
            key="mutilate_under_soak", title="절단(Mutilate) 소크 인원", status="success", evidence_level=ev,
            summary=f"최소 관측 {best.get('targetCount')}명 · 신화 기준 5명 이상",
            ids=mutilate.get("spellIDs") or {},
        )
        check["idEvidenceLevel"] = best.get("idEvidenceLevel") or EVIDENCE_CANDIDATE
        check["meaningEvidenceLevel"] = ev
        checks.append(check)
    elif not _mythic(encounter) and mutilate.get("strikes"):
        checks.append(make_check(
            key="mutilate_under_soak", title="절단(Mutilate) 소크 인원", status="unknown",
            evidence_level=(mutilate.get("strikes")[0].get("idEvidenceLevel") or EVIDENCE_CANDIDATE),
            summary="영웅/비신화에서는 신화 최소 인원 실패 판정을 적용하지 않고 적중 인원만 관측",
            ids=mutilate.get("spellIDs") or {},
        ))
    else:
        checks.append(make_check(
            key="mutilate_under_soak", title="절단(Mutilate) 소크 인원", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE,
            summary="Mutilate 직접 적중/Gash 인원 신호 미관측",
        ))

    chain = cyst.get("chainEpisodes") or []
    if chain:
        first = chain[0]
        ev = first.get("meaningEvidenceLevel") or EVIDENCE_CANDIDATE
        check = make_check(
            key="viscous_cyst_chain", title="점성 낭종(구슬) 연쇄 폭발", status="failed", evidence_level=ev,
            summary=f"{_fmt_time(first.get('sec'))} · 1초 안 {first.get('pulseCount')}개 폭발 pulse · 직후 사망 {first.get('deathLinkCount')}명",
            ids={"burstSpellIDs": cyst.get("spellIDs") or []},
        )
        check["idEvidenceLevel"] = first.get("idEvidenceLevel") or EVIDENCE_CANDIDATE
        check["meaningEvidenceLevel"] = ev
        checks.append(check)
        for ep in chain:
            item = {
                "kind":"viscous_cyst_chain", "sec":ep.get("sec"), "title":"구슬 연쇄 폭발", "label":"구슬 연쇄 폭발",
                "summary":f"폭발 pulse {ep.get('pulseCount')} · 직후 사망 {ep.get('deathLinkCount')}명",
                "status":"failed", "idEvidenceLevel":ep.get("idEvidenceLevel"), "meaningEvidenceLevel":ev, "evidenceLevel":ev,
            }
            timeline.append(item)
            if ev == EVIDENCE_VERIFIED:
                verified_failures.append(dict(item))
    elif cyst.get("episodes"):
        first = cyst.get("episodes")[0]
        checks.append(make_check(
            key="viscous_cyst_chain", title="점성 낭종(구슬) 연쇄 폭발", status="success",
            evidence_level=EVIDENCE_VERIFIED if str(source_kind).lower() in {"raw","archive","fixture"} else (first.get("idEvidenceLevel") or EVIDENCE_CANDIDATE),
            summary=f"구슬 폭발 {len(cyst.get('episodes') or [])}묶음 · 1초 안 2중 연쇄 0회 · 단일 폭발만 관측",
            ids={"burstSpellIDs": cyst.get("spellIDs") or []},
        ))
    else:
        checks.append(make_check(
            key="viscous_cyst_chain", title="점성 낭종(구슬) 연쇄 폭발", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE,
            summary="Viscous Cyst 초기 직접 폭발 신호 미관측",
        ))

    pair_miss = cross.get("pairMissEpisodes") or []
    if pair_miss:
        first = pair_miss[0]
        ev = first.get("meaningEvidenceLevel") or EVIDENCE_CANDIDATE
        check = make_check(
            key="crosswind_pair_miss", title="교차의 바람 짝 충돌", status="failed", evidence_level=ev,
            summary=f"{_fmt_time(first.get('sec'))} · {first.get('player')} · Gust {first.get('durationSec')}초 유지 · 자연 만료 근접 후보",
            ids={"turbulentGustSpellIDs": cross.get("spellIDs") or []},
        )
        check["idEvidenceLevel"] = first.get("idEvidenceLevel") or EVIDENCE_CANDIDATE
        check["meaningEvidenceLevel"] = ev
        checks.append(check)
        for ep in pair_miss:
            timeline.append({
                "kind":"crosswind_pair_miss", "sec":ep.get("sec"), "title":"Crosswinds 짝 미해결 후보", "label":"교차의 바람 짝 미해결 후보",
                "summary":f"{ep.get('player')} · Gust {ep.get('durationSec')}초 유지", "status":"failed",
                "idEvidenceLevel":ep.get("idEvidenceLevel"), "meaningEvidenceLevel":ev, "evidenceLevel":ev,
            })
    elif cross.get("applyCount"):
        deaths_active = len(cross.get("deathWhileActiveEpisodes") or [])
        checks.append(make_check(
            key="crosswind_pair_miss", title="교차의 바람 짝 충돌", status="unknown",
            evidence_level=(cross.get("episodes")[0].get("idEvidenceLevel") if cross.get("episodes") else EVIDENCE_CANDIDATE),
            summary=f"Turbulent Gusts {cross.get('applyCount')}회 · 9.5초 이상 장기 잔류 0회 · 활성 중 사망 {deaths_active}회는 문맥만 보존",
            ids={"turbulentGustSpellIDs": cross.get("spellIDs") or []},
        ))
    else:
        checks.append(make_check(
            key="crosswind_pair_miss", title="교차의 바람 짝 충돌", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE,
            summary="Turbulent Gusts 적용 신호 미관측",
        ))

    if fury.get("mythicApplies") and int(fury.get("unboundFerocityEventCount") or 0) > 0:
        ev = fury.get("meaningEvidenceLevel") or EVIDENCE_CANDIDATE
        item = {
            "kind":"serpents_fury_enrage", "sec":fury.get("unboundFirstSec"), "title":"Unbound Ferocity", "label":"뱀의 격노 100분노 광폭",
            "summary":"100 rage 도달 · Unbound Ferocity 광폭", "status":"failed", "idEvidenceLevel":fury.get("idEvidenceLevel"),
            "meaningEvidenceLevel":ev, "evidenceLevel":ev,
        }
        check = make_check(
            key="serpents_fury_enrage", title="뱀의 격노 · 100분노 광폭", status="failed", evidence_level=ev,
            summary=f"{_fmt_time(fury.get('unboundFirstSec'))} · Unbound Ferocity 관측 · 광폭 실패",
            ids=fury.get("spellIDs") or {},
        )
        check["idEvidenceLevel"] = fury.get("idEvidenceLevel") or EVIDENCE_CANDIDATE
        check["meaningEvidenceLevel"] = ev
        checks.append(check)
        timeline.append(item)
        if ev == EVIDENCE_VERIFIED:
            verified_failures.append(dict(item))
    elif fury.get("mythicApplies") and fury.get("streamComplete") and int(fury.get("successfulPopCount") or 0) > 0:
        checks.append(make_check(
            key="serpents_fury_enrage", title="뱀의 격노 · 100분노 광폭", status="success", evidence_level=EVIDENCE_VERIFIED,
            summary=f"성공 해제 {fury.get('successfulPopCount')}회 · Unbound Ferocity 0회",
            ids=fury.get("spellIDs") or {},
        ))
    elif fury.get("mythicApplies"):
        checks.append(make_check(
            key="serpents_fury_enrage", title="뱀의 격노 · 100분노 광폭", status="unknown",
            evidence_level=fury.get("idEvidenceLevel") or EVIDENCE_UNAVAILABLE,
            summary=f"Serpent's Fury 적용 {fury.get('furyApplyCount') or 0} · 성공 해제 {fury.get('successfulPopCount') or 0} · Unbound Ferocity 0 · 완전한 성공 기준은 직접 사이클 관측 필요",
            ids=fury.get("spellIDs") or {},
        ))
    else:
        checks.append(make_check(
            key="serpents_fury_enrage", title="뱀의 격노 · 100분노 광폭", status="unknown", evidence_level=EVIDENCE_UNAVAILABLE,
            summary="신화 전용 판정 · 현재 난이도에서는 적용하지 않음",
        ))

    timeline.sort(key=lambda x: float(x.get("sec") or 0))
    # De-duplicate verified failures by kind+time and preserve every real failure.
    dedup = {}
    for row in verified_failures:
        try:
            key = (str(row.get("kind") or ""), round(float(row.get("sec")), 3))
        except (TypeError, ValueError):
            continue
        dedup[key] = dict(row)
    verified_failures = sorted(dedup.values(), key=lambda x: float(x.get("sec") or 0))
    first = verified_failures[0] if verified_failures else None
    candidate_incident = next((x for x in timeline if x.get("status") == "failed" and str(x.get("evidenceLevel") or "") == EVIDENCE_CANDIDATE), None)
    if first:
        action_by_kind = {
            "mutilate_under_soak": "다음 풀: Mutilate 소크를 최소 5명 이상으로 맞추세요.",
            "viscous_cyst_chain": "다음 풀: 구슬을 정해진 순서대로 한 번씩만 터뜨려 연쇄 폭발을 0회로 만드세요.",
            "serpents_fury_enrage": "다음 풀: Serpent's Fury 대상에 정해진 인원이 제때 모여 100 rage 전에 To the Slaughter를 발동하세요.",
        }
        verdict = {
            "severity":"HIGH", "status":"failed", "evidenceLevel":EVIDENCE_VERIFIED,
            "primary":first.get("label"), "why":f"{_fmt_time(first.get('sec'))}에 의미까지 검증된 스조라크 실패 신호가 확인됐습니다.",
            "nextAction":action_by_kind.get(str(first.get("kind") or ""), "다음 풀에서 해당 기믹 실패를 0회로 만드세요."),
        }
    elif candidate_incident:
        verdict = {
            "severity":"WARN", "status":"unknown", "evidenceLevel":EVIDENCE_CANDIDATE,
            "primary":candidate_incident.get("label") or candidate_incident.get("title") or "스조라크 실패 후보",
            "why":"직접 위험 신호는 관측됐지만 현재 로그만으로 성공/실패 경계를 확정할 수 없어 candidate로 유지합니다.",
            "nextAction":"Crosswinds 장기 잔류 후보의 실제 짝 충돌 여부를 영상/추가 로그로 확인하세요.",
        }
    else:
        verdict = {
            "severity":"INFO", "status":"unknown", "evidenceLevel":EVIDENCE_UNAVAILABLE,
            "primary":"스조라크 verified 실패 신호 미관측",
            "why":"현재 입력에서 Mutilate 인원 부족·구슬 연쇄 폭발·Unbound Ferocity 같은 verified 실패가 관측되지 않았습니다.",
            "nextAction":"Mutilate 소크, 구슬 연쇄 폭발, Serpent's Fury 100 rage 실패를 계속 확인하세요.",
        }

    death_events = [
        {"sec": round(float(x.get("sec") or 0), 3), "player": _sszorak_player_label(x)}
        for x in rows
        if str(x.get("event") or "") == "UNIT_DIED" and str(x.get("targetType") or "") == "Player"
    ]

    return {
        "schemaVersion":SCHEMA_VERSION, "engineVersion":ENGINE_VERSION, "rulesVersion":RULES_VERSION, "bossKey":"sszorak", "sourceKind":source_kind,
        "encounter":{
            "name":encounter.get("name") or encounter.get("encounterName") or rule.get("displayName"),
            "encounterID":encounter.get("encounterID"), "difficultyID":encounter.get("difficultyID") or encounter.get("difficulty"),
            "tryNo":encounter.get("tryNo"), "result":encounter.get("result") or ("Kill" if encounter.get("kill") else "Wipe"),
            "durationSec":encounter.get("durationSec"),
        },
        "rule":{"bossKey":rule.get("bossKey"),"schemaVersion":rule.get("schemaVersion"),"idBased":True,"provenance":"B37.4.29-actual-mythic-kill-wipe-calibration"},
        "strategy":{"source":strategy_source,"values":strategy},
        "checks":checks, "timeline":timeline,
        "observations":{
            "mutilate":mutilate, "viscousCyst":cyst, "ragingCrosswinds":cross, "serpentsFury":fury,
            "deathEvents":death_events,
            "verifiedFailures":verified_failures, "firstVerifiedFailure":first,
        },
        "verdict":verdict,
        "collapseLink":{"status":"unknown","evidenceLevel":EVIDENCE_UNAVAILABLE,"summary":"B31 붕괴 체인과 직접 인과 연결은 같은 기믹 직접 사망 근거가 있을 때만 승격"},
        "limitations":[
            "Mutilate는 실제 적중/Gash 인원을 세며 신화 5명 미만만 verified 실패로 판정합니다.",
            "Viscous Cyst 단일 폭발은 정상 인터미션 처리일 수 있어 실패가 아닙니다. 1초 안 2개 이상 초기 pulse만 연쇄 폭발 실패로 판정합니다.",
            "구슬 위치 자체와 넉백 방향은 로그만으로 확정하지 않습니다. 연쇄 폭발과 B31 동일 기믹 직접 사망이 함께 있을 때만 붕괴 직결로 올립니다.",
            "Turbulent Gusts 활성 중 사망은 실제 Kill에서도 관측되어 짝 실패 근거에서 제외합니다. 자연 만료에 가까운 장기 잔류는 candidate입니다.",
            "Unbound Ferocity는 100 rage 광폭의 verified 실패이며, B31 직접 사망 기술과 동일하지 않으면 붕괴 직결이 아니라 시간상 선행 실패로만 표시합니다.",
        ],
    }

def _sentinel_observed_episodes(rows: list[dict], rule: dict, mechanic_key: str) -> list[dict]:
    """Group a verified spell-ID observation without assigning failure semantics.

    B37.1.1 contract: seeing an ID in real Mythic raw verifies the ID/event shape only.
    Whether that occurrence means a mechanic failure is a separate claim. 1296962 and
    1284941 both occur in the supplied Mythic kill, so occurrence alone is observation-only.
    """
    ids = _rule_spell_ids(rule, mechanic_key)
    signal = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in ids
        and (_is_damage(x) or _is_aura_apply(x) or _is_completed_cast(x))
    ]
    out = []
    for group in _group_times(signal, max_gap=0.5):
        out.append({
            "sec": round(min(float(x.get("sec") or 0) for x in group), 3),
            "uniqueTargets": len({_target_key(x) for x in group if _is_player_target(x) and _target_key(x) is not None}),
            "eventCount": len(group),
            "spellIDs": sorted(ids),
            "idEvidenceLevel": EVIDENCE_VERIFIED,
            "failureMeaningEvidenceLevel": "rejected_direct_failure",
            "failureEligible": False,
        })
    return out


def _sentinel_miasma_episodes(rows: list[dict], rule: dict, *, source_kind: str) -> list[dict]:
    """Observe Unstable Miasma share size without treating deaths alone as a mistake.

    The supplied Mythic 3445 fixtures establish the actual combat-log IDs as
    1288260 (debuff) and 1288282 (split-damage impact).  B31 already uses the
    same share baseline contract: an impact is "under" only when its participant
    count is below every earlier impact in the same pull.  BossMechanic promotes
    that observation only when one or more of the actual participants die within
    0.6s of the impact.  A death during a normal 7-9 player split stays observed,
    not failed (the supplied Mythic kill contains exactly that case).
    """
    spec = ((rule.get("mechanics") or {}).get("unstableMiasma") or {})
    damage_ids = {safe_int(x) for x in (spec.get("damageSpellIDs") or [1288282]) if safe_int(x) is not None}
    debuff_ids = {safe_int(x) for x in (spec.get("debuffSpellIDs") or [1288260]) if safe_int(x) is not None}
    event_names = {"spell_damage", "damage", "spell_absorbed", "spell_missed"}
    impacts = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in damage_ids
        and _event_name(x) in event_names
        and _is_player_target(x)
    ]
    groups = _group_times(impacts, max_gap=0.35)
    deaths = [x for x in rows if _event_name(x) in {"death", "unit_died", "unit_destroyed"}]
    aura_rows = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in debuff_ids
        and _is_aura_apply(x) and _is_player_target(x)
    ]

    episodes: list[dict] = []
    previous_counts: list[int] = []
    for idx, group in enumerate(groups, 1):
        if not group:
            continue
        sec = round(float(group[0].get("sec") or 0), 3)
        participant_keys = {_target_key(x) for x in group if _target_key(x) is not None}
        participant_names = sorted({
            str(x.get("destName") or x.get("targetName") or "?").split("-")[0]
            for x in group if _target_key(x) is not None
        })
        count = len(participant_keys)
        baseline = None
        share_status = "unknown"
        if previous_counts:
            ordered = sorted(previous_counts)
            n = len(ordered)
            median = float(ordered[n // 2]) if n % 2 else (float(ordered[n // 2 - 1]) + float(ordered[n // 2])) / 2.0
            baseline = {
                "previous": list(previous_counts),
                "range": [min(previous_counts), max(previous_counts)],
                "median": median,
            }
            share_status = (
                "under" if count < min(previous_counts)
                else "normal_low" if count == min(previous_counts) and count < median
                else "normal"
            )
        direct_deaths = []
        for death in deaths:
            dsec = death.get("sec")
            try:
                delta = float(dsec) - sec
            except (TypeError, ValueError):
                continue
            if not (0.0 <= delta <= 0.6):
                continue
            if _target_key(death) not in participant_keys:
                continue
            name = str(death.get("destName") or death.get("targetName") or "?").split("-")[0]
            if name not in direct_deaths:
                direct_deaths.append(name)

        aura = None
        for row in aura_rows:
            try:
                age = sec - float(row.get("sec") or 0)
            except (TypeError, ValueError):
                continue
            if 7.0 <= age <= 9.0:
                aura = {
                    "sec": round(float(row.get("sec") or 0), 3),
                    "player": str(row.get("destName") or row.get("targetName") or "?").split("-")[0],
                }
                break

        under_consequence = bool(share_status == "under" and direct_deaths)
        episodes.append({
            "index": idx,
            "sec": sec,
            "shareCount": count,
            "participants": participant_names,
            "eventTypes": sorted({_event_name(x) for x in group}),
            "baseline": baseline,
            "shareStatus": "under_consequence" if under_consequence else share_status,
            "directDeaths": direct_deaths,
            "directDeathCount": len(direct_deaths),
            "debuff": aura,
            "status": "failed" if under_consequence else "observed",
            "evidenceLevel": EVIDENCE_VERIFIED if str(source_kind).lower() in {"raw", "archive", "fixture"} else EVIDENCE_CANDIDATE,
            "failureEligible": False,
        })
        previous_counts.append(count)
    return episodes


def _analyze_sentinel(events: Iterable[dict], encounter: dict, rule: dict, *, source_kind: str) -> dict:
    rows = sorted((dict(x) for x in events if x.get("sec") is not None), key=lambda x: float(x.get("sec") or 0))
    episodes = _sentinel_blast_episodes(rows, rule)
    proto_eruption = _sentinel_observed_episodes(rows, rule, "protovenomEruption")
    cultivated = _sentinel_observed_episodes(rows, rule, "cultivatedBurst")
    miasma = _sentinel_miasma_episodes(rows, rule, source_kind=source_kind)
    miasma_failures = [x for x in miasma if x.get("status") == "failed" and x.get("evidenceLevel") == EVIDENCE_VERIFIED]
    worst = max(episodes, key=lambda x: (x.get("missedOrbCount") or 0, x.get("sec") or 0), default=None)
    timeline = []
    checks = []

    # 1284452 has an established failure meaning: each raid-wide repeated hit corresponds
    # to an unsoaked toxic globule. Keep these episodes eligible for first-mistake selection.
    for ep in episodes:
        n = int(ep.get("missedOrbCount") or 0)
        timeline.append({
            "kind": "missed_globules",
            "sec": ep["sec"],
            "title": f"유독한 물방울 {n}개 미섭취",
            "summary": f"맹독성 작렬 {n}회 · 공대 피격 대상 {ep['uniqueTargets']}명",
            "status": "failed",
            "failureEligible": True,
            "evidenceLevel": EVIDENCE_VERIFIED,
            "ids": {"spellIDs": ep.get("spellIDs") or []},
        })

    # B37.1.1: these are verified IDs, not verified failures. The supplied Mythic kill
    # contains both 1296962 and 1284941, proving that their occurrence alone is normal/allowed.
    for ep in proto_eruption:
        timeline.append({
            "kind": "protovenom_eruption_observed",
            "sec": ep["sec"],
            "title": "원시맹독 분출",
            "summary": f"정규 기믹 신호 관측 · 피격 {ep['uniqueTargets']}명 · 발생 자체는 실패 판정에 사용하지 않음",
            "status": "observed",
            "failureEligible": False,
            "evidenceLevel": EVIDENCE_VERIFIED,
            "failureMeaningEvidenceLevel": "rejected_direct_failure",
            "ids": {"spellIDs": ep.get("spellIDs") or []},
        })
    for ep in cultivated:
        timeline.append({
            "kind": "cultivated_burst_observed",
            "sec": ep["sec"],
            "title": "배양된 파열",
            "summary": f"정규 인터미션 피해 관측 · 피격 {ep['uniqueTargets']}명 · 발생 자체는 실패 판정에 사용하지 않음",
            "status": "observed",
            "failureEligible": False,
            "evidenceLevel": EVIDENCE_VERIFIED,
            "failureMeaningEvidenceLevel": "rejected_direct_failure",
            "ids": {"spellIDs": ep.get("spellIDs") or []},
        })

    # B37.5.0.4: Unstable Miasma is a split mechanic.  A death alone is not
    # a failure: the supplied Mythic kill has a normal 8-player split that kills
    # one player.  Only a locally under-shared impact with direct participant
    # deaths is a verified execution failure, and it is deliberately excluded
    # from firstMistake because the fixture can occur downstream of an earlier
    # collapse root.
    for ep in miasma_failures:
        baseline = ep.get("baseline") or {}
        low = (baseline.get("range") or [None])[0]
        deaths = ", ".join(ep.get("directDeaths") or [])
        timeline.append({
            "kind": "unstable_miasma_under_share",
            "sec": ep.get("sec"),
            "title": "불안정한 독기 분담 부족",
            "summary": f"분담 {ep.get('shareCount')}명 < 이전 최소 {low}명 · 직접 사망 {ep.get('directDeathCount')}명" + (f" ({deaths})" if deaths else ""),
            "status": "failed",
            "failureEligible": False,
            "evidenceLevel": EVIDENCE_VERIFIED,
            "failureRole": "downstream_verified_failure",
            "failureRoleLabel": "추가 검증 실패 · 첫 실수 제외",
            "ids": {"debuffSpellIDs": [1288260], "damageSpellIDs": [1288282]},
        })

    if miasma_failures:
        first_miasma = miasma_failures[0]
        baseline = first_miasma.get("baseline") or {}
        low = (baseline.get("range") or [None])[0]
        checks.append(make_check(
            key="unstable_miasma_share", title="불안정한 독기 분담", status="failed",
            evidence_level=EVIDENCE_VERIFIED,
            summary=f"{_fmt_time(first_miasma.get('sec'))} · 분담 {first_miasma.get('shareCount')}명 < 이전 최소 {low}명 · 직접 사망 {first_miasma.get('directDeathCount')}명",
            ids={"debuffSpellIDs": [1288260], "damageSpellIDs": [1288282]},
        ))
    elif miasma:
        normal_deaths = [x for x in miasma if x.get("directDeathCount")]
        min_share = min(int(x.get("shareCount") or 0) for x in miasma)
        summary = f"관측 {len(miasma)}회 · 최소 분담 {min_share}명 · 분담 부족+직접 사망 실패 0회"
        if normal_deaths:
            summary += f" · 정상 분담 중 사망 {sum(int(x.get('directDeathCount') or 0) for x in normal_deaths)}명은 실패로 승격하지 않음"
        checks.append(make_check(
            key="unstable_miasma_share", title="불안정한 독기 분담", status="unknown",
            evidence_level=EVIDENCE_VERIFIED if str(source_kind).lower() in {"raw", "archive", "fixture"} else EVIDENCE_CANDIDATE,
            summary=summary, ids={"debuffSpellIDs": [1288260], "damageSpellIDs": [1288282]},
        ))
    else:
        checks.append(make_check(
            key="unstable_miasma_share", title="불안정한 독기 분담", status="unknown",
            evidence_level=EVIDENCE_UNAVAILABLE, summary="불안정한 독기 실제 폭발 1288282 미관측",
            ids={"debuffSpellIDs": [1288260], "damageSpellIDs": [1288282]},
        ))

    proto_rule = (rule.get("mechanics") or {}).get("protovenomEruption") or {}
    cultivated_rule = (rule.get("mechanics") or {}).get("cultivatedBurst") or {}
    checks.append(make_check(
        key="shifting_protovenom",
        title="변화하는 원시맹독 짝맞추기",
        status="unknown",
        evidence_level=EVIDENCE_VERIFIED if proto_eruption else EVIDENCE_UNAVAILABLE,
        summary=(
            f"원시맹독 분출 {len(proto_eruption)}회 관측 · ID는 verified · 신화 킬에서도 발생하여 단독 실패 판정 금지"
            if proto_eruption else
            "원시맹독 분출 신호 미관측 · 발생 유무만으로 짝맞추기 성공/실패를 판정하지 않음"
        ),
        ids={
            "observedSpellIDs": sorted(_rule_spell_ids(rule, "protovenomEruption")),
            "idEvidenceLevel": proto_rule.get("idEvidenceLevel") or proto_rule.get("evidenceLevel"),
            "failureMeaningEvidenceLevel": proto_rule.get("failureMeaningEvidenceLevel"),
        },
    ))
    checks.append(make_check(
        key="helical_toxins",
        title="인터미션 나선 독소 해결",
        status="unknown",
        evidence_level=EVIDENCE_VERIFIED if cultivated else EVIDENCE_UNAVAILABLE,
        summary=(
            f"배양된 파열 {len(cultivated)}회 관측 · ID는 verified · 신화 킬에서도 발생하여 단독 실패 판정 금지"
            if cultivated else
            "배양된 파열 신호 미관측 · 발생 유무만으로 인터미션 성공/실패를 판정하지 않음"
        ),
        ids={
            "observedSpellIDs": sorted(_rule_spell_ids(rule, "cultivatedBurst")),
            "idEvidenceLevel": cultivated_rule.get("idEvidenceLevel") or cultivated_rule.get("evidenceLevel"),
            "failureMeaningEvidenceLevel": cultivated_rule.get("failureMeaningEvidenceLevel"),
        },
    ))

    if episodes:
        n = int(worst.get("missedOrbCount") or 0)
        checks.append(make_check(
            key="toxic_globule_soak",
            title="유독한 물방울 섭취",
            status="failed" if n > 0 else "success",
            evidence_level=EVIDENCE_VERIFIED,
            summary=f"{_fmt_time(worst.get('sec'))} · 구슬 {n}개 미섭취 → 맹독성 작렬 {n}회",
            ids={"toxicBlastSpellIDs": worst.get("spellIDs") or []},
        ))
        verdict = {
            "severity": "HIGH" if str(encounter.get("result") or "").lower() != "kill" else "WARN",
            "status": "failed",
            "evidenceLevel": EVIDENCE_VERIFIED,
            "primary": f"유독한 물방울 {n}개 미섭취",
            "why": f"{_fmt_time(worst.get('sec'))}에 맹독성 작렬이 같은 대상에게 {n}회 반복되어, 미섭취 물방울 {n}개와 일치합니다.",
            "nextAction": f"다음 풀: 해당 물방울 구간에서 놓친 {n}개를 우선 섭취하고, 작렬 연속 피격을 0회로 만드세요.",
        }
    else:
        checks.append(make_check(
            key="toxic_globule_soak", title="유독한 물방울 섭취", status="unknown",
            evidence_level=EVIDENCE_UNAVAILABLE, summary="맹독성 작렬 spellID 1284452 피해를 찾지 못했습니다."
        ))
        verdict = {
            "severity": "INFO", "status": "unknown", "evidenceLevel": EVIDENCE_UNAVAILABLE,
            "primary": "유독한 물방울 실패 신호 미관측",
            "why": "검증된 맹독성 작렬 피해가 없습니다.",
            "nextAction": "다른 파수꾼 기믹 판정 규칙을 확인하세요.",
        }

    timeline.sort(key=lambda x: float(x.get("sec") or 0))
    failure_timeline = [x for x in timeline if x.get("failureEligible") and x.get("status") == "failed" and x.get("evidenceLevel") == EVIDENCE_VERIFIED]
    verified_failures = [
        {
            "sec": item.get("sec"),
            "kind": item.get("kind"),
            "label": item.get("title"),
            "summary": item.get("summary"),
            "evidenceLevel": EVIDENCE_VERIFIED,
            "status": "failed",
        }
        for item in failure_timeline
    ]
    first_verified = dict(verified_failures[0]) if verified_failures else None
    return {
        "schemaVersion": SCHEMA_VERSION,
        "engineVersion": ENGINE_VERSION,
        "rulesVersion": RULES_VERSION,
        "bossKey": "buried_sentinel",
        "sourceKind": source_kind,
        "encounter": {
            "name": encounter.get("name") or encounter.get("encounterName") or rule.get("displayName"),
            "encounterID": encounter.get("encounterID"),
            "difficultyID": encounter.get("difficultyID") or encounter.get("difficulty"),
            "tryNo": encounter.get("tryNo"),
            "result": encounter.get("result") or ("Kill" if encounter.get("kill") else "Wipe"),
            "durationSec": encounter.get("durationSec"),
        },
        "rule": {
            "bossKey": rule.get("bossKey"),
            "schemaVersion": rule.get("schemaVersion"),
            "idBased": True,
            "evidenceContract": "id-existence-and-failure-meaning-are-separate",
        },
        "strategy": {"source": "encounter-rule", "values": {}},
        "checks": checks,
        "timeline": timeline,
        "observations": {
            "toxicBlastEpisodes": episodes,
            "episodeCount": len(episodes),
            "protovenomEruptionEpisodes": proto_eruption,
            "cultivatedBurstEpisodes": cultivated,
            "unstableMiasmaEpisodes": miasma,
            "unstableMiasmaFailures": miasma_failures,
            "verifiedFailures": verified_failures,
            "firstVerifiedFailure": first_verified,
        },
        "verdict": verdict,
        "collapseLink": {"status": "unknown", "evidenceLevel": EVIDENCE_UNAVAILABLE, "summary": "B31 붕괴 체인과 아직 연결되지 않음"},
        "limitations": [
            "유독한 물방울 위치 자체는 전투 로그만으로 판정하지 않습니다.",
            "미섭취 개수는 검증된 맹독성 작렬 spellID 1284452의 공대 반복 피격 횟수로 계산합니다.",
            "B37.1.1: 원시맹독 분출 1296962와 배양된 파열 1284941은 ID 존재는 verified지만, supplied Mythic kill에도 발생하므로 발생 자체를 실패로 판정하지 않습니다.",
            "1284941이 실제 사망/붕괴에 기여했는지는 B31 사고 체인에서 별도 판정하며, BossMechanic 첫 실수와 동일시하지 않습니다.",
            "B37.5.0.4: 불안정한 독기 실제 로그 ID는 디버프 1288260 / 분담 폭발 1288282입니다. 1288232는 도감 계열 ID로 실제 판정에 사용하지 않습니다.",
            "불안정한 독기는 이전 같은 풀의 분담 인원 최소치보다 적게 맞고 실제 참여자가 0.6초 내 직접 사망할 때만 verified 분담 실패로 표시합니다. 정상 7~9명 분담 중 사망은 실패로 승격하지 않습니다.",
            "불안정한 독기 분담 실패는 실제 표본에서 앞선 붕괴 원인 뒤에 발생할 수 있어 firstMistake 후보에서는 제외하고 추가 검증 실패로만 표시합니다.",
        ],
    }



def _ulatek_fmt_sec(value) -> str:
    try:
        sec = max(0.0, float(value))
    except (TypeError, ValueError):
        return "?"
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


def _ulatek_nearest_hp(samples: list[dict], sec: float, *, max_delta: float = 2.5) -> float | None:
    valid = []
    for row in samples or []:
        try:
            rsec = float(row.get("sec"))
            pct = float(row.get("hpPct"))
            max_hp = float(row.get("maxHP") or 0)
        except (TypeError, ValueError):
            continue
        # hp_1s may contain actor-snapshot contamination. Ula'tek's real raid HP
        # is orders of magnitude above player HP, so retain only boss-scale rows.
        if str(row.get("name") or "") != "울라텍" or max_hp < 10_000_000:
            continue
        valid.append((abs(rsec - sec), rsec, pct))
    if not valid:
        return None
    delta, _rsec, pct = min(valid, key=lambda x: x[0])
    return round(pct, 3) if delta <= max_delta else None


def _ulatek_event_hp(rows: list[dict], sec: float, *, before: float = 1.5, after: float = 1.5) -> float | None:
    candidates = []
    for row in rows:
        rsec = row.get("sec")
        if rsec is None:
            continue
        try:
            rsec = float(rsec)
        except (TypeError, ValueError):
            continue
        if not (sec - before <= rsec <= sec + after):
            continue
        # Prefer the shared-health boss/heart snapshots themselves, never player HP.
        if str(row.get("sourceName") or "") in {"울라텍", "맹독의 심장"}:
            pct = row.get("sourceHealthPct")
            max_hp = row.get("sourceMaxHP")
            try:
                if pct is not None and float(max_hp or 0) >= 10_000_000:
                    candidates.append((abs(rsec - sec), float(pct)))
            except (TypeError, ValueError):
                pass
        if str(row.get("destName") or "") in {"울라텍", "맹독의 심장"}:
            pct = row.get("targetHealthPct")
            max_hp = row.get("targetMaxHP")
            try:
                if pct is not None and float(max_hp or 0) >= 10_000_000:
                    candidates.append((abs(rsec - sec), float(pct)))
            except (TypeError, ValueError):
                pass
    if not candidates:
        return None
    return round(min(candidates, key=lambda x: x[0])[1], 3)


def _ulatek_damage_to_heart(rows: list[dict], start_sec: float, end_sec: float) -> float | None:
    total = 0.0
    seen = False
    for row in rows:
        sec = row.get("sec")
        try:
            sec = float(sec)
        except (TypeError, ValueError):
            continue
        if sec < start_sec or sec > end_sec:
            continue
        if str(row.get("destName") or "") != "맹독의 심장":
            continue
        amount = row.get("amount")
        try:
            total += float(amount)
            seen = True
        except (TypeError, ValueError):
            continue
    return round(total, 1) if seen else None


def _ulatek_supplemental_heart_damage(samples: list[dict], start_sec: float, end_sec: float) -> float | None:
    total = 0.0
    seen = False
    for row in samples or []:
        try:
            sec = float(row.get("sec"))
            damage = float(row.get("damage") or 0)
        except (TypeError, ValueError):
            continue
        if start_sec <= sec <= end_sec and str(row.get("targetName") or "") == "맹독의 심장":
            total += damage
            seen = True
    return round(total, 1) if seen else None


def _ulatek_event_kind(row: dict) -> str:
    """Normalize raw CombatLog and WCL normalized event names for Ula'tek."""
    value = str((row or {}).get("event") or "").strip().lower()
    mapping = {
        "spell_aura_applied": "applybuff",
        "spell_aura_removed": "removebuff",
        "spell_cast_success": "cast",
        "spell_cast_start": "begincast",
    }
    return mapping.get(value, value)


def _analyze_ulatek(events: Iterable[dict], encounter: dict, rule: dict, *, source_kind: str, supplemental: dict | None = None) -> dict:
    rows = sorted([dict(x) for x in events if isinstance(x, dict)], key=lambda x: float(x.get("sec") or 0))
    supplemental = supplemental or {}
    mechanics = rule.get("mechanics") or {}
    spec = mechanics.get("venomousHeart") or {}
    aura_ids = {safe_int(x) for x in (spec.get("auraSpellIDs") or [1299526]) if safe_int(x) is not None}
    heart_npcs = {safe_int(x) for x in (spec.get("heartNpcIDs") or [267460]) if safe_int(x) is not None}
    window_fallback = float(spec.get("windowDurationSec") or 20.0)

    # B37.4.33: WCL normalized reference streams use applybuff/removebuff/cast,
    # while local raw normalization may expose the SPELL_* names.  Accept both
    # representations.  One heart cast also emits applybuff, so applybuff is the
    # canonical window-start signal and cast is only a fallback.
    starts = []
    removals = []
    cast_fallbacks = []
    for row in rows:
        sid = safe_int(row.get("spellID"))
        if sid not in aura_ids:
            continue
        src_id = safe_int(row.get("sourceGameID")); dst_id = safe_int(row.get("targetGameID"))
        heart_actor = (
            src_id in heart_npcs or dst_id in heart_npcs
            or str(row.get("sourceName") or "").lower() in {"맹독의 심장", "venomous heart"}
            or str(row.get("targetName") or row.get("destName") or "").lower() in {"맹독의 심장", "venomous heart"}
        )
        if not heart_actor or row.get("sec") is None:
            continue
        event = _ulatek_event_kind(row)
        sec = float(row.get("sec"))
        if event == "applybuff":
            if not starts or abs(sec - starts[-1]["sec"]) > 0.25:
                starts.append({"sec": sec, "guid": row.get("sourceGUID") or row.get("destGUID")})
        elif event == "removebuff":
            removals.append({"sec": sec, "guid": row.get("sourceGUID") or row.get("destGUID")})
        elif event == "cast":
            cast_fallbacks.append({"sec": sec, "guid": row.get("sourceGUID") or row.get("destGUID")})
    if not starts and cast_fallbacks:
        starts = cast_fallbacks

    duration = float(encounter.get("durationSec") or (max([float(x.get("sec") or 0) for x in rows] + [0.0])))
    hp_samples = list(supplemental.get("hpSamples") or [])
    dps_samples = list(supplemental.get("raidDpsSamples") or [])
    windows = []
    for idx, st in enumerate(starts, 1):
        start_sec = float(st["sec"])
        rem = next((x for x in removals if x["sec"] > start_sec and (not st.get("guid") or x.get("guid") == st.get("guid"))), None)
        end_sec = min(float(rem["sec"]) if rem else start_sec + window_fallback, duration)
        start_hp = _ulatek_nearest_hp(hp_samples, start_sec) if hp_samples else None
        end_hp = _ulatek_nearest_hp(hp_samples, end_sec) if hp_samples else None
        if start_hp is None:
            start_hp = _ulatek_event_hp(rows, start_sec)
        if end_hp is None:
            end_hp = _ulatek_event_hp(rows, end_sec, before=2.0, after=2.0)
        drop = round(start_hp - end_hp, 3) if start_hp is not None and end_hp is not None else None
        heart_damage = _ulatek_supplemental_heart_damage(dps_samples, start_sec, end_sec) if dps_samples else None
        if heart_damage is None:
            heart_damage = _ulatek_damage_to_heart(rows, start_sec, end_sec)
        heart_dps = round(heart_damage / max(end_sec - start_sec, 0.001), 1) if heart_damage is not None else None
        windows.append({
            "index": idx,
            "startSec": round(start_sec, 3),
            "endSec": round(end_sec, 3),
            "durationSec": round(end_sec - start_sec, 3),
            "startBossHpPct": start_hp,
            "endBossHpPct": end_hp,
            "bossHpDropPct": drop,
            "heartDamage": heart_damage,
            "heartDps": heart_dps,
            "evidenceLevel": EVIDENCE_VERIFIED,
            "status": "observed",
        })

    diff = safe_int(encounter.get("difficultyID"))
    result = str(encounter.get("result") or ("Kill" if encounter.get("success") else "Wipe")).upper()
    cal = (rule.get("localCalibration") or {}).get("heroic") or {}
    mythic_cal = (rule.get("localCalibration") or {}).get("mythicPublic") or {}
    projection = {
        "status": "unavailable", "evidenceLevel": EVIDENCE_UNAVAILABLE,
        "label": "표본 부족", "summary": "현재 난이도/심장 종료 HP에 맞는 로컬 킬 표본이 부족합니다.",
    }

    # An observed kill is not a projection: its actual heart count is verified
    # at every supported difficulty.  Mythic public reference kills both reached
    # the third heart, so expose that actual observation without inventing an HP
    # threshold from missing public HP samples.
    if result == "KILL" and windows:
        observed_count = len(windows)
        projection = {
            "status": "observed", "evidenceLevel": EVIDENCE_VERIFIED,
            "label": f"{observed_count}심장 킬",
            "summary": f"실제 처치에서 심장 노출 {observed_count}회 관측",
            "observedHeartCount": observed_count,
        }
        if diff == 16 and mythic_cal:
            projection["calibration"] = dict(mythic_cal)
            projection["summary"] += f" · 공개 신화 킬 {mythic_cal.get('killSampleN', 0)}표본 참고"
    elif diff == 15 and len(windows) >= 2 and windows[1].get("endBossHpPct") is not None and cal:
        post2 = float(windows[1]["endBossHpPct"])
        two_max = float(cal.get("twoHeartPostHeart2HpPctMax"))
        three_min = float(cal.get("threeHeartPostHeart2HpPctMin"))
        if post2 <= two_max:
            projection = {
                "status": "projected", "evidenceLevel": EVIDENCE_CANDIDATE, "label": "2심장 킬권 후보",
                "summary": f"2심장 종료 {post2:.1f}% · 최근 2심장 킬 표본 상한 {two_max:.1f}% 이하",
            }
        elif post2 >= three_min:
            projection = {
                "status": "projected", "evidenceLevel": EVIDENCE_CANDIDATE, "label": "3심장 진입권 후보",
                "summary": f"2심장 종료 {post2:.1f}% · 최근 3심장 킬 표본 {three_min:.1f}% 이상",
            }
        else:
            projection = {
                "status": "borderline", "evidenceLevel": EVIDENCE_CANDIDATE, "label": "2~3심장 경계",
                "summary": f"2심장 종료 {post2:.1f}% · 로컬 표본 경계 {two_max:.1f}~{three_min:.1f}%",
            }
        projection["calibration"] = dict(cal)
    elif diff == 16 and windows and mythic_cal:
        expected = safe_int(mythic_cal.get("observedKillHeartCount"))
        if expected:
            projection = {
                "status": "progress", "evidenceLevel": EVIDENCE_VERIFIED,
                "label": f"{len(windows)}심장 도달",
                "summary": f"현재 {len(windows)}심장 관측 · 공개 신화 킬 {mythic_cal.get('killSampleN', 0)}표본은 모두 {expected}심장 도달 후 처치",
                "calibration": dict(mythic_cal),
            }

    # B37.4.33 Mythic public-log execution locks.
    # - Unchecked Rage 1301007 completed in 2 sampled wipes and 0/2 kills.
    # - Anguished Cry 1305650 completed 3 times in the sampled 09/22 wipe and
    #   0 times in both kills; begin-casts alone are normal and interruptible.
    unchecked_ids = {safe_int(x) for x in ((mechanics.get("uncheckedRage") or {}).get("castSpellIDs") or [1301007]) if safe_int(x) is not None}
    cry_ids = {safe_int(x) for x in ((mechanics.get("anguishedCry") or {}).get("castSpellIDs") or [1305650]) if safe_int(x) is not None}
    corpse_ids = {safe_int(x) for x in ((mechanics.get("calcifiedCorpse") or {}).get("debuffSpellIDs") or [1306119]) if safe_int(x) is not None}

    unchecked = []
    cry_starts = []
    cry_completes = []
    corpse = []

    # B37.5.0.5: Calcified Corpse 1306119 is the verified *result* of an
    # unsoaked Serpent's Bite.  It may still be downstream of an earlier death:
    # the public Mythic samples contain exactly that shape.  Preserve the
    # mechanic failure, but only let it compete for firstMistake when a full
    # local raw/archive stream shows the target was alive at application.
    death_by_target = {}
    for row in rows:
        if row.get("sec") is None:
            continue
        if _event_name(row) not in {"death", "unit_died", "unit_destroyed", "spell_instakill"}:
            continue
        if not _is_player_target(row):
            continue
        key = _player_identity(row, side="target")
        if key is None:
            continue
        sec = round(float(row.get("sec")), 3)
        prior = death_by_target.get(key)
        if prior is None or sec < prior:
            death_by_target[key] = sec

    local_complete_stream = str(source_kind or "").lower() in {"raw", "archive"}
    for row in rows:
        sid = safe_int(row.get("spellID"))
        if row.get("sec") is None:
            continue
        sec = round(float(row.get("sec")), 3)
        event = _ulatek_event_kind(row)
        if sid in unchecked_ids and event == "cast":
            unchecked.append({
                "kind": "unchecked_rage", "sec": sec, "label": "막을 수 없는 분노",
                "title": "막을 수 없는 분노", "status": "failed", "evidenceLevel": EVIDENCE_VERIFIED,
                "meaningEvidenceLevel": EVIDENCE_VERIFIED,
                "summary": "울라텍의 막을 수 없는 분노 시전 완료 · 보스 근접 탱킹 공백 신호",
                "spellIDs": sorted(unchecked_ids),
            })
        if sid in cry_ids:
            if event == "begincast":
                cry_starts.append(sec)
            elif event == "cast":
                cry_completes.append(sec)
        if sid in corpse_ids and event in {"applydebuff", "applybuff"} and str(row.get("targetType") or "") == "Player":
            tkey = _player_identity(row, side="target")
            death_sec = death_by_target.get(tkey) if tkey is not None else None
            dead_before = death_sec is not None and float(death_sec) <= float(sec)
            eligible = bool(local_complete_stream and not dead_before)
            corpse.append({
                "sec": sec,
                "target": row.get("targetName") or row.get("destName"),
                "targetDeathSec": death_sec,
                "targetDeadBeforeApply": dead_before,
                "failureEligible": eligible,
                "evidenceLevel": EVIDENCE_VERIFIED,
            })

    # Some public reports duplicate begin-casts from multiple source instances;
    # only successful cast completions are verified failures.
    cry_completes_unique = []
    for sec in sorted(cry_completes):
        if not cry_completes_unique or abs(sec - cry_completes_unique[-1]) > 0.12:
            cry_completes_unique.append(sec)
    cry_failures = [{
        "kind": "anguished_cry", "sec": sec, "label": "고통스러운 절규 미차단",
        "title": "고통스러운 절규 미차단", "status": "failed", "evidenceLevel": EVIDENCE_VERIFIED,
        "meaningEvidenceLevel": EVIDENCE_VERIFIED,
        "summary": "약화된 파멸비늘의 고통스러운 절규 시전 완료 · 차단 누락",
        "spellIDs": sorted(cry_ids),
    } for sec in cry_completes_unique]

    corpse_failures = []
    for item in corpse:
        downstream = not bool(item.get("failureEligible"))
        target = item.get("target") or "대상"
        summary = f"{target} · 뱀의 물어뜯기 미흡수 → 석회화한 시체"
        if item.get("targetDeadBeforeApply"):
            summary += " · 대상 선행 사망 후 적용"
        elif not local_complete_stream:
            summary += " · 공개/축약 표본이라 첫 실수 판정 제외"
        corpse_failures.append({
            "kind": "calcified_corpse", "sec": item.get("sec"),
            "label": "뱀의 물어뜯기 미흡수 → 석회화한 시체",
            "title": "뱀의 물어뜯기 미흡수 → 석회화한 시체",
            "status": "failed", "evidenceLevel": EVIDENCE_VERIFIED,
            "meaningEvidenceLevel": EVIDENCE_VERIFIED,
            "summary": summary,
            "target": item.get("target"),
            "targetDeathSec": item.get("targetDeathSec"),
            "targetDeadBeforeApply": item.get("targetDeadBeforeApply"),
            "failureEligible": bool(item.get("failureEligible")),
            "failureRole": "downstream_verified_failure" if downstream else None,
            "failureRoleLabel": "추가 검증 실패 · 첫 실수 제외" if downstream else None,
            "spellIDs": sorted(corpse_ids),
        })

    verified_failures = sorted(unchecked + cry_failures + corpse_failures, key=lambda x: float(x.get("sec") or 0))
    first_candidates = [x for x in verified_failures if x.get("failureEligible", True)]
    first_failure = dict(first_candidates[0]) if first_candidates else None

    checks = []
    for idx in (1, 2, 3):
        win = next((x for x in windows if x.get("index") == idx), None)
        if win:
            hp_text = "관측"
            if win.get("startBossHpPct") is not None and win.get("endBossHpPct") is not None:
                hp_text = f"{win['startBossHpPct']:.1f}% → {win['endBossHpPct']:.1f}%"
            dmg_text = ""
            if win.get("heartDamage") is not None:
                dmg_text = f" · 심장 직접딜 {win['heartDamage']/1_000_000:.1f}M"
            hp_drop = f" · HP 감소 {win.get('bossHpDropPct')}%p" if win.get("bossHpDropPct") is not None else ""
            checks.append({
                "key": f"heart_{idx}", "title": f"{idx}심장 진도", "status": "observed",
                "evidenceLevel": EVIDENCE_VERIFIED, "informational": True, "displayValue": hp_text,
                "summary": f"{_ulatek_fmt_sec(win.get('startSec'))}~{_ulatek_fmt_sec(win.get('endSec'))}{hp_drop}{dmg_text}",
                "details": [win],
            })
        elif idx <= 2:
            checks.append({
                "key": f"heart_{idx}", "title": f"{idx}심장 진도", "status": "unknown",
                "evidenceLevel": EVIDENCE_UNAVAILABLE, "informational": True, "displayValue": "미도달/미관측",
                "summary": "맹독의 심장 1299526 노출 신호 미관측",
            })
    checks.append({
        "key": "heart_projection", "title": "심장 진행", "status": "observed" if windows else "unknown",
        "evidenceLevel": projection.get("evidenceLevel") or EVIDENCE_UNAVAILABLE,
        "informational": True, "displayValue": projection.get("label") or "표본 부족",
        "summary": projection.get("summary") or "",
    })

    checks.append({
        "key": "unchecked_rage", "title": "막을 수 없는 분노", "status": "failed" if unchecked else "passed",
        "evidenceLevel": EVIDENCE_VERIFIED,
        "displayValue": f"{len(unchecked)}회" if unchecked else "없음",
        "summary": "시전 완료는 보스 근접 탱킹 공백의 verified 실패" if unchecked else "1301007 시전 완료 없음",
        "details": unchecked,
    })
    if cry_starts or cry_completes_unique:
        checks.append({
            "key": "anguished_cry", "title": "고통스러운 절규 차단", "status": "failed" if cry_completes_unique else "passed",
            "evidenceLevel": EVIDENCE_VERIFIED,
            "displayValue": f"실패 {len(cry_completes_unique)}회" if cry_completes_unique else "완료 0회",
            "summary": f"시전 시작 {len(cry_starts)}건 · 완료 {len(cry_completes_unique)}건",
            "details": cry_failures,
        })
    else:
        checks.append({
            "key": "anguished_cry", "title": "고통스러운 절규 차단", "status": "unknown",
            "evidenceLevel": EVIDENCE_UNAVAILABLE, "displayValue": "미도달/미관측",
            "summary": "약화된 파멸비늘의 1305650 시전 구간 미관측",
        })
    if corpse:
        eligible_n = sum(1 for x in corpse if x.get("failureEligible"))
        downstream_n = len(corpse) - eligible_n
        bits = [f"미흡수 결과 {len(corpse)}명"]
        if eligible_n:
            bits.append(f"첫 실수 판정 가능 {eligible_n}명")
        if downstream_n:
            bits.append(f"후속/축약 표본 {downstream_n}명은 첫 실수 제외")
        corpse_summary = " · ".join(bits)
    else:
        corpse_summary = "1306119 미관측"
    checks.append({
        "key": "calcified_corpse", "title": "뱀의 물어뜯기 미흡수", "status": "failed" if corpse else "unknown",
        "evidenceLevel": EVIDENCE_VERIFIED if corpse else EVIDENCE_UNAVAILABLE,
        "displayValue": f"석회화 {len(corpse)}명" if corpse else "미관측",
        "summary": corpse_summary,
        "details": corpse_failures,
        "ids": {"debuffSpellIDs": sorted(corpse_ids)},
    })

    timeline = []
    for win in windows:
        timeline.append({"kind": "heart_start", "sec": win["startSec"], "title": f"{win['index']}심장 시작", "status": "observed", "evidenceLevel": EVIDENCE_VERIFIED, "spellIDs": [1299526]})
        timeline.append({"kind": "heart_end", "sec": win["endSec"], "title": f"{win['index']}심장 종료", "status": "observed", "evidenceLevel": EVIDENCE_VERIFIED, "spellIDs": [1299526]})
    # B37.5 TimelineGlyph: a normal Anguished Cry begin-cast is still a real
    # boss event even when it is interrupted successfully.  Show the cast as an
    # observed spell icon; only a completed cast remains the verified failure.
    for sec in sorted(set(round(float(x), 3) for x in cry_starts)):
        timeline.append({
            "kind": "anguished_cry", "sec": sec, "title": "고통스러운 절규 시전",
            "label": "고통스러운 절규", "status": "observed",
            "evidenceLevel": EVIDENCE_VERIFIED, "meaningEvidenceLevel": EVIDENCE_VERIFIED,
            "summary": "차단 가능한 시전 시작 · 완료 시에만 실패 판정",
            "spellIDs": sorted(cry_ids),
        })
    timeline.extend(dict(x) for x in verified_failures)
    timeline.sort(key=lambda x: float(x.get("sec") or 0))

    if first_failure:
        primary = first_failure.get("label") or "검증 실패"
        counts = []
        if unchecked: counts.append(f"막을 수 없는 분노 {len(unchecked)}회")
        if cry_completes_unique: counts.append(f"고통스러운 절규 미차단 {len(cry_completes_unique)}회")
        if corpse: counts.append(f"석회화한 시체 {len(corpse)}명")
        verdict = {
            "status": "failed", "severity": "HIGH", "evidenceLevel": EVIDENCE_VERIFIED,
            "primary": primary,
            "why": f"{_ulatek_fmt_sec(first_failure.get('sec'))} 첫 verified 실패 · " + " · ".join(counts),
            "nextAction": "막을 수 없는 분노는 탱킹 공백을, 고통스러운 절규 완료는 차단 누락을, 석회화한 시체는 뱀의 물어뜯기 미흡수를 우선 0회로 만드세요.",
        }
    elif corpse:
        verdict = {
            "status": "observed", "severity": "INFO", "evidenceLevel": EVIDENCE_VERIFIED,
            "primary": f"석회화한 시체 {len(corpse)}명 · 후속 실패 관측",
            "why": "뱀의 물어뜯기 미흡수 결과는 verified지만, 이 표본에서는 선행 사망/축약 로그 때문에 첫 실수로 승격하지 않습니다.",
            "nextAction": "해당 대상의 뱀의 물어뜯기 만료 전 흡수 여부와 선행 사망 원인을 함께 확인하세요.",
        }
    elif result == "KILL":
        verdict = {
            "status": "passed", "severity": "INFO", "evidenceLevel": EVIDENCE_VERIFIED,
            "primary": "잠금된 verified 실패 신호 없음",
            "why": "막을 수 없는 분노 0회 · 고통스러운 절규 완료 0회",
            "nextAction": "킬 기준 패턴을 유지하고 심장 진도와 후보 관측은 정보성으로만 비교하세요.",
        }
    elif windows:
        verdict = {
            "status": "observed", "severity": "INFO", "evidenceLevel": EVIDENCE_VERIFIED,
            "primary": f"심장 {len(windows)}회 관측 · verified 실행 실패 미관측",
            "why": "현재 잠금된 두 실패 신호(막을 수 없는 분노/고통스러운 절규 완료)는 없음",
            "nextAction": "B31 붕괴 근거와 후보 기믹을 함께 확인하세요.",
        }
    else:
        verdict = {"status": "unknown", "severity": "INFO", "evidenceLevel": EVIDENCE_UNAVAILABLE, "primary": "심장 노출 미관측", "why": "1299526 신호 없음", "nextAction": "심장까지 도달한 트라이에서 진도를 확인하세요."}

    limitations = [
        "심장 횟수는 1299526 Venomous Heart/맹독의 심장 노출 오라로 계산합니다.",
        "심장 종료 HP는 저장된 1초 HP 표본을 우선 사용하며 raw 분석에서는 전투 로그 HP 스냅샷을 보조 사용합니다.",
        "영웅 2/3심장 예상은 사용자 저장 영웅 최근 킬 표본 기반 로컬 보정이며 보편 임계값이 아닙니다.",
        "신화 공개 표본의 두 Kill은 모두 3심장 노출을 관측했으나, 이를 모든 공대의 성능 임계값으로 사용하지 않습니다.",
        "1306119 석회화한 시체는 뱀의 물어뜯기 미흡수 결과로 verified입니다. 다만 대상 선행 사망 뒤 적용되거나 공개/축약 표본처럼 사망 순서를 완전하게 확인할 수 없으면 firstMistake에서는 제외합니다.",
        "부식의 파도/산성 배출/불안정한 정화 등 사망 기반 규칙은 B31 직접 사망 근거와 별도로 유지하며 발생 자체를 실패로 승격하지 않습니다.",
    ]
    return {
        "schemaVersion": SCHEMA_VERSION, "engineVersion": ENGINE_VERSION, "rulesVersion": RULES_VERSION,
        "bossKey": "ulatek", "sourceKind": source_kind,
        "encounter": {"encounterID": encounter.get("encounterID"), "name": encounter.get("name"), "difficultyID": diff, "difficultyName": encounter.get("difficultyName"), "tryNo": encounter.get("tryNo"), "result": encounter.get("result"), "durationSec": encounter.get("durationSec")},
        "rule": {"bossKey": rule.get("bossKey"), "schemaVersion": rule.get("schemaVersion"), "idBased": True, "calibrationScope": "heroic-local + mythic-public-reference"},
        "strategy": {"source": "encounter-rule", "values": {}},
        "checks": checks,
        "timeline": timeline,
        "observations": {
            "heartWindows": windows, "heartCount": len(windows), "heartProjection": projection,
            "verifiedFailures": verified_failures,
            "firstVerifiedFailure": first_failure,
            "uncheckedRage": {"count": len(unchecked), "events": unchecked},
            "anguishedCry": {"startCount": len(cry_starts), "startSecs": sorted(set(round(float(x), 3) for x in cry_starts)), "completedCount": len(cry_completes_unique), "completedSecs": cry_completes_unique},
            "calcifiedCorpse": {"count": len(corpse), "events": corpse, "failures": corpse_failures, "meaningEvidenceLevel": EVIDENCE_VERIFIED},
        },
        "verdict": verdict,
        "collapseLink": {"status": "unknown", "evidenceLevel": EVIDENCE_UNAVAILABLE, "summary": "울라텍 verified 실행 실패와 B31 붕괴의 직접 인과는 동일 기믹 직접 사망 근거가 있을 때만 별도 연결"},
        "limitations": limitations,
    }

def _append_bloodlust_observation(review: dict | None, events: Iterable[dict], *, source_kind: str) -> dict | None:
    """Attach one neutral, raid-wide bloodlust timing observation to every supported boss.

    This is intentionally not a success/failure judgment.  Raid-leader timing plans
    can be layered on later without turning an observed late/early cast into a wipe
    cause automatically.
    """
    if not review:
        return review
    rows = [dict(x) for x in events if isinstance(x, dict) and x.get("sec") is not None]
    casts = [
        x for x in rows
        if safe_int(x.get("spellID") or x.get("abilityGameID")) in _BLOODLUST_FAMILY_IDS
        and _is_completed_cast(x)
    ]
    evidence_rows = casts
    source = "cast"
    if not evidence_rows:
        evidence_rows = [
            x for x in rows
            if safe_int(x.get("spellID") or x.get("abilityGameID")) in _BLOODLUST_FAMILY_IDS
            and _is_aura_apply(x)
        ]
        source = "aura"
    evidence_rows.sort(key=lambda x: float(x.get("sec") or 0))
    first = evidence_rows[0] if evidence_rows else None

    checks = [dict(x) if isinstance(x, dict) else x for x in (review.get("checks") or [])]
    # Re-analysis must stay idempotent.
    checks = [x for x in checks if not (isinstance(x, dict) and str(x.get("key") or "") == "bloodlust_timing")]
    if first:
        sec = round(float(first.get("sec") or 0), 3)
        spell_name = first.get("spellName") or "블러드"
        player = first.get("sourceName")
        summary = f"첫 사용 {_fmt_time(sec)} · {spell_name}" + (f" · {player}" if player else "")
        check = make_check(
            key="bloodlust_timing", title="블러드 타이밍", status="unknown",
            evidence_level=EVIDENCE_VERIFIED, summary=summary,
            ids={"spellIDs": sorted(_BLOODLUST_FAMILY_IDS)},
        )
        check.update({
            "informational": True,
            "rawStatus": "observed",
            "displayValue": _fmt_time(sec),
            "observationOnly": True,
        })
    else:
        check = make_check(
            key="bloodlust_timing", title="블러드 타이밍", status="unknown",
            evidence_level=EVIDENCE_UNAVAILABLE, summary="현재 입력에서 블러드 계열 사용 신호 미관측",
            ids={"spellIDs": sorted(_BLOODLUST_FAMILY_IDS)},
        )
        check.update({"informational": True, "rawStatus": "unobserved", "displayValue": "미관측", "observationOnly": True})
    checks.append(check)
    review["checks"] = checks

    timeline = [dict(x) if isinstance(x, dict) else x for x in (review.get("timeline") or [])]
    timeline = [x for x in timeline if not (isinstance(x, dict) and str(x.get("kind") or "") == "bloodlust_timing")]
    if first:
        sec = round(float(first.get("sec") or 0), 3)
        timeline.append({
            "kind": "bloodlust_timing", "sec": sec, "title": "블러드", "label": "BL",
            "summary": f"{first.get('spellName') or '블러드'} · 첫 사용",
            "status": "unknown", "evidenceLevel": EVIDENCE_VERIFIED,
            "informational": True, "observationOnly": True,
        })
        timeline.sort(key=lambda x: float(x.get("sec") or 0) if isinstance(x, dict) and x.get("sec") is not None else 10**12)
    review["timeline"] = timeline
    obs = dict(review.get("observations") or {})
    obs["bloodlust"] = {
        "observed": bool(first),
        "firstSec": round(float(first.get("sec") or 0), 3) if first else None,
        "spellID": safe_int(first.get("spellID") or first.get("abilityGameID")) if first else None,
        "spellName": first.get("spellName") if first else None,
        "player": first.get("sourceName") if first else None,
        "source": source if first else None,
        "observationOnly": True,
        "sourceKind": source_kind,
    }
    review["observations"] = obs
    return review



def analyze_events(events: Iterable[dict], encounter: dict, *, source_kind: str = "normalized", supplemental: dict | None = None) -> dict | None:
    rule = _rule_for_encounter(encounter)
    if not rule:
        return None
    event_rows = [dict(x) for x in events if isinstance(x, dict)]
    boss_key = rule.get("bossKey")
    review = None
    if boss_key == "coiled_altar":
        try:
            review = _analyze_coiled(event_rows, encounter, rule, source_kind=source_kind, supplemental=supplemental)
        finally:
            _fast_end()
    elif boss_key == "buried_sentinel":
        review = _analyze_sentinel(event_rows, encounter, rule, source_kind=source_kind)
    elif boss_key == "twin_fangs":
        review = _analyze_twin_fangs(event_rows, encounter, rule, source_kind=source_kind)
    elif boss_key == "nekzali":
        review = _analyze_nekzali(event_rows, encounter, rule, source_kind=source_kind)
    elif boss_key == "lost_explorers":
        review = _analyze_lost_explorers(event_rows, encounter, rule, source_kind=source_kind)
    elif boss_key == "vashnik":
        review = _analyze_vashnik(event_rows, encounter, rule, source_kind=source_kind)
    elif boss_key == "sszorak":
        review = _analyze_sszorak(event_rows, encounter, rule, source_kind=source_kind)
    elif boss_key == "ulatek":
        review = _analyze_ulatek(event_rows, encounter, rule, source_kind=source_kind, supplemental=supplemental)
    elif boss_key == "nymrissa":
        review = _analyze_nymrissa(event_rows, encounter, rule, source_kind=source_kind)
    return _append_bloodlust_observation(review, event_rows, source_kind=source_kind)


def analyze_raw_try(raw_path: Path | str, encounter: dict) -> dict | None:
    rule = _rule_for_encounter(encounter)
    if not rule:
        return None
    rows = _normalize_raw(Path(raw_path), encounter)
    return analyze_events(rows, encounter, source_kind="raw")


def _generic_b31_temporal_context(mechanic_review: dict, b31_review: dict) -> dict:
    """Return conservative timing context between BossMechanic and B31.

    B37.3.6 deliberately does *not* turn temporal proximity into causal proof.
    A verified mechanic failure may be exposed as the immediately preceding
    event before a B31 collapse, or before a wipe/reset that ended within 15s.
    Only explicit same-mechanic evidence may become ``status=linked``.
    Candidate mechanic meanings never enter this projection.
    """
    mech = mechanic_review or {}
    raid = b31_review or {}
    obs = mech.get("observations") or {}
    first = obs.get("firstVerifiedFailure") if isinstance(obs.get("firstVerifiedFailure"), dict) else None
    if not first or str(first.get("evidenceLevel") or "").lower() != EVIDENCE_VERIFIED:
        return {}
    try:
        failure_sec = float(first.get("sec"))
    except (TypeError, ValueError):
        return {}

    collapse = raid.get("collapseChain") or {}
    # Only the explicit B31 collapse chain counts as a collapse anchor here.
    # A standalone B31 verdict timestamp may describe an earlier finding and
    # must not be relabelled as raid collapse.
    collapse_sec = collapse.get("rootTime")
    try:
        collapse_sec = float(collapse_sec) if collapse_sec is not None else None
    except (TypeError, ValueError):
        collapse_sec = None

    if collapse_sec is not None:
        delta = collapse_sec - failure_sec
        if 0.0 <= delta <= 8.0:
            return {
                "status": "preceding",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "summary": f"{first.get('label') or first.get('kind') or '검증 실패'} 후 {delta:.1f}초 뒤 B31 붕괴 시작 · 선후관계만 확인",
                "precedingFailure": dict(first),
                "collapseSec": round(collapse_sec, 3),
                "deltaSec": round(delta, 3),
                "b31RootMechanic": collapse.get("rootMechanic") or (raid.get("verdict") or {}).get("title"),
                "causal": False,
            }

    enc = raid.get("encounter") or mech.get("encounter") or {}
    result = str(enc.get("result") or "").upper()
    duration = enc.get("durationSec")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    if result == "WIPE" and duration is not None:
        delta = duration - failure_sec
        if 0.0 <= delta <= 15.0:
            return {
                "status": "preceding_end",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "summary": f"{first.get('label') or first.get('kind') or '검증 실패'} 후 {delta:.1f}초 내 전투 종료 · 종료 연관만 확인",
                "precedingFailure": dict(first),
                "fightEndSec": round(duration, 3),
                "deltaSec": round(delta, 3),
                "causal": False,
            }
    return {}




def _ulatek_verified_failure_rows(review: dict) -> list[dict]:
    """Return every locked Mythic Ula'tek execution failure chronologically."""
    obs = (review or {}).get("observations") or {}
    rows = []
    stored = obs.get("verifiedFailures")
    if isinstance(stored, list):
        rows.extend(dict(x) for x in stored if isinstance(x, dict))
    if not rows:
        for item in (review or {}).get("timeline") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "") not in {"unchecked_rage", "anguished_cry", "calcified_corpse"}:
                continue
            if str(item.get("status") or "").lower() != "failed":
                continue
            if str(item.get("evidenceLevel") or "").lower() != EVIDENCE_VERIFIED:
                continue
            rows.append(dict(item))
    out, seen = [], set()
    for row in rows:
        kind = str(row.get("kind") or "")
        if kind not in {"unchecked_rage", "anguished_cry", "calcified_corpse"}:
            continue
        try:
            sec = round(float(row.get("sec")), 3)
        except (TypeError, ValueError):
            continue
        key = (kind, sec)
        if key in seen:
            continue
        seen.add(key)
        item = dict(row)
        item["sec"] = sec
        item.setdefault("evidenceLevel", EVIDENCE_VERIFIED)
        item.setdefault("status", "failed")
        out.append(item)
    out.sort(key=lambda x: float(x.get("sec") or 0))
    return out


def _ulatek_apply_failure_roles(out: dict) -> None:
    """Classify locked Ula'tek execution failures without inventing collapse causality."""
    if str(out.get("bossKey") or "") != "ulatek":
        return
    obs = dict(out.get("observations") or {})
    failures = _ulatek_verified_failure_rows(out)
    if not failures:
        obs["failureRoles"] = []
        out["observations"] = obs
        return
    result = str((out.get("encounter") or {}).get("result") or "").upper()
    first = obs.get("firstVerifiedFailure") or {}
    direct = out.get("collapseDirect") or {}

    def same(a, b):
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        if str(a.get("kind") or "") != str(b.get("kind") or ""):
            return False
        try:
            return abs(float(a.get("sec")) - float(b.get("sec"))) <= 0.25
        except (TypeError, ValueError):
            return False

    roles = []
    for item in failures:
        if item.get("failureEligible") is False:
            role, label = "downstream_verified_failure", "추가 검증 실패 · 첫 실수 제외"
        elif result == "KILL":
            role, label = "recovered_on_kill", "처치 성공 · 회복된 실수"
        elif same(item, direct):
            role, label = "collapse_driver", "공대 붕괴 직결"
        elif same(item, first):
            role, label = "first_mistake", "첫 검증 실수"
        else:
            role, label = "additional_failure", "추가 검증 실수"
        row = dict(item)
        row["failureRole"] = role
        row["failureRoleLabel"] = label
        roles.append(row)

    obs["failureRoles"] = roles
    obs["verifiedFailures"] = roles
    if isinstance(obs.get("firstVerifiedFailure"), dict):
        for item in roles:
            if same(item, obs["firstVerifiedFailure"]):
                obs["firstVerifiedFailure"] = dict(item)
                break
    for field in ("firstMistake", "majorIncident", "collapseDirect"):
        current = out.get(field)
        if isinstance(current, dict):
            for item in roles:
                if same(item, current):
                    merged = dict(current)
                    merged["failureRole"] = item.get("failureRole")
                    merged["failureRoleLabel"] = item.get("failureRoleLabel")
                    out[field] = merged
                    break
    timeline = []
    for item in out.get("timeline") or []:
        row = dict(item) if isinstance(item, dict) else item
        if isinstance(row, dict):
            for role_item in roles:
                if same(role_item, row):
                    row["failureRole"] = role_item.get("failureRole")
                    row["failureRoleLabel"] = role_item.get("failureRoleLabel")
                    break
        timeline.append(row)
    out["timeline"] = timeline
    out["observations"] = obs

_SENTINEL_COLLAPSE_SPEC = {
    "aliases": ("맹독성 작렬", "Toxic Blast"),
    "triggerIDs": {1284452},
    "action": "다음 풀: 유독한 물방울 미섭취를 0개로 만들어 맹독성 작렬 연속 피격을 막으세요.",
}


def _nekzali_verified_failure_rows(review: dict) -> list[dict]:
    """Return every verified Soulcoiler's Curse completion in chronological order."""
    obs = (review or {}).get("observations") or {}
    rows = []
    stored = obs.get("verifiedFailures")
    if isinstance(stored, list):
        rows.extend(dict(x) for x in stored if isinstance(x, dict))
    if not rows:
        for item in (review or {}).get("timeline") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "") != "soulcoilers_curse":
                continue
            if str(item.get("status") or "").lower() != "failed":
                continue
            if str(item.get("evidenceLevel") or "").lower() != EVIDENCE_VERIFIED:
                continue
            rows.append(dict(item))
    first = obs.get("firstVerifiedFailure")
    if isinstance(first, dict) and str(first.get("evidenceLevel") or "").lower() == EVIDENCE_VERIFIED:
        rows.append(dict(first))
    out, seen = [], set()
    for row in rows:
        if str(row.get("kind") or "soulcoilers_curse") != "soulcoilers_curse":
            continue
        try:
            sec = round(float(row.get("sec")), 3)
        except (TypeError, ValueError):
            continue
        key = ("soulcoilers_curse", sec)
        if key in seen:
            continue
        seen.add(key)
        item = dict(row)
        item.update({"sec": sec, "kind": "soulcoilers_curse"})
        item.setdefault("evidenceLevel", EVIDENCE_VERIFIED)
        item.setdefault("status", "failed")
        item.setdefault("label", "영혼살무사의 저주 미차단")
        out.append(item)
    out.sort(key=lambda x: float(x.get("sec") or 0))
    return out


def _nekzali_apply_failure_roles(out: dict) -> None:
    """Classify verified Curse completions without turning Salvo collapse observations into player blame."""
    if str(out.get("bossKey") or "") != "nekzali":
        return
    obs = dict(out.get("observations") or {})
    failures = _nekzali_verified_failure_rows(out)
    if not failures:
        obs["failureRoles"] = []
        out["observations"] = obs
        return
    result = str((out.get("encounter") or {}).get("result") or "").upper()
    first = obs.get("firstVerifiedFailure") or {}
    direct = out.get("collapseDirect") or {}

    def same(a, b):
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        # A Haunted Salvo collapse is an observed collapse mechanism, not a Curse execution failure.
        if str(b.get("kind") or "") != "soulcoilers_curse":
            return False
        try:
            return abs(float(a.get("sec")) - float(b.get("sec"))) <= 0.25
        except (TypeError, ValueError):
            return False

    roles = []
    for item in failures:
        if result == "KILL":
            role, label = "recovered_on_kill", "처치 성공 · 회복된 실수"
        elif same(item, direct):
            role, label = "collapse_driver", "공대 붕괴 직결"
        elif same(item, first):
            role, label = "first_mistake", "첫 검증 실수"
        else:
            role, label = "additional_failure", "추가 검증 실수"
        row = dict(item)
        row["failureRole"] = role
        row["failureRoleLabel"] = label
        roles.append(row)

    obs["failureRoles"] = roles
    obs["verifiedFailures"] = roles
    if isinstance(obs.get("firstVerifiedFailure"), dict):
        for item in roles:
            if same(item, obs["firstVerifiedFailure"]):
                obs["firstVerifiedFailure"] = dict(item)
                break
    for field in ("firstMistake", "majorIncident", "collapseDirect"):
        current = out.get(field)
        if isinstance(current, dict):
            for item in roles:
                if same(item, current):
                    merged = dict(current)
                    merged["failureRole"] = item.get("failureRole")
                    merged["failureRoleLabel"] = item.get("failureRoleLabel")
                    out[field] = merged
                    break
    timeline = []
    for item in out.get("timeline") or []:
        row = dict(item) if isinstance(item, dict) else item
        if isinstance(row, dict):
            for role_item in roles:
                if same(role_item, row):
                    row["failureRole"] = role_item.get("failureRole")
                    row["failureRoleLabel"] = role_item.get("failureRoleLabel")
                    break
        timeline.append(row)
    out["timeline"] = timeline
    out["observations"] = obs


def _sentinel_verified_failure_rows(review: dict) -> list[dict]:
    """Return every verified missed-globule failure in chronological order."""
    obs = (review or {}).get("observations") or {}
    rows = []
    stored = obs.get("verifiedFailures")
    if isinstance(stored, list):
        rows.extend(dict(x) for x in stored if isinstance(x, dict))
    if not rows:
        for item in (review or {}).get("timeline") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "") != "missed_globules":
                continue
            if str(item.get("status") or "").lower() != "failed":
                continue
            if str(item.get("evidenceLevel") or "").lower() != EVIDENCE_VERIFIED:
                continue
            rows.append({
                "sec": item.get("sec"), "kind": "missed_globules",
                "label": item.get("title") or item.get("label"),
                "summary": item.get("summary"),
                "evidenceLevel": EVIDENCE_VERIFIED, "status": "failed",
            })
    first = obs.get("firstVerifiedFailure")
    if isinstance(first, dict) and str(first.get("evidenceLevel") or "").lower() == EVIDENCE_VERIFIED:
        row = dict(first)
        row.setdefault("kind", "missed_globules")
        rows.append(row)

    out, seen = [], set()
    for row in rows:
        try:
            sec = round(float(row.get("sec")), 3)
        except (TypeError, ValueError):
            continue
        kind = str(row.get("kind") or "missed_globules")
        if kind != "missed_globules":
            continue
        key = (kind, sec)
        if key in seen:
            continue
        seen.add(key)
        item = dict(row)
        item.update({"sec": sec, "kind": kind})
        item.setdefault("evidenceLevel", EVIDENCE_VERIFIED)
        item.setdefault("status", "failed")
        out.append(item)
    out.sort(key=lambda x: float(x.get("sec") or 0))
    return out


def _sentinel_exact_collapse_link(review: dict, b31_review: dict) -> dict:
    """Link a specific missed-globule failure to the actual B31 collapse.

    Prefer a Toxic Blast-dominated collapse cluster. If B31 has no explicit
    collapse cluster (for example a short low-health cascade), require the B31
    root spell/title itself to be Toxic Blast and match the failure tightly.
    Merely mentioning Toxic Blast later in a longer collapse chain is not enough.
    """
    failures = _sentinel_verified_failure_rows(review)
    if not failures:
        return {}
    raid = b31_review or {}
    collapse = raid.get("collapse") if isinstance(raid.get("collapse"), dict) else {}
    spells = collapse.get("spells") if isinstance(collapse.get("spells"), dict) else {}
    try:
        collapse_sec = float(collapse.get("startSec")) if collapse.get("startSec") is not None else None
    except (TypeError, ValueError):
        collapse_sec = None
    try:
        collapse_count = int(collapse.get("count") or 0)
    except (TypeError, ValueError):
        collapse_count = 0

    toxic_deaths = 0
    toxic_name = None
    for name, raw in spells.items():
        if any(alias.lower() in str(name).lower() for alias in _SENTINEL_COLLAPSE_SPEC["aliases"]):
            try:
                n = int(raw or 0)
            except (TypeError, ValueError):
                n = 0
            if n > toxic_deaths:
                toxic_deaths, toxic_name = n, str(name)

    if collapse_sec is not None and toxic_deaths > 0:
        required = max(3, int(math.ceil(collapse_count * 0.60))) if collapse_count else 3
        if toxic_deaths >= required:
            candidates = []
            for failure in failures:
                delta = float(failure.get("sec")) - collapse_sec
                if -2.0 <= delta <= 1.0:
                    candidates.append((abs(delta), failure, delta))
            if candidates:
                candidates.sort(key=lambda x: x[0])
                _dist, failure, delta = candidates[0]
                direct = dict(failure)
                direct.update({
                    "projection": "verified_failure_linked_to_b31_exact",
                    "collapseDeaths": toxic_deaths,
                    "collapseCount": collapse_count,
                    "collapseSec": round(collapse_sec, 3),
                    "deltaSec": round(delta, 3),
                })
                primary = raid.get("primaryFinding") if isinstance(raid.get("primaryFinding"), dict) else {}
                root_title = str(primary.get("title") or (raid.get("verdict") or {}).get("title") or "")
                root_sec = primary.get("sec") if primary.get("sec") is not None else (raid.get("verdict") or {}).get("sec")
                same_root = 1284452 in {safe_int(x) for x in (primary.get("trigger_spell_ids") or []) if safe_int(x) is not None}
                if same_root:
                    summary = f"{direct.get('label') or '유독한 물방울 미섭취'} {_fmt_time(direct.get('sec'))} ↔ B31 동일 기믹 붕괴 {_fmt_time(collapse_sec)}"
                    scope = "same_mechanic_collapse_root"
                else:
                    root_note = f"B31 원인 체인 {root_title} {_fmt_time(root_sec)}에서 시작 · " if root_title else ""
                    summary = f"{root_note}실제 붕괴 {collapse_count}명 중 {toxic_name or '맹독성 작렬'} 직접 사망 {toxic_deaths}명 · 미섭취 신호 {_fmt_time(direct.get('sec'))}"
                    scope = "direct_collapse_driver_after_earlier_root"
                direct["linkSummary"] = summary
                return {
                    "status": "linked", "evidenceLevel": EVIDENCE_VERIFIED,
                    "summary": summary, "matchingFailure": direct,
                    "collapseSec": round(collapse_sec, 3),
                    "collapseDeathCount": toxic_deaths,
                    "collapseCount": collapse_count,
                    "causal": True, "causalScope": scope,
                }

    verdict = raid.get("verdict") if isinstance(raid.get("verdict"), dict) else {}
    primary = raid.get("primaryFinding") if isinstance(raid.get("primaryFinding"), dict) else {}
    trigger_ids = {safe_int(x) for x in (primary.get("trigger_spell_ids") or []) if safe_int(x) is not None}
    root_text = " ".join(str(x or "") for x in (
        verdict.get("title"), verdict.get("interpretation"), primary.get("title"), primary.get("interpretation")
    ))
    same_mechanic = bool(trigger_ids & _SENTINEL_COLLAPSE_SPEC["triggerIDs"]) or any(
        alias.lower() in root_text.lower() for alias in _SENTINEL_COLLAPSE_SPEC["aliases"]
    )
    if not same_mechanic:
        return {}
    root_sec = primary.get("sec") if primary.get("sec") is not None else verdict.get("sec")
    try:
        root_sec = float(root_sec) if root_sec is not None else None
    except (TypeError, ValueError):
        root_sec = None
    if root_sec is None:
        return {}
    candidates = []
    for failure in failures:
        delta = root_sec - float(failure.get("sec"))
        if -1.0 <= delta <= 3.0:
            candidates.append((abs(delta), failure, delta))
    if not candidates:
        return {}
    candidates.sort(key=lambda x: x[0])
    _dist, failure, delta = candidates[0]
    direct = dict(failure)
    direct.update({
        "projection": "verified_failure_linked_to_b31_exact",
        "b31RootSec": round(root_sec, 3), "deltaSec": round(delta, 3),
    })
    summary = f"{direct.get('label') or '유독한 물방울 미섭취'} {_fmt_time(direct.get('sec'))} ↔ B31 동일 기믹 {_fmt_time(root_sec)}"
    direct["linkSummary"] = summary
    return {
        "status": "linked", "evidenceLevel": EVIDENCE_VERIFIED,
        "summary": summary, "matchingFailure": direct,
        "b31VerdictTitle": verdict.get("title"), "b31VerdictSec": verdict.get("sec"),
        "causal": True, "causalScope": "same_mechanic_root",
    }


def _sentinel_apply_failure_roles(out: dict) -> None:
    if str(out.get("bossKey") or "") != "buried_sentinel":
        return
    obs = dict(out.get("observations") or {})
    failures = _sentinel_verified_failure_rows(out)
    if not failures:
        obs["failureRoles"] = []
        out["observations"] = obs
        return
    result = str((out.get("encounter") or {}).get("result") or "").upper()
    first = obs.get("firstVerifiedFailure") or {}
    direct = out.get("collapseDirect") or {}

    def same(a, b):
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        if str(a.get("kind") or "missed_globules") != str(b.get("kind") or "missed_globules"):
            return False
        try:
            return abs(float(a.get("sec")) - float(b.get("sec"))) <= 0.25
        except (TypeError, ValueError):
            return False

    roles = []
    for item in failures:
        if result == "KILL":
            role, label = "recovered_on_kill", "처치 성공 · 회복된 실수"
        elif same(item, direct):
            role, label = "collapse_driver", "공대 붕괴 직결"
        elif same(item, first):
            role, label = "first_mistake", "첫 검증 실수"
        else:
            role, label = "additional_failure", "추가 검증 실수"
        row = dict(item)
        row["failureRole"] = role
        row["failureRoleLabel"] = label
        roles.append(row)

    obs["failureRoles"] = roles
    obs["verifiedFailures"] = roles
    if isinstance(obs.get("firstVerifiedFailure"), dict):
        for item in roles:
            if same(item, obs["firstVerifiedFailure"]):
                obs["firstVerifiedFailure"] = dict(item)
                break
    for field in ("majorIncident", "collapseDirect"):
        current = out.get(field)
        if isinstance(current, dict):
            for item in roles:
                if same(item, current):
                    merged = dict(current)
                    merged["failureRole"] = item.get("failureRole")
                    merged["failureRoleLabel"] = item.get("failureRoleLabel")
                    out[field] = merged
                    break
    out["observations"] = obs

    timeline = []
    for row in (out.get("timeline") or []):
        row = dict(row) if isinstance(row, dict) else row
        if isinstance(row, dict) and str(row.get("status") or "") == "failed":
            for item in roles:
                if same(row, item):
                    row["failureRole"] = item.get("failureRole")
                    row["failureRoleLabel"] = item.get("failureRoleLabel")
                    break
        timeline.append(row)
    out["timeline"] = timeline


_TWIN_COLLAPSE_SPECS = {
    "ravenous_feast_mass_hit": {
        "aliases": ("걸신들린 탐식", "Ravenous Feast"),
        "triggerIDs": {1290662},
        "action": "다음 풀: 걸신들린 탐식의 공대 대규모 동시 피격과 즉시 다수 사망을 0회로 만드세요.",
    },
    "visceral_burst": {
        "aliases": ("내장 파열", "Visceral Burst"),
        "triggerIDs": {1308386},
        "action": "다음 풀: 새끼의 내장 파열 차단 누락을 0회로 만드세요.",
    },
    "caustic_globule_rupture": {
        "aliases": ("산성 방울", "부식성 구체", "Caustic Globule"),
        "triggerIDs": {1290338},
        "action": "다음 풀: 부식성 구체가 파열되기 전에 전부 흡수하세요.",
    },
    "stone_breaker_reverb": {
        "aliases": ("돌 파괴자", "Stone Breaker"),
        "triggerIDs": {1289153},
        "action": "다음 풀: 돌 파괴자 타격을 반드시 받아 플랫폼 반향을 0회로 만드세요.",
    },
    "tainted_burst": {
        "aliases": ("오염된 폭발", "Tainted Burst"),
        "triggerIDs": {1310105},
        "action": "다음 풀: 탐식 뒤 오염된 피 웅덩이를 만료 전에 전부 흡수하세요.",
    },
}


def _twin_verified_failure_rows(review: dict) -> list[dict]:
    """Return all verified Twin Fangs failures, not only the earliest one.

    B37.3.9.16 needs later failures because Mythic pulls can contain an early
    recoverable miss and a different failure that actually overlaps the wipe.
    Older persisted reviews may not yet carry ``verifiedFailures``, so timeline
    rows are a safe compatibility fallback.
    """
    obs = (review or {}).get("observations") or {}
    rows = []
    stored = obs.get("verifiedFailures")
    if isinstance(stored, list):
        rows.extend(dict(x) for x in stored if isinstance(x, dict))
    if not rows:
        for item in (review or {}).get("timeline") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "").lower() != "failed":
                continue
            if str(item.get("evidenceLevel") or "").lower() != EVIDENCE_VERIFIED:
                continue
            kind = str(item.get("kind") or "")
            if kind not in _TWIN_COLLAPSE_SPECS:
                continue
            rows.append({
                "sec": item.get("sec"), "kind": kind,
                "label": item.get("label") or item.get("title"),
                "summary": item.get("summary"),
                "evidenceLevel": EVIDENCE_VERIFIED,
                "status": "failed",
            })
    first = obs.get("firstVerifiedFailure")
    if isinstance(first, dict) and str(first.get("evidenceLevel") or "").lower() == EVIDENCE_VERIFIED:
        rows.append(dict(first))

    deduped = []
    seen = set()
    for row in rows:
        try:
            sec = round(float(row.get("sec")), 3)
        except (TypeError, ValueError):
            continue
        kind = str(row.get("kind") or "")
        if kind not in _TWIN_COLLAPSE_SPECS:
            continue
        key = (kind, sec)
        if key in seen:
            continue
        seen.add(key)
        out = dict(row)
        out["sec"] = sec
        out.setdefault("evidenceLevel", EVIDENCE_VERIFIED)
        out.setdefault("status", "failed")
        deduped.append(out)
    deduped.sort(key=lambda x: float(x.get("sec") or 0))
    return deduped


def _twin_failure_matches_text(kind: str, text: str, trigger_ids: set[int]) -> bool:
    spec = _TWIN_COLLAPSE_SPECS.get(kind) or {}
    if trigger_ids & set(spec.get("triggerIDs") or set()):
        return True
    low = str(text or "").lower()
    return any(str(alias).lower() in low for alias in (spec.get("aliases") or ()))


def _twin_exact_collapse_link(review: dict, b31_review: dict) -> dict:
    """Link a *specific* verified Twin Fangs failure to the B31 collapse.

    Priority:
    1) the observed B31 collapse cluster: match its death-spell label to a
       verified failure near the cluster and choose the mechanic responsible
       for the most cluster deaths;
    2) B31 primary/root spellID/title with a nearby matching verified failure.

    This prevents the old bug where any Visceral Burst somewhere in the pull
    made the *earliest unrelated* verified failure become collapseDirect.
    """
    failures = _twin_verified_failure_rows(review)
    if not failures:
        return {}
    raid = b31_review or {}
    collapse = raid.get("collapse") if isinstance(raid.get("collapse"), dict) else {}
    collapse_spells = collapse.get("spells") if isinstance(collapse.get("spells"), dict) else {}
    try:
        collapse_sec = float(collapse.get("startSec")) if collapse.get("startSec") is not None else None
    except (TypeError, ValueError):
        collapse_sec = None

    # Prefer a failure that actually overlaps the observed death cluster.
    cluster_candidates = []
    if collapse_sec is not None and collapse_spells:
        for failure in failures:
            kind = str(failure.get("kind") or "")
            spec = _TWIN_COLLAPSE_SPECS.get(kind) or {}
            direct_deaths = 0
            matched_name = None
            for name, count in collapse_spells.items():
                if any(str(alias).lower() in str(name).lower() for alias in (spec.get("aliases") or ())):
                    try:
                        c = int(count or 0)
                    except (TypeError, ValueError):
                        c = 0
                    if c > direct_deaths:
                        direct_deaths, matched_name = c, str(name)
            if direct_deaths <= 0:
                continue
            delta = float(failure.get("sec")) - collapse_sec
            # A mechanic signal may land just before or just after the first
            # death in the cluster. Keep this window deliberately tight.
            if -5.0 <= delta <= 2.0:
                cluster_candidates.append((direct_deaths, -abs(delta), failure, matched_name, delta))
    if cluster_candidates:
        cluster_candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        deaths, _neg_dist, failure, matched_name, delta = cluster_candidates[0]
        direct = dict(failure)
        direct.update({
            "projection": "verified_failure_linked_to_b31_exact",
            "collapseDeaths": int(deaths),
            "collapseSec": round(collapse_sec, 3),
            "deltaSec": round(delta, 3),
        })
        label = direct.get("label") or direct.get("title") or direct.get("kind")
        summary = f"B31 붕괴 구간에서 {matched_name} 직접 사망 {deaths}명 · {label} 신호 {_fmt_time(direct.get('sec'))}"
        direct["linkSummary"] = summary
        return {
            "status": "linked", "evidenceLevel": EVIDENCE_VERIFIED,
            "summary": summary, "matchingFailure": direct,
            "collapseSec": round(collapse_sec, 3), "collapseDeathCount": int(deaths),
            "causal": True, "causalScope": "same_mechanic_collapse_deaths",
        }

    verdict = raid.get("verdict") if isinstance(raid.get("verdict"), dict) else {}
    primary = raid.get("primaryFinding") if isinstance(raid.get("primaryFinding"), dict) else {}
    trigger_ids = {safe_int(x) for x in (primary.get("trigger_spell_ids") or []) if safe_int(x) is not None}
    root_text = " ".join(str(x or "") for x in (
        verdict.get("title"), verdict.get("interpretation"), primary.get("title"), primary.get("interpretation")
    ))
    root_sec = primary.get("sec") if primary.get("sec") is not None else verdict.get("sec")
    try:
        root_sec = float(root_sec) if root_sec is not None else None
    except (TypeError, ValueError):
        root_sec = None
    root_candidates = []
    for failure in failures:
        kind = str(failure.get("kind") or "")
        if not _twin_failure_matches_text(kind, root_text, trigger_ids):
            continue
        if root_sec is None:
            continue
        delta = root_sec - float(failure.get("sec"))
        if -1.0 <= delta <= 8.0:
            root_candidates.append((abs(delta), failure, delta))
    if root_candidates:
        root_candidates.sort(key=lambda x: x[0])
        _dist, failure, delta = root_candidates[0]
        direct = dict(failure)
        direct.update({
            "projection": "verified_failure_linked_to_b31_exact",
            "b31RootSec": round(root_sec, 3), "deltaSec": round(delta, 3),
        })
        label = direct.get("label") or direct.get("title") or direct.get("kind")
        summary = f"{label} {_fmt_time(direct.get('sec'))} ↔ B31 동일 기믹 {_fmt_time(root_sec)}"
        direct["linkSummary"] = summary
        return {
            "status": "linked", "evidenceLevel": EVIDENCE_VERIFIED,
            "summary": summary, "matchingFailure": direct,
            "b31VerdictTitle": verdict.get("title"), "b31VerdictSec": verdict.get("sec"),
            "causal": True, "causalScope": "same_mechanic_root",
        }
    return {}



_SSZORAK_COLLAPSE_SPECS = {
    "viscous_cyst_chain": {
        "aliases": ("끈적거리는 낭종", "Viscous Cyst"),
        "triggerIDs": {1287205},
        "action": "다음 풀: 구슬을 정해진 순서대로 한 번씩만 터뜨려 1초 안 연쇄 폭발을 0회로 만드세요.",
    },
}

_SSZORAK_FAILURE_KINDS = {"mutilate_under_soak", "viscous_cyst_chain", "serpents_fury_enrage"}


def _sszorak_verified_failure_rows(review: dict) -> list[dict]:
    """Return all verified Sszorak execution failures in chronological order."""
    obs = (review or {}).get("observations") or {}
    rows: list[dict] = []
    stored = obs.get("verifiedFailures")
    if isinstance(stored, list):
        rows.extend(dict(x) for x in stored if isinstance(x, dict))
    if not rows:
        for item in (review or {}).get("timeline") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "") not in _SSZORAK_FAILURE_KINDS:
                continue
            if str(item.get("status") or "").lower() != "failed":
                continue
            if str(item.get("evidenceLevel") or "").lower() != EVIDENCE_VERIFIED:
                continue
            rows.append(dict(item))
    first = obs.get("firstVerifiedFailure")
    if isinstance(first, dict) and str(first.get("evidenceLevel") or "").lower() == EVIDENCE_VERIFIED:
        rows.append(dict(first))

    out, seen = [], set()
    for row in rows:
        kind = str(row.get("kind") or "")
        if kind not in _SSZORAK_FAILURE_KINDS:
            continue
        try:
            sec = round(float(row.get("sec")), 3)
        except (TypeError, ValueError):
            continue
        key = (kind, sec)
        if key in seen:
            continue
        seen.add(key)
        item = dict(row)
        item.update({"sec": sec, "kind": kind})
        item.setdefault("evidenceLevel", EVIDENCE_VERIFIED)
        item.setdefault("status", "failed")
        out.append(item)
    out.sort(key=lambda x: float(x.get("sec") or 0))
    return out


def _sszorak_exact_collapse_link(review: dict, b31_review: dict) -> dict:
    """Link only a verified Cyst chain to a Cyst-dominated B31 collapse.

    A single Cyst burst can be normal encounter execution, so direct Cyst deaths
    are not enough.  We require the verified >=2-pulse chain and an immediately
    matching B31 collapse in which Cyst accounts for most direct deaths.
    """
    failures = [x for x in _sszorak_verified_failure_rows(review) if str(x.get("kind") or "") == "viscous_cyst_chain"]
    if not failures:
        return {}
    collapse = (b31_review or {}).get("collapse") if isinstance((b31_review or {}).get("collapse"), dict) else {}
    spells = collapse.get("spells") if isinstance(collapse.get("spells"), dict) else {}
    try:
        csec = float(collapse.get("startSec")) if collapse.get("startSec") is not None else None
    except (TypeError, ValueError):
        csec = None
    try:
        ccount = int(collapse.get("count") or 0)
    except (TypeError, ValueError):
        ccount = 0
    if csec is None or ccount < 4:
        return {}

    cyst_deaths = 0
    matched_spell = None
    for name, raw in spells.items():
        if any(alias.lower() in str(name).lower() for alias in _SSZORAK_COLLAPSE_SPECS["viscous_cyst_chain"]["aliases"]):
            try:
                n = int(raw or 0)
            except (TypeError, ValueError):
                n = 0
            if n > cyst_deaths:
                cyst_deaths, matched_spell = n, str(name)
    required = max(3, int(math.ceil(ccount * 0.60)))
    if cyst_deaths < required:
        return {}

    candidates = []
    for row in failures:
        try:
            sec = float(row.get("sec"))
        except (TypeError, ValueError):
            continue
        delta = sec - csec
        # Actual Try 4: chain starts 97.798, collapse starts 97.836.
        # Keep this intentionally tight so a later/earlier normal Cyst cycle
        # cannot be promoted merely because the same spell killed someone.
        if -1.0 <= delta <= 1.0:
            candidates.append((abs(delta), row, delta))
    if not candidates:
        return {}
    candidates.sort(key=lambda x: x[0])
    _dist, row, delta = candidates[0]
    matched = dict(row)
    matched.update({
        "collapseDeaths": cyst_deaths,
        "collapseCount": ccount,
        "collapseSec": round(csec, 3),
        "deltaSec": round(delta, 3),
        "causalScope": "same_mechanic_collapse_deaths",
    })
    return {
        "status": "linked",
        "evidenceLevel": EVIDENCE_VERIFIED,
        "summary": f"B31 붕괴 {ccount}명 중 {matched_spell or '끈적거리는 낭종'} 직접 사망 {cyst_deaths}명 · 구슬 2중 연쇄 {_fmt_time(matched.get('sec'))}",
        "matchingFailure": matched,
        "causal": True,
        "causalScope": "same_mechanic_collapse_deaths",
    }



def _sszorak_verified_temporal_context(review: dict, b31_review: dict) -> dict:
    """Attach verified Sszorak failures to the B31 collapse by time only.

    This is intentionally non-causal.  It is useful for Unbound Ferocity: the
    actual Mythic samples show it immediately before collapse, but B31 direct
    deaths are other spells, so we preserve chronology without calling it the
    direct death spell.
    """
    failures = _sszorak_verified_failure_rows(review)
    if not failures:
        return {}
    collapse = (b31_review or {}).get("collapse") if isinstance((b31_review or {}).get("collapse"), dict) else {}
    raw_sec = collapse.get("startSec") if collapse.get("startSec") is not None else collapse.get("rootTime")
    try:
        csec = float(raw_sec) if raw_sec is not None else None
    except (TypeError, ValueError):
        csec = None
    if csec is None:
        return {}
    preceding = []
    for row in failures:
        try:
            sec = float(row.get("sec"))
        except (TypeError, ValueError):
            continue
        delta = csec - sec
        if 0.0 <= delta <= 8.0:
            preceding.append((delta, row))
    if not preceding:
        return {}
    preceding.sort(key=lambda x: x[0])
    delta, row = preceding[0]
    return {
        "status": "preceding",
        "evidenceLevel": EVIDENCE_VERIFIED,
        "summary": f"{row.get('label') or row.get('kind')} 후 {delta:.1f}초 뒤 B31 붕괴 시작 · 선후관계만 확인",
        "precedingFailure": dict(row),
        "collapseSec": round(csec, 3),
        "deltaSec": round(delta, 3),
        "causal": False,
    }

def _sszorak_apply_failure_roles(out: dict) -> None:
    """Classify verified Sszorak mistakes separately from the wipe cause."""
    if str(out.get("bossKey") or "") != "sszorak":
        return
    obs = dict(out.get("observations") or {})
    failures = _sszorak_verified_failure_rows(out)
    if not failures:
        obs["failureRoles"] = []
        out["observations"] = obs
        return
    result = str((out.get("encounter") or {}).get("result") or "").upper()
    first = obs.get("firstVerifiedFailure") or {}
    direct = out.get("collapseDirect") or {}

    def same(a, b):
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        if str(a.get("kind") or "") != str(b.get("kind") or ""):
            return False
        try:
            return abs(float(a.get("sec")) - float(b.get("sec"))) <= 0.25
        except (TypeError, ValueError):
            return False

    roles = []
    for item in failures:
        if result == "KILL":
            role, label = "recovered_on_kill", "처치 성공 · 회복된 실수"
        elif same(item, direct):
            role, label = "collapse_driver", "공대 붕괴 직결"
        elif same(item, first):
            role, label = "first_mistake", "첫 검증 실수"
        else:
            role, label = "additional_failure", "추가 검증 실수"
        row = dict(item)
        row["failureRole"] = role
        row["failureRoleLabel"] = label
        roles.append(row)

    obs["failureRoles"] = roles
    obs["verifiedFailures"] = roles
    if isinstance(obs.get("firstVerifiedFailure"), dict):
        for item in roles:
            if same(item, obs["firstVerifiedFailure"]):
                obs["firstVerifiedFailure"] = dict(item)
                break
    for field in ("firstMistake", "majorIncident", "collapseDirect"):
        current = out.get(field)
        if isinstance(current, dict):
            for item in roles:
                if same(item, current):
                    merged = dict(current)
                    merged["failureRole"] = item.get("failureRole")
                    merged["failureRoleLabel"] = item.get("failureRoleLabel")
                    out[field] = merged
                    break
    timeline = []
    for row in out.get("timeline") or []:
        row = dict(row) if isinstance(row, dict) else row
        if isinstance(row, dict):
            for item in roles:
                if same(item, row):
                    row["failureRole"] = item.get("failureRole")
                    row["failureRoleLabel"] = item.get("failureRoleLabel")
                    break
        timeline.append(row)
    out["timeline"] = timeline
    out["observations"] = obs


def _sszorak_suppress_postcollapse_headline(out: dict, b31_review: dict) -> None:
    """Do not call a verified mistake *after* the B31 collapse the wipe cause.

    The mistake remains in firstMistake/failureRoles for coaching, while the
    presentation layer falls back to B31 for "why the pull ended".
    """
    if str(out.get("bossKey") or "") != "sszorak":
        return
    failures = _sszorak_verified_failure_rows(out)
    if not failures:
        return
    collapse = (b31_review or {}).get("collapse") if isinstance((b31_review or {}).get("collapse"), dict) else {}
    try:
        csec = float(collapse.get("startSec")) if collapse.get("startSec") is not None else None
        fsec = float(failures[0].get("sec"))
    except (TypeError, ValueError):
        return
    if csec is None or fsec <= csec + 0.25:
        return
    verdict = dict(out.get("verdict") or {})
    verdict.update({
        "severity": "INFO",
        "status": "unknown",
        "evidenceLevel": EVIDENCE_UNAVAILABLE,
        "primary": "붕괴 이후 검증 실수",
        "why": f"첫 verified 실행 실수는 {_fmt_time(fsec)}로, B31 붕괴 시작 {_fmt_time(csec)}보다 뒤입니다. 실수 자체는 보존하지만 이번 전멸 원인으로 사용하지 않습니다.",
        "nextAction": "전멸 원인은 B31 붕괴 체인으로 복기하고, 붕괴 이후 verified 실수는 별도 개선점으로 확인하세요.",
    })
    out["verdict"] = verdict
    out["postCollapseVerifiedFailure"] = {
        "failureSec": round(fsec, 3),
        "collapseSec": round(csec, 3),
        "deltaSec": round(fsec - csec, 3),
        "evidenceLevel": EVIDENCE_VERIFIED,
    }


_LOST_EXPLORERS_COLLAPSE_SPECS = {
    "relic_rupture": {"aliases": ("유물 파열", "Relic Rupture"), "triggerIDs": {1310027}},
    "elemental_explosion": {"aliases": ("원소 폭발", "Elemental Explosion"), "triggerIDs": {1295952}},
}

_LOST_EXPLORERS_FAILURE_KINDS = {"icebound_flames", "relic_rupture", "elemental_explosion"}


def _lost_explorers_verified_failure_rows(review: dict) -> list[dict]:
    """Return all verified execution failures for Lost Explorers in time order."""
    obs = (review or {}).get("observations") or {}
    rows: list[dict] = []
    stored = obs.get("verifiedFailures")
    if isinstance(stored, list):
        rows.extend(dict(x) for x in stored if isinstance(x, dict))
    if not rows:
        for item in (review or {}).get("timeline") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "") not in _LOST_EXPLORERS_FAILURE_KINDS:
                continue
            if str(item.get("status") or "").lower() != "failed":
                continue
            if str(item.get("evidenceLevel") or "").lower() != EVIDENCE_VERIFIED:
                continue
            rows.append(dict(item))
    first = obs.get("firstVerifiedFailure")
    if isinstance(first, dict) and str(first.get("evidenceLevel") or "").lower() == EVIDENCE_VERIFIED:
        rows.append(dict(first))
    out, seen = [], set()
    for row in rows:
        kind = str(row.get("kind") or "")
        if kind not in _LOST_EXPLORERS_FAILURE_KINDS:
            continue
        try:
            sec = round(float(row.get("sec")), 3)
        except (TypeError, ValueError):
            continue
        key = (kind, sec)
        if key in seen:
            continue
        seen.add(key)
        item = dict(row)
        item.update({"sec": sec, "kind": kind})
        item.setdefault("evidenceLevel", EVIDENCE_VERIFIED)
        item.setdefault("status", "failed")
        out.append(item)
    out.sort(key=lambda x: float(x.get("sec") or 0))
    return out


def _lost_explorers_apply_failure_roles(out: dict) -> None:
    """Separate verified execution mistakes from the wipe role they eventually played."""
    if str(out.get("bossKey") or "") != "lost_explorers":
        return
    obs = dict(out.get("observations") or {})
    failures = _lost_explorers_verified_failure_rows(out)
    if not failures:
        obs["failureRoles"] = []
        out["observations"] = obs
        return
    result = str((out.get("encounter") or {}).get("result") or "").upper()
    first = obs.get("firstVerifiedFailure") or {}
    direct = out.get("collapseDirect") or {}

    def same(a, b):
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        if str(a.get("kind") or "") != str(b.get("kind") or ""):
            return False
        try:
            return abs(float(a.get("sec")) - float(b.get("sec"))) <= 0.25
        except (TypeError, ValueError):
            return False

    roles = []
    for item in failures:
        if result == "KILL":
            role, label = "recovered_on_kill", "처치 성공 · 회복된 실수"
        elif same(item, direct):
            role, label = "collapse_driver", "공대 붕괴 직결"
        elif same(item, first):
            role, label = "first_mistake", "첫 검증 실수"
        else:
            role, label = "additional_failure", "추가 검증 실수"
        row = dict(item)
        row["failureRole"] = role
        row["failureRoleLabel"] = label
        roles.append(row)

    obs["failureRoles"] = roles
    obs["verifiedFailures"] = roles
    if isinstance(obs.get("firstVerifiedFailure"), dict):
        for item in roles:
            if same(item, obs["firstVerifiedFailure"]):
                obs["firstVerifiedFailure"] = dict(item)
                break
    for field in ("firstMistake", "majorIncident", "collapseDirect"):
        current = out.get(field)
        if isinstance(current, dict):
            for item in roles:
                if same(item, current):
                    merged = dict(current)
                    merged["failureRole"] = item.get("failureRole")
                    merged["failureRoleLabel"] = item.get("failureRoleLabel")
                    out[field] = merged
                    break
    timeline = []
    for row in (out.get("timeline") or []):
        row = dict(row) if isinstance(row, dict) else row
        if isinstance(row, dict) and str(row.get("status") or "") == "failed" and str(row.get("evidenceLevel") or "") == EVIDENCE_VERIFIED:
            for item in roles:
                if same(item, row):
                    row["failureRole"] = item.get("failureRole")
                    row["failureRoleLabel"] = item.get("failureRoleLabel")
                    break
        timeline.append(row)
    out["timeline"] = timeline
    out["observations"] = obs


def _lost_explorers_exact_collapse_link(review: dict, b31_review: dict) -> dict:
    rows = [dict(x) for x in ((review or {}).get("timeline") or [])
            if x.get("evidenceLevel") == EVIDENCE_VERIFIED and str(x.get("kind") or "") in _LOST_EXPLORERS_COLLAPSE_SPECS]
    if not rows:
        return {}
    raid = b31_review or {}
    collapse = raid.get("collapse") if isinstance(raid.get("collapse"), dict) else {}
    spells = collapse.get("spells") if isinstance(collapse.get("spells"), dict) else {}
    try:
        csec = float(collapse.get("startSec")) if collapse.get("startSec") is not None else None
    except (TypeError, ValueError):
        csec = None
    candidates = []
    if csec is not None:
        for row in rows:
            spec = _LOST_EXPLORERS_COLLAPSE_SPECS.get(str(row.get("kind") or "")) or {}
            deaths = 0
            matched = None
            for name, count in spells.items():
                if any(str(alias).lower() in str(name).lower() for alias in spec.get("aliases", ())):
                    try:
                        n = int(count or 0)
                    except Exception:
                        n = 0
                    if n > deaths:
                        deaths, matched = n, str(name)
            delta = float(row.get("sec") or 0) - csec
            if deaths > 0 and -5.0 <= delta <= 2.0:
                candidates.append((deaths, -abs(delta), row, matched, delta))
    if not candidates:
        return {}
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    deaths, _dist, row, matched, delta = candidates[0]
    direct = dict(row)
    direct.update({"collapseDeaths": deaths, "collapseSec": round(csec, 3), "deltaSec": round(delta, 3)})
    summary = f"B31 붕괴 구간에서 {matched} 직접 사망 {deaths}명 · {direct.get('label') or direct.get('title')} 신호 {_fmt_time(direct.get('sec'))}"
    return {
        "status": "linked", "evidenceLevel": EVIDENCE_VERIFIED, "summary": summary,
        "matchingFailure": direct, "causal": True, "causalScope": "same_mechanic_collapse_deaths",
    }

_LOST_FINAL_ASCENSION_DAMAGE_ID = 1292780

def _lost_final_ascension_collapse_observation(review: dict, b31_review: dict) -> dict:
    """Promote only direct Final Ascension mass deaths aligned with collapse.

    Fish timing, cast completion and 1292780 damage exposure remain neutral
    observations because all three can occur in successful Mythic kills.  This
    helper is deliberately narrower: B31 must name 1292780 as the direct
    first-death root for at least four players and that mass-death root must
    coincide with the canonical collapse start.  It never creates a
    firstVerifiedFailure or assigns player blame.
    """
    raid = b31_review or {}
    collapse = raid.get("collapse") if isinstance(raid.get("collapse"), dict) else {}
    primary = raid.get("primaryFinding") if isinstance(raid.get("primaryFinding"), dict) else {}
    trigger_ids = {safe_int(x) for x in (primary.get("trigger_spell_ids") or []) if safe_int(x) is not None}
    if _LOST_FINAL_ASCENSION_DAMAGE_ID not in trigger_ids:
        return {}
    try:
        affected = int(primary.get("affected") or 0)
        collapse_count = int(collapse.get("count") or 0)
        psec = float(primary.get("sec"))
        csec = float(collapse.get("startSec"))
    except (TypeError, ValueError):
        return {}
    if affected < 4 or collapse_count < 4 or abs(psec - csec) > 2.0:
        return {}
    result = str(((review or {}).get("encounter") or {}).get("result") or "").upper()
    if result == "KILL":
        return {}

    # Attach the most recent charge/fish cycle as context only.  This explains
    # what the raid did without turning lateness/no-feed into a blame verdict.
    fish_cycles = ((((review or {}).get("observations") or {}).get("finalAscension") or {}).get("fishCycles") or [])
    cycle_context = None
    prior = []
    for cycle in fish_cycles:
        if not isinstance(cycle, dict):
            continue
        try:
            start = float(cycle.get("chargeStartSec"))
        except (TypeError, ValueError):
            continue
        if start <= psec + 0.001:
            prior.append((start, cycle))
    if prior:
        _, cycle = max(prior, key=lambda x: x[0])
        cycle_context = {
            "cycle": cycle.get("cycle"),
            "chargeStartSec": cycle.get("chargeStartSec"),
            "deadlineSec": cycle.get("deadlineSec"),
            "feedSec": cycle.get("feedSec"),
            "feedPlayer": cycle.get("feedPlayer"),
            "feedTarget": cycle.get("feedTarget"),
            "feedBeforeDeadlineSec": cycle.get("feedBeforeDeadlineSec"),
            "feedAfterCastStartSec": cycle.get("feedAfterCastStartSec"),
            "ascensionDamageEventCount": cycle.get("ascensionDamageEventCount"),
            "timing": cycle.get("timing"),
            "evidenceLevel": cycle.get("evidenceLevel"),
        }

    delta = psec - csec
    direct = {
        "kind": "final_ascension_collapse",
        "sec": round(psec, 3),
        "label": "최후의 승천 · 공대 붕괴",
        "title": "최후의 승천 · 공대 붕괴",
        "summary": f"최후의 승천 직접 첫 사망 {affected}명 · 붕괴 {collapse_count}명",
        "status": "failed",
        "evidenceLevel": EVIDENCE_VERIFIED,
        "directSpellIDs": [_LOST_FINAL_ASCENSION_DAMAGE_ID],
        "affected": affected,
        "collapseDeaths": collapse_count,
        "collapseSec": round(csec, 3),
        "deltaSec": round(delta, 3),
        "failureRole": "collapse_driver",
        "failureRoleLabel": "공대 붕괴 직결",
        "causalScope": "b31_final_ascension_mass_death_at_collapse",
        "blame": "unassigned",
        "fishCycleContext": cycle_context,
    }
    context_text = ""
    if cycle_context:
        feed = cycle_context.get("feedSec")
        target = cycle_context.get("feedTarget")
        before = cycle_context.get("feedBeforeDeadlineSec")
        if feed is None:
            context_text = f" · {cycle_context.get('cycle')}번째 충전에서 물고기 전달 미관측"
        elif before is not None:
            try:
                x = float(before)
                when = f"마감 {x:.1f}초 전" if x >= 0 else f"마감 {abs(x):.1f}초 후"
                context_text = f" · {cycle_context.get('cycle')}번째 물고기 {target or '?'} {when}"
            except (TypeError, ValueError):
                context_text = f" · {cycle_context.get('cycle')}번째 물고기 전달 관측"
    summary = (
        f"B31이 최후의 승천(1292780)을 직접 첫 사망 {affected}명의 원인으로 관측했고 "
        f"같은 시각대에 붕괴 {collapse_count}명이 시작됨 · Δ {delta:+.1f}초{context_text}"
    )
    return {
        "status": "linked",
        "evidenceLevel": EVIDENCE_VERIFIED,
        "summary": summary,
        "matchingFailure": direct,
        "causal": True,
        "causalScope": "b31_final_ascension_mass_death_at_collapse",
    }


_VASHNIK_COLLAPSE_SPECS = {
    "malignant_burst": {"aliases": ("악의 파열", "악성 파열", "Malignant Burst"), "triggerIDs": {1280189}},
    "catalytic_bile_miss": {"aliases": ("촉매성 담즙", "Catalytic Bile"), "triggerIDs": {1282616}},
}

def _vashnik_exact_collapse_link(review: dict, b31_review: dict) -> dict:
    rows = [dict(x) for x in ((review or {}).get("timeline") or [])
            if x.get("evidenceLevel") == EVIDENCE_VERIFIED and str(x.get("kind") or "") in _VASHNIK_COLLAPSE_SPECS]
    if not rows:
        return {}
    raid = b31_review or {}
    collapse = raid.get("collapse") if isinstance(raid.get("collapse"), dict) else {}
    spells = collapse.get("spells") if isinstance(collapse.get("spells"), dict) else {}
    try:
        csec = float(collapse.get("startSec")) if collapse.get("startSec") is not None else None
    except (TypeError, ValueError):
        csec = None
    cand=[]
    if csec is not None:
        for row in rows:
            spec=_VASHNIK_COLLAPSE_SPECS.get(str(row.get("kind") or "")) or {}
            deaths=0; matched=None
            for name,count in spells.items():
                if any(a.lower() in str(name).lower() for a in spec.get("aliases",())):
                    try: n=int(count or 0)
                    except Exception: n=0
                    if n>deaths: deaths=n; matched=str(name)
            delta=float(row.get("sec") or 0)-csec
            if deaths>0 and -5.0 <= delta <= 2.0:
                cand.append((deaths,-abs(delta),row,matched,delta))
    if not cand:
        return {}
    cand.sort(key=lambda x:(x[0],x[1]), reverse=True)
    deaths,_d,row,matched,delta=cand[0]
    direct=dict(row); direct.update({"collapseDeaths":deaths,"collapseSec":round(csec,3),"deltaSec":round(delta,3)})
    summary=f"B31 붕괴 구간에서 {matched} 직접 사망 {deaths}명 · {direct.get('label') or direct.get('title')} 신호 {_fmt_time(direct.get('sec'))}"
    return {"status":"linked","evidenceLevel":EVIDENCE_VERIFIED,"summary":summary,"matchingFailure":direct,"causal":True,"causalScope":"same_mechanic_collapse_deaths"}


_VASHNIK_MALIGNANCE_ID = 1304459

def _vashnik_malignance_collapse_observation(review: dict, b31_review: dict) -> dict:
    """Promote only a B31-observed Malignance mass-death that coincides with collapse.

    Malignance occurrence remains candidate/observational in the normal Vashnik
    mechanic review because successful kills also contain the mechanic.  The
    collapse observation is narrower: B31 must identify spell 1304459 as the
    direct first-death root for at least four players, and that mass-death root
    must land within two seconds of the canonical collapse start.
    """
    raid = b31_review or {}
    collapse = raid.get("collapse") if isinstance(raid.get("collapse"), dict) else {}
    primary = raid.get("primaryFinding") if isinstance(raid.get("primaryFinding"), dict) else {}
    trigger_ids = {safe_int(x) for x in (primary.get("trigger_spell_ids") or []) if safe_int(x) is not None}
    if _VASHNIK_MALIGNANCE_ID not in trigger_ids:
        return {}
    try:
        affected = int(primary.get("affected") or 0)
    except (TypeError, ValueError):
        affected = 0
    try:
        collapse_count = int(collapse.get("count") or 0)
    except (TypeError, ValueError):
        collapse_count = 0
    try:
        psec = float(primary.get("sec"))
        csec = float(collapse.get("startSec"))
    except (TypeError, ValueError):
        return {}
    if affected < 4 or collapse_count < 4 or abs(psec - csec) > 2.0:
        return {}
    result = str(((review or {}).get("encounter") or {}).get("result") or "").upper()
    if result == "KILL":
        return {}
    delta = psec - csec
    direct = {
        "kind": "malignance_collapse",
        "sec": round(psec, 3),
        "label": "악성 · 공대 붕괴",
        "title": "악성 · 공대 붕괴",
        "summary": f"악성 직접 첫 사망 {affected}명 · 붕괴 {collapse_count}명",
        "status": "failed",
        "evidenceLevel": EVIDENCE_VERIFIED,
        "directSpellIDs": [_VASHNIK_MALIGNANCE_ID],
        "affected": affected,
        "collapseDeaths": collapse_count,
        "collapseSec": round(csec, 3),
        "deltaSec": round(delta, 3),
        "failureRole": "collapse_driver",
        "failureRoleLabel": "공대 붕괴 직결",
        "causalScope": "b31_malignance_mass_death_at_collapse",
        "blame": "unassigned",
    }
    summary = (
        f"B31이 악성(1304459)을 직접 첫 사망 {affected}명의 원인으로 관측했고 "
        f"같은 시각대에 붕괴 {collapse_count}명이 시작됨 · Δ {delta:+.1f}초"
    )
    return {
        "status": "linked",
        "evidenceLevel": EVIDENCE_VERIFIED,
        "summary": summary,
        "matchingFailure": direct,
        "causal": True,
        "causalScope": "b31_malignance_mass_death_at_collapse",
    }



_COILED_FAILURE_RULE_KEY = {
    "manifest_collision": "malevolentResonance",
    "gloombomb_failure": "gloombomb",
    "wail_interrupt_failure": "wailOfTerror",
    "soul_sever_non_tank_hit": "soulSever",
    "prior_add_overlap": "spiritcackle",
}

_COILED_TIMELINE_RULE_KEY = {
    "orb_wave": "coalescedVenomSummon",
    "guillotine": "guillotine",
    "breath": "cleaveBreath",
    "phase2_entry": "phase2Activation",
    "phase2_activation": "phase2Activation",
    "p2_mind_control": "dreadmarch",
    "p2_soulcoilers": "spiritcackle",
    "p2_gloombomb": "gloombomb",
    "p2_soul_sever": "soulSever",
    "p2_wail_failure": "wailOfTerror",
    "p2_shield_start": "eternalNightfall",
    "p2_shield_break": "eternalNightfall",
    "p2_nightfall_interrupt": "eternalNightfall",
    "p2_nightfall_success": "eternalNightfall",
}

def _coiled_apply_split_evidence(phase2: dict, timeline: list[dict], rule: dict, source_kind: str) -> None:
    """B37.4.13: expose ID evidence separately from mechanic-meaning evidence."""
    for cycle in phase2.get("cycles") or []:
        judgment = cycle.get("judgment") or {}
        for failure in judgment.get("failures") or []:
            mechanic_key = _COILED_FAILURE_RULE_KEY.get(str(failure.get("kind") or ""))
            if not mechanic_key:
                continue
            id_ev = _observed_evidence_for_rule(rule, mechanic_key, source_kind)
            meaning_ev = _meaning_evidence_for_rule(rule, mechanic_key)
            failure["idEvidenceLevel"] = id_ev
            failure["meaningEvidenceLevel"] = meaning_ev
            failure["evidenceLevel"] = _combined_evidence(id_ev, meaning_ev)
            source = (((rule.get("mechanics") or {}).get(mechanic_key) or {}).get("meaningEvidenceSource"))
            if source:
                failure["meaningEvidenceSource"] = source
        failures = judgment.get("failures") or []
        if failures:
            failures.sort(key=lambda x: float(x.get("sec") or 0))
            judgment["firstMistake"] = dict(failures[0])
            judgment["firstMistakeEvidenceLevel"] = failures[0].get("meaningEvidenceLevel") or failures[0].get("evidenceLevel")
    # Rebuild summary references from enriched failures so the presentation does
    # not lose the split fields through stale copies.
    all_failures = [dict(f) for c in (phase2.get("cycles") or []) for f in (((c.get("judgment") or {}).get("failures")) or [])]
    all_failures.sort(key=lambda x: float(x.get("sec") or 0))
    summary = phase2.get("summary") or {}
    if all_failures:
        summary["firstMistake"] = dict(all_failures[0])
    for item in timeline:
        mechanic_key = _COILED_TIMELINE_RULE_KEY.get(str(item.get("kind") or ""))
        if not mechanic_key:
            continue
        id_ev = _observed_evidence_for_rule(rule, mechanic_key, source_kind)
        meaning_ev = _meaning_evidence_for_rule(rule, mechanic_key)
        item["idEvidenceLevel"] = id_ev
        item["meaningEvidenceLevel"] = meaning_ev
        # Observation rows keep ID evidence visible; failure rows require both.
        if str(item.get("status") or "") in {"failed", "warning"} or str(item.get("kind") or "").endswith("failure"):
            item["evidenceLevel"] = _combined_evidence(id_ev, meaning_ev)
        else:
            item["evidenceLevel"] = id_ev


def _coiled_b31_collapse_context(review: dict, b31_review: dict) -> dict:
    """Classify the B31 collapse without pretending every final hit is a boss-mechanic root."""
    collapse = (b31_review or {}).get("collapse") or {}
    try:
        start = float(collapse.get("startSec"))
        end = float(collapse.get("endSec"))
    except (TypeError, ValueError):
        return {}
    total = int(collapse.get("count") or 0)
    if total <= 0:
        return {}
    spells = dict(collapse.get("spells") or {})
    buckets = {"verified_mechanic_failure": 0, "ambient_raid_pressure": 0, "downstream_player_damage": 0, "unclassified": 0}
    classified = []
    for name, raw_count in spells.items():
        try: count = int(raw_count or 0)
        except Exception: count = 0
        low = str(name or "").lower()
        if "악의적인 공명" in low or "malevolent resonance" in low:
            category, sid = "verified_mechanic_failure", 1312132
        elif "무시무시한 존재감" in low or "dreadful presence" in low:
            category, sid = "ambient_raid_pressure", 1288635
        elif "고통의 맞불" in low or "set fire to the pain" in low:
            category, sid = "downstream_player_damage", 453286
        elif "피의 역병" in low or "blood plague" in low:
            category, sid = "downstream_player_damage", 55078
        else:
            category, sid = "unclassified", None
        buckets[category] += count
        classified.append({"spell": str(name), "count": count, "category": category, "spellID": sid})

    # Exact local link: count manifest-collision linked deaths whose death times
    # fall inside the B31 collapse window.  This is stronger than merely seeing
    # resonance somewhere earlier in the pull.
    linked = []
    matching_failure = None
    phase2 = ((review.get("observations") or {}).get("phase2") or {})
    for cycle in phase2.get("cycles") or []:
        for failure in ((cycle.get("judgment") or {}).get("failures") or []):
            if str(failure.get("kind") or "") != "manifest_collision":
                continue
            local = []
            for death in failure.get("linkedDeaths") or []:
                try: sec = float(death.get("deathSec"))
                except (TypeError, ValueError): continue
                if start - 0.15 <= sec <= end + 0.15:
                    local.append(dict(death))
            if local:
                linked.extend(local)
                if matching_failure is None or len(local) > len((matching_failure.get("_b31LinkedDeaths") or [])):
                    matching_failure = dict(failure)
                    matching_failure["_b31LinkedDeaths"] = local
    # Dedupe by player+time.
    dedup = {}
    for death in linked:
        key = (str(death.get("player") or ""), round(float(death.get("deathSec") or 0), 3))
        dedup[key] = death
    linked = list(dedup.values())
    linked_count = len(linked)
    ratio = (linked_count / total) if total else 0.0
    causal = linked_count >= 3 and ratio >= 0.60
    if matching_failure:
        matching_failure.pop("_b31LinkedDeaths", None)
        matching_failure["b31LinkedDeathCount"] = linked_count
        matching_failure["b31CollapseCount"] = total
    parts = [f"B31 붕괴 {_fmt_time(start)}~{_fmt_time(end)} · {total}명"]
    if linked_count:
        parts.append(f"현신 접촉 후속 {linked_count}명")
    if buckets["ambient_raid_pressure"]:
        parts.append(f"상시 공대피해 {buckets['ambient_raid_pressure']}명")
    if buckets["downstream_player_damage"]:
        parts.append(f"지연·플레이어 피해 {buckets['downstream_player_damage']}명")
    if buckets["unclassified"]:
        parts.append(f"미분류 {buckets['unclassified']}명")
    parts.append("단일 원인 직접 연결" if causal else "단일 원인 미확정")
    return {
        "status": "linked" if causal else "partial" if linked_count else "context_only",
        "evidenceLevel": EVIDENCE_VERIFIED,
        "basis": "b31_collapse_cluster",
        "startSec": round(start, 3), "endSec": round(end, 3), "count": total,
        "spells": spells, "classifications": classified, "buckets": buckets,
        "manifestLinkedDeathCount": linked_count, "manifestLinkedDeaths": linked,
        "manifestLinkedRatio": round(ratio, 4), "causal": causal,
        "matchingFailure": matching_failure, "summary": " · ".join(parts),
    }

def _twin_apply_failure_roles(out: dict) -> None:
    """Classify verified Twin Fangs failures by what happened after them."""
    if str(out.get("bossKey") or "") != "twin_fangs":
        return
    obs = dict(out.get("observations") or {})
    failures = [dict(x) for x in (obs.get("verifiedFailures") or []) if isinstance(x, dict)]
    if not failures:
        obs["failureRoles"] = []
        out["observations"] = obs
        return
    result = str((out.get("encounter") or {}).get("result") or "").upper()
    first = obs.get("firstVerifiedFailure") or {}
    direct = out.get("collapseDirect") or {}

    def same(a, b):
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        if str(a.get("kind") or "") != str(b.get("kind") or ""):
            return False
        try:
            return abs(float(a.get("sec")) - float(b.get("sec"))) <= 0.25
        except (TypeError, ValueError):
            return False

    roles = []
    for item in failures:
        if result == "KILL":
            role, label = "recovered_on_kill", "처치 성공 · 회복된 실수"
        elif same(item, direct):
            role, label = "collapse_driver", "공대 붕괴 직결"
        elif same(item, first):
            role, label = "first_mistake", "첫 검증 실수"
        else:
            role, label = "additional_failure", "추가 검증 실수"
        item["failureRole"] = role
        item["failureRoleLabel"] = label
        roles.append(item)

    obs["failureRoles"] = roles
    obs["verifiedFailures"] = roles
    if isinstance(obs.get("firstVerifiedFailure"), dict):
        for item in roles:
            if same(item, obs["firstVerifiedFailure"]):
                obs["firstVerifiedFailure"] = dict(item)
                break
    for field in ("majorIncident", "collapseDirect"):
        current = out.get(field)
        if isinstance(current, dict):
            for item in roles:
                if same(item, current):
                    merged = dict(current)
                    merged["failureRole"] = item.get("failureRole")
                    merged["failureRoleLabel"] = item.get("failureRoleLabel")
                    out[field] = merged
                    break
    out["observations"] = obs

    timeline = []
    for row in (out.get("timeline") or []):
        row = dict(row) if isinstance(row, dict) else row
        if isinstance(row, dict) and str(row.get("status") or "") == "failed":
            for item in roles:
                if same(row, item):
                    row["failureRole"] = item.get("failureRole")
                    row["failureRoleLabel"] = item.get("failureRoleLabel")
                    break
        timeline.append(row)
    out["timeline"] = timeline


_NYMRISSA_COLLAPSE_ALIASES = ("고동치는 해일", "Pulsing Tides")
_NYMRISSA_PULSING_TIDES_ID = 1271380


def _nymrissa_exact_collapse_link(review: dict, b31_review: dict) -> dict:
    """Link a verified Berserker leak to a Pulsing Tides B31 collapse.

    The execution mistake is the Berserker creation; the downstream direct-death
    spell is Pulsing Tides.  A tight 6-second window prevents unrelated earlier
    leaks from being relabelled as the collapse driver.
    """
    failures = [
        dict(x) for x in (((review or {}).get("observations") or {}).get("verifiedFailures") or [])
        if isinstance(x, dict)
        and str(x.get("kind") or "") == "berserker_leak"
        and str(x.get("evidenceLevel") or "").lower() == EVIDENCE_VERIFIED
    ]
    if not failures:
        return {}
    raid = b31_review or {}
    collapse = raid.get("collapse") if isinstance(raid.get("collapse"), dict) else {}
    spells = collapse.get("spells") if isinstance(collapse.get("spells"), dict) else {}
    try:
        csec = float(collapse.get("startSec")) if collapse.get("startSec") is not None else None
    except (TypeError, ValueError):
        csec = None
    deaths = 0
    matched_name = None
    for name, count in spells.items():
        if any(alias.lower() in str(name).lower() for alias in _NYMRISSA_COLLAPSE_ALIASES):
            try:
                n = int(count or 0)
            except (TypeError, ValueError):
                n = 0
            if n > deaths:
                deaths, matched_name = n, str(name)
    primary = raid.get("primaryFinding") if isinstance(raid.get("primaryFinding"), dict) else {}
    trigger_ids = {safe_int(x) for x in (primary.get("trigger_spell_ids") or []) if safe_int(x) is not None}
    if deaths <= 0 and _NYMRISSA_PULSING_TIDES_ID not in trigger_ids:
        return {}
    primary_sec = primary.get("sec")
    if csec is None and primary_sec is not None:
        try:
            csec = float(primary_sec)
        except (TypeError, ValueError):
            csec = None
    if csec is None:
        return {}
    candidates = []
    for row in failures:
        try:
            delta = float(row.get("sec")) - csec
        except (TypeError, ValueError):
            continue
        if -6.0 <= delta <= 0.5:
            candidates.append((abs(delta), row, delta))
    if not candidates:
        return {}
    candidates.sort(key=lambda x: x[0])
    _, row, delta = candidates[0]
    direct = dict(row)
    direct.update({
        "collapseDeaths": deaths or None,
        "collapseSec": round(csec, 3),
        "deltaSec": round(delta, 3),
        "downstreamSpellID": _NYMRISSA_PULSING_TIDES_ID,
        "causalScope": "berserker_spawn_to_pulsing_tides_collapse",
    })
    death_text = f"직접 사망 {deaths}명" if deaths else "B31 직접 사망 root"
    summary = (
        f"B31 붕괴에서 {matched_name or '고동치는 해일'} {death_text} · "
        f"광전사 생성 {_fmt_time(direct.get('sec'))} 후 {abs(delta):.1f}초 내 붕괴"
    )
    return {
        "status": "linked", "evidenceLevel": EVIDENCE_VERIFIED,
        "summary": summary, "matchingFailure": direct,
        "causal": True, "causalScope": "same_mechanic_downstream_direct_deaths",
    }



def link_review_to_b31(review: dict | None, b31_review: dict | None) -> dict | None:
    if not review:
        return review
    out = dict(review)
    link = {"status": "unknown", "evidenceLevel": EVIDENCE_UNAVAILABLE, "summary": "B31 붕괴 체인 연결 근거 없음"}
    b31_review = b31_review or {}
    verdict = b31_review.get("verdict") or {}
    text = " ".join(str(x or "") for x in [verdict.get("title"), verdict.get("interpretation"), (b31_review.get("primaryFinding") or {}).get("title"), (b31_review.get("primaryFinding") or {}).get("interpretation")])
    if out.get("bossKey") == "nymrissa":
        exact = _nymrissa_exact_collapse_link(out, b31_review)
        if exact:
            link = exact
            matched = dict(exact.get("matchingFailure") or {})
            out["majorIncident"] = dict(matched)
            out["collapseDirect"] = dict(matched)
            out["presentationOverride"] = {
                "mode": "verified_mechanic_collapse",
                "title": "멀록 유출 → 고동치는 해일 붕괴",
                "confidence": "high",
                "sec": matched.get("sec"),
                "interpretation": exact.get("summary"),
                "action": "다음 풀: Bubblefin 멀록이 거품에 들어가기 전에 처리해 광전사 생성을 0마리로 만드세요.",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "source": "BossMechanic + B31 exact mechanic link",
            }
            v = dict(out.get("verdict") or {})
            v["severity"] = "HIGH"
            v["why"] = (str(v.get("why") or "") + " " + str(exact.get("summary") or "")).strip()
            out["verdict"] = v
    if out.get("bossKey") == "coiled_altar":
        context = _coiled_b31_collapse_context(out, b31_review)
        if context:
            out["b31CollapseContext"] = context
            link = {
                "status": context.get("status"),
                "evidenceLevel": context.get("evidenceLevel"),
                "summary": context.get("summary"),
                "causal": bool(context.get("causal")),
                "causalScope": "b31_cluster_exact_link" if context.get("causal") else "partial_b31_cluster_context",
            }
            if context.get("causal") and context.get("matchingFailure"):
                matched = dict(context.get("matchingFailure") or {})
                out["majorIncident"] = dict(matched)
                out["collapseDirect"] = dict(matched)
            # Preserve the older 1-second diagnostic inside observations, but
            # user-facing collapse anchoring now follows the canonical B31 cluster.
    if out.get("bossKey") == "buried_sentinel":
        exact = _sentinel_exact_collapse_link(out, b31_review)
        if exact:
            link = exact
            matched = dict(exact.get("matchingFailure") or {})
            out["majorIncident"] = dict(matched)
            out["collapseDirect"] = dict(matched)
            out["presentationOverride"] = {
                "mode": "verified_mechanic_collapse",
                "title": matched.get("label") or "유독한 물방울 미섭취",
                "confidence": "high",
                "sec": matched.get("sec"),
                "interpretation": exact.get("summary"),
                "action": _SENTINEL_COLLAPSE_SPEC["action"],
                "evidenceLevel": EVIDENCE_VERIFIED,
                "source": "BossMechanic + B31 exact mechanic link",
            }
            first = ((out.get("observations") or {}).get("firstVerifiedFailure") or {})
            try:
                same_first = abs(float(first.get("sec")) - float(matched.get("sec"))) <= 0.25
            except (TypeError, ValueError):
                same_first = False
            if same_first:
                v = dict(out.get("verdict") or {})
                v["severity"] = "HIGH"
                v["why"] = (str(v.get("why") or "") + " " + str(exact.get("summary") or "")).strip()
                out["verdict"] = v
    if out.get("bossKey") == "lost_explorers":
        exact = _lost_explorers_exact_collapse_link(out, b31_review)
        if exact:
            link = exact
            matched = dict(exact.get("matchingFailure") or {})
            out["majorIncident"] = dict(matched)
            out["collapseDirect"] = dict(matched)
            action_by_kind = {
                "relic_rupture": "다음 풀: 유물을 제한 시간 안에 처리해 Relic Rupture를 0회로 만드세요.",
                "elemental_explosion": "다음 풀: 서리불꽃 효과 충돌을 해소해 Elemental Explosion을 0회로 만드세요.",
            }
            out["presentationOverride"] = {
                "mode": "verified_mechanic_collapse",
                "title": matched.get("label") or matched.get("title") or "검증된 붕괴 직결 기믹",
                "confidence": "high",
                "sec": matched.get("sec"),
                "interpretation": exact.get("summary"),
                "action": action_by_kind.get(str(matched.get("kind") or ""), "다음 풀에서 해당 직접 실패 신호를 0회로 만드세요."),
                "evidenceLevel": EVIDENCE_VERIFIED,
                "source": "BossMechanic + B31 exact mechanic link",
            }
        else:
            ascension_link = _lost_final_ascension_collapse_observation(out, b31_review)
            if ascension_link:
                link = ascension_link
                matched = dict(ascension_link.get("matchingFailure") or {})
                out["majorIncident"] = dict(matched)
                out["collapseDirect"] = dict(matched)
                checks = list(out.get("checks") or [])
                if not any(str(x.get("key") or "") == "final_ascension_collapse" for x in checks):
                    checks.append(make_check(
                        key="final_ascension_collapse",
                        title="최후의 승천 · 붕괴 사망",
                        status="failed",
                        evidence_level=EVIDENCE_VERIFIED,
                        summary=matched.get("summary") or ascension_link.get("summary") or "최후의 승천 직접 사망이 붕괴와 연결됨",
                        ids={"spellIDs": [_LOST_FINAL_ASCENSION_DAMAGE_ID]},
                    ))
                out["checks"] = checks
                timeline = list(out.get("timeline") or [])
                if not any(str(x.get("kind") or "") == "final_ascension_collapse" for x in timeline):
                    timeline.append(dict(matched))
                    timeline.sort(key=lambda x: float(x.get("sec") or 0))
                out["timeline"] = timeline
                out["presentationOverride"] = {
                    "mode": "verified_collapse_observation",
                    "title": "최후의 승천으로 공대 붕괴",
                    "confidence": "high",
                    "sec": matched.get("sec"),
                    "interpretation": ascension_link.get("summary"),
                    "action": "다음 풀: 최후의 승천 충전 종료 전후 물고기 전달 대상·시각과 모르자히 제어 전환, 공대 생존기 사용을 함께 확인하세요. 이 신호만으로 개인 실수는 단정하지 않습니다.",
                    "evidenceLevel": EVIDENCE_VERIFIED,
                    "source": "B31 collapse + BossMechanic fish-cycle calibration",
                }
                # Keep an already verified execution mistake as the top-level
                # first-mistake verdict.  When there is no such mistake, make
                # the verified collapse observation coherent with the detail
                # card without inventing blame.
                first = ((out.get("observations") or {}).get("firstVerifiedFailure") or {})
                if not first:
                    out["verdict"] = {
                        "severity": "HIGH",
                        "status": "failed",
                        "evidenceLevel": EVIDENCE_VERIFIED,
                        "primary": "최후의 승천 · 공대 붕괴",
                        "why": ascension_link.get("summary"),
                        "nextAction": "다음 풀: 물고기 전달과 모르자히 제어 전환을 최후의 승천 충전 종료 전에 준비하고, 직접 피해 노출 시 공생존기 사용까지 함께 복기하세요. 개인 실수는 이 신호만으로 단정하지 않습니다.",
                    }
    if out.get("bossKey") == "sszorak":
        exact = _sszorak_exact_collapse_link(out, b31_review)
        if exact:
            link = exact
            matched = dict(exact.get("matchingFailure") or {})
            out["majorIncident"] = dict(matched)
            out["collapseDirect"] = dict(matched)
            out["presentationOverride"] = {
                "mode": "verified_mechanic_collapse",
                "title": "구슬 연쇄 폭발",
                "confidence": "high",
                "sec": matched.get("sec"),
                "interpretation": exact.get("summary"),
                "action": _SSZORAK_COLLAPSE_SPECS["viscous_cyst_chain"]["action"],
                "evidenceLevel": EVIDENCE_VERIFIED,
                "source": "BossMechanic + B31 exact mechanic link",
            }
            v = dict(out.get("verdict") or {})
            v["severity"] = "HIGH"
            v["why"] = (str(v.get("why") or "") + " " + str(exact.get("summary") or "")).strip()
            out["verdict"] = v
    if out.get("bossKey") == "vashnik":
        exact = _vashnik_exact_collapse_link(out, b31_review)
        if exact:
            link = exact
            matched = dict(exact.get("matchingFailure") or {})
            out["majorIncident"] = dict(matched)
            out["collapseDirect"] = dict(matched)
            out["presentationOverride"] = {
                "mode": "verified_mechanic_collapse",
                "title": matched.get("label") or matched.get("title") or "검증된 붕괴 직결 기믹",
                "confidence": "high",
                "sec": matched.get("sec"),
                "interpretation": exact.get("summary"),
                "action": "다음 풀: 악의 파열과 촉매성 담즙 미흡수 직접 실패를 우선 0회로 만드세요.",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "source": "BossMechanic + B31 exact mechanic link",
            }
        else:
            malignance_link = _vashnik_malignance_collapse_observation(out, b31_review)
            if malignance_link:
                link = malignance_link
                matched = dict(malignance_link.get("matchingFailure") or {})
                out["majorIncident"] = dict(matched)
                out["collapseDirect"] = dict(matched)
                checks = list(out.get("checks") or [])
                if not any(str(x.get("key") or "") == "malignance_collapse" for x in checks):
                    checks.append(make_check(
                        key="malignance_collapse",
                        title="악성 · 붕괴 사망",
                        status="failed",
                        evidence_level=EVIDENCE_VERIFIED,
                        summary=matched.get("summary") or malignance_link.get("summary") or "악성 직접 사망이 붕괴와 연결됨",
                        ids={"spellIDs": [_VASHNIK_MALIGNANCE_ID]},
                    ))
                out["checks"] = checks
                timeline = list(out.get("timeline") or [])
                if not any(str(x.get("kind") or "") == "malignance_collapse" for x in timeline):
                    timeline.append(dict(matched))
                    timeline.sort(key=lambda x: float(x.get("sec") or 0))
                out["timeline"] = timeline
                out["presentationOverride"] = {
                    "mode": "verified_collapse_observation",
                    "title": "악성으로 공대 붕괴",
                    "confidence": "high",
                    "sec": matched.get("sec"),
                    "interpretation": malignance_link.get("summary"),
                    "action": "다음 풀: 악성 직전 Malignant Totem 처리 지연과 공대 생존기·후속 피해를 우선 확인하세요. 이 신호만으로 개인 실수는 단정하지 않습니다.",
                    "evidenceLevel": EVIDENCE_VERIFIED,
                    "source": "B31 collapse + BossMechanic calibration",
                }
                out["verdict"] = {
                    "severity": "HIGH",
                    "status": "failed",
                    "evidenceLevel": EVIDENCE_VERIFIED,
                    "primary": "악성 · 공대 붕괴",
                    "why": malignance_link.get("summary"),
                    "nextAction": "다음 풀: 악성 직전 Malignant Totem 처리 지연과 공대 생존기·후속 피해를 확인하세요. 개인 실수는 이 신호만으로 단정하지 않습니다.",
                }
    if out.get("bossKey") == "twin_fangs":
        # B37.3.9.16: link the *matching* verified failure, not merely the
        # earliest failure in a pull that also happened to contain the B31
        # mechanic somewhere else.  This keeps firstMistake separate from
        # majorIncident / collapseDirect in long Mythic pulls.
        exact = _twin_exact_collapse_link(out, b31_review)
        if exact:
            link = exact
            matched = dict(exact.get("matchingFailure") or {})
            if matched:
                out["majorIncident"] = dict(matched)
                out["collapseDirect"] = dict(matched)
                spec = _TWIN_COLLAPSE_SPECS.get(str(matched.get("kind") or "")) or {}
                action = spec.get("action") or "다음 풀: 붕괴 직결 기믹을 우선 제거하세요."
                out["presentationOverride"] = {
                    "mode": "verified_mechanic_collapse",
                    "title": matched.get("label") or matched.get("title") or "검증된 붕괴 직결 기믹",
                    "confidence": "high",
                    "sec": matched.get("sec"),
                    "interpretation": exact.get("summary") or matched.get("summary") or "동일 기믹이 B31 붕괴 구간과 직접 연결됐습니다.",
                    "action": action,
                    "evidenceLevel": EVIDENCE_VERIFIED,
                    "source": "BossMechanic + B31 exact mechanic link",
                }
                # The top-level BossMechanic verdict still describes the first
                # verified failure.  Only append B31 linkage text when that
                # first failure is the same mechanic as the collapse link.
                first = ((out.get("observations") or {}).get("firstVerifiedFailure") or {})
                if str(first.get("kind") or "") == str(matched.get("kind") or ""):
                    v = dict(out.get("verdict") or {})
                    v["severity"] = "HIGH"
                    v["why"] = (str(v.get("why") or "") + " " + str(exact.get("summary") or "")).strip()
                    out["verdict"] = v
    if out.get("bossKey") == "nekzali":
        primary = b31_review.get("primaryFinding") or {}
        trigger_ids = {safe_int(x) for x in (primary.get("trigger_spell_ids") or []) if safe_int(x) is not None}
        ritual_root = 1288772 in trigger_ids or "영혼똬리 의식" in text or "Soulcoil Rite" in text
        if ritual_root:
            out["presentationOverride"] = {
                "mode": "ambient_root_suppressed",
                "title": "붕괴 원인 확인 중",
                "confidence": "medium",
                "sec": verdict.get("sec"),
                "interpretation": "영혼똬리 의식은 정상 킬에서도 반복 관측되는 공대 압박이라 발생 자체를 전멸 원인으로 확정하지 않습니다.",
                "action": "붕괴 시점의 후속 피해·개인 사망·담당 기믹 실패를 우선 확인하세요.",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "source": "BossMechanic calibration",
            }
            link = {
                "status": "context_only",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "summary": "B31의 영혼똬리 의식 다수 사망은 유지하되, 정상 킬 비교로 사용자-facing root headline에서는 제외",
                "b31VerdictTitle": verdict.get("title"),
                "b31VerdictSec": verdict.get("sec"),
            }

        # B37.3.9.12: a generic/ambient B31 root may be earlier than the actual
        # raid collapse.  When the B31 collapse cluster itself says one named
        # mechanic directly killed most of the cluster, surface that *observed
        # collapse event* without inventing a first mistake or assigning player
        # blame.  This is presentation-only; B31's original causal verdict stays
        # untouched.  The current verified case is Haunted Salvo (1292034): in
        # the 2026-10-04 Mythic sample, 5 of the 6 collapse deaths were direct
        # Haunted Salvo deaths.
        collapse = b31_review.get("collapse") or {}
        collapse_spells = collapse.get("spells") if isinstance(collapse.get("spells"), dict) else {}
        try:
            collapse_count = int(collapse.get("count") or 0)
        except (TypeError, ValueError):
            collapse_count = 0
        try:
            haunted_deaths = int(collapse_spells.get("귀신 들린 포화") or collapse_spells.get("Haunted Salvo") or 0)
        except (TypeError, ValueError):
            haunted_deaths = 0
        required = max(3, int(math.ceil(collapse_count * 0.6))) if collapse_count else 3
        if collapse_count >= 4 and haunted_deaths >= required:
            sec = collapse.get("startSec")
            try:
                sec = round(float(sec), 3) if sec is not None else None
            except (TypeError, ValueError):
                sec = None
            # Bind the observed collapse to the nearest preceding Haunted Salvo
            # cast cycle when that timing is present in the same raw/archive.
            # This is a timing anchor, not a player-error verdict: successful
            # Heroic kills also contain the same 1284103 -> 1292034 sequence.
            salvo_cast = ((out.get("observations") or {}).get("hauntedSalvoCast") or {})
            cast_starts = [float(x) for x in (salvo_cast.get("starts") or []) if x is not None]
            cast_completes = [float(x) for x in (salvo_cast.get("completes") or []) if x is not None]
            prior_start = max((x for x in cast_starts if sec is not None and x <= sec), default=None)
            prior_complete = max((x for x in cast_completes if sec is not None and x <= sec), default=None)
            cast_context = None
            if prior_start is not None or prior_complete is not None:
                cast_context = {
                    "startSec": round(prior_start, 3) if prior_start is not None else None,
                    "completeSec": round(prior_complete, 3) if prior_complete is not None else None,
                    "collapseAfterStartSec": round(sec - prior_start, 3) if sec is not None and prior_start is not None else None,
                    "collapseAfterCompleteSec": round(sec - prior_complete, 3) if sec is not None and prior_complete is not None else None,
                    "spellIDs": [1284103, 1292034],
                    "evidenceLevel": EVIDENCE_VERIFIED,
                    "interpretation": "귀신 들린 포화 시전/발사와 붕괴의 시간 관계만 확인하며, 발생 자체를 플레이어 실수로 판정하지 않음",
                }
            incident = {
                "kind": "haunted_salvo_collapse",
                "sec": sec,
                "label": "귀신 들린 포화",
                "title": "귀신 들린 포화",
                "summary": f"붕괴 {collapse_count}명 중 {haunted_deaths}명의 직접 사망 원인",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "status": "failed",
                "directSpellIDs": [1292034],
                "affected": haunted_deaths,
                "collapseDeaths": collapse_count,
                "causalScope": "collapse_direct_deaths",
                "blame": "unassigned",
                "castContext": cast_context,
            }
            out["majorIncident"] = dict(incident)
            out["collapseDirect"] = dict(incident)
            obs = dict(out.get("observations") or {})
            obs["b31CollapseObservation"] = {
                "startSec": sec,
                "deathCount": collapse_count,
                "hauntedSalvoDirectDeaths": haunted_deaths,
                "share": round(haunted_deaths / collapse_count, 3) if collapse_count else None,
                "castContext": cast_context,
                "evidenceLevel": EVIDENCE_VERIFIED,
            }
            out["observations"] = obs
            checks = list(out.get("checks") or [])
            if not any(str(x.get("key") or "") == "haunted_salvo_collapse" for x in checks):
                checks.append(make_check(
                    key="haunted_salvo_collapse",
                    title="귀신 들린 포화 · 붕괴 사망",
                    status="failed",
                    evidence_level=EVIDENCE_VERIFIED,
                    summary=f"붕괴 {collapse_count}명 중 {haunted_deaths}명 직접 사망 · 실패 책임은 이 신호만으로 단정하지 않음",
                    ids={"spellIDs": [1292034]},
                ))
            out["checks"] = checks
            timeline = list(out.get("timeline") or [])
            if not any(str(x.get("kind") or "") == "haunted_salvo_collapse" for x in timeline):
                timeline.append({
                    "kind": "haunted_salvo_collapse",
                    "sec": sec,
                    "title": "귀신 들린 포화 · 공대 붕괴",
                    "label": "귀신 들린 포화",
                    "summary": f"붕괴 {collapse_count}명 중 {haunted_deaths}명 직접 사망",
                    "evidenceLevel": EVIDENCE_VERIFIED,
                    "status": "failed",
                })
                timeline.sort(key=lambda x: float(x.get("sec") or 0))
            out["timeline"] = timeline
            timing_note = ""
            if cast_context and cast_context.get("completeSec") is not None and cast_context.get("collapseAfterCompleteSec") is not None:
                timing_note = f" 포화 시전 완료 {_fmt_time(cast_context.get('completeSec'))} 후 {cast_context.get('collapseAfterCompleteSec'):.1f}초 만에 붕괴가 시작됐습니다."
            out["presentationOverride"] = {
                "mode": "verified_collapse_observation",
                "title": "귀신 들린 포화로 공대 붕괴",
                "confidence": "high",
                "sec": sec,
                "interpretation": f"붕괴 {collapse_count}명 중 {haunted_deaths}명의 직접 사망 원인이 귀신 들린 포화로 확인됐습니다.{timing_note} 이 신호만으로 누구의 실수였는지까지는 단정하지 않습니다.",
                "action": "포화 시전 시작부터 탱커 방향·유령 경로·거리·공생존기 준비를 확인하세요.",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "source": "B31 collapse + BossMechanic calibration",
            }
            # B37.3.9.15: keep the BossMechanic top-level verdict coherent with
            # the verified linked collapse observation.  Previously only the
            # presentation override / slots were promoted, so the detail text
            # could still say "verified 실패 신호 미관측" below a verified
            # Haunted Salvo card.  Recompose the *derived* mechanic verdict
            # without changing B31's stored causal verdict or inventing blame.
            out["verdict"] = {
                "severity": "HIGH",
                "status": "failed",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "primary": "귀신 들린 포화 · 공대 붕괴",
                "why": f"{_fmt_time(sec)}에 붕괴 {collapse_count}명 중 {haunted_deaths}명의 직접 사망 원인이 귀신 들린 포화로 확인됐습니다.{timing_note}",
                "nextAction": "다음 풀: 포화 시전 시작부터 탱커 방향·유령 경로·거리·공생존기 준비를 확인하세요. 이 신호만으로 개인 실수는 단정하지 않습니다.",
            }
            link = {
                "status": "linked",
                "evidenceLevel": EVIDENCE_VERIFIED,
                "summary": f"B31 붕괴 {collapse_count}명 중 귀신 들린 포화 직접 사망 {haunted_deaths}명",
                "b31VerdictTitle": verdict.get("title"),
                "b31VerdictSec": verdict.get("sec"),
                "collapseSec": sec,
                "causal": True,
                "causalScope": "collapse_direct_deaths",
            }

    # B37.4.29: Sszorak uses the canonical B31 collapse start when it is
    # available, but a preceding verified failure is still timing context only.
    if link.get("status") == "unknown" and out.get("bossKey") == "sszorak":
        temporal = _sszorak_verified_temporal_context(out, b31_review)
        if temporal:
            link = temporal
    # B37.3.6: generic timing fallback.  It is intentionally weaker than
    # ``linked`` and never populates collapseDirect in verdict_projection.
    if link.get("status") == "unknown" and out.get("bossKey") in {"twin_fangs", "sszorak", "ulatek"}:
        temporal = _generic_b31_temporal_context(out, b31_review)
        if temporal:
            link = temporal
    if out.get("bossKey") == "sszorak" and link.get("status") != "linked":
        _sszorak_suppress_postcollapse_headline(out, b31_review)
    out["collapseLink"] = link
    if out.get("bossKey") == "twin_fangs":
        _twin_apply_failure_roles(out)
    elif out.get("bossKey") == "buried_sentinel":
        _sentinel_apply_failure_roles(out)
    elif out.get("bossKey") == "nekzali":
        _nekzali_apply_failure_roles(out)
    elif out.get("bossKey") == "lost_explorers":
        _lost_explorers_apply_failure_roles(out)
    elif out.get("bossKey") == "sszorak":
        _sszorak_apply_failure_roles(out)
    elif out.get("bossKey") == "ulatek":
        _ulatek_apply_failure_roles(out)
    return out


def link_saved_review(folder: Path | str) -> dict:
    folder = Path(folder)
    mech_path = folder / "mechanic_review.json"
    b31_path = folder / "raid_review.json"
    if not mech_path.exists() or not b31_path.exists():
        return {}
    try:
        mech = json.loads(mech_path.read_text(encoding="utf-8"))
        b31 = json.loads(b31_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    linked = link_review_to_b31(mech, b31)
    if not linked:
        return {}

    # B37.3.3: project the first *verified* BossMechanic failure into the
    # three presentation slots.  The original B31 verdict/collapseChain remain
    # untouched causal evidence; only presentationVerdict is allowed to own the
    # headline and next-pull instruction.
    slots = mechanic_failure_slots(linked)
    for key, value in slots.items():
        if value is not None:
            linked[key] = value
    presentation = build_presentation_verdict(linked, b31)
    if presentation:
        b31["presentationVerdict"] = presentation
        b31["mechanicFirstMistake"] = slots.get("firstMistake")
        b31["mechanicMajorIncident"] = slots.get("majorIncident")
        b31["mechanicCollapseDirect"] = slots.get("collapseDirect")

    atomic_write_json(mech_path, linked)
    atomic_write_text(folder / "mechanic_review.txt", render_review(linked))
    atomic_write_json(b31_path, b31)
    return {
        "mechanicReviewStatus": "ready",
        "mechanicReviewJson": str(mech_path),
        "mechanicReviewTxt": str(folder / "mechanic_review.txt"),
        "mechanicReviewEngineVersion": linked.get("engineVersion"),
        "mechanicReviewSchemaVersion": linked.get("schemaVersion"),
        "mechanicVerdict": (linked.get("verdict") or {}).get("primary"),
        "mechanicFirstMistake": slots.get("firstMistake"),
        "mechanicMajorIncident": slots.get("majorIncident"),
        "mechanicCollapseDirect": slots.get("collapseDirect"),
        "raidPresentationVerdict": presentation,
    }


def render_review(review: dict) -> str:
    if not review:
        return ""
    verdict = review.get("verdict") or {}
    ev = evidence_label(verdict.get("evidenceLevel"))
    lines = [
        f"=== 이번 트라이 핵심 기믹 · BossMechanic {str(review.get('engineVersion') or '').replace('boss-mechanic-', '')} ===",
        f"[{ev}] [{verdict.get('severity') or 'INFO'}] {verdict.get('primary') or '판정 보류'}",
        str(verdict.get("why") or ""),
        str(verdict.get("nextAction") or ""),
    ]
    link = review.get("collapseLink") or {}
    if link.get("status") == "linked":
        lines.append(f"전멸 연결: [{evidence_label(link.get('evidenceLevel'))}] {link.get('summary')}")
    lines += ["", "--- 기믹 수행 체크 ---"]
    for check in review.get("checks") or []:
        status_map = {"failed": "실패", "success": "성공", "warning": "경고", "unknown": "판정 보류"}
        lines.append(f"- [{evidence_label(check.get('evidenceLevel'))}] {check.get('title')} · {status_map.get(check.get('status'), check.get('status'))}: {check.get('summary')}")
    lines += ["", "--- 기믹 시간축 ---"]
    timeline = review.get("timeline") or []
    if not timeline:
        lines.append("- 관측된 기믹 기준점 없음")
    for item in timeline:
        lines.append(f"- {_fmt_time(item.get('sec'))} [{evidence_label(item.get('evidenceLevel'))}] {item.get('title')} · {item.get('summary')}")

    if review.get("bossKey") == "coiled_altar":
        obs = review.get("observations") or {}
        lines += ["", "--- 똬리 probe 상태 ---"]
        lines.append(f"브레스 {obs.get('breathCount') or 0}회 · 구슬 웨이브 {obs.get('orbWaveCount') or 0}회")
        collapse = obs.get("collapseSnapshot") or {}
        if collapse:
            stage = collapse.get("stage") or {}
            lines.append(
                f"- 첫 사망 {_fmt_time(collapse.get('firstDeathSec'))} · {collapse.get('firstDeathPlayer') or '-'} / "
                f"붕괴 {_fmt_time(collapse.get('collapseSec'))} · {stage.get('label') or '진행 구간 미분류'} · "
                f"1초 내 {collapse.get('collapseCluster1s') or 0}명"
            )
            pressure = collapse.get("raidWidePressureCandidate") or {}
            if pressure:
                lines.append(
                    f"  · 공대 압박 후보: {pressure.get('spellName') or pressure.get('spellID')} "
                    f"({pressure.get('spellID')}) · 대상 {pressure.get('targetCount') or 0}명 · 총피해 {pressure.get('totalDamage') or 0}"
                )
            final = collapse.get("collapseLeadFinalHit") or {}
            if final:
                lines.append(
                    f"  · 붕괴 선두 막타 후보: {final.get('spellName') or final.get('spellID')} "
                    f"({final.get('spellID')}) · {final.get('amount') or 0}"
                )
            chain = collapse.get("collapseBurstChainCandidates") or []
            if chain:
                parts = [
                    f"{_fmt_time(x.get('sec'))} {x.get('spellName') or x.get('spellID')}({x.get('spellID')}) {x.get('targetCount') or 0}명"
                    for x in chain[:4]
                ]
                lines.append("  · 붕괴 연쇄 후보: " + " → ".join(parts))

        lines += ["", "--- 구슬 제거 / active_orbs ---"]
        for row in obs.get("breaths") or []:
            goal = ""
            if row.get("goalMin") is not None and row.get("goalMax") is not None:
                goal = f" · 목표 {row['goalMin']}~{row['goalMax']}개"
                if row.get("goalNote"):
                    goal += f" ({row['goalNote']})"
            signal = row.get("orbRemovalSignal") or {}
            removed = row.get("destroyedOrbs")
            if removed is None:
                removal = "제거 수 확인 불가"
            else:
                removal = f"제거 {removed}개 [검증됨]"
                tc = signal.get("targetCount")
                if tc:
                    removal += f" · 파열 공통값 {tc}명"
            pool = ""
            if row.get("activeOrbsBefore") is not None or row.get("activeOrbsAfter") is not None:
                pool = f" · active {row.get('activeOrbsBefore')}→{row.get('activeOrbsAfter')}"
            status_map = {"success": "성공", "failed": "실패", "unknown": "판정 보류", "excluded": "판정 제외"}
            status = f" · {status_map.get(row.get('status'), row.get('status'))}" if row.get("strategyApplies") else ""
            if row.get("judgmentExcluded"):
                status += f" · {row.get('judgmentExclusionLabel') or '붕괴 후 기믹 · 판정 제외'}"
            risks = row.get("riskFlags") or []
            risk_text = f" · 경고 {','.join(risks)}" if risks else ""
            pulse_diag = f" · 후보 펄스 {row.get('candidateCondensedVenomPulseCount') or 0}회"
            lines.append(f"- {row.get('label')} {_fmt_time(row.get('sec'))}{goal} · {removal}{pool}{status}{risk_text}{pulse_diag}")

        accounting = obs.get("orbAccounting") or {}
        for state in accounting.get("waveStates") or []:
            if state.get("carryInOrbs") is not None and state.get("carryInOrbs") > 0:
                lines.append(
                    f"- {_fmt_time(state.get('sec'))} 새 웨이브 {state.get('waveIndex')} · "
                    f"이전 잔존 {state.get('carryInOrbs')} + 신규 {state.get('spawnedOrbs')} = active {state.get('activeAfter')}"
                )
        for state in accounting.get("guillotineStates") or []:
            remain = state.get("remainingOrbs")
            text = "확인 불가" if remain is None else f"{remain}개"
            status_map = {"success": "성공", "failed": "실패", "unknown": "판정 보류", "excluded": "판정 제외"}
            extra = ""
            if state.get("judgmentExcluded"):
                extra = f" · {(state.get('judgmentGuard') or {}).get('judgmentExclusionLabel') or '붕괴 후 기믹'}"
            lines.append(
                f"- {_fmt_time(state.get('sec'))} 단두대 {state.get('guillotineIndex')} 직전 잔존 · {text} · "
                f"{status_map.get(state.get('status'), state.get('status'))}{extra}"
            )
        flags = accounting.get("flagCounts") or {}
        if flags:
            lines.append(
                "- 구슬 경고 집계(붕괴 후 제외): " + ", ".join(f"{k} {v}회" for k, v in sorted(flags.items()))
            )
        if accounting.get("excludedBreathCount"):
            lines.append(f"- 붕괴/생존 가드로 판정 제외된 브레스: {accounting.get('excludedBreathCount')}회")

        causal = obs.get("orbCausalChainCandidate") or {}
        if causal:
            lines += ["", "--- 구슬 → 붕괴 인과 후보 ---"]
            lines.append(f"- [{evidence_label(causal.get('evidenceLevel'))}] {causal.get('summary')}")
            lines.append(f"  · {causal.get('note')}")

        for cal in obs.get("ruptureBurstCalibration") or []:
            lines.append(
                f"- 단두대 {cal.get('guillotineIndex')} 기준 · 웨이브 {cal.get('anchorOrbWaveIndex')} 소환 {cal.get('spawnedOrbCount')}개 "
                f"(응결 {cal.get('coalescedCount')} + 변이 {cal.get('mutationCount')}) · "
                f"전략 브레스 제거 {cal.get('verifiedRemovedOrbs')} / 합 {cal.get('verifiedRemovedSum')} · "
                f"단두대 잔존 {cal.get('remainingOrbsAtGuillotine')}"
            )
        p2 = obs.get("phase2") or {}
        if p2.get("reached"):
            lines += ["", "--- P2 사이클 종합 · B36.4 ---"]
            p2sum = p2.get("summary") or {}
            if p2sum.get("oneLine"):
                lines.append(f"- {p2sum.get('oneLine')}")
            first = p2sum.get("firstMistake") or {}
            direct = p2sum.get("collapseDirect") or {}
            preceding = p2sum.get("collapsePreceding") or {}
            if first:
                lines.append(
                    f"- 첫 실수: {_fmt_time(first.get('sec'))} · {first.get('label')}"
                    + (f" · {first.get('summary')}" if first.get('summary') else "")
                )
            else:
                lines.append("- 첫 실수: 검증된 실패 미관측")
            if direct:
                lines.append(
                    f"- 붕괴 직결: {_fmt_time(direct.get('sec'))} · {direct.get('label')} → "
                    f"{_fmt_time(p2sum.get('collapseSec'))} 1초 내 {int(p2sum.get('collapseDeathCount1s') or 0)}명 사망 [검증됨]"
                )
            elif p2sum.get("collapseSec") is not None:
                text = "- 붕괴 직결: 직접 인과 근거 없음"
                if preceding:
                    text += f" · 직전 실패 {_fmt_time(preceding.get('sec'))} {preceding.get('label')} (선후관계만 확인)"
                lines.append(text)
            else:
                lines.append("- 붕괴 직결: 1초 내 4명 이상 사망 군집 미관측 · 확인 불가")
            for cyc in p2.get("cycles") or []:
                lines.append(f"- {cyc.get('summaryLine') or ('P2 ' + str(cyc.get('index')) + '사이클')}")
                cfirst = ((cyc.get("judgment") or {}).get("firstMistake") or {})
                if cfirst:
                    lines.append(f"  · 첫 실수 {_fmt_time(cfirst.get('sec'))} {cfirst.get('label')} · {cfirst.get('summary') or ''}".rstrip())
                lines.append("  · 정배: 확인 불가(판정 미승격) · 영혼 3개: 확인 불가(판정 미승격)")
                ints = (cyc.get("wail") or {}).get("interrupts") or []
                if ints:
                    parts = [f"{_fmt_time(x.get('sec'))} {str(x.get('player') or '?').split('-')[0]}" for x in ints[:8]]
                    lines.append("  · 통곡 실제 차단: " + " / ".join(parts))

        disc = obs.get("discoveryOnly") or {}
        if any(disc.values()):
            lines.append(f"ID 발견 후보(판정 미사용): {disc}")
        lines.append(f"동명이인 '절단' 완료 시전 {obs.get('sameNameCleaveCompletedCount') or 0}회 · 보스 브레스 외 제외 {obs.get('ignoredSameNameCleaveCount') or 0}회")

    if review.get("bossKey") == "nekzali":
        obs=review.get("observations") or {}
        grasp=obs.get("graspingDepths") or {}
        curse=obs.get("soulcoilersCurse") or {}
        ex=obs.get("soulExhaustion") or {}
        rite=obs.get("soulcoilRite") or {}
        lines += ["", "--- 영혼살무사 네크잘리 · B37.1.1 ---"]
        lines.append(f"- 심연의 손아귀: 신호 {grasp.get('signalCount') or 0} · 익사한 메아리 소환 {grasp.get('drownedEchoSummonCount') or 0}")
        lines.append(f"- 영혼살무사의 저주: 시작 {curse.get('startCount') or 0} · 차단 {curse.get('interruptCount') or 0} · 완료 {curse.get('completeCount') or 0}")
        lines.append(f"- 영혼 탈진: 적용 {ex.get('applyCount') or 0}회 · {ex.get('uniquePlayers') or 0}명 (배정 없이는 관측 전용)")
        lines.append(f"- 영혼똬리 의식: {rite.get('episodeCount') or 0}회 · Invoke와 실패 원인이 신호를 공유하므로 단독 실패 판정 안 함")
    if review.get("bossKey") == "lost_explorers":
        obs = review.get("observations") or {}
        asc = obs.get("finalAscension") or {}
        relic = obs.get("relicRupture") or {}
        elem = obs.get("elementalExplosion") or {}
        spreading = obs.get("spreadingFlames") or {}
        thud = obs.get("mightyThud") or {}
        defense = obs.get("unitedDefense") or {}
        lines += ["", "--- 길 잃은 탐험가 · B37.4.28 ---"]
        fish_targets = [t for ep in (asc.get('fishEpisodes') or []) for t in (ep.get('targets') or [])]
        lines.append(f"- 최종 승천: 시작 {asc.get('startCount') or 0} · 물고기 전달 {asc.get('fishResetCount') or 0} · 대상 {', '.join(fish_targets[:6]) if fish_targets else '-'}")
        ice = obs.get("iceboundFlames") or {}
        ice_failed = len([x for x in (ice.get('outcomes') or []) if x.get('status') == 'failed'])
        lines.append(f"- 얼음결속 불꽃: 시작 {ice.get('startCount') or 0} · 차단 {ice.get('interruptCount') or 0} · 완료 {ice_failed}")
        lines.append(f"- 상자 방치: Relic Rupture {relic.get('episodeCount') or 0}회")
        lines.append(f"- 서리불꽃: Elemental Explosion {elem.get('episodeCount') or 0}회")
        lines.append(f"- 번지는 불길: 피격 묶음 {spreading.get('episodeCount') or 0}회 · 후보 관측(실패 경계 미확정)")
        for ep in (elem.get("episodes") or [])[:3]:
            carriers = ep.get("activeElementalCarriers") or []
            if carriers:
                lines.append("  · 폭발 당시 잔존 디버프: " + ", ".join(str(x.get("player") or "?") for x in carriers[:8]) + " (책임 단정 안 함)")
        lines.append(f"- Mighty Thud: 대상 신호 {thud.get('targetSignalCount') or 0} · impact 묶음 {len(thud.get('impactEpisodes') or [])} · 관측 전용")
        lines.append(f"- United Defense: 신호 {defense.get('signalCount') or 0} · 위치/지속 검증 전까지 관측 전용")
    if review.get("bossKey") == "vashnik":
        obs = review.get("observations") or {}
        burst = obs.get("malignantBurst") or {}
        bile = obs.get("catalyticBile") or {}
        wave = obs.get("plagueWave") or {}
        malignance = obs.get("malignance") or {}
        infection = obs.get("explodingInfection") or {}
        caustic = obs.get("causticSurge") or {}
        lines += ["", "--- 악성의 바쉬니크 · B37.4.4 ---"]
        lines.append(f"- 악의 파열: {burst.get('episodeCount') or 0}회")
        lines.append(f"- 촉매성 담즙 미흡수: {bile.get('missEpisodeCount') or 0}회")
        lines.append(f"- 역병 파동 플레이어 피격: {wave.get('hitEpisodeCount') or 0}회")
        if malignance.get("mythicApplies"):
            lines.append(f"- Malignance 완료/피해: {malignance.get('episodeCount') or 0}회")
        infection_eps = infection.get("explosionEpisodes") or []
        risky_infection = [x for x in infection_eps if int(x.get('pulseCount') or 0) >= int(infection.get('overlapWarningAt') or 2)]
        lines.append(f"- 폭발 전염병: 폭발 묶음 {len(infection_eps)} · 다중 겹침 후보 {len(risky_infection)}")
        caustic_eps = caustic.get("episodes") or []
        risky_caustic = [x for x in caustic_eps if int(x.get('pulseCount') or 0) >= int(caustic.get('overlapWarningAt') or 2)]
        lines.append(f"- 부식성 쇄도: 폭발 묶음 {len(caustic_eps)} · 다중 겹침 후보 {len(risky_caustic)}")
    if review.get("bossKey") == "sszorak":
        obs = review.get("observations") or {}
        mutilate = obs.get("mutilate") or {}
        cyst = obs.get("viscousCyst") or {}
        cross = obs.get("ragingCrosswinds") or {}
        fury = obs.get("serpentsFury") or {}
        lines += ["", "--- 스조라크 · B37.4.29 ---"]
        if mutilate.get("strikes"):
            lines.append("- Mutilate 적중: " + " / ".join(f"{_fmt_time(x.get('sec'))} {x.get('targetCount')}명" for x in (mutilate.get("strikes") or [])[:12]))
            lines.append(f"  · 신화 실패선 <5명 · 실제 Kill/Wipe + Dungeon Journal 검증")
        else:
            lines.append("- Mutilate: 직접 적중/Gash 인원 신호 없음")
        lines.append(f"- 점성 낭종: 폭발 묶음 {len(cyst.get('episodes') or [])} · 연쇄 폭발 후보 {len(cyst.get('chainEpisodes') or [])}")
        for ep in (cyst.get("chainEpisodes") or [])[:5]:
            lines.append(f"  · {_fmt_time(ep.get('sec'))} pulse {ep.get('pulseCount')} · 직후 사망 {ep.get('deathLinkCount')}명")
        lines.append(f"- 교차의 바람: Gust 적용 {cross.get('applyCount') or 0} · 짝 실패 후보 {len(cross.get('pairMissEpisodes') or [])}")
        for ep in (cross.get("pairMissEpisodes") or [])[:5]:
            why = f"{ep.get('durationSec')}초 유지"
            lines.append(f"  · {_fmt_time(ep.get('sec'))} {ep.get('player')} · {why}")
        if fury.get("mythicApplies"):
            lines.append(f"- 뱀의 격노: 성공 해제 {fury.get('successfulPopCount') or 0} · Unbound Ferocity {fury.get('unboundFerocityEventCount') or 0}")
    if review.get("bossKey") == "twin_fangs":
        obs = review.get("observations") or {}
        venom = obs.get("eternalVenom") or {}
        visceral = obs.get("visceralBurst") or {}
        tainted = obs.get("taintedBlood") or {}
        feast = obs.get("ravenousFeast") or {}
        feast_mass = obs.get("ravenousFeastMassHit") or {}
        brood = obs.get("brood") or {}
        bulwark = obs.get("barbedBulwark") or {}
        lines += ["", "--- 쌍둥이 송곳니 · B37.0.1 ---"]
        if venom.get("available"):
            leaders = ", ".join(f"{x.get('player')} {x.get('maxStack')}" for x in (venom.get("maxima") or [])[:5]) or "-"
            lines.append(f"- 영원의 맹독: 최고 {venom.get('maxStack')}중첩 · 임계 {venom.get('threshold')} · 상위 {leaders}")
        else:
            lines.append("- 영원의 맹독: 중첩 수치 신호 없음")
        lines.append(f"- 내장 파열: 시작 {visceral.get('startCount') or 0} · 차단 {visceral.get('interruptCount') or 0} · 실패 에피소드 {len(visceral.get('failureEpisodes') or [])}")
        lines.append(f"- 오염된 피: 신호 {tainted.get('taintedBloodEventCount') or 0} · 오염된 폭발 {len(tainted.get('burstEpisodes') or [])}회")
        if feast_mass.get("episodes"):
            lines.append("- 걸신들린 탐식 직접피해: " + " / ".join(f"{_fmt_time(x.get('sec'))} {x.get('targetCount')}명" for x in (feast_mass.get("episodes") or [])[:18]))
            if feast_mass.get("fatalEpisodes"):
                lines.append("  · 대규모 즉사 실패: " + " / ".join(f"{_fmt_time(x.get('sec'))} {x.get('targetCount')}명→사망 {x.get('linkedDeathCount')}명" for x in feast_mass.get("fatalEpisodes") or []))
        elif feast.get("strikes"):
            lines.append("- 걸신들린 탐식 적중: " + " / ".join(f"{_fmt_time(x.get('sec'))} {x.get('targetCount')}명" for x in feast.get("strikes") or []))
        else:
            lines.append(f"- 걸신들린 탐식: 적중 인원 직접 신호 없음 · 현재 최소 {feast.get('minTargets') or 4}명")
        if brood.get("waves"):
            lines.append("- 새끼 깨우기: " + " / ".join(f"#{x.get('index')} {x.get('summonCount')}마리" for x in brood.get("waves") or []) + f" · 현재 기준 {brood.get('expectedCount')}마리")
        lines.append(f"- 가시 보루 실제 차단: {bulwark.get('interruptCount') or 0}회")
    lines += ["", "※ 근거 수준: 검증됨 = 실제 트라이/규칙으로 확인, 후보 = 신호는 있으나 의미 미확정, 확인 불가 = 현재 로그/ID로 판정 불가."]
    return "\n".join(lines).rstrip() + "\n"


def write_review(folder: Path | str, review: dict | None) -> dict:
    folder = Path(folder)
    if not review:
        return {"mechanicReviewStatus": "unsupported"}
    json_path = folder / "mechanic_review.json"
    txt_path = folder / "mechanic_review.txt"
    atomic_write_json(json_path, review)
    atomic_write_text(txt_path, render_review(review))
    p2_summary = ((((review.get("observations") or {}).get("phase2") or {}).get("summary") or {}))
    first = p2_summary.get("firstMistake") or {}
    direct = p2_summary.get("collapseDirect") or {}
    generic_first = ((review.get("observations") or {}).get("firstVerifiedFailure") or {})
    first_text = (f"{_fmt_time(first.get('sec'))} {first.get('label')}" if first else (f"{_fmt_time(generic_first.get('sec'))} {generic_first.get('label')}" if generic_first else None))
    return {
        "mechanicReviewStatus": "ready",
        "mechanicReviewJson": str(json_path),
        "mechanicReviewTxt": str(txt_path),
        "mechanicReviewEngineVersion": review.get("engineVersion"),
        "mechanicReviewSchemaVersion": review.get("schemaVersion"),
        "mechanicVerdict": (review.get("verdict") or {}).get("primary"),
        "mechanicSummaryLine": p2_summary.get("oneLine") or (review.get("verdict") or {}).get("primary"),
        "mechanicFirstMistake": first_text,
        "mechanicCollapseDirect": (f"{_fmt_time(direct.get('sec'))} {direct.get('label')}" if direct else None),
    }
