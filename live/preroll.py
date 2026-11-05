from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .encounter_tracker import parse_combat_datetime, parse_log_line

DEFAULT_WINDOW_SEC = 10.0
DEFAULT_MAX_LINES = 50_000


@dataclass(frozen=True)
class PreRollSnapshot:
    lines: list[str]
    preroll_sec: float
    window_sec: float
    max_lines: int
    boundary_reason: str
    boundary_timestamp: str | None
    line_count: int


class PreRollBuffer:
    """Bounded live-combat-log history used only for pre-pull evidence.

    The buffer is intentionally separate from raw.log.  Consumers must opt in to
    reading preroll.log so existing encounter analysis remains byte-for-byte scoped
    to ENCOUNTER_START..ENCOUNTER_END.
    """

    def __init__(self, window_sec: float = DEFAULT_WINDOW_SEC, max_lines: int = DEFAULT_MAX_LINES):
        self.window_sec = max(0.1, float(window_sec))
        self.max_lines = max(1, int(max_lines))
        self._rows = deque()
        self.coverage_started_at: datetime | None = None
        self._line_cap_boundary_dt: datetime | None = None

    def reset(self, coverage_started_at: datetime | None = None):
        self._rows.clear()
        self.coverage_started_at = coverage_started_at or datetime.now()
        self._line_cap_boundary_dt = None

    def set_coverage_start(self, value: datetime | None = None):
        self.coverage_started_at = value or datetime.now()

    @staticmethod
    def _row(raw_line: str):
        text = str(raw_line).rstrip("\r\n")
        parsed = parse_log_line(text)
        if not parsed:
            return {"raw": text, "parsed": None, "dt": None, "event": None, "timestamp": None}
        ts = parsed.get("timestamp")
        dt = parse_combat_datetime(str(ts or "")) if ts else None
        return {
            "raw": text,
            "parsed": parsed,
            "dt": dt,
            "event": parsed.get("event"),
            "timestamp": ts,
        }

    def append(self, raw_line: str):
        row = self._row(raw_line)
        self._rows.append(row)
        while len(self._rows) > self.max_lines:
            dropped = self._rows.popleft()
            dropped_dt = dropped.get("dt")
            if dropped_dt is not None:
                self._line_cap_boundary_dt = dropped_dt

        newest_dt = row.get("dt")
        if newest_dt is not None:
            cutoff = newest_dt - timedelta(seconds=self.window_sec)
            if self._line_cap_boundary_dt is not None and self._line_cap_boundary_dt <= cutoff:
                self._line_cap_boundary_dt = None
            while self._rows:
                first_dt = self._rows[0].get("dt")
                # Undated/unparsed lines cannot safely extend the time window; retain
                # them only while they are adjacent to dated rows and line-bounded.
                if first_dt is None or first_dt >= cutoff:
                    break
                self._rows.popleft()

    def extend(self, lines: Iterable[str]):
        for line in lines:
            self.append(line)

    def snapshot_before(self, encounter_start_timestamp: str | None) -> PreRollSnapshot:
        start_dt = parse_combat_datetime(str(encounter_start_timestamp or ""))
        rows = list(self._rows)
        if start_dt is None:
            return PreRollSnapshot(
                lines=[r["raw"] for r in rows[-self.max_lines:]],
                preroll_sec=0.0,
                window_sec=self.window_sec,
                max_lines=self.max_lines,
                boundary_reason="unknown-start-time",
                boundary_timestamp=None,
                line_count=min(len(rows), self.max_lines),
            )

        # Never allow the previous encounter into the next pull's evidence.  The
        # ENCOUNTER_END line itself is a boundary marker and is not saved.
        last_end_idx = None
        last_end_dt = None
        last_end_ts = None
        for idx, row in enumerate(rows):
            if row.get("event") == "ENCOUNTER_END":
                dt = row.get("dt")
                if dt is None or dt <= start_dt:
                    last_end_idx = idx
                    last_end_dt = dt
                    last_end_ts = row.get("timestamp")
        if last_end_idx is not None:
            rows = rows[last_end_idx + 1 :]

        window_start = start_dt - timedelta(seconds=self.window_sec)
        coverage_start = self.coverage_started_at
        boundary_reason = "window"
        boundary_dt = window_start
        boundary_ts = window_start.isoformat(timespec="milliseconds")

        if coverage_start is not None and coverage_start > boundary_dt:
            boundary_reason = "monitor-start"
            boundary_dt = coverage_start
            boundary_ts = coverage_start.isoformat(timespec="milliseconds")
        if self._line_cap_boundary_dt is not None and self._line_cap_boundary_dt > boundary_dt:
            boundary_reason = "line-cap"
            boundary_dt = self._line_cap_boundary_dt
            boundary_ts = self._line_cap_boundary_dt.isoformat(timespec="milliseconds")
        if last_end_dt is not None and last_end_dt > boundary_dt:
            boundary_reason = "previous-encounter-end"
            boundary_dt = last_end_dt
            boundary_ts = last_end_ts or last_end_dt.isoformat(timespec="milliseconds")

        kept = []
        for row in rows:
            dt = row.get("dt")
            if dt is not None and dt < window_start:
                continue
            if dt is not None and dt >= start_dt:
                continue
            kept.append(row)
        preroll_sec = max(0.0, min(self.window_sec, (start_dt - boundary_dt).total_seconds()))
        return PreRollSnapshot(
            lines=[r["raw"] for r in kept],
            preroll_sec=round(preroll_sec, 3),
            window_sec=self.window_sec,
            max_lines=self.max_lines,
            boundary_reason=boundary_reason,
            boundary_timestamp=boundary_ts,
            line_count=len(kept),
        )
