from __future__ import annotations

"""Presentation-only normalization for the B37.5 timeline glyph renderer.

The mechanic engine remains authoritative.  This module only converts the
existing ``build_try_priority_model`` timeline into visual primitives:

- real WoW events -> spell glyphs
- phases -> vertical phase marks
- bounded mechanic windows -> translucent ranges
- raid cooldowns -> duration bars
- analyzer-only judgments -> standalone markers only when no spell glyph owns it

No success/failure is promoted here.
"""

from dataclasses import dataclass
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

try:
    from live import timeline_skill_tips_bridge as _skill_tips_bridge
except Exception:  # catalog is optional for backward-compatible patch application
    _skill_tips_bridge = None


PHASE_KINDS = {"p2_entry", "phase", "phase_entry", "phase2_entry"}
# Internal analysis anchors are not physical WoW events and should not render as spell glyphs.
HIDDEN_KINDS = {"phase2_activation"}

# UI grouping aliases.  Different engine labels for the same physical mechanic
# must land on one glyphKey so hover-highlighting and dedupe are stable.
GLYPH_KIND_ALIASES = {
    "p1_orb_failure": "breath",
    "p1_tank_breath_non_tank_hit": "breath",
    "orb_wave": "orb_wave",
    "p1_glaive_death": "axegrinder",
    "breath": "breath",
    "guillotine": "guillotine",
    "p2_mind_control": "dreadmarch",
    "p2_soulcoilers": "spiritcackle",
    "p2_gloombomb": "gloombomb",
    "gloombomb_failure": "gloombomb",
    "gloombomb": "gloombomb",
    "interrupt": "gloombomb",
    "p2_wail_failure": "wail",
    "wail_interrupt_failure": "wail",
    "wail_fail": "wail",
    "wail": "wail",
    "p2_soul_sever": "soul_sever",
    "soul_sever_non_tank_hit": "soul_sever",
    "manifest_collision": "manifest",
    "manifest": "manifest",
    "p2_shield_start": "shield",
    "p2_shield_break": "shield",
    "p2_nightfall_interrupt": "nightfall",
    "p2_nightfall_success": "nightfall",
    "shield_break": "shield",
    "shield": "shield",
    "nightfall_fail": "nightfall",
    "nightfall": "nightfall",
    "bloodlust_timing": "bloodlust",
    "collapse": "collapse",
    "collapse_start": "collapse",
    "viscous_cyst_chain": "viscous_cyst",
    "crosswind_pair_miss": "crosswinds",
    "serpents_fury_enrage": "serpents_fury",
    "mutilate_under_soak": "mutilate",
    # Nymrissa
    "berserker_leak": "berserker_leak",
    "frost_orb_shatter": "frost_orb_shatter",
    "whirlpool_hit": "whirlpool_hit",
    # Lost Explorers
    "final_ascension_cycle": "final_ascension",
    "fish_feed": "fish_feed",
    "relic_rupture": "relic_rupture",
    "icebound_flames": "icebound_flames",
    "elemental_explosion": "elemental_explosion",
    "spreading_flames_candidate": "spreading_flames",
    # Nek'Zali
    "soulcoilers_curse": "soulcoilers_curse",
    # Twin Fangs
    "eternal_venom": "eternal_venom",
    "eternal_venom_high": "eternal_venom",
    "causticDeluge": "caustic_deluge",
    "caustic_globule_rupture": "caustic_globule",
    "venomousEmergence": "venomous_emergence",
    "corrosive_spit_cleave": "corrosive_spit",
    "stirTheDepths": "stir_the_depths",
    "stir_wave_hit": "stir_the_depths",
    "vileFlood": "vile_flood",
    "rouseTheBrood": "rouse_brood",
    "visceral_burst": "visceral_burst",
    "ravenousFeast": "ravenous_feast",
    "ravenous_feast": "ravenous_feast",
    "ravenous_feast_mass_hit": "ravenous_feast",
    "tainted_burst": "tainted_burst",
    "stone_breaker_reverb": "stone_breaker",
    "sanguineStorm": "sanguine_storm",
    # Buried Sentinel
    "missed_globules": "toxic_globule",
    "protovenom_eruption_observed": "protovenom_eruption",
    "cultivated_burst_observed": "cultivated_burst",
    "unstable_miasma_under_share": "unstable_miasma",
    # Ula'tek
    "heart_start": "venomous_heart",
    "heart_end": "venomous_heart",
    "unchecked_rage": "unchecked_rage",
    "anguished_cry": "anguished_cry",
    "calcified_corpse": "calcified_corpse",
    # Preview4 presentation-only cast catalog
    "caustic_waves": "caustic_waves",
    "call_of_the_serpent": "call_of_the_serpent",
    "spectral_coils": "spectral_coils",
    "mephitic_thrash": "mephitic_thrash",
    "mothers_wrath": "mothers_wrath",
    "rage_of_the_shackled": "rage_of_the_shackled",
    "shadow_molt": "shadow_molt",
    "writhing_gestation": "writhing_gestation",
    "grasping_fangs": "grasping_fangs",
    "defect_weakened": "defect_weakened",
    # Vashnik
    "malignant_burst": "malignant_burst",
    "catalytic_bile_miss": "catalytic_bile",
    "plague_wave_hit": "plague_wave",
    "malignance": "malignance",
    "exploding_infection_overlap": "exploding_infection",
    "caustic_surge_overlap": "caustic_surge",
}

# Preferred spell identity when an analysis/failure row itself contains no ID.
# These IDs are already verified in the boss rules/engine; this table does not
# introduce new mechanic meaning.
KIND_SPELL_IDS = {
    "breath": 1299684,
    "guillotine": 1283489,
    "dreadful_presence": 1288624,
    "dreadmarch": 1285643,
    "spiritcackle": 1286441,
    "gloombomb": 1286895,
    "wail": 1286399,
    "soul_sever": 1286620,
    "nightfall": 1286918,
    "shield": 1286912,
    "manifest": 1310744,
    "bloodlust": 80353,
    "orb_wave": 1299781,
    "axegrinder": 1285017,
    "mutilate": 1277027,
    "viscous_cyst": 1287205,
    "crosswinds": 1285419,
    "serpents_fury": 1297367,
    # Nymrissa
    "berserker_leak": 1258186,
    "frost_orb_shatter": 1313456,
    "whirlpool_hit": 1258673,
    # Lost Explorers
    "final_ascension": 1292779,
    "fish_feed": 1292490,
    "relic_rupture": 1310027,
    "icebound_flames": 1286922,
    "elemental_explosion": 1295952,
    "spreading_flames": 1314061,
    # Nek'Zali
    "soulcoilers_curse": 1300238,
    # Twin Fangs
    "eternal_venom": 1290336,
    "caustic_deluge": 1289192,
    "caustic_globule": 1289993,
    "venomous_emergence": 1291404,
    "corrosive_spit": 1291478,
    "stir_the_depths": 1290956,
    "vile_flood": 1293749,
    "rouse_brood": 1308356,
    "visceral_burst": 1308385,
    "ravenous_feast": 1290516,
    "tainted_burst": 1310105,
    "stone_breaker": 1288538,
    "sanguine_storm": 1306872,
    # Buried Sentinel
    "toxic_globule": 1284452,
    "protovenom_eruption": 1296962,
    "cultivated_burst": 1284941,
    "unstable_miasma": 1288232,
    # Ula'tek
    "venomous_heart": 1299526,
    "unchecked_rage": 1301007,
    "anguished_cry": 1305650,
    "calcified_corpse": 1306119,
    "caustic_waves": 1311807,
    "call_of_the_serpent": 1304012,
    "spectral_coils": 1308927,
    "mephitic_thrash": 1296301,
    "mothers_wrath": 1298367,
    "rage_of_the_shackled": 1286860,
    "shadow_molt": 1301213,
    "writhing_gestation": 1302950,
    "grasping_fangs": 1301117,
    "defect_weakened": 1303410,
    # Vashnik
    "malignant_burst": 1286872,
    "catalytic_bile": 1282601,
    "plague_wave": 1295798,
    "malignance": 1306820,
    "exploding_infection": 1295173,
    "caustic_surge": 1285979,
}

