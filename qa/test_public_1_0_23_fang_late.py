"""1.0.23: 송곳니 줄 늦게 끊음 — 역병의 핏줄이 걸려 있는 동안 2틱 뒤에 끊으면 중첩 그대로 틱 추가 ·
핏줄이 끝난 뒤(2틱 보고) 끊으면 새 핏줄이라 괜찮음."""
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


def ts(sec):
    m, s = divmod(sec, 60)
    return f"11/3/2026 21:{int(m):02d}:{s:07.4f}"


def fang(sec, ev, who):
    return {"sec": sec, "event": ev, "target": who}


def group(breaks):
    rows = [fang(200.0, "SPELL_AURA_APPLIED", n) for n, _ in breaks]
    rows += [fang(t, "SPELL_AURA_REMOVED", n) for n, t in breaks]
    return rows


class FangLate(unittest.TestCase):
    def test_sync(self):
        raw = {"fang": group([("가짜하나", 215.0), ("가짜둘", 215.3), ("가짜셋", 215.8)]), "veinFresh": [215.0],
               "veinTick": [(215.0, 216685), (217.0, 650055), (219.0, 650055)]}
        r = U.fang_review(raw)[0]
        self.assertTrue(r["sync"])
        self.assertEqual(r["late"], [])
        self.assertIn("동시 ✅", r["line"])

    def test_late_refresh_adds_tick(self):
        raw = {"fang": group([("가짜하나", 206.7), ("가짜둘", 206.7), ("가짜셋", 207.7), ("가짜넷", 210.2)]), "veinFresh": [206.7],
               "veinTick": [(206.7, 216685), (208.7, 650055), (210.7, 866740), (212.7, 866740)]}
        r = U.fang_review(raw)[0]
        self.assertEqual([x["name"] for x in r["late"]], ["가짜넷"])
        self.assertEqual(r["extraTicks"], 1)
        self.assertIn("늦게 끊음 ❌ 가짜넷", r["line"])
        self.assertIn("핏줄 틱 4번 (보통 3번", r["line"])

    def test_waited_after_vein_ended(self):
        raw = {"fang": group([("가짜하나", 208.0), ("가짜둘", 215.5), ("가짜셋", 215.8), ("가짜넷", 216.3)]), "veinFresh": [208.0, 215.5],
               "veinTick": [(208.0, 216685), (210.0, 216685), (212.0, 216685), (215.5, 216685), (217.5, 650055), (219.5, 650055)]}
        r = U.fang_review(raw)[0]
        self.assertEqual(r["late"], [])
        self.assertEqual([x["name"] for x in r["waited"]], ["가짜둘"])
        self.assertIn("늦었지만 핏줄 끝나고 끊음 ✅", r["line"])
        self.assertEqual(r["extraTicks"], 0)

    def test_scan_vein_fresh_and_ticks(self):
        adv = ",".join(["Player-1-00002", "0000000000000000", "100", "100"] + ["0"] * 9 + ["1550.0", "40.0", "2610", "1.0", "300"])
        lines = [
            f'{ts(206.7)}  SPELL_AURA_APPLIED,Vehicle-0-1-2-3-257758-00001,"울라텍",0xa48,0x0,Player-1-00002,"가짜하나-서버-KR",0x514,0x0,1311609,"역병의 핏줄",0x8,DEBUFF\n',
            f'{ts(206.8)}  SPELL_AURA_APPLIED_DOSE,Vehicle-0-1-2-3-257758-00001,"울라텍",0xa48,0x0,Player-1-00002,"가짜하나-서버-KR",0x514,0x0,1311609,"역병의 핏줄",0x8,DEBUFF,2\n',
            f'{ts(206.7)}  SPELL_DAMAGE,Vehicle-0-1-2-3-257758-00001,"울라텍",0xa48,0x0,Player-1-00002,"가짜하나-서버-KR",0x514,0x0,1317955,"역병의 핏줄",0x8,{adv},186246,216685,-1,8,0,0,3781,nil,nil,nil,ST\n',
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "raw.log.gz"
            with gzip.open(p, "wt", encoding="utf-8") as fh:
                fh.writelines(lines)
            raw = U.scan_raw(p, START)
        self.assertEqual(raw["veinFresh"], [206.7])
        self.assertEqual(raw["veinTick"], [(206.7, 216685)])

    def test_next_text(self):
        self.assertIn("2틱 보고", U.NEXT["fang_late"])
        self.assertEqual(U.VEIN_TICK, 1317955)


class Version(unittest.TestCase):
    def test_version(self):
        gui = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        self.assertRegex(gui, r'PUBLIC_APP_VERSION = "1\.0\.\d+"')


# 1.0.23 탱 사망 → 붕괴 · 송곳니 늦게 끊음이 build 에서 뿌리로

HEARTS = [{"index": 1, "startSec": 145.0, "endSec": 165.0, "startBossHpPct": 78.0, "endBossHpPct": 63.0,
           "evidenceLevel": "verified", "status": "observed"}]


def hit(sec, src, amt, spell="근접 공격", sid=None):
    return {"sec": sec, "sourceName": src, "spellName": spell, "spellID": sid, "amount": amt, "hostileSource": True}


def make(*, deaths=None, roster=None, col=294.0, duration=298.0, spells=None, raw_extra=None, fm=None):
    raw = {"available": True, "hp": [(290.0, 60.0)]}
    raw.update(raw_extra or {})
    meta = {"durationSec": duration, "result": "Wipe", "difficultyID": 16, "tryNo": 4}
    obs = {"heartWindows": HEARTS}
    if fm:
        obs["firstVerifiedFailure"] = fm
        obs["verifiedFailures"] = [fm]
    mech = {"observations": obs, "encounter": {"result": "Wipe", "difficultyID": 16}}
    raid = {"collapse": {"startSec": col, "spells": spells or {"산성 배출": 4}}}
    facts = {"deaths": deaths or [], "roster": roster or []}
    return meta, mech, raid, facts, raw


ROSTER = [{"name": "가짜탱-서버", "specID": 104}, {"name": "가짜딜-서버", "specID": 71}]


class TankDeath(unittest.TestCase):
    def tank_death(self, sec=285.0):
        return {"name": "가짜탱-서버", "sec": sec, "damage8s": 6820000.0,
                "lastHit": hit(sec, "역병비늘 유체", 186619.0),
                "recentDamage": [hit(sec - 2.5, "약화된 파멸비늘", 854378.0), hit(sec - 2.0, "울라텍", 79000.0, "괴저 증기", 1286835),
                                 hit(sec - 0.5, "울라텍", 2193762.0), hit(sec - 1.0, "역병비늘 유체", 560000.0)]}

    def test_tank_death_is_root(self):
        cry = {"kind": "anguished_cry", "sec": 292.0, "label": "고통스러운 절규 미차단"}
        out = U.build(*make(deaths=[self.tank_death()], roster=ROSTER, fm=cry))
        self.assertIn("탱 사망 · 가짜탱", out["lines"]["첫 실수"])
        self.assertIn("탱 가짜탱 사망 4:45", out["lines"]["전멸 원인"])
        self.assertNotIn("괴저 증기", out["lines"]["첫 실수"])
        self.assertEqual(out["lines"]["다음 풀"], U.NEXT["doomscale_tank"])   # 보스 근접보다 파멸비늘이 앞

    def test_not_a_tank_or_too_early(self):
        d = dict(self.tank_death(), name="가짜딜-서버")
        out = U.build(*make(deaths=[d], roster=ROSTER))
        self.assertNotIn("탱 사망", out["lines"]["첫 실수"])
        out = U.build(*make(deaths=[self.tank_death(sec=260.0)], roster=ROSTER))   # 붕괴 34초 전
        self.assertNotIn("탱 사망", out["lines"]["첫 실수"])

    def test_next_by_source_order(self):
        self.assertEqual(U.TANK_NEXT_BY_SOURCE[0][0], "약화된 파멸비늘")
        self.assertEqual(U.TANK_NEXT_BY_SOURCE[-1][0], "울라텍")
        self.assertIn(1286835, U.TANK_IGNORE_SPELLS)


class FangLateBuild(unittest.TestCase):
    def test_fang_late_is_root(self):
        breaks = [("가짜하나", 211.6), ("가짜둘", 211.9), ("가짜셋", 212.4), ("가짜넷", 213.9)]
        fang = [{"sec": 197.0, "event": "SPELL_AURA_APPLIED", "target": n} for n, _ in breaks]
        fang += [{"sec": t, "event": "SPELL_AURA_REMOVED", "target": n} for n, t in breaks]
        ticks = [(211.6, 216685), (213.6, 650055), (215.6, 866740), (217.6, 866740)]
        out = U.build(*make(col=217.0, duration=235.0, spells={"역병의 핏줄": 4},
                            raw_extra={"fang": fang, "veinFresh": [211.6], "veinTick": ticks}))
        self.assertIn("송곳니 줄 늦게 끊음 · 가짜넷 +2.3초", out["lines"]["첫 실수"])
        self.assertIn("역병의 핏줄 중첩 그대로 연장", out["lines"]["전멸 원인"])
        self.assertEqual(out["lines"]["다음 풀"], U.NEXT["fang_late"])


if __name__ == "__main__":
    unittest.main()
