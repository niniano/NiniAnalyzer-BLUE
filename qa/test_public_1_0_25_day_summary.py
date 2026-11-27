"""1.0.25 그날 최종 정리 · 보완사항 (보드 칸) — 가짜 이름 · 가짜 트라이 자료."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import day_summary_v1 as ds


def _coiled(no, kill=False, **kw):
    f = {"tryNo": no, "kill": kill, "dur": 450.0, "result": "kill" if kill else "wipe",
         "lines": {"첫 실수": kw.get("first", "확정된 첫 실수 없음"), "전멸 원인": kw.get("cause_line", "6:40 공대 붕괴 [추정]"),
                   "다음 풀": "다음", "어디까지": "막페"},
         "cause": {"group": None if kill else kw.get("group", "막페 공포의 통곡 못 끊음"), "confidence": kw.get("conf", "verified")},
         "p1Switch": kw.get("p1", 125.0), "p1Soak": {"D": 4, "H": 4},
         "g2": kw.get("g2"), "wail": kw.get("wail", {"starts": 10, "kicks": {"가짜하나": 5, "가짜둘": 4}, "done": 1, "doneHealthy": 0}),
         "resonance": kw.get("res", False), "heal": kw.get("heal")}
    return f


class DaySummaryCoiledTests(unittest.TestCase):
    def _run(self, facts):
        it = iter(facts)
        with mock.patch.object(ds, "try_facts", lambda enc, d, use_cache=True: next(it)):
            return ds.build_day(3429, [f"/x/try_{i}" for i in range(len(facts))])

    def test_wail_healthy_completion_makes_todo_and_rows(self):
        fs = [_coiled(1, first="6:36 공포의 통곡 못 끊음 → 6:47 공대 공포 (아무도 안 끊음) [확정]",
                      wail={"starts": 12, "kicks": {"가짜하나": 10, "가짜둘": 1}, "done": 1, "doneHealthy": 1}),
              _coiled(2, kill=True)]
        out = self._run(fs)
        self.assertTrue(any("공포의 통곡" in s and "가짜하나 혼자" in s for s in out["summary"]))
        self.assertTrue(any(t.startswith("통곡 차단 2~3명 순번 (가짜하나") for t in out["todo"]))
        row = out["rows"][str(Path("/x/try_0"))]
        self.assertEqual(row["tag"], "verified")
        self.assertNotIn("[확정]", row["issue"])
        self.assertEqual(out["rows"][str(Path("/x/try_1"))]["tag"], "kill")

    def test_second_guillotine_zero_soak_is_failure_not_pass(self):
        fs = [_coiled(1, g2={"mark": 430, "afterMk": 84, "soak": 0, "exec": 0, "target": "가짜셋", "why": "공포"}),
              _coiled(2, g2={"mark": 430, "afterMk": 84, "soak": 6, "exec": 0, "target": "가짜넷", "why": ""}),
              _coiled(3, g2={"mark": 430, "afterMk": 84, "soak": 1, "exec": 14, "target": "가짜넷", "why": "조원 먼저 사망"})]
        out = self._run(fs)
        line = next(s for s in out["summary"] if s.startswith("막페 2단두대"))
        self.assertIn("넘김 1 (막은 인원 6)", line)
        self.assertIn("실패 #1, #3", line)
        self.assertIn("막페 시작 +84초", line)
        self.assertTrue(any("2단두대" in t for t in out["todo"]))

    def test_p1_reference_and_top_side_healers(self):
        heal = {"topThin": 35, "isolation": [{"name": "가짜힐", "after": "암영폭탄", "sec": 100, "dur": 6.0}] * 3,
                "orbCarry": {"가짜하나": 5}}
        out = self._run([_coiled(1, heal=heal), _coiled(2, heal=dict(heal, isolation=[]))])
        self.assertTrue(any("1페 전환 평균 2:05" in s and "딜 부족 아님" in s for s in out["summary"]))
        self.assertTrue(any("위쪽에 고정" in t for t in out["todo"]))
        self.assertTrue(any(t.startswith("가짜힐: 암영폭탄") for t in out["todo"]))
        self.assertFalse(any("1페 단두대는" in t for t in out["todo"]))   # 전환이 빠르면 단두대 역할은 그대로

    def test_repeats_group_wipes(self):
        fs = [_coiled(i, group="P2 누적 붕괴") for i in (3, 1, 2)]
        out = self._run(fs)
        self.assertEqual(out["repeats"][0], {"label": "P2 누적 붕괴", "count": 3, "tries": [1, 2, 3]})


class DaySummaryUlatekTests(unittest.TestCase):
    def test_egg_lake_fang(self):
        base = {"dur": 300.0, "result": "wipe", "kill": False, "heart1Short": False,
                "fang": [{"late": ["가짜하나"], "spread": 9.0, "extra": 1}], "bigEgg": {"picks": [247.0], "drops": [265.0]},
                "south": {"actual": "big_first", "enraged": 1}, "lake": {"n": 3, "dead": 2, "lakeKill": 1, "short": 2, "shortKill": 0}}
        fs = [dict(base, tryNo=1, lines={"첫 실수": "1페 1:58 알 든 가짜둘 부식의 파도 1:58 → 살무사 1:59 [확정]", "다음 풀": "x"},
                   cause={"group": "알 운반 → 살무사", "confidence": "verified"}),
              dict(base, tryNo=2, lines={"첫 실수": "2페(알방) 3:48 알 든 가짜셋 사망 3:48 → 알 깨짐 → 살무사 [확정]", "다음 풀": "x"},
                   cause={"group": "알 운반 → 살무사", "confidence": "verified"})]
        it = iter(fs)
        with mock.patch.object(ds, "try_facts", lambda enc, d, use_cache=True: next(it)):
            out = ds.build_day(3492, ["/u/1", "/u/2"])
        s = "\n".join(out["summary"])
        self.assertIn("부식의 파도 1 · 알 든 채 사망 1", s)
        self.assertIn("첫 끊기 → 마지막까지 평균 9.0초", s)
        self.assertIn("큰 알 들기 4:07 · 넣기 4:25 (부화 2:54 고정)", s)
        self.assertIn("똥물(죽음의 맹독) 1초에 생명력 약 11% · 빠짐 6번 · 똥물로 사망 2 · 빠진 채 다른 피해로 사망 2", s)   # 1.0.29
        self.assertTrue(any("알 운반자 힐·생존기 우선" in t for t in out["todo"]))
        self.assertFalse(any("넘기" in t for t in out["todo"]))   # 알은 넘길 수 없음

    def test_cache_written_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "meta.json").write_text('{"tryNo": 1, "result": "wipe", "durationSec": 60}', encoding="utf-8")
            fake = mock.Mock()
            fake.VERSION = "FAKE-1"
            fake.build_from_dir = mock.Mock(return_value={"lines": {"첫 실수": "x"}, "plan": {}})
            with mock.patch.object(ds, "_module", lambda enc: fake), \
                 mock.patch("live.v52_screen.build_module", lambda mod, key: mod.build_from_dir(key)):
                self.assertTrue(ds.needs_compute(3492, d))
                ds.try_facts(3492, d)
                self.assertTrue((d / ds.CACHE_NAME).is_file())
                self.assertFalse(ds.needs_compute(3492, d))
                ds.try_facts(3492, d)
                self.assertEqual(fake.build_from_dir.call_count, 1)
                fake.VERSION = "FAKE-2"          # 결론 모듈이 바뀌면 다시 계산
                self.assertTrue(ds.needs_compute(3492, d))


def _app(test):
    if sys.platform not in {"win32", "darwin"} and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        test.skipTest("GUI display is not available")
    import tkinter as tk
    from nini_wcl_gui import App
    try:
        return App(edition="raid")
    except tk.TclError as exc:
        test.skipTest(f"Tk display is not available: {exc}")


class DayBoardPanelTests(unittest.TestCase):
    def test_panel_waits_then_overrides_board(self):
        from live import day_board as db
        app = _app(self)
        try:
            st = db._st(app)
            self.assertIsNotNone(st.get("panel"))
            pairs = [({"tryNo": 1, "storageDir": "/d/t1"}, {}), ({"tryNo": 2, "storageDir": "/d/t2"}, {})]
            model = {"rows": [{"tryNo": 1, "result": "와이프", "issue": "원인 확인 불가 · x"},
                              {"tryNo": 2, "result": "와이프", "issue": "확정 원인 없음"}],
                     "repeats": [], "voiceOrders": ["예전"]}
            with mock.patch.object(db, "_start") as start:
                out = db.apply(app, 3429, pairs, "2026-01-05", model)
                start.assert_called_once()
            self.assertEqual(out["voiceOrders"], ["예전"])      # 계산 전엔 그대로
            key = db._key(3429, "2026-01-05", [db._row_dir({"storageDir": "/d/t1"}), db._row_dir({"storageDir": "/d/t2"})])
            st["results"][key] = {"tries": 2, "summary": ["2트"], "todo": ["가", "나", "다", "라"],
                                  "rows": {str(Path("/d/t1")): {"tryNo": 1, "issue": "6:36 공포의 통곡 못 끊음", "tag": "verified"}},
                                  "repeats": [{"label": "막페 공포의 통곡 못 끊음", "count": 2, "tries": [1, 2]}]}
            out = db.apply(app, 3429, pairs, "2026-01-05", model)
            self.assertEqual(out["voiceOrders"], ["가", "나", "다", "라"])   # 보완사항 전부 (3개 제한 없음)
            self.assertTrue(out["voiceOrdersFull"])
            self.assertEqual(out["rows"][0]["issue"], "6:36 공포의 통곡 못 끊음")
            self.assertEqual(app._b3793_issue(out["rows"][0], None), ("6:36 공포의 통곡 못 끊음", "verified"))
            self.assertNotIn("dayTag", out["rows"][1])
            self.assertEqual(out["repeats"][0]["count"], 2)
            self.assertNotIn("right", st["panel"])          # 아래 칸엔 수치만
            app._b3798_board_sig = None
            app._render_b3793_board(out, pairs, "x")
            self.assertEqual(len(app.b3793_voice_list.winfo_children()), 4)
            # 다른 보스는 칸을 숨기고 모델을 안 건드림
            out2 = db.apply(app, 3513, pairs, "2026-01-05", model)
            self.assertIs(out2, model)
            self.assertFalse(st["panel"]["box"].winfo_manager())
        finally:
            app.destroy()

    def test_waiting_text_says_not_frozen(self):
        from live import day_board as db
        app = _app(self)
        try:
            st = db._st(app)
            key = ("k",)
            st["shown"] = key
            st["jobs"][key] = {"i": 2, "n": 18, "new": 16, "t0": 0, "done": False, "res": None}
            with mock.patch.object(app, "after"):
                db._poll(app, key)
            txt = " ".join(w.cget("text") for w in st["panel"]["left"].winfo_children())
            self.assertIn("멈춘 거 아님", txt)
            self.assertIn("2/18", txt)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
