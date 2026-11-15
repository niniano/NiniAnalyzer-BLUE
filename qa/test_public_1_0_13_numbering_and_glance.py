"""BLUE 1.0.13 (GOLD V5.4.9): Try 번호 겹침 고침 · 한눈에 보기 보완 (알 충돌 → 알끼리 3미터 · 알 놓침 추정). 이름은 가짜."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import v544_glance as G
from live.public_import import renumber_recent_sessions


def meta(boss, name, no, hhmm):
    d = boss / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "encounterID": 999, "difficultyID": 16, "tryNo": no, "durationSec": 120.0, "result": "Wipe",
        "startTimestamp": f"9/28/2026 {hhmm}:00.0000"}), encoding="utf-8")


def numbers(boss):
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in boss.glob("try_*/meta.json")]
    return [m["tryNo"] for m in sorted(rows, key=lambda m: m["startTimestamp"])]


class Renumber(unittest.TestCase):
    def test_duplicate_number_from_live_pull_during_backfill(self):
        # 채우기가 21번까지 저장했을 때 시작한 실시간 풀이 22번을 받고, 채우기가 끝난 뒤(1~27)에도 22로 남은 경우
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            boss = root / "2026-09-28" / "999_Test_신화"
            meta(boss, "try_021_WIPE_a", 21, "16:16")
            meta(boss, "try_022_WIPE_b", 22, "16:18")
            meta(boss, "try_023_WIPE_c", 23, "16:22")
            meta(boss, "try_022_WIPE_d", 22, "16:50")     # 겹친 번호
            meta(boss, "try_028_WIPE_e", 28, "16:56")
            old = root / "2026-09-20" / "999_Test_신화"      # 예전 날짜는 건드리지 않는다 (dates=1)
            meta(old, "try_005_WIPE_x", 5, "20:00")
            self.assertEqual(renumber_recent_sessions(root, dates=1), 5)
            self.assertEqual(numbers(boss), [1, 2, 3, 4, 5])
            self.assertEqual(numbers(old), [5])
            self.assertEqual(renumber_recent_sessions(root, dates=1), 0)   # 바뀔 것이 없으면 아무것도 안 씀

    def test_missing_root_is_safe(self):
        self.assertEqual(renumber_recent_sessions(Path("/nonexistent/none")), 0)


BASE = {
    "version": "ULATEK-CONCLUSION-V2.0", "durationSec": 65.0,
    "plan": {"eggCarry": {"rounds": [{"bad": 0}]}, "viper": {"waveCarriers": [], "stuns": []}},
    "lines": {"어디까지": "1페 · 0심장 · 1:05 전멸 · 보스 HP 92.5%",
              "첫 실수": "1페 0:37 알 충돌 가나다·라마바 0:37 → 살무사 0:37 [확정]",
              "전멸 원인": "알 충돌 가나다·라마바 0:37 → 살무사 2마리 0:37 → 붕괴 막을 수 없는 분노 5명 (0:43) [확정]",
              "딜·힐": "-",
              "다음 풀": "알 든 사람은 파도 피하기가 최우선 (꼬리 파도 방향 미리 · 이동기 아끼기) · 알끼리 3미터 · 살무사 주시 대상은 떨어지기."},
}


class Glance(unittest.TestCase):
    def test_collision_next_is_three_meters(self):
        self.assertEqual(G.build(BASE, {"deaths": []}, "Wipe")["next"], "알 든 사람끼리 3미터 떨어지기")

    def test_wave_next_unchanged(self):
        out = dict(BASE, lines=dict(BASE["lines"], **{"첫 실수": "1페 2:00 알 든 가나다 부식의 파도 2:00 → 살무사 2:01 [확정]"}))
        self.assertEqual(G.build(out, {"deaths": []}, "Wipe")["next"], "알 든 사람은 파도 피하기가 최우선")

    def test_dropped_egg_is_not_a_check_mark(self):
        out = dict(BASE, lines=dict(BASE["lines"], **{"첫 실수": "1페 1:46 알 든 가나다 알 놓침 1:46 (물·피격?) → 살무사 1:46 [추정]"}))
        card = next(c for c in G.build(out, {"deaths": []}, "Wipe")["stats"] if c["k"] == "알 운반")
        self.assertEqual((card["v"], card["tone"]), ("놓침 추정", "orange"))
        card = next(c for c in G.build(BASE, {"deaths": []}, "Wipe")["stats"] if c["k"] == "알 운반")
        self.assertEqual((card["v"], card["tone"]), ("✓", "green"))


if __name__ == "__main__":
    unittest.main()
