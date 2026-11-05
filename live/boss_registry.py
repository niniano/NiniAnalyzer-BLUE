from __future__ import annotations

"""Canonical raid-boss registration used by storage and presentation layers.

Boss mechanic rule JSON files still own spell IDs and mechanic-specific logic.
This registry owns the stable encounter -> key/display-name identity so UI and
archive paths do not invent independent aliases such as encounter_3492.
"""

BOSS_REGISTRY: dict[int, dict[str, str]] = {
    3379: {"key": "nymrissa", "slug": "nymrissa", "name": "님리사 웨이브콜러"},
    3420: {"key": "sszorak", "slug": "sszorak", "name": "스조라크"},
    3421: {"key": "twin_fangs", "slug": "twin_fangs", "name": "쌍둥이 송곳니"},
    3429: {"key": "coiled_altar", "slug": "coiled_altar", "name": "똬리의 제단"},
    3445: {"key": "buried_sentinel", "slug": "buried_sentinel", "name": "매장된 파수꾼"},
    3455: {"key": "vashnik", "slug": "vashnik", "name": "악성의 바쉬니크"},
    3470: {"key": "nekzali", "slug": "nekzali", "name": "영혼살무사 네크잘리"},
    3492: {"key": "ulatek", "slug": "ulatek", "name": "울라텍"},
    3497: {"key": "lost_explorers", "slug": "lost_explorers", "name": "길 잃은 탐험가"},
}


def boss_entry(encounter_id) -> dict[str, str] | None:
    try:
        return BOSS_REGISTRY.get(int(encounter_id or 0))
    except (TypeError, ValueError):
        return None


def boss_key(encounter_id, default: str | None = None) -> str | None:
    item = boss_entry(encounter_id)
    return item["key"] if item else default


def boss_slug(encounter_id) -> str:
    try:
        eid = int(encounter_id or 0)
    except (TypeError, ValueError):
        eid = 0
    item = BOSS_REGISTRY.get(eid)
    return item["slug"] if item else f"encounter_{eid or 'unknown'}"


def boss_name(encounter_id, default: str | None = None) -> str:
    item = boss_entry(encounter_id)
    if item:
        return item["name"]
    return str(default or f"Encounter {encounter_id or '?'}")


def registered_encounter_ids() -> tuple[int, ...]:
    return tuple(sorted(BOSS_REGISTRY))
