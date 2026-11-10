"""1.0.7 (GOLD V5.3.7): 판정 3가지 (이름은 전부 가짜).

1. 똬리: P1 단두대 처형 붕괴가 P2 진입(보스 0%)보다 1초 먼저 → P2 붕괴가 아니라 P1 단두대 실패
2. 똬리 킬 판: 사망 정리 · 단계 시각 비교 · 다음 주 (예전엔 「킬 [확정]」 · 「-」)
3. 울라텍: 심장 도중 전멸이면 그 심장의 끝 HP를 「1심장 종료 · 미달」로 읽지 않음
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import coiled_wipe_cause_v1 as W
from live import ulatek_conclusion_v1 as U


def coiled_meta(dur, result="Wipe"):
    return {"encounterID": 3429, "difficultyID": 16, "durationSec": dur, "result": result, "tryNo": 2}


def death(name, sec, spell="처형", sid=1283606):
    return {"name": f"{name}-서버-KR", "sec": sec, "lastHit": {"spellID": sid, "spellName": spell}}


class CoiledP1Origin(unittest.TestCase):
    def build(self, entry):
        mech = {"observations": {"phase1Transition": {"entrySec": entry},
                                 "guillotineMarkers": [{"index": 1, "sec": 43.0}, {"index": 2, "sec": 128.0}]}}
        raid = {"collapse": {"startSec": 133.2, "spells": {"처형": 8}, "count": 12}}
        facts = {"deaths": [death(f"사람{i}", 133.2 + i * 0.001) for i in range(8)]}
        return W.build(coiled_meta(153.0), mech, raid, facts, {"available": False})

    def test_collapse_before_entry_is_p1_guillotine(self):
        r = self.build(134.19)
        self.assertTrue(r["p1Origin"])
        self.assertEqual(r["lines"]["어디까지"], "P1 · 2:13 단두대 붕괴 → 2:33 전멸 (붕괴 뒤 2:14 보스 0%)")
        self.assertTrue(r["lines"]["전멸 원인"].startswith("2번째 단두대 실패 → 처형 8명"))
        self.assertIn("P1 딜은 충분", r["lines"]["딜·힐"])

    def test_entry_well_before_collapse_stays_p2(self):
        r = self.build(120.0)
        self.assertFalse(r["p1Origin"])
        self.assertTrue(r["lines"]["전멸 원인"].startswith("P2"))


class CoiledKill(unittest.TestCase):
    def test_kill_review(self):
        mech = {"observations": {"phase1Transition": {"entrySec": 129.2}, "phase2": {"endSec": 272.2}},
                "firstMistake": {"sec": 101.3, "label": "P1 글레이브 피격 사망", "players": ["다-서버-KR"]}}
        facts = {"deaths": [death("가", 33.4, "맹독성 파열", 1299838), death("나", 66.8, "응결된 맹독", 1282408),
                            death("다", 101.3, "유독한 지면", 1283290), death("라", 430.5, "응결된 맹독", 1282408)],
                 "roster": [{"name": "라-서버-KR", "specID": 65}]}
        r = W.build(coiled_meta(475.5, "Kill"), mech, {}, facts, {"available": False})
        L = r["lines"]
        self.assertEqual(r["labels"], {"전멸 원인": "사망 정리", "다음 풀": "다음 주"})
        self.assertEqual(L["어디까지"], "킬 7:55 · P2 진입 2:09 · 사이페 4:32")
        self.assertEqual(L["전멸 원인"], "사망 4명 · 응결된 맹독 2명 (나 1:06, 라 7:10) · 맹독성 파열 1명 (가 0:33) · 유독한 지면 1명 (다 1:41)")
        self.assertIn("P2 진입 2:09 (다른 공대 2:21~2:36)", L["딜·힐"])
        self.assertIn("킬 7:55 (다른 공대 8:42~9:47)", L["딜·힐"])
        self.assertIn("힐러 사망 라", L["딜·힐"])
        self.assertTrue(L["다음 풀"].startswith("응결된 맹독 2명 줄이기 / 유독한 지면 1명 →"))

    def test_wipe_has_no_labels(self):
        r = W.build(coiled_meta(60.0), {"observations": {}}, {}, {}, {"available": False})
        self.assertEqual(r["labels"], {})


class UlatekHeartCut(unittest.TestCase):
    def meta(self, dur):
        return {"encounterID": 3492, "difficultyID": 16, "durationSec": dur, "result": "Wipe", "tryNo": 13}

    def test_wipe_inside_heart_is_not_heart_shortfall(self):
        hw = [{"index": 1, "startSec": 145.4, "endSec": 151.6, "durationSec": 6.2, "startBossHpPct": 80.1, "endBossHpPct": 80.1}]
        r = U.build(self.meta(151.6), {"observations": {"heartWindows": hw}}, {}, {}, {"available": True, "hp": [[150.0, 80.1]]})
        self.assertEqual(r["lines"]["어디까지"], "1심장 도중 · 2:31 전멸 · 보스 HP 80.1%")
        self.assertIn("1심장 도중 전멸 (2:25 시작 · 6초 만) · 딜 판정 안 함", r["lines"]["딜·힐"])
        self.assertNotIn("미달", r["lines"]["딜·힐"])
        self.assertNotIn("1심장 66% 미달", r["lines"]["다음 풀"])
        self.assertIsNone(r["heart1EndHp"])

    def test_finished_heart_still_judged(self):
        hw = [{"index": 1, "startSec": 145.5, "endSec": 165.5, "durationSec": 20.0, "startBossHpPct": 77.5, "endBossHpPct": 67.7}]
        r = U.build(self.meta(195.2), {"observations": {"heartWindows": hw}}, {}, {}, {"available": True, "hp": [[194.0, 67.6]]})
        self.assertEqual(r["lines"]["어디까지"], "2페(알방) · 1심장 · 3:15 전멸 · 보스 HP 67.6%")
        self.assertIn("1심장 종료 67.7% (미달 +1.7%p)", r["lines"]["딜·힐"])


if __name__ == "__main__":
    unittest.main()