# Human-readable fallback badge when an offline icon is unavailable.  Keep this
# short because it occupies the same square as the future spell icon.
FALLBACK_BADGE = {
    "breath": "브",
    "guillotine": "단",
    "dreadful_presence": "P2",
    "dreadmarch": "정",
    "spiritcackle": "쫄",
    "gloombomb": "폭",
    "wail": "통",
    "soul_sever": "절",
    "shield": "보",
    "nightfall": "일",
    "manifest": "현",
    "bloodlust": "BL",
    "orb_wave": "구",
    "axegrinder": "도",
    "mutilate": "소",
    "viscous_cyst": "낭",
    "crosswinds": "돌",
    "serpents_fury": "격",
    "berserker_leak": "멀", "frost_orb_shatter": "구", "whirlpool_hit": "소",
    "final_ascension": "승", "fish_feed": "물", "relic_rupture": "유", "icebound_flames": "얼",
    "elemental_explosion": "폭", "spreading_flames": "불", "soulcoilers_curse": "저",
    "eternal_venom": "독", "caustic_deluge": "폭", "caustic_globule": "구",
    "venomous_emergence": "출", "corrosive_spit": "침", "stir_the_depths": "휘",
    "vile_flood": "홍", "rouse_brood": "깨", "visceral_burst": "파",
    "ravenous_feast": "탐", "tainted_burst": "피", "stone_breaker": "돌", "sanguine_storm": "핏",
    "toxic_globule": "물", "protovenom_eruption": "독", "cultivated_burst": "배", "unstable_miasma": "독",
    "venomous_heart": "심", "unchecked_rage": "분", "anguished_cry": "절", "calcified_corpse": "시",
    "caustic_waves": "파", "call_of_the_serpent": "뱀", "spectral_coils": "똬",
    "mephitic_thrash": "후", "mothers_wrath": "분", "rage_of_the_shackled": "격",
    "shadow_molt": "탈", "writhing_gestation": "잉", "grasping_fangs": "송", "defect_weakened": "약",
    "malignant_burst": "악", "catalytic_bile": "담", "plague_wave": "파", "malignance": "성",
    "exploding_infection": "감", "caustic_surge": "쇄",
}


GLYPH_TITLES = {
    "breath": "브레스", "guillotine": "단두대", "orb_wave": "맹독 구슬 웨이브",
    "dreadful_presence": "P2 진입", "dreadmarch": "정배", "spiritcackle": "영혼살무사 소환",
    "gloombomb": "암영폭탄", "wail": "공포의 통곡", "soul_sever": "영혼 절단",
    "shield": "영원한 일몰", "nightfall": "영원한 일몰", "manifest": "현신 접촉",
    "axegrinder": "도끼날",
    "berserker_leak": "멀록 유출 · 광전사", "frost_orb_shatter": "서리 구체 Shatter",
    "whirlpool_hit": "휘몰아치는 소용돌이", "mutilate": "Mutilate", "viscous_cyst": "점성 낭종",
    "crosswinds": "격노하는 돌풍", "serpents_fury": "뱀의 격노", "final_ascension": "최후의 승천",
    "fish_feed": "역겨운 물고기 전달",
    "relic_rupture": "유물 파열", "icebound_flames": "얼음결속 불꽃", "elemental_explosion": "원소 폭발",
    "spreading_flames": "번지는 불길", "soulcoilers_curse": "영혼살무사의 저주",
    "eternal_venom": "영원의 맹독", "caustic_deluge": "부식성 폭우", "caustic_globule": "부식성 구체",
    "venomous_emergence": "맹독성 출현", "corrosive_spit": "부식성 침", "stir_the_depths": "심연 휘젓기",
    "vile_flood": "끔찍한 홍수", "rouse_brood": "새끼 깨우기", "visceral_burst": "내장 파열",
    "ravenous_feast": "걸신들린 탐식", "tainted_burst": "오염된 피", "stone_breaker": "돌 파괴자",
    "sanguine_storm": "핏빛 폭풍", "toxic_globule": "유독한 물방울", "protovenom_eruption": "원시맹독 분출",
    "cultivated_burst": "배양된 파열", "unstable_miasma": "불안정한 독기", "venomous_heart": "맹독 심장", "unchecked_rage": "막을 수 없는 분노",
    "anguished_cry": "고통스러운 절규", "calcified_corpse": "석회화한 시체",
    "caustic_waves": "부식성 파도", "call_of_the_serpent": "뱀의 부름", "spectral_coils": "망령의 똬리",
    "mephitic_thrash": "독성 후려치기", "mothers_wrath": "어미의 분노", "rage_of_the_shackled": "속박된 자의 분노",
    "shadow_molt": "암흑 탈피", "writhing_gestation": "꿈틀대는 잉태", "grasping_fangs": "휘감는 송곳니",
    "defect_weakened": "결함: 약화", "malignant_burst": "악의 파열",
    "catalytic_bile": "촉매성 담즙", "plague_wave": "역병 파동", "malignance": "악성",
    "exploding_infection": "폭발 전염병", "caustic_surge": "부식성 쇄도", "bloodlust": "블러드",
}


