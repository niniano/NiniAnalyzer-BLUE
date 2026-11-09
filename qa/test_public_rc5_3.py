"""RC5.3: GOLD V5.0 (키식스 신화 · 울라텍 늦은 절규 표시) + GOLD V5.2 (트라이 화면) in the public build.

- 개발용 글자(범위: · n= · raidPlan judge) 숨김, 판정 순서 보통 굵기
- 붕괴 체인 링크(타임라인 → 시간축 주황 선), P2 보호막 칸 네 줄, 암영폭탄 · 현신 접촉 카드
- 똬리 공대 차단 규칙(kick_rules_3429.json)이 빌드에 들어가고 똬리 모듈에 넘어감
- 개인 합본 글꼴은 들어가지 않음
"""
from __future__ import annotations

import gzip
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import v52_screen as V  # noqa: E402

BG = importlib.import_module("live.boss_guides_v1")
UL = importlib.import_module("live.ulatek_conclusion_v1")


class Build(unittest.TestCase):
    def test_hidden_import_and_rules_data(self):
        from build_public_windows import build_command
        args = " ".join(map(str, build_command(ROOT, python="py", data_sep=";")))
        self.assertIn("--hidden-import=live.v52_screen", args)
        self.assertIn("kick_rules_3429.json;.", args)
        rules = json.loads((ROOT / "kick_rules_3429.json").read_text(encoding="utf-8"))
        self.assertEqual(rules.get("secondAdd"), "beforeBomb")

    def test_public_fonts_only(self):
        fonts = {p.name for p in (ROOT / "assets" / "fonts").iterdir()}
        self.assertFalse([f for f in fonts if f.lower().startswith(("naowh", "gotham"))])
        self.assertIn("PyeojinGothic-Medium.ttf", fonts)

    def test_versions(self):
        gui = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        self.assertIn('PUBLIC_APP_VERSION = "1.0.3"', gui)
        self.assertNotIn('"bold" if int(j.get("number") or 1) == 1 else "normal"', gui)
        self.assertNotIn('"bold" if i == 1 else "normal"', gui)
        self.assertEqual(BG.VERSION, "BOSS-GUIDES-V1.6")
        self.assertRegex(UL.VERSION, r"^ULATEK-CONCLUSION-V1\.(8|9|\d\d)")  # RC5.4: V1.9
        coiled = importlib.import_module("live.coiled_wipe_cause_v1")
        self.assertRegex(coiled.VERSION, r"^COILED-WIPE-CAUSE-V3\.[4-9]")  # RC5.4: V3.7
        self.assertEqual(coiled.parse_kick_rules(V.kick_rules(ROOT))["backupLine"], 3)

    def test_layout_generic_name_and_hooks(self):
        lay = (ROOT / "live" / "v43_layout.py").read_text(encoding="utf-8")
        self.assertIn('("내 캐릭터 · 그 순간의 행동", "내 캐릭터")', lay)
        self.assertIn("from live.v52_screen import sanitize as _v52_sanitize", lay)
        self.assertIn("out = _v52_build(mod, key)", lay)
        self.assertIn("_v52.apply(app, row)", lay)
        self.assertIn('st["five_note"] = (note_row, note_txt)', lay)


