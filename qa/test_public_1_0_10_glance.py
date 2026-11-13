"""BLUE 1.0.10 (GOLD V5.4.4): 「한눈에 보기」 — 무너진 순서 칩 · 숫자 카드 · 다음 풀 한 줄 (모든 보스) · 5줄 문장은 「자세히」 안. 이름은 가짜."""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import v544_glance as G

ULATEK = {
    "version": "ULATEK-CONCLUSION-V2.0", "durationSec": 145.0,
    "lines": {
        "어디까지": "1페 · 0심장 · 2:25 전멸 · 보스 HP 80.1%",
        "첫 실수": "1페 1:53 알 든 가 부식의 파도 1:53 → 살무사 1:53 [확정]",
        "전멸 원인": "알 든 가 부식의 파도 1:53 → 살무사 1마리 1:53 → 붕괴 유령의 똬리 3명 (2:12) · 붕괴 전 사망 3명 (첫 가 1:54) [확정]",
        "딜·힐": "심장 전 전멸 · 보스 HP 80.1% · 같은 시각 다른 공대 전멸 HP 중앙 80% (우리 +0%p)",
        "다음 풀": "알 든 사람은 파도 피하기가 최우선 (꼬리 파도 방향 미리 · 이동기 아끼기) · 알끼리 3미터.",
    },
    "plan": {"eggCarry": {"rounds": [{"bad": 0}, {"bad": 3}]},
             "viper": {"waveCarriers": ["가", "나"], "stuns": [{"sec": 129.0, "stunned": ["다"]}]}},
}
FACTS = {"deaths": [{"name": "가-서버", "sec": 114.0}] + [{"name": f"x{i}-서버", "sec": 130.0 + i} for i in range(19)]}


