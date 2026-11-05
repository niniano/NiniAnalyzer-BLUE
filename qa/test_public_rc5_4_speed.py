"""RC5.4 (GOLD V5.2.4): 똬리 분석 속도 (결과는 그대로) · 라이브 분석 중에는 미리 계산 쉼.

실측 (복제본, 10/25 Try 24): 38초 → 24초. 10/25 24트 · 9/29 48트 기믹 결과가 패치 전과 바이트 단위로 같음.
"""
import random
import unittest
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live.boss_mechanics import engine as E


def make_rows(n=400, seed=7):
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        rows.append({"sec": round(i * 0.1 + rnd.choice([0, 0, 0.05]), 3),
                     "event": rnd.choice(["damage", "SPELL_AURA_APPLIED", "unit_died", "Interrupt", None]),
                     "spellID": rnd.choice([1, 2, 3, None, "4"]) if i % 7 else None,
                     "abilityGameID": rnd.choice([5, None]),
                     "sourceGUID": f"Player-1-{i % 9}", "sourceName": f"P{i % 9}", "sourceType": "player",
                     "destGUID": f"Player-1-{i % 5}", "destName": f"P{i % 5}"})
    rows.sort(key=lambda x: float(x.get("sec") or 0))
    return rows


class FastRows(unittest.TestCase):
    def tearDown(self):
        E._fast_end()

    def test_spell_event_window_match_plain_scan(self):
        rows = make_rows()
        plain_sp = [x for x in rows if E._row_spell_id(x) in {1, 4, 5, None}]
        plain_ev = [x for x in rows if E._event_name(x) in {"unit_died", "interrupt"}]
        plain_win = [x for x in rows if 3.0 <= float(x.get("sec") or 0) < 9.05]
        E._fast_begin(rows)
        self.assertEqual(E._fast_rows_spell(rows, {1, 4, 5, None}), plain_sp)
        self.assertEqual(E._fast_rows_event(rows, {"unit_died", "interrupt"}), plain_ev)
        self.assertEqual(E._fast_rows_between(rows, 3.0, 9.05, hi_open=True), plain_win)
        self.assertEqual(E._fast_rows_between(rows, 3.0, 9.05),
                         [x for x in rows if 3.0 <= float(x.get("sec") or 0) <= 9.05])

    def test_other_list_or_unsorted_falls_back(self):
        rows = make_rows()
        E._fast_begin(rows)
        other = list(reversed(rows))
        self.assertEqual(E._fast_rows_spell(other, {2}), [x for x in other if E._row_spell_id(x) == 2])
        E._fast_end()
        E._fast_begin(other)   # 시각순이 아님 → 창 조회는 전부 훑기
        self.assertEqual(E._fast_rows_between(other, 1.0, 2.0), [x for x in other if 1.0 <= float(x.get("sec") or 0) <= 2.0])

    def test_roster_memo_returns_fresh_copies(self):
        rows = make_rows()
        plain = E._coiled_player_roster(rows)
        E._fast_begin(rows)
        a = E._coiled_player_roster(rows)
        a["namesByKey"]["x"] = "y"
        a["names"].append("y")
        b = E._coiled_player_roster(rows)
        self.assertEqual(b, plain)

    def test_appended_rows_invalidate(self):
        rows = make_rows()
        E._fast_begin(rows)
        E._fast_rows_spell(rows, {1})
        rows.append({"sec": 999.0, "event": "damage", "spellID": 1})
        self.assertEqual(E._fast_rows_spell(rows, {1})[-1]["sec"], 999.0)


class LiveBusy(unittest.TestCase):
    def test_counter(self):
        from live import raid_review as RR
        self.assertFalse(RR.live_analysis_busy())
        RR._live_busy_add(1)
        self.assertTrue(RR.live_analysis_busy())
        RR._live_busy_add(-1)
        RR._live_busy_add(-1)          # 0 아래로 안 내려감
        self.assertFalse(RR.live_analysis_busy())


if __name__ == "__main__":
    unittest.main()
