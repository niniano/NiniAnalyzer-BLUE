"""RC5: GOLD V4.7 display (결론 5줄 · 단계 칩 · 스킬별 시간축 · 보스 공략 판정) in the public build.

- New display modules ship and are listed as PyInstaller hidden imports.
- v43_layout finds boss modules in a frozen build (no .py next to the module).
- Personal-only data and modules stay out of the public build.
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NEW_MODULES = (
    "v43_layout", "v45_timeline", "boss_guides_v1", "coiled_wipe_cause_v1", "ulatek_conclusion_v1",
    "raid_personal_context_v35", "raid_chronology_v37", "raid_chronology_glance_v42",
    "raid_collapse_display_v38", "raid_readable_korean_v39", "raid_glance_v40",
)
PRIVATE = (
    "engine/nini_archive.py", "engine/external_wcl_report.py", "engine/elite_rogue_viewer.py",
    "live/raid_chronology_index_v37.json", "live/raid_personal_audit_v35.json", ".env",
)


class RC5Layout(unittest.TestCase):
    def test_modules_ship_and_are_hidden_imports(self):
        from build_public_windows import build_command
        args = " ".join(map(str, build_command(ROOT, python="py", data_sep=";")))
        for name in NEW_MODULES:
            self.assertTrue((ROOT / "live" / f"{name}.py").is_file(), name)
            self.assertIn(f"--hidden-import=live.{name}", args, name)

    def test_private_content_absent(self):
        for rel in PRIVATE:
            self.assertFalse((ROOT / rel).exists(), rel)

    def test_personal_panel_is_generic(self):
        v35 = importlib.import_module("live.raid_personal_context_v35")
        self.assertEqual(v35.PLAYER, "내 캐릭터")
        gui = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        self.assertIn('text="내 캐릭터 · 그 순간의 행동"', gui)
        self.assertIn('if not row or self.raid_public_mode', gui)

    def test_boss_guides_nekzali_vessel(self):
        bg = importlib.import_module("live.boss_guides_v1")
        spec = bg.SPECS[3470]
        label = str(spec["mass"][1297630][0])
        self.assertIn("각성의 그릇", label)
        # RC5.1: 키식스 (boss_guides V1.5) · 울라텍 신화 알 구간 (V1.7)
        self.assertEqual(bg.SPECS[3513]["name"], "키식스")
        src = (ROOT / "live" / "v43_layout.py").read_text(encoding="utf-8")
        self.assertIn('3513: "boss_guides_v1"', src)
        self.assertIn('"egg"', (ROOT / "live" / "ulatek_conclusion_v1.py").read_text(encoding="utf-8"))
        # RC5.2: 똬리 신화 공략 (악의적인 공명 등) · 엔진 판정 없는 보스 체크 칸
        self.assertRegex((ROOT / "live" / "coiled_wipe_cause_v1.py").read_text(encoding="utf-8"), r"COILED-WIPE-CAUSE-V3\.[3-9]")
        self.assertIn('"엔진 기믹 판정 없음" if _v49_i == 0', (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8"))

    def test_frozen_build_finds_boss_modules(self):
        try:
            L = importlib.import_module("live.v43_layout")
        except ImportError as exc:  # tkinter missing on a headless build box
            self.skipTest(str(exc))

        class App:
            _v43 = {"modules": {}}

        real = Path.is_file
        with mock.patch.object(Path, "is_file", lambda self: False if self.suffix == ".py" else real(self)):
            mod = L._module(App(), "boss_guides_v1")
        self.assertIsNotNone(mod)
        self.assertTrue(hasattr(mod, "build"))


if __name__ == "__main__":
    unittest.main()
