"""1.0.27: 보드 아래 칸 제목 = 「최종 정리 · 날짜」 (「그날」 없음)."""
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


if __name__ == "__main__":
    unittest.main()
