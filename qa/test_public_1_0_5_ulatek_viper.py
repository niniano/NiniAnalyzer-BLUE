"""1.0.5: 울라텍 신화 알 운반 → 살무사 (GOLD V5.2.7 · V5.3.6).

- 알 든 사람(유독한 껍질)이 부식의 파도에 맞음 / 알끼리 부딪힘(맹독성 분출) / 알 놓침 → 역병비늘 살무사 → 붕괴
- 붕괴보다 한참(20초+) 앞선 막을 수 없는 분노는 첫 실수로 치지 않음
- 화면 참고 줄: 알 든 사람 파도 전부 · 살무사 주시 → 착지 스턴(1303414) 걸린 사람
이름은 전부 가짜.
"""
from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import ulatek_conclusion_v1 as U  # noqa: E402

START = "11/3/2026 21:00:00.0000"
BOSS = 'Vehicle-0-1-2-3-257758-00000A,"울라텍",0x10a48,0x0'
NOBODY = "0000000000000000,nil,0x80000000,0x80000000"


def ts(sec: float) -> str:
    m, s = divmod(sec, 60)
    return f"11/3/2026 21:{int(m):02d}:{s:07.4f}"


def pl(name: str) -> str:
    return f'Player-1-{abs(hash(name)) % 99999:05d},"{name}-서버-KR",0x512,0x0'


def viper(k: int) -> str:
    return f'Creature-0-1-2-3-261915-{k:06d},"역병비늘 살무사",0xa48,0x0'


def line(sec, body):
    return f"{ts(sec)}  {body}\n"


def mythic_meta(dur=140.0):
    return {"tryNo": 1, "result": "Wipe", "durationSec": dur, "difficultyID": 16}


def mech(fm=None):
    obs = {"heartWindows": []}
    if fm:
        obs["firstVerifiedFailure"] = fm
        obs["verifiedFailures"] = [fm]
    return {"observations": obs}


def raid(col, top="부식의 파도", n=5):
    return {"collapse": {"startSec": col, "spells": {top: n}}}


