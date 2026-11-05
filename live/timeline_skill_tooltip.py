"""Presentation-only formatter for B37.5.0 skill catalog tooltips.

It consumes the already-normalized timeline glyph/reference/range dictionaries.
No mechanic status or evidence level is changed here.
"""
from __future__ import annotations

from typing import Any


_LOG_NAMES = {
    "SPELL_CAST_START": "시전 시작", "CAST_START": "시전 시작",
    "SPELL_CAST_SUCCESS": "시전 완료", "CAST_SUCCESS": "시전 완료",
    "SPELL_AURA_APPLIED": "효과 적용", "APPLYBUFF": "효과 적용",
    "SPELL_AURA_REMOVED": "효과 종료", "REMOVEBUFF": "효과 종료",
    "SPELL_DAMAGE": "피해", "DAMAGE": "피해",
}

_ROLE_NAMES = {
    "first_mistake": "첫 실수",
    "additional_failure": "추가 실패",
    "collapse_driver": "붕괴 직결",
    "recovered_on_kill": "킬에서 회복",
}

_STATUS_NAMES = {
    "success": "정상",
    "failed": "실패",
    "warning": "경고",
    "observed": "관측",
    "unknown": "관측",
    "pending": "판정 대기",
}


def _fmt_clock(sec: Any) -> str:
    try:
        value = max(0.0, float(sec))
    except (TypeError, ValueError):
        return "-"
    minute = int(value // 60)
    second = value - minute * 60
    return f"{minute}:{second:04.1f}"


def _unique_names(values: Any, limit: int = 6) -> str:
    out: list[str] = []
    for raw in values or []:
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text)
    if not out:
        return ""
    shown = ", ".join(out[:limit])
    if len(out) > limit:
        shown += f" 외 {len(out)-limit}명"
    return shown


def _this_try_text(item: dict, reference: bool = False) -> str:
    if reference:
        return "스킬 참고 · 실제 발생 내용은 타임라인의 해당 아이콘에서 확인"

    start = item.get("startSec")
    end = item.get("endSec")
    sec = item.get("sec")
    if start is not None:
        when = _fmt_clock(start)
        if end is not None:
            when += f"~{_fmt_clock(end)}"
    else:
        when = _fmt_clock(sec)

    summary = str(item.get("summary") or "").strip()
    targets = _unique_names(item.get("targets") or item.get("targetNames"))
    target_count = item.get("targetCount")
    raw = str(item.get("logEvent") or item.get("event") or "").upper()
    log_label = _LOG_NAMES.get(raw, "")

    parts = [when]
    if summary:
        parts.append(summary)
    elif targets:
        prefix = "대상/피격"
        if target_count:
            try:
                prefix += f" {int(target_count)}명"
            except (TypeError, ValueError):
                pass
        parts.append(f"{prefix} · {targets}")
    elif log_label:
        parts.append(log_label)
    elif item.get("durationSec") is not None:
        try:
            parts.append(f"지속 {float(item.get('durationSec')):.1f}초")
        except (TypeError, ValueError):
            pass
    else:
        parts.append("관측됨")

    linked = list(item.get("linkedEvents") or [])
    if linked:
        parts.append(f"후속 사건 {len(linked)}건")
    return " · ".join(x for x in parts if x)


def _judgment_text(item: dict, reference: bool = False) -> str:
    if reference:
        return "참고 정보 · 카탈로그 자체는 판정 단계를 바꾸지 않음"

    status = str(item.get("status") or "unknown").strip().lower()
    evidence = str(item.get("evidenceLevel") or item.get("meaningEvidenceLevel") or "").strip().lower()
    role_key = str(item.get("failureRole") or "").strip()
    role = str(item.get("failureRoleLabel") or "").strip() or _ROLE_NAMES.get(role_key, "")

    if evidence == "candidate":
        base = "후보"
        if status in {"failed", "warning"}:
            base += f" · {_STATUS_NAMES.get(status, status)} 관측"
        return base + " · candidate · 판정 승격 안 함"

    if evidence == "verified":
        if role:
            return f"{role} · verified"
        return f"{_STATUS_NAMES.get(status, '관측')} · verified"

    if role:
        return role + (f" · {evidence}" if evidence else "")
    if status in {"success", "failed", "warning", "pending"}:
        base = _STATUS_NAMES.get(status, status)
        return base + (f" · {evidence}" if evidence else "")
    if evidence:
        return f"관측 · {evidence}"
    return "관측값 · 별도 판정 없음"



