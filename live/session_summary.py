"""B37.4.35 · date session summary (read-only projection).

The date screen summarises judgments that already exist on each Try.  Nothing
here judges a mechanic: it only reads the per-Try priority model produced by
``live.b3641_ui.build_try_priority_model`` (firstMistake / killImprovement /
collapseDirect / majorIncident and the timeline evidence levels).

Rules kept from the analyzer's evidence model:
* only ``evidenceLevel == "verified"`` items can become 주요 개선점;
* candidate signals are listed separately as 추가 확인 필요, never as
  improvements, and only when fewer than three verified items exist;
* a Kill with a verified failure shows that failure as 남은 개선점; a Kill with
  none shows 검증 실패 없음 (never "perfect").
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

RESULT_KILL = "Kill"
RESULT_WIPE = "Wipe"
RESULT_EARLY = "Early Wipe"

TONE_FAILED = "failed"      # verified failure → red
TONE_OK = "ok"              # verified clean → green
TONE_NEUTRAL = "neutral"    # unsupported / pending / no verified signal → gray

# Raid difficulties (legacy 10/25 N/H, LFR, Normal, Heroic, Mythic, Timewalking raid).
RAID_DIFFICULTIES = {3, 4, 5, 6, 7, 9, 14, 15, 16, 17, 33, 233}

_COUNT_RE = re.compile(r"\s*\d+\s*(개|명|회|마리)\s*")


def mechanic_label(label: str) -> str:
    """Drop per-Try counts so one mechanic groups once (물방울 4개 미섭취 → 물방울 미섭취)."""
    text = _COUNT_RE.sub(" ", str(label or "")).strip()
    return re.sub(r"\s{2,}", " ", text) or str(label or "")


def _is_raid(row: dict) -> bool:
    try:
        diff = int((row or {}).get("difficultyID"))
    except (TypeError, ValueError):
        return True   # unknown difficulty: keep (older rows)
    return diff in RAID_DIFFICULTIES


_ROLE_WEIGHT = {"collapse_driver": 3, "collapseDirect": 3, "first_mistake": 2, "firstMistake": 2,
                "killImprovement": 1, "recovered_on_kill": 1, "majorIncident": 1, "additional_failure": 1}
_ROLE_LABEL = {"collapseDirect": "붕괴 직결", "collapse_driver": "붕괴 직결", "firstMistake": "첫 실수",
               "first_mistake": "첫 실수", "killImprovement": "Kill 개선점", "recovered_on_kill": "Kill에서 회복",
               "majorIncident": "주요 사고", "additional_failure": "추가 실수"}


def fmt_duration(seconds: Any) -> str:
    """Truncating m:ss, identical to the sidebar (409.685 → 6:49)."""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    return f"{total // 60}:{total % 60:02d}"


def _clock(ts: Any) -> datetime | None:
    text = str(ts or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})", text)
    if m:
        mo, d, y, hh, mm, ss = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, hh, mm, ss)
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", text)
    if m:
        try:
            return datetime(*(int(x) for x in m.groups()))
        except ValueError:
            return None
    return None


def _item_label(item: dict) -> str:
    return str(item.get("label") or item.get("title") or item.get("kind") or "기믹").strip()


def _is_verified(item: Any) -> bool:
    return isinstance(item, dict) and str(item.get("evidenceLevel") or "") == "verified"


def _try_verified_items(pm: dict) -> list[tuple[str, dict]]:
    """(slot, item) for each verified failure slot of one Try, deduped by label."""
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for slot in ("collapseDirect", "firstMistake", "killImprovement", "majorIncident"):
        item = (pm or {}).get(slot)
        if not _is_verified(item):
            continue
        status = str(item.get("status") or "")
        if status and status not in {"failed", "warning"}:
            continue
        label = _item_label(item)
        if label in seen:
            continue
        seen.add(label)
        out.append((slot, item))
    return out


def _try_candidate_labels(pm: dict) -> list[str]:
    labels: list[str] = []
    for ev in (pm or {}).get("timeline") or []:
        if not isinstance(ev, dict) or str(ev.get("evidenceLevel") or "") != "candidate":
            continue
        label = str(ev.get("label") or ev.get("kind") or "").split(" · ")[0].strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def _boss_short(name: str) -> str:
    return str(name or "?").strip()


def build_session_summary(date: str, entries: Iterable[dict]) -> dict:
    """Summarise one raid day.

    ``entries`` (chronological): dicts with
      row        – live row (name, difficultyName, tryNo, result, durationSec,
                   startTimestamp, endTimestamp)
      key        – opaque navigation key for this Try
      group      – opaque boss grouping key
      pm         – per-Try priority model ({} when unavailable)
      supported  – bool, BossMechanic exists for this boss
      early      – bool, Early Wipe
      excluded   – bool, reset / fake pull (counted but not summarised)
      pending    – bool, review stale / being rebuilt
      context    – opaque boss context passed through for navigation
    """
    entries = list(entries or [])
    raid_entries = [e for e in entries if _is_raid(e.get("row") or {})]
    other_entries = [e for e in entries if not _is_raid(e.get("row") or {}) and not e.get("excluded")]
    included = [e for e in raid_entries if not e.get("excluded")]
    excluded_n = len(raid_entries) - len(included)

    bosses: dict[Any, dict] = {}
    order: list[Any] = []
    for e in included:
        g = e.get("group")
        if g not in bosses:
            row = e.get("row") or {}
            bosses[g] = {"group": g, "name": _boss_short(row.get("name")),
                         "difficulty": str(row.get("difficultyName") or ""), "entries": []}
            order.append(g)
        bosses[g]["entries"].append(e)

    kills = sum(1 for e in included if str((e.get("row") or {}).get("result") or "").upper() == "KILL")
    early = sum(1 for e in included if e.get("early") and str((e.get("row") or {}).get("result") or "").upper() != "KILL")
    wipes = len(included) - kills - early

    starts = [c for c in (_clock((e.get("row") or {}).get("startTimestamp")) for e in included) if c]
    ends = []
    for e in included:
        row = e.get("row") or {}
        end = _clock(row.get("endTimestamp"))
        if end is None:
            st = _clock(row.get("startTimestamp"))
            try:
                if st is not None:
                    from datetime import timedelta
                    end = st + timedelta(seconds=float(row.get("durationSec") or 0))
            except (TypeError, ValueError):
                end = None
        if end:
            ends.append(end)
    span = ""
    if starts and ends:
        span = f"진행 {min(starts):%H:%M} → {max(ends):%H:%M}"

    # ---- boss rows --------------------------------------------------------
    boss_rows: list[dict] = []
    for g in order:
        b = bosses[g]
        ents = b["entries"]
        kill_ents = [e for e in ents if str((e.get("row") or {}).get("result") or "").upper() == "KILL"]
        all_early = all(e.get("early") for e in ents)
        result = RESULT_KILL if kill_ents else (RESULT_EARLY if all_early else RESULT_WIPE)
        target = kill_ents[-1] if kill_ents else ents[-1]
        if kill_ents:
            time_text = fmt_duration((target.get("row") or {}).get("durationSec"))
        else:
            longest = max(ents, key=lambda e: float((e.get("row") or {}).get("durationSec") or 0))
            time_text = f"최장 {fmt_duration((longest.get('row') or {}).get('durationSec'))}"
            target = longest

        supported = any(e.get("supported") for e in ents)
        pending = any(e.get("pending") for e in ents)
        key_line, tone = "", TONE_NEUTRAL
        if kill_ents:
            pm = target.get("pm") or {}
            items = _try_verified_items(pm)
            if items:
                slot, item = items[0]
                recovered = str(item.get("failureRole") or "") == "recovered_on_kill"
                key_line = f"남은 개선점{'(회복됨)' if recovered else ''} · {_item_label(item)}"
                tone = TONE_FAILED
            elif str(pm.get("footer") or "").startswith("남은 개선점"):
                key_line, tone = str(pm.get("footer")), TONE_FAILED
            elif target.get("supported") and pm:
                key_line, tone = "검증 실패 없음", TONE_OK
        else:
            counts: dict[str, int] = {}
            first_seen: dict[str, int] = {}
            for i, e in enumerate(ents):
                pm = e.get("pm") or {}
                for slot in ("collapseDirect", "firstMistake"):
                    item = pm.get(slot)
                    if _is_verified(item):
                        lab = mechanic_label(_item_label(item))
                        counts[lab] = counts.get(lab, 0) + 1
                        first_seen.setdefault(lab, i)
                        break
            if counts:
                lab = sorted(counts, key=lambda k: (-counts[k], first_seen[k]))[0]
                key_line = f"{lab}" + (f" · {counts[lab]}회" if len(ents) > 1 else "")
                tone = TONE_FAILED
            elif supported and any(e.get("pm") for e in ents):
                key_line, tone = "검증된 기믹 실패 없음", TONE_NEUTRAL
        if not key_line:
            if pending:
                key_line = "복기 최신화 중"
            elif not supported:
                key_line = "전용 기믹 판정 없음"
            else:
                key_line = "판정 데이터 없음"
            tone = TONE_NEUTRAL

        chips = []
        try_nos = [(e.get("row") or {}).get("tryNo") for e in ents]
        for e in ents:
            row = e.get("row") or {}
            res = RESULT_KILL if str(row.get("result") or "").upper() == "KILL" else (RESULT_EARLY if e.get("early") else RESULT_WIPE)
            st = _clock(row.get("startTimestamp"))
            dup = try_nos.count(row.get("tryNo")) > 1
            chips.append({"tryNo": row.get("tryNo"), "result": res, "clock": (f"{st:%H:%M}" if (dup and st) else ""),
                          "duration": fmt_duration(row.get("durationSec")), "key": e.get("key")})
        boss_rows.append({
            "group": g, "name": b["name"], "difficulty": b["difficulty"], "result": result,
            "tryCount": len(ents), "timeText": time_text, "keyLine": key_line, "tone": tone,
            "targetKey": target.get("key"), "chips": chips, "context": ents[0].get("context"),
        })

    # ---- session improvements (verified only) ----------------------------
    agg: dict[tuple, dict] = {}
    for idx, e in enumerate(included):
        row = e.get("row") or {}
        for slot, item in _try_verified_items(e.get("pm") or {}):
            role = str(item.get("failureRole") or "") or slot
            lab = mechanic_label(_item_label(item))
            k = (e.get("group"), str(item.get("kind") or "") or lab)
            a = agg.setdefault(k, {"boss": _boss_short(row.get("name")), "label": lab,
                                   "tries": [], "keys": [], "weight": 0, "roles": [], "first": idx})
            if e.get("key") not in a["keys"]:
                a["tries"].append(row.get("tryNo"))
                a["keys"].append(e.get("key"))
                st = _clock(row.get("startTimestamp"))
                a.setdefault("clocks", []).append(f"{st:%H:%M}" if st else "")
            a["weight"] += _ROLE_WEIGHT.get(role, 1)
            rl = _ROLE_LABEL.get(role)
            if rl and rl not in a["roles"]:
                a["roles"].append(rl)
    improvements = sorted(agg.values(), key=lambda a: (-a["weight"], -len(a["tries"]), a["first"]))[:3]
    for a in improvements:
        dup = len(set(a["tries"])) < len(a["tries"])
        names = [f"{t}({c})" if (dup and c) else str(t) for t, c in zip(a["tries"], a.get("clocks") or [""] * len(a["tries"]))]
        tries = ", ".join(names[:4]) + (" …" if len(names) > 4 else "")
        a["detail"] = f"Try {tries} · {len(a['tries'])}회" + (f" · {' / '.join(a['roles'][:2])}" if a["roles"] else "")
        a["key"] = a["keys"][0] if a["keys"] else None

    checks: list[dict] = []
    if len(improvements) < 3:
        cagg: dict[tuple, dict] = {}
        for e in included:
            row = e.get("row") or {}
            for lab in _try_candidate_labels(e.get("pm") or {}):
                k = (e.get("group"), lab)
                c = cagg.setdefault(k, {"boss": _boss_short(row.get("name")), "label": lab, "count": 0, "key": e.get("key")})
                c["count"] += 1
        checks = sorted(cagg.values(), key=lambda c: -c["count"])[: 3 - len(improvements)]
        for c in checks:
            c["detail"] = f"후보 · {c['count']}개 Try에서 관측 · 의미 미확정"

    boss_n = len(boss_rows)
    stats = f"{boss_n}보스 · Kill {kills} · Wipe {wipes}" + (f" · Reset {early}" if early else "") + f" · 총 {len(included)} Try"
    if excluded_n:
        stats += f" · 리셋/가짜 풀 {excluded_n} 제외"
    title = "레이드 세션" if date in ("", "분석 세션") else f"{date} 레이드 세션"
    others = []
    for e in other_entries:
        row = e.get("row") or {}
        res = RESULT_KILL if str(row.get("result") or "").upper() == "KILL" else RESULT_WIPE
        others.append({"name": _boss_short(row.get("name")), "result": res,
                       "duration": fmt_duration(row.get("durationSec")), "key": e.get("key")})
    return {"date": date, "title": title, "stats": stats, "span": span,
            "kills": kills, "wipes": wipes, "early": early, "tries": len(included), "excluded": excluded_n,
            "bosses": boss_rows, "improvements": improvements, "checks": checks, "others": others}
