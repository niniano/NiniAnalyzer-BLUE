from __future__ import annotations

import os
import sys
from pathlib import Path
from public_errorlog import (
    install_error_logging, tk_error_callback, start_error_notice_poll,
    log_exception, show_startup_error_dialog,
)

WORKDIR = Path(__file__).resolve().parent
APP_USER_MODEL_ID = "NINI.Analyzer.Public"

def _set_windows_app_identity() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass

def _apply_icon(app) -> None:
    from _nini_win_icon import install_icon
    resources = WORKDIR / "resources" / "brand_public"
    if not resources.is_dir():
        resources = WORKDIR / "resources"
    install_icon(app, resources)

def main() -> None:
    # Crash logger is installed before the large GUI and optional dependencies.
    install_error_logging()
    from nini_wcl_gui import App
    from public_first_run import maybe_show_first_run
    _set_windows_app_identity()
    App.report_callback_exception = tk_error_callback
    app = App(edition="raid")
    _apply_icon(app)
    start_error_notice_poll(app)
    # First launch guide is entrypoint-only (not shown by tests constructing App).
    app.after(1800, lambda: maybe_show_first_run(app))
    # 1.0.3: 새 버전으로 처음 켰을 때 바뀐 내용 카드 (새 설치는 첫 실행 안내만)
    from live.public_onboarding import maybe_show_whats_new
    app.after(2600, lambda: maybe_show_whats_new(app))
    app.mainloop()

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_exception(*sys.exc_info())
        show_startup_error_dialog()
        raise SystemExit(1)
