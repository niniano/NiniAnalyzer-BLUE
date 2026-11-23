"""1.0.19: 「공대 계획」 창 — 우리 공대 택틱(울라텍 큰 알 시각 · 남쪽 구역)을 저장한다.

공대마다 택틱이 달라서, 비워 두면 큰 알 시각은 판정하지 않고 보스 기믹으로 정해진 것만 판정한다.
저장 위치: 데이터 폴더의 raid_plans.json (이 PC에만).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from live import raid_plans as RP
from live import ui_theme as T
from live.public_paths import public_home
from live.ui_theme import RoundButton

FONT = "Segoe UI"
PLAN_ID = "mine"


def plan_file():
    return public_home() / RP.PLAN_FILE_NAME


def current() -> dict:
    data = RP.load([plan_file()])
    p = next((p for p in data.get("plans") or [] if p.get("id") == PLAN_ID), None) or {}
    u = p.get("ulatek") or {}
    return {"name": p.get("name") or "", "pick": u.get("bigEggPick") or "", "put": u.get("bigEggPut") or "",
            "south": u.get("south") or ""}


def save_values(name: str, pick: str, put: str, south: str, path=None) -> dict:
    """입력값 확인 → raid_plans.json 저장. 시각이 이상하면 ValueError."""
    for label, v in (("큰 알 집기", pick), ("큰 알 넣기", put)):
        if v.strip() and RP.parse_mmss(v.strip()) is None:
            raise ValueError(f"{label} 시각은 4:18 처럼 적어 주세요.")
    south = south if south in RP.SOUTH_TACTICS else ""
    ul = {k: v for k, v in (("bigEggPick", pick.strip()), ("bigEggPut", put.strip()), ("south", south)) if v}
    data = {"default": PLAN_ID,
            "plans": [{"id": PLAN_ID, "name": name.strip() or "우리 공대", "members": [], "ulatek": ul}]}
    RP.save(data, path or plan_file())
    return data


def clear(path=None) -> None:
    RP.save({"plans": []}, path or plan_file())


class PlanWindow:
    def __init__(self, app):
        self.app = app
        cur = current()
        w = tk.Toplevel(app)
        w.title("공대 계획")
        w.configure(bg=T.PAGE_BG)
        w.geometry("560x520")
        w.minsize(480, 460)
        self.win = w
        pad = {"padx": 16}

        def label(parent, text, size=10, fg=T.TEXT, bold=False):
            return tk.Label(parent, text=text, bg=T.PAGE_BG, fg=fg, font=(FONT, size, "bold" if bold else "normal"),
                            justify="left", anchor="w", wraplength=520)

        label(w, "공대 계획", 14, bold=True).pack(fill="x", pady=(14, 2), **pad)
        label(w, "공대마다 택틱이 달라서 우리 공대 시각을 적어 두면 그 기준으로 판정합니다. "
                 "비워 두면 시각은 판정하지 않고 보스 기믹(안 집은 큰 알 · 20초 · 유체 광폭 등)만 봅니다. "
                 "저장한 뒤 트라이를 「다시 분석」하면 새 기준으로 나옵니다.", 9, T.MUTED).pack(fill="x", **pad)

        box = tk.Frame(w, bg=T.PAGE_BG)
        box.pack(fill="x", pady=(12, 0), **pad)
        self.v_name = tk.StringVar(value=cur["name"])
        self.v_pick = tk.StringVar(value=cur["pick"])
        self.v_put = tk.StringVar(value=cur["put"])
        self.v_south = tk.StringVar(value=cur["south"])

        def row(text, var, hint):
            f = tk.Frame(box, bg=T.PAGE_BG)
            f.pack(fill="x", pady=(6, 0))
            tk.Label(f, text=text, width=12, anchor="w", bg=T.PAGE_BG, fg=T.TEXT, font=(FONT, 10)).pack(side="left")
            tk.Entry(f, textvariable=var, width=14, font=(FONT, 10)).pack(side="left")
            tk.Label(f, text=hint, bg=T.PAGE_BG, fg=T.MUTED, font=(FONT, 9)).pack(side="left", padx=(8, 0))

        row("공대 이름", self.v_name, "")
        label(box, "울라텍 신화 · 알방", 11, T.ORANGE, True).pack(fill="x", pady=(12, 0))
        row("큰 알 집기", self.v_pick, "예) 4:18")
        row("큰 알 넣기", self.v_put, "예) 4:30 · 1초 넘으면 늦음")
        label(box, "남쪽 구역 (큰 알 + 보호막 알 2개)", 10).pack(fill="x", pady=(10, 0))
        for value, text in (("", "정하지 않음"),
                            ("shield_first", "1번 · 보호막 알 먼저 깨고 든 다음 큰 알 (유체는 가마솥에 넣을 때)"),
                            ("big_first", "2번 · 큰 알 먼저 (보호막 알 → 산란무리 → 남쪽에서 유체)")):
            tk.Radiobutton(box, text=text, value=value, variable=self.v_south, bg=T.PAGE_BG, fg=T.TEXT,
                           selectcolor=T.PAGE_BG, activebackground=T.PAGE_BG, font=(FONT, 9), anchor="w",
                           justify="left").pack(fill="x")

        btns = tk.Frame(w, bg=T.PAGE_BG)
        btns.pack(fill="x", pady=(16, 0), **pad)
        RoundButton(btns, text="저장", kind="accent", command=self.save, font=(FONT, 10), outer=T.PAGE_BG, pady=5).pack(side="left")
        RoundButton(btns, text="계획 지우기", command=self.clear, font=(FONT, 9), outer=T.PAGE_BG, pady=5).pack(side="left", padx=(6, 0))
        self.status = tk.StringVar(value="")
        tk.Label(w, textvariable=self.status, bg=T.PAGE_BG, fg=T.MUTED, font=(FONT, 9), anchor="w").pack(fill="x", pady=(8, 0), **pad)

    def save(self):
        try:
            data = save_values(self.v_name.get(), self.v_pick.get(), self.v_put.get(), self.v_south.get())
        except ValueError as e:
            messagebox.showwarning("공대 계획", str(e), parent=self.win)
            return
        self.status.set("저장했습니다 · " + RP.plan_line(RP.resolve(None, data)))

    def clear(self):
        clear()
        self.v_pick.set("")
        self.v_put.set("")
        self.v_south.set("")
        self.status.set("계획을 지웠습니다 · 시각 판정 안 함")


def open_window(app) -> None:
    win = getattr(app, "_raid_plan_window", None)
    try:
        if win is not None and win.win.winfo_exists():
            win.win.deiconify()
            win.win.lift()
            return
    except Exception:
        pass
    app._raid_plan_window = PlanWindow(app)
