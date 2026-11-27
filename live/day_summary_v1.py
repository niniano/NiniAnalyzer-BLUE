"""1.0.25 그날 최종 정리 · 보완사항 (네임드 트라이를 날짜별로 묶어 최종 정리 · 보완사항).

보스 하루치 트라이(같은 날 · 같은 난이도)를 모아서
  · 최종 정리: 그날 수치 (어디서 · 왜 무너졌나, 다른 공대 킬 기준과 비교)
  · 보완사항: 다음 주에 고칠 것 (보드 오른쪽 「보완사항」 칸에 전부)
  · 트라이별 핵심 실패: 결론 모듈의 첫 실수 (보드 「트라이 한눈에」 · 「반복 실패」에 씀)

지원: 똬리의 제단(3429) · 울라텍(3492). 원본 로그를 한 번 더 읽으므로(좌표 · 디버프) 트라이마다
day_summary_v1.json 으로 저장해 두고 다음부터는 바로 읽는다 (원본 로그가 바뀌면 다시 계산).

기준값 (여러 공대 킬 로그를 같은 방법으로 잰 값):
  · 똬리 1페 전환 2:00~2:10 · 1페 단두대 막은 사람 중 딜러 55~70% · 막페 2단두대 = 막페 시작 +84초 (1단두대 +64초)
  · 똬리 1페 위쪽 공대원이 사거리 안 힐러 2명 이하인 시간 4~11% · 암영폭탄·구슬 뒤 힐러 5초 넘게 이탈은 힐러당 하루 1번 정도
  · 울라텍 송곳니 줄 3:17 고정 → 1명 먼저 → 핏줄 끝난 뒤(+4초 넘게) 5명이 1초 안 · 큰 알 부화 2:54 고정
"""
from __future__ import annotations

import gzip
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

VERSION = "1.0.25-DAY"
CACHE_NAME = "day_summary_v1.json"
SUPPORTED = {3429, 3492}

HEALER_SPECS = {65, 105, 256, 257, 264, 270, 1468}
TANK_SPECS = {66, 73, 104, 250, 268, 581}
EVOKER_PRES = 1468
GLOOMBOMB = "1310881"         # 암영폭탄
PURPLE_ORB = "1310498"        # 돌연변이 맹독 (보라구슬 · 5초)
REF_P1_SWITCH = (120.0, 130.0)
REF_P1_DPS_SOAK = (55, 70)
REF_TOP_THIN = (4, 11)
HEAL_RANGE = 40.0
EVOKER_RANGE = 30.0
ISOLATE_RANGE = 30.0
ISOLATE_MIN_SEC = 5.0
DPS_SOAK_WARN = 75            # 1페 전환이 늦을 때만: 1페 단두대를 딜러가 이만큼(%) 넘게 막으면 보완사항
TOP_THIN_WARN = 15            # 위쪽 공대원이 힐러 2명 이하인 시간(%) 이만큼 넘으면 보완사항


def mmss(sec) -> str:
    if sec is None:
        return "-"
    sec = max(0, int(float(sec)))
    return f"{sec // 60}:{sec % 60:02d}"


def short(name) -> str:
    return str(name or "").split("-")[0]


def _module(enc: int):
    if enc == 3429:
        from live import coiled_wipe_cause_v1 as m
    else:
        from live import ulatek_conclusion_v1 as m
    return m


def _raw_path(d: Path) -> Path | None:
    for n in ("raw.log.gz", "raw.log"):
        p = d / n
        if p.is_file():
            return p
    return None


def _stamp(d: Path, enc: int | None = None) -> list:
    out = []
    if enc in SUPPORTED:
        try:
            out.append(["module", str(getattr(_module(enc), "VERSION", ""))])
        except Exception:
            pass
    for n in ("meta.json", "raid_facts.json", "raw.log.gz", "raw.log", "mechanic_review.json", "raid_wipe_review.json"):
        p = d / n
        try:
            out.append([n, int(p.stat().st_mtime), p.stat().st_size] if p.exists() else [n, 0, 0])
        except OSError:
            out.append([n, 0, 0])
    return out


