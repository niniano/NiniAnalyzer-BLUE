"""Conservative, additive P1 closeout for Mythic Coiled Altar.

Never infers responsibility from a fatal spell alone. Observations are attached
separately from verified simultaneous spell->death collapse links.
"""
from __future__ import annotations

from collections import defaultdict

SPELL_GUILLOTINE = 1283489
SPELL_GUILLOTINE_MARK = 1283485
SPELL_GUILLOTINE_SOAK = 1283594
GUILLOTINE_REQUIRED_SOAKERS = 6
SPELL_EXECUTION = 1283606
SPELL_BOP = 1022
SPELL_RUPTURE = 1299838
SPELL_ORB = 1282408
ZULJAN_NPC = 257911


def _sec(row):
    try:
        return float(row.get("sec"))
    except (ValueError, TypeError):
        return None


def _id(row):
    try:
        return int(row.get("spellID") or row.get("abilityGameID"))
    except (ValueError, TypeError):
        return None


def _event(row):
    return str(row.get("event") or "").lower().replace("_", "")


def _dest(row):
    return str(row.get("destGUID") or row.get("targetGUID") or row.get("destName") or row.get("targetName") or "")


def _player(row):
    return str(row.get("destName") or row.get("targetName") or _dest(row))


def _fmt(sec):
    s = max(float(sec or 0), 0.0)
    return f"{int(s//60)}:{s%60:04.1f}"


def _npc(row):
    try:
        return int(row.get("targetGameID"))
    except (ValueError, TypeError):
        guid = str(row.get("targetGUID") or row.get("destGUID") or "")
        parts = guid.split("-")
        try:
            return int(parts[5]) if len(parts) > 5 else None
        except (ValueError, TypeError):
            return None



# COILED_ACCURACY_HF3: local advanced-log HP snapshots are attached to
# regular spell events, not to a synthetic resourceSnapshot event type.
# Check BOTH actor sides; only NPC 257911 (Zul'jan) is a valid HP source.
def _zuljan_hp_near(events, checkpoint=170.0, tolerance=1.0):
    valid = []
    for r in events:
        sec = _sec(r)
        if sec is None or abs(sec - checkpoint) > tolerance:
            continue
        for side in ("target", "source"):
            sid = r.get(side + "GameID")
            try:
                if int(sid) != ZULJAN_NPC:
                    continue
            except (ValueError, TypeError):
                continue
            current = r.get(side + "CurrentHP")
            maxhp = r.get(side + "MaxHP")
            pct = r.get(side + "HealthPct")
            try:
                if pct is None:
                    pct = 100.0 * float(current) / float(maxhp)
                pct = float(pct)
                maximum = float(maxhp) if maxhp is not None else None
            except (ValueError, TypeError, ZeroDivisionError):
                continue
            if not 0 <= pct <= 100 or (maximum is not None and maximum <= 0):
                continue
            # Observed HP only. Do not extrapolate an exact value across 2 sec.
            valid.append((abs(sec-checkpoint), bool(r.get("supplementalResource")),
                          sec, pct))
    if not valid:
        return None
    delta, supplemental, sec, pct = min(valid)
    return {"sec": round(sec, 3), "healthPct": round(pct, 3),
            "distanceSec": round(delta, 3),
            "evidenceLevel": "verified", "source": "zuljan_native_event_hp",
            "supplementalResource": supplemental}


def _zuljan_late_rain(events):
    casts = []
    for r in events:
        if _id(r) != 1299960 or _event(r) != "spellcastsuccess":
            continue
        sec = _sec(r)
        if sec is None or not 170.0 <= sec <= 174.5:
            continue
        try:
            npc = int(r.get("sourceGameID"))
        except (TypeError, ValueError):
            continue
        if npc == ZULJAN_NPC:
            casts.append(sec)
    return round(min(casts), 3) if casts else None


