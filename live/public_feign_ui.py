"""RC4.2 PUBLIC: settings window + opt-in Feign Death recheck window.

Flow (user decision 2026-10-12):
  1. 후보 검색 - read-only: only Tries whose raw log contains Feign Death (5384).
  2. List shows each Try and whether it can be rechecked (raw present/complete).
  3. Only Tries the user ticks are rechecked: verify raw -> zip backup -> reanalyse.
  4. Before/after of death count, first death, wipe cause, mechanic, first attacker.
  5. Missing/damaged raw or a failed backup -> that Try is not reanalysed.
Log facts (deaths excluded on Feign Death evidence) and engine re-judgement
(cause, first attacker) are reported in separate sections.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import ui_theme as _ui_theme

CHECK_ON, CHECK_OFF, CHECK_NA = "☑", "☐", "—"


def busy_reason(app) -> str | None:
    checks = (
        ("live_running", "실시간 감시"),
        ("_live_start_pending", "실시간 감시 시작"),
        ("_public_import_running", "로그 가져오기"),
        ("_public_migration_running", "기존 세션 복사"),
        ("_saved_reanalysis_running", "저장 복기 최신화"),
        ("_identity_audit_running", "기존 기록 확인"),
        ("_feign_recheck_running", "죽척 재검사"),
    )
    for attr, label in checks:
        if getattr(app, attr, False):
            return label
    return None


def _body_font(widget):
    """The app's resolved body font (bundled font when loaded)."""
    try:
        f = ttk.Style(widget).lookup("TLabel", "font")
        if f:
            return f
    except tk.TclError:
        pass
    return (_ui_theme.BODY_FAMILY, 10)