# Preview4.1: human mechanic descriptions for the Skill Reference strip.
# Keep these player-facing and concise. Internal spell IDs / analyzer keys never
# belong in the hover text. Unknown future glyphs still fall back to a neutral
# observation sentence, but every currently supported boss mechanic is covered.
SKILL_DESCRIPTION_BY_GLYPH = {
    # Coiled Altar
    "breath": "전방 탱커 공격입니다. 신화에서는 맞은 공격으로 응집된 독/돌연변이를 정리하는 데도 사용됩니다.",
    "guillotine": "대상 지점에 도끼를 던지는 분담 피해입니다. 충분한 인원이 함께 맞지 않으면 처형으로 이어질 수 있습니다.",
    "orb_wave": "P1의 맹독 구슬 웨이브입니다. 구슬 처리 결과와 선행 흡수 수는 별도 판정으로 연결됩니다.",
    "axegrinder": "도끼가 전장에 떨어져 주변을 밀쳐내고 이후에도 위험 구역을 만드는 이동 방해 기믹입니다.",
    "dreadmarch": "플레이어를 정신 지배하는 P2 기믹입니다. 해제되면 공포의 현신이 등장합니다.",
    "spiritcackle": "P2에서 영혼살무사를 소환하는 시전입니다.",
    "gloombomb": "플레이어에게 암영폭탄을 부여합니다. 만료 시 주변 플레이어에게 폭발 피해를 줍니다.",
    "wail": "공포의 통곡 차단 기믹입니다. 성공/실패는 실제 차단·완료 로그와 연결해 판정합니다.",
    "soul_sever": "탱커 방향의 영혼 절단입니다. 전방 판정이며 공포의 현신 처리에도 사용됩니다.",
    "nightfall": "영원한 일몰 시전입니다. 보호막을 제거한 뒤 차단 가능한 핵심 P2 기믹입니다.",
    "shield": "영원한 일몰의 보호막 구간입니다. 보호막 파괴와 차단 정렬을 별도로 확인합니다.",
    "manifest": "공포의 현신 접촉/충돌 관측입니다. 접촉 여부를 실행 판정과 연결합니다.",
    "dreadful_presence": "P2 진입 기준 신호입니다. 실제 기술 실패가 아니라 페이즈 전환 관측입니다.",
    # Nymrissa
    "berserker_leak": "멀록이 거품에 닿아 광전사로 변하면 주기적인 공대 피해를 일으킵니다. 생성 수를 실행 실패 신호로 봅니다.",
    "frost_orb_shatter": "서리 구체를 제때 처리하지 못했을 때 발생하는 파열입니다.",
    "whirlpool_hit": "휘몰아치는 소용돌이/파도에 직접 맞은 플레이어를 관측합니다.",
    # Lost Explorers
    "final_ascension": "최후의 승천 사이클 시작을 표시합니다. 사이클 진행을 읽기 위한 관측 스킬입니다.",
    "fish_feed": "역겨운 물고기를 어느 탐험가에게 언제 전달했는지 표시합니다. 전달 타이밍 자체는 관측이며, 최후의 승천 직접 사망이 붕괴와 연결될 때만 실패로 승격합니다.",
    "relic_rupture": "유물을 오래 방치했을 때 발생하는 파열입니다. 방치 시간 기준과 연결해 판정합니다.",
    "icebound_flames": "얼음결속 불꽃의 차단 대상 시전입니다.",
    "elemental_explosion": "서리불꽃 중첩 처리 실패와 연결되는 원소 폭발입니다.",
    "spreading_flames": "번지는 불길의 확산/피격 관측입니다. 현재는 후보 신호로만 사용합니다.",
    # Nek'zali
    "soulcoilers_curse": "영혼살무사의 저주 차단 기믹입니다. 완료 시전과 차단 로그를 함께 봅니다.",
    # Twin Fangs
    "eternal_venom": "여러 기술로 쌓이는 영원의 맹독 중첩입니다. 걸신들린 탐식으로 일부 중첩을 제거할 수 있습니다.",
    "caustic_deluge": "부식성 투사체/구체를 피하는 이동 기믹입니다.",
    "caustic_globule": "부식성 구체의 미흡수/파열 여부를 보는 기믹입니다.",
    "venomous_emergence": "맹독성 출현으로 새끼를 불러내고 공대에 맹독 압박을 추가합니다.",
    "corrosive_spit": "부식성 침의 추가 피격/클리브 여부를 관측합니다.",
    "stir_the_depths": "주변 독을 휘저어 파도를 만드는 광역 기믹입니다. 파도 직접 피격자를 따로 집계합니다.",
    "vile_flood": "전방으로 독을 지속 방출하는 기술입니다. 맞은 플레이어에게 영원의 맹독이 추가될 수 있습니다.",
    "rouse_brood": "새끼를 깨워 추가 적을 등장시키는 소환 기믹입니다.",
    "visceral_burst": "내장 파열 차단 대상 시전입니다. 완료 여부를 차단 판정과 연결합니다.",
    "ravenous_feast": "여러 명이 함께 맞는 대형 분담 피해입니다. 맞은 플레이어의 영원의 맹독 중첩을 일부 제거합니다.",
    "tainted_burst": "오염된 피 처리와 연결되는 폭발 신호입니다.",
    "stone_breaker": "반복 내려찍기입니다. 아무도 맞지 않으면 공대 전체에 벌점 피해가 들어갈 수 있습니다.",
    "sanguine_storm": "핏빛 폭풍 지속 구간입니다. 전투 흐름과 다른 기믹 정렬을 읽기 위한 관측 스킬입니다.",
    # Entombed Sentinels
    "toxic_globule": "유독한 물방울/구슬 처리 기믹입니다. 남은 구슬 수와 후속 파열을 연결해 봅니다.",
    "protovenom_eruption": "변화하는 원시맹독 처리 과정의 분출 관측입니다.",
    "cultivated_burst": "배양된 파열 관측입니다. 인터미션 처리 결과를 읽는 보조 신호로 사용합니다.",
    # Ula'tek
    "venomous_heart": "속박된 자의 분노 중 노출되는 맹독 심장입니다. 노출 동안 집중 딜을 넣는 핵심 딜 구간입니다.",
    "unchecked_rage": "보스가 근접 공격 대상을 잃었을 때 발생하는 공대 벌점 기술입니다. 탱킹 공백 신호로 사용합니다.",
    "anguished_cry": "약화된 파멸비늘의 차단 가능한 광역 절규입니다. 시전 완료는 차단 누락으로 판정합니다.",
    "calcified_corpse": "뱀의 물어뜯기를 시간 안에 흡수하지 못해 석회화한 시체가 된 결과입니다. 선행 사망 뒤 적용된 경우에는 첫 실수에서 제외합니다.",
    "caustic_waves": "플랫폼을 가로지르는 독 파도입니다. 새끼 알과 맞닿으면 즉시 부화시킬 수 있습니다.",
    "call_of_the_serpent": "울라텍이 새끼를 불러 전장에 추가 적을 떨어뜨리는 소환 기믹입니다.",
    "spectral_coils": "지정 지점에 내려찍는 분담 피해입니다. 충분한 인원이 함께 맞아 피해를 나눕니다.",
    "mephitic_thrash": "꼬리 휘두르기로 플레이어를 밀쳐내고 지속 피해를 남기는 공격입니다.",
    "mothers_wrath": "현재 탱커를 밀쳐내고 분노를 남기는 탱커 기술입니다. 보스 근접 대상이 비면 효과가 공대 전체로 확산될 수 있습니다.",
    "rage_of_the_shackled": "약 20초 동안 울라텍의 맹독 심장이 노출되는 핵심 딜 몰이 구간을 엽니다.",
    "shadow_molt": "파멸비늘 수호자가 그림자 허물을 벗고 새 위치로 이동하는 전환 기술입니다.",
    "writhing_gestation": "주변 새끼를 부화 준비 상태로 바꾸는 기술입니다. 방치하면 잠시 뒤 추가 적이 등장합니다.",
    "grasping_fangs": "무작위 플레이어를 속박하는 공격입니다. 속박이 풀릴 때까지 이동이 제한됩니다.",
    "defect_weakened": "잉태 방해에 성공했을 때 파멸비늘이 약화되어 받는 피해가 증가하는 상태입니다.",
    # Vashnik
    "malignant_burst": "맹독 풀이 중앙에 도달했을 때 발생하는 악의 파열 신호입니다.",
    "catalytic_bile": "촉매성 담즙의 흡수/미흡수 결과를 보는 기믹입니다. 미흡수 시 공대 피해로 이어질 수 있습니다.",
    "plague_wave": "역병 파동의 플레이어 직접 피격을 관측합니다.",
    "malignance": "악성 토템/완료 피해 흐름을 관측하는 기믹입니다.",
    "exploding_infection": "폭발 전염병의 해제·폭발 정렬을 보는 기믹입니다. 동시에 터지면 공대 압박이 커집니다.",
    "caustic_surge": "부식성 쇄도와 맹독 풀 겹침/충돌을 관측합니다.",
}

