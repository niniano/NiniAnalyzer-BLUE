"""1.0.6 (GOLD V5.2.8): 키식스 신화 첫킬(2026-11-04) 뒤 화면 정리 · BOSS-GUIDES-V1.7.

- 233(유동 신화)도 신화 기준선(우리 킬 · 다른 공대 · HP 체크)을 쓴다
- 붕괴 시작: 몇 명 동시 사망 뒤 공대가 10초 넘게 버티고 나중에 진짜 무너지면 나중 쪽
- 킬 판 결론: 사망 정리(중첩 디버프 묶음) · 다음 주 · HP 체크 · 단계 비교 · 칸 이름
- 「.;」 문구 · 판정 순서 빈 문구 · 시간축 끝 = 전투 시간 · 겹친 같은 기믹 ×n
이름은 전부 가짜.
"""
from __future__ import annotations

import gzip
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BG = _load("live/boss_guides_v1.py", "bg528")
RV = _load("live/v528_review.py", "rv528")
T45 = _load("live/v45_timeline.py", "t45_528")
ROSTER = [{"name": f"P{i}", "specID": 65 if i < 4 else 71} for i in range(20)]


def death(sec, name, sid):
    return {"sec": sec, "name": name, "lastHit": {"spellID": sid}}


def cast(sid, times, start=True):
    return {"spellID": sid, "event": "SPELL_CAST_START" if start else "SPELL_CAST_SUCCESS", "count": len(times), "times": times}


KITH = [cast(1302884, [17.2, 125.7, 141.5, 235.4, 251.2]), cast(1302951, [52.0, 161.0]), cast(1301479, [260.0])]


def meta(dur, result="Wipe", diff=233):
    return {"encounterID": 3513, "difficultyID": diff, "durationSec": dur, "result": result}


class Collapse(unittest.TestCase):
    def test_small_burst_then_hold_then_real_collapse(self):
        # 11/04 1트 모양: 0:18 1명 · 2:25 소멸 3명 · 2:39 1명 · 2:48~2:53 13명
        d = [death(18, "P0", 1302319)] + [death(145 + i * 0.1, f"P{1+i}", 1304424) for i in range(3)] \
            + [death(159, "P4", 1303685)] + [death(168 + i * 0.3, f"P{5+i}", 1303261) for i in range(13)]
        ds = BG.deaths_of({"deaths": d})
        self.assertEqual(round(BG.collapse_at(ds, 20)), 168)
        out = BG.build(meta(173.5), {"encounter": {"encounterID": 3513}, "roster": ROSTER, "enemyCastSummary": KITH, "deaths": d})
        self.assertIn("붕괴 전 사망 5명", out["lines"]["딜·힐"])

    def test_old_cases_unchanged(self):
        d = BG.deaths_of({"deaths": [death(s, f"P{i}", 1) for i, s in enumerate((14, 15, 17, 146, 209, 210, 211, 221, 233, 234, 235))]})
        self.assertEqual(BG.collapse_at(d, 20), 209)          # V1.5 테스트와 같은 답
        d2 = BG.deaths_of({"deaths": [death(s, f"P{i}", 1) for i, s in enumerate((100, 100.5, 101, 106, 111, 117, 140, 141, 142, 143, 144))]})
        self.assertEqual(BG.collapse_at(d2, 20), 100)          # 쉬지 않고 이어지면 처음 무리 그대로


