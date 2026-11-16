"""BLUE 1.0.14 (GOLD V5.4.10): 무너진 순서 칩을 시각 순서로 (시각 없는 칩은 뒤 칩 시각 · 횟수 요약은 제자리). 이름은 가짜."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import v544_glance as G


def chips(first, cause):
    out = {"version": "ULATEK-CONCLUSION-V2.0", "durationSec": 286.0,
           "lines": {"어디까지": "2페 · 1심장", "첫 실수": first, "전멸 원인": cause, "딜·힐": "-", "다음 풀": "-"}}
    return [(c.get("t"), c.get("title"), c["text"]) for c in G.build(out, {"deaths": []}, "Wipe")["chain"]]


class ChainOrder(unittest.TestCase):
    def test_reversed_times_are_sorted(self):
        got = chips("2페(알방) 4:37 고통스러운 절규 미차단",
                    "유체 광폭 산성 배출 → 산성 배출 3명 (4:30) · 붕괴 전 사망 2명 (첫 가나다 2:05) [추정]")
        self.assertEqual([t for t, _, _ in got], ["4:30", "4:30", "4:37", "4:46"])
        self.assertEqual([x for _, x, _ in got][:3], ["뿌리", "", "첫 실수"])

    def test_missing_time_takes_next(self):
        got = chips("확정된 첫 실수 없음", "유체 광폭 산성 배출 → 산성 배출 2명 (4:31) [추정]")
        self.assertEqual(got[0][:2], ("4:31", "뿌리"))

    def test_count_summary_stays_in_place(self):
        got = chips("1심장 2:29 부식의 파도 사망 [추정]", "부식의 파도 사망 2:29 → 유체 광폭 산성 배출 1회 (첫 1페 1:20)")
        self.assertEqual(got[0][1], "첫 실수")
        self.assertIn("1회", got[1][2])

    def test_already_in_order_unchanged(self):
        got = chips("1페 1:56 알 든 가나다 부식의 파도 1:56 [확정]",
                    "알 든 가나다 부식의 파도 1:56 → 살무사 1마리 1:57 → 부패한 막 3명 (2:12) [확정]")
        self.assertEqual([t for t, _, _ in got], ["1:56", "1:57", "2:12", "4:46"])
        self.assertEqual(got[0][1], "첫 실수")


if __name__ == "__main__":
    unittest.main()
