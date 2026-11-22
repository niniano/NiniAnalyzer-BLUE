"""BLUE 1.0.18: 울라텍 알 깨짐(공대 전체 부패한 막) → 살무사 원인 (이름은 가짜)."""
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
from test_public_1_0_5_ulatek_viper import BOSS, START, base_raw, line, mech, mythic_meta, pl, raid


def memb(sec, hits=12):
    return {"sec": sec, "hits": hits}


class MembraneScan(unittest.TestCase):
    def test_burst_grouped(self):
        rows = [line(0.0, 'ENCOUNTER_START,3492,"울라텍",16,20,2939')]
        for i in range(6):
            rows.append(line(100.0 + i * 0.02, f'SPELL_AURA_APPLIED,{BOSS},{pl("가짜" + str(i))},1301268,"부패한 막",0x8,DEBUFF'))
        rows.append(line(130.0, f'SPELL_AURA_APPLIED,{BOSS},{pl("혼자")},1301268,"부패한 막",0x8,DEBUFF'))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "raw.log.gz"
            with gzip.open(p, "wt", encoding="utf-8") as fh:
                fh.writelines(rows)
            raw = U.scan_raw(p, START)
        self.assertEqual(raw["membrane"], [{"sec": 100.0, "hits": 6}, {"sec": 130.0, "hits": 1}])


class EggBreak(unittest.TestCase):
    def test_version(self):
        self.assertEqual(U.VERSION, "ULATEK-CONCLUSION-V2.3")
        self.assertEqual(U.EGG_MEMBRANE, 1301268)

    def test_carrier_break_is_verified(self):
        raw = base_raw(shell=[{"sec": 60.0, "event": "SPELL_AURA_APPLIED", "target": "알이"},
                              {"sec": 70.6, "event": "SPELL_AURA_REMOVED", "target": "알이"}],
                       membrane=[memb(70.6)], viperSeen={"v1": 75.0})
        r = U.build(mythic_meta(90.0), mech(), raid(85.0), {}, raw)
        self.assertEqual(r["cause"]["confidence"], "verified")
        self.assertIn("알 든 알이 알 깨짐 1:10 (공대 전체 부패한 막)", r["lines"]["전멸 원인"])
        self.assertEqual(r["lines"]["다음 풀"], U.NEXT["egg_viper"])

    def test_ground_break(self):
        raw = base_raw(membrane=[memb(66.0)], viperSeen={"v1": 75.0})
        r = U.build(mythic_meta(90.0), mech(), raid(85.0), {}, raw)
        self.assertIn("아무도 안 든 알 깨짐 1:06 (공대 전체 부패한 막)", r["lines"]["전멸 원인"])
        self.assertEqual(r["lines"]["다음 풀"], U.NEXT["egg_ground"])

    def test_small_membrane_ignored(self):
        raw = base_raw(membrane=[memb(66.0, hits=2)], viperSeen={"v1": 75.0})
        r = U.build(mythic_meta(90.0), mech(), raid(85.0), {}, raw)
        self.assertIn("원인 미확인", r["lines"]["전멸 원인"])


if __name__ == "__main__":
    unittest.main()