class Screen(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(V.sanitize("P2 1사이클 · 범위: 로컬 세션 · n=0"), "P2 1사이클")
        self.assertEqual(V.sanitize("대상 계획 미설정 · 범위: 현재 트라이 · 보호막 1 · raidPlan judge n=3 + 관측 n=1"),
                         "대상 계획 미설정 · 보호막 1")
        self.assertEqual(V.sanitize("범위: 현재 세션"), "범위: 현재 세션")

    def test_shield_rows(self):
        sh = {"index": 1, "status": "failed", "startSec": 241.4, "resolutionSec": 258.4, "breakSec": 258.5,
              "breakDurationSec": 17.1, "remainingPct": 13.9, "breakMarginSec": -0.13, "castSuccessSec": 258.4,
              "scheduleDeviationSec": 8.35, "absorbedPerSec": 1762814, "initialAbsorb": 34828260,
              "castDurationObservedSec": 17.0, "deathsDuring": [{"player": "가-x"}],
              "cooldownAlignment": {"autoSpecCooldowns": {
                  "judgedRequirements": [{"player": "나-x", "label": "정조준", "status": "missing"}],
                  "observedRequirements": [{"player": "다-x", "label": "마력 주입", "status": "late", "deviationSec": 7.18}]}}}
        rows = V.shield_rows({"observations": {"shieldCycles": [sh]}})
        self.assertEqual([k for k, _ in rows], ["보호막 1", "딜", "쿨기", "사망"])
        self.assertEqual(rows[0][1], "4:01 → 4:18 · 못 깸 ✕ · 13.9% 남음 (0.1초 모자람) → 영원한 일몰 · 계획보다 +8초")
        self.assertEqual(rows[1][1], "초당 1.8M · 깨려면 2.0M (17초 안에 34.8M)")
        self.assertEqual(rows[2][1], "계획 2분 쿨기 0/1 · 안 씀: 나 정조준 · 늦음: 다 마력 주입 +7.2초")
        self.assertEqual(rows[3][1], "보호막 중 사망 1명 (가)")

    def test_bomb_card_ignores_order_rule(self):
        cyc = lambda i, rem: {"index": i, "gloombomb": {
            "soulcoilerExpectedCount": 2, "soulcoilerVulnerabilityKnownCount": 2, "soulcoilerVulnerabilityRemovedCount": rem,
            "damageSec": 196.4 + 37 * (i - 1), "extraHitPlayers": [], "firstInterruptOrderOk": False}}
        card = V.bomb_card({"observations": {"phase2": {"cycles": [cyc(1, 2), cyc(2, 1)]}}})
        self.assertEqual((card["value"], card["status"]), ("1번 2/2 · 2번 1/2", "failed"))
        ok = V.bomb_card({"observations": {"phase2": {"cycles": [cyc(1, 2)]}}})
        self.assertEqual(ok["status"], "success")

    def test_resonance_pair(self):
        def aura(sec, sid, name, who):
            m, s = divmod(sec, 60)
            return (f"10/25/2026 16:{58 + int(m):02d}:{s:07.4f}  SPELL_AURA_APPLIED,Creature-0-1-2-3-257911-0001,\"보스\",0xa48,0x0,"
                    f"Player-1-{who},\"{who}-서버\",0x512,0x0,{sid},\"{name}\",0x20,DEBUFF\n")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "raw.log.gz"
            with gzip.open(p, "wt", encoding="utf-8") as fh:
                fh.write(aura(10.0, 1285911, "섬뜩한 집착", "가"))
                fh.write(aura(11.0, 1285911, "섬뜩한 집착", "나"))
                fh.write(aura(20.0, 1310744, "악의적인 공명", "가"))
                fh.write(aura(20.02, 1310744, "악의적인 공명", "나"))
            eps = V.scan_resonance(p, "10/25/2026 16:58:00.0000")
        self.assertEqual([(e["players"], e["level"]) for e in eps], [(["가", "나"], "[확정]")])
        self.assertEqual(V.resonance_lines(eps)[0], "0:20 현신 접촉 · 가 ↔ 나 · 사망 없음 [확정]")

    def test_clock(self):
        self.assertEqual(V.parse_clock("4:18.5"), 258.5)
        self.assertIsNone(V.parse_clock("타임라인 →"))

    @unittest.skipUnless(os.name == "nt" or os.environ.get("DISPLAY"), "no screen")
    def test_link_scrolls_to_timeline(self):
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(str(exc))
        root = tk.Tk()
        try:
            c = tk.Canvas(root, width=600, height=200)
            for s, x in ((0, 100), (30, 200), (60, 300)):
                c.create_text(x, 180, text=f"{s // 60}:{s % 60:02d}")

            class App:
                pass
            app = App()
            app.b3641_try_timeline = c
            body = tk.Frame(root)
            line = tk.Frame(body)
            tk.Label(line, text="0:45.0").pack()
            card = tk.Frame(line)
            link = tk.Label(card, text="타임라인 →")
            link.pack(); card.pack(); line.pack(); body.pack()
            app.b3796_chain_panel = type("P", (), {"body": body})()
            V.wire_links(app)
            self.assertEqual(str(link.cget("cursor")), "hand2")
            V.goto_timeline(app, V._card_time(link))
            marks = c.find_withtag("v52_mark")
            self.assertTrue(marks)
            self.assertAlmostEqual(c.coords(marks[0])[0], 250.0)
        finally:
            root.destroy()


class KithixMythic(unittest.TestCase):
    META = {"encounterID": 3513, "difficultyID": 16, "durationSec": 236.0, "result": "Wipe"}
    HP = [(1.0, 99.0), (115.0, 68.0), (224.0, 36.0), (235.0, 34.0)]

    @staticmethod
    def facts():
        cast = lambda sid, t: {"spellID": sid, "event": "SPELL_CAST_START", "times": t, "count": len(t)}
        return {"encounter": {"encounterID": 3513, "difficultyID": 16},
                "roster": [{"name": f"P{i}-x", "specID": 71} for i in range(20)], "deaths": [],
                "enemyCastSummary": [cast(1302884, [5.0, 126.0, 235.0]), cast(1302951, [52.0, 161.0])]}

    def test_hp_checks(self):
        r = BG.build(self.META, self.facts(), self.HP, {})
        self.assertIn("2웨이브 1:55 HP 68% ⚠ (공략 65% · 1페 끝 보스 이동)", r["lines"]["딜·힐"])

    def test_stack_notes(self):
        rawc = {"stack": {1308875: {"A": {"max": 3, "sec": 120.0}}}, "tankStack": [], "fearImmune": []}
        r = BG.build(self.META, self.facts(), self.HP, rawc)
        self.assertTrue(r["notes"] and r["notes"][0].startswith("무효화"))


class UlatekLateCry(unittest.TestCase):
    def test_late_kick_is_display_only(self):
        raw = {"cry": [{"sec": 110.0, "kind": "start", "add": "a2"}, {"sec": 113.1, "kind": "kick", "add": "a2", "by": "Y"}]}
        cr = UL.cry_review(raw)
        self.assertEqual([k["by"] for k in cr["late"]], ["Y"])
        self.assertIn("[표시만]", cr["lateLine"])


if __name__ == "__main__":
    unittest.main()