def open_settings(app):
    """Replaces the old yes/no settings dialog."""
    existing = getattr(app, "_public_settings_win", None)
    if existing is not None and existing.winfo_exists():
        existing.lift(); existing.focus_force()
        return existing
    from .public_paths import public_sessions_root
    win = tk.Toplevel(app)
    app._public_settings_win = win
    win.title("NiniAnalyzer 공개판 설정")
    win.transient(app)
    win.resizable(False, False)
    body = ttk.Frame(win, padding=18)
    body.pack(fill="both", expand=True)

    ttk.Label(body, text="설정", style="Title.TLabel").pack(anchor="w")
    ttk.Label(body, text=f"공대 세션 저장: {public_sessions_root(app._public_home)}",
              style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
    log_var = tk.StringVar(value=f"WoW 로그 폴더: {app._public_log_dir or '미지정'}")
    row = ttk.Frame(body); row.pack(fill="x", pady=(4, 0))
    ttk.Label(row, textvariable=log_var, style="Muted.TLabel").pack(side="left")

    def change_folder():
        reason = busy_reason(app)
        if reason:
            messagebox.showwarning("작업 진행 중", f"{reason}이(가) 끝난 뒤 폴더를 변경해 주세요.", parent=win)
            return
        chosen = app._public_select_log_directory()
        if chosen:
            log_var.set(f"WoW 로그 폴더: {chosen}")

    ttk.Button(row, text="로그 폴더 변경", command=change_folder).pack(side="right", padx=(16, 0))

    ttk.Separator(body).pack(fill="x", pady=14)
    ttk.Label(body, text="기존 기록 죽척 재검사", style="Header.TLabel").pack(anchor="w")
    ttk.Label(
        body,
        text=("이미 가져온 트라이 중 냥꾼 '죽은 척하기'가 들어 있는 트라이만 찾아,\n"
              "선택한 트라이만 백업 후 다시 분석합니다. 검색은 기존 결과를 바꾸지 않습니다."),
        style="Muted.TLabel", justify="left",
    ).pack(anchor="w", pady=(4, 8))
    ttk.Button(body, text="죽척 재검사 열기", command=lambda: open_feign_recheck(app)).pack(anchor="w")

    ttk.Separator(body).pack(fill="x", pady=14)
    ttk.Button(body, text="닫기", command=win.destroy).pack(anchor="e")
    win.update_idletasks()
    try:
        x = app.winfo_rootx() + 80; y = app.winfo_rooty() + 80
        win.geometry(f"+{x}+{y}")
    except Exception:
        pass
    return win


def open_feign_recheck(app):
    existing = getattr(app, "_feign_recheck_win", None)
    if existing is not None and existing.winfo_exists():
        existing.lift(); existing.focus_force()
        return existing
    return FeignRecheckWindow(app).win


class FeignRecheckWindow:
    COLS = (("sel", "선택", 44), ("date", "날짜", 92), ("boss", "보스", 150), ("try", "Try", 50),
            ("result", "결과", 60), ("fd", "죽척", 50), ("cur", "현재 사망", 74),
            ("pred", "재검사 후(예상)", 104), ("note", "상태", 300))

    def __init__(self, app):
        self.app = app
        self.q: queue.Queue = queue.Queue()
        self.items: dict[str, dict] = {}      # tree iid -> candidate
        self.checked: set[str] = set()
        self.scanning = False
        win = self.win = tk.Toplevel(app)
        app._feign_recheck_win = win
        app._feign_recheck_ctrl = self
        win.title("기존 기록 죽척 재검사")
        win.transient(app)
        win.geometry("1060x680")
        win.minsize(860, 520)
        win.protocol("WM_DELETE_WINDOW", self.close)

        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="기존 기록 죽척 재검사", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            body,
            text=("① 후보 검색: 저장된 원본 로그에서 '죽은 척하기(5384)'가 있는 트라이만 찾습니다. 읽기 전용입니다.\n"
                  "② 체크한 트라이만 재검사: 원본 로그를 확인하고, 결과 파일을 zip으로 백업한 뒤 다시 분석합니다.\n"
                  "   원본 로그가 없거나 손상됐거나 백업에 실패하면 그 트라이는 재분석하지 않습니다."),
            style="Muted.TLabel", justify="left",
        ).pack(anchor="w", pady=(6, 10))

        bar = ttk.Frame(body); bar.pack(fill="x")
        self.scan_btn = ttk.Button(bar, text="후보 검색", command=self.start_scan)
        self.scan_btn.pack(side="left")
        self.run_btn = ttk.Button(bar, text="선택한 Try 재검사", style="Accent.TButton",
                                  command=self.start_recheck, state="disabled")
        self.run_btn.pack(side="left", padx=(8, 0))
        self.status_var = tk.StringVar(value="후보 검색을 눌러 주세요.")
        ttk.Label(bar, textvariable=self.status_var, style="Muted.TLabel").pack(side="left", padx=(14, 0))

        table = ttk.Frame(body); table.pack(fill="both", expand=True, pady=(10, 0))
        self.tree = ttk.Treeview(table, columns=[c[0] for c in self.COLS], show="headings", height=9,
                                 selectmode="browse")
        for key, label, width in self.COLS:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w" if key in ("boss", "note") else "center",
                             stretch=key == "note")
        self.tree.tag_configure("na", foreground="#7c8590")
        ysb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_click, add="+")
        self.tree.bind("<space>", self._on_space, add="+")
        ttk.Label(body, text="행을 클릭하면 선택/해제됩니다. 사망 수 변화가 예상되는 트라이는 미리 체크돼 있습니다.",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 8))

        out = ttk.Frame(body); out.pack(fill="both", expand=True)
        self.text = tk.Text(out, height=12, wrap="word", bg="#11161c", fg="#dfe5eb",
                            insertbackground="#dfe5eb", relief="flat", padx=10, pady=8,
                            font=_body_font(win))
        tsb = ttk.Scrollbar(out, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=tsb.set, state="disabled")
        self.text.tag_configure("fact", foreground="#9fd8a8")
        self.text.tag_configure("judge", foreground="#f5c27a")
        self.text.tag_configure("fail", foreground="#ff8f8f")
        self.text.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self._write("재검사 결과가 여기에 표시됩니다.\n"
                    "■ 로그 사실: 죽척 근거로 사망에서 뺀 기록 / 근거가 없어 유지한 기록\n"
                    "■ 엔진 재판정: 사망 수·첫 사망자·전멸 원인·첫 공격자의 변경 전→후 (확정 아님)\n")

    # ---- helpers -------------------------------------------------------
    def _alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def _write(self, text: str, clear: bool = True):
        if not self._alive():
            return
        self.text.configure(state="normal")
        if clear:
            self.text.delete("1.0", "end")
        for line in text.splitlines(True):
            tag = ()
            s = line.strip()
            if s.startswith("■ 로그 사실"):
                tag = ("fact",)
            elif s.startswith("■ 엔진 재판정"):
                tag = ("judge",)
            elif s.startswith("✕"):
                tag = ("fail",)
            self.text.insert("end", line, tag)
        self.text.configure(state="disabled")

    def _rows(self) -> list[dict]:
        rows = list(getattr(self.app, "live_persisted_rows", []) or [])
        seen, out = set(), []
        for r in rows:
            d = str((r or {}).get("storageDir") or "")
            if d and d not in seen:
                seen.add(d); out.append(r)
        return out

    def _update_run_btn(self):
        if not self._alive():
            return
        busy = self.scanning or getattr(self.app, "_feign_recheck_running", False)
        self.run_btn.configure(state="normal" if (self.checked and not busy) else "disabled")
        self.scan_btn.configure(state="disabled" if busy else "normal")

    def _toggle(self, iid):
        item = self.items.get(iid)
        if not item or not item.get("selectable"):
            return
        if iid in self.checked:
            self.checked.discard(iid)
        else:
            self.checked.add(iid)
        self.tree.set(iid, "sel", CHECK_ON if iid in self.checked else CHECK_OFF)
        self._update_run_btn()

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell":
            iid = self.tree.identify_row(event.y)
            if iid:
                self._toggle(iid)

    def _on_space(self, _event=None):
        for iid in self.tree.selection():
            self._toggle(iid)

    def _poll(self):
        handled_done = False
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "progress" and self._alive():
                    self.status_var.set(data)
                elif kind == "scan_done":
                    self._scan_done(data); handled_done = True
                elif kind == "recheck_done":
                    self._recheck_done(data); handled_done = True
        except queue.Empty:
            pass
        if not handled_done:
            self.app.after(150, self._poll)

    # ---- scan (read-only) ----------------------------------------------
    def start_scan(self):
        rows = self._rows()
        if not rows:
            self.status_var.set("가져온 트라이가 없습니다.")
            return
        self.scanning = True
        self._update_run_btn()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.items.clear(); self.checked.clear()
        self.status_var.set(f"검색 중 · 0/{len(rows)} (읽기 전용)")

        def worker():
            from .feign_recheck import scan_feign_candidates
            try:
                res = scan_feign_candidates(
                    rows, progress=lambda i, n, label: self.q.put(("progress", f"검색 중 · {i}/{n} · {label}")))
                self.q.put(("scan_done", {"items": res, "total": len(rows)}))
            except Exception as exc:
                self.q.put(("scan_done", {"error": str(exc)}))

        threading.Thread(target=worker, name="nini-feign-scan", daemon=True).start()
        self.app.after(150, self._poll)

    def _scan_done(self, data):
        self.scanning = False
        if not self._alive():
            return
        if data.get("error"):
            self.status_var.set(f"검색 오류 · {data['error']}")
            self._update_run_btn()
            return
        items = data.get("items") or []
        ready = 0
        for it in items:
            sel = bool(it.get("selectable"))
            pred = it.get("predictedDeathCount")
            cur = it.get("currentDeathCount")
            iid = self.tree.insert("", "end", values=(
                CHECK_OFF if sel else CHECK_NA, it.get("date", ""), it.get("boss", ""), it.get("tryNo", ""),
                it.get("result", ""), it.get("feignCasts", "-"),
                "-" if cur is None else cur, "-" if pred is None else pred, it.get("note", ""),
            ), tags=() if sel else ("na",))
            self.items[iid] = it
            if sel:
                ready += 1
                if cur is not None and pred is not None and int(cur) != int(pred):
                    self.checked.add(iid)
                    self.tree.set(iid, "sel", CHECK_ON)
        blocked = len(items) - ready
        if not items:
            self.status_var.set(f"죽척이 들어 있는 트라이 없음 · {data.get('total', 0)}개 확인")
        else:
            self.status_var.set(f"죽척 포함 {ready}개 재검사 가능" + (f" · 재검사 불가 {blocked}개" if blocked else "")
                                + " · 기존 결과는 변경되지 않았습니다")
        self._update_run_btn()

    # ---- recheck (selected only) ---------------------------------------
    def start_recheck(self):
        reason = busy_reason(self.app)
        if reason:
            messagebox.showwarning("작업 진행 중", f"{reason}이(가) 끝난 뒤 재검사해 주세요.", parent=self.win)
            return
        chosen = [self.items[i] for i in self.tree.get_children() if i in self.checked]
        if not chosen:
            return
        if not messagebox.askyesno(
            "선택한 Try 재검사",
            f"선택한 {len(chosen)}개 트라이를 재검사합니다.\n\n"
            "· 트라이마다 결과 파일을 zip으로 먼저 백업합니다.\n"
            "· 같은 보스의 반복 문제 집계도 백업 후 갱신됩니다.\n"
            "· 원본 로그(raw.log.gz)는 변경하지 않습니다.\n\n계속할까요?",
            parent=self.win,
        ):
            return
        by_dir = {str(r.get("storageDir")): r for r in self._rows()}
        rows = [by_dir.get(str(c["storageDir"])) or {"storageDir": c["storageDir"], "name": c.get("boss"),
                                                      "tryNo": c.get("tryNo")} for c in chosen]
        monitor = getattr(self.app, "live_monitor", None)
        storage_root = Path(monitor.storage_root) if monitor is not None else None
        if storage_root is None:
            messagebox.showerror("저장 위치 없음", "공대 세션 저장 위치를 찾지 못했습니다.", parent=self.win)
            return
        self.app._feign_recheck_running = True
        self._update_run_btn()
        self._write(f"재검사 중 · {len(rows)}개\n")
        raid_only = bool(getattr(self.app, "raid_public_mode", True))

        def worker():
            from .feign_recheck import recheck_selected
            try:
                res = recheck_selected(
                    rows, storage_root, raid_only=raid_only,
                    progress=lambda i, n, label: self.q.put(("progress", f"재검사 중 · {i}/{n} · {label}")))
                self.q.put(("recheck_done", {"results": res}))
            except Exception as exc:
                self.q.put(("recheck_done", {"error": str(exc), "results": []}))

        threading.Thread(target=worker, name="nini-feign-recheck", daemon=True).start()
        self.app.after(150, self._poll)

    def _recheck_done(self, data):
        from .feign_recheck import render_result
        self.app._feign_recheck_running = False
        results = data.get("results") or []
        done = sum(1 for r in results if r.get("status") == "done")
        failed = len(results) - done
        try:
            self.app._refresh_live_recent(force_storage_reload=True)
        except Exception:
            pass
        try:
            self.app.status_var.set(f"죽척 재검사 완료 · {done}개" + (f" · 재분석 안 함 {failed}개" if failed else ""))
        except Exception:
            pass
        if not self._alive():
            return
        text = []
        if data.get("error"):
            text.append(f"✕ 재검사 오류 · {data['error']}\n")
        text.extend(render_result(r) for r in results)
        self._write("\n".join(text) or "처리된 트라이가 없습니다.\n")
        self.status_var.set(f"재검사 완료 · {done}개" + (f" · 재분석 안 함 {failed}개" if failed else "")
                            + " · 다시 검색하면 최신 상태가 표시됩니다")
        done_dirs = {str(r.get("storageDir")) for r in results if r.get("status") == "done"}
        for iid, it in list(self.items.items()):
            if str(it.get("storageDir")) in done_dirs and self.tree.exists(iid):
                self.checked.discard(iid)
                self.tree.set(iid, "sel", CHECK_OFF)
                self.tree.set(iid, "note", "재검사 완료")
        self._update_run_btn()

    def close(self):
        if getattr(self.app, "_feign_recheck_running", False):
            messagebox.showinfo("재검사 진행 중", "재검사가 끝난 뒤 닫아 주세요.", parent=self.win)
            return
        self.win.destroy()