def place_tooltip_within_owner(
    pointer_x: int | float, pointer_y: int | float,
    tooltip_w: int | float, tooltip_h: int | float,
    owner_x: int | float, owner_y: int | float,
    owner_w: int | float, owner_h: int | float,
    *, offset: int = 14, pad: int = 8,
) -> tuple[int, int]:
    """Place a tooltip near the pointer without covering the pointer hotspot.

    The previous clamp could push a tooltip back underneath the mouse when the
    icon was close to an analyzer-window edge.  That makes the canvas receive
    ``<Leave>``, destroys the tooltip, then receive ``<Enter>`` again: visible
    flicker.  Try the four pointer-relative quadrants first and only clamp as a
    last resort, preserving a small no-hover corridor around the pointer.
    """
    px, py = int(pointer_x), int(pointer_y)
    tw, th = max(1, int(tooltip_w)), max(1, int(tooltip_h))
    ox, oy = int(owner_x), int(owner_y)
    ow, oh = max(1, int(owner_w)), max(1, int(owner_h))
    offset, pad = max(0, int(offset)), max(0, int(pad))

    left, top = ox + pad, oy + pad
    right, bottom = ox + ow - pad, oy + oh - pad
    gap = max(18, offset)

    # Prefer the familiar lower-right placement, then flip whole quadrants.
    candidates = [
        (px + gap, py + gap),
        (px - tw - gap, py + gap),
        (px + gap, py - th - gap),
        (px - tw - gap, py - th - gap),
    ]
    for x, y in candidates:
        if x >= left and y >= top and x + tw <= right and y + th <= bottom:
            return int(x), int(y)

    usable_w, usable_h = max(1, right - left), max(1, bottom - top)
    if usable_w <= tw:
        x = left
    else:
        # Keep the pointer outside the tooltip horizontally whenever possible.
        left_x = px - tw - gap
        right_x = px + gap
        if left_x >= left:
            x = left_x
        elif right_x + tw <= right:
            x = right_x
        else:
            x = max(left, min(px + gap, right - tw))

    if usable_h <= th:
        y = top
    else:
        # And vertically; one safe axis is enough to prevent hover stealing.
        above_y = py - th - gap
        below_y = py + gap
        if below_y + th <= bottom:
            y = below_y
        elif above_y >= top:
            y = above_y
        else:
            y = max(top, min(py + gap, bottom - th))

    # Final safety: if clamping still put the pointer inside the rectangle, move
    # to the opposite side on whichever axis has enough room.
    if x <= px < x + tw and y <= py < y + th:
        if px - tw - gap >= left:
            x = px - tw - gap
        elif px + gap + tw <= right:
            x = px + gap
        elif py - th - gap >= top:
            y = py - th - gap
        elif py + gap + th <= bottom:
            y = py + gap
    return int(x), int(y)

def format_tk_position(x: int | float, y: int | float) -> str:
    """Return a syntactically valid Tk position string for signed coordinates.

    ``+{-1200}`` becomes the invalid ``+-1200``.  This helper keeps the sign
    itself, producing ``-1200+180`` instead.  Windows uses SetWindowPos for
    true virtual-desktop absolute placement; this is the safe Tk fallback.
    """
    xi, yi = int(x), int(y)
    sx = f"+{xi}" if xi >= 0 else str(xi)
    sy = f"+{yi}" if yi >= 0 else str(yi)
    return sx + sy


def build_skill_tooltip_view(item: dict | None, *, reference: bool = False) -> dict | None:
    """Return a renderer-friendly rich tooltip model, or None without catalog rows."""
    item = item or {}
    rows = []
    for row in item.get("tooltipRows") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        label, text = str(row[0] or "").strip(), str(row[1] or "").strip()
        if label and text:
            rows.append((label, text))
    if not rows:
        return None

    # Keep the catalog's promised order even if a future producer changes order.
    ordered = []
    for wanted in ("무엇", "결과", "대처"):
        ordered.extend((label, text) for label, text in rows if label == wanted)
    ordered.extend((label, text) for label, text in rows if label not in {"무엇", "결과", "대처"})

    return {
        "title": str(item.get("title") or "스킬").strip() or "스킬",
        "rows": ordered,
        "thisTry": _this_try_text(item, reference=reference),
        "judgment": _judgment_text(item, reference=reference),
        "catalogIconPath": str(item.get("catalogIconPath") or "").strip(),
        "reference": bool(reference),
    }


# ---------------------------------------------------------------------------
# B37.5.0.2 · split roles
#   스킬 참고 strip  → build_reference_tooltip_view : what the skill is and
#                      what to do (무엇 / 결과 / 대처) + how often it showed up.
#   timeline icons  → build_event_tooltip_view     : what actually happened in
#                      this Try (time, occurrence, targets, linked events) and
#                      the analyzer's judgment.  No catalog text here.
# ---------------------------------------------------------------------------
import re as _re

_SUMMARY_NOISE = (
    _re.compile(r"\s*(?:marker\s*)?spell\s*ID\s*\d+", _re.I),
    _re.compile(r"\s*\[(?:검증됨|후보|관측|verified|candidate|observed)\]", _re.I),
)


def clean_summary(text: Any) -> str:
    """Drop analyzer-internal tokens (spell IDs, [검증됨]) from a summary line."""
    out = str(text or "").strip()
    for pat in _SUMMARY_NOISE:
        out = pat.sub("", out)
    out = _re.sub(r"\s{2,}", " ", out)
    return out.strip(" ·")


