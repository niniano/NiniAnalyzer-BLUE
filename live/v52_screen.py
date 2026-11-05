"""V5.2 트라이 복기 화면 보강 (화면 전용 · 판정·저장 데이터 불변).

v43_layout.apply 끝에서 apply(app, row) 를 부른다. 어디서 실패해도 기존 화면은 그대로 남는다.

- 개발용 글자 숨기기: 「범위: …」, 「n=3」, 「raidPlan judge n=… + 관측 n=…」 (sanitize, v43 reword 가 부른다)
- 붕괴 체인 「타임라인 →」: 기믹 시간축으로 내려가서 그 시각에 주황 세로선 / 「개인 분석 →」: 개인 사망 기록 열기
- P2 보호막 칸: 결과 · 딜 · 쿨기 · 사망 네 줄 (mechanic_review.json 의 보호막 기록 그대로)
- 암영폭탄 카드: 폭탄마다 쫄 뎀감 해제 n/2 (「1차단 전 폭발」 순서 규칙은 이 공대 공략과 반대라 카드 판정에서 뺌)
- 현신 접촉: raw 로그의 악의적인 공명 디버프(1310744)로 누가 누구와 닿았는지 + 사망 여부
"""
from __future__ import annotations

import gzip
import json
import re
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path

VERSION = "V5.2-SCREEN"
COILED = 3429
RESONANCE_AURA = "1310744"
RESONANCE_DMG = "1312132"
FIXATION = "1285911"

# ---------------------------------------------------------------- 개발용 글자
_DEV = (
    (re.compile(r"\s*·?\s*raidPlan judge n=\d+\s*\+\s*관측 n=\d+"), ""),
    (re.compile(r"\s*·\s*범위:[^·\n]*?(?=\s*·|\s*$|\n)"), ""),
    (re.compile(r"\s*·\s*n=\d+"), ""),
)


def sanitize(text) -> str:
    out = str(text)
    if "범위:" not in out and "n=" not in out and "raidPlan" not in out:
        return out
    for pat, rep in _DEV:
        out = pat.sub(rep, out)
    return out


# ---------------------------------------------------------------- 공용
def mmss(sec) -> str:
    if sec is None:
        return "-"
    sec = max(0.0, float(sec))
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def short(name) -> str:
    return str(name or "?").split("-")[0]


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------- 보호막
def _cooldown_text(align: dict) -> str:
    auto = (align or {}).get("autoSpecCooldowns") or {}
    judged = [x for x in (auto.get("judgedRequirements") or []) if isinstance(x, dict)]
    observed = [x for x in (auto.get("observedRequirements") or []) if isinstance(x, dict)]
    rows = judged + observed
    if not rows:
        return "공대 쿨기 계획 없음"
    ok = sum(1 for x in judged if x.get("status") in ("success", "used", "ok"))
    parts = [f"계획 2분 쿨기 {ok}/{len(judged)}"] if judged else []
    missing = [x for x in rows if x.get("status") == "missing"]
    late = [x for x in rows if x.get("status") == "late"]
    if missing:
        parts.append("안 씀: " + ", ".join(f"{short(x.get('player'))} {x.get('label') or x.get('spellID')}" for x in missing[:5]))
    if late:
        parts.append("늦음: " + ", ".join(f"{short(x.get('player'))} {x.get('label') or x.get('spellID')} {float(x.get('deviationSec') or 0):+.1f}초"
                                          for x in late[:4]))
    if not missing and not late and judged:
        parts.append("전부 제때 사용")
    return " · ".join(parts)


