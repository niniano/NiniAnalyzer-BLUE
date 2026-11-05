"""One-time PUBLIC welcome. Never touches private analyzer data."""
from __future__ import annotations

from tkinter import messagebox
from pathlib import Path

from live.io_utils import atomic_write_json
from live.public_paths import public_home, read_public_settings

WELCOME_KEY = "firstRunGuideShown"

def maybe_show_first_run(app, home=None, ask_fn=None, info_fn=None) -> bool:
    """Display once after GUI startup; guide to the existing Logs settings workflow."""
    folder = public_home() if home is None else home
    settings = read_public_settings(folder)
    if settings.get(WELCOME_KEY):
        return False
    if (getattr(app, "_public_import_running", False) or
        getattr(app, "live_running", False) or
        getattr(app, "_public_migration_running", False)):
        # Do not interrupt an import, live raid, or legacy migration.
        app.after(1500, lambda: maybe_show_first_run(app, home=folder, ask_fn=ask_fn, info_fn=info_fn))
        return False
    # Mark BEFORE opening a modal dialog. A crash or early close must not repeat it.
    settings[WELCOME_KEY] = True
    atomic_write_json(folder / "settings.json", settings)
    detected = getattr(app, '_public_log_dir', None)
    if detected is not None:
        try:
            detected = Path(detected)
            if detected.is_dir() and detected.name.casefold() == 'logs':
                guide = (
                    "NiniAnalyzer Public에 오신 것을 환영합니다!\n\n"
                    "WoW 전투 로그 폴더를 찾았습니다:\n"
                    f"{detected}\n\n"
                    "1. 게임에서 고급 전투 기록을 켜고 /combatlog를 사용하세요.\n"
                    "2. '최신 로그 분석'으로 전투 기록을 가져오세요.\n"
                    "3. 레이드 세션에서 보스와 트라이를 선택해 복기하세요.\n\n"
                    "로그 폴더는 왼쪽 '설정'에서 변경할 수 있습니다."
                )
                inform = info_fn or messagebox.showinfo
                inform('NiniAnalyzer Public · 첫 실행 안내', guide, parent=app)
                return True
        except (OSError, ValueError, TypeError):
            pass
    message = (
        "NiniAnalyzer Public에 오신 것을 환영합니다!\n\n"
        "1. WoW에서 고급 전투 기록을 켜고 /combatlog로 기록을 시작하세요.\n"
        "2. '설정'에서 월드 오브 워크래프트 _retail_\\Logs 폴더를 선택하세요.\n"
        "3. '최신 로그 분석'으로 로그를 가져오세요.\n"
        "4. 레이드 세션에서 보스와 트라이를 선택해 복기하세요.\n\n"
        "WoW Logs 폴더를 지금 선택할까요?\n"
        "(나중에 왼쪽 '설정'에서 변경할 수 있습니다.)"
    )
    choose = ask_fn or messagebox.askyesno
    if choose("NiniAnalyzer Public · 첫 실행 안내", message, parent=app):
        app._public_select_log_directory()
    return True
