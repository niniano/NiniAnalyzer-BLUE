from __future__ import annotations

"""Public edition offline WoW CombatLog import using the B31 raid-only monitor.

Never calls the character-bound analysis pipeline. Inputs are read-only; the
normal live pull store persists completed encounters. Tk never runs this worker.
"""

import json
from pathlib import Path
from .encounter_tracker import parse_combat_datetime, parse_log_line, safe_int
from .io_utils import atomic_write_json
from .raid_review import LiveRaidReviewMonitor
from .mechanics import is_fake_pull
from .reset_policy import is_reset_pull


def _pull_key(row):
    return (
        str((row or {}).get("startTimestamp") or "").strip(),
        safe_int((row or {}).get("encounterID")),
        safe_int((row or {}).get("difficultyID")),
    )


def missing_pull_count(log_file: Path | str, existing_keys: set) -> int:
    """V5.4.7: 로그에서 ENCOUNTER_START 줄만 빨리 훑어, 아직 저장 안 된 풀이 몇 개인지 센다.

    놓친 풀이 없으면 무거운 재생(replay)을 아예 하지 않기 위한 사전 확인.
    """
    missing = 0
    with Path(log_file).open("rb") as handle:
        for raw in handle:
            if b"ENCOUNTER_START" not in raw:
                continue
            parsed = parse_log_line(raw.decode("utf-8-sig", errors="replace").rstrip("\r\n"))
            if not parsed or parsed.get("event") != "ENCOUNTER_START":
                continue
            fields = parsed.get("fields") or []
            key = (
                str(parsed.get("timestamp") or "").strip(),
                safe_int(fields[1]) if len(fields) > 1 else None,
                safe_int(fields[3]) if len(fields) > 3 else None,
            )
            if key not in existing_keys:
                missing += 1
    return missing


def renumber_tries(boss_dirs) -> int:
    """V5.4.7: 나중에 채워 넣은 풀 때문에 Try 번호가 시간 순서와 어긋나지 않게 다시 매긴다.

    실시간으로 18~21번째 풀만 잡힌 뒤(Try 1~4) 앞의 17풀을 채우면 번호가 5~21 로 붙었다.
    같은 날 · 같은 보스 폴더 안에서 시작 시각 순으로 1부터 다시 매긴다. 규칙은 실시간과 같다:
    0.0x초 표식(fake)은 건드리지 않고, 리셋 풀은 번호를 받되 다음 풀이 같은 번호를 이어 쓴다.
    meta.json 의 tryNo 만 고친다 (폴더 이름 · 원본 로그 · 판정 파일은 그대로 · 예전 번호는 tryNoBeforeRenumber).
    """
    changed = 0
    for boss_dir in {Path(x) for x in boss_dirs}:
        try:
            try_dirs = [x for x in boss_dir.iterdir() if x.is_dir() and x.name.startswith("try_")]
        except OSError:
            continue
        by_difficulty: dict = {}
        for try_dir in try_dirs:
            meta_path = try_dir / "meta.json"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(meta, dict) or is_fake_pull(meta):
                continue
            started = parse_combat_datetime(str(meta.get("startTimestamp") or ""))
            if started is None:
                by_difficulty = {}
                break   # 시각을 모르는 풀이 섞여 있으면 이 보스는 손대지 않는다
            by_difficulty.setdefault(safe_int(meta.get("difficultyID")), []).append((started, meta_path, meta))
        for rows in by_difficulty.values():
            rows.sort(key=lambda item: item[0])
            count = 0
            for _, meta_path, meta in rows:
                number = count + 1
                if not is_reset_pull(meta):
                    count = number
                if safe_int(meta.get("tryNo")) != number:
                    meta.setdefault("tryNoBeforeRenumber", meta.get("tryNo"))
                    meta["tryNo"] = number
                    try:
                        atomic_write_json(meta_path, meta)
                        changed += 1
                    except Exception:
                        pass
    return changed


def renumber_recent_sessions(storage_root: Path | str, dates: int = 2) -> int:
    """가장 최근 날짜 폴더(기본 2개)의 모든 보스에 대해 Try 번호를 시간순으로 맞춘다.

    채우기가 도는 동안 실시간 풀이 번호를 받으면 같은 번호가 두 번 생길 수 있었다
    (채우기가 아직 21번까지만 저장했을 때 시작한 실시간 풀이 22번을 받고, 채우기가 끝난 뒤에도 그대로 남음).
    풀이 저장될 때마다 · 채우기가 끝날 때마다 불러서 번호를 늘 시간순으로 둔다. 바뀐 것이 없으면 아무것도 쓰지 않는다.
    """
    root = Path(storage_root)
    try:
        date_dirs = sorted((x for x in root.iterdir() if x.is_dir() and not x.name.startswith("_")),
                           key=lambda x: x.name, reverse=True)[:max(1, int(dates))]
    except OSError:
        return 0
    boss_dirs = []
    for date_dir in date_dirs:
        try:
            boss_dirs.extend(x for x in date_dir.iterdir() if x.is_dir())
        except OSError:
            pass
    return renumber_tries(boss_dirs)


