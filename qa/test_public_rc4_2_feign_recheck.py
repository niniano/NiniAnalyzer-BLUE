"""RC4.2: opt-in Feign Death recheck of saved Tries + series never rewrites other Tries."""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

H = 'Player-1-0000HUNT'
W = 'Player-1-0000WARR'
BOSS = 'Creature-0-1-1-1-1-0000000001'


def _ts(sec):
    m, s = divmod(sec, 60)
    return f'10/10/2026 20:{int(m):02d}:{s:06.3f}'


def _log(body, end=90.0):
    head = ['10/10/2026 20:00:00.000  ENCOUNTER_START,3445,"Test Boss",16,20,3004',
            f'{_ts(1)}  SWING_DAMAGE,{W},"Warrior-Realm",0x514,0x0,{BOSS},"Test Boss",0xa48,0x0,{W},0000000000000000,100,100,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,100,-1,1,0,0,0,nil,nil,nil']
    return head + body + [f'{_ts(end)}  ENCOUNTER_END,3445,"Test Boss",16,20,0,90000']


def _fd(sec):
    return [f'{_ts(sec)}  SPELL_CAST_SUCCESS,{H},"Hunter-Realm",0x514,0x0,0000000000000000,nil,0x80000000,0x80000000,5384,"Feign Death",0x1']


def _died(sec, guid=H, name='Hunter-Realm'):
    return f'{_ts(sec)}  UNIT_DIED,0000000000000000,nil,0x80000000,0x80000000,{guid},"{name}",0x514,0x0,0,0'


def _cast(sec):
    return f'{_ts(sec)}  SPELL_CAST_SUCCESS,{H},"Hunter-Realm",0x514,0x0,0000000000000000,nil,0x80000000,0x80000000,19434,"Aimed Shot",0x1'


