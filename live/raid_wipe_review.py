from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .encounter_tracker import parse_combat_datetime
from .io_utils import atomic_write_json, atomic_write_text

from .versions import ENGINE_VERSION, APP_BUILD, VERSION_LABEL, INCIDENT_SCHEMA_VERSION
from .rawlog import resolve_raw, raw_exists
MODULE_VERSION = f"RaidWipeReview-{ENGINE_VERSION}"
from .mechanics import is_fake_pull
from .reset_policy import is_reset_pull, RESET_MAX_DURATION_SEC
RULE_DIR = Path(__file__).resolve().parent / "raid_rules"
SERIES_SETTINGS_PATH = Path(__file__).resolve().parent / "raid_series_settings.json"
DEFAULT_SERIES_SETTINGS = {"maxDays": 14, "maxTries": 30}
LOGGER = logging.getLogger(__name__)
DEFAULT_TANK_SPEC_IDS = {66, 73, 104, 250, 268, 581}

# Ambient/encounter-pressure spells may be the literal last hit on many players,
# but they are not actionable root-cause mechanics.  Keep the deaths in the
# timeline/collapse math while suppressing a generic mass-death root finding.
AMBIENT_MASS_DEATH_SUPPRESS = {
    3420: {1285965},  # Ula'tek's Presence: periodic temple-wide raid pressure during Sszorak
    3470: {1288772},  # Soulcoil Rite: recurring raid pressure, observed throughout successful kills
}


def short_name(name: Optional[str]) -> str:
    if not name:
        return "?"
    return str(name).split("-")[0]


def fmt_time(sec: float) -> str:
    sec = max(0.0, float(sec or 0.0))
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m}:{s:05.2f}"


def _spell_id_from_death(d: Dict[str, Any]) -> Optional[int]:
    value = ((d.get("lastHit") or {}).get("spellID"))
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def death_spell(d: Dict[str, Any]) -> str:
    return str((d.get("lastHit") or {}).get("spellName") or "")


def death_source(d: Dict[str, Any]) -> str:
    return str((d.get("lastHit") or {}).get("sourceName") or "")


