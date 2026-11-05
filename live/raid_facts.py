from __future__ import annotations

import json
import math
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path

from .encounter_tracker import parse_combat_datetime, parse_log_line
from . import progress as _live_progress
from .rawlog import resolve_raw, open_raw_text, resolve_preroll, open_preroll_text
from .io_utils import atomic_write_json, atomic_write_text
from .fact_extractor import (
    DAMAGE_EVENTS,
    _advanced_target_hp,
    _damage_overkill,
    _event_amount,
    _fmt_amount,
    _fmt_sec,
    _relative_sec,
    _safe_int,
    _spell_info,
)

from .versions import ENGINE_VERSION, APP_BUILD
MODULE_VERSION = f"RaidWipeReview-{ENGINE_VERSION}"

from .mechanics import KNOWN_SPEC_IDS, canonical
from .death_incidents import collect_event, build_incident_analysis

# v0.3-A is deliberately a factual raid-wide extractor.  It does NOT decide
# who made a mistake or why the raid wiped.  Those judgments come in v0.3-B.

CAST_EVENTS = {"SPELL_CAST_START", "SPELL_CAST_SUCCESS"}
DEATH_EVENTS = {"UNIT_DIED", "UNIT_DESTROYED", "SPELL_INSTAKILL"}
FEIGN_DEATH_SPELL_ID = 5384
FEIGN_DEATH_WINDOW_SEC = 3.0
# RC4.1: a death right after Feign Death is only a *candidate*.  It is dropped
# only when the same player visibly acts again (no resurrection in between)
# within FEIGN_CONFIRM_SEC.  Without that evidence the death is preserved.
FEIGN_CONFIRM_SEC = 30.0
FEIGN_CAST_EVIDENCE = frozenset({"SPELL_CAST_SUCCESS", "SPELL_CAST_START"})
FEIGN_MELEE_EVIDENCE = frozenset({"SWING_DAMAGE", "SWING_MISSED", "RANGE_DAMAGE", "RANGE_MISSED"})
FEIGN_MELEE_MIN_GAP_SEC = 1.5  # an in-flight auto shot may land just after a real death
DISPEL_EVENTS = {"SPELL_DISPEL", "SPELL_STOLEN"}

# CombatLog object flags used only as an extra hostile/friendly hint.
AFFILIATION_MINE = 0x1
AFFILIATION_PARTY = 0x2
AFFILIATION_RAID = 0x4
REACTION_FRIENDLY = 0x10
REACTION_HOSTILE = 0x40
TYPE_PLAYER = 0x400
TYPE_PET = 0x1000
TYPE_GUARDIAN = 0x2000

PULL_MISS_EVENTS = {"SPELL_MISSED", "RANGE_MISSED", "SWING_MISSED"}
PULL_AURA_EVENTS = {"SPELL_AURA_APPLIED"}
PET_SUMMON_EVENTS = {"SPELL_SUMMON", "SPELL_CREATE"}

# Tank taunts are valid hostile-target pull candidates.  Mark them explicitly so
# the UI can render `어둠의 명령 (도발)` instead of making the reader infer it.
TAUNT_SPELL_IDS = {355, 6795, 56222, 62124, 116189, 185245}

# First-pass cross-class registry.  Unknown spells are never discarded from raw.log;
# this table only decides which casts get promoted into the compact "major cooldown"
# summary.  The registry is intentionally easy to extend as live raid samples arrive.
MAJOR_OFFENSIVE_IDS = {
    # Rogue
    1856: "소멸", 185313: "어둠의 춤", 121471: "어둠의 칼날",
    360194: "죽음표식", 385627: "왕의 파멸", 381623: "엉겅퀴 차",
    280719: "은밀한 기술", 1297761: "탐식을 일삼는 울라텍의 심장",
    # Warrior
    1719: "무모함", 107574: "투신", 227847: "칼날폭풍",
    # Paladin
    31884: "응징의 격노", 231895: "성전",
    # Death Knight
    51271: "얼음 기둥", 47568: "룬 무기 강화", 42650: "사자의 군대", 315443: "누더기골렘 팔",
    # Druid
    194223: "천체의 정렬", 102543: "화신: 밀림의 왕", 102558: "화신: 우르속의 수호자",
    # Hunter
    19574: "야수의 격노", 288613: "정조준", 359844: "야생의 부름", 266779: "협공",
    # Mage
    190319: "발화", 12472: "얼음 핏줄", 365350: "비전 쇄도", 80353: "시간 왜곡",
    # Monk
    137639: "폭풍과 대지와 불", 152173: "평온", 123904: "쉬엔의 원령 소환",
    # Priest
    10060: "마력 주입", 228260: "공허 방출", 200183: "절정",
    # Shaman
    114050: "승천", 114051: "승천", 114052: "승천", 51533: "야수 정령", 198067: "불의 정령",
    # Warlock
    1122: "지옥불정령 소환", 205180: "암흑시선 소환", 265187: "악마 폭군 소환",
    # Demon Hunter
    191427: "탈태", 187827: "탈태", 370965: "사냥",
    # Evoker
    375087: "용의 분노", 403631: "영겁의 숨결", 370553: "비늘사령관의 지배",
}

MAJOR_DEFENSIVE_IDS = {
    # Rogue
    1966: "교란", 31224: "그림자 망토", 5277: "회피", 185311: "진홍색 약병",
    # Warrior
    871: "방패의 벽", 12975: "최후의 저항", 97462: "재집결의 함성", 23920: "주문 반사", 184364: "격노의 재생력",
    # Paladin
    642: "천상의 보호막", 498: "신의 가호", 31850: "헌신적인 수호자", 86659: "고대 왕의 수호자",
    1022: "보호의 축복", 6940: "희생의 축복", 31821: "오라 숙련",
    # Death Knight
    48707: "대마법 보호막", 48792: "얼음같은 인내력", 55233: "흡혈", 51052: "대마법 지대",
    # Druid
    22812: "나무껍질", 61336: "생존 본능", 22842: "광포한 재생력", 102342: "무쇠껍질", 740: "평온",
    # Hunter
    186265: "거북의 상", 109304: "활기", 264735: "적자생존",
    # Mage
    45438: "얼음 방패", 55342: "환영 복제",
    # Monk
    122278: "해악 감퇴", 122783: "마법 해소", 115203: "강화주", 116849: "기의 고치", 115310: "재활",
    # Priest
    33206: "고통 억제", 47788: "수호 영혼", 62618: "신의 권능: 방벽", 64843: "천상의 찬가",
    47585: "분산", 19236: "구원의 기도",
    # Shaman
    108271: "영혼 이동", 98008: "정신의 고리 토템", 108280: "치유의 해일 토템",
    # Warlock
    104773: "영원한 결의", 108416: "어둠의 서약",
    # Demon Hunter
    198589: "흐릿해지기", 196555: "황천걸음",
    # Evoker
    363916: "흑요석 비늘", 374348: "소생의 불길", 374227: "산들바람", 363534: "되감기",
}

POTION_KEYWORDS = ("물약", "potion", "ポーション", "药水", "藥水", "poción", "poção")


def _relative_sec_signed(ts: str, start_dt: datetime | None):
    """Relative timestamp that preserves a small pre-pull negative offset.

    The older shared fact extractor intentionally clamps negatives to zero.  B32
    keeps that behavior for every existing analysis path, but pull-start evidence
    needs to distinguish "observed after ENCOUNTER_START" from a genuine pre-start
    hit when such a line is present in the raw source.
    """
    if start_dt is None:
        return None
    dt = parse_combat_datetime(ts, now=start_dt)
    if not dt:
        return None
    sec = (dt - start_dt).total_seconds()
    if sec < -43200:
        sec += 86400
    return round(sec, 4)


def _fmt_signed_sec(sec):
    if sec is None:
        return "-"
    try:
        value = float(sec)
    except Exception:
        return "-"
    if value < 0:
        return f"{value:.2f}s"
    return _fmt_sec(value)

# Live exact-cast capture follows the selector catalog produced by the cooldown/NSRT
# builder.  These core rogue IDs are only a safe bootstrap when a fresh install has
# not generated data/live_selector_catalog.json yet.  Dynamic items such as potions
# are learned from the persisted selector catalog instead of being hard-coded here.
FALLBACK_SELECTOR_SPELLS = {
    1856: {"name": "소멸", "kind": "spell", "specIDs": [259, 261]},
    360194: {"name": "죽음표식", "kind": "spell", "specIDs": [259]},
    385627: {"name": "왕의 파멸", "kind": "spell", "specIDs": [259]},
    426591: {"name": "피아귀의 이빨", "displayNameKo": "피아귀의 이빨", "kind": "spell", "specIDs": [261]},
    # B29: verified combat-potion variants are not Subtlety abilities. Keep them
    # in the bootstrap so Live exact-cast capture works before a dynamic selector
    # catalog has been generated. Empty specIDs means raid-wide selector capture.
    1236616: {"name": "빛의 잠재력", "displayNameKo": "빛의 잠재력", "kind": "potion", "specIDs": []},
    1236994: {"name": "무모함의 물약", "displayNameKo": "무모함의 물약", "kind": "potion", "specIDs": []},
    1295247: {"name": "농축된 실버문 생명력 물약", "kind": "defensive_potion", "specIDs": []},
    121471: {"name": "어둠의 칼날", "kind": "spell", "specIDs": [261]},
    185313: {"name": "어둠의 춤", "kind": "spell", "specIDs": [261]},
}
SELECTOR_RAW_EVENTS = {"SPELL_CAST_SUCCESS", "SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH"}


def _selector_catalog_file() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "live_selector_catalog.json"


