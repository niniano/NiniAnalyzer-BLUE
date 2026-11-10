"""1.0.8 (GOLD V5.3.8): 울라텍 2페(알방) 큰 알(파멸비늘 알 4개 · 파멸비늘 껍질 1300312) · 4:15 기한.\n개인 임무 줄은 GOLD 전용 파일이 있을 때만 (BLUE 는 파일 없음 → 공용 줄만).
이름은 전부 가짜."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import ulatek_conclusion_v1 as U

HEARTS = [{"index": 1, "startSec": 145.5, "endSec": 165.0, "endBossHpPct": 64.5}]


def raw(big=(), shell=(), hatch=(174.1,)):
    return {"available": True, "bigEggHatch": list(hatch),
            "bigEgg": [{"sec": s, "event": e, "target": n} for s, e, n in big],
            "shell": [{"sec": s, "event": e, "target": n} for s, e, n in shell]}


class BigEgg(unittest.TestCase):
    def test_general_line(self):
        r = raw(big=[(247.3, "SPELL_AURA_APPLIED", "가"), (249.3, "SPELL_AURA_REMOVED", "가")])
        be = U.big_egg_review(r, HEARTS, [], 258.7)
        self.assertEqual(be["deadline"], 255.1)
        self.assertEqual(be["line"], "알방 큰 알 1/4 (기한 4:15 · 4:18 전멸) · 가 4:07→4:09")
        self.assertNotIn("duty", be)

    def test_late_pick(self):
        r = raw(big=[(262.0, "SPELL_AURA_APPLIED", "나")])
        be = U.big_egg_review(r, HEARTS, [], 300.0)
        self.assertIn("늦음 나", be["line"])

    def test_no_room_reached(self):
        self.assertIsNone(U.big_egg_review(raw(), HEARTS, [], 195.0))
        self.assertIsNone(U.big_egg_review(raw(hatch=()), HEARTS, [], 300.0))

    def test_duty_first_egg_then_died(self):
        r = raw(big=[(247.3, "SPELL_AURA_APPLIED", "가")],
                shell=[(25.0, "SPELL_AURA_APPLIED", "나"), (43.8, "SPELL_AURA_REMOVED", "나"),
                       (247.5, "SPELL_AURA_APPLIED", "나"), (249.5, "SPELL_AURA_REMOVED", "나")])
        deaths = [{"name": "나-서버-KR", "sec": 249.6, "lastHit": {"spellName": "유독한 껍질"}}]
        be = U.big_egg_review(r, HEARTS, deaths, 258.7, {"player": "나", "bigEggBySec": 255})
        self.assertTrue(be["line"].startswith("나 알방: 첫 알 4:07→4:09 · 큰 알 전 사망 4:09(유독한 껍질) | 알방 큰 알 1/4"))

    def test_duty_ok_and_late(self):
        r = raw(big=[(240.0, "SPELL_AURA_APPLIED", "나")], shell=[(230.0, "SPELL_AURA_APPLIED", "나"), (234.0, "SPELL_AURA_REMOVED", "나")])
        be = U.big_egg_review(r, HEARTS, [], 300.0, {"player": "나", "bigEggBySec": 255})
        self.assertIn("첫 알 3:50→3:54 · 큰 알 4:00 ✓", be["line"])
        r = raw(big=[(262.0, "SPELL_AURA_APPLIED", "나")])
        be = U.big_egg_review(r, HEARTS, [], 300.0, {"player": "나", "bigEggBySec": 255})
        self.assertIn("첫 알 안 듦 · 큰 알 4:22 늦음 +7초", be["line"])

    def test_duty_not_picked(self):
        be = U.big_egg_review(raw(), HEARTS, [], 300.0, {"player": "나", "bigEggBySec": 255})
        self.assertIn("큰 알 안 듦 (기한 4:15)", be["line"])

    def test_note_first_in_build(self):
        meta = {"encounterID": 3492, "difficultyID": 16, "durationSec": 258.7, "result": "Wipe", "tryNo": 25}
        r = raw(big=[(247.3, "SPELL_AURA_APPLIED", "가")])
        r["hp"] = [[250.0, 64.4]]
        out = U.build(meta, {"observations": {"heartWindows": HEARTS}}, {}, {}, r)
        self.assertTrue(out["notes"][0].startswith("알방 큰 알 1/4"))   # BLUE: 개인 임무 파일 없음
        self.assertIsNone(U.load_personal_duty())
        self.assertIn("bigEgg", out["plan"])



class OnTime(unittest.TestCase):
    """4:15 이내 = 4:15.x 까지 제때."""
    def test_four_fifteen_point_six(self):
        r = raw(big=[(255.6, "SPELL_AURA_APPLIED", "가"), (256.0, "SPELL_AURA_APPLIED", "나")])
        r["bigEggHatch"] = [174.05]
        be = U.big_egg_review(r, HEARTS, [], 280.0)
        self.assertIn("늦음 나", be["line"])
        self.assertNotIn("늦음 가", be["line"])


if __name__ == "__main__":
    unittest.main()