def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ------------------------------------------------------------------ 원본 로그 한 번 더 (좌표 · 디버프)
def _ts(line: str):
    try:
        from datetime import datetime
        a, b = line.split()[:2]
        return datetime.strptime(a + " " + b[:15], "%m/%d/%Y %H:%M:%S.%f")
    except Exception:
        return None


def scan_positions(raw_path: Path) -> dict:
    """플레이어 시전 성공(정보 = 시전자)에서 좌표 · 암영폭탄/보라구슬 디버프 시작·끝."""
    import csv
    pos = defaultdict(list)
    debuff = []
    t0 = None
    opener = gzip.open if str(raw_path).endswith(".gz") else open
    with opener(raw_path, "rb") as fh:
        for raw in fh:
            if t0 is None:
                if b"  " not in raw[:30]:
                    continue
                t0 = _ts(raw.decode("utf-8", "replace"))
                continue
            is_cast = b"  SPELL_CAST_SUCCESS,Player-" in raw
            is_deb = (b"SPELL_AURA_" in raw[:60]) and (b",1310881," in raw or b",1310498," in raw)
            if not (is_cast or is_deb):
                continue
            line = raw.decode("utf-8", "replace")
            t = _ts(line)
            if t is None:
                continue
            s = (t - t0).total_seconds()
            try:
                f = next(csv.reader([line.split("  ", 1)[1].strip()]))
            except Exception:
                continue
            if is_cast and len(f) > 27 and f[12] == f[1]:
                try:
                    pos[short(f[2])].append((round(s, 1), float(f[26]), float(f[27])))
                except ValueError:
                    pass
            elif is_deb and len(f) > 9 and f[0] in ("SPELL_AURA_APPLIED", "SPELL_AURA_REMOVED"):
                debuff.append((round(s, 2), f[0][11:], f[9], short(f[6])))
    return {"pos": {k: v for k, v in pos.items()}, "debuff": debuff}


def _at(series: list, t: float, slack: float = 3.0):
    best = None
    for p in series:
        if p[0] <= t:
            best = p
        else:
            break
    return best if best and t - best[0] <= slack else None


def healer_metrics(scan: dict, roster: list, p1_end: float | None, end: float) -> dict:
    """1페 위·아래 사거리 안 힐러 수 · 암영폭탄/보라구슬 끝난 뒤 힐러가 떨어져 있던 시간."""
    spec = {short(r.get("name")): int(r.get("specID") or 0) for r in roster or []}
    healers = [n for n, s in spec.items() if s in HEALER_SPECS]
    pos = scan.get("pos") or {}
    rng = {h: (EVOKER_RANGE if spec.get(h) == EVOKER_PRES else HEAL_RANGE) for h in healers}
    top, bot, xs = [], [], defaultdict(list)
    t = 5.0
    while p1_end and t < p1_end:
        hp = {h: _at(pos.get(h, []), t) for h in healers}
        hp = {k: v for k, v in hp.items() if v}
        for h, p in hp.items():
            xs[h].append(p[1])
        ps = {n: _at(pos.get(n, []), t) for n in spec if n not in healers}
        ps = {k: v for k, v in ps.items() if v}
        if len(ps) >= 5:
            med = statistics.median(p[1] for p in ps.values())
            for n, p in ps.items():
                k = sum(1 for h, q in hp.items() if math.hypot(p[1] - q[1], p[2] - q[2]) <= rng[h])
                if p[1] > med + 5:
                    top.append(k)
                elif p[1] < med - 5:
                    bot.append(k)
        t += 2.0
    # 힐러 이탈
    iso = []
    for h in healers:
        ends = [s for s, ev, sid, who in scan.get("debuff") or [] if who == h and ev == "REMOVED"]
        kinds = {s: ("암영폭탄" if sid == GLOOMBOMB else "구슬") for s, ev, sid, who in scan.get("debuff") or []
                 if who == h and ev == "REMOVED"}
        for e in ends:
            run, t = 0.0, e
            while t <= e + 40:
                p = _at(pos.get(h, []), t)
                if not p:
                    break
                near = sum(1 for n in spec if n != h and _at(pos.get(n, []), t)
                           and math.hypot(p[1] - _at(pos.get(n, []), t)[1], p[2] - _at(pos.get(n, []), t)[2]) <= ISOLATE_RANGE)
                if near <= 3:
                    run += 1.0
                    t += 1.0
                else:
                    break
            if run >= ISOLATE_MIN_SEC:
                iso.append({"name": h, "after": kinds.get(e, ""), "sec": e, "dur": run})
    carry = Counter(who for s, ev, sid, who in scan.get("debuff") or [] if sid == PURPLE_ORB and ev == "APPLIED")
    return {"healers": healers, "top": top, "bot": bot,
            "topThin": (round(100 * sum(1 for k in top if k <= 2) / len(top)) if top else None),
            "healerX": {h: round(statistics.mean(v)) for h, v in xs.items() if v},
            "isolation": iso, "orbCarry": dict(carry)}


