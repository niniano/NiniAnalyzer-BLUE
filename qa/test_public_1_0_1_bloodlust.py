"""1.0.1 (GOLD V5.3.4 와 같은 수정): 본인에게만 걸린 블러드 계열 버프(증강 기원사 위상의 격노 등)를 블러드로 보지 않는다."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live.boss_mechanics import engine as E  # noqa: E402


def _aura(sec, sid, src, dst, name="위상의 격노"):
    return {"event": "SPELL_AURA_APPLIED", "spellID": sid, "spellName": name, "sec": sec,
            "sourceName": src, "sourceGUID": "Player-" + src, "destName": dst, "destGUID": "Player-" + dst}


def _obs(rows):
    review = E._append_bloodlust_observation({"checks": [], "timeline": []}, rows, source_kind="local")
    return review["observations"]["bloodlust"], review["timeline"]


def test_self_only_fury_of_aspects_is_not_bloodlust():
    # 2026-11-08 똬리 신화 T5: 증강 본인에게만 390386 이 0:04.7 부터 몇 초씩 반복
    rows = [_aura(4.7, 390386, "증강", "증강"), _aura(6.8, 390386, "증강", "증강"),
            _aura(75.6, 390386, "증강", "증강")]
    obs, timeline = _obs(rows)
    assert obs["observed"] is False
    assert not [x for x in timeline if x.get("kind") == "bloodlust_timing"]


def test_raidwide_aura_without_cast_row_is_bloodlust():
    rows = [_aura(300.0 + i * 0.01, 80353, "법사", f"P{i}", "시간 왜곡") for i in range(18)]
    obs, timeline = _obs(rows)
    assert obs["observed"] is True and obs["firstSec"] == 300.0 and obs["source"] == "aura"
    assert [x for x in timeline if x.get("kind") == "bloodlust_timing"]


def test_aura_with_exhaustion_debuff_is_bloodlust_even_in_small_group():
    rows = [_aura(10.0, 390386, "기원", "기원"),
            {"event": "SPELL_AURA_APPLIED", "spellID": 390435, "spellName": "탈진", "sec": 10.0,
             "sourceName": "기원", "destName": "기원", "destGUID": "Player-기원"}]
    obs, _ = _obs(rows)
    assert obs["observed"] is True and obs["firstSec"] == 10.0


def test_cast_row_wins_and_self_aura_ignored_by_coiled_matcher():
    cast = {"event": "SPELL_CAST_SUCCESS", "spellID": 80353, "spellName": "시간 왜곡", "sec": 312.0,
            "sourceName": "법사", "sourceGUID": "Player-법사"}
    rows = [_aura(4.7, 390386, "증강", "증강"), cast]
    obs, _ = _obs(rows)
    assert obs["firstSec"] == 312.0 and obs["source"] == "cast"
    # 보호막 계획 판정: 0:05 근처 본인 전용 버프는 블러드 사용으로 치지 않는다
    near_start = E._coiled_match_player_spell(rows, spell_ids=E._BLOODLUST_FAMILY_IDS,
                                              center_sec=5.0, before_sec=3, after_sec=5)
    assert near_start == []
    near_cast = E._coiled_match_player_spell(rows, spell_ids=E._BLOODLUST_FAMILY_IDS,
                                             center_sec=312.0, before_sec=3, after_sec=5)
    assert [x["sec"] for x in near_cast] == [312.0]


class BloodlustSelfAuraTests(unittest.TestCase):
    def test_all(self):
        test_self_only_fury_of_aspects_is_not_bloodlust()
        test_raidwide_aura_without_cast_row_is_bloodlust()
        test_aura_with_exhaustion_debuff_is_bloodlust_even_in_small_group()
        test_cast_row_wins_and_self_aura_ignored_by_coiled_matcher()


if __name__ == "__main__":
    unittest.main()
