"""V4.5 · 기믹 시간축 스킬별 줄 (Viserio Ability timeline 방식) + 캐릭터 이름 직업색 (display only).

- 시간축: 스킬마다 한 줄. 왼쪽에 아이콘 + 기믹 색 이름, 줄 안에는 그 스킬이 나온 시각마다 아이콘.
  판정(실패·주의)은 기존처럼 테두리 색 + 번호. 보통 아이콘 테두리는 그 스킬 색으로 얇게.
- 이름: 그 트라이 raid_facts 로스터의 전문화 → 직업색. 이름은 보통 굵기, 기믹은 굵게라서 서로 구분된다.
- 읽기만 한다. 저장·판정·엔진은 건드리지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import tkinter as tk
from pathlib import Path

VERSION = "V4.6-TIMELINE"
ROW_MODE = "스킬별"
ROW_LEFT = 168          # 왼쪽 이름 칸 (design px)
ROW_H = 24
ROW_ICON = 18
NEUTRAL_OUTLINE = "#7f8994"
DEFAULT_ROW_COLOR = "#c9d1d9"
ROW_STRIPE = "#1b232c"
ROW_LINE = "#232d37"


def _layout():
    try:
        from live import v43_layout as L  # type: ignore
        return L
    except Exception:
        return None


def mechanic_color(title: str) -> str:
    """기믹 이름 → 고정 색 (V4.4 결론 5줄과 같은 표). 모르는 이름은 밝은 회색."""
    L = _layout()
    if L is None:
        return DEFAULT_ROW_COLOR
    t = str(title or "")
    for key in sorted(L.MECHANIC_GROUP, key=len, reverse=True):
        if key in t:
            return L.GROUP_COLORS.get(L.MECHANIC_GROUP[key], DEFAULT_ROW_COLOR)
    return DEFAULT_ROW_COLOR


def _title(glyph: dict) -> str:
    return str(glyph.get("title") or glyph.get("glyphKey") or glyph.get("spellID") or "?")


def _has_hangul(t: str) -> bool:
    return any("\uac00" <= ch <= "\ud7a3" for ch in str(t))


def _row_key(glyph: dict) -> str:
    """같은 주문(시전·피해 ID 묶음)은 한 줄. 주문 ID 가 없으면 이름으로."""
    sid = None
    try:
        from live.timeline_glyphs import _main_spell_id  # type: ignore
        sid = _main_spell_id(glyph)
    except Exception:
        sid = glyph.get("spellID")
    return f"sid:{sid}" if sid else _title(glyph)


def ability_rows(glyphs, x_for, icon_px: int = ROW_ICON):
    """스킬마다 한 줄. place_glyph_lanes 와 같은 (placed, overflow) 모양 + 줄 정보.

    줄 순서 = 그 스킬이 처음 나온 시각. 판정 글리프도 자기 스킬 줄에 그대로 둔다 (숨기지 않음).
    """
    rows: list[dict] = []
    index: dict[str, int] = {}
    placed: list[dict] = []
    for raw in sorted((dict(g) for g in (glyphs or [])), key=lambda g: (float(g.get("sec") or 0), -int(g.get("priority") or 0))):
        key = _row_key(raw)
        title = _title(raw)
        if key not in index:
            index[key] = len(rows)
            rows.append({"key": key, "title": title, "glyph": raw, "count": 0, "fails": 0,
                         "color": mechanic_color(title), "firstSec": float(raw.get("sec") or 0)})
        row = rows[index[key]]
        if not _has_hangul(row["title"]) and _has_hangul(title):
            row["title"], row["color"] = title, mechanic_color(title)  # 영어 이름보다 한글 이름
        row["count"] += 1
        if str(raw.get("status") or "") in ("failed", "warning") and raw.get("judgmentNumbers"):
            row["fails"] += 1
        raw["lane"] = index[key]
        raw["x"] = float(x_for(raw.get("sec") or 0))
        row.setdefault("xs", []).append(raw["x"])
        raw["v45Color"] = row["color"]
        placed.append(raw)
    return placed, [], rows


def row_outline(glyph: dict, outline: str) -> str:
    """판정 색이 없는 보통 아이콘은 그 스킬 색 테두리."""
    if str(outline or "").lower() == NEUTRAL_OUTLINE:
        return str(glyph.get("v45Color") or outline)
    return outline


def _short(title: str, limit: int = 9) -> str:
    t = str(title or "")
    return t if len(t) <= limit else t[: limit - 1] + "…"


ANCHOR_DASH = "#7d7cf0"


def _anchor_ids() -> set:
    try:
        from live.boss_guides_v1 import anchor_spells  # type: ignore
        return set(anchor_spells())
    except Exception:
        return set()


def draw_anchor_lines(c, rows, glyph_top, lane_h):
    """V4.6: 주기 기준 시전(시음·정지장·강화 등)마다 점선 (Viserio 「dashed = each Imbibe cast」)."""
    ids = _anchor_ids()
    bottom = glyph_top + len(rows) * lane_h
    n = 0
    for row in rows:
        g = row.get("glyph") or {}
        sids = {int(x) for x in (g.get("spellIDs") or []) if str(x).isdigit()} | ({int(g["spellID"])} if str(g.get("spellID") or "").isdigit() else set())
        if not (sids & ids):
            continue
        for x in row.get("xs") or []:
            c.create_line(x, glyph_top, x, bottom, fill=ANCHOR_DASH, dash=(3, 3), tags=("v46_anchor_line",))
            n += 1
    return n


def draw_row_labels(app, c, rows, glyph_top, lane_h, left, right, refs=None, font_body="TkDefaultFont", px=lambda v: v):
    """왼쪽 이름 칸: 줄무늬 + 작은 아이콘 + 기믹 색 이름 + 이번 트라이 횟수. 줄 경계선은 차트 전체."""
    refs = list(refs or [])
    try:
        draw_anchor_lines(c, rows, glyph_top, lane_h)
    except Exception:
        pass
    for i, row in enumerate(rows):
        y0 = glyph_top + i * lane_h
        y1 = y0 + lane_h
        if i % 2 == 0:
            c.create_rectangle(0, y0, left - 4, y1, fill=ROW_STRIPE, outline="", tags=("v45_row_stripe",))
        c.create_line(0, y1, right, y1, fill=ROW_LINE, tags=("v45_row_line",))
        glyph = row.get("glyph") or {}
        ix = 12 + px(ROW_ICON) / 2
        iy = (y0 + y1) / 2
        photo = None
        try:
            photo = app._b3750_load_glyph_icon(glyph, ROW_ICON - 2, row.get("color") or NEUTRAL_OUTLINE)
        except Exception:
            photo = None
        if photo:
            item = c.create_image(ix, iy, image=photo, tags=("v45_row_icon",))
        else:
            badge = None
            try:
                badge = app._b3750_make_fallback_badge(ROW_ICON - 2, row.get("color") or NEUTRAL_OUTLINE)
            except Exception:
                badge = None
            if badge:
                item = c.create_image(ix, iy, image=badge, tags=("v45_row_icon",))
            else:
                item = c.create_rectangle(ix - 7, iy - 7, ix + 7, iy + 7, outline=row.get("color"), width=1, tags=("v45_row_icon",))
            c.create_text(ix, iy, text=str(glyph.get("fallback") or row.get("title") or "?")[:1], fill="#e9edf2",
                          font=(font_body, 7, "bold"), tags=("v45_row_icon_text",))
        ref = next((r for r in refs if str(r.get("title") or "") == row.get("title")), None)
        try:
            if ref is not None and ref.get("tooltipRows"):
                app._bind_b3750_skill_tip(c, item, ref, reference=True)
            elif glyph.get("tooltipRows"):
                app._bind_b3750_skill_tip(c, item, glyph, reference=False)
        except Exception:
            pass
        label = _short(row.get("title"))
        text = c.create_text(ix + px(ROW_ICON) / 2 + 6, iy, anchor="w", text=label, fill=row.get("color"),
                             font=(font_body, 8, "bold"), tags=("v45_row_label",))
        if row.get("count", 0) > 1:
            c.create_text(left - 10, iy, anchor="e", text=f"{row['count']}", fill="#76808b", font=(font_body, 7),
                          tags=("v45_row_count",))
        try:
            app._bind_b3641_timeline_tip(c, text, f"{row.get('title')}\n이번 트라이 {row.get('count')}번")
        except Exception:
            pass


# ---------------------------------------------------------------- 이름 직업색

_NAME_CACHE: dict[str, tuple[int, dict]] = {}


def _facts_path(row) -> Path | None:
    row = row or {}
    folder = Path(str(row.get("storageDir") or "")) if row.get("storageDir") else None
    value = row.get("raidFactsJson")
    if value:
        p = Path(str(value))
        if not p.is_absolute() and folder is not None:
            p = folder / p
        if p.is_file():
            return p
    if folder is not None and (folder / "raid_facts.json").is_file():
        return folder / "raid_facts.json"
    return None


def name_colors(row) -> dict:
    """이 트라이 로스터: 이름(전체·서버 뺀 이름) → 직업색."""
    path = _facts_path(row)
    if path is None:
        return {}
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    hit = _NAME_CACHE.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        from live.class_colors import color_for_spec  # type: ignore
    except Exception:
        return {}
    out: dict[str, str] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        for m in data.get("roster") or []:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name") or "")
            color = color_for_spec(m.get("specID"))
            if not name or not color:
                continue
            if len(name) >= 2:
                out[name] = color
            short = name.split("-")[0]
            if len(short) >= 2:
                out.setdefault(short, color)
    except Exception:
        out = {}
    if len(_NAME_CACHE) > 128:
        _NAME_CACHE.clear()
    _NAME_CACHE[str(path)] = (mtime, out)
    return out


def split_names(text: str, colors: dict) -> list[tuple[str, str | None]]:
    """글을 (조각, 직업색 또는 None) 으로 나눈다. 긴 이름 먼저 (서버 붙은 이름이 짧은 이름보다 먼저)."""
    text = str(text or "")
    if not colors or not text:
        return [(text, None)]
    pat = re.compile("|".join(re.escape(k) for k in sorted(colors, key=len, reverse=True)))
    out, pos = [], 0
    for m in pat.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], None))
        out.append((m.group(0), colors[m.group(0)]))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], None))
    return out


def draw_rich_line(c, x, y, text, fill, font, colors, tags=(), name_font=None):
    """캔버스 한 줄: 이름만 직업색(보통 굵기), 나머지는 원래 색. 아이템 id 목록을 돌려준다."""
    from tkinter import font as tkfont
    segs = split_names(text, colors)
    if len(segs) == 1 and segs[0][1] is None:
        return [c.create_text(x, y, anchor="nw", text=text, fill=fill, font=font, tags=tags)]
    f_base = tkfont.Font(root=c, font=font)
    nf = name_font or ((font[0], font[1]) if isinstance(font, tuple) and len(font) >= 2 else font)
    f_name = tkfont.Font(root=c, font=nf)
    items, cx = [], x
    for seg, color in segs:
        f = f_name if color else f_base
        items.append(c.create_text(cx, y, anchor="nw", text=seg, fill=color or fill, font=nf if color else font, tags=tags))
        cx += f.measure(seg)
    return items


def colorize_text_widget(t, colors, tag_prefix="cls:", font=None):
    """이미 글이 들어간 tk.Text 에 이름 직업색 태그를 덧입힌다 (기믹 태그는 그대로)."""
    if not colors:
        return
    try:
        content = t.get("1.0", "end-1c")
    except Exception:
        return
    state = str(t.cget("state"))
    t.configure(state="normal")
    for name, color in colors.items():
        tag = tag_prefix + color
        try:
            t.tag_configure(tag, foreground=color, **({"font": font} if font else {}))
            t.tag_raise(tag)
        except Exception:
            continue
    pat = re.compile("|".join(re.escape(k) for k in sorted(colors, key=len, reverse=True)))
    for m in pat.finditer(content):
        try:
            t.tag_add(tag_prefix + colors[m.group(0)], f"1.0+{m.start()}c", f"1.0+{m.end()}c")
        except Exception:
            pass
    t.configure(state=state)


def recolor_labels(container, colors, font_body="TkDefaultFont"):
    """붕괴 체인처럼 Label 로 그린 글: 로스터 이름이 들어 있으면 같은 자리에 읽기 전용 Text 로 바꿔 이름만 직업색.

    원래 Label 은 지우지 않고 감춘다 (다른 코드가 들고 있는 참조가 깨지지 않게). 이미 바꾼 Label 은 건너뛴다.
    """
    if not colors or container is None:
        return 0
    pat = re.compile("|".join(re.escape(k) for k in sorted(colors, key=len, reverse=True)))
    changed = 0
    stack = [container]
    while stack:
        w = stack.pop()
        try:
            kids = w.winfo_children()
        except Exception:
            continue
        stack.extend(kids)
        try:
            if w.winfo_class() != "Label" or getattr(w, "_v45_swapped", False) or not w.winfo_manager():
                continue
            if str(w.cget("textvariable") or ""):
                continue  # 값이 바뀌는 Label 은 그대로 둔다
            text = str(w.cget("text") or "")
        except Exception:
            continue
        if not text or not pat.search(text) or w.winfo_manager() != "pack":
            continue
        try:
            info = w.pack_info()
            info.pop("in", None)
            font = w.cget("font")
            bg = w.cget("bg")
            fg = w.cget("fg")
            t = tk.Text(w.master, height=1, width=1, wrap="word", bg=bg, fg=fg, bd=0, relief="flat",
                        highlightthickness=0, font=font, cursor="arrow", padx=0, pady=0,
                        selectbackground=bg, insertwidth=0, takefocus=0)
            t.insert("1.0", text)
            colorize_text_widget(t, colors)
            t.configure(state="disabled")
            side = info.pop("side", "top")
            t.pack(side=side, before=w, **{k: v for k, v in info.items() if k in ("anchor", "expand", "fill", "ipadx", "ipady", "padx", "pady")})
            if side in ("left", "right") and str(info.get("fill")) not in ("x", "both"):
                from tkinter import font as tkfont
                f = tkfont.Font(root=t, font=font)
                t.configure(width=max(2, int(f.measure(text) / max(1, f.measure("0"))) + 1), wrap="none")
            w.pack_forget()
            w._v45_swapped = True
            t._v45_text = True

            def fit(_e=None, t=t):
                try:
                    n = int(t.count("1.0", "end", "displaylines")[0])
                    if n and int(t.cget("height")) != n:
                        t.configure(height=n)
                except Exception:
                    pass
            t.bind("<Configure>", fit, add="+")
            t.after_idle(fit)
            changed += 1
        except Exception:
            continue
    return changed


# ---------------------------------------------------------------- 창 위치 (V4.5 R2)

def fit_rect(rect, work):
    """창 테두리 rect(left, top, right, bottom)를 작업 영역 work 안으로. 바뀐 (left, top, width, height) 또는 None.

    높이가 작업 영역보다 크면 작업 영역 높이로 줄이고, 위가 넘치면 위에 붙이고, 아래가 넘치면 올린다.
    가로는 건드리지 않는다 (세로 QHD 1440 폭에서 테두리 8px 걸침은 정상).
    """
    left, top, right, bottom = [int(v) for v in rect]
    w_top, w_bottom = int(work[1]), int(work[3])
    w, h = right - left, bottom - top
    new_h = min(h, w_bottom - w_top)
    new_top = top
    if new_top < w_top:
        new_top = w_top
    if new_top + new_h > w_bottom:
        new_top = max(w_top, w_bottom - new_h)
    if new_top == top and new_h == h:
        return None
    return left, new_top, w, new_h


def keep_on_screen(app) -> bool:
    """Windows: 실제 창(제목줄 포함)이 그 모니터 작업 영역을 벗어나면 안으로 옮긴다. 옮겼으면 True."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        if str(app.state()) in ("zoomed", "iconic"):
            return False
        hwnd = int(str(app.wm_frame()), 16)
        r = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return False
        mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return False
        # 보이지 않는 크기조절 테두리(약 8px)는 위쪽에는 없으므로 top 은 그대로 비교한다.
        fit = fit_rect((r.left, r.top, r.right, r.bottom), (info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom))
        if fit is None:
            return False
        left, top, w, h = fit
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        return bool(user32.SetWindowPos(hwnd, 0, left, top, w, h, SWP_NOZORDER | SWP_NOACTIVATE))
    except Exception:
        return False