def import_public_combat_log(log_file: Path | str, storage_root: Path | str, progress=None, cancel_event=None,
                             *, player=None, tracked_players=None, raid_only: bool = True,
                             active_dir_name: str | None = None, only_if_missing: bool = False,
                             extra_keys=None) -> dict:
    """Replay complete raid pulls from a chosen log (incl. arbitrary path).

    Previously stored pulls are skipped by encounter id, difficulty and exact
    start timestamp, even when the file was renamed or appended. Partial pulls
    at the end are discarded instead of misclassified as wipes.

    V5.4.7 (놓친 트라이 채우기): ``player`` / ``tracked_players`` / ``raid_only`` 로 개인판
    감시와 같은 방식으로 저장할 수 있고, ``active_dir_name`` 으로 임시 저장 폴더를 따로 써서
    실시간 감시와 동시에 돌아도 서로의 진행 중 파일을 건드리지 않는다. ``only_if_missing`` 이면
    놓친 풀이 없을 때 로그를 재생하지 않고 바로 끝낸다. 새로 넣은 풀이 있으면 Try 번호를 시간순으로 다시 매긴다.
    ``extra_keys`` 는 실시간 감시가 지금 잡고 있는(아직 저장 전일 수 있는) 풀의 키를 돌려주는 함수 — 그 풀은 건너뛴다.
    """
    source = Path(log_file).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".txt":
        raise ValueError(f"WoW CombatLog 텍스트 파일이 아닙니다: {source}")
    monitor = LiveRaidReviewMonitor(source.parent, storage_root=storage_root, player=player,
                                    tracked_players=tracked_players, raid_only=raid_only)
    if active_dir_name:
        monitor.store.active_dir = monitor.store.root / str(active_dir_name)
    before_rows = list(monitor.saved_rows(migrate_identity=False))
    existing_keys = {_pull_key(row) for row in before_rows}
    before_dirs = {str(row.get("storageDir") or "") for row in before_rows}
    if only_if_missing and missing_pull_count(source, existing_keys) == 0:
        monitor._facts_executor.shutdown(wait=False)
        if progress:
            progress({"percent": 100, "lines": 0, "imported": 0, "skipped": len(existing_keys)})
        return {"path": str(source), "lines": 0, "imported": 0, "skipped": 0, "completed": 0,
                "errors": 0, "cancelled": False, "renumbered": 0, "nothingMissing": True}
    size = max(1, source.stat().st_size)
    imported = skipped = scanned = completed = errors = 0
    current_duplicate = False
    cancelled = False
    monitor.tailer.current_file = source
    notify_at = 0
    try:
        with source.open("rb") as handle:
            for raw in handle:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                scanned += 1
                line = raw.decode("utf-8-sig", errors="replace").rstrip("\r\n")
                # Skip double parsing duplicate combat events; only encounter
                # boundaries can change the import state while skipping.
                parsed = (parse_log_line(line) if not current_duplicate or "ENCOUNTER_" in line else None)
                if parsed and parsed.get("event") == "ENCOUNTER_START":
                    fields = parsed.get("fields") or []
                    key = (
                        str(parsed.get("timestamp") or "").strip(),
                        safe_int(fields[1]) if len(fields) > 1 else None,
                        safe_int(fields[3]) if len(fields) > 3 else None,
                    )
                    current_duplicate = key in existing_keys
                    if not current_duplicate and extra_keys is not None:
                        try:
                            current_duplicate = key in set(extra_keys() or ())
                        except Exception:
                            pass
                    if current_duplicate:
                        skipped += 1
                        # The previous source may have ended mid-pull; a
                        # duplicate start is a boundary, not permission to
                        # later finalize that incomplete capture as an ABORT.
                        if monitor.store.active:
                            monitor.store.discard_active()
                            monitor.tracker.current = None
                    else:
                        existing_keys.add(key)
                if not current_duplicate:
                    events = monitor._feed_live_line(line)
                    for item in events:
                        if item.get("type") == "encounter_end":
                            imported += 1
                        if item.get("type") == "encounter_abort":
                            imported += 1
                else:
                    # Duplicates are not replayed to the raid review engine,
                    # but recent combat-log lines still matter for the NEXT
                    # pull's 10-second pre-pot/pre-buff evidence.
                    monitor.preroll.append(line)
                    if parsed and parsed.get("event") == "ENCOUNTER_END":
                        # The duplicate ends here. Lines between encounters
                        # must go through _feed_live_line() as usual.
                        current_duplicate = False
                if progress and handle.tell() >= notify_at:
                    notify_at = handle.tell() + max(512 * 1024, size // 50)
                    progress({"percent": min(98, handle.tell() * 98 / size), "lines": scanned,
                              "imported": imported, "skipped": skipped})
            # Never persist a partially written final encounter as an actual wipe.
            if monitor.store.active:
                monitor.store.discard_active()
                monitor.tracker.current = None
            # On cancellation wait only for the CURRENT review. Unstarted
            # queued futures are cancelled; their raw logs remain intact and
            # the next launch detects them as missing-review for reanalysis.
            monitor._facts_executor.shutdown(wait=True, cancel_futures=cancelled)
            if cancelled:
                monitor._fact_jobs = [job for job in monitor._fact_jobs
                                      if not job["future"].cancelled()]
            for event in monitor._collect_fact_events():
                if event.get("type") == "facts_ready":
                    completed += 1
                elif event.get("type") == "facts_error":
                    errors += 1
            renumbered = 0
            if imported:
                try:
                    touched = {str(Path(row["storageDir"]).parent)
                               for row in monitor.saved_rows(migrate_identity=False)
                               if row.get("storageDir") and str(row.get("storageDir")) not in before_dirs}
                    renumbered = renumber_tries(touched)
                except Exception:
                    renumbered = 0
            if progress:
                progress({"percent": 100, "lines": scanned, "imported": imported,
                          "skipped": skipped, "cancelled": cancelled})
            return {"path": str(source), "lines": scanned, "imported": imported,
                    "skipped": skipped, "completed": completed, "errors": errors,
                    "cancelled": cancelled, "renumbered": renumbered}
    finally:
        if monitor.store.active:
            monitor.store.discard_active()
        # Also handle an exception raised during import/progress notification.
        monitor._facts_executor.shutdown(
            wait=True,
            cancel_futures=bool(cancelled or (cancel_event is not None and cancel_event.is_set())),
        )