# ------------------------------------------------------------------ 트라이 하나
def try_facts(enc: int, d, use_cache: bool = True) -> dict:
    d = Path(d)
    cache = d / CACHE_NAME
    stamp = _stamp(d, enc)
    if use_cache and cache.is_file():
        c = _load_json(cache)
        if c.get("version") == VERSION and c.get("stamp") == stamp:
            return c
    meta = _load_json(d / "meta.json")
    facts = _load_json(d / "raid_facts.json")
    mod = _module(enc)
    try:  # 보드 결론 5줄과 같은 결과 (똬리는 공대 차단 규칙까지)
        from live.v52_screen import build_module as _v52_build
        out = _v52_build(mod, str(d))
    except ImportError:
        out = mod.build_from_dir(d, use_raw=True)
    L = out.get("lines") or {}
    res = {"version": VERSION, "stamp": stamp, "tryNo": meta.get("tryNo"), "result": str(meta.get("result") or ""),
           "kill": str(meta.get("result") or "").lower() == "kill", "dur": float(meta.get("durationSec") or 0),
           "lines": {k: str(L.get(k) or "") for k in ("어디까지", "첫 실수", "전멸 원인", "다음 풀")},
           "cause": {k: (out.get("cause") or {}).get(k) for k in ("group", "code", "confidence", "label")},
           "stages": out.get("stages") or []}
    roster = facts.get("roster") or []
    roles = {short(r.get("name")): ("H" if int(r.get("specID") or 0) in HEALER_SPECS else
                                    "T" if int(r.get("specID") or 0) in TANK_SPECS else "D") for r in roster}
    deaths = sorted(facts.get("deaths") or [], key=lambda x: float(x.get("sec") or 0))
    if enc == 3429:
        st = {s["name"]: s.get("start") for s in res["stages"]}
        res["p1Switch"] = st.get("말라크라스")
        g1 = [g for g in out.get("guillotines") or [] if g.get("phase") == "p1"]
        res["p1Soak"] = Counter(roles.get(n, "?") for g in g1 for n in g.get("soakers") or [])
        mk = st.get("막페")
        g2 = next((g for g in out.get("guillotines") or [] if g.get("phase") == "p3" and g.get("index") == 2), None)
        wail = out.get("wailEvents") or []
        if g2 and g2.get("resolveSec") is not None and res["dur"] >= float(g2.get("resolveSec") or 0) + 1:
            m = float(g2["markSec"])
            feared = any(e["kind"] == "done" and m - 15 <= float(e["sec"]) <= m + 5 for e in wail)
            early = [short(x.get("name")) for x in deaths if m - 20 <= float(x.get("sec") or 0) < m + 4.9]
            res["g2"] = {"mark": m, "afterMk": (m - float(mk)) if mk is not None else None, "soak": len(g2.get("soakers") or []),
                         "exec": int(g2.get("executeHits") or 0), "target": g2.get("target"),
                         "why": "공포" if feared else ("조원 먼저 사망" if len(early) >= 2 else "")}
        kicks = Counter(e.get("by") or "?" for e in wail if e["kind"] == "kick")
        done = [float(e["sec"]) for e in wail if e["kind"] == "done"]
        res["wail"] = {"starts": sum(1 for e in wail if e["kind"] == "start"), "kicks": dict(kicks), "done": len(done),
                       "doneHealthy": sum(1 for t in done if sum(1 for x in deaths if float(x.get("sec") or 0) < t) <= 6)}
        res["resonance"] = "현신" in res["lines"]["첫 실수"]
        rp = _raw_path(d)
        if rp is not None:
            try:
                scan = scan_positions(rp)
                res["heal"] = healer_metrics(scan, roster, res.get("p1Switch") or min(res["dur"], 125.0), res["dur"])
            except Exception:
                res["heal"] = None
    else:
        plan = out.get("plan") or {}
        fang = plan.get("fang") or []
        res["fang"] = [{"late": [str(y.get("name") if isinstance(y, dict) else y) for y in x.get("late") or []],
                        "spread": x.get("spread"), "extra": int(x.get("extraTicks") or 0)} for x in fang]
        be = plan.get("bigEgg") or {}
        res["bigEgg"] = {"picks": [float(p["pick"]) for p in be.get("picks") or []],
                         "drops": [float(p["drop"]) for p in be.get("picks") or [] if p.get("drop") is not None]}
        sr = plan.get("south") or {}
        res["south"] = {"actual": sr.get("actual"), "enraged": sr.get("oozeEnraged"), "ooze": sr.get("ooze")}
        lake = plan.get("venomLake") or []
        res["lake"] = {"n": len(lake), "dead": sum(1 for x in lake if x.get("died") is not None),
                       "short": sum(1 for x in lake if float(x.get("dur") or 0) <= 2.0),
                       "shortDead": sum(1 for x in lake if float(x.get("dur") or 0) <= 2.0 and x.get("died") is not None)}
        res["heart1Short"] = bool(out.get("heart1Short"))
    try:
        cache.write_text(json.dumps(res, ensure_ascii=False, default=lambda o: dict(o) if isinstance(o, Counter) else str(o)),
                         encoding="utf-8")
    except OSError:
        pass
    return res