SKILL_REFERENCE_BY_BOSS = {
    "ulatek": [
        {"glyphKey":"caustic_waves", "title":"부식성 파도 (Caustic Waves)"},
        {"glyphKey":"call_of_the_serpent", "title":"뱀의 부름 (Call of the Serpent)"},
        {"glyphKey":"spectral_coils", "title":"망령의 똬리 (Spectral Coils)"},
        {"glyphKey":"mephitic_thrash", "title":"독성 후려치기 (Mephitic Thrash)"},
        {"glyphKey":"mothers_wrath", "title":"어미의 분노 (Mother's Wrath)"},
        {"glyphKey":"rage_of_the_shackled", "title":"속박된 자의 분노 (Rage of the Shackled)"},
        {"glyphKey":"venomous_heart", "title":"맹독 심장 (Venomous Heart)"},
        {"glyphKey":"unchecked_rage", "title":"막을 수 없는 분노 (Unchecked Rage)"},
        {"glyphKey":"anguished_cry", "title":"고통스러운 절규 (Anguished Cry)"},
        {"glyphKey":"calcified_corpse", "title":"석회화한 시체 (Calcified Corpse)"},
        {"glyphKey":"shadow_molt", "title":"암흑 탈피 (Shadow Molt)"},
        {"glyphKey":"writhing_gestation", "title":"꿈틀대는 잉태 (Writhing Gestation)"},
        {"glyphKey":"grasping_fangs", "title":"휘감는 송곳니 (Grasping Fangs)"},
        {"glyphKey":"defect_weakened", "title":"결함: 약화 (Defect: Weakened)"},
    ],
}

_CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "timeline_glyph_catalog.json"

@lru_cache(maxsize=1)
def _icon_catalog() -> dict:
    try:
        obj = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _catalog_icon(spell_id: int | None, glyph_key: str) -> str | None:
    catalog = _icon_catalog()
    by_spell = catalog.get("bySpellID") or {}
    by_key = catalog.get("byGlyphKey") or {}
    if spell_id:
        row = by_spell.get(str(int(spell_id))) or {}
        if isinstance(row, str):
            return row
        if isinstance(row, dict) and row.get("icon"):
            return str(row.get("icon"))
    row = by_key.get(str(glyph_key)) or {}
    if isinstance(row, str):
        return row
    if isinstance(row, dict) and row.get("icon"):
        return str(row.get("icon"))
    return None


def _skill_tip_title(glyph_key: str, fallback: str | None = None) -> str | None:
    if _skill_tips_bridge is None:
        return fallback
    try:
        return _skill_tips_bridge.title_for(glyph_key, fallback)
    except Exception:
        return fallback


def _skill_tip_rows(spell_id: int | None, glyph_key: str) -> list[tuple[str, str]]:
    if _skill_tips_bridge is None:
        return []
    try:
        return list(_skill_tips_bridge.tooltip_rows(spell_id=spell_id, glyph_key=glyph_key) or [])
    except Exception:
        return []


def _skill_tip_icon_path(spell_id: int | None, glyph_key: str) -> str | None:
    if _skill_tips_bridge is None:
        return None
    try:
        tip = _skill_tips_bridge.tip_for(spell_id=spell_id, glyph_key=glyph_key)
        if not tip:
            return None
        # icon_fallback resolves related/cast IDs to the bundled catalog image.
        resolved_sid = tip.get("spellID") or spell_id
        path = _skill_tips_bridge.icon_fallback(resolved_sid, None)
        return str(path) if path else None
    except Exception:
        return None


def _skill_tip_description(spell_id: int | None, glyph_key: str, legacy: str) -> str:
    rows = _skill_tip_rows(spell_id, glyph_key)
    if rows:
        return "\n".join(f"{label} · {text}" for label, text in rows)
    return legacy


@dataclass(frozen=True)
class GlyphCollisionPolicy:
    max_lanes: int = 3
    min_gap_px: int = 24


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _kind(event: dict) -> str:
    raw = str(event.get("judgmentKind") or event.get("kind") or "mechanic")
    return GLYPH_KIND_ALIASES.get(raw, raw)


def _spell_ids(event: dict) -> list[int]:
    out: list[int] = []
    candidates: list[Any] = []
    ids = event.get("ids")
    if isinstance(ids, dict):
        candidates.extend(ids.get("spellIDs") or [])
    for key in ("spellIDs", "directSpellIDs"):
        candidates.extend(event.get(key) or [])
    for key in ("spellID", "abilityGameID"):
        if event.get(key) is not None:
            candidates.append(event.get(key))
    for value in candidates:
        iv = _i(value)
        if iv is not None and iv > 0 and iv not in out:
            out.append(iv)
    return out


def _status_rank(event: dict) -> int:
    status = str(event.get("status") or "unknown")
    evidence = str(event.get("meaningEvidenceLevel") or event.get("evidenceLevel") or "")
    role = str(event.get("failureRole") or "")
    if role or (status == "failed" and evidence == "verified"):
        return 5
    if status == "warning" and evidence == "verified":
        return 4
    if status == "failed":
        return 3
    if status == "success":
        return 2
    return 1