def _guillotine_soak_rounds(events, deaths):
    """Count unique player targets of the actual 1283594 resolution event.

    SPELL_ABSORBED is excluded: it can repeat for the same victim and inflates
    the headcount. SPELL_MISSED is included (IMMUNE/MISS in real successful
    pulls), while a bare cast/marker is never interpreted as zero soakers.
    Names/raid assignments are not baked into this rule.
    """
    markers = [r for r in events if _id(r) == SPELL_GUILLOTINE_MARK
               and _event(r) == "spellauraapplied" and _dest(r).startswith("Player-")]
    casts = [r for r in events if _id(r) == SPELL_GUILLOTINE
             and _event(r) == "spellcastsuccess"]
    # Fallback for streams without the debuff marker. Do not merge both paths.
    anchors = markers[:2] if markers else casts[:2]
    impact_rows = [r for r in events if _id(r) == SPELL_GUILLOTINE_SOAK
                   and _event(r) in {"spelldamage", "spellmissed"}
                   and _dest(r).startswith("Player-")]
    execution_rows = [r for r in events if _id(r) == SPELL_EXECUTION
                      and _event(r) == "spelldamage" and _dest(r).startswith("Player-")]
    end_sec = max((_sec(r) for r in events), default=0.0)
    rounds = []
    for i, anchor in enumerate(anchors, start=1):
        anchor_sec = _sec(anchor)
        if markers:
            target_cast = next((r for r in casts if 0 <= _sec(r) - anchor_sec <= 5.5
                                and _dest(r) == _dest(anchor)), None)
        else:
            target_cast = anchor
        cast_sec = _sec(target_cast) if target_cast else None
        # Real captures: aura applied at ~43/128, cast success at ~46.5/131.5,
        # soak resolution at ~48/133. Windows exclude other guillotines.
        left = cast_sec + 0.5 if cast_sec is not None else anchor_sec + 3.5
        right = cast_sec + 3.25 if cast_sec is not None else anchor_sec + 8.0
        if i < len(anchors):
            right = min(right, _sec(anchors[i]))
        hits = [r for r in impact_rows if left <= _sec(r) <= right]
        exec_hits = [r for r in execution_rows if left <= _sec(r) <= right + 0.5]
        people = {}
        for r in hits:
            people.setdefault(_dest(r), _player(r))
        count = len(people) if hits else None
        # Direct damage/death association, not merely a B31 temporal overlap.
        linked = set()
        for r in exec_hits:
            for d in deaths:
                if d["guid"] == _dest(r) and 0 <= d["sec"] - _sec(r) <= 0.35:
                    linked.add(d["guid"])
        lethal_count = len(linked)
        if count is None and exec_hits:
            # Execution after the attempted soak gives a real resolution event,
            # and absence of all 1283594 hits is meaningful on a full stream.
            count = 0
        if count is None:
            status = "unknown"
            reason = ("soak_resolution_not_reached" if end_sec < left
                      else "soak_impact_signal_unobserved")
        elif exec_hits and count < GUILLOTINE_REQUIRED_SOAKERS:
            status, reason = "failed", "short_soak_execution_observed"
        elif exec_hits:
            status, reason = "warning", "execution_despite_full_soak_observed"
        elif count < GUILLOTINE_REQUIRED_SOAKERS:
            # Not every observed 5/6 episode has Execution in real archives.
            status, reason = "warning", "short_soak_without_execution"
        else:
            status, reason = "success", "soak_requirement_met"
        rounds.append({
            "index": i, "sec": round(anchor_sec, 3),
            "castSec": round(cast_sec, 3) if cast_sec is not None else None,
            "impactSec": round(min(_sec(r) for r in hits), 3) if hits else None,
            "requiredCount": GUILLOTINE_REQUIRED_SOAKERS,
            "soakCount": count, "players": list(people.values()),
            "status": status, "reason": reason,
            "executionObserved": bool(exec_hits),
            "executionHitCount": len({_dest(r) for r in exec_hits}),
            "executionLinkedDeathCount": lethal_count,
            "evidenceLevel": "verified" if count is not None else "unavailable",
            "soakSpellID": SPELL_GUILLOTINE_SOAK,
            "countedEvents": ["SPELL_DAMAGE", "SPELL_MISSED"],
        })
    return rounds