def needs_compute(enc: int, d) -> bool:
    d = Path(d)
    c = _load_json(d / CACHE_NAME)
    return not (c.get("version") == VERSION and c.get("stamp") == _stamp(d, enc))


# ------------------------------------------------------------------ 하루치
def board_issue(f: dict) -> tuple[str, str]:
    """트라이 한눈에 칸: 첫 실수(있으면) · 없으면 전멸 원인. 꼬리표 verified / candidate."""
    if f.get("kill"):
        return "킬 · 공대 붕괴 없음", "kill"
    first = str(f["lines"].get("첫 실수") or "")
    cause = str(f["lines"].get("전멸 원인") or "")
    if first and not first.startswith("확정된 첫 실수 없음") and first != "-":
        txt = first
    else:
        txt = cause.split(" · 붕괴 전")[0]
    tag = "verified" if ("[확정]" in txt or (f.get("cause") or {}).get("confidence") == "verified") else "candidate"
    for t in (" [확정]", " [추정]", " [미확정]"):
        txt = txt.replace(t, "")
    return txt, tag


def _pct(a, b) -> int:
    return round(100 * a / b) if b else 0


def build_day(enc: int, dirs: list, progress=None, use_cache: bool = True) -> dict:
    """dirs = 그날 트라이 폴더들. progress(i, n) 는 트라이 하나 끝날 때마다 부름."""
    fs = []
    dirs = [Path(x) for x in dirs]
    for i, d in enumerate(dirs):
        try:
            f = dict(try_facts(enc, d, use_cache=use_cache))
            f["dir"] = str(d)
            if f.get("dur", 0) >= 20:
                fs.append(f)
        except Exception:
            pass
        if progress:
            try:
                progress(i + 1, len(dirs))
            except Exception:
                pass
    fs.sort(key=lambda f: int(f.get("tryNo") or 0))
    wipes = [f for f in fs if not f["kill"]]
    kills = [f for f in fs if f["kill"]]
    summary, todo = [], []
    head = f"{len(fs)}트 · 킬 {len(kills)} · 와이프 {len(wipes)}"
    causes = Counter(str((f.get("cause") or {}).get("group") or "") for f in wipes)
    causes.pop("", None)
    for k in ("원인 미확정", "판정 제외"):
        causes.pop(k, None)
    if causes:
        head += " · 많은 원인 " + " · ".join(f"{k} {v}" for k, v in causes.most_common(3))
    summary.append(head)
    if enc == 3429:
        _coiled(fs, wipes, summary, todo)
    else:
        _ulatek(fs, wipes, summary, todo)
    rows = {}
    for f in fs:
        issue, tag = board_issue(f)
        rows[str(f.get("dir") or f.get("tryNo"))] = {"tryNo": f.get("tryNo"), "issue": issue, "tag": tag,
                                                     "group": (f.get("cause") or {}).get("group"),
                                                     "next": f["lines"].get("다음 풀")}
    repeats = []
    for k, v in causes.most_common():
        tries = sorted(int(f.get("tryNo") or 0) for f in wipes if (f.get("cause") or {}).get("group") == k)
        repeats.append({"label": k, "count": v, "tries": tries})
    return {"version": VERSION, "encounterID": enc, "tries": len(fs), "summary": summary, "todo": todo,
            "rows": rows, "repeats": repeats}


