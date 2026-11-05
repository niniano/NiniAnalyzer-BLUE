"""RC4/RC4.1 regressions: Feign Death is not a death, puller badge, first screen, ScrollPage, fonts."""
from __future__ import annotations
import os
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

H = 'Player-1-0000HUNT'
W = 'Player-1-0000WARR'
LOG = [
    '10/10/2026 20:00:00.000  ENCOUNTER_START,3445,"Test Boss",16,20,3004',
    '10/10/2026 20:00:01.000  SWING_DAMAGE,Player-1-0000WARR,"Warrior-Realm",0x514,0x0,Creature-0-1-1-1-1-0000000001,"Test Boss",0xa48,0x0,Player-1-0000WARR,0000000000000000,100,100,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,100,-1,1,0,0,0,nil,nil,nil',
    f'10/10/2026 20:00:10.000  SPELL_CAST_SUCCESS,{H},"Hunter-Realm",0x514,0x0,0000000000000000,nil,0x80000000,0x80000000,5384,"Feign Death",0x1',
    f'10/10/2026 20:00:10.000  SPELL_AURA_APPLIED,{H},"Hunter-Realm",0x514,0x0,{H},"Hunter-Realm",0x514,0x0,5384,"Feign Death",0x1,BUFF',
    f'10/10/2026 20:00:10.200  UNIT_DIED,0000000000000000,nil,0x80000000,0x80000000,{H},"Hunter-Realm",0x514,0x0,0,1',
    f'10/10/2026 20:00:13.000  SPELL_CAST_SUCCESS,{H},"Hunter-Realm",0x514,0x0,0000000000000000,nil,0x80000000,0x80000000,19434,"Aimed Shot",0x1',
    f'10/10/2026 20:00:30.000  UNIT_DIED,0000000000000000,nil,0x80000000,0x80000000,{W},"Warrior-Realm",0x514,0x0,0,0',
    f'10/10/2026 20:01:00.000  UNIT_DIED,0000000000000000,nil,0x80000000,0x80000000,{H},"Hunter-Realm",0x514,0x0,0,0',
    '10/10/2026 20:01:05.000  ENCOUNTER_END,3445,"Test Boss",16,20,0,65000',
]
ENC = {"encounterID": 3445, "name": "Test Boss", "difficultyID": 16,
       "startTimestamp": "10/10/2026 20:00:00.000", "endTimestamp": "10/10/2026 20:01:05.000"}


class FeignDeath(unittest.TestCase):
    def test_raid_facts_ignores_feign_but_keeps_real_hunter_death(self):
        from live.raid_facts import extract_raid_facts
        with tempfile.TemporaryDirectory() as t:
            raw = Path(t) / 'raw.log'
            raw.write_text('\n'.join(LOG) + '\n', encoding='utf-8')
            facts = extract_raid_facts(raw, dict(ENC))
        deaths = [(d['guid'], round(float(d['sec']), 1)) for d in facts['deaths']]
        self.assertEqual(deaths, [(W, 30.0), (H, 60.0)])
        self.assertEqual(len(facts['feignDeathsIgnored']), 1)

    def test_encounter_tracker_death_count(self):
        from live.encounter_tracker import EncounterTracker
        tr = EncounterTracker()
        end = None
        for line in LOG:
            for ev in tr.feed_line(line):
                if ev.get('type') == 'encounter_end':
                    end = ev['encounter']
        self.assertIsNotNone(end)
        self.assertEqual(end.get('deathCount'), 2)
        self.assertNotIn('_feignAt', end)

    def test_mechanics_engine_rows(self):
        from live.boss_mechanics.engine import _drop_feign_deaths
        rows = [
            {'event': 'SPELL_CAST_SUCCESS', 'spellID': 5384, 'sourceGUID': H, 'sec': 10.0},
            {'event': 'UNIT_DIED', 'destGUID': H, 'sec': 10.2},
            {'event': 'SPELL_CAST_SUCCESS', 'spellID': 19434, 'sourceGUID': H, 'sec': 13.0},
            {'event': 'UNIT_DIED', 'destGUID': W, 'sec': 30.0},
            {'event': 'UNIT_DIED', 'destGUID': H, 'sec': 60.0},
        ]
        kept = [(r['destGUID'], r['sec']) for r in _drop_feign_deaths(rows) if r['event'] == 'UNIT_DIED']
        self.assertEqual(kept, [(W, 30.0), (H, 60.0)])


