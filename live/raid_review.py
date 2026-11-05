from __future__ import annotations

import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import threading
from pathlib import Path

from .encounter_tracker import EncounterTracker, parse_log_line, safe_int
from .fact_extractor import extract_and_write
from .death_review import analyze_and_write as analyze_death_and_write
from .raid_facts import extract_and_write as extract_raid_and_write
from .raid_wipe_review import process_live_try
from .log_watcher import CombatLogTailer
from .pull_store import LivePullStore
from .preroll import PreRollBuffer
from .io_utils import atomic_write_json
from .rawlog import resolve_raw
from .reset_policy import is_reset_pull, reset_metadata
from .selection_context import session_date_from_timestamp, session_date_from_log_path
from .mechanics import is_fake_pull
from .boss_mechanics import analyze_raw_try as analyze_boss_mechanics_raw, write_review as write_boss_mechanic_review, link_saved_review as link_saved_boss_mechanic_review
from .compact_archive import build_compact_capsule, sync_pending_archives



def _short_player(value):
    return str(value or "").split("-")[0].strip()


def _spec_for_player(raid_facts: dict, player: str):
    wanted = _short_player(player)
    if not wanted:
        return None
    for member in (raid_facts or {}).get("roster") or []:
        if _short_player(member.get("name")) == wanted:
            try:
                return int(member.get("specID")) if member.get("specID") is not None else None
            except (TypeError, ValueError):
                return None
    return None


def _persist_context_meta(raw_path: Path, info: dict):
    meta_path = Path(raw_path).parent / "meta.json"
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
        if isinstance(meta, dict):
            for key in ("factsPlayer", "factsSpecID", "analysisExcluded", "analysisOverride",
                        "resetPull", "autoResetPull", "resetReason", "resetMaxDurationSec",
                        "mechanicReviewStatus", "mechanicReviewEngine", "mechanicReviewSchema",
                        "mechanicReviewJson", "mechanicReviewTxt", "mechanicVerdict",
                        "mechanicSummaryLine", "mechanicFirstMistake", "mechanicCollapseDirect"):
                if key in info:
                    meta[key] = info.get(key)
            atomic_write_json(meta_path, meta)
    except Exception:
        pass

# V5.2.4: 라이브 분석(방금 끝난 풀)이 도는 동안 화면의 「앞뒤 트라이 미리 계산」은 쉰다 (CPU 나눠 쓰면 둘 다 느려짐).
_LIVE_BUSY = [0]
_LIVE_BUSY_LOCK = threading.Lock()


def _live_busy_add(n: int) -> None:
    with _LIVE_BUSY_LOCK:
        _LIVE_BUSY[0] = max(0, _LIVE_BUSY[0] + n)


def live_analysis_busy() -> bool:
    """라이브 분석이 대기 중이거나 도는 중이면 True."""
    return _LIVE_BUSY[0] > 0


def _extract_boss_mechanics(raw_path: Path, encounter: dict):
    """Optional boss-specific sidecar. Never mutates B31 raid facts/review outputs."""
    try:
        review = analyze_boss_mechanics_raw(raw_path, encounter)
        if not review:
            return {}
        return write_boss_mechanic_review(Path(raw_path).parent, review)
    except Exception as exc:
        return {
            "mechanicReviewStatus": "error",
            "mechanicReviewError": str(exc),
        }