class Glance(unittest.TestCase):
    def test_ulatek_chain(self):
        g = G.build(ULATEK, FACTS, "Wipe")
        ch = g["chain"]
        self.assertEqual([c.get("t") for c in ch], ["1:53", "1:53", "2:12", "2:25"])
        self.assertEqual(ch[0]["title"], "첫 실수")
        self.assertEqual(ch[0]["text"], "알 든 가 부식의 파도")       # 시각은 칩 위 작은 글로
        self.assertEqual(ch[0]["conf"], "확정")
        self.assertEqual(ch[1]["text"], "살무사 1마리")
        self.assertEqual(ch[2]["text"], "유령의 똬리 3명")             # 「붕괴」 · 괄호 시각 빠짐
        self.assertEqual(ch[-1]["text"], "전멸")

    def test_ulatek_stats_and_next(self):
        g = G.build(ULATEK, FACTS, "Wipe")
        st = {c["k"]: c for c in g["stats"]}
        self.assertEqual(list(st), ["어디까지", "보스 HP", "사망", "알 운반", "살무사 스턴"])
        self.assertEqual(st["어디까지"]["v"], "1페")
        self.assertEqual(st["보스 HP"]["v"], "80.1%")
        self.assertIn("+0%p", st["보스 HP"]["s"])
        self.assertEqual(st["사망"]["v"], "20명")
        self.assertEqual(st["사망"]["s"], "붕괴 전 3명")
        self.assertEqual(st["알 운반"]["v"], "문제 3")
        self.assertEqual(st["살무사 스턴"]["v"], "1명")
        self.assertEqual(g["next"], "알 든 사람은 파도 피하기가 최우선")   # 괄호 · 뒷부분은 「자세히」
        self.assertEqual(g["nextLabel"], "다음 풀 · 한 가지만")

    def test_coiled_section_name_skipped(self):
        out = {"version": "COILED-WIPE-CAUSE-V3.8", "durationSec": 388.0, "bossHpPct": 43.0,
               "guillotines": [{"executeHit": []}, {"executeHit": ["가"]}],
               "stages": [{"name": "말라크라스", "start": 129.0}],
               "lines": {"어디까지": "사이페(영혼결속) 이후 · 6:28 전멸 (P2 진입 2:09, 사이페 4:50) · 보스 HP 줄잔 27.8%",
                         "첫 실수": "1:43 P1 글레이브 피격 사망 · 가",
                         "전멸 원인": "사이페 이후 · 영원한 일몰 15명 (보호막 못 깸, 6:22) [추정] · 보호막 2번 6:05",
                         "딜·힐": "P1 딜 체크 통과", "다음 풀": "황혼의 장막 딜 집중·쿨기 배분 확인; 첫 실수도 같이."}}
        g = G.build(out, {"deaths": []}, "Wipe")
        self.assertEqual(g["chain"][1]["t"], "6:22")
        self.assertTrue(g["chain"][1]["text"].startswith("영원한 일몰 15명"))
        st = {c["k"]: c for c in g["stats"]}
        self.assertEqual(st["어디까지"]["v"], "사이페 이후")
        self.assertEqual(st["단두대"]["v"], "2번")
        self.assertEqual(st["단두대"]["s"], "처형 1명")
        self.assertEqual(st["P2 진입"]["v"], "2:09")
        self.assertEqual(g["next"], "황혼의 장막 딜 집중·쿨기 배분 확인")

    def test_kill_no_chain(self):
        out = {"version": "BOSS-GUIDES-V1", "durationSec": 271.0, "labels": {"다음 풀": "다음 주"},
               "lines": {"어디까지": "처치 4:31", "첫 실수": "사망 없음", "전멸 원인": "처치 · 사망 없음",
                         "딜·힐": "처치 4:31 (우리 최고 5:47 · 다른 공대 킬 4:16~5:57) · 의식 진입 1:25 (다른 공대 1:14~1:54) · 사망 0명",
                         "다음 풀": "사망 없이 더 빠른 킬: 같은 단계 시각을 다른 공대와 비교 (WCL 자료 받은 뒤)."}}
        g = G.build(out, {"deaths": []}, "Kill")
        self.assertEqual(g["chain"], [])
        st = {c["k"]: c for c in g["stats"]}
        self.assertEqual(st["결과"]["v"], "킬 4:31")
        self.assertEqual(st["우리 최고"]["v"], "5:47")
        self.assertEqual(st["우리 최고"]["s"], "다른 공대 킬 4:16~5:57")
        self.assertEqual(st["첫 사망"]["v"], "없음")
        self.assertEqual(st["의식 진입"]["v"], "1:25")
        self.assertEqual(g["nextLabel"], "다음 주 · 한 가지만")

    def test_no_first_mistake(self):
        out = dict(ULATEK, lines=dict(ULATEK["lines"], **{"첫 실수": "확정된 첫 실수 없음",
                                                          "전멸 원인": "유체 광폭 산성 배출 → 산성 배출 10명 (4:33) [추정]"}))
        g = G.build(out, FACTS, "Wipe")
        self.assertEqual(g["chain"][0]["title"], "뿌리")
        self.assertEqual(g["chain"][0]["text"], "유체 광폭 산성 배출")

    def test_excluded(self):
        self.assertIsNone(G.build({"lines": {"어디까지": "가짜/리셋 풀 (0.0초)"}}, {}, "Wipe"))
        self.assertIsNone(G.build({"excluded": True, "lines": {"어디까지": "1페"}}, {}, "Wipe"))


class Panel(unittest.TestCase):
    def test_glance_and_details_toggle(self):
        if sys.platform not in {"win32", "darwin"} and not os.environ.get("DISPLAY"):
            self.skipTest("no display")
        import tkinter as tk
        from live import v43_layout as V
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        try:
            class App:
                pass
            app = App()
            app._v43 = {"five_box": None}
            app.v40_overview_box = tk.Frame(root)
            app.v40_overview_box.pack(fill="both", expand=True)
            box, rows, stages = V._five_box(app)
            box.pack(fill="x")
            glance, toggle, details = app._v43["five_glance"]
            V._show_glance(app, G.build(ULATEK, FACTS, "Wipe"))
            root.update()
            self.assertTrue(glance.winfo_ismapped())
            self.assertTrue(toggle.winfo_ismapped())
            self.assertFalse(details.winfo_ismapped())             # 5줄 문장은 접힘
            self.assertIn("자세히 펼치기", toggle.cget("text"))
            V._toggle_details(app)
            root.update()
            self.assertTrue(details.winfo_ismapped())
            self.assertIn("자세히 접기", toggle.cget("text"))
            V._show_glance(app, None)                                # 판정 제외 · 계산 중 → 예전 5줄
            root.update()
            self.assertFalse(glance.winfo_ismapped())
            self.assertFalse(toggle.winfo_ismapped())
            self.assertTrue(details.winfo_ismapped())
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
