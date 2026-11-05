from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from .io_utils import atomic_write_json, atomic_write_text

from .versions import ENGINE_VERSION, APP_BUILD
MODULE_VERSION = f"LiveRaidReview-{ENGINE_VERSION}"


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


def _pct(value):
    return None if value is None else round(float(value) * 100.0, 1)


def _active_defensive_auras(facts: dict, death_sec: float | None):
    if death_sec is None:
        return []
    states = {}
    names = {}
    for row in sorted(facts.get("defensiveAuras") or [], key=lambda x: float(x.get("sec") or 0.0)):
        sec = row.get("sec")
        if sec is None or float(sec) > float(death_sec) + 0.001:
            continue
        sid = row.get("spellID")
        names[sid] = row.get("spellName") or str(sid)
        ev = row.get("event") or ""
        if ev in {"SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH", "SPELL_AURA_APPLIED_DOSE"}:
            states[sid] = True
        elif ev in {"SPELL_AURA_REMOVED", "SPELL_AURA_REMOVED_DOSE"}:
            states[sid] = False
    return [names[sid] for sid, active in states.items() if active]


def analyze_death(facts: dict) -> dict:
    player = facts.get("player") or "?"
    encounter = dict(facts.get("encounter") or {})
    summary = facts.get("summary") or {}
    death = facts.get("playerDeath")
    deaths = facts.get("playerDeaths") or []
    lookback = facts.get("deathLookback") or {}

    review = {
        "schemaVersion": 1,
        "moduleVersion": MODULE_VERSION,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "player": player,
        "specID": facts.get("specID"),
        "specName": facts.get("specName"),
        "encounter": encounter,
        "status": "ready",
        "hasFinalDeath": bool(death),
        "classification": None,
        "confidence": None,
        "headline": None,
        "evidence": [],
        "cautions": [],
        "finalHit": None,
        "defensiveContext": {},
        "healingContext": {},
        "deathHistory": deaths,
        "deathIncidents": facts.get("deathIncidents") or [],
    }

    if not facts.get("playerFound"):
        review.update({
            "status": "no-player",
            "headline": "선택 캐릭터 관련 이벤트를 찾지 못해 사망 판정을 만들 수 없습니다.",
            "confidence": "none",
        })
        return review

    if not death:
        if deaths:
            review.update({
                "status": "no-final-death",
                "headline": "전투 중 사망 기록은 있으나 이후 행동 재개가 확인되어 최종 미복귀 사망은 없습니다.",
                "confidence": "high",
                "classification": "resumed_after_death",
            })
        else:
            review.update({
                "status": "survived",
                "headline": "이 트라이에서 선택 캐릭터의 사망 이벤트가 없습니다.",
                "confidence": "high",
                "classification": "survived",
            })
        return review

    death_sec = float(death.get("sec") or 0.0)
    damage_events = [x for x in (lookback.get("damageEvents") or []) if x.get("sec") is not None]
    heal_events = [x for x in (lookback.get("healEvents") or []) if x.get("sec") is not None]
    damage_events.sort(key=lambda x: float(x.get("sec") or 0.0))
    heal_events.sort(key=lambda x: float(x.get("sec") or 0.0))

    # Prefer a logged overkill event closest to UNIT_DIED, otherwise the final damage event.
    fatal_candidates = [
        x for x in damage_events
        if float(x.get("sec") or 0.0) <= death_sec + 0.05 and float(x.get("overkill") or 0.0) > 0
    ]
    fatal = fatal_candidates[-1] if fatal_candidates else (damage_events[-1] if damage_events else None)

    total_damage = float(lookback.get("damageTotal") or 0.0)
    final_amount = float(fatal.get("amount") or 0.0) if fatal else 0.0
    max_hp = float(fatal.get("maxHP") or 0.0) if fatal and fatal.get("maxHP") else None
    overkill = float(fatal.get("overkill") or 0.0) if fatal and fatal.get("overkill") is not None else None
    final_share = final_amount / total_damage if total_damage > 0 else None
    final_maxhp_share = final_amount / max_hp if max_hp and max_hp > 0 else None
    before_death = death_sec - float(fatal.get("sec") or death_sec) if fatal else None

    inferred_pre_hp = None
    inferred_pre_hp_pct = None
    if fatal and max_hp and max_hp > 0 and overkill is not None and overkill >= 0 and final_amount >= overkill:
        inferred_pre_hp = max(0.0, final_amount - overkill)
        inferred_pre_hp_pct = inferred_pre_hp / max_hp
    else:
        # last known post-event HP before fatal event
        prior_rows = []
        fatal_sec = float(fatal.get("sec") or death_sec) if fatal else death_sec
        for row in damage_events + heal_events:
            if row.get("sec") is None or float(row.get("sec")) >= fatal_sec - 1e-6:
                continue
            if row.get("currentHP") is not None and row.get("maxHP"):
                prior_rows.append(row)
        if prior_rows:
            last = max(prior_rows, key=lambda x: float(x.get("sec") or 0.0))
            inferred_pre_hp = float(last.get("currentHP") or 0.0)
            inferred_pre_hp_pct = inferred_pre_hp / float(last.get("maxHP")) if last.get("maxHP") else None

    # Damage concentration immediately before death.
    damage_2s = sum(float(x.get("amount") or 0.0) for x in damage_events if death_sec - float(x.get("sec") or 0.0) <= 2.0)
    damage_3s = sum(float(x.get("amount") or 0.0) for x in damage_events if death_sec - float(x.get("sec") or 0.0) <= 3.0)
    hits_2s = sum(1 for x in damage_events if death_sec - float(x.get("sec") or 0.0) <= 2.0)

    classification = "unclear"
    confidence = "low"
    headline = "로그만으로 직접 사망 원인을 한 가지 형태로 분류하기 어렵습니다."
    if fatal:
        if overkill is not None and overkill > 0 and final_maxhp_share is not None and final_maxhp_share >= 0.50 and (final_share or 0) >= 0.50:
            classification = "large_single_hit"
            confidence = "high"
            headline = "마지막 대형 단일 피해가 직접 사망을 만든 형태입니다."
        elif max_hp and damage_2s >= max_hp * 0.55 and hits_2s >= 2:
            classification = "burst_sequence"
            confidence = "medium"
            headline = "사망 직전 2초 안에 여러 피해가 겹친 연속 폭딜 형태입니다."
        elif max_hp and damage_3s >= max_hp * 0.65:
            classification = "short_pressure"
            confidence = "medium"
            headline = "사망 직전 짧은 구간의 누적 피해가 크게 쌓인 형태입니다."
        else:
            classification = "final_hit_lethal"
            confidence = "medium" if overkill and overkill > 0 else "low"
            headline = "마지막 피해가 사망 시점과 직접 맞물렸지만 추가 맥락이 필요합니다."

    review["classification"] = classification
    review["confidence"] = confidence
    review["headline"] = headline

    if fatal:
        review["finalHit"] = {
            "sec": fatal.get("sec"),
            "sourceName": fatal.get("sourceName"),
            "spellID": fatal.get("spellID"),
            "spellName": fatal.get("spellName"),
            "amount": final_amount,
            "overkill": overkill,
            "maxHP": max_hp,
            "amountPctMaxHP": _pct(final_maxhp_share),
            "shareOfLookbackDamagePct": _pct(final_share),
            "beforeDeathSec": round(before_death, 3) if before_death is not None else None,
            "inferredPreHitHP": round(inferred_pre_hp, 3) if inferred_pre_hp is not None else None,
            "inferredPreHitPct": _pct(inferred_pre_hp_pct),
        }
        review["evidence"].append(
            f"{_fmt_sec(fatal.get('sec'))} {fatal.get('spellName') or fatal.get('event') or '?'} {_fmt_amount(final_amount)}"
        )
        if final_maxhp_share is not None:
            review["evidence"].append(f"최종 피해량은 최대 체력의 {_pct(final_maxhp_share):.1f}%")
        if final_share is not None:
            review["evidence"].append(f"사망 전 {lookback.get('seconds', 8):g}초 피해 중 최종 타격 비중 {_pct(final_share):.1f}%")
        if inferred_pre_hp_pct is not None:
            review["evidence"].append(f"치명타 직전 추정 체력 {_fmt_amount(inferred_pre_hp)} ({_pct(inferred_pre_hp_pct):.1f}%)")
        if overkill is not None and overkill > 0:
            review["evidence"].append(f"오버킬 {_fmt_amount(overkill)}")

    # Defensives: usage/auras are observable, availability is not.
    window_start = max(0.0, death_sec - float(lookback.get("seconds") or 8.0))
    def_casts = [x for x in facts.get("defensives") or [] if x.get("sec") is not None and window_start <= float(x.get("sec")) <= death_sec]
    active_auras = _active_defensive_auras(facts, death_sec)
    review["defensiveContext"] = {
        "lookbackSec": float(lookback.get("seconds") or 8.0),
        "casts": def_casts,
        "activeAurasAtDeath": active_auras,
        "availabilityKnown": False,
    }

    # Healing context. We distinguish a long no-heal gap from recent healing, but do not blame healers.
    effective_heals = [x for x in heal_events if float(x.get("effective") or 0.0) > 0]
    last_heal = effective_heals[-1] if effective_heals else None
    heal_gap = death_sec - float(last_heal.get("sec")) if last_heal else None
    effective_total = float(lookback.get("effectiveHealTotal") or 0.0)
    review["healingContext"] = {
        "effectiveHealTotal": effective_total,
        "lastEffectiveHeal": last_heal,
        "lastEffectiveHealGapSec": round(heal_gap, 3) if heal_gap is not None else None,
        "longNoHealGap": bool(heal_gap is not None and heal_gap >= 3.0),
    }

    review["cautions"] = [
        "CombatLog 사실만으로 생존기의 당시 재사용 가능 여부는 아직 알 수 없습니다.",
        "해당 보스 기술이 피할 수 있는 피해인지, 교란/그망 등이 유효한지는 아직 판정하지 않습니다.",
        "힐러 책임 여부를 이 단계에서 판정하지 않습니다. 회복 유입 시점과 양만 표시합니다.",
    ]
    return review


