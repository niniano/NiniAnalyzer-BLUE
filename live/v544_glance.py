"""V5.4.4 「이번 트라이 · 한눈에 보기」 요약 (화면 전용 · 판정·저장 데이터 불변).

사용자 11/13: 「분석기가 전체적으로 긴 글이 많아 가독성이 떨어짐 · 한눈에 볼 수 있는 방법」
→ 시안에서 고른 것: 무너진 순서 칩 · 숫자 카드 5칸 · 다음 풀 한 줄 (모든 보스).
지금의 5줄 문장(어디까지 · 첫 실수 · 전멸 원인 · 딜·힐 · 다음 풀)과 참고는 「자세히」 안으로 접는다.

결론 모듈(울라텍 · 똬리 · boss_guides)이 이미 만든 5줄과 구조 자료에서 뽑기만 한다. 새로 판정하지 않는다.
"""
from __future__ import annotations

import re

VERSION = "V5.4.4-GLANCE"

_TIME = re.compile(r"(?<![\d:])(\d{1,2}:\d{2})(?![\d:])")
_CONF = re.compile(r"\s*\[(확정|추정|미확정)\]\s*")
_STAGE_PREFIX = re.compile(r"^(?:\d페|[0-9]심장|P\d|사이페|막페|2페\(알방\)|1페|2페|3페)(?:\([^)]*\))?\s+")
CHIP_MAX = 30          # 칩 한 개 글자 수 (넘으면 …)
NEXT_MAX = 70          # 다음 풀 한 줄 글자 수
CHAIN_MAX = 4          # 칩 개수 (전멸 칩 빼고)


def mmss(sec) -> str:
    try:
        s = int(float(sec))
    except (TypeError, ValueError):
        return ""
    return f"{s // 60}:{s % 60:02d}"


def _sec(t: str | None):
    if not t:
        return None
    m, s = t.split(":")
    return int(m) * 60 + int(s)


def _clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1].rstrip(" ·,→") + "…"


def _conf(text: str) -> str | None:
    m = _CONF.search(text or "")
    return m.group(1) if m else None


def _strip(text: str) -> str:
    return _CONF.sub(" ", str(text or "")).strip(" ·")


def _step(text: str) -> dict | None:
    """「알 든 Mohammed 부식의 파도 1:53」 → {t: 1:53, text: 알 든 Mohammed 부식의 파도}."""
    segs = [x.strip() for x in _strip(text).split(" · ") if x.strip()]
    # 「사이페 이후 · 영원한 일몰 15명 (… 6:22)」처럼 앞 조각이 구간 이름뿐이면 시각·인원이 있는 조각을 쓴다
    raw = next((x for x in segs if _TIME.search(x) or re.search(r"\d+명", x)), segs[0] if segs else "")
    if not raw or raw in ("-",):
        return None
    times = _TIME.findall(raw)
    t = times[0] if times else None
    body = raw
    if t:
        # 맨 앞 「1:53 P1 …」 · 끝 「… 1:53」 · 괄호 「(2:12)」 시각은 칩 위 작은 글로 옮긴다
        body = re.sub(r"\(\s*" + re.escape(t) + r"\s*\)", "", body)
        body = re.sub(r"(^|\s)" + re.escape(t) + r"(?=\s|$)", " ", body)
    body = re.sub(r"^붕괴\s+", "", body.strip())
    body = _STAGE_PREFIX.sub("", body).strip(" ·")
    return {"t": t, "text": _clip(body, CHIP_MAX)} if body else None


def chain(lines: dict, kill: bool, duration: float | None, cause: dict | None = None) -> list[dict]:
    """무너진 순서 칩: 첫 실수 → 전멸 원인 단계들 → 전멸 (킬이면 킬 칩 하나)."""
    end = {"t": mmss(duration) if duration else None, "title": "킬" if kill else "전멸",
           "text": "킬" if kill else "전멸", "tone": "green" if kill else "end"}
    if kill:
        return []   # 킬은 결과 카드로 충분 (칩 없음)
    out: list[dict] = []
    first_full = str(lines.get("첫 실수") or "")
    first = first_full.split(" → ")[0]
    none = first.startswith(("사망 없음", "-")) or ("없음" in first and not _TIME.search(first))
    if first and not none:
        st = _step(first)
        if st:
            out.append({**st, "title": "첫 실수", "tone": "red", "conf": _conf(first_full)})
    cause_txt = str(lines.get("전멸 원인") or "")
    conf = _conf(cause_txt)
    for i, part in enumerate(_strip(cause_txt).split(" → ")):
        st = _step(part)
        if not st:
            continue
        if st["t"] is None and i == 0 and cause and cause.get("sec") is not None:
            st["t"] = mmss(cause["sec"])
        if i == 0 and out and st["t"] is not None and out[0].get("t") == st["t"]:
            continue   # 원인 첫 단계 = 첫 실수 (같은 시각 · 같은 사건)
        out.append({**st, "title": "", "tone": "orange" if out else "red", "conf": conf if not out else None})
        if len(out) >= CHAIN_MAX:
            break
    if out and not out[0].get("title"):
        out[0]["title"] = "뿌리"
    return out + [end]


