"""1.0.16: 「WoW 달력 → 구글」 창 · 자동 동기화 (설정에서 켜는 선택 기능).

- A (기본): 구글 캘린더 「니니 레이드 (WoW)」에 맞춤. 켜 두면 분석기가 켜져 있는 동안
  NiniTools.lua가 바뀔 때(게임 종료 · /reload) 자동으로 맞춘다.
- B (보조): .ics 파일로 내보내기 (구글 캘린더 → 설정 → 가져오기).
"""
from __future__ import annotations

import queue
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from live import wow_calendar_sync as S
from live import ui_theme as T
from live.ui_theme import RoundButton

from live.public_paths import public_home
FONT = "Segoe UI"
CHECK_EVERY_MS = 60_000


def _ddir() -> Path:
    return S.data_dir(public_home())   # %LOCALAPPDATA%\NiniAnalyzer\Public\data\wow_calendar


def _ago(ts) -> str:
    if not ts:
        return "아직 없음"
    s = max(0, time.time() - float(ts))
    if s < 90:
        return "방금"
    if s < 3600:
        return f"{int(s // 60)}분 전"
    if s < 86400:
        return f"{int(s // 3600)}시간 전"
    return f"{int(s // 86400)}일 전"


class _Worker:
    """백그라운드 작업 하나 + 결과를 tk 메인 스레드로 넘김."""

    def __init__(self, root):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self._poll()

    def _poll(self):
        try:
            while True:
                cb, ok, val = self.q.get_nowait()
                self.busy = False
                try:
                    cb(ok, val)
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            self.root.after(300, self._poll)
        except Exception:
            pass

    def run(self, fn, cb):
        if self.busy:
            return False
        self.busy = True

        def work():
            try:
                self.q.put((cb, True, fn()))
            except Exception as e:  # noqa: BLE001
                self.q.put((cb, False, e))

        threading.Thread(target=work, daemon=True, name="wow-calendar-sync").start()
        return True


