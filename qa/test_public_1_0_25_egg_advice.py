"""1.0.25: 알 든 채 사망 조언 — 알은 넘길 수 없음."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import ulatek_conclusion_v1 as U  # noqa: E402


class EggAdvice(unittest.TestCase):
    def test_no_passing_egg(self):
        self.assertNotIn("넘기기", U.NEXT["egg_died"])
        self.assertIn("알 들지 않기", U.NEXT["egg_died"])


if __name__ == "__main__":
    unittest.main()
