from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Iterable

from .encounter_tracker import parse_combat_datetime
from .difficulty_scope import check_difficulty_ids, difficulty_matches, is_mythic_only

COILED_ALTAR_ENCOUNTER_ID = 3429
MYTHIC_DIFFICULTIES = {5, 16, 233}


def normalize_difficulty(value: Any):
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return value
    return 16 if iv in MYTHIC_DIFFICULTIES else iv


_EPOCH_CACHE: dict[str, float | None] = {}


def _row_start_epoch(row: dict | None):
    text = str((row or {}).get("startTimestamp") or "").strip()
    if not text:
        return None
    # B38 SPEED1: parsed once per distinct timestamp text (was ~1M parses/start).
    if text in _EPOCH_CACHE:
        return _EPOCH_CACHE[text]
    dt = parse_combat_datetime(text)
    value = dt.timestamp() if dt is not None else None
    if len(_EPOCH_CACHE) > 50000:
        _EPOCH_CACHE.clear()
    _EPOCH_CACHE[text] = value
    return value


def _row_source_kind(row: dict | None) -> str:
    row = row or {}
    for value in (
        row.get("sourceKind"),
        ((row.get("eventCoverage") or {}).get("sourceKind") if isinstance(row.get("eventCoverage"), dict) else None),
        ((row.get("mechanicReview") or {}).get("sourceKind") if isinstance(row.get("mechanicReview"), dict) else None),
    ):
        if value:
            return str(value).strip().lower()
    return ""


def _row_preference_score(row: dict | None) -> tuple[int, int, float, int, int, int]:
    """Choose the best representative when two rows are the same physical pull.

    B37.4.8: a zero-length reset/fake marker can share the same encounter start
    timestamp as the later completed capture.  B37.4.7 preferred local/raw
    coordinates first, so the marker could win and then be hidden from ordinary
    navigation, taking the real Wipe/Kill with it.  Prefer a real completed
    capture first; only then use local/raw richness as the tiebreaker.
    """
    row = row or {}
    source = _row_source_kind(row)
    try:
        duration = float(row.get("durationSec")) if row.get("durationSec") is not None else -1.0
    except (TypeError, ValueError):
        duration = -1.0
    excluded = bool(row.get("resetPull")) or str(row.get("analysisExcluded") or "").lower() == "fake_pull"
    if not excluded and str(row.get("result") or "").upper() != "KILL" and 0.0 <= duration < 1.0:
        excluded = True
    normal = int(not excluded)
    kill = int(str(row.get("result") or "").upper() == "KILL")
    local = int(source in {"raw", "local_raw", "local"} or bool(row.get("rawLog")))
    coords = int(bool(row.get("rawLog")) or bool(row.get("localAdvancedCombatLog")))
    artifacts = sum(1 for k in ("mechanicReviewJson", "raidReviewJson", "raidFactsJson", "factsJson") if row.get(k))
    return (normal, kill, max(duration, -1.0), local, coords, artifacts)


def same_physical_pull(left: dict | None, right: dict | None, *, tolerance_sec: float = 1.5) -> bool:
    """Return True when WCL/raw rows describe the same encounter pull.

    Mythic difficulty 5 (WCL), 16 (local CombatLog), and 233 (Flexible
    Mythic Nymrissa) are normalized before comparison.  Start time is the primary identity so source-specific Try
    numbers cannot double-count one physical pull.
    """
    left, right = left or {}, right or {}
    try:
        if int(left.get("encounterID") or -1) != int(right.get("encounterID") or -1):
            return False
    except Exception:
        return False
    if normalize_difficulty(left.get("difficultyID")) != normalize_difficulty(right.get("difficultyID")):
        return False
    le, re = _row_start_epoch(left), _row_start_epoch(right)
    if le is not None and re is not None:
        return abs(le - re) <= float(tolerance_sec)
    lt = str(left.get("startTimestamp") or "").strip()
    rt = str(right.get("startTimestamp") or "").strip()
    if lt and rt:
        return lt == rt
    # Legacy fallback only when start timestamps are unavailable.
    return (
        str(left.get("sessionDate") or "") == str(right.get("sessionDate") or "")
        and str(left.get("tryNo") or "") == str(right.get("tryNo") or "")
        and abs(_safe_float(left.get("durationSec"), -9999) - _safe_float(right.get("durationSec"), 9999)) <= 0.25
    )


def _canonical_group_key(row: dict):
    """Rows can only be the same pull with equal encounter + normalized difficulty
    (exactly the first two checks of ``same_physical_pull``)."""
    try:
        enc = int(row.get("encounterID") or -1)
    except Exception:
        return None  # same_physical_pull() returns False for these: never matches
    diff = normalize_difficulty(row.get("difficultyID"))
    try:
        hash(diff)
    except TypeError:
        diff = repr(diff)
    return (enc, diff)