def shield_rows(mech: dict) -> list[tuple[str, str]]:
    """보호막마다 (이름표, 한 줄) 네 개: 결과 · 딜 · 쿨기 · 사망."""
    out = []
    shields = [x for x in ((mech or {}).get("observations") or {}).get("shieldCycles") or [] if isinstance(x, dict)]
    for sh in shields:
        idx = sh.get("index") or len(out) // 4 + 1
        status = str(sh.get("status") or "unknown")
        start, end = sh.get("startSec"), sh.get("resolutionSec") or sh.get("breakSec")
        margin = sh.get("breakMarginSec")
        res = f"{mmss(start)} → {mmss(end)} · "
        if status == "success":
            res += f"깸 ✅ {float(sh.get('breakDurationSec') or 0):.0f}초"
            if margin is not None and float(margin) > 0:
                res += f" (여유 {float(margin):.1f}초)"
        elif status == "failed":
            res += f"못 깸 ✕ · {float(sh.get('remainingPct') or 0):.1f}% 남음"
            if margin is not None and -3.0 < float(margin) < 0:
                res += f" ({-float(margin):.1f}초 모자람)"
            if sh.get("castSuccessSec") is not None:
                res += " → 영원한 일몰"
        elif sh.get("wipeDuringCycle"):
            res += "보호막 중 전멸"
        else:
            res += "결과 확인 중"
        dev = sh.get("scheduleDeviationSec")
        if dev is not None and abs(float(dev)) >= 1.0:
            res += f" · 계획보다 {float(dev):+.0f}초"
        aps = sh.get("absorbedPerSec")
        init = sh.get("initialAbsorb")
        win = sh.get("castDurationObservedSec") or sh.get("windowDurationSec")
        if aps:
            dps = f"초당 {float(aps) / 1e6:.1f}M"
            if init and win:
                need = float(init) / float(win)
                dps += f" · 깨려면 {need / 1e6:.1f}M ({float(win):.0f}초 안에 {float(init) / 1e6:.1f}M)"
        else:
            dps = "흡수량 기록 없음"
        deaths = [short(d.get("player") or d.get("name")) if isinstance(d, dict) else short(d) for d in (sh.get("deathsDuring") or [])]
        dead = f"보호막 중 사망 {len(deaths)}명" + (f" ({', '.join(deaths[:6])}{' 외' if len(deaths) > 6 else ''})" if deaths else "") if deaths else "보호막 중 사망 없음"
        out += [(f"보호막 {idx}", res), ("딜", dps), ("쿨기", _cooldown_text(sh.get("cooldownAlignment") or {})), ("사망", dead)]
    return out


# ---------------------------------------------------------------- 암영폭탄 카드
def bomb_card(mech: dict) -> dict | None:
    cycles = [c for c in (((mech or {}).get("observations") or {}).get("phase2") or {}).get("cycles") or [] if isinstance(c, dict)]
    parts, bad, notes = [], 0, []
    for c in cycles:
        gb = c.get("gloombomb") or {}
        exp = int(gb.get("soulcoilerExpectedCount") or 0)
        known = int(gb.get("soulcoilerVulnerabilityKnownCount") or 0)
        rem = int(gb.get("soulcoilerVulnerabilityRemovedCount") or 0)
        if not exp or gb.get("damageSec") is None:
            continue
        parts.append(f"{c.get('index')}번 {rem}/{exp}" if known else f"{c.get('index')}번 ?")
        extra = [short(x) for x in (gb.get("extraHitPlayers") or [])]
        if known and rem < exp:
            bad += 1
            notes.append(f"{c.get('index')}번 {mmss(gb.get('damageSec'))} 쫄 {exp - rem}마리 그대로" + (f" · 같이 맞음 {len(extra)}명" if extra else ""))
        elif extra:
            notes.append(f"{c.get('index')}번 같이 맞음 {len(extra)}명")
    if not parts:
        return None
    return {"key": "gloombomb_add", "title": "암영폭탄 → 쫄 뎀감 해제", "status": "failed" if bad else "success",
            "value": " · ".join(parts[:3]) + (" …" if len(parts) > 3 else ""),
            "note": " · ".join(notes[:2]) if notes else "폭탄마다 쫄 2마리 해제 · 차단 순서는 판정 안 함"}


# ---------------------------------------------------------------- 현신 접촉 (raw)
def _ts(stamp: str):
    try:
        d, t = stamp.split(" ")
        return datetime.strptime(d + " " + t, "%m/%d/%Y %H:%M:%S.%f")
    except Exception:
        return None


