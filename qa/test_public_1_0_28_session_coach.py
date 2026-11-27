"""1.0.28: 레이드 세션 「오늘의 교정」 = 그날 똬리 · 울라텍 보완사항 (가짜 이름)."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _app(test):
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        test.skipTest("no screen")
    from nini_wcl_gui import App
    return App(edition="raid")


class SessionCoachTests(unittest.TestCase):
    def _app(self):
        return _app(self)

    def test_coach_lists_todo_per_boss(self):
        from live import day_board as db
        app = self._app()
        try:
            app.boss_data = []
            rows = [{"encounterID": 3429, "storageDir": "/s/c1"}, {"encounterID": 3492, "storageDir": "/s/u1"},
                    {"encounterID": 3513, "storageDir": "/s/k1"}]
            st = db._st(app)
            kc = db._key(3429, "2026-01-05", [db._row_dir({"storageDir": "/s/c1"})])
            ku = db._key(3492, "2026-01-05", [db._row_dir({"storageDir": "/s/u1"})])
            st["results"][kc] = {"tries": 3, "todo": ["통곡 차단 순번"], "summary": [], "rows": {}, "repeats": []}
            with mock.patch.object(db, "_start") as start:
                db.fill_session_coach(app, rows, "2026-01-05")
                start.assert_called_once()            # 울라텍만 계산 시작 (똬리는 이미 있음 · 다른 보스는 안 함)
            st["jobs"][ku] = {"i": 1, "n": 1, "new": 1, "t0": 0, "done": False, "res": None}
            db._coach_redraw(app)
            txt = app.coach.get("1.0", "end")
            self.assertIn("똬리의 제단 · 3트", txt)
            self.assertIn("1. 통곡 차단 순번", txt)
            self.assertIn("멈춘 거 아님", txt)
            st["jobs"].pop(ku)
            st["results"][ku] = {"tries": 2, "todo": [], "summary": [], "rows": {}, "repeats": []}
            db._coach_redraw(app)
            self.assertIn("울라텍 · 2트\n따로 고칠 것 없음", app.coach.get("1.0", "end"))
        finally:
            app.destroy()

    def test_personal_coaching_wins_and_empty_session(self):
        from live import day_board as db
        app = self._app()
        try:
            app.boss_data = []
            db.fill_session_coach(app, [{"encounterID": 3513, "storageDir": "/s/k1"}], "2026-01-05")
            self.assertIn("보완사항을 만드는 보스", app.coach.get("1.0", "end"))
            app.coach.delete("1.0", "end")
            app.coach.insert("end", "개인 교정")
            app.boss_data = [{"boss": "x"}]
            with mock.patch.object(db, "_start") as start:
                db.fill_session_coach(app, [{"encounterID": 3429, "storageDir": "/s/c1"}], "2026-01-05")
                start.assert_not_called()
            self.assertEqual(app.coach.get("1.0", "end").strip(), "개인 교정")
        finally:
            app.destroy()



if __name__ == "__main__":
    unittest.main()
