"""Local crash diagnostics and a single user-visible PUBLIC error notice."""
from __future__ import annotations

import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

from live.public_paths import public_home

_PENDING_NOTICE = threading.Event()
_NOTICE_SHOWN = False

def error_path() -> Path:
    return public_home() / "error.log"

def log_exception(exc_type, exc_value, exc_tb):
    """Always write locally; never transmit logs or touch NiniWoWSync."""
    try:
        folder = public_home()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "error.log"
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(folder / "error.previous.log")
        with path.open("a", encoding="utf-8") as out:
            out.write("\n[" + datetime.now().isoformat(timespec="seconds") + "] PUBLIC UNHANDLED\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=out)
    except Exception:
        pass
    _PENDING_NOTICE.set()

def install_error_logging():
    sys.excepthook = log_exception
    if hasattr(threading, "excepthook"):
        def _thread_exception(args):
            log_exception(args.exc_type, args.exc_value, args.exc_traceback)
        threading.excepthook = _thread_exception

def tk_error_callback(self, exc_type, exc_value, exc_tb):
    log_exception(exc_type, exc_value, exc_tb)

def start_error_notice_poll(app, *, interval_ms=600):
    """Poll from the Tk owner thread; worker threads must never create Tk dialogs."""
    def check():
        global _NOTICE_SHOWN
        if _PENDING_NOTICE.is_set() and not _NOTICE_SHOWN:
            _NOTICE_SHOWN = True
            _PENDING_NOTICE.clear()
            try:
                from tkinter import messagebox
                messagebox.showerror(
                    "NiniAnalyzer Public 오류",
                    "문제가 발생해 오류 기록을 저장했습니다.\n"
                    "문의할 때 다음 파일을 알려주세요:\n\n" + str(error_path()),
                    parent=app,
                )
            except Exception:
                pass
        try:
            if app.winfo_exists():
                app.after(interval_ms, check)
        except Exception:
            pass
    app.after(interval_ms, check)

def show_startup_error_dialog():
    """Best-effort notice even if App cannot be constructed."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        temp = tk.Tk()
        temp.withdraw()
        try:
            messagebox.showerror(
                "NiniAnalyzer Public 시작 오류",
                "프로그램을 시작하지 못했습니다.\n오류 기록:\n" + str(error_path()),
                parent=temp,
            )
        finally:
            temp.destroy()
    except Exception:
        pass