def scan_resonance(raw_path: Path, start_ts: str) -> list[dict]:
    """악의적인 공명 디버프가 0.5초 안에 걸린 사람끼리 한 번의 접촉. 고정 대상·사망 여부를 붙인다."""
    t0 = _ts(start_ts)
    if t0 is None or not Path(raw_path).is_file():
        return []
    applies, fix_on, fix_off, deaths, dmg = [], [], [], [], []
    toks = ("," + RESONANCE_AURA + ",", "," + RESONANCE_DMG + ",", "," + FIXATION + ",", "UNIT_DIED")
    try:
        with gzip.open(raw_path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not any(t in line for t in toks):
                    continue
                stamp, _, body = line.partition("  ")
                t = _ts(stamp)
                if t is None:
                    continue
                sec = (t - t0).total_seconds()
                f = body.rstrip("\n").split(",")
                if len(f) < 7:
                    continue
                ev, who = f[0], short(f[6].strip('"'))
                sid = f[9] if len(f) > 9 else ""
                if ev == "UNIT_DIED" and f[5].startswith("Player-"):
                    deaths.append((sec, who))
                elif sid == RESONANCE_AURA and ev == "SPELL_AURA_APPLIED":
                    applies.append((sec, who))
                elif sid == RESONANCE_DMG and ev in ("SPELL_DAMAGE", "SPELL_ABSORBED", "SPELL_MISSED"):
                    dmg.append((sec, who))
                elif sid == FIXATION and ev == "SPELL_AURA_APPLIED":
                    fix_on.append((sec, who))
                elif sid == FIXATION and ev == "SPELL_AURA_REMOVED":
                    fix_off.append((sec, who))
    except Exception:
        return []

    def fixated(name, sec):
        on = [s for s, w in fix_on if w == name and s <= sec]
        if not on:
            return False
        last_on = max(on)
        return not any(w == name and last_on < s < sec for s, w in fix_off)

    eps = []
    for sec, who in sorted(applies):
        if eps and sec - eps[-1]["sec"] <= 0.5:
            if who not in eps[-1]["players"]:
                eps[-1]["players"].append(who)
        else:
            eps.append({"sec": round(sec, 1), "players": [who]})
    for ep in eps:
        ep["dead"] = [w for s, w in deaths if w in ep["players"] and 0 <= s - ep["sec"] <= 7.0
                      and any(w2 == w and ep["sec"] - 0.1 <= s2 <= s + 0.1 for s2, w2 in dmg)]
        ep["fixated"] = [w for w in ep["players"] if fixated(w, ep["sec"])]
        ep["level"] = "[확정]" if len(ep["players"]) >= 2 and len(ep["fixated"]) >= 2 else "[관측]"
    return eps


def resonance_lines(eps: list[dict]) -> list[str]:
    out = []
    for ep in eps:
        ps = ep["players"]
        pair = " ↔ ".join(ps[:2]) if len(ps) >= 2 else f"{ps[0]} ↔ (상대 기록 없음)"
        if len(ps) > 2:
            pair += f" 외 {len(ps) - 2}명"
        alive = [p for p in ps if p not in ep["dead"]]
        if ep["dead"]:
            tail = "사망: " + ", ".join(ep["dead"]) + (f" · 생존: {', '.join(alive)}" if alive else "")
        else:
            tail = "사망 없음"
        out.append(f"{mmss(ep['sec'])}{('.' + str(int(round(ep['sec'] % 1 * 10))) if ep['sec'] % 1 else '')} 현신 접촉 · {pair} · {tail} {ep['level']}")
    return out


def manifest_card(eps: list[dict]) -> dict | None:
    if not eps:
        return None
    dead = sum(len(e["dead"]) for e in eps)
    first = next((e for e in eps if e["dead"] and len(e["players"]) >= 2), eps[0])
    return {"key": "manifest", "title": "현신 접촉", "status": "failed",
            "value": f"{len(eps)}회" + (f" · 사망 {dead}명" if dead else ""),
            "note": f"{mmss(first['sec'])} " + " ↔ ".join(first["players"][:2]) + (f" 외 {len(eps) - 1}회" if len(eps) > 1 else "")}


# ---------------------------------------------------------------- 데이터 (스레드)
def load(row) -> dict:
    key = str(row.get("storageDir") or row.get("metaJson") or "")
    d = Path(key)
    if not d.is_dir():
        return {}
    meta = _read_json(d / "meta.json")
    mech = _read_json(d / "mechanic_review.json")
    eps = scan_resonance(d / str(meta.get("rawFile") or "raw.log.gz"), str(meta.get("startTimestamp") or ""))
    return {"shield": shield_rows(mech), "bomb": bomb_card(mech), "eps": eps, "manifest": manifest_card(eps)}


def prefill(app, row) -> None:
    """V5.2.2: 화면 없이 자료만 계산해 둔다 (백그라운드에서 부름)."""
    try:
        enc = int(row.get("encounterID") or 0)
    except (TypeError, ValueError):
        return
    key = str(row.get("storageDir") or row.get("metaJson") or "")
    if enc != COILED or not key:
        return
    st = _state(app)
    if key in st["data"]:
        return
    try:
        st["data"][key] = load(row)
    except Exception:
        st["data"][key] = {}


def _state(app) -> dict:
    st = getattr(app, "_v52", None)
    if st is None:
        st = {"data": {}, "pending": set(), "shield_frame": None, "wrapped": False}
        app._v52 = st
    return st


def apply(app, row) -> None:
    st = _state(app)
    _wrap_chain(app)
    wire_links(app)
    try:
        enc = int(row.get("encounterID") or 0)
    except (TypeError, ValueError):
        enc = 0
    key = str(row.get("storageDir") or row.get("metaJson") or "")
    st["current"] = key
    if enc != COILED or not key:
        _restore_shield(app)
        return
    data = st["data"].get(key)
    if data is None:
        if key in st["pending"]:
            return
        v43 = getattr(app, "_v43", None)
        if v43 is not None and key in v43.get("pending", ()):
            # V5.2.2: 결론 5줄 계산과 같이 계산된다 → 끝나면 한 번에 그린다
            _restore_shield(app)
            return
        st["pending"].add(key)

        def work():
            try:
                out = load(row)
            except Exception:
                out = {}
            st["data"][key] = out

            def done():
                st["pending"].discard(key)
                if st.get("current") == key:
                    try:
                        _render(app, out)
                    except Exception:
                        pass
            try:
                app.after(0, done)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()
        return
    _render(app, data)


def _render(app, data: dict) -> None:
    if not data:
        return
    for card in (data.get("bomb"), data.get("manifest")):
        if card:
            try:
                app._set_b3641_card(card)
            except Exception:
                pass
    lines = resonance_lines(data.get("eps") or [])
    if lines and hasattr(app, "b3641_try_evidence"):
        try:
            app._replace_readonly_text(app.b3641_try_evidence, "\n".join(lines)
                                       + "\n\n악의적인 공명 디버프를 같이 받은 사람 = 현신이 서로 닿은 두 고정 대상. [확정] = 둘 다 그때 현신 고정 대상")
        except Exception:
            pass
    if data.get("shield"):
        _render_shield(app, data["shield"])
    else:
        _restore_shield(app)


# ---------------------------------------------------------------- 보호막 칸 그리기
def _render_shield(app, rows) -> None:
    panel = getattr(app, "b365_shield_panel", None)
    if panel is None:
        return
    from live import ui_theme as T
    from live import v43_layout as L
    try:
        from nini_wcl_gui import FONT_BODY, FONT_TITLE  # type: ignore
    except Exception:
        FONT_BODY = FONT_TITLE = "TkDefaultFont"
    st = _state(app)
    frame = st.get("shield_frame")
    if frame is None or not frame.winfo_exists():
        frame = tk.Frame(panel.master, bg=T.PANEL_BG)
        st["shield_frame"] = frame
    for w in frame.winfo_children():
        w.destroy()
    for i, (label, text) in enumerate(rows):
        r = tk.Frame(frame, bg=T.PANEL_BG)
        r.pack(fill="x", pady=(L._px(app, 6) if (label.startswith("보호막") and i) else L._px(app, 1), L._px(app, 1)))
        tk.Label(r, text=label, bg=T.PANEL_BG, fg=T.MUTED, font=(FONT_TITLE, 10), width=8, anchor="nw").pack(side="left", anchor="n")
        txt = tk.Text(r, height=1, wrap="word", bg=T.PANEL_BG, fg=T.TEXT, bd=0, relief="flat", highlightthickness=0,
                      font=(FONT_BODY, 10), cursor="arrow", padx=0, pady=0, selectbackground=T.PANEL_BG, insertwidth=0, takefocus=0)
        for grp, color in L.GROUP_COLORS.items():
            txt.tag_configure("mech:" + grp, foreground=color)
        for k, color in L.CONF_COLORS.items():
            txt.tag_configure("conf:" + k, foreground=color)
        txt.pack(side="left", fill="x", expand=True)
        txt.bind("<Configure>", lambda e, t=txt: L._fit_text(t), add="+")
        L._set_text(txt, text)
        if "✕" in text:
            txt.tag_add("v52bad", "1.0", "end")
            txt.tag_configure("v52bad", foreground="#ff8a7a")
    try:
        mgr = panel.winfo_manager()
        if mgr == "pack":
            info = panel.pack_info()
            panel.pack_forget()
            frame.pack(**{k: v for k, v in info.items() if k in ("fill", "expand", "padx", "pady", "side", "anchor")})
            st["shield_pack"] = info
        elif mgr == "grid":
            info = panel.grid_info()
            panel.grid_remove()
            frame.grid(row=info.get("row"), column=info.get("column"), sticky=info.get("sticky") or "ew")
            st["shield_grid"] = info
        elif frame.winfo_manager() == "":
            frame.pack(fill="x")
    except Exception:
        pass


def _restore_shield(app) -> None:
    st = _state(app)
    frame, panel = st.get("shield_frame"), getattr(app, "b365_shield_panel", None)
    if frame is None or panel is None:
        return
    try:
        if frame.winfo_manager() == "pack":
            frame.pack_forget()
            info = st.get("shield_pack") or {}
            panel.pack(**{k: v for k, v in info.items() if k in ("fill", "expand", "padx", "pady", "side", "anchor")})
        elif frame.winfo_manager() == "grid":
            frame.grid_remove()
            panel.grid()
    except Exception:
        pass


# ---------------------------------------------------------------- 링크
_TIME_RE = re.compile(r"^\s*(\d+):(\d{2})(?:\.(\d))?\s*$")


def parse_clock(text) -> float | None:
    m = _TIME_RE.match(str(text or ""))
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2)) + (int(m.group(3)) / 10 if m.group(3) else 0.0)


