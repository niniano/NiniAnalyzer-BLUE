"""1.0.6 (GOLD V5.3.0): 키식스 무효화 3중첩 즉사 묶음 (BOSS-GUIDES-V1.9). 이름은 전부 가짜.

11/04 두 킬 실측: 3중첩이 되는 순간 즉사 피해(1308877 · 약 400만)가 같이 들어와 3중첩 기록이 안 남는 경우가 많고,
면역기·보호막이면 흡수돼 살아남는다 (3중첩 기록만 남음). 예전엔 이 사망이 「무효화 2중첩」으로 묶였다.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("bg530", ROOT / "live/boss_guides_v1.py")
BG = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BG)
ROSTER = [{"name": f"P{i}", "specID": 71} for i in range(20)]
CASTS = [{"spellID": 1302884, "event": "SPELL_CAST_START", "count": 4, "times": [17.0, 125.0, 141.0, 234.0]},
         {"spellID": 1302951, "event": "SPELL_CAST_START", "count": 2, "times": [52.0, 161.0]},
         {"spellID": 1301479, "event": "SPELL_CAST_START", "count": 1, "times": [250.0]}]


def death(sec, name, sid):
    return {"sec": sec, "name": name, "lastHit": {"spellID": sid}}


class Nullify(unittest.TestCase):
    def build(self, deaths, stack):
        facts = {"encounter": {"encounterID": 3513}, "roster": ROSTER, "deaths": deaths, "enemyCastSummary": CASTS}
        rawc = {"available": True, "stack": {1308875: stack}, "tankStack": [], "fearImmune": []}
        return BG.build({"encounterID": 3513, "difficultyID": 233, "durationSec": 302.9, "result": "Kill"}, facts, [], rawc)

    def test_lethal_without_third_stack_record(self):
        out = self.build([death(281.7, "P1", 1308877), death(282.4, "P2", 1308877)],
                         {"P1": {"max": 2, "sec": 271.0}, "P2": {"max": 2, "sec": 273.0}, "P3": {"max": 3, "sec": 280.0}})
        self.assertIn("무효화 3중첩 즉사 2명", out["lines"]["전멸 원인"])
        self.assertNotIn("2중첩", out["lines"]["전멸 원인"])
        chk = next(c for c in out["checks"] if c["title"].startswith("무효화"))
        self.assertEqual(chk["value"], "3중첩 3명")   # 죽은 2명 + 면역으로 버틴 1명
        self.assertEqual(BG._label(BG.SPECS[3513], {"spellID": 1308877})[0], "무효화 중첩 즉사")

    def test_heroic_label(self):
        facts = {"encounter": {"encounterID": 3513}, "roster": ROSTER, "deaths": [death(281.7, "P1", 1308877)], "enemyCastSummary": CASTS}
        rawc = {"available": True, "stack": {1308875: {"P1": {"max": 4, "sec": 271.0}}}, "tankStack": [], "fearImmune": []}
        out = BG.build({"encounterID": 3513, "difficultyID": 15, "durationSec": 302.9, "result": "Kill"}, facts, [], rawc)
        self.assertIn("무효화 중첩 즉사 1명", out["lines"]["전멸 원인"])
        self.assertNotIn("3중첩", out["lines"]["전멸 원인"])

    def test_other_hit_keeps_real_name_with_stack(self):
        out = self.build([death(283.0, "P1", 1301479)], {"P1": {"max": 2, "sec": 271.0}})
        self.assertIn("· 무효화 2중첩 1명", out["lines"]["전멸 원인"])
        chk = next(c for c in out["checks"] if c["title"].startswith("무효화"))
        self.assertEqual(chk["value"], "2중첩 1명")

    def test_version(self):
        self.assertRegex(BG.VERSION, r"^BOSS-GUIDES-V1\.(9|\d\d)$")


if __name__ == "__main__":
    unittest.main()
