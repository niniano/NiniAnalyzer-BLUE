from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

EVIDENCE_VERIFIED = "verified"
EVIDENCE_CANDIDATE = "candidate"
EVIDENCE_UNAVAILABLE = "unavailable"

EVIDENCE_LABELS = {
    EVIDENCE_VERIFIED: "검증됨",
    EVIDENCE_CANDIDATE: "후보",
    EVIDENCE_UNAVAILABLE: "확인 불가",
}

PACKAGE_DIR = Path(__file__).resolve().parent
RULE_DIR = PACKAGE_DIR / "rules"
DEFAULT_STRATEGY_PATH = PACKAGE_DIR / "default_strategies.json"


def repo_root() -> Path:
    # live/boss_mechanics/contracts.py -> repository root
    return PACKAGE_DIR.parents[1]


def user_strategy_path() -> Path:
    return repo_root() / "data" / "boss_strategy.json"


def load_json(path: Path, fallback):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception:
        return deepcopy(fallback)


def load_rule(boss_key: str) -> dict:
    path = RULE_DIR / f"{boss_key}.json"
    data = load_json(path, {})
    if not isinstance(data, dict):
        return {}
    return data


def load_all_rules() -> list[dict]:
    out = []
    if not RULE_DIR.exists():
        return out
    for path in sorted(RULE_DIR.glob("*.json")):
        data = load_json(path, {})
        if isinstance(data, dict) and data.get("bossKey"):
            out.append(data)
    return out



def _deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out

def _migrate_strategy_override(boss_key: str, override: dict) -> dict:
    """Normalize legacy strategy keys without mutating the user's file.

    B36.5 renamed the P1->P2 orb cleanup plan from raidPlan.tankHandoff
    to raidPlan.orbHandoff.  Existing user plans keep working: a legacy
    tankHandoff block is copied to orbHandoff only when the modern key is
    absent.  If both are present, the explicit modern orbHandoff wins.
    """
    out = deepcopy(override or {})
    if boss_key == "coiled_altar":
        raid_plan = out.get("raidPlan")
        if isinstance(raid_plan, dict):
            legacy = raid_plan.get("tankHandoff")
            if "orbHandoff" not in raid_plan and isinstance(legacy, dict):
                raid_plan["orbHandoff"] = deepcopy(legacy)
            raid_plan.pop("tankHandoff", None)
    return out


def load_strategy(boss_key: str) -> tuple[dict, str]:
    defaults = load_json(DEFAULT_STRATEGY_PATH, {})
    default_value = deepcopy((defaults or {}).get(boss_key) or {})
    user_path = user_strategy_path()
    if user_path.exists():
        overrides = load_json(user_path, {})
        override = (overrides or {}).get(boss_key)
        if isinstance(override, dict):
            override = _migrate_strategy_override(boss_key, override)
            return _deep_merge(default_value, override), "user-override"
    return default_value, "packaged-default"


def evidence_label(level: str | None) -> str:
    return EVIDENCE_LABELS.get(str(level or ""), "확인 불가")


def make_check(*, key: str, title: str, status: str = "unknown",
               evidence_level: str = EVIDENCE_UNAVAILABLE, summary: str = "",
               details=None, ids=None) -> dict:
    return {
        "key": key,
        "title": title,
        "status": status,
        "evidenceLevel": evidence_level,
        "summary": summary,
        "details": list(details or []),
        "ids": dict(ids or {}),
    }
