"""B31.2: stored per-pull raw logs may be plain (raw.log) or gzip (raw.log.gz).

Combat logs compress ~10x.  All readers go through these helpers so a try folder
works the same either way.  Compression keeps the original mtime on the .gz so
existing cache-freshness checks (raw mtime vs raid_facts mtime) stay valid.
"""
from __future__ import annotations

import gzip
import os
import shutil
from pathlib import Path

RAW_NAME = "raw.log"
GZ_NAME = "raw.log.gz"
PREROLL_NAME = "preroll.log"
PREROLL_GZ_NAME = "preroll.log.gz"


def resolve_raw(path) -> Path:
    """Return the existing raw log for a try folder or a raw.log path.

    Accepts the folder, `.../raw.log` or `.../raw.log.gz`.  Prefers the plain file
    when both exist (e.g. mid-compression).  If neither exists, returns the plain
    path so callers' `.exists()` checks behave as before.
    """
    p = Path(path)
    if p.is_dir():
        folder = p
    elif p.name in (RAW_NAME, GZ_NAME):
        folder = p.parent
    else:
        return p
    plain, gz = folder / RAW_NAME, folder / GZ_NAME
    if plain.exists():
        return plain
    if gz.exists():
        return gz
    return plain


def raw_exists(path) -> bool:
    return resolve_raw(path).exists()


def is_gz(path) -> bool:
    return str(path).endswith(".gz")


def open_raw_text(path):
    p = resolve_raw(path)
    if is_gz(p):
        return gzip.open(p, "rt", encoding="utf-8", errors="replace", newline="")
    return Path(p).open("r", encoding="utf-8", errors="replace", newline="")


def raw_uncompressed_size(path) -> int:
    """Uncompressed byte size (gzip ISIZE trailer; exact below 4GB)."""
    p = resolve_raw(path)
    try:
        if is_gz(p):
            with open(p, "rb") as fh:
                fh.seek(-4, os.SEEK_END)
                return int.from_bytes(fh.read(4), "little")
        return os.path.getsize(p)
    except OSError:
        return 0



def resolve_preroll(path) -> Path:
    """Return the existing optional pre-pull log for a try folder/path."""
    p = Path(path)
    if p.is_dir():
        folder = p
    elif p.name in (PREROLL_NAME, PREROLL_GZ_NAME, RAW_NAME, GZ_NAME):
        folder = p.parent
    else:
        folder = p.parent if p.suffix else p
    plain, gz = folder / PREROLL_NAME, folder / PREROLL_GZ_NAME
    if plain.exists():
        return plain
    if gz.exists():
        return gz
    return plain


def open_preroll_text(path):
    p = resolve_preroll(path)
    if str(p).endswith(".gz"):
        return gzip.open(p, "rt", encoding="utf-8", errors="replace", newline="")
    return Path(p).open("r", encoding="utf-8", errors="replace", newline="")


def compress_named_log(path, plain_name: str, gz_name: str, remove_original: bool = True) -> Path | None:
    """Safely gzip one named log in a try folder while preserving mtime."""
    p = Path(path)
    folder = p if p.is_dir() else p.parent
    plain, gz = folder / plain_name, folder / gz_name
    if not plain.exists():
        return gz if gz.exists() else None
    st = plain.stat()
    tmp = folder / (gz_name + ".tmp")
    try:
        with plain.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        with open(tmp, "rb") as fh:
            fh.seek(-4, os.SEEK_END)
            isize = int.from_bytes(fh.read(4), "little")
        if isize != (st.st_size & 0xFFFFFFFF):
            tmp.unlink(missing_ok=True)
            return None
        os.replace(tmp, gz)
        os.utime(gz, ns=(st.st_atime_ns, st.st_mtime_ns))
        if remove_original:
            plain.unlink()
        return gz
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def compress_preroll(path, remove_original: bool = True) -> Path | None:
    return compress_named_log(path, PREROLL_NAME, PREROLL_GZ_NAME, remove_original=remove_original)

def compress_raw(path, remove_original: bool = True) -> Path | None:
    """Compress a try's raw.log to raw.log.gz safely."""
    return compress_named_log(path, RAW_NAME, GZ_NAME, remove_original=remove_original)

AUTO_COMPRESS_AFTER_REVIEW = True  # set False to keep plain raw.log files
