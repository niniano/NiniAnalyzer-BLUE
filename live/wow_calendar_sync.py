"""1.0.16: WoW 달력 초대 → 구글 캘린더 (선택 기능, 기본 꺼짐).

NiniTools 2.5.8 「계정 달력」이 모은 캐릭별 달력 초대를 SavedVariables(NiniTools.lua)에서 읽어서
  A) 구글 캘린더 「니니 레이드 (WoW)」 전용 캘린더에 맞춰 넣거나 (기본)
  B) .ics 파일로 내보낸다 (보조 · 구글 캘린더 「가져오기」용)

- 애드온은 인터넷을 못 쓰므로, 게임을 끄거나 /reload 해서 NiniTools.lua가 저장된 뒤에 반영된다.
- 구글 권한은 calendar.app.created (이 앱이 만든 캘린더만 만들고 고칠 수 있음) 하나만 쓴다.
  다른 캘린더·일정은 읽지도 고치지도 못한다.
- 로그인 정보는 이 PC의 NiniAnalyzer 데이터 폴더 data/wow_calendar/ 안에만 둔다 (google_token.json).
  구글 연결용 앱 정보(OAuth 클라이언트)는 설치 파일에 들어 있고 (빌드할 때 넣음 · 저장소에는 없음),
  직접 만든 클라이언트 JSON을 고르면 그것을 먼저 쓴다.
- 표준 라이브러리만 쓴다 (urllib · http.server).
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

VERSION = "WOW-CALENDAR-SYNC-V1.1"
SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
API = "https://www.googleapis.com/calendar/v3"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,               # 구글 동기화 사용 (기본 꺼짐)
    "auto": True,                   # 분석기가 켜져 있을 때 NiniTools.lua가 바뀌면 자동
    "duration_min": 180,            # 와우 초대에는 끝 시간이 없다
    "reminder_min": 30,             # 0 = 알림 없음
    "include_declined": False,      # 거절·불참 일정도 올리기
    "include_participants": False,  # 참가자 이름까지 설명에 넣기
    "include_announcements": False, # 길드 공지
    "timezone": "Asia/Seoul",       # 한국 서버 시간
    "savedvariables_path": "",      # 비우면 자동으로 찾음
    "calendar_id": "",
    "calendar_name": "니니 레이드 (WoW)",
    "keep_past_days": 1,            # 이보다 지난 일정은 구글에서 지우지 않음
}

STATUS = {0: ("응답 전", "wait"), 7: ("미신청", "wait"), 1: ("수락", "yes"), 3: ("확정", "yes"), 6: ("신청", "yes"),
          5: ("대기", "maybe"), 8: ("미정", "maybe"), 2: ("거절", "no"), 4: ("불참", "no")}
TYPE_NAME = {"PLAYER": "개인 초대", "GUILD_EVENT": "길드 일정", "COMMUNITY_EVENT": "커뮤니티 일정", "GUILD_ANNOUNCEMENT": "길드 공지"}
GOOGLE_COLOR = {"yes": "10", "maybe": "5", "wait": "6", "no": "8", "info": "8"}  # 바질 · 바나나 · 귤 · 흑연


class CalendarSyncError(RuntimeError):
    def __init__(self, msg: str = "", code: str = ""):
        super().__init__(msg)
        self.code = code


# V1.1: 구글이 로그인을 끊었을 때 (테스트 상태 앱은 7일 · 비밀번호 변경 · 앱 연결 해제)
EXPIRED_TOKEN = "google_token.expired.json"
EXPIRED_MSG = "구글 연결이 끊겼습니다 (로그인 만료 또는 연결 해제). 「구글 연결」을 다시 눌러 주세요"


# ── 저장 위치 ───────────────────────────────────────────
def data_dir(workdir: Path | str) -> Path:
    d = Path(workdir) / "data" / "wow_calendar"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def load_config(ddir: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    raw = load_json(ddir / "config.json", {})
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    return cfg


def save_config(ddir: Path, cfg: dict) -> None:
    _atomic_write(ddir / "config.json", json.dumps({k: cfg.get(k, v) for k, v in DEFAULT_CONFIG.items()}, ensure_ascii=False, indent=1))


# ── NiniTools.lua 찾기 ─────────────────────────────────
def find_savedvariables(logdir: Path | str | None, override: str = "") -> Path | None:
    """…\\_retail_\\Logs → …\\_retail_\\WTF\\Account\\<계정>\\SavedVariables\\NiniTools.lua (가장 최근 것)."""
    if override:
        p = Path(override)
        return p if p.is_file() else None
    if not logdir:
        return None
    base = Path(logdir)
    roots = [base.parent, base] if base.name.lower() == "logs" else [base, base / "_retail_"]
    found: list[Path] = []
    for root in roots:
        acct = root / "WTF" / "Account"
        if acct.is_dir():
            found += [p for p in acct.glob("*/SavedVariables/NiniTools.lua") if p.is_file()]
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


# ── SavedVariables (Lua 테이블) 읽기 ────────────────────
_NUM = re.compile(rb"-?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
_IDENT = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
_ESC = {ord("n"): b"\n", ord("t"): b"\t", ord("r"): b"\r", ord("a"): b"\a", ord("b"): b"\b", ord("f"): b"\f",
        ord("v"): b"\v", ord("\\"): b"\\", ord('"'): b'"', ord("'"): b"'", ord("\n"): b"\n"}


class _LuaReader:
    def __init__(self, data: bytes):
        self.s = data
        self.i = 0

    def ws(self) -> None:
        s, n = self.s, len(self.s)
        while self.i < n:
            c = s[self.i]
            if c in b" \t\r\n":
                self.i += 1
            elif s.startswith(b"--", self.i):
                if s.startswith(b"--[[", self.i):
                    j = s.find(b"]]", self.i + 4)
                    self.i = n if j < 0 else j + 2
                else:
                    j = s.find(b"\n", self.i)
                    self.i = n if j < 0 else j + 1
            else:
                break

    def string(self) -> str:
        s = self.s
        q = s[self.i]
        self.i += 1
        out = bytearray()
        while True:
            if self.i >= len(s):
                raise CalendarSyncError("SavedVariables 문자열이 끝나지 않았습니다")
            c = s[self.i]
            if c == q:
                self.i += 1
                break
            if c == 0x5C:  # backslash
                nc = s[self.i + 1]
                if 0x30 <= nc <= 0x39:
                    m = re.match(rb"\d{1,3}", s[self.i + 1:self.i + 4])
                    out.append(int(m.group(0)) & 0xFF)
                    self.i += 1 + len(m.group(0))
                    continue
                if nc == ord("x"):
                    out.append(int(s[self.i + 2:self.i + 4], 16))
                    self.i += 4
                    continue
                if nc == ord("z"):
                    self.i += 2
                    while self.i < len(s) and s[self.i] in b" \t\r\n":
                        self.i += 1
                    continue
                if nc == 0x0D:  # \ + CRLF
                    out += b"\n"
                    self.i += 3 if s[self.i + 2:self.i + 3] == b"\n" else 2
                    continue
                out += _ESC.get(nc, bytes([nc]))
                self.i += 2
                continue
            out.append(c)
            self.i += 1
        return out.decode("utf-8", "replace")

    def value(self) -> Any:
        self.ws()
        s = self.s
        c = s[self.i:self.i + 1]
        if c == b"{":
            return self.table()
        if c in (b'"', b"'"):
            return self.string()
        if s.startswith(b"true", self.i):
            self.i += 4
            return True
        if s.startswith(b"false", self.i):
            self.i += 5
            return False
        if s.startswith(b"nil", self.i):
            self.i += 3
            return None
        m = _NUM.match(s, self.i)
        if m:
            self.i = m.end()
            t = m.group(0)
            if t.lower().startswith((b"0x", b"-0x")):
                return int(t, 16)
            if re.fullmatch(rb"-?\d+", t):
                return int(t)
            return float(t)
        raise CalendarSyncError(f"SavedVariables 읽기 실패 (위치 {self.i})")

    def table(self) -> Any:
        self.i += 1  # {
        arr: list = []
        obj: dict = {}
        while True:
            self.ws()
            if self.i >= len(self.s):
                raise CalendarSyncError("SavedVariables 테이블이 끝나지 않았습니다")
            if self.s[self.i:self.i + 1] == b"}":
                self.i += 1
                break
            if self.s[self.i:self.i + 1] == b"[":
                self.i += 1
                key = self.value()
                self.ws()
                self._expect(b"]")
                self.ws()
                self._expect(b"=")
                obj[key] = self.value()
            else:
                m = _IDENT.match(self.s, self.i)
                save = self.i
                if m and m.group(0) not in (b"true", b"false", b"nil"):
                    self.i = m.end()
                    self.ws()
                    if self.s[self.i:self.i + 1] == b"=":
                        self.i += 1
                        obj[m.group(0).decode()] = self.value()
                    else:
                        self.i = save
                        arr.append(self.value())
                else:
                    arr.append(self.value())
            self.ws()
            if self.s[self.i:self.i + 1] in (b",", b";"):
                self.i += 1
        if obj and not arr:
            return obj
        if arr and not obj:
            return arr
        for n, v in enumerate(arr, 1):
            obj.setdefault(n, v)
        return obj

    def _expect(self, ch: bytes) -> None:
        if self.s[self.i:self.i + 1] != ch:
            raise CalendarSyncError(f"SavedVariables 읽기 실패: {ch!r} 위치 {self.i}")
        self.i += 1


def parse_savedvariable(data: bytes, name: str) -> Any:
    m = re.search(rb"(?m)^" + re.escape(name.encode()) + rb"\s*=\s*", data)
    if not m:
        return None
    r = _LuaReader(data)
    r.i = m.end()
    return r.value()


def _as_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v[k] for k in sorted(k for k in v if isinstance(k, int))]
    return []


def read_calendar(sv_path: Path) -> dict:
    db = parse_savedvariable(Path(sv_path).read_bytes(), "NiniToolsDB")
    if not isinstance(db, dict):
        raise CalendarSyncError("NiniTools.lua에 NiniToolsDB가 없습니다")
    cal = ((db.get("modules") or {}).get("calendar")) if isinstance(db.get("modules"), dict) else None
    if not isinstance(cal, dict):
        raise CalendarSyncError("NiniTools 2.5.8 계정 달력 기록이 아직 없습니다 (게임 접속 후 /reload 또는 종료)")
    return cal


# ── 일정 만들기 ────────────────────────────────────────
def detail_key(e: dict) -> str:
    if e.get("id") and str(e.get("id")) != "0":
        return "id:" + str(e["id"])
    return "t:%04d%02d%02d%02d%02d:%s:%s" % (e.get("y", 0), e.get("m", 0), e.get("d", 0), e.get("h", 0), e.get("mi", 0),
                                             e.get("title") or "", e.get("by") or "")


def sync_key(char: str, e: dict) -> str:
    return hashlib.sha1((char + "|" + detail_key(e)).encode("utf-8")).hexdigest()[:26]


def status_of(e: dict) -> tuple[str, str]:
    if e.get("ctype") == "GUILD_ANNOUNCEMENT":
        return "길드 공지", "info"
    return STATUS.get(int(e.get("status") if e.get("status") is not None else -1), ("?", "wait"))


def build_events(cal: dict, cfg: dict) -> list[dict]:
    chars = cal.get("chars") if isinstance(cal.get("chars"), dict) else {}
    details = cal.get("details") if isinstance(cal.get("details"), dict) else {}
    out = []
    for char_key, ch in chars.items():
        if not isinstance(ch, dict):
            continue
        name = ch.get("name") or str(char_key).split("-")[0]
        for e in _as_list(ch.get("events")):
            if not isinstance(e, dict) or not all(isinstance(e.get(k), int) for k in ("y", "m", "d")):
                continue
            label, grp = status_of(e)
            if grp == "no" and not cfg.get("include_declined"):
                continue
            if e.get("ctype") == "GUILD_ANNOUNCEMENT" and not cfg.get("include_announcements"):
                continue
            try:
                start = _dt.datetime(e["y"], e["m"], e["d"], int(e.get("h") or 0), int(e.get("mi") or 0))
            except ValueError:
                continue
            end = start + _dt.timedelta(minutes=max(15, int(cfg.get("duration_min") or 180)))
            det = details.get(detail_key(e)) if isinstance(details.get(detail_key(e)), dict) else {}
            title = e.get("title") or "(제목 없음)"
            prefix = "" if grp == "yes" else f"[{label}] "
            summary = f"{prefix}{title} · {name}"
            lines = [f"받은 캐릭: {name}" + (f" ({ch.get('realm')})" if ch.get("realm") else ""),
                     f"상태: {label}",
                     f"종류: {TYPE_NAME.get(e.get('ctype'), e.get('ctype') or '?')}"]
            if e.get("diff"):
                lines.append(f"난이도: {e['diff']}")
            who = det.get("creator") or e.get("by")
            if who:
                lines.append(f"초대한 사람: {who}")
            invites = [i for i in _as_list(det.get("invites")) if isinstance(i, dict)]
            if invites:
                cnt = {"yes": 0, "maybe": 0, "wait": 0, "no": 0}
                for inv in invites:
                    cnt[STATUS.get(int(inv.get("status") if inv.get("status") is not None else -1), ("?", "wait"))[1]] += 1
                lines.append(f"참가자 {len(invites)}명 · 수락 {cnt['yes']} · 미정 {cnt['maybe']} · 응답 전 {cnt['wait']} · 거절 {cnt['no']}")
                if cfg.get("include_participants"):
                    names = [f"{i.get('name')}({STATUS.get(int(i.get('status') if i.get('status') is not None else -1), ('?',))[0]})" for i in invites]
                    lines.append("참가자: " + ", ".join(names))
            if det.get("desc"):
                lines.append("")
                lines.append(str(det["desc"]))
            lines.append("")
            lines.append("NiniTools 계정 달력 → NiniAnalyzer 동기화")
            ev = {
                "key": sync_key(str(char_key), e), "char": str(char_key), "dkey": detail_key(e),
                "start": start, "end": end, "summary": summary, "description": "\n".join(lines),
                "group": grp, "status_label": label,
            }
            ev["hash"] = hashlib.sha1(json.dumps([ev["summary"], ev["description"], start.isoformat(), end.isoformat(),
                                                 cfg.get("reminder_min"), cfg.get("timezone"), grp], ensure_ascii=False).encode()).hexdigest()[:16]
            out.append(ev)
    out.sort(key=lambda x: (x["start"], x["char"]))
    return out


# ── B) .ics 내보내기 ───────────────────────────────────
def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\n").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    out, cur, size = [], "", 0
    for ch in line:
        b = len(ch.encode("utf-8"))
        limit = 75 if not out else 74
        if size + b > limit:
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += b
    out.append(cur)
    return [out[0]] + [" " + x for x in out[1:]]


def build_ics(events: list[dict], cfg: dict, now: _dt.datetime | None = None) -> str:
    tz = cfg.get("timezone") or "Asia/Seoul"
    stamp = (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Nini//WoW Calendar Sync//KO", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:" + _ics_escape(cfg.get("calendar_name") or "니니 레이드 (WoW)"), "X-WR-TIMEZONE:" + tz]
    if tz == "Asia/Seoul":
        lines += ["BEGIN:VTIMEZONE", "TZID:Asia/Seoul", "BEGIN:STANDARD", "DTSTART:19700101T000000",
                  "TZOFFSETFROM:+0900", "TZOFFSETTO:+0900", "TZNAME:KST", "END:STANDARD", "END:VTIMEZONE"]
    rem = int(cfg.get("reminder_min") or 0)
    for ev in events:
        lines += ["BEGIN:VEVENT", f"UID:{ev['key']}@nini-wow-calendar", f"DTSTAMP:{stamp}",
                  f"DTSTART;TZID={tz}:{ev['start'].strftime('%Y%m%dT%H%M%S')}",
                  f"DTEND;TZID={tz}:{ev['end'].strftime('%Y%m%dT%H%M%S')}",
                  "SUMMARY:" + _ics_escape(ev["summary"]), "DESCRIPTION:" + _ics_escape(ev["description"]),
                  "STATUS:" + ("CONFIRMED" if ev["group"] == "yes" else ("CANCELLED" if ev["group"] == "no" else "TENTATIVE"))]
        if rem > 0:
            lines += ["BEGIN:VALARM", f"TRIGGER:-PT{rem}M", "ACTION:DISPLAY", "DESCRIPTION:" + _ics_escape(ev["summary"]), "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    folded = []
    for ln in lines:
        folded += _fold(ln)
    return "\r\n".join(folded) + "\r\n"


def export_ics(sv_path: Path, out_path: Path, cfg: dict) -> int:
    events = build_events(read_calendar(sv_path), cfg)
    Path(out_path).write_bytes(build_ics(events, cfg).encode("utf-8"))
    return len(events)


# ── A) 구글 캘린더 ──────────────────────────────────────
Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]


def _urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


BUNDLED_CLIENT = Path(__file__).resolve().with_name("wow_calendar_client.json")   # 빌드 때 GitHub Secret 으로 만듦 (.gitignore)


def _installed(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    inst = raw.get("installed")
    return inst if isinstance(inst, dict) and inst.get("client_id") else None


def load_client(ddir: Path) -> dict | None:
    """직접 고른 클라이언트(data/wow_calendar/google_client.json) → 없으면 설치 파일에 든 것."""
    return _installed(load_json(ddir / "google_client.json", None)) or _installed(load_json(BUNDLED_CLIENT, None))


def has_bundled_client() -> bool:
    return _installed(load_json(BUNDLED_CLIENT, None)) is not None


def install_client_file(ddir: Path, src: Path) -> dict:
    raw = load_json(Path(src), None)
    if not isinstance(raw, dict):
        raise CalendarSyncError("구글 OAuth 클라이언트 JSON을 읽지 못했습니다")
    if "installed" not in raw:
        raise CalendarSyncError("「데스크톱 앱」 유형 OAuth 클라이언트 JSON이 아닙니다 (웹 애플리케이션 유형은 쓸 수 없음)")
    inst = raw["installed"]
    if not inst.get("client_id"):
        raise CalendarSyncError("client_id가 없습니다")
    _atomic_write(ddir / "google_client.json", json.dumps(raw, ensure_ascii=False, indent=1))
    return inst


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(client: dict, redirect_uri: str, state: str, challenge: str) -> str:
    q = {"client_id": client["client_id"], "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
         "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge,
         "code_challenge_method": "S256"}
    return (client.get("auth_uri") or AUTH_URI) + "?" + urllib.parse.urlencode(q)


def parse_redirect(path: str, state: str) -> str:
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    if qs.get("error"):
        raise CalendarSyncError("구글 로그인 취소/거부: " + qs["error"][0])
    if (qs.get("state") or [""])[0] != state:
        raise CalendarSyncError("구글 로그인 응답이 맞지 않습니다 (state)")
    code = (qs.get("code") or [""])[0]
    if not code:
        raise CalendarSyncError("구글 로그인 코드가 없습니다")
    return code


def _token_request(client: dict, form: dict, transport: Transport) -> dict:
    body = urllib.parse.urlencode(form).encode()
    st, data = transport("POST", client.get("token_uri") or TOKEN_URI, {"Content-Type": "application/x-www-form-urlencoded"}, body)
    try:
        js = json.loads(data.decode("utf-8") or "{}")
    except Exception:
        js = {}
    if st != 200 or "access_token" not in js:
        raise CalendarSyncError(f"구글 토큰 오류 HTTP {st}: {js.get('error_description') or js.get('error') or ''}".strip(),
                                code=str(js.get("error") or ""))
    js["expires_at"] = time.time() + int(js.get("expires_in") or 3600) - 60
    return js


def oauth_login(ddir: Path, open_browser: Callable[[str], Any], transport: Transport = _urllib_transport, timeout: float = 240) -> dict:
    """루프백(127.0.0.1) + PKCE 로그인. 브라우저에서 허용하면 토큰을 저장한다."""
    import http.server

    client = load_client(ddir)
    if not client:
        raise CalendarSyncError("먼저 구글 OAuth 클라이언트 JSON(데스크톱 앱)을 넣어 주세요")
    state = secrets.token_urlsafe(16)
    verifier, challenge = pkce_pair()
    box: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            try:
                box["code"] = parse_redirect(self.path, state)
                msg = "구글 연결 완료! 이 창을 닫고 분석기로 돌아가세요."
            except CalendarSyncError as e:
                box["error"] = str(e)
                msg = "구글 연결 실패: " + str(e)
            page = f"<html><meta charset='utf-8'><body style='font-family:sans-serif;padding:40px'><h2>{msg}</h2></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode("utf-8"))

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    srv.timeout = 1.0
    redirect = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        open_browser(auth_url(client, redirect, state, challenge))
        end = time.time() + timeout
        while time.time() < end and "code" not in box and "error" not in box:
            srv.handle_request()
    finally:
        srv.server_close()
    if "error" in box:
        raise CalendarSyncError(box["error"])
    if "code" not in box:
        raise CalendarSyncError("구글 로그인 시간이 지났습니다 (4분). 다시 눌러 주세요")
    tok = _token_request(client, {"code": box["code"], "client_id": client["client_id"],
                                  "client_secret": client.get("client_secret", ""), "redirect_uri": redirect,
                                  "grant_type": "authorization_code", "code_verifier": verifier}, transport)
    if not tok.get("refresh_token"):
        raise CalendarSyncError("구글이 갱신 토큰을 주지 않았습니다. 구글 계정 보안 설정에서 이 앱 연결을 지운 뒤 다시 연결해 주세요")
    _atomic_write(ddir / "google_token.json", json.dumps(tok, indent=1))
    return tok


def mark_expired(ddir: Path) -> None:
    """끊긴 토큰은 지우지 않고 google_token.expired.json 으로 옮겨 둔다 (연결 안 됨으로 보이게)."""
    tok = Path(ddir) / "google_token.json"
    if tok.is_file():
        os.replace(tok, Path(ddir) / EXPIRED_TOKEN)


def was_expired(ddir: Path) -> bool:
    return (Path(ddir) / EXPIRED_TOKEN).is_file() and not (Path(ddir) / "google_token.json").is_file()


def is_connected(ddir: Path) -> bool:
    tok = load_json(ddir / "google_token.json", None)
    return bool(isinstance(tok, dict) and tok.get("refresh_token") and load_client(ddir))


class GoogleCalendar:
    def __init__(self, ddir: Path, transport: Transport = _urllib_transport):
        self.ddir = Path(ddir)
        self.transport = transport
        self.client = load_client(self.ddir)
        self.token = load_json(self.ddir / "google_token.json", None)
        if not self.client or not isinstance(self.token, dict) or not self.token.get("refresh_token"):
            raise CalendarSyncError("구글 캘린더가 연결되지 않았습니다 (「구글 연결」을 먼저 눌러 주세요)")
        self.calls = 0

    def _refresh(self) -> None:
        try:
            new = _token_request(self.client, {"client_id": self.client["client_id"], "client_secret": self.client.get("client_secret", ""),
                                               "refresh_token": self.token["refresh_token"], "grant_type": "refresh_token"}, self.transport)
        except CalendarSyncError as e:
            if e.code in ("invalid_grant", "unauthorized_client", "invalid_client"):
                mark_expired(self.ddir)
                raise CalendarSyncError(EXPIRED_MSG, code="expired") from e
            raise
        new.setdefault("refresh_token", self.token["refresh_token"])
        self.token = new
        _atomic_write(self.ddir / "google_token.json", json.dumps(new, indent=1))

    def request(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> tuple[int, dict]:
        if float(self.token.get("expires_at") or 0) < time.time():
            self._refresh()
        url = API + path + (("?" + urllib.parse.urlencode(params, doseq=True)) if params else "")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        for attempt in range(4):
            headers = {"Authorization": "Bearer " + self.token["access_token"], "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json; charset=utf-8"
            self.calls += 1
            st, raw = self.transport(method, url, headers, data)
            if st == 401 and attempt == 0:
                self._refresh()
                continue
            if st in (429, 500, 502, 503) and attempt < 3:
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                js = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                js = {}
            return st, js
        return st, {}

    def ensure_calendar(self, cfg: dict) -> str:
        cid = cfg.get("calendar_id") or ""
        if cid:
            st, _ = self.request("GET", "/calendars/" + urllib.parse.quote(cid, safe=""))
            if st == 200:
                return cid
            if st not in (404, 410, 403):
                raise CalendarSyncError(f"구글 캘린더 확인 실패 HTTP {st}")
        st, js = self.request("POST", "/calendars", {"summary": cfg.get("calendar_name") or "니니 레이드 (WoW)",
                                                     "description": "NiniTools 계정 달력 (WoW 달력 초대) · NiniAnalyzer가 관리",
                                                     "timeZone": cfg.get("timezone") or "Asia/Seoul"})
        if st not in (200, 201) or not js.get("id"):
            raise CalendarSyncError(f"구글 캘린더 만들기 실패 HTTP {st}: {(js.get('error') or {}).get('message', '')}")
        cfg["calendar_id"] = js["id"]
        return js["id"]

    def list_managed(self, cid: str) -> list[dict]:
        items, page = [], None
        while True:
            params = {"privateExtendedProperty": "niniSync=1", "maxResults": 2500, "showDeleted": "false"}
            if page:
                params["pageToken"] = page
            st, js = self.request("GET", f"/calendars/{urllib.parse.quote(cid, safe='')}/events", params=params)
            if st != 200:
                raise CalendarSyncError(f"구글 일정 목록 실패 HTTP {st}")
            items += js.get("items") or []
            page = js.get("nextPageToken")
            if not page:
                return items


def google_body(ev: dict, cfg: dict) -> dict:
    tz = cfg.get("timezone") or "Asia/Seoul"
    rem = int(cfg.get("reminder_min") or 0)
    return {
        "summary": ev["summary"],
        "description": ev["description"],
        "start": {"dateTime": ev["start"].strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz},
        "end": {"dateTime": ev["end"].strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz},
        "colorId": GOOGLE_COLOR.get(ev["group"], "8"),
        "transparency": "opaque" if ev["group"] == "yes" else "transparent",
        "reminders": {"useDefault": False, "overrides": ([{"method": "popup", "minutes": rem}] if rem > 0 else [])},
        "extendedProperties": {"private": {"niniSync": "1", "niniKey": ev["key"], "niniHash": ev["hash"], "niniChar": ev["char"]}},
    }


def sync_google(sv_path: Path, ddir: Path, cfg: dict, transport: Transport = _urllib_transport,
                now: _dt.datetime | None = None, log: Callable[[str], Any] | None = None) -> dict:
    """구글 캘린더를 NiniTools 달력에 맞춘다. 같은 일정은 고치고, 사라진 앞으로의 일정은 지운다."""
    events = build_events(read_calendar(sv_path), cfg)
    g = GoogleCalendar(ddir, transport)
    cid = g.ensure_calendar(cfg)
    save_config(ddir, cfg)
    existing = {}
    for it in g.list_managed(cid):
        key = ((it.get("extendedProperties") or {}).get("private") or {}).get("niniKey")
        if key:
            existing[key] = it
    want = {ev["key"]: ev for ev in events}
    res = {"added": 0, "updated": 0, "deleted": 0, "same": 0, "events": len(events), "calendar_id": cid}
    qcid = urllib.parse.quote(cid, safe="")
    for key, ev in want.items():
        old = existing.get(key)
        body = google_body(ev, cfg)
        if old is None:
            st, js = g.request("POST", f"/calendars/{qcid}/events", body)
            if st not in (200, 201):
                raise CalendarSyncError(f"구글 일정 추가 실패 HTTP {st}: {(js.get('error') or {}).get('message', '')}")
            res["added"] += 1
        elif ((old.get("extendedProperties") or {}).get("private") or {}).get("niniHash") != ev["hash"]:
            st, js = g.request("PUT", f"/calendars/{qcid}/events/{urllib.parse.quote(old['id'], safe='')}", body)
            if st != 200:
                raise CalendarSyncError(f"구글 일정 수정 실패 HTTP {st}")
            res["updated"] += 1
        else:
            res["same"] += 1
    keep_from = (now or _dt.datetime.now()) - _dt.timedelta(days=int(cfg.get("keep_past_days") or 1))
    for key, it in existing.items():
        if key in want:
            continue
        start = ((it.get("start") or {}).get("dateTime") or "")[:19]
        try:
            st_dt = _dt.datetime.fromisoformat(start)
        except ValueError:
            st_dt = None
        if st_dt is not None and st_dt < keep_from:
            continue  # 지난 일정은 기록으로 남긴다
        st, _ = g.request("DELETE", f"/calendars/{qcid}/events/{urllib.parse.quote(it['id'], safe='')}")
        if st not in (200, 204, 404, 410):
            raise CalendarSyncError(f"구글 일정 지우기 실패 HTTP {st}")
        res["deleted"] += 1
    res["calls"] = g.calls
    return res


# ── 상태 기록 · 자동 동기화 판단 ───────────────────────
def load_state(ddir: Path) -> dict:
    st = load_json(ddir / "state.json", {})
    return st if isinstance(st, dict) else {}


def save_state(ddir: Path, state: dict) -> None:
    _atomic_write(ddir / "state.json", json.dumps(state, ensure_ascii=False, indent=1))


def needs_auto_sync(sv_path: Path | None, ddir: Path, cfg: dict) -> bool:
    if not (cfg.get("enabled") and cfg.get("auto") and sv_path and Path(sv_path).is_file() and is_connected(ddir)):
        return False
    st = load_state(ddir)
    return abs(Path(sv_path).stat().st_mtime - float(st.get("synced_mtime") or 0)) > 0.5


_LOCK = threading.Lock()


def run_sync(sv_path: Path, ddir: Path, cfg: dict, transport: Transport = _urllib_transport) -> dict:
    """한 번에 하나만. 결과를 state.json에 남긴다."""
    if not _LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        mtime = Path(sv_path).stat().st_mtime
        state = load_state(ddir)
        try:
            res = sync_google(sv_path, ddir, cfg, transport)
            state.update({"synced_mtime": mtime, "last_sync": time.time(), "last_result": res, "last_error": ""})
        except Exception as e:  # noqa: BLE001 - 화면에 그대로 보여 준다
            state.update({"last_error": str(e), "last_try": time.time()})
            save_state(ddir, state)
            raise
        save_state(ddir, state)
        return res
    finally:
        _LOCK.release()


def summarize(sv_path: Path | None, cfg: dict) -> dict:
    """화면 표시용: 캐릭별 일정 수 · 마지막 저장 시각."""
    if not sv_path or not Path(sv_path).is_file():
        return {"ok": False, "error": "NiniTools.lua를 찾지 못했습니다"}
    try:
        cal = read_calendar(Path(sv_path))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    events = build_events(cal, cfg)
    per: dict[str, int] = {}
    for ev in events:
        per[ev["char"]] = per.get(ev["char"], 0) + 1
    chars = []
    for key, ch in (cal.get("chars") or {}).items():
        if isinstance(ch, dict):
            chars.append({"key": key, "name": ch.get("name") or key, "synced": ch.get("synced"), "count": per.get(key, 0)})
    chars.sort(key=lambda c: -(c.get("synced") or 0))
    return {"ok": True, "events": len(events), "chars": chars, "mtime": Path(sv_path).stat().st_mtime}