def _load_selector_catalog():
    rows = {int(sid): {"spellID": int(sid), **dict(meta)} for sid, meta in FALLBACK_SELECTOR_SPELLS.items()}
    source = "builtin-fallback"
    path = _selector_catalog_file()
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            loaded = payload.get("spells") if isinstance(payload, dict) else None
            if isinstance(loaded, list):
                for item in loaded:
                    sid = _safe_int(item.get("spellID")) if isinstance(item, dict) else None
                    if sid is None:
                        continue
                    display_ko = str(item.get("displayNameKo") or "").strip()
                    rows[int(sid)] = {
                        "spellID": int(sid),
                        "name": display_ko or item.get("name") or rows.get(int(sid), {}).get("name") or str(sid),
                        "displayNameKo": display_ko or rows.get(int(sid), {}).get("displayNameKo"),
                        "kind": item.get("kind") or rows.get(int(sid), {}).get("kind") or "spell",
                        "specIDs": list(item.get("specIDs") or rows.get(int(sid), {}).get("specIDs") or []),
                        "groups": list(item.get("groups") or []),
                    }
                # B29 migration guard: older persisted catalogs may still mark
                # Light's Potential as a Subtlety spell. Verified potion IDs win
                # over stale catalog metadata so Live capture is correct immediately
                # after overwrite, even before cooldown data is regenerated.
                for sid in (1236616, 1236994):
                    if sid in rows:
                        rows[sid]["kind"] = "potion"
                        rows[sid]["specIDs"] = []
                if 1295247 in rows:
                    rows[1295247]["kind"] = "defensive_potion"
                    rows[1295247]["specIDs"] = []
                source = "data/live_selector_catalog.json"
        except Exception:
            # A broken optional catalog must not break raid-facts extraction.  The
            # fallback IDs remain active and the source makes the degraded mode visible.
            source = "builtin-fallback (catalog read failed)"
    return rows, source


def _contains_hangul(text: str | None) -> bool:
    return any(0xAC00 <= ord(ch) <= 0xD7A3 for ch in str(text or ""))


def _persist_observed_selector_display_names(selector_rows: list[dict]):
    """Remember spellID -> Korean client name for future WCL-only analysis.

    The shared selector catalog is deliberately not packaged in overwrite ZIPs, so
    this enriches the user's existing data/live_selector_catalog.json in place.
    Only Hangul observations are written to displayNameKo; English/localized logs
    never overwrite a Korean name already learned.
    """
    path = _selector_catalog_file()
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False

    learned = {}
    for row in selector_rows or []:
        sid = _safe_int(row.get("spellID"))
        if sid is None:
            continue
        observed = [str(x).strip() for x in (row.get("observedSpellNames") or []) if str(x).strip()]
        ko = next((x for x in observed if _contains_hangul(x)), None)
        if ko:
            learned[int(sid)] = ko
    if not learned:
        return False

    changed = False
    for item in payload.get("spells") or []:
        sid = _safe_int(item.get("spellID")) if isinstance(item, dict) else None
        if sid in learned and item.get("displayNameKo") != learned[sid]:
            item["displayNameKo"] = learned[sid]
            changed = True
    catalogs = payload.get("specCatalogs") if isinstance(payload.get("specCatalogs"), dict) else {}
    for block in catalogs.values():
        for item in (block or {}).get("spells") or []:
            sid = _safe_int(item.get("spellID")) if isinstance(item, dict) else None
            if sid in learned and item.get("displayNameKo") != learned[sid]:
                item["displayNameKo"] = learned[sid]
                changed = True
    if changed:
        payload["schemaVersion"] = max(2, int(payload.get("schemaVersion") or 1))
        payload["displayNamesUpdatedAt"] = datetime.now().isoformat(timespec="seconds")
        atomic_write_json(path, payload)
    return changed


def _dedupe_aura_fallback(rows: list[dict], window_sec: float = 0.45):
    """Collapse multi-target/duplicate AURA_APPLIED rows into one use per actor/time."""
    out = []
    last_by_actor = {}
    for row in sorted(rows, key=lambda x: (str(x.get("playerGUID") or ""), float(x.get("sec") or 0.0))):
        actor = str(row.get("playerGUID") or "")
        sec = float(row.get("sec") or 0.0)
        prev = last_by_actor.get(actor)
        if prev is not None and sec - prev <= window_sec:
            continue
        last_by_actor[actor] = sec
        out.append(row)
    out.sort(key=lambda x: float(x.get("sec") or 0.0))
    return out


def _build_selector_exact_casts(catalog: dict[int, dict], event_rows: dict, names_by_guid: dict):
    result = []
    for sid in sorted(catalog):
        meta = catalog[sid]
        buckets = event_rows.get(sid) or {}
        cast_rows = list(buckets.get("SPELL_CAST_SUCCESS") or [])
        aura_rows = list(buckets.get("SPELL_AURA_APPLIED") or [])
        refresh_rows = list(buckets.get("SPELL_AURA_REFRESH") or [])
        if cast_rows:
            selected_type = "SPELL_CAST_SUCCESS"
            selected_rows = sorted(cast_rows, key=lambda x: float(x.get("sec") or 0.0))
            dedupe_window = None
        elif aura_rows:
            selected_type = "SPELL_AURA_APPLIED"
            selected_rows = _dedupe_aura_fallback(aura_rows)
            dedupe_window = 0.45
        else:
            selected_type = None
            selected_rows = []
            dedupe_window = None

        per_player_raw = defaultdict(Counter)
        for event_name, rows in (("SPELL_CAST_SUCCESS", cast_rows), ("SPELL_AURA_APPLIED", aura_rows), ("SPELL_AURA_REFRESH", refresh_rows)):
            for row in rows:
                guid = row.get("playerGUID")
                if guid:
                    per_player_raw[guid][event_name] += 1
        selected_by_player = defaultdict(list)
        for row in selected_rows:
            guid = row.get("playerGUID")
            if guid:
                selected_by_player[guid].append(float(row.get("sec") or 0.0))

        players = []
        all_guids = set(per_player_raw) | set(selected_by_player)
        for guid in sorted(all_guids, key=lambda g: _short_player_name(names_by_guid.get(g) or g)):
            rc = per_player_raw.get(guid) or Counter()
            times = sorted(selected_by_player.get(guid) or [])
            players.append({
                "playerGUID": guid,
                "playerName": names_by_guid.get(guid) or guid,
                "count": len(times),
                "times": [round(x, 3) for x in times],
                "rawCounts": {
                    "SPELL_CAST_SUCCESS": int(rc.get("SPELL_CAST_SUCCESS", 0)),
                    "SPELL_AURA_APPLIED": int(rc.get("SPELL_AURA_APPLIED", 0)),
                    "SPELL_AURA_REFRESH": int(rc.get("SPELL_AURA_REFRESH", 0)),
                },
            })

        observed = Counter()
        for rows in (cast_rows, aura_rows, refresh_rows):
            for row in rows:
                if row.get("spellName"):
                    observed[str(row.get("spellName"))] += 1
        observed_names = [name for name, _n in observed.most_common(5)]
        observed_ko = next((name for name in observed_names if _contains_hangul(name)), None)
        result.append({
            "spellID": int(sid),
            "spellName": observed_ko or meta.get("displayNameKo") or meta.get("name") or str(sid),
            "displayNameKo": observed_ko or meta.get("displayNameKo"),
            "kind": meta.get("kind") or "spell",
            "specIDs": list(meta.get("specIDs") or []),
            "selectedEventType": selected_type,
            "selectedCount": len(selected_rows),
            "dedupeWindowSec": dedupe_window,
            "rawCounts": {
                "SPELL_CAST_SUCCESS": len(cast_rows),
                "SPELL_AURA_APPLIED": len(aura_rows),
                "SPELL_AURA_REFRESH": len(refresh_rows),
            },
            "observedSpellNames": observed_names,
            "events": selected_rows,
            "players": players,
        })
    return result


# Encounter-specific priority burn windows.  These are not generic blame rules; they
# identify boss-defined targets/windows where the raid is expected to have damage
# cooldowns available.  v0.3-A1 starts with Ulratek's Venomous Heart because the
# live-test requirement is explicit and the raw combat log exposes a stable actor/spell.
PRIORITY_BURN_RULES = {
    3492: [
        {
            "label": "맹독의 심장",
            "actorName": "맹독의 심장",
            "spawnSpellID": 1299526,
            "preWindowSec": 30.0,
            "fallbackDurationSec": 20.0,
            "note": "울라텍은 심장 노출 구간이 우선 딜 구간입니다. 심장 직전 쫄에 주요 공격 쿨기를 소모했는지 확인합니다.",
        }
    ]
}


def _flag_int(value) -> int:
    try:
        text = str(value or "0").strip()
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except Exception:
        return 0


def _is_player_guid(guid: str | None) -> bool:
    return str(guid or "").startswith("Player-")


def _is_hostile(flags_value) -> bool:
    return bool(_flag_int(flags_value) & REACTION_HOSTILE)


def _is_raid_affiliated(flags_value) -> bool:
    flags = _flag_int(flags_value)
    return bool(flags & (AFFILIATION_MINE | AFFILIATION_PARTY | AFFILIATION_RAID)) and not bool(flags & REACTION_HOSTILE)


def _is_pet_or_guardian(guid: str | None, flags_value) -> bool:
    if _is_player_guid(guid):
        return False
    flags = _flag_int(flags_value)
    return str(guid or "").startswith("Pet-") or bool(flags & (TYPE_PET | TYPE_GUARDIAN))


def _pull_candidate_kind(event: str, fields: list[str], extra_start: int) -> str | None:
    if event in DAMAGE_EVENTS:
        return "damage"
    if event == "SPELL_CAST_SUCCESS":
        return "cast_success"
    if event in PULL_MISS_EVENTS:
        return "miss"
    if event in PULL_AURA_EVENTS:
        aura_type = str(fields[extra_start] if len(fields) > extra_start else "").strip().upper()
        if aura_type in {"", "DEBUFF"}:
            return "aura"
    return None


def _advanced_owner_guid(fields: list[str], extra_start: int, source_guid: str | None, dest_guid: str | None) -> str | None:
    """Best-effort owner lookup for pet/guardian source rows.

    Modern Advanced Combat Logging can expose actor/owner GUIDs immediately after
    the common spell/swing fields.  We only consult this for a non-player source
    attacking a hostile non-player target, so a Player-* value here is safe to use
    as the controlling player when present.
    """
    if _is_player_guid(source_guid) or _is_player_guid(dest_guid):
        return None
    for idx in (extra_start + 1, extra_start):
        if len(fields) > idx and _is_player_guid(fields[idx]):
            return fields[idx]
    return None


def _actor_key(row: dict) -> str:
    return str(row.get("playerGUID") or row.get("sourceGUID") or row.get("playerName") or row.get("sourceName") or "?")


