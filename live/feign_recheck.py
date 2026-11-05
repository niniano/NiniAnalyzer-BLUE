"""RC4.2: opt-in Feign Death recheck for already saved Tries.

Two strictly separated steps:

1. ``scan_feign_candidates`` (READ-ONLY): find saved Tries whose original raw
   log contains a player Feign Death (5384), check whether the raw log is
   usable, and preview what the current engine would count.  It writes nothing
   (not even the shared spell-name cache).
2. ``recheck_selected``: only for Tries the user selected.  The raw log is
   verified first, the boss series results and the Try results are zipped,
   and only then the normal reanalysis pipeline runs.  A failed check or backup
   means that Try is not reanalysed.

Reporting keeps two layers apart:
* ``facts``  - what the log proves (deaths excluded by Feign Death evidence,
  candidates kept for lack of evidence).
* ``judgement`` - engine outputs (death count, first death, wipe cause,
  first attacker) shown before -> after.  These are re-judged, not confirmed.
"""
from __future__ import annotations

import gzip
import json
from zlib import error as zlib_error
from pathlib import Path
from typing import Callable, Iterable

from .rawlog import open_raw_text, resolve_raw, is_gz

FEIGN_DEATH_SPELL_ID = 5384
_FD_TOKEN = f",{FEIGN_DEATH_SPELL_ID},"

JUDGEMENT_FIELDS = (
    ("deathCount", "사망 수"),
    ("firstDeath", "첫 사망자"),
    ("shownCause", "화면 표시 원인"),
    ("cause", "전멸 원인(B31)"),
    ("mechanic", "기믹 판정"),
    ("firstAttacker", "첫 공격자"),
)


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _result_file(folder: Path, meta: dict, key: str, default: str) -> Path:
    value = meta.get(key)
    if value:
        p = Path(str(value))
        if not p.is_absolute():
            p = folder / p
        if p.exists():
            return p
    return folder / default


def _short(name) -> str:
    return str(name or "?").split("-")[0]


def _fmt_sec(sec) -> str:
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return "?"
    m, s = divmod(max(0.0, sec), 60.0)
    return f"{int(m)}:{s:04.1f}"


def _pull_label(pull: dict | None) -> str | None:
    pull = pull or {}
    confirmed = bool(pull.get("pullerConfirmed"))
    who = (pull.get("puller") if confirmed else None) or pull.get("firstObservedAttack") or {}
    if not who:
        return None
    if not confirmed and pull.get("ambiguity") == "near-simultaneous-prestart-attacks":
        return "판단 보류(동시 공격)"
    return f"{'풀러' if confirmed else '첫 공격'} {_short(who.get('playerName'))}"


def try_summary(storage_dir) -> dict:
    """Engine outputs currently stored for one Try (read-only)."""
    folder = Path(storage_dir)
    meta = _load_json(folder / "meta.json")
    facts = _load_json(_result_file(folder, meta, "raidFactsJson", "raid_facts.json"))
    review = _load_json(_result_file(folder, meta, "raidReviewJson", "raid_review.json"))
    deaths = sorted(
        [d for d in (facts.get("deaths") or []) if isinstance(d, dict)],
        key=lambda d: float(d.get("sec") or 0.0),
    )
    first = deaths[0] if deaths else None
    verdict = review.get("verdict") or {}
    presentation = review.get("presentationVerdict") or {}
    mech = meta.get("mechanicCollapseDirect") or {}
    return {
        "hasResults": bool(facts),
        "deathCount": len(deaths) if facts else meta.get("deathCount"),
        "deaths": [(_short(d.get("name")), d.get("sec")) for d in deaths],
        "firstDeath": (f"{_short(first.get('name'))} {_fmt_sec(first.get('sec'))}" if first else None),
        "shownCause": presentation.get("title") or verdict.get("title"),
        "cause": verdict.get("title"),
        "causeConfidence": verdict.get("confidence"),
        "mechanic": (mech.get("title") if isinstance(mech, dict) else None) or meta.get("mechanicVerdict"),
        "firstAttacker": _pull_label(facts.get("pullStart") or meta.get("pullStart")),
    }


def judgement_changes(before: dict, after: dict) -> list[dict]:
    out = []
    for key, label in JUDGEMENT_FIELDS:
        b, a = (before or {}).get(key), (after or {}).get(key)
        if b != a:
            out.append({"field": key, "label": label, "before": b, "after": a})
    return out


def count_feign_casts(raw_path) -> int:
    """Number of player Feign Death uses in a raw log (cheap text scan)."""
    casts = auras = 0
    with open_raw_text(raw_path) as fh:
        for line in fh:
            if _FD_TOKEN not in line or "Player-" not in line:
                continue
            body = line.split("  ", 1)[-1]
            parts = body.split(",", 11)
            if len(parts) < 11 or parts[9].strip() != str(FEIGN_DEATH_SPELL_ID):
                continue
            if parts[0] == "SPELL_CAST_SUCCESS" and parts[1].startswith("Player-"):
                casts += 1
            elif parts[0] == "SPELL_AURA_APPLIED" and parts[5].startswith("Player-"):
                auras += 1
    return casts or auras