def _make_try(root: Path, name: str, lines, stored_deaths, raw=True):
    d = root / '2026-10-10' / '3445_Test_Boss' / name
    d.mkdir(parents=True)
    meta = {"encounterID": 3445, "difficultyID": 16, "name": "Test Boss", "tryNo": int(name[-1]),
            "result": "Wipe", "startTimestamp": "10/10/2026 20:00:00.000",
            "endTimestamp": _ts(90), "durationSec": 90.0}
    (d / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')
    if raw:
        with gzip.open(d / 'raw.log.gz', 'wt', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
    facts = {"deaths": [{"guid": g, "name": n, "sec": s} for g, n, s in stored_deaths]}
    (d / 'raid_facts.json').write_text(json.dumps(facts), encoding='utf-8')
    (d / 'raid_review.json').write_text(json.dumps({"verdict": {"title": "old cause"}}), encoding='utf-8')
    return {"storageDir": str(d), "name": "Test Boss", "tryNo": meta["tryNo"], "result": "Wipe"}


def _hash_tree(root: Path):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Try 1: genuine feign stored (by an old build) as a death.
        self.r1 = _make_try(self.root, 'try_001', _log(_fd(10) + [_died(10.2), _cast(13), _died(30, W, 'Warrior-Realm')]),
                            [(H, 'Hunter-Realm', 10.2), (W, 'Warrior-Realm', 30.0)])
        # Try 2: FD then real death, no further action.
        self.r2 = _make_try(self.root, 'try_002', _log(_fd(10) + [_died(11)]), [(H, 'Hunter-Realm', 11.0)])
        # Try 3: no Feign Death at all -> not a candidate.
        self.r3 = _make_try(self.root, 'try_003', _log([_died(30, W, 'Warrior-Realm')]), [(W, 'Warrior-Realm', 30.0)])
        # Try 4: raw log missing -> listed, not selectable.
        self.r4 = _make_try(self.root, 'try_004', [], [(W, 'Warrior-Realm', 30.0)], raw=False)
        self.rows = [self.r1, self.r2, self.r3, self.r4]

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_is_read_only_and_lists_only_feign_tries(self):
        before = _hash_tree(self.root)
        from live.feign_recheck import scan_feign_candidates
        items = scan_feign_candidates(self.rows)
        self.assertEqual(_hash_tree(self.root), before, 'search must not change any file')
        by_try = {it['tryNo']: it for it in items}
        self.assertEqual(sorted(by_try), [1, 2, 4])
        self.assertEqual((by_try[1]['currentDeathCount'], by_try[1]['predictedDeathCount']), (2, 1))
        self.assertTrue(by_try[1]['selectable'])
        self.assertEqual((by_try[2]['currentDeathCount'], by_try[2]['predictedDeathCount']), (1, 1))
        self.assertEqual(by_try[4]['status'], 'no-raw')
        self.assertFalse(by_try[4]['selectable'])

    def test_scan_does_not_touch_shared_spell_name_cache(self):
        src = (ROOT / 'live' / 'raid_facts.py').read_text(encoding='utf-8')
        self.assertIn('persist_display_names: bool = True', src)
        rc = (ROOT / 'live' / 'feign_recheck.py').read_text(encoding='utf-8')
        self.assertIn('persist_display_names=False', rc)

    def test_recheck_only_selected_backs_up_first_and_reports_separately(self):
        from live.feign_recheck import recheck_selected, render_result
        calls = []

        def fake_rebuild(row, storage_root, raid_only=True):
            d = Path(row['storageDir'])
            # the backup must already exist when the pipeline starts
            calls.append((d.name, sorted(p.name for p in d.glob('reanalysis_backup_*.zip'))))
            facts = json.loads((d / 'raid_facts.json').read_text(encoding='utf-8'))
            facts['deaths'] = [x for x in facts['deaths'] if x['guid'] != H]
            facts['feignDeathsIgnored'] = [{'sec': 10.2, 'name': 'Hunter-Realm', 'evidenceSec': 13.0,
                                            'evidenceEvent': 'SPELL_CAST_SUCCESS', 'evidenceSpellName': 'Aimed Shot'}]
            (d / 'raid_facts.json').write_text(json.dumps(facts), encoding='utf-8')
            return {'lastReanalysisBackup': 'x.zip'}

        from live.reanalysis import backup_try_results
        # mimic rebuild_saved_row: backup first, then pipeline
        def rebuild(row, storage_root, raid_only=True):
            backup_try_results(row['storageDir'])
            return fake_rebuild(row, storage_root, raid_only)

        untouched = _hash_tree(Path(self.r2['storageDir']))
        res = recheck_selected([self.r1, self.r4], self.root, rebuild=rebuild)
        self.assertEqual(_hash_tree(Path(self.r2['storageDir'])), untouched)
        self.assertEqual([c[0] for c in calls], ['try_001'])
        self.assertTrue(calls[0][1], 'backup zip exists before reanalysis')
        self.assertEqual(res[0]['status'], 'done')
        self.assertEqual(res[1]['status'], 'skipped')
        self.assertIn('원본 로그', res[1]['reason'])
        ch = {c['field']: (c['before'], c['after']) for c in res[0]['changes']}
        self.assertEqual(ch['deathCount'], (2, 1))
        self.assertEqual(ch['firstDeath'], ('Hunter 0:10.2', 'Warrior 0:30.0'))
        txt = render_result(res[0])
        self.assertLess(txt.index('■ 로그 사실'), txt.index('■ 엔진 재판정'))
        self.assertIn('확정 아님', txt)
        self.assertIn('Aimed Shot', txt)

    def test_backup_failure_means_no_reanalysis(self):
        from live import feign_recheck
        called = []
        def rebuild(row, storage_root, raid_only=True):
            raise OSError('disk full during backup')
        res = feign_recheck.recheck_selected([self.r1], self.root, rebuild=rebuild)
        self.assertEqual(res[0]['status'], 'error')
        self.assertEqual(json.loads((Path(self.r1['storageDir']) / 'raid_facts.json').read_text())['deaths'][0]['sec'], 10.2)
        # series backup failure stops before the pipeline is even called
        series = self.root / '_series' / '3445_d16'
        series.mkdir(parents=True)
        (series / 'raid_series_review.json').write_text('{}', encoding='utf-8')
        import live.reanalysis as rn
        real = rn.backup_try_results
        rn.backup_try_results = lambda *_a, **_k: (_ for _ in ()).throw(OSError('no space'))
        try:
            res = feign_recheck.recheck_selected([self.r1], self.root, rebuild=lambda *a, **k: called.append(1))
        finally:
            rn.backup_try_results = real
        self.assertEqual(called, [])
        self.assertEqual(res[0]['status'], 'skipped')
        self.assertIn('백업 실패', res[0]['reason'])

    def test_damaged_raw_is_not_reanalysed(self):
        from live.feign_recheck import recheck_selected, check_raw
        raw = Path(self.r1['storageDir']) / 'raw.log.gz'
        raw.write_bytes(raw.read_bytes()[:40])  # truncated gzip
        ok, why = check_raw(self.r1['storageDir'])
        self.assertFalse(ok)
        called = []
        res = recheck_selected([self.r1], self.root, rebuild=lambda *a, **k: called.append(1))
        self.assertEqual(called, [])
        self.assertEqual(res[0]['status'], 'skipped')


class RawCheckReadsEverything(unittest.TestCase):
    """Damage AFTER the encounter start must be caught (plain and gzip)."""
    HEAD = b'10/10/2026 20:00:00.000  ENCOUNTER_START,3445,"B",16,20,3004\n'

    def _check(self, data: bytes, gz: bool):
        from live.feign_recheck import check_raw
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            if gz:
                with gzip.open(d / 'raw.log.gz', 'wb') as fh:
                    fh.write(data)
            else:
                (d / 'raw.log').write_bytes(data)
            return check_raw(d)

    def test_clean_logs_pass(self):
        for gz in (False, True):
            ok, why = self._check(self.HEAD + '한글 줄\n'.encode() * 50, gz)
            self.assertTrue(ok, why)
            self.assertIn('51줄 전체 읽기', why)

    def test_trailing_damage_fails(self):
        for gz in (False, True):
            for bad in (b'\xff\xfe junk\n', b'\x00\x00\x00\n'):
                ok, why = self._check(self.HEAD + b'ok\n' * 100 + bad, gz)
                self.assertFalse(ok, (gz, bad))
                self.assertIn('손상', why)

    def test_truncated_gzip_fails(self):
        from live.feign_recheck import check_raw
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            with gzip.open(d / 'raw.log.gz', 'wb') as fh:
                fh.write(self.HEAD * 3000)
            b = (d / 'raw.log.gz').read_bytes()
            (d / 'raw.log.gz').write_bytes(b[:-20])
            self.assertFalse(check_raw(d)[0])

    def test_recheck_runs_the_full_check_right_before_rebuild(self):
        src = (ROOT / 'live' / 'feign_recheck.py').read_text(encoding='utf-8')
        body = src[src.index('def recheck_selected'):]
        self.assertLess(body.index('check_raw(folder)'), body.index('rebuild(row, storage_root'))


class SeriesDoesNotRewriteOtherTries(unittest.TestCase):
    def test_series_recompute_writes_no_sibling_review(self):
        from live.raid_facts import extract_raid_facts
        from live.raid_wipe_review import process_encounter_series
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for n in (1, 2):
                d = root / '2026-10-10' / '3445_Test_Boss' / f'try_00{n}'
                d.mkdir(parents=True)
                lines = _log([_died(30, W, 'Warrior-Realm')])
                (d / 'raw.log').write_text('\n'.join(lines) + '\n', encoding='utf-8')
                enc = {"encounterID": 3445, "difficultyID": 16, "name": "Test Boss", "result": "Wipe",
                       "startTimestamp": f"10/10/2026 2{n}:00:00.000", "endTimestamp": _ts(90), "durationSec": 90.0}
                facts = extract_raid_facts(d / 'raw.log', enc, persist_display_names=False)
                (d / 'raid_facts.json').write_text(json.dumps(facts), encoding='utf-8')
                (d / 'meta.json').write_text(json.dumps(enc), encoding='utf-8')
            series = process_encounter_series(root, 3445, 16, write=True)
            self.assertIsNotNone(series)
            self.assertEqual(list(root.rglob('raid_review.json')), [], 'series must not write Try reviews')
            self.assertTrue((root / '_series' / '3445_d16' / 'raid_series_review.json').exists())


class SettingsWiring(unittest.TestCase):
    def test_gui_hooks(self):
        gui = (ROOT / 'nini_wcl_gui.py').read_text(encoding='utf-8')
        self.assertIn('from live.public_feign_ui import open_settings', gui)
        self.assertIn('_feign_recheck_running', gui)
        ui = (ROOT / 'live' / 'public_feign_ui.py').read_text(encoding='utf-8')
        self.assertIn('후보 검색', ui)
        self.assertIn('선택한 Try 재검사', ui)


if __name__ == '__main__':
    unittest.main()
