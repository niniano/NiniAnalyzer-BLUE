"""1.0.6 (GOLD V5.2.9): 엔진 기믹 규칙이 없는 보스(키식스)의 기믹 수행 체크 칸 · 보스 보드 · 키식스 판정 수정. 이름은 전부 가짜."""
from __future__ import annotations

import importlib.util
import json
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


BG = _load("live/boss_guides_v1.py", "bg529")
BD = _load("live/v529_board.py", "bd529")
ROSTER = [{"name": f"P{i}", "specID": 71} for i in range(20)]


def death(sec, name, sid):
    return {"sec": sec, "name": name, "lastHit": {"spellID": sid}}


def cast(sid, times):
    return {"spellID": sid, "event": "SPELL_CAST_START", "count": len(times), "times": times}


def facts(deaths, casts=None, sim=None):
    return {"encounter": {"encounterID": 3513}, "roster": ROSTER, "deaths": deaths,
            "enemyCastSummary": casts if casts is not None else [cast(1302884, [17.0, 125.0]), cast(1302951, [52.0]), cast(1304424, [28.0, 70.0])],
            "simultaneousDamage": sim or []}


def meta(dur, result="Wipe"):
    return {"encounterID": 3513, "difficultyID": 233, "durationSec": dur, "result": result}


class Checks(unittest.TestCase):
    def test_wipe_checks(self):
        d = [death(36, "P1", 1304424)] + [death(88.8 + i * 0.3, f"P{2+i}", 1303257) for i in range(8)]
        sim = [{"sec": 60.0, "spellID": 1303257, "players": ["P9-서버", "P10"]}]
        out = BG.build(meta(116.0), facts(d, sim=sim), [], {"available": True, "stack": {}, "tankStack": [], "fearImmune": []})
        by = {c["title"]: c for c in out["checks"]}
        self.assertEqual(by["빛구슬 (소멸)"]["status"], "failed")
        self.assertEqual(by["빛구슬 (소멸)"]["value"], "사망 1명")
        self.assertEqual(by["빛 안 (숨막히는 암흑)"]["value"], "사망 8명")
        self.assertEqual(by["딱정벌레 (소화액)"]["status"], "success")
        self.assertTrue(by["무효화 중첩 (2단계)"]["value"].startswith("적용 안 함"))
        self.assertEqual(by["탱 교대 (암흑 황폐)"]["value"], "교대 정상")

    def test_hits_without_death_is_warning_and_no_raw(self):
        sim = [{"sec": 60.0, "spellID": 1303261, "players": ["P3", "P4", "P5"]}]
        out = BG.build(meta(116.0), facts([], sim=sim))
        by = {c["title"]: c for c in out["checks"]}
        self.assertEqual(by["쫄·알 처리 (어둠의 광란)"]["status"], "warning")
        self.assertEqual(by["쫄·알 처리 (어둠의 광란)"]["value"], "3명 맞음 · 사망 없음")
        self.assertEqual(by["공포 중 리혼·결의"]["value"], "원본 로그 없음")

    def test_kill_nullify(self):
        casts = [cast(1302884, [17.0, 125.0, 141.0, 234.0]), cast(1302951, [52.0, 161.0]), cast(1301479, [250.0])]
        rawc = {"available": True, "stack": {1308875: {"P1": {"max": 3, "sec": 280.0}, "P2": {"max": 2, "sec": 273.0}}},
                "tankStack": [], "fearImmune": []}
        out = BG.build(meta(302.9, "Kill"), facts([death(292, "P1", 1308875)], casts), [], rawc)
        by = {c["title"]: c for c in out["checks"]}
        self.assertEqual(by["무효화 중첩 (2단계)"]["status"], "failed")
        self.assertEqual(by["무효화 중첩 (2단계)"]["value"], "3중첩 1명")

    def test_other_bosses_have_no_checks(self):
        f = {"encounter": {"encounterID": 3420}, "roster": ROSTER, "deaths": [], "enemyCastSummary": []}
        self.assertEqual(BG.build({"encounterID": 3420, "difficultyID": 16, "durationSec": 100, "result": "Wipe"}, f)["checks"], [])
        self.assertRegex(BG.VERSION, r"^BOSS-GUIDES-V1\.([89]|\d\d)$")


