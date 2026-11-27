"""BLUE 1.0.22: 울라텍 「똥물에 빠짐 (죽음의 맹독)」 · 붕괴 직전 사망 순서 · 독성 배양은 탱 몫. 이름은 전부 가짜."""
from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
QA = Path(__file__).resolve().parent
if str(QA) not in sys.path:
    sys.path.insert(0, str(QA))

from live import ulatek_conclusion_v1 as U
from test_public_1_0_5_ulatek_viper import NOBODY, START, line, pl


def death(n, sec, spell):
    return {"name": n + "-서버-KR", "sec": sec, "lastHit": {"spellName": spell, "canonicalMechanic": spell}}


class Lake(unittest.TestCase):
    def test_scan_and_line(self):
        rows = [
            line(170.0, f'SPELL_AURA_APPLIED,{NOBODY},{pl("가짜하나")},1297338,"죽음의 맹독",0x8,DEBUFF'),
            line(171.0, f'SPELL_AURA_APPLIED,{NOBODY},{pl("가짜둘")},1297338,"죽음의 맹독",0x8,DEBUFF'),
            line(173.0, f'SPELL_AURA_REMOVED,{NOBODY},{pl("가짜둘")},1297338,"죽음의 맹독",0x8,DEBUFF'),
            line(180.0, f'SPELL_AURA_REMOVED,{NOBODY},{pl("가짜하나")},1297338,"죽음의 맹독",0x8,DEBUFF'),
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "raw.log.gz"
            with gzip.open(p, "wt", encoding="utf-8") as fh:
                fh.writelines(rows)
            raw = U.scan_raw(p, START)
        self.assertEqual(len(raw["lake"]), 4)
        lk = U.lake_review(raw, [death("가짜하나", 180.1, "죽음의 맹독")], 200.0)
        self.assertEqual([(r["name"], r["dur"], r["died"]) for r in lk], [("가짜하나", 10.0, 180.1), ("가짜둘", 2.0, None)])
        t = U.lake_line(lk)
        self.assertIn("똥물에 빠짐 (죽음의 맹독) 2명", t)
        self.assertIn("2:50 가짜하나(10초 · 똥물로 사망)", t)   # 1.0.29
        self.assertIn("똥물로 사망 1명", t)

    def test_pre_collapse(self):
        ds = [death("가짜하나", 150.0, "부식의 파도"), death("가짜둘", 178.0, "죽음의 맹독"), death("가짜셋", 181.0, "괴저 증기")]
        t = U.pre_collapse_txt(ds, 180.0)
        self.assertTrue(t.startswith("붕괴 직전 사망: 2:58 가짜둘(죽음의 맹독) · 3:01 가짜셋(괴저 증기)"))
        self.assertNotIn("가짜하나", t)
        self.assertIsNone(U.pre_collapse_txt([], 180.0))

    def test_brood_tank_and_version(self):
        self.assertTrue(U.DEATH_WINDOWS["brood"][1].startswith("탱: "))
        self.assertIn("venom_lake", U.NEXT)
        self.assertRegex((ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8"), r'PUBLIC_APP_VERSION = "1\.0\.\d+"')


class ViperHeart(unittest.TestCase):
    def test_share(self):
        self.assertEqual(U.viper_dmg_share({"dmgAll": {"100": 200.0, "300": 999.0}, "dmgViper": {"100": 50.0}}, 90, 150), 25.0)
        self.assertIsNone(U.viper_dmg_share({}, 0, 10))
        self.assertEqual(U.VIPER_DPS_SHARE_ROOT, 5.0)


if __name__ == "__main__":
    unittest.main()
