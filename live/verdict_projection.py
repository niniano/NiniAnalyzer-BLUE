from __future__ import annotations

from copy import deepcopy
from typing import Any

VERIFIED = "verified"


def _is_verified(value: Any) -> bool:
    return str(value or "").strip().lower() == VERIFIED


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_failure(row: dict | None, verdict: dict | None = None) -> dict | None:
    if not isinstance(row, dict) or not row:
        return None
    verdict = verdict or {}
    out = dict(row)
    label = out.get("label") or out.get("title") or verdict.get("primary")
    if label:
        out["label"] = label
        out.setdefault("title", label)
    if not out.get("summary"):
        out["summary"] = verdict.get("why") or label
    out.setdefault("evidenceLevel", row.get("evidenceLevel") or verdict.get("evidenceLevel"))
    out.setdefault("status", "failed")
    return out


def mechanic_failure_slots(mechanic_review: dict | None) -> dict:
    """Project a verified BossMechanic failure into the three summary slots.

    The projection is presentation-only. It never rewrites the B31 collapse chain.
    ``collapseDirect`` is populated only when BossMechanic was explicitly linked
    to the B31 collapse evidence (or the mechanic review already supplied one).
    """
    mech = mechanic_review or {}
    verdict = _dict(mech.get("verdict"))
    obs = _dict(mech.get("observations"))
    p2 = _dict(obs.get("phase2"))
    p2_summary = _dict(p2.get("summary"))

    first = (
        _dict(mech.get("firstMistake"))
        or _dict(p2_summary.get("firstMistake"))
        or _dict(obs.get("firstVerifiedFailure"))
    )
    first = _normalize_failure(first, verdict)

    verdict_is_verified_failure = (
        str(verdict.get("status") or "").lower() == "failed"
        and _is_verified(verdict.get("evidenceLevel"))
    )
    row_is_verified = bool(first and _is_verified(first.get("evidenceLevel")))
    if first and not (verdict_is_verified_failure or row_is_verified):
        first = None

    major = _normalize_failure(_dict(mech.get("majorIncident")), verdict)
    if major is None and first is not None:
        major = deepcopy(first)
        major["projection"] = "verified_first_failure"

    direct = (
        _normalize_failure(_dict(mech.get("collapseDirect")), verdict)
        or _normalize_failure(_dict(p2_summary.get("collapseDirect")), verdict)
    )
    link = _dict(mech.get("collapseLink"))
    linked = (
        str(link.get("status") or "").lower() == "linked"
        and _is_verified(link.get("evidenceLevel"))
    )
    if direct is None and first is not None and linked:
        direct = deepcopy(first)
        direct["projection"] = "verified_failure_linked_to_b31"
        direct["linkSummary"] = link.get("summary")

    return {
        "firstMistake": first,
        "majorIncident": major,
        "collapseDirect": direct,
    }


def build_presentation_verdict(mechanic_review: dict | None, raid_review: dict | None) -> dict:
    """Return the user-facing verdict without mutating B31 causal evidence.

    A verified BossMechanic failure owns the headline/action. B31's original
    verdict remains in ``raid_review['verdict']`` and is still used as the causal
    chain/evidence record.
    """
    mech = mechanic_review or {}
    raid = raid_review or {}
    base = _dict(raid.get("verdict"))
    verdict = _dict(mech.get("verdict"))
    slots = mechanic_failure_slots(mech)
    first = slots.get("firstMistake")
    direct = slots.get("collapseDirect")
    override = _dict(mech.get("presentationOverride"))
    link = _dict(mech.get("collapseLink"))

    # B37.3.9.16: when a specific verified mechanic is linked to the observed
    # B31 collapse, the "왜 끝났나" headline should describe that collapse
    # mechanic, while firstMistake remains the earlier chronology slot.
    if (
        override and direct
        and str(link.get("status") or "").lower() == "linked"
        and _is_verified(link.get("evidenceLevel"))
    ):
        return override

    if not first:
        return override or base
    if str(verdict.get("status") or "").lower() != "failed" or not _is_verified(verdict.get("evidenceLevel")):
        return override or base

    return {
        "mode": "verified_mechanic_failure",
        "title": verdict.get("primary") or first.get("label") or first.get("title") or "검증된 기믹 실패",
        "confidence": "high",
        "sec": first.get("sec"),
        "interpretation": verdict.get("why") or first.get("summary") or "검증된 기믹 실패가 확인됐습니다.",
        "action": verdict.get("nextAction") or "다음 풀에서 첫 검증 실패를 우선 제거하세요.",
        "evidenceLevel": VERIFIED,
        "source": "BossMechanic",
        "collapseLinked": (
            str(link.get("status") or "").lower() == "linked"
            and _is_verified(link.get("evidenceLevel"))
        ),
    }
