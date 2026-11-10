"""RC5.6: 2026-11-06 맹독 심연 신화 핫픽스 반영 (GOLD V5.3.3 과 같은 수정). 이름은 전부 가짜.

- 똬리 무덤구속: 영혼 파편이 2초 늦게 사라짐 → 11초가 아니라 13초 판도 있음 (로그에서 자동 판단)
- 송곳니 내장 파열 -40%: 차단 실패 뒤 3명까지 안 죽어도 붕괴 직전 끊지 못한 시전이면 [추정]
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live.boss_mechanics import engine as E  # noqa: E402


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BG = _load("live/boss_guides_v1.py", "bg_rc56")


def aura(sec, ev, who="Player-1-A"):
    return {"sec": sec, "event": ev, "spellID": E.GRAVEBOUND_DEBUFF_ID, "destGUID": who}


class Gravebound(unittest.TestCase):
    def test_duration_from_log(self):
        old = [aura(10.0, "applydebuff"), aura(21.0, "removedebuff")]
        self.assertEqual(E.gravebound_duration_from_rows(old), 11.0)
        new = [aura(10.0, "applydebuff"), aura(23.0, "removedebuff", ), aura(10.0, "applydebuff", "Player-1-B"),
               aura(15.0, "removedebuff", "Player-1-B")]
        self.assertEqual(E.gravebound_duration_from_rows(new), 13.0)
        self.assertEqual(E.GRAVEBOUND_DURATION_SEC, 11.0)            # 예전 기록 기준은 그대로

    def test_outcome_uses_duration(self):
        key = "Player-1-A"
        applied, removed = aura(10.0, "applydebuff"), aura(21.2, "removedebuff")
        # 11초 판: 11.2초에 풀림 = 시간 초과
        self.assertEqual(E._coiled_gravebound_outcome(key, applied, removed, 0, [], [], 40.0)[0], "expired")
        # 13초 판: 11.2초에 풀리고 그때 죽음 = 다른 원인 사망 (시간 초과 아님)
        death = [{"sec": 21.25, "event": "death", "destGUID": key}]
        self.assertEqual(E._coiled_gravebound_outcome(key, applied, removed, 0, [], death, 40.0, 13.0)[0], "died")
        # 13초 판: 13초에 풀림 = 시간 초과 · 12.4초에 파편 밟고 풀림 = 완료
        late = aura(23.0, "removedebuff")
        self.assertEqual(E._coiled_gravebound_outcome(key, applied, late, 0, [], [], 40.0, 13.0)[0], "expired")
        frag = [{"sec": 22.4, "event": "damage", "spellID": E.GRAVEBOUND_FRAGMENT_DAMAGE_ID, "destGUID": key}]
        self.assertEqual(E._coiled_gravebound_outcome(key, applied, aura(22.4, "removedebuff"), 2, frag, [], 40.0, 13.0)[0],
                         "complete")

    def test_ui_saved_duration(self):
        from live import b3641_ui as UI
        self.assertEqual(UI._gravebound_outcome_of({"appliedSec": 10.0, "removedSec": 21.2, "removedStackEvents": 2}), "expired")
        self.assertEqual(UI._gravebound_outcome_of({"appliedSec": 10.0, "removedSec": 21.2, "removedStackEvents": 2,
                                                   "durationSec": 13.0}), "complete")


ROSTER = [{"name": f"P{i}", "specID": 65 if i < 4 else 71} for i in range(20)]


def death(sec, name, sid):
    return {"sec": sec, "name": name, "lastHit": {"spellID": sid}}


class TwinFangs(unittest.TestCase):
    def facts(self, deaths):
        return {"encounter": {"encounterID": 3421}, "roster": ROSTER, "deaths": deaths,
                "enemyCastSummary": [{"spellID": 1308385, "event": "SPELL_CAST_SUCCESS", "count": 1, "times": [195.0]},
                                     {"spellID": 1306872, "event": "SPELL_CAST_START", "count": 3, "times": [18.0, 58.0, 98.0]}]}

    def meta(self):
        return {"encounterID": 3421, "difficultyID": 16, "durationSec": 210.0, "result": "Wipe"}

    def test_one_death_after_missed_kick_is_estimate(self):
        d = [death(196.0, "P1", 1308386)] + [death(198.0 + i * 0.4, f"P{2+i}", 1290516) for i in range(8)]
        out = BG.build(self.meta(), self.facts(d))
        line = out["lines"]["전멸 원인"]
        self.assertIn("자식 차단 실패 → 내장 파열 1명", line)
        self.assertIn("붕괴 전 끊지 못한 시전 1번", line)

    def test_three_deaths_still_confirmed(self):
        d = [death(196.0 + i * 0.2, f"P{i}", 1308386) for i in range(3)] + [death(198.0 + i * 0.4, f"P{4+i}", 1290516) for i in range(6)]
        out = BG.build(self.meta(), self.facts(d))
        self.assertIn("끊지 못한 시전 1번 (첫 3:15)", out["lines"]["전멸 원인"])

    def test_version(self):
        gui = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        self.assertIn('PUBLIC_APP_VERSION = "1.0.8"', gui)


if __name__ == "__main__":
    unittest.main()
