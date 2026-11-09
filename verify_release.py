"""Minimal pre-build audit; does not read or modify user session data."""
from __future__ import annotations
import ast, json, re
from pathlib import Path
ROOT=Path(__file__).resolve().parent
for must_not in ('nini_wcl_pipeline.py', 'resources/brand_personal',
                 'data/live_sessions', '.env', 'engine/nini_archive.py'):
    assert not (ROOT/must_not).exists(), f'Private content found: {must_not}'
assert (ROOT/'resources/brand_public/nini_analyzer.ico').is_file()
gui=(ROOT/'nini_wcl_gui.py').read_text(encoding='utf-8')
assert 'Public distribution does not support personal mode' in gui
assert 'CHARACTERS = ()' in gui
assert 'DEFAULT_PLAYER = ""' in gui
assert 'if not self.raid_public_mode:\n            self._build_nini_archive_page()' in gui
strategy=json.loads((ROOT/'live/boss_mechanics/default_strategies.json').read_text(encoding='utf-8'))
def has_private_plan(x):
    if isinstance(x, dict): return 'currentFourHealPlan' in x or any(has_private_plan(v) for v in x.values())
    if isinstance(x,list): return any(has_private_plan(v) for v in x)
    return False
assert not has_private_plan(strategy)
for p in ROOT.rglob('*.py'):
    if 'dist' in p.parts or 'build' in p.parts: continue
    ast.parse(p.read_text(encoding='utf-8'),filename=str(p))
print('PUBLIC AUDIT PASS — mode lock, resources, rules, source syntax')

# Public-specific archive policy: never create capsules or discover Drive folders.
review=(ROOT/'live/raid_review.py').read_text(encoding='utf-8')
assert 'if not raid_only:\n                    info.update(_archive_after_review' in review
public_branch=review.split('    if raid_only:\n        try:', 1)[1].split('    try:\n        with live_progress.active(progress):',1)[0]
assert '_archive_after_review(' not in public_branch
assert 'if self.raid_only:\n            # PUBLIC never scans' in review
assert (ROOT/'public_errorlog.py').is_file()
assert 'self._public_home if self.raid_public_mode' in gui
print('PUBLIC LEAK GUARD PASS — no raid-only capsule, Drive sync, or EXE-local writable cache')

# Stage 2.4: first-run + single error dialog + sealed release packager.
assert (ROOT/'public_first_run.py').is_file()
assert (ROOT/'package_public_release.py').is_file()
entry=(ROOT/'nini_raid_gui.py').read_text(encoding='utf-8')
assert 'start_error_notice_poll(app)' in entry
assert 'maybe_show_first_run(app)' in entry
assert 'PUBLIC_APP_VERSION = "1.0.5"' in gui
print('STAGE 2.4 RELEASE GATE PASS — first-run guide, local-only error notice and packager')

assert (ROOT/'build_public_windows.py').read_text(encoding='utf-8').find("'--contents-directory', '_internal'") >= 0
print('RC3 LAYOUT GATE PASS — bundled libs and assets stored in _internal')

# RC4: fixes that must never silently regress in a public build.
theme=(ROOT/'live/ui_theme.py').read_text(encoding='utf-8')
# RC5: GOLD HF6 (child-geometry guard + always-visible scrollbar) replaces the RC4 reqheight poll.
assert ('def _watch_reqheight' in theme or 'def _sync_region_after_layout' in theme), 'ScrollPage HF6 missing'
assert 'AddFontResourceExW' in theme and '0x10, None' in theme, 'process-private font loader missing'
for f in ('PyeojinGothic-Medium.ttf','Paperlogy-6SemiBold.ttf','LICENSE_PyeojinGothic_OFL.txt','LICENSE_Paperlogy_OFL.txt'):
    assert (ROOT/'assets/fonts'/f).is_file(), f'bundled font/licence missing: {f}'
assert 'FEIGN_DEATH_SPELL_ID' in (ROOT/'live/raid_facts.py').read_text(encoding='utf-8')
assert '_drop_feign_deaths' in (ROOT/'live/boss_mechanics/engine.py').read_text(encoding='utf-8')
assert 'def _pull_badge_text' in gui and 'def _public_auto_open_latest_session' in gui
iss=(ROOT/'installer/NiniAnalyzer_Public.iss').read_text(encoding='utf-8-sig')
assert 'AppId={{' in iss and 'PrivilegesRequired=lowest' in iss and 'AppUserModelID: "{#AppUserModelID}"' in iss
assert '#define AppVersion "1.0.5"' in iss
assert 'UninstallDelete' not in iss, 'installer must never delete user raid records'
print('RC4 GATE PASS — scroll, fonts, feign death, puller, first screen, installer')

