"""Stage 2.3.1 public distribution: archive/Drive privacy guard."""
from __future__ import annotations
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live.public_import import import_public_combat_log
from live.raid_review import LiveRaidReviewMonitor
from live.public_paths import public_home
from public_errorlog import log_exception


class PublicNoDriveLeakTests(unittest.TestCase):
    def _import(self, root: Path, second: int) -> None:
        download = root / 'Downloads'
        download.mkdir(exist_ok=True)
        logfile = download / f'WoWCombatLog-test{second}.txt'
        # 1-sec reset + 8-sec ordinary Wipe: both archive paths must be disabled.
        logfile.write_text(
            '10/10/2026 19:00:00.000  ENCOUNTER_START,3445,"Test Boss",16,20\n'
            '10/10/2026 19:00:01.000  ENCOUNTER_END,3445,"Test Boss",16,20,0\n'
            '10/10/2026 19:01:00.000  ENCOUNTER_START,3445,"Test Boss",16,20\n'
            '10/10/2026 19:01:08.000  ENCOUNTER_END,3445,"Test Boss",16,20,0\n',encoding='utf-8')
        result = import_public_combat_log(logfile, root / 'public' / 'data' / 'live_sessions')
        self.assertEqual(result['imported'],2, result)
        self.assertEqual(result['errors'],0, result)

    def test_import_and_monitor_do_not_create_any_drive_files(self):
        import live.raid_review as rr
        import live.compact_archive as capsule
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            drive=root / 'Google Drive' / 'NiniWoWSync'
            syncroot=drive / 'WCLAnalyzer'
            syncroot.mkdir(parents=True)
            # Make a real, writable shared-folder surrogate. If the old
            # archive workflow runs, its destination resolver finds it.
            with patch.object(capsule, 'resolve_wcl_sync_root', return_value=syncroot) as finder, \
                 patch.object(rr, '_archive_after_review', wraps=rr._archive_after_review) as archived, \
                 patch.object(rr, 'sync_pending_archives', wraps=rr.sync_pending_archives) as replay:
                self._import(root,1)
                self.assertEqual(list(drive.rglob('*.wcla.zip')), [])
                self.assertEqual([p for p in drive.rglob('*') if p.is_file()], [])
                self.assertFalse((root/'public'/'data'/'archive_outbox').exists())
                monitor=LiveRaidReviewMonitor(root/'Downloads',storage_root=root/'public'/'data'/'live_sessions',raid_only=True)
                try:
                    events=monitor.start(start_at_end=True)
                    self.assertEqual(events[0]['archiveSync']['status'], 'disabled_public')
                    monitor.stop()
                finally:
                    monitor._facts_executor.shutdown(wait=True, cancel_futures=True)
                archived.assert_not_called()
                replay.assert_not_called()
                finder.assert_not_called()

    def test_public_reanalysis_never_creates_archive(self):
        from live.reanalysis import rebuild_saved_row
        import live.raid_review as rr
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            self._import(root,1)
            storage=root/'public'/'data'/'live_sessions'
            monitor=LiveRaidReviewMonitor(root/'Downloads',storage_root=storage,raid_only=True)
            try:
                rows=monitor.saved_rows(migrate_identity=False)
            finally:
                monitor._facts_executor.shutdown(wait=True,cancel_futures=True)
            self.assertTrue(rows)
            with patch.object(rr,'_archive_after_review',wraps=rr._archive_after_review) as archived:
                for row in rows:
                    rebuild_saved_row(row, storage,raid_only=True)
                archived.assert_not_called()
            self.assertFalse((root/'public'/'data'/'archive_outbox').exists())

    def test_public_icon_cache_is_user_writable_location(self):
        from nini_wcl_gui import App
        with tempfile.TemporaryDirectory() as d:
            app=object.__new__(App)
            object.__setattr__(app,'raid_public_mode',True)
            object.__setattr__(app,'_public_home',Path(d)/'NiniAnalyzer'/'Public')
            icon=App._icon_cache_path(app,'spell_fire')
            self.assertEqual(icon.parent,Path(d)/'NiniAnalyzer'/'Public'/'icon_cache')
            self.assertTrue(icon.parent.exists())

    def test_unhandled_errors_write_to_public_home_only(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {'NINI_PUBLIC_HOME':str(Path(d)/'public')}):
                try: raise RuntimeError('synthetic error for crash logger')
                except RuntimeError:
                    log_exception(*sys.exc_info())
                content=(public_home()/'error.log').read_text(encoding='utf-8')
                self.assertIn('synthetic error for crash logger',content)


if __name__=='__main__': unittest.main()
