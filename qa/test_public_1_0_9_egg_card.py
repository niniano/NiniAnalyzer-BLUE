"""BLUE 1.0.9 (GOLD V5.4.2): 울라텍 「알 운반」 카드 (누가 알을 들었나) · 참고 줄 짧게 · 작은 알만 드는 캐릭터. 이름은 가짜."""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import ulatek_conclusion_v1 as U


def shell(*spans):
    out = []
    for a, b, n in spans:
        out.append({"sec": a, "event": "SPELL_AURA_APPLIED", "target": n})
        if b is not None:
            out.append({"sec": b, "event": "SPELL_AURA_REMOVED", "target": n})
    return out


ROUND1 = [(15.0 + i, 40.0, n) for i, n in enumerate("가나다라마바사")] + [(22.0, 32.0, "아")]


class EggCarry(unittest.TestCase):
    def test_rounds_and_early_drop(self):
        raw = {"shell": shell(*ROUND1, *[(90.0 + i, 134.0, n) for i, n in enumerate("가나다라")])}
        ec = U.egg_carry_review(raw, [], None, room_start=165.0)
        self.assertEqual([r["label"] for r in ec["rounds"]], ["1차", "2차"])
        r1 = ec["rounds"][0]
        self.assertEqual(r1["count"], 8)
        self.assertEqual(r1["bad"], 1)
        self.assertEqual(r1["items"][0]["name"], "아")            # 문제 있는 사람이 앞
        self.assertEqual(r1["items"][0]["flags"][0]["text"], "먼저 놓음 0:32")
        lines = U.egg_carry_lines(ec)
        self.assertEqual(lines[0]["head"], "1차 0:15")
        self.assertEqual(lines[0]["count"], "8명 · 문제 1")
        self.assertEqual(lines[1]["count"], "4명 ✓")

    def test_two_drop_batches_not_early(self):
        # 2:14 · 2:17 두 번에 나눠 넣어도 정상
        raw = {"shell": shell(*[(90.0 + i, 134.0 if i % 2 else 137.0, n) for i, n in enumerate("가나다라마바")])}
        ec = U.egg_carry_review(raw, [], None, room_start=165.0)
        self.assertEqual(ec["rounds"][0]["bad"], 0)

    def test_wave_and_viper(self):
        raw = {"shell": shell(*ROUND1[:7], (21.0, 30.0, "아"))}
        vr = {"waves": [(29.8, "아")], "bursts": [], "clusters": [{"sec": 30.5, "count": 2, "names": ["아"], "triggerSec": 29.8}]}
        it = U.egg_carry_review(raw, [], vr, 165.0)["rounds"][0]["items"][0]
        self.assertEqual([f["text"] for f in it["flags"]], ["파도 0:29", "→ 살무사 0:30"])
        self.assertTrue(it["bad"])

    def test_wipe_grouped_not_blamed(self):
        raw = {"shell": shell(*[(90.0 + i, 134.2, n) for i, n in enumerate("가나다라")], (91.0, 120.0, "마"))}
        deaths = [{"name": n + "-서버", "sec": 134.2, "lastHit": {"spellName": "유령의 똬리"}} for n in "가나다"]
        vr = {"waves": [(120.0, "마")], "bursts": [], "clusters": []}
        r = U.egg_carry_review(raw, deaths, vr, 165.0, collapse_at=134.5)["rounds"][0]
        self.assertEqual(r["bad"], 1)   # 파도 맞은 「마」만 문제 · 전멸 중 사망 3명은 회색
        wipe = [it for it in r["items"] if any(f["kind"] == "wipe" for f in it["flags"])]
        self.assertEqual(len(wipe), 3)
        tags = [t for _p, t in U.egg_carry_lines({"rounds": [r]})[0]["parts"]]
        self.assertEqual(tags.count("wipe"), 3)
        self.assertEqual(tags.count("bad"), 1)

    def test_room_and_big_egg(self):
        hearts = [{"index": 1, "startSec": 145.0, "endSec": 165.0}]
        raw = {"available": True, "bigEggHatch": [174.0],
               "shell": shell((222.0, 234.0, "가"), (223.0, 235.0, "나")),
               "bigEgg": [{"sec": 254.0, "event": "SPELL_AURA_APPLIED", "target": "다"},
                          {"sec": 259.0, "event": "SPELL_AURA_APPLIED", "target": "라"}]}
        be = U.big_egg_review(raw, hearts, [], 280.0)
        ec = U.egg_carry_review(raw, [], None, 165.0, be, owner="가")
        self.assertEqual(ec["rounds"][0]["label"], "알방")
        lines = U.egg_carry_lines(ec)
        self.assertEqual(lines[0]["parts"][0], ("가 3:42→3:54", "owner"))   # 알방 시각은 내 캐릭터만
        self.assertEqual(lines[0]["parts"][1], ("나", None))
        self.assertEqual(lines[1]["head"], "큰 알 기한 4:15")
        self.assertEqual(lines[1]["count"], "2/4")
        self.assertEqual(lines[1]["parts"][1], ("라 4:19 늦음", "late"))
        # 참고 줄(short)은 누가 들었는지 빼고 짧게 · 예전 line 은 그대로
        self.assertEqual(be["short"], "알방 큰 알 2/4 (기한 4:15 · 4:40 전멸) · 늦음 1 · 안 든 큰 알 2개 → 4:24 게걸스러운 파멸비늘")
        self.assertIn("다 4:14", be["line"])