def check_raw(storage_dir) -> tuple[bool, str]:
    """Is the original raw log present and readable from first to last byte?

    Always reads the WHOLE file (plain or gzip) in binary, decodes every line as
    strict UTF-8 and rejects NUL bytes, so damage after the encounter start is
    caught too.  gzip CRC/length errors surface as exceptions.  Called again
    immediately before every reanalysis, not only during the search.
    """
    raw = resolve_raw(Path(storage_dir))
    if not raw.exists():
        return False, "원본 로그(raw.log) 없음"
    has_start = False
    lineno = 0
    try:
        if raw.stat().st_size <= 0:
            return False, "원본 로그가 비어 있음"
        opener = (lambda: gzip.open(raw, "rb")) if is_gz(raw) else (lambda: open(raw, "rb"))
        with opener() as fh:
            for lineno, line in enumerate(fh, 1):
                if b"\x00" in line:
                    return False, f"원본 로그 손상: {lineno}번째 줄에 NUL 바이트"
                try:
                    text = line.decode("utf-8")
                except UnicodeDecodeError:
                    return False, f"원본 로그 손상: {lineno}번째 줄 UTF-8 오류"
                if not has_start and "ENCOUNTER_START" in text:
                    has_start = True
    except (OSError, EOFError, gzip.BadGzipFile, zlib_error) as exc:
        return False, f"원본 로그 손상: {exc}"
    if not has_start:
        return False, "원본 로그에 전투 시작 기록 없음"
    return True, f"원본 확인 · {lineno:,}줄 전체 읽기"


def _row_labels(row: dict, folder: Path) -> dict:
    return {
        "storageDir": str(folder),
        "date": folder.parent.parent.name if folder.parent.parent.name else "",
        "boss": str(row.get("name") or "?"),
        "tryNo": row.get("tryNo"),
        "result": str(row.get("result") or ""),
    }


def scan_feign_candidates(rows: Iterable[dict], progress: Callable | None = None,
                          cancel=None, preview: bool = True) -> list[dict]:
    """READ-ONLY search.  Returns only Tries that contain Feign Death, plus
    Tries whose raw log cannot be searched (so the user sees why)."""
    from .raid_facts import extract_raid_facts

    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("storageDir")]
    out: list[dict] = []
    for idx, row in enumerate(rows, 1):
        if cancel is not None and cancel.is_set():
            break
        folder = Path(str(row.get("storageDir")))
        if progress:
            progress(idx, len(rows), f"{row.get('name', '?')} Try {row.get('tryNo', '?')}")
        if not folder.is_dir():
            continue
        item = _row_labels(row, folder)
        raw = resolve_raw(folder)
        if not raw.exists():
            item.update(status="no-raw", selectable=False, note="원본 로그 없음 · 검색/재검사 불가")
            out.append(item)
            continue
        try:
            fd = count_feign_casts(raw)
        except Exception as exc:
            item.update(status="unreadable", selectable=False, note=f"원본 로그 읽기 실패 · {exc}")
            out.append(item)
            continue
        if fd <= 0:
            continue
        current = try_summary(folder)
        item.update(feignCasts=fd, currentDeathCount=current.get("deathCount"),
                    hasResults=current.get("hasResults"))
        ok, why = check_raw(folder)
        if not ok:
            item.update(status="raw-incomplete", selectable=False, note=f"{why} · 재검사 불가")
            out.append(item)
            continue
        item.update(status="ready", selectable=True)
        if preview:
            meta = _load_json(folder / "meta.json")
            try:
                facts = extract_raid_facts(raw, meta, persist_display_names=False)
                predicted = len(facts.get("deaths") or [])
                item.update(
                    predictedDeathCount=predicted,
                    predictedIgnored=list(facts.get("feignDeathsIgnored") or []),
                    predictedKept=list(facts.get("feignDeathsKeptUncertain") or []),
                )
                cur = current.get("deathCount")
                if cur is None:
                    item["note"] = "기존 결과 없음 · 재검사 가능"
                elif int(cur) != predicted:
                    item["note"] = f"사망 수 변화 예상 {cur}→{predicted}"
                else:
                    item["note"] = "사망 수 변화 없음 예상"
            except Exception as exc:
                item.update(predictedDeathCount=None, note=f"미리보기 실패 · 재검사는 가능 ({exc})")
        else:
            item["note"] = "재검사 가능"
        out.append(item)
    return out


