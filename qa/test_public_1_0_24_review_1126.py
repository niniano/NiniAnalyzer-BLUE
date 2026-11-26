"""1.0.24: 울라텍 살무사 원인 순서 · 똥물 · 킬 분노 · 똬리 역병의 절단 (이름은 전부 가짜).

1. 울라텍 살무사 원인: 살무사보다 뒤에 맞은 파도는 원인이 아님 · 알 든 채 사망 → 알 깨짐/떨어짐
2. 울라텍 똥물: 공대 절반이 이미 죽은 뒤의 똥물은 뿌리 아님
3. 울라텍 킬: 1:15 막을 수 없는 분노는 첫 실수로 안 씀
4. 똬리 막페 역병의 절단: 탱 사망 뒤 탱 전방에 다른 사람이 맞음
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import coiled_wipe_cause_v1 as W
from live import ulatek_conclusion_v1 as U

HEARTS = [{"index": 1, "startSec": 145.0, "endSec": 165.0, "startBossHpPct": 78.0, "endBossHpPct": 74.0,
           "evidenceLevel": "verified", "status": "observed"}]


def d(name, sec, spell="근접 공격", src="역병비늘 살무사"):
    return {"name": f"{name}-서버-KR", "sec": sec, "lastHit": {"spellName": spell, "sourceName": src}}


def make(*, raw=None, deaths=None, col=None, spells=None, duration=131.0, result="Wipe", fm=None, hearts=None):
    r = {"available": True, "hp": [(100.0, 81.0)]}
    r.update(raw or {})
    meta = {"durationSec": duration, "result": result, "difficultyID": 16, "tryNo": 21}
    obs = {"heartWindows": hearts or []}
    if fm:
        obs["firstVerifiedFailure"] = fm
        obs["verifiedFailures"] = [fm]
    mech = {"observations": obs, "encounter": {"result": result, "difficultyID": 16}}
    raid = {"collapse": {"startSec": col, "spells": spells or {}}} if col is not None else {}
    return meta, mech, raid, {"deaths": deaths or [], "roster": []}, r


EGG_RAW = {
    "viperSeen": {"a": 119.209, "b": 119.423, "c": 120.602},
    "shell": [{"sec": 108.7, "event": "SPELL_AURA_APPLIED", "target": "가짜하나"},
              {"sec": 113.957, "event": "SPELL_AURA_REMOVED", "target": "가짜하나"},
              {"sec": 110.0, "event": "SPELL_AURA_APPLIED", "target": "가짜둘"},
              {"sec": 120.572, "event": "SPELL_AURA_REMOVED", "target": "가짜둘"}],
    "waveHits": [{"sec": 120.574, "target": "가짜둘"}],
    "membrane": [{"sec": 113.98, "hits": 19}],
}


class ViperTrigger(unittest.TestCase):
    def test_wave_after_viper_is_not_root(self):
        c = U.viper_review(EGG_RAW)["clusters"][0]
        self.assertEqual(c["trigger"], "break")
        self.assertAlmostEqual(c["triggerSec"], 113.957)

    def test_simultaneous_wave_and_break_stays_wave(self):
        raw = dict(EGG_RAW, membrane=[{"sec": 120.58, "hits": 19}],
                   shell=[{"sec": 110.0, "event": "SPELL_AURA_APPLIED", "target": "가짜둘"},
                          {"sec": 120.572, "event": "SPELL_AURA_REMOVED", "target": "가짜둘"}],
                   viperSeen={"a": 121.5})
        self.assertEqual(U.viper_review(raw)["clusters"][0]["trigger"], "wave")

    def test_died_holding_label(self):
        out = U.build(*make(raw=EGG_RAW, col=120.0, spells={"부식의 파도": 4},
                            deaths=[d("가짜하나", 113.96, "유해한 난타", "울라텍")]))
        self.assertIn("알 든 가짜하나 사망 1:53 (유해한 난타) → 알 깨짐", out["lines"]["첫 실수"])
        self.assertEqual(out["lines"]["다음 풀"], U.NEXT["egg_died"])


class LakeGuards(unittest.TestCase):
    def lake(self, sec, died=True):
        return [{"sec": sec, "event": "SPELL_AURA_APPLIED", "target": n} for n in ("가짜하나", "가짜둘", "가짜셋")] + \
               [{"sec": sec + 5, "event": "SPELL_AURA_REMOVED", "target": n} for n in ("가짜하나", "가짜둘", "가짜셋")]

    def test_lake_after_mass_death_is_not_root(self):
        deaths = [d(f"가짜{i}", 342.0 + i, "유령의 똬리", "유령의 똬리") for i in range(12)]
        deaths += [d("가짜하나", 368.0, "죽음의 맹독", "nil"), d("가짜둘", 369.0, "죽음의 맹독", "nil")]
        out = U.build(*make(raw={"lake": self.lake(366.0)}, deaths=deaths, duration=372.0))
        self.assertNotIn("똥물", out["lines"]["첫 실수"])

    def test_lake_with_few_dead_before_stays_root(self):
        deaths = [d(n, 150.0 + i, "죽음의 맹독", "nil") for i, n in enumerate(("가짜하나", "가짜둘", "가짜셋"))]
        out = U.build(*make(raw={"lake": self.lake(148.0)}, deaths=deaths, duration=166.0))
        self.assertIn("똥물", out["lines"]["첫 실수"])


class KillRoutineRage(unittest.TestCase):
    def test_kill_rage_at_115_is_not_first_mistake(self):
        rage = {"kind": "unchecked_rage", "sec": 75.0, "label": "막을 수 없는 분노"}
        out = U.build(*make(result="Kill", fm=rage, duration=585.0))
        self.assertEqual(out["lines"]["첫 실수"], "확정된 첫 실수 없음")


class CoiledCleave(unittest.TestCase):
    def test_tank_death_then_cleave(self):
        mech = {"observations": {"phase1Transition": {"entrySec": 128.0}, "phase2": {"endSec": 257.0}}}
        raid = {"collapse": {"startSec": 393.0, "spells": {"역병의 절단": 3}, "count": 3}}
        facts = {"roster": [{"name": "가짜탱-서버-KR", "specID": 250}, {"name": "가짜곰-서버-KR", "specID": 104},
                            {"name": "가짜딜-서버-KR", "specID": 71}],
                 "deaths": [{"name": "가짜탱-서버-KR", "sec": 390.8, "lastHit": {"spellName": "근접 공격"}}]
                 + [{"name": f"{n}-서버-KR", "sec": 395.2, "lastHit": {"spellID": 1307292, "spellName": "역병의 절단"}}
                    for n in ("가짜곰", "가짜딜")]}
        meta = {"encounterID": 3429, "difficultyID": 16, "durationSec": 397.0, "result": "Wipe", "tryNo": 1}
        r = W.build(meta, mech, raid, facts, {"available": False})
        self.assertIn("탱 가짜탱 사망 6:30 → 역병의 절단(탱 전방)", r["lines"]["전멸 원인"])
        self.assertIn("탱 아님 가짜딜", r["lines"]["전멸 원인"])
        self.assertIn("줄잔 탱 전방", r["lines"]["다음 풀"])


if __name__ == "__main__":
    unittest.main()