def _clean_name(name: str | None) -> str:
    text = str(name or "").strip().strip('"')
    return text or "?"


def _short_player_name(name: str | None) -> str:
    text = _clean_name(name)
    # Keep names readable while preserving full name in JSON elsewhere.
    return text.split("-")[0] if "-" in text else text


def _extra_spell(fields: list[str]):
    if len(fields) < 15:
        return None, None
    return _safe_int(fields[12]), fields[13]


def _bucket_simultaneous(rows: list[dict], window: float = 0.25, min_players: int = 3):
    by_spell = defaultdict(list)
    for row in rows:
        key = (row.get("spellID"), row.get("spellName"), row.get("sourceGUID"), row.get("sourceName"))
        by_spell[key].append(row)

    clusters = []
    for key, vals in by_spell.items():
        vals.sort(key=lambda r: float(r.get("sec") or 0.0))
        current = []
        last_sec = None
        for row in vals:
            sec = row.get("sec")
            if sec is None:
                continue
            sec = float(sec)
            if current and last_sec is not None and sec - last_sec > window:
                clusters.append((key, current))
                current = []
            current.append(row)
            last_sec = sec
        if current:
            clusters.append((key, current))

    out = []
    for (spell_id, spell_name, source_guid, source_name), vals in clusters:
        unique = {}
        for r in vals:
            unique[r.get("destGUID") or r.get("destName")] = r
        if len(unique) < min_players:
            continue
        sample = list(unique.values())
        amounts = [float(x.get("amount") or 0.0) for x in sample]
        hp_pcts = [float(x["hpPct"]) for x in sample if x.get("hpPct") is not None]
        out.append({
            "sec": round(min(float(x.get("sec") or 0.0) for x in sample), 3),
            "spellID": spell_id,
            "spellName": spell_name or "근접/기타 피해",
            "sourceGUID": source_guid,
            "sourceName": _clean_name(source_name),
            "playerCount": len(sample),
            "players": sorted(_short_player_name(x.get("destName")) for x in sample),
            "totalDamage": round(sum(amounts), 3),
            "avgDamage": round(sum(amounts) / len(amounts), 3) if amounts else 0.0,
            "maxDamage": round(max(amounts), 3) if amounts else 0.0,
            "minHpPctAfter": round(min(hp_pcts), 2) if hp_pcts else None,
        })
    out.sort(key=lambda x: (float(x.get("sec") or 0.0), -int(x.get("playerCount") or 0)))
    return out


def _infer_combatant_spec(fields: list[str] | None):
    if not fields:
        return None
    # Variable stat prefixes exist: the first talent array anchors the spec field.
    for i, value in enumerate(fields):
        if str(value).lstrip().startswith("["):
            sid = _safe_int(fields[i - 1]) if i else None
            return sid if sid in KNOWN_SPEC_IDS else None
    return None



def _load_preroll_context(raw_path: Path, encounter: dict) -> dict:
    """Load optional pre-pull coverage metadata without changing raw analysis."""
    meta = {}
    meta_path = raw_path.parent / "meta.json"
    if meta_path.exists():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:
            meta = {}
    def num(key, default=0.0):
        try:
            return float(encounter.get(key) if encounter.get(key) is not None else meta.get(key, default))
        except Exception:
            return float(default)
    ppath = resolve_preroll(raw_path.parent)
    return {
        "path": ppath if ppath.exists() else None,
        "prerollSec": max(0.0, num("prerollSec", 0.0)),
        "windowSec": max(0.1, num("prerollWindowSec", 10.0)),
        "lineCount": int(encounter.get("prerollLineCount") or meta.get("prerollLineCount") or 0),
        "maxLines": int(meta.get("prerollMaxLines") or 50000),
        "boundaryReason": encounter.get("prerollBoundaryReason") or meta.get("prerollBoundaryReason"),
        "boundaryTimestamp": meta.get("prerollBoundaryTimestamp"),
    }


def _extract_preroll_evidence(
    preroll_ctx: dict,
    start_dt: datetime | None,
    selector_catalog: dict,
    names_by_guid: dict,
    pet_owner_guid: dict,
    pet_owner_name: dict,
):
    """Read preroll only for pull-start evidence and offensive pre-pot casts.

    Nothing returned here is fed into deaths, damage, targets, roster, incident
    chains, cooldown totals, or any other existing encounter analysis.
    """
    path = preroll_ctx.get("path")
    if path is None or start_dt is None:
        return [], []

    candidates = []
    potion_rows = defaultdict(lambda: defaultdict(list))
    local_pet_owner = dict(pet_owner_guid)
    local_pet_name = dict(pet_owner_name)
    order = 0
    try:
        fh = open_preroll_text(path)
    except OSError:
        return [], []
    with fh:
        for raw in fh:
            parsed = parse_log_line(raw)
            if not parsed:
                continue
            sec = _relative_sec_signed(parsed.get("timestamp"), start_dt)
            if sec is None or sec >= 0:
                continue
            fields = parsed.get("fields") or []
            event = str(parsed.get("event") or "")
            source_guid = fields[1] if len(fields) > 1 else ""
            source_name = fields[2] if len(fields) > 2 else ""
            source_flags = fields[3] if len(fields) > 3 else "0"
            dest_guid = fields[5] if len(fields) > 5 else ""
            dest_name = fields[6] if len(fields) > 6 else ""
            dest_flags = fields[7] if len(fields) > 7 else "0"
            spell_id, spell_name, extra_start = _spell_info(event, fields)

            if event in PET_SUMMON_EVENTS and _is_player_guid(source_guid) and dest_guid and not _is_player_guid(dest_guid):
                local_pet_owner[dest_guid] = source_guid
                local_pet_name[dest_guid] = names_by_guid.get(source_guid) or _clean_name(source_name)

            pull_kind = _pull_candidate_kind(event, fields, extra_start)
            source_is_player = _is_player_guid(source_guid)
            source_is_pet = _is_pet_or_guardian(source_guid, source_flags) or source_guid in local_pet_owner
            owner_guid = source_guid if source_is_player else local_pet_owner.get(source_guid)
            if owner_guid is None and source_is_pet:
                owner_guid = _advanced_owner_guid(fields, extra_start, source_guid, dest_guid)
                if owner_guid:
                    local_pet_owner[source_guid] = owner_guid
            source_is_raid_pet = source_is_pet and bool(owner_guid or _is_raid_affiliated(source_flags))
            if pull_kind and dest_guid and _is_hostile(dest_flags) and (source_is_player or source_is_raid_pet):
                owner_name = (names_by_guid.get(owner_guid) if owner_guid else None) or local_pet_name.get(source_guid)
                order += 1
                row = {
                    "sec": sec,
                    "timestamp": parsed.get("timestamp"),
                    "event": event,
                    "eventKind": pull_kind,
                    "order": -1000000 + order,
                    "playerGUID": owner_guid or source_guid,
                    "playerName": owner_name or names_by_guid.get(source_guid) or _clean_name(source_name),
                    "sourceGUID": source_guid or None,
                    "sourceName": _clean_name(source_name) if source_guid else None,
                    "actorType": "player" if source_is_player else "pet",
                    "ownerKnown": bool(source_is_player or owner_guid),
                    "targetGUID": dest_guid,
                    "targetName": _clean_name(dest_name),
                    "spellID": spell_id,
                    "spellName": spell_name or ("근접 공격" if event.startswith("SWING_") else event),
                    "isTaunt": bool(spell_id in TAUNT_SPELL_IDS),
                    "evidenceSource": "preroll",
                }
                if event in PULL_AURA_EVENTS:
                    row["auraType"] = str(fields[extra_start] if len(fields) > extra_start else "") or None
                candidates.append(row)

            # Plan-vs-Actual is allowed to consume only offensive combat potions
            # from preroll. Defensive potions and ordinary cooldowns stay scoped to
            # raw.log so preroll cannot alter existing cooldown/fixture analysis.
            item = selector_catalog.get(int(spell_id)) if spell_id is not None else None
            if item and str(item.get("kind") or "").lower() == "potion" and event in SELECTOR_RAW_EVENTS:
                actor_guid = source_guid if _is_player_guid(source_guid) else None
                actor_name = names_by_guid.get(source_guid) or _clean_name(source_name)
                actor_side = "source"
                if actor_guid is None and event in {"SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH"} and _is_player_guid(dest_guid):
                    actor_guid = dest_guid
                    actor_name = names_by_guid.get(dest_guid) or _clean_name(dest_name)
                    actor_side = "dest-fallback"
                if actor_guid is not None:
                    potion_rows[(int(spell_id), actor_guid)][event].append({
                        "sec": sec,
                        "event": event,
                        "playerGUID": actor_guid,
                        "playerName": actor_name,
                        "actorSide": actor_side,
                        "spellID": int(spell_id),
                        "spellName": spell_name or item.get("name") or str(spell_id),
                        "evidenceSource": "preroll",
                    })

    potions = []
    for (sid, guid), by_event in potion_rows.items():
        selected_event = "SPELL_CAST_SUCCESS" if by_event.get("SPELL_CAST_SUCCESS") else "SPELL_AURA_APPLIED"
        rows = by_event.get(selected_event) or []
        for row in rows:
            potions.append({**row, "selectedEventType": selected_event})
    potions.sort(key=lambda x: (float(x.get("sec") or 0.0), int(x.get("spellID") or 0)))
    return candidates, potions