def is_judgment(event: dict) -> bool:
    status = str(event.get("status") or "unknown")
    evidence = str(event.get("meaningEvidenceLevel") or event.get("evidenceLevel") or "")
    return bool(event.get("failureRole")) or (status in {"failed", "warning"} and evidence == "verified")


def judgment_id(event: dict) -> str:
    """Stable presentation ID shared by glyph, number and judgment text row."""
    explicit = event.get("judgmentId")
    if explicit:
        return str(explicit)
    sec = _f(event.get("sec"), 0.0) or 0.0
    basis = "|".join([
        _kind(event), f"{sec:.2f}", str(event.get("failureRole") or ""),
        str(event.get("status") or ""),
    ])
    return "j-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _prefer_spell_id(event: dict, glyph_key: str) -> int | None:
    ids = _spell_ids(event)
    canonical = KIND_SPELL_IDS.get(glyph_key)
    # Display identity follows the physical mechanic, not whichever downstream
    # damage/debuff row happened to survive into the review model.  Raw IDs are
    # retained separately in spellIDs for evidence/tooltips.
    if canonical:
        return int(canonical)
    if ids:
        return ids[0]
    return None


def _glyph_from_event(event: dict) -> dict:
    key = _kind(event)
    sec = _f(event.get("sec"), 0.0) or 0.0
    spell_id = _prefer_spell_id(event, key)
    source_title = str(event.get("label") or event.get("title") or key or "기믹")
    title = _skill_tip_title(key, GLYPH_TITLES.get(key) or source_title) or source_title
    tip_rows = _skill_tip_rows(spell_id, key)
    status = str(event.get("status") or "unknown")
    evidence = str(event.get("meaningEvidenceLevel") or event.get("evidenceLevel") or "")
    role = str(event.get("failureRole") or "")
    return {
        "glyphKey": key,
        "sec": round(sec, 3),
        "spellID": spell_id,
        "spellIDs": _spell_ids(event),
        "icon": event.get("icon") or _catalog_icon(spell_id, key),
        "catalogIconPath": _skill_tip_icon_path(spell_id, key),
        "tooltipRows": tip_rows,
        "fallback": FALLBACK_BADGE.get(key) or (title[:2] if title else "?"),
        "title": title,
        "sourceTitle": source_title,
        "logEvent": str(event.get("logEvent") or event.get("event") or ""),
        "summary": str(event.get("summary") or ""),
        "status": status,
        "evidenceLevel": evidence,
        "failureRole": role,
        "failureRoleLabel": str(event.get("failureRoleLabel") or ""),
        "judgmentId": judgment_id(event) if is_judgment(event) else None,
        "judgmentIds": [judgment_id(event)] if is_judgment(event) else [],
        "judgmentNumbers": [],
        "priority": _status_rank(event),
        "targets": [str(x) for x in (event.get("targets") or event.get("targetNames") or []) if x],
        "targetCount": _i(event.get("targetCount") if event.get("targetCount") is not None else event.get("uniqueTargets")),
        "sourceEvents": [dict(event)],
    }


def _merge_candidate(old: dict, new: dict, tolerance: float = 0.16) -> bool:
    if old.get("glyphKey") != new.get("glyphKey"):
        return False
    try:
        if abs(float(old.get("sec")) - float(new.get("sec"))) > tolerance:
            return False
    except (TypeError, ValueError):
        return False
    # A real judgment and the physical cast at the same instant are one glyph.
    # Presentation supplements (raid_facts enemy casts) may duplicate an
    # engine timeline cast at the same instant; merge only that explicit case.
    if bool(old.get("judgmentId") or new.get("judgmentId")):
        return True
    old_sup = any(bool(x.get("presentationSupplemental")) for x in (old.get("sourceEvents") or []))
    new_sup = any(bool(x.get("presentationSupplemental")) for x in (new.get("sourceEvents") or []))
    return bool(old_sup or new_sup) and _i(old.get("spellID")) == _i(new.get("spellID"))


def _merge_glyph(old: dict, new: dict) -> dict:
    if int(new.get("priority") or 0) > int(old.get("priority") or 0):
        keep, other = dict(new), old
    else:
        keep, other = dict(old), new
    keep["sourceEvents"] = list(old.get("sourceEvents") or []) + list(new.get("sourceEvents") or [])
    for field in ("spellID", "icon", "judgmentId", "failureRole", "failureRoleLabel", "summary", "evidenceLevel", "logEvent"):
        if not keep.get(field) and other.get(field):
            keep[field] = other.get(field)
    ids: list[int] = []
    for source in (old.get("spellIDs") or [], new.get("spellIDs") or []):
        for sid in source:
            iv = _i(sid)
            if iv and iv not in ids:
                ids.append(iv)
    keep["spellIDs"] = ids
    jids = []
    for source in (old.get("judgmentIds") or [], new.get("judgmentIds") or []):
        for jid in source:
            if jid and jid not in jids:
                jids.append(jid)
    if old.get("judgmentId") and old.get("judgmentId") not in jids:
        jids.append(old.get("judgmentId"))
    if new.get("judgmentId") and new.get("judgmentId") not in jids:
        jids.append(new.get("judgmentId"))
    keep["judgmentIds"] = jids
    targets = []
    for source in (old.get("targets") or [], new.get("targets") or []):
        for name in source:
            text = str(name or "").strip()
            if text and text not in targets:
                targets.append(text)
    keep["targets"] = targets
    if targets:
        keep["targetCount"] = max(int(old.get("targetCount") or 0), int(new.get("targetCount") or 0), len(targets))
    return keep


def _phase_marks(events: Iterable[dict]) -> list[dict]:
    marks = [{"phase": "P1", "sec": 0.0, "source": "implicit"}]
    seen = {("P1", 0.0)}
    for event in events:
        kind = str(event.get("kind") or "")
        if kind not in PHASE_KINDS:
            continue
        sec = _f(event.get("sec"))
        if sec is None:
            continue
        title = str(event.get("label") or event.get("title") or "")
        phase = "P2" if "P2" in title or kind in {"p2_entry", "phase2_entry"} else title or "P"
        key = (phase, round(sec, 3))
        if key in seen:
            continue
        seen.add(key)
        marks.append({"phase": phase, "sec": round(sec, 3), "source": kind, "summary": str(event.get("summary") or "")})
    return sorted(marks, key=lambda x: float(x.get("sec") or 0))