class WaveAndHardWindow(unittest.TestCase):
    def test_wave_casts_chain(self):
        # 맹독 포효가 2:05 · 2:22 · 2:37 로 이어지면 한 웨이브 (예전엔 2:37 을 3웨이브로 봐서 「3웨이브 2:26 HP ⚠」)
        casts = [cast(1302884, [17.0, 32.0, 125.0, 142.0, 157.0]), cast(1302951, [52.0])]
        facts = {"encounter": {"encounterID": 3513}, "enemyCastSummary": casts}
        st = BG.stages(BG.SPECS[3513], facts, 159.0, False)
        self.assertNotIn("3웨이브", [c["name"] for c in st if c.get("status") in ("done", "fail")])
        self.assertEqual(BG.cast_times(facts, (1302884,), gap=30, chain=True), [17.0, 125.0])
        self.assertEqual(BG.cast_times(facts, (1302884,), gap=30), [17.0, 125.0, 157.0])   # 다른 보스는 그대로

    def test_hard_rule_only_inside_collapse(self):
        # 11/04 4트 모양: 1:18 · 2:25 소멸이 흩어져 있어도 2:10 소화액 붕괴가 원인
        d = [death(78, "P0", 1304424)] + [death(130 + i * 0.3, f"P{1+i}", 1302319) for i in range(9)] \
            + [death(145, "P10", 1304424), death(145.2, "P11", 1304424)]
        facts = {"encounter": {"encounterID": 3513}, "roster": ROSTER, "deaths": d,
                 "enemyCastSummary": [cast(1302884, [17.0, 125.0]), cast(1302951, [52.0]), cast(1304424, [28.0, 70.0, 138.0])]}
        out = BG.build(meta(159.0), facts)
        self.assertIn("소화액", out["lines"]["전멸 원인"])
        self.assertNotIn("[확정]", out["lines"]["전멸 원인"])

    def test_hard_consequence_suffix(self):
        d = [death(145 + i * 0.2, f"P{i}", 1304424) for i in range(5)] + [death(167 + i * 0.2, f"P{5+i}", 1303261) for i in range(8)]
        facts = {"encounter": {"encounterID": 3513}, "roster": ROSTER, "deaths": d,
                 "enemyCastSummary": [cast(1302884, [17.0, 125.0]), cast(1302951, [52.0, 161.0]), cast(1304424, [138.0])]}
        out = BG.build(meta(168.0), facts)
        self.assertIn("소멸 5명 (2:25) → 어둠의 광란 8명 (2:47) [확정]", out["lines"]["전멸 원인"])


class Mythic233(unittest.TestCase):
    def test_flex_mythic_uses_baselines(self):
        facts = {"encounter": {"encounterID": 3513}, "roster": ROSTER, "enemyCastSummary": KITH, "deaths": []}
        out = BG.build(meta(116.0), facts, hp=[(60, 80.0), (116, 71.0)])
        self.assertIn("같은 시각 우리 킬", out["lines"]["딜·힐"])
        heroic = BG.build(meta(116.0, diff=15), facts, hp=[(116, 71.0)])
        self.assertNotIn("우리 킬", heroic["lines"]["딜·힐"])


class KillLines(unittest.TestCase):
    def setUp(self):
        self.d = [death(78, "P1", 1304424), death(133, "P2", 1302319), death(254, "P3", 1304424), death(254.2, "P4", 1304424),
                  death(283, "P5", 1301479), death(291, "P6", 1308877), death(291.3, "P7", 1308877)]   # V5.3.0: 즉사 주문
        self.facts = {"encounter": {"encounterID": 3513}, "roster": ROSTER, "enemyCastSummary": KITH, "deaths": self.d}
        self.rawc = {"stack": {1308875: {"P5": {"max": 2, "sec": 271.0}, "P6": {"max": 2, "sec": 271.0}, "P7": {"max": 2, "sec": 285.0}}},
                     "ownerCd": [{"sec": s, "name": "나", "spellID": 121471} for s in (5.0, 98.0, 199.0, 298.0)]}
        self.hp = [(0, 100.0), (114, 60.0), (224, 34.0), (256, 24.0), (298, 2.0), (301, 0.0)]

    def test_kill_summary(self):
        out = BG.build(meta(301.6, "Kill"), self.facts, self.hp, self.rawc)
        L = out["lines"]
        self.assertTrue(L["첫 실수"].startswith("첫 사망 1:18"))
        self.assertIn("사망 7명", L["전멸 원인"])
        self.assertIn("소멸 (빛의 은총 못 먹음) 3명 (1:18, 4:14, 4:14)", L["전멸 원인"])
        self.assertIn("무효화 3중첩 즉사 2명 (4:51, 4:51)", L["전멸 원인"])   # V5.3.0
        self.assertIn("· 무효화 2중첩 1명 (4:43)", L["전멸 원인"])
        self.assertIn("처치 5:01 (우리 최고 5:01 · 다른 공대 킬 4:55~5:13)", L["딜·힐"])
        self.assertIn("2단계 진입 4:16 (다른 공대 4:10~4:17)", L["딜·힐"])
        self.assertIn("HP 60% ✅", L["딜·힐"])
        self.assertIn("소멸 (빛의 은총 못 먹음) 3명 →", L["다음 풀"])
        self.assertIn("무효화 3중첩 즉사 2명 →", L["다음 풀"])
        self.assertEqual(out["labels"], {"전멸 원인": "사망 정리", "다음 풀": "다음 주"})

    def test_no_owner_rogue_line_in_blue(self):
        # BLUE: 로그 주인 도적 쿨기(어둠의 칼날) 줄은 GOLD 전용이라 설정에서 뺐다
        self.assertNotIn("ownerCd", BG.SPECS[3513]["raw_checks"])
        out = BG.build(meta(301.6, "Kill"), self.facts, self.hp, self.rawc)
        self.assertFalse(any("어둠의 칼날" in x for x in out["notes"] + out["extraLines"]))

    def test_wipe_has_no_labels_and_no_semicolon(self):
        d = [death(36, "P1", 1304424), death(78, "P2", 1304424)] + [death(88.8 + i * 0.3, f"P{3+i}", 1303257) for i in range(8)]
        out = BG.build(meta(116.0), dict(self.facts, deaths=d), [(116, 71.0)])
        self.assertEqual(out["labels"], {})
        self.assertNotIn(".;", out["lines"]["다음 풀"])
        self.assertNotIn(";", out["lines"]["다음 풀"])

    def test_raw_scan_has_no_owner_casts(self):
        rows = ['11/4/2026 21:13:21.8749  ENCOUNTER_START,3513,"키식스",233,20,3095\n',
                '11/4/2026 21:13:26.8749  SPELL_CAST_SUCCESS,Player-1-AAA,"나-서버-KR",0x511,0x0,0000000000000000,nil,0x80000000,0x80000000,121471,"어둠의 칼날",0x20\n']
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "raw.log.gz"
            with gzip.open(p, "wt", encoding="utf-8") as fh:
                fh.writelines(rows)
            r = BG.scan_raw_checks(p, "11/4/2026 21:13:21.8749", BG.SPECS[3513]["raw_checks"])
        self.assertEqual(r.get("ownerCd") or [], [])

    def test_our_kill_and_version(self):
        self.assertEqual(BG.OUR_KILLS[3513]["kills"], (301,))
        self.assertRegex(BG.VERSION, r"^BOSS-GUIDES-V1\.([7-9]|\d\d)$")
        self.assertIn(1302319, BG.SPECS[3513]["avoid"])


