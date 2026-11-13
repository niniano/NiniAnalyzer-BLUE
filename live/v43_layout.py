"""V4.3 트라이 복기 화면 · 2단 압축 레이아웃 + 표시 용어 (화면 전용, 판정·저장 데이터 불변).

nini_wcl_gui.App._render_b3641_try_priority 의 마지막 단계에서 apply(app, row) 를 부른다.
실패해도 기존 화면은 그대로 남는다 (호출부가 예외를 삼킨다).

    ┌ 한눈에 보기 (똬리·울라텍은 결론 5줄) ┬ 기믹 수행 체크 (작은 칸 2열) ┐
    ├ 내 캐릭터                           │                               │
    └ 공대 사고 순서 (펼쳤을 때)         ┴───────────────────────────────┘
    기믹 시간축 · 보호막 · 붕괴 체인은 아래 그대로, B31 원본 상세는 기본 접힘.
"""
from __future__ import annotations

import importlib.util
import threading
import tkinter as tk
from pathlib import Path

VERSION = "V5.2.2-LAYOUT"

# 화면에 보이는 말만 바꾼다. 저장된 판정 문구·엔진 문구는 그대로다.
WORDING = (
    ("폭탄 폭발 추가 피격", "폭탄 같이 맞음"),
    ("추가 피격", "같이 맞음"),
    # V5.4.5 (사용자 11/13): 「오피격」 → 「같이 맞음」 (화면 글자만 · 판정 자료는 그대로)
    ("오피격 없음", "같이 맞은 사람 없음"),
    ("오피격", "같이 맞음"),
    ("Early Wipe", "Reset"),
    ("초반 전멸", "Reset"),
    ("내 캐릭터 · 그 순간의 행동", "내 캐릭터"),
)

# V4.4: 기믹 이름 색 (Viserio 공략처럼 기믹 종류별 고정 색) · 판정 꼬리표 색 · 단계 진행 칩
GROUP_COLORS = {"red": "#ff8a7a", "yellow": "#e8c35a", "orange": "#f0a04b", "green": "#79c99e",
                "purple": "#c58af9", "blue": "#7fb8e6", "gray": "#b8c0ca"}
MECHANIC_GROUP = {
    # 똬리 (skill_tips 분류 기준)
    "똬리의 제단의 송곳니": "red", "맹독성 파열": "red", "영혼 말소": "red", "응결된 맹독": "yellow",
    "무시무시한 존재감": "yellow", "냉혹한 단두대": "orange", "단두대": "orange", "냉혹한 처형": "orange", "처형": "orange",
    "숨막히는 암흑": "yellow", "똬리의 제단의 오염": "yellow", "영혼 절단": "gray", "암영폭탄": "green", "현신": "green",
    "공포의 통곡": "blue", "통곡": "blue", "공포의 행진": "blue", "영원한 일몰": "red", "황혼의 장막": "yellow",
    "보호막": "yellow", "글레이브": "green", "구슬": "purple", "탱 브레스": "orange", "브레스": "orange", "앙갚음의 원한": "blue",
    # 울라텍
    "유령의 똬리": "red", "유해한 난타": "red", "역병의 핏줄": "red", "송곳니 끊기": "red", "불안정한 정화": "red",
    "사냥감 포위": "red", "곪은 폭발": "red", "괴저 증기": "yellow", "산성 배출": "yellow", "뱀의 물어뜯기": "orange",
    "부식의 파도": "green", "파도": "green", "고통스러운 절규": "blue", "절규": "blue", "파멸비늘": "blue",
    "막을 수 없는 분노": "red", "석회화한 시체": "purple", "석회화": "purple", "어미의 분노": "red",
    "심장": "purple", "큰 알": "blue", "유체": "blue", "사이페 똬리": "red",
}
try:  # V4.6: 잡은 네임드 6개 기믹 이름 색
    from live.boss_guides_v1 import mechanic_colors as _bg_colors  # type: ignore
    for _k, _v in _bg_colors().items():
        MECHANIC_GROUP.setdefault(_k, _v)
except Exception:
    pass
CONF_COLORS = {"[확정]": "#79c99e", "[추정]": "#f0a04b", "[미확정]": "#9aa3ad"}
STAGE_STYLE = {"done": ("#173825", "#79c99e", "✓"), "fail": ("#4a2222", "#ff756f", "✗"), "todo": ("#252b33", "#6f7883", "")}


def colorize(text: str) -> list[tuple[str, str | None]]:
    """글을 (조각, 꼬리표) 목록으로: 기믹 이름은 그 색, [확정]/[추정]/[미확정]은 판정 색."""
    import re
    keys = sorted(list(MECHANIC_GROUP) + list(CONF_COLORS), key=len, reverse=True)
    pat = re.compile("|".join(re.escape(k) for k in keys))
    out, pos = [], 0
    for m in pat.finditer(str(text)):
        if m.start() > pos:
            out.append((text[pos:m.start()], None))
        k = m.group(0)
        out.append((k, ("conf:" + k) if k in CONF_COLORS else ("mech:" + MECHANIC_GROUP[k])))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], None))
    return out


def _fmt(sec) -> str:
    if sec is None:
        return ""
    sec = max(0, int(float(sec)))
    return f"{sec // 60}:{sec % 60:02d}"


CARD_COLS = 2
NA_PREFIXES = ("적용 안 함", "P2 미진입", "이 난이도엔 없음")
FIVE_KEYS = ("어디까지", "첫 실수", "전멸 원인", "딜·힐", "다음 풀")
_BOSS_MODULES = {3429: "coiled_wipe_cause_v1", 3492: "ulatek_conclusion_v1",
                 # V4.6: 잡은 네임드 6개 (Viserio 공략 기준 공통 모듈)
                 3420: "boss_guides_v1", 3421: "boss_guides_v1", 3445: "boss_guides_v1",
                 3455: "boss_guides_v1", 3470: "boss_guides_v1", 3497: "boss_guides_v1",
                 # V4.8: 키식스 (키식스 해방) · boss_guides V1.5
                 3513: "boss_guides_v1"}


def _px(app, v):
    try:
        from live.ui_theme import px
        return px(v)
    except Exception:
        return v


def _theme():
    from live import ui_theme
    return ui_theme


# ---------------------------------------------------------------- wording
def reword(text: str) -> str:
    out = str(text)
    for old, new in WORDING:
        out = out.replace(old, new)
    try:  # V5.2: 「범위: …」 · 「n=3」 · 「raidPlan judge …」 같은 개발용 글자는 화면에서 뺀다
        from live.v52_screen import sanitize as _v52_sanitize
        out = _v52_sanitize(out)
    except Exception:
        pass
    return out


