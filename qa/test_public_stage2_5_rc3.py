"""RC3 packaging/layout/privacy contract; source tests, not Windows EXE runtime QA."""
from __future__ import annotations
import hashlib
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from build_public_windows import build_command
from package_public_release import package,validate, VERSION, ALLOWED_ROOT
from public_first_run import maybe_show_first_run


def mock_windows_layout(folder: Path, version=VERSION):
    internal=folder/'_internal'
    (internal/'resources'/'brand_public').mkdir(parents=True)
    (internal/'data'/'skill_icons').mkdir(parents=True)
    (internal/'assets'/'fonts').mkdir(parents=True)
    for _n in ('PyeojinGothic-Medium.ttf','Paperlogy-6SemiBold.ttf','LICENSE_PyeojinGothic_OFL.txt','LICENSE_Paperlogy_OFL.txt'):
        (internal/'assets'/'fonts'/_n).write_bytes(b'x')
    (internal/'live'/'boss_mechanics'/'rules').mkdir(parents=True)
    (folder/'NiniAnalyzer_Public.exe').write_bytes(b'test-Windows-EXE-placeholder')
    (internal/'NiniAnalyzer_Public.version').write_text(version+'\n', encoding='ascii')
    (internal/'resources'/'brand_public'/'nini_analyzer.ico').write_bytes(b'test-BLUE-ico')
    (internal/'data'/'skill_icons'/'123.jpeg').write_bytes(b'test-skill-icon')
    (internal/'live'/'boss_mechanics'/'rules'/'test.json').write_text('{}',encoding='utf-8')
    (internal/'python312.dll').write_bytes(b'test-DLL')


class FakeApp:
    _public_import_running=False
    _public_migration_running=False
    live_running=False
    def __init__(self, found=None):
        self._public_log_dir=found
        self.selected=0
    def _public_select_log_directory(self): self.selected+=1
    def after(self, delay,cb): raise AssertionError('no busy worker')


class RC3Tests(unittest.TestCase):
    def test_pyinstaller_onedir_internal_and_numpy_exclusion(self):
        cmd=build_command(data_sep=';')
        self.assertEqual(cmd[cmd.index('--contents-directory')+1], '_internal')
        self.assertIn('--exclude-module=numpy',cmd)
        self.assertIn('--onedir',cmd)
        self.assertNotIn('--onefile',cmd)
        self.assertIn('--hidden-import=live.raid_review',cmd)
        self.assertNotIn('brand_personal',' '.join(cmd))
        self.assertTrue(any(x.endswith(';resources/brand_public') for x in cmd))

    def test_runtime_asset_root_follows_meipass_contents(self):
        for name in ('nini_raid_gui.py','nini_wcl_gui.py'):
            src=(ROOT/name).read_text(encoding='utf-8')
            self.assertIn('WORKDIR = Path(__file__).resolve().parent',src)
        self.assertIn('WORKDIR / "resources" / "brand_public"',(ROOT/'nini_raid_gui.py').read_text(encoding='utf-8'))
        self.assertIn('ROOT / "data" / "skill_icons"',(ROOT/'live/skill_tips.py').read_text(encoding='utf-8'))

    def test_release_exposes_only_exe_internal_readme(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)/'NiniAnalyzer_Public'
            mock_windows_layout(folder)
            guide=Path(t)/'intro.txt';guide.write_text('Welcome',encoding='utf-8')
            destination=Path(t)/'rc3.zip'
            package(folder,destination,guide)
            self.assertEqual({p.name for p in folder.iterdir()},ALLOWED_ROOT)
            with zipfile.ZipFile(destination) as z:
                names=z.namelist()
                self.assertIsNone(z.testzip())
                self.assertEqual({'/'.join(x.split('/')[:2]) for x in names}, {'NiniAnalyzer_Public/'+n for n in ALLOWED_ROOT})
                self.assertIn('NiniAnalyzer_Public/_internal/data/skill_icons/123.jpeg',names)
                self.assertIn('NiniAnalyzer_Public/_internal/live/boss_mechanics/rules/test.json',names)
                self.assertNotIn('NiniAnalyzer_Public/python312.dll',names)
            digest=hashlib.sha256(destination.read_bytes()).hexdigest()
            self.assertIn(digest,destination.with_suffix('.zip.sha256.txt').read_text())

    def test_release_rejects_old_flat_layout_and_extra_dll(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);mock_windows_layout(root)
            (root/'python312.dll').write_bytes(b'flat-dir surprise')
            with self.assertRaisesRegex(ValueError,'top-level'):
                validate(root)

    def test_release_rejects_rc2_marker(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);mock_windows_layout(root,'1.0.0-rc2')
            with self.assertRaisesRegex(ValueError,'not a genuine 1.0.8'):validate(root)

    def test_release_rejects_runtime_caches_even_in_internal(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);mock_windows_layout(root)
            (root/'_internal'/'data'/'icon_cache').mkdir(parents=True)
            (root/'_internal'/'data'/'icon_cache'/'cached.png').write_bytes(b'fake')
            with self.assertRaisesRegex(ValueError,'Private'):validate(root)

    def test_first_run_auto_detected_logs_shows_path_and_no_selection(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);logs=p/'_retail_'/'Logs';logs.mkdir(parents=True)
            app=FakeApp(logs); messages=[]
            self.assertTrue(maybe_show_first_run(app,p/'home',ask_fn=lambda *a,**k:self.fail('should not ask to select'),info_fn=lambda *a,**k:messages.append(a)))
            self.assertEqual(app.selected,0)
            self.assertEqual(len(messages),1)
            self.assertIn(str(logs),messages[0][1])
            self.assertFalse(maybe_show_first_run(app,p/'home',info_fn=lambda *a,**k:self.fail('repeated')))

    def test_first_run_missing_logs_still_asks_for_directory(self):
        with tempfile.TemporaryDirectory() as t:
            app=FakeApp(Path(t)/'not-installed'/'Logs');asked=[]
            self.assertTrue(maybe_show_first_run(app,Path(t)/'home',ask_fn=lambda *a,**k:asked.append(a) or True))
            self.assertEqual(app.selected,1)
            self.assertEqual(len(asked),1)

    def test_cmd_files_crlf_ascii_and_safety_gates(self):
        for name in ('Build_Public_EXE.cmd','Build_Installer_Only.cmd','Package_Public_Release.cmd','Run_Public_Python.cmd'):
            data=(ROOT/name).read_bytes()
            self.assertTrue(data.isascii())
            self.assertNotIn(b'\n',data.replace(b'\r\n',b''))
        data=(ROOT/'Build_Public_EXE.cmd').read_text()
        for expected in ('verify_release.py','test_public_no_drive_leak.py','test_public_stage2_4_release.py','test_public_stage2_5_rc3.py','package_public_release.py'):
            self.assertIn(expected,data)

if __name__=='__main__':unittest.main()
