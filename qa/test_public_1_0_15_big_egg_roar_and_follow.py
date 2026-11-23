"""BLUE 1.0.15 (GOLD V5.4.11): 울라텍 큰 알 → 게걸스러운 파멸비늘 → 공포의 포효 연쇄 · 붕괴 뒤 절규 · 근접 공격 출처 ·
큰 알 NSRT 알림 기준 한 줄 · 감시 중 마지막 트라이 따라가기 (이름은 가짜)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import ulatek_conclusion_v1 as U

HEARTS = [{"index": 1, "startSec": 145.0, "endSec": 165.0, "startBossHpPct": 78.0, "endBossHpPct": 63.0,
           "evidenceLevel": "verified", "status": "observed"}]


def make(*, picks=3, roar=None, fm=None, spells=None, deaths=None, col=274.0, duration=281.0, weakness=None):
    big = []
    names = ["가", "나", "다", "라"][:picks]
    for i, n in enumerate(names):
        big.append({"sec": 252.0 + i, "event": "SPELL_AURA_APPLIED", "target": n})
        big.append({"sec": 268.0 + i, "event": "SPELL_AURA_REMOVED", "target": n})
    raw = {"available": True, "bigEggHatch": [174.0], "bigEgg": big, "shell": [], "hp": [(270.0, 63.0)],
           "roar": roar or [], "weakness": weakness or []}
    meta = {"durationSec": duration, "result": "Wipe", "difficultyID": 16, "tryNo": 7}
    obs = {"heartWindows": HEARTS}
    if fm:
        obs["firstVerifiedFailure"] = fm
        obs["verifiedFailures"] = [fm]
    mech = {"observations": obs, "encounter": {"result": "Wipe", "difficultyID": 16}}
    raid = {"collapse": {"startSec": col, "spells": spells or {}}}
    facts = {"deaths": deaths or [], "roster": []}
    return meta, mech, raid, facts, raw


class RoarChain(unittest.TestCase):
    def test_unpicked_egg_roar_is_first_and_cause(self):
        cry = {"kind": "anguished_cry", "sec": 281.0, "label": "고통스러운 절규 미차단"}
        out = U.build(*make(picks=3, roar=[{"sec": 272.6, "hits": 19}], fm=cry, spells={"공포의 포효": 13}))
        self.assertIn("큰 알 1개 안 집음 (마지막 알림 4:19) → 4:24 게걸스러운 파멸비늘 → 공포의 포효 19명 4:32 [확정]",
                      out["lines"]["첫 실수"])
        self.assertEqual(out["cause"]["code"], "BIG_EGG_ROAR")
        self.assertEqual(out["cause"]["confidence"], "verified")
        self.assertEqual(out["firstKind"], "big_egg_unpicked")
        self.assertEqual(out["cause"]["next"], U.NEXT["big_egg_unpicked"])

    def test_small_roar_does_not_link(self):
        out = U.build(*make(picks=2, roar=[{"sec": 262.3, "hits": 1}], spells={"산성 배출": 4}))
        self.assertNotEqual(out["cause"]["code"], "BIG_EGG_ROAR")

    def test_all_picked_no_chain(self):
        out = U.build(*make(picks=4, roar=[{"sec": 272.6, "hits": 19}], spells={"공포의 포효": 13}))
        self.assertNotEqual(out["cause"]["code"], "BIG_EGG_ROAR")


class CryAfterCollapse(unittest.TestCase):
    def test_cry_after_collapse_is_result(self):
        cry = {"kind": "anguished_cry", "sec": 281.0, "label": "고통스러운 절규 미차단"}
        out = U.build(*make(picks=4, fm=cry, spells={"근접 공격": 3}))
        self.assertIn("고통스러운 절규 미차단 4:41는 붕괴가 시작된 뒤라 결과로 봄", out["lines"]["첫 실수"])
        self.assertTrue(out["lines"]["첫 실수"].startswith("붕괴(4:34) 20초 안 확정 실패 없음"))

    def test_cry_before_collapse_stays_first(self):
        cry = {"kind": "anguished_cry", "sec": 270.0, "label": "고통스러운 절규 미차단"}
        out = U.build(*make(picks=4, fm=cry, spells={"고통스러운 절규": 4}))
        self.assertIn("4:30 고통스러운 절규 미차단", out["lines"]["첫 실수"])


class MeleeSource(unittest.TestCase):
    def test_ooze_melee_named_and_advice(self):
        deaths = [{"name": "가-서버", "sec": 275.0, "lastHit": {"spellName": "근접 공격", "sourceName": "역병비늘 유체"}},
                  {"name": "나-서버", "sec": 276.0, "lastHit": {"spellName": "근접 공격", "sourceName": "역병비늘 유체"}},
                  {"name": "다-서버", "sec": 277.0, "lastHit": {"spellName": "근접 공격", "sourceName": "약화된 파멸비늘"}}]
        rage = {"kind": "unchecked_rage", "sec": 75.0, "label": "막을 수 없는 분노"}
        out = U.build(*make(picks=4, fm=rage, spells={"근접 공격": 3}, deaths=deaths))
        self.assertIn("역병비늘 유체 근접 공격 3명 중심 붕괴 (4:34)", out["lines"]["전멸 원인"])
        self.assertEqual(out["cause"]["code"], "OOZE_MELEE")
        self.assertEqual(out["cause"]["next"], U.NEXT["ooze_melee"])

    def test_far_first_failure_not_used_for_advice(self):
        deaths = [{"name": "가-서버", "sec": 275.0, "lastHit": {"spellName": "근접 공격", "sourceName": "약화된 파멸비늘"}}]
        rage = {"kind": "unchecked_rage", "sec": 75.0, "label": "막을 수 없는 분노"}
        out = U.build(*make(picks=4, fm=rage, spells={"근접 공격": 1}, deaths=deaths))
        self.assertIn("약화된 파멸비늘 근접 공격 1명", out["lines"]["전멸 원인"])
        self.assertNotEqual(out["cause"]["next"], U.NEXT["unchecked_rage"])


class BigEggLine(unittest.TestCase):
    def test_alert_line(self):
        meta, mech, raid, facts, raw = make(picks=4, weakness=[{"sec": 268.4, "target": "가"}])
        be = U.big_egg_review(raw, HEARTS, [], 281.0)
        line = U.big_egg_timeline_line(be, raw["weakness"])
        # 1.0.17: 공대 공략 = 4:18 집기 · 4:30쯤 넣기 (4:31 넘으면 늦음)
        self.assertEqual(line, "큰 알 들기 4:12~4:15 · 마지막 알림 4:19 · 부화 4:24 · 넣음 4:28~4:31")   # 1.0.19: 공대 계획이 없으면 들기 시각 없음
        out = U.build(meta, mech, raid, facts, raw)
        self.assertTrue(any(x.startswith("알     큰 알 들기 4:12~4:15") for x in out["extraLines"]))


class FollowLatest(unittest.TestCase):
    def test_facts_ready_follows_latest(self):
        src = Path(__file__).resolve().parents[1].joinpath("nini_wcl_gui.py").read_text(encoding="utf-8")
        block = src[src.index('elif kind == "facts_ready":'):src.index('elif kind == "facts_error":')]
        self.assertIn("self._select_live_row_by_key(completed_key)", block)
        self.assertIn('getattr(self, "_backfill_running", False)', block)
        self.assertLess(block.index("self._select_live_row_by_key(completed_key)"),
                        block.index('selected_key = getattr(self, "live_selected_key", None)'))


if __name__ == "__main__":
    unittest.main()