def _catalog_tip(item: dict) -> dict | None:
    try:
        from live import skill_tips
    except Exception:
        return None
    try:
        return skill_tips.tip_for(spell_id=item.get("spellID"), glyph_key=str(item.get("glyphKey") or "") or None)
    except Exception:
        return None


def _observation_text(item: dict) -> str:
    try:
        count = int(item.get("observedCount") or 0)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return "이 트라이: 시간축 표시 없음"
    secs = [s for s in (item.get("observedSecs") or []) if s is not None]
    shown = ", ".join(_fmt_clock(s) for s in secs[:4])
    if len(secs) > 4:
        shown += " …"
    text = f"이 트라이: {count}회"
    if shown:
        text += f" ({shown})"
    return text + " · 실제 내용은 시간축 아이콘"


def build_reference_tooltip_view(item: dict | None) -> dict | None:
    """스킬 참고 tooltip: 제목 · 분류 · 무엇 / 결과 / 대처 · 관측 횟수."""
    item = item or {}
    base = build_skill_tooltip_view(item, reference=True)
    if not base:
        return None
    tip = _catalog_tip(item) or {}
    chips = [str(c) for c in (tip.get("categories") or []) if str(c or "").strip()]
    cast = str(tip.get("castTime") or "").strip()
    if cast:
        chips.append(f"시전 {cast}")
    return {
        "kind": "reference",
        "title": base["title"],
        "chips": chips,
        "rows": base["rows"],
        "footer": _observation_text(item),
        "catalogIconPath": base["catalogIconPath"],
    }


def _event_lines(item: dict) -> list[str]:
    lines: list[str] = []
    sources = [s for s in (item.get("sourceEvents") or []) if isinstance(s, dict)]
    seen: set[str] = set()
    if len(sources) > 1:
        for src in sources[:5]:
            text = clean_summary(src.get("summary") or src.get("title"))
            line = f"{_fmt_clock(src.get('sec'))} · {text}" if text else _fmt_clock(src.get("sec"))
            if line not in seen:
                seen.add(line)
                lines.append(line)
        if len(sources) > 5:
            lines.append(f"외 {len(sources) - 5}건")
    else:
        summary = clean_summary(item.get("summary"))
        if summary:
            lines.append(summary)
    targets = _unique_names(item.get("targets") or item.get("targetNames"), limit=8)
    if targets:
        prefix = "대상/피격"
        try:
            if item.get("targetCount"):
                prefix += f" {int(item.get('targetCount'))}명"
        except (TypeError, ValueError):
            pass
        lines.append(f"{prefix} · {targets}")
    if item.get("startSec") is not None and item.get("endSec") is not None:
        try:
            lines.append(f"지속 {float(item['endSec']) - float(item['startSec']):.1f}초")
        except (TypeError, ValueError):
            pass
    for linked in list(item.get("linkedEvents") or [])[:4]:
        text = clean_summary(linked.get("summary") or linked.get("title"))
        lines.append(f"→ {_fmt_clock(linked.get('sec'))} {text}".rstrip())
    if not lines:
        raw = str(item.get("logEvent") or item.get("event") or "").upper()
        lines.append(_LOG_NAMES.get(raw, "관측됨"))
    return lines


def _judgment_tone(item: dict) -> str:
    evidence = str(item.get("evidenceLevel") or item.get("meaningEvidenceLevel") or "").lower()
    status = str(item.get("status") or "").lower()
    role = str(item.get("failureRole") or "")
    if evidence == "candidate":
        return "candidate"
    if evidence == "verified" and (role or status == "failed"):
        return "failure"
    if status == "success":
        return "success"
    if status == "warning":
        return "warning"
    return "neutral"


def _event_judgment_text(item: dict) -> str:
    text = _judgment_text(item, reference=False)
    for raw, label in (("· unavailable", "· 판정 기준 없음"), ("· observed", "· 관측값"), ("· unknown", "· 판정 기준 없음")):
        if text.endswith(raw):
            text = text[: -len(raw)] + label
    return text


def build_event_tooltip_view(item: dict | None) -> dict:
    """Timeline icon tooltip: 제목 · 시각 · n번째 · 이 트라이 · 판정 (no catalog text)."""
    item = item or {}
    title = str(item.get("title") or item.get("label") or "기믹").strip() or "기믹"
    start, end, sec = item.get("startSec"), item.get("endSec"), item.get("sec")
    if start is not None:
        when = _fmt_clock(start) + (f"~{_fmt_clock(end)}" if end is not None else "")
    else:
        when = _fmt_clock(sec)
    occurrence = ""
    try:
        idx, total = int(item.get("occurrenceIndex") or 0), int(item.get("occurrenceTotal") or 0)
        if idx and total > 1:
            occurrence = f"{idx}/{total}번째"
    except (TypeError, ValueError):
        pass
    return {
        "kind": "event",
        "title": title,
        "when": when,
        "occurrence": occurrence,
        "lines": _event_lines(item),
        "judgment": _event_judgment_text(item),
        "tone": _judgment_tone(item),
        "catalogIconPath": str(item.get("catalogIconPath") or "").strip(),
    }