def _reword_tree(widget) -> None:
    """Label/Button 글자와 Canvas 글자를 화면에서만 바꾼다."""
    stack = [widget]
    while stack:
        w = stack.pop()
        try:
            stack.extend(w.winfo_children())
        except Exception:
            continue
        try:
            cls = w.winfo_class()
        except Exception:
            continue
        if cls in ("Label", "Button", "TLabel", "TButton"):
            try:
                var = str(w.cget("textvariable") or "")
            except Exception:
                var = ""
            try:
                if var:
                    cur = w.getvar(var)
                    new = reword(cur)
                    if new != cur:
                        w.setvar(var, new)
                else:
                    cur = str(w.cget("text"))
                    new = reword(cur)
                    if new != cur:
                        w.configure(text=new)
            except Exception:
                pass
        elif cls == "Canvas":
            try:
                for item in w.find_all():
                    if w.type(item) == "text":
                        cur = w.itemcget(item, "text")
                        new = reword(cur)
                        if new != cur:
                            w.itemconfigure(item, text=new)
            except Exception:
                pass


# ---------------------------------------------------------------- layout
def _setup(app):
    st = getattr(app, "_v43", None)
    if st is not None:
        return st
    T = _theme()
    wrap = app.b3641_try_wrap
    nav = app.b3642_prev_try_btn.master
    row = tk.Frame(wrap, bg=T.PAGE_BG)
    row.pack(fill="x", after=nav)
    left = tk.Frame(row, bg=T.PAGE_BG)
    right = tk.Frame(row, bg=T.PAGE_BG)
    row.columnconfigure(0, weight=3, uniform="v43")
    row.columnconfigure(1, weight=2, uniform="v43")
    st = {"row": row, "left": left, "right": right, "b31_open": False, "five": {}, "pending": set(),
          "modules": {}, "five_box": None}
    app._v43 = st
    # 접었다 펼치는 버튼은 원래 동작 뒤에 다시 2단으로 정리한다.
    for btn_name, fn_name in (("v40_toggle_btn", "_v40_toggle_details"), ("b3739_evidence_btn", "_toggle_b3739_evidence")):
        btn, fn = getattr(app, btn_name, None), getattr(app, fn_name, None)
        if btn is not None and fn is not None:
            def _cmd(fn=fn, btn_name=btn_name):
                # V5.4.5: 결론 칸(한눈에 보기)이 보이는 동안 위 버튼은 「자세히」 하나로 쓴다
                # (예전에는 「판정 상세 펼치기」 와 「자세히 펼치기」 가 따로 있었다).
                if btn_name == "v40_toggle_btn" and st.get("five_active"):
                    _toggle_details(app)
                    return
                fn()
                try:
                    relayout(app)
                except Exception:
                    pass
            try:
                btn.configure(command=_cmd)
            except Exception:
                pass
    # V5.4.5: 「B31 원본 · 개인 사망 · 쿨기 비교 상세」 펼치기 버튼은 없앤다 (사용자 11/13 · 볼 일 없음).
    # 같은 글은 위 「개인 사망 열기」 · 「전멸 복기 열기」 버튼으로 파일을 열면 그대로 있다.
    st["b31_btn"] = None
    _compact_cards(app)
    return st


def _compact_cards(app):
    """기믹 카드: 제목 8pt + 값 10pt 두 줄, 여백 축소."""
    cards = getattr(app, "b3641_try_cards", {}) or {}
    try:
        from nini_wcl_gui import FONT_BODY  # type: ignore
    except Exception:
        FONT_BODY = "TkDefaultFont"
    for c in cards.values():
        try:
            c["title"].configure(font=(FONT_BODY, 8))
            c["title"].pack_configure(padx=_px(app, 4), pady=(_px(app, 3), 0))
            c["value"].configure(font=(FONT_BODY, 10, "bold"))
            c["value"].pack_configure(padx=_px(app, 4), pady=(0, _px(app, 3)))
            c["frame"].inner.pack_configure(padx=_px(app, 6), pady=_px(app, 4))
        except Exception:
            pass