def analyze(rows, *, mythic=True, encounter=None):
    """Use only direct ID evidence for verified conclusions; otherwise candidates."""
    out = {"schema": "coiled_p1_closeout_v1", "status": "not_applicable", "collapses": [],
           "guillotines": [], "soakRounds": [], "hpAt170": None, "phase2EntrySec": None, "limitations": []}
    if not mythic:
        return out
    out["status"] = "observed"
    enc = encounter or {}
    result = str(enc.get("result") or "").lower()
    events = sorted((r for r in rows if isinstance(r, dict) and _sec(r) is not None), key=_sec)
    deaths = []
    for r in events:
        if _event(r) in {"unitdied", "death"} and (str(r.get("targetType") or "").lower()=="player" or _dest(r).startswith("Player-")):
            target = _dest(r)
            if target:
                deaths.append({"sec": _sec(r), "guid": target, "player": _player(r)})
    seen = set()
    unique_deaths = []
    for d in deaths:
        if d["guid"] not in seen:
            seen.add(d["guid"]); unique_deaths.append(d)
    # HP-zero phase entry remains authoritative in engine.phase1Transition.
    # P1 closeout only stores the nearest observed HP and late rain cast.
    hp_170 = _zuljan_hp_near(events)
    if hp_170 is not None:
        out["hpAt170"] = hp_170["healthPct"]
        out["hpAt170Observation"] = hp_170
    out["lateRainCastSec"] = _zuljan_late_rain(events)
    # New card data only; does not promote any first-mistake or collapse verdict.
    out["soakRounds"] = _guillotine_soak_rounds(events, unique_deaths)
    casts = [r for r in events if _id(r)==SPELL_GUILLOTINE and _event(r) in {"spellcastsuccess", "cast"}]
    bops = [r for r in events if _id(r)==SPELL_BOP and _event(r) in {"spellcastsuccess", "cast"}]
    applications = [r for r in events if _id(r)==SPELL_BOP and _event(r) in {"spellauraapplied", "applybuff", "applydebuff"}]
    removals = [r for r in events if _id(r)==SPELL_BOP and _event(r) in {"spellauraremoved", "removebuff"}]
    for i,cast in enumerate(casts,1):
        sec=_sec(cast);target=_dest(cast);name=_player(cast)
        applied=[r for r in applications if _dest(r)==target and sec-5 <=_sec(r)<=sec+3]
        prior=[r for r in bops if _dest(r)==target and sec-5<=_sec(r)<=sec+1.5]
        removed=[r for r in removals if _dest(r)==target and sec-5<=_sec(r)<=sec+3]
        if applied:
            protection="aura_application_observed"
        elif prior:
            protection="cast_observed_application_unverified"
        else:
            protection="not_observed_in_capsule"
        out["guillotines"].append({"index":i,"sec":round(sec,3),"target":name,
          "bopEvidence":protection,"bopCaster":str((applied or prior)[-1].get("sourceName") or "") if (applied or prior) else None,
          "bopCastSec":round(_sec(prior[-1]),3) if prior else None,
          "bopAuraSec":round(_sec(applied[-1]),3) if applied else None,
          "bopRemovedNearby":bool(removed),
          "soakCount":None,"soakStatus":"unavailable_without_verified_spatial_or_soak_signal"})
    if not out["guillotines"]:
        out["limitations"].append("단두대 시전 대상과 참여 인원 미확인")
    # All player-target damage rows by unique spell and victim.
    damage = defaultdict(list)
    for r in events:
        if _event(r) in {"spelldamage", "spellperiodicdamage"} and _id(r) in {SPELL_EXECUTION,SPELL_RUPTURE,SPELL_ORB}:
            if _dest(r): damage[_id(r),_dest(r)].append(_sec(r))
    # Find local simultaneous deaths (without assuming B31 represents the same cluster).
    group = []
    groups=[]
    for d in unique_deaths:
        if not group or d["sec"]-group[0]["sec"]<=0.65:
            group.append(d)
        else:
            groups.append(group);group=[d]
    if group:groups.append(group)
    for grp in groups:
        if len(grp)<3:continue
        start=grp[0]["sec"];end=grp[-1]["sec"]
        if out["phase2EntrySec"] is not None and start >= out["phase2EntrySec"]-0.25:
            continue
        per={}
        for sid in (SPELL_EXECUTION,SPELL_RUPTURE,SPELL_ORB):
            matched=[d for d in grp if any(d["sec"]-0.8<=t<=d["sec"]+0.02 for t in damage[sid,d["guid"]])]
            per[sid]=matched
        strongest=max(per,key=lambda sid:len(per[sid]))
        strong_deaths=per[strongest]
        exact=strongest==SPELL_EXECUTION and len(strong_deaths)>=4 and len(strong_deaths)/len(grp)>=0.60
        if start < 30 and not exact:
            kind="p1_early_opening_collapse";title="풀 직후 동시 사망 · 별도 초기 공략 사고"
            evidence="candidate";status="opening_context_not_orb_collision"
            action="풀 직후 광역 피해 및 구슬/이동 경로를 별도 검증하세요. 2:50 딜 체크로 분류하지 않습니다."
        elif exact:
            kind="p1_execution_mass_deaths";title="처형 광역 피격 · 공대 다수 사망"
            evidence="verified";status="direct_damage_to_death_verified"
            action="단두대 분담 6명·대상자 보축 적용/유지·해제 시점을 확인하세요. 원인 미확정인 개인에게 책임을 부여하지 마세요."
        elif strongest in {SPELL_RUPTURE,SPELL_ORB} and len(strong_deaths)>=3:
            if start>=165 and out["phase2EntrySec"] is None:
                kind="p1_late_rupture_collapse";title="맹독성 파열 대량 사망 · 2:50 딜 체크 연관 후보"
                action="2:50 줄잔 체력 및 이전 사망으로 인한 딜 손실 확인. 시각만으로 딜 부족 확정 금지."
            elif start<165:
                kind="p1_early_orb_rupture";title="이른 파열 대량 사망 · 구슬 충돌 후보"
                action="구슬 이동 및 파열 근원 이벤트 확인. 시간만으로 충돌 담당자 확정 금지."
            else:
                kind="p1_rupture_unknown";title="맹독성 파열 대량 사망 · 원인 미확정"
                action="줄잔 HP, 구슬 이동, 단두대 전후 상황을 대조하세요."
            evidence="candidate";status="multi_death_spell_context_only"
        else:
            kind="p1_unknown_mass_death";title="P1 동시 사망 · 원인 미확정";action="동시 사망 직전 피해의 원본 근거 확인."
            evidence="unavailable";status="unknown"
        gull=next((g for g in reversed(out["guillotines"]) if g["sec"]<=start+0.5),None)
        out["collapses"].append({"sec":round(start,3),"endSec":round(end,3),"deathCount":len(grp),
           "kind":kind,"title":title,"evidenceLevel":evidence,"status":status,
           "directSpellID":strongest if len(strong_deaths)>0 else None,"directLinkedDeaths":len(strong_deaths),
           "players":[d["player"] for d in grp], "action":action,
           "guillotine":gull if kind=="p1_execution_mass_deaths" else None,
           "hpAt170":out["hpAt170"]})
    out["limitations"].extend(["단두대 6명 충족은 실제 1283594 피해/빗나감 대상 GUID를 중복 제거해 관측함. 물리적 위치/미참여 사유와 담당 배정은 단정하지 않음",
          "보축 미관측과 실제 미사용을 구분하며, 시전 기록만으로 오라 유지까지 확정하지 않음",
          "2:45 전후 시각은 후보 분류에만 사용. 체력·타이밍과 파열 유발 사건 확인 전 딜/충돌 책임 확정하지 않음"])
    return out