def render_death_review_text(review: dict) -> str:
    e = review.get("encounter") or {}
    lines = [
        "=== Live 개인 사망 판정 v0.2-B1 ===",
        f"[{review.get('player','?')}] {review.get('specName') or '미확인'} · {e.get('name','?')} · {e.get('difficultyName','-')} · Try {e.get('tryNo','?')} · {e.get('result','-')} · {_fmt_sec(e.get('durationSec'))}",
        "※ 이 단계는 개인 사망 형태만 판정합니다. 보스 공략/Top30/NSRT 판단은 아직 포함하지 않습니다.",
        "",
    ]

    if review.get('deathIncidents'):
        lines.append('--- 원인부터 사망까지 (사건당 1건) ---')
        for incident in review['deathIncidents']:
            lines.append(incident['sentence'])
        lines.append('')
    status = review.get("status")
    if status in {"no-player", "survived", "no-final-death"}:
        lines.append("--- 결론 ---")
        lines.append(review.get("headline") or "판정 없음")
        deaths = review.get("deathHistory") or []
        if deaths:
            parts = []
            for d in deaths:
                item = f"{d.get('deathNo','?')}차 {_fmt_sec(d.get('sec'))}"
                if d.get("resumedAtSec") is not None:
                    item += f" → {_fmt_sec(d.get('resumedAtSec'))} 행동 재개"
                parts.append(item)
            lines.append("사망 기록: " + " / ".join(parts))
        return "\n".join(lines) + "\n"

    confidence_label = {"high": "높은 확신", "medium": "중간 확신", "low": "낮은 확신"}.get(review.get("confidence"), "판정 보류")
    lines += [
        "--- 결론 ---",
        f"[{confidence_label}] {review.get('headline') or '판정 없음'}",
    ]
    for ev in review.get("evidence") or []:
        lines.append(f"- {ev}")

    fh = review.get("finalHit") or {}
    if fh:
        lines += ["", "--- 치명타 ---"]
        lines.append(
            f"{_fmt_sec(fh.get('sec'))} · {fh.get('sourceName') or '?'} · {fh.get('spellName') or '?'} · {_fmt_amount(fh.get('amount'))}"
        )
        extra = []
        if fh.get("inferredPreHitPct") is not None:
            extra.append(f"직전 추정 HP {fh.get('inferredPreHitPct'):.1f}%")
        if fh.get("amountPctMaxHP") is not None:
            extra.append(f"최대체력 대비 {fh.get('amountPctMaxHP'):.1f}%")
        if fh.get("overkill") is not None and float(fh.get("overkill") or 0) > 0:
            extra.append(f"오버킬 {_fmt_amount(fh.get('overkill'))}")
        if fh.get("beforeDeathSec") is not None:
            extra.append(f"사망 {fh.get('beforeDeathSec'):.2f}초 전")
        if extra:
            lines.append(" · ".join(extra))

    dc = review.get("defensiveContext") or {}
    lines += ["", "--- 생존기 맥락 ---"]
    casts = dc.get("casts") or []
    active = dc.get("activeAurasAtDeath") or []
    if casts:
        lines.append("사망 직전 사용: " + ", ".join(f"{x.get('spellName')}({_fmt_sec(x.get('sec'))})" for x in casts))
    else:
        lines.append(f"사망 전 {dc.get('lookbackSec',8):g}초 교란/그망/회피/진홍색 약병 캐스트 기록 없음")
    if active:
        lines.append("사망 시점 활성 오라: " + ", ".join(active))
    lines.append("※ '사용 기록 없음'과 '사용 가능했는데 안 씀'은 다릅니다. 쿨다운 가용 여부는 아직 판정하지 않습니다.")

    hc = review.get("healingContext") or {}
    lines += ["", "--- 회복 맥락 ---"]
    last = hc.get("lastEffectiveHeal")
    if last:
        lines.append(
            f"마지막 유효회복: 사망 {float(hc.get('lastEffectiveHealGapSec') or 0):.2f}초 전 · "
            f"{last.get('sourceName') or '?'} · {last.get('spellName') or '?'} · {_fmt_amount(last.get('effective'))}"
        )
    else:
        lines.append("사망 직전 관측창에 유효회복 기록 없음")
    lines.append(f"사망 전 8초 유효회복 합계: {_fmt_amount(hc.get('effectiveHealTotal'))}")
    if hc.get("longNoHealGap"):
        lines.append("관측: 마지막 유효회복 이후 3초 이상 공백이 있었습니다. 원인/책임은 아직 판정하지 않습니다.")
    else:
        lines.append("관측: 3초 이상 장시간 무회복 공백 형태는 아닙니다.")

    lines += ["", "--- 아직 판정하지 않는 것 ---"]
    for c in review.get("cautions") or []:
        lines.append(f"- {c}")
    return "\n".join(lines) + "\n"


def analyze_and_write(facts: dict, out_dir: Path | str) -> dict:
    out_dir = Path(out_dir)
    review = analyze_death(facts)
    json_path = out_dir / "death_review.json"
    txt_path = out_dir / "death_review.txt"
    atomic_write_json(json_path, review)
    atomic_write_text(txt_path, render_death_review_text(review))

    meta_path = out_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(meta, dict):
                meta.update({
                    "deathReviewModuleVersion": review.get("moduleVersion"),
                    "deathReviewJson": "death_review.json",
                    "deathReviewTxt": "death_review.txt",
                    "deathReviewGeneratedAt": review.get("generatedAt"),
                })
                atomic_write_json(meta_path, meta)
        except Exception:
            pass

    return {
        "deathReviewStatus": "ready",
        "deathReviewJson": str(json_path),
        "deathReviewTxt": str(txt_path),
        "deathReview": review,
    }
