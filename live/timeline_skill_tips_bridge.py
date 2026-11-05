"""B37.5.0 step 2: tiny bridge from timeline glyphs to skill_tips catalog.

This module deliberately does not import Tk and does not change judgment rules.
Existing timeline code can call these helpers first, then keep its legacy
fallbacks when no catalog entry exists.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from live import skill_tips


def title_for(glyph_key: str, legacy_title: str | None = None) -> str | None:
    """Prefer the catalog's official in-game Korean title."""
    return skill_tips.official_title(glyph_key, legacy_title)


def tip_for(spell_id: Any = None, glyph_key: str | None = None) -> dict | None:
    """Return catalog tooltip data without altering evidence/judgment level."""
    return skill_tips.tip_for(spell_id=spell_id, glyph_key=glyph_key)


def tooltip_rows(spell_id: Any = None, glyph_key: str | None = None) -> list[tuple[str, str]]:
    """Return catalog rows in the fixed WCLA order: 무엇 / 결과 / 대처."""
    tip = tip_for(spell_id=spell_id, glyph_key=glyph_key)
    return skill_tips.tooltip_lines(tip) if tip else []


def icon_fallback(spell_id: Any = None, existing: str | Path | None = None) -> str | Path | None:
    """Keep an icon already found by timeline code; otherwise use catalog icon."""
    if existing:
        return existing
    return skill_tips.icon_path(spell_id)


def has_catalog_tip(spell_id: Any = None, glyph_key: str | None = None) -> bool:
    return tip_for(spell_id=spell_id, glyph_key=glyph_key) is not None

def boss_spells(boss_key: str) -> list[dict]:
    """Return all curated catalog spells for the selected boss."""
    return skill_tips.boss_spells(boss_key)


def glyph_key_for_spell(boss_key: str, spell_id: Any) -> str | None:
    """Return the analyzer glyph key when this catalog spell already has one."""
    return skill_tips.glyph_key_for_spell(boss_key, spell_id)

