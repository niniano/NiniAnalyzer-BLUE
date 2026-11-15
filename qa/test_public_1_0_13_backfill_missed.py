"""BLUE 1.0.13 (GOLD V5.4.7): 놓친 트라이 채우기 — 감시가 뒤 풀만 잡은 뒤 앞 풀을 채우고 Try 번호를 시간순으로 다시 매긴다. 이름은 가짜."""
import gc
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live.public_import import import_public_combat_log, missing_pull_count, renumber_tries


def pull(minute, kill=0):
    start = f"9/28/2026 12:{minute:02d}:00.0000"
    end = f"9/28/2026 12:{minute + 2:02d}:00.0000"
    return [
        f'{start}  ENCOUNTER_START,999,"Test",16,3,1',
        f'9/28/2026 12:{minute:02d}:01.0000  SPELL_DAMAGE,Player-2,"Dps",0x514,0x0,Creature-1,"Boss",0x40,0x0,333,"Hit",0x1,100',
        f'9/28/2026 12:{minute + 1:02d}:30.0000  SPELL_DAMAGE,Player-2,"Dps",0x514,0x0,Creature-1,"Boss",0x40,0x0,333,"Hit",0x1,100',
        f'{end}  ENCOUNTER_END,999,"Test",16,3,{kill},120000',
    ]


def metas(root):
    rows = []
    for p in Path(root).rglob("meta.json"):
        if p.parent.name.startswith("try_"):
            m = json.loads(p.read_text(encoding="utf-8"))
            rows.append((m["startTimestamp"], m["tryNo"]))
    return sorted(rows)


class Backfill(unittest.TestCase):
    def setUp(self):
        # 앞선 화면 테스트가 남긴 Tk 객체를 여기(주 스레드)서 먼저 치운다.
        # 가져오기는 작업 스레드를 쓰는데, Tk 객체가 그 스레드에서 수거되면 Tcl 이 프로세스를 죽인다.
        gc.collect()

    def test_fill_earlier_pulls_and_renumber(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            store = td / "store"
            late = td / "WoWCombatLog-late.txt"          # 감시가 잡은 것 = 세 번째 풀만
            late.write_text("\n".join(pull(30)) + "\n", encoding="utf-8")
            first = import_public_combat_log(late, store)
            self.assertEqual(first["imported"], 1)
            self.assertEqual([n for _, n in metas(store)], [1])

            full = td / "WoWCombatLog-full.txt"          # 실제 로그 = 풀 세 개
            full.write_text("\n".join(pull(10) + pull(20) + pull(30)) + "\n", encoding="utf-8")
            keys = {("9/28/2026 12:30:00.0000", 999, 16)}
            self.assertEqual(missing_pull_count(full, keys), 2)
            out = import_public_combat_log(full, store, active_dir_name="_active_backfill", only_if_missing=True)
            self.assertEqual((out["imported"], out["skipped"]), (2, 1))
            self.assertGreaterEqual(out["renumbered"], 1)
            # 시간순 1 · 2 · 3 (늦게 넣은 앞 풀이 2 · 3 으로 붙지 않는다)
            self.assertEqual([n for _, n in metas(store)], [1, 2, 3])
            # 다시 돌리면 놓친 것이 없으니 로그를 재생하지 않는다
            again = import_public_combat_log(full, store, only_if_missing=True)
            self.assertTrue(again.get("nothingMissing"))
            self.assertEqual(again["lines"], 0)
            self.assertEqual(len(metas(store)), 3)

    def test_live_pull_in_progress_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            full = td / "WoWCombatLog-full.txt"
            full.write_text("\n".join(pull(10) + pull(20)) + "\n", encoding="utf-8")
            live = {("9/28/2026 12:20:00.0000", 999, 16)}   # 실시간 감시가 지금 잡고 있는 풀
            out = import_public_combat_log(full, td / "store", extra_keys=lambda: live)
            self.assertEqual((out["imported"], out["skipped"]), (1, 1))
            self.assertEqual(len(metas(td / "store")), 1)

    def test_renumber_keeps_original_number(self):
        with tempfile.TemporaryDirectory() as td:
            boss = Path(td) / "2026-09-28" / "999_Test_신화"
            for name, minute, no in (("try_001_WIPE_a", 30, 1), ("try_002_WIPE_b", 10, 2)):
                d = boss / name
                d.mkdir(parents=True)
                (d / "meta.json").write_text(json.dumps({
                    "encounterID": 999, "difficultyID": 16, "tryNo": no, "durationSec": 120.0, "result": "Wipe",
                    "startTimestamp": f"9/28/2026 12:{minute:02d}:00.0000"}), encoding="utf-8")
            self.assertEqual(renumber_tries([boss]), 2)
            a = json.loads((boss / "try_001_WIPE_a" / "meta.json").read_text(encoding="utf-8"))
            b = json.loads((boss / "try_002_WIPE_b" / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual((b["tryNo"], a["tryNo"]), (1, 2))
            self.assertEqual((b["tryNoBeforeRenumber"], a["tryNoBeforeRenumber"]), (2, 1))
            self.assertEqual(renumber_tries([boss]), 0)


if __name__ == "__main__":
    unittest.main()
