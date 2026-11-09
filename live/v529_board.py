"""V5.2.9 · 엔진 기믹 규칙이 없는 보스(키식스 등)의 보스 보드 · 기믹 수행 체크 (화면 전용 · 판정/저장 데이터 불변).

- 보스 보드: 저장된 mechanic_review 가 없는 트라이도 결론 5줄(boss_guides, raid_facts 만 · raw 안 읽음)로 한 줄씩 보인다.
  트라이 진척 막대 · 트라이 한눈에(도달 구간 · 핵심 실패) · 반복 실패 · 다음 풀 전에 말할 것.
- 기믹 수행 체크: 결론 모듈이 준 checks 를 카드 칸에 채운다 (엔진 판정이 있는 보스는 건드리지 않음).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

VERSION = "V5.2.9-BOARD"
_CACHE: dict = {}
_BG = None


def _bg():
    global _BG
    if _BG is None:
        path = Path(__file__).resolve().parent / "boss_guides_v1.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("v529_boss_guides", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        else:  # BLUE: PyInstaller build has no .py next to this module
            from importlib import import_module
            mod = import_module("live.boss_guides_v1")
        _BG = mod
    return _BG


def _try_dir(row: dict):
    d = row.get("storageDir")
    if d:
        return Path(d)
    m = row.get("metaJson")
    return Path(m).parent if m else None


def guide_for(row: dict) -> dict | None:
    try:
        enc = int(row.get("encounterID") or 0)
    except (TypeError, ValueError):
        return None
    try:
        bg = _bg()
    except Exception:
        return None
    if enc not in bg.SPECS:
        return None
    d = _try_dir(row)
    if d is None or not d.is_dir():
        return None
    try:
        stamp = max((p.stat().st_mtime for p in (d / "meta.json", d / "raid_facts.json") if p.exists()), default=0)
    except OSError:
        stamp = 0
    key = (str(d), stamp, bg.VERSION)
    if key not in _CACHE:
        try:
            _CACHE[key] = bg.build_from_dir(d, use_raw=False)
        except Exception:
            _CACHE[key] = None
    return _CACHE[key]


def _excluded(row: dict) -> bool:
    return bool(row.get("analysisExcluded") or row.get("resetPull") or row.get("autoResetPull")
                or float(row.get("durationSec") or 0) < 30)


def add_guide_pairs(pairs, rows):
    """review 가 없는 트라이 → 결론 5줄로 만든 가벼운 review (bossKey=boss_guides)."""
    out = list(pairs or [])
    have = {str(r.get("storageDir") or r.get("metaJson") or id(r)) for r, _ in out}
    for row in rows or []:
        k = str(row.get("storageDir") or row.get("metaJson") or id(row))
        if k in have or _excluded(row):
            continue
        g = guide_for(row)
        if not g or not g.get("lines"):
            continue
        out.append((row, {
            "bossKey": "boss_guides",
            "encounter": {"encounterID": row.get("encounterID"), "result": row.get("result"),
                          "durationSec": row.get("durationSec"), "tryNo": row.get("tryNo")},
            "observations": {"collapseSnapshot": {"significantCluster": g.get("collapseSec") is not None,
                                                  "collapseSec": g.get("collapseSec")}},
            "_v529Guide": g,
        }))
        have.add(k)
    return out


def _stage_index(g: dict) -> int:
    chips = g.get("stages") or []
    if g.get("kill"):
        return 4
    if not chips:
        return 1
    i = next((n for n, c in enumerate(chips) if c.get("status") == "fail"), len(chips) - 1)
    return max(1, min(3, 1 + int(3 * i / max(1, len(chips)))))


def augment_board(model: dict) -> dict:
    rows = list(model.get("rows") or [])
    guided = [r for r in rows if (r.get("review") or {}).get("_v529Guide")]
    if not guided:
        return model
    repeat: dict = {}
    for r in guided:
        g = r["review"]["_v529Guide"]
        L = g.get("lines") or {}
        r["stageIndex"] = _stage_index(g)
        if g.get("kill"):
            r["progress"] = r["stage"] = "킬"
            r["keyFailure"] = r["issue"] = "킬 · " + str(L.get("전멸 원인") or "").split(" · ")[0]
            r["nextAction"] = str(L.get("다음 풀") or "")
            continue
        where = str(L.get("어디까지") or "").split(" · ")[0] or "전멸"
        r["progress"] = r["stage"] = where
        cause = str(L.get("전멸 원인") or "").strip()
        conf = (g.get("cause") or {}).get("confidence")
        txt = cause.replace(" [확정]", "").replace(" [추정]", "").replace(" [미확정]", "")
        if conf != "확정":
            txt += " · 추정 후보"
        r["keyFailure"] = r["issue"] = txt
        r["nextAction"] = str(L.get("다음 풀") or "")
        grp = str((g.get("cause") or {}).get("group") or "")
        if grp and grp not in ("원인 미확정", "리셋", "처치"):
            slot = repeat.setdefault(grp, {"count": 0, "tries": [], "action": ""})
            slot["count"] += 1
            slot["tries"].append(r.get("tryNo"))
            slot["action"] = slot["action"] or str(L.get("다음 풀") or "").split(" · 첫 실수")[0]
    model = dict(model, rows=rows)
    if repeat:
        reps = sorted(({"label": k, **v} for k, v in repeat.items()), key=lambda x: (-x["count"], str(x["label"])))
        model["repeats"] = list(model.get("repeats") or []) + reps
        orders = [x["action"] for x in reps if x.get("action")][:3]
        old = [x for x in (model.get("voiceOrders") or []) if not str(x).startswith("현재 반복되는 검증 실패 없음")]
        model["voiceOrders"] = (old + orders)[:3] or model.get("voiceOrders")
    counts = [0, 0, 0, 0, 0]
    for r in rows:
        counts[min(4, max(0, int(r.get("stageIndex") or 0)))] += 1
    model["stageCounts"] = counts
    return model


def fill_check_cards(app, checks) -> bool:
    """엔진 판정이 없는 트라이(「엔진 기믹 판정 없음」)만 · 카드 칸 순서대로 채우고 남는 칸은 숨긴다."""
    model = getattr(app, "_b3641_try_model", {}) or {}
    cards = getattr(app, "b3641_try_cards", {}) or {}
    if model.get("supported") or not checks or not cards:
        return False
    try:
        app.b3641_try_scope_var.set("현재 트라이 · 결론 5줄 기준 체크 (엔진 기믹 규칙 없는 보스 · 사망·동시 피격·중첩만)")
    except Exception:
        pass
    targets = list(cards.items())
    for i, (key, target) in enumerate(targets):
        if i < len(checks):
            app._set_b3641_card(dict(checks[i], key=key))
        else:
            try:
                target["frame"].grid_remove()
            except Exception:
                pass
    return True