def first_deaths(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """First death per player. Battle-rez/redeath rows are excluded from collapse math."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for d in sorted(data.get("deaths", []), key=lambda x: float(x.get("sec", 0.0) or 0.0)):
        guid = d.get("guid") or d.get("name")
        if guid in seen:
            continue
        seen.add(guid)
        out.append(d)
    return out


def death_clusters(deaths: List[Dict[str, Any]], window: float = 2.5) -> List[Dict[str, Any]]:
    clusters: List[Dict[str, Any]] = []
    for i, row in enumerate(deaths):
        t0 = float(row.get("sec", 0.0) or 0.0)
        members = [d for d in deaths[i:] if float(d.get("sec", 0.0) or 0.0) <= t0 + window]
        if len(members) < 2:
            continue
        spells = Counter(death_spell(d) for d in members if death_spell(d))
        clusters.append({
            "startSec": t0,
            "endSec": max(float(d.get("sec", 0.0) or 0.0) for d in members),
            "count": len(members),
            "players": [short_name(d.get("name")) for d in members],
            "spells": dict(spells),
        })
    out: List[Dict[str, Any]] = []
    for c in clusters:
        if any(abs(c["startSec"] - p["startSec"]) < 0.5 and c["count"] <= p["count"] for p in out):
            continue
        out.append(c)
    return out


def earliest_collapse(deaths: List[Dict[str, Any]], roster_n: int) -> Optional[Dict[str, Any]]:
    threshold = max(4, math.ceil(max(roster_n, 1) * 0.25))
    rows = [c for c in death_clusters(deaths) if c["count"] >= threshold]
    return min(rows, key=lambda c: c["startSec"]) if rows else None


def deaths_by_ids(data: Dict[str, Any], spell_ids: List[int], unique_only: bool = True) -> List[Dict[str, Any]]:
    wanted = {int(x) for x in spell_ids or []}
    rows = first_deaths(data) if unique_only else list(data.get("deaths", []))
    return [d for d in rows if _spell_id_from_death(d) in wanted]


def simultaneous_by_ids(data: Dict[str, Any], spell_ids: List[int], min_players: int = 1) -> List[Dict[str, Any]]:
    wanted = {int(x) for x in spell_ids or []}
    return [
        row for row in data.get("simultaneousDamage", [])
        if row.get("spellID") in wanted and int(row.get("playerCount", 0) or 0) >= min_players
    ]


def enemy_casts_by_ids(data: Dict[str, Any], spell_ids: List[int], event: Optional[str] = None) -> List[Dict[str, Any]]:
    wanted = {int(x) for x in spell_ids or []}
    return [
        row for row in data.get("enemyCastSummary", [])
        if row.get("spellID") in wanted and (not event or row.get("event") == event)
    ]


def _npc_id(guid: Any) -> Optional[int]:
    parts = str(guid or "").split("-")
    if len(parts) < 6:
        return None
    try:
        return int(parts[5])
    except Exception:
        return None


def target_exists_by_npc(data: Dict[str, Any], npc_ids: List[int]) -> bool:
    wanted = {int(x) for x in npc_ids or []}
    return any(_npc_id(row.get("guid")) in wanted for row in data.get("targets", []))


def _file_sha256(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _load_rule_bundle(encounter_id: int) -> tuple[Dict[str, Any], List[str], Optional[str]]:
    path = RULE_DIR / f"{int(encounter_id)}.json"
    if not path.exists():
        return {}, [], None
    fingerprint = _file_sha256(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("root must be a JSON object")
        if "rules" in data and not isinstance(data.get("rules"), list):
            raise ValueError("'rules' must be an array")
        return data, [], fingerprint
    except Exception as exc:
        msg = f"raid rule parse failed: {path.name}: {exc}"
        LOGGER.warning(msg)
        return {}, [msg], fingerprint


def _load_rule_config(encounter_id: int) -> Dict[str, Any]:
    # Backward-compatible helper for callers/tests that only need the config.
    return _load_rule_bundle(encounter_id)[0]


def _load_series_settings() -> tuple[Dict[str, Any], List[str]]:
    settings = dict(DEFAULT_SERIES_SETTINGS)
    if not SERIES_SETTINGS_PATH.exists():
        return settings, []
    try:
        raw = json.loads(SERIES_SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("root must be a JSON object")
        for key in ("maxDays", "maxTries"):
            if key in raw:
                value = raw.get(key)
                if value is None:
                    settings[key] = 0
                else:
                    settings[key] = max(0, int(value))
        return settings, []
    except Exception as exc:
        msg = f"raid series settings parse failed: {SERIES_SETTINGS_PATH.name}: {exc}; using defaults"
        LOGGER.warning(msg)
        return settings, [msg]


@dataclass
class Finding:
    code: str
    title: str
    category: str
    confidence: str
    severity: int
    sec: Optional[float]
    affected: int
    evidence: List[str]
    interpretation: str
    action: str
    is_primary_candidate: bool = True
    trigger_spell_ids: Optional[List[int]] = None
    downstream_of: Optional[str] = None

    def score(self, roster_n: int, collapse_sec: Optional[float]) -> float:
        conf = {"high": 3.0, "medium": 2.0, "low": 1.0}.get(self.confidence, 1.0)
        affected_ratio = min(1.0, self.affected / max(1, roster_n))
        proximity = 0.0
        if collapse_sec is not None and self.sec is not None:
            delta = collapse_sec - self.sec
            if -2.0 <= delta <= 8.0:
                proximity = 2.5
            elif -5.0 <= delta <= 20.0:
                proximity = 1.4
            elif delta > 20.0:
                proximity = max(0.0, 0.8 - delta / 120.0)
        candidate_bonus = 0.0 if self.is_primary_candidate else -8.0
        return self.severity * 1.7 + conf + affected_ratio * 4.0 + proximity + candidate_bonus


def _finding_from_rule(data: Dict[str, Any], rule: Dict[str, Any], roster_n: int) -> Optional[Finding]:
    typ = rule.get("type")
    evidence: List[str] = []
    sec: Optional[float] = None
    affected = 0
    primary_candidate = True

    if typ == "mass_death":
        death_ids = rule.get("deathSpellIDs") or []
        sim_ids = rule.get("simultaneousSpellIDs") or []
        deaths = deaths_by_ids(data, death_ids)
        sims = simultaneous_by_ids(data, sim_ids, int(rule.get("minPlayers", 1) or 1)) if sim_ids else []
        min_deaths = int(rule.get("minDeaths", 1) or 1)
        if len(deaths) < min_deaths and not sims:
            return None
        candidates = [float(d.get("sec", 0.0) or 0.0) for d in deaths] + [float(x.get("sec", 0.0) or 0.0) for x in sims]
        sec = min(candidates) if candidates else None
        max_sim = max([int(x.get("playerCount", 0) or 0) for x in sims] or [0])
        affected = max(len(deaths), max_sim)
        evidence.append(f"직접 첫 사망 {len(deaths)}명")
        if sims:
            evidence.append(f"같은 타이밍 최대 {max_sim}명 피격")
        primary_min = int(rule.get("primaryCandidateMinDeaths", min_deaths) or min_deaths)
        if len(deaths) < primary_min and not (sims and rule.get("simultaneousCanPrimary", True)):
            primary_candidate = False

    elif typ == "enemy_cast_success":
        rows = enemy_casts_by_ids(data, rule.get("castSpellIDs") or [], "SPELL_CAST_SUCCESS")
        total = sum(int(x.get("count", 0) or 0) for x in rows)
        if total < int(rule.get("minCount", 1) or 1):
            return None
        times = sorted(float(t) for row in rows for t in (row.get("times") or []))
        sec = times[0] if times else None
        affected = roster_n
        evidence.append(f"성공 시전 {total}회")

    elif typ == "target_or_death":
        deaths = deaths_by_ids(data, rule.get("deathSpellIDs") or [])
        exists = target_exists_by_npc(data, rule.get("targetNpcIDs") or [])
        if not exists and len(deaths) < int(rule.get("minDeaths", 1) or 1):
            return None
        sec = min([float(d.get("sec", 0.0) or 0.0) for d in deaths] or [float(data.get("encounter", {}).get("durationSec", 0.0) or 0.0)])
        affected = max(len(deaths), 1)
        evidence.append("실패 개체가 전투 대상에 등장함" if exists else "실패 개체 등장은 대상 목록에서 미확인")
        evidence.append(f"연계 기술 직접 첫 사망 {len(deaths)}명")
    else:
        return None

    return Finding(
        code=str(rule.get("code") or "RULE"),
        title=str(rule.get("title") or rule.get("code") or "기믹 문제"),
        category=str(rule.get("category") or "mechanic"),
        confidence=str(rule.get("confidence") or "medium"),
        severity=int(rule.get("severity", 3) or 3),
        sec=sec,
        affected=affected,
        evidence=evidence,
        interpretation=str(rule.get("interpretation") or ""),
        action=str(rule.get("action") or ""),
        is_primary_candidate=primary_candidate,
        trigger_spell_ids=[int(x) for x in (rule.get("deathSpellIDs") or rule.get("castSpellIDs") or [])],
    )


def _tank_roster(data: Dict[str, Any], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    ids = {int(x) for x in (config.get("tankSpecIDs") or DEFAULT_TANK_SPEC_IDS)}
    return [row for row in data.get("roster", []) if row.get("specID") in ids]


def _alive_state_at(data: Dict[str, Any], guid: str, at_sec: float) -> Dict[str, Any]:
    """Reconstruct whether a player had actually resumed play by ``at_sec``.

    B1 treated SPELL_RESURRECT as a completed resurrection.  CombatLog emits that
    event when the resurrection is offered/cast, not when the target accepts it.
    B2 only transitions back to alive on a post-death player action recorded by the
    raid-facts extractor (first self SPELL_CAST_SUCCESS).  Legacy raid_facts without
    playerReturns are marked uncertain when a resurrection cast exists, rather than
    silently assuming the player was alive.
    """
    events: List[tuple] = []
    for d in data.get("deaths", []):
        if d.get("guid") == guid and d.get("sec") is not None and float(d.get("sec")) < at_sec:
            events.append((float(d.get("sec")), 0, "death", d))

    return_tracking = "playerReturns" in data
    seen_returns = set()
    for r in data.get("playerReturns", []) or []:
        if r.get("playerGUID") == guid and r.get("sec") is not None and float(r.get("sec")) < at_sec:
            key = round(float(r.get("sec")), 3)
            seen_returns.add(key)
            events.append((float(r.get("sec")), 1, "return", r))
    # B2 also annotates the resurrection row itself with returnSec.  Accept it as a
    # fallback so hand-built/synthetic facts do not need to duplicate playerReturns.
    for r in data.get("resurrections", []) or []:
        if r.get("targetGUID") != guid or r.get("returnSec") is None:
            continue
        rsec = float(r.get("returnSec"))
        if rsec < at_sec and round(rsec, 3) not in seen_returns:
            events.append((rsec, 1, "return", r))

    events.sort(key=lambda x: (x[0], x[1]))
    alive = True
    last_death = None
    last_return = None
    for _, _, kind, row in events:
        if kind == "death":
            alive = False
            last_death = row
        else:
            alive = True
            last_return = row

    pending_res = None
    if not alive and last_death and last_death.get("sec") is not None:
        dsec = float(last_death.get("sec"))
        attempts = [
            r for r in (data.get("resurrections", []) or [])
            if r.get("targetGUID") == guid
            and r.get("sec") is not None
            and dsec <= float(r.get("sec")) < at_sec
            and (r.get("returnSec") is None or float(r.get("returnSec")) >= at_sec)
        ]
        if attempts:
            pending_res = max(attempts, key=lambda r: float(r.get("sec") or 0.0))

    uncertain = bool(not return_tracking and not alive and pending_res)
    return {
        "alive": alive,
        "lastDeath": last_death,
        "lastReturn": last_return,
        "pendingResurrection": pending_res,
        "returnTrackingAvailable": return_tracking,
        "uncertain": uncertain,
    }


def _tank_states_at(data: Dict[str, Any], at_sec: float, config: Dict[str, Any]) -> List[tuple]:
    rows = []
    for tank in _tank_roster(data, config):
        rows.append((tank, _alive_state_at(data, str(tank.get("guid")), at_sec)))
    return rows


def _tank_state_evidence(states: List[tuple], at_sec: float) -> List[str]:
    if not states:
        return []
    alive_n = sum(1 for _, state in states if state.get("alive"))
    evidence = [f"{fmt_time(at_sec)} 분노 시점 행동 가능한 탱커 {alive_n}/{len(states)}명"]
    for tank, state in states:
        if state.get("alive"):
            continue
        name = short_name(tank.get("name"))
        death = state.get("lastDeath") or {}
        dsec = death.get("sec")
        pending = state.get("pendingResurrection") or {}
        if state.get("uncertain"):
            evidence.append(
                f"{name}: {fmt_time(dsec)} 사망 후 {fmt_time(pending.get('sec'))} 부활 시전은 있으나 "
                "구형 facts라 실제 행동 재개 시점 확인 불가"
            )
        elif pending:
            evidence.append(
                f"{name}: {fmt_time(dsec)} 사망 · {fmt_time(pending.get('sec'))} 부활 시전 후 분노 시점까지 본인 행동 재개 없음"
            )
        elif dsec is not None:
            evidence.append(f"{name}: {fmt_time(dsec)} 사망 후 분노 시점까지 행동 재개 없음")
    return evidence


def _tank_upstream_finding(
    data: Dict[str, Any],
    downstream: Finding,
    rule: Dict[str, Any],
    config: Dict[str, Any],
    states: Optional[List[tuple]] = None,
) -> Optional[Finding]:
    if not rule.get("tankUpstream") or downstream.sec is None:
        return None
    states = states if states is not None else _tank_states_at(data, float(downstream.sec), config)
    if not states:
        return None

    # Uncertain legacy states are evidence only.  Do not promote a tank death when a
    # resurrection offer exists but old raid_facts cannot prove whether it was accepted.
    dead = [(t, st) for t, st in states if not st.get("alive") and not st.get("uncertain") and st.get("lastDeath")]
    if not dead:
        return None

    dead.sort(key=lambda item: float((item[1].get("lastDeath") or {}).get("sec", 0.0) or 0.0), reverse=True)
    tank, state = dead[0]
    d = state["lastDeath"]
    dsec = float(d.get("sec", 0.0) or 0.0)
    delta = float(downstream.sec) - dsec
    recent_window = float(rule.get("tankRecentDeathWindowSec", 15.0) or 15.0)
    all_certainly_dead = len(dead) == len(states)

    # One tank being dead for a long time is strategically important, but does not by
    # itself prove that the later rage was caused by that old death.  Keep 1/2 in the
    # downstream evidence, and only promote if all tanks are unavailable or the death
    # is within the configured causal window.
    if not all_certainly_dead and delta > recent_window:
        return None

    hit = d.get("lastHit") or {}
    sid = hit.get("spellID")
    spell = hit.get("spellName") or "알 수 없는 피해"
    player = short_name(d.get("name") or tank.get("name"))
    if all_certainly_dead:
        confidence = "high"
        state_text = f"분노 직전 확인 가능한 탱커 {len(states)}명이 모두 행동 불가 상태"
    else:
        confidence = "high" if delta <= 8.0 else "medium"
        state_text = f"탱커 {player}가 분노 {delta:.1f}초 전에 사망하고 분노 시점까지 행동 재개 없음"
    code = f"TANK_DEATH_UPSTREAM_{sid if sid is not None else 'UNKNOWN'}"
    title = f"{spell} 탱커 사망 → 탱킹 인계 공백 → {downstream.title}"
    return Finding(
        code=code,
        title=title,
        category="tank_upstream",
        confidence=confidence,
        severity=6,
        sec=dsec,
        affected=max(1, downstream.affected),
        evidence=[
            f"{fmt_time(dsec)} {player} 사망 · 마지막 피해 {spell} ({hit.get('sourceName') or '?'})",
            state_text,
            f"{fmt_time(float(downstream.sec))} 후속 결과: {downstream.title}",
        ],
        interpretation=f"공대 광역 분노 자체보다 먼저 탱커 {player}가 {spell}에 사망했고 실제 행동 복귀가 확인되지 않았습니다. 시간 순서상 이 사망과 탱킹 인계 공백을 상위 원인으로 봅니다.",
        action=f"우선 {player}의 {spell} 사망을 막는 처리부터 수정하고, 탱커 사망 시 오프탱 즉시 인계/전투부활 콜을 고정. 후속 분노는 결과로 취급합니다.",
        is_primary_candidate=True,
        trigger_spell_ids=[int(sid)] if sid is not None else [],
        downstream_of=downstream.code,
    )


def rule_findings(data: Dict[str, Any], config: Dict[str, Any]) -> List[Finding]:
    roster_n = max(1, int(data.get("summary", {}).get("humanPlayersSeen", 0) or len(data.get("roster", [])) or 1))
    findings: List[Finding] = []
    for rule in config.get("rules", []):
        f = _finding_from_rule(data, rule, roster_n)
        if not f:
            continue
        states = None
        if rule.get("tankUpstream") and f.sec is not None:
            states = _tank_states_at(data, float(f.sec), config)
            f.evidence.extend(_tank_state_evidence(states, float(f.sec)))
        findings.append(f)
        upstream = _tank_upstream_finding(data, f, rule, config, states=states)
        if upstream:
            # Keep the downstream raid explosion as evidence/secondary, but promote the
            # actual tank death mechanism as a separate causal candidate.
            f.is_primary_candidate = False
            findings.append(upstream)
    return findings


def generic_findings(data: Dict[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    deaths = first_deaths(data)
    roster_n = max(1, int(data.get("summary", {}).get("humanPlayersSeen", 0) or len(data.get("roster", [])) or 1))
    try:
        encounter_id = int((data.get("encounter") or {}).get("encounterID") or 0)
    except Exception:
        encounter_id = 0
    ambient_suppressed = AMBIENT_MASS_DEATH_SUPPRESS.get(encounter_id, set())
    by_spell: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for d in deaths:
        sid = _spell_id_from_death(d)
        if sid in ambient_suppressed:
            continue
        sp = death_spell(d)
        if sp:
            by_spell[(sid, sp)].append(d)
    for (sid, spell), rows in sorted(by_spell.items(), key=lambda kv: len(kv[1]), reverse=True):
        if len(rows) < max(3, math.ceil(roster_n * 0.18)):
            continue
        findings.append(Finding(
            code=f"GENERIC_MASS_DEATH_{sid if sid is not None else re.sub(r'[^A-Za-z0-9]+','_',spell)}",
            title=f"{spell} 다수 사망",
            category="mass_death",
            confidence="medium",
            severity=3,
            sec=min(float(x.get("sec", 0.0) or 0.0) for x in rows),
            affected=len(rows),
            evidence=[f"같은 spellID 직접 첫 사망 {len(rows)}명"],
            interpretation="같은 기술이 공대 다수의 직접 사망 원인으로 관측됐습니다. 보스별 규칙이 없으면 기믹 의미까지는 단정하지 않습니다.",
            action="해당 기술 직전 10초의 위치·생존기·차단·대상 전환을 우선 복기.",
            trigger_spell_ids=[int(sid)] if sid is not None else [],
        ))
        break
    return findings


def analyze_try(data: Dict[str, Any]) -> Dict[str, Any]:
    enc = data.get("encounter", {})
    roster_n = max(1, int(data.get("summary", {}).get("humanPlayersSeen", 0) or len(data.get("roster", [])) or 1))
    deaths = first_deaths(data)
    collapse = earliest_collapse(deaths, roster_n)
    collapse_sec = collapse["startSec"] if collapse else None

    config, rule_warnings, rule_fingerprint = _load_rule_bundle(int(enc.get("encounterID", 0) or 0))
    findings = rule_findings(data, config) if config else []
    findings.extend(generic_findings(data))

    # De-duplicate generic findings when an ID-based encounter rule explains the same spell.
    specific_ids = {sid for f in findings if not f.code.startswith("GENERIC_") for sid in (f.trigger_spell_ids or [])}
    findings = [f for f in findings if not (f.code.startswith("GENERIC_") and specific_ids.intersection(f.trigger_spell_ids or []))]

    ranked = sorted(findings, key=lambda f: f.score(roster_n, collapse_sec), reverse=True)
    primary_candidates = [f for f in ranked if f.is_primary_candidate]
    primary = primary_candidates[0] if primary_candidates else None

    # Spread-out attrition should not be overclaimed unless a severe causal rule exists.
    if deaths and len(deaths) >= 6:
        span = float(deaths[-1].get("sec", 0.0) or 0.0) - float(deaths[0].get("sec", 0.0) or 0.0)
        largest_cluster = max([c["count"] for c in death_clusters(deaths)] or [1])
        if span > 35 and largest_cluster < max(5, math.ceil(roster_n * 0.35)):
            severe = [f for f in primary_candidates if f.severity >= 5 and f.affected >= max(4, math.ceil(roster_n * 0.25))]
            if not severe:
                primary = None

    # "붕괴 전 첫 손실" must actually be before the collapse.  If no collapse was
    # detected, keep the first five losses as an early-loss diagnostic.
    pre_collapse = deaths if collapse_sec is None else [d for d in deaths if float(d.get("sec", 0.0) or 0.0) < float(collapse_sec)]
    first_losses = [{
        "sec": d.get("sec"),
        "player": short_name(d.get("name")),
        "spellID": _spell_id_from_death(d),
        "spell": death_spell(d),
        "source": death_source(d),
    } for d in pre_collapse[:5]]

    if primary:
        verdict = {
            "mode": "single_root",
            "title": primary.title,
            "confidence": primary.confidence,
            "sec": primary.sec,
            "interpretation": primary.interpretation,
            "action": primary.action,
        }
    else:
        spell_counts = Counter(death_spell(d) for d in deaths if death_spell(d))
        verdict = {
            "mode": "attrition_chain",
            "title": "단일 원인보다 연쇄 손실/누적 붕괴",
            "confidence": "medium",
            "sec": collapse_sec,
            "interpretation": "한 번의 대형 사고보다 여러 기믹에서 인원이 순차적으로 빠지며 복구력이 무너진 패턴입니다.",
            "action": "첫 사망부터 마지막 붕괴까지 시간순으로 따라가며 반복 손실부터 하나씩 제거.",
            "topDeathSpells": spell_counts.most_common(4),
        }

    secondary = []
    for f in ranked:
        if primary and f.code == primary.code:
            continue
        secondary.append({**asdict(f), "score": round(f.score(roster_n, collapse_sec), 2)})
        if len(secondary) >= 6:
            break

    risk = int(data.get("summary", {}).get("priorityCooldownRiskCandidates", 0) or 0)
    performance_notes = []
    if risk:
        performance_notes.append({
            "title": "우선 딜 구간 직전 쿨기 소모 후보",
            "count": risk,
            "note": "딜 효율 점검 항목이며 이번 전멸의 직접 원인으로 자동 승격하지 않음.",
        })

    review = {
        "schemaVersion": 5,
        "engineVersion": ENGINE_VERSION,
        "appBuild": APP_BUILD,
        "moduleVersion": MODULE_VERSION,
        "ruleFingerprint": rule_fingerprint,
        "warnings": rule_warnings,
        "encounter": enc,
        "collapse": collapse,
        "verdict": verdict,
        "primaryFinding": ({**asdict(primary), "score": round(primary.score(roster_n, collapse_sec), 2)} if primary else None),
        "secondaryFindings": secondary,
        "firstLosses": first_losses,
        "firstLossesArePreCollapse": collapse_sec is not None,
        "performanceNotes": performance_notes,
        "allFindings": [{**asdict(f), "score": round(f.score(roster_n, collapse_sec), 2)} for f in ranked],
        "limitations": [
            "B2는 raid_facts.json의 spellID와 보스별 규칙 JSON을 우선 사용합니다. 표시 이름은 설명용이며 판정 키가 아닙니다.",
            "탱커 선행사망 역추적은 COMBATANT_INFO specID, 사망, 부활 시전, 사망 후 본인 SPELL_CAST_SUCCESS 재등장을 함께 사용합니다.",
            "보스별 규칙이 없는 기술은 대량 사망/시간상관 수준까지만 판정합니다.",
            "쿨기 보존 후보는 전멸 직접 원인과 분리합니다.",
        ],
    }

    chain = data.get('collapseChain')
    personal = data.get('deathIncidents') or []
    review.update({'collapseChain': chain, 'deathIncidents': personal,
                   'analysisExcluded': data.get('analysisExcluded'),
                   'rootIncidents': [chain] if chain else personal})
    if chain:
        interpretation = ' → '.join([chain['rootMechanic']] + [
            f"{fmt_time(x['t'])} {x.get('mechanic') or x.get('type')}"
            for x in chain['downstreamEvents']])
        action = ('광역 전 체력 회복과 직전 연속 피해를 확인하세요.' if chain['rootType'] == 'low_health_cascade'
                  else '선행 파열 대상의 생존과 지속 피해 대응을 먼저 점검하세요.')
        pf = {'code': 'CHAIN_' + chain['sessionBucket'], 'title': chain['sessionBucket'],
              'category': 'collapse_chain', 'confidence': chain['confidence'], 'severity': 5,
              'sec': chain['rootTime'], 'affected': chain['rootVictimCount'],
              'evidence': chain['stateWorseningEvidence'], 'interpretation': interpretation,
              'action': action, 'is_primary_candidate': True, 'trigger_spell_ids': [chain['rootSpellID']]}
        review.update({'primaryFinding': pf, 'allFindings': [pf], 'secondaryFindings': [],
                       'verdict': {'mode': 'collapse_chain', 'title': chain['sessionBucket'],
                                   'confidence': chain['confidence'], 'sec': chain['rootTime'],
                                   'interpretation': interpretation, 'action': action}})
        review['firstLosses'] = [dict(sec=x['t'], player=x['player'], spell=x['mechanic'], source='') for x in chain['priorLosses']]
        review['firstLossesArePreCollapse'] = True
    elif str(enc.get('result', '')).upper() == 'KILL':
        review.update({'primaryFinding': None, 'allFindings': [], 'secondaryFindings': [],
                       'verdict': {'mode': 'kill_personal_review', 'title': '처치 성공 · 개인 사망 복기',
                                   'confidence': 'high', 'interpretation': '전멸 원인은 집계하지 않습니다.',
                                   'action': '개인 사망 체인을 확인하세요.'}})
    elif int(enc.get('encounterID') or 0) == 3445:
        # Generic death-count ranking must not override the causal evidence gate.
        review.update({'primaryFinding': None, 'allFindings': [], 'secondaryFindings': [],
                       'verdict': {'mode': 'insufficient_evidence', 'title': '선행 원인 연결 근거 부족',
                                   'confidence': 'low', 'interpretation': '시간상 인접한 사망만으로 원인을 확정하지 않습니다.',
                                   'action': '원본 피해·생존 복귀·체력 기록을 확인하세요.'}})
    if is_reset_pull(enc):
        # B31 compatibility: sub-second encounter-marker artifacts keep the
        # historical `fake_pull` exclusion label.  They are also a B32 reset
        # by duration policy, so retain reset metadata without overwriting the
        # more specific exclusion reason.
        fake_pull = is_fake_pull(enc)
        review.update({'analysisExcluded': 'fake_pull' if fake_pull else 'reset_pull',
                       'resetPull': True,
                       'rootIncidents': [], 'deathIncidents': [],
                       'primaryFinding': None, 'allFindings': [], 'secondaryFindings': [],
                       'verdict': {'mode': 'excluded',
                                   'title': ('1초 미만 fake pull 제외' if fake_pull else
                                             f'{RESET_MAX_DURATION_SEC:.0f}초 이내 + 무사망/저관여 리셋 · 기본 집계 제외'),
                                   'confidence': 'high',
                                   'interpretation': '원본 Try와 사실 기록은 보존하고 일반 전멸 원인/반복 원인 집계에서는 제외합니다.',
                                   'action': '필요하면 사실 로그에서 첫 관측 공격과 사망을 확인하세요.'}})
        if fake_pull:
            review['excludedSubtype'] = 'fake_pull'
    return review


def render_try(review: Dict[str, Any]) -> str:
    e = review.get("encounter", {})
    v = review.get("verdict") or {}
    lines = [
        f"=== Raid Wipe Review · 원인/개선 {VERSION_LABEL} ===",
        f"{e.get('name','?')} · {e.get('difficultyName','?')} · 원본 Try {e.get('tryNo','?')} · {e.get('result','?')} · {fmt_time(e.get('durationSec',0))}",
        "",
        "--- 이번 트라이 핵심 전멸 원인 ---",
    ]
    if v.get("sec") is not None:
        lines.append(f"[{str(v.get('confidence','?')).upper()}] {fmt_time(v['sec'])}  {v.get('title','?')}")
    else:
        lines.append(f"[{str(v.get('confidence','?')).upper()}] {v.get('title','?')}")
    lines.append(f"왜 터졌나: {v.get('interpretation','')}")
    lines.append(f"다음 트라이: {v.get('action','')}")
    lines.append("")

    pf = review.get("primaryFinding")
    if pf:
        lines.append("--- 근거 ---")
        for x in pf.get("evidence", []):
            lines.append(f"- {x}")
        lines.append("")

    secondary = review.get("secondaryFindings") or []
    if secondary:
        lines.append("--- 같이 고칠 것 / 후속 결과 ---")
        for f in secondary[:5]:
            t = f"{fmt_time(f['sec'])} " if f.get("sec") is not None else ""
            suffix = " · 보조 관측" if not f.get("is_primary_candidate", True) else ""
            lines.append(f"- {t}{f.get('title')} [{f.get('confidence')}] {suffix} · {f.get('action')}")
        lines.append("")

    fl = review.get("firstLosses") or []
    if fl:
        heading = "--- 붕괴 전 첫 손실 ---" if review.get("firstLossesArePreCollapse") else "--- 초기 손실 ---"
        lines.append(heading)
        for x in fl:
            sid = f" #{x.get('spellID')}" if x.get("spellID") is not None else ""
            lines.append(f"- {fmt_time(x['sec'])} {x['player']} ← {x['spell']}{sid} ({x['source'] or '?'})")
        lines.append("")

    if review.get("performanceNotes"):
        lines.append("--- 딜/쿨기 보조 점검 ---")
        for x in review["performanceNotes"]:
            lines.append(f"- {x['title']} {x['count']}건 · {x['note']}")
        lines.append("")

    if review.get("warnings"):
        lines.append("--- 규칙 경고 ---")
        for warning in review.get("warnings") or []:
            lines.append(f"- {warning}")
        lines.append("")

    if review.get('deathIncidents'):
        lines.append('--- 개인 사망 체인 (사건당 1건) ---')
        for incident in review['deathIncidents']:
            lines.append(f"- {incident['player']}: {incident['sentence']}")
    lines.append("※ B31은 마지막 사망기보다 선행 원인을 우선합니다. 부활은 시전이 아니라 실제 행동 재개를 기준으로 생존 복귀를 판정합니다.")
    return "\n".join(lines) + "\n"


def _parse_start(value: Any) -> datetime:
    dt = parse_combat_datetime(str(value or ""))
    if dt:
        return dt
    for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value).split(".")[0], fmt)
        except Exception:
            pass
    return datetime.min


def analyze_series(reviews: List[Dict[str, Any]], scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    rows = sorted([
        r for r in reviews
        if not r.get("analysisExcluded")
        and not is_reset_pull(r.get("encounter") or {})
        and not is_fake_pull(r.get("encounter") or {})
    ], key=lambda r: _parse_start((r.get("encounter") or {}).get("startTimestamp")))
    unique_rows = []
    seen_fights = set()
    for review in rows:
        enc = review.get('encounter') or {}
        key = (enc.get('encounterID'), enc.get('difficultyID'), enc.get('startTimestamp'))
        if key[2] and key in seen_fights:
            continue
        if key[2]:
            seen_fights.add(key)
        unique_rows.append(review)
    rows = unique_rows
    primary_counts = Counter()
    occurrence_counts = Counter()
    titles: Dict[str, str] = {}
    examples: Dict[str, list] = defaultdict(list)
    reached = []

    for seq, r in enumerate(rows, 1):
        e = r.get("encounter", {})
        reached.append(float(e.get("durationSec", 0.0) or 0.0))
        pf = r.get("primaryFinding")
        if pf:
            primary_counts[pf.get("code")] += 1
            titles[pf.get("code")] = pf.get("title") or pf.get("code")
        elif str(e.get("result", "WIPE")).upper() == "WIPE":
            primary_counts["ATTRITION_CHAIN"] += 1
            titles["ATTRITION_CHAIN"] = "단일 원인보다 연쇄 손실/누적 붕괴"

        seen_this_try = set()
        for f in r.get("allFindings") or []:
            code = f.get("code")
            if not code or code in seen_this_try:
                continue
            seen_this_try.add(code)
            occurrence_counts[code] += 1
            titles[code] = f.get("title") or code
            examples[code].append({
                "sequenceNo": seq,
                "originalTryNo": e.get("tryNo"),
                "startTimestamp": e.get("startTimestamp"),
                "durationSec": e.get("durationSec"),
                "sec": f.get("sec"),
                "wasPrimary": bool(pf and pf.get("code") == code),
            })
        if not pf and str(e.get("result", "WIPE")).upper() == "WIPE":
            occurrence_counts["ATTRITION_CHAIN"] += 1
            examples["ATTRITION_CHAIN"].append({
                "sequenceNo": seq,
                "originalTryNo": e.get("tryNo"),
                "startTimestamp": e.get("startTimestamp"),
                "durationSec": e.get("durationSec"),
                "sec": (r.get("verdict") or {}).get("sec"),
                "wasPrimary": True,
            })

    repeated = []
    all_codes = set(occurrence_counts) | set(primary_counts)
    for code in all_codes:
        repeated.append({
            "code": code,
            "title": titles.get(code, code),
            "primaryCount": int(primary_counts.get(code, 0)),
            "occurrenceTryCount": int(occurrence_counts.get(code, 0)),
            "examples": examples.get(code, [])[:6],
        })
    # A raid lead should see wipe-causing patterns first.  Secondary observations can
    # still be frequent, but they must not outrank a repeated primary wipe cause.
    repeated.sort(key=lambda x: (-x["primaryCount"], -x["occurrenceTryCount"], x["title"]))

    root_buckets = Counter()
    seen_roots = []
    for index, review in enumerate(rows):
        enc = review.get('encounter') or {}
        fight_id = (enc.get('encounterID'), enc.get('difficultyID'), enc.get('startTimestamp') or enc.get('fightID') or ('unidentified', index))
        for incident in review.get('rootIncidents') or []:
            mechanic = incident['rootMechanic']
            t = float(incident['rootTime'])
            if any(fid == fight_id and mech == mechanic and abs(sec-t) <= 2 for fid, mech, sec in seen_roots):
                continue
            seen_roots.append((fight_id, mechanic, t))
            root_buckets[incident.get('sessionBucket') or mechanic] += 1

    return {
        "schemaVersion": 3,
        "moduleVersion": MODULE_VERSION,
        "encounterID": rows[0].get("encounter", {}).get("encounterID") if rows else None,
        "difficultyID": rows[0].get("encounter", {}).get("difficultyID") if rows else None,
        "encounterName": rows[0].get("encounter", {}).get("name") if rows else None,
        "rootBuckets": [{'mechanic': k, 'count': v} for k,v in sorted(root_buckets.items())],
        "rootDedupePolicy": "fightID + rootMechanic + rootTime±2s",
        "tries": len(rows),
        "bestDurationSec": max(reached) if reached else 0,
        "scope": scope or {},
        "primaryCauseCounts": {titles.get(code, code): int(n) for code, n in primary_counts.items()},
        "problemOccurrenceTryCounts": {titles.get(code, code): int(n) for code, n in occurrence_counts.items()},
        "repeatedProblems": repeated,
        "reviews": [{
            "sequenceNo": seq,
            "originalTryNo": r.get("encounter", {}).get("tryNo"),
            "startTimestamp": r.get("encounter", {}).get("startTimestamp"),
            "durationSec": r.get("encounter", {}).get("durationSec"),
            "verdict": r.get("verdict"),
        } for seq, r in enumerate(rows, 1)],
    }


def render_series(series: Dict[str, Any]) -> str:
    scope = series.get("scope") or {}
    max_days = int(scope.get("maxDays", 0) or 0)
    max_tries = int(scope.get("maxTries", 0) or 0)
    scope_parts = []
    scope_parts.append(f"최근 {max_days}일" if max_days else "기간 제한 없음")
    scope_parts.append(f"최대 {max_tries}트라이" if max_tries else "트라이 수 제한 없음")
    deduped = int(scope.get("duplicatesRemoved", 0) or 0)
    if deduped:
        scope_parts.append(f"중복 {deduped}건 제외")

    lines = [
        f"=== Raid Wipe Review · 트라이 누적 패턴 {VERSION_LABEL} ===",
        f"{series.get('encounterName','?')} · 난이도ID {series.get('difficultyID','?')} · {series.get('tries',0)}트라이 · 최장 {fmt_time(series.get('bestDurationSec',0))}",
        f"집계 범위: {' · '.join(scope_parts)}",
        "",
        "--- 반복되는 전멸 주원인 ---",
    ]
    rows = series.get("repeatedProblems") or []
    primary_rows = [x for x in rows if int(x.get("primaryCount", 0) or 0) > 0]
    observation_rows = [x for x in rows if int(x.get("primaryCount", 0) or 0) == 0]
    if primary_rows:
        for i, x in enumerate(primary_rows[:8], 1):
            lines.append(f"{i}. {x['title']} · 주원인 {x['primaryCount']}회 / 발생 {x['occurrenceTryCount']}트라이")
    else:
        lines.append("집계된 주원인 없음")

    lines += ["", "--- 반복 관측된 보조 문제 ---"]
    if observation_rows:
        for i, x in enumerate(observation_rows[:8], 1):
            lines.append(f"{i}. {x['title']} · 발생 {x['occurrenceTryCount']}트라이 / 주원인 0회")
    else:
        lines.append("반복 관측된 보조 문제 없음")

    lines += ["", "--- 시간순 트라이 복기 (세션 Try 번호와 별도) ---"]
    for r in series.get("reviews", []):
        v = r.get("verdict") or {}
        lines.append(
            f"- #{r.get('sequenceNo')} · {r.get('startTimestamp','?')} · 원본 Try {r.get('originalTryNo')} · "
            f"{fmt_time(r.get('durationSec',0))} → {v.get('title','?')}"
        )
    lines += [
        "",
        "※ 누적 번호(#)는 실제 시작시각을 datetime으로 파싱해 새로 매깁니다. 같은 encounterID+startTimestamp는 한 번만 집계합니다.",
        "※ 주원인 반복과 보조 관측 반복을 분리해, 잔손실이 전멸 원인보다 위에 올라오지 않도록 정렬합니다.",
    ]
    if series.get('rootBuckets'):
        lines.append('--- 개인·공대 원인 사건 집계 (중복 제거) ---')
        lines.extend(f"- {x['mechanic']}: {x['count']}건" for x in series['rootBuckets'])
    return "\n".join(lines) + "\n"


def _apply_persisted_try_overrides(data: Dict[str, Any], try_dir: Path) -> Dict[str, Any]:
    """Overlay user-owned classification stored in meta.json onto raid facts.

    raid_facts.json can be older than a manual reset→normal decision (for example
    after an app restart before the async rebuild finished).  meta.json is the
    durable source for that explicit decision.
    """
    meta_path = Path(try_dir) / "meta.json"
    if not meta_path.exists():
        return data
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return data
    if not isinstance(meta, dict):
        return data
    enc = dict(data.get("encounter") or {})
    for key in ("analysisOverride", "analysisOverrideAt", "resetPull", "autoResetPull",
                "resetReason", "resetMaxDurationSec"):
        if key in meta:
            enc[key] = meta.get(key)
    data["encounter"] = enc
    if "analysisExcluded" in meta:
        data["analysisExcluded"] = meta.get("analysisExcluded")
    return data


def _review_cache_signature(src: Path, encounter_id: int) -> Dict[str, Any]:
    stat = src.stat()
    meta_path = src.parent / "meta.json"
    meta_stat = meta_path.stat() if meta_path.exists() else None
    _, _, rule_fp = _load_rule_bundle(encounter_id)
    return {
        "moduleVersion": MODULE_VERSION,
        "raidFactsSize": int(stat.st_size),
        "raidFactsMtimeNs": int(stat.st_mtime_ns),
        "metaSize": int(meta_stat.st_size) if meta_stat else None,
        "metaMtimeNs": int(meta_stat.st_mtime_ns) if meta_stat else None,
        "ruleFingerprint": rule_fp,
        "rawMtimeNs": resolve_raw(src.parent).stat().st_mtime_ns if raw_exists(src.parent) else None,
    }


def _cached_review_if_valid(try_dir: Path) -> Optional[Dict[str, Any]]:
    src = try_dir / "raid_facts.json"
    cached_path = try_dir / "raid_review.json"
    if not src.exists() or not cached_path.exists():
        return None
    try:
        review = json.loads(cached_path.read_text(encoding="utf-8"))
        if not isinstance(review, dict):
            return None
        encounter_id = int((review.get("encounter") or {}).get("encounterID", 0) or 0)
        expected = _review_cache_signature(src, encounter_id)
        cache = review.get("_cache") or {}
        if all(cache.get(k) == v for k, v in expected.items()):
            return review
    except Exception:
        return None
    return None


def process_try_dir(try_dir: Path, write: bool = True, use_cache: bool = True) -> Optional[Dict[str, Any]]:
    try_dir = Path(try_dir)
    src = try_dir / "raid_facts.json"
    if not src.exists():
        return None
    # Rebuild stale facts; parser failures propagate instead of silently using
    # old spec IDs or incomplete causal evidence.
    try:
        old = json.loads(src.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    raw = resolve_raw(try_dir)
    if raw.exists() and (old.get('incidentSchemaVersion') != INCIDENT_SCHEMA_VERSION or raw.stat().st_mtime_ns > src.stat().st_mtime_ns):
        from .raid_facts import extract_raid_facts
        old = _apply_persisted_try_overrides(old, try_dir)
        fresh = extract_raid_facts(raw, old.get('encounter') or {})
        fresh = _apply_persisted_try_overrides(fresh, try_dir)
        if write:
            atomic_write_json(src, fresh)
        else:
            enc = fresh.get('encounter') or {}
            return analyze_try(fresh) if float(enc.get('durationSec') or 0) >= 5 and str(enc.get('result','')).upper() in {'KILL','WIPE'} else None
    if use_cache:
        cached = _cached_review_if_valid(try_dir)
        if cached is not None:
            return cached
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        return None
    data = _apply_persisted_try_overrides(data, try_dir)
    if str(data.get("encounter", {}).get("result", "")).upper() not in {"WIPE", "KILL"}:
        return None
    if float(data.get("encounter", {}).get("durationSec", 0.0) or 0.0) < 5.0:
        return None
    review = analyze_try(data)
    encounter_id = int((review.get("encounter") or {}).get("encounterID", 0) or 0)
    review["_cache"] = _review_cache_signature(src, encounter_id)
    if write:
        json_path = try_dir / "raid_review.json"
        txt_path = try_dir / "raid_review.txt"
        atomic_write_json(json_path, review)
        atomic_write_text(txt_path, render_try(review))
        # Keep legacy B0 filenames during the transition so old scripts do not break.
        atomic_write_json(try_dir / "raid_review_b0.json", review)
        atomic_write_text(try_dir / "raid_review_b0.txt", render_try(review))
    return review


def _series_meta_match(meta: Dict[str, Any], encounter_id: int, difficulty_id: int) -> bool:
    return (
        int(meta.get("encounterID", 0) or 0) == int(encounter_id)
        and int(meta.get("difficultyID", 0) or 0) == int(difficulty_id)
        and str(meta.get("result", "")).upper() in {"WIPE", "KILL"}
        and float(meta.get("durationSec", 0.0) or 0.0) >= 5.0
    )


def _series_candidate_dirs(storage_root: Path, encounter_id: int, difficulty_id: int) -> tuple[List[Dict[str, Any]], int]:
    records: List[Dict[str, Any]] = []
    for meta_path in storage_root.rglob("meta.json"):
        if "_series" in meta_path.parts or "_active" in meta_path.parts:
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(meta, dict) or not _series_meta_match(meta, encounter_id, difficulty_id):
            continue
        try_dir = meta_path.parent
        if not (try_dir / "raid_facts.json").exists():
            continue
        records.append({
            "tryDir": try_dir,
            "startTimestamp": meta.get("startTimestamp"),
            "startDt": _parse_start(meta.get("startTimestamp")),
            "meta": meta,
        })

    # Tests/legacy imports may contain raid_facts without meta.json.  Only take the
    # more expensive fallback if the normal LivePullStore metadata scan found none.
    if not records:
        for src in storage_root.rglob("raid_facts.json"):
            if "_series" in src.parts or "_active" in src.parts:
                continue
            try:
                data = json.loads(src.read_text(encoding="utf-8"))
            except Exception:
                continue
            enc = data.get("encounter", {})
            if not _series_meta_match(enc, encounter_id, difficulty_id):
                continue
            records.append({
                "tryDir": src.parent,
                "startTimestamp": enc.get("startTimestamp"),
                "startDt": _parse_start(enc.get("startTimestamp")),
                "meta": enc,
            })

    # encounterID + startTimestamp identifies a pull across reconnect/test copies.
    # If startTimestamp is missing, keep the folder unique rather than collapsing it.
    dedup: Dict[tuple, Dict[str, Any]] = {}
    duplicate_count = 0
    for row in sorted(records, key=lambda x: (x["startDt"], str(x["tryDir"]))):
        stamp = str(row.get("startTimestamp") or "").strip()
        key = (int(encounter_id), stamp) if stamp else (int(encounter_id), str(row["tryDir"].resolve()))
        if key in dedup:
            duplicate_count += 1
            # Prefer the copy with a cached review, then the newer metadata file.
            old = dedup[key]
            old_cached = (old["tryDir"] / "raid_review.json").exists()
            new_cached = (row["tryDir"] / "raid_review.json").exists()
            if new_cached and not old_cached:
                dedup[key] = row
            continue
        dedup[key] = row
    return list(dedup.values()), duplicate_count


def _apply_series_scope(records: List[Dict[str, Any]], settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = sorted(records, key=lambda x: x.get("startDt") or datetime.min)
    max_days = int(settings.get("maxDays", 0) or 0)
    max_tries = int(settings.get("maxTries", 0) or 0)
    valid_dates = [x.get("startDt") for x in rows if x.get("startDt") and x.get("startDt") != datetime.min]
    if max_days > 0 and valid_dates:
        latest = max(valid_dates)
        cutoff = latest - timedelta(days=max_days)
        rows = [x for x in rows if x.get("startDt") == datetime.min or x.get("startDt") >= cutoff]
    if max_tries > 0 and len(rows) > max_tries:
        rows = rows[-max_tries:]
    return rows


def process_encounter_series(storage_root: Path, encounter_id: int, difficulty_id: int, write: bool = True) -> Optional[Dict[str, Any]]:
    storage_root = Path(storage_root)
    settings, setting_warnings = _load_series_settings()
    candidates, duplicate_count = _series_candidate_dirs(storage_root, encounter_id, difficulty_id)
    scoped = _apply_series_scope(candidates, settings)

    reviews: List[Dict[str, Any]] = []
    cache_hits = 0
    cache_misses = 0
    for row in scoped:
        try_dir = Path(row["tryDir"])
        cached_before = _cached_review_if_valid(try_dir)
        if cached_before is not None:
            review = cached_before
            cache_hits += 1
        else:
            # RC4.2: a series recompute must never rewrite *other* Tries.  Their
            # stale-cache review is rebuilt in memory only; writing it here used
            # to drop the BossMechanic link (presentationVerdict) of earlier Tries
            # every time a new Try was imported or one Try was reanalysed.
            review = process_try_dir(try_dir, write=False, use_cache=False)
            cache_misses += 1
        if review:
            reviews.append(review)
    if not reviews:
        return None

    scope = {
        "maxDays": int(settings.get("maxDays", 0) or 0),
        "maxTries": int(settings.get("maxTries", 0) or 0),
        "matchedBeforeScope": len(candidates),
        "includedAfterScope": len(scoped),
        "duplicatesRemoved": int(duplicate_count),
        "cacheHits": int(cache_hits),
        "cacheMisses": int(cache_misses),
        "warnings": setting_warnings,
    }
    series = analyze_series(reviews, scope=scope)
    if write:
        out_dir = storage_root / "_series" / f"{int(encounter_id)}_d{int(difficulty_id)}"
        out_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(out_dir / "raid_series_review.json", series)
        atomic_write_text(out_dir / "raid_series_review.txt", render_series(series))
    return series


def process_live_try(try_dir: Path, storage_root: Path) -> Dict[str, Any]:
    try_dir = Path(try_dir)
    review = process_try_dir(try_dir, write=True)
    if not review:
        return {"raidReviewStatus": "skipped"}
    e = review.get("encounter", {})
    encounter_id = int(e.get("encounterID", 0) or 0)
    difficulty_id = int(e.get("difficultyID", 0) or 0)
    series = process_encounter_series(Path(storage_root), encounter_id, difficulty_id, write=True)
    series_dir = Path(storage_root) / "_series" / f"{encounter_id}_d{difficulty_id}"
    info = {
        "raidReviewStatus": "ready",
        "raidReviewJson": str(try_dir / "raid_review.json"),
        "raidReviewTxt": str(try_dir / "raid_review.txt"),
        "raidReview": review,
        "raidReviewWarnings": list(review.get("warnings") or []),
    }
    if series:
        series_warnings = list((series.get("scope") or {}).get("warnings") or [])
        info.update({
            "raidSeriesReviewStatus": "ready",
            "raidSeriesReviewJson": str(series_dir / "raid_series_review.json"),
            "raidSeriesReviewTxt": str(series_dir / "raid_series_review.txt"),
            "raidSeriesReview": series,
            "raidSeriesWarnings": series_warnings,
        })
    meta_path = try_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(meta, dict):
                series_txt = series_dir / "raid_series_review.txt"
                series_json = series_dir / "raid_series_review.json"
                meta.update({
                    "raidReviewModuleVersion": MODULE_VERSION,
                    "raidReviewJson": "raid_review.json",
                    "raidReviewTxt": "raid_review.txt",
                    # Keep persistent metadata relocatable. Runtime info above remains
                    # absolute for the currently running GUI process.
                    "raidSeriesReviewJson": os.path.relpath(series_json, start=try_dir) if series else None,
                    "raidSeriesReviewTxt": os.path.relpath(series_txt, start=try_dir) if series else None,
                })
                atomic_write_json(meta_path, meta)
        except Exception as exc:
            LOGGER.warning("failed to update live meta %s: %s", meta_path, exc)
    return info
