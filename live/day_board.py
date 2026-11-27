"""1.0.25 보드 「그날 최종 정리」 칸.

- 보드 아래에 칸 하나: 「최종 정리」(그날 수치)
- 1.0.27: 보완사항(다음 주에 고칠 것)은 오른쪽 「보완사항」 칸(예전 「다음 풀 전에 말할 것」)에 전부 (3개 제한 없음)
- 계산은 뒤에서 (원본 로그 좌표를 한 번 더 읽어서 트라이당 몇 초) · 도는 동안 「멈춘 거 아님」 표시
- 끝나면 보드를 다시 그림: 트라이 한눈에 = 결론 모듈 첫 실수 · 반복 실패 = 원인 묶음 · 보완사항 칸 = 보완사항 전부
- 지원 보스(똬리 · 울라텍)가 아니면 칸을 숨긴다. 계산 결과는 트라이 폴더 day_summary_v1.json 에 저장 (기존 파일은 안 지움)
"""
from __future__ import annotations

import threading
import time
import tkinter as tk
from pathlib import Path

from live import day_summary_v1 as ds

SEC_PER_TRY = 4.0      # 새로 계산할 때 트라이 하나 대략 (원본 로그 + 결론 모듈)
POLL_MS = 500


def _st(app) -> dict:
    st = getattr(app, "_day_board", None)
    if st is None:
        st = {"results": {}, "jobs": {}, "panel": None}
        app._day_board = st
    return st


def _row_dir(row) -> str:
    d = str((row or {}).get("storageDir") or "")
    if not d:
        mj = str((row or {}).get("metaJson") or "")
        d = str(Path(mj).parent) if mj else ""
    return str(Path(d)) if d else ""   # 윈도우 \ · / 섞여도 같은 키 (day_summary 는 str(Path) 로 저장)


def _key(enc, session_date, dirs) -> tuple:
    return (int(enc or 0), str(session_date or ""), tuple(dirs))


# ------------------------------------------------------------------ 칸 만들기
def build_panel(app, body, *, row=2) -> None:
    """보드 본문(grid) 맨 아래에 두 칸 짜리 패널을 붙인다."""
    from live import ui_theme as th
    st = _st(app)
    fb = getattr(th, "BODY_FAMILY_ACTIVE", None) or getattr(th, "BODY_FAMILY", "Segoe UI")
    ft = getattr(th, "TITLE_FAMILY_ACTIVE", None) or getattr(th, "TITLE_FAMILY", "Segoe UI")
    panel = th.RoundPanel(body, "최종 정리", "", title_font=(ft, 12), body_font=(fb, 9))
    panel.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=(th.px(8), 0))
    left = tk.Frame(panel.body, bg=th.PANEL_BG)   # 1.0.27: 수치만 (보완사항은 오른쪽 「보완사항」 칸)
    left.pack(fill="both", expand=True)
    st["panel"] = {"box": panel, "left": left, "sub": panel.subtitle_var, "fb": fb, "ft": ft, "th": th}
    panel.grid_remove()


def _fill(app, res) -> None:
    p = _st(app).get("panel")
    if not p:
        return
    th, fb = p["th"], p["fb"]
    for w in p["left"].winfo_children():
        w.destroy()
    for line in res.get("summary") or []:
        tk.Label(p["left"], text="· " + line, bg=th.PANEL_BG, fg=th.TEXT, font=(fb, 10), anchor="w", justify="left",
                 wraplength=th.px(1000)).pack(fill="x", anchor="w", pady=(0, th.px(3)))


def _waiting(app, text) -> None:
    p = _st(app).get("panel")
    if not p:
        return
    th, fb = p["th"], p["fb"]
    for w in p["left"].winfo_children():
        w.destroy()
    tk.Label(p["left"], text=text, bg=th.PANEL_BG, fg=th.ORANGE, font=(fb, 10), anchor="w", justify="left",
             wraplength=th.px(620)).pack(fill="x", anchor="w")


# ------------------------------------------------------------------ 계산 (뒤에서)
def _busy() -> bool:
    try:
        from live.raid_review import live_analysis_busy
        return bool(live_analysis_busy())
    except Exception:
        return False