def _card_time(label) -> float | None:
    w = label
    for _ in range(8):
        w = getattr(w, "master", None)
        if w is None:
            return None
        try:
            kids = w.winfo_children()
        except Exception:
            continue
        for k in kids[:1]:
            try:
                if k.winfo_class() == "Label":
                    sec = parse_clock(k.cget("text"))
                    if sec is not None:
                        return sec
            except Exception:
                pass
    return None


def wire_links(app) -> None:
    panel = getattr(app, "b3796_chain_panel", None)
    body = getattr(panel, "body", None)
    if body is None:
        return
    stack = [body]
    while stack:
        w = stack.pop()
        try:
            stack.extend(w.winfo_children())
        except Exception:
            continue
        try:
            if w.winfo_class() != "Label" or getattr(w, "_v52_link", False):
                continue
            text = str(w.cget("text"))
        except Exception:
            continue
        if text not in ("타임라인 →", "개인 분석 →"):
            continue
        w._v52_link = True
        try:
            import tkinter.font as tkfont
            base = tkfont.Font(font=w.cget("font"))
            under = base.copy()
            under.configure(underline=1)
            w._v52_fonts = (base, under)
            w.configure(cursor="hand2")
            w.bind("<Enter>", lambda e, l=w: l.configure(font=l._v52_fonts[1]), add="+")
            w.bind("<Leave>", lambda e, l=w: l.configure(font=l._v52_fonts[0]), add="+")
        except Exception:
            pass
        if text == "타임라인 →":
            w.bind("<Button-1>", lambda e, l=w: goto_timeline(app, _card_time(l)), add="+")
        else:
            w.bind("<Button-1>", lambda e: _open_personal(app), add="+")


