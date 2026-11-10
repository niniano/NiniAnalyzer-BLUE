"""RC5.4: GOLD V5.2.2~V5.2.4 + 똬리 판정 V3.7 · 울라텍 판정 V1.9 (공개판).

- 트라이 화면 한 번에 그리기 · 앞뒤 트라이 미리 계산 (GOLD V5.2.2) · 라이브 분석 중엔 미리 계산 쉼 (V5.2.4)
- 영혼 3개(무덤구속) 완료/시간 초과 카드 (V5.2.3)
- 똬리 엔진 색인 (V5.2.4, 결과 같음)
- 결론 5줄: 막페 영혼결속 순서 실패 · 보호막 흡수 인원 · 무적기로 받음 (V3.7) · 울라텍 신화 공략 판정 (V1.9)
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


W = _load("rc54_coiled", "live/coiled_wipe_cause_v1.py")
U = _load("rc54_ulatek", "live/ulatek_conclusion_v1.py")


class Versions(unittest.TestCase):
    def test_gui_version(self):
        gui = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        self.assertIn('PUBLIC_APP_VERSION = "1.0.7"', gui)

    def test_judgment_modules(self):
        self.assertRegex(W.VERSION, r"^COILED-WIPE-CAUSE-V3\.([7-9]|\d\d)$")  # 1.0.7: V3.8
        self.assertRegex(U.VERSION, r"^ULATEK-CONCLUSION-V(1\.9|[2-9]\.\d+)$")  # 1.0.5: V2.0

    def test_no_personal_data_in_modules(self):
        for rel in ("live/coiled_wipe_cause_v1.py", "live/ulatek_conclusion_v1.py"):
            t = (ROOT / rel).read_text(encoding="utf-8")
            for bad in ("C:/WCLAnalyzer", "C:\\\\WCLAnalyzer", "guillotine_teams_", "raidplan_ulatek", "\ub2c8\ub2c8\uc544\ub178"):
                self.assertNotIn(bad, t, (rel, bad))


class SoulLink(unittest.TestCase):
    def test_zul_first(self):
        hp = [(100.0, 60.0), (398.0, 68.0), (428.0, 60.0), (590.0, 3.0), (597.0, 0.0), (613.0, 0.0)]
        hp2 = [(428.0, 55.0), (590.0, 15.0), (613.0, 9.7)]
        meta = {"tryNo": 1, "durationSec": 614.0, "result": "wipe"}
        mech = {"observations": {"phase1Transition": {"entrySec": 168.0}, "phase2": {"endSec": 388.0}}}
        r = W.build(meta, mech, {}, {}, {"available": True, "hp": hp, "hp2": hp2, "rez": []})
        self.assertEqual(r["cause"]["code"], "SOUL_LINK_ORDER")
        self.assertTrue(r["lines"]["다음 풀"].startswith("말라크라스 먼저"))


class AbsorbAndImmune(unittest.TestCase):
    def test_counts(self):
        h = lambda who, missed=False, mt=None: {"sec": 512.0, "spellID": W.P3_SOAK, "target": who, "missed": missed, "missType": mt}
        raw = {"available": True, "immunity": [],
               "guillotines": [{"sec": 507.0, "event": "SPELL_AURA_APPLIED", "target": "A", "set": "p3"},
                               {"sec": 512.0, "event": "SPELL_AURA_REMOVED", "target": "A", "set": "p3"}],
               "guillotineHits": [h("A"), h("B", True, "ABSORB"), h("C", True, "MISS")]}
        line = W.guillotine_lines(raw)[0]
        self.assertIn("단두대 맞은 인원 2 (보호막 흡수 1 포함) · 무적기로 받음(인원 제외) C(MISS)", line)


class RaidScanSpeed(unittest.TestCase):
    def test_prefilter_keeps_malacrass_and_rez(self):
        src = (ROOT / "live/coiled_wipe_cause_v1.py").read_text(encoding="utf-8")
        self.assertIn('mal_token, b"SPELL_RESURRECT,")', src)


class UlatekPlan(unittest.TestCase):
    def test_mythic_only(self):
        meta = {"tryNo": 1, "result": "Wipe", "durationSec": 410.0, "difficultyID": 15}
        r = U.build(meta, {"observations": {"heartWindows": []}}, {}, {}, {"available": True, "coilCasts": [336.0 + 3.7 * k for k in range(8)]})
        self.assertEqual(r["plan"], {})


class ScreenWiring(unittest.TestCase):
    def test_prefetch_pause_and_card(self):
        v43 = (ROOT / "live/v43_layout.py").read_text(encoding="utf-8")
        self.assertIn("def _prefetch(", v43)
        self.assertIn("_live_busy()", v43)
        self.assertIn("BLUE RC5: PyInstaller build has no .py next to this module", v43)
        self.assertIn('("내 캐릭터 · 그 순간의 행동", "내 캐릭터")', v43)
        ui = (ROOT / "live/b3641_ui.py").read_text(encoding="utf-8")
        self.assertIn("def _gravebound_card(", ui)
        rr = (ROOT / "live/raid_review.py").read_text(encoding="utf-8")
        self.assertIn("def live_analysis_busy(", rr)


if __name__ == "__main__":
    unittest.main()