def attach(review,b31_review):
    """Project verified execution->death evidence into existing V4.0 slots only.

    The initial firstMistake is intentionally unchanged. The B31 collapse must
    match the captured spell->death cluster before a causal promotion.
    """
    if str(review.get("bossKey") or "")!="coiled_altar":return review
    out=dict(review)
    obs=dict(out.get("observations") or {})
    data=obs.get("p1Closeout") or {}
    if data.get("status")!="observed":return out
    # NINI_COILED_ACCURACY_HF2: P1 scope has to use the engine's VERIFIED
    # Zul'jan HP-zero entry; P1's local health sample stream may be incomplete.
    # Preserve the pre-entry collapse (including Try13), exclude only later ones.
    transition=obs.get("phase1Transition") or {}
    if (transition.get("evidenceLevel")=="verified"
            and transition.get("basis")=="zuljan_hp_zero"
            and transition.get("entrySec") is not None):
        try:
            entry=float(transition["entrySec"])
            if not (0 <= entry < 100000 and entry==entry):
                entry=None
        except (ValueError,TypeError,OverflowError):
            entry=None
        if entry is not None:
            data=dict(data)
            data["phase2EntrySec"]=round(entry,3)
            before=[]
            for item in (data.get("collapses") or []):
                try:
                    sec=float(item["sec"])
                except (ValueError,TypeError,KeyError):
                    before.append(item)   # unknown clock is not silently discarded
                    continue
                if sec < entry-0.25:
                    before.append(item)
            data["collapses"]=before
            obs["p1Closeout"]=data
            out["observations"]=obs
    # Preserve all P1 candidates for the detailed evidence/UI even if not causal.
    b31=(b31_review or {}).get("collapse") or {}
    try: b31_start=float(b31["startSec"]);b31_count=int(b31["count"])
    except (KeyError,TypeError,ValueError):return out
    for event in data.get("collapses") or []:
        if event.get("kind")!="p1_execution_mass_deaths" or event.get("evidenceLevel")!="verified":continue
        sec=float(event.get("sec"));linked=int(event.get("directLinkedDeaths") or 0)
        if abs(sec-b31_start)>0.75 or linked<4 or linked/max(b31_count,1)<0.6:continue
        incident={"kind":"p1_execution_mass_deaths","sec":sec,"label":event["title"],"title":event["title"],
           "summary":f"처형(1283606) 직접 피격 후 {linked}명 사망 · B31 붕괴 {b31_count}명",
           "status":"failed","evidenceLevel":"verified","directSpellIDs":[SPELL_EXECUTION],
           "causalScope":"direct_damage_to_deaths", "blame":"unassigned",
           "guillotine":event.get("guillotine")}
        prior=(out.get("collapseDirect") or {})
        if prior.get("evidenceLevel")=="verified":return out # never displace existing verified decision
        out["majorIncident"]=incident
        out["collapseDirect"]=incident
        # The Coiled V4.0 projection requires causal=True on the B31 context.
        context=dict(out.get("b31CollapseContext") or {})
        context.update({"causal":True,"status":"linked", "summary":incident["summary"],
             "matchingFailure":incident})
        out["b31CollapseContext"]=context
        out["collapseLink"]={"status":"linked","evidenceLevel":"verified","causal":True,
           "causalScope":"execution_direct_damage_deaths", "summary":incident["summary"],"collapseSec":b31_start}
        verdict=dict(out.get("verdict") or {})
        verdict.update({"severity":"HIGH","status":"failed","primary":event["title"],
           "why":incident["summary"]+" · 단두대 분담/보축의 구체적인 실패자는 별도 검증 필요",
           "nextAction":"다음 풀: "+event["action"]})
        out["verdict"]=verdict
        break
    return out
