from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from .encounter_tracker import parse_combat_datetime
from .io_utils import atomic_write_json, atomic_write_text
from .selection_context import session_date_from_log_path, session_date_from_timestamp
from .reset_policy import is_reset_pull
from .mechanics import is_fake_pull
from .player_identity import IDENTITY_SOURCE, infer_tracked_player, apply_identity_to_meta


_INVALID_WIN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_component(value: object, fallback: str = "unknown") -> str:
    text = str(value or "").strip()
    text = _INVALID_WIN_CHARS.sub("_", text)
    text = re.sub(r"\s+", "_", text).strip(" ._")
    return (text or fallback)[:96]


def _result_token(value: object) -> str:
    text = str(value or "END").strip().upper()
    if text == "KILL":
        return "KILL"
    if text == "WIPE":
        return "WIPE"
    if text in {"중단", "ABORT", "ABORTED"}:
        return "ABORT"
    return _safe_component(text, "END").upper()


class LivePullStore:
    """Stream one live encounter into a temporary raw log, then finalize it.

    v0.1-B intentionally stores the raw encounter exactly as the live tailer saw
    it (one decoded CombatLog line per line) plus a compact meta.json.  Keeping
    the active pull on disk avoids retaining a very large raid pull in memory.
    """

    def __init__(self, root: Path | str, tracked_players=None):
        self.root = Path(root)
        self.tracked_players = tuple(tracked_players or ())
        self.active_dir = self.root / "_active"
        self._fh = None
        self._tmp_path: Path | None = None
        self._source_log: str | None = None
        self._start_encounter: dict | None = None
        self._raw_line_count = 0
        self._raw_bytes = 0
        self._preroll_lines: list[str] = []
        self._preroll_meta: dict = {}

    @property
    def active(self) -> bool:
        return self._fh is not None

    def _ensure_dirs(self):
        self.active_dir.mkdir(parents=True, exist_ok=True)

    def start(
        self,
        encounter: dict,
        source_log: Path | str | None,
        first_line: str,
        *,
        preroll_lines: list[str] | None = None,
        preroll_meta: dict | None = None,
    ):
        # A stale capture should never swallow the new ENCOUNTER_START.
        if self.active:
            self.discard_active()
        self._ensure_dirs()
        token = uuid.uuid4().hex[:12]
        self._tmp_path = self.active_dir / f"pull_{token}.log.part"
        self._fh = self._tmp_path.open("w", encoding="utf-8", newline="\n")
        self._source_log = str(source_log) if source_log else None
        self._start_encounter = dict(encounter or {})
        self._raw_line_count = 0
        self._raw_bytes = 0
        self._preroll_lines = [str(x).rstrip("\r\n") for x in (preroll_lines or [])]
        self._preroll_meta = dict(preroll_meta or {})
        self.append(first_line)

    def append(self, raw_line: str):
        if not self.active:
            return
        line = str(raw_line).rstrip("\r\n")
        payload = line + "\n"
        self._fh.write(payload)
        self._fh.flush()
        self._raw_line_count += 1
        self._raw_bytes += len(payload.encode("utf-8", errors="replace"))

    def _close(self):
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
        self._fh = None

    def discard_active(self):
        self._close()
        if self._tmp_path:
            try:
                self._tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        self._tmp_path = None
        self._source_log = None
        self._start_encounter = None
        self._raw_line_count = 0
        self._raw_bytes = 0
        self._preroll_lines = []
        self._preroll_meta = {}

    def _session_date_for(self, encounter: dict, source_log: Path | str | None = None) -> str:
        start_text = encounter.get("startTimestamp") or (self._start_encounter or {}).get("startTimestamp") or ""
        start_dt = parse_combat_datetime(str(start_text)) or datetime.now()
        return (
            session_date_from_timestamp(start_text)
            or session_date_from_log_path(source_log if source_log is not None else self._source_log)
            or start_dt.strftime("%Y-%m-%d")
        )

    @staticmethod
    def _calendar_date_for(encounter: dict) -> str | None:
        start_text = encounter.get("startTimestamp") or ""
        start_dt = parse_combat_datetime(str(start_text))
        return start_dt.strftime("%Y-%m-%d") if start_dt is not None else None

    def max_saved_try_no(self, encounter: dict, source_log: Path | str | None = None) -> int:
        """Return the highest persisted Try number for this raid-night boss.

        B33.4 deliberately reads metadata instead of trusting folder ordering.
        We inspect both the logical raid-day folder and the literal calendar-day
        folder so pulls written by pre-B33.3 builds after midnight are still
        counted.  Every candidate meta is re-validated by logical session date,
        encounterID and difficultyID before its Try number is accepted.
        """
        row = dict(encounter or {})
        target_session = self._session_date_for(row, source_log)
        try:
            target_encounter = int(row.get("encounterID"))
        except (TypeError, ValueError):
            target_encounter = None
        try:
            target_difficulty = int(row.get("difficultyID"))
        except (TypeError, ValueError):
            target_difficulty = None
        if target_encounter is None:
            return 0

        date_names = {target_session}
        calendar_date = self._calendar_date_for(row)
        if calendar_date:
            date_names.add(calendar_date)

        highest = 0
        normal_pull_keys = set()
        saw_classifiable_meta = False
        saw_excluded_artifact = False
        for date_name in date_names:
            date_dir = self.root / str(date_name)
            if not date_dir.is_dir():
                continue
            prefix = f"{target_encounter}_"
            try:
                boss_dirs = [x for x in date_dir.iterdir() if x.is_dir() and x.name.startswith(prefix)]
            except OSError:
                continue
            for boss_dir in boss_dirs:
                try:
                    try_dirs = [x for x in boss_dir.iterdir() if x.is_dir() and x.name.startswith("try_")]
                except OSError:
                    continue
                for try_dir in try_dirs:
                    meta_path = try_dir / "meta.json"
                    meta = None
                    if meta_path.exists():
                        try:
                            loaded = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
                            if isinstance(loaded, dict):
                                meta = loaded
                        except Exception:
                            meta = None
                    if meta is None:
                        # Folder-name fallback is safe only inside the logical
                        # raid-day folder; calendar-date compatibility folders
                        # need metadata to prove session membership.
                        if str(date_name) != str(target_session):
                            continue
                        m = re.match(r"^try_(\d+)_", try_dir.name)
                        if m:
                            highest = max(highest, int(m.group(1)))
                        continue
                    saw_classifiable_meta = True
                    try:
                        meta_encounter = int(meta.get("encounterID"))
                    except (TypeError, ValueError):
                        continue
                    try:
                        meta_difficulty = int(meta.get("difficultyID")) if meta.get("difficultyID") is not None else None
                    except (TypeError, ValueError):
                        meta_difficulty = None
                    meta_session = (
                        session_date_from_timestamp(meta.get("startTimestamp"))
                        or str(date_name)
                    )
                    if meta_encounter != target_encounter:
                        continue
                    if target_difficulty is not None and meta_difficulty != target_difficulty:
                        continue
                    if str(meta_session) != str(target_session):
                        continue
                    # B37.3.4: only completed, non-reset pulls establish the
                    # persisted Try floor.  Old 0.02s marker artifacts and
                    # interrupted captures must not shift the next real pull.
                    if is_fake_pull(meta) or is_reset_pull(meta):
                        saw_excluded_artifact = True
                        continue
                    # Interrupted captures still represent a chronological pull;
                    # only reset/fake markers are removed from Try numbering.
                    start_key = str(meta.get("startTimestamp") or "").strip()
                    normal_pull_keys.add((meta_encounter, meta_difficulty, start_key or str(try_dir)))
                    try:
                        highest = max(highest, int(meta.get("tryNo") or 0))
                    except (TypeError, ValueError):
                        pass
        # B37.3.4 canonical numbering is the count of completed, non-reset pulls,
        # not the largest legacy Try number.  This repairs old nights where a
        # 0.02s marker artifact made all later real pulls off by one.
        if saw_classifiable_meta and saw_excluded_artifact:
            return len(normal_pull_keys)
        return highest



    @staticmethod
    def _read_meta_file(meta_path: Path) -> dict:
        try:
            value = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _resolve_saved_artifact(try_dir: Path, value: object) -> Path | None:
        if not value:
            return None
        try:
            p = Path(str(value))
        except Exception:
            return None
        if p.is_absolute():
            return p
        # B38 SPEED1: try folders are absolute, so a textual join + normpath gives
        # the same result as resolve() without a Windows GetFinalPathName call
        # per artifact (26k calls on a 600-Try store).
        if Path(try_dir).is_absolute():
            return Path(os.path.normpath(os.path.join(str(try_dir), str(p))))
        try:
            return (try_dir / p).resolve(strict=False)
        except Exception:
            return try_dir / p

    def _migrate_saved_player_identity(self, try_dir: Path, meta: dict) -> dict:
        """One-time B34.1.1 migration from GUI-selected owner to raw-log owner."""
        if not self.tracked_players:
            return meta
        # A conclusive/attempted v1 scan is persisted so old sessions are not
        # rescanned on every app launch.
        if str(meta.get("playerIdentitySource") or "") == IDENTITY_SOURCE:
            return meta
        raw_value = meta.get("rawLog") or meta.get("rawFile") or "raw.log"
        raw_path = self._resolve_saved_artifact(try_dir, raw_value) or (try_dir / "raw.log")
        if not raw_path.exists():
            for candidate_name in ("raw.log.gz", "raw.log"):
                candidate = try_dir / candidate_name
                if candidate.exists():
                    raw_path = candidate
                    break
        result = infer_tracked_player(raw_path, self.tracked_players)
        migrated, changed = apply_identity_to_meta(meta, result, try_dir=try_dir)
        if changed:
            try:
                atomic_write_json(try_dir / "meta.json", migrated)
            except Exception:
                pass
        return migrated

    def load_saved_rows(self, migrate_identity: bool = True) -> list[dict]:
        """Hydrate persisted Live pulls from ``meta.json`` files.

        B34.1 makes the on-disk session store the canonical source for the shared
        date -> boss -> Try navigator.  Runtime ``tracker.recent`` rows are only a
        fresher overlay.  Every returned row keeps absolute paths to the original
        try directory and generated review artifacts so selecting an old Try does
        not depend on the hidden legacy Live list.
        """
        rows: list[dict] = []
        if not self.root.is_dir():
            return rows
        try:
            from .reanalysis import load_field_cache
            load_field_cache(self.root)
        except Exception:
            pass

        try:
            date_dirs = [x for x in self.root.iterdir() if x.is_dir() and not x.name.startswith("_")]
        except OSError:
            return rows

        artifact_keys = (
            "factsJson", "factsTxt", "raidFactsJson", "raidFactsTxt",
            "deathReviewJson", "deathReviewTxt", "raidReviewJson",
            "raidReviewTxt", "raidSeriesReviewJson", "raidSeriesReviewTxt",
        )
        status_files = {
            "factsStatus": "factsJson",
            "raidFactsStatus": "raidFactsJson",
            "deathReviewStatus": "deathReviewJson",
            "raidReviewStatus": "raidReviewJson",
            "raidSeriesReviewStatus": "raidSeriesReviewJson",
        }

        for date_dir in date_dirs:
            try:
                boss_dirs = [x for x in date_dir.iterdir() if x.is_dir()]
            except OSError:
                continue
            for boss_dir in boss_dirs:
                try:
                    try_dirs = [x for x in boss_dir.iterdir() if x.is_dir() and x.name.startswith("try_")]
                except OSError:
                    continue
                for try_dir in try_dirs:
                    meta_path = try_dir / "meta.json"
                    if not meta_path.is_file():
                        continue
                    meta = self._read_meta_file(meta_path)
                    if not meta:
                        continue
                    if migrate_identity:
                        meta = self._migrate_saved_player_identity(try_dir, meta)
                    row = dict(meta)
                    row["storageDir"] = str(try_dir)
                    row["metaJson"] = str(meta_path)
                    row["sessionDate"] = (
                        session_date_from_timestamp(row.get("startTimestamp"))
                        or date_dir.name
                    )

                    raw_value = row.get("rawLog") or row.get("rawFile") or "raw.log"
                    raw_path = self._resolve_saved_artifact(try_dir, raw_value)
                    if raw_path is not None and not raw_path.exists():
                        for candidate_name in ("raw.log.gz", "raw.log"):
                            candidate = try_dir / candidate_name
                            if candidate.exists():
                                raw_path = candidate
                                break
                    if raw_path is not None:
                        row["rawLog"] = str(raw_path)
                        row["rawCompressed"] = str(raw_path).endswith(".gz")

                    preroll_value = row.get("prerollLog") or row.get("prerollFile") or "preroll.log"
                    preroll_path = self._resolve_saved_artifact(try_dir, preroll_value)
                    if preroll_path is not None and not preroll_path.exists():
                        for candidate_name in ("preroll.log.gz", "preroll.log"):
                            candidate = try_dir / candidate_name
                            if candidate.exists():
                                preroll_path = candidate
                                break
                    if preroll_path is not None:
                        row["prerollLog"] = str(preroll_path)

                    for key in artifact_keys:
                        resolved = self._resolve_saved_artifact(try_dir, row.get(key))
                        if resolved is not None:
                            row[key] = str(resolved)
                    for status_key, path_key in status_files.items():
                        if not row.get(status_key):
                            path_value = row.get(path_key)
                            if path_value and Path(str(path_value)).exists():
                                row[status_key] = "ready"

                    # B34.2.1: mark legacy/missing generated reviews during the
                    # cheap meta hydration pass.  This reads only small JSON
                    # artifacts; raw.log is never parsed on the Tk UI thread.
                    try:
                        from .reanalysis import inspect_saved_row, inspect_series
                        row.update(inspect_saved_row(row))
                        if row.get("raidSeriesReviewJson"):
                            row.update(inspect_series(row.get("raidSeriesReviewJson")))
                    except Exception:
                        pass

                    rows.append(row)

        def _sort_key(row: dict):
            dt = parse_combat_datetime(str(row.get("startTimestamp") or ""))
            if dt is not None:
                return (dt, str(row.get("storageDir") or ""))
            return (datetime.min, str(row.get("storageDir") or ""))

        rows.sort(key=_sort_key)
        try:
            from .reanalysis import save_field_cache
            save_field_cache(self.root)
        except Exception:
            pass
        return rows

    def _unique_final_dir(self, encounter: dict) -> Path:
        start_text = encounter.get("startTimestamp") or (self._start_encounter or {}).get("startTimestamp") or ""
        start_dt = parse_combat_datetime(str(start_text)) or datetime.now()
        # B33.3+: storage folders use the same raid-night date as the shared
        # selection context. Existing folders are never migrated; this only
        # changes where newly finalized pulls are written.
        session_date = self._session_date_for(encounter)
        date_dir = self.root / session_date
        encounter_id = encounter.get("encounterID") or (self._start_encounter or {}).get("encounterID") or 0
        name = encounter.get("name") or (self._start_encounter or {}).get("name") or "unknown"
        diff = encounter.get("difficultyName") or (self._start_encounter or {}).get("difficultyName") or "unknown"
        boss_dir = date_dir / f"{int(encounter_id) if str(encounter_id).isdigit() else encounter_id}_{_safe_component(name)}_{_safe_component(diff)}"
        try_no = encounter.get("tryNo") or (self._start_encounter or {}).get("tryNo") or 0
        result = _result_token(encounter.get("result"))
        stamp = start_dt.strftime("%H%M%S")
        base = boss_dir / f"try_{int(try_no):03d}_{result}_{stamp}"
        candidate = base
        suffix = 2
        while candidate.exists():
            candidate = Path(f"{base}_{suffix:02d}")
            suffix += 1
        return candidate

    def finalize(self, encounter: dict) -> dict:
        if not self.active or not self._tmp_path:
            return {}
        self._close()
        final_dir = self._unique_final_dir(encounter or {})
        final_dir.mkdir(parents=True, exist_ok=False)
        raw_path = final_dir / "raw.log"
        try:
            shutil.move(str(self._tmp_path), str(raw_path))
        except Exception:
            # Fallback for an unusual cross-device root.
            shutil.copy2(self._tmp_path, raw_path)
            self._tmp_path.unlink(missing_ok=True)

        preroll_path = final_dir / "preroll.log"
        preroll_payload = "".join(line + "\n" for line in self._preroll_lines)
        # Keep a dedicated file even when there were no events.  Coverage can be
        # non-zero while the combat log was quiet, and meta.json records that fact.
        preroll_path.write_text(preroll_payload, encoding="utf-8", newline="\n")

        merged = dict(self._start_encounter or {})
        merged.update(encounter or {})
        meta = {
            "schemaVersion": 1,
            "savedAt": datetime.now().isoformat(timespec="seconds"),
            "sourceLog": self._source_log,
            "encounterID": merged.get("encounterID"),
            "name": merged.get("name"),
            "difficultyID": merged.get("difficultyID"),
            "difficultyName": merged.get("difficultyName"),
            "groupSize": merged.get("groupSize"),
            "tryNo": merged.get("tryNo"),
            "startTimestamp": merged.get("startTimestamp"),
            "endTimestamp": merged.get("endTimestamp"),
            "success": merged.get("success"),
            "result": merged.get("result"),
            "durationSec": merged.get("durationSec"),
            "eventCount": merged.get("eventCount"),
            "endReason": merged.get("endReason"),
            "rawLineCount": int(self._raw_line_count),
            "rawBytes": int(raw_path.stat().st_size if raw_path.exists() else self._raw_bytes),
            "rawFile": "raw.log",
            "prerollFile": "preroll.log",
            "prerollSec": round(float(self._preroll_meta.get("prerollSec") or 0.0), 3),
            "prerollLineCount": int(len(self._preroll_lines)),
            "prerollBytes": int(preroll_path.stat().st_size if preroll_path.exists() else 0),
            "prerollWindowSec": float(self._preroll_meta.get("windowSec") or 10.0),
            "prerollMaxLines": int(self._preroll_meta.get("maxLines") or 50000),
            "prerollBoundaryReason": self._preroll_meta.get("boundaryReason"),
            "prerollBoundaryTimestamp": self._preroll_meta.get("boundaryTimestamp"),
        }
        if self.tracked_players:
            identity = infer_tracked_player(raw_path, self.tracked_players)
            meta, _ = apply_identity_to_meta(meta, identity, try_dir=final_dir)
        if is_fake_pull(merged):
            meta['analysisExcluded'] = 'fake_pull'
        meta_path = final_dir / "meta.json"
        atomic_write_json(meta_path, meta)

        info = {
            "storageDir": str(final_dir),
            "rawLog": str(raw_path),
            "metaJson": str(meta_path),
            "rawLineCount": meta["rawLineCount"],
            "rawBytes": meta["rawBytes"],
            "prerollLog": str(preroll_path),
            "prerollSec": meta["prerollSec"],
            "prerollLineCount": meta["prerollLineCount"],
        }
        for key in (
            "factsPlayer", "factsSpecID", "factsPlayerOriginal",
            "playerIdentitySource", "playerIdentityCounts",
            "playerIdentityAmbiguous", "playerIdentityCorrected",
            "playerIdentityReanalysisRequired", "playerIdentityCheckedAt",
        ):
            if key in meta:
                info[key] = meta.get(key)
        self._tmp_path = None
        self._source_log = None
        self._start_encounter = None
        self._raw_line_count = 0
        self._raw_bytes = 0
        self._preroll_lines = []
        self._preroll_meta = {}
        return info
