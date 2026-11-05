from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .encounter_tracker import parse_combat_datetime, parse_log_line
from . import progress as _live_progress
from .rawlog import resolve_raw, open_raw_text
from .io_utils import atomic_write_json, atomic_write_text

# Live Review v0.2-A.1: 판단이 아니라 '관측 사실'만 추출한다.
# 다음 단계(v0.2-B)에서 Top30/NSRT/phase 기준과 비교해 의미를 붙인다.

DAMAGE_EVENTS = {
    "SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "SPELL_DAMAGE_SUPPORT",
    "SPELL_PERIODIC_DAMAGE_SUPPORT", "RANGE_DAMAGE", "RANGE_DAMAGE_SUPPORT",
    "SWING_DAMAGE", "SWING_DAMAGE_LANDED", "SWING_DAMAGE_LANDED_SUPPORT",
}
HEAL_EVENTS = {"SPELL_HEAL", "SPELL_PERIODIC_HEAL"}
DEATH_EVENTS = {"UNIT_DIED", "UNIT_DESTROYED", "SPELL_INSTAKILL"}
CAST_SUCCESS = "SPELL_CAST_SUCCESS"
CAST_FAILED = "SPELL_CAST_FAILED"
AURA_EVENTS = {
    "SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH", "SPELL_AURA_REMOVED",
    "SPELL_AURA_APPLIED_DOSE", "SPELL_AURA_REMOVED_DOSE",
}

SPEC_NAMES = {259: "암살", 261: "잠행"}

DEFENSIVES = {
    1966: "교란",
    31224: "그림자 망토",
    5277: "회피",
    185311: "진홍색 약병",
}

MAJOR_COOLDOWNS = {
    # 공통 / 암살
    1856: "소멸",
    1297761: "탐식을 일삼는 울라텍의 심장",
    360194: "죽음표식",
    385627: "왕의 파멸",
    381623: "엉겅퀴 차",
    1236994: "무모함의 물약",
    # 잠행
    185313: "어둠의 춤",
    121471: "어둠의 칼날",
    280719: "은밀한 기술",
}

POTION_KEYWORDS = ("물약", "potion", "ポーション", "药水", "藥水", "poción", "poção")


def _safe_num(value):
    try:
        if value is None or str(value).strip().lower() in {"", "nil", "none"}:
            return None
        return float(value)
    except Exception:
        return None


def _safe_int(value):
    v = _safe_num(value)
    return int(v) if v is not None else None


def _is_player_name(log_name: str | None, player: str) -> bool:
    text = str(log_name or "").strip().strip('"')
    return text == player or text.startswith(player + "-")


def _spell_info(event: str, fields: list[str]):
    if event.startswith("SPELL_") or event.startswith("RANGE_"):
        if len(fields) >= 12:
            return _safe_int(fields[9]), fields[10], 12
    return None, None, 9


def _advanced_target_hp(fields: list[str], extra_start: int):
    # Advanced Combat Logging v22: spell/common fields 뒤 19-field advanced block.
    # infoGUID, ownerGUID, currentHP, maxHP, ...
    if len(fields) < extra_start + 19:
        return None, None
    hp = _safe_num(fields[extra_start + 2])
    max_hp = _safe_num(fields[extra_start + 3])
    return hp, max_hp


def _event_amount(event: str, fields: list[str], extra_start: int):
    # Advanced block(19 fields) 뒤부터 이벤트 고유 payload가 시작한다.
    idx = extra_start + 19
    if len(fields) <= idx:
        return None
    return _safe_num(fields[idx])


def _damage_overkill(fields: list[str], extra_start: int):
    # v22 damage payload observed: amount, originalAmount, overkill, school, ...
    idx = extra_start + 21
    if len(fields) <= idx:
        return None
    return _safe_num(fields[idx])


def _heal_overheal(fields: list[str], extra_start: int):
    # v22 heal payload observed: amount, originalAmount, overheal, absorbed, critical
    idx = extra_start + 21
    if len(fields) <= idx:
        return None
    return _safe_num(fields[idx])


def _relative_sec(ts: str, start_dt: datetime | None):
    if start_dt is None:
        return None
    dt = parse_combat_datetime(ts, now=start_dt)
    if not dt:
        return None
    sec = (dt - start_dt).total_seconds()
    if sec < -43200:
        sec += 86400
    return round(max(0.0, sec), 4)


