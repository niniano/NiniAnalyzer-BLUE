"""1.0.3 PUBLIC: 첫 실행 안내 카드 · 새 소식 카드.

- 첫 실행 안내: 메시지 상자 대신 3단계 카드 창 (전투 기록 켜기 → 로그 폴더 → 최신 로그 분석).
  로그 폴더를 찾았는지 바로 보여 주고, 그 자리에서 폴더 선택 · 분석 시작을 할 수 있다.
- 새 소식: 새 버전으로 처음 켰을 때 바뀐 내용을 카드로 한 번 보여 준다.
  왼쪽 위 버전 글자를 누르면 언제든 다시 볼 수 있다.
분석 데이터는 건드리지 않는다. settings.json 의 lastSeenVersion 만 쓴다.
"""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .io_utils import atomic_write_json
from .public_paths import public_home, read_public_settings

SEEN_KEY = "lastSeenVersion"

# 최신이 위. (버전, [(제목, 설명), ...])
WHATS_NEW: list[tuple[str, list[tuple[str, str]]]] = [
    ("1.0.11", [
        ("한눈에 보기 다듬기", "무너진 순서 칩을 한 줄에 같은 크기로 놓고 굵은 화살표를 칩 가운데에 두었습니다. 칩 · 숫자 카드 · 다음 풀 칸은 "
                         "옆 카드처럼 둥근 테두리입니다. 같은 글이 두 번 나오던 칩과 「전멸」 칩 두 번도 고쳤습니다."),
        ("버튼 하나로", "「판정 상세 펼치기」와 「자세히 펼치기」를 칸 제목 오른쪽 버튼 하나(「자세히 펼치기」)로 합쳤습니다. "
                    "아래의 「B31 원본 · 개인 사망 · 쿨기 비교 상세」 버튼은 없앴습니다 (같은 글은 「개인 사망 열기」 · 「전멸 복기 열기」로 볼 수 있습니다)."),
        ("오피격 → 같이 맞음", "탱 브레스를 탱커가 아닌 사람이 맞은 것을 「오피격」 대신 「같이 맞음」으로 씁니다."),
    ]),
    ("1.0.10", [
        ("한눈에 보기", "트라이를 열면 결론 칸 맨 위에 무너진 순서(첫 실수 → … → 전멸)를 칩으로, 어디까지 · 보스 HP · 사망과 "
                     "보스별 숫자 2칸을 카드로, 다음 풀에 할 일 한 가지를 초록 칸으로 보여 줍니다. 긴 5줄 문장과 참고는 「자세히 펼치기」 안에 있습니다."),
        ("참고 줄 잘림 고침", "결론 칸 · 참고 줄의 마지막 줄이 조금 잘려서 스크롤해야 보이던 것을 고쳤습니다. "
                       "이름처럼 큰 글자가 섞인 줄도 글 전체 높이에 맞춰 다 보입니다 (Windows 화면 배율에서 특히)."),
    ]),
    ("1.0.9", [
        ("울라텍 알 운반 카드", "오른쪽 「기믹 수행 체크」 아래에 차수(1차 · 2차 · 알방)마다 누가 알을 들었는지와 큰 알 4개를 보여 줍니다. "
                          "파도 · 충돌 · 먼저 놓음 · 들고 사망은 빨강, 늦은 큰 알은 주황, 전멸 중에 생긴 것은 회색입니다."),
        ("참고 줄 짧게", "누가 들었는지는 카드로 옮기고, 결론 아래 참고 줄에는 횟수와 결과만 남겨 스크롤 없이 보이게 했습니다."),
    ]),
    ("1.0.8", [
        ("울라텍 알방 큰 알", "2페(알방) 큰 알 4개를 누가 언제 들고 넣었는지와 4:15 기한을 결론 아래 참고 첫 줄에 보여 줍니다. "
                          "기한보다 늦게 든 사람은 「늦음」, 안 든 큰 알과 들고 죽은 큰 알에서 나온 게걸스러운 파멸비늘도 적습니다."),
    ]),
    ("1.0.7", [
        ("똬리 신화 판정 2가지", "P1 단두대 처형 붕괴 바로 뒤에 보스가 0%가 된 트라이를 P2 전멸이 아니라 P1 단두대 실패로 봅니다. "
                             "킬한 트라이도 결론 칸에 사망 정리 · P2 진입/사이페/킬 시각 비교 · 다음 주가 나옵니다."),
        ("울라텍 심장 도중 전멸", "심장 도중에 전멸한 트라이를 「1심장 종료 · 딜 미달」로 보지 않습니다. 「1심장 도중 전멸 · 딜 판정 안 함」으로 적고 "
                               "딜 조언도 붙이지 않습니다."),
    ]),
    ("1.0.6", [
        ("키식스 판정 보강", "유동 신화(233)도 신화 기준으로 · 기믹 수행 체크 칸(빛구슬 · 빛 안 · 쫄·알 · 딱정벌레 · 일월식 파편 · "
                         "촉수 · 무효화 중첩 · 탱 교대 · 공포 중 리혼·결의) · 소화액도 피할 수 있는 실수 · 무효화 3중첩 즉사 묶음."),
        ("킬 판 결론 · 보스 보드", "킬한 트라이도 결론 칸에 사망 정리 · 다음 주가 나옵니다. 엔진 판정이 없는 보스도 보스 보드에 결론 5줄로 보입니다. "
                               "시간축에서 겹친 같은 기믹은 하나로 묶고 ×n 을 붙입니다."),
        ("똬리 신화 정확도", "P1 단두대 소킹 인원 칸 · 붕괴 뒤에 난 실패는 첫 실수에서 제외 · 일몰 완료 뒤 보호막 해제는 「파괴 미확정」 · "
                          "P1 전환 지연 중 맹독성 파열 집단 사망 관측."),
    ]),
    ("1.0.5", [
        ("울라텍 신화 · 알 운반 → 살무사", "알 든 사람이 부식의 파도에 맞거나 알끼리 부딪혀 살무사가 나온 것을 전멸 원인으로 잡습니다. "
                                    "결론 아래 참고 줄에 알 든 사람이 맞은 파도 전부와, 살무사가 찍은 사람 → 착지 스턴에 같이 걸린 사람을 보여 줍니다."),
    ]),
    ("1.0.3", [
        ("첫 실행 안내", "처음 켜면 3단계 카드로 안내합니다. 로그 폴더를 찾았는지 바로 보이고, "
                     "그 자리에서 폴더 선택과 최신 로그 분석을 할 수 있습니다."),
        ("새 소식", "새 버전으로 처음 켜면 바뀐 내용을 이 카드로 한 번 보여 줍니다. "
                  "왼쪽 위 버전 글자를 누르면 다시 볼 수 있습니다."),
    ]),
    ("1.0.2", [
        ("똬리의 제단 신화 판정 3개 수정", "P2 진입 잔여 구슬을 진입 시점 기준으로 계산 · P1→P2 진입이 몇 초 빠르거나 "
                                  "늦으면 실패 대신 주의 · 보호막이 제때 깨졌으면 쿨기 지연은 참고로만 표시"),
    ]),
    ("1.0.1", [
        ("블러드 판정 수정", "블러드 계열 효과가 한 사람에게만 걸린 경우(개인 버프 · 증강 등)를 "
                         "공대 블러드 사용으로 잘못 보던 문제를 고쳤습니다."),
    ]),
]


