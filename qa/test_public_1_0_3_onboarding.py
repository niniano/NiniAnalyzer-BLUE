"""1.0.3: 첫 실행 안내 카드 · 새 소식 카드."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import public_onboarding as ob  # noqa: E402
from public_first_run import maybe_show_first_run  # noqa: E402


class _App:
    public_version = "1.0.3"
    _public_import_running = False
    live_running = False
    _public_migration_running = False
    _public_log_dir = None

    def after(self, *_a, **_k):
        return None


class WhatsNewLogic(unittest.TestCase):
    def test_entries_since(self):
        self.assertEqual([v for v, _ in ob.entries_since("1.0.2", "1.0.3")], ["1.0.3"])
        self.assertEqual([v for v, _ in ob.entries_since("1.0.0", "1.0.3")], ["1.0.3", "1.0.2", "1.0.1"])
        self.assertEqual([v for v, _ in ob.entries_since(None, "1.0.3")], ["1.0.3", "1.0.2", "1.0.1"])
        self.assertEqual([v for v, _ in ob.entries_since(None, "1.0.2")], ["1.0.2", "1.0.1"])
        self.assertEqual(ob.entries_since("1.0.3", "1.0.3"), [])

    def test_latest_entry_is_current_version(self):
        import re
        gui = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        current = re.search(r'PUBLIC_APP_VERSION = "([^"]+)"', gui).group(1)
        self.assertEqual(ob.WHATS_NEW[0][0], current)

    def test_existing_user_sees_news_once(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            (home / "settings.json").write_text(json.dumps({"firstRunGuideShown": True}), encoding="utf-8")
            shown = []
            self.assertTrue(ob.maybe_show_whats_new(_App(), home, show_fn=lambda app, since: shown.append(since)))
            self.assertEqual(shown, ["1.0.2"])
            self.assertFalse(ob.maybe_show_whats_new(_App(), home, show_fn=lambda *a: self.fail("twice")))
            self.assertEqual(json.loads((home / "settings.json").read_text(encoding="utf-8"))["lastSeenVersion"], "1.0.3")

    def test_fresh_install_gets_welcome_not_news(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            self.assertFalse(ob.maybe_show_whats_new(_App(), home, show_fn=lambda *a: self.fail("news on fresh install")))
            self.assertTrue(maybe_show_first_run(_App(), home, ask_fn=lambda *a, **k: False))
            payload = json.loads((home / "settings.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["firstRunGuideShown"])
            self.assertEqual(payload["lastSeenVersion"], "1.0.3")
            self.assertFalse(ob.maybe_show_whats_new(_App(), home, show_fn=lambda *a: self.fail("news after welcome")))

    def test_busy_defers(self):
        app = _App()
        app._public_import_running = True
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "settings.json").write_text(json.dumps({"firstRunGuideShown": True}), encoding="utf-8")
            self.assertFalse(ob.maybe_show_whats_new(app, Path(t), show_fn=lambda *a: self.fail("while busy")))
            self.assertNotIn("lastSeenVersion", json.loads((Path(t) / "settings.json").read_text(encoding="utf-8")))


try:
    import tkinter as tk
    _root = tk.Tk()
    _root.withdraw()
    _HAS_TK = True
except Exception:   # 화면 없는 환경
    _HAS_TK = False


@unittest.skipUnless(_HAS_TK, "Tk display not available")
class Windows(unittest.TestCase):
    def setUp(self):
        self.app = tk.Toplevel(_root)
        self.app.public_version = "1.0.3"
        self.app._public_log_dir = None
        self.app._public_select_log_directory = lambda: None
        self.app._public_import_latest = lambda: None

    def tearDown(self):
        self.app.destroy()

    def test_welcome_opens_with_folder_missing(self):
        win = ob.open_welcome(self.app)
        self.assertTrue(win.winfo_exists())
        win.destroy()

    def test_welcome_folder_found(self):
        with tempfile.TemporaryDirectory() as t:
            logs = Path(t) / "Logs"
            logs.mkdir()
            (logs / "WoWCombatLog-110926_120000.txt").write_text("x", encoding="utf-8")
            self.app._public_log_dir = logs
            ok, text = ob._folder_state(self.app)
            self.assertTrue(ok)
            self.assertIn("찾음", text)
            win = ob.open_welcome(self.app)
            win.destroy()

    def test_whats_new_window(self):
        win = ob.open_whats_new(self.app, "1.0.1")
        self.assertIsNotNone(win)
        win.destroy()


if __name__ == "__main__":
    unittest.main()