# RC4.1: Feign Death needs positive evidence; puller line is model-independent;
# reanalysis always backs results up first.
rf=(ROOT/'live/raid_facts.py').read_text(encoding='utf-8')
assert 'FEIGN_CONFIRM_SEC' in rf and 'feignDeathsKeptUncertain' in rf
assert 'FEIGN_CONFIRM_SEC' in (ROOT/'live/boss_mechanics/engine.py').read_text(encoding='utf-8')
assert '_feignPending' in (ROOT/'live/encounter_tracker.py').read_text(encoding='utf-8')
assert 'self.live_pull_var.set(self._pull_line_text(row))' in gui
assert 'backup = backup_try_results(storage_dir)' in (ROOT/'live/reanalysis.py').read_text(encoding='utf-8')
print('RC4.1 GATE PASS — feign death evidence, first-attacker line, reanalysis backup')

# RC4.2: opt-in Feign Death recheck; series never rewrites other Tries.
fr=(ROOT/'live/feign_recheck.py').read_text(encoding='utf-8')
assert 'def scan_feign_candidates' in fr and 'persist_display_names=False' in fr
assert 'def recheck_selected' in fr and 'backup_try_results(series_dir)' in fr
assert 'write=False, use_cache=False' in (ROOT/'live/raid_wipe_review.py').read_text(encoding='utf-8')
assert 'from live.public_feign_ui import open_settings' in gui
assert "live.feign_recheck" in (ROOT/'build_public_windows.py').read_text(encoding='utf-8')
_cr = fr[fr.index('def check_raw'):fr.index('def _row_labels')]
assert 'line.decode("utf-8")' in _cr and 'errors="replace"' not in _cr and 'break' not in _cr, 'raw check must read every byte strictly'
print('RC4.2 GATE PASS — feign recheck (read-only search, selected only, backup first), series isolation')

# RC4.3: puller / first attacker name in WoW class colour.
cc=(ROOT/'live/class_colors.py').read_text(encoding='utf-8')
assert '"MAGE": "#3FC7EB"' in cc and 'def color_for_player' in cc
assert 'self._render_pull_line(row)' in gui
assert 'live.class_colors' in (ROOT/'build_public_windows.py').read_text(encoding='utf-8')
print('RC4.3 GATE PASS — puller name class colour')

# RC4.4: an installed copy of a bundled font must not be registered again privately.
ut=(ROOT/'live/ui_theme.py').read_text(encoding='utf-8')
_rb=ut[ut.index('def register_bundled_fonts'):ut.index('def resolve_families')]
assert '_installed_copy(filename, entries)' in _rb and _rb.index('_installed_copy(filename') < _rb.index('add_font(str(fp)'), 'installed check must precede private load'
assert 'HKEY_CURRENT_USER' in ut and 'HKEY_LOCAL_MACHINE' in ut
assert (ROOT/'qa/test_public_rc4_4_fonts.py').is_file()
print('RC4.4 GATE PASS — no double font registration')

# RC5: GOLD V4.7 display merged; personal-only data and modules stay out.
for _m in ('v43_layout','v45_timeline','boss_guides_v1','coiled_wipe_cause_v1','ulatek_conclusion_v1',
           'raid_personal_context_v35','raid_chronology_v37','raid_chronology_glance_v42',
           'raid_collapse_display_v38','raid_readable_korean_v39','raid_glance_v40'):
    assert (ROOT/'live'/f'{_m}.py').is_file(), f'RC5 module missing: {_m}'
    assert f'live.{_m}' in (ROOT/'build_public_windows.py').read_text(encoding='utf-8'), f'RC5 hidden import missing: {_m}'
for _p in ('engine/external_wcl_report.py','engine/elite_rogue_viewer.py',
           'live/raid_chronology_index_v37.json','live/raid_personal_audit_v35.json'):
    assert not (ROOT/_p).exists(), f'Private content found: {_p}'
assert 'PLAYER = "내 캐릭터"' in (ROOT/'live/raid_personal_context_v35.py').read_text(encoding='utf-8')
assert 'import_module(f"live.{name}")' in (ROOT/'live/v43_layout.py').read_text(encoding='utf-8'), 'frozen boss module fallback missing'
assert 'if not self.raid_public_mode:\n            # B37.5.0.1: archive and external WCL are PRIVATE_ONLY' in gui or 'self.raid_public_mode' in gui
assert 1297630 and '각성의 그릇' in (ROOT/'live/boss_guides_v1.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_rc5_layout.py').is_file()
print('RC5 GATE PASS — V4.7 display modules bundled, frozen boss lookup, personal data excluded')