class Screen(unittest.TestCase):
    def test_no_judgment_text(self):
        self.assertEqual(RV.no_judgment_text({"encounterID": 3000}), "검증된 실수 없음")
        self.assertIn("[추정] 첫 실수", RV.no_judgment_text({"encounterID": 3513, "result": "Wipe"}))
        self.assertIn("사망 정리", RV.no_judgment_text({"encounterID": 3513, "result": "Kill"}))

    def test_fill_duration(self):
        self.assertEqual(RV.fill_duration({"supported": False}, {"durationSec": 116.06}, {})["durationSec"], 116.06)
        self.assertEqual(RV.fill_duration({"durationSec": 50.0}, {"durationSec": 116.0}, {})["durationSec"], 50.0)
        self.assertEqual(RV.fill_duration({}, {}, {"encounter": {"durationSec": 90}})["durationSec"], 90.0)

    def test_overlapping_same_glyphs_merge(self):
        g = [{"title": "부화", "sec": s} for s in (17.0, 17.4, 18.0, 30.0)] + \
            [{"title": "부화", "sec": 30.2, "status": "failed", "judgmentNumbers": [1]}]
        placed, _o, rows = T45.ability_rows(g, lambda s: s * 10, icon_px=18)
        self.assertEqual(rows[0]["count"], 5)                       # 줄 횟수는 그대로
        self.assertEqual([int(x.get("v528Count") or 1) for x in placed], [3, 1, 1])
        self.assertTrue(any(x.get("judgmentNumbers") for x in placed))   # 판정 글리프는 안 합침

    def test_gui_hooks(self):
        src = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        self.assertIn("from live.v528_review import fill_duration as _v528_fill", src)
        self.assertEqual(src.count("text=self._v528_no_judgment_text()"), 2)
        self.assertIn('glyph.get("v528Count")', src)
        lay = (ROOT / "live" / "v43_layout.py").read_text(encoding="utf-8")
        self.assertIn("def _kill_glance(app, row)", lay)
        self.assertIn('lines["_labels"]', lay)
        self.assertFalse((ROOT / "live" / "display_patch.py").exists())   # GOLD 화면 패치 번호는 BLUE 에 없음


if __name__ == "__main__":
    unittest.main()
