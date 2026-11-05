"""Skill tooltip catalog (data/skill_tips.json).

Pure lookup, no Tk.  Sources per field:
  nameKO   = WCL log Korean name (source of truth) / Wowhead ko fallback
  effect   = Wowhead Korean tooltip, scaled numbers removed
  categories, icon = Viserio
  what, todo = WCLA hand-written (raid strategy may override)
The tooltip never states a judgment by itself: "이 트라이 / 판정" lines come
from the analyzer's own evidence level at render time.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "data" / "skill_tips.json"
ICON_DIR = ROOT / "data" / "skill_icons"


@lru_cache(maxsize=1)
def _catalog() -> dict:
    try:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"glyphMap": {}, "titleFixes": {}, "bosses": {}}


@lru_cache(maxsize=1)
def _by_spell() -> dict[int, dict]:
    out: dict[int, dict] = {}
    for boss_key, boss in (_catalog().get("bosses") or {}).items():
        for spell in boss.get("spells") or []:
            row = dict(spell, boss=boss_key, bossNameKO=boss.get("nameKO", ""))
            out.setdefault(int(spell["spellID"]), row)
    # related IDs (damage tick / cast pair) resolve to the main tip
    for sid, row in list(out.items()):
        for rel in row.get("relatedSpellIDs") or []:
            out.setdefault(int(rel), row)
    return out


def reload() -> None:
    _catalog.cache_clear()
    _by_spell.cache_clear()
    _glyph_by_boss_spell.cache_clear()


def glyph_entry(glyph_key: str) -> dict | None:
    return (_catalog().get("glyphMap") or {}).get(str(glyph_key or ""))


def boss_spells(boss_key: str) -> list[dict]:
    """Return the curated catalog spell rows for one boss, in catalog order.

    Presentation helper only.  Returning copies prevents timeline/UI code from
    mutating the cached catalog.
    """
    boss = (_catalog().get("bosses") or {}).get(str(boss_key or "")) or {}
    return [dict(row) for row in (boss.get("spells") or []) if isinstance(row, dict)]


@lru_cache(maxsize=1)
def _glyph_by_boss_spell() -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    for glyph_key, entry in (_catalog().get("glyphMap") or {}).items():
        if not isinstance(entry, dict):
            continue
        boss = str(entry.get("boss") or "")
        for raw_sid in (entry.get("spellID"), entry.get("eventSpellID")):
            try:
                sid = int(raw_sid)
            except (TypeError, ValueError):
                continue
            if boss and sid:
                out.setdefault((boss, sid), str(glyph_key))
    return out


def glyph_key_for_spell(boss_key: str, spell_id: Any) -> str | None:
    try:
        sid = int(spell_id)
    except (TypeError, ValueError):
        return None
    return _glyph_by_boss_spell().get((str(boss_key or ""), sid))


def tip_for(spell_id: Any = None, glyph_key: str | None = None) -> dict | None:
    """Tooltip dict for a spell ID or an analyzer glyph key (spell ID wins)."""
    table = _by_spell()
    try:
        if spell_id is not None and int(spell_id) in table:
            return table[int(spell_id)]
    except (TypeError, ValueError):
        pass
    entry = glyph_entry(glyph_key) if glyph_key else None
    if entry:
        for sid in (entry.get("spellID"), entry.get("eventSpellID")):
            if sid is not None and int(sid) in table:
                return table[int(sid)]
    return None


def official_title(glyph_key: str, fallback: str | None = None) -> str | None:
    """In-game Korean name for a glyph (log name), else the fallback title."""
    entry = glyph_entry(glyph_key)
    if entry and entry.get("title"):
        return entry["title"]
    return fallback


def icon_path(spell_id: Any) -> Path | None:
    tip = tip_for(spell_id)
    name = (tip or {}).get("icon")
    path = ICON_DIR / name if name else None
    return path if path and path.is_file() else None


def tooltip_lines(tip: dict) -> list[tuple[str, str]]:
    """(label, text) rows in WCLA order: 무엇 / 결과 / 대처."""
    rows = []
    for label, key in (("무엇", "what"), ("결과", "effect"), ("대처", "todo")):
        text = str(tip.get(key) or "").strip()
        if text:
            rows.append((label, text))
    return rows