def _extract_live_reviews(raw_path: Path, encounter: dict, player: str | None, storage_root: Path, progress=None, raid_only: bool = False):
    # Keep personal facts/death review for drill-down, but v0.3-A promotes a
    # raid-wide factual timeline to the primary Live Review output.
    # B31.1: `progress` (live.progress.LiveProgress) receives real stage/byte progress.
    from . import progress as live_progress
    # B32: short non-kill pulls are resets, not discarded data.  Keep the raw log
    # and extract the raid-wide factual layer (deaths + first observed attack), but
    # do not spend work on personal/causal/series analysis that feeds normal stats.
    if is_reset_pull(encounter):
        try:
            with live_progress.active(progress):
                raid_info = extract_raid_and_write(raw_path, encounter)
                raid_facts = raid_info.get("raidFacts") or {}
                fact_spec = _spec_for_player(raid_facts, player)
                fake_pull = is_fake_pull(encounter)
                # B37.4.7: an excluded reset/fake pull is not a mechanic sample.
                # Preserve raid-wide facts/raw, but explicitly clear any legacy
                # mechanic sidecar pointer so zero observed failures cannot render
                # as a green success card.
                mechanic_info = {
                    "mechanicReviewStatus": "excluded",
                    "mechanicReviewJson": None,
                    "mechanicReviewTxt": None,
                    "mechanicVerdict": None,
                    "mechanicSummaryLine": None,
                    "mechanicFirstMistake": None,
                    "mechanicCollapseDirect": None,
                }
                info = {
                    "factsStatus": "ready",
                    "deathReviewStatus": "excluded",
                    "raidReviewStatus": "excluded",
                    "raidSeriesReviewStatus": "excluded",
                    "analysisExcluded": "fake_pull" if fake_pull else "reset_pull",
                    "deathCount": len(raid_facts.get("deaths") or []),
                    "pullStart": raid_facts.get("pullStart"),
                }
                if not raid_only:
                    info["factsPlayer"] = player
                    info["factsSpecID"] = fact_spec
                info.update(reset_metadata(encounter))
                info.update(raid_info)
                info.update(mechanic_info)
                live_progress.enter("save")
                gz = _compress_after_review(Path(raw_path))
                if gz is not None:
                    info["rawLog"] = str(gz)
                    info["rawCompressed"] = True
                # Persist the classification alongside the generated factual files.
                meta_path = Path(raw_path).parent / "meta.json"
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                        if isinstance(meta, dict):
                            meta_updates = {
                                "analysisExcluded": info.get("analysisExcluded"),
                                "deathCount": info.get("deathCount"),
                                "pullStart": info.get("pullStart"),
                                **reset_metadata(encounter),
                                **{k: v for k, v in mechanic_info.items() if k != "mechanicReviewError"},
                            }
                            if not raid_only:
                                meta_updates["factsPlayer"] = player
                                meta_updates["factsSpecID"] = fact_spec
                            meta.update(meta_updates)
                            atomic_write_json(meta_path, meta)
                    except Exception:
                        pass
                if not raid_only:
                    info.update(_archive_after_review(Path(raw_path), storage_root))
        except Exception:
            if progress is not None:
                progress.finish(False, "오류")
            raise
        if progress is not None:
            progress.finish(True, "리셋 분류")
        return info
    if raid_only:
        try:
            with live_progress.active(progress):
                # Public raid mode deliberately skips character-bound facts and
                # death-review extraction.  The raid-wide B31/BossMechanic path
                # is sufficient for session/board/Try review and can run without
                # a character name.
                raid_info = extract_raid_and_write(raw_path, encounter)
                raid_facts = raid_info.get("raidFacts") or {}
                info = dict(raid_info)
                info.update(_extract_boss_mechanics(Path(raw_path), encounter))
                info.update({
                    "factsStatus": "ready",
                    "deathReviewStatus": "excluded",
                    "analysisExcluded": None,
                    "deathCount": len(raid_facts.get("deaths") or []),
                    "pullStart": raid_facts.get("pullStart"),
                })
                info.update(reset_metadata(encounter))
                live_progress.enter("collapse")
                info.update(process_live_try(Path(raw_path).parent, storage_root))
                info.update(link_saved_boss_mechanic_review(Path(raw_path).parent))
                _persist_context_meta(Path(raw_path), info)
                live_progress.enter("save")
                gz = _compress_after_review(Path(raw_path))
                if gz is not None:
                    info["rawLog"] = str(gz)
                    info["rawCompressed"] = True
                # PUBLIC: no compact capsules, no archive outbox, no Drive discovery.
                # raw.log.gz remains in the isolated public session store.
        except Exception:
            if progress is not None:
                progress.finish(False, "오류")
            raise
        if progress is not None:
            progress.finish(True, "완료")
        return info

    try:
        with live_progress.active(progress):
            info = extract_and_write(raw_path, encounter, player)
            facts = info.get("facts") or {}
            raid_info = extract_raid_and_write(raw_path, encounter)
            info.update(raid_info)
            info.update(_extract_boss_mechanics(Path(raw_path), encounter))
            raid_facts = raid_info.get('raidFacts') or {}
            info["factsPlayer"] = player
            info["factsSpecID"] = _spec_for_player(raid_facts, player)
            info["analysisExcluded"] = None
            info["deathCount"] = len(raid_facts.get("deaths") or [])
            info["pullStart"] = raid_facts.get("pullStart")
            info.update(reset_metadata(encounter))
            facts['deathIncidents'] = [x for x in raid_facts.get('deathIncidents', []) if x['player'] == player.split('-')[0]]
            live_progress.enter("death")
            death_info = analyze_death_and_write(facts, Path(raw_path).parent)
            info.update(death_info)
            live_progress.enter("collapse")
            wipe_info = process_live_try(Path(raw_path).parent, storage_root)
            info.update(wipe_info)
            # B35.1: BossMechanic is an upper semantic layer. B31 remains the
            # factual/causal source of truth; after B31 finishes, link the
            # sidecar to the generic collapse verdict without mutating B31.
            mech_link_info = link_saved_boss_mechanic_review(Path(raw_path).parent)
            info.update(mech_link_info)
            _persist_context_meta(Path(raw_path), info)
            live_progress.enter("save")
            gz = _compress_after_review(Path(raw_path))
            if gz is not None:
                info["rawLog"] = str(gz)
                info["rawCompressed"] = True
            info.update(_archive_after_review(Path(raw_path), storage_root))
    except Exception:
        if progress is not None:
            progress.finish(False, "오류")
        raise
    if progress is not None:
        progress.finish(True, "완료")
    return info