class Board(unittest.TestCase):
    def _try(self, td, n, result, deaths, dur):
        d = Path(td) / f"try_{n:03d}_{result.upper()}_200000"
        d.mkdir()
        (d / "meta.json").write_text(json.dumps(dict(meta(dur, result), tryNo=n)), encoding="utf-8")
        (d / "raid_facts.json").write_text(json.dumps(facts(deaths)), encoding="utf-8")
        return {"storageDir": str(d), "encounterID": 3513, "difficultyID": 233, "result": result, "durationSec": dur, "tryNo": n}

    def test_pairs_and_board_rows(self):
        with tempfile.TemporaryDirectory() as td:
            r1 = self._try(td, 1, "Wipe", [death(88.8 + i * 0.3, f"P{i}", 1303257) for i in range(8)], 116.0)
            r2 = self._try(td, 2, "Wipe", [death(88.8 + i * 0.3, f"P{i}", 1303257) for i in range(8)], 117.0)
            r3 = self._try(td, 3, "Kill", [death(78, "P1", 1304424)], 301.0)
            reset = dict(r1, storageDir=r1["storageDir"] + "x", durationSec=12.0, tryNo=4)
            pairs = BD.add_guide_pairs([], [r1, r2, r3, reset])
            self.assertEqual(len(pairs), 3)
            model = {"rows": [{"tryNo": r["tryNo"], "durationSec": r["durationSec"], "result": "킬" if r["result"] == "Kill" else "와이프",
                               "review": rev, "stageIndex": 1, "progress": "전멸"} for r, rev in pairs],
                     "repeats": [], "voiceOrders": ["현재 반복되는 검증 실패 없음 · 다음 구간 진행 확인"]}
            out = BD.augment_board(model)
            rows = {r["tryNo"]: r for r in out["rows"]}
            self.assertEqual(rows[1]["progress"], "2웨이브")
            self.assertIn("질식 (빛 밖)", rows[1]["keyFailure"])
            self.assertIn("추정 후보", rows[1]["keyFailure"])
            self.assertEqual(rows[3]["stageIndex"], 4)
            self.assertEqual(out["repeats"][0]["count"], 2)
            self.assertTrue(out["voiceOrders"][0].startswith("암흑 동안"))
            # 이미 review 가 있는 트라이는 그대로
            self.assertEqual(len(BD.add_guide_pairs([(r1, {"bossKey": "x"})], [r1])), 1)

    def test_fill_cards_only_without_engine(self):
        class Var:
            def __init__(self): self.v = ""
            def set(self, v): self.v = v
            def get(self): return self.v

        class Frame:
            def __init__(self): self.removed = False
            def grid_remove(self): self.removed = True

        class App:
            def __init__(self, supported):
                self._b3641_try_model = {"supported": supported}
                self.b3641_try_cards = {f"k{i}": {"frame": Frame()} for i in range(4)}
                self.b3641_try_scope_var = Var()
                self.set_cards = []
            def _set_b3641_card(self, card): self.set_cards.append(card)

        checks = [{"title": "a", "status": "failed", "value": "사망 1명"}, {"title": "b", "status": "success", "value": "사망 없음"}]
        app = App(False)
        self.assertTrue(BD.fill_check_cards(app, checks))
        self.assertEqual([c["key"] for c in app.set_cards], ["k0", "k1"])
        self.assertTrue(app.b3641_try_cards["k3"]["frame"].removed)
        self.assertFalse(BD.fill_check_cards(App(True), checks))

    def test_hooks(self):
        src = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        self.assertIn("from live.v529_board import add_guide_pairs as _v529_pairs", src)
        self.assertIn("from live.v529_board import augment_board as _v529_aug", src)
        lay = (ROOT / "live" / "v43_layout.py").read_text(encoding="utf-8")
        self.assertIn('lines["_checks"]', lay)
        self.assertIn("from live.v529_board import fill_check_cards", lay)
        self.assertFalse((ROOT / "live" / "display_patch.py").exists())


if __name__ == "__main__":
    unittest.main()
