from __future__ import annotations

"""One-way, copy-only public migration of raid evidence from legacy sessions.

Never changes the developer's original files. Excludes personal extraction data.
Migrated pulls are staged under an invisible _-prefixed directory before the
atomic folder rename, and reruns skip already-migrated encounter identities.
"""

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from .io_utils import atomic_write_json

MARKER = '.public_legacy_migration_v1.json'
RAID_FILES = {
    'raw.log', 'raw.log.gz', 'preroll.log', 'preroll.log.gz',
    'raid_facts.json', 'raid_facts.txt', 'raid_review.json', 'raid_review.txt',
    'raid_review_b0.json', 'raid_review_b0.txt',
    'boss_mechanic_review.json', 'boss_mechanic_review.txt',
    'mechanic_review.json', 'mechanic_review.txt',
}
# Only data originating from raid-wide analysis can cross editions. Personal
# player/facts/death artifacts, absolute original paths, auth, settings do not.
META_CORE = {
    'schemaVersion', 'savedAt', 'encounterID', 'name', 'difficultyID',
    'difficultyName', 'groupSize', 'tryNo', 'startTimestamp', 'endTimestamp',
    'success', 'result', 'durationSec', 'eventCount', 'endReason',
    'rawLineCount', 'rawBytes', 'rawCompressed', 'prerollSec',
    'prerollLineCount', 'prerollBytes', 'prerollWindowSec', 'prerollMaxLines',
    'prerollBoundaryReason', 'prerollBoundaryTimestamp', 'resetPull',
    'autoResetPull', 'resetReason', 'analysisExcluded', 'analysisOverride',
    'deathCount', 'pullStart', 'raidReviewStatus', 'raidFactsStatus',
    'mechanicReviewStatus', 'mechanicVerdict', 'mechanicSummaryLine',
    'mechanicFirstMistake', 'mechanicCollapseDirect',
    'raidReviewModuleVersion', 'mechanicReviewEngine', 'mechanicReviewSchema',
}
ARTIFACT_NAMES = {
    'rawLog': ('raw.log.gz', 'raw.log'),
    'rawFile': ('raw.log.gz', 'raw.log'),
    'prerollLog': ('preroll.log.gz', 'preroll.log'),
    'prerollFile': ('preroll.log.gz', 'preroll.log'),
    'raidFactsJson': ('raid_facts.json',),
    'raidFactsTxt': ('raid_facts.txt',),
    'raidReviewJson': ('raid_review.json',),
    'raidReviewTxt': ('raid_review.txt',),
    'mechanicReviewJson': ('boss_mechanic_review.json', 'mechanic_review.json'),
    'mechanicReviewTxt': ('boss_mechanic_review.txt', 'mechanic_review.txt'),
}


def _key(meta):
    return (str(meta.get('startTimestamp') or '').strip(),
            str(meta.get('encounterID') or ''), str(meta.get('difficultyID') or ''))


def _metadata_files(root):
    if not root.is_dir():
        return
    for date in root.iterdir():
        if not date.is_dir() or date.name.startswith('_'):
            continue
        for boss in date.iterdir():
            if boss.is_dir():
                for attempt in boss.iterdir():
                    if attempt.is_dir() and attempt.name.startswith('try_') and (attempt / 'meta.json').is_file():
                        yield attempt / 'meta.json'