def _coiled(fs, wipes, summary, todo):
    sw = [float(f["p1Switch"]) for f in fs if f.get("p1Switch")]
    if sw:
        avg = statistics.mean(sw)
        ok = avg <= REF_P1_SWITCH[1] + 3
        summary.append(f"1페 전환 평균 {mmss(avg)} (다른 공대 킬 {mmss(REF_P1_SWITCH[0])}~{mmss(REF_P1_SWITCH[1])})"
                       + (" → 1페 딜 부족 아님" if ok else " → 1페 딜이 늦음"))
        if not ok:
            todo.append(f"1페 딜: 전환이 평균 {mmss(avg)} 로 다른 공대보다 늦어요 · 쿨기 타이밍 확인")
    soak = Counter()
    for f in fs:
        soak.update(f.get("p1Soak") or {})
    tot = sum(soak.values())
    if tot:
        dps = _pct(soak.get("D", 0), tot)
        summary.append(f"1페 단두대 막은 사람 딜러 {dps}% · 힐러 {_pct(soak.get('H', 0), tot)}% "
                       f"(다른 공대 딜러 {REF_P1_DPS_SOAK[0]}~{REF_P1_DPS_SOAK[1]}%)")
        if dps >= DPS_SOAK_WARN and sw and statistics.mean(sw) > REF_P1_SWITCH[1] + 3:
            todo.append("1페 단두대는 원딜·힐러 위주로 맡기기 (딜러가 덜 빠지면 딜 손실이 줄어요)")
    g2 = [f for f in fs if f.get("g2")]
    if g2:
        ok = [f for f in g2 if not f["g2"]["exec"] and f["g2"]["soak"] > 0]
        bad = [f for f in g2 if f not in ok]
        aft = [f["g2"]["afterMk"] for f in g2 if f["g2"].get("afterMk") is not None]
        txt = f"막페 2단두대 {len(g2)}번 · 막페 시작 +{statistics.mean(aft):.0f}초" if aft else f"막페 2단두대 {len(g2)}번"
        txt += f" · 넘김 {len(ok)}"
        if ok:
            txt += " (막은 인원 " + "·".join(str(f["g2"]["soak"]) for f in ok) + ")"
        if bad:
            why = Counter(f["g2"]["why"] or "인원 부족" for f in bad)
            txt += " · 실패 " + ", ".join(f"#{f['tryNo']}" for f in bad) + " (" + " · ".join(f"{k} {v}" for k, v in why.items()) + ")"
        summary.append(txt)
        if bad:
            todo.append("막페 2단두대(막페 시작 +84초 · 1단두대 +64초) 조원은 그 전 20초 동안 구슬·공명·무덤구속으로 죽지 않기")
    kicks = Counter()
    starts = done = healthy = 0
    for f in fs:
        w = f.get("wail") or {}
        kicks.update(w.get("kicks") or {})
        starts += int(w.get("starts") or 0)
        done += int(w.get("done") or 0)
        healthy += int(w.get("doneHealthy") or 0)
    if starts:
        tk = sum(kicks.values())
        top = kicks.most_common(3)
        share = _pct(top[0][1], tk) if top else 0
        summary.append(f"공포의 통곡: 시전 {starts} · 차단 {tk} (" + " · ".join(f"{n} {c}" for n, c in top) + ")"
                       + f" · 끝까지 시전 {done} (공대가 멀쩡할 때 {healthy})")
        if share >= 60 and top:
            summary[-1] += f" · {top[0][0]} 혼자 {share}%"
        if healthy:
            order = " → ".join(n for n, _c in kicks.most_common(3))
            todo.append(f"통곡 차단 2~3명 순번 ({order}) · 1차 차단 직후 다음 차단자 콜 · 차단 담당이 죽으면 바로 대신")
    res = sum(1 for f in wipes if f.get("resonance"))
    if res:
        summary.append(f"현신 접촉(악의적인 공명)이 첫 실수인 트라이 {res}")
        todo.append("현신(유령) 대상자끼리 동선 분리")
    orb_fail = sum(1 for f in wipes if "구슬" in str((f.get("cause") or {}).get("group") or ""))
    if orb_fail:
        summary.append(f"막페 구슬 처리 실패로 무너진 트라이 {orb_fail}")
        todo.append("막페 보라구슬 4개는 1보막 중 옮기기 · 2단두대 뒤 남은 구슬 순서 확인")
    hs = [f["heal"] for f in fs if f.get("heal")]
    if hs:
        thin = [h["topThin"] for h in hs if h.get("topThin") is not None]
        if thin:
            avg = round(statistics.mean(thin))
            summary.append(f"1페 위쪽 공대원이 사거리 안 힐러 2명 이하인 시간 평균 {avg}% (다른 공대 {REF_TOP_THIN[0]}~{REF_TOP_THIN[1]}%)")
            if avg >= TOP_THIN_WARN:
                todo.append("1페 힐러 한 명은 위쪽에 고정 (힐러가 아래에 몰려 있어요)")
        iso = defaultdict(list)
        for h in hs:
            for x in h.get("isolation") or []:
                iso[x["name"]].append(x)
        if iso:
            parts = []
            for n, xs in sorted(iso.items(), key=lambda kv: -len(kv[1])):
                parts.append(f"{n} {len(xs)}번 (" + ", ".join(f"{x['after']} 뒤 {x['dur']:.0f}초" for x in xs[:3]) + ")")
            summary.append("암영폭탄·구슬 끝난 뒤 힐러가 5초 넘게 떨어져 있음: " + " · ".join(parts[:3]))
            many = [n for n, xs in iso.items() if len(xs) >= 3]
            if many:
                todo.append(", ".join(many) + ": 암영폭탄·구슬 끝나면 바로 본진 쪽으로 (힐 공백)")
        carry = Counter()
        for h in hs:
            carry.update(h.get("orbCarry") or {})
        if carry:
            summary.append("보라구슬 많이 든 사람: " + " · ".join(f"{n} {c}" for n, c in carry.most_common(3)))


