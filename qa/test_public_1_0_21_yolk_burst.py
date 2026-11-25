"""BLUE 1.0.21: 울라텍 「썩은 노른자 남은 채로 작은 알 집음」 · 알 충돌(맹독성 분출) 뒤 10초 안 사망. 이름은 전부 가짜."""
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
from test_public_1_0_5_ulatek_viper import BOSS, START, line, pl


class ScanYolk(unittest.TestCase):
    def test_scan_and_repick(self):
        rows = [
            line(100.0, f'SPELL_AURA_APPLIED,{BOSS},{pl("가짜알")},1307612,"유독한 껍질",0x8,DEBUFF'),
            line(110.0, f'SPELL_AURA_REMOVED,{BOSS},{pl("가짜알")},1307612,"유독한 껍질",0x8,DEBUFF'),
            line(110.0, f'SPELL_AURA_APPLIED,{BOSS},{pl("가짜알")},1312150,"썩은 노른자",0x8,DEBUFF'),
            line(124.0, f'SPELL_AURA_APPLIED,{BOSS},{pl("가짜알")},1307612,"유독한 껍질",0x8,DEBUFF'),
            line(130.0, f'SPELL_AURA_REMOVED,{BOSS},{pl("가짜알")},1312150,"썩은 노른자",0x8,DEBUFF'),
            line(140.0, f'SPELL_AURA_APPLIED,{BOSS},{pl("가짜둘")},1307635,"맹독성 분출",0x8,DEBUFF'),
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "raw.log.gz"
            with gzip.open(p, "wt", encoding="utf-8") as fh:
                fh.writelines(rows)
            raw = U.scan_raw(p, START)
        self.assertEqual([x["event"] for x in raw["yolk"]], ["SPELL_AURA_APPLIED", "SPELL_AURA_REMOVED"])
        picks = U.yolk_pick_review(raw)
        self.assertEqual([(x["name"], x["age"]) for x in picks], [("가짜알", 14.0)])
        self.assertIn("2:04 가짜알(노른자 14초째)", U.yolk_pick_line(picks))
        dead = U.egg_burst_deaths(raw, [{"name": "가짜둘-서버-KR", "sec": 141.0}, {"name": "가짜셋", "sec": 141.0}])
        self.assertEqual([n for n, _b, _d in dead], ["가짜둘"])
        self.assertIn("분출 뒤 10초 안 사망 가짜둘 2:21", U.egg_burst_line(raw, [{"name": "가짜둘", "sec": 141.0}]))

    def test_yolk_gone_no_line(self):
        raw = {"shell": [{"sec": 131.0, "event": "SPELL_AURA_APPLIED", "target": "가짜알"}],
               "yolk": [{"sec": 110.0, "event": "SPELL_AURA_APPLIED", "target": "가짜알"},
                        {"sec": 130.0, "event": "SPELL_AURA_REMOVED", "target": "가짜알"}]}
        self.assertEqual(U.yolk_pick_review(raw), [])

    def test_version(self):
        gui = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        self.assertRegex(gui, r'PUBLIC_APP_VERSION = "1\.0\.\d+"')   # 다음 버전에서도 통과


if __name__ == "__main__":
    unittest.main()