class SmallEggOnly(unittest.TestCase):
    HEARTS = [{"index": 1, "startSec": 145.0, "endSec": 165.0}]
    DUTY = {"players": ["가", "나"], "smallEggOnly": ["나"], "bigEggBySec": 255}

    def raw(self, *spans):
        return {"available": True, "bigEggHatch": [174.0], "shell": shell(*spans), "bigEgg": []}

    def test_small_only_no_big_egg_judgement(self):
        be = U.big_egg_review(self.raw((226.0, 237.0, "나")), self.HEARTS, [], 300.0, self.DUTY, ["나-서버"])
        self.assertTrue(be["line"].startswith("나 알방(작은 알): 작은 알 3:46→3:57 | 알방 큰 알 0/4"))
        self.assertNotIn("큰 알 안 듦", be["line"])
        self.assertTrue(be["duty"]["smallEggOnly"])

    def test_small_only_died_before(self):
        d = [{"name": "나-서버", "sec": 218.0, "lastHit": {"spellName": "역병의 핏줄"}}]
        be = U.big_egg_review(self.raw(), self.HEARTS, d, 239.0, self.DUTY, ["나-서버"])
        self.assertTrue(be["short"].startswith("나 알방(작은 알): 작은 알 전 사망 3:38(역병의 핏줄)"))

    def test_main_character_still_big_egg(self):
        be = U.big_egg_review(self.raw(), self.HEARTS, [], 300.0, self.DUTY, ["가-서버"])
        self.assertIn("가 알방: 첫 알 안 듦 · 큰 알 안 듦", be["line"])


class NotesShort(unittest.TestCase):
    def test_viper_notes_without_names(self):
        vr = {"clusters": [{"sec": 120.0, "count": 2}], "waves": [(119.0, "가")], "bursts": [(100.0, "나"), (100.2, "다")], "marks": []}
        self.assertEqual(U.egg_viper_notes(vr, names=False)[0], "알 든 사람 부식의 파도 1번 · 알 충돌 1번 → 살무사 2마리 (2:00 2)")
        self.assertIn("가 1:59", U.egg_viper_notes(vr)[0])


class Card(unittest.TestCase):
    def test_card_renders_when_display(self):
        if sys.platform not in {"win32", "darwin"} and not os.environ.get("DISPLAY"):
            self.skipTest("no display")
        import tkinter as tk
        from live import v43_layout as V
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        try:
            body = tk.Frame(root)
            body.pack()

            class App:
                pass
            app = App()
            app._v43 = {}
            app.b3739_cards_box = type("B", (), {"body": body})()
            lines = [{"head": "1차 0:15", "count": "8명 · 문제 1", "parts": [("아 먼저 놓음 0:32", "bad"), ("가", None)]}]
            V._egg_card(app, lines)
            root.update()
            frame, txt = app._v43["egg_card"]
            self.assertTrue(frame.winfo_ismapped())
            self.assertIn("아 먼저 놓음 0:32", txt.get("1.0", "end"))
            self.assertIn("bad", txt.tag_names("2.0"))
            V._egg_card(app, [])
            root.update()
            self.assertFalse(frame.winfo_ismapped())
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
