"""BLUE 1.0.10 (GOLD V5.4.3): 결론 칸 · 참고 줄이 글 전체 높이(픽셀)로 맞춰져 마지막 줄이 잘리지 않음 (큰 글꼴 이름이 섞인 줄)."""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FitText(unittest.TestCase):
    def test_mixed_font_lines_not_clipped(self):
        if sys.platform not in {"win32", "darwin"} and not os.environ.get("DISPLAY"):
            self.skipTest("no display")
        import tkinter as tk
        from live import v43_layout as V
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        try:
            t = tk.Text(root, height=1, wrap="word", font=("TkDefaultFont", 9), width=40, bd=0, padx=0, pady=0)
            t.tag_configure("big", font=("TkDefaultFont", 16))
            t.pack()
            for i in range(4):
                t.insert("end", "알 든 사람 부식의 파도 ", None)
                t.insert("end", "이름이름 ", "big")
                t.insert("end", "→ 살무사\n" if i < 3 else "→ 살무사", None)
            root.update()
            V._fit_text(t)
            root.update()
            ypix = int(t.tk.call(t._w, "count", "-ypixels", "1.0", "end"))
            shown = int(t.winfo_height())
            self.assertGreaterEqual(shown + 2, ypix, f"보이는 높이 {shown} < 글 높이 {ypix}")
            self.assertEqual(t.yview()[0], 0.0)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
