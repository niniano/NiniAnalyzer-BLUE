"""RC5.4 (GOLD V5.2.3): 영혼 3개(무덤구속) 완료/11초 시간 초과 판정 · 정배 카드는 참고 (판정은 현신 칸).

실측 근거 (10/25 · 9/29 신화 원본 로그): 무덤구속 1286837 은 11초. 영혼 파편을 밟을 때마다 1308330 피해 + 중첩 감소,
세 번째 파편에서 풀림. 완료 169번은 모두 10.4초 안, 시간 초과 37번은 모두 10.9~11.0초에 풀리고 대부분 그 자리에서 사망.
"""
import unittest
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live.boss_mechanics import engine as E
from live.b3641_ui import _gravebound_card, _gravebound_outcome_of


def aura(sec, who, event):
    return {"sec": sec, "event": event, "destGUID": who, "destName": who, "spellID": E.GRAVEBOUND_DEBUFF_ID}


def frag(sec, who, event="damage"):
    return {"sec": sec, "event": event, "destGUID": who, "destName": who, "spellID": E.GRAVEBOUND_FRAGMENT_DAMAGE_ID}


def died(sec, who):
    return {"sec": sec, "event": "unit_died", "destGUID": who, "destName": who}


class Outcome(unittest.TestCase):
    def states(self, rows, players=("A",), boom=100.0):
        targets = [{"destGUID": p, "destName": p} for p in players]
        return {s["player"]: s for s in E._coiled_gravebound_states(rows, targets, boom, 15.0)}

    def test_complete_when_third_fragment_removes(self):
        rows = [aura(100.0, "A", "spell_aura_applied"), frag(102.0, "A"), aura(102.0, "A", "spell_aura_removed_dose"),
                frag(103.0, "A"), aura(103.0, "A", "spell_aura_removed_dose"), frag(105.2, "A"),
                aura(105.2, "A", "spell_aura_removed"), {"sec": 130.0, "event": "damage"}]
        s = self.states(rows)["A"]
        self.assertEqual(s["outcome"], "complete")
        self.assertIn("5.2초", s["outcomeNote"])

    def test_expired_at_eleven_seconds(self):
        rows = [aura(100.0, "A", "spell_aura_applied"), frag(102.0, "A"), aura(102.0, "A", "spell_aura_removed_dose"),
                aura(111.0, "A", "spell_aura_removed"), died(111.05, "A")]
        s = self.states(rows)["A"]
        self.assertEqual(s["outcome"], "expired")
        # 예전 판정은 15초 마감 안에 풀렸다고 success 로 봤다 (이번에 고친 오판)
        self.assertEqual(s["status"], "success")

    def test_early_death_is_not_a_failure(self):
        rows = [aura(100.0, "A", "spell_aura_applied"), aura(104.0, "A", "spell_aura_removed"), died(104.02, "A")]
        self.assertEqual(self.states(rows)["A"]["outcome"], "died")

    def test_death_without_removal(self):
        rows = [aura(100.0, "A", "spell_aura_applied"), died(111.2, "A"),
                aura(100.0, "B", "spell_aura_applied"), died(108.6, "B"), {"sec": 130.0, "event": "damage"}]
        st = self.states(rows, players=("A", "B"))
        self.assertEqual(st["A"]["outcome"], "expired")
        self.assertEqual(st["B"]["outcome"], "died")

    def test_fight_ended_is_unknown(self):
        rows = [aura(100.0, "A", "spell_aura_applied"), {"sec": 105.0, "event": "damage"}]
        self.assertEqual(self.states(rows)["A"]["outcome"], "unknown")

    def test_archive_without_fragment_rows_uses_stack_count(self):
        rows = [aura(100.0, "A", "spell_aura_applied"), aura(102.0, "A", "spell_aura_removed_dose"),
                aura(103.0, "A", "spell_aura_removed_dose"), aura(105.0, "A", "spell_aura_removed")]
        self.assertEqual(self.states(rows)["A"]["outcome"], "complete")

    def test_summary(self):
        tg = [{"outcome": "complete"}, {"outcome": "expired", "player": "B-서버"}, {"outcome": "died"}]
        o = E._coiled_gravebound_summary(tg)
        self.assertEqual((o["completeCount"], o["expiredCount"], o["diedCount"], o["status"]), (1, 1, 1, "failed"))


def review(*cycles):
    return {"observations": {"phase2": {"cycles": [{"index": i + 1, "gravebound": {"targets": t}} for i, t in enumerate(cycles)]}}}


class Card(unittest.TestCase):
    def test_old_saved_review_is_recomputed_from_times(self):
        # 예전 엔진 저장본: outcome 없음 · status 는 15초 기준이라 시간 초과도 success
        old = {"player": "라면-아즈샤라-KR", "appliedSec": 201.6, "removedSec": 212.6, "removedStackEvents": 2, "status": "success"}
        self.assertEqual(_gravebound_outcome_of(old), "expired")
        ok = {"player": "PlayerA", "appliedSec": 201.6, "removedSec": 206.9, "removedStackEvents": 2, "status": "success"}
        self.assertEqual(_gravebound_outcome_of(ok), "complete")

    def test_card_failed_lists_expired_players(self):
        rv = review([{"player": "A-x", "outcome": "complete"}, {"player": "라면-아즈샤라-KR", "outcome": "expired"}],
                    [{"player": "C", "outcome": "died"}])
        status, value, note = _gravebound_card(rv)
        self.assertEqual(status, "failed")
        self.assertEqual(value, "완료 1 · 시간 초과 1")
        self.assertIn("1번 라면", note)
        self.assertIn("다른 원인 사망 1명 제외", note)

    def test_card_success(self):
        rv = review([{"player": "A", "outcome": "complete"}] * 4, [{"player": "B", "outcome": "complete"}] * 4)
        self.assertEqual(_gravebound_card(rv)[:2], ("success", "8명 모두 완료"))

    def test_card_none_without_judgeable_targets(self):
        self.assertIsNone(_gravebound_card(review([{"player": "A", "outcome": "unknown"}])))
        self.assertIsNone(_gravebound_card(review()))


if __name__ == "__main__":
    unittest.main()