def _app_version(app=None) -> str:
    v = getattr(app, "public_version", None)
    if v:
        return str(v)
    mod = sys.modules.get("nini_wcl_gui")
    return str(getattr(mod, "PUBLIC_APP_VERSION", "0.0.0"))


def _vkey(v: str) -> tuple[int, ...]:
    parts = []
    for p in str(v or "").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts + [0] * (3 - len(parts)))


def entries_since(since: str | None, current: str, limit: int = 3):
    """since 보다 새 버전(현재 버전까지). since 가 없으면 최근 limit 개."""
    out = [(v, items) for v, items in WHATS_NEW if _vkey(v) <= _vkey(current)]
    if since:
        return [(v, items) for v, items in out if _vkey(v) > _vkey(since)]
    return out[:limit]


def _busy(app) -> bool:
    return bool(getattr(app, "_public_import_running", False) or getattr(app, "live_running", False)
                or getattr(app, "_public_migration_running", False))


def mark_seen(folder: Path, settings: dict, version: str) -> None:
    settings[SEEN_KEY] = version
    atomic_write_json(Path(folder) / "settings.json", settings)


# ---------------------------------------------------------------- 공통 카드
def _card(parent, title: str, text: str, badge: str = "", wrap: int = 460):
    card = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10, 14, 12))
    head = ttk.Frame(card, style="Card.TFrame")
    head.pack(fill="x")
    ttk.Label(head, text=title, style="CardTitle.TLabel").pack(side="left", anchor="w")
    card.badge = ttk.Label(head, text=badge, style="Muted.Card.TLabel")
    card.badge.pack(side="right", anchor="e")
    body = ttk.Label(card, text=text, style="Muted.Card.TLabel", wraplength=wrap, justify="left")
    body.pack(anchor="w", fill="x", pady=(6, 0))
    return card, body


