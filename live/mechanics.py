"""Encounter-scoped canonical IDs. Preserve the observed spell alongside the alias."""
KNOWN_SPEC_IDS = {
    62, 63, 64, 65, 66, 70, 71, 72, 73, 102, 103, 104, 105,
    250, 251, 252, 253, 254, 255, 256, 257, 258, 259, 260, 261,
    262, 263, 264, 265, 266, 267, 268, 269, 270, 577, 581,
    1467, 1468, 1473, 1480,
}
HEALER_SPECS = {65, 105, 256, 257, 264, 270, 1468}
TANK_SPECS = {66, 73, 104, 250, 268, 581}
MECHANICS = {
    3445: {
        1284941: (1284941, '배양된 파열'),
        1284947: (1284941, '배양된 파열'),
        1284948: (1284941, '배양된 파열'),
        1288282: (1288282, '불안정한 독기'),
        1284452: (1284452, '맹독성 작렬'),
        1284813: (1284813, '나선의 독소'),
        1284590: (1284813, '나선의 독소'),
    },
}


def canonical(encounter_id, spell_id, name=None):
    return MECHANICS.get(int(encounter_id or 0), {}).get(spell_id, (spell_id, name or str(spell_id)))


def role(spec_id):
    return 'healer' if spec_id in HEALER_SPECS else 'tank' if spec_id in TANK_SPECS else 'dps' if spec_id in KNOWN_SPEC_IDS else 'unknown'


def is_fake_pull(encounter):
    # Keep the raw capture; only exclude sub-second encounter-marker artifacts.
    duration = encounter.get('durationSec')
    return duration is not None and 0 <= float(duration) < 1.0