def extract_raid_facts(raw_path: Path | str, encounter: dict, death_lookback_sec: float = 8.0,
                       persist_display_names: bool = True) -> dict:
    raw_path = Path(raw_path)
    start_ts = str(encounter.get("startTimestamp") or "")
    start_dt = parse_combat_datetime(start_ts) if start_ts else None

    names_by_guid: dict[str, str] = {}
    combatant_rows: dict[str, list[str]] = {}
    player_guids: set[str] = set()
    player_cast_counts = defaultdict(Counter)
    selector_catalog, selector_catalog_source = _load_selector_catalog()
    selector_ids = set(selector_catalog)
    selector_event_rows = defaultdict(lambda: defaultdict(list))

    raid_deaths = []
    recent_damage = defaultdict(deque)
    all_player_damage = []
    simultaneous_candidates = []
    major_offensive = []
    major_defensive = []
    interrupts = []
    dispels = []
    resurrections = []
    player_returns = []
    # RC4.1: Hunter Feign Death (5384) can be logged as a player UNIT_DIED.  A
    # death within FEIGN_DEATH_WINDOW_SEC after FD is kept as a normal death but
    # marked as a candidate; it is removed only on positive evidence (the player
    # acts again with no resurrection in between).  Uncertain -> keep the death.
    feign_death_at: dict[str, float] = {}
    feign_deaths: list[dict] = []
    feign_candidates: dict[str, dict] = {}
    dead_since: dict[str, float] = {}
    enemy_casts = []
    hostile_seen: dict[str, dict] = {}
    hostile_deaths: dict[str, float] = {}
    target_first_hit: dict[str, dict] = {}
    player_hostile_attacks: list[dict] = []
    pull_start_candidates: list[dict] = []
    pet_owner_guid: dict[str, str] = {}
    pet_owner_name: dict[str, str] = {}
    pull_event_order = 0
    priority_markers: list[dict] = []

    incident_events = []
    parsed_lines = 0

    raw_path = resolve_raw(raw_path)
    preroll_ctx = _load_preroll_context(raw_path, encounter)
    preroll_potions = []
    _lp = _live_progress.file_progress(raw_path, "parse")
    with open_raw_text(raw_path) as fh:
        for raw in fh:
            _lp.tick(raw)
            parsed = parse_log_line(raw)
            if not parsed:
                continue
            parsed_lines += 1
            event = parsed["event"]
            fields = parsed["fields"]
            sec = _relative_sec(parsed["timestamp"], start_dt)

            if event == "COMBATANT_INFO" and len(fields) > 1:
                guid = fields[1]
                if _is_player_guid(guid):
                    player_guids.add(guid)
                    combatant_rows[guid] = fields
                continue

            source_guid = fields[1] if len(fields) > 1 else ""
            source_name = fields[2] if len(fields) > 2 else ""
            source_flags = fields[3] if len(fields) > 3 else "0"
            dest_guid = fields[5] if len(fields) > 5 else ""
            dest_name = fields[6] if len(fields) > 6 else ""
            dest_flags = fields[7] if len(fields) > 7 else "0"

            if _is_player_guid(source_guid):
                player_guids.add(source_guid)
                if source_name:
                    names_by_guid[source_guid] = _clean_name(source_name)
            if _is_player_guid(dest_guid):
                player_guids.add(dest_guid)
                if dest_name:
                    names_by_guid[dest_guid] = _clean_name(dest_name)

            # Earliest observation of hostile non-player actors.
            for guid, name, flags in ((source_guid, source_name, source_flags), (dest_guid, dest_name, dest_flags)):
                if guid and not _is_player_guid(guid) and _is_hostile(flags):
                    row = hostile_seen.get(guid)
                    if row is None or (sec is not None and (row.get("firstSeenSec") is None or sec < row.get("firstSeenSec"))):
                        hostile_seen[guid] = {
                            "guid": guid,
                            "name": _clean_name(name),
                            "firstSeenSec": sec,
                        }

            spell_id, spell_name, extra_start = _spell_info(event, fields)
            if (spell_id == FEIGN_DEATH_SPELL_ID and sec is not None
                    and event in ("SPELL_CAST_SUCCESS", "SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH")):
                fd_guid = source_guid if event == "SPELL_CAST_SUCCESS" else dest_guid
                if _is_player_guid(fd_guid):
                    feign_death_at[fd_guid] = float(sec)

            # RC4.1: resolve open Feign Death candidates.
            if feign_candidates and sec is not None:
                if event == "SPELL_RESURRECT" and dest_guid in feign_candidates:
                    feign_candidates.pop(dest_guid, None)  # someone rezzed them -> real death
                cand = feign_candidates.get(source_guid) if source_guid else None
                if cand is not None:
                    gap = float(sec) - float(cand["sec"])
                    if gap > FEIGN_CONFIRM_SEC:
                        feign_candidates.pop(source_guid, None)
                    elif ((event in FEIGN_CAST_EVIDENCE and gap > 0.0)
                            or (event in FEIGN_MELEE_EVIDENCE and gap >= FEIGN_MELEE_MIN_GAP_SEC)):
                        feign_candidates.pop(source_guid, None)
                        cand["_feignConfirmed"] = True
                        dead_since.pop(source_guid, None)
                        feign_deaths.append({
                            "sec": cand["sec"],
                            "guid": source_guid,
                            "name": cand.get("name"),
                            "feignDeathSec": cand.get("feignDeathSec"),
                            "evidenceSec": float(sec),
                            "evidenceEvent": event,
                            "evidenceSpellID": spell_id,
                            "evidenceSpellName": spell_name or None,
                        })

            # B32.1: learn player -> pet/guardian ownership when the summon is visible.
            # A future preroll buffer will make this available for more pre-pull pets;
            # Advanced Combat Logging owner GUIDs are also used as a fallback below.
            if event in PET_SUMMON_EVENTS and _is_player_guid(source_guid) and dest_guid and not _is_player_guid(dest_guid):
                pet_owner_guid[dest_guid] = source_guid
                pet_owner_name[dest_guid] = names_by_guid.get(source_guid) or _clean_name(source_name)

            # Pull-start evidence is broader than damage.  A hostile-target cast can
            # initiate the pull before its projectile lands; misses and hostile debuff
            # auras (e.g. taunt) are also valid evidence.  Player-controlled pets and
            # guardians are candidates too.
            pull_kind = _pull_candidate_kind(event, fields, extra_start)
            source_is_player = _is_player_guid(source_guid)
            source_is_pet = _is_pet_or_guardian(source_guid, source_flags) or source_guid in pet_owner_guid
            owner_guid = source_guid if source_is_player else pet_owner_guid.get(source_guid)
            if owner_guid is None and source_is_pet:
                owner_guid = _advanced_owner_guid(fields, extra_start, source_guid, dest_guid)
                if owner_guid:
                    pet_owner_guid[source_guid] = owner_guid
            source_is_raid_pet = source_is_pet and bool(owner_guid or _is_raid_affiliated(source_flags))
            if (
                pull_kind
                and dest_guid
                and _is_hostile(dest_flags)
                and (source_is_player or source_is_raid_pet)
            ):
                owner_name = (
                    names_by_guid.get(owner_guid)
                    if owner_guid
                    else None
                ) or pet_owner_name.get(source_guid)
                if owner_guid and owner_name:
                    names_by_guid.setdefault(owner_guid, owner_name)
                pull_event_order += 1
                candidate = {
                    "sec": _relative_sec_signed(parsed["timestamp"], start_dt),
                    "timestamp": parsed.get("timestamp"),
                    "event": event,
                    "eventKind": pull_kind,
                    "order": pull_event_order,
                    "playerGUID": owner_guid or source_guid,
                    "playerName": owner_name or names_by_guid.get(source_guid) or _clean_name(source_name),
                    "sourceGUID": source_guid or None,
                    "sourceName": _clean_name(source_name) if source_guid else None,
                    "actorType": "player" if source_is_player else "pet",
                    "ownerKnown": bool(source_is_player or owner_guid),
                    "targetGUID": dest_guid,
                    "targetName": _clean_name(dest_name),
                    "spellID": spell_id,
                    "spellName": spell_name or ("근접 공격" if event.startswith("SWING_") else event),
                    "isTaunt": bool(spell_id in TAUNT_SPELL_IDS),
                }
                if event in PULL_AURA_EVENTS:
                    candidate["auraType"] = str(fields[extra_start] if len(fields) > extra_start else "") or None
                pull_start_candidates.append(candidate)

            if sec is not None and sec >= 0:
                captured = collect_event(event, fields, sec, spell_id, spell_name, extra_start)
                if captured is not None:
                    incident_events.append(captured)

            # B11 live exact-cast raw capture.  Keep CAST/AURA/REFRESH separately and
            # choose one basis only after the pull: CAST_SUCCESS wins; AURA_APPLIED is
            # a fallback only when that spell produced no cast events at all. REFRESH
            # is never counted as a use and is preserved only for diagnosis.
            if spell_id in selector_ids and event in SELECTOR_RAW_EVENTS:
                actor_guid = source_guid if _is_player_guid(source_guid) else None
                actor_name = names_by_guid.get(source_guid) or _clean_name(source_name)
                actor_side = "source"
                if actor_guid is None and event in {"SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH"} and _is_player_guid(dest_guid):
                    actor_guid = dest_guid
                    actor_name = names_by_guid.get(dest_guid) or _clean_name(dest_name)
                    actor_side = "dest-fallback"
                if actor_guid is not None:
                    selector_event_rows[int(spell_id)][event].append({
                        "sec": sec,
                        "event": event,
                        "playerGUID": actor_guid,
                        "playerName": actor_name,
                        "actorSide": actor_side,
                        "spellID": int(spell_id),
                        "spellName": spell_name or selector_catalog.get(int(spell_id), {}).get("name") or "?",
                        "sourceGUID": source_guid or None,
                        "sourceName": _clean_name(source_name) if source_guid else None,
                        "destGUID": dest_guid or None,
                        "destName": _clean_name(dest_name) if dest_guid else None,
                    })

            # Encounter-defined priority window markers.  For Ulratek the Heart actor
            # applies/removes spell 1299526; using the aura makes this independent of
            # whether a particular player managed to hit the target.
            for rule in PRIORITY_BURN_RULES.get(_safe_int(encounter.get("encounterID")), []):
                if spell_id == rule.get("spawnSpellID") and (
                    _clean_name(source_name) == rule.get("actorName")
                    or _clean_name(dest_name) == rule.get("actorName")
                    or str(spell_name or "") == rule.get("actorName")
                ):
                    if event == "SPELL_AURA_APPLIED":
                        priority_markers.append({
                            "kind": "start", "sec": sec, "guid": source_guid or dest_guid,
                            "label": rule.get("label"), "rule": rule,
                        })
                    elif event == "SPELL_AURA_REMOVED":
                        priority_markers.append({
                            "kind": "end", "sec": sec, "guid": source_guid or dest_guid,
                            "label": rule.get("label"), "rule": rule,
                        })

            # Combat resurrection facts.  Raid Wipe Review B1 uses these to avoid
            # treating a previously resurrected tank as still dead when tracing a
            # later tank-gap raid explosion.
            if event == "SPELL_RESURRECT" and _is_player_guid(dest_guid):
                # SPELL_RESURRECT is only the resurrection *cast/offer*.  It does not
                # prove that the target accepted and resumed play.  B2 therefore keeps
                # the cast as an attempt and records the actual return separately when
                # the dead player later produces a SPELL_CAST_SUCCESS of their own.
                resurrections.append({
                    "sec": sec,
                    "sourceGUID": source_guid or None,
                    "sourceName": names_by_guid.get(source_guid) or _clean_name(source_name),
                    "targetGUID": dest_guid,
                    "targetName": names_by_guid.get(dest_guid) or _clean_name(dest_name),
                    "spellID": spell_id,
                    "spellName": spell_name or "?",
                    "returnSec": None,
                    "returnSpellID": None,
                    "returnSpellName": None,
                })

            # Player deaths + a factual lookback of incoming damage.
            if event in DEATH_EVENTS and len(fields) > 6:
                if _is_player_guid(dest_guid):
                    # SPELL_INSTAKILL followed by UNIT_DIED is one death episode.
                    if dest_guid in dead_since:
                        continue
                    fd_sec = feign_death_at.get(dest_guid)
                    is_feign_candidate = (fd_sec is not None and sec is not None
                                          and 0.0 <= float(sec) - fd_sec <= FEIGN_DEATH_WINDOW_SEC)
                    history = list(recent_damage.get(dest_guid) or [])
                    if sec is not None:
                        history = [x for x in history if x.get("sec") is not None and float(x["sec"]) >= float(sec) - death_lookback_sec]
                    final_hit = history[-1] if history else None
                    dmg_2s = sum(float(x.get("amount") or 0.0) for x in history if sec is not None and x.get("sec") is not None and float(x["sec"]) >= float(sec) - 2.0)
                    dmg_8s = sum(float(x.get("amount") or 0.0) for x in history)
                    raid_deaths.append({
                        "sec": sec,
                        "timestamp": parsed["timestamp"],
                        "guid": dest_guid,
                        "name": names_by_guid.get(dest_guid) or _clean_name(dest_name),
                        "lastHit": final_hit,
                        "damage2s": round(dmg_2s, 3),
                        "damage8s": round(dmg_8s, 3),
                        "recentDamage": history[-10:],
                    })
                    if is_feign_candidate:
                        raid_deaths[-1]["feignDeathSec"] = fd_sec
                        feign_candidates[dest_guid] = raid_deaths[-1]
                        feign_death_at.pop(dest_guid, None)
                    if sec is not None:
                        dead_since[dest_guid] = float(sec)
                elif dest_guid and _is_hostile(dest_flags):
                    hostile_deaths[dest_guid] = sec
                continue

            # Hostile damage against human raid members.
            if event in DAMAGE_EVENTS and _is_player_guid(dest_guid):
                amount = _event_amount(event, fields, extra_start)
                hp, max_hp = _advanced_target_hp(fields, extra_start)
                row = {
                    "sec": sec,
                    "event": event,
                    "sourceGUID": source_guid,
                    "sourceName": _clean_name(source_name),
                    "destGUID": dest_guid,
                    "destName": names_by_guid.get(dest_guid) or _clean_name(dest_name),
                    "spellID": spell_id,
                    "spellName": spell_name or ("근접 공격" if event.startswith("SWING_") else event),
                    "amount": amount,
                    "overkill": _damage_overkill(fields, extra_start),
                    "currentHP": hp,
                    "maxHP": max_hp,
                    "hpPct": round(hp / max_hp * 100.0, 2) if hp is not None and max_hp and max_hp > 0 else None,
                    "hostileSource": _is_hostile(source_flags),
                }
                canonical_id, canonical_name = canonical(encounter.get('encounterID'), spell_id, row['spellName'])
                row.update({'canonicalSpellID': canonical_id, 'canonicalMechanic': canonical_name})
                dq = recent_damage[dest_guid]
                dq.append(row)
                if sec is not None:
                    while dq and dq[0].get("sec") is not None and float(dq[0]["sec"]) < float(sec) - max(12.0, death_lookback_sec + 2.0):
                        dq.popleft()
                all_player_damage.append(row)
                if row["hostileSource"]:
                    simultaneous_candidates.append(row)

            # Player damage to hostile actors => raid target switch facts.
            if event in DAMAGE_EVENTS and _is_player_guid(source_guid) and dest_guid and _is_hostile(dest_flags):
                attack_sec = _relative_sec_signed(parsed["timestamp"], start_dt)
                attack_row = {
                    "sec": attack_sec,
                    "timestamp": parsed.get("timestamp"),
                    "event": event,
                    "playerGUID": source_guid,
                    "playerName": names_by_guid.get(source_guid) or _clean_name(source_name),
                    "targetGUID": dest_guid,
                    "targetName": _clean_name(dest_name),
                    "spellID": spell_id,
                    "spellName": spell_name or ("근접 공격" if event.startswith("SWING_") else event),
                    "amount": _event_amount(event, fields, extra_start),
                }
                player_hostile_attacks.append(attack_row)
                key = dest_guid
                row = target_first_hit.get(key)
                if row is None:
                    target_first_hit[key] = {
                        "guid": dest_guid,
                        "name": _clean_name(dest_name),
                        "firstSeenSec": (hostile_seen.get(dest_guid) or {}).get("firstSeenSec"),
                        "firstRaidHitSec": sec,
                        "firstHitPlayer": names_by_guid.get(source_guid) or _clean_name(source_name),
                        "hitPlayers": set(),
                        "playerFirstHits": {},
                        "hitCount": 0,
                        "damage": 0.0,
                    }
                    row = target_first_hit[key]
                row["hitPlayers"].add(source_guid)
                if source_guid not in row.get("playerFirstHits", {}):
                    row.setdefault("playerFirstHits", {})[source_guid] = {
                        "sec": sec,
                        "playerName": names_by_guid.get(source_guid) or _clean_name(source_name),
                    }
                row["hitCount"] += 1
                row["damage"] += float(_event_amount(event, fields, extra_start) or 0.0)

            # Human-player casts: keep a compact catalog plus recognized major CDs.
            if event == "SPELL_CAST_SUCCESS" and _is_player_guid(source_guid):
                pname = names_by_guid.get(source_guid) or _clean_name(source_name)

                # A resurrection becomes "real" only when the previously dead player
                # acts again.  This mirrors the older beta6 signal and avoids treating
                # a SPELL_RESURRECT offer that was never accepted as a completed rez.
                if source_guid in dead_since and sec is not None and float(sec) > float(dead_since[source_guid]):
                    death_sec = float(dead_since[source_guid])
                    ret = {
                        "sec": float(sec),
                        "playerGUID": source_guid,
                        "playerName": pname,
                        "deathSec": death_sec,
                        "spellID": spell_id,
                        "spellName": spell_name or "?",
                        "resurrectionCastSec": None,
                    }
                    for rr in reversed(resurrections):
                        if rr.get("targetGUID") != source_guid:
                            continue
                        rr_sec = rr.get("sec")
                        if rr_sec is None or float(rr_sec) < death_sec or float(rr_sec) > float(sec):
                            continue
                        if rr.get("returnSec") is None:
                            rr["returnSec"] = float(sec)
                            rr["returnSpellID"] = spell_id
                            rr["returnSpellName"] = spell_name or "?"
                            ret["resurrectionCastSec"] = float(rr_sec)
                            break
                    player_returns.append(ret)
                    dead_since.pop(source_guid, None)

                player_cast_counts[source_guid][(spell_id, spell_name or "?")] += 1
                lower = str(spell_name or "").lower()
                base = {
                    "sec": sec,
                    "playerGUID": source_guid,
                    "playerName": pname,
                    "spellID": spell_id,
                    "spellName": spell_name or "?",
                    "targetGUID": dest_guid,
                    "targetName": _clean_name(dest_name) if dest_guid else None,
                }
                if spell_id in MAJOR_OFFENSIVE_IDS or any(k in lower for k in POTION_KEYWORDS):
                    item = dict(base)
                    item["spellName"] = MAJOR_OFFENSIVE_IDS.get(spell_id, spell_name or "물약")
                    major_offensive.append(item)
                if spell_id in MAJOR_DEFENSIVE_IDS:
                    item = dict(base)
                    item["spellName"] = MAJOR_DEFENSIVE_IDS[spell_id]
                    major_defensive.append(item)

            if event == "SPELL_INTERRUPT" and _is_player_guid(source_guid):
                extra_id, extra_name = _extra_spell(fields)
                interrupts.append({
                    "sec": sec,
                    "playerGUID": source_guid,
                    "playerName": names_by_guid.get(source_guid) or _clean_name(source_name),
                    "targetGUID": dest_guid,
                    "targetName": _clean_name(dest_name),
                    "spellID": spell_id,
                    "spellName": spell_name or "?",
                    "interruptedSpellID": extra_id,
                    "interruptedSpellName": extra_name or "?",
                })

            if event in DISPEL_EVENTS and _is_player_guid(source_guid):
                extra_id, extra_name = _extra_spell(fields)
                dispels.append({
                    "sec": sec,
                    "playerGUID": source_guid,
                    "playerName": names_by_guid.get(source_guid) or _clean_name(source_name),
                    "targetGUID": dest_guid,
                    "targetName": _clean_name(dest_name),
                    "spellID": spell_id,
                    "spellName": spell_name or "?",
                    "removedSpellID": extra_id,
                    "removedSpellName": extra_name or "?",
                    "event": event,
                })

            # Hostile cast timeline.  Friendly followers/pets are excluded by flags.
            if event in CAST_EVENTS and source_guid and not _is_player_guid(source_guid) and _is_hostile(source_flags):
                enemy_casts.append({
                    "sec": sec,
                    "event": event,
                    "sourceGUID": source_guid,
                    "sourceName": _clean_name(source_name),
                    "spellID": spell_id,
                    "spellName": spell_name or "?",
                    "targetGUID": dest_guid or None,
                    "targetName": _clean_name(dest_name) if dest_guid else None,
                })

    _lp.close()

    # RC4.1: only deaths with positive Feign Death evidence are removed.
    feign_unresolved = [
        {"sec": d.get("sec"), "guid": d.get("guid"), "name": d.get("name"),
         "feignDeathSec": d.get("feignDeathSec")}
        for d in raid_deaths if d.get("feignDeathSec") is not None and not d.get("_feignConfirmed")
    ]
    raid_deaths = [d for d in raid_deaths if not d.get("_feignConfirmed")]

    # B33.1: optional preroll is deliberately parsed in a side channel. Only pull
    # evidence and offensive pre-pot events are merged; all legacy encounter facts
    # above remain based solely on raw.log.
    pre_candidates, preroll_potions = _extract_preroll_evidence(
        preroll_ctx, start_dt, selector_catalog, names_by_guid, pet_owner_guid, pet_owner_name
    )
    pull_start_candidates.extend(pre_candidates)

    _live_progress.enter("mechanics")
    # Enrich roster with spec ID if COMBATANT_INFO was present.
    roster = []
    for guid in sorted(player_guids, key=lambda g: _short_player_name(names_by_guid.get(g) or g)):
        roster.append({
            "guid": guid,
            "name": names_by_guid.get(guid) or guid,
            "specID": _infer_combatant_spec(combatant_rows.get(guid)),
        })

    # Number repeated deaths for the same player; this is factual only (battle rez may exist).
    death_counts = defaultdict(int)
    for row in raid_deaths:
        death_counts[row.get("guid")] += 1
        row["deathNo"] = death_counts[row.get("guid")]

    simultaneous = _bucket_simultaneous(simultaneous_candidates, window=0.25, min_players=3)

    # Compact enemy-cast aggregation, while keeping exact times for later v0.3-B matching.
    enemy_groups = defaultdict(list)
    for row in enemy_casts:
        key = (row.get("sourceName"), row.get("spellID"), row.get("spellName"), row.get("event"))
        if row.get("sec") is not None:
            enemy_groups[key].append(row.get("sec"))
    enemy_cast_summary = []
    for (source_name, spell_id, spell_name, event), times in enemy_groups.items():
        enemy_cast_summary.append({
            "sourceName": source_name,
            "spellID": spell_id,
            "spellName": spell_name,
            "event": event,
            "count": len(times),
            "times": [round(float(x), 3) for x in times[:30]],
        })
    enemy_cast_summary.sort(key=lambda x: (-x["count"], x["spellName"]))

    targets = []
    enc_name = str(encounter.get("name") or "")
    for guid, row in target_first_hit.items():
        seen = row.get("firstSeenSec")
        first_hit = row.get("firstRaidHitSec")
        death_sec = hostile_deaths.get(guid)
        targets.append({
            "guid": guid,
            "name": row.get("name") or "?",
            "isEncounterName": bool(enc_name and (row.get("name") == enc_name)),
            "firstSeenSec": seen,
            "firstRaidHitSec": first_hit,
            "switchDelayFromFirstSeenSec": round(float(first_hit) - float(seen), 3) if first_hit is not None and seen is not None else None,
            "firstHitPlayer": row.get("firstHitPlayer"),
            "uniquePlayersHit": len(row.get("hitPlayers") or set()),
            "playerFirstHits": [
                {"playerGUID": pg, "playerName": data.get("playerName"), "sec": data.get("sec")}
                for pg, data in sorted((row.get("playerFirstHits") or {}).items(), key=lambda kv: float((kv[1] or {}).get("sec") or 1e12))
            ],
            "hitCount": row.get("hitCount") or 0,
            "damage": round(float(row.get("damage") or 0.0), 3),
            "deathSec": death_sec,
            "lifetimeFromFirstSeenSec": round(float(death_sec) - float(seen), 3) if death_sec is not None and seen is not None else None,
            "timeFromFirstHitToDeathSec": round(float(death_sec) - float(first_hit), 3) if death_sec is not None and first_hit is not None else None,
        })
    targets.sort(key=lambda x: (float(x.get("firstRaidHitSec") if x.get("firstRaidHitSec") is not None else 1e12), x.get("name") or ""))

    # B32.1: one fight-level pull-start fact.  Candidate evidence includes direct
    # damage, hostile-target CAST_SUCCESS, misses, hostile debuff auras, and
    # player-controlled pet/guardian attacks.  Near-simultaneous evidence is reduced
    # to one earliest row per controlling player so one actor cannot fill the list.
    for row in pull_start_candidates:
        if row.get("actorType") == "pet" and not row.get("ownerKnown"):
            owner_guid = pet_owner_guid.get(str(row.get("sourceGUID") or ""))
            if owner_guid:
                row["playerGUID"] = owner_guid
                row["playerName"] = names_by_guid.get(owner_guid) or pet_owner_name.get(str(row.get("sourceGUID") or "")) or row.get("playerName")
                row["ownerKnown"] = True

    attacks = sorted(
        [x for x in pull_start_candidates if x.get("sec") is not None],
        key=lambda x: (float(x.get("sec") or 0.0), int(x.get("order") or 0)),
    )
    first_attack = attacks[0] if attacks else None
    near_attacks = []
    puller_confirmed = False
    ambiguity = None
    puller_lead_sec = None
    if first_attack is not None:
        first_sec = float(first_attack.get("sec") or 0.0)
        window_rows = [x for x in attacks if float(x.get("sec") or 0.0) - first_sec <= 0.35]
        seen_actors = set()
        for x in window_rows:
            key = _actor_key(x)
            if key in seen_actors:
                continue
            seen_actors.add(key)
            near_attacks.append(x)
            if len(near_attacks) >= 8:
                break
        first_key = _actor_key(first_attack)
        other_players = [x for x in attacks if _actor_key(x) != first_key]
        if other_players:
            puller_lead_sec = round(float(other_players[0].get("sec") or 0.0) - first_sec, 3)
        simultaneous_other = any(
            _actor_key(x) != first_key
            and abs(float(x.get("sec") or 0.0) - first_sec) <= 0.05
            for x in near_attacks[1:]
        )
        if first_sec < -0.05 and not simultaneous_other:
            puller_confirmed = True
        elif first_sec >= -0.05:
            ambiguity = "partial-preroll-no-prestart-attack" if 0 < float(preroll_ctx.get("prerollSec") or 0.0) < float(preroll_ctx.get("windowSec") or 10.0) - 0.05 else "no-prestart-evidence"
        elif simultaneous_other:
            ambiguity = "near-simultaneous-prestart-attacks"

    def _public_pull_row(row):
        if not row:
            return None
        out = dict(row)
        out.pop("order", None)
        return out

    pull_start = {
        "displayMode": "puller" if puller_confirmed else "first_observed_attack",
        "pullerConfirmed": bool(puller_confirmed),
        "puller": _public_pull_row(first_attack) if puller_confirmed else None,
        "firstObservedAttack": _public_pull_row(first_attack),
        "nearSimultaneousAttacks": [_public_pull_row(x) for x in near_attacks],
        "nearSimultaneousWindowSec": 0.35,
        "pullerLeadSec": puller_lead_sec,
        "ambiguity": ambiguity,
        "evidenceBoundary": "prestart-evidence" if (first_attack and float(first_attack.get("sec") or 0.0) < 0) else "encounter-start-or-later",
        "candidatePolicy": "damage + hostile-target cast success + miss + hostile debuff aura + raid pet/guardian",
        "prerollAvailable": bool(preroll_ctx.get("path")),
        "prerollSec": round(float(preroll_ctx.get("prerollSec") or 0.0), 3),
        "prerollWindowSec": float(preroll_ctx.get("windowSec") or 10.0),
        "prerollLineCount": int(preroll_ctx.get("lineCount") or 0),
        "prerollMaxLines": int(preroll_ctx.get("maxLines") or 50000),
        "prerollBoundaryReason": preroll_ctx.get("boundaryReason"),
        "prerollCoverage": (
            "full" if float(preroll_ctx.get("prerollSec") or 0.0) >= float(preroll_ctx.get("windowSec") or 10.0) - 0.05
            else "partial" if float(preroll_ctx.get("prerollSec") or 0.0) > 0
            else "none"
        ),
    }

    # Build encounter-specific priority burn windows and audit raid major cooldowns
    # around them.  A pre-window cast is only marked as a *risk/candidate* when the
    # same spell has no recorded cast during the priority window; talents/charges and
    # assignment differences can still make the cast valid, so v0.3-A1 does not call
    # it an error by itself.
    priority_windows = []
    rules = PRIORITY_BURN_RULES.get(_safe_int(encounter.get("encounterID")), [])
    for rule in rules:
        starts = [m for m in priority_markers if m.get("kind") == "start" and m.get("label") == rule.get("label") and m.get("sec") is not None]
        ends = [m for m in priority_markers if m.get("kind") == "end" and m.get("label") == rule.get("label") and m.get("sec") is not None]
        starts.sort(key=lambda x: float(x.get("sec") or 0.0))
        ends.sort(key=lambda x: float(x.get("sec") or 0.0))
        for idx, st in enumerate(starts, 1):
            start_sec = float(st.get("sec"))
            end_sec = None
            for en in ends:
                es = float(en.get("sec"))
                if es >= start_sec and (st.get("guid") is None or en.get("guid") == st.get("guid")):
                    end_sec = es
                    break
            if end_sec is None:
                end_sec = start_sec + float(rule.get("fallbackDurationSec") or 20.0)
            pre = float(rule.get("preWindowSec") or 30.0)
            before = [c for c in major_offensive if c.get("sec") is not None and start_sec - pre <= float(c.get("sec")) < start_sec]
            during = [c for c in major_offensive if c.get("sec") is not None and start_sec <= float(c.get("sec")) <= end_sec]
            before_rows = []
            for c in before:
                same_during = any(
                    d.get("playerGUID") == c.get("playerGUID") and d.get("spellID") == c.get("spellID")
                    for d in during
                )
                active_adds = []
                csec = float(c.get("sec") or 0.0)
                for t in targets:
                    tname = str(t.get("name") or "")
                    if t.get("isEncounterName") or tname == rule.get("actorName"):
                        continue
                    first = t.get("firstSeenSec") if t.get("firstSeenSec") is not None else t.get("firstRaidHitSec")
                    death = t.get("deathSec")
                    if first is None:
                        continue
                    # If no death marker exists, only treat a recently observed add as
                    # active to avoid carrying stale actors through the entire fight.
                    alive = (death is not None and float(first) <= csec <= float(death) + 0.5) or (death is None and float(first) <= csec <= float(first) + 30.0)
                    if alive:
                        active_adds.append(tname)
                before_rows.append({
                    **c,
                    "secondsBeforePriority": round(start_sec - csec, 3),
                    "sameSpellCastDuringPriority": same_during,
                    "priorityRiskCandidate": not same_during,
                    "activeAdds": sorted(set(active_adds)),
                })

            heart_target = next((t for t in targets if str(t.get("name") or "") == rule.get("actorName") and t.get("firstRaidHitSec") is not None and abs(float(t.get("firstSeenSec") or start_sec)-start_sec) <= 3.0), None)
            priority_windows.append({
                "index": idx,
                "label": rule.get("label"),
                "actorName": rule.get("actorName"),
                "startSec": round(start_sec, 3),
                "endSec": round(end_sec, 3),
                "durationSec": round(end_sec - start_sec, 3),
                "preWindowSec": pre,
                "note": rule.get("note"),
                "preMajorCooldowns": before_rows,
                "duringMajorCooldowns": during,
                "riskCandidates": [x for x in before_rows if x.get("priorityRiskCandidate")],
                "target": heart_target,
            })

    cast_catalog = []
    for guid, counts in player_cast_counts.items():
        spells = [
            {"spellID": sid, "spellName": name, "count": count}
            for (sid, name), count in counts.most_common()
        ]
        cast_catalog.append({
            "playerGUID": guid,
            "playerName": names_by_guid.get(guid) or guid,
            "spells": spells[:80],
        })
    cast_catalog.sort(key=lambda x: _short_player_name(x.get("playerName")))

    selector_exact_casts = _build_selector_exact_casts(selector_catalog, selector_event_rows, names_by_guid)
    if persist_display_names:  # RC4.2: read-only previews pass False
        _persist_observed_selector_display_names(selector_exact_casts)
    selector_selected_count = sum(int(x.get("selectedCount") or 0) for x in selector_exact_casts)
    selector_fallback_count = sum(1 for x in selector_exact_casts if x.get("selectedEventType") == "SPELL_AURA_APPLIED")

    unique_deaths = len({x.get("guid") for x in raid_deaths if x.get("guid")})
    facts = {
        "schemaVersion": 1,
        "engineVersion": ENGINE_VERSION,
        "appBuild": APP_BUILD,
        "moduleVersion": MODULE_VERSION,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "encounter": {
            "encounterID": encounter.get("encounterID"),
            "name": encounter.get("name"),
            "difficultyID": encounter.get("difficultyID"),
            "difficultyName": encounter.get("difficultyName"),
            "groupSize": encounter.get("groupSize"),
            "tryNo": encounter.get("tryNo"),
            "result": encounter.get("result"),
            "durationSec": encounter.get("durationSec"),
            "startTimestamp": encounter.get("startTimestamp"),
            "endTimestamp": encounter.get("endTimestamp"),
            "analysisOverride": encounter.get("analysisOverride"),
        },
        "summary": {
            "parsedLines": parsed_lines,
            "humanPlayersSeen": len(roster),
            "deathEvents": len(raid_deaths),
            "uniquePlayersDied": unique_deaths,
            "simultaneousDamageGroups": len(simultaneous),
            "majorOffensiveCasts": len(major_offensive),
            "majorDefensiveCasts": len(major_defensive),
            "interrupts": len(interrupts),
            "dispels": len(dispels),
            "resurrections": len(resurrections),
            "playerReturns": len(player_returns),
            "hostileTargetsHit": len(targets),
            "priorityBurnWindows": len(priority_windows),
            "priorityCooldownRiskCandidates": sum(len(x.get("riskCandidates") or []) for x in priority_windows),
            "selectorTrackedSpells": len(selector_exact_casts),
            "selectorExactEvents": selector_selected_count,
            "selectorAuraFallbackSpells": selector_fallback_count,
            "pullStartAttackObserved": bool(first_attack),
            "pullerConfirmed": bool(puller_confirmed),
        },
        "pullStart": pull_start,
        "prePull": {
            "prerollSec": round(float(preroll_ctx.get("prerollSec") or 0.0), 3),
            "prerollWindowSec": float(preroll_ctx.get("windowSec") or 10.0),
            "prerollLineCount": int(preroll_ctx.get("lineCount") or 0),
            "prerollBoundaryReason": preroll_ctx.get("boundaryReason"),
            "combatPotions": preroll_potions,
            "consumerPolicy": "pull-start evidence + Plan-vs-Actual offensive pre-pot only",
        },
        "roster": roster,
        "deaths": raid_deaths,
        "feignDeathsIgnored": feign_deaths,
        "feignDeathsKeptUncertain": feign_unresolved,
        "simultaneousDamage": simultaneous[-100:],
        "majorOffensiveCooldowns": major_offensive,
        "majorDefensiveCooldowns": major_defensive,
        "interrupts": interrupts,
        "dispels": dispels,
        "resurrections": resurrections,
        "playerReturns": player_returns,
        "targets": targets[:100],
        "priorityBurnWindows": priority_windows,
        "enemyCastSummary": enemy_cast_summary[:120],
        "playerCastCatalog": cast_catalog,
        "selectorCatalog": {
            "source": selector_catalog_source,
            "spellCount": len(selector_exact_casts),
            "policy": "SPELL_CAST_SUCCESS first; SPELL_AURA_APPLIED fallback only when cast count is zero; SPELL_AURA_REFRESH diagnostic only",
        },
        "selectorExactCasts": selector_exact_casts,
        "limitations": [
            "v0.3-A는 공대 전체 관측 사실만 추출하며 실수/전멸 원인을 아직 판정하지 않습니다.",
            "주요 쿨기 표는 내장된 1차 spellID 레지스트리 요약입니다. Plan-vs-Actual용 exact cast는 selectorCatalog 기준으로 별도 보존합니다.",
            "쫄 '등장'은 로그에서 처음 관측된 시점을 사용합니다. 실제 스폰 시각과 다를 수 있어 v0.3-B에서 보스별 기준과 보정합니다.",
            "차단 실패/해제 지연은 성공 이벤트만으로는 판정하지 않습니다. 현재는 성공한 차단/해제 사실만 표시합니다.",
        ],
    }
    facts.update(build_incident_analysis(facts, incident_events))
    return facts


