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
    ("1.0.28", [
        ("오늘의 교정", "레이드 세션 화면 오른쪽 아래 「오늘의 교정」에 그날 똬리 · 울라텍 보완사항을 보스별로 보여 줍니다 (비어 있던 칸)."),
    ]),
    ("1.0.27", [
        ("최종 정리 제목", "트라이 보드 아래 칸 제목을 「그날 최종 정리」에서 「최종 정리」로 줄였습니다."),
        ("보완사항 칸", "오른쪽 「다음 풀 전에 말할 것」이 「보완사항」으로 바뀌고 그날 보완사항을 전부 보여 줍니다. 아래 칸엔 수치만."),
    ]),
    ("1.0.26", [
        ("1.0.25 다시 배포", "1.0.25 설치 파일이 빌드 테스트에서 막혀 안 만들어졌습니다. 1.0.25 내용(그날 최종 정리 · 알 조언 · 단두대 인원 · 공포의 통곡)을 그대로 담았습니다."),
        ("그날 정리 · 윈도우 경로", "트라이 폴더 경로에 / 와 \\ 가 섞여 있어도 그날 정리가 트라이를 제대로 찾습니다."),
    ]),
    ("1.0.25", [
        ("그날 최종 정리", "트라이 보드 아래에 똬리·울라텍 그날 수치와 보완사항 칸이 생깁니다. 처음 계산은 트라이당 몇 초 (「멈춘 거 아님」 표시)."),
        ("알 든 채 사망 조언", "조언을 「알 운반자 힐·생존기 우선」까지만 씁니다. 알 운반은 특임이 정해져 있어 「알 넘기기」는 뺐습니다."),
        ("똬리 단두대 막은 인원", "무적기·회피로 받은 사람도 막은 인원으로 셉니다 (예전에는 인원에서 뺌)."),
        ("똬리 공포의 통곡", "영혼살무사 공포의 통곡을 못 끊어 공대 전체 공포 → 바로 무너지면 전멸 원인으로 잡고, 누가 차단했는지 같이 보여 줍니다."),
    ]),
    ("1.0.24", [
        ("울라텍 살무사 원인 순서", "살무사보다 뒤에 맞은 파도는 원인으로 잡지 않고, 알 든 사람이 죽어서 알이 깨진 경우는 「알 든 이름 사망 → 알 깨짐」으로 적습니다."),
        ("울라텍 똥물 · 킬", "공대 절반이 이미 죽은 뒤 빠진 똥물은 원인으로 보지 않습니다. 킬에서 1:15쯤 늘 나오는 막을 수 없는 분노는 첫 실수로 안 씁니다."),
        ("똬리 막페 역병의 절단", "탱이 죽은 직후 줄잔 탱 전방(역병의 절단)에 다른 사람이 맞아 무너지면 「탱 사망 → 역병의 절단」으로 판정합니다."),
    ]),
    ("1.0.23", [
        ("송곳니 줄 늦게 끊음", "송곳니 줄을 역병의 핏줄이 걸려 있는 동안 2틱 뒤에 끊은 사람을 「줄」에 ❌로 보여 줍니다. 중첩이 그대로 연장돼 공대 전체가 한 번 더 맞기 때문이고, 그 뒤 바로 무너졌으면 전멸 원인으로 잡습니다."),
        ("늦으면 2틱 보고", "늦었어도 핏줄이 끝난 뒤(2틱 보고) 끊었으면 새 핏줄 1중첩부터라 ✅로 봅니다."),
        ("탱 사망 → 붕괴", "다른 뿌리가 없고 붕괴 직전(3~20초 전)에 탱이 죽었으면 전멸 원인으로 잡고, 탱이 무엇에 맞았는지와 그에 맞는 조언(약화된 파멸비늘은 탱 둘이 하나씩 등)을 보여 줍니다."),
    ]),
    ("1.0.22", [
        ("울라텍 똥물", "단상과 알방 사이 똥물에 빠진 사람(죽음의 맹독)을 「똥물」 줄로 보여 주고, 여러 명이 빠져 죽었으면 전멸 원인으로 잡습니다."),
        ("살무사 → 1심장 미달 → 똥물", "똥물 몰살 앞에 1심장 66% 미달이 있으면 그걸 뿌리로 봅니다. 그동안 공대 딜 중 살무사에 들어간 비중도 적고, 살무사가 딜을 먹었으면 알 · 살무사 조언을 보여 줍니다."),
        ("붕괴 직전 사망 순서", "원인을 못 정한 트라이는 「직접 확인하세요」 대신 붕괴 직전 10초에 누가 무엇에 죽었는지 적어 줍니다. 독성 배양 조언은 탱 몫으로 표시합니다."),
    ]),
    ("1.0.21", [
        ("울라텍 알 처리", "작은 알을 내려놓으면 붙는 「썩은 노른자」(20초)가 남은 채로 또 알을 집은 사람과, 알끼리 부딪혀 「맹독성 분출」을 받고 10초 안에 죽은 사람을 알 줄에 보여 줍니다."),
    ]),
    ("1.0.20", [
        ("구글 로그인 암호화", "구글 캘린더 로그인 정보를 이 PC·이 Windows 계정에서만 풀리게 암호화(DPAPI)해서 저장합니다. 이미 연결한 분은 처음 동기화할 때 저절로 바뀝니다."),
    ]),
    ("1.0.19", [
        ("공대 계획", "왼쪽 「공대 계획」에서 우리 공대의 울라텍 큰 알 시각과 남쪽 구역 택틱을 적어 두면 그 기준으로 판정합니다. 비워 두면 시각은 판정하지 않습니다."),
        ("울라텍 남쪽 구역", "남쪽 보호막 알을 깼는지·들었는지, 큰 알을 집을 때 산란무리 → 유체가 나왔는지, 남쪽 유체가 광폭 전에 잡혔는지를 「남쪽」 줄로 보여 줍니다."),
    ]),
    ("1.0.18", [
        ("울라텍 알 깨짐 원인", "살무사가 나온 원인을 「알 든 사람 알 깨짐」(공대 전체 부패한 막)으로 확정하고, 아무도 안 든 알이 깨진 경우도 따로 알려 줍니다."),
    ]),
    ("1.0.17", [
        ("울라텍 큰 알 4:18 · 4:30", "큰 알 줄과 판정이 공대 공략(4:18에 집고 4:30쯤 가마솥)에 맞춰집니다. 4:31을 넘겨 넣어야 늦음으로 봅니다."),
    ]),
    ("1.0.16", [
        ("WoW 달력 → 구글 캘린더", "왼쪽 「WoW 달력 → 구글」에서 구글을 연결하면 NiniTools 계정 달력의 공대 초대가 내 구글 캘린더 「니니 레이드 (WoW)」에 자동으로 들어갑니다 (켜야 동작)."),
        ("내 일정은 안 건드림", "권한은 「이 앱이 만든 캘린더만」 하나라 다른 일정은 읽지도 고치지도 못합니다. 구글 연결 없이 .ics 파일로 내보낼 수도 있습니다."),
    ]),
    ("1.0.15", [
        ("마지막 트라이 따라가기", "실시간 감시 중 트라이 복기가 끝나면 그 트라이 화면으로 바로 넘어갑니다. 지난 트라이는 눌러서 보면 됩니다."),
        ("큰 알 → 공포의 포효", "울라텍 큰 알을 안 집어 나온 게걸스러운 파멸비늘의 공포의 포효로 무너진 판을 첫 실수 · 전멸 원인으로 잡습니다."),
        ("큰 알 알림 기준", "큰 알 줄이 들기 알림 4:12 · 마지막 4:19 · 부화 4:24 · 넣은 시각을 한 줄로 보여 줍니다."),
    ]),
    ("1.0.14", [
        ("무너진 순서 시각 순", "한눈에 보기의 무너진 순서 칩이 시각 순서로 놓입니다. 시각이 비어 있던 칩은 바로 다음 단계의 시각을 씁니다."),
    ]),
    ("1.0.13", [
        ("놓친 트라이 채우기", "실시간 감시를 켜면, 감시를 켜기 전에 지나간 풀(프로그램을 껐다 켠 사이 등)을 뒤에서 자동으로 채웁니다. "
                         "채워지는 풀은 목록에 바로 나타나고 상태줄에 진행 상황이 보입니다. Try 번호는 시간 순서로 다시 매깁니다."),
        ("울라텍 판정 보완", "붕괴가 시작된 뒤의 「막을 수 없는 분노」는 첫 실수로 치지 않습니다. 알 충돌이 첫 실수면 다음 풀 한 줄이 "
                        "「알 든 사람끼리 3미터 떨어지기」로 나옵니다."),
    ]),
    ("1.0.12", [
        ("풀러 판정", "「정신의 위안」처럼 전투를 걸지 않는 주문은 풀러(선풀)로 치지 않습니다. 이미 분석한 기록은 다시 분석하면 바뀝니다."),
    ]),
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