# RC5.3: GOLD V5.0 (키식스 신화 · 울라텍 늦은 절규 표시) + V5.2 (트라이 화면 · 똬리 차단 규칙). 글꼴은 공개 글꼴 그대로.
assert 'BOSS-GUIDES-V1.6' in (ROOT/'live/boss_guides_v1.py').read_text(encoding='utf-8')
assert re.search(r'ULATEK-CONCLUSION-V(1\.(8|9|\d\d)|[2-9]\.\d+)', (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8'))  # RC5.4: V1.8 이상 (1.0.5: V2.0)
assert re.search(r'COILED-WIPE-CAUSE-V3\.[4-9]', (ROOT/'live/coiled_wipe_cause_v1.py').read_text(encoding='utf-8'))  # RC5.4: V3.4 이상
assert 'live.v52_screen' in (ROOT/'build_public_windows.py').read_text(encoding='utf-8')
assert "('kick_rules_3429.json','.')" in (ROOT/'build_public_windows.py').read_text(encoding='utf-8')
assert json.loads((ROOT/'kick_rules_3429.json').read_text(encoding='utf-8')).get('secondAdd') == 'beforeBomb'
assert '_v52.apply(app, row)' in (ROOT/'live/v43_layout.py').read_text(encoding='utf-8')
assert 'import_module(f"live.{name}")' in (ROOT/'live/v43_layout.py').read_text(encoding='utf-8')
assert not list((ROOT/'assets/fonts').glob('Naowh*')) and not list((ROOT/'assets/fonts').glob('Gotham*')), 'personal fonts must not ship'
assert (ROOT/'qa/test_public_rc5_3.py').is_file()
print('RC5.3 GATE PASS — Kithix mythic, Coiled kick rules, try screen links/shield box, public fonts only')

# RC5.3.1: 도적 전용 모듈(내 캐릭터 쿨기 계획 비교 · 도적 정밀 분석)은 공개판에 넣지 않는다.
for _p in ('engine/plan_vs_actual.py', 'live/historical_precision.py'):
    assert not (ROOT/_p).exists(), f'Rogue-only module must not ship: {_p}'
_b = (ROOT/'build_public_windows.py').read_text(encoding='utf-8')
assert 'plan_vs_actual' not in _b and 'historical_precision' not in _b, 'rogue-only hidden import left'
assert 'RC5.3.1: 도적 정밀 분석 모듈은 공개판에 없다' in gui
assert (ROOT/'qa/test_public_rc5_3_1.py').is_file()
print('RC5.3.1 GATE PASS — rogue-only modules removed from the public build')

# RC5.4: GOLD V5.2.2~V5.2.4 화면·엔진 + 판정 V3.7/V1.9. GOLD 전용(화면 버전 표시·글꼴·사이드바)은 넣지 않는다.
assert not (ROOT/'live/display_patch.py').exists(), 'GOLD display_patch must not ship'
_c = (ROOT/'live/coiled_wipe_cause_v1.py').read_text(encoding='utf-8')
assert 'VERSION = "COILED-WIPE-CAUSE-V3.7"' in _c and 'def soul_link_order(' in _c
assert re.search(r'VERSION = "ULATEK-CONCLUSION-V(1\.9|[2-9]\.\d+)"', (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8'))  # 1.0.5: V2.0
_e = (ROOT/'live/boss_mechanics/engine.py').read_text(encoding='utf-8')
assert 'def _fast_rows_spell(' in _e and 'GRAVEBOUND_DURATION_SEC = 11.0' in _e
assert 'def live_analysis_busy(' in (ROOT/'live/raid_review.py').read_text(encoding='utf-8')
for _q in ('qa/test_public_rc5_4.py', 'qa/test_public_rc5_4_gravebound.py', 'qa/test_public_rc5_4_speed.py'):
    assert (ROOT/_q).is_file(), _q
print('RC5.4 GATE PASS — GOLD V5.2.4 sync, judgment V3.7/V1.9')

# RC5.5: 아이콘 · 로고 Nini Brand Kit R3 (BLUE). 예전 흰 네모 아이콘이 다시 들어오면 막는다.
# (Pillow 는 이 검사 뒤에 설치될 수 있어서, 없으면 그림 검사는 qa/test_public_rc5_5_brand.py 에 맡긴다)
assert (ROOT/'qa/test_public_rc5_5_brand.py').is_file()
try:
    from PIL import Image as _Img
except ImportError:
    _Img = None
if _Img is not None:
    _ic = _Img.open(ROOT/'resources/brand_public/nini_analyzer_256.png').convert('RGBA')
    assert _ic.getpixel((0, 0))[3] == 0, 'RC5.5: icon corner must be transparent (R3 round icon)'
    assert all(abs(a - b) <= 6 for a, b in zip(_ic.getpixel((128, 20))[:3], (0x0D, 0x39, 0x52))), 'RC5.5: R3 navy circle'
    assert _Img.open(ROOT/'resources/brand_public/nini_analyzer_sidebar_wordmark.png').size == (196, 42)
print('RC5.5 GATE PASS — Brand Kit R3 icons and sidebar wordmark')

# RC5.6: 2026-11-06 신화 핫픽스 (무덤구속 13초 자동 · 송곳니 내장 파열 [추정])
_e = (ROOT/'live/boss_mechanics/engine.py').read_text(encoding='utf-8')
assert 'GRAVEBOUND_DURATION_SEC_HOTFIX = 13.0' in _e and 'def gravebound_duration_from_rows(' in _e, 'RC5.6: gravebound hotfix duration'
assert '붕괴 전 끊지 못한 시전' in (ROOT/'live/boss_guides_v1.py').read_text(encoding='utf-8'), 'RC5.6: twin fangs estimate'
assert (ROOT/'qa/test_public_rc5_6_hotfix.py').is_file()
print('RC5.6 GATE PASS — 2026-11-06 mythic hotfix (Gravebound 13s, Twin Fangs missed kick)')

# 1.0.1: 본인에게만 걸린 블러드 계열 버프는 블러드가 아님 (GOLD V5.3.4)
_e = (ROOT/'live/boss_mechanics/engine.py').read_text(encoding='utf-8')
assert 'def _lust_raidwide_aura_rows(' in _e and '_LUST_DEBUFF_IDS' in _e, '1.0.1: raid-wide bloodlust filter'
assert (ROOT/'qa/test_public_1_0_1_bloodlust.py').is_file()
print('1.0.1 GATE PASS — bloodlust needs cast / raid-wide aura / exhaustion debuff')

# 1.0.2: 똬리 판정 3개 (GOLD V5.3.5) — P2 진입 잔여 구슬은 진입 시점 장부값 · P1→P2 몇 초 차이는 주의 · 보호막 깨졌으면 쿨기 지연은 참고
assert 'fight_end_active = safe_int(orb_accounting.get("currentActiveOrbs"))' in _e, '1.0.2: P2 entry orbs from ledger at entry'
assert '"minorEarlySec"' in _e and 'p1_status = "warning"' in _e, '1.0.2: minor P1 timing is a warning'
assert 'align_note = "해당 보호막은 모두 제때 깨짐 · 참고"' in _e, '1.0.2: cooldown alignment on broken shields is a note'
assert (ROOT/'qa/test_public_1_0_2_coiled_judgments.py').is_file()
print('1.0.2 GATE PASS — coiled P2 entry orbs / P1 timing / shield cooldown notes')

# 1.0.3: 첫 실행 안내 카드 · 새 소식 카드
_ob = (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert 'def open_welcome(app)' in _ob and 'def maybe_show_whats_new(' in _ob, '1.0.3: onboarding cards'
assert '("1.0.3", [' in _ob, '1.0.3: whats-new entry for this version'
assert 'maybe_show_whats_new' in (ROOT/'nini_raid_gui.py').read_text(encoding='utf-8'), '1.0.3: entrypoint schedules whats-new'
assert (ROOT/'qa/test_public_1_0_3_onboarding.py').is_file()
print('1.0.3 GATE PASS — onboarding / whats-new cards')

# 1.0.5: 울라텍 신화 알 운반 → 살무사 (GOLD V5.2.7 · V5.3.6) · 1.0.4 태그는 1.0.3 커밋에 남아 있어 건너뜀
_u = (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8')
assert 'VERSION = "ULATEK-CONCLUSION-V2.0"' in _u and 'def viper_review(' in _u, '1.0.5: egg carry -> viper chain'
assert 'VIPER_STUN = 1303414' in _u and 'def viper_marks(' in _u and 'def egg_viper_notes(' in _u, '1.0.5: viper mark -> landing stun notes'
assert 'notes[:0] = egg_viper_notes(vr)' in _u, '1.0.5: egg/viper notes shown first'
assert 'raidplan_ulatek' not in _u
assert '("1.0.5", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8'), '1.0.5: whats-new entry'
assert (ROOT/'qa/test_public_1_0_5_ulatek_viper.py').is_file()
print('1.0.5 GATE PASS — Ulatek egg/wave -> viper -> stun')