def _pull_spell_display(row: dict | None) -> str:
    row = row or {}
    name = str(row.get("spellName") or "?")
    if row.get("isTaunt") and name.strip() != "도발" and "(도발)" not in name:
        return f"{name} (도발)"
    return name


def render_raid_facts_text(facts: dict) -> str:
    e = facts.get("encounter") or {}
    s = facts.get("summary") or {}
    lines = [
        "=== Raid Wipe Review · 공대 전체 사실 v0.3-A4 ===",
        f"{e.get('name','?')} · {e.get('difficultyName','-')} · Try {e.get('tryNo','?')} · {e.get('result','-')} · {_fmt_sec(e.get('durationSec'))}",
        "※ 공대 전체 사실 + 보스별 우선 딜 구간의 쿨기 소모 후보를 표시합니다. 최종 전멸 원인/책임은 아직 단정하지 않습니다.",
        "",
        "--- 공대 개요 ---",
        f"사람 플레이어 관측 {s.get('humanPlayersSeen',0)}명 · 사망 이벤트 {s.get('deathEvents',0)}건 / 고유 사망자 {s.get('uniquePlayersDied',0)}명",
        f"동시 피해 묶음 {s.get('simultaneousDamageGroups',0)}건 · 주요 공격 쿨기 {s.get('majorOffensiveCasts',0)}회 · 주요 생존기 {s.get('majorDefensiveCasts',0)}회",
        f"차단 {s.get('interrupts',0)}회 · 해제 {s.get('dispels',0)}회 · 전투부활 시전 {s.get('resurrections',0)}회 / 행동 재개 {s.get('playerReturns',0)}회 · 공대가 타격한 적 {s.get('hostileTargetsHit',0)}종",
    ]

    pull = facts.get("pullStart") or {}
    first = pull.get("firstObservedAttack") or {}
    lines += ["", "--- 풀 시작 관측 ---"]
    if pull.get("prerollAvailable"):
        sec = float(pull.get("prerollSec") or 0.0)
        window = float(pull.get("prerollWindowSec") or 10.0)
        if sec + 0.05 < window:
            lines.append(f"사전 로그: 풀 시작 전 {sec:.1f}초까지만 확인됨 · 그 이전 선행 공격은 확인 불가")
        else:
            lines.append(f"사전 로그: 풀 시작 전 {window:.1f}초 구간 확보")
    if first:
        who = _short_player_name(first.get("playerName"))
        if first.get("actorType") == "pet":
            who += f" [펫:{_short_player_name(first.get('sourceName'))}]"
        skill = _pull_spell_display(first)
        target = first.get("targetName") or "?"
        if pull.get("pullerConfirmed"):
            lines.append(f"풀러 확인: {_fmt_signed_sec(first.get('sec'))}  {who} · {skill} → {target}")
        else:
            lines.append(f"첫 관측 공격: {_fmt_signed_sec(first.get('sec'))}  {who} · {skill} → {target}")
            lines.append("※ 현재 저장 구간만으로 풀러를 확정할 수 없어 '첫 관측 공격'으로 표시합니다.")
        near = pull.get("nearSimultaneousAttacks") or []
        if len(near) > 1:
            lines.append(f"근접 시점 공격({float(pull.get('nearSimultaneousWindowSec') or 0.35):.2f}s 창):")
            for row in near[:8]:
                actor = _short_player_name(row.get("playerName"))
                if row.get("actorType") == "pet":
                    actor += f" [펫:{_short_player_name(row.get('sourceName'))}]"
                lines.append(
                    f"  {_fmt_signed_sec(row.get('sec'))} {actor} · "
                    f"{_pull_spell_display(row)} → {row.get('targetName') or '?'}"
                )
    else:
        lines.append("적 대상 공격 후보 이벤트가 관측되지 않았습니다.")

    deaths = facts.get("deaths") or []
    lines += ["", "--- 사망 타임라인 ---"]
    if deaths:
        for row in deaths[:30]:
            last = row.get("lastHit") or {}
            detail = ""
            if last:
                detail = f" · 마지막 {last.get('sourceName') or '?'} / {last.get('spellName') or '?'} {_fmt_amount(last.get('amount'))}"
            n = f" [{row.get('deathNo')}차]" if int(row.get("deathNo") or 1) > 1 else ""
            lines.append(
                f"{_fmt_sec(row.get('sec'))}  {_short_player_name(row.get('name'))}{n}{detail} · 직전2초 {_fmt_amount(row.get('damage2s'))}"
            )
    else:
        lines.append("Player-* 사망 이벤트 없음")

    sims = facts.get("simultaneousDamage") or []
    lines += ["", "--- 동일 기술 동시 피해 (3명 이상) ---"]
    if sims:
        # Most useful events are larger player-count clusters; keep chronological order but cap text.
        for row in sims[-25:]:
            hp = f" · 피격후 최저HP {row.get('minHpPctAfter'):.1f}%" if row.get("minHpPctAfter") is not None else ""
            names = ", ".join(row.get("players") or [])
            if len(names) > 120:
                names = names[:117] + "..."
            lines.append(
                f"{_fmt_sec(row.get('sec'))}  {row.get('sourceName') or '?'} · {row.get('spellName') or '?'} · "
                f"{row.get('playerCount',0)}명 · 합계 {_fmt_amount(row.get('totalDamage'))}{hp}"
            )
            lines.append(f"  대상: {names}")
    else:
        lines.append("3명 이상 동시 피해 묶음 없음 (추종자/NPC는 사람 플레이어 수에 포함하지 않음)")

    windows = facts.get("priorityBurnWindows") or []
    if windows:
        lines += ["", "--- 보스 우선 딜 구간 · 쿨기 보존 확인 ---"]
        for win in windows:
            lines.append(
                f"[{win.get('label')}] {_fmt_sec(win.get('startSec'))}~{_fmt_sec(win.get('endSec'))} "
                f"({float(win.get('durationSec') or 0.0):.1f}s)"
            )
            if win.get("note"):
                lines.append(f"  기준: {win.get('note')}")
            during = win.get("duringMajorCooldowns") or []
            if during:
                lines.append("  심장 구간 주요 쿨기:")
                for row in during[:40]:
                    lines.append(f"    {_fmt_sec(row.get('sec'))} {_short_player_name(row.get('playerName'))} · {row.get('spellName') or '?'}")
            else:
                lines.append("  심장 구간에 기록된 주요 공격 쿨기 없음")
            risks = win.get("riskCandidates") or []
            if risks:
                lines.append(f"  ⚠ 심장 직전 {float(win.get('preWindowSec') or 0):.0f}초 내 사용 + 심장 중 같은 쿨기 재사용 없음:")
                for row in risks[:40]:
                    adds = ", ".join(row.get("activeAdds") or [])
                    add_text = f" · 당시 쫄: {adds}" if adds else ""
                    lines.append(
                        f"    {_fmt_sec(row.get('sec'))} {_short_player_name(row.get('playerName'))} · {row.get('spellName') or '?'} "
                        f"(심장 {float(row.get('secondsBeforePriority') or 0):.1f}s 전){add_text}"
                    )
                lines.append("  ※ 현재는 '쿨기 소모 후보'입니다. 쿨감/충전/공대 배정에 따라 정당할 수 있어 최종 실수 판정은 다음 단계에서 비교 로그와 함께 합니다.")
            else:
                lines.append("  ✓ 심장 직전 쿨기 소모 후보 없음")

    lines += ["", "--- 공대 주요 공격 쿨기 ---"]
    offs = facts.get("majorOffensiveCooldowns") or []
    if offs:
        for row in offs[:80]:
            lines.append(f"{_fmt_sec(row.get('sec'))}  {_short_player_name(row.get('playerName'))} · {row.get('spellName') or '?'}")
    else:
        lines.append("현재 내장 레지스트리에 잡힌 사람 플레이어 주요 공격 쿨기 없음")

    lines += ["", "--- 공대 주요 생존기 ---"]
    defs = facts.get("majorDefensiveCooldowns") or []
    if defs:
        for row in defs[:80]:
            target = f" → {_short_player_name(row.get('targetName'))}" if row.get("targetName") and row.get("targetGUID") else ""
            lines.append(f"{_fmt_sec(row.get('sec'))}  {_short_player_name(row.get('playerName'))} · {row.get('spellName') or '?'}{target}")
    else:
        lines.append("현재 내장 레지스트리에 잡힌 사람 플레이어 주요 생존기 없음")

    lines += ["", "--- 차단 / 해제 성공 ---"]
    ints = facts.get("interrupts") or []
    disps = facts.get("dispels") or []
    if ints:
        lines.append("[차단]")
        for row in ints[:60]:
            lines.append(
                f"{_fmt_sec(row.get('sec'))}  {_short_player_name(row.get('playerName'))} · {row.get('spellName') or '?'} → "
                f"{row.get('targetName') or '?'} / {row.get('interruptedSpellName') or '?'}"
            )
    if disps:
        lines.append("[해제]")
        for row in disps[:60]:
            lines.append(
                f"{_fmt_sec(row.get('sec'))}  {_short_player_name(row.get('playerName'))} · {row.get('spellName') or '?'} → "
                f"{_short_player_name(row.get('targetName'))} / {row.get('removedSpellName') or '?'}"
            )
    if not ints and not disps:
        lines.append("사람 플레이어의 SPELL_INTERRUPT / SPELL_DISPEL 성공 이벤트 없음")

    lines += ["", "--- 공대 타깃 전환 재료 ---"]
    targets = facts.get("targets") or []
    add_rows = [x for x in targets if not x.get("isEncounterName")]
    if add_rows:
        for row in add_rows[:40]:
            delay = f" · 첫관측→첫타 {row.get('switchDelayFromFirstSeenSec'):+.2f}s" if row.get("switchDelayFromFirstSeenSec") is not None else ""
            death = f" · 사망 {_fmt_sec(row.get('deathSec'))}" if row.get("deathSec") is not None else ""
            life = f" · 생존 {row.get('timeFromFirstHitToDeathSec'):.1f}s" if row.get("timeFromFirstHitToDeathSec") is not None else ""
            lines.append(
                f"{row.get('name') or '?'} · 첫관측 {_fmt_sec(row.get('firstSeenSec'))} · 첫 공대타격 {_fmt_sec(row.get('firstRaidHitSec'))}"
                f" ({_short_player_name(row.get('firstHitPlayer'))}) · 타격자 {row.get('uniquePlayersHit',0)}명{delay}{death}{life}"
            )
    else:
        lines.append("보스명 외 별도 적 타깃 기록 없음")

    lines += ["", "--- Selector catalog exact cast · Plan vs Actual 원재료 ---"]
    selector_rows = facts.get("selectorExactCasts") or []
    shown = 0
    for row in selector_rows:
        raw_counts = row.get("rawCounts") or {}
        if not any(int(raw_counts.get(k) or 0) for k in ("SPELL_CAST_SUCCESS", "SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH")):
            continue
        shown += 1
        selected_type = row.get("selectedEventType") or "없음"
        lines.append(
            f"{row.get('spellName') or '?'} ({row.get('spellID')}) · 기준 {selected_type} · "
            f"선택 {row.get('selectedCount',0)}회 · raw CAST {raw_counts.get('SPELL_CAST_SUCCESS',0)} / "
            f"AURA {raw_counts.get('SPELL_AURA_APPLIED',0)} / REFRESH {raw_counts.get('SPELL_AURA_REFRESH',0)}"
        )
        for player in (row.get("players") or []):
            if not int(player.get("count") or 0) and not any(int(v or 0) for v in (player.get("rawCounts") or {}).values()):
                continue
            times = ", ".join(_fmt_sec(x) for x in (player.get("times") or [])[:12])
            more = " ..." if len(player.get("times") or []) > 12 else ""
            lines.append(f"  {_short_player_name(player.get('playerName'))}: {player.get('count',0)}회 · {times}{more}")
    if not shown:
        lines.append("selector catalog 기술 이벤트 없음")

    lines += ["", "--- 적 주요 캐스트 원재료 ---"]
    casts = facts.get("enemyCastSummary") or []
    if casts:
        for row in casts[:30]:
            times = ", ".join(_fmt_sec(x) for x in (row.get("times") or [])[:8])
            more = " ..." if len(row.get("times") or []) > 8 else ""
            lines.append(
                f"{row.get('sourceName') or '?'} · {row.get('spellName') or '?'} · {row.get('event')} {row.get('count',0)}회 · {times}{more}"
            )
    else:
        lines.append("hostile SPELL_CAST_START/SUCCESS 기록 없음")

    lines += ["", "--- 다음 단계에서 판정할 것 ---"]
    lines.append("- 최초 사고/연쇄 사망의 연결, 공통 기믹 실패, 쫄 전환 지연, 쿨기 과소비, 차단/해제 누락을 v0.3-B에서 Top30/NSRT와 교차판정합니다.")
    lines.append("- 지금은 같은 시간에 무엇이 일어났는지 정확히 재구성하는 단계입니다.")
    return "\n".join(lines) + "\n"


def extract_and_write(raw_path: Path | str, encounter: dict) -> dict:
    raw_path = Path(raw_path)
    facts = extract_raid_facts(raw_path, encounter)
    out_dir = raw_path.parent
    json_path = out_dir / "raid_facts.json"
    txt_path = out_dir / "raid_facts.txt"
    atomic_write_json(json_path, facts)
    atomic_write_text(txt_path, render_raid_facts_text(facts))

    meta_path = out_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(meta, dict):
                meta.update({
                    "raidFactsModuleVersion": facts.get("moduleVersion"),
                    "raidFactsJson": "raid_facts.json",
                    "raidFactsTxt": "raid_facts.txt",
                    "raidFactsGeneratedAt": facts.get("generatedAt"),
                    "pullStart": facts.get("pullStart"),
                    "deathCount": len(facts.get("deaths") or []),
                })
                atomic_write_json(meta_path, meta)
        except Exception:
            pass

    return {
        "raidFactsStatus": "ready",
        "raidFactsJson": str(json_path),
        "raidFactsTxt": str(txt_path),
        "raidFacts": facts,
    }