def recheck_selected(rows: Iterable[dict], storage_root, raid_only: bool = True,
                     progress: Callable | None = None, cancel=None,
                     rebuild: Callable | None = None) -> list[dict]:
    """Back up, then reanalyse ONLY the given (user-selected) Tries."""
    from .reanalysis import backup_try_results, rebuild_saved_row
    rebuild = rebuild or rebuild_saved_row
    storage_root = Path(storage_root)
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("storageDir")]
    series_backed: dict[str, str | None] = {}
    results = []
    for idx, row in enumerate(rows, 1):
        if cancel is not None and cancel.is_set():
            break
        folder = Path(str(row.get("storageDir")))
        res = _row_labels(row, folder)
        if progress:
            progress(idx, len(rows), f"{res['boss']} Try {res['tryNo']}")
        ok, why = check_raw(folder)
        if not ok:
            res.update(status="skipped", reason=f"{why} · 재분석 안 함")
            results.append(res)
            continue
        meta = _load_json(folder / "meta.json")
        if not meta:
            res.update(status="skipped", reason="meta.json 읽기 실패 · 재분석 안 함")
            results.append(res)
            continue
        before = try_summary(folder)
        # The boss series (repeated-problem summary) is rewritten by the same
        # pipeline, so it is backed up once per boss/difficulty first.
        series_dir = storage_root / "_series" / f"{int(meta.get('encounterID') or 0)}_d{int(meta.get('difficultyID') or 0)}"
        key = str(series_dir)
        try:
            if key not in series_backed:
                z = backup_try_results(series_dir) if series_dir.is_dir() else None
                series_backed[key] = z.name if z else None
        except Exception as exc:
            res.update(status="skipped", reason=f"반복 문제 집계 백업 실패 · 재분석 안 함 ({exc})")
            results.append(res)
            continue
        try:
            fresh = rebuild(row, storage_root, raid_only=raid_only)
        except Exception as exc:
            # rebuild_saved_row backs up before touching anything; any failure
            # up to that point leaves the old results untouched.
            res.update(status="error", reason=f"재분석 실패 · {exc}")
            results.append(res)
            continue
        after = try_summary(folder)
        facts = _load_json(_result_file(folder, _load_json(folder / "meta.json"), "raidFactsJson", "raid_facts.json"))
        res.update(
            status="done",
            facts={
                "feignIgnored": list(facts.get("feignDeathsIgnored") or []),
                "feignKept": list(facts.get("feignDeathsKeptUncertain") or []),
            },
            before=before,
            after=after,
            changes=judgement_changes(before, after),
            backup=(fresh or {}).get("lastReanalysisBackup"),
            seriesBackup=series_backed.get(key),
            row=fresh,
        )
        results.append(res)
    return results


_EVENT_KO = {
    "SPELL_CAST_SUCCESS": "스킬 시전",
    "SPELL_CAST_START": "시전 시작",
    "SWING_DAMAGE": "근접 공격",
    "SWING_MISSED": "근접 공격",
    "RANGE_DAMAGE": "자동 사격",
    "RANGE_MISSED": "자동 사격",
}


def render_result(res: dict) -> str:
    """Plain-text report; facts and engine judgement are separate sections."""
    head = f"[{res.get('date', '')}] {res.get('boss', '?')} Try {res.get('tryNo', '?')}"
    if res.get("status") != "done":
        return f"{head}\n  ✕ {res.get('reason', '처리 안 됨')}\n"
    lines = [head]
    facts = res.get("facts") or {}
    lines.append("  ■ 로그 사실 (죽은 척하기)")
    ignored = facts.get("feignIgnored") or []
    kept = facts.get("feignKept") or []
    if ignored:
        for f in ignored:
            ev = _EVENT_KO.get(str(f.get("evidenceEvent") or ""), f.get("evidenceEvent") or "행동")
            spell = f.get("evidenceSpellName") or (f"스킬 {f['evidenceSpellID']}" if f.get("evidenceSpellID") else "")
            lines.append(
                f"    - {_short(f.get('name'))}: {_fmt_sec(f.get('sec'))} 사망 기록 → 사망에서 제외 "
                f"(근거: {_fmt_sec(f.get('evidenceSec'))} {ev}{' ' + spell if spell else ''}, 그 사이 부활 없음)"
            )
    if kept:
        for f in kept:
            lines.append(f"    - {_short(f.get('name'))}: {_fmt_sec(f.get('sec'))} 죽척 직후 사망 → 다시 행동한 근거가 없어 사망 유지")
    if not ignored and not kept:
        lines.append("    - 죽척 직후 사망 기록 없음 (죽척은 있었지만 사망 판정과 무관)")
    lines.append("  ■ 엔진 재판정 (확정 아님 · 원본 로그로 확인 필요)")
    changes = res.get("changes") or []
    if not changes:
        lines.append("    - 변경 없음")
    for c in changes:
        lines.append(f"    - {c['label']}: {c['before'] if c['before'] is not None else '없음'} → {c['after'] if c['after'] is not None else '없음'}")
    if res.get("backup"):
        lines.append(f"  백업 · 트라이 결과: {res['backup']}")
    if res.get("seriesBackup"):
        lines.append(f"  백업 · 반복 문제 집계: _series/{res['seriesBackup']}")
    lines.append("  원본 로그(raw.log.gz)는 변경하지 않았습니다.")
    return "\n".join(lines) + "\n"
