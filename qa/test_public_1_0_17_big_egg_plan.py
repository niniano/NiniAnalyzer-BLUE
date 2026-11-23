"""BLUE 1.0.17 (GOLD V5.4.13): 울라텍 큰 알 공대 공략 = 4:18 집기 · 4:30쯤 가마솥 (이름은 가짜)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
QA = Path(__file__).resolve().parent
if str(QA) not in sys.path:
    sys.path.insert(0, str(QA))

from live import ulatek_conclusion_v1 as U
from test_public_1_0_15_big_egg_roar_and_follow import HEARTS, make


PLAN = {"id": "x", "name": "가짜 공대", "bigEggPick": 258.0, "bigEggPut": 270.0, "south": "big_first"}   # 1.0.19 공대 계획


class Plan430(unittest.TestCase):
    def line(self, put):
        meta, mech, raid, facts, raw = make(picks=4, weakness=[{"sec": put, "target": "가"}])
        return U.big_egg_timeline_line(U.big_egg_review(raw, HEARTS, [], 281.0, plan=PLAN), raw["weakness"], PLAN)

    def test_constants(self):
        self.assertEqual(U.EGG_BIG_SEC, 270)
        self.assertEqual(U.EGG_DEFECT_LATE_SEC, 271)
        self.assertEqual(U.BIG_EGG_HATCH_TO_PICK_ALERT, 84.0)

    def test_put_by_430_is_on_time(self):
        self.assertIn("✅ (계획 4:30쯤)", self.line(270.6))
        self.assertIn("✅", self.line(271.0))
        self.assertIn("⚠ 늦음", self.line(271.4))

    def test_pick_line_418(self):
        self.assertTrue(self.line(270.0).startswith("큰 알 들기 4:18 → 실제"))
        self.assertIn("4:18에 담당 4명이 집기", U.next_big_egg(PLAN))


if __name__ == "__main__":
    unittest.main()