class ScanRaw(unittest.TestCase):
    def test_reads_shell_wave_viper_sting_rage(self):
        rows = [
            line(0.0, "ENCOUNTER_START,3492,\"울라텍\",16,20,2939"),
            line(100.0, f'SPELL_AURA_APPLIED,{BOSS},{pl("알이")},1307612,"유독한 껍질",0x8,DEBUFF'),
            line(110.0, f'SPELL_DAMAGE,{BOSS},{pl("알이")},1292403,"부식의 파도",0x8'),
            line(110.4, f'SPELL_CAST_START,{viper(1)},{NOBODY},1301800,"산성 파열",0x8'),
            line(111.0, f'SPELL_AURA_APPLIED,{viper(1)},{pl("옆사람")},1305163,"석화의 독침",0x8,DEBUFF'),
            line(115.0, f'SPELL_CAST_SUCCESS,{BOSS},{NOBODY},1301007,"막을 수 없는 분노",0x1'),
            line(115.5, f'SPELL_CAST_SUCCESS,{BOSS},{NOBODY},1301007,"막을 수 없는 분노",0x1'),
            line(118.0, f'SPELL_AURA_REMOVED,{BOSS},{pl("알이")},1307612,"유독한 껍질",0x8,DEBUFF'),
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "raw.log.gz"
            with gzip.open(p, "wt", encoding="utf-8") as fh:
                fh.writelines(rows)
            raw = U.scan_raw(p, START)
        self.assertTrue(raw["available"])
        self.assertEqual([x["event"] for x in raw["shell"]], ["SPELL_AURA_APPLIED", "SPELL_AURA_REMOVED"])
        self.assertEqual(raw["waveHits"], [{"sec": 110.0, "target": "알이"}])
        self.assertEqual(list(raw["viperSeen"].values()), [110.4])
        self.assertEqual(raw["viperSting"][0]["target"], "옆사람")
        self.assertEqual(raw["rage"], [115.0])   # 1초 안 중복은 한 번


def base_raw(**kw):
    raw = {"available": True, "shell": [], "waveHits": [], "viperSeen": {}, "viperSting": [], "rage": [], "eggBurst": []}
    raw.update(kw)
    return raw


class EggViperCause(unittest.TestCase):
    def test_wave_on_carrier(self):
        raw = base_raw(shell=[{"sec": 100.0, "event": "SPELL_AURA_APPLIED", "target": "알이"}],
                       waveHits=[{"sec": 117.0, "target": "알이"}, {"sec": 117.2, "target": "맨손"}],
                       viperSeen={"v1": 118.0, "v2": 118.5}, viperSting=[{"sec": 120.0, "target": "옆사람"}])
        r = U.build(mythic_meta(), mech(), raid(126.0), {}, raw)
        self.assertEqual(r["cause"]["code"], "EGG_VIPER")
        self.assertEqual(r["cause"]["confidence"], "verified")
        self.assertIn("알 든 알이 부식의 파도 1:57 → 살무사 2마리 1:58", r["lines"]["전멸 원인"])
        self.assertIn("[확정]", r["lines"]["전멸 원인"])
        self.assertTrue(r["lines"]["첫 실수"].startswith("1페 1:57 알 든 알이 부식의 파도"))
        self.assertEqual(r["firstKind"], U.EGG_VIPER_KIND)
        self.assertEqual(r["lines"]["다음 풀"], U.NEXT["egg_viper"])
        self.assertTrue(any(x.startswith("살무사 첫 1:58 · 2마리") and "석화의 독침 스턴 1번" in x for x in r["extraLines"]))
        self.assertEqual(r["plan"]["viper"]["names"], ["알이"])

    def test_wave_on_empty_hand_is_not_trigger(self):
        raw = base_raw(waveHits=[{"sec": 117.0, "target": "맨손"}], viperSeen={"v1": 118.0})
        r = U.build(mythic_meta(), mech(), raid(126.0), {}, raw)
        self.assertEqual(r["cause"]["code"], "EGG_VIPER")
        self.assertEqual(r["cause"]["confidence"], "likely")
        self.assertIn("원인 미확인", r["lines"]["전멸 원인"])

    def test_burst(self):
        raw = base_raw(eggBurst=[{"sec": 40.6, "event": "SPELL_AURA_APPLIED", "source": "울라텍", "target": "가"},
                                 {"sec": 40.6, "event": "SPELL_AURA_APPLIED", "source": "울라텍", "target": "나"}],
                       viperSeen={"v1": 40.7})
        r = U.build(mythic_meta(60.0), mech(), raid(44.0, "막을 수 없는 분노", 8), {}, raw)
        self.assertEqual(r["cause"]["code"], "EGG_VIPER")
        self.assertIn("알 충돌 가·나 0:40 → 살무사 1마리 0:40", r["lines"]["전멸 원인"])
        eb = next(x for x in r["extraLines"] if x.startswith("알충돌"))
        self.assertNotIn("실측 전", eb)

    def test_drop_is_likely(self):
        raw = base_raw(shell=[{"sec": 60.0, "event": "SPELL_AURA_APPLIED", "target": "알이"},
                              {"sec": 70.6, "event": "SPELL_AURA_REMOVED", "target": "알이"}],
                       viperSeen={"v1": 71.0})
        r = U.build(mythic_meta(90.0), mech(), raid(85.0), {}, raw)
        self.assertEqual(r["cause"]["code"], "EGG_VIPER")
        self.assertEqual(r["cause"]["confidence"], "likely")
        self.assertIn("알 든 알이 알 놓침 1:10", r["lines"]["전멸 원인"])

    def test_viper_after_collapse_or_too_early_is_ignored(self):
        late = base_raw(viperSeen={"v1": 86.0})
        r = U.build(mythic_meta(90.0), mech(), raid(73.0), {}, late)
        self.assertNotEqual(r["cause"]["code"], "EGG_VIPER")
        early = base_raw(viperSeen={"v1": 20.0})
        r = U.build(mythic_meta(140.0), mech(), raid(126.0), {}, early)
        self.assertNotEqual(r["cause"]["code"], "EGG_VIPER")

    def test_rage_between_is_noted(self):
        raw = base_raw(viperSeen={"v1": 71.0}, rage=[76.0, 84.8])
        r = U.build(mythic_meta(90.0), mech(), raid(85.0), {}, raw)
        self.assertIn("그 사이 막을 수 없는 분노 2회", r["lines"]["전멸 원인"])

    def test_heroic_unchanged(self):
        raw = base_raw(viperSeen={"v1": 118.0})
        meta = dict(mythic_meta(), difficultyID=15)
        r = U.build(meta, mech(), raid(126.0), {}, raw)
        self.assertNotEqual(r["cause"]["code"], "EGG_VIPER")


class FirstMistake(unittest.TestCase):
    RAGE = {"sec": 76.0, "kind": "unchecked_rage", "label": "막을 수 없는 분노"}

    def test_old_rage_is_reference_only(self):
        r = U.build(mythic_meta(), mech(self.RAGE), raid(126.0), {}, base_raw())
        self.assertNotEqual(r["firstKind"], "unchecked_rage")
        self.assertIn("막을 수 없는 분노 1:16는 붕괴보다 한참 앞이라 참고만", r["lines"]["첫 실수"])

    def test_rage_near_collapse_stays(self):
        fm = dict(self.RAGE, sec=84.8)
        r = U.build(mythic_meta(90.0), mech(fm), raid(85.0, "막을 수 없는 분노", 7), {}, base_raw())
        self.assertEqual(r["firstKind"], "unchecked_rage")
        self.assertEqual(r["cause"]["code"], "UNCHECKED_RAGE")

    def test_rage_times_listed(self):
        m = mech(self.RAGE)
        m["observations"]["uncheckedRage"] = {"count": 2}
        r = U.build(mythic_meta(), m, raid(126.0), {}, base_raw(rage=[76.0, 84.8]))
        self.assertIn("분노   막을 수 없는 분노 2회 (탱 공백) · 1:16, 1:24", r["extraLines"])


class Version(unittest.TestCase):
    def test_version(self):
        self.assertEqual(U.VERSION, "ULATEK-CONCLUSION-V2.0")

class ScanStun(unittest.TestCase):
    def test_reads_landing_stun(self):
        rows = [
            line(0.0, 'ENCOUNTER_START,3492,"울라텍",16,20,2939'),
            line(70.5, f'SPELL_AURA_APPLIED,{viper(1)},{pl("찍힘")},1305163,"석화의 독침",0x8,DEBUFF'),
            line(75.6, f'SPELL_AURA_APPLIED,{viper(1)},{pl("찍힘")},1303414,"석화의 독침",0x8,DEBUFF'),
            line(75.6, f'SPELL_AURA_APPLIED,{viper(1)},{pl("옆사람")},1303414,"석화의 독침",0x8,DEBUFF'),
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "raw.log.gz"
            with gzip.open(p, "wt", encoding="utf-8") as fh:
                fh.writelines(rows)
            raw = U.scan_raw(p, START)
        self.assertEqual([x["target"] for x in raw["viperStun"]], ["찍힘", "옆사람"])
        m = U.viper_marks(raw)
        self.assertEqual(m, [{"sec": 70.5, "target": "찍힘", "stunSec": 75.6, "stunned": ["찍힘", "옆사람"]}])


class Notes(unittest.TestCase):
    def raw(self):
        return base_raw(
            shell=[{"sec": 100.0, "event": "SPELL_AURA_APPLIED", "target": n} for n in ("가", "나", "다")],
            waveHits=[{"sec": 113.0, "target": "가"}, {"sec": 118.0, "target": "나"}, {"sec": 120.0, "target": "다"},
                      {"sec": 120.1, "target": "맨손"}],
            viperSeen={"v1": 114.0, "v2": 116.0, "v3": 118.5, "v4": 120.5},
            viperSting=[{"sec": 115.0, "target": "라"}, {"sec": 125.0, "target": "마"}],
            viperStun=[{"sec": 120.1, "target": "바"}, {"sec": 120.1, "target": "라"}])

    def test_all_carrier_waves_and_stuns_in_notes(self):
        r = U.build(mythic_meta(), mech(), raid(126.0), {}, self.raw())
        notes = r["notes"]
        # 1.0.9: 누가 맞았는지는 「알 운반」 카드로 (참고 줄은 횟수만)
        self.assertEqual(notes[0], "알 든 사람 부식의 파도 3번 → 살무사 4마리 (1:54 4)")
        self.assertEqual(U.egg_viper_notes(U.viper_review(self.raw()))[0],
                         "알 든 사람 부식의 파도 3번 · 가 1:53, 나 1:58, 다 2:00 → 살무사 4마리 (1:54 4)")
        self.assertEqual(notes[1], "살무사 주시→스턴 2번 · 2명 · 2:00 라+바, 2:05 마 주시(스턴 없음)")
        # 무리 안쪽에서 맞은 알 든 사람도 원인에 같이
        self.assertIn("알 든 가·나·다 부식의 파도 1:53 → 살무사 4마리 1:54", r["lines"]["전멸 원인"])
        self.assertEqual(r["plan"]["viper"]["stuns"][0]["stunned"], ["라", "바"])

    def test_notes_first_and_capped_list(self):
        raw = base_raw(viperSeen={f"v{i}": 20.0 + i * 10 for i in range(9)})
        r = U.build(mythic_meta(140.0), mech(), raid(126.0), {}, raw)
        self.assertTrue(r["notes"][0].startswith("알 든 사람 부식의 파도 0번 → 살무사 9마리"))
        self.assertIn(" …)", r["notes"][0])
        vline = next(x for x in r["extraLines"] if x.startswith("살무사 첫"))
        self.assertEqual(vline.count("마리 ←"), 8)   # 예전엔 3무리까지만

    def test_heroic_has_no_egg_notes(self):
        meta = dict(mythic_meta(), difficultyID=15)
        r = U.build(meta, mech(), raid(126.0), {}, self.raw())
        self.assertFalse(any(n.startswith(("알 든 사람", "살무사 주시")) for n in r["notes"]))


if __name__ == "__main__":
    unittest.main()
