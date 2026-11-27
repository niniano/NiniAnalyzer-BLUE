"""1.0.25: 똬리 공포의 통곡을 못 끊음 → 공대 공포 → 붕괴 (이름은 가짜)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import coiled_wipe_cause_v1 as W


def ev(sec, kind, add="A1", by=None):
    e = {"sec": sec, "kind": kind, "add": add, "marked": False}
    if by:
        e.update({"by": by, "byGuid": "x"})
    return e


def dd(sec, n="가짜"):
    return {"name": f"{n}{sec}-서버", "sec": sec, "lastHit": {"spellName": "냉혹한 처형"}}


class WailRoot(unittest.TestCase):
    def test_kicked_then_recast_unkicked(self):
        events = [ev(390.8, "start"), ev(395.8, "kick", by="가짜탱"), ev(396.1, "start"), ev(406.0, "done"),
                  ev(300.0, "kick", add="A0", by="가짜탱"), ev(310.0, "kick", add="A0", by="가짜딜")]
        deaths = [dd(410.0 + i * 0.1) for i in range(15)]
        r = W.wail_root(events, deaths, 407.0)
        self.assertEqual(r["sec"], 406.0)
        self.assertEqual(r["last"], "가짜탱")
        self.assertEqual(r["share"][0], ("가짜탱", 2))

    def test_after_collapse_is_not_root(self):
        events = [ev(430.0, "start"), ev(440.0, "done")]
        deaths = [dd(400.0 + i) for i in range(10)]
        self.assertIsNone(W.wail_root(events, deaths, 405.0))

    def test_build_cause(self):
        raw = {"available": True, "hp": [], "wail": [ev(390.8, "start"), ev(400.8, "done")]}
        meta = {"tryNo": 14, "durationSec": 420.0, "result": "wipe"}
        mech = {"observations": {"phase1Transition": {"entrySec": 125.0}, "phase2": {"endSec": 309.0}}}
        raid = {"collapse": {"startSec": 409.0, "spells": {"맹독성 파열": 8}, "count": 8}}
        facts = {"deaths": [dd(405.0 + i * 0.3) for i in range(18)], "roster": []}
        r = W.build(meta, mech, raid, facts, raw)
        self.assertEqual(r["cause"]["code"], "WAIL_UNKICKED")
        self.assertIn("공포의 통곡 못 끊음 → 6:40 공대 공포 (아무도 안 끊음) [확정]", r["lines"]["첫 실수"])
        self.assertIn("2~3명 순번", r["lines"]["다음 풀"])


if __name__ == "__main__":
    unittest.main()