def _ts(sec):
    m, s_ = divmod(sec, 60)
    return f'10/10/2026 20:{int(m):02d}:{s_:06.3f}'


def _fd(sec):
    return [f'{_ts(sec)}  SPELL_CAST_SUCCESS,{H},"Hunter-Realm",0x514,0x0,0000000000000000,nil,0x80000000,0x80000000,5384,"Feign Death",0x1',
            f'{_ts(sec)}  SPELL_AURA_APPLIED,{H},"Hunter-Realm",0x514,0x0,{H},"Hunter-Realm",0x514,0x0,5384,"Feign Death",0x1,BUFF']


def _died(sec, guid=H, name='Hunter-Realm'):
    return f'{_ts(sec)}  UNIT_DIED,0000000000000000,nil,0x80000000,0x80000000,{guid},"{name}",0x514,0x0,0,0'


def _cast(sec, spell=19434, sname='Aimed Shot'):
    return f'{_ts(sec)}  SPELL_CAST_SUCCESS,{H},"Hunter-Realm",0x514,0x0,0000000000000000,nil,0x80000000,0x80000000,{spell},"{sname}",0x1'


def _rez(sec):
    return f'{_ts(sec)}  SPELL_RESURRECT,Player-1-0000DRUI,"Druid-Realm",0x514,0x0,{H},"Hunter-Realm",0x514,0x0,20484,"Rebirth",0x8'


def _run_all(body):
    lines = [LOG[0], LOG[1]] + body + ['10/10/2026 20:01:30.000  ENCOUNTER_END,3445,"Test Boss",16,20,0,90000']
    from live.raid_facts import extract_raid_facts
    from live.encounter_tracker import EncounterTracker, parse_log_line
    from live.boss_mechanics.engine import _drop_feign_deaths
    enc = dict(ENC, endTimestamp='10/10/2026 20:01:30.000')
    with tempfile.TemporaryDirectory() as t:
        raw = Path(t) / 'raw.log'
        raw.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        facts = extract_raid_facts(raw, enc)
    tr = EncounterTracker(); end = None
    for line in lines:
        for ev in tr.feed_line(line):
            if ev.get('type') == 'encounter_end':
                end = ev['encounter']
    rows = []
    for line in body:
        p = parse_log_line(line); f = p['fields']
        sid = None
        if p['event'].startswith('SPELL_') and len(f) > 9:
            try: sid = int(f[9])
            except ValueError: sid = None
        rows.append({'event': p['event'], 'sourceGUID': f[1], 'destGUID': f[5], 'spellID': sid,
                     'sec': float(p['timestamp'].split(':')[-1]) + 60 * int(p['timestamp'].split(':')[-2])})
    engine = sum(1 for r in _drop_feign_deaths(rows) if r['event'] == 'UNIT_DIED' and r['destGUID'] == H)
    hunter = sum(1 for d in facts['deaths'] if d['guid'] == H)
    return hunter, int(end.get('deathCount') or 0), engine, facts