def _coiled_soulbinding_range(review: dict | None, duration: float) -> list[dict]:
    if not isinstance(review, dict) or str(review.get("bossKey") or "") != "coiled_altar":
        return []
    obs = review.get("observations") or {}
    p2 = obs.get("phase2") or {}
    start = _f(p2.get("endSec"))
    if start is None:
        return []
    difficulty = _i((review.get("encounter") or {}).get("difficultyID"))
    # 35 s is currently verified from Heroic supplied samples only.  On Mythic
    # preserve the start anchor without inventing an end duration.
    if difficulty == 15:
        end = min(duration, start + 35.0) if duration > 0 else start + 35.0
        return [{
            "rangeKey": "soulbinding", "label": "영혼 결속", "startSec": round(start, 3),
            "endSec": round(end, 3), "evidenceLevel": "verified", "durationSec": round(end-start, 3), "style": "band",
        }]
    return [{
        "rangeKey": "soulbinding_anchor", "label": "영혼 결속", "startSec": round(start, 3),
        "endSec": None, "evidenceLevel": "verified", "durationSec": None, "style": "anchor",
    }]


def _ulatek_heart_ranges(events: Iterable[dict], review: dict | None, duration: float) -> list[dict]:
    """Pair Ula'tek heart start/end anchors into compact duration bars.

    The engine already records verified heart start/end observations.  The glyph
    view renders the exposure window as one bar instead of two identical icons.
    This is presentation-only and does not change heart progress semantics.
    """
    if not isinstance(review, dict) or str(review.get("bossKey") or "") != "ulatek":
        return []
    starts = []
    ends = []
    for event in events:
        kind = str(event.get("kind") or "")
        if kind not in {"heart_start", "heart_end"}:
            continue
        sec = _f(event.get("sec"))
        if sec is None:
            continue
        title = str(event.get("label") or event.get("title") or "")
        m = re.search(r"(\d+)심장", title)
        idx = int(m.group(1)) if m else None
        row = {"index": idx, "sec": round(sec, 3), "title": title, "event": dict(event)}
        (starts if kind == "heart_start" else ends).append(row)
    starts.sort(key=lambda x: x["sec"]); ends.sort(key=lambda x: x["sec"])
    out = []
    used_end = set()
    for pos, start in enumerate(starts, start=1):
        idx = start.get("index") or pos
        end_row = None
        for i, candidate in enumerate(ends):
            if i in used_end or candidate["sec"] < start["sec"]:
                continue
            if candidate.get("index") in {None, idx}:
                end_row = candidate; used_end.add(i); break
        end_sec = end_row["sec"] if end_row is not None else None
        if end_sec is None:
            # Rules define a 20 s exposure window, but do not invent an end past
            # the observed fight.  This fallback is only for truncated old data.
            end_sec = min(duration, start["sec"] + 20.0) if duration > start["sec"] else start["sec"]
        out.append({
            "rangeKey": f"venomous_heart_{idx}", "label": f"{idx}심장",
            "title": _skill_tip_title("venomous_heart", "맹독의 심장") or "맹독의 심장",
            "startSec": round(start["sec"], 3), "endSec": round(end_sec, 3),
            "durationSec": round(max(0.0, end_sec-start["sec"]), 3),
            "evidenceLevel": "verified", "style": "bar",
            "glyphKey": "venomous_heart", "spellID": KIND_SPELL_IDS.get("venomous_heart"),
            "icon": _catalog_icon(KIND_SPELL_IDS.get("venomous_heart"), "venomous_heart"),
            "catalogIconPath": _skill_tip_icon_path(KIND_SPELL_IDS.get("venomous_heart"), "venomous_heart"),
            "tooltipRows": _skill_tip_rows(KIND_SPELL_IDS.get("venomous_heart"), "venomous_heart"),
        })
    return out


def _cooldown_bars(events: Iterable[dict], duration: float) -> list[dict]:
    bars = []
    for event in events:
        if _kind(event) != "bloodlust":
            continue
        sec = _f(event.get("sec"))
        if sec is None:
            continue
        end = min(duration, sec + 40.0) if duration > 0 else sec + 40.0
        bars.append({
            "cooldownKey": "bloodlust", "label": "블러드", "startSec": round(sec, 3),
            "endSec": round(end, 3), "durationSec": round(end-sec, 3),
            "spellID": _prefer_spell_id(event, "bloodlust"), "summary": str(event.get("summary") or ""),
        })
    return bars


def _is_physical_glyph(event: dict) -> bool:
    """Return True only for events that can honestly be drawn as a WoW spell glyph."""
    key = _kind(event)
    return bool(_prefer_spell_id(event, key) or event.get("icon") or _catalog_icon(None, key))


def _link_related_child_events(glyphs: list[dict]) -> list[dict]:
    """Attach non-judgment child events to the physical cast they describe.

    Coiled Altar's Gloombomb is the first case: cast -> interrupt -> explosion.
    A failed child remains separate so its exact judgment location is never hidden.
    """
    out: list[dict] = []
    for glyph in sorted(glyphs, key=lambda x: float(x.get("sec") or 0)):
        sources = glyph.get("sourceEvents") or []
        source_kind = str((sources[0] if sources else {}).get("kind") or "")
        key = str(glyph.get("glyphKey") or "")
        is_child = key == "gloombomb" and source_kind in {"interrupt", "gloombomb"}
        protected = bool(glyph.get("judgmentId")) or str(glyph.get("status") or "") in {"failed", "warning"}
        if is_child and not protected:
            sec = float(glyph.get("sec") or 0)
            owner = next((x for x in reversed(out)
                          if x.get("glyphKey") == "gloombomb"
                          and 0 <= sec - float(x.get("sec") or 0) <= 8.0), None)
            if owner is not None:
                owner.setdefault("linkedEvents", []).append({
                    "sec": round(sec, 3),
                    "title": str(glyph.get("sourceTitle") or glyph.get("title") or "암영폭탄 후속"),
                    "summary": str(glyph.get("summary") or ""),
                    "status": str(glyph.get("status") or "unknown"),
                    "evidenceLevel": str(glyph.get("evidenceLevel") or ""),
                    "spellIDs": list(glyph.get("spellIDs") or []),
                })
                owner["sourceEvents"] = list(owner.get("sourceEvents") or []) + list(glyph.get("sourceEvents") or [])
                continue
        out.append(glyph)
    return out


def _analysis_marker_from_event(event: dict) -> dict:
    key = _kind(event)
    sec = _f(event.get("sec"), 0.0) or 0.0
    title = str(event.get("label") or event.get("title") or GLYPH_TITLES.get(key) or key or "분석 표시")
    return {
        "markerKey": key,
        "sec": round(sec, 3),
        "title": title,
        "summary": str(event.get("summary") or ""),
        "status": str(event.get("status") or "unknown"),
        "evidenceLevel": str(event.get("meaningEvidenceLevel") or event.get("evidenceLevel") or ""),
        "marker": "◆",
    }