def _start(app, key, enc, dirs) -> None:
    st = _st(app)
    if key in st["jobs"]:
        return
    todo_n = sum(1 for d in dirs if ds.needs_compute(enc, d))
    job = {"i": 0, "n": len(dirs), "new": todo_n, "t0": time.time(), "done": False, "res": None}
    st["jobs"][key] = job

    def work():
        def prog(i, n):
            job["i"] = i
            while _busy():          # 라이브 분석이 먼저
                time.sleep(1.0)
        try:
            job["res"] = ds.build_day(enc, dirs, progress=prog)
        except Exception as e:   # 실패해도 보드는 원래대로
            job["res"] = {"summary": [f"그날 정리 계산 실패: {e}"], "todo": [], "rows": {}, "repeats": [], "error": True}
        job["done"] = True

    threading.Thread(target=work, daemon=True, name="day-summary").start()
    _poll(app, key)


def _poll(app, key) -> None:
    st = _st(app)
    job = st["jobs"].get(key)
    if job is None:
        return
    if job["done"]:
        st["jobs"].pop(key, None)
        st["results"][key] = job["res"]
        # 보드 다시 그리기 (지금 보고 있는 날이 이 날이면)
        if st.get("shown") == key:
            app._b3798_board_pre_sig = None
            app._b3798_board_sig = None
            try:
                app._refresh_b3641_boss_views()
            except Exception:
                pass
        return
    if st.get("shown") == key:
        p = st.get("panel")
        i, n, new = job["i"], job["n"], job["new"]
        left = max(0, int((min(new, n - i)) * SEC_PER_TRY))
        msg = (f"계산 중 {i}/{n}트 · 원본 로그에서 위치·디버프를 다시 읽느라 오래 걸려요 (멈춘 거 아님)"
               + (f" · 남은 약 {left // 60}분 {left % 60}초" if left >= 60 else f" · 남은 약 {left}초" if left else "")
               + "\n한 번 계산하면 저장돼서 다음부터는 바로 나와요.")
        if p:
            p["sub"].set(f"계산 중 {i}/{n} · 멈춘 거 아님")
        _waiting(app, msg)
    try:
        app.after(POLL_MS, lambda: _poll(app, key))
    except Exception:
        pass


# ------------------------------------------------------------------ 보드에 반영
def apply(app, enc, pairs, session_date, model) -> dict:
    """보드 모델에 그날 정리를 덮어쓴다. 아직 없으면 계산을 시작하고 모델은 그대로 돌려준다."""
    st = _st(app)
    p = st.get("panel")
    try:
        enc = int(enc or 0)
    except Exception:
        enc = 0
    if enc not in ds.SUPPORTED:
        st["shown"] = None
        if p:
            p["box"].grid_remove()
        return model
    dirs = sorted({_row_dir(r) for r, _rev in pairs or [] if _row_dir(r)})
    if not dirs:
        if p:
            p["box"].grid_remove()
        return model
    key = _key(enc, session_date, dirs)
    st["shown"] = key
    if p:
        p["box"].grid()
        p["box"].title_var.set(f"최종 정리 · {session_date}")   # 1.0.27: 제목에서 「그날」 뺌
    res = st["results"].get(key)
    if res is None:
        _start(app, key, enc, dirs)
        return model
    if p:
        p["sub"].set(f"{res.get('tries', 0)}트 기준 · {ds.VERSION}")
        _fill(app, res)
    if res.get("error"):
        return model
    rows = res.get("rows") or {}
    by_try = {str(r.get("tryNo")): _row_dir(r) for r, _rev in pairs or []}
    model = dict(model)
    new_rows = []
    for item in model.get("rows") or []:
        item = dict(item)
        d = by_try.get(str(item.get("tryNo")))
        dr = rows.get(d) if d else None
        if dr and dr.get("issue") and str(item.get("result")) not in {"킬", "Kill"}:
            item["issue"] = dr["issue"]
            item["dayTag"] = dr.get("tag") or "candidate"
        new_rows.append(item)
    model["rows"] = new_rows
    if res.get("repeats"):
        model["repeats"] = [{"label": r["label"], "count": r["count"], "tries": r.get("tries") or []}
                            for r in res["repeats"] if int(r.get("count") or 0) >= 2] or model.get("repeats")
    model["voiceOrders"] = list(res.get("todo") or []) or ["이날 따로 고칠 것 없음"]   # 1.0.27: 보완사항 전부
    model["voiceOrdersFull"] = True
    return model


def signature(app) -> tuple:
    st = _st(app)
    return (ds.VERSION, len(st.get("results") or {}))