class CalendarWindow:
    def __init__(self, app, logdir_getter):
        self.app = app
        self.logdir_getter = logdir_getter
        self.ddir = _ddir()
        self.cfg = S.load_config(self.ddir)
        self.worker = getattr(app, "_wow_calendar_worker", None) or _Worker(app)
        app._wow_calendar_worker = self.worker
        w = tk.Toplevel(app)
        w.title("WoW 달력 → 구글 캘린더")
        w.configure(bg=T.PAGE_BG)
        w.geometry("640x700")
        w.minsize(560, 600)
        self.win = w
        pad = {"padx": 16}

        def label(parent, text, size=10, fg=T.TEXT, bold=False):
            return tk.Label(parent, text=text, bg=T.PAGE_BG, fg=fg, font=(FONT, size, "bold" if bold else "normal"),
                            justify="left", anchor="w", wraplength=600)

        label(w, "WoW 달력 초대 → 구글 캘린더", 14, bold=True).pack(fill="x", pady=(14, 2), **pad)
        label(w, "NiniTools 「계정 달력」이 모은 초대를 내 구글 캘린더 「니니 레이드 (WoW)」에 맞춥니다. "
                 "게임을 끄거나 /reload 해서 NiniTools.lua가 저장된 뒤에 반영됩니다. "
                 "로그인 정보는 이 PC에만 저장되고, 다른 사람에게 보내지 않습니다.", 9, T.MUTED).pack(fill="x", **pad)

        # 1) NiniTools.lua
        box1 = tk.Frame(w, bg=T.PAGE_BG)
        box1.pack(fill="x", pady=(12, 0), **pad)
        label(box1, "1. NiniTools 달력 기록", 11, T.ORANGE, True).pack(fill="x")
        self.sv_var = tk.StringVar()
        self.sum_var = tk.StringVar()
        tk.Label(box1, textvariable=self.sv_var, bg=T.PAGE_BG, fg=T.MUTED, font=(FONT, 9), anchor="w", justify="left", wraplength=600).pack(fill="x")
        tk.Label(box1, textvariable=self.sum_var, bg=T.PAGE_BG, fg=T.TEXT, font=(FONT, 10), anchor="w", justify="left", wraplength=600).pack(fill="x", pady=(2, 0))
        row = tk.Frame(box1, bg=T.PAGE_BG)
        row.pack(fill="x", pady=(6, 0))
        RoundButton(row, text="파일 직접 고르기", command=self.pick_sv, font=(FONT, 9), outer=T.PAGE_BG, pady=4).pack(side="left")
        RoundButton(row, text="자동으로 찾기", command=self.auto_sv, font=(FONT, 9), outer=T.PAGE_BG, pady=4).pack(side="left", padx=(6, 0))

        # 2) 구글 연결
        box2 = tk.Frame(w, bg=T.PAGE_BG)
        box2.pack(fill="x", pady=(14, 0), **pad)
        label(box2, "2. 구글 캘린더 (기본)", 11, T.ORANGE, True).pack(fill="x")
        self.g_var = tk.StringVar()
        tk.Label(box2, textvariable=self.g_var, bg=T.PAGE_BG, fg=T.TEXT, font=(FONT, 10), anchor="w", justify="left", wraplength=600).pack(fill="x")
        row2 = tk.Frame(box2, bg=T.PAGE_BG)
        row2.pack(fill="x", pady=(6, 0))
        self.connect_btn = RoundButton(row2, text="구글 연결", command=self.connect, font=(FONT, 10), outer=T.PAGE_BG, pady=5)
        self.connect_btn.pack(side="left")
        self.sync_btn = RoundButton(row2, text="지금 동기화", kind="accent", command=self.sync_now, font=(FONT, 10), outer=T.PAGE_BG, pady=5)
        self.sync_btn.pack(side="left", padx=(6, 0))
        RoundButton(row2, text="구글 캘린더 열기", command=lambda: webbrowser.open("https://calendar.google.com/"), font=(FONT, 9), outer=T.PAGE_BG, pady=5).pack(side="left", padx=(6, 0))

        # 3) 설정
        box3 = tk.Frame(w, bg=T.PAGE_BG)
        box3.pack(fill="x", pady=(14, 0), **pad)
        label(box3, "3. 설정", 11, T.ORANGE, True).pack(fill="x")
        self.v_enabled = tk.BooleanVar(value=bool(self.cfg.get("enabled")))
        self.v_auto = tk.BooleanVar(value=bool(self.cfg.get("auto")))
        self.v_declined = tk.BooleanVar(value=bool(self.cfg.get("include_declined")))
        self.v_parts = tk.BooleanVar(value=bool(self.cfg.get("include_participants")))
        self.v_ann = tk.BooleanVar(value=bool(self.cfg.get("include_announcements")))
        for text, var in (("구글 동기화 사용", self.v_enabled),
                          ("자동 동기화 (분석기가 켜져 있을 때 NiniTools.lua가 바뀌면)", self.v_auto),
                          ("거절 · 불참한 일정도 올리기", self.v_declined),
                          ("참가자 이름까지 설명에 넣기", self.v_parts),
                          ("길드 공지도 올리기", self.v_ann)):
            tk.Checkbutton(box3, text=text, variable=var, command=self.save_settings, bg=T.PAGE_BG, fg=T.TEXT,
                           selectcolor=T.CARD_BG, activebackground=T.PAGE_BG, activeforeground=T.TEXT,
                           font=(FONT, 10), anchor="w", highlightthickness=0, bd=0).pack(fill="x")
        row3 = tk.Frame(box3, bg=T.PAGE_BG)
        row3.pack(fill="x", pady=(4, 0))
        self.v_dur = tk.IntVar(value=int(self.cfg.get("duration_min") or 180))
        self.v_rem = tk.IntVar(value=int(self.cfg.get("reminder_min") or 0))
        for text, var, lo, hi, step in (("일정 길이(분)", self.v_dur, 30, 600, 30), ("알림(분 전, 0=없음)", self.v_rem, 0, 240, 5)):
            tk.Label(row3, text=text, bg=T.PAGE_BG, fg=T.TEXT, font=(FONT, 10)).pack(side="left")
            sp = tk.Spinbox(row3, from_=lo, to=hi, increment=step, textvariable=var, width=5, command=self.save_settings,
                            bg=T.CARD_BG, fg=T.TEXT, buttonbackground=T.CARD_BG, insertbackground=T.TEXT, relief="flat")
            sp.bind("<FocusOut>", lambda _e: self.save_settings())
            sp.pack(side="left", padx=(4, 14))

        # 4) B: .ics
        box4 = tk.Frame(w, bg=T.PAGE_BG)
        box4.pack(fill="x", pady=(14, 0), **pad)
        label(box4, "보조: .ics 파일로 내보내기", 11, T.ORANGE, True).pack(fill="x")
        label(box4, "구글 연결 없이 쓰는 방법입니다. 구글 캘린더 → 설정 → 가져오기/내보내기 → 가져오기. "
                    "자동으로 바뀌지 않으니 응답이 바뀌면 다시 내보내 가져오세요.", 9, T.MUTED).pack(fill="x")
        RoundButton(box4, text=".ics 파일로 내보내기", command=self.export_ics, font=(FONT, 10), outer=T.PAGE_BG, pady=5).pack(anchor="w", pady=(6, 0))

        self.result_var = tk.StringVar()
        tk.Label(w, textvariable=self.result_var, bg=T.PAGE_BG, fg=T.GREEN, font=(FONT, 10), anchor="w", justify="left", wraplength=600).pack(fill="x", pady=(14, 8), **pad)
        self.refresh()

    # ── 상태 ──────────────────────────────────────────
    def sv_path(self) -> Path | None:
        try:
            return S.find_savedvariables(self.logdir_getter(), self.cfg.get("savedvariables_path") or "")
        except Exception:
            return None

    def refresh(self):
        sv = self.sv_path()
        if sv:
            self.sv_var.set(f"{sv}  ·  저장 {_ago(sv.stat().st_mtime)}")
            info = S.summarize(sv, self.cfg)
            if info.get("ok"):
                parts = [f"{c['name']} {c['count']}개 (게임에서 읽음 {_ago(c.get('synced'))})" for c in info["chars"]]
                self.sum_var.set(f"올릴 일정 {info['events']}개  ·  " + ("  /  ".join(parts) if parts else "캐릭 없음"))
            else:
                self.sum_var.set(info.get("error") or "")
        else:
            self.sv_var.set("NiniTools.lua를 찾지 못했습니다 (WoW 폴더 _retail_\\WTF\\Account\\<계정>\\SavedVariables\\NiniTools.lua)")
            self.sum_var.set("")
        st = S.load_state(self.ddir)
        if S.is_connected(self.ddir):
            res = st.get("last_result") or {}
            txt = f"연결됨 · 캘린더 「{self.cfg.get('calendar_name')}」 · 마지막 동기화 {_ago(st.get('last_sync'))}"
            if res:
                txt += f" (추가 {res.get('added', 0)} · 수정 {res.get('updated', 0)} · 삭제 {res.get('deleted', 0)} · 그대로 {res.get('same', 0)})"
            if not self.cfg.get("enabled"):
                txt += "\n「구글 동기화 사용」이 꺼져 있어 자동으로 올리지 않습니다."
            if st.get("last_error"):
                txt += "\n마지막 오류: " + str(st["last_error"])
            self.connect_btn.configure(text="다시 연결")
        elif S.load_client(self.ddir) and S.was_expired(self.ddir):
            txt = (f"⚠ 구글 연결이 끊겼습니다 · 마지막 동기화 {_ago(st.get('last_sync'))}\n"
                   "「구글 연결」을 다시 눌러 허용하면 이어서 올립니다. (그동안 바뀐 일정도 한 번에 맞춰집니다)")
            self.connect_btn.configure(text="구글 다시 연결")
        elif S.load_client(self.ddir):
            txt = ("연결 안 됨 · 「구글 연결」을 누르면 브라우저가 열립니다. 내 구글 계정을 고르고 「허용」을 눌러 주세요.\n"
                   "권한은 「이 앱이 만든 캘린더만」(calendar.app.created) 하나만 씁니다 · 내 다른 일정은 읽지도 고치지도 못합니다.")
            self.connect_btn.configure(text="구글 연결")
        else:
            txt = ("연결 안 됨 · 이 설치 파일에는 구글 연결 정보가 없습니다. 아래 .ics 내보내기를 쓰거나,\n"
                   "Google Cloud 「데스크톱 앱」 OAuth 클라이언트 JSON이 있으면 「구글 연결」에서 고르세요.")
            self.connect_btn.configure(text="구글 연결")
        self.g_var.set(txt)

    def _update_cfg(self, **changes):
        # 디스크의 최신 설정(calendar_id 등)을 바탕으로 바뀐 값만 덮는다 → 자동 동기화가 만든 캘린더 ID를 잃지 않음
        cfg = S.load_config(self.ddir)
        cfg.update(changes)
        S.save_config(self.ddir, cfg)
        self.cfg = cfg

    def save_settings(self):
        try:
            changes = ({"enabled": bool(self.v_enabled.get()), "auto": bool(self.v_auto.get()),
                             "include_declined": bool(self.v_declined.get()), "include_participants": bool(self.v_parts.get()),
                             "include_announcements": bool(self.v_ann.get()),
                             "duration_min": max(30, min(600, int(self.v_dur.get()))),
                        "reminder_min": max(0, min(240, int(self.v_rem.get())))})
        except (tk.TclError, ValueError):
            return
        self._update_cfg(**changes)
        self.refresh()

    # ── 동작 ──────────────────────────────────────────
    def pick_sv(self):
        p = filedialog.askopenfilename(parent=self.win, title="NiniTools.lua 선택 (WTF\\Account\\<계정>\\SavedVariables)",
                                       filetypes=[("NiniTools SavedVariables", "NiniTools.lua"), ("Lua", "*.lua")])
        if p:
            self._update_cfg(savedvariables_path=p)
            self.refresh()

    def auto_sv(self):
        self._update_cfg(savedvariables_path="")
        self.refresh()

    def connect(self):
        # 1.0.16: 설치 파일에 연결 정보가 들어 있으면 바로 로그인 (JSON 고르기 없음)
        if not S.load_client(self.ddir):
            p = filedialog.askopenfilename(parent=self.win, title="Google Cloud OAuth 클라이언트 JSON (데스크톱 앱)", filetypes=[("JSON", "*.json")])
            if not p:
                return
            try:
                S.install_client_file(self.ddir, Path(p))
            except S.CalendarSyncError as e:
                messagebox.showerror("구글 연결", str(e), parent=self.win)
                return
        self.result_var.set("브라우저에서 구글 계정을 고르고 「허용」을 눌러 주세요… (4분 안에)")
        started = self.worker.run(lambda: S.oauth_login(self.ddir, webbrowser.open), self._after_connect)
        if not started:
            self.result_var.set("다른 작업이 끝난 뒤 다시 눌러 주세요.")

    def _after_connect(self, ok, val):
        if ok:
            self.v_enabled.set(True)
            self.save_settings()
            self.result_var.set("구글 연결 완료 · 동기화를 시작합니다.")
            self.sync_now()
        else:
            self.result_var.set("구글 연결 실패: " + str(val))
        self.refresh()

    def sync_now(self):
        sv = self.sv_path()
        if not sv:
            self.result_var.set("NiniTools.lua를 먼저 찾아 주세요.")
            return
        if not S.is_connected(self.ddir):
            self.result_var.set("구글 연결을 먼저 해 주세요. (또는 아래 .ics 내보내기)")
            return
        self.result_var.set("동기화 중…")
        cfg = S.load_config(self.ddir)
        if not self.worker.run(lambda: (S.run_sync(sv, self.ddir, cfg), cfg), self._after_sync):
            self.result_var.set("이미 동기화 중입니다.")

    def _after_sync(self, ok, val):
        if ok:
            res, cfg = val
            if res.get("busy"):
                self.result_var.set("이미 동기화 중입니다.")
            else:
                self.cfg = S.load_config(self.ddir)
                self.result_var.set(f"동기화 완료 · 일정 {res['events']}개 · 추가 {res['added']} · 수정 {res['updated']} · 삭제 {res['deleted']} · 그대로 {res['same']}")
        else:
            self.result_var.set("동기화 실패: " + str(val))
        self.refresh()

    def export_ics(self):
        sv = self.sv_path()
        if not sv:
            self.result_var.set("NiniTools.lua를 먼저 찾아 주세요.")
            return
        p = filedialog.asksaveasfilename(parent=self.win, title=".ics 저장", defaultextension=".ics",
                                         initialfile=f"nini_wow_calendar_{time.strftime('%Y%m%d')}.ics", filetypes=[("iCalendar", "*.ics")])
        if not p:
            return
        try:
            n = S.export_ics(sv, Path(p), self.cfg)
        except Exception as e:  # noqa: BLE001
            self.result_var.set("내보내기 실패: " + str(e))
            return
        self.result_var.set(f".ics 저장 완료 · 일정 {n}개 · 구글 캘린더 → 설정 → 가져오기에서 이 파일을 고르세요.")


