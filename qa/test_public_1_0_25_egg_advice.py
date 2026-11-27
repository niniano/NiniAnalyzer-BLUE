"""1.0.25: 알 든 채 사망 조언 — 알 운반자 힐·생존기 우선 까지만 (알은 특임이 정해져 있음)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import coiled_wipe_cause_v1 as W  # noqa: E402
from live import ulatek_conclusion_v1 as U  # noqa: E402


class EggAdvice(unittest.TestCase):
    def test_no_passing_egg(self):
        self.assertNotIn("넘기기", U.NEXT["egg_died"])
        self.assertTrue(U.NEXT["egg_died"].endswith("알 운반자 힐·생존기 우선."))



class SoakCount(unittest.TestCase):
    def test_immune_and_miss_count(self):
        h = lambda who, missed=False, mt=None: {"sec": 512.0, "spellID": W.P3_SOAK, "target": who, "missed": missed, "missType": mt}
        raw = {"available": True, "immunity": [],
               "guillotines": [{"sec": 507.0, "event": "SPELL_AURA_APPLIED", "target": "가짜하나", "set": "p3"},
                               {"sec": 512.0, "event": "SPELL_AURA_REMOVED", "target": "가짜하나", "set": "p3"}],
               "guillotineHits": [h("가짜하나", True, "IMMUNE"), h("가짜둘"), h("가짜셋", True, "MISS"), h("가짜넷", True, "ABSORB")]}
        g = W.guillotine_detail(raw, "p3")[0]
        self.assertEqual(len(g["soakers"]), 4)


if __name__ == "__main__":
    unittest.main()
