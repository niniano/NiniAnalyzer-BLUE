"""B31.1: real progress reporting for the live review pipeline.

A LiveProgress object is created per analysed pull.  The two raw.log parsers
report how far into the file they are (estimated bytes / file size) through a
thread-local hook, so the percentage follows the real work instead of a fixed
animation.  Stage ranges come from measured timings on 2026-09-27 3445 mythic
logs (32-76MB): the two parsing passes are ~90% of the wall time.

The percentage never moves backwards and only reaches 100 in finish().
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager

# key: (label, start%, end%)
LIVE_STAGES = {
    "read": ("로그 읽기 · 개인 이벤트 파싱", 0.0, 38.0),
    "parse": ("공대 이벤트 파싱", 38.0, 86.0),
    "mechanics": ("기믹 매핑 · 사망 체인", 86.0, 92.0),
    "death": ("개인 사망 분석", 92.0, 94.0),
    "collapse": ("공대 붕괴 체인", 94.0, 98.0),
    "save": ("보고서 저장", 98.0, 99.5),
}

_local = threading.local()


class LiveProgress:
    def __init__(self, stages: dict | None = None):
        self.stages = stages or LIVE_STAGES
        self.pct = 0.0
        self.stage = "대기"
        self.done = False
        self.failed = False
        self._lock = threading.Lock()

    def _set(self, pct: float, label: str | None = None):
        with self._lock:
            pct = max(self.pct, min(99.5, float(pct)))
            self.pct = pct
            if label:
                self.stage = label

    def enter(self, key: str):
        label, start, _end = self.stages[key]
        self._set(start, label)

    def fraction(self, key: str, frac: float):
        label, start, end = self.stages[key]
        frac = max(0.0, min(1.0, float(frac)))
        self._set(start + (end - start) * frac, label)

    def finish(self, ok: bool = True, label: str | None = None):
        with self._lock:
            if ok:
                self.pct = 100.0
            self.done = True
            self.failed = not ok
            self.stage = label or ("완료" if ok else "오류")

    def snapshot(self) -> dict:
        with self._lock:
            return {"pct": round(self.pct, 1), "stage": self.stage, "done": self.done, "failed": self.failed}


def current() -> LiveProgress | None:
    return getattr(_local, "progress", None)


@contextmanager
def active(progress: LiveProgress | None):
    prev = getattr(_local, "progress", None)
    _local.progress = progress
    try:
        yield progress
    finally:
        _local.progress = prev


class FileLineProgress:
    """Cheap per-line progress for a text-mode file loop.

    Text iteration disables f.tell(), so we count characters and convert them
    to bytes with the UTF-8 ratio measured on the first lines (Korean names make
    lines multi-byte).  Only every `every` lines does real work.
    """

    def __init__(self, path, stage_key: str, every: int = 20000):
        self.progress = current()
        self.key = stage_key
        self.every = every
        self.n = 0
        self.chars = 0
        self.sample_chars = 0
        self.sample_bytes = 0
        from .rawlog import raw_uncompressed_size
        self.size = raw_uncompressed_size(path)
        if self.progress is not None:
            self.progress.enter(stage_key)

    def tick(self, line: str):
        if self.progress is None or not self.size:
            return
        self.n += 1
        self.chars += len(line)
        if self.n <= 2000:
            self.sample_chars += len(line)
            self.sample_bytes += len(line.encode("utf-8", "replace"))
        if self.n % self.every:
            return
        ratio = (self.sample_bytes / self.sample_chars) if self.sample_chars else 1.0
        self.progress.fraction(self.key, min(0.999, self.chars * ratio / self.size))

    def close(self):
        if self.progress is not None:
            self.progress.fraction(self.key, 1.0)


def file_progress(path, stage_key: str) -> FileLineProgress:
    return FileLineProgress(path, stage_key)


def enter(stage_key: str):
    p = current()
    if p is not None:
        p.enter(stage_key)
