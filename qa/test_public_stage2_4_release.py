"""Stage 2.4 release validation without requiring a Windows/Tk display."""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import public_errorlog as errorlog
from public_first_run import maybe_show_first_run
from package_public_release import package, validate

class FakeApp:
    _public_import_running = False
    live_running = False
    _public_migration_running = False
    def __init__(self):
        self.selected = 0
        self.callbacks = []
    def _public_select_log_directory(self):
        self.selected += 1
    def after(self, delay, callback):
        self.callbacks.append((delay, callback))
    def winfo_exists(self):
        return False

class ReleaseTests(unittest.TestCase):
    def test_welcome_once_and_settings_remain(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            root.mkdir(exist_ok=True)
            (root/'settings.json').write_text(json.dumps({'lastLogDir':'X:/Game/_retail_/Logs'}),encoding='utf-8')
            asked=[]
            app=FakeApp()
            self.assertTrue(maybe_show_first_run(app,root,lambda *a,**kw: asked.append(a) or True))
            self.assertEqual(app.selected,1)
            self.assertEqual(len(asked),1)
            self.assertFalse(maybe_show_first_run(app,root,lambda *a,**kw: self.fail('shown twice')))
            payload=json.loads((root/'settings.json').read_text(encoding='utf-8'))
            self.assertTrue(payload['firstRunGuideShown'])
            self.assertEqual(payload['lastLogDir'],'X:/Game/_retail_/Logs')

    def test_welcome_does_not_interrupt_running_import(self):
        with tempfile.TemporaryDirectory() as t:
            app=FakeApp(); app._public_import_running=True
            self.assertFalse(maybe_show_first_run(app,Path(t),lambda *a,**kw:False))
            self.assertFalse((Path(t)/'settings.json').exists())
            self.assertEqual(len(app.callbacks),1)
            app._public_import_running=False
            app.callbacks[0][1]()
            self.assertTrue((Path(t)/'settings.json').exists())

    def test_error_notice_once_even_two_exceptions(self):
        with tempfile.TemporaryDirectory() as t, patch.dict(os.environ,{'NINI_PUBLIC_HOME':t}):
            app=FakeApp()
            errorlog._PENDING_NOTICE.clear()
            errorlog._NOTICE_SHOWN=False
            for x in range(2):
                try: raise RuntimeError(f'public synthetic error {x}')
                except RuntimeError: errorlog.log_exception(*sys.exc_info())
            errorlog.start_error_notice_poll(app,interval_ms=3)
            self.assertEqual(len(app.callbacks),1)
            with patch('tkinter.messagebox.showerror') as dialog:
                app.callbacks.pop()[1]()
                self.assertEqual(dialog.call_count,1)
                self.assertIn('error.log',dialog.call_args.args[1])
                # Repeated callback remains silent.
                errorlog.log_exception(RuntimeError,RuntimeError('third'),None)
                errorlog.start_error_notice_poll(app,interval_ms=3)
                app.callbacks.pop()[1]()
                self.assertEqual(dialog.call_count,1)
            content=(Path(t)/'error.log').read_text(encoding='utf-8')
            self.assertIn('public synthetic error 0',content)
            self.assertIn('public synthetic error 1',content)
            errorlog._PENDING_NOTICE.clear(); errorlog._NOTICE_SHOWN=False

    def test_release_zip_is_real_onedir_contents_and_hash(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); app=root/'dist'/'NiniAnalyzer_Public'
            (app/'_internal'/'resources'/'brand_public').mkdir(parents=True)
            (app/'NiniAnalyzer_Public.exe').write_bytes(b'fake exe for testing')
            (app/'_internal'/'NiniAnalyzer_Public.version').write_text('1.0.0-rc5.4\n',encoding='ascii')
            (app/'_internal'/'resources'/'brand_public'/'nini_analyzer.ico').write_bytes(b'ico bytes')
            (app/'_internal'/'data'/'skill_icons').mkdir(parents=True)
            (app/'_internal'/'assets'/'fonts').mkdir(parents=True)
            for _n in ('PyeojinGothic-Medium.ttf','Paperlogy-6SemiBold.ttf','LICENSE_PyeojinGothic_OFL.txt','LICENSE_Paperlogy_OFL.txt'): (app/'_internal'/'assets'/'fonts'/_n).write_bytes(b'x')
            (app/'_internal'/'live'/'boss_mechanics'/'rules').mkdir(parents=True)
            (app/'_internal'/'python312.dll').write_bytes(b'test only')
            guide=root/'guide.txt'; guide.write_text('First run docs',encoding='utf-8')
            z=root/'result.zip'
            self.assertEqual(package(app,z,guide),z)
            with zipfile.ZipFile(z) as f:
                self.assertEqual(f.testzip(),None)
                self.assertIn('NiniAnalyzer_Public/NiniAnalyzer_Public.exe',f.namelist())
                self.assertIn('NiniAnalyzer_Public/START_HERE_PUBLIC.txt',f.namelist())
                self.assertIn('NiniAnalyzer_Public/_internal/python312.dll',f.namelist())
            self.assertTrue((root/'result.zip.sha256.txt').exists())

    def test_release_refuses_mislabeled_old_build(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            (root/'_internal'/'resources'/'brand_public').mkdir(parents=True)
            (root/'_internal'/'resources'/'brand_public'/'nini_analyzer.ico').write_bytes(b'ico')
            (root/'NiniAnalyzer_Public.exe').write_bytes(b'exe')
            (root/'_internal'/'NiniAnalyzer_Public.version').write_text('1.0.0-rc1\n',encoding='ascii')
            with self.assertRaisesRegex(ValueError, 'not a genuine rc5.4'): validate(root)

    def test_release_rejects_session_data_and_archive(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            (root/'_internal'/'resources'/'brand_public').mkdir(parents=True)
            (root/'_internal'/'resources'/'brand_public'/'nini_analyzer.ico').write_bytes(b'ico')
            (root/'NiniAnalyzer_Public.exe').write_bytes(b'exe')
            (root/'_internal'/'NiniAnalyzer_Public.version').write_text('1.0.0-rc5.4\n',encoding='ascii')
            (root/'_internal'/'data'/'skill_icons').mkdir(parents=True)
            (root/'_internal'/'assets'/'fonts').mkdir(parents=True)
            for _n in ('PyeojinGothic-Medium.ttf','Paperlogy-6SemiBold.ttf','LICENSE_PyeojinGothic_OFL.txt','LICENSE_Paperlogy_OFL.txt'): (root/'_internal'/'assets'/'fonts'/_n).write_bytes(b'x')
            (root/'_internal'/'live'/'boss_mechanics'/'rules').mkdir(parents=True)
            (root/'_internal'/'data'/'live_sessions').mkdir(parents=True)
            (root/'_internal'/'data'/'live_sessions'/'Try001.log').write_text('do not ship')
            with self.assertRaises(ValueError): validate(root)

if __name__ == '__main__': unittest.main()
