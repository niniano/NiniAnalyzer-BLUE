"""1.0.26: 그날 정리가 윈도우 경로(/ · \\ 섞임)에서도 트라이를 찾는지 — 가짜 이름."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import day_board as db
from live import day_summary_v1 as ds


class DayPathTests(unittest.TestCase):
    def test_row_dir_is_normalized_like_build_day_keys(self):
        raw = "C:/NiniData/raids/try_001_WIPE_201522"
        fact = {"tryNo": 1, "kill": False, "dur": 300.0, "result": "wipe",
                "lines": {"첫 실수": "3:12 현신 접촉 · 가짜하나 [확정]", "다음 풀": "x"},
                "cause": {"group": "P2 전멸", "confidence": "verified"}}
        with mock.patch.object(ds, "try_facts", lambda enc, d, use_cache=True: dict(fact)):
            out = ds.build_day(3429, [raw])
        key = db._row_dir({"storageDir": raw})
        self.assertEqual(key, str(Path(raw)))
        self.assertIn(key, out["rows"])
        self.assertEqual(out["rows"][key]["issue"], "3:12 현신 접촉 · 가짜하나")

    def test_meta_json_fallback(self):
        self.assertEqual(db._row_dir({"metaJson": "D:/x/try_002/meta.json"}), str(Path("D:/x/try_002")))
        self.assertEqual(db._row_dir({}), "")


if __name__ == "__main__":
    unittest.main()
