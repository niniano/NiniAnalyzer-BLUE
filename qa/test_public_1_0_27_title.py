"""1.0.27: 보드 아래 칸 제목 = 「최종 정리 · 날짜」 (「그날」 없음) · 오른쪽 칸 = 「보완사항」 전부."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import day_board as db


class TitleSourceTests(unittest.TestCase):
    def test_source_has_no_old_title(self):
        s = (ROOT / "live" / "day_board.py").read_text(encoding="utf-8")
        self.assertNotIn('"그날 최종 정리', s)
        self.assertIn('f"최종 정리 · {session_date}"', s)


@unittest.skipUnless(os.name == "nt" or os.environ.get("DISPLAY"), "no screen")
class TitleTkTests(unittest.TestCase):
    def test_title_after_apply(self):
        from nini_wcl_gui import App
        app = App(edition="raid")
        try:
            pairs = [({"tryNo": 1, "storageDir": "/d/t1"}, {})]
            with mock.patch.object(db, "_start"):
                db.apply(app, 3492, pairs, "2026-01-05", {"rows": [], "repeats": [], "voiceOrders": []})
            self.assertEqual(db._st(app)["panel"]["box"].title_var.get(), "최종 정리 · 2026-01-05")
        finally:
            app.destroy()

    def test_todo_panel_shows_all(self):
        from nini_wcl_gui import App
        app = App(edition="raid")
        try:
            pairs = [({"tryNo": 1, "storageDir": "/d/t1"}, {})]
            st = db._st(app)
            key = db._key(3492, "2026-01-05", [db._row_dir({"storageDir": "/d/t1"})])
            st["results"][key] = {"tries": 1, "summary": ["1트"], "todo": ["가", "나", "다", "라", "마"], "rows": {}, "repeats": []}
            model = db.apply(app, 3492, pairs, "2026-01-05", {"rows": [], "repeats": [], "voiceOrders": ["예전"]})
            app._b3798_board_sig = None
            app._render_b3793_board(model, pairs, "2026-01-05")
            self.assertEqual(len(app.b3793_voice_list.winfo_children()), 5)
            labels = [w.cget("text") for w in st["panel"]["left"].winfo_children()]
            self.assertEqual(labels, ["· 1트"])                  # 아래 칸엔 수치만
            # 그날 정리 없는 보스는 예전처럼 3개
            app._b3798_board_sig = None
            app._render_b3793_board({"rows": [], "repeats": [], "voiceOrders": ["1", "2", "3", "4"]}, [], "x")
            self.assertEqual(len(app.b3793_voice_list.winfo_children()), 3)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
