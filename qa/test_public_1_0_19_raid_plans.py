"""BLUE 1.0.19: 공대 계획(공대마다 택틱이 다름) · 울라텍 남쪽 구역(보호막 알 · 산란무리 → 유체 · 유체 광폭). 이름은 전부 가짜."""
from __future__ import annotations

import gzip
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
QA = Path(__file__).resolve().parent
if str(QA) not in sys.path:
    sys.path.insert(0, str(QA))

from live import raid_plans as RP
from live import raid_plans_ui as UI
from live import ulatek_conclusion_v1 as U
from test_public_1_0_5_ulatek_viper import START, line, pl

NAMES = [f"가짜{i}" for i in range(20)]
DATA = {"default": None, "plans": [
    {"id": "a", "name": "가 공대", "members": NAMES[:12], "ulatek": {"bigEggPick": "4:18", "bigEggPut": "4:30", "south": "big_first"}},
    {"id": "b", "name": "나 공대", "members": NAMES[8:], "ulatek": {"bigEggPick": "4:12", "south": "shield_first"}},
    {"id": "c", "name": "다 공대", "members": [], "ulatek": {}},
]}


class Plans(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(RP.parse_mmss("4:18"), 258.0)
        self.assertEqual(RP.parse_mmss(270), 270.0)
        self.assertIsNone(RP.parse_mmss(""))
        self.assertIsNone(RP.parse_mmss("가나"))

    def test_resolve_by_roster(self):
        v = RP.resolve([n + "-서버" for n in NAMES[:10]], DATA)
        self.assertEqual((v["id"], v["bigEggPick"], v["bigEggPut"], v["south"], v["how"]), ("a", 258.0, 270.0, "big_first", "공대원"))
        v = RP.resolve(NAMES[12:], DATA)
        self.assertEqual((v["id"], v["bigEggPut"], v["south"]), ("b", None, "shield_first"))

    def test_too_few_members_no_plan(self):
        self.assertIsNone(RP.resolve(NAMES[:5], DATA))
        self.assertIsNone(RP.resolve(None, DATA))

    def test_default(self):
        d = dict(DATA, default="c")
        v = RP.resolve(["없는사람"], d)
        self.assertEqual((v["id"], v["how"], v["bigEggPick"]), ("c", "기본", None))
        self.assertEqual(RP.plan_line(v), "다 공대 · 시각 미정")

    def test_plan_line(self):
        self.assertEqual(RP.plan_line(RP.resolve(NAMES[:10], DATA)), "가 공대 · 큰 알 4:18 집기 · 4:30쯤 넣기 · 남쪽 2번 큰 알 먼저")
        self.assertTrue(RP.plan_line(None).startswith("공대 계획 없음"))

    def test_load_save(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "raid_plans.json"
            self.assertEqual(RP.load([p]), {"plans": []})
            RP.save(DATA, p)
            self.assertEqual(RP.load([p])["plans"][0]["id"], "a")


def swarm_hit(sec, swarm_uid, owner_uid, x, y):
    sw = f'Creature-0-1-2-3-263535-{swarm_uid},"역병비늘 산란무리",0xa48,0x0'
    owner = f"Creature-0-1-2-3-268121-{owner_uid}"
    adv = f"Creature-0-1-2-3-263535-{swarm_uid},{owner},100,100,0,0,0,0,0,0,1,0,0,0,{x},{y},2610,0,91"
    return line(sec, f'SPELL_DAMAGE,{pl("딜러")},{sw},1,"공격",0x1,{adv},50,50,0,1,0,0,0,nil,nil,nil')


def egg_hit(sec, uid, x, y):
    egg = f'Creature-0-1-2-3-268121-{uid},"역병비늘 자손",0xa48,0x0'
    adv = f"Creature-0-1-2-3-268121-{uid},0000000000000000,100,100,0,0,0,0,0,0,1,0,0,0,{x},{y},2610,0,91"
    return line(sec, f'SPELL_DAMAGE,{pl("딜러")},{egg},1,"공격",0x1,{adv},50,50,0,1,0,0,0,nil,nil,nil')


class BroodScan(unittest.TestCase):
    def test_scan(self):
        rows = [line(0.0, 'ENCOUNTER_START,3492,"울라텍",16,20,2939'),
                line(174.0, 'SPELL_CAST_START,Creature-0-1-2-3-268164-0000AAAAAA,"파멸비늘 알",0xa48,0x0,0000000000000000,nil,0x80000000,0x80000000,1306862,"부화하는 파멸",0x8'),
                egg_hit(181.0, "0000000001", 1620.5, 70.1),          # 북쪽 알
                egg_hit(245.0, "0000000002", 1476.0, -68.0),         # 남쪽 보호막 알
                line(246.0, 'SPELL_AURA_REMOVED,Creature-0-1-2-3-268121-0000000002,"역병비늘 자손",0xa48,0x0,'
                            'Creature-0-1-2-3-268121-0000000002,"역병비늘 자손",0xa48,0x0,1299650,"강화",0x1,BUFF'),
                swarm_hit(257.0, "0000000009", "0000000003", 1478.0, -66.0),
                line(264.0, 'SPELL_AURA_APPLIED,Creature-0-1-2-3-261874-0000000007,"역병비늘 유체",0xa48,0x0,'
                            'Creature-0-1-2-3-261874-0000000007,"역병비늘 유체",0xa48,0x0,1313758,"끓어오르는 맹독",0x8,BUFF'),
                line(290.0, 'UNIT_DIED,0000000000000000,nil,0x80000000,0x80000000,Creature-0-1-2-3-261874-0000000007,"역병비늘 유체",0xa48,0x0,0')]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "raw.log.gz"
            with gzip.open(p, "wt", encoding="utf-8") as fh:
                fh.writelines(rows)
            raw = U.scan_raw(p, START)
        b = raw["brood"]
        self.assertEqual(b["eggs"]["0000000001"]["x"], 1620.5)
        self.assertEqual(b["eggs"]["0000000002"]["shieldOff"], 246.0)
        self.assertEqual(b["swarm"]["0000000009"]["owner"], "0000000003")
        self.assertEqual(b["ooze"]["0000000007"], {"first": 264.0, "died": 290.0})


def brood_raw(broken: bool, carried: bool, acid: bool):
    eggs = {"N1": {"first": 181.0, "shieldOff": 195.0, "x": 1620.0, "y": 70.0}}
    for i in range(4):
        eggs[f"S{i}"] = {"first": 245.0 + i, "shieldOff": (248.0 + i) if broken else None, "x": 1476.0, "y": -68.0 if i < 2 else 66.0}
    swarm = {} if broken else {f"W{i}": {"first": 256.5 + i * 0.3, "owner": f"S{i}", "x": 1478.0, "y": 0.0} for i in range(4)}
    ooze = {} if broken else {f"O{i}": {"first": 263.0, "died": 290.0 + i} for i in range(4)}
    shell = [{"sec": 249.0 + i, "event": "SPELL_AURA_APPLIED", "target": f"가짜{i}"} for i in range(4)] if carried else []
    return {"available": True, "bigEggHatch": [174.0], "brood": {"eggs": eggs, "swarm": swarm, "ooze": ooze}, "shell": shell,
            "acid": [{"sec": 296.0, "kind": "start", "src": "O3", "rawling": True}] if acid else []}


class South(unittest.TestCase):
    SHIELD = {"id": "b", "name": "나 공대", "south": "shield_first", "bigEggPick": None, "bigEggPut": None}
    BIG = {"id": "a", "name": "가 공대", "south": "big_first", "bigEggPick": 258.0, "bigEggPut": 270.0}

    def test_big_first_ooze(self):
        sr = U.south_review(brood_raw(False, False, False), 340.0, self.BIG)
        self.assertEqual((sr["eggs"], sr["broken"], sr["swarm"], sr["ooze"], sr["oozeDead"]), (4, 0, 4, 4, 4))
        self.assertEqual(sr["line"], "보호막 알 4개 · 깸 0 → 큰 알 집을 때 산란무리 4 (4:16) → 유체 4 (4:23) · 처치 4/4 4:50~4:53"
                                     " | 계획 2번 큰 알 먼저 ✅")

    def test_big_first_enrage(self):
        sr = U.south_review(brood_raw(False, False, True), 340.0, self.BIG)
        self.assertEqual((sr["oozeEnraged"], sr["verdict"]), (1, "bad"))
        self.assertIn("광폭(산성 배출) 1마리 ⚠", sr["line"])

    def test_shield_first_ok(self):
        sr = U.south_review(brood_raw(True, True, False), 340.0, self.SHIELD)
        self.assertEqual(sr["line"], "보호막 알 4개 · 깸 4 (4:08~4:11) · 듦 4 | 계획 1번 보호막 알 먼저 ✅")

    def test_shield_first_missed(self):
        sr = U.south_review(brood_raw(False, False, False), 340.0, self.SHIELD)
        self.assertEqual(sr["verdict"], "bad")
        self.assertIn("⚠ 보호막 알이 남은 채 큰 알을 집음", sr["line"])

    def test_no_plan_no_verdict(self):
        sr = U.south_review(brood_raw(False, False, True), 340.0, None)
        self.assertIsNone(sr["verdict"])
        self.assertNotIn("계획", sr["line"])

    def test_never_reached_south(self):
        r = brood_raw(False, False, False)
        r["brood"]["eggs"] = {"N1": r["brood"]["eggs"]["N1"]}
        self.assertIsNone(U.south_review(r, 240.0, None))

    def test_build_lines(self):
        from test_public_1_0_15_big_egg_roar_and_follow import make
        meta, mech, raid, facts, raw = make(picks=4)
        raw.update(brood_raw(False, False, True))
        with mock.patch.object(U.RP, "resolve", return_value=self.BIG):
            out = U.build(meta, mech, raid, facts, raw)
        self.assertIn("계획   가 공대 · 큰 알 4:18 집기 · 4:30쯤 넣기 · 남쪽 2번 큰 알 먼저", out["extraLines"])
        self.assertTrue(any(x.startswith("남쪽   보호막 알 4개") for x in out["extraLines"]))
        self.assertEqual(out["plan"]["south"]["verdict"], "bad")
        self.assertIn(U.NEXT["south_ooze"], out["notes"])


class DutyOnlyInItsRaid(unittest.TestCase):
    def _run(self, plan_id):
        from test_public_1_0_15_big_egg_roar_and_follow import make
        meta, mech, raid, facts, raw = make(picks=4)
        facts["roster"] = [{"name": n + "-서버"} for n in NAMES[:12]]
        duty = {"players": ["가짜1"], "raidPlans": ["old"]}
        with mock.patch.object(U, "load_personal_duty", return_value=duty), \
                mock.patch.object(U.RP, "resolve", return_value={"id": plan_id, "name": "x", "south": None,
                                                                 "bigEggPick": None, "bigEggPut": None}):
            return U.build(meta, mech, raid, facts, raw)

    def test_other_raid_no_duty(self):
        self.assertNotIn("duty", self._run("new")["plan"]["bigEgg"])

    def test_own_raid_duty(self):
        self.assertEqual(self._run("old")["plan"]["bigEgg"]["duty"]["player"], "가짜1")


class PlanWindowSave(unittest.TestCase):
    def test_save_and_resolve(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "raid_plans.json"
            data = UI.save_values("우리", "4:18", "4:30", "big_first", path=p)
            v = RP.resolve(["아무개"], RP.load([p]))
            self.assertEqual((v["name"], v["bigEggPick"], v["bigEggPut"], v["south"], v["how"]), ("우리", 258.0, 270.0, "big_first", "기본"))
            self.assertEqual(data["default"], "mine")
            UI.save_values("", "", "", "", path=p)
            v = RP.resolve(None, RP.load([p]))
            self.assertEqual((v["name"], v["bigEggPick"], v["south"]), ("우리 공대", None, None))
            UI.clear(path=p)
            self.assertIsNone(RP.resolve(None, RP.load([p])))

    def test_bad_time(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                UI.save_values("우리", "네시", "", "", path=Path(d) / "x.json")

    def test_version(self):
        self.assertEqual(U.VERSION, "ULATEK-CONCLUSION-V2.4")


if __name__ == "__main__":
    unittest.main()