def _archive_after_review(raw_path: Path, storage_root: Path):
    """B37.1.2 best-effort compact archive. Analysis must never fail because sync fails."""
    try:
        try_dir = Path(raw_path).parent
        # storage_root is <workdir>/data/live_sessions in the desktop app.
        workdir = Path(storage_root).resolve().parent.parent
        return build_compact_capsule(try_dir, workdir=workdir, archive_kind="Sessions")
    except Exception as exc:
        return {"archiveStatus": "error", "archiveError": str(exc)}

def _compress_after_review(raw_path: Path):
    """B31.2: keep the per-pull raw log as raw.log.gz (~10x smaller) once analysed.

    Readers resolve raw.log / raw.log.gz transparently (live.rawlog), so
    re-analysis keeps working.  Failures leave the plain raw.log untouched.
    """
    from .rawlog import compress_raw, compress_preroll, AUTO_COMPRESS_AFTER_REVIEW
    if not AUTO_COMPRESS_AFTER_REVIEW:
        return None
    gz = compress_raw(raw_path)
    if gz is None or not str(gz).endswith(".gz"):
        return None
    preroll_gz = compress_preroll(gz.parent)
    meta_path = gz.parent / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(meta, dict):
            meta["rawFile"] = gz.name
            if meta.get("rawLog"):
                meta["rawLog"] = str(gz)
            meta["rawCompressed"] = True
            if preroll_gz is not None and str(preroll_gz).endswith(".gz"):
                meta["prerollFile"] = preroll_gz.name
                meta["prerollCompressed"] = True
            atomic_write_json(meta_path, meta)
    except Exception:
        pass
    return gz