def _hp(lines: dict, out: dict):
    v = out.get("bossHpPct", out.get("endHpPct"))
    if v is None:
        m = re.search(r"보스 HP\s*([\d.]+)%", str(lines.get("어디까지") or ""))
        v = float(m.group(1)) if m else None
    return v


def stats(lines: dict, out: dict, facts: dict | None, kill: bool, duration: float | None) -> list[dict]:
    """숫자 카드 5칸: 어디까지 · 보스 HP · 사망 · 보스별 2칸."""
    cards = []
    where = str(lines.get("어디까지") or "-")
    parts = [p.strip() for p in where.split(" · ") if p.strip()]
    if kill:
        cards.append({"k": "결과", "v": f"킬 {mmss(duration)}" if duration else "킬", "s": "", "tone": "green"})
    else:
        head = parts[0] if parts else "-"
        sub = " · ".join(re.sub(r"\s*\([^)]*\)", "", p) for p in parts[1:] if not p.startswith("보스 HP"))
        cards.append({"k": "어디까지", "v": _clip(re.sub(r"\s*\([^)]*\)", "", head), 12), "s": _clip(sub, 26)})
    hp = _hp(lines, out)
    dps = str(lines.get("딜·힐") or "")
    m = re.search(r"우리\s*([+\-−]?\d+(?:\.\d+)?%p)", dps)
    best = re.search(r"우리 최고 (\d+:\d{2})", dps)
    if kill:
        kc = next((c for c in dps.split(" · ") if c.strip().startswith(("처치", "킬"))), "")
        other = (re.search(r"다른 공대 (?:킬 )?(\d+:\d{2}~\d+:\d{2})", kc)
                 or re.search(r"다른 공대 킬 (\d+:\d{2}~\d+:\d{2})", dps))
        if best:
            cards.append({"k": "우리 최고", "v": best.group(1), "s": f"다른 공대 킬 {other.group(1)}" if other else ""})
        else:
            cards.append({"k": "다른 공대 킬", "v": other.group(1) if other else "-", "s": ""})
    else:
        cards.append({"k": "보스 HP", "v": f"{hp:.1f}%" if isinstance(hp, (int, float)) else "-",
                      "s": f"다른 공대 같은 시각 대비 {m.group(1)}" if m else ""})
    deaths = [d for d in ((facts or {}).get("deaths") or []) if isinstance(d, dict)]
    pre = re.search(r"붕괴 전 사망 (\d+)명", str(lines.get("전멸 원인") or ""))
    first = min(deaths, key=lambda d: float(d.get("sec") or 0)) if deaths else None
    sub = (f"붕괴 전 {pre.group(1)}명" if pre else
           (f"첫 {str(first.get('name') or '').split('-')[0]} {mmss(first.get('sec'))}" if first else ""))
    cards.append({"k": "사망", "v": f"{len(deaths)}명", "s": sub, "tone": "red" if (kill and deaths) else None})
    cards += boss_cards(lines, out, deaths)
    return cards[:5]


