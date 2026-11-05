"""RC4.3: WoW class colours for player names (first attacker / puller line).

Spec -> class comes from COMBATANT_INFO (raid_facts.json ``roster``), so it
works for already saved Tries without reanalysis.  Unknown spec -> None
(caller keeps the default text colour).
"""
from __future__ import annotations

import json
from pathlib import Path

# Official in-game class colours (RAID_CLASS_COLORS).
CLASS_COLORS = {
    "DEATHKNIGHT": "#C41E3A",
    "DEMONHUNTER": "#A330C9",
    "DRUID": "#FF7C0A",
    "EVOKER": "#33937F",
    "HUNTER": "#AAD372",
    "MAGE": "#3FC7EB",
    "MONK": "#00FF98",
    "PALADIN": "#F48CBA",
    "PRIEST": "#FFFFFF",
    "ROGUE": "#FFF468",
    "SHAMAN": "#0070DD",
    "WARLOCK": "#8788EE",
    "WARRIOR": "#C69B6D",
}

SPEC_CLASS = {
    250: "DEATHKNIGHT", 251: "DEATHKNIGHT", 252: "DEATHKNIGHT",
    577: "DEMONHUNTER", 581: "DEMONHUNTER", 1480: "DEMONHUNTER",
    102: "DRUID", 103: "DRUID", 104: "DRUID", 105: "DRUID",
    1467: "EVOKER", 1468: "EVOKER", 1473: "EVOKER",
    253: "HUNTER", 254: "HUNTER", 255: "HUNTER",
    62: "MAGE", 63: "MAGE", 64: "MAGE",
    268: "MONK", 269: "MONK", 270: "MONK",
    65: "PALADIN", 66: "PALADIN", 70: "PALADIN",
    256: "PRIEST", 257: "PRIEST", 258: "PRIEST",
    259: "ROGUE", 260: "ROGUE", 261: "ROGUE",
    262: "SHAMAN", 263: "SHAMAN", 264: "SHAMAN",
    265: "WARLOCK", 266: "WARLOCK", 267: "WARLOCK",
    71: "WARRIOR", 72: "WARRIOR", 73: "WARRIOR",
}


def color_for_spec(spec_id) -> str | None:
    try:
        cls = SPEC_CLASS.get(int(spec_id))
    except (TypeError, ValueError):
        return None
    return CLASS_COLORS.get(cls) if cls else None


_ROSTER_CACHE: dict[str, tuple[int, dict]] = {}


def _facts_path(row: dict) -> Path | None:
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


def roster_specs(row: dict) -> dict:
    """guid -> specID for one Try (cached by file mtime)."""
    path = _facts_path(row)
    if path is None:
        return {}
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    key = str(path)
    hit = _ROSTER_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    specs = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        for m in data.get("roster") or []:
            if isinstance(m, dict) and m.get("guid") and m.get("specID") is not None:
                specs[str(m["guid"])] = m.get("specID")
    except Exception:
        specs = {}
    if len(_ROSTER_CACHE) > 256:
        _ROSTER_CACHE.clear()
    _ROSTER_CACHE[key] = (mtime, specs)
    return specs


def color_for_player(row: dict, guid) -> str | None:
    if not guid:
        return None
    return color_for_spec(roster_specs(row).get(str(guid)))