def open_window(app, logdir) -> None:
    win = getattr(app, "_wow_calendar_window", None)
    try:
        if win is not None and win.win.winfo_exists():
            win.win.deiconify()
            win.win.lift()
            win.refresh()
            return
    except Exception:
        pass
    getter = logdir if callable(logdir) else (lambda: logdir)
    app._wow_calendar_window = CalendarWindow(app, getter)


def install_auto(app, logdir_getter) -> None:
    """분석기가 켜져 있는 동안 1분마다 NiniTools.lua가 바뀌었는지 보고, 바뀌었으면 조용히 맞춘다."""
    ddir = _ddir()
    worker = getattr(app, "_wow_calendar_worker", None) or _Worker(app)
    app._wow_calendar_worker = worker

    def done(ok, val):
        w = getattr(app, "_wow_calendar_window", None)
        try:
            if w is not None and w.win.winfo_exists():
                w.refresh()
        except Exception:
            pass

    def tick():
        try:
            cfg = S.load_config(ddir)
            sv = S.find_savedvariables(logdir_getter(), cfg.get("savedvariables_path") or "")
            if not worker.busy and S.needs_auto_sync(sv, ddir, cfg):
                worker.run(lambda: S.run_sync(sv, ddir, cfg), done)
        except Exception:
            pass
        try:
            app.after(CHECK_EVERY_MS, tick)
        except Exception:
            pass

    try:
        app.after(15_000, tick)
    except Exception:
        pass