def _catalog_skill_references(boss_key: str) -> list[dict]:
    """Build the always-visible boss skill reference strip from skill_tips.json.

    This is presentation-only.  Catalog entries do not acquire a status or an
    evidence level merely because they are shown in the reference strip.
    """
    if not boss_key or _skill_tips_bridge is None:
        return []
    try:
        tips = list(_skill_tips_bridge.boss_spells(boss_key) or [])
    except Exception:
        return []

    rows: list[dict] = []
    for tip in tips:
        if not isinstance(tip, dict):
            continue
        sid = _i(tip.get("spellID"))
        if not sid:
            continue
        try:
            mapped_key = _skill_tips_bridge.glyph_key_for_spell(boss_key, sid)
        except Exception:
            mapped_key = None
        key = str(mapped_key or f"catalog_{sid}")
        legacy_title = str(tip.get("nameKO") or tip.get("nameEN") or GLYPH_TITLES.get(key) or key)
        rows.append({
            "glyphKey": key,
            "spellID": sid,
            "icon": tip.get("icon") or _catalog_icon(sid, key),
            "catalogIconPath": _skill_tip_icon_path(sid, key),
            "title": _skill_tip_title(key, legacy_title) or legacy_title,
            "tooltipRows": _skill_tip_rows(sid, key),
            "description": _skill_tip_description(
                sid, key,
                str(tip.get("what") or tip.get("todo") or "카탈로그 설명 준비 중"),
            ),
            "catalogReference": True,
        })
    return rows


def _skill_references(review: dict | None, glyphs: Iterable[dict], ranges: Iterable[dict]) -> list[dict]:
    boss_key = str((review or {}).get("bossKey") or "") if isinstance(review, dict) else ""
    rows: list[dict] = []
    seen_keys: set[str] = set()
    seen_spell_ids: set[int] = set()

    # B37.5 STEP6: every boss uses the complete curated skill catalog first.
    # The old hand-maintained list remains only as a backward-compatible
    # fallback for installations that do not yet have the catalog helpers.
    catalog_rows = _catalog_skill_references(boss_key)
    source_rows = catalog_rows if catalog_rows else [dict(x) for x in SKILL_REFERENCE_BY_BOSS.get(boss_key, [])]
    for raw in source_rows:
        row = dict(raw)
        key = str(row.get("glyphKey") or "")
        sid = _i(row.get("spellID")) or KIND_SPELL_IDS.get(key)
        if not row.get("catalogReference"):
            legacy_title = str(row.get("title") or GLYPH_TITLES.get(key) or key)
            legacy_desc = SKILL_DESCRIPTION_BY_GLYPH.get(key) or "보스 스킬 참고 정보입니다."
            row.setdefault("spellID", sid)
            row.setdefault("icon", _catalog_icon(sid, key))
            row["catalogIconPath"] = _skill_tip_icon_path(sid, key)
            row["title"] = _skill_tip_title(key, legacy_title) or legacy_title
            row["tooltipRows"] = _skill_tip_rows(sid, key)
            row["description"] = _skill_tip_description(sid, key, legacy_desc)
        rows.append(row)
        if key:
            seen_keys.add(key)
        if sid:
            seen_spell_ids.add(int(sid))

    # Keep observed/range-only mechanics that are not represented in the boss
    # catalog.  This preserves analyzer-specific candidates and synthetic
    # mechanics without duplicating catalog spells.
    for item in list(glyphs) + list(ranges):
        key = str(item.get("glyphKey") or item.get("rangeKey") or "")
        if not key or key in {"bloodlust", "collapse", "soulbinding", "soulbinding_anchor"}:
            continue
        sid = _i(item.get("spellID")) or KIND_SPELL_IDS.get(key)
        if key in seen_keys or (sid and int(sid) in seen_spell_ids):
            continue
        legacy_title = GLYPH_TITLES.get(key) or str(item.get("title") or item.get("label") or key)
        legacy_desc = SKILL_DESCRIPTION_BY_GLYPH.get(key) or "이 트라이에서 관측된 실제 스킬입니다."
        rows.append({
            "glyphKey": key, "spellID": sid,
            "icon": item.get("icon") or _catalog_icon(sid, key),
            "catalogIconPath": item.get("catalogIconPath") or _skill_tip_icon_path(sid, key),
            "title": _skill_tip_title(key, legacy_title) or legacy_title,
            "tooltipRows": _skill_tip_rows(sid, key),
            "description": _skill_tip_description(sid, key, legacy_desc),
        })
        seen_keys.add(key)
        if sid:
            seen_spell_ids.add(int(sid))
    return rows


def _main_spell_id(item: dict) -> int | None:
    """Catalog main spell for a glyph/range/reference (cast vs damage IDs merge)."""
    key = str(item.get("glyphKey") or item.get("rangeKey") or "")
    sid = _i(item.get("spellID"))
    if _skill_tips_bridge is not None:
        candidates = [sid] + [_i(x) for x in (item.get("spellIDs") or [])]
        for cand in [c for c in candidates if c]:
            try:
                tip = _skill_tips_bridge.tip_for(spell_id=cand)
            except Exception:
                tip = None
            if tip and _i(tip.get("spellID")):
                return _i(tip.get("spellID"))
        if key and not key.startswith("catalog_"):
            try:
                tip = _skill_tips_bridge.tip_for(glyph_key=key)
            except Exception:
                tip = None
            if tip and _i(tip.get("spellID")):
                return _i(tip.get("spellID"))
    return sid or KIND_SPELL_IDS.get(key)


def _annotate_occurrences(glyphs: list[dict]) -> None:
    """B37.5.0.2: '2/3번째' for timeline tooltips (same glyph kind, time order)."""
    totals: dict[str, int] = {}
    for g in glyphs:
        k = str(g.get("glyphKey") or "")
        totals[k] = totals.get(k, 0) + 1
    seen: dict[str, int] = {}
    for g in glyphs:
        k = str(g.get("glyphKey") or "")
        seen[k] = seen.get(k, 0) + 1
        g["occurrenceIndex"] = seen[k]
        g["occurrenceTotal"] = totals[k]


def _annotate_reference_observations(refs: list[dict], glyphs: Iterable[dict], ranges: Iterable[dict]) -> None:
    """B37.5.0.2: how often each 스킬 참고 skill actually appears in this Try.

    Presentation only — a count never changes status or evidence level.
    """
    observed: dict[Any, list[float]] = {}
    for item in list(glyphs) + list(ranges):
        sec = _f(item.get("sec"), None)
        if sec is None:
            sec = _f(item.get("startSec"), None)
        keys = {("k", str(item.get("glyphKey") or item.get("rangeKey") or ""))}
        main = _main_spell_id(item)
        if main:
            keys.add(("s", main))
        for k in keys:
            observed.setdefault(k, []).append(sec)
    for ref in refs:
        secs: list = []
        main = _main_spell_id(ref)
        for k in (("s", main), ("k", str(ref.get("glyphKey") or ""))):
            if k[1] and k in observed:
                secs = observed[k]
                break
        clean = sorted(round(s, 3) for s in secs if s is not None)
        ref["observedCount"] = len(secs)
        ref["observedSecs"] = clean