def _fmt_sec(sec):
    if sec is None:
        return "-"
    sec = float(sec)
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m}:{s:05.2f}"


def _fmt_amount(value):
    if value is None:
        return "-"
    x = float(value)
    if abs(x) >= 1_000_000:
        return f"{x/1_000_000:.2f}M"
    if abs(x) >= 1_000:
        return f"{x/1_000:.1f}K"
    return f"{x:.0f}"


def _infer_spec(combatant_rows: dict[str, list[str]], player_guids: set[str], seen_spell_ids: set[int]):
    for guid in player_guids:
        fields = combatant_rows.get(guid)
        if not fields:
            continue
        # 기존 multiboss index와 동일한 specID 탐색 규칙.
        if len(fields) > 25:
            sid = _safe_int(fields[25])
            if sid in SPEC_NAMES:
                return sid
        for x in fields[20:36]:
            sid = _safe_int(x)
            if sid in SPEC_NAMES:
                return sid
    if seen_spell_ids & {185313, 121471, 185438, 196819, 197835, 280719}:
        return 261
    if seen_spell_ids & {360194, 385627, 32645, 1329, 703, 1943}:
        return 259
    return None


def extract_pull_facts(raw_path: Path | str, encounter: dict, player: str, death_lookback_sec: float = 8.0) -> dict:
    raw_path = Path(raw_path)
    start_ts = str(encounter.get("startTimestamp") or "")
    start_dt = parse_combat_datetime(start_ts) if start_ts else None

    player_guids: set[str] = set()
    combatant_rows: dict[str, list[str]] = {}
    seen_spell_ids: set[int] = set()

    player_events = 0
    raid_deaths = []
    player_deaths = []
    player_active_cast_secs = []
    incoming_damage = []
    received_heals = []
    defensives = []
    cooldowns = []
    cast_failures = []
    target_rows: dict[str, dict] = {}
    aura_rows = []

    raw_path = resolve_raw(raw_path)
    _lp = _live_progress.file_progress(raw_path, "read")
    with open_raw_text(raw_path) as fh:
        for raw in fh:
            _lp.tick(raw)
            parsed = parse_log_line(raw)
            if not parsed:
                continue
            event = parsed["event"]
            fields = parsed["fields"]
            sec = _relative_sec(parsed["timestamp"], start_dt)

            if event == "COMBATANT_INFO" and len(fields) > 1:
                combatant_rows[fields[1]] = fields
                continue

            source_guid = fields[1] if len(fields) > 1 else ""
            source_name = fields[2] if len(fields) > 2 else ""
            dest_guid = fields[5] if len(fields) > 5 else ""
            dest_name = fields[6] if len(fields) > 6 else ""
            src_me = _is_player_name(source_name, player)
            dst_me = _is_player_name(dest_name, player)
            if src_me and source_guid:
                player_guids.add(source_guid)
            if dst_me and dest_guid:
                player_guids.add(dest_guid)
            if src_me or dst_me:
                player_events += 1

            spell_id, spell_name, extra_start = _spell_info(event, fields)
            if src_me and spell_id is not None:
                seen_spell_ids.add(spell_id)

            # 공대 사망 타임라인: Player-* GUID만. NPC 사망은 제외.
            if event in DEATH_EVENTS and len(fields) > 6:
                death = {
                    "sec": sec,
                    "timestamp": parsed["timestamp"],
                    "guid": dest_guid,
                    "name": dest_name,
                    "isPlayer": str(dest_guid).startswith("Player-"),
                }
                if death["isPlayer"]:
                    raid_deaths.append(death)
                if dst_me:
                    player_deaths.append(death)
                continue

            if event in DAMAGE_EVENTS and dst_me:
                amount = _event_amount(event, fields, extra_start)
                hp, max_hp = _advanced_target_hp(fields, extra_start)
                row = {
                    "sec": sec,
                    "timestamp": parsed["timestamp"],
                    "event": event,
                    "sourceGUID": source_guid,
                    "sourceName": source_name,
                    "spellID": spell_id,
                    "spellName": spell_name or ("근접 공격" if event.startswith("SWING_") else event),
                    "amount": amount,
                    "overkill": _damage_overkill(fields, extra_start),
                    "currentHP": hp,
                    "maxHP": max_hp,
                    "hpPct": round(hp / max_hp * 100.0, 2) if hp is not None and max_hp and max_hp > 0 else None,
                }
                incoming_damage.append(row)

            if event in HEAL_EVENTS and dst_me:
                amount = _event_amount(event, fields, extra_start)
                overheal = _heal_overheal(fields, extra_start)
                hp, max_hp = _advanced_target_hp(fields, extra_start)
                effective = None
                if amount is not None:
                    effective = max(0.0, amount - max(0.0, overheal or 0.0))
                received_heals.append({
                    "sec": sec,
                    "timestamp": parsed["timestamp"],
                    "sourceGUID": source_guid,
                    "sourceName": source_name,
                    "spellID": spell_id,
                    "spellName": spell_name or event,
                    "amount": amount,
                    "overheal": overheal,
                    "effective": effective,
                    "currentHP": hp,
                    "maxHP": max_hp,
                    "hpPct": round(hp / max_hp * 100.0, 2) if hp is not None and max_hp and max_hp > 0 else None,
                })

            if event == CAST_SUCCESS and src_me:
                if sec is not None:
                    player_active_cast_secs.append(float(sec))
                lower_name = str(spell_name or "").lower()
                if spell_id in DEFENSIVES:
                    defensives.append({"sec": sec, "spellID": spell_id, "spellName": DEFENSIVES[spell_id]})
                if spell_id in MAJOR_COOLDOWNS or any(k in lower_name for k in POTION_KEYWORDS):
                    cooldowns.append({
                        "sec": sec,
                        "spellID": spell_id,
                        "spellName": MAJOR_COOLDOWNS.get(spell_id, spell_name or "물약"),
                    })

            if event == CAST_FAILED and src_me:
                reason = fields[12] if len(fields) > 12 else ""
                cast_failures.append({
                    "sec": sec,
                    "spellID": spell_id,
                    "spellName": spell_name or "알 수 없는 기술",
                    "reason": reason,
                })

            if event in AURA_EVENTS and dst_me and spell_id in DEFENSIVES:
                aura_rows.append({
                    "sec": sec,
                    "event": event,
                    "spellID": spell_id,
                    "spellName": DEFENSIVES.get(spell_id, spell_name or str(spell_id)),
                })

            if event in DAMAGE_EVENTS and src_me and dest_guid and not dst_me:
                amount = _event_amount(event, fields, extra_start) or 0.0
                key = dest_guid or dest_name or "unknown"
                row = target_rows.setdefault(key, {
                    "guid": dest_guid,
                    "name": dest_name or "알 수 없는 대상",
                    "firstHitSec": sec,
                    "lastHitSec": sec,
                    "hitCount": 0,
                    "damage": 0.0,
                })
                if row["firstHitSec"] is None or (sec is not None and sec < row["firstHitSec"]):
                    row["firstHitSec"] = sec
                if sec is not None:
                    row["lastHitSec"] = sec
                row["hitCount"] += 1
                row["damage"] += float(amount)

    spec_id = _infer_spec(combatant_rows, player_guids, seen_spell_ids)
    duration = encounter.get("durationSec")
    if duration is None:
        duration = 0.0

    # 한 트라이에서 전투부활/추종자 자동부활 등으로 같은 플레이어가 여러 번 죽을 수 있다.
    # CombatLog에 SPELL_RESURRECT가 항상 남는 것은 아니므로, 사망 뒤 본인의
    # SPELL_CAST_SUCCESS가 다시 나타나면 "행동 재개" 사실로만 기록한다.
    enriched_player_deaths = []
    for i, d in enumerate(player_deaths):
        row = dict(d)
        row["deathNo"] = i + 1
        dsec = row.get("sec")
        next_death_sec = player_deaths[i + 1].get("sec") if i + 1 < len(player_deaths) else None
        resumed = None
        if dsec is not None:
            for asec in player_active_cast_secs:
                if asec <= float(dsec) + 0.05:
                    continue
                if next_death_sec is not None and asec >= float(next_death_sec):
                    break
                resumed = asec
                break
        row["resumedAtSec"] = round(resumed, 3) if resumed is not None else None
        row["resumedAfterSec"] = round(resumed - float(dsec), 3) if resumed is not None and dsec is not None else None
        enriched_player_deaths.append(row)

    unresolved_deaths = [d for d in enriched_player_deaths if d.get("resumedAtSec") is None]
    death = unresolved_deaths[-1] if unresolved_deaths else None
    death_sec = death.get("sec") if death else None
    death_start = None if death_sec is None else max(0.0, float(death_sec) - float(death_lookback_sec))
    death_damage = []
    death_heals = []
    if death_sec is not None:
        death_damage = [x for x in incoming_damage if x.get("sec") is not None and death_start <= x["sec"] <= death_sec + 0.001]
        death_heals = [x for x in received_heals if x.get("sec") is not None and death_start <= x["sec"] <= death_sec + 0.001]
        for x in death_damage:
            x["beforeDeathSec"] = round(float(x["sec"]) - float(death_sec), 3)
        for x in death_heals:
            x["beforeDeathSec"] = round(float(x["sec"]) - float(death_sec), 3)

    damage_taken_total = sum(float(x.get("amount") or 0.0) for x in incoming_damage)
    healing_received_total = sum(float(x.get("amount") or 0.0) for x in received_heals)
    effective_healing_total = sum(float(x.get("effective") or 0.0) for x in received_heals)

    cd_counts = Counter(x["spellName"] for x in cooldowns)
    defensive_counts = Counter(x["spellName"] for x in defensives)
    cast_failure_reasons = Counter((x.get("reason") or "미상") for x in cast_failures)
    cast_failures_non_unavailable = [x for x in cast_failures if (x.get("reason") or "") != "아직 사용 불가"]

    targets = sorted(target_rows.values(), key=lambda x: (-float(x.get("damage") or 0.0), float(x.get("firstHitSec") or 999999)))
    total_outgoing_target_damage = sum(float(x.get("damage") or 0.0) for x in targets)
    for row in targets:
        row["damage"] = round(float(row["damage"]), 3)
        row["sharePct"] = round(row["damage"] / total_outgoing_target_damage * 100.0, 2) if total_outgoing_target_damage > 0 else 0.0

    facts = {
        "schemaVersion": 1,
        "moduleVersion": "LiveRaidReview-v0.2-A.1",
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "player": player,
        "playerFound": bool(player_events or player_guids),
        "playerGUIDs": sorted(player_guids),
        "specID": spec_id,
        "specName": SPEC_NAMES.get(spec_id, "미확인"),
        "encounter": {
            "encounterID": encounter.get("encounterID"),
            "name": encounter.get("name"),
            "difficultyID": encounter.get("difficultyID"),
            "difficultyName": encounter.get("difficultyName"),
            "tryNo": encounter.get("tryNo"),
            "result": encounter.get("result"),
            "durationSec": encounter.get("durationSec"),
            "startTimestamp": encounter.get("startTimestamp"),
            "endTimestamp": encounter.get("endTimestamp"),
        },
        "summary": {
            "playerEvents": player_events,
            "survivedToEncounterEnd": death is None,
            "playerDeathSec": death_sec,
            "playerDeathEvents": len(enriched_player_deaths),
            "playerResumedAfterDeath": sum(1 for d in enriched_player_deaths if d.get("resumedAtSec") is not None),
            "damageTakenTotal": round(damage_taken_total, 3),
            "healingReceivedTotal": round(healing_received_total, 3),
            "effectiveHealingReceivedTotal": round(effective_healing_total, 3),
            "raidPlayerDeaths": len(raid_deaths),
            "raidUniquePlayersDied": len({d.get("guid") for d in raid_deaths if d.get("guid")}),
            "castFailures": len(cast_failures),
            "targetCount": len(targets),
        },
        "playerDeath": death,
        "playerDeaths": enriched_player_deaths,
        "deathLookback": {
            "seconds": float(death_lookback_sec),
            "damageTotal": round(sum(float(x.get("amount") or 0.0) for x in death_damage), 3),
            "healTotal": round(sum(float(x.get("amount") or 0.0) for x in death_heals), 3),
            "effectiveHealTotal": round(sum(float(x.get("effective") or 0.0) for x in death_heals), 3),
            "damageEvents": death_damage[-30:],
            "healEvents": death_heals[-30:],
        },
        "raidDeaths": raid_deaths,
        "defensives": defensives,
        "defensiveCounts": dict(defensive_counts),
        "defensiveAuras": aura_rows,
        "cooldowns": cooldowns,
        "cooldownCounts": dict(cd_counts),
        "castFailures": cast_failures[-100:],
        "castFailureSummary": {
            "reasonCounts": dict(cast_failure_reasons),
            "nonUnavailableCount": len(cast_failures_non_unavailable),
            "nonUnavailable": cast_failures_non_unavailable[-30:],
        },
        "targets": targets[:20],
    }
    return facts