def _read(path):
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def migrate_legacy_public_sessions(legacy_root, public_root, progress=None) -> dict:
    src = Path(legacy_root).expanduser().resolve()
    dst = Path(public_root).expanduser().resolve()
    result = {'copied': 0, 'skipped': 0, 'invalid': 0, 'alreadyMigrated': False}
    if src == dst or not src.is_dir():
        return result
    marker = dst / MARKER
    if marker.exists():
        result['alreadyMigrated'] = True
        return result
    dst.mkdir(parents=True, exist_ok=True)
    # Source tree remains read-only. Only own transient stage is modified.
    staging_parent = dst / '_migration_tmp'
    staging_parent.mkdir(exist_ok=True)
    for residue in staging_parent.iterdir():
        if residue.is_dir():
            shutil.rmtree(residue, ignore_errors=True)
    # Raid-wide series summaries do not contain the personal facts pipeline.
    # Preserve historical boss-level comparisons without importing personal
    # character artifacts. Never replace a newer PUBLIC summary.
    source_series = src / '_series'
    if source_series.is_dir():
        for series_dir in source_series.iterdir():
            if not series_dir.is_dir() or not series_dir.name.rsplit('_d', 1)[-1].isdigit():
                continue
            target_series = dst / '_series' / series_dir.name
            target_series.mkdir(parents=True, exist_ok=True)
            for fn in ('raid_series_review.json', 'raid_series_review.txt'):
                if (series_dir / fn).is_file() and not (target_series / fn).exists():
                    shutil.copy2(series_dir / fn, target_series / fn)
    present = set()
    for meta_path in _metadata_files(dst):
        meta = _read(meta_path)
        if meta:
            present.add(_key(meta))
    for meta_path in _metadata_files(src):
        meta = _read(meta_path)
        if not meta or not meta.get('endTimestamp') or not meta.get('startTimestamp'):
            result['invalid'] += 1
            continue
        key = _key(meta)
        if key in present:
            result['skipped'] += 1
            continue
        source_try = meta_path.parent
        rel = source_try.relative_to(src)
        dest_try = dst / rel
        if dest_try.exists():
            dest_try = dest_try.with_name(dest_try.name + '_public_migrated')
            if dest_try.exists():
                result['skipped'] += 1
                continue
        temporary = staging_parent / uuid.uuid4().hex
        try:
            temporary.mkdir()
            for p in source_try.iterdir():
                if p.is_file() and not p.is_symlink() and p.name in RAID_FILES:
                    shutil.copy2(p, temporary / p.name)
            if not (temporary / 'raw.log').exists() and not (temporary / 'raw.log.gz').exists():
                result['invalid'] += 1
                continue
            clean = {k: meta[k] for k in META_CORE if k in meta}
            for name, candidates in ARTIFACT_NAMES.items():
                # Preserve only copied local artifacts; never leak full paths.
                for filename in candidates:
                    if (temporary / filename).is_file():
                        clean[name] = filename
                        break
            if not (temporary / 'raid_review.json').is_file():
                clean.pop('raidReviewStatus', None)
            if not (temporary / 'raid_facts.json').is_file():
                clean.pop('raidFactsStatus', None)
            target_series = dst / '_series' / f"{meta.get('encounterID')}_d{meta.get('difficultyID')}"
            if (target_series / 'raid_series_review.json').is_file():
                for key, filename in (('raidSeriesReviewJson', 'raid_series_review.json'),
                                      ('raidSeriesReviewTxt', 'raid_series_review.txt')):
                    artifact = target_series / filename
                    if artifact.is_file():
                        clean[key] = os.path.relpath(artifact, start=dest_try)
                clean['raidSeriesReviewStatus'] = 'ready'
            atomic_write_json(temporary / 'meta.json', clean)
            dest_try.parent.mkdir(parents=True, exist_ok=True)
            temporary.rename(dest_try)
            present.add(key)
            result['copied'] += 1
            if progress and result['copied'] % 10 == 0:
                progress(dict(result))
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
    atomic_write_json(marker, {'finishedAt': time.strftime('%Y-%m-%dT%H:%M:%S'), **result})
    return result


def cleanup_stale_active(root, min_age_sec=24 * 3600) -> int:
    """Only remove old orphan partial files, never touch recently active writes."""
    active = Path(root) / '_active'
    if not active.is_dir():
        return 0
    now = time.time()
    count = 0
    for p in active.glob('pull_*.log.part'):
        try:
            if p.is_file() and now - p.stat().st_mtime > min_age_sec:
                p.unlink()
                count += 1
        except OSError:
            pass
    return count
