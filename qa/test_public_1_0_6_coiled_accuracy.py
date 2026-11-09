"""1.0.6: GOLD 똬리 정확도 보강 따라잡기 (P1 단두대 소킹 칸 · 붕괴 뒤 실패는 첫 실수에서 제외 ·
일몰 완료 뒤 보호막 해제는 파괴 미확정 · 전환 지연 맹독성 파열 관측 · 생성 칸 자리 메우기). 이름은 전부 가짜."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import b3641_ui as B  # noqa: E402
from live.boss_mechanics import coiled_p1_closeout as C  # noqa: E402

ROW = {"encounterID": 3429, "difficultyID": 16, "result": "Wipe"}


def review(obs):
    return {"bossKey": "coiled_altar", "encounter": {"encounterID": 3429, "difficultyID": 16, "result": "Wipe"},
            "observations": obs}


def card(model, key):
    return next(c for c in model["cards"] if c.get("key") == key)


class P1Closeout(unittest.TestCase):
    def test_analyze_scope(self):
        self.assertEqual(C.analyze([], mythic=False)["status"], "not_applicable")
        out = C.analyze([], mythic=True)
        self.assertEqual(out["status"], "observed")
        self.assertEqual(out["soakRounds"], [])

    def test_soak_card(self):
        rounds = [{"index": 1, "soakCount": 5, "requiredCount": 6, "status": "failed", "executionObserved": True,
                   "executionLinkedDeathCount": 4, "evidenceLevel": "verified"}]
        c = card(B.build_try_priority_model(review({"p1Closeout": {"soakRounds": rounds}}), ROW), "p1_guillotine_soak")
        self.assertEqual((c["status"], c["value"]), ("failed", "1차 5/6명 ✕"))
        self.assertIn("직접 연계사망 4명", c["note"])

    def test_old_review_without_field(self):
        c = card(B.build_try_priority_model(review({}), ROW), "p1_guillotine_soak")
        self.assertEqual(c["status"], "unknown")

    def test_heroic_has_no_soak_card(self):
        rev = review({})
        rev["encounter"]["difficultyID"] = 15
        m = B.build_try_priority_model(rev, dict(ROW, difficultyID=15))
        self.assertFalse(any(c.get("key") == "p1_guillotine_soak" for c in m["cards"]))


class NightfallShield(unittest.TestCase):
    def test_removal_after_cast_is_not_a_break(self):
        self.assertTrue(B._nightfall_post_success_shield_removal({"breakSec": 10.0, "castSuccessSec": 9.0}))
        self.assertFalse(B._nightfall_post_success_shield_removal({"breakSec": 8.0, "castSuccessSec": 9.0}))
        self.assertFalse(B._nightfall_post_success_shield_removal({"breakSec": 8.0}))


class GuiHooks(unittest.TestCase):
    def test_hooks(self):
        src = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        self.assertIn('"p1_orbs", "p1_guillotine_soak", "mind_control"', src)
        self.assertIn("from live.raid_coiled_brief_v43 import refine_glance", src)
        self.assertIn("def _coiled_hf4_factual_glance(", src)
        self.assertIn("_visible_slot // _visible_cols", src)
        eng = (ROOT / "live" / "boss_mechanics" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("from .coiled_p1_closeout import analyze", eng)
        self.assertNotIn("\ub2c8\ub2c8\uc544\ub178", eng)
        self.assertNotIn("\ud558\ub808", eng)


if __name__ == "__main__":
    unittest.main()