class FeignDeathRC41(unittest.TestCase):
    """User criterion: never drop a death just for being <3s after FD; keep it unless proven fake."""

    def test_real_death_1s_after_fd_with_no_further_action_is_counted(self):
        hunter, tracker, engine, facts = _run_all(_fd(10) + [_died(11)])
        self.assertEqual((hunter, tracker, engine), (1, 1, 1))
        self.assertEqual(facts['feignDeathsIgnored'], [])
        self.assertEqual(len(facts['feignDeathsKeptUncertain']), 1)

    def test_fd_then_acts_again_is_not_a_death(self):
        hunter, tracker, engine, facts = _run_all(_fd(10) + [_died(10.2), _cast(13)])
        self.assertEqual((hunter, tracker, engine), (0, 0, 0))
        self.assertEqual(len(facts['feignDeathsIgnored']), 1)
        self.assertEqual(facts.get('playerReturns'), [])

    def test_fd_death_then_battle_rez_then_cast_is_a_real_death(self):
        hunter, tracker, engine, _ = _run_all(_fd(10) + [_died(10.5), _rez(20), _cast(25)])
        self.assertEqual((hunter, tracker, engine), (1, 1, 1))

    def test_action_after_30s_does_not_retroactively_erase_death(self):
        hunter, tracker, engine, _ = _run_all(_fd(10) + [_died(10.5), _cast(45)])
        self.assertEqual((hunter, tracker, engine), (1, 1, 1))

    def test_feign_then_later_real_death_both_handled(self):
        hunter, tracker, engine, _ = _run_all(_fd(10) + [_died(10.2), _cast(12), _died(40)])
        self.assertEqual((hunter, tracker, engine), (1, 1, 1))

    def test_second_fd_real_death_after_earlier_genuine_feign(self):
        body = _fd(5) + [_died(5.1), _cast(7)] + _fd(20) + [_died(21)]
        hunter, tracker, engine, _ = _run_all(body)
        self.assertEqual((hunter, tracker, engine), (1, 1, 1))


class PullerLine(unittest.TestCase):
    def _line(self, row):
        from nini_wcl_gui import App
        class Fake:
            _pull_badge_text = App._pull_badge_text
            _pull_spell_display = staticmethod(App._pull_spell_display)
        return App._pull_line_text(Fake(), row)

    def test_shown_without_review_model(self):
        row = {'factsStatus': 'ready', 'raidReviewStatus': 'pending',
               'pullStart': {'pullerConfirmed': True, 'puller': {'playerName': 'Tank-R', 'sec': -0.8, 'spellName': 'Taunt'}}}
        self.assertTrue(self._line(row).startswith('풀러 Tank'))

    def test_waiting_and_legacy_states(self):
        self.assertIn('분석 후', self._line({'factsStatus': 'analyzing'}))
        self.assertIn('재분석', self._line({'factsStatus': 'ready'}))
        self.assertIn('확인되지', self._line({'factsStatus': 'ready', 'pullStart': {}}))
        self.assertEqual(self._line(None), '')

    def test_gui_sets_line_outside_model_branch(self):
        src = (ROOT / 'nini_wcl_gui.py').read_text(encoding='utf-8')
        self.assertIn('self.live_pull_var.set(self._pull_line_text(row))', src)
        self.assertNotIn("명\" + self._pull_badge_text(row)", src)


class ReanalysisBackup(unittest.TestCase):
    def test_backup_zip_keeps_results_and_is_not_a_try(self):
        import zipfile
        from live.reanalysis import backup_try_results
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / 'meta.json').write_text('{"a":1}', encoding='utf-8')
            (d / 'raid_facts.json').write_text('{}', encoding='utf-8')
            (d / 'raw.log.gz').write_bytes(b'x')
            z = backup_try_results(d)
            self.assertTrue(z.name.startswith('reanalysis_backup_') and z.suffix == '.zip')
            self.assertEqual(sorted(zipfile.ZipFile(z).namelist()), ['meta.json', 'raid_facts.json'])
            self.assertEqual(len(list(d.rglob('meta.json'))), 1)
            self.assertTrue((d / 'meta.json').exists() and (d / 'raw.log.gz').exists())


