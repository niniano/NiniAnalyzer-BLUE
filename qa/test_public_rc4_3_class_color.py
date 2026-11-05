"""RC4.3: first attacker / puller name in WoW class colour."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ClassColours(unittest.TestCase):
    def test_every_class_has_official_colour_and_specs(self):
        from live.class_colors import CLASS_COLORS, SPEC_CLASS
        self.assertEqual(len(CLASS_COLORS), 13)
        self.assertEqual(set(SPEC_CLASS.values()), set(CLASS_COLORS))
        self.assertEqual(CLASS_COLORS['MAGE'], '#3FC7EB')
        self.assertEqual(CLASS_COLORS['HUNTER'], '#AAD372')
        self.assertEqual(CLASS_COLORS['PALADIN'], '#F48CBA')

    def test_spec_lookup(self):
        from live.class_colors import color_for_spec
        self.assertEqual(color_for_spec(64), '#3FC7EB')       # Frost mage
        self.assertEqual(color_for_spec('255'), '#AAD372')    # Survival hunter
        self.assertIsNone(color_for_spec(None))
        self.assertIsNone(color_for_spec(99999))


def _row(tmp: Path, roster, pull):
    (tmp / 'raid_facts.json').write_text(json.dumps({'roster': roster}), encoding='utf-8')
    return {'storageDir': str(tmp), 'factsStatus': 'ready', 'pullStart': pull}


class PullLineParts(unittest.TestCase):
    def _parts(self, row):
        from nini_wcl_gui import App

        class Fake:
            _pull_badge_text = App._pull_badge_text
            _pull_spell_display = staticmethod(App._pull_spell_display)
            _pull_line_text = App._pull_line_text
        return App._pull_line_parts(Fake(), row)

    def test_player_named_in_class_colour(self):
        with tempfile.TemporaryDirectory() as t:
            row = _row(Path(t), [{'guid': 'Player-1-A', 'name': 'Mage-R', 'specID': 64}],
                       {'pullerConfirmed': True, 'puller': {'playerGUID': 'Player-1-A', 'playerName': 'Mage-R',
                                                            'sec': -1.2, 'spellName': 'Frostbolt'}})
            prefix, name, suffix, colour = self._parts(row)
        self.assertEqual((prefix, name, colour), ('풀러', 'Mage', '#3FC7EB'))
        self.assertIn('1.2초 전', suffix)

    def test_pet_uses_owner_class(self):
        with tempfile.TemporaryDirectory() as t:
            row = _row(Path(t), [{'guid': 'Player-1-H', 'name': 'Hunt-R', 'specID': 253}],
                       {'pullerConfirmed': False, 'firstObservedAttack': {
                           'playerGUID': 'Player-1-H', 'playerName': 'Hunt-R', 'actorType': 'pet',
                           'sec': 0.1, 'spellName': 'Claw'}})
            prefix, name, suffix, colour = self._parts(row)
        self.assertEqual((prefix, name, colour), ('첫 공격', 'Hunt [펫]', '#AAD372'))

    def test_unknown_spec_and_ambiguous_stay_plain(self):
        with tempfile.TemporaryDirectory() as t:
            row = _row(Path(t), [], {'pullerConfirmed': False, 'firstObservedAttack': {
                'playerGUID': 'Player-1-X', 'playerName': 'X-R', 'sec': 0.0, 'spellName': 'Hit'}})
            self.assertIsNone(self._parts(row)[3])
            row['pullStart'] = {'pullerConfirmed': False, 'ambiguity': 'near-simultaneous-prestart-attacks',
                                'firstObservedAttack': {'playerName': 'A', 'sec': -0.2}}
            prefix, name, suffix, colour = self._parts(row)
        self.assertEqual((name, colour), ('', None))
        self.assertIn('보류', prefix)

    def test_waiting_text_stays_plain(self):
        prefix, name, _, colour = self._parts({'factsStatus': 'analyzing'})
        self.assertIn('분석 후', prefix)
        self.assertEqual((name, colour), ('', None))

    def test_gui_renders_parts(self):
        src = (ROOT / 'nini_wcl_gui.py').read_text(encoding='utf-8')
        self.assertIn('self._render_pull_line(row)', src)
        self.assertIn('live_pull_name_lbl.configure(foreground=colour', src)


if __name__ == '__main__':
    unittest.main()
