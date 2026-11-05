from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def fmt_time(seconds: Any) -> str:
    try:
        sec = max(0.0, float(seconds or 0.0))
    except (TypeError, ValueError):
        sec = 0.0
    minute = int(sec // 60)
    remain = sec - minute * 60
    if abs(remain - round(remain)) < 0.05:
        return f"{minute}:{int(round(remain)):02d}"
    return f"{minute}:{remain:04.1f}"


def _same_time(a: Any, b: Any, tolerance: float = 0.08) -> bool:
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return False


def load_json(path: str | Path | None) -> dict:
    if not path:
        return {}
    try:
        p = Path(path)
        if not p.exists():
            return {}
        value = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def resolve_row_path(row: dict, key: str) -> Path | None:
    value = (row or {}).get(key)
    if not value:
        return None
    p = Path(str(value))
    if p.is_absolute():
        return p
    storage = (row or {}).get("storageDir")
    if storage:
        return Path(storage) / p
    return p


def row_json(row: dict, key: str) -> dict:
    return load_json(resolve_row_path(row, key))


def build_try_layout_model(
    review: dict,
    selected_player: str | None = None,
    raid_facts: dict | None = None,
    raid_death_count: int | None = None,
) -> dict:
    """Build a read-only screen model from the current engine artifacts.

    B34.2.2 rule: the GUI never re-decides causal membership.  If B31 put a
    downstream event in ``collapseChain.downstreamEvents``, the screen renders it
    in that chain.  Causal changes belong in the engine + fixture together, not
    in layout-only exceptions.
    """
    review = review or {}
    raid_facts = raid_facts or {}
    enc = review.get("encounter") or {}
    presentation = review.get("presentationVerdict") or {}
    verdict = presentation or review.get("verdict") or {}
    collapse = review.get("collapseChain") or {}
    deaths = list(review.get("deathIncidents") or [])

    chain_events = []
    if collapse:
        root_time = collapse.get("rootTime")
        chain_events.append({
            "kind": "root",
            "t": root_time,
            "title": collapse.get("rootMechanic") or verdict.get("title") or "붕괴 시작",
            "detail": f"{int(collapse.get('rootVictimCount') or 0)}명 동시 사망" if collapse.get("rootVictimCount") else "",
        })
        chain_events.extend(dict(event) for event in (collapse.get("downstreamEvents") or []))

    selected = None
    short = str(selected_player or "").split("-")[0]
    if short:
        selected = next((x for x in deaths if str(x.get("player") or "").split("-")[0] == short), None)
    elif deaths:
        selected = deaths[0]

    raid_deaths = list(raid_facts.get("deaths") or [])
    selected_player_death = None
    if short:
        candidates = [
            x for x in raid_deaths
            if str(x.get("name") or x.get("player") or "").split("-")[0] == short
            and x.get("sec") is not None
        ]
        if candidates:
            # For the boss progress/PvA scope use the final observed death of the
            # selected character in this pull.  A personal causal incident is not
            # required merely to know when the player died.
            selected_player_death = max(candidates, key=lambda x: float(x.get("sec") or 0.0))

    cooldown_end = enc.get("durationSec")
    cooldown_reason = "fight_end"
    selected_death_sec = None
    if selected_player_death is not None:
        selected_death_sec = selected_player_death.get("sec")
    elif selected:
        pva = selected.get("pvaExpected") or {}
        selected_death_sec = pva.get("afterPlayerDeathFrom")
    if selected_death_sec is not None:
        cooldown_end = selected_death_sec
        cooldown_reason = "player_death"

    if raid_death_count is not None:
        try:
            death_count = int(raid_death_count)
        except (TypeError, ValueError):
            death_count = len(raid_deaths) if raid_deaths else len(deaths)
    else:
        death_count = len(raid_deaths) if raid_deaths else len(deaths)

    return {
        "encounter": enc,
        "verdict": verdict,
        "result": enc.get("result"),
        "tryNo": enc.get("tryNo"),
        "durationSec": enc.get("durationSec"),
        "deathCount": death_count,
        "confidence": verdict.get("confidence"),
        "rootMechanic": verdict.get("title") if presentation else ((collapse.get("rootMechanic") if collapse else None) or verdict.get("title")),
        "rootTime": verdict.get("sec") if presentation else ((collapse.get("rootTime") if collapse else None) or verdict.get("sec")),
        "chainEvents": chain_events,
        "sideEvents": [],
        "deathIncidents": deaths,
        "selectedDeath": selected,
        "selectedPlayerDeath": selected_player_death,
        "cooldownEndSec": cooldown_end,
        "cooldownReason": cooldown_reason,
    }

def build_series_layout_model(series: dict) -> dict:
    series = series or {}
    repeated = list(series.get("repeatedProblems") or [])
    repeated.sort(key=lambda x: (-int(x.get("primaryCount") or 0), -int(x.get("occurrenceTryCount") or 0), str(x.get("title") or "")))
    repeated_by_title = {str(x.get("title") or ""): x for x in repeated}
    primary_counts = {str(k): int(v or 0) for k, v in (series.get("primaryCauseCounts") or {}).items()}

    root_buckets = []
    for source in series.get("rootBuckets") or []:
        row = dict(source)
        mechanic = str(row.get("mechanic") or "")
        total = int(row.get("count") or 0)
        repeated_row = repeated_by_title.get(mechanic) or {}
        collapse = int(repeated_row.get("primaryCount") or primary_counts.get(mechanic, 0) or 0)
        collapse = max(0, min(total, collapse))
        row["collapseCount"] = collapse
        row["personalCount"] = max(0, total - collapse)
        root_buckets.append(row)
    root_buckets.sort(key=lambda x: (-int(x.get("count") or 0), str(x.get("mechanic") or "")))
    return {
        "tries": int(series.get("tries") or 0),
        "bestDurationSec": float(series.get("bestDurationSec") or 0.0),
        "rootBuckets": root_buckets,
        "repeatedProblems": repeated,
        "reviews": list(series.get("reviews") or []),
    }


def render_try_summary(model: dict, selected_player: str | None = None) -> str:
    if not model:
        return ""
    enc = model.get("encounter") or {}
    verdict = model.get("verdict") or {}
    lines = []
    result = str(enc.get("result") or "-")
    lines.append(f"Try {enc.get('tryNo','?')} · {result} {fmt_time(enc.get('durationSec'))}")
    root = model.get("rootMechanic")
    if root:
        conf = str(model.get("confidence") or "-").upper()
        rt = model.get("rootTime")
        prefix = f"{fmt_time(rt)} · " if rt is not None else ""
        lines.append(f"[{conf}] {prefix}{root}")
    if verdict.get("interpretation"):
        lines.append(str(verdict.get("interpretation")))
    if model.get("chainEvents"):
        lines.append("")
        lines.append("붕괴 체인")
        for event in model.get("chainEvents") or []:
            kind = event.get("kind")
            t = event.get("t")
            if kind == "root":
                detail = f" · {event.get('detail')}" if event.get("detail") else ""
                lines.append(f"- {fmt_time(t)} {event.get('title')}{detail}")
            elif event.get("type") == "under_share":
                deaths = ", ".join(event.get("deaths") or [])
                lines.append(f"- {fmt_time(t)} {event.get('mechanic')} · 분담 {event.get('shareCount','?')}명 · {deaths}")
            elif event.get("type") == "WIPE":
                lines.append(f"- {fmt_time(t)} 전멸 확정")
            else:
                who = f"{event.get('player')} · " if event.get("player") else ""
                lines.append(f"- {fmt_time(t)} {who}{event.get('mechanic') or event.get('type') or '?'}")
    if model.get("sideEvents"):
        lines.append("")
        lines.append("같은 구간 · 연결 근거 없음 (체인에 넣지 않음)")
        for event in model.get("sideEvents") or []:
            who = f"{event.get('player')} · " if event.get("player") else ""
            lines.append(f"- {fmt_time(event.get('t'))} {who}{event.get('mechanic') or event.get('type') or '?'}")
    if model.get("selectedDeath"):
        death = model["selectedDeath"]
        who = death.get("player") or selected_player or "개인"
        lines.append("")
        lines.append(f"개인 · {who}")
        if death.get("sentence"):
            lines.append(str(death.get("sentence")))
    if model.get("cooldownEndSec") is not None:
        reason = "개인 사망까지" if model.get("cooldownReason") == "player_death" else "전투 종료까지"
        lines.append("")
        lines.append(f"쿨기 비교 범위: 0:00~{fmt_time(model.get('cooldownEndSec'))} · {reason}")
    return "\n".join(lines).rstrip() + "\n"
