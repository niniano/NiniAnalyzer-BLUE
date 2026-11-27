"""1.0.29: 울라텍 똥물 — 잠깐(2틱) 빠져서는 안 죽음 · 「똥물로 사망」은 마지막 피해가 죽음의 맹독일 때만 (가짜 이름)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import day_summary_v1 as ds
from live import ulatek_conclusion_v1 as U


def lk(sec, ev, n):
    return {"sec": sec, "event": "SPELL_AURA_" + ev, "target": n}


def death(n, sec, spell, sid=0):
    return {"name": n + "-서버-KR", "sec": sec, "lastHit": {"spellName": spell, "spellID": sid}}


class LakeKillTests(unittest.TestCase):
    def test_last_hit_decides(self):
        raw = {"lake": [lk(170.0, "APPLIED", "가짜하나"), lk(180.0, "REMOVED", "가짜하나"),
                        lk(171.0, "APPLIED", "가짜둘"), lk(173.0, "REMOVED", "가짜둘"),
                        lk(172.0, "APPLIED", "가짜셋"), lk(174.0, "REMOVED", "가짜셋")]}
        rows = U.lake_review(raw, [death("가짜하나", 180.1, "죽음의 맹독", 1297338), death("가짜둘", 174.0, "괴저 증기", 1)], 200.0)
        by = {r["name"]: r for r in rows}
        self.assertTrue(by["가짜하나"]["lakeKill"])
        self.assertFalse(by["가짜둘"]["lakeKill"])
        self.assertEqual(by["가짜둘"]["killedBy"], "괴저 증기")
        self.assertIsNone(by["가짜셋"]["died"])          # 2초 빠졌다 나오면 멀쩡
        t = U.lake_line(rows)
        self.assertIn("똥물로 사망 1명", t)
        self.assertIn("빠진 채 다른 피해로 사망 1명", t)
        self.assertIn("1초에 생명력 약 11%", U.NEXT["venom_lake"])
        self.assertEqual(U.LAKE_TICK_PCT, 11)

    def test_day_summary_no_lake_todo_without_lake_cause(self):
        f = {"tryNo": 1, "dur": 200.0, "kill": False, "result": "wipe", "heart1Short": False,
             "lines": {"첫 실수": "x", "다음 풀": "x"}, "cause": {"group": "공대 붕괴", "confidence": "candidate"},
             "fang": [], "bigEgg": {"picks": [], "drops": []}, "south": {},
             "lake": {"n": 5, "dead": 3, "lakeKill": 0, "short": 4, "shortKill": 0}}
        with mock.patch.object(ds, "try_facts", lambda enc, d, use_cache=True: dict(f)):
            out = ds.build_day(3492, ["/u/1"])
        self.assertTrue(any("똥물로 사망 0 · 빠진 채 다른 피해로 사망 3" in s for s in out["summary"]))
        self.assertFalse(any("똥물" in t for t in out["todo"]))
        self.assertFalse(any("잠깐 빠져도" in s for s in out["summary"]))


if __name__ == "__main__":
    unittest.main()
