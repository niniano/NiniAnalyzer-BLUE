"""BLUE 1.0.12 (GOLD V5.4.6): 어그로를 끌지 않는 주문(정신의 위안 등)은 풀러(선풀) 근거가 아니다. 이름은 가짜."""
import json
import tempfile
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live.raid_facts import extract_raid_facts, _non_aggro_spell

ENC = {
    "encounterID": 999, "name": "Test", "difficultyID": 16, "difficultyName": "신화", "groupSize": 3, "tryNo": 1,
    "result": "WIPE", "durationSec": 5.0,
    "startTimestamp": "9/28/2026 12:00:00.0000", "endTimestamp": "9/28/2026 12:00:05.0000",
}


def facts_for(pre_lines):
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        raw = folder / "raw.log"
        raw.write_text("\n".join([
            '9/28/2026 12:00:00.0000  ENCOUNTER_START,999,"Test",16,3,1',
            '9/28/2026 12:00:00.2000  SPELL_DAMAGE,Player-2,"Dps",0x514,0x0,Creature-1,"Boss",0x40,0x0,333,"Later",0x1,100',
            '9/28/2026 12:00:05.0000  ENCOUNTER_END,999,"Test",16,3,0',
        ]), encoding="utf-8")
        (folder / "preroll.log").write_text("\n".join(pre_lines) + "\n", encoding="utf-8")
        (folder / "meta.json").write_text(json.dumps({
            "prerollFile": "preroll.log", "prerollSec": 6.0, "prerollWindowSec": 10.0,
            "prerollLineCount": len(pre_lines), "prerollBoundaryReason": "monitor-start",
        }), encoding="utf-8")
        return extract_raid_facts(raw, ENC)


SOOTHE = [
    '9/28/2026 11:59:55.3000  SPELL_CAST_SUCCESS,Player-3,"Priest",0x514,0x0,Creature-1,"Boss",0x40,0x0,453,"정신의 위안",0x20',
    '9/28/2026 11:59:55.3000  SPELL_AURA_APPLIED,Player-3,"Priest",0x514,0x0,Creature-1,"Boss",0x40,0x0,453,"정신의 위안",0x20,DEBUFF',
]
TAUNT = '9/28/2026 11:59:59.8000  SPELL_AURA_APPLIED,Player-1,"Tank",0x514,0x0,Creature-1,"Boss",0x40,0x0,56222,"어둠의 명령",0x1,DEBUFF'


class NonAggroPull(unittest.TestCase):
    def test_helper(self):
        self.assertTrue(_non_aggro_spell(453, "아무 이름"))
        self.assertTrue(_non_aggro_spell(None, '"정신의 위안"'))
        self.assertTrue(_non_aggro_spell("x", "Mind Soothe"))
        self.assertFalse(_non_aggro_spell(56222, "어둠의 명령"))

    def test_soothe_is_not_the_puller(self):
        first = facts_for(SOOTHE + [TAUNT])["pullStart"]["firstObservedAttack"]
        self.assertEqual(first["playerName"], "Tank")
        self.assertAlmostEqual(first["sec"], -0.2, places=3)

    def test_soothe_only_falls_back_to_first_real_hit(self):
        ps = facts_for(SOOTHE)["pullStart"]
        self.assertEqual(ps["firstObservedAttack"]["playerName"], "Dps")
        self.assertFalse(ps["pullerConfirmed"])


if __name__ == "__main__":
    unittest.main()
