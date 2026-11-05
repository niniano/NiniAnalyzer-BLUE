from __future__ import annotations

"""Public edition offline WoW CombatLog import using the B31 raid-only monitor.

Never calls the character-bound analysis pipeline. Inputs are read-only; the
normal live pull store persists completed encounters. Tk never runs this worker.
"""

from pathlib import Path
from .encounter_tracker import parse_log_line, safe_int
from .raid_review import LiveRaidReviewMonitor


def _pull_key(row):
    return (
        str((row or {}).get("startTimestamp") or "").strip(),
        safe_int((row or {}).get("encounterID")),
        safe_int((row or {}).get("difficultyID")),
    )


def import_public_combat_log(log_file: Path | str, storage_root: Path | str, progress=None, cancel_event=None) -> dict:
    """Replay complete raid pulls from a chosen log (incl. arbitrary path).

    Previously stored pulls are skipped by encounter id, difficulty and exact
    start timestamp, even when the file was renamed or appended. Partial pulls
    at the end are discarded instead of misclassified as wipes.
    """
    source = Path(log_file).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".txt":
        raise ValueError(f"WoW CombatLog 텍스트 파일이 아닙니다: {source}")
    monitor = LiveRaidReviewMonitor(source.parent, storage_root=storage_root, raid_only=True)
    existing_keys = {_pull_key(row) for row in monitor.saved_rows(migrate_identity=False)}
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
            if progress:
                progress({"percent": 100, "lines": scanned, "imported": imported,
                          "skipped": skipped, "cancelled": cancelled})
            return {"path": str(source), "lines": scanned, "imported": imported,
                    "skipped": skipped, "completed": completed, "errors": errors,
                    "cancelled": cancelled}
    finally:
        if monitor.store.active:
            monitor.store.discard_active()
        # Also handle an exception raised during import/progress notification.
        monitor._facts_executor.shutdown(
            wait=True,
            cancel_futures=bool(cancelled or (cancel_event is not None and cancel_event.is_set())),
        )
