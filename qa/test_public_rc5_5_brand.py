"""RC5.5: 아이콘 · 로고를 Nini Brand Kit R3 (BLUE)로 교체.

- 원형 아이콘: 남색 #0D3952 원 · 파란 #5AB2F6 한 줄 N · 원 밖은 투명 (예전 흰 네모 없음)
- ICO 16~256 · PNG 16~512 · 왼쪽 위 로고 196x42 (글자 가운데 = 아이콘 가운데)
"""
from __future__ import annotations

import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "resources" / "brand_public"
NAVY = (0x0D, 0x39, 0x52)
BLUE = (0x5A, 0xB2, 0xF6)


def near(a, b, tol=6):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


class Icon(unittest.TestCase):
    def test_round_navy_no_white_tile(self):
        for s in (16, 24, 32, 40, 48, 64, 128, 256, 512):
            im = Image.open(BRAND / f"nini_analyzer_{s}.png").convert("RGBA")
            self.assertEqual(im.size, (s, s))
            self.assertEqual(im.getpixel((0, 0))[3], 0, f"{s}: corner must be transparent")
            self.assertTrue(near(im.getpixel((s // 2, max(1, s // 12)))[:3], NAVY), f"{s}: navy circle")
            px = [im.getpixel((x, y))[:3] for x in range(s) for y in range(s) if im.getpixel((x, y))[3] > 250]
            self.assertTrue(any(near(p, BLUE, 10) for p in px), f"{s}: blue N")
            self.assertFalse(any(min(p) > 240 for p in px), f"{s}: no white tile")

    def test_ico_sizes(self):
        sizes = Image.open(BRAND / "nini_analyzer.ico").info["sizes"]
        for s in (16, 24, 32, 48, 64, 128, 256):
            self.assertIn((s, s), sizes)


class Sidebar(unittest.TestCase):
    def test_wordmark_centred_on_icon(self):
        im = Image.open(BRAND / "nini_analyzer_sidebar_wordmark.png").convert("RGBA")
        self.assertEqual(im.size, (196, 42))
        w, h = im.size
        # 아이콘(왼쪽 원) 세로 범위와 글자(파란 Nini) 세로 범위의 가운데가 1px 안에서 같다
        icon_rows = [y for y in range(h) if any(near(im.getpixel((x, y))[:3], NAVY) and im.getpixel((x, y))[3] > 200 for x in range(0, 44))]
        text_rows = [y for y in range(h) if any(near(im.getpixel((x, y))[:3], BLUE, 12) and im.getpixel((x, y))[3] > 200 for x in range(50, w))]
        self.assertTrue(icon_rows and text_rows)
        icon_mid = (icon_rows[0] + icon_rows[-1]) / 2
        text_mid = (text_rows[0] + text_rows[-1]) / 2
        self.assertLessEqual(abs(icon_mid - text_mid), 1.0)

    def test_version(self):
        gui = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        self.assertIn('PUBLIC_APP_VERSION = "1.0.0-rc5.5"', gui)


if __name__ == "__main__":
    unittest.main()
