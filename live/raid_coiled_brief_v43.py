"""Coiled Altar (3429 / mythic 16) raid-first text enhancement, display ONLY.

Does not load files, call engine, change result, promote causal candidates or mutate input.
It only proposes next-pull *checks* for an already verified boss mechanic failure,
when the existing action is boilerplate, and marks missing first-failure data as unknown.
"""
from __future__ import annotations

from typing import Any

_ACTIONS = {
    "p1_glaive_death": "P1 글레이브 연속 피격 0명 목표 · 위험 디버프와 겹치는 구간 먼저 회피",
    "p1_tank_breath_non_tank_hit": "P1 탱 브레스 비탱커 피격 0명 목표 · 브레스 방향과 자리 재확인",
    "p1_orb_failure": "P1 브레스별 구슬 제거량을 기존 공대 계획과 대조 · 짝수 브레스 결과 확인",
    "manifest_collision": "P2 현신 대상자 동선 분리 · 현신 접촉 0회 목표",
    "gloombomb_failure": "P2 암영폭탄 추가 피격 0명 목표 · 지정 쫄 적중과 이전 쫄 중첩 확인",
}

_GENERIC = (
    "첫 실수와 붕괴 직결을 따로 확인",
    "첫 실수와 붕괴 직결을 분리",
    "현재 검증된 실패 없음",
    "현재 검증된 다음 행동 없음",
    "다음 풀에서 확인할 행동을 아직 정하지 못",
    "첫 사망부터 마지막 붕괴까지 시간순으로 따라가",
)


def _supported_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    try:
        if int(row.get("encounterID")) != 3429 or int(row.get("difficultyID")) != 16:
            return False
    except (TypeError, ValueError):
        return False
    if str(row.get("result") or "").casefold() != "wipe":
        return False
    if any(row.get(k) for k in ("isFake", "isReset", "resetExcluded", "mechanicReviewStale")):
        return False
    ts = row.get("startTimestamp")
    if not isinstance(ts, str) or not ts.strip():
        return False
    sk = str(row.get("sourceKey") or "")
    if sk:
        parts = sk.split("|", 3)
        if len(parts) < 4 or parts[:3] != ["3429", "16", ts]:
            return False
    return True


def refine_glance(existing: Any, row: Any, model: Any) -> Any:
    """Return original 3 lines, or a guarded presentation-only rewrite.

    `model` is the GUI's already loaded per-Try BossMechanic priority model.
    No assertion about *cause of wipe* is made by this helper.
    """
    if not (isinstance(existing, (tuple, list)) and len(existing) == 3):
        return existing
    if not _supported_row(row) or not isinstance(model, dict) or model.get("supported") is False:
        return existing
    if model.get("sourceKey") and model["sourceKey"] != row.get("sourceKey"):
        return existing

    incident, relation, action = [str(x or "") for x in existing]
    first = model.get("firstMistake")
    if not isinstance(first, dict) or not first:
        # Do not allow absent observations to be read as a verified success.
        if "첫 기믹 실패는 아직 확인되지 않았" in incident and "성공 판정 아님" not in incident:
            return (incident + " · 성공 판정 아님", relation, action)
        return existing

    if str(first.get("status") or "").lower() != "failed" or str(first.get("evidenceLevel") or "").lower() != "verified":
        return existing
    kind = str(first.get("kind") or "")
    next_check = _ACTIONS.get(kind)
    if not next_check:
        return existing
    if not any(x in action for x in _GENERIC):
        # A bespoke existing instruction is more authoritative than this fallback.
        return existing
    action = "다음 풀: " + next_check
    direct = model.get("collapseDirect")
    if not (isinstance(direct, dict) and str(direct.get("evidenceLevel") or "").lower() == "verified"):
        action += " · 붕괴 직결 원인은 별도 검증"
    return (incident, relation, action)
