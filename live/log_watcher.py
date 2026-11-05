from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class TailBatch:
    file_path: Path | None
    lines: list[str] = field(default_factory=list)
    start_offset: int = 0
    end_offset: int = 0
    switched: bool = False
    switch_reason: str | None = None


class CombatLogTailer:
    """Incrementally follow the newest WoWCombatLog-*.txt file.

    The cursor only advances through the last complete newline.  If WoW is in
    the middle of writing a line, that partial line is intentionally read again
    on the next poll instead of being emitted early.
    """

    def __init__(
        self,
        log_dir: Path | str,
        pattern: str = "WoWCombatLog-*.txt",
        max_read_bytes: int = 8 * 1024 * 1024,
    ):
        self.log_dir = Path(log_dir)
        self.pattern = pattern
        self.max_read_bytes = max(64 * 1024, int(max_read_bytes))
        self.current_file: Path | None = None
        self.offset = 0
        self.running = False
        self.total_lines = 0
        self.total_bytes = 0

    def _all_logs_newest_first(self) -> list[Path]:
        if not self.log_dir.exists():
            return []
        logs = list(self.log_dir.glob(self.pattern))

        def key(path: Path):
            try:
                st = path.stat()
                return st.st_mtime_ns, st.st_size
            except OSError:
                return 0, 0

        return sorted(logs, key=key, reverse=True)

    def latest_file(self) -> Path | None:
        logs = self._all_logs_newest_first()
        return logs[0] if logs else None

    def _attach(self, path: Path | None, *, start_at_end: bool, reason: str) -> TailBatch:
        old = self.current_file
        self.current_file = path
        if path is None:
            self.offset = 0
            return TailBatch(None, [], 0, 0, old is not None, reason)
        try:
            size = int(path.stat().st_size)
        except OSError:
            size = 0
        self.offset = size if start_at_end else 0
        return TailBatch(path, [], self.offset, self.offset, old != path, reason)

    def start(self, *, start_at_end: bool = True) -> TailBatch:
        self.running = True
        return self._attach(self.latest_file(), start_at_end=start_at_end, reason="start")

    def stop(self):
        self.running = False

    def reconnect_latest(self, *, start_at_end: bool = True) -> TailBatch:
        return self._attach(self.latest_file(), start_at_end=start_at_end, reason="manual-reconnect")

    def _maybe_rotate(self) -> TailBatch | None:
        latest = self.latest_file()
        if latest is None:
            if self.current_file is not None:
                return self._attach(None, start_at_end=False, reason="log-missing")
            return None
        if self.current_file is None:
            # Monitoring is already active, so a file that appears later is a
            # genuinely new live file. Start at byte 0 to avoid missing its
            # first ENCOUNTER_START.
            return self._attach(latest, start_at_end=False, reason="log-appeared")
        try:
            current_resolved = self.current_file.resolve()
            latest_resolved = latest.resolve()
        except OSError:
            current_resolved = self.current_file
            latest_resolved = latest
        if latest_resolved != current_resolved:
            return self._attach(latest, start_at_end=False, reason="new-log-file")
        return None

    def poll(self) -> TailBatch:
        if not self.running:
            return TailBatch(self.current_file, [], self.offset, self.offset)

        rotated = self._maybe_rotate()
        switched = bool(rotated and rotated.switched)
        switch_reason = rotated.switch_reason if rotated else None

        path = self.current_file
        if path is None:
            return TailBatch(None, [], self.offset, self.offset, switched, switch_reason)

        try:
            size = int(path.stat().st_size)
        except OSError:
            return TailBatch(path, [], self.offset, self.offset, switched, switch_reason)

        # Same file was truncated/recreated. Treat it as a fresh stream.
        if size < self.offset:
            self.offset = 0
            switched = True
            switch_reason = "truncated"

        if size <= self.offset:
            return TailBatch(path, [], self.offset, self.offset, switched, switch_reason)

        start = self.offset
        to_read = min(size - self.offset, self.max_read_bytes)
        try:
            with path.open("rb") as f:
                f.seek(self.offset)
                data = f.read(to_read)
        except OSError:
            return TailBatch(path, [], start, self.offset, switched, switch_reason)

        if not data:
            return TailBatch(path, [], start, self.offset, switched, switch_reason)

        last_newline = data.rfind(b"\n")
        if last_newline < 0:
            # Do not consume an incomplete line.
            return TailBatch(path, [], start, self.offset, switched, switch_reason)

        complete = data[: last_newline + 1]
        self.offset += len(complete)
        self.total_bytes += len(complete)
        text = complete.decode("utf-8", errors="replace")
        lines = text.splitlines()
        self.total_lines += len(lines)
        return TailBatch(path, lines, start, self.offset, switched, switch_reason)