class PullerBadge(unittest.TestCase):
    def _badge(self, pull):
        from nini_wcl_gui import App
        return App._pull_badge_text(App, {'pullStart': pull})

    def test_confirmed_puller(self):
        txt = self._badge({'pullerConfirmed': True, 'puller': {'playerName': 'Tank-Realm', 'sec': -1.24, 'spellName': 'Taunt', 'isTaunt': True}})
        self.assertIn('풀러 Tank', txt); self.assertIn('1.2초 전', txt); self.assertIn('(도발)', txt)

    def test_first_attack_and_ambiguity(self):
        self.assertIn('첫 공격 Dps', self._badge({'pullerConfirmed': False, 'firstObservedAttack': {'playerName': 'Dps-R', 'sec': 0.03, 'spellName': 'Shot'}}))
        self.assertIn('보류', self._badge({'pullerConfirmed': False, 'ambiguity': 'near-simultaneous-prestart-attacks', 'firstObservedAttack': {'playerName': 'A', 'sec': -0.2}}))
        self.assertEqual(self._badge({}), '')


@unittest.skipUnless(os.name == 'nt' or os.environ.get('DISPLAY'), 'no screen')
class ScrollPageGrowth(unittest.TestCase):
    def test_late_content_gets_scrollbar(self):
        from live.ui_theme import ScrollPage
        root = tk.Tk(); root.geometry('800x500')
        try:
            page = ScrollPage(root); page._outer.pack(fill='both', expand=True)
            tk.Label(page, text='loading').pack()
            root.after(500, root.quit); root.mainloop()
            for i in range(40):
                tk.Label(page, text=str(i), height=2).pack(fill='x')
            root.after(500, root.quit); root.mainloop()
            self.assertLess(page._canvas.yview()[1], 0.9)
            self.assertTrue(page._bar.winfo_manager())
        finally:
            root.destroy()


class Fonts(unittest.TestCase):
    def test_fonts_and_licences_bundled(self):
        d = ROOT / 'assets' / 'fonts'
        for n in ('PyeojinGothic-Medium.ttf', 'Paperlogy-6SemiBold.ttf',
                  'LICENSE_PyeojinGothic_OFL.txt', 'LICENSE_Paperlogy_OFL.txt'):
            self.assertTrue((d / n).is_file(), n)
        src = (ROOT / 'build_public_windows.py').read_text(encoding='utf-8')
        self.assertIn("('assets/fonts','assets/fonts')", src)

    def test_loader_is_process_private(self):
        src = (ROOT / 'live' / 'ui_theme.py').read_text(encoding='utf-8')
        self.assertIn('AddFontResourceExW', src)
        self.assertIn('0x10, None', src)  # FR_PRIVATE only; never installs system-wide


@unittest.skipUnless(os.name == 'nt' or os.environ.get('DISPLAY'), 'no screen')
class FirstScreen(unittest.TestCase):
    def test_public_opens_latest_session(self):
        from nini_wcl_gui import App
        old = os.environ.get('NINI_PUBLIC_HOME')
        with tempfile.TemporaryDirectory() as t:
            os.environ['NINI_PUBLIC_HOME'] = t
            app = App(edition='raid')
            try:
                app._public_migration_running = False
                app._public_auto_session_done = False
                app.nav_state['page'] = 'session'; app.nav_state['sessionDate'] = ''
                rows = [{'startTimestamp': '9/27/2026 13:24:56.891'}, {'startTimestamp': '10/2/2026 21:00:00.000'}]
                app._public_auto_open_latest_session(rows)
                self.assertEqual(app.nav_state.get('sessionDate'), '2026-10-02')
                app.nav_state['sessionDate'] = '2026-09-27'
                app._public_auto_open_latest_session(rows)  # runs once only
                self.assertEqual(app.nav_state.get('sessionDate'), '2026-09-27')
            finally:
                try:
                    for h in app.tk.call('after', 'info'):
                        app.after_cancel(h)
                except tk.TclError:
                    pass
                app.destroy()
                if old is None:
                    os.environ.pop('NINI_PUBLIC_HOME', None)
                else:
                    os.environ['NINI_PUBLIC_HOME'] = old


if __name__ == '__main__':
    unittest.main()
