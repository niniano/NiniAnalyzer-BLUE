"""BLUE 1.0.11 (GOLD V5.4.5): 「한눈에 보기」 다듬기 — 둥근 칩 · 한 줄 무너진 순서 · 굵은 화살표 · 버튼 하나 · 「오피격」 → 「같이 맞음」. 이름은 가짜."""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import v544_glance as G

BASE = {
    "version": "ULATEK-CONCLUSION-V2.0", "durationSec": 101.0,
    "lines": {"어디까지": "1페 · 0심장 · 1:41 전멸 · 보스 HP 85.2%",
              "첫 실수": "원인 미확인 (알 운반 밖) 1:28 [추정]",
              "전멸 원인": "원인 미확인 (알 운반 밖) → 살무사 4마리 1:28 → 방울뱀 격돌 5명 1:34 → 2페(알방) 1:41 전멸 [추정]",
              "딜·힐": "-", "다음 풀": "알 든 사람은 파도 피하기가 최우선"},
}


class Chain(unittest.TestCase):
    def test_same_text_once_and_single_end_chip(self):
        ch = G.build(BASE, {"deaths": []}, "Wipe")["chain"]
        texts = [c["text"] for c in ch]
        self.assertEqual(texts.count("원인 미확인 (알 운반 밖)"), 1)   # 첫 실수 = 원인 첫 단계 → 한 번
        self.assertEqual(texts.count("전멸"), 1)                       # 「… 1:41 전멸」 단계는 끝 칩과 합침
        self.assertEqual(texts, ["원인 미확인 (알 운반 밖)", "살무사 4마리", "방울뱀 격돌 5명", "전멸"])

    def test_heart_prefix_kept(self):
        out = dict(BASE, lines=dict(BASE["lines"], **{"첫 실수": "확정된 첫 실수 없음",
                                                      "전멸 원인": "1심장 종료 보스 67.3% (공략 66%) → 2심장 절규 차단 실패 3:10"}))
        ch = G.build(out, {"deaths": []}, "Wipe")["chain"]
        self.assertTrue(ch[0]["text"].startswith("1심장 종료 보스 67.3%"))

    def test_name_join_spaced(self):
        out = dict(BASE, lines=dict(BASE["lines"], **{"첫 실수": "1페 2:01 알 든 가나다라·마바사아 부식의 파도 2:01 [확정]"}))
        ch = G.build(out, {"deaths": []}, "Wipe")["chain"]
        self.assertEqual(ch[0]["text"], "알 든 가나다라 · 마바사아 부식의 파도")


class Wording(unittest.TestCase):
    def test_overcall_reworded(self):
        try:
            from live import v43_layout as V
        except ImportError as exc:   # tkinter 없음
            self.skipTest(str(exc))
        self.assertEqual(V.reword("P1 탱 브레스 오피격"), "P1 탱 브레스 같이 맞음")
        self.assertEqual(V.reword("오피격 없음"), "같이 맞은 사람 없음")
        self.assertEqual(V.reword("비탱 오피격 0회"), "비탱 같이 맞음 0회")


class Panel(unittest.TestCase):
    def _root(self):
        if sys.platform not in {"win32", "darwin"} and not os.environ.get("DISPLAY"):
            self.skipTest("no display")
        import tkinter as tk
        try:
            return tk, tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))

    def test_one_row_rounded_and_header_toggle(self):
        tk, root = self._root()
        from live import v43_layout as V
        from live import ui_theme as T
        try:
            class App:
                pass
            app = App()
            app._v43 = {"five_box": None}
            app.v40_overview_box = tk.Frame(root, width=700)
            app.v40_overview_box.pack(fill="both", expand=True)
            head = tk.Frame(app.v40_overview_box)
            head.pack(fill="x")
            app.v40_toggle_btn = tk.Button(head, text="판정 상세 펼치기 ▾")
            app.v40_toggle_btn.pack(side="right")
            box, rows, stages = V._five_box(app)
            box.pack(fill="x")
            glance, toggle, details = app._v43["five_glance"]
            self.assertIs(toggle, app.v40_toggle_btn)                    # 버튼 하나 (칸 제목 오른쪽)
            V._show_glance(app, G.build(BASE, {"deaths": []}, "Wipe"))
            root.geometry("760x600")
            root.update()
            self.assertIn("자세히 펼치기", toggle.cget("text"))
            self.assertFalse(details.winfo_ismapped())
            # 무너진 순서: 칩이 모두 한 줄(같은 y) · 둥근 상자 · 화살표는 칩 높이 가운데
            row = [w for w in glance.winfo_children() if w.winfo_manager() == "pack" and w.grid_slaves()][0]
            chips = [w for w in row.grid_slaves() if isinstance(w, T.RoundBox)]
            arrows = [w for w in row.grid_slaves() if isinstance(w, tk.Canvas) and not isinstance(w, T.RoundBox)]
            self.assertEqual(len(chips), 4)
            self.assertEqual(len(arrows), 3)
            self.assertEqual(len({c.winfo_y() for c in chips}), 1)
            self.assertEqual(len({c.winfo_height() for c in chips}), 1)
            h = chips[0].winfo_height()
            for a in arrows:
                mid = a.winfo_y() + a.winfo_height() / 2
                self.assertLess(abs(mid - h / 2), 3)
            main = sorted(chips, key=lambda c: c.winfo_x())[:-1]
            ws = [c.winfo_width() for c in main]
            self.assertLessEqual(max(ws) - min(ws), 1)                   # 끝 칩 빼고 폭 같음 (나눗셈 1px)
            V._toggle_details(app)
            root.update()
            self.assertTrue(details.winfo_ismapped())
            self.assertIn("자세히 접기", toggle.cget("text"))
            V._show_glance(app, None)
            root.update()
            self.assertFalse(toggle.winfo_ismapped())                    # 펼칠 것 없음 → 버튼 숨김
            self.assertTrue(details.winfo_ismapped())
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