def _open_personal(app) -> None:
    fn = getattr(app, "open_live_death_review_txt", None)
    if fn is not None:
        try:
            fn()
        except Exception:
            pass


def timeline_x(canvas, sec: float) -> float | None:
    """눈금 글자(0:00, 0:30 …) 두 개 이상으로 시각 → x 를 맞춘다."""
    got = timeline_axis(canvas)
    if got is None:
        return None
    (s0, x0), (s1, x1), _y = got
    return x0 + (sec - s0) * (x1 - x0) / (s1 - s0)


def timeline_axis(canvas):
    """((첫 눈금 초, x), (끝 눈금 초, x), 눈금 y) · 눈금이 두 개 안 되면 None."""
    pts, ys = [], []
    try:
        for item in canvas.find_all():
            if canvas.type(item) != "text":
                continue
            s = parse_clock(canvas.itemcget(item, "text"))
            if s is None:
                continue
            x, y = canvas.coords(item)[:2]
            pts.append((s, x))
            ys.append(y)
    except Exception:
        return None
    pts = sorted(set(pts))
    if len(pts) < 2 or pts[-1][0] == pts[0][0]:
        return None
    return pts[0], pts[-1], (min(ys) if ys else None)


def goto_timeline(app, sec) -> None:
    c = getattr(app, "b3641_try_timeline", None)
    if c is None:
        return
    # 1) 시간축이 보이게 바깥 스크롤을 내린다
    outer, inner = None, None
    w = c
    for _ in range(14):
        w = getattr(w, "master", None)
        if w is None:
            break
        try:
            if w.winfo_class() == "Canvas" and w is not c:
                outer = w
                break
        except Exception:
            pass
    try:
        if outer is not None:
            items = outer.find_all()
            inner = outer.nametowidget(outer.itemcget(items[0], "window")) if items else None
        if outer is not None and inner is not None and inner.winfo_height() > 0:
            y = c.winfo_rooty() - inner.winfo_rooty() - 60
            outer.yview_moveto(max(0.0, y / inner.winfo_height()))
    except Exception:
        pass
    # 2) 그 시각에 주황 세로선 4초
    if sec is None:
        return
    axis = timeline_axis(c)
    if axis is None:
        return
    (s0, x0), (s1, x1), tick_y = axis
    x = x0 + (float(sec) - s0) * (x1 - x0) / (s1 - s0)
    try:
        c.delete("v52_mark")
        bottom = (tick_y - 10) if tick_y else int(c.winfo_height() or 200) - 34
        c.create_line(x, 14, x, bottom, fill="#f0a04b", width=3, tags=("v52_mark",))
        c.create_text(x + 4, 16, anchor="nw", text=mmss(sec), fill="#f0a04b", tags=("v52_mark",))
        c.after(4000, lambda: c.delete("v52_mark"))
    except Exception:
        pass


