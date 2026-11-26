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


if __name__ == "__main__":
    unittest.main()
