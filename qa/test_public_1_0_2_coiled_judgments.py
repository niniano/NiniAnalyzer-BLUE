"""1.0.2 (GOLD V5.3.5 와 같은 수정): 똬리 신화 킬 복기에서 나온 오판 3개.

1. P2 진입 잔여 구슬 — 전투 끝 장부값(P3 웨이브 누적 39)이 아니라 P2 진입 시점 장부값(7)으로 계산
2. P1→P2 진입 — 목표(2:45~2:50)보다 몇 초 빠른 2:40.4 는 실패가 아니라 주의
3. 보호막 쿨기 정렬 — 해당 보호막이 제때 깨졌으면 미사용/지연은 실패가 아니라 참고
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live.boss_mechanics import engine as E  # noqa: E402

RUPTURE = 1299838
RULE = {"mechanics": {"toxicRupture": {"spellIDs": [RUPTURE], "orbCountSignal": {"minConsensusTargets": 4}}}}
STRATEGY = {"raidPlan": {"orbHandoff": {"graceSec": 2.0, "cleanupClusterGapSec": 0.15}}}


def _ledger_kill():
    return [
        {"sec": 90.02, "kind": "spawn", "activeBefore": 0, "activeAfter": 10},
        {"sec": 108.017, "kind": "breath", "activeBefore": 10, "activeAfter": 4},
        {"sec": 125.005, "kind": "breath", "activeBefore": 4, "activeAfter": 1},
        {"sec": 128.005, "kind": "guillotine", "activeBefore": None, "activeAfter": None},
        {"sec": 133.023, "kind": "spawn", "activeBefore": 1, "activeAfter": 11},
        {"sec": 145.031, "kind": "breath", "activeBefore": 11, "activeAfter": 7},
        # P3 웨이브: 브레스 장부가 지우지 않아 전투 끝까지 누적된다
        {"sec": 388.626, "kind": "spawn", "activeBefore": 7, "activeAfter": 14},
        {"sec": 429.747, "kind": "spawn", "activeBefore": 14, "activeAfter": 21},
        {"sec": 489.747, "kind": "spawn", "activeBefore": 21, "activeAfter": 30},
        {"sec": 530.835, "kind": "spawn", "activeBefore": 30, "activeAfter": 39},
    ]


def _rupture_rows(sec, per_player, players=12):
    rows = []
    for p in range(players):
        for n in range(per_player):
            rows.append({
                "event": "SPELL_AURA_APPLIED" if n == 0 else "SPELL_AURA_APPLIED_DOSE",
                "spellID": RUPTURE, "sec": round(sec + 0.001 * n, 3),
                "destGUID": f"Player-{p}", "destName": f"P{p}",
            })
    return rows


def _cleanup(rows):
    orb = {
        "currentActiveOrbs": 39,
        "ledger": _ledger_kill(),
        "guillotineStates": [{"guillotineIndex": 2, "sec": 128.005, "remainingOrbs": 1}],
    }
    return E._coiled_post_g2_cleanup_signal(
        rows, RULE, orb, {"entrySec": 160.4}, {}, STRATEGY, source_kind="local",
    )


def test_p2_entry_uses_ledger_at_entry_not_fight_end():
    out = _cleanup(_rupture_rows(160.837, 7))
    assert out["activeBeforeCleanup"] == 7
    assert out["remainingOrbs"] == 0
    assert out["status"] == "success"


def test_p2_entry_partial_cleanup_still_fails():
    out = _cleanup(_rupture_rows(160.837, 5))
    assert out["activeBeforeCleanup"] == 7
    assert out["remainingOrbs"] == 2
    assert out["status"] == "failed"


def _zuljan_zero_rows(sec):
    return [
        {"event": "SPELL_DAMAGE", "sec": 10.0, "targetGameID": 257911, "targetCurrentHP": 900, "targetMaxHP": 1000},
        {"event": "SPELL_DAMAGE", "sec": sec, "targetGameID": 257911, "targetCurrentHP": 0, "targetMaxHP": 1000},
    ]


def _p1(sec):
    strategy = {"phase1Transition": {"targetMinSec": 165, "targetMaxSec": 170, "boundaryToleranceSec": 0.1,
                                     "minorEarlySec": 6.0, "minorLateSec": 5.0}}
    return E._coiled_phase1_transition(_zuljan_zero_rows(sec), {}, strategy)


def test_p1_slightly_early_is_minor():
    out = _p1(160.4)
    assert out["status"] == "early" and out["severity"] == "minor"


def test_p1_guillotine_two_push_is_major():
    out = _p1(130.0)
    assert out["status"] == "early" and out["severity"] == "major"


def test_p1_in_window_and_late():
    assert _p1(167.0)["status"] == "success"
    late = _p1(173.0)
    assert late["status"] == "late" and late["severity"] == "minor"
    assert _p1(180.0)["severity"] == "major"



class CoiledJudgmentTests(unittest.TestCase):
    def test_p2_entry_uses_ledger_at_entry_not_fight_end(self):
        test_p2_entry_uses_ledger_at_entry_not_fight_end()

    def test_p2_entry_partial_cleanup_still_fails(self):
        test_p2_entry_partial_cleanup_still_fails()

    def test_p1_slightly_early_is_minor(self):
        test_p1_slightly_early_is_minor()

    def test_p1_guillotine_two_push_is_major(self):
        test_p1_guillotine_two_push_is_major()

    def test_p1_in_window_and_late(self):
        test_p1_in_window_and_late()

    def test_no_roster_plans_means_no_plan(self):
        out = E._coiled_roster_plan([], {"raidPlan": {}})
        self.assertFalse(out["matched"])
        self.assertEqual(out["candidatePlans"], [])


if __name__ == "__main__":
    unittest.main()