def _wrap_chain(app) -> None:
    st = _state(app)
    if st.get("wrapped"):
        return
    orig = getattr(app, "_render_b3796_chain", None)
    if orig is None:
        return

    def wrapped(row, _orig=orig):
        out = _orig(row)
        try:
            wire_links(app)
        except Exception:
            pass
        return out
    try:
        app._render_b3796_chain = wrapped
        st["wrapped"] = True
    except Exception:
        pass


# ---------------------------------------------------------------- 똬리 모듈 + 공대 차단 규칙
def kick_rules(gold_root: Path | None = None) -> dict:
    root = gold_root or Path(__file__).resolve().parents[1]
    for p in (root / "kick_rules_3429.json", root / "memos" / "kick_rules_3429.json"):
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8-sig"))
            except Exception:
                return {}
    return {}


def build_module(mod, key: str) -> dict:
    """똬리 모듈이 공대 차단 규칙을 받으면 넘겨 준다 (V3.4.3 이상). 아니면 예전처럼."""
    rules = None
    if getattr(mod, "VERSION", "").startswith("COILED-WIPE-CAUSE") and hasattr(mod, "parse_kick_rules"):
        try:
            rules = mod.parse_kick_rules(kick_rules())
        except Exception:
            rules = None
    if rules:
        try:
            return mod.build_from_dir(key, use_raw=True, kick_rules=rules)
        except TypeError:
            pass
    return mod.build_from_dir(key, use_raw=True)