def boss_cards(lines: dict, out: dict, deaths: list) -> list[dict]:
    """보스별 2칸. 울라텍: 알 운반 · 살무사 스턴 / 똬리: 단두대 · P2 / 나머지: 첫 사망 · 딜."""
    plan = out.get("plan") or {}
    ver = str(out.get("version") or "")
    if ver.startswith("ULATEK"):
        ec = plan.get("eggCarry") or {}
        bad = sum(int(r.get("bad") or 0) for r in ec.get("rounds") or [])
        vp = plan.get("viper") or {}
        waves = len(vp.get("waveCarriers") or [])
        stuns = [s for s in vp.get("stuns") or [] if s.get("stunned")]
        stunned = [n for s in stuns for n in s.get("stunned") or []]
        big = (plan.get("bigEgg") or {})
        egg_sub = f"파도 {waves}" + (f" · 큰 알 {len(big.get('picks') or [])}/4" if big else "")
        return [
            {"k": "알 운반", "v": f"문제 {bad}" if bad else ("✓" if ec else "-"), "s": egg_sub if ec else "알 단계 전",
             "tone": "red" if bad else ("green" if ec else None)},
            {"k": "살무사 스턴", "v": f"{len(stunned)}명", "s": _clip(", ".join(f"{n} {mmss(s.get('sec'))}" for s in stuns[:2] for n in (s.get('stunned') or [])[:1]), 26),
             "tone": "orange" if stunned else None},
        ]
    if ver.startswith("COILED"):
        gs = out.get("guillotines") or []
        exe = sum(len(g.get("executeHit") or []) for g in gs) if gs else 0
        miss = sum(len(g.get("soakMissed") or []) for g in gs) if gs else 0
        p2 = next((s for s in out.get("stages") or [] if s.get("name") in ("말라크라스", "P2") and s.get("start") is not None), None)
        return [
            {"k": "단두대", "v": f"{len(gs)}번", "s": (f"처형 {exe}명" if exe else "처형 없음") + (f" · 못 받음 {miss}" if miss else ""),
             "tone": "red" if exe else None},
            {"k": "P2 진입", "v": mmss(p2.get("start")) if p2 else "못 감", "s": "", "tone": None if p2 else "muted"},
        ]
    dps = str(lines.get("딜·힐") or "")
    first = str(lines.get("첫 실수") or "").split(" → ")[0]
    none = first.startswith(("사망 없음", "-")) or ("없음" in first and not _TIME.search(first))
    st = _step(first) if first and not none else None
    is_death = first.startswith("첫 사망")
    card4 = {"k": "첫 사망" if (is_death or not st) else "첫 실수",
             "v": st["t"] if st and st.get("t") else ("없음" if none else "-"),
             "s": _clip(re.sub(r"^첫 사망\s*", "", st["text"]), 26) if st else ""}
    # 딜·힐에서 단계 진입 시각 하나 (「의식 진입 1:25 (다른 공대 1:14~1:54)」)
    stage = None
    for c in dps.split(" · "):
        m = re.match(r"^(.+?) (\d+:\d{2}) \(다른 공대 (?:킬 )?([^)]*)\)", c.strip())
        if m and not m.group(1).startswith(("처치", "킬")):
            stage = {"k": _clip(m.group(1), 10), "v": m.group(2), "s": "다른 공대 " + m.group(3).split(" ·")[0]}
            break
    if stage is None and str(lines.get("어디까지") or "").startswith(("처치", "킬")):
        return [card4]   # 킬인데 단계 시각이 없으면 4칸
    if stage is None:
        head = dps.split(" · ")[0]
        stage = {"k": "딜·힐", "v": _clip(re.sub(r"\s*\([^)]*\)", "", head), 14) or "-",
                 "s": _clip(" · ".join(dps.split(" · ")[1:2]), 26)}
    return [card4, stage]


def next_line(lines: dict) -> str:
    t = str(lines.get("다음 풀") or "").strip()
    if not t or t == "-":
        return ""
    t = re.sub(r"\s*\([^)]*\)", "", t)          # 괄호 설명은 「자세히」에
    t = re.split(r" / | · |\. |; ", t)[0]
    t = t.strip().rstrip(".")
    return _clip(t, NEXT_MAX)


def build(out: dict, facts: dict | None = None, result: str | None = None) -> dict:
    """결론 모듈 결과(out) → {"chain": [...], "stats": [...], "next": str, "nextLabel": str}."""
    lines = dict(out.get("lines") or {})
    labels = dict(out.get("labels") or {})
    where = str(lines.get("어디까지") or "")
    if out.get("excluded") or not lines or where.startswith(("가짜", "리셋", "Reset")):
        return None   # 판정 제외 풀은 예전 5줄 그대로
    kill = str(result or "").lower() == "kill" or str(lines.get("어디까지") or "").startswith("처치")
    duration = out.get("durationSec")
    return {
        "chain": chain(lines, kill, duration, out.get("cause") if isinstance(out.get("cause"), dict) else None),
        "stats": stats(lines, out, facts, kill, duration),
        "next": next_line(lines),
        "nextLabel": "다음 주 · 한 가지만" if (kill or labels.get("다음 풀") == "다음 주") else "다음 풀 · 한 가지만",
    }