def _window(app, attr: str, title: str):
    existing = getattr(app, attr, None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.destroy()
        except tk.TclError:
            pass
    win = tk.Toplevel(app)
    setattr(app, attr, win)
    win.title(title)
    win.transient(app)
    win.resizable(False, False)
    try:
        win.configure(bg=ttk.Style(app).lookup("App.TFrame", "background") or "#13161b")
    except tk.TclError:
        pass
    win.bind("<Escape>", lambda _e: win.destroy())
    body = ttk.Frame(win, style="App.TFrame", padding=18)
    body.pack(fill="both", expand=True)
    return win, body


def _center(app, win) -> None:
    try:
        win.update_idletasks()
        x = app.winfo_rootx() + max(0, (app.winfo_width() - win.winfo_width()) // 2)
        y = app.winfo_rooty() + max(0, (app.winfo_height() - win.winfo_height()) // 3)
        win.geometry(f"+{x}+{y}")
        win.lift()
        win.focus_force()
    except tk.TclError:
        pass


# ---------------------------------------------------------------- 첫 실행 안내
def _folder_state(app) -> tuple[bool, str]:
    folder = getattr(app, "_public_log_dir", None)
    try:
        if folder and Path(folder).is_dir() and Path(folder).name.casefold() == "logs":
            has_log = any(Path(folder).glob("WoWCombatLog-*.txt"))
            return True, f"찾음 · {folder}" + ("" if has_log else "\n아직 전투 기록 파일은 없어요. 게임에서 /combatlog 후 다시 와 주세요.")
    except (OSError, ValueError, TypeError):
        pass
    return False, "아직 못 찾았어요. 아래 「폴더 선택」으로 World of Warcraft\\_retail_\\Logs 를 골라 주세요."


def open_welcome(app):
    win, body = _window(app, "_public_welcome_win", "NiniAnalyzer · 첫 실행 안내")
    ttk.Label(body, text="NiniAnalyzer에 오신 걸 환영해요", style="Title.TLabel").pack(anchor="w")
    ttk.Label(body, text="세 단계면 첫 복기를 볼 수 있어요. 로그는 이 PC 안에서만 읽어요.",
              style="Muted.TLabel").pack(anchor="w", pady=(4, 12))

    c1, _ = _card(body, "1 · 게임에서 전투 기록 켜기",
                  "시스템 → 네트워크 → 「고급 전투 기록」을 켜고, 레이드 전에 채팅창에 /combatlog 를 입력하세요.",
                  "게임")
    c1.pack(fill="x")

    ok, text = _folder_state(app)
    c2, folder_label = _card(body, "2 · WoW 로그 폴더", text, "✓ 준비됨" if ok else "선택 필요")
    c2.pack(fill="x", pady=(8, 0))
    row2 = ttk.Frame(c2, style="Card.TFrame")
    row2.pack(fill="x", pady=(8, 0))

    c3, _ = _card(body, "3 · 최신 로그 분석",
                  "왼쪽 아래 「최신 로그 분석」과 같은 버튼이에요. 가져온 뒤 레이드 세션에서 보스와 트라이를 골라 복기하세요.",
                  "")
    c3.pack(fill="x", pady=(8, 0))
    row3 = ttk.Frame(c3, style="Card.TFrame")
    row3.pack(fill="x", pady=(8, 0))

    def run_import():
        win.destroy()
        app._public_import_latest()

    run_btn = ttk.Button(row3, text="최신 로그 분석", style="Accent.TButton", command=run_import)
    run_btn.pack(side="left")

    def refresh():
        ok2, text2 = _folder_state(app)
        folder_label.configure(text=text2)
        c2.badge.configure(text="✓ 준비됨" if ok2 else "선택 필요")
        run_btn.configure(state=("normal" if ok2 else "disabled"))

    def choose():
        app._public_select_log_directory()
        refresh()

    ttk.Button(row2, text="폴더 선택", command=choose).pack(side="left")
    refresh()

    foot = ttk.Frame(body, style="App.TFrame")
    foot.pack(fill="x", pady=(14, 0))
    ttk.Label(foot, text="로그 폴더는 나중에 왼쪽 「설정」에서도 바꿀 수 있어요.", style="Muted.TLabel").pack(side="left")
    ttk.Button(foot, text="나중에", command=win.destroy).pack(side="right")
    _center(app, win)
    return win


# ---------------------------------------------------------------- 새 소식
def open_whats_new(app, since: str | None = None):
    current = _app_version(app)
    entries = entries_since(since, current)
    if not entries:
        return None
    win, body = _window(app, "_public_whats_new_win", "NiniAnalyzer · 새 소식")
    ttk.Label(body, text="새 소식", style="Title.TLabel").pack(anchor="w")
    sub = (f"v{since} → v{current} 에서 바뀐 내용" if since else f"최근 바뀐 내용 · 현재 v{current}")
    ttk.Label(body, text=sub, style="Muted.TLabel").pack(anchor="w", pady=(4, 12))
    first = True
    for version, items in entries:
        for title, text in items:
            card, _ = _card(body, title, text, f"v{version}")
            card.pack(fill="x", pady=(0 if first else 8, 0))
            first = False
    foot = ttk.Frame(body, style="App.TFrame")
    foot.pack(fill="x", pady=(14, 0))
    ttk.Button(foot, text="확인", style="Accent.TButton", command=win.destroy).pack(side="right")
    _center(app, win)
    return win


def maybe_show_whats_new(app, home=None, show_fn=None) -> bool:
    """새 버전으로 처음 켰을 때 한 번. 새로 설치한 경우(첫 실행 안내를 아직 안 본 경우)는 띄우지 않는다."""
    folder = public_home() if home is None else Path(home)
    settings = read_public_settings(folder)
    current = _app_version(app)
    seen = settings.get(SEEN_KEY)
    if seen == current:
        return False
    if _busy(app):
        try:
            app.after(1500, lambda: maybe_show_whats_new(app, home=folder, show_fn=show_fn))
        except Exception:
            pass
        return False
    if not seen and not settings.get("firstRunGuideShown"):
        return False   # 새 설치: 첫 실행 안내가 맡는다 (그쪽이 lastSeenVersion 을 적는다)
    since = seen or "1.0.2"   # 1.0.3 이전 사용자는 기록이 없다
    mark_seen(folder, settings, current)   # 창을 열기 전에 적어서, 닫기 전에 꺼져도 반복하지 않음
    if not entries_since(since, current):
        return False
    (show_fn or open_whats_new)(app, since)
    return True