def _regrid_cards(app):
    cards = getattr(app, "b3641_try_cards", {}) or {}
    body = None
    shown = []
    for key, c in cards.items():
        f = c.get("frame")
        if f is None:
            continue
        body = f.master
        try:
            if f.winfo_manager() == "grid":
                shown.append((int(f.grid_info().get("row", 0)), int(f.grid_info().get("column", 0)), f))
        except Exception:
            continue
    if body is None:
        return
    shown.sort(key=lambda x: (x[0], x[1]))
    # 이번 트라이와 상관없는 칸(적용 안 함 · P2 미진입)은 숨긴다. 한 칸도 안 남으면 그대로 둔다.
    keep = []
    for item in shown:
        f = item[2]
        val = ""
        for c in cards.values():
            if c.get("frame") is f:
                try:
                    val = str(c["valueVar"].get())
                except Exception:
                    val = ""
        if val.startswith(NA_PREFIXES):
            f.grid_remove()
        else:
            keep.append(item)
    if keep:
        shown = keep
    for i, (_r, _c, f) in enumerate(shown):
        f.grid_configure(row=i // CARD_COLS, column=i % CARD_COLS, padx=_px(app, 3), pady=_px(app, 3))
    for col in range(6):
        body.columnconfigure(col, weight=1 if col < CARD_COLS else 0, uniform="v43cards" if col < CARD_COLS else "")
    for c in cards.values():
        try:
            c["value"].configure(wraplength=_px(app, 200))
            c["title"].configure(wraplength=_px(app, 200))
        except Exception:
            pass


def _managed(w) -> bool:
    try:
        return bool(w is not None and w.winfo_manager())
    except Exception:
        return False


def _has_text(widget, needle) -> bool:
    stack = [widget]
    while stack:
        w = stack.pop()
        try:
            stack.extend(w.winfo_children())
            if w.winfo_class() == "Label":
                var = str(w.cget("textvariable") or "")
                txt = w.getvar(var) if var else str(w.cget("text"))
                if needle in str(txt):
                    return True
        except Exception:
            continue
    return False


def relayout(app):
    st = _setup(app)
    left, right, row = st["left"], st["right"], st["row"]
    concl = getattr(app, "_v35_concl_box", None)
    personal = getattr(app, "v35_personal_box", None)
    chrono = getattr(app, "v37_raid_time_box", None)
    cards = getattr(app, "b3739_cards_box", None)
    shield = getattr(app, "b365_shield_box", None)
    pad = (0, _px(app, 8))
    # P2에 못 간 트라이의 「P2 보호막」 칸은 내용이 없어서 숨긴다.
    sp = getattr(app, "b365_shield_panel", None)
    try:
        shield_text = sp.get("1.0", "end") if sp is not None else ""
    except Exception:
        shield_text = ""
    if _managed(shield) and ("P2 미진입" in shield_text or _has_text(shield, "P2 미진입")):
        shield.pack_forget()
    # 보호막 칸은 왼쪽 칸 맨 아래로 (오른쪽 기믹 체크와 높이를 맞춘다)
    order = [w for w in (concl, personal, chrono, shield) if _managed(w)]
    if [w for w in left.pack_slaves()] != order:
        for w in order:
            w.pack_forget()
        for w in order:
            w.pack(in_=left, fill="x", pady=pad)
    # 두 칸 높이 맞춤: 각 칸의 마지막 상자가 남는 높이를 채운다.
    for w in order:
        try:
            w.pack_configure(fill="x", expand=False)
        except Exception:
            pass
    if order:
        try:
            order[-1].pack_configure(fill="both", expand=True)
        except Exception:
            pass
    for w in order:
        try:
            w.tkraise()   # 원래 부모(wrap)의 자식이라 새 칸(left)보다 아래에 깔리지 않게
        except Exception:
            pass
    has_left = bool(order)
    has_right = _managed(cards)
    if has_right:
        cards.pack(in_=right, fill="both", expand=True, pady=pad)
        try:
            cards.tkraise()
        except Exception:
            pass
        _regrid_cards(app)
    left.grid_forget()
    right.grid_forget()
    if has_left and has_right:
        left.grid(row=0, column=0, sticky="nsew", padx=(0, _px(app, 8)))
        right.grid(row=0, column=1, sticky="nsew")
        row.columnconfigure(1, weight=2, uniform="v43")
    elif has_left or has_right:
        (left if has_left else right).grid(row=0, column=0, columnspan=2, sticky="nsew")
    # 왼쪽 글 폭: 기존 wraplength(860~900) 대신 칸 폭에 맞춘다.
    try:
        width = max(300, int(left.winfo_width() or 0) - _px(app, 60))
        for lbl in list(getattr(app, "v40_overview_labels", [])) + list(getattr(app, "v35_personal_labels", [])) \
                + list(getattr(app, "v37_raid_time_labels", [])):
            lbl.configure(wraplength=width if left.winfo_width() > 50 else _px(app, 520))
        nl = getattr(app, "b3792_next_label", None)
        if nl is not None:
            nl.configure(wraplength=width if left.winfo_width() > 50 else _px(app, 520))
    except Exception:
        pass
    # 기믹 체크 설명은 좁은 칸에 맞게 짧게
    try:
        cards.subtitle_var.set("✓ 충족 · ✗ 실패 · ! 주의 · ? 판단 못 함")
    except Exception:
        pass
    # 「다음 트라이」 칸은 위 결론의 「다음 풀」과 같은 말이라 숨긴다 (연결 칸은 그대로).
    nxt = getattr(app, "b3796_next_panel", None)
    if _managed(nxt):
        nxt.pack_forget()
    _apply_b31(app)


def _apply_b31(app):
    st = app._v43
    box = getattr(app, "live_facts_box", None)
    btn = st.get("b31_btn")
    chain = getattr(app, "b3796_chain_wrap", None)
    if box is None:
        return
    if btn is None:   # V5.4.5: 버튼 없음 → 상세 칸도 늘 접힘
        try:
            box.pack_forget()
        except Exception:
            pass
        return
    anchor = chain if _managed(chain) else app.b3641_try_wrap
    btn.pack(fill="x", padx=_px(app, 2), pady=(_px(app, 2), _px(app, 8)), after=anchor)
    if st["b31_open"]:
        box.pack(fill="both", expand=True, padx=_px(app, 2), pady=(_px(app, 4), _px(app, 8)), after=btn)
        btn.configure(text="B31 원본 · 개인 사망 · 쿨기 비교 상세 접기 ▾")
    else:
        try:
            box.pack_forget()
        except Exception:
            pass
        btn.configure(text="B31 원본 · 개인 사망 · 쿨기 비교 상세 펼치기 ▸")


def _toggle_b31(app):
    st = app._v43
    st["b31_open"] = not st["b31_open"]
    _apply_b31(app)


# ---------------------------------------------------------------- 결론 5줄 (똬리·울라텍)
def _module(app, name):
    st = app._v43
    if name in st["modules"]:
        return st["modules"][name]
    mod = None
    try:
        path = Path(__file__).resolve().parent / f"{name}.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location(f"v43_{name}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        else:  # BLUE RC5: PyInstaller build has no .py next to this module
            from importlib import import_module as _import_module
            mod = _import_module(f"live.{name}")
    except Exception:
        mod = None
    st["modules"][name] = mod
    return mod


def _five_box(app):
    st = app._v43
    if st["five_box"] is not None:
        return st["five_box"]
    T = _theme()
    try:
        from nini_wcl_gui import FONT_BODY, FONT_TITLE  # type: ignore
    except Exception:
        FONT_BODY = FONT_TITLE = "TkDefaultFont"
    parent = app.v40_overview_box
    box = tk.Frame(parent, bg=T.PANEL_BG)
    stages = tk.Frame(box, bg=T.PANEL_BG)
    stages.pack(fill="x", pady=(0, _px(app, 6)))
    # V5.4.4: 한눈에 보기 (무너진 순서 칩 · 숫자 카드 · 다음 풀 한 줄) → 5줄 문장과 참고는 「자세히」 안
    glance = tk.Frame(box, bg=T.PANEL_BG)
    # V5.4.5: 펼치기 버튼은 칸 제목 오른쪽 버튼 하나(예전 「판정 상세 펼치기」 자리)로 통일.
    # 그 버튼이 없는 화면(시험용)만 칸 안에 글자 버튼을 둔다.
    toggle = getattr(app, "v40_toggle_btn", None)
    st["five_toggle_head"] = toggle is not None
    if toggle is None:
        toggle = tk.Label(box, text="", bg=T.PANEL_BG, fg=T.BLUE, font=(FONT_BODY, 9), cursor="hand2", anchor="w")
        toggle.bind("<Button-1>", lambda e: _toggle_details(app))
    details = tk.Frame(box, bg=T.PANEL_BG)
    details.pack(fill="x")
    st["five_glance"] = (glance, toggle, details)
    rows = {}
    for key in FIVE_KEYS:
        r = tk.Frame(details, bg=T.PANEL_BG)
        r.pack(fill="x", pady=(_px(app, 1), _px(app, 2)))
        _lab = tk.Label(r, text=key, bg=T.PANEL_BG, fg=T.MUTED, font=(FONT_TITLE, 10), width=8, anchor="nw")
        _lab.pack(side="left", anchor="n")
        st.setdefault("five_labels", {})[key] = _lab   # V5.2.8: 킬 판은 칸 이름을 바꿔 보인다
        bold = key in ("다음 풀",)
        txt = tk.Text(r, height=1, wrap="word", bg=T.PANEL_BG, fg=T.TEXT, bd=0, relief="flat", highlightthickness=0,
                      font=(FONT_BODY, 11, "bold") if bold else (FONT_BODY, 11), cursor="arrow", padx=0, pady=0,
                      selectbackground=T.PANEL_BG, insertwidth=0, takefocus=0)
        for grp, color in GROUP_COLORS.items():
            txt.tag_configure("mech:" + grp, foreground=color, font=(FONT_BODY, 11, "bold"))
        for k, color in CONF_COLORS.items():
            txt.tag_configure("conf:" + k, foreground=color, font=(FONT_BODY, 9, "bold"))
        txt.pack(side="left", fill="x", expand=True)
        txt.configure(state="disabled")
        txt.bind("<Configure>", lambda e, t=txt: _fit_text(t), add="+")
        rows[key] = txt
    # V5.0 「참고」 줄: 모듈이 notes 를 줄 때만 보인다 (키식스 무효화·탱 교대·공포 중 면역기 · 울라텍 늦은 절규)
    note_row = tk.Frame(details, bg=T.PANEL_BG)
    tk.Label(note_row, text="참고", bg=T.PANEL_BG, fg=T.MUTED, font=(FONT_TITLE, 10), width=8, anchor="nw").pack(side="left", anchor="n")
    note_txt = tk.Text(note_row, height=1, wrap="word", bg=T.PANEL_BG, fg=T.ORANGE, bd=0, relief="flat", highlightthickness=0,
                       font=(FONT_BODY, 10), cursor="arrow", padx=0, pady=0, selectbackground=T.PANEL_BG, insertwidth=0, takefocus=0)
    note_txt.pack(side="left", fill="x", expand=True)
    note_txt.configure(state="disabled")
    note_txt.bind("<Configure>", lambda e, t=note_txt: _fit_text(t), add="+")
    st["five_note"] = (note_row, note_txt)
    st["five_box"] = (box, rows, stages)
    return st["five_box"]


_LINESPACE: dict = {}


def _linespace(t) -> int:
    key = str(t.cget("font"))
    if key not in _LINESPACE:
        from tkinter import font as tkfont
        _LINESPACE[key] = max(1, int(tkfont.Font(root=t, font=t.cget("font")).metrics("linespace")))
    return _LINESPACE[key]


def _fit_text(t):
    # V5.2.2: 그 칸 하나만 줄 수를 다시 센다 (-update). 예전처럼 창 전체를
    # update_idletasks 로 다시 그리지 않으므로 화면이 위에서부터 차례로 그려지지 않는다.
    # V5.4.3: 줄 수가 아니라 글 전체 높이(픽셀)로 맞춘다. 이름(11pt)·굵은 기믹 이름이 섞인 줄은
    # 기본 글꼴 한 줄보다 높아서, 줄 수로만 맞추면 마지막 줄이 잘려 스크롤해야 보였다 (Windows 배율에서 특히).
    try:
        if int(t.winfo_width()) <= 1:
            return  # 아직 폭이 없으면 <Configure> 때 다시 맞춘다
        n = int(t.tk.call(t._w, "count", "-update", "-displaylines", "1.0", "end"))
        try:
            ypix = int(t.tk.call(t._w, "count", "-ypixels", "1.0", "end"))
            n = max(n, -(-ypix // _linespace(t)))
        except Exception:
            pass
        if n and int(t.cget("height")) != n:
            t.configure(height=n)
        t.yview_moveto(0)
    except Exception:
        pass


def _set_text(t, text):
    t.configure(state="normal")
    t.delete("1.0", "end")
    for seg, tag in colorize(reword(text or "-")):
        t.insert("end", seg, tag) if tag else t.insert("end", seg)
    t.configure(state="disabled")
    _fit_text(t)


def _v45():
    try:
        from live import v45_timeline as m  # type: ignore
        return m
    except Exception:
        return None


def _name_colors(row) -> dict:
    """V4.5: 그 트라이 로스터 이름 → 직업색 (없으면 빈 dict, 글은 그대로)."""
    m = _v45()
    try:
        return m.name_colors(row) if m is not None else {}
    except Exception:
        return {}


def _paint_names(txt, names):
    m = _v45()
    if m is None or not names:
        return
    try:
        from nini_wcl_gui import FONT_BODY  # type: ignore
    except Exception:
        FONT_BODY = "TkDefaultFont"
    try:
        # 이름은 보통 굵기 → 굵은 기믹 이름·다음 풀 줄과 구분된다.
        m.colorize_text_widget(txt, names, font=(FONT_BODY, 11))
    except Exception:
        pass
    _fit_text(txt)   # V5.4.3: 이름 글꼴이 들어가면 줄 높이·줄바꿈이 바뀐다 → 다시 맞춤


def _set_stages(app, frame, stages):
    for w in frame.winfo_children():
        w.destroy()
    if not stages:
        frame.pack_forget()
        return
    if not frame.winfo_manager():
        sib = [w for w in frame.master.winfo_children() if w is not frame and w.winfo_manager()]
        frame.pack(fill="x", pady=(0, _px(app, 6)), before=sib[0]) if sib else frame.pack(fill="x", pady=(0, _px(app, 6)))
    try:
        from nini_wcl_gui import FONT_BODY  # type: ignore
    except Exception:
        FONT_BODY = "TkDefaultFont"
    for i, stg in enumerate(stages, 1):
        bg, fg, mark = STAGE_STYLE.get(stg.get("status"), STAGE_STYLE["todo"])
        span = ""
        # V4.6: 칩이 5개 이상이면 시각은 전멸한 칩에만 (한 줄에 다 들어가게).
        if stg.get("status") != "todo" and stg.get("start") is not None and (len(stages) <= 4 or stg.get("status") == "fail"):
            span = f" {_fmt(stg.get('start'))}–{_fmt(stg.get('end'))}"
        name = str(stg.get("name") or "")
        # 이름에 이미 번호가 있으면(1주기·2루프·3시음) 앞 번호는 생략
        num = "" if any(ch.isdigit() for ch in name) else f"{i}  "
        label = f"{num}{name}{span}" + (f"  {mark}" if mark else "")
        tk.Label(frame, text=label, bg=bg, fg=fg, font=(FONT_BODY, 9, "bold"), padx=_px(app, 8), pady=_px(app, 2)).pack(side="left", padx=(0, _px(app, 6)))


def _row_key(row):
    return str(row.get("storageDir") or row.get("metaJson") or "")


def _show_five(app, row):
    st = app._v43
    v40 = getattr(app, "v40_overview_box", None)
    try:
        enc = int(row.get("encounterID") or 0)
    except (TypeError, ValueError):
        enc = 0
    name = _BOSS_MODULES.get(enc)
    key = _row_key(row)
    box_rows = None
    _egg_card(app, [])   # V5.4.2: 다른 트라이로 가면 알 운반 카드는 먼저 숨김 (울라텍 결론이 오면 다시 채움)
    kill_on = False
    if name and key and not _managed(v40) and st["five"].get(key) != {}:
        kill_on = _kill_glance(app, row)   # V5.2.8: 킬 판도 결론 칸 (예전 칸은 「판정 상세 펼치기」)
    if name and key and _managed(v40):
        result = st["five"].get(key)
        if result is None and key not in st["pending"]:
            mod = _module(app, name)
            if mod is not None and Path(key).is_dir():
                st["pending"].add(key)

                st["fg"] = st.get("fg", 0) + 1  # 미리 계산은 이게 끝날 때까지 쉰다

                def work(mod=mod, key=key, row=row):
                    try:
                        _compute(app, mod, key, row)
                    finally:
                        st["fg"] = max(0, st.get("fg", 1) - 1)
                    try:
                        app.after(0, lambda: _finish(app, key))
                    except Exception:
                        pass
                threading.Thread(target=work, daemon=True).start()
        if result is None and key in st["pending"]:
            # V5.2.2: 계산 중에는 결론 칸을 그대로 두고 「불러오는 중」만 보여 준다
            # (예전에는 다른 칸이 먼저 보였다가 결론 칸으로 통째로 바뀌었다).
            box_rows = _five_box(app)
            box, rows, stage_frame = box_rows
            _set_stages(app, stage_frame, [])
            _set_labels(app, {})
            _show_glance(app, None)
            for k, txt in rows.items():
                _set_text(txt, "불러오는 중…" if k == FIVE_KEYS[0] else "")
            note = st.get("five_note")
            if note is not None:
                note[0].pack_forget()
        if result:
            box_rows = _five_box(app)
            box, rows, stage_frame = box_rows
            _set_stages(app, stage_frame, result.get("_stages"))
            _set_labels(app, result.get("_labels") or {})
            try:   # V5.2.9: 「엔진 기믹 판정 없음」 대신 결론 모듈 체크 칸
                from live.v529_board import fill_check_cards
                if fill_check_cards(app, result.get("_checks") or []):
                    _compact_cards(app)
                    _regrid_cards(app)
            except Exception:
                pass
            names = _name_colors(row)
            _egg_card(app, result.get("_eggCarry") or [], names)
            for k, txt in rows.items():
                _set_text(txt, result.get(k) or "-")
                _paint_names(txt, names)
            _show_glance(app, result.get("_glance"))
            note = st.get("five_note")
            if note is not None:
                notes = result.get("_notes") or []
                if notes:
                    _set_text(note[1], "\n".join(notes))
                    _paint_names(note[1], names)
                    note[0].pack(fill="x", pady=(_px(app, 1), _px(app, 2)))
                else:
                    note[0].pack_forget()
    legacy_rows = [w for w in (v40.winfo_children() if v40 is not None else []) if w is not (st["five_box"] or (None,))[0]]
    head = legacy_rows[0] if legacy_rows else None
    st["five_active"] = bool(box_rows)
    if box_rows:
        _close_legacy_details(app)
        box = box_rows[0]
        for w in legacy_rows[1:]:
            if _managed(w):
                w.pack_forget()
        box.pack(fill="x", after=head) if head is not None else box.pack(fill="x")
        st["five_hidden_rows"] = legacy_rows[1:]
    else:
        if st["five_box"] is not None:
            st["five_box"][0].pack_forget()
        _restore_legacy_toggle(app)
        for w in st.get("five_hidden_rows", []):
            if not _managed(w):
                w.pack(fill="x", pady=(_px(app, 4), _px(app, 6)))
        st["five_hidden_rows"] = []
        if kill_on:   # V5.2.8: 킬 결론을 못 만들었으면 예전 화면으로
            try:
                v40.pack_forget()
                app._v40_restore_legacy()
                app._v40_visible_key = None
            except Exception:
                pass


GLANCE_TONES = {   # 칩 · 카드 색 (배경, 테두리, 글)
    "red": ("#3a1d1d", "#7a3434", "#f3b0aa"), "orange": ("#3a2a16", "#d9963d", "#f0c890"),
    "end": ("#262b33", "#3a414b", "#e8eaed"), "green": ("#1f2a22", "#2f5a3c", "#9fe0b8"),
}
VALUE_TONES = {"red": "RED", "orange": "ORANGE", "green": "GREEN", "muted": "MUTED"}
ARROW_COLOR = "#8b939e"


def _close_legacy_details(app) -> None:
    """V5.4.5: 결론 칸이 보이면 예전 「판정 상세」(옛 3칸 · 시간 칸)는 닫아 둔다."""
    try:
        if getattr(app, "_v40_details_open", False):
            app._v40_details_open = False
            app._v40_apply_detail_visibility()
            if "five_glance" in app._v43:
                _apply_details(app)   # 위 함수가 버튼 글을 「판정 상세」로 되돌리므로 다시
    except Exception:
        pass


def _restore_legacy_toggle(app) -> None:
    """결론 칸이 없는 화면(판정 모듈 없는 보스 등)은 예전 「판정 상세」 버튼 그대로."""
    btn = getattr(app, "v40_toggle_btn", None)
    if btn is None:
        return
    try:
        btn.configure(text="판정 상세 접기 ▴" if getattr(app, "_v40_details_open", False) else "판정 상세 펼치기 ▾")
        if not btn.winfo_manager():
            btn.pack(side="right")
    except Exception:
        pass


def _toggle_details(app) -> None:
    st = app._v43
    st["five_open"] = not st.get("five_open")
    _apply_details(app)


def _apply_details(app) -> None:
    st = app._v43
    glance, toggle, details = st["five_glance"]
    head = st.get("five_toggle_head")
    has = bool(glance.winfo_children()) and glance.winfo_manager() != ""
    if not has:
        # 한눈에 보기가 없으면(판정 제외 · 계산 중) 5줄이 늘 보이므로 펼칠 것이 없다 → 버튼 숨김
        toggle.pack_forget()
        if not details.winfo_manager():
            details.pack(fill="x")
        return
    opened = bool(st.get("five_open"))
    toggle.configure(text="자세히 접기 ▴" if opened else "자세히 펼치기 ▾")
    if not toggle.winfo_manager():
        toggle.pack(side="right") if head else toggle.pack(fill="x", pady=(_px(app, 2), _px(app, 4)), after=glance)
    if opened and not details.winfo_manager():
        details.pack(fill="x", after=glance, pady=(_px(app, 6), 0))
    elif not opened and details.winfo_manager():
        details.pack_forget()


def _round(parent, fill, outline, padx=8, pady=5):
    """V5.4.5: 칩 · 카드는 둥근 테두리(옆 「기믹 수행 체크」 카드와 같은 radius 8)."""
    T = _theme()
    try:
        box = T.RoundBox(parent, fill=fill, outline=outline, radius=8, padx=padx, pady=pady)
        return box, box.inner
    except Exception:
        f = tk.Frame(parent, bg=fill, highlightthickness=1, highlightbackground=outline)
        return f, f


def _arrow(parent, bg):
    """V5.4.5: 굵은 화살표 (칩 높이 가운데)."""
    w, h = _px(None, 26), _px(None, 18)
    c = tk.Canvas(parent, width=w, height=h, bg=bg, highlightthickness=0, bd=0)
    m = h // 2
    c.create_line(2, m, w - 3, m, fill=ARROW_COLOR, width=max(3, _px(None, 3)), arrow="last",
                  arrowshape=(_px(None, 9), _px(None, 11), _px(None, 5)), capstyle="round")
    return c


def _chain_row(app, parent, chain, FONT_BODY):
    """V5.4.5 무너진 순서: 한 줄 · 칩 폭 같게 · 높이 같게 · 굵은 화살표는 칩 높이 가운데.

    끝 칩(전멸)은 글이 짧으니 제 폭만. 나머지 칩은 남은 폭을 똑같이 나누고 글은 칩 안에서 줄바꿈.
    """
    T = _theme()
    row = tk.Frame(parent, bg=T.PANEL_BG)
    row.pack(fill="x", pady=(_px(app, 3), _px(app, 10)))
    labels, arrows = [], 0
    end_box = None
    n = len(chain)
    for i, c in enumerate(chain):
        is_end = (i == n - 1 and c.get("title") in ("전멸", "킬"))
        if i:
            _arrow(row, T.PANEL_BG).grid(row=0, column=2 * i - 1, padx=_px(app, 3))
            arrows += 1
        bg, bd, fg = GLANCE_TONES.get(c.get("tone") or "end", GLANCE_TONES["end"])
        box, inner = _round(row, bg, bd, padx=9, pady=6)
        box.grid(row=0, column=2 * i, sticky="nsew")
        if is_end:
            end_box = box
        else:
            row.columnconfigure(2 * i, weight=1, uniform="v545chain")
        top = " · ".join(x for x in (c.get("t"), c.get("title") if c.get("title") not in ("전멸", "킬") else "") if x)
        if c.get("conf"):
            top += f"  [{c['conf']}]"
        if top:
            lt = tk.Label(inner, text=top, bg=bg, fg=T.MUTED, font=(FONT_BODY, 8), anchor="w", justify="left",
                          wraplength=_px(app, 100))
            lt.pack(fill="x")
            labels.append(lt)
        lb = tk.Label(inner, text=reword(c.get("text") or ""), bg=bg, fg=fg, font=(FONT_BODY, 10, "bold"), anchor="w",
                      justify="left", wraplength=_px(app, 100))
        lb.pack(fill="x", pady=(_px(app, 1) if top else 0, 0))
        if not is_end:
            labels.append(lb)
    main = n - (1 if end_box is not None else 0)
    state = {"w": 0}

    def wrap(_e=None):
        width = int(row.winfo_width())
        if width <= 1 or width == state["w"]:
            return
        state["w"] = width
        fixed = arrows * _px(app, 32)
        if end_box is not None:
            fixed += int(end_box.winfo_reqwidth())
        each = max(_px(app, 70), int((width - fixed) / max(1, main)) - _px(app, 22))
        for lab in labels:
            lab.configure(wraplength=each)
    row.bind("<Configure>", wrap, add="+")
    return row


def _show_glance(app, g) -> None:
    """V5.4.4 「한눈에 보기」: 무너진 순서 칩 → 숫자 카드 → 다음 풀 한 줄. g 가 없으면(판정 제외 · 계산 중) 예전 5줄.

    V5.4.5: 둥근 테두리 · 무너진 순서는 한 줄(칩 폭 · 높이 같게) · 굵은 화살표 가운데.
    """
    st = app._v43
    if "five_glance" not in st:
        return
    glance, toggle, details = st["five_glance"]
    for w in glance.winfo_children():
        w.destroy()
    if not g or not (g.get("chain") or g.get("stats")):
        glance.pack_forget()
        _apply_details(app)
        return
    T = _theme()
    try:
        from nini_wcl_gui import FONT_BODY  # type: ignore
    except Exception:
        FONT_BODY = "TkDefaultFont"
    if not glance.winfo_manager():
        if details.winfo_manager():
            glance.pack(fill="x", before=details)
        else:
            glance.pack(fill="x")
    chain = g.get("chain") or []
    if chain:
        tk.Label(glance, text="무너진 순서 (뿌리 → 전멸)" if len(chain) > 1 else "결과", bg=T.PANEL_BG, fg=T.MUTED,
                 font=(FONT_BODY, 8), anchor="w").pack(fill="x")
        _chain_row(app, glance, chain, FONT_BODY)
    stats = g.get("stats") or []
    if stats:
        grid = tk.Frame(glance, bg=T.PANEL_BG)
        grid.pack(fill="x", pady=(0, _px(app, 8)))
        subs = []
        for i, c in enumerate(stats):
            card, inner = _round(grid, T.CARD_BG, T.CARD_BORDER, padx=8, pady=6)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else _px(app, 5), 0))
            grid.columnconfigure(i, weight=1, uniform="v544stats")
            tk.Label(inner, text=c.get("k") or "", bg=T.CARD_BG, fg=T.MUTED, font=(FONT_BODY, 8), anchor="w").pack(fill="x")
            fg = getattr(T, VALUE_TONES.get(c.get("tone") or "", "TEXT"), T.TEXT)
            tk.Label(inner, text=reword(c.get("v") or "-"), bg=T.CARD_BG, fg=fg, font=(FONT_BODY, 13, "bold"),
                     anchor="w").pack(fill="x")
            sub = tk.Label(inner, text=reword(c.get("s") or ""), bg=T.CARD_BG, fg=T.MUTED, font=(FONT_BODY, 8),
                           anchor="w", justify="left", wraplength=_px(app, 90))
            sub.pack(fill="x")
            subs.append(sub)

        def wrap(_e=None, subs=subs, grid=grid, n=len(stats)):
            w = max(60, int(grid.winfo_width() / max(1, n)) - _px(app, 24))
            for lab in subs:
                if int(lab.cget("wraplength")) != w:
                    lab.configure(wraplength=w)
        grid.bind("<Configure>", wrap, add="+")
    if g.get("next"):
        bg, bd, fg = GLANCE_TONES["green"]
        nx, inner = _round(glance, bg, bd, padx=10, pady=6)
        nx.pack(fill="x", pady=(0, _px(app, 2)))
        tk.Label(inner, text=g.get("nextLabel") or "다음 풀", bg=bg, fg=T.GREEN, font=(FONT_BODY, 8), anchor="w").pack(fill="x")
        lab = tk.Label(inner, text=reword(g["next"]), bg=bg, fg=T.TEXT, font=(FONT_BODY, 11, "bold"), anchor="w", justify="left")
        lab.pack(fill="x")
        inner.bind("<Configure>", lambda e, l=lab: l.configure(wraplength=max(120, e.width - _px(app, 4))), add="+")
    _apply_details(app)


EGG_TAG_COLORS = {"bad": "RED", "late": "ORANGE", "wipe": "STATE_INACTIVE"}


def _egg_card(app, lines, names=None) -> None:
    """V5.4.2 울라텍 「알 운반」 카드: 차수마다 누가 알을 들었나 (문제 = 빨강 · 늦음 = 주황 · 전멸 중 = 회색).

    오른쪽 「기믹 수행 체크」 카드 칸 아래에 두 칸 너비로 붙인다. 줄이 없으면(다른 보스 · 알 단계 전) 숨긴다.
    """
    st = app._v43
    card = st.get("egg_card")
    if not lines:
        if card is not None:
            try:
                card[0].grid_remove()
            except Exception:
                pass
        return
    T = _theme()
    try:
        from nini_wcl_gui import FONT_BODY  # type: ignore
    except Exception:
        FONT_BODY = "TkDefaultFont"
    if card is None:
        body = None
        for c in (getattr(app, "b3641_try_cards", {}) or {}).values():
            if c.get("frame") is not None:
                body = c["frame"].master
                break
        if body is None:
            box = getattr(app, "b3739_cards_box", None)
            body = getattr(box, "body", None)
        if body is None:
            return
        try:
            frame = T.RoundBox(body, fill=T.CARD_BG, outline=T.CARD_BORDER, radius=8, padx=10, pady=8)
            inner = frame.inner
        except Exception:
            frame = inner = tk.Frame(body, bg=T.CARD_BG)
        head = tk.Frame(inner, bg=T.CARD_BG)
        head.pack(fill="x")
        tk.Label(head, text="알 운반 · 누가 들었나", bg=T.CARD_BG, fg=T.TEXT, font=(FONT_BODY, 10, "bold"),
                 anchor="w").pack(side="left", padx=(_px(app, 4), 0))
        tk.Label(head, text="빨강 문제 · 주황 늦음 · 회색 전멸 중", bg=T.CARD_BG, fg=T.MUTED, font=(FONT_BODY, 8),
                 anchor="e").pack(side="right")
        txt = tk.Text(inner, height=1, wrap="word", bg=T.CARD_BG, fg=T.TEXT, bd=0, relief="flat", highlightthickness=0,
                      font=(FONT_BODY, 10), cursor="arrow", padx=_px(app, 4), pady=_px(app, 2),
                      selectbackground=T.CARD_BG, insertwidth=0, takefocus=0, spacing1=_px(app, 3))
        txt.tag_configure("head", foreground=T.MUTED, font=(FONT_BODY, 9, "bold"))
        txt.tag_configure("count", foreground=T.MUTED, font=(FONT_BODY, 9))
        txt.tag_configure("owner", font=(FONT_BODY, 10, "bold"))
        for tag, attr in EGG_TAG_COLORS.items():
            txt.tag_configure(tag, foreground=getattr(T, attr, T.MUTED))
        txt.pack(fill="x", expand=True)
        txt.configure(state="disabled")
        txt.bind("<Configure>", lambda e, t=txt: _fit_text(t), add="+")
        card = st["egg_card"] = (frame, txt)
    frame, txt = card
    txt.configure(state="normal")
    txt.delete("1.0", "end")
    for i, ln in enumerate(lines):
        if i:
            txt.insert("end", "\n")
        txt.insert("end", f"{ln.get('head', '')}  ", "head")
        txt.insert("end", f"{ln.get('count', '')}\n", "count")
        for j, (part, tag) in enumerate(ln.get("parts") or []):
            if j:
                txt.insert("end", " · ", "count")
            txt.insert("end", str(part), tag) if tag else txt.insert("end", str(part))
    txt.configure(state="disabled")
    m = _v45()
    if names and m is not None:
        try:
            m.colorize_text_widget(txt, names, font=(FONT_BODY, 10))
        except Exception:
            pass
    try:
        frame.grid(row=50, column=0, columnspan=CARD_COLS, sticky="nsew", padx=_px(app, 3), pady=_px(app, 3))
    except Exception:
        pass
    _fit_text(txt)


def _set_labels(app, labels: dict) -> None:
    for k, lab in (app._v43.get("five_labels") or {}).items():
        try:
            lab.configure(text=labels.get(k, k))
        except Exception:
            pass


def _kill_glance(app, row) -> bool:
    """V5.2.8: 킬 판도 「한눈에 보기」 결론 칸을 보인다. 예전 칸(첫 실수·주요 사고·붕괴 직결)은 「판정 상세 펼치기」."""
    try:
        if str(row.get("result") or "").lower() != "kill" or row.get("mechanicReviewStale"):
            return False
        if app._live_row_is_fake(row) or app._live_row_is_reset(row):
            return False
        box = app.v40_overview_box
        for widget, _pack in getattr(app, "_v40_legacy_pack", ()):
            if widget.winfo_manager():
                widget.pack_forget()
        chron = getattr(app, "v37_raid_time_box", None)
        if chron is not None and chron.winfo_manager():
            chron.pack_forget()
        app._v40_visible_key = _row_key(row) or "kill"
        app._v40_details_open = False
        app._v40_chronology_available = False
        app.v40_toggle_btn.configure(text="판정 상세 펼치기 ▾")
        if not box.winfo_manager():
            box.pack(fill="x", pady=(0, _px(app, 3)))
        return True
    except Exception:
        return False


def _compute(app, mod, key, row) -> None:
    """V5.2.2: 결론 5줄과 (똬리면) 보호막·암영폭탄·현신 접촉 자료를 한 번에 계산해 둔다. 화면은 건드리지 않는다."""
    st = app._v43
    try:
        try:  # V5.2: 똬리는 공대 차단 규칙(kick_rules_3429.json)을 같이 넘긴다
            from live.v52_screen import build_module as _v52_build
            out = _v52_build(mod, key)
        except ImportError:
            out = mod.build_from_dir(key, use_raw=True)
        lines = {k: str((out.get("lines") or {}).get(k) or "") for k in FIVE_KEYS}
        lines["_stages"] = out.get("stages") or []
        lines["_notes"] = [str(x) for x in (out.get("notes") or [])][:4]
        lines["_labels"] = dict(out.get("labels") or {})   # V5.2.8: 킬 판 칸 이름 (사망 정리 · 다음 주)
        lines["_checks"] = list(out.get("checks") or [])   # V5.2.9: 엔진 판정 없는 보스의 기믹 수행 체크 칸
        # V5.4.2: 울라텍 「알 운반」 카드 (누가 알을 들었나 · 오른쪽 기믹 수행 체크 칸 아래)
        try:   # V5.4.4 한눈에 보기 (모든 보스 · 결론 5줄에서 뽑기만)
            from live import v544_glance as _g
            import json as _json
            _facts = {}
            _fp = Path(key) / "raid_facts.json"
            if _fp.is_file():
                _facts = _json.loads(_fp.read_text(encoding="utf-8"))
            lines["_glance"] = _g.build(out, _facts, row.get("result"))
        except Exception:
            lines["_glance"] = None
        ec = (out.get("plan") or {}).get("eggCarry")
        lines["_eggCarry"] = mod.egg_carry_lines(ec) if (ec and hasattr(mod, "egg_carry_lines")) else []
    except Exception:
        lines = {}
    try:
        from live import v52_screen as _v52
        _v52.prefill(app, row)
    except Exception:
        pass
    st["five"][key] = lines


def _finish(app, key) -> None:
    st = app._v43
    st["pending"].discard(key)
    try:
        cur = st.get("current")
        if cur is not None and _row_key(cur) == key:
            _show_five(app, cur)
            _reword_tree(app.b3641_try_wrap)
            try:
                from live import v52_screen as _v52
                _v52.apply(app, cur)
            except Exception:
                pass
    except Exception:
        pass


PREFETCH_NEAR = 4


def _live_busy() -> bool:
    """V5.2.4: 라이브 분석이 도는 중이면 미리 계산을 쉰다."""
    try:
        from live.raid_review import live_analysis_busy
        return bool(live_analysis_busy())
    except Exception:
        return False


def _prefetch(app, row) -> None:
    """V5.2.2: 지금 트라이 앞뒤(같은 보스·같은 날)를 미리 계산해 둔다. 한 번에 하나씩, 화면은 안 건드림."""
    st = app._v43
    try:
        enc = int(row.get("encounterID") or 0)
    except (TypeError, ValueError):
        return
    name = _BOSS_MODULES.get(enc)
    if not name:
        return
    mod = _module(app, name)
    if mod is None:
        return
    try:
        rows = [r for r in app._all_live_rows() if str(r.get("encounterID")) == str(enc)
                and app._live_context_key(r) == app._live_context_key(row)]
    except Exception:
        return
    keys = [_row_key(r) for r in rows]
    cur = _row_key(row)
    if cur not in keys:
        return
    i = keys.index(cur)
    order = sorted(range(len(rows)), key=lambda j: (abs(j - i), -j))
    queue = [rows[j] for j in order if j != i][:PREFETCH_NEAR]
    if rows and rows[-1] not in queue and _row_key(rows[-1]) != cur:
        queue.insert(0, rows[-1])  # 방금 끝난 트라이를 먼저
    queue = [r for r in queue if _row_key(r) and _row_key(r) not in st["five"] and Path(_row_key(r)).is_dir()]
    st["prefetch_queue"] = queue
    if not queue or st.get("prefetch_running"):
        return
    st["prefetch_running"] = True

    def worker():
        import time as _t
        try:
            while st.get("prefetch_queue"):
                while st.get("fg", 0) > 0 or _live_busy():  # 지금 고른 트라이 · 라이브 분석이 먼저 (V5.2.4)
                    _t.sleep(0.5 if _live_busy() else 0.2)
                if not st.get("prefetch_queue"):
                    break
                r = st["prefetch_queue"].pop(0)
                k = _row_key(r)
                if k in st["five"] or k in st["pending"]:
                    continue
                st["pending"].add(k)
                _compute(app, mod, k, r)
                try:
                    app.after(0, lambda k=k: _finish(app, k))
                except Exception:
                    st["pending"].discard(k)
                    return
                _t.sleep(0.3)
        finally:
            st["prefetch_running"] = False
    threading.Thread(target=worker, daemon=True).start()


def apply(app, row):
    relayout(app)
    app._v43["current"] = row
    try:
        _show_five(app, row)
    except Exception:
        pass
    for w in (getattr(app, "b3641_try_wrap", None), getattr(app, "b3796_chain_wrap", None)):
        if w is not None:
            _reword_tree(w)
    # V4.5: 붕괴 체인·연결 칸의 캐릭터 이름은 직업색 (보통 굵기).
    m = _v45()
    names = _name_colors(row)
    if m is not None and names:
        try:
            from nini_wcl_gui import FONT_BODY  # type: ignore
        except Exception:
            FONT_BODY = "TkDefaultFont"
        for panel in (getattr(app, "b3796_chain_panel", None), getattr(app, "b3796_link_panel", None)):
            body = getattr(panel, "body", None)
            if body is not None:
                try:
                    m.recolor_labels(body, names, font_body=FONT_BODY)
                except Exception:
                    pass
    # V5.2: 링크 · P2 보호막 칸 · 암영폭탄/현신 접촉 카드 (실패해도 기존 화면 그대로)
    try:
        from live import v52_screen as _v52
        _v52.apply(app, row)
    except Exception:
        pass
    # V5.2.2: 앞뒤 트라이 미리 계산 (클릭하면 바로 보이게)
    try:
        app.after(1500, lambda r=row: _prefetch(app, r) if app._v43.get("current") is r else None)
    except Exception:
        pass
