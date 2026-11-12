"""BLUE 1.0.9 (다음 버전): 개인 임무는 players 중 이 풀에 있는 캐릭터로 · 아무도 없으면 개인 임무 줄 없음 ·
4개 다 다른 사람이 들었으면 「→ 4:24 게걸스러운」 안 붙임. 이름은 가짜."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import ulatek_conclusion_v1 as U

HEARTS = [{"index": 1, "startSec": 145.5, "endSec": 165.0}]
DUTY = {"player": "가", "players": ["가", "나"], "bigEggBySec": 255}


def raw(big=()):
    return {"available": True, "bigEggHatch": [174.05], "shell": [],
            "bigEgg": [{"sec": s, "event": e, "target": n} for s, e, n in big]}


class DutyPlayer(unittest.TestCase):
    def test_pick_present_alt(self):
        self.assertEqual(U.duty_player(DUTY, ["나-서버", "다-서버"]), "나")
        self.assertEqual(U.duty_player(DUTY, ["가-서버", "나-서버"]), "가")

    def test_none_present(self):
        self.assertIsNone(U.duty_player(DUTY, ["다-서버"]))
        be = U.big_egg_review(raw(), HEARTS, [], 280.0, DUTY, ["다-서버"])
        self.assertNotIn("알방:", be["line"])
        self.assertNotIn("duty", be)

    def test_no_roster_old_behavior(self):
        self.assertEqual(U.duty_player({"player": "가"}, None), "가")
        be = U.big_egg_review(raw(), HEARTS, [], 280.0, {"player": "가", "bigEggBySec": 255})
        self.assertTrue(be["line"].startswith("가 알방:"))

    def test_alt_line(self):
        r = raw(big=[(252.0, "SPELL_AURA_APPLIED", "나"), (262.0, "SPELL_AURA_REMOVED", "나")])
        be = U.big_egg_review(r, HEARTS, [], 280.0, DUTY, ["나-서버"])
        self.assertTrue(be["line"].startswith("나 알방: 첫 알 안 듦 · 큰 알 4:12 ✓"))

    def test_all_four_by_others(self):
        r = raw(big=[(254.0 + i, "SPELL_AURA_APPLIED", n) for i, n in enumerate("다라마바")])
        be = U.big_egg_review(r, HEARTS, [], 300.0, DUTY, ["가-서버", "다-서버"])
        self.assertIn("가 알방: 첫 알 안 듦 · 큰 알 안 듦 (기한 4:15) · 큰 알 4개 다른 사람이 듦", be["line"])
        self.assertNotIn("4:24 게걸스러운", be["line"])


if __name__ == "__main__":
    unittest.main()