def render_facts_text(facts: dict) -> str:
    e = facts.get("encounter") or {}
    s = facts.get("summary") or {}
    player = facts.get("player") or "?"
    spec = facts.get("specName") or "미확인"
    lines = [
        f"=== Live 트라이 사실 추출 v0.2-A.1 ===",
        f"[{player}] {spec} · {e.get('name','?')} · {e.get('difficultyName','-')} · Try {e.get('tryNo','?')} · {e.get('result','-')} · {_fmt_sec(e.get('durationSec'))}",
        "※ 아직 잘했다/못했다 판정 없음. raw CombatLog에서 확인된 사실만 표시.",
        "",
    ]

    if not facts.get("playerFound"):
        lines += [
            "[플레이어] 선택 캐릭터 관련 이벤트를 이 트라이 raw.log에서 찾지 못했습니다.",
            "현재 GUI 캐릭터 선택과 실제 플레이 캐릭터가 같은지 확인하세요.",
        ]
        return "\n".join(lines) + "\n"

    death = facts.get("playerDeath")
    player_deaths = facts.get("playerDeaths") or []
    lines.append("--- 생존 / 사망 ---")
    if len(player_deaths) > 1 or any(d.get("resumedAtSec") is not None for d in player_deaths):
        parts = []
        for d in player_deaths:
            label = f"{d.get('deathNo','?')}차 {_fmt_sec(d.get('sec'))}"
            if d.get("resumedAtSec") is not None:
                label += f" → {_fmt_sec(d.get('resumedAtSec'))} 행동 재개"
            parts.append(label)
        lines.append("사망 기록: " + " / ".join(parts))
    if death:
        lines.append(f"사망: {_fmt_sec(death.get('sec'))} · {death.get('name') or player}")
        lb = facts.get("deathLookback") or {}
        lines.append(
            f"사망 전 {lb.get('seconds',8):g}초: 받은 피해 {_fmt_amount(lb.get('damageTotal'))} / "
            f"회복 {_fmt_amount(lb.get('healTotal'))} (유효 {_fmt_amount(lb.get('effectiveHealTotal'))})"
        )
        dmg = lb.get("damageEvents") or []
        if dmg:
            lines.append("마지막 피해:")
            for row in dmg[-8:]:
                hp = f" · HP {row.get('hpPct'):.1f}%" if row.get("hpPct") is not None else ""
                before = row.get("beforeDeathSec")
                before_text = f"{before:+.2f}s" if before is not None else ""
                lines.append(
                    f"  {_fmt_sec(row.get('sec'))} ({before_text})  {row.get('sourceName') or '?'} · "
                    f"{row.get('spellName') or row.get('event')}  {_fmt_amount(row.get('amount'))}{hp}"
                )
        heals = lb.get("healEvents") or []
        if heals:
            lines.append("마지막 회복:")
            for row in heals[-5:]:
                hp = f" · HP {row.get('hpPct'):.1f}%" if row.get("hpPct") is not None else ""
                lines.append(
                    f"  {_fmt_sec(row.get('sec'))}  {row.get('sourceName') or '?'} · {row.get('spellName') or '?'}  "
                    f"{_fmt_amount(row.get('amount'))} (유효 {_fmt_amount(row.get('effective'))}){hp}"
                )
    else:
        if player_deaths:
            lines.append("최종 미복귀 사망 없음 · 사망 뒤 행동 재개가 확인되어 전투 종료 생존으로 기록")
        else:
            lines.append(f"사망 이벤트 없음 · 전투 종료까지 생존 기록")

    lines += ["", "--- 개인 생존기 사용 ---"]
    defensives = facts.get("defensives") or []
    if defensives:
        by_name = defaultdict(list)
        for row in defensives:
            by_name[row.get("spellName") or str(row.get("spellID"))].append(row.get("sec"))
        for name, times in by_name.items():
            lines.append(f"{name}: {len(times)}회 · " + ", ".join(_fmt_sec(x) for x in times))
    else:
        lines.append("기록된 교란/그망/회피/진홍색 약병 캐스트 없음")

    lines += ["", "--- 주요 쿨기 사용 ---"]
    cds = facts.get("cooldowns") or []
    if cds:
        by_name = defaultdict(list)
        for row in cds:
            by_name[row.get("spellName") or str(row.get("spellID"))].append(row.get("sec"))
        for name, times in by_name.items():
            lines.append(f"{name}: {len(times)}회 · " + ", ".join(_fmt_sec(x) for x in times))
    else:
        lines.append("이 트라이에서 추적 중인 주요 쿨기 캐스트 없음")

    fails = facts.get("castFailures") or []
    fail_summary = facts.get("castFailureSummary") or {}
    lines += ["", "--- 실패 입력 ---"]
    if fails:
        reason_counts = fail_summary.get("reasonCounts") or {}
        if reason_counts:
            lines.append("사유 집계: " + " / ".join(f"{k} {v}회" for k, v in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))))
        notable = fail_summary.get("nonUnavailable") or []
        if notable:
            lines.append("'아직 사용 불가' 외 확인할 입력:")
            for row in notable[-10:]:
                reason = f" · {row.get('reason')}" if row.get("reason") else ""
                lines.append(f"  {_fmt_sec(row.get('sec'))}  {row.get('spellName') or '?'}{reason}")
        else:
            lines.append("'아직 사용 불가' 외 다른 실패 사유 없음")
    else:
        lines.append("SPELL_CAST_FAILED 없음")

    lines += ["", "--- 피해 대상 · 첫 타격 사실 ---"]
    targets = facts.get("targets") or []
    if targets:
        for row in targets[:10]:
            lines.append(
                f"{row.get('name') or '?'} · 첫 {_fmt_sec(row.get('firstHitSec'))} · "
                f"{row.get('hitCount',0)} hit · {_fmt_amount(row.get('damage'))} · {row.get('sharePct',0):.1f}%"
            )
    else:
        lines.append("선택 캐릭터의 피해 대상 기록 없음")

    deaths = facts.get("raidDeaths") or []
    lines += ["", "--- 공대원 사망 타임라인 ---"]
    if deaths:
        seen_death_counts = defaultdict(int)
        for row in deaths[:20]:
            key = row.get("guid") or row.get("name") or "?"
            seen_death_counts[key] += 1
            suffix = f" [{seen_death_counts[key]}차]" if seen_death_counts[key] > 1 else ""
            lines.append(f"{_fmt_sec(row.get('sec'))}  {row.get('name') or row.get('guid') or '?'}{suffix}")
    else:
        lines.append("Player-* UNIT_DIED 기록 없음")

    return "\n".join(lines) + "\n"


def extract_and_write(raw_path: Path | str, encounter: dict, player: str) -> dict:
    raw_path = Path(raw_path)
    facts = extract_pull_facts(raw_path, encounter, player)
    out_dir = raw_path.parent
    json_path = out_dir / "facts.json"
    txt_path = out_dir / "facts.txt"
    atomic_write_json(json_path, facts)
    atomic_write_text(txt_path, render_facts_text(facts))
    meta_path = out_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(meta, dict):
                meta.update({
                    "factsModuleVersion": facts.get("moduleVersion"),
                    "factsPlayer": player,
                    "factsJson": "facts.json",
                    "factsTxt": "facts.txt",
                    "factsGeneratedAt": facts.get("generatedAt"),
                })
                atomic_write_json(meta_path, meta)
        except Exception:
            pass
    return {
        "factsStatus": "ready",
        "factsJson": str(json_path),
        "factsTxt": str(txt_path),
        "facts": facts,
    }