class LiveRaidReviewMonitor:
    """Live tail + encounter separation + per-pull persistence + async fact extraction.

    v0.3-B2 runs the raid-wide factual extractor and then the causal wipe-review
    core.  Wipes receive a per-try root-cause review plus an encounterID+difficulty
    series summary across stored sessions.  Personal facts/death review remain as
    drill-down evidence.
    """

    def __init__(
        self,
        log_dir: Path | str,
        storage_root: Path | str | None = None,
        player: str | None = None,
        tracked_players=None,
        raid_only: bool = False,
    ):
        self.tailer = CombatLogTailer(log_dir)
        self.tracker = EncounterTracker(recent_limit=30)
        self.store = LivePullStore(storage_root or (Path.cwd() / "data" / "live_sessions"), tracked_players=tracked_players)
        self.preroll = PreRollBuffer(window_sec=10.0, max_lines=50_000)
        self.player = str(player or "").strip()
        self.raid_only = bool(raid_only)
        self.running = False
        self.last_new_lines = 0
        self.last_events: list[dict] = []
        self._facts_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nini-live-facts")
        self._fact_jobs: list[dict] = []
        # Incremented whenever this process changes persisted Live state.  The GUI
        # can reload meta.json rows only on revision changes instead of rescanning
        # the whole store every polling tick.
        self._storage_revision = 0

    @property
    def log_dir(self) -> Path:
        return self.tailer.log_dir

    @property
    def storage_root(self) -> Path:
        return self.store.root

    @property
    def storage_revision(self) -> int:
        return int(self._storage_revision)

    def _touch_storage_revision(self):
        self._storage_revision += 1

    def set_player(self, player: str | None):
        self.player = str(player or "").strip()

    @staticmethod
    def _encounter_key(encounter: dict):
        return (
            encounter.get("encounterID"),
            encounter.get("difficultyID"),
            encounter.get("tryNo"),
            encounter.get("startTimestamp"),
        )

    def _annotate_recent(self, encounter: dict, info: dict):
        if not info:
            return
        encounter.update(info)
        keys = self._encounter_key(encounter)
        for row in self.tracker.recent:
            if self._encounter_key(row) == keys:
                row.update(info)
                break

    def _schedule_facts(self, encounter: dict):
        raw = encounter.get("rawLog")
        player = str(encounter.get("factsPlayer") or encounter.get("livePlayer") or self.player or "").strip()
        if not raw or (not player and not self.raid_only):
            return
        raw_path = resolve_raw(Path(raw))
        if not raw_path.exists():
            return
        encounter_snapshot = dict(encounter)
        encounter["factsStatus"] = "analyzing"
        annotate = {"factsStatus": "analyzing"}
        if player:
            annotate["factsPlayer"] = player
        self._annotate_recent(encounter, annotate)
        from .progress import LiveProgress
        progress = LiveProgress()
        _live_busy_add(1)
        future = self._facts_executor.submit(
            _extract_live_reviews, raw_path, encounter_snapshot, player,
            self.storage_root, progress, self.raid_only,
        )
        future.add_done_callback(lambda _f: _live_busy_add(-1))
        self._fact_jobs.append({
            "future": future,
            "key": self._encounter_key(encounter_snapshot),
            "encounter": encounter_snapshot,
            "player": player,
            "progress": progress,
        })


    def promote_reset_to_normal(self, encounter: dict) -> dict:
        """Persist a manual normal-attempt override and re-run full review.

        The original Try number/raw evidence is untouched.  The automatic reset
        classification remains auditable through ``autoResetPull`` while the
        effective ``resetPull``/``analysisExcluded`` state is changed.
        """
        row = dict(encounter or {})
        raw_value = row.get("rawLog") or row.get("storageDir")
        raw_path = resolve_raw(Path(raw_value)) if raw_value else None
        if raw_path is None or not raw_path.exists():
            raise FileNotFoundError("선택한 트라이의 raw 로그를 찾지 못했습니다.")
        override_at = datetime.now().isoformat(timespec="seconds")
        updates = {
            "analysisOverride": "normal",
            "analysisOverrideAt": override_at,
            "analysisExcluded": None,
            "resetPull": False,
            "resetReason": None,
        }
        row.update(updates)
        meta_path = raw_path.parent / "meta.json"
        meta = {}
        if meta_path.exists():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(loaded, dict):
                    meta = loaded
            except Exception:
                meta = {}
        meta.update(updates)
        meta.update(reset_metadata(row))
        meta["analysisOverrideAt"] = override_at
        meta["analysisExcluded"] = None
        atomic_write_json(meta_path, meta)
        self._touch_storage_revision()
        row.update(meta)
        row["rawLog"] = str(raw_path)
        self._annotate_recent(row, row)
        self._schedule_facts(row)
        return row

    def _collect_fact_events(self) -> list[dict]:
        if not self._fact_jobs:
            return []
        out = []
        keep = []
        for job in self._fact_jobs:
            future = job["future"]
            if not future.done():
                keep.append(job)
                continue
            enc = dict(job["encounter"])
            try:
                info = future.result()
                if job.get("player") and not self.raid_only:
                    info["factsPlayer"] = job.get("player")
                self._annotate_recent(enc, info)
                enc.update(info)
                self._touch_storage_revision()
                out.append({"type": "facts_ready", "encounter": enc})
            except Exception as exc:
                info = {
                    "factsStatus": "error",
                    "factsError": str(exc),
                }
                if job.get("player") and not self.raid_only:
                    info["factsPlayer"] = job.get("player")
                self._annotate_recent(enc, info)
                enc.update(info)
                self._touch_storage_revision()
                out.append({"type": "facts_error", "encounter": enc, "error": str(exc)})
        self._fact_jobs = keep
        return out

    def _finalize_capture(self, encounter: dict):
        info = self.store.finalize(encounter)
        self._annotate_recent(encounter, info)
        if info:
            self._touch_storage_revision()
            self._schedule_facts(encounter)
        return info

    def _abort_current(self, reason: str):
        event = self.tracker.reset_current(reason)
        if event:
            enc = event.get("encounter") or {}
            self._finalize_capture(enc)
        elif self.store.active:
            self.store.discard_active()
        return event

    def start(self, *, start_at_end: bool = True) -> list[dict]:
        self.running = True
        batch = self.tailer.start(start_at_end=start_at_end)
        self.preroll.reset(datetime.now())
        self.last_new_lines = 0
        if self.raid_only:
            # PUBLIC never scans personal Drive settings or replays any queued
            # archives (even if NiniWoWSync exists on this computer).
            sync_info = {"status": "disabled_public", "files": 0}
        else:
            try:
                workdir = self.storage_root.resolve().parent.parent
                sync_info = sync_pending_archives(workdir)
            except Exception as exc:
                sync_info = {"status": f"sync_check_error:{exc}", "files": 0}
        events = [{
            "type": "monitor_start",
            "file": str(batch.file_path) if batch.file_path else None,
            "offset": batch.end_offset,
            "storageRoot": str(self.storage_root),
            "player": self.player,
            "archiveSync": sync_info,
        }]
        self.last_events = events
        return events

    def stop(self) -> list[dict]:
        events = []
        aborted = self._abort_current("monitor-stop")
        if aborted:
            events.append(aborted)
        self.running = False
        self.tailer.stop()
        events.extend(self._collect_fact_events())
        events.append({"type": "monitor_stop"})
        self.last_events = events
        return events

    def reconnect_latest(self, *, start_at_end: bool = True) -> list[dict]:
        events = []
        aborted = self._abort_current("manual-reconnect")
        if aborted:
            events.append(aborted)
        batch = self.tailer.reconnect_latest(start_at_end=start_at_end)
        self.preroll.reset(datetime.now())
        events.append({
            "type": "file_switch",
            "file": str(batch.file_path) if batch.file_path else None,
            "reason": batch.switch_reason,
            "offset": batch.end_offset,
        })
        events.extend(self._collect_fact_events())
        self.last_events = events
        return events

    def _feed_live_line(self, line: str) -> list[dict]:
        parsed = parse_log_line(line)
        event_name = parsed.get("event") if parsed else None

        # Snapshot BEFORE ENCOUNTER_START is added to the rolling history.  raw.log
        # remains unchanged and starts at ENCOUNTER_START exactly as before.
        preroll_snapshot = None
        if event_name == "ENCOUNTER_START":
            preroll_snapshot = self.preroll.snapshot_before(parsed.get("timestamp"))

        # B33.4: before the tracker assigns a Try number, seed its counter from
        # already-persisted pulls in the same raid-night session.  This survives
        # app restarts and WoW log-file rotation without duplicate Try 1s.
        if event_name == "ENCOUNTER_START" and parsed:
            fields = parsed.get("fields") or []
            encounter_id = safe_int(fields[1]) if len(fields) > 1 else None
            difficulty_id = safe_int(fields[3]) if len(fields) > 3 else None
            session_date = (
                session_date_from_timestamp(parsed.get("timestamp"))
                or session_date_from_log_path(self.tailer.current_file)
                or ""
            )
            persisted_max = self.store.max_saved_try_no(
                {
                    "encounterID": encounter_id,
                    "difficultyID": difficulty_id,
                    "startTimestamp": parsed.get("timestamp"),
                },
                self.tailer.current_file,
            )
            self.tracker.prepare_try_count(
                encounter_id,
                difficulty_id,
                session_token=session_date,
                existing_max=persisted_max,
            )

        # All lines after ENCOUNTER_START belong to the active capture, including
        # ENCOUNTER_END. A new ENCOUNTER_START aborts the old pull first and is
        # never appended to the old raw.log.
        if self.store.active and event_name != "ENCOUNTER_START":
            self.store.append(line)

        events = self.tracker.feed_line(line)
        for event in events:
            kind = event.get("type")
            enc = event.get("encounter") or {}
            if kind == "encounter_abort":
                self._finalize_capture(enc)
            elif kind == "encounter_start":
                if self.player:
                    enc["livePlayer"] = self.player
                    if self.tracker.current is not None:
                        self.tracker.current["livePlayer"] = self.player
                snap = preroll_snapshot or self.preroll.snapshot_before(enc.get("startTimestamp"))
                self.store.start(
                    enc,
                    self.tailer.current_file,
                    line,
                    preroll_lines=snap.lines,
                    preroll_meta={
                        "prerollSec": snap.preroll_sec,
                        "windowSec": snap.window_sec,
                        "maxLines": snap.max_lines,
                        "boundaryReason": snap.boundary_reason,
                        "boundaryTimestamp": snap.boundary_timestamp,
                    },
                )
                enc["prerollSec"] = snap.preroll_sec
                enc["prerollLineCount"] = snap.line_count
                enc["prerollBoundaryReason"] = snap.boundary_reason
            elif kind == "encounter_end":
                self._finalize_capture(enc)

        # Keep one continuously bounded history.  ENCOUNTER_END stays in memory as
        # a cut marker, but snapshot_before() excludes it and everything before it.
        self.preroll.append(line)
        return events

    def poll(self) -> list[dict]:
        # Fact extraction is independent of live tail availability; collect jobs
        # first so a just-finished pull can appear as facts_ready on the next tick.
        events = self._collect_fact_events()
        if not self.running:
            self.last_new_lines = 0
            self.last_events = events
            return events

        batch = self.tailer.poll()
        if batch.switched:
            aborted = self._abort_current(batch.switch_reason or "file-switch")
            if aborted:
                events.append(aborted)
            self.preroll.reset(datetime.now())
            events.append({
                "type": "file_switch",
                "file": str(batch.file_path) if batch.file_path else None,
                "reason": batch.switch_reason,
                "offset": batch.end_offset,
            })
        self.last_new_lines = len(batch.lines)
        for line in batch.lines:
            events.extend(self._feed_live_line(line))
        # Tiny pulls can finish fact extraction during this same poll.
        events.extend(self._collect_fact_events())
        self.last_events = events
        return events

    def current_elapsed(self):
        return self.tracker.current_elapsed()

    def saved_rows(self, migrate_identity: bool = True) -> list[dict]:
        """Return persisted Live pulls hydrated from the session store.

        ``migrate_identity=False`` is the non-blocking GUI startup path: it only
        reads meta/artifact paths.  Raw-log ownership migration can then run on a
        worker thread after the window has already rendered.
        """
        try:
            return self.store.load_saved_rows(migrate_identity=migrate_identity)
        except Exception:
            return []

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "player": self.player,
            "raidOnly": self.raid_only,
            "logDir": str(self.log_dir),
            "storageRoot": str(self.storage_root),
            "file": str(self.tailer.current_file) if self.tailer.current_file else None,
            "offset": int(self.tailer.offset),
            "totalLines": int(self.tailer.total_lines),
            "totalBytes": int(self.tailer.total_bytes),
            "lastNewLines": int(self.last_new_lines),
            "current": ({k: v for k, v in self.tracker.current.items() if not str(k).startswith("_")}
                        if self.tracker.current else None),
            "recent": list(self.tracker.recent),
            "parsedLines": int(self.tracker.parsed_lines),
            "unparsedLines": int(self.tracker.unparsed_lines),
            "captureActive": bool(self.store.active),
            "prerollBufferedLines": len(self.preroll._rows),
            "prerollWindowSec": self.preroll.window_sec,
            "prerollMaxLines": self.preroll.max_lines,
            "pendingFactJobs": sum(1 for job in self._fact_jobs if not job["future"].done()),
            "factJobProgress": self._fact_job_progress(),
        }

    def _fact_job_progress(self):
        """B31.1: progress of the pull currently being analysed (oldest unfinished job)."""
        pending = [job for job in self._fact_jobs if not job["future"].done()]
        if not pending:
            return None
        job = pending[0]
        prog = job.get("progress")
        snap = prog.snapshot() if prog is not None else {"pct": 0.0, "stage": "대기", "done": False, "failed": False}
        enc = job.get("encounter") or {}
        snap["label"] = f"{enc.get('name') or '전투'} Try {enc.get('tryNo') or '?'}"
        snap["queued"] = len(pending) - 1
        return snap