def _ulatek(fs, wipes, summary, todo):
    egg = [f for f in wipes if "살무사" in str((f.get("cause") or {}).get("group") or "")]
    h1 = sum(1 for f in wipes if f.get("heart1Short"))
    if egg:
        first = [str(f["lines"].get("첫 실수") or "") for f in egg]
        wave = sum(1 for t in first if "부식의 파도" in t)
        hit = sum(1 for t in first if "알 충돌" in t)
        died = sum(1 for t in first if "알 든" in t and "사망" in t)
        parts = [f"부식의 파도 {wave}" if wave else "", f"알끼리 충돌 {hit}" if hit else "", f"알 든 채 사망 {died}" if died else ""]
        summary.append(f"알 운반 → 살무사로 시작한 트라이 {len(egg)} (" + " · ".join(x for x in parts if x) + ")"
                       + (f" · 1심장 미달 {h1}" if h1 else ""))
        if wave or hit:
            todo.append("알 든 사람은 부식의 파도·다른 알 든 사람과 부딪히지 않기 (살무사 → 1심장 딜 부족)")
        if died:
            todo.append("알 운반자 힐·생존기 우선 (알 든 채 죽으면 그 자리에서 깨져 공대 전체 부패한 막 + 살무사)")
    fg = [x for f in fs for x in (f.get("fang") or [])]
    if fg:
        late = [n for x in fg for n in x.get("late") or []]
        ok = sum(1 for x in fg if not x.get("late"))
        sp = [float(x["spread"]) for x in fg if x.get("spread") is not None]
        summary.append(f"송곳니 줄 {len(fg)}번" + (f" · 첫 끊기 → 마지막까지 평균 {statistics.mean(sp):.1f}초" if sp else "")
                       + f" · 제대로 끊음 {ok}" + (f" · 늦게 끊음 {len(fg) - ok} (" + ", ".join(sorted(set(late))[:4]) + ")" if late else ""))
        if late:
            todo.append("송곳니 줄은 1명 먼저 → 핏줄 3틱(끊는 순간 · 2초 · 4초) 끝난 뒤 5명 1초 안에 같이")
    picks = [p for f in fs for p in (f.get("bigEgg") or {}).get("picks") or []]
    drops = [p for f in fs for p in (f.get("bigEgg") or {}).get("drops") or []]
    if picks:
        acts = Counter((f.get("south") or {}).get("actual") for f in fs if (f.get("south") or {}).get("actual"))
        act = {"big_first": "큰 알 먼저(보호막 알 안 깸)", "shield_first": "보호막 알 먼저", "mixed": "섞임"}
        rg = lambda xs: mmss(min(xs)) if mmss(min(xs)) == mmss(max(xs)) else f"{mmss(min(xs))}~{mmss(max(xs))}"
        txt = f"큰 알 들기 {rg(picks)}" + (f" · 넣기 {rg(drops)}" if drops else "") + " (부화 2:54 고정)"
        if acts:
            txt += " · 남쪽 " + " · ".join(f"{act.get(k, k)} {v}" for k, v in acts.items())
        enr = sum(int((f.get("south") or {}).get("enraged") or 0) for f in fs)
        if enr:
            txt += f" · 유체 광폭 {enr}"
            todo.append("남쪽 유체는 광폭(산성 배출) 전에 처치 · 광폭 시전 땐 배정된 CC")
        summary.append(txt)
    lake_root = sum(1 for f in wipes if "똥물" in str((f.get("cause") or {}).get("group") or ""))
    sh = sum((f.get("lake") or {}).get("short", 0) for f in fs)
    shd = sum((f.get("lake") or {}).get("shortDead", 0) for f in fs)
    if sh or lake_root:
        summary.append("똥물(죽음의 맹독)" + (f" · 2초 이하로 잠깐 빠져도 {_pct(shd, sh)}% 사망 ({shd}/{sh})" if sh else "")
                       + (f" · 똥물이 들어간 원인 {lake_root}트" if lake_root else ""))
        if lake_root or (sh >= 5 and _pct(shd, sh) >= 40):
            todo.append("똥물은 잠깐 밟아도 죽어요 · 1심장 구간 동선 확인")
    tank = [f for f in wipes if "탱 사망" in str((f.get("cause") or {}).get("group") or "")]
    if tank:
        summary.append(f"탱 사망 → 붕괴 {len(tank)} (" + ", ".join(f"#{f.get('tryNo')}" for f in tank) + ")")
        nx = Counter(str(f["lines"].get("다음 풀") or "") for f in tank)
        nx.pop("", None)
        if nx:
            todo.append(nx.most_common(1)[0][0])
