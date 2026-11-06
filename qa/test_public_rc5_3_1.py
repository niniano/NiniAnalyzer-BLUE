"""RC5.3.1: 공개판에서 도적 전용 모듈을 뺀다 (공대장용 화면·판정은 그대로).

- engine/plan_vs_actual.py (내 캐릭터 쿨기 계획 vs 실제, 도적 버스트 가중치)
- live/historical_precision.py (GOLD 도적 정밀 분석 Fight 찾기)
둘 다 공개판 화면(레이드 세션 · 트라이 보드 · 트라이 복기)에서 쓰지 않는다.
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ROGUE_ONLY = ("engine/plan_vs_actual.py", "live/historical_precision.py",
              "engine/elite_rogue_viewer.py", "engine/external_wcl_report.py")


class RogueModulesRemoved(unittest.TestCase):
    def test_files_absent(self):
        for rel in ROGUE_ONLY:
            self.assertFalse((ROOT / rel).exists(), rel)

    def test_not_bundled(self):
        from build_public_windows import build_command
        args = " ".join(map(str, build_command(ROOT, python="py", data_sep=";")))
        self.assertNotIn("plan_vs_actual", args)
        self.assertNotIn("historical_precision", args)
        self.assertNotIn("elite_rogue", args)

    def test_gui_imports_without_them(self):
        gui = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        self.assertIn("RC5.3.1: 도적 정밀 분석 모듈은 공개판에 없다", gui)
        try:
            mod = importlib.import_module("nini_wcl_gui")
        except ImportError as exc:   # tkinter 없는 빌드 PC
            self.skipTest(str(exc))
        self.assertEqual(mod.find_fights(ROOT, "2026-10-25", "x", 3429, 16, []), [])
        self.assertFalse(mod.cache_has_fight(None, 1))
        self.assertIsNone(mod.dataset_source_log(None, None, None))
        self.assertIsNone(mod.compare_plan_vs_actual)

    def test_version(self):
        gui = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        self.assertIn('PUBLIC_APP_VERSION = "1.0.0-rc5.6"', gui)


if __name__ == "__main__":
    unittest.main()