def canonicalize_rows(rows: Iterable[dict]) -> list[dict]:
    """Deduplicate WCL/local copies and retain one canonical representative.

    B38 SPEED1: same result as the original first-match scan over all selected
    rows, but only rows of the same encounter/difficulty are compared (others
    can never match), so the cost no longer grows with the whole archive.
    """
    selected: list[dict] = []
    # B38 SPEED2: per encounter/difficulty group, selected rows are indexed by
    # start-time bucket (2 s wide >= 1.5 s tolerance).  Rows without a parsable
    # start time are always candidates (string / legacy fallback).  Candidates
    # are then checked with same_physical_pull in original index order, so the
    # first match is exactly the same as scanning every selected row.
    groups: dict = {}
    epoch_of: dict[int, float | None] = {}

    def _bucket_add(g, i, e):
        if e is None:
            g["none"].append(i)
        else:
            g["b"].setdefault(int(e // 2.0), []).append(i)

    def _bucket_remove(g, i, e):
        lst = g["none"] if e is None else g["b"].get(int(e // 2.0), [])
        if i in lst:
            lst.remove(i)

    for raw in rows:
        row = dict(raw or {})
        key = _canonical_group_key(row)
        e = _row_start_epoch(row)
        g = groups.setdefault(key, {"b": {}, "none": []}) if key is not None else None
        if g is None:
            candidates = []
        elif e is None:
            candidates = sorted(i for lst in list(g["b"].values()) + [g["none"]] for i in lst)
        else:
            k = int(e // 2.0)
            candidates = sorted(g["none"] + g["b"].get(k - 1, []) + g["b"].get(k, []) + g["b"].get(k + 1, []))
        match = next((i for i in candidates if same_physical_pull(selected[i], row)), None)
        if match is None:
            row.setdefault("canonicalDuplicateCount", 1)
            selected.append(row)
            idx = len(selected) - 1
            epoch_of[idx] = e
            if g is not None:
                _bucket_add(g, idx, e)
            continue
        old = selected[match]
        count = int(old.get("canonicalDuplicateCount") or 1) + 1
        winner = row if _row_preference_score(row) > _row_preference_score(old) else old
        winner = dict(winner)
        winner["canonicalDuplicateCount"] = count
        sources = set(old.get("canonicalSources") or []) | set(row.get("canonicalSources") or [])
        for source_row in (old, row):
            src = _row_source_kind(source_row)
            if src:
                sources.add(src)
            elif source_row.get("rawLog"):
                sources.add("local_raw")
        if sources:
            winner["canonicalSources"] = sorted(sources)
        selected[match] = winner
        new_e = _row_start_epoch(winner)
        if g is not None and new_e != epoch_of.get(match):
            _bucket_remove(g, match, epoch_of.get(match))
            _bucket_add(g, match, new_e)
        epoch_of[match] = new_e
    selected.sort(key=lambda r: (_row_start_epoch(r) or 0.0, _safe_int(r.get("tryNo"))))
    return selected


def canonicalize_review_pairs(items: Iterable[tuple[dict, dict]]) -> list[tuple[dict, dict]]:
    selected: list[tuple[dict, dict]] = []
    for row, review in items:
        match = next((i for i, (old, _rev) in enumerate(selected) if same_physical_pull(old, row)), None)
        if match is None:
            selected.append((dict(row or {}), review))
            continue
        old_row, old_review = selected[match]
        if _row_preference_score(row) > _row_preference_score(old_row):
            selected[match] = (dict(row or {}), review)
        else:
            selected[match] = (old_row, old_review)
    selected.sort(key=lambda x: (_row_start_epoch(x[0]) or 0.0, _safe_int(x[0].get("tryNo"))))
    return selected


def _safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def fmt_clock(sec: Any) -> str:
    value = _safe_float(sec)
    if value is None:
        return "-"
    value = max(0.0, value)
    m = int(value // 60)
    s = value - (m * 60)
    return f"{m}:{s:04.1f}" if abs(s - round(s)) > 0.05 else f"{m}:{int(round(s)):02d}"


def short_player(name: Any) -> str:
    text = str(name or "?")
    return text.split("-")[0]


# V4.1 SAFE: metadata-validated, size-limited cache for persisted mechanic reviews.
# Cache only parsed file snapshots; callers continue to receive independent dicts.
from collections import OrderedDict as _ReviewOrderedDict
from copy import deepcopy as _review_deepcopy

_REVIEW_CACHE_LIMIT = 128
_REVIEW_CACHE = _ReviewOrderedDict()


def _review_cache_fingerprint(st):
    return (st.st_mtime_ns, getattr(st, "st_ctime_ns", 0),
            st.st_size, st.st_dev, st.st_ino)


def _load_review_file_cached(path: Path) -> dict | None:
    # No negative caching: deleted, created, and reanalyzed files are rechecked.
    key = str(path.absolute())
    try:
        before = _review_cache_fingerprint(path.stat())
        cached = _REVIEW_CACHE.get(key)
        if cached is not None and cached[0] == before:
            _REVIEW_CACHE.move_to_end(key)
            return _review_deepcopy(cached[1])
        # Parse the file as before, with the same UTF-8 replacement policy.
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(data, dict):
            _REVIEW_CACHE.pop(key, None)
            return None
        try:
            after = _review_cache_fingerprint(path.stat())
        except OSError:
            after = None
        if before == after:
            _REVIEW_CACHE[key] = (after, data)
            _REVIEW_CACHE.move_to_end(key)
            while len(_REVIEW_CACHE) > _REVIEW_CACHE_LIMIT:
                _REVIEW_CACHE.popitem(last=False)
        else:
            # Concurrent updates are returned once, never saved as a stale cache.
            _REVIEW_CACHE.pop(key, None)
        return _review_deepcopy(data)
    except Exception:
        _REVIEW_CACHE.pop(key, None)
        return None


def load_mechanic_review(row: dict | None) -> dict:
    row = row or {}
    candidates: list[Path] = []
    if row.get("mechanicReviewJson"):
        candidates.append(Path(str(row["mechanicReviewJson"])))
    if row.get("storageDir"):
        candidates.append(Path(str(row["storageDir"])) / "mechanic_review.json")
    for path in candidates:
        data = _load_review_file_cached(path)
        if isinstance(data, dict):
            return data
    inline = row.get("mechanicReview")
    return inline if isinstance(inline, dict) else {}



def is_coiled_review(review: dict | None) -> bool:
    review = review or {}
    try:
        return int(((review.get("encounter") or {}).get("encounterID")) or 0) == COILED_ALTAR_ENCOUNTER_ID
    except Exception:
        return str(review.get("bossKey") or "") == "coiled_altar"


def _checks(review: dict) -> dict[str, dict]:
    return {str(x.get("key")): x for x in (review.get("checks") or []) if isinstance(x, dict)}


def _cycles(review: dict) -> list[dict]:
    return list((((review.get("observations") or {}).get("phase2") or {}).get("cycles") or []))


def _p2(review: dict) -> dict:
    return ((review.get("observations") or {}).get("phase2") or {})


def _p2_failures(review: dict) -> list[dict]:
    out: list[dict] = []
    for cycle in _cycles(review):
        judgment = cycle.get("judgment") or {}
        for failure in judgment.get("failures") or []:
            if not isinstance(failure, dict):
                continue
            item = dict(failure)
            item.setdefault("cycleIndex", cycle.get("index"))
            out.append(item)
    out.sort(key=lambda x: (_safe_float(x.get("sec"), 10**9), str(x.get("kind") or "")))
    return out


def _fallback_first_mistake(review: dict) -> dict | None:
    failures = _p2_failures(review)
    if failures:
        return failures[0]
    # Older B36.3 samples do not have cycle.judgment yet. Rebuild only from
    # signals that B36.4 already promoted as verified failures.
    fallback: list[dict] = []
    for cycle in _cycles(review):
        idx = _safe_int(cycle.get("index"))
        gb = cycle.get("gloombomb") or {}
        extra = _safe_int(gb.get("extraHitCount"))
        retained = _safe_int(gb.get("soulcoilerVulnerabilityRetainedCount"))
        if extra or retained:
            sec = gb.get("damageSec") if gb.get("damageSec") is not None else gb.get("applySec")
            fallback.append({
                "kind": "gloombomb_failure", "cycleIndex": idx, "sec": sec,
                "label": "암영폭탄 처리 실패",
                "summary": " · ".join([
                    *( [f"같이 맞음 {extra}명"] if extra else [] ),
                    *( [f"영혼살무사 뎀감 유지 {retained}마리"] if retained else [] ),
                ])
            })
        wail = cycle.get("wail") or {}
        for ep in wail.get("failureEpisodes") or []:
            fallback.append({
                "kind": "wail_interrupt_failure", "cycleIndex": idx, "sec": ep.get("sec"),
                "label": "공포의 통곡 미차단",
                "summary": f"플레이어 {_safe_int(ep.get('hitCount'))}명 공포",
            })
        collision = cycle.get("manifestCollision") or {}
        for ep in collision.get("episodes") or []:
            fallback.append({
                "kind": "manifest_collision", "cycleIndex": idx, "sec": ep.get("sec"),
                "label": "현신 접촉",
                "summary": f"직후 사망 {_safe_int(ep.get('linkedDeathCount'))}명",
                "linkedDeathCount": _safe_int(ep.get("linkedDeathCount")),
            })
        ss = cycle.get("soulSever") or {}
        if _safe_int(ss.get("nonTankHitCount")) > 0:
            fallback.append({
                "kind": "soul_sever_non_tank_hit", "cycleIndex": idx, "sec": ss.get("castSec"),
                "label": "Soul Sever 비탱커 피격",
                "summary": ", ".join(short_player(x) for x in (ss.get("nonTankHitPlayers") or [])),
            })
        overlap = cycle.get("priorAddOverlap") or {}
        if overlap.get("detected"):
            secs = [_safe_float(x) for x in (overlap.get("wailSecs") or [])]
            secs = [x for x in secs if x is not None]
            fallback.append({
                "kind": "prior_add_overlap", "cycleIndex": idx, "sec": min(secs) if secs else cycle.get("nextCycleSec"),
                "label": "이전 차단쫄 다음 웨이브 중첩",
                "summary": "이전 Soulcoiler가 다음 웨이브 이후에도 생존",
            })
    fallback.sort(key=lambda x: (_safe_float(x.get("sec"), 10**9), str(x.get("kind") or "")))
    return fallback[0] if fallback else None


def _cycle_by_index(review: dict, cycle_index: Any) -> dict:
    idx = _safe_int(cycle_index, 0)
    if idx:
        for cycle in _cycles(review):
            if _safe_int(cycle.get("index"), 0) == idx:
                return cycle
    return _first_cycle(review)


def _major_incident(review: dict, cycle_index: Any = None) -> dict | None:
    """Pick the largest verified incident inside the UI focus cycle.

    The approved B36.4.1 Try view is a cycle-level explanation.  Aggregate P2
    totals remain available in the engine artifact, but mixing cycle 2 contact
    counts into cycle 1 cards made the top line internally inconsistent.
    """
    cycle = _cycle_by_index(review, cycle_index)
    collision = cycle.get("manifestCollision") or {}
    episodes = list(collision.get("episodes") or [])
    collision_count = _safe_int(collision.get("episodeCount"), len(episodes))
    linked_deaths = _safe_int(collision.get("linkedDeathCount"))
    if collision_count:
        secs = []
        players = []
        for ep in episodes:
            if ep.get("sec") is not None:
                secs.append(float(ep["sec"]))
            players.extend(ep.get("players") or [])
        return {
            "kind": "manifest_collision",
            "cycleIndex": cycle.get("index"),
            "sec": min(secs) if secs else None,
            "label": "현신 접촉",
            "summary": f"현신 접촉 {collision_count}회" + (f" → {linked_deaths}명 사망" if linked_deaths else ""),
            "linkedDeathCount": linked_deaths,
            "players": list(dict.fromkeys(players)),
        }

    failures = [
        x for x in _p2_failures(review)
        if _safe_int(x.get("cycleIndex"), 0) == _safe_int(cycle.get("index"), 0)
    ]
    if failures:
        def score(item: dict):
            kind = item.get("kind")
            if kind == "wail_interrupt_failure":
                return (300 + len(item.get("players") or []), -_safe_float(item.get("sec"), 0))
            if kind == "soul_sever_non_tank_hit":
                return (200 + len(item.get("players") or []), -_safe_float(item.get("sec"), 0))
            if kind == "gloombomb_failure":
                return (100, -_safe_float(item.get("sec"), 0))
            return (50, -_safe_float(item.get("sec"), 0))
        return max(failures, key=score)
    return None

def _collapse_direct(review: dict) -> tuple[dict | None, dict]:
    # B37.4.13: the user-facing collapse anchor follows B31's canonical death
    # cluster.  Coiled's older >=4 deaths / 1 sec snapshot remains diagnostic
    # only, so the two layers no longer show contradictory collapse messages.
    b31 = review.get("b31CollapseContext") or {}
    if b31.get("count"):
        direct = review.get("collapseDirect") if b31.get("causal") else None
        return (dict(direct) if isinstance(direct, dict) else None), {
            "collapseSec": b31.get("startSec"),
            "collapseEndSec": b31.get("endSec"),
            "collapseDeathCountB31": _safe_int(b31.get("count")),
            "collapseStatus": "linked" if b31.get("causal") else "partial",
            "collapseBasis": "b31",
            "collapseSummary": b31.get("summary"),
            "collapsePreceding": None,
        }
    p2 = _p2(review)
    summary = p2.get("summary") or {}
    direct = summary.get("collapseDirect") if isinstance(summary, dict) else None
    if direct:
        return dict(direct), summary
    collapse = ((review.get("observations") or {}).get("collapseSnapshot") or {})
    if collapse.get("significantCluster"):
        return None, {
            "collapseSec": collapse.get("collapseSec"),
            "collapseDeathCount1s": _safe_int(collapse.get("collapseCluster1s")),
            "collapseStatus": "unavailable",
            "collapsePreceding": (summary.get("collapsePreceding") if isinstance(summary, dict) else None),
        }
    return None, {
        "collapseSec": None,
        "collapseDeathCount1s": _safe_int(collapse.get("collapseCluster1s")),
        "collapseStatus": "unavailable",
    }


def _next_pull(first: dict | None, major: dict | None) -> str:
    kinds = {str((major or {}).get("kind") or ""), str((first or {}).get("kind") or "")}
    if "manifest_collision" in kinds:
        return "현신 대상자끼리 동선 분리"
    if "wail_interrupt_failure" in kinds:
        return "통곡 차단 담당·순번 다시 확인"
    if "gloombomb_failure" in kinds:
        return "암영폭탄 대상 외 이격 + 영혼살무사 적중 확인"
    if "soul_sever_non_tank_hit" in kinds:
        return "Soul Sever 전방은 탱커만 맞기"
    if "prior_add_overlap" in kinds:
        return "다음 웨이브 전 이전 영혼살무사 제거 확인"
    if "p1_tank_breath_non_tank_hit" in kinds:
        return "P1 브레스 전방은 탱커만 · 비탱 오피격 0회"
    if "p1_glaive_death" in kinds:
        return "글레이브 연속 피격 0회 · 파열 디버프 중 최우선 회피"
    if "p1_orb_failure" in kinds:
        return "짝수 브레스 구슬 처리량 낮추고 다음 단두대 전 잔존 0 확인"
    return "현재 검증된 실패 중 우선 교정 항목 없음"


def _card(key: str, title: str, status: str, value: str, note: str = "", *, raw_status: str | None = None, evidence_level: str | None = None) -> dict:
    out = {"key": key, "title": title, "status": status, "value": value, "note": note}
    if raw_status is not None:
        out["rawStatus"] = raw_status
    if evidence_level is not None:
        out["evidenceLevel"] = evidence_level
    return out


GRAVEBOUND_EXPIRE_SEC = 10.5   # 무덤구속 11초. 완료는 실측 10.4초 안, 시간 초과는 10.9~11.0초


def _gravebound_outcome_of(target: dict) -> str:
    """V5.2.3: 엔진 outcome 이 있으면 그대로, 예전 저장본이면 걸린·풀린 시각과 중첩 감소로 다시 계산한다."""
    out = str(target.get("outcome") or "")
    if out:
        return out
    a, r = target.get("appliedSec"), target.get("removedSec")
    if a is None or r is None:
        return "unknown"
    held = _safe_float(r, 0.0) - _safe_float(a, 0.0)
    if held >= GRAVEBOUND_EXPIRE_SEC:
        return "expired"
    return "complete" if _safe_int(target.get("removedStackEvents")) >= 2 else "unknown"


def _gravebound_card(review: dict):
    """영혼 3개 카드: 모든 P2 사이클의 무덤구속 대상 → 완료 / 11초 시간 초과 / 다른 원인 사망. 판정 대상이 없으면 None."""
    done, expired, died, unknown = 0, [], 0, 0
    for cycle in _cycles(review):
        for t in ((cycle.get("gravebound") or {}).get("targets") or []):
            o = _gravebound_outcome_of(t)
            name = str(t.get("player") or "?").split("-")[0]
            if o == "complete":
                done += 1
            elif o == "expired":
                expired.append(f"{_safe_int(cycle.get('index'))}번 {name}")
            elif o == "died":
                died += 1
            else:
                unknown += 1
    if not done and not expired:
        return None
    extra = []
    if died:
        extra.append(f"다른 원인 사망 {died}명 제외")
    if unknown:
        extra.append(f"판정 못 함 {unknown}명")
    if expired:
        value = f"완료 {done} · 시간 초과 {len(expired)}"
        note = "시간 초과: " + ", ".join(expired[:4]) + (" 외" if len(expired) > 4 else "")
        return "failed", value, " · ".join([note] + extra)
    # 판정 못 함 = 전투가 먼저 끝났거나 일찍 죽어서 풀린 경우. 시간 초과(11초)는 시각만으로 잡히므로 여기에 섞이지 않는다.
    value = f"{done}명 모두 완료" if not unknown else f"완료 {done}"
    note = " · ".join(["11초 안에 영혼 파편 3개"] + extra)
    return "success", value, note


def _status_from_check(check: dict | None) -> str:
    check = check or {}
    status = str(check.get("status") or "unknown")
    level = str(check.get("evidenceLevel") or "")
    if level != "verified":
        return "unknown"
    if status == "success":
        return "success"
    if status == "failed":
        return "failed"
    if status == "warning":
        return "warning"
    return "unknown"


def _first_cycle(review: dict) -> dict:
    cycles = _cycles(review)
    return cycles[0] if cycles else {}


def _orb_guillotine_snapshot(review: dict) -> dict:
    accounting = ((review.get("observations") or {}).get("orbAccounting") or {})
    rows = [
        x for x in (accounting.get("ledger") or [])
        if isinstance(x, dict) and str(x.get("kind") or "") == "guillotine"
    ]
    if not rows:
        rows = [x for x in (accounting.get("guillotineStates") or []) if isinstance(x, dict)]
    by_index = {_safe_int(x.get("guillotineIndex"), 0): x for x in rows if _safe_int(x.get("guillotineIndex"), 0)}
    wave_states = [x for x in (accounting.get("waveStates") or []) if isinstance(x, dict)]
    carries = [
        x for x in wave_states
        if not x.get("judgmentExcluded")
        and _safe_int(x.get("carryInOrbs"), 0) > 0
        and str(x.get("carryoverKind") or "wave_carryover") != "tank_handoff"
        and bool(x.get("carryover", True))
    ]
    return {
        "first": by_index.get(1),
        "second": by_index.get(2),
        "carryover": carries,
    }


def _orb_residual_note(review: dict) -> str:
    snap = _orb_guillotine_snapshot(review)
    obs = review.get("observations") or {}
    handoff = obs.get("tankHandoff") or {}
    parts = []
    first = snap.get("first") or {}
    second = snap.get("second") or {}
    if first.get("remainingOrbs") is not None:
        n = _safe_int(first.get("remainingOrbs"))
        parts.append(f"1차 단두대 직전 잔존 {n}개 {'✓' if n == 0 else '⚠ · 단독 실패 아님'}")
    if second.get("remainingOrbs") is not None:
        n = _safe_int(second.get("remainingOrbs"))
        hmax = handoff.get("tankHandoffMax")
        suffix = ""
        if hmax is not None:
            suffix = f" · 상한 {_safe_int(hmax)}개 {'!' if n > _safe_int(hmax) else '✓'}"
        parts.append(f"2차 단두대 잔존 {n}개 → P2 전 정리 대상{suffix}")
    if snap.get("carryover"):
        max_n = max(_safe_int(x.get("carryInOrbs")) for x in snap["carryover"])
        parts.append(f"일반 다음 웨이브 이월 {max_n}개 !")
    elif first.get("remainingOrbs") is not None or second.get("remainingOrbs") is not None:
        parts.append("일반 다음 웨이브 이월 없음")
    cleanup = str(handoff.get("p2EntryCleanupStatus") or "")
    if cleanup == "success":
        parts.append("P2 진입 잔존 0개 ✓")
    elif cleanup == "failed":
        parts.append(f"P2 진입 잔존 {_safe_int(handoff.get('p2EntryActiveOrbs'))}개 ✗")
    elif handoff:
        parts.append("P2 진입 잔존 미확정 · 직접 제거 신호 미검증")

    # B37.4.16: if an even-breath / Guillotine failure exists, show the prior
    # odd-breath amount as context only.  It never changes firstMistake/status.
    contexts = []
    for breath in obs.get("breaths") or []:
        if not isinstance(breath, dict):
            continue
        assignment = breath.get("strategyAssignment") or {}
        if _safe_int(assignment.get("positionBeforeGuillotine"), 0) != 2:
            continue
        failed_here = str(breath.get("status") or "") in {"failed", "warning"} or bool(breath.get("riskFlags"))
        ctx = breath.get("precedingOddContext") or {}
        if failed_here and ctx.get("removedOrbs") is not None:
            contexts.append(ctx)
    for gs in ((obs.get("orbAccounting") or {}).get("guillotineStates") or []):
        if not isinstance(gs, dict) or str(gs.get("status") or "") not in {"failed", "warning"}:
            continue
        ctx = gs.get("precedingOddContext") or {}
        if ctx.get("removedOrbs") is not None:
            contexts.append(ctx)
    seen = set()
    for ctx in contexts:
        key = (ctx.get("sec"), ctx.get("removedOrbs"), ctx.get("goalMin"), ctx.get("goalMax"))
        if key in seen:
            continue
        seen.add(key)
        goal = ""
        if ctx.get("goalMin") is not None and ctx.get("goalMax") is not None:
            goal = f"(계획 {_safe_int(ctx.get('goalMin'))}~{_safe_int(ctx.get('goalMax'))})"
        parts.append(f"선행: 직전 홀수 {_safe_int(ctx.get('removedOrbs'))}개{goal} · 실패 판정 아님")
        if len(seen) >= 2:
            break
    return " · ".join(parts)


_GENERIC_CARD_KEYS = [
    "p2_entry", "p1_orbs", "mind_control", "gravebound", "gloombomb_add",
    "gloombomb_extra", "wail", "manifest", "soul_sever", "prior_add", "p2_entry_orbs",
]


def _timeline_dedupe(events: list[dict], *, sec_tolerance: float = 0.12) -> list[dict]:
    """Suppress only presentation duplicates that are known to be duplicates.

    B37.4.31 deliberately narrows B37.4.30's generic same-kind/same-label rule.
    Two distinct physical events may legitimately share a label and occur inside
    120 ms (for example separate Viscous Cyst burst records), so proximity alone
    must never erase them.  Events are merged only when either side explicitly
    opts into UI de-duplication, or for a very small safe set of singleton anchors
    that can be emitted by more than one presentation path.
    """
    safe_kinds = {"wail", "wail_fail", "p2_entry", "phase", "phase_entry", "checkpoint"}
    out: list[dict] = []
    for raw in sorted((dict(x) for x in events if isinstance(x, dict) and x.get("sec") is not None), key=lambda x: float(x.get("sec") or 0)):
        kind = str(raw.get("kind") or "mechanic")
        label = str(raw.get("label") or raw.get("title") or "")
        key = raw.get("uiDedupeKey")
        raw_safe = bool(raw.get("uiDedupeSafe")) or kind in safe_kinds or bool(key)
        match = None
        if raw_safe:
            for old in reversed(out[-16:]):
                old_kind = str(old.get("kind") or "mechanic")
                old_key = old.get("uiDedupeKey")
                if key and old_key:
                    if str(key) != str(old_key):
                        continue
                elif old_kind != kind or not (bool(old.get("uiDedupeSafe")) or old_kind in safe_kinds):
                    continue
                try:
                    close = abs(float(old.get("sec")) - float(raw.get("sec"))) <= sec_tolerance
                except (TypeError, ValueError):
                    close = False
                if close and str(old.get("label") or old.get("title") or "") == label:
                    match = old
                    break
        if match is None:
            out.append(raw)
            continue
        # Keep the richer / stronger semantic projection without changing meaning.
        rank = {"unknown": 0, "success": 1, "warning": 2, "failed": 3}
        if rank.get(str(raw.get("status") or "unknown"), 0) > rank.get(str(match.get("status") or "unknown"), 0):
            for k, v in raw.items():
                if v not in (None, "", [], {}):
                    match[k] = v
        for k in ("failureRole", "failureRoleLabel", "summary", "evidenceLevel", "meaningEvidenceLevel"):
            if raw.get(k) and not match.get(k):
                match[k] = raw.get(k)
    return out


def _timeline_judgment_role(item: dict, *, result: str, first: dict | None, direct: dict | None) -> tuple[str | None, str | None]:
    role = item.get("failureRole")
    label = item.get("failureRoleLabel")
    if role:
        return role, label
    evidence = str(item.get("meaningEvidenceLevel") or item.get("evidenceLevel") or "")
    if evidence != "verified" or str(item.get("status") or "") != "failed":
        return None, None
    def same(candidate):
        if not isinstance(candidate, dict):
            return False
        # P1 failure rows can be folded into their physical breath row, so time
        # equality is enough for this presentation-only role assignment.
        try:
            return abs(float(candidate.get("sec")) - float(item.get("sec"))) <= 0.25
        except (TypeError, ValueError):
            return False
    if result == "KILL":
        return "recovered_on_kill", "처치 성공 · 회복된 실수"
    if same(direct):
        return "collapse_driver", "공대 붕괴 직결"
    if same(first):
        return "first_mistake", "첫 검증 실수"
    return "additional_failure", "추가 검증 실수"


def _sszorak_timeline_observations(review: dict) -> list[dict]:
    """Expose already-computed Sszorak observations to the UI timeline.

    No success/failure rule is introduced here; values are copied from the
    existing engine observation objects.
    """
    obs = review.get("observations") or {}
    out: list[dict] = []
    for strike in ((obs.get("mutilate") or {}).get("strikes") or []):
        if strike.get("sec") is None:
            continue
        out.append({
            "kind": "mutilate", "sec": strike.get("sec"), "label": "산개 소크",
            "summary": f"적중 {int(strike.get('targetCount') or 0)}명",
            "status": str(strike.get("status") or "unknown"),
            "evidenceLevel": strike.get("meaningEvidenceLevel") or strike.get("evidenceLevel"),
        })
    for ep in ((obs.get("viscousCyst") or {}).get("episodes") or []):
        if ep.get("sec") is None:
            continue
        status = "failed" if ep.get("chainCandidate") else "unknown"
        out.append({
            "kind": "viscous_cyst", "sec": ep.get("sec"), "label": "점성 낭종",
            "summary": f"폭발 pulse {int(ep.get('pulseCount') or 0)}",
            "status": status,
            "evidenceLevel": ep.get("meaningEvidenceLevel") if status == "failed" else ep.get("idEvidenceLevel"),
        })
    # Crosswinds applies once to many players.  Collapse same-second applications
    # into one neutral observation marker; existing failure rows remain separate.
    seen_cross: list[float] = []
    for ep in ((obs.get("ragingCrosswinds") or {}).get("episodes") or []):
        sec = _safe_float(ep.get("sec"))
        if sec is None or any(abs(sec - old) <= 0.12 for old in seen_cross):
            continue
        seen_cross.append(sec)
        out.append({
            "kind": "crosswinds", "sec": sec, "label": "격노하는 돌풍",
            "summary": "Turbulent Gusts 적용", "status": "unknown",
            "evidenceLevel": ep.get("idEvidenceLevel"),
        })
    for pop in ((obs.get("serpentsFury") or {}).get("successfulPops") or []):
        sec = pop.get("slaughterSec") if pop.get("slaughterSec") is not None else pop.get("furyRemovedSec")
        if sec is not None:
            out.append({
                "kind": "serpents_fury", "sec": sec, "label": "뱀의 격노 해제",
                "summary": "To the Slaughter 전환", "status": "success",
                "evidenceLevel": pop.get("evidenceLevel"),
            })
    return out


def _current_difficulty(review: dict, row: dict | None = None):
    row = row or {}
    if row.get("difficultyID") is not None:
        return row.get("difficultyID")
    return (review.get("encounter") or {}).get("difficultyID")


def _apply_check_difficulty(card: dict, review: dict, check: dict, row: dict | None = None) -> dict:
    """Attach per-check difficultyIDs and project out-of-scope checks neutrally.

    Only explicitly confirmed difficulty-specific mechanics get metadata.  The
    original check status/value/note is retained so the folded evidence can
    preserve every fact even when the compact card says ``해당 없음``.
    """
    out = dict(card or {})
    boss_key = str(review.get("bossKey") or "")
    check_key = str(check.get("key") or "")
    ids = tuple(check.get("difficultyIDs") or check_difficulty_ids(boss_key, check_key))
    out["checkKey"] = check_key
    if not ids:
        return out
    out["difficultyIDs"] = list(ids)
    if is_mythic_only(ids):
        title = str(out.get("title") or "")
        if not title.endswith(" (M)"):
            out["title"] = title + " (M)"
        out["difficultyLabel"] = "M"
    match = difficulty_matches(ids, _current_difficulty(review, row))
    if match is False:
        out["sourceStatus"] = out.get("status")
        out["sourceRawStatus"] = out.get("rawStatus")
        out["sourceValue"] = out.get("value")
        out["sourceNote"] = out.get("note")
        out["status"] = "unknown"
        out["rawStatus"] = "not_applicable"
        out["value"] = "해당 없음"
        out["mutedForDifficulty"] = True
        if not str(out.get("note") or "").strip():
            out["note"] = "현재 난이도에는 적용하지 않는 신화 전용 판정"
    return out


def _card_detail_text(cards: list[dict]) -> str:
    """Full text backing the two-line mechanic cards."""
    lines = []
    for card in cards or []:
        title = str(card.get("title") or "기믹")
        value = str(card.get("value") or "-")
        note = str(card.get("sourceNote") if card.get("mutedForDifficulty") else card.get("note") or "").strip()
        if card.get("mutedForDifficulty"):
            note = (note + " · 현재 난이도 해당 없음").strip(" ·")
        detail = f"- {title}: {value}"
        if note:
            detail += f" · {note}"
        lines.append(detail)
    return "\n".join(lines)


def _generic_mechanic_priority_model(review: dict, row: dict | None = None) -> dict:
    row = row or {}
    enc = review.get("encounter") or {}
    verdict = review.get("verdict") or {}
    obs = review.get("observations") or {}
    checks = [x for x in (review.get("checks") or []) if isinstance(x, dict)]
    status_label = {"success": "정상", "failed": "실패", "warning": "경고", "unknown": "확인 불가"}
    cards = []
    for slot, check in zip(_GENERIC_CARD_KEYS, checks[:len(_GENERIC_CARD_KEYS)]):
        raw_status = str(check.get("rawStatus") or check.get("status") or "unknown")
        evidence_level = str(check.get("evidenceLevel") or "")
        note = str(check.get("summary") or "")

        informational = bool(check.get("informational"))
        if informational:
            projected_status = "unknown"
            candidate_semantic = False
        else:
            projected_status = _status_from_check(check)
            check_status = str(check.get("status") or "unknown")
            candidate_semantic = evidence_level == "candidate" and check_status in {"success", "failed", "warning"}
        if check.get("displayValue") is not None:
            value = str(check.get("displayValue"))
        elif candidate_semantic:
            value = "후보"
        elif projected_status != "unknown":
            value = status_label.get(projected_status, str(projected_status or "?"))
        elif "현재 난이도" in note or "적용하지 않" in note or "신화 전용 판정" in note:
            value = "적용 안 함"
        elif evidence_level == "candidate":
            # A candidate with an observed-but-undecided result is not a warning/failure.
            # Keep it visually neutral and say the success/failure criterion is unresolved.
            value = "후보" if any(token in note for token in ("미관측", "찾지 못", "신호 미관측")) else "기준 미확정"
        elif any(token in note for token in ("미관측", "찾지 못", "신호 미관측")):
            value = "로그 신호 없음"
        else:
            value = "판정 대기"
        if candidate_semantic and "후보" not in note:
            note = (note + " · 의미 검증 전 후보").strip(" ·")
        card = {
            "key": slot,
            "title": str(check.get("title") or check.get("key") or "기믹"),
            "status": projected_status,
            "rawStatus": raw_status,
            "evidenceLevel": evidence_level or None,
            "value": value,
            "note": note,
        }
        cards.append(_apply_check_difficulty(card, review, check, row))
    # B37.3.6: generic bosses now honor the same three presentation slots as
    # the Coiled screen.  Candidate mechanics still stay outside these slots.
    # B37.4.11: a Kill can still contain a verified execution mistake.  Keep the
    # mechanic card/timeline red, but do not present that survived mistake as a
    # wipe-chain firstMistake / majorIncident / collapseDirect.  It belongs in
    # the Kill-only remaining-improvement lane instead.
    result = str(row.get("result") or enc.get("result") or "").upper()
    first = review.get("firstMistake") if isinstance(review.get("firstMistake"), dict) else None
    if first is None:
        first = obs.get("firstVerifiedFailure") if isinstance(obs.get("firstVerifiedFailure"), dict) else None
    major = review.get("majorIncident") if isinstance(review.get("majorIncident"), dict) else None
    direct = review.get("collapseDirect") if isinstance(review.get("collapseDirect"), dict) else None
    link = review.get("collapseLink") or {}

    kill_improvement = None
    verified_failure_count = 0
    first_recovered_failure = None
    if result == "KILL":
        # B37.4.12: remaining-improvement promotion is a *meaning* decision.
        # Some timelines use evidenceLevel for runtime ID observation, so prefer
        # meaningEvidenceLevel whenever it is present.  Also prefer a real
        # verified failure over an earlier verified warning; warnings are only
        # a fallback when no verified failure survived the kill.
        def _meaning_level(item):
            if not isinstance(item, dict):
                return ""
            return str(item.get("meaningEvidenceLevel") or item.get("evidenceLevel") or "")

        verified_failed = [
            x for x in (review.get("timeline") or [])
            if isinstance(x, dict)
            and str(x.get("status") or x.get("rawStatus") or "") == "failed"
            and _meaning_level(x) == "verified"
        ]
        verified_warning_timeline = [
            x for x in (review.get("timeline") or [])
            if isinstance(x, dict)
            and str(x.get("status") or x.get("rawStatus") or "") == "warning"
            and _meaning_level(x) == "verified"
        ]
        verified_failed.sort(key=lambda x: float(x.get("sec") or 10**12))
        verified_warning_timeline.sort(key=lambda x: float(x.get("sec") or 10**12))
        verified_failure_count = len(verified_failed)
        if verified_failed:
            first_recovered_failure = dict(verified_failed[0])
            first_recovered_failure.setdefault("failureRole", "recovered_on_kill")
            first_recovered_failure.setdefault("failureRoleLabel", "처치 성공 · 회복된 실수")
            kill_improvement = dict(first_recovered_failure)
        elif isinstance(first, dict) and _meaning_level(first) == "verified" and str(first.get("status") or "failed") == "failed":
            first_recovered_failure = dict(first)
            first_recovered_failure.setdefault("failureRole", "recovered_on_kill")
            first_recovered_failure.setdefault("failureRoleLabel", "처치 성공 · 회복된 실수")
            verified_failure_count = 1
            kill_improvement = dict(first_recovered_failure)
        elif verified_warning_timeline:
            kill_improvement = dict(verified_warning_timeline[0])
        first = None
        major = None
        direct = None

    if first:
        first_text = f"{fmt_clock(first.get('sec'))} · {first.get('label') or first.get('kind') or '검증 실패'}"
        if first.get("summary"):
            first_text += f" · {first.get('summary')}"
    elif result == "KILL" and verified_failure_count:
        first_text = f"없음 · Kill 트라이 · 검증 실패 {verified_failure_count}회 · 남은 개선점으로 분리"
    else:
        candidate_count = sum(
            1 for x in (review.get("timeline") or [])
            if str(x.get("rawStatus") or x.get("status") or "") in {"failed", "warning"}
            and str(x.get("evidenceLevel") or x.get("meaningEvidenceLevel") or "") == "candidate"
        )
        first_text = "없음 · 검증 실패 0회"
        if candidate_count:
            first_text += f" · 후보 {candidate_count}건은 카드에 별도 표시"

    if major:
        major_text = str(major.get("label") or major.get("title") or major.get("summary") or "주요 사고")
        if major.get("sec") is not None:
            major_text = f"{fmt_clock(major.get('sec'))} · {major_text}"
        if major.get("summary") and major.get("summary") not in major_text:
            major_text += f" · {major.get('summary')}"
    else:
        if result == "KILL":
            major_text = "없음 · Kill 트라이"
        elif first:
            major_text = "없음 · 검증 실패는 있으나 주요 사고 연결 근거 없음"
        else:
            major_text = "없음 · 검증된 주요 사고 없음"
    if direct:
        collapse_text = str(link.get("summary") or f"{direct.get('label') or direct.get('title') or '검증 실패'} → B31 붕괴")
        collapse_status = "failed"
    elif link.get("status") == "linked":
        collapse_text = str(link.get("summary") or "B31 붕괴 체인 연결")
        collapse_status = "failed"
    elif link.get("status") in {"preceding", "preceding_end"}:
        collapse_text = str(link.get("summary") or "검증 실패가 붕괴/전투 종료 직전에 관측됨 · 인과는 미확정")
        collapse_status = "unknown"
    elif result == "KILL":
        collapse_text = "킬 성공 · 공대 붕괴 없음"
        collapse_status = "success"
    else:
        collapse_text = "직접 인과 연결 대기 · B31 상세 복기 보존"
        collapse_status = "unknown"
    timeline = []
    for item in review.get("timeline") or []:
        if not isinstance(item, dict) or item.get("sec") is None:
            continue
        raw_status = str(item.get("status") or "unknown")
        evidence_level = str(item.get("evidenceLevel") or item.get("meaningEvidenceLevel") or "")
        projected_status = raw_status if evidence_level == "verified" and raw_status in {"success", "failed", "warning"} else "unknown"
        label = str(item.get("title") or item.get("summary") or "기믹")
        if evidence_level == "candidate" and raw_status in {"success", "failed", "warning"} and "후보" not in label:
            label += " · 후보"
        failure_role = item.get("failureRole")
        failure_role_label = item.get("failureRoleLabel")
        # B37.4.23: timeline rendering owns presentation, not mechanic semantics.
        # Preserve explicit engine roles when available and infer only a UI role
        # for verified failures so every boss can use the same visual grammar.
        if not failure_role and evidence_level == "verified" and raw_status == "failed":
            def _same_timeline_incident(candidate):
                if not isinstance(candidate, dict):
                    return False
                if str(candidate.get("kind") or "") != str(item.get("kind") or ""):
                    return False
                try:
                    return abs(float(candidate.get("sec")) - float(item.get("sec"))) <= 0.25
                except (TypeError, ValueError):
                    return False
            if result == "KILL":
                failure_role, failure_role_label = "recovered_on_kill", "처치 성공 · 회복된 실수"
            elif _same_timeline_incident(direct):
                failure_role, failure_role_label = "collapse_driver", "공대 붕괴 직결"
            elif _same_timeline_incident(first):
                failure_role, failure_role_label = "first_mistake", "첫 검증 실수"
            else:
                failure_role, failure_role_label = "additional_failure", "추가 검증 실수"
        timeline_row = {
            "kind": str(item.get("kind") or "mechanic"),
            "sec": item.get("sec"),
            "label": label,
            "status": projected_status,
            "rawStatus": raw_status,
            "evidenceLevel": evidence_level or None,
            "failureRole": failure_role,
            "failureRoleLabel": failure_role_label,
            "summary": str(item.get("summary") or ""),
        }
        # B37.5 TimelineGlyph: retain factual presentation metadata so an icon
        # tooltip can show who was hit and which physical spell produced it.
        # These fields do not affect verdict semantics.
        for field in ("spellID", "abilityGameID", "spellIDs", "directSpellIDs", "ids",
                      "targets", "targetNames", "targetCount", "uniqueTargets", "linkedDeaths"):
            if item.get(field) not in (None, "", [], {}):
                timeline_row[field] = item.get(field)
        timeline.append(timeline_row)
    if review.get("bossKey") == "sszorak":
        timeline.extend(_sszorak_timeline_observations(review))
    if review.get("bossKey") == "twin_fangs":
        # Old stored reviews already contain the per-wave target roster under
        # observations even when the compact timeline row only said "1명".
        # Enrich the presentation in-place so no reanalysis is required.
        wave_eps = ((((obs.get("directFailuresB3731") or {}).get("stirWaveHits") or {}).get("episodes")) or [])
        all_wave_targets = []
        for ep in wave_eps:
            for name in ep.get("targets") or []:
                text = str(name or "").strip()
                if text and text not in all_wave_targets:
                    all_wave_targets.append(text)
        total_wave_hits = sum(int(ep.get("uniqueTargets") or 0) for ep in wave_eps)
        for row_item in timeline:
            if str(row_item.get("kind") or "") != "stir_wave_hit" or row_item.get("sec") is None:
                continue
            sec = float(row_item.get("sec") or 0)
            ep = next((x for x in wave_eps if abs(float(x.get("sec") or 0)-sec) <= 0.12), None)
            if ep is not None:
                row_item["targets"] = list(ep.get("targets") or [])
                row_item["targetCount"] = int(ep.get("uniqueTargets") or len(row_item["targets"]))
                if ep.get("spellIDs"):
                    row_item["spellIDs"] = list(ep.get("spellIDs") or [])
            if str(row_item.get("label") or "").startswith("심연 휘젓기") and all_wave_targets:
                row_item["targets"] = list(all_wave_targets)
                row_item["targetCount"] = len(all_wave_targets)
                names = ", ".join(all_wave_targets[:4])
                if len(all_wave_targets) > 4:
                    names += f" 외 {len(all_wave_targets)-4}명"
                base = f"{len(wave_eps)}회 묶음 · {total_wave_hits}명-피격 합"
                row_item["summary"] = f"{base} · 피격자 {names}"
    timeline = _timeline_dedupe(timeline)
    timeline.sort(key=lambda x: float(x.get("sec") or 0))
    limitations = [str(x) for x in (review.get("limitations") or []) if x]
    side_lines = []
    if review.get("bossKey") == "twin_fangs":
        vis = obs.get("visceralBurst") or {}
        bul = obs.get("barbedBulwark") or {}
        brood = obs.get("brood") or {}
        roles = [x for x in (obs.get("failureRoles") or []) if isinstance(x, dict)]
        side_lines.append(f"내장 파열 · 시작 {int(vis.get('startCount') or 0)} / 차단 {int(vis.get('interruptCount') or 0)} / 실패 {len(vis.get('failureEpisodes') or [])}")
        if roles:
            collapse_n = sum(1 for x in roles if x.get("failureRole") == "collapse_driver")
            recovered_n = sum(1 for x in roles if x.get("failureRole") == "recovered_on_kill")
            first_n = sum(1 for x in roles if x.get("failureRole") == "first_mistake")
            extra_n = sum(1 for x in roles if x.get("failureRole") == "additional_failure")
            side_lines.append(f"검증 실수 역할 · 붕괴 직결 {collapse_n} / 첫 실수 {first_n} / 추가 {extra_n} / 킬에서 회복 {recovered_n}")
        side_lines.append(f"가시 보루 · 실제 차단 {int(bul.get('interruptCount') or 0)}회")
        if brood.get("waves"):
            side_lines.append("새끼 깨우기 · " + " / ".join(f"#{x.get('index')} {x.get('summonCount')}마리" for x in brood.get("waves") or []))
    if review.get("bossKey") == "buried_sentinel":
        roles = [x for x in (obs.get("failureRoles") or []) if isinstance(x, dict)]
        if roles:
            collapse_n = sum(1 for x in roles if x.get("failureRole") == "collapse_driver")
            recovered_n = sum(1 for x in roles if x.get("failureRole") == "recovered_on_kill")
            first_n = sum(1 for x in roles if x.get("failureRole") == "first_mistake")
            extra_n = sum(1 for x in roles if x.get("failureRole") == "additional_failure")
            side_lines.append(f"물방울 실수 역할 · 붕괴 직결 {collapse_n} / 첫 실수 {first_n} / 추가 {extra_n} / 킬에서 회복 {recovered_n}")
    if review.get("bossKey") == "nekzali":
        roles = [x for x in (obs.get("failureRoles") or []) if isinstance(x, dict)]
        if roles:
            collapse_n = sum(1 for x in roles if x.get("failureRole") == "collapse_driver")
            recovered_n = sum(1 for x in roles if x.get("failureRole") == "recovered_on_kill")
            first_n = sum(1 for x in roles if x.get("failureRole") == "first_mistake")
            extra_n = sum(1 for x in roles if x.get("failureRole") == "additional_failure")
            side_lines.append(f"저주 차단 실수 역할 · 붕괴 직결 {collapse_n} / 첫 실수 {first_n} / 추가 {extra_n} / 킬에서 회복 {recovered_n}")
    if review.get("bossKey") == "lost_explorers":
        roles = [x for x in (obs.get("failureRoles") or []) if isinstance(x, dict)]
        if roles:
            collapse_n = sum(1 for x in roles if x.get("failureRole") == "collapse_driver")
            recovered_n = sum(1 for x in roles if x.get("failureRole") == "recovered_on_kill")
            first_n = sum(1 for x in roles if x.get("failureRole") == "first_mistake")
            extra_n = sum(1 for x in roles if x.get("failureRole") == "additional_failure")
            side_lines.append(f"탐험가 검증 실수 역할 · 붕괴 직결 {collapse_n} / 첫 실수 {first_n} / 추가 {extra_n} / 킬에서 회복 {recovered_n}")
    if review.get("bossKey") == "sszorak":
        roles = [x for x in (obs.get("failureRoles") or []) if isinstance(x, dict)]
        if roles:
            collapse_n = sum(1 for x in roles if x.get("failureRole") == "collapse_driver")
            recovered_n = sum(1 for x in roles if x.get("failureRole") == "recovered_on_kill")
            first_n = sum(1 for x in roles if x.get("failureRole") == "first_mistake")
            extra_n = sum(1 for x in roles if x.get("failureRole") == "additional_failure")
            side_lines.append(f"스조라크 검증 실수 역할 · 붕괴 직결 {collapse_n} / 첫 실수 {first_n} / 추가 {extra_n} / 킬에서 회복 {recovered_n}")
    if review.get("bossKey") == "ulatek":
        roles = [x for x in (obs.get("failureRoles") or []) if isinstance(x, dict)]
        if roles:
            collapse_n = sum(1 for x in roles if x.get("failureRole") == "collapse_driver")
            recovered_n = sum(1 for x in roles if x.get("failureRole") == "recovered_on_kill")
            first_n = sum(1 for x in roles if x.get("failureRole") == "first_mistake")
            extra_n = sum(1 for x in roles if x.get("failureRole") == "additional_failure")
            side_lines.append(f"울라텍 검증 실수 역할 · 붕괴 직결 {collapse_n} / 첫 실수 {first_n} / 추가 {extra_n} / 킬에서 회복 {recovered_n}")
        hearts = obs.get("heartWindows") or []
        if hearts:
            hp_parts = [
                f"{int(x.get('index') or 0)}심장 종료 {float(x.get('endBossHpPct')):.1f}%"
                for x in hearts if x.get("endBossHpPct") is not None
            ]
            if hp_parts:
                side_lines.append("심장 진도 · " + " / ".join(hp_parts))
            else:
                side_lines.append(f"심장 진도 · {len(hearts)}회 관측")
            dmg = [x for x in hearts if x.get("heartDamage") is not None]
            if dmg:
                side_lines.append("심장 직접딜 · " + " / ".join(f"{int(x.get('index') or 0)}심장 {float(x.get('heartDamage'))/1_000_000:.1f}M" for x in dmg))
        proj = obs.get("heartProjection") or {}
        if proj.get("label"):
            side_lines.append(f"심장 진행 · {proj.get('label')} · {proj.get('summary') or ''}")
    footer = str(verdict.get("nextAction") or "")
    verified_warnings = [
        x for x in checks
        if str(x.get("status") or "") == "warning"
        and str(x.get("evidenceLevel") or "") == "verified"
    ]
    if result == "KILL" and kill_improvement:
        label = str(kill_improvement.get("label") or kill_improvement.get("title") or kill_improvement.get("kind") or "검증 실패")
        summary = str(kill_improvement.get("summary") or "").strip()
        sec = kill_improvement.get("sec")
        recovered = str(kill_improvement.get("failureRole") or "") == "recovered_on_kill"
        lead = "남은 개선점(회복됨)" if recovered else "남은 개선점"
        prefix = f"{lead} · {fmt_clock(sec)} · {label}" if sec is not None else f"{lead} · {label}"
        footer = (prefix + (f" · {summary}" if summary else "")).rstrip(" ·")
    elif result == "KILL" and verified_warnings:
        w = verified_warnings[0]
        footer = f"남은 개선점 · {w.get('title') or '경고'} · {w.get('summary') or ''}".rstrip(" ·")
    elif result == "KILL" and not first:
        footer = "킬 성공 · 검증 실패 없음. 후보/미관측 항목은 성공으로 단정하지 않고 다음 표본에서 계속 검증합니다."

    return {
        "supported": True,
        "generic": True,
        "bossKey": review.get("bossKey"),
        "tryNo": row.get("tryNo") if row.get("tryNo") is not None else enc.get("tryNo"),
        "durationSec": row.get("durationSec") if row.get("durationSec") is not None else enc.get("durationSec"),
        "scopeText": f"{enc.get('name') or review.get('bossKey') or 'BossMechanic'} · 범위: 현재 트라이",
        "firstMistake": first,
        "firstMistakeText": first_text,
        "majorIncident": major,
        "majorIncidentText": major_text,
        "collapseDirect": direct,
        "collapseLink": link,
        "killImprovement": kill_improvement,
        "verifiedFailureCount": verified_failure_count,
        "firstRecoveredFailure": first_recovered_failure,
        "collapseText": collapse_text,
        "collapseStatus": collapse_status,
        "collapseSec": (link.get("collapseSec") if isinstance(link, dict) else None) or ((direct or {}).get("collapseSec") if isinstance(direct, dict) else None),
        "deathEvents": list(obs.get("deathEvents") or []),
        "footer": footer,
        "cards": cards,
        "timeline": timeline,
        "evidence": [],
        "evidenceText": "\n\n".join([
            "카드 상세\n" + _card_detail_text(cards),
            "판정 한계\n" + ("\n".join(limitations) if limitations else "추가 판정 한계 없음"),
        ]),
        "sidePanelText": "\n".join(side_lines) if side_lines else "보조 관측 없음",
    }


def build_try_priority_model(review: dict, row: dict | None = None) -> dict:
    """B36.4.1 read-only UI projection.

    This function never promotes candidate/diagnostic signals. It only projects
    B36.4 judgments into the 6→7→8 layout contract.
    """
    row = row or {}
    if not is_coiled_review(review):
        if isinstance(review, dict) and review.get("bossKey") and review.get("checks") is not None:
            return _generic_mechanic_priority_model(review, row)
        return {"supported": False, "cards": [], "timeline": [], "evidence": []}

    obs = review.get("observations") or {}
    p2 = _p2(review)
    p2_summary = p2.get("summary") or {}
    # B37.4.15: fight-level earliest verified failure wins, including P1.
    first_candidates = []
    for candidate in (review.get("firstMistake"), obs.get("firstVerifiedFailure"),
                      p2_summary.get("firstMistake") if isinstance(p2_summary, dict) else None):
        if isinstance(candidate, dict) and candidate.get("sec") is not None:
            first_candidates.append(dict(candidate))
    first = min(first_candidates, key=lambda x: _safe_float(x.get("sec"), 1e12)) if first_candidates else _fallback_first_mistake(review)
    direct, collapse_meta = _collapse_direct(review)
    # Screen 7 is a cycle-level review.  Focus the mechanic cards/evidence on
    # the cycle containing the first verified mistake; if none exists, use the
    # first observed P2 cycle.  The collapse-direct line remains fight-level.
    focus_cycle_index = _safe_int((first or {}).get("cycleIndex"), 0) or 1
    cycle = _cycle_by_index(review, focus_cycle_index)
    focus_cycle_index = _safe_int(cycle.get("index"), focus_cycle_index)
    major = _major_incident(review, focus_cycle_index)
    checks = _checks(review)

    phase = obs.get("phase1Transition") or {}
    p2_entry_reached = phase.get("entrySec") is not None
    p2_cycle_reached = _safe_int(p2.get("cycleCount")) > 0
    if not p2_cycle_reached:
        p2_not_applicable_value = "도달 전 종료" if p2_entry_reached else "적용 안 함"
        p2_not_applicable_note = "P2 진입 후 1사이클 시작 전 종료" if p2_entry_reached else "P2 미진입"
    else:
        p2_not_applicable_value = "확인 불가"
        p2_not_applicable_note = "관측 신호 부족"
    entry_status = "success" if phase.get("status") == "success" and phase.get("evidenceLevel") == "verified" else "failed" if phase.get("status") == "failed" and phase.get("evidenceLevel") == "verified" else "unknown"
    entry_val = fmt_clock(phase.get("entrySec")) if phase.get("entrySec") is not None else "P2 미진입"
    entry_raw_status = "observed" if phase.get("entrySec") is not None and entry_status == "unknown" else None
    hp = phase.get("healthAtCheckpoint") or {}
    target_min = _safe_float(phase.get("targetMinSec"), 165.0)
    target_max = _safe_float(phase.get("targetMaxSec"), 170.0)
    entry_note = f"목표 {fmt_clock(target_min)}~{fmt_clock(target_max)}"
    if phase.get("entrySec") is not None:
        sec = float(phase.get("entrySec"))
        phase_status = str(phase.get("status") or "unknown")
        if phase_status == "early":
            entry_note += f" · 목표 시작 대비 {sec-target_min:+.1f}초"
        elif phase_status == "late":
            entry_note += f" · 목표 종료 대비 {sec-target_max:+.1f}초"
        elif phase_status == "success":
            entry_note += " · 목표 구간 관측"
        else:
            entry_note += " · 시각 관측 · 목표 판정 보류"
    if hp.get("available") and hp.get("healthPct") is not None:
        entry_note += f" · 2:45 줄잔 {float(hp['healthPct']):.1f}%"

    orb_check = checks.get("orb_processing")
    orb_status = _status_from_check(orb_check)
    breath_values = []
    for b in obs.get("breaths") or []:
        if not b.get("strategyApplies"):
            continue
        val = b.get("destroyedOrbs")
        if val is not None:
            breath_values.append(str(int(val)))
    if breath_values:
        orb_val = " · ".join(breath_values)
    elif obs.get("breaths"):
        orb_val = "일부 로그 신호 없음"
    else:
        orb_val = "로그 신호 없음"
    orb_note = _orb_residual_note(review) or str((orb_check or {}).get("summary") or "브레스별 전략 슬롯")
    if orb_status == "unknown" and breath_values:
        orb_note = "판정 미완료 · " + orb_note

    mc = cycle.get("mindControl") or {}
    mc_count = mc.get("targetCount")
    # V5.2.3: 정배 자체는 위치 기믹이라 로그로 성공/실패를 볼 수 없다. 망치면 현신 접촉으로 나오므로 판정은 현신 칸에 맡긴다.
    mc_val = f"대상 {_safe_int(mc_count)}명 · 참고" if mc_count is not None else (p2_not_applicable_value if not p2_cycle_reached else "로그 신호 없음")
    mc_note = "판정은 현신 접촉 칸 (정배 위치는 로그에 없음)" if p2_cycle_reached and mc_count is not None else ("성공 판정 신호 ID 미확정" if p2_cycle_reached else p2_not_applicable_note)

    grave = cycle.get("gravebound") or {}
    grave_targets = grave.get("targets") or []
    grave_val = p2_not_applicable_value if not p2_cycle_reached else "로그 신호 없음"
    grave_note = "수집 신호 계약 미확정" if p2_cycle_reached else p2_not_applicable_note
    if grave_targets:
        grave_val = f"대상 {len(grave_targets)}명 · 기준 미확정"
        grave_note = "후보 관측 · 영혼 3개 성공/실패 계약 미확정"
    grave_status, grave_judged = "unknown", _gravebound_card(review)
    if grave_judged:
        grave_status, grave_val, grave_note = grave_judged

    gb = cycle.get("gloombomb") or {}
    expected = _safe_int(gb.get("soulcoilerExpectedCount"))
    known = _safe_int(gb.get("soulcoilerVulnerabilityKnownCount"))
    removed = _safe_int(gb.get("soulcoilerVulnerabilityRemovedCount"))
    retained = _safe_int(gb.get("soulcoilerVulnerabilityRetainedCount"))
    cycle_flags = set(cycle.get("flags") or [])
    bomb_verified_failure = "gloombomb_soulcoiler_hit_missing" in cycle_flags or retained > 0
    if expected and (known >= expected or bomb_verified_failure):
        bomb_status = "failed" if bomb_verified_failure else ("success" if removed == expected and retained == 0 else "failed")
        bomb_val = f"{removed} / {expected}"
    else:
        bomb_status, bomb_val = "unknown", (p2_not_applicable_value if not p2_cycle_reached else "로그 신호 없음")
    bomb_state_bits = []
    for state in gb.get("soulcoilerVulnerabilityStates") or []:
        idx = _safe_int(state.get("soulcoilerIndex"), 0)
        status = str(state.get("status") or "unknown")
        if idx:
            bomb_state_bits.append(f"쫄#{idx} " + ("해제" if status == "success" else "남음" if status == "failed" else "?"))
    if bomb_state_bits:
        bomb_note = " · ".join(bomb_state_bits) + " · 위치 식별은 로그로 불가"
    else:
        bomb_note = "피해 비율 0.01 → 정상" if bomb_status != "unknown" else (p2_not_applicable_note if not p2_cycle_reached else "전후 합산 피해 비율 신호 부족")
    if gb.get("firstInterruptToDamageSec") is not None:
        delta = float(gb.get("firstInterruptToDamageSec"))
        who = short_player(gb.get("firstInterruptPlayer"))
        order_ok = gb.get("firstInterruptOrderOk")
        mark = "✅" if order_ok is True else "❌" if order_ok is False else ""
        bomb_note += f" · 1차단 {who or '-'} → 폭탄 {delta:+.1f}초 {mark}".rstrip()
        if order_ok is False:
            bomb_status = "failed"

    extra = _safe_int(gb.get("extraHitCount"))
    extra_known = gb.get("damageUniqueTargetCount") is not None and gb.get("targetCount") is not None
    extra_status = "warning" if extra > 0 else "success" if extra_known else "unknown"
    extra_val = f"{extra}명" if extra > 0 else "없음" if extra_known else (p2_not_applicable_value if not p2_cycle_reached else "확인 불가")
    extra_players = [short_player(x) for x in (gb.get("extraHitPlayers") or [])]
    extra_note = ("대상 4명 외 · " + ", ".join(extra_players)) if extra > 0 and extra_players else "대상 4명 외 피격" if extra > 0 else (p2_not_applicable_note if not p2_cycle_reached else "폭탄 대상 외 추가피격")

    wail = cycle.get("wail") or {}
    wfail = _safe_int(wail.get("failureEpisodeCount"))
    wail_known = _safe_int(wail.get("castCount")) > 0 or _safe_int(wail.get("interruptCount")) > 0 or wfail > 0
    wail_status = "failed" if wfail else "success" if wail_known else "unknown"
    wail_val = f"실패 {wfail}" if wail_known else (p2_not_applicable_value if not p2_cycle_reached else "확인 불가")
    completed_secs = sorted({_safe_float(x) for x in (wail.get("completedCastSecs") or []) if _safe_float(x) is not None})
    if completed_secs:
        wail_note = "완주시전 " + ", ".join(fmt_clock(x) for x in completed_secs)
        collapse_anchor = _safe_float(collapse_meta.get("collapseSec"))
        if not wfail and collapse_anchor is not None and all(float(x) >= collapse_anchor for x in completed_secs):
            # Fact-only UI wording: the engine counts Wail failures from the
            # observed player fear debuff, not from a generic "post-collapse
            # exclusion" rule.  Do not invent a causal reason here.
            wail_note += " 관측 · 붕괴 이후 시전 · 플레이어 공포(1286399) 미관측"
    else:
        wail_note = "1286399 공포 없음" if wail_known and not wfail else (p2_not_applicable_note if not p2_cycle_reached else "1286399 플레이어 디버프")

    collision = cycle.get("manifestCollision") or {}
    collision_n = _safe_int(collision.get("episodeCount"))
    collision_deaths = _safe_int(collision.get("linkedDeathCount"))
    collision_known = collision_n > 0 or (cycle.get("soulSever") or {}).get("castSec") is not None or cycle.get("nextCycleSec") is not None
    collision_status = "failed" if collision_n else "success" if collision_known else "unknown"
    collision_val = (
        f"이벤트 {collision_n}회" + (f" · 연계사망 {collision_deaths}명" if collision_deaths else "")
        if collision_n else "없음" if collision_known else
        (p2_not_applicable_value if not p2_cycle_reached else "사이클 중단 · 판정 보류")
    )

    ss = cycle.get("soulSever") or {}
    ss_title = "탱 브레스 오피격"
    tanks = ss.get("tankPlayersEffective") or ss.get("tankPlayersConfigured") or ss.get("tankPlayersInferred") or []
    non_tank = _safe_int(ss.get("nonTankHitCount"))
    p1tb = ((obs.get("p1Execution") or {}).get("tankBreath") or {})
    p1_tanks = p1tb.get("tankPlayersEffective") or []
    p1_non_tank = list(p1tb.get("nonTankHits") or [])
    p1_verified = bool(p1_tanks) and str(p1tb.get("evidenceLevel") or "") == "verified"
    if p2_cycle_reached and tanks and ss.get("castSec") is not None:
        ss_status = "failed" if non_tank else "success"
        ss_val = f"비탱 {non_tank}명" if non_tank else "오피격 없음"
        source = str(ss.get("tankResolutionSource") or "")
        source_label = "raidPlan" if source == "manual_raidplan" else "로스터 spec 자동" if source == "roster_spec" else "탱커"
        ss_note = source_label + " · " + ", ".join(short_player(x) for x in tanks)
    elif p1_verified:
        ss_title = "P1 탱 브레스 오피격"
        if p1_non_tank:
            ss_status = "failed"
            ss_val = f"비탱 {len(p1_non_tank)}명"
            bits = []
            for hit in p1_non_tank[:6]:
                label = short_player(hit.get("player"))
                if hit.get("linkedDeath"):
                    label += " → 사망"
                bits.append(label)
            ss_note = ", ".join(bits)
            if p1_tanks:
                ss_note += " · 탱 " + ", ".join(short_player(x) for x in p1_tanks)
        else:
            ss_status = "success"
            ss_val = "비탱 오피격 0회"
            ss_note = "로스터 spec 자동 · 탱 " + ", ".join(short_player(x) for x in p1_tanks)
        if p2_cycle_reached and ss.get("castSec") is None:
            ss_note += " · P2 Soul Sever 도달 전"
    elif not p2_cycle_reached:
        ss_title = "P1 탱 브레스 오피격"
        ss_status = "unknown"
        ss_val = "로그 신호 없음"
        ss_note = "탱커/브레스 직접 피해 근거 부족"
    else:
        ss_status = "unknown"
        ss_val = "확인 불가"
        hit_players = [short_player(x) for x in (ss.get("playerHitPlayers") or [])]
        ss_note = ("피격 " + ", ".join(hit_players)) if hit_players else "raidPlan 또는 로스터 spec 필요"

    overlap = cycle.get("priorAddOverlap") or {}
    overlap_detected = bool(overlap.get("detected"))
    overlap_known = cycle.get("nextCycleSec") is not None
    overlap_status = "warning" if overlap_detected else "success" if overlap_known else "unknown"
    overlap_val = "다음 웨이브까지 생존" if overlap_detected else "없음" if overlap_known else (p2_not_applicable_value if not p2_cycle_reached else "확인 불가")
    overlap_note = "왜 남았는지는 미확정" if overlap_detected else (p2_not_applicable_note if not p2_cycle_reached else "이전 쫄 웨이브 중첩")

    handoff = obs.get("tankHandoff") or {}
    cleanup = str(handoff.get("p2EntryCleanupStatus") or "unknown")
    p2_orb_status = "success" if cleanup == "success" else "failed" if cleanup == "failed" else "unknown"
    p2_orb_count = handoff.get("p2EntryActiveOrbs")
    if p2_orb_count is not None:
        p2_orb_val = f"{_safe_int(p2_orb_count)}개"
    elif not handoff.get("p2Reached"):
        p2_orb_val = "P2 미진입"
    else:
        p2_orb_val = "확인 불가"
    p2_orb_note_bits = []
    residual = handoff.get("secondGuillotineResidual")
    if residual is not None:
        p2_orb_note_bits.append(f"2단두대 잔존 {_safe_int(residual)}개")
    sig = handoff.get("cleanupSignal") or {}
    if sig.get("method") == "bulk_rupture" and sig.get("removedOrbs") is not None:
        p2_orb_note_bits.append(f"{fmt_clock(sig.get('sec'))} 일괄 파열 {_safe_int(sig.get('removedOrbs'))}개")
    elif handoff.get("cleanupMethod"):
        p2_orb_note_bits.append(str(handoff.get("cleanupMethod")))
    if handoff.get("p2EntryBasis") == "malacrass_activation_fallback":
        p2_orb_note_bits.append("말라크라스 앵커 기준")
    if str(handoff.get("handoffLoadStatus") or "") == "warning":
        hmax = handoff.get("tankHandoffMax")
        p2_orb_note_bits.append(f"인계량 상한 {_safe_int(hmax)}개 초과" if hmax is not None else "인계량 경고")
    if not p2_orb_note_bits:
        p2_orb_note_bits.append(str(handoff.get("cleanupSummary") or "P2 진입 잔여 구슬 신호 확인 중"))
    p2_orb_note = " · ".join(p2_orb_note_bits)

    shields = [x for x in (obs.get("shieldCycles") or []) if isinstance(x, dict)]
    shield_panel_cycles = []
    for sh in shields:
        status = str(sh.get("status") or "unknown")
        align = sh.get("cooldownAlignment") or {}
        bits = []
        if sh.get("p2RelativeSec") is not None:
            bits.append(f"P2 {float(sh.get('p2RelativeSec')):+.1f}초")
        if sh.get("breakDurationSec") is not None:
            bits.append(f"파괴 {float(sh.get('breakDurationSec')):.1f}초")
        if sh.get("remainingPct") is not None and status == "failed":
            bits.append(f"잔여 {float(sh.get('remainingPct')):.1f}%")
        if sh.get("castSuccessSec") is not None:
            bits.append("영원한 일몰 완료 ✗")
        elif sh.get("interruptSec") is not None:
            bits.append(f"일몰 차단 {short_player(sh.get('interruptPlayer'))}")
        elif sh.get("wipeDuringCycle"):
            bits.append("사이클 중 전멸 · ?")
        else:
            bits.append("해결 신호 확인 중")

        align_summary = "raidPlan 미설정"
        if align.get("requirements"):
            reqs = len(align.get("requirements") or [])
            failures = len(align.get("failures") or [])
            align_summary = f"쿨기 {reqs - failures}/{reqs} · 미정렬 {failures}"
            pot = next((x for x in (align.get("requirements") or []) if x.get("kind") == "potion"), None)
            if pot:
                align_summary += f" · 물약 {_safe_int(pot.get('usedCount'))}/{_safe_int(pot.get('denominator'))}"
                if _safe_int(pot.get("deadExcludedCount")):
                    align_summary += f" (사망 {_safe_int(pot.get('deadExcludedCount'))}명 제외)"
        auto = align.get("autoSpecCooldowns") or {}
        auto_n = _safe_int(auto.get("denominator"))
        judge_n = _safe_int(auto.get("judgedDenominator"))
        observe_n = _safe_int(auto.get("observedDenominator"))
        auto_parts = []
        if judge_n:
            j_ok = _safe_int(auto.get("judgedSuccessCount"))
            j_late = _safe_int(auto.get("judgedLateCount"))
            j_miss = _safe_int(auto.get("judgedMissingCount"))
            j_unknown = _safe_int(auto.get("judgedUnknownCount"))
            text = f"계획 2분쿨 {j_ok}/{judge_n}"
            if j_late:
                text += f" · 지연 {j_late}"
            if j_miss:
                text += f" · 미사용 {j_miss}"
            if j_unknown:
                text += f" · 판정불가 {j_unknown}"
            auto_parts.append(text)
        if observe_n:
            o_ok = _safe_int(auto.get("observedSuccessCount"))
            o_late = _safe_int(auto.get("observedLateCount"))
            o_miss = _safe_int(auto.get("observedMissingCount"))
            text = f"자동 2분쿨 {o_ok}/{observe_n} · 관측"
            if o_late:
                text += f" · 지연 {o_late}"
            if o_miss:
                text += f" · 미사용 {o_miss}"
            auto_parts.append(text)
        # Backward-compatible fallback for old mechanic_review rows.
        if not auto_parts and auto_n:
            ok_n = _safe_int(auto.get("successCount"))
            late_n = _safe_int(auto.get("lateCount"))
            miss_n = _safe_int(auto.get("missingCount"))
            text = f"자동 2분쿨 {ok_n}/{auto_n}"
            if late_n:
                text += f" · 지연 {late_n}"
            if miss_n:
                text += f" · 미사용 {miss_n}"
            if str(auto.get("judgmentMode") or "observe") == "observe":
                text += " · 관측"
            auto_parts.append(text)
        if auto_parts:
            auto_text = " · ".join(auto_parts)
            align_summary = (align_summary + " · " + auto_text) if align_summary != "raidPlan 미설정" else auto_text
        elif auto.get("reason") == "friendly_cast_stream_unavailable":
            align_summary += " · 자동 2분쿨 ? 아군 시전 스트림 없음"
        elif auto.get("reason") == "catalog_unavailable":
            align_summary += " · 자동 2분쿨 ? 카탈로그 없음"
        if sh.get("scheduleDeviationSec") is not None:
            align_summary += f" · NSRT 편차 {float(sh.get('scheduleDeviationSec')):+.1f}초"
        if sh.get("causeClass"):
            align_summary += f" · 원인 {sh.get('causeClass')} {sh.get('causeLabel') or ''}".rstrip()
        auto_detail = []
        if auto_n:
            judged_rows = list(auto.get("judgedRequirements") or [])
            observed_rows = list(auto.get("observedRequirements") or [])
            j_missing = [x for x in judged_rows if x.get("status") == "missing"]
            j_late = [x for x in judged_rows if x.get("status") == "late"]
            j_unknown = [x for x in judged_rows if x.get("status") == "unknown"]
            o_missing = [x for x in observed_rows if x.get("status") == "missing"]
            o_late = [x for x in observed_rows if x.get("status") == "late"]
            if j_missing:
                auto_detail.append("계획 미사용 " + ", ".join(f"{short_player(x.get('player'))}:{x.get('label') or x.get('spellID')}" for x in j_missing[:6]))
            if j_late:
                auto_detail.append("계획 지연 " + ", ".join(f"{short_player(x.get('player'))}:{x.get('label') or x.get('spellID')} {float(x.get('deviationSec') or 0):+.1f}초" for x in j_late[:6]))
            if j_unknown:
                auto_detail.append("계획 판정불가 " + ", ".join(f"{short_player(x.get('player'))}:{x.get('label') or x.get('spellID')}" for x in j_unknown[:6]))
            if o_missing:
                auto_detail.append("관측 미사용 " + ", ".join(f"{short_player(x.get('player'))}:{x.get('label') or x.get('spellID')}" for x in o_missing[:6]))
            if o_late:
                auto_detail.append("관측 지연 " + ", ".join(f"{short_player(x.get('player'))}:{x.get('label') or x.get('spellID')} {float(x.get('deviationSec') or 0):+.1f}초" for x in o_late[:6]))
            target_unset = [x for x in observed_rows if x.get("targetAssignmentRequired") and x.get("targetPlanMissing")]
            if target_unset:
                auto_detail.append("대상 계획 미설정 " + ", ".join(f"{short_player(x.get('player'))}:{x.get('label') or x.get('spellID')}" for x in target_unset[:6]))
            if auto.get("scope"):
                auto_detail.append(str(auto.get("scope")))
        shield_panel_cycles.append({
            "index": _safe_int(sh.get("index"), 1),
            "status": status,
            "summary": " · ".join(bits),
            "cooldownStatus": str(align.get("status") or "unknown"),
            "cooldownSummary": align_summary,
            "autoCooldownStatus": str((align.get("autoSpecCooldowns") or {}).get("status") or "unknown"),
            "autoCooldownScope": (align.get("autoSpecCooldowns") or {}).get("scope"),
            "autoCooldownRequirements": list((align.get("autoSpecCooldowns") or {}).get("requirements") or []),
            "autoJudgeRequirements": list((align.get("autoSpecCooldowns") or {}).get("judgedRequirements") or []),
            "autoObserveRequirements": list((align.get("autoSpecCooldowns") or {}).get("observedRequirements") or []),
            "autoCooldownDetail": " · ".join(auto_detail),
            "startSec": sh.get("startSec"),
        })

    if shield_panel_cycles:
        shield_plan = obs.get("shieldObservationPlan") or {}
        shield_panel = {
            "status": "failed" if any(x["status"] == "failed" for x in shield_panel_cycles) else
                      "warning" if any(x["status"] == "warning" for x in shield_panel_cycles) else
                      "success" if all(x["status"] == "success" for x in shield_panel_cycles) else "unknown",
            "cycles": shield_panel_cycles,
            "summary": f"보호막 {len(shield_panel_cycles)}회 관측",
            "plannedSlots": [x for x in (shield_plan.get("slots") or []) if isinstance(x, dict)],
            "verificationFields": list(shield_plan.get("verificationFields") or []),
            "anchorSec": shield_plan.get("anchorSec"),
            "anchorBasis": shield_plan.get("anchorBasis"),
        }
    else:
        shield_plan = obs.get("shieldObservationPlan") or {}
        planned_slots = [x for x in (shield_plan.get("slots") or []) if isinstance(x, dict)]
        first_slot = next((x for x in planned_slots if _safe_int(x.get("index"), 0) == 1), {})
        shield_anchor_reached = bool(shield_plan.get("anchorSec") is not None)
        if shield_anchor_reached and first_slot.get("state") == "awaiting" and first_slot.get("timeToTargetAtEndSec") is not None:
            summary = (
                f"보호막 관측 대기 · #1 P2+{_safe_int(first_slot.get('targetOffsetSec'), 70)}초까지 "
                f"약 {float(first_slot.get('timeToTargetAtEndSec')):.1f}초 전 종료"
            )
        elif shield_anchor_reached and first_slot.get("state") == "target_passed_unobserved":
            summary = "보호막 예상 구간 통과 · 1286912 신호 미관측(스트림/실제 일정 확인)"
        else:
            summary = "보호막 도달 전 종료 또는 1286912 신호 미관측" if shield_anchor_reached else "P2 미진입 · 보호막 판정 불가"
        shield_panel = {
            "status": "unknown",
            "cycles": [],
            "summary": summary,
            "plannedSlots": planned_slots,
            "verificationFields": list(shield_plan.get("verificationFields") or []),
            "anchorSec": shield_plan.get("anchorSec"),
            "anchorBasis": shield_plan.get("anchorBasis"),
        }

    cards = [
        _card("p2_entry", "P2 진입", entry_status, entry_val, entry_note, raw_status=entry_raw_status, evidence_level="verified" if phase.get("entrySec") is not None else None),
        _card("p1_orbs", "P1 구슬 처리", orb_status, orb_val, orb_note),
        _card("mind_control", "정배", "unknown", mc_val, mc_note, raw_status=("observed" if mc_count is not None else ("observed" if not p2_cycle_reached else None)), evidence_level=("candidate" if mc_count is not None else None)),
        _card("gravebound", "영혼 3개", grave_status, grave_val, grave_note,
              raw_status=(None if grave_judged else ("observed" if grave_targets or not p2_cycle_reached else None)),
              evidence_level=("verified" if grave_judged else ("candidate" if grave_targets else None))),
        _card("gloombomb_add", "암영폭탄 → 쫄 뎀감 해제", bomb_status, bomb_val, bomb_note),
        _card("gloombomb_extra", "폭탄 같이 맞음", extra_status, extra_val, extra_note),
        _card("wail", "통곡 차단", wail_status, wail_val, wail_note),
        _card("manifest", "현신 접촉", collision_status, collision_val, (p2_not_applicable_note if not p2_cycle_reached else ("Malevolent Resonance" if collision_known or collision_n else "접촉 0회를 성공으로 승격하지 않음")), raw_status=("observed" if p2_cycle_reached and not collision_known and not collision_n else None)),
        _card("soul_sever", ss_title, ss_status, ss_val, ss_note),
        _card("prior_add", "이전 쫄 중첩", overlap_status, overlap_val, overlap_note),
        _card("p2_entry_orbs", "P2 진입 잔여 구슬", p2_orb_status, p2_orb_val, p2_orb_note),
    ]

    # B37.4.30: the chart must preserve the engine's mechanic semantics.
    # Start from review.timeline (authoritative status/evidence) and add only
    # observation anchors needed for the human-readable flow.
    result = str(row.get("result") or (review.get("encounter") or {}).get("result") or "").upper()
    timeline: list[dict] = []
    for raw in (review.get("timeline") or []):
        if not isinstance(raw, dict) or raw.get("sec") is None:
            continue
        kind = str(raw.get("kind") or "")
        if kind == "candidate_pulse":
            continue
        item = dict(raw)
        item["label"] = str(item.get("label") or item.get("title") or kind or "기믹")
        if item.get("meaningEvidenceLevel") and not item.get("evidenceLevel"):
            item["evidenceLevel"] = item.get("meaningEvidenceLevel")
        role, role_label = _timeline_judgment_role(item, result=result, first=first, direct=direct)
        if role:
            item["failureRole"], item["failureRoleLabel"] = role, role_label
        timeline.append(item)

    # P2 UI observations are not all emitted into review.timeline yet.  Copy the
    # already-computed observation values; do not create new judgments here.
    if phase.get("entrySec") is not None:
        timeline.append({"kind": "p2_entry", "sec": float(phase["entrySec"]), "label": "P2 진입", "status": "unknown", "evidenceLevel": phase.get("evidenceLevel")})
    if gb.get("firstInterruptSec") is not None:
        who = short_player(gb.get("firstInterruptPlayer"))
        timeline.append({"kind": "interrupt", "sec": float(gb["firstInterruptSec"]), "label": f"1차단 · {who}" if who else "1차단", "status": "success" if gb.get("firstInterruptOrderOk") is True else "unknown", "evidenceLevel": "verified"})
    if gb.get("damageSec") is not None:
        label = "암영폭탄 폭발"
        if extra > 0:
            label += f" · 추가피격 {extra}명"
        timeline.append({"kind": "gloombomb", "sec": float(gb["damageSec"]), "label": label, "status": bomb_status, "evidenceLevel": "verified" if bomb_status != "unknown" else None})
    for sec in completed_secs:
        after_collapse = _safe_float(collapse_meta.get("collapseSec")) is not None and float(sec) >= float(collapse_meta.get("collapseSec"))
        timeline.append({
            "kind": "wail", "sec": float(sec), "label": "공포의 통곡 완주시전",
            "status": "unknown" if (after_collapse and not wfail) else ("failed" if wfail else "unknown"),
            "evidenceLevel": "verified",
            "summary": "붕괴 이후 시전 · 플레이어 공포(1286399) 미관측" if (after_collapse and not wfail) else "",
        })
    for ep in ((cycle.get("manifestCollision") or {}).get("episodes") or []):
        if ep.get("sec") is not None:
            players = [short_player(x) for x in (ep.get("players") or [])]
            deaths = _safe_int(ep.get("linkedDeathCount"))
            label = "현신 접촉"
            if players:
                label += " · " + ", ".join(players)
            item = {"kind": "manifest", "sec": float(ep["sec"]), "label": label, "status": "failed", "evidenceLevel": "verified", "summary": f"연계사망 {deaths}명" if deaths else ""}
            role, role_label = _timeline_judgment_role(item, result=result, first=first, direct=direct)
            if role:
                item["failureRole"], item["failureRoleLabel"] = role, role_label
            timeline.append(item)
    for sh in shields:
        if sh.get("startSec") is not None:
            timeline.append({"kind": "shield", "sec": float(sh["startSec"]), "label": f"보호막 {sh.get('index')}", "status": "unknown"})
        if sh.get("breakSec") is not None:
            timeline.append({"kind": "shield", "sec": float(sh["breakSec"]), "label": f"보호막 {sh.get('index')} 파괴", "status": "success", "evidenceLevel": "verified"})
        if sh.get("castSuccessSec") is not None:
            item = {"kind": "nightfall", "sec": float(sh["castSuccessSec"]), "label": "영원한 일몰 완료", "status": "failed", "evidenceLevel": "verified"}
            role, role_label = _timeline_judgment_role(item, result=result, first=first, direct=direct)
            if role:
                item["failureRole"], item["failureRoleLabel"] = role, role_label
            timeline.append(item)

    # Fold a same-second P1 judgment into its physical mechanic row.  Example:
    # Try 8 breath #6 stays on the Breath row but carries first-mistake styling.
    failures_by_sec = [x for x in timeline if str(x.get("status") or "") == "failed"]
    for anchor in timeline:
        if str(anchor.get("kind") or "") != "breath":
            continue
        for failure in failures_by_sec:
            if str(failure.get("kind") or "") != "p1_orb_failure":
                continue
            try:
                same = abs(float(anchor.get("sec")) - float(failure.get("sec"))) <= 0.12
            except (TypeError, ValueError):
                same = False
            if not same:
                continue
            anchor.update({
                "status": "failed",
                "rawStatus": "failed",
                "evidenceLevel": failure.get("meaningEvidenceLevel") or failure.get("evidenceLevel") or "verified",
                "failureRole": failure.get("failureRole"),
                "failureRoleLabel": failure.get("failureRoleLabel"),
                "summary": failure.get("summary") or anchor.get("summary") or "",
                "judgmentKind": failure.get("kind"),
            })
    # Remove the folded duplicate failure row, then remove exact UI duplicates.
    timeline = [x for x in timeline if not (str(x.get("kind") or "") == "p1_orb_failure" and any(str(b.get("kind") or "") == "breath" and abs(float(b.get("sec") or -999) - float(x.get("sec") or 999)) <= 0.12 and str(b.get("status") or "") == "failed" for b in timeline))]
    timeline = _timeline_dedupe(timeline)
    timeline.sort(key=lambda x: float(x.get("sec") or 0))

    evidence = []
    for ep in ((cycle.get("manifestCollision") or {}).get("episodes") or []):
        spatial = ep.get("spatial") or {}
        pair = [short_player(x) for x in (spatial.get("pairPlayers") or ep.get("players") or [])]
        deaths = [short_player((d or {}).get("player")) for d in (ep.get("linkedDeaths") or []) if isinstance(d, dict)]
        movement = spatial.get("movement") or {}
        movement_text = []
        for full_name, mv in movement.items():
            movement_text.append(f"{short_player(full_name)} {_safe_float((mv or {}).get('pathDistance'), 0):.1f}야드")
        start_d = spatial.get("pairDistanceTraceStart")
        end_d = spatial.get("pairDistanceTraceEnd")
        evidence.append({
            "sec": ep.get("sec"),
            "title": "Malevolent Resonance",
            "pair": pair,
            "deaths": deaths,
            "distanceStart": start_d,
            "distanceEnd": end_d,
            "movement": movement_text,
            "evidenceLevel": "candidate" if spatial else "verified",
            "note": "좌표는 유령 위치의 대리값 · 개인 책임 확정 근거 아님" if spatial else "현신 접촉 자체는 검증됨",
        })

    first_text = "검증된 실패 없음"
    if first:
        first_text = f"{first.get('label') or '실패'}"
        if first.get("summary"):
            first_text += f" · {first.get('summary')}"
        if first.get("sec") is not None:
            first_text += f" ({fmt_clock(first.get('sec'))})"

    major_text = "주요 사고 없음"
    if major:
        major_text = str(major.get("summary") or major.get("label") or "주요 사고")
        if major.get("sec") is not None and "(" not in major_text:
            major_text += f" ({fmt_clock(major.get('sec'))})"

    result = str(row.get("result") or (review.get("encounter") or {}).get("result") or "").upper()
    if direct and collapse_meta.get("collapseBasis") == "b31":
        collapse_text = str(collapse_meta.get("collapseSummary") or f"{direct.get('label') or '검증된 실패'} → B31 붕괴 직접 연결")
        collapse_status = "failed"
    elif direct:
        collapse_text = f"{direct.get('label') or '검증된 실패'} → 1초 내 {_safe_int(collapse_meta.get('collapseDeathCount1s'))}명 사망"
        collapse_status = "failed"
    elif collapse_meta.get("collapseBasis") == "b31":
        collapse_text = str(collapse_meta.get("collapseSummary") or "B31 붕괴 묶음 · 단일 원인 미확정")
        collapse_status = "unknown"
    elif collapse_meta.get("collapseSec") is not None:
        collapse_text = (
            f"원인 확인 불가 · {fmt_clock(collapse_meta.get('collapseSec'))} · "
            f"1초 안 {_safe_int(collapse_meta.get('collapseDeathCount1s'))}명 사망"
        )
        preceding = collapse_meta.get("collapsePreceding") or {}
        if isinstance(preceding, dict) and preceding.get("sec") is not None:
            collapse_text += (
                f" · 직전 실패: {preceding.get('label') or preceding.get('summary') or preceding.get('kind') or '실패'} "
                f"{fmt_clock(preceding.get('sec'))} — 선후관계만 확인"
            )
        collapse_status = "unknown"
    elif result == "KILL":
        collapse_text = "킬 성공 · 공대 붕괴 없음"
        collapse_status = "success"
    else:
        collapse_text = "확인 불가 · 1초 안 4명 이상 사망 없음"
        collapse_status = "unknown"

    footer_bits = []
    if expected and known >= expected:
        footer_bits.append(f"폭탄 쫄 적중 {removed}/{expected}")
    if wail_known:
        footer_bits.append(f"통곡 미차단 {wfail}회")
    footer = " · ".join(footer_bits)
    next_pull = _next_pull(first, major)
    if footer:
        footer += f" → 다음 풀: {next_pull}"
    else:
        footer = f"다음 풀: {next_pull}"

    enc = review.get("encounter") or {}
    return {
        "supported": True,
        "tryNo": row.get("tryNo") if row.get("tryNo") is not None else enc.get("tryNo"),
        "durationSec": row.get("durationSec") if row.get("durationSec") is not None else enc.get("durationSec"),
        "cycleCount": _safe_int(p2.get("cycleCount")),
        "focusCycleIndex": focus_cycle_index,
        "firstMistake": first,
        "firstMistakeText": first_text,
        "majorIncident": major,
        "majorIncidentText": major_text,
        "collapseDirect": direct,
        "collapseSec": collapse_meta.get("collapseSec"),
        "collapseDeathCount1s": _safe_int(collapse_meta.get("collapseDeathCount1s")),
        "collapseDeathCountB31": _safe_int(collapse_meta.get("collapseDeathCountB31")),
        "collapseBasis": collapse_meta.get("collapseBasis"),
        "collapsePreceding": collapse_meta.get("collapsePreceding"),
        "deathEvents": list(((obs.get("collapseSnapshot") or {}).get("collapseDeaths") or [])),
        "collapseText": collapse_text,
        "collapseStatus": collapse_status,
        "footer": footer,
        "nextPull": next_pull,
        "cards": cards,
        "shieldPanel": shield_panel,
        "timeline": timeline,
        "evidence": evidence,
        "evidenceText": "카드 상세\n" + _card_detail_text(cards),
    }


def _strategy_breaths(review: dict) -> list[dict]:
    out = []
    for b in ((review.get("observations") or {}).get("breaths") or []):
        if b.get("strategyApplies") and b.get("strategySlot"):
            out.append(b)
    return out


def progression_stage(review: dict, duration_sec: Any = None) -> tuple[str, int]:
    obs = review.get("observations") or {}
    phase = obs.get("phase1Transition") or {}
    if phase.get("entrySec") is not None:
        return "P2 도달", 4
    duration = _safe_float(duration_sec)
    if duration is None:
        duration = _safe_float((review.get("encounter") or {}).get("durationSec"), 0.0)
    # Reference anchors from the verified Coiled Altar P1 pattern.
    if duration < 23.0:
        return "1차 브레스 전", 0
    if duration < 108.0:
        return "1단두대 후 P1 중반", 1
    if duration < 128.0:
        return "2단두대 전 브레스", 2
    return "2단두대 이후", 3


def build_board_row(review: dict, row: dict | None = None) -> dict:
    row = row or {}
    enc = review.get("encounter") or {}
    duration = row.get("durationSec") if row.get("durationSec") is not None else enc.get("durationSec")
    stage, stage_idx = progression_stage(review, duration)
    orb_groups: dict[int, list[str]] = {}
    for b in _strategy_breaths(review):
        assignment = b.get("strategyAssignment") or {}
        guillotine_idx = _safe_int(assignment.get("guillotineIndex"), 0)
        if not guillotine_idx:
            continue
        val = b.get("destroyedOrbs")
        orb_groups.setdefault(guillotine_idx, []).append("?" if val is None else str(int(val)))
    orbs = " │ ".join("·".join(vals) for _, vals in sorted(orb_groups.items())) or "-"

    model = build_try_priority_model(review, row)
    collapse = ((review.get("observations") or {}).get("collapseSnapshot") or {})
    key_failure = ""
    if model.get("majorIncident"):
        key_failure = model["majorIncidentText"]
    elif model.get("firstMistake"):
        key_failure = model["firstMistakeText"]
    else:
        verdict = review.get("verdict") or {}
        key_failure = str(verdict.get("primary") or "원인 확인 중")
    collapse_text = ""
    if collapse.get("significantCluster"):
        collapse_text = f"붕괴 {fmt_clock(collapse.get('collapseSec'))} · 1초 {_safe_int(collapse.get('collapseCluster1s'))}명"
    elif str((row.get("result") or enc.get("result") or "")).upper() == "KILL":
        collapse_text = "킬"
    return {
        "tryNo": row.get("tryNo") if row.get("tryNo") is not None else enc.get("tryNo"),
        "durationSec": duration,
        "duration": fmt_clock(duration),
        "stage": stage,
        "stageIndex": stage_idx,
        "orbs": orbs,
        "keyFailure": key_failure,
        "collapse": collapse_text,
        "result": row.get("result") or enc.get("result") or "-",
        "review": review,
    }


def build_board_summary(items: Iterable[tuple[dict, dict]]) -> dict:
    items = canonicalize_review_pairs(items)
    board_rows = []
    first_low = 0
    first_samples = 0
    high_breath = 0
    g1_residual = []
    g2_residual = []
    g1_samples = 0
    g2_samples = 0
    carryover = []
    g2_values: dict[Any, int] = {}
    stages = [0, 0, 0, 0, 0]
    for row, review in items:
        if not is_coiled_review(review):
            continue
        br = build_board_row(review, row)
        board_rows.append(br)
        stages[min(4, max(0, int(br["stageIndex"])))]+=1
        for b in _strategy_breaths(review):
            if b.get("judgmentExcluded"):
                continue
            val = b.get("destroyedOrbs")
            if val is None:
                continue
            val = int(val)
            if b.get("strategyJudgmentEnabled") and b.get("strategySlot") == "wave1_breath1":
                first_samples += 1
                if val < int(b.get("goalMin") or 6):
                    first_low += 1
            if b.get("strategyJudgmentEnabled") and val >= 7:
                high_breath += 1
        snap = _orb_guillotine_snapshot(review)
        first_g = snap.get("first") or {}
        second_g = snap.get("second") or {}
        if first_g and not first_g.get("judgmentExcluded") and first_g.get("remainingOrbs") is not None:
            g1_samples += 1
            if _safe_int(first_g.get("remainingOrbs")) > 0:
                g1_residual.append(br.get("tryNo"))
        if second_g and not second_g.get("judgmentExcluded") and second_g.get("remainingOrbs") is not None:
            g2_samples += 1
            g2n = _safe_int(second_g.get("remainingOrbs"))
            if g2n > 0:
                g2_residual.append(br.get("tryNo"))
                g2_values[br.get("tryNo")] = g2n
        if snap.get("carryover"):
            carryover.append(br.get("tryNo"))
    board_rows.sort(key=lambda x: (_safe_int(x.get("tryNo")), _safe_float(x.get("durationSec"), 0)))

    orders = []
    if first_low:
        orders.append(f"첫 브레스 6~7개. 현재 목표 미달 {first_low}/{first_samples or '?'}트")
    if high_breath:
        orders.append(f"짝수 브레스 7개 이상 {high_breath}회. 뒤 단두대 대비 중첩 낮추기")
    if g1_residual:
        orders.append(f"1차 단두대 직전 잔존 0 확인 · {len(g1_residual)}/{g1_samples or '?'}트 잔존")
    if carryover:
        orders.append(f"다음 웨이브 이월 0 확인 · {len(carryover)}트 이월")
    elif g2_residual:
        orders.append("2차 단두대 잔존은 관측 경고로만 유지 · 붕괴 원인 승격 금지")
    if not orders:
        orders.append("현재 검증된 반복 실패 없음 · 다음 구간 진행 확인")
    return {
        "rows": board_rows,
        "count": len(board_rows),
        "stageCounts": stages,
        "firstBreathLow": first_low,
        "firstBreathSamples": first_samples,
        "highBreath": high_breath,
        "guillotine1Samples": g1_samples,
        "guillotine1ResidualTryNos": g1_residual,
        "guillotine2Samples": g2_samples,
        "guillotine2ResidualTryNos": g2_residual,
        "guillotine2ResidualValues": g2_values,
        "nextWaveCarryoverTryNos": carryover,
        # Compatibility alias: from B36.4.2 this means actual next-wave
        # carryover only, never "any guillotine residual".
        "carryoverTryNos": carryover,
        "voiceOrders": orders[:3],
    }


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _dedupe_reviews(reviews: Iterable[dict]) -> list[dict]:
    """Deduplicate saved WCL/local mechanic reviews by physical pull identity.

    Use the same Mythic 5=16 normalization and 1.5-second start-time window as
    the board/navigation layer.  Local raw wins when both representations are
    available so coordinate evidence is not lost.
    """
    selected: list[tuple[dict, dict]] = []
    passthrough: list[dict] = []
    for review in reviews:
        if not is_coiled_review(review):
            continue
        enc = dict(review.get("encounter") or {})
        if not enc.get("startTimestamp"):
            passthrough.append(review)
            continue
        pseudo = {
            "encounterID": enc.get("encounterID"),
            "difficultyID": enc.get("difficultyID"),
            "startTimestamp": enc.get("startTimestamp"),
            "tryNo": enc.get("tryNo"),
            "sourceKind": review.get("sourceKind"),
            "rawLog": review.get("rawLog"),
        }
        match = next((i for i, (old_row, _old_review) in enumerate(selected) if same_physical_pull(old_row, pseudo)), None)
        if match is None:
            selected.append((pseudo, review))
            continue
        old_row, old_review = selected[match]
        if _row_preference_score(pseudo) > _row_preference_score(old_row):
            selected[match] = (pseudo, review)
        else:
            selected[match] = (old_row, old_review)
    out = [review for _row, review in selected] + passthrough
    out.sort(key=lambda r: (_row_start_epoch(r.get("encounter") or {}) or 0.0, _safe_int((r.get("encounter") or {}).get("tryNo"))))
    return out


def build_briefing_model(reviews: Iterable[dict]) -> dict:
    reviews = _dedupe_reviews(reviews)
    source = reviews[-1] if reviews else {}
    strategy = ((source.get("strategy") or {}).get("values") or {})
    goals = strategy.get("breathGoals") or {}
    defaults = {
        "wave1_breath1": {"label": "1차 브레스", "min": 6, "max": 7},
        "wave1_breath2": {"label": "2차 · 단두대 직전", "min": 3, "max": 4},
        "wave2_breath1": {"label": "2바닥 후 1차", "min": 6, "max": 8},
        "wave2_breath2": {"label": "2바닥 후 2차", "min": 4, "max": 6},
    }
    values: dict[str, list[float]] = {k: [] for k in defaults}
    for review in reviews:
        for b in _strategy_breaths(review):
            slot = str(b.get("strategySlot") or "")
            if slot in values and b.get("destroyedOrbs") is not None and not b.get("judgmentExcluded"):
                values[slot].append(float(b["destroyedOrbs"]))
    cards = []
    for slot, default in defaults.items():
        goal = goals.get(slot) or default
        med = _median(values[slot])
        cards.append({
            "slot": slot,
            "label": str(goal.get("label") or default["label"]),
            "target": f"{_safe_int(goal.get('min'), default['min'])}~{_safe_int(goal.get('max'), default['max'])}",
            "median": med,
            "samples": len(values[slot]),
        })
    raid_plan = strategy.get("raidPlan") or {}
    plan = raid_plan.get("phase2") or {}
    tank_handoff_plan = raid_plan.get("orbHandoff") or raid_plan.get("tankHandoff") or {}
    shield_windows = [x for x in (plan.get("shieldWindows") or []) if isinstance(x, dict)]
    assignments = plan.get("interruptAssignments") or {}
    interrupt_names = []
    for key in ("unmarked", "marked"):
        for name in assignments.get(key) or []:
            if str(name).strip() and str(name).strip() not in interrupt_names:
                interrupt_names.append(str(name).strip())
    tanks = [str(x) for x in (plan.get("tankPlayers") or []) if str(x).strip()]
    tank_source = "manual_raidplan" if tanks else "unavailable"
    if not tanks:
        for review in reversed(reviews):
            for cycle in _cycles(review):
                ss = cycle.get("soulSever") or {}
                inferred = ss.get("tankPlayersEffective") or ss.get("tankPlayersInferred") or []
                if inferred:
                    tanks = [str(x) for x in inferred if str(x).strip()]
                    tank_source = str(ss.get("tankResolutionSource") or "roster_spec")
                    break
            if tanks:
                break
    auto_cfg = dict(plan.get("autoSpecCooldowns") or {})
    policies = [x for x in (auto_cfg.get("entryPolicies") or []) if isinstance(x, dict)]
    judge_policies = [dict(x) for x in policies if str(x.get("mode") or "observe").strip().lower() == "judge"]
    observe_policies = [dict(x) for x in policies if str(x.get("mode") or "observe").strip().lower() == "observe"]
    return {
        "cards": cards,
        "p2Target": "2:45 ~ 2:50",
        "interruptNames": interrupt_names,
        "tanks": tanks,
        "tankSource": tank_source,
        "bombRole": str(plan.get("gloombombAssignments") or "근딜 2 + 원딜 2"),
        "tankHandoffMax": tank_handoff_plan.get("tankHandoffMax"),
        "tankHandoffNote": str(tank_handoff_plan.get("note") or "2차 단두대 이후 잔여 구슬은 P2 진입 전 정리"),
        "shieldWindows": shield_windows,
        "autoSpecCooldowns": auto_cfg,
        "autoJudgePolicies": judge_policies,
        "autoObservePolicies": observe_policies,
        "autoJudgeCount": len(judge_policies),
        "autoObserveCount": len(observe_policies),
        "opening": [
            "관문 → 해골 징표 집결",
            "정배 대상 확인",
            "공포의 통곡 1차단 확인 → 바로 암영폭탄 영혼살무사 적중",
            "이후 통곡 차단 순서대로 · 성공 시 현신 잠깐 보임",
            "탱 브레스로 현신 제거",
            "현신끼리 절대 붙지 않게 · 접촉 = 치명적 실패",
        ],
        "reviewCount": len(reviews),
    }

def _phase2_relative_anchor(review: dict) -> tuple[float | None, str]:
    obs = review.get("observations") or {}
    transition = obs.get("phase1Transition") or {}
    if transition.get("entrySec") is not None:
        return _safe_float(transition.get("entrySec")), "P2 진입"
    p2 = obs.get("phase2") or {}
    if p2.get("activationSec") is not None:
        return _safe_float(p2.get("activationSec")), "말라크라스 활성 앵커"
    return None, "P2 기준 없음"


def _relative_clock(sec: Any) -> str:
    value = _safe_float(sec)
    if value is None:
        return "-"
    sign = "+" if value >= 0 else "-"
    value = abs(value)
    m = int(value // 60)
    s = value - m * 60
    return f"{sign}{m}:{s:04.1f}"


def _p2_milestones(review: dict) -> list[dict]:
    anchor, _basis = _phase2_relative_anchor(review)
    if anchor is None:
        return []
    out: list[dict] = []
    for cycle in _cycles(review):
        idx = _safe_int(cycle.get("index"), 0)
        if cycle.get("startSec") is not None:
            out.append({"kind": "cycle", "label": f"{idx}사이클 영혼살무사", "sec": _safe_float(cycle.get("startSec"))})
        gb = cycle.get("gloombomb") or {}
        if gb.get("damageSec") is not None:
            extra = _safe_int(gb.get("extraHitCount"))
            label = f"{idx}사이클 암영폭탄 폭발"
            if extra:
                label += f" · 추가피격 {extra}명"
            out.append({"kind": "gloombomb", "label": label, "sec": _safe_float(gb.get("damageSec"))})
        wail = cycle.get("wail") or {}
        for j, intr in enumerate(wail.get("interrupts") or [], 1):
            if intr.get("sec") is not None:
                player = short_player(intr.get("player"))
                out.append({"kind": "interrupt", "label": f"{idx}사이클 통곡 차단 {j} · {player}", "sec": _safe_float(intr.get("sec"))})
        for j, ep in enumerate(wail.get("failureEpisodes") or [], 1):
            if ep.get("sec") is not None:
                out.append({"kind": "wail_fail", "label": f"{idx}사이클 통곡 미차단 {j}", "sec": _safe_float(ep.get("sec"))})
        for j, ep in enumerate((cycle.get("manifestCollision") or {}).get("episodes") or [], 1):
            if ep.get("sec") is not None:
                out.append({"kind": "manifest", "label": f"{idx}사이클 현신 접촉 {j}", "sec": _safe_float(ep.get("sec"))})
        ss = cycle.get("soulSever") or {}
        if ss.get("castSec") is not None:
            out.append({"kind": "soul_sever", "label": f"{idx}사이클 Soul Sever", "sec": _safe_float(ss.get("castSec"))})
    for sh in (((review.get("observations") or {}).get("shieldCycles") or [])):
        if not isinstance(sh, dict):
            continue
        si = _safe_int(sh.get("index"), 0)
        if sh.get("startSec") is not None:
            label = f"보호막 {si} 시작"
            if sh.get("targetOffsetSec") is not None:
                label += f" · 목표 +{float(sh.get('targetOffsetSec')):.0f}s"
            out.append({"kind": "shield", "label": label, "sec": _safe_float(sh.get("startSec"))})
        if sh.get("breakSec") is not None:
            out.append({"kind": "shield_break", "label": f"보호막 {si} 파괴", "sec": _safe_float(sh.get("breakSec"))})
        if sh.get("interruptSec") is not None:
            out.append({"kind": "nightfall_interrupt", "label": f"영원한 일몰 {si} 차단 · {short_player(sh.get('interruptPlayer'))}", "sec": _safe_float(sh.get("interruptSec"))})
        if sh.get("castSuccessSec") is not None:
            out.append({"kind": "nightfall_fail", "label": f"영원한 일몰 {si} 완료 ✗", "sec": _safe_float(sh.get("castSuccessSec"))})
    out = [x for x in out if x.get("sec") is not None]
    for item in out:
        item["relativeSec"] = round(float(item["sec"]) - float(anchor), 3)
        item["relative"] = _relative_clock(item["relativeSec"])
    out.sort(key=lambda x: float(x.get("sec") or 0))
    return out


def build_p2_relative_compare(
    current_review: dict,
    recent_kill_review: dict | None = None,
    current_row: dict | None = None,
    recent_kill_row: dict | None = None,
) -> dict:
    """Compare P2 timing after rebasing each pull's P2 entry to 0:00."""
    current_row = current_row or {}
    recent_kill_row = recent_kill_row or {}
    current_anchor, current_basis = _phase2_relative_anchor(current_review or {})
    kill_anchor, kill_basis = _phase2_relative_anchor(recent_kill_review or {}) if recent_kill_review else (None, "P2 기준 없음")
    current_events = _p2_milestones(current_review or {})
    kill_events = _p2_milestones(recent_kill_review or {}) if recent_kill_review else []
    return {
        "currentTryNo": current_row.get("tryNo") or ((current_review.get("encounter") or {}).get("tryNo") if current_review else None),
        "killTryNo": recent_kill_row.get("tryNo") or (((recent_kill_review or {}).get("encounter") or {}).get("tryNo") if recent_kill_review else None),
        "currentAnchorSec": current_anchor,
        "killAnchorSec": kill_anchor,
        "currentBasis": current_basis,
        "killBasis": kill_basis,
        "currentEvents": current_events,
        "killEvents": kill_events,
        "comparable": current_anchor is not None and kill_anchor is not None,
        "note": "P1 길이는 절대시간으로 유지하고, P2 이벤트는 각 트라이의 P2 진입을 0:00으로 맞춘 상대시간입니다.",
    }



def build_shield_throughput_compare(
    current_review: dict,
    candidate_pairs: Iterable[tuple[dict, dict]],
    current_row: dict | None = None,
) -> dict:
    """B36.5 D-stage comparative evidence for a failed shield.

    This does not override the engine's within-pull A/B/C classification.  It
    supplies the next comparison layer with an explicit scope+n, preferring the
    user's own kills, then own successful shield tries, then same-session
    observed shield samples. External top logs remain a later fallback.
    """
    current_row = current_row or {}
    current_cycles = [x for x in (((current_review or {}).get("observations") or {}).get("shieldCycles") or []) if isinstance(x, dict)]
    failed = next((x for x in current_cycles if str(x.get("status") or "") == "failed"), None)
    if not failed:
        return {"eligible": False, "reason": "failed_shield_absent"}
    align = failed.get("cooldownAlignment") or {}
    if str(align.get("status") or "") != "success":
        return {"eligible": False, "reason": "cooldown_alignment_not_success", "causeClass": failed.get("causeClass")}
    if failed.get("deathsDuring"):
        return {"eligible": False, "reason": "execution_loss_present", "causeClass": failed.get("causeClass")}
    current_rate = _safe_float(failed.get("absorbedPerSec"))
    if current_rate is None:
        return {"eligible": False, "reason": "current_throughput_unavailable", "causeClass": failed.get("causeClass")}
    shield_index = _safe_int(failed.get("index"), 1)

    pairs = canonicalize_review_pairs(candidate_pairs)
    current_start = _row_start_epoch(current_row) if current_row else None
    pools = {"recent_kill": [], "successful_try": [], "same_session": []}
    for row, review in pairs:
        if current_row and same_physical_pull(row, current_row):
            continue
        epoch = _row_start_epoch(row)
        if current_start is not None and epoch is not None and epoch >= current_start:
            continue
        cycles = [x for x in (((review or {}).get("observations") or {}).get("shieldCycles") or []) if isinstance(x, dict)]
        cycle = next((x for x in cycles if _safe_int(x.get("index"), 0) == shield_index), None)
        if not cycle:
            continue
        rate = _safe_float(cycle.get("absorbedPerSec"))
        if rate is None:
            continue
        sample = {
            "tryNo": row.get("tryNo") or ((review.get("encounter") or {}).get("tryNo")),
            "rate": rate,
            "status": cycle.get("status"),
            "result": str(row.get("result") or (review.get("encounter") or {}).get("result") or "").upper(),
            "epoch": epoch,
        }
        pools["same_session"].append(sample)
        if sample["result"] == "KILL" and str(cycle.get("status") or "") == "success":
            pools["recent_kill"].append(sample)
        elif str(cycle.get("status") or "") == "success":
            pools["successful_try"].append(sample)

    source = None
    label = None
    if pools["recent_kill"]:
        source, label = "recent_kill", "최근 킬"
    elif pools["successful_try"]:
        source, label = "successful_try", "보호막 성공 트라이"
    elif pools["same_session"]:
        source, label = "same_session", "같은 세션"
    if source is None:
        return {
            "eligible": True,
            "status": "unknown",
            "causeClass": "D",
            "scope": "비교 표본 없음 · n=0",
            "currentRate": current_rate,
            "baselineSource": "external_unavailable",
            "note": "내 최근 킬/성공 트라이/같은 세션 표본 없음. 외부 상위 로그 비교는 다음 확장 단계.",
        }

    samples = sorted(pools[source], key=lambda x: x.get("epoch") or 0, reverse=True)[:3]
    rates = [float(x["rate"]) for x in samples]
    baseline = statistics.median(rates)
    delta_pct = round((current_rate / baseline - 1.0) * 100.0, 1) if baseline else None
    lower = delta_pct is not None and delta_pct < 0
    return {
        "eligible": True,
        "status": "lower" if lower else "not_lower",
        "causeClass": "D",
        "scope": f"범위: {label} · 보호막 {shield_index} · n={len(samples)}",
        "baselineSource": source,
        "sampleTryNos": [x.get("tryNo") for x in samples],
        "currentRate": current_rate,
        "baselineMedianRate": baseline,
        "deltaPct": delta_pct,
        "summary": (
            f"D 비교 근거 · 동일 구간 보호막 처리량이 기준 중앙값보다 {abs(delta_pct):.1f}% 낮음"
            if lower and delta_pct is not None
            else f"D 비교 · 동일 구간 보호막 처리량이 기준보다 낮다는 근거 없음 ({delta_pct:+.1f}%)"
            if delta_pct is not None
            else "D 비교 · 처리량 편차 계산 불가"
        ),
        "note": "이 비교는 처리량 차이만 보여주며 개인 실력/책임을 단정하지 않습니다.",
    }

# ---------------------------------------------------------------------------
# B37.3.9 · compact Akunda/WWF-style presentation projection
# ---------------------------------------------------------------------------

def _generic_progress_text(review: dict, row: dict) -> tuple[str, int]:
    """Return a short progress label and 0..4 visual progress index.

    This is presentation-only. It never changes a mechanic verdict.
    """
    review = review or {}
    row = row or {}
    result = str(row.get("result") or ((review.get("encounter") or {}).get("result")) or "").upper()
    boss_key = str(review.get("bossKey") or "")
    obs = review.get("observations") or {}

    if boss_key == "ulatek":
        hearts = [x for x in (obs.get("heartWindows") or []) if isinstance(x, dict)]
        if hearts:
            count = len(hearts)
            last = hearts[-1]
            hp = last.get("endBossHpPct")
            if result == "KILL":
                return (f"{count}심장 킬", min(4, max(1, count + 1)))
            if hp is not None:
                return (f"{count}심장 · 종료 {float(hp):.1f}%", min(4, max(1, count)))
            return (f"{count}심장 도달", min(4, max(1, count)))
        return (("킬" if result == "KILL" else "심장 전"), 4 if result == "KILL" else 0)

    if boss_key == "coiled_altar":
        br = build_board_row(review, row)
        return (str(br.get("stage") or "진도 확인 중"), int(br.get("stageIndex") or 0))

    if result == "KILL":
        return ("킬", 4)
    if result in {"WIPE", "ABORT"}:
        return ("전멸", 1)
    return ("진도 확인 중", 0)


def _generic_primary_issue(model: dict, row: dict) -> str:
    result = str((row or {}).get("result") or "").upper()
    direct = model.get("collapseDirect") if isinstance(model.get("collapseDirect"), dict) else None
    major = model.get("majorIncident") if isinstance(model.get("majorIncident"), dict) else None
    first = model.get("firstMistake") if isinstance(model.get("firstMistake"), dict) else None
    chosen = direct or major or first
    if chosen:
        label = str(chosen.get("label") or chosen.get("title") or chosen.get("kind") or "검증 실패")
        sec = chosen.get("sec")
        return (f"{fmt_clock(sec)} · {label}" if sec is not None else label)
    if result == "KILL":
        footer = str(model.get("footer") or "").strip()
        if footer.startswith("남은 개선점"):
            return footer
        return "킬 성공 · 공대 붕괴 없음"
    collapse = str(model.get("collapseText") or "").strip()
    if collapse and "직접 인과 연결 대기" not in collapse:
        return collapse
    return "확정 원인 없음"


def build_simple_try_summary(review: dict, row: dict | None = None) -> dict:
    """One reason + evidence + next-pull action for the default Try screen."""
    row = row or {}
    model = build_try_priority_model(review or {}, row)
    if not model.get("supported"):
        result = str(row.get("result") or "").upper()
        return {
            "supported": False,
            "reason": "킬 성공" if result == "KILL" else "확정 원인 없음",
            "evidence": ["이 보스의 전용 기믹 판정 데이터가 없습니다."],
            "action": "상세 로그에서 공대 붕괴 체인을 확인하세요.",
        }

    reason = _generic_primary_issue(model, row)
    evidence: list[str] = []
    first = model.get("firstMistake") if isinstance(model.get("firstMistake"), dict) else None
    major = model.get("majorIncident") if isinstance(model.get("majorIncident"), dict) else None
    improvement = model.get("killImprovement") if isinstance(model.get("killImprovement"), dict) else None
    link = model.get("collapseLink") if isinstance(model.get("collapseLink"), dict) else {}
    for item in (first, major, improvement):
        if not item:
            continue
        label = str(item.get("label") or item.get("title") or item.get("kind") or "기믹")
        summary = str(item.get("summary") or "").strip()
        sec = item.get("sec")
        text = f"{fmt_clock(sec)} · {label}" if sec is not None else label
        if summary and summary not in text:
            text += f" · {summary}"
        if text not in evidence:
            evidence.append(text)
    link_summary = str(link.get("summary") or "").strip()
    if link_summary and link_summary not in evidence:
        evidence.append(link_summary)
    if not evidence:
        side = str(model.get("sidePanelText") or "").strip()
        if side and side != "보조 관측 없음":
            evidence.extend([x for x in side.splitlines() if x.strip()][:2])
    if not evidence:
        evidence.append("검증된 실패 근거 없음")

    action = str(model.get("footer") or "").strip()
    if action.startswith("남은 개선점 · "):
        action = action.replace("남은 개선점 · ", "다음 풀 · ", 1)
    elif action and not action.startswith("다음 풀"):
        action = "다음 풀 · " + action
    if not action:
        action = "다음 풀 · 현재 검증된 실패 없음 · 다음 표본에서 계속 확인"
    return {"supported": True, "reason": reason, "evidence": evidence[:3], "action": action, "model": model}


def build_generic_board_summary(items: Iterable[tuple[dict, dict]]) -> dict:
    """Boss-agnostic Try board used as the primary boss screen in B37.3.9.

    The board surfaces only what the raid lead needs before the next pull:
    result/time, progress, one primary issue, and up to three repeated actions.
    Detailed evidence remains in the Try screen.
    """
    items = canonicalize_review_pairs(items)
    rows: list[dict] = []
    repeat: dict[str, dict] = {}
    stage_counts = [0, 0, 0, 0, 0]
    for row, review in items:
        model = build_try_priority_model(review, row)
        progress, stage_index = _generic_progress_text(review, row)
        stage_index = min(4, max(0, int(stage_index or 0)))
        stage_counts[stage_index] += 1
        reason = _generic_primary_issue(model, row) if model.get("supported") else "확정 원인 없음"
        first = model.get("firstMistake") if isinstance(model.get("firstMistake"), dict) else None
        major = model.get("majorIncident") if isinstance(model.get("majorIncident"), dict) else None
        action = _next_pull(first, major) if model.get("supported") and first else ""
        if action == "현재 검증된 실패 중 우선 교정 항목 없음":
            action = ""
        if first:
            label = str(first.get("label") or first.get("title") or first.get("kind") or "검증 실패")
            slot = repeat.setdefault(label, {"count": 0, "tries": [], "action": ""})
            slot["count"] += 1
            slot["tries"].append(row.get("tryNo"))
            if action and not slot["action"]:
                slot["action"] = action
        result = str(row.get("result") or ((review.get("encounter") or {}).get("result")) or "").upper()
        rows.append({
            "tryNo": row.get("tryNo") if row.get("tryNo") is not None else (review.get("encounter") or {}).get("tryNo"),
            "durationSec": row.get("durationSec") if row.get("durationSec") is not None else (review.get("encounter") or {}).get("durationSec"),
            "duration": fmt_clock(row.get("durationSec") if row.get("durationSec") is not None else (review.get("encounter") or {}).get("durationSec")),
            "result": "킬" if result == "KILL" else "와이프" if result in {"WIPE", "ABORT"} else (result or "-"),
            "progress": progress,
            "stage": progress,
            "stageIndex": stage_index,
            "keyFailure": reason,
            "issue": reason,
            "nextAction": action,
            "review": review,
        })
    rows.sort(key=lambda x: (_safe_int(x.get("tryNo")), _safe_float(x.get("durationSec"), 0)))
    repeats = sorted(
        ({"label": label, **data} for label, data in repeat.items()),
        key=lambda x: (-int(x.get("count") or 0), str(x.get("label") or "")),
    )
    voice_orders: list[str] = []
    for item in repeats:
        action = str(item.get("action") or "").strip()
        if action.startswith("다음 풀 · "):
            action = action[len("다음 풀 · "):]
        if action:
            voice_orders.append(action)
        else:
            voice_orders.append(f"{item.get('label')} 재발 방지 · 직전 위치/대상/차단 확인")
        if len(voice_orders) >= 3:
            break
    if not voice_orders:
        voice_orders.append("현재 반복되는 검증 실패 없음 · 다음 구간 진행 확인")
    return {
        "rows": rows,
        "count": len(rows),
        "stageCounts": stage_counts,
        "repeats": repeats,
        "voiceOrders": voice_orders,
    }
