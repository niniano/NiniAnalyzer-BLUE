"""BLUE 1.0.16 (GOLD V5.2.6~V5.4.12): WoW 달력 초대 → 구글 캘린더 (기본 꺼짐) · .ics 내보내기 (이름은 가짜).

NiniTools 계정 달력 SavedVariables를 읽고, 구글 캘린더 API 호출은 가짜 전송으로 확인한다.
OAuth 클라이언트는 빌드 때 넣은 것(BUNDLED_CLIENT)보다 직접 고른 것이 먼저.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import wow_calendar_sync as S

SV = r'''
NiniToolsDB = {
	["lastPage"] = "CAL",
	["modules"] = {
		["gold"] = {
			["note"] = "따옴표 \" 와 역슬래시 \\ 와 줄바꿈\n 시험 -- 주석 아님",
		},
		["calendar"] = {
			["settings"] = {
				["enabled"] = true,
				["monthsAhead"] = 1,
			},
			["chars"] = {
				["가나다-서버하나"] = {
					["name"] = "가나다",
					["realm"] = "서버하나",
					["class"] = "ROGUE",
					["synced"] = 1800000000,
					["events"] = {
						{
							["y"] = 2026,
							["m"] = 11,
							["d"] = 4,
							["h"] = 20,
							["mi"] = 30,
							["id"] = "101",
							["title"] = "신화 울라텍",
							["ctype"] = "PLAYER",
							["status"] = 1,
							["by"] = "공대장",
							["diff"] = "신화",
						}, -- [1]
						{
							["y"] = 2026,
							["m"] = 11,
							["d"] = 5,
							["h"] = 21,
							["mi"] = 0,
							["id"] = "102",
							["title"] = "영웅, 막공; 쉼표",
							["ctype"] = "PLAYER",
							["status"] = 0,
							["by"] = "모집",
						}, -- [2]
						{
							["y"] = 2026,
							["m"] = 11,
							["d"] = 6,
							["h"] = 20,
							["mi"] = 0,
							["id"] = "103",
							["title"] = "거절한 일정",
							["ctype"] = "PLAYER",
							["status"] = 2,
						}, -- [3]
					},
				},
				["라마-서버하나"] = {
					["name"] = "라마",
					["synced"] = 1799990000.5,
					["events"] = {
						{
							["y"] = 2026,
							["m"] = 11,
							["d"] = 8,
							["h"] = 12,
							["mi"] = 30,
							["id"] = "201",
							["title"] = "라마공대",
							["ctype"] = "GUILD_EVENT",
							["status"] = 8,
							["itype"] = 1,
						}, -- [1]
					},
				},
			},
			["details"] = {
				["id:101"] = {
					["creator"] = "공대장",
					["desc"] = "8시 20분 집결",
					["updated"] = 1800000000,
					["invites"] = {
						{
							["name"] = "가나다",
							["class"] = "ROGUE",
							["status"] = 1,
						}, -- [1]
						{
							["name"] = "친구-서버둘",
							["class"] = "MAGE",
							["status"] = 8,
						}, -- [2]
					},
				},
			},
			["notes"] = {
			},
		},
	},
}
'''



# 1.0.20: Windows 에서는 비밀값이 DPAPI XML 로 옮겨짐 → 이 테스트는 예전 평문 .json 흐름을 보므로 DPAPI 를 끈다
#   (DPAPI 흐름은 test_v5416_dpapi_secrets / qa test_public_1_0_20_dpapi)
from live import dpapi_secret as _dpapi  # noqa: E402
_DPAPI_WAS = _dpapi._available


def setUpModule():
    _dpapi._available = False


def tearDownModule():
    _dpapi._available = _DPAPI_WAS


class FakeGoogle:
    """구글 캘린더 API 흉내: 캘린더 · 일정 저장, 토큰 만료·401 재현."""

    def __init__(self):
        self.calendars = {}
        self.events = {}
        self.seq = 0
        self.calls = []
        self.force_401 = 0
        self.tokens = 0

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        if url.startswith(S.TOKEN_URI):
            self.tokens += 1
            return 200, json.dumps({"access_token": f"at{self.tokens}", "expires_in": 3600}).encode()
        if self.force_401:
            self.force_401 -= 1
            return 401, b"{}"
        u = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(u.path.replace("/calendar/v3", ""))
        js = json.loads(body.decode()) if body else None
        parts = path.strip("/").split("/")
        if parts == ["calendars"] and method == "POST":
            self.seq += 1
            cid = f"cal{self.seq}@group.calendar.google.com"
            self.calendars[cid] = js
            self.events[cid] = {}
            return 200, json.dumps({"id": cid}).encode()
        cid = parts[1]
        if cid not in self.calendars:
            return 404, b"{}"
        if len(parts) == 2:
            return 200, json.dumps({"id": cid}).encode()
        evs = self.events[cid]
        if len(parts) == 3 and method == "GET":
            q = urllib.parse.parse_qs(u.query)
            assert q["privateExtendedProperty"] == ["niniSync=1"]
            return 200, json.dumps({"items": list(evs.values())}).encode()
        if len(parts) == 3 and method == "POST":
            self.seq += 1
            js["id"] = f"ev{self.seq}"
            evs[js["id"]] = js
            return 200, json.dumps(js).encode()
        eid = parts[3]
        if method == "PUT":
            js["id"] = eid
            evs[eid] = js
            return 200, json.dumps(js).encode()
        if method == "DELETE":
            evs.pop(eid, None)
            return 204, b""
        return 400, b"{}"


def make_env(tmp: Path):
    wow = tmp / "World of Warcraft" / "_retail_"
    sv = wow / "WTF" / "Account" / "TESTACCOUNT" / "SavedVariables" / "NiniTools.lua"
    sv.parent.mkdir(parents=True)
    (wow / "Logs").mkdir(parents=True)
    sv.write_text(SV, encoding="utf-8")
    work = tmp / "home"
    ddir = S.data_dir(work)
    (ddir / "google_client.json").write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "x",
                                                                       "token_uri": S.TOKEN_URI, "auth_uri": S.AUTH_URI}}), encoding="utf-8")
    (ddir / "google_token.json").write_text(json.dumps({"access_token": "at0", "refresh_token": "rt", "expires_at": 9e18}), encoding="utf-8")
    return wow / "Logs", sv, ddir


class Parse(unittest.TestCase):
    def test_wow_style_savedvariables(self):
        db = S.parse_savedvariable(SV.encode("utf-8"), "NiniToolsDB")
        self.assertEqual(db["lastPage"], "CAL")
        self.assertEqual(db["modules"]["gold"]["note"], '따옴표 " 와 역슬래시 \\ 와 줄바꿈\n 시험 -- 주석 아님')
        chars = db["modules"]["calendar"]["chars"]
        self.assertEqual(len(chars["가나다-서버하나"]["events"]), 3)
        self.assertEqual(chars["라마-서버하나"]["synced"], 1799990000.5)
        self.assertEqual(db["modules"]["calendar"]["notes"], {})

    def test_escapes_and_mixed_tables(self):
        v = S.parse_savedvariable(b'X = { "a\\065\\x42\\\nc", [3] = -2.5e1, key = false, 7, }\n', "X")
        self.assertEqual(v[1], "aAB\nc")
        self.assertEqual(v[2], 7)
        self.assertEqual(v[3], -25.0)
        self.assertIs(v["key"], False)

    def test_find_savedvariables_from_logs(self):
        with tempfile.TemporaryDirectory() as t:
            logs, sv, _ = make_env(Path(t))
            self.assertEqual(S.find_savedvariables(logs), sv)
            self.assertIsNone(S.find_savedvariables(Path(t) / "nowhere" / "Logs"))
            self.assertEqual(S.find_savedvariables(None, str(sv)), sv)


class Events(unittest.TestCase):
    def setUp(self):
        self.cal = S.parse_savedvariable(SV.encode(), "NiniToolsDB")["modules"]["calendar"]

    def test_build(self):
        cfg = dict(S.DEFAULT_CONFIG)
        evs = S.build_events(self.cal, cfg)
        self.assertEqual([e["summary"] for e in evs], ["신화 울라텍 · 가나다", "[응답 전] 영웅, 막공; 쉼표 · 가나다", "[미정] 라마공대 · 라마"])
        first = evs[0]
        self.assertEqual(first["start"], dt.datetime(2026, 11, 4, 20, 30))
        self.assertEqual(first["end"], dt.datetime(2026, 11, 4, 23, 30))
        self.assertIn("참가자 2명 · 수락 1 · 미정 1", first["description"])
        self.assertIn("8시 20분 집결", first["description"])
        self.assertNotIn("친구-서버둘", first["description"])
        cfg.update(include_declined=True, include_participants=True)
        evs2 = S.build_events(self.cal, cfg)
        self.assertEqual(len(evs2), 4)
        self.assertIn("친구-서버둘(미정)", evs2[0]["description"])
        self.assertEqual(evs[0]["key"], evs2[0]["key"])  # 키는 설정과 무관하게 같다

    def test_ics(self):
        cfg = dict(S.DEFAULT_CONFIG)
        ics = S.build_ics(S.build_events(self.cal, cfg), cfg, now=dt.datetime(2026, 11, 2, 10, 0, tzinfo=dt.timezone.utc))
        lines = ics.split("\r\n")
        self.assertTrue(ics.endswith("\r\n"))
        self.assertTrue(all(len(ln.encode("utf-8")) <= 75 for ln in lines))
        self.assertEqual(ics.count("BEGIN:VEVENT"), 3)
        self.assertIn("DTSTART;TZID=Asia/Seoul:20261104T203000", ics)
        self.assertIn("TRIGGER:-PT30M", ics)
        unfolded = ics.replace("\r\n ", "")
        self.assertIn("SUMMARY:[응답 전] 영웅\\, 막공\\; 쉼표 · 가나다", unfolded)
        self.assertIn("STATUS:TENTATIVE", ics)


class Google(unittest.TestCase):
    def test_sync_cycle(self):
        with tempfile.TemporaryDirectory() as t:
            logs, sv, ddir = make_env(Path(t))
            g = FakeGoogle()
            cfg = S.load_config(ddir)
            cfg["enabled"] = True
            now = dt.datetime(2026, 11, 2, 12, 0)
            r1 = S.sync_google(sv, ddir, cfg, g, now=now)
            self.assertEqual((r1["added"], r1["updated"], r1["deleted"]), (3, 0, 0))
            cid = r1["calendar_id"]
            self.assertEqual(S.load_config(ddir)["calendar_id"], cid)            # 캘린더 ID 저장
            self.assertEqual(g.calendars[cid]["timeZone"], "Asia/Seoul")
            body = next(iter(g.events[cid].values()))
            self.assertEqual(body["extendedProperties"]["private"]["niniSync"], "1")
            self.assertEqual(body["reminders"]["overrides"], [{"method": "popup", "minutes": 30}])
            r2 = S.sync_google(sv, ddir, S.load_config(ddir), g, now=now)
            self.assertEqual((r2["added"], r2["updated"], r2["deleted"], r2["same"]), (0, 0, 0, 3))   # 두 번째는 그대로
            # 응답 바뀜 + 미래 일정 하나 사라짐
            txt = SV.replace('["status"] = 0,', '["status"] = 1,').replace('["title"] = "라마공대",', '["title"] = "라마공대",', 1)
            txt = txt.replace('["id"] = "201",', '["id"] = "999",')  # 라마 일정 바뀜 (예전 것은 사라짐)
            sv.write_text(txt, encoding="utf-8")
            r3 = S.sync_google(sv, ddir, S.load_config(ddir), g, now=now)
            self.assertEqual((r3["added"], r3["updated"], r3["deleted"]), (1, 1, 1))
            titles = sorted(e["summary"] for e in g.events[cid].values())
            self.assertIn("영웅, 막공; 쉼표 · 가나다", titles)
            # 지난 일정은 지우지 않는다
            r4 = S.sync_google(sv, ddir, S.load_config(ddir), g, now=dt.datetime(2026, 12, 1))
            sv.write_text(SV.replace('["events"] = {', '["events"] = {}, ["old"] = {', 1), encoding="utf-8")
            r5 = S.sync_google(sv, ddir, S.load_config(ddir), g, now=dt.datetime(2026, 12, 1))
            self.assertEqual(r5["deleted"], 0)

    def test_token_refresh_and_calendar_recreate(self):
        with tempfile.TemporaryDirectory() as t:
            _, sv, ddir = make_env(Path(t))
            g = FakeGoogle()
            cfg = S.load_config(ddir)
            cfg["calendar_id"] = "deleted@group.calendar.google.com"
            g.force_401 = 1
            r = S.sync_google(sv, ddir, cfg, g, now=dt.datetime(2026, 11, 2))
            self.assertEqual(g.tokens, 1)                      # 401 → 갱신 후 다시
            self.assertNotEqual(r["calendar_id"], "deleted@group.calendar.google.com")  # 지워진 캘린더 → 새로 만듦
            self.assertEqual(json.loads((ddir / "google_token.json").read_text())["refresh_token"], "rt")

    def test_only_app_created_scope_and_pkce(self):
        url = S.auth_url({"client_id": "c"}, "http://127.0.0.1:5555", "st", "chal")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["scope"], ["https://www.googleapis.com/auth/calendar.app.created"])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["access_type"], ["offline"])
        with self.assertRaises(S.CalendarSyncError):
            S.parse_redirect("/?code=abc&state=other", "st")
        self.assertEqual(S.parse_redirect("/?code=abc&state=st", "st"), "abc")
        v, c = S.pkce_pair()
        self.assertTrue(43 <= len(v) <= 128 and len(c) == 43)

    def test_client_file_must_be_desktop(self):
        with tempfile.TemporaryDirectory() as t:
            ddir = S.data_dir(Path(t))
            web = Path(t) / "web.json"
            web.write_text(json.dumps({"web": {"client_id": "x"}}), encoding="utf-8")
            with self.assertRaises(S.CalendarSyncError):
                S.install_client_file(ddir, web)

    def test_auto_sync_only_when_enabled_and_changed(self):
        with tempfile.TemporaryDirectory() as t:
            _, sv, ddir = make_env(Path(t))
            cfg = S.load_config(ddir)
            self.assertFalse(cfg["enabled"])                     # 기본 꺼짐
            self.assertFalse(S.needs_auto_sync(sv, ddir, cfg))
            cfg["enabled"] = True
            self.assertTrue(S.needs_auto_sync(sv, ddir, cfg))
            S.run_sync(sv, ddir, cfg, FakeGoogle())
            self.assertFalse(S.needs_auto_sync(sv, ddir, cfg))  # 같은 파일이면 다시 안 함
            os.utime(sv, (sv.stat().st_atime, sv.stat().st_mtime + 30))
            self.assertTrue(S.needs_auto_sync(sv, ddir, cfg))


class ExpiredAndClient(unittest.TestCase):
    def test_invalid_grant_marks_disconnected(self):
        class Expired(FakeGoogle):
            def __call__(self, method, url, headers, body):
                if url.startswith(S.TOKEN_URI):
                    return 400, b'{"error":"invalid_grant","error_description":"Token has been expired or revoked."}'
                return super().__call__(method, url, headers, body)
        with tempfile.TemporaryDirectory() as t:
            _, sv, ddir = make_env(Path(t))
            tok = json.loads((ddir / "google_token.json").read_text())
            tok["expires_at"] = 0
            (ddir / "google_token.json").write_text(json.dumps(tok))
            cfg = S.load_config(ddir)
            cfg["enabled"] = True
            with self.assertRaises(S.CalendarSyncError) as cm:
                S.run_sync(sv, ddir, cfg, Expired())
            self.assertEqual(str(cm.exception), S.EXPIRED_MSG)
            self.assertFalse(S.is_connected(ddir))
            self.assertTrue(S.was_expired(ddir))
            self.assertFalse(S.needs_auto_sync(sv, ddir, cfg))

    def test_bundled_client_and_own_client_first(self):
        with tempfile.TemporaryDirectory() as t:
            ddir = S.data_dir(Path(t) / "home")
            bundled = Path(t) / "wow_calendar_client.json"
            with mock.patch.object(S, "BUNDLED_CLIENT", bundled):
                self.assertIsNone(S.load_client(ddir))
                self.assertFalse(S.has_bundled_client())
                bundled.write_text(json.dumps({"installed": {"client_id": "bundled.apps.googleusercontent.com"}}), encoding="utf-8")
                self.assertTrue(S.has_bundled_client())
                self.assertEqual(S.load_client(ddir)["client_id"], "bundled.apps.googleusercontent.com")
                own = Path(t) / "own.json"
                own.write_text(json.dumps({"installed": {"client_id": "own.apps.googleusercontent.com"}}), encoding="utf-8")
                S.install_client_file(ddir, own)
                self.assertEqual(S.load_client(ddir)["client_id"], "own.apps.googleusercontent.com")
                web = Path(t) / "web.json"
                web.write_text(json.dumps({"web": {"client_id": "x"}}), encoding="utf-8")
                with self.assertRaises(S.CalendarSyncError):
                    S.install_client_file(ddir, web)

    def test_auto_sync_only_when_enabled_and_changed(self):
        with tempfile.TemporaryDirectory() as t:
            _, sv, ddir = make_env(Path(t))
            cfg = S.load_config(ddir)
            self.assertFalse(cfg["enabled"])                     # 기본 꺼짐
            self.assertFalse(S.needs_auto_sync(sv, ddir, cfg))
            cfg["enabled"] = True
            self.assertTrue(S.needs_auto_sync(sv, ddir, cfg))
            S.run_sync(sv, ddir, cfg, FakeGoogle())
            self.assertFalse(S.needs_auto_sync(sv, ddir, cfg))
            os.utime(sv, (sv.stat().st_atime, sv.stat().st_mtime + 30))
            self.assertTrue(S.needs_auto_sync(sv, ddir, cfg))


class Wiring(unittest.TestCase):
    def test_public_data_dir_and_no_secret_in_repo(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"NINI_PUBLIC_HOME": t}):
            from live import wow_calendar_ui as UI
            self.assertEqual(UI._ddir(), Path(t) / "data" / "wow_calendar")
        self.assertIn("live/wow_calendar_client.json", (ROOT / ".gitignore").read_text(encoding="utf-8"))
        for p in (ROOT / "live").glob("*.py"):
            self.assertNotIn("GOCSPX-", p.read_text(encoding="utf-8"), p.name)

    def test_sidebar_and_build(self):
        gui = (ROOT / "nini_wcl_gui.py").read_text(encoding="utf-8")
        self.assertIn('text="WoW 달력 → 구글"', gui)
        self.assertIn("_wcal_ui.install_auto(self, lambda: self._public_log_dir)", gui)
        build = (ROOT / "build_public_windows.py").read_text(encoding="utf-8")
        self.assertIn("'--hidden-import=live.wow_calendar_sync'", build)
        wf = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        self.assertIn("secrets.NINI_GOOGLE_CLIENT_JSON", wf)
        self.assertIn('"test_public_1_0_16*.py"', wf)

    def test_build_adds_client_only_when_present(self):
        import build_public_windows as B
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for src in ("resources/brand_public", "data", "live/boss_mechanics/rules", "live/raid_rules", "assets/fonts"):
                (root / src).mkdir(parents=True, exist_ok=True)
            for f in ("live/boss_mechanics/default_strategies.json", "live/raid_series_settings.json", "kick_rules_3429.json"):
                (root / f).write_text("{}", encoding="utf-8")
            args = B.build_command(root, python="py", data_sep=";")
            self.assertFalse(any("wow_calendar_client.json" in a for a in args))
            (root / "live" / "wow_calendar_client.json").write_text("{}", encoding="utf-8")
            args = B.build_command(root, python="py", data_sep=";")
            self.assertIn(f"{root / 'live' / 'wow_calendar_client.json'};live", args)

    def test_window_builds(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as e:  # 창이 없는 환경
            self.skipTest(f"no display: {e}")
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"NINI_PUBLIC_HOME": t}):
                from live import wow_calendar_ui as UI
                UI.open_window(root, lambda: None)
                w = root._wow_calendar_window
                self.assertTrue(w.win.winfo_exists())
                self.assertIn("NiniTools.lua", w.sv_var.get())
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