def build_timeline_glyph_model(model: dict | None, review: dict | None = None) -> dict:
    model = model or {}
    events = [dict(x) for x in (model.get("timeline") or []) if isinstance(x, dict) and x.get("sec") is not None]
    events += [dict(x) for x in (model.get("supplementalGlyphEvents") or []) if isinstance(x, dict) and x.get("sec") is not None]
    duration = _f(model.get("durationSec"), 0.0) or 0.0
    if events:
        duration = max(duration, max(_f(x.get("sec"), 0.0) or 0.0 for x in events) + 2.0)

    phases = _phase_marks(events)
    cooldowns = _cooldown_bars(events, duration)
    ranges = _coiled_soulbinding_range(review, duration) + _ulatek_heart_ranges(events, review, duration)

    glyphs: list[dict] = []
    analysis_markers: list[dict] = []
    judgments: list[dict] = []
    for event in sorted(events, key=lambda x: _f(x.get("sec"), 0.0) or 0.0):
        kind = str(event.get("kind") or "")
        if kind in HIDDEN_KINDS or kind in PHASE_KINDS or _kind(event) == "bloodlust":
            continue
        if isinstance(review, dict) and str(review.get("bossKey") or "") == "ulatek" and kind in {"heart_start", "heart_end"}:
            continue
        # Synthetic analyzer events without a real spell identity must not masquerade
        # as WoW spell squares. Judgments are projected below as standalone markers.
        if not _is_physical_glyph(event):
            if not is_judgment(event):
                analysis_markers.append(_analysis_marker_from_event(event))
            continue
        glyph = _glyph_from_event(event)
        match_i = next((i for i, old in enumerate(glyphs) if _merge_candidate(old, glyph)), None)
        if match_i is None:
            glyphs.append(glyph)
        else:
            glyphs[match_i] = _merge_glyph(glyphs[match_i], glyph)

    glyphs = _link_related_child_events(glyphs)

    # Assign judgment order from the authoritative timeline order.  A judgment
    # with a spell/glyph owner is attached there; standalone marker only when no
    # such owner exists.
    seen_jids: set[str] = set()
    standalone: list[dict] = []
    for event in sorted((x for x in events if is_judgment(x)), key=lambda x: _f(x.get("sec"), 0.0) or 0.0):
        jid = judgment_id(event)
        if jid in seen_jids:
            continue
        seen_jids.add(jid)
        key = _kind(event)
        sec = _f(event.get("sec"), 0.0) or 0.0
        owner = next((g for g in glyphs if g.get("glyphKey") == key and abs(float(g.get("sec") or 0)-sec) <= 0.20), None)
        number = len(judgments) + 1
        entry = {
            "judgmentId": jid, "number": number, "sec": round(sec, 3), "glyphKey": key,
            "title": str(event.get("label") or event.get("title") or key),
            "summary": str(event.get("summary") or ""), "status": str(event.get("status") or "unknown"),
            "evidenceLevel": str(event.get("meaningEvidenceLevel") or event.get("evidenceLevel") or ""),
            "failureRole": str(event.get("failureRole") or ""),
            "failureRoleLabel": str(event.get("failureRoleLabel") or ""),
            "targets": [str(x) for x in (event.get("targets") or event.get("targetNames") or []) if x],
            "targetCount": _i(event.get("targetCount") if event.get("targetCount") is not None else event.get("uniqueTargets")),
            "attached": bool(owner),
        }
        judgments.append(entry)
        if owner is not None:
            owner["judgmentId"] = owner.get("judgmentId") or jid
            owner.setdefault("judgmentIds", [])
            if jid not in owner["judgmentIds"]:
                owner["judgmentIds"].append(jid)
            owner.setdefault("judgmentNumbers", [])
            if number not in owner["judgmentNumbers"]:
                owner["judgmentNumbers"].append(number)
            owner["judgmentNumber"] = owner["judgmentNumbers"][0]
            owner["priority"] = max(int(owner.get("priority") or 0), _status_rank(event))
            if str(event.get("status") or "") in {"failed", "warning"}:
                owner["status"] = str(event.get("status"))
            if event.get("failureRole"):
                owner["failureRole"] = event.get("failureRole")
                owner["failureRoleLabel"] = event.get("failureRoleLabel")
        else:
            standalone.append({**entry, "marker": "▼" if key in {"collapse", "collapse_start"} else "◆"})

    collapse_sec = _f(model.get("collapseSec"))
    collapse_range = None
    if collapse_sec is not None:
        collapse_range = {"startSec": round(collapse_sec, 3), "endSec": round(duration, 3), "label": "붕괴 이후"}

    death_events = [dict(x) for x in (model.get("deathEvents") or []) if isinstance(x, dict) and x.get("sec") is not None]

    glyphs = sorted(glyphs, key=lambda x: (float(x.get("sec") or 0), -int(x.get("priority") or 0)))
    _annotate_occurrences(glyphs)
    skill_refs = _skill_references(review, glyphs, ranges)
    _annotate_reference_observations(skill_refs, glyphs, ranges)
    return {
        "schemaVersion": 1,
        "durationSec": round(duration, 3),
        "glyphs": glyphs,
        "phaseMarks": phases,
        "ranges": ranges,
        "skillReferences": skill_refs,
        "cooldowns": cooldowns,
        "standaloneMarkers": standalone,
        "analysisMarkers": sorted(analysis_markers, key=lambda x: float(x.get("sec") or 0)),
        "judgments": judgments,
        "deathEvents": death_events,
        "collapseRange": collapse_range,
    }


def place_glyph_lanes(glyphs: Iterable[dict], x_for, *, icon_px: int = 20, max_lanes: int = 3) -> tuple[list[dict], list[dict]]:
    """Assign visual lanes while never hiding failure/warning glyphs.

    Normal/observation glyphs that cannot fit in ``max_lanes`` are returned in
    ``overflow`` for a +N badge.  Judgments always receive a lane even if that
    temporarily exceeds the nominal lane count.
    """
    placed: list[dict] = []
    overflow: list[dict] = []
    lane_last_x: list[float] = [-10**9] * max_lanes
    gap = max(14, int(icon_px + 3))
    for raw in sorted((dict(x) for x in glyphs), key=lambda x: (float(x.get("sec") or 0), -int(x.get("priority") or 0))):
        x = float(x_for(raw.get("sec") or 0))
        lane = next((i for i, last in enumerate(lane_last_x) if x - last >= gap), None)
        protected = bool(raw.get("judgmentId")) or str(raw.get("status") or "") in {"failed", "warning"}
        if lane is None and protected:
            lane = len(lane_last_x)
            lane_last_x.append(-10**9)
        if lane is None:
            raw["x"] = x
            overflow.append(raw)
            continue
        raw["lane"] = lane
        raw["x"] = x
        lane_last_x[lane] = x
        placed.append(raw)
    return placed, overflow
