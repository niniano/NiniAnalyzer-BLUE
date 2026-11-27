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
assert 'PUBLIC_APP_VERSION = "1.0.27"' in gui
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
assert '#define AppVersion "1.0.27"' in iss
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
assert re.search(r'BOSS-GUIDES-V1\.([6-9]|\d\d)\b', (ROOT/'live/boss_guides_v1.py').read_text(encoding='utf-8'))  # 1.0.6: V1.10
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
assert re.search(r'VERSION = "COILED-WIPE-CAUSE-V3\.([7-9]|\d\d)"', _c) and 'def soul_link_order(' in _c
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
assert 'VERSION = "ULATEK-CONCLUSION-V2.' in _u and 'def viper_review(' in _u, '1.0.5: egg carry -> viper chain'
assert 'VIPER_STUN = 1303414' in _u and 'def viper_marks(' in _u and 'def egg_viper_notes(' in _u, '1.0.5: viper mark -> landing stun notes'
assert 'notes[:0] = egg_viper_notes(vr' in _u, '1.0.5: egg/viper notes shown first'
assert 'raidplan_ulatek' not in _u
assert '("1.0.5", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8'), '1.0.5: whats-new entry'
assert (ROOT/'qa/test_public_1_0_5_ulatek_viper.py').is_file()
print('1.0.5 GATE PASS — Ulatek egg/wave -> viper -> stun')

# 1.0.6: GOLD V5.2.8~V5.3.3 따라잡기 (BOSS-GUIDES V1.10 · 킬 판 결론 · 기믹 수행 체크 보드 · 똬리 정확도)
_bg = (ROOT/'live/boss_guides_v1.py').read_text(encoding='utf-8')
assert 'VERSION = "BOSS-GUIDES-V1.10"' in _bg and 'MYTHIC_IDS = (16, 233)' in _bg, '1.0.6: boss guides V1.10'
assert '"ownerCd": {' not in _bg and '121471' not in _bg, '1.0.6: rogue owner cooldown line stays GOLD-only'
for _m in ('v528_review', 'v529_board', 'raid_coiled_brief_v43', 'boss_mechanics.coiled_p1_closeout'):
    assert (ROOT/'live'/(_m.replace('.', '/') + '.py')).is_file(), _m
    assert f'live.{_m}' in (ROOT/'build_public_windows.py').read_text(encoding='utf-8'), f'1.0.6 hidden import missing: {_m}'
assert 'import_module("live.boss_guides_v1")' in (ROOT/'live/v529_board.py').read_text(encoding='utf-8'), '1.0.6: frozen board fallback'
_g = (ROOT/'nini_wcl_gui.py').read_text(encoding='utf-8')
assert 'from live.v529_board import add_guide_pairs' in _g and 'from live.v528_review import fill_duration' in _g
assert '"p1_guillotine_soak"' in _g and 'def _coiled_hf4_factual_glance(' in _g
assert 'from .coiled_p1_closeout import analyze' in (ROOT/'live/boss_mechanics/engine.py').read_text(encoding='utf-8')
assert 233 in __import__('ast').literal_eval((ROOT/'live/session_summary.py').read_text(encoding='utf-8').split('RAID_DIFFICULTIES = ', 1)[1].split('\n', 1)[0])
# 1.0.16: live/wow_calendar_sync.py 는 이제 BLUE 에도 들어감 (아래 1.0.16 GATE)
for _p in ('live/display_patch.py', 'live/raid_chronology_index_v37.json'):
    assert not (ROOT/_p).exists(), f'GOLD-only file found: {_p}'
for _q in ('kill_review', 'board', 'nullify', 'coiled_accuracy'):
    assert (ROOT/f'qa/test_public_1_0_6_{_q}.py').is_file(), _q
print('1.0.6 GATE PASS — GOLD V5.2.8~V5.3.3 catch-up (Kith\'ix V1.10, kill review, board, Coiled accuracy)')

# 1.0.7: GOLD V5.3.7 (똬리 V3.8 P1 단두대 붕괴 · 킬 판 결론 · 울라텍 심장 도중 전멸)
_c7 = (ROOT/'live/coiled_wipe_cause_v1.py').read_text(encoding='utf-8')
assert 'VERSION = "COILED-WIPE-CAUSE-V3.8"' in _c7 and 'p1_origin' in _c7 and 'def kill_review(' in _c7, '1.0.7: coiled V3.8'
_u7 = (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8')
assert 'def _heart_done(h)' in _u7 and '도중 전멸' in _u7 and 'raidplan_ulatek' not in _u7, '1.0.7: ulatek heart cut'
assert '("1.0.7", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_7_coiled_kill_ulatek_heart.py').is_file()
print('1.0.7 GATE PASS — Coiled P1 guillotine collapse / kill review, Ulatek heart cut')

# 1.0.8: GOLD V5.3.8 울라텍 알방 큰 알 (개인 임무 파일은 GOLD 전용)
_u8 = (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8')
assert 'def big_egg_review(' in _u8 and 'BIG_EGG_SHELL = 1300312' in _u8, '1.0.8: big egg review'
assert not (ROOT/'live/personal_duties.json').exists(), '1.0.8: personal duties stay GOLD-only'
assert '("1.0.8", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_8_ulatek_big_egg.py').is_file()
print('1.0.8 GATE PASS — Ulatek egg room big eggs (4:15)')

# 1.0.9: GOLD V5.4.1~V5.4.2 울라텍 「알 운반」 카드 (누가 알을 들었나) · 참고 줄 짧게 · 개인 임무 캐릭터 고르기
_u9 = (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8')
assert 'def egg_carry_review(' in _u9 and 'def egg_carry_lines(' in _u9 and 'def duty_player(' in _u9, '1.0.9: egg carry card data'
assert 'def _egg_card(' in (ROOT/'live/v43_layout.py').read_text(encoding='utf-8'), '1.0.9: egg carry card'
assert not (ROOT/'live/personal_duties.json').exists(), '1.0.9: personal duties stay GOLD-only'
assert '("1.0.9", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_9_egg_card.py').is_file()
print('1.0.9 GATE PASS — Ulatek egg carry card')

# 1.0.10: GOLD V5.4.3 결론 칸 · 참고 줄 마지막 줄 잘림 (글 높이를 픽셀로 맞춤)
_v10 = (ROOT/'live/v43_layout.py').read_text(encoding='utf-8')
assert '"-ypixels"' in _v10 and 'def _linespace(' in _v10, '1.0.10: fit text by pixels'
assert '("1.0.10", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_10_fit_text.py').is_file()
assert (ROOT/'live/v544_glance.py').is_file() and '_show_glance(app' in _v10, '1.0.10: glance'
assert "--hidden-import=live.v544_glance" in (ROOT/'build_public_windows.py').read_text(encoding='utf-8'), '1.0.10: glance in frozen build'
assert (ROOT/'qa/test_public_1_0_10_glance.py').is_file()
print('1.0.10 GATE PASS — text rows not clipped · glance')

# 1.0.11: GOLD V5.4.5 한눈에 보기 다듬기 (둥근 칩 · 한 줄 무너진 순서 · 굵은 화살표 · 버튼 하나 · 오피격 → 같이 맞음)
_v11 = (ROOT/'live/v43_layout.py').read_text(encoding='utf-8')
assert 'def _chain_row(' in _v11 and 'def _arrow(' in _v11 and 'T.RoundBox(parent' in _v11, '1.0.11: rounded one-row chain'
assert '("오피격", "같이 맞음")' in _v11, '1.0.11: overcall wording'
assert 'st["b31_btn"] = None' in _v11 and '_round_button(parent, text="B31' not in _v11, '1.0.11: B31 detail button removed'
assert 'st.get("five_active")' in _v11, '1.0.11: one details toggle'
assert '_END_ONLY' in (ROOT/'live/v544_glance.py').read_text(encoding='utf-8'), '1.0.11: one end chip'
assert '("1.0.11", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_11_glance_layout.py').is_file()
print('1.0.11 GATE PASS — glance rounded one-row chain · one toggle')

# 1.0.12: GOLD V5.4.6 어그로 없는 주문(정신의 위안 등)은 풀러 근거가 아님
assert 'def _non_aggro_spell(' in (ROOT/'live/raid_facts.py').read_text(encoding='utf-8'), '1.0.12: non-aggro spells are not pull evidence'
assert '("1.0.12", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_12_non_aggro_pull.py').is_file()
print('1.0.12 GATE PASS — non-aggro spells are not pull evidence')

# 1.0.13: GOLD V5.4.7 놓친 트라이 채우기 (감시 시작 때 저장 안 된 풀 채움 · Try 번호 시간순)
_pi13 = (ROOT/'live/public_import.py').read_text(encoding='utf-8')
assert 'def renumber_tries(' in _pi13 and 'def missing_pull_count(' in _pi13 and 'only_if_missing' in _pi13, '1.0.13: backfill import'
assert '_backfill_missed_start("live-start")' in gui and '_active_backfill' in gui, '1.0.13: backfill on live start'
assert 'def _backfill_missed_progress(' in gui and '_all_logs_newest_first()' in gui and '놓친 트라이 없음' in gui, '1.0.13: backfill progress · all logs of the day'
assert '("1.0.13", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_13_backfill_missed.py').is_file()
assert 'def renumber_recent_sessions(' in _pi13 and '_renumber_after_live_pull()' in gui, '1.0.13: numbering stays chronological'
assert 'rage_after' in (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8'), '1.0.13: rage after collapse is not the first mistake'
assert (ROOT/'qa/test_public_1_0_13_numbering_and_glance.py').is_file()
print('1.0.13 GATE PASS — backfill missed pulls')

# 1.0.14: GOLD V5.4.10 무너진 순서 칩 시각 순서
assert 'def _in_time_order(' in (ROOT/'live/v544_glance.py').read_text(encoding='utf-8'), '1.0.14: chain chips in time order'
assert '("1.0.14", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_14_chain_order.py').is_file()
print('1.0.14 GATE PASS — chain chips in time order')

# 1.0.15: GOLD V5.4.11 큰 알 → 공포의 포효 연쇄 · 붕괴 뒤 절규 · 근접 공격 출처 · 큰 알 NSRT 알림 줄 · 마지막 트라이 따라가기
_u = (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8')
assert 'DEVOURING_ROAR = 1305775' in _u and 'def big_egg_timeline_line(' in _u and 'def _melee_source(' in _u, '1.0.15: ulatek roar/melee/egg line'
assert 'self._select_live_row_by_key(completed_key)' in gui, '1.0.15: follow latest try'
assert '("1.0.15", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_15_big_egg_roar_and_follow.py').is_file()
print('1.0.15 GATE PASS — big egg roar chain · follow latest try')

# 1.0.16: GOLD V5.2.6~V5.4.12 WoW 달력 → 구글 캘린더 (기본 꺼짐 · 이 앱이 만든 캘린더만 · OAuth 클라이언트는 빌드 때 Secret)
_wc=(ROOT/'live/wow_calendar_sync.py').read_text(encoding='utf-8')
assert 'SCOPE = "https://www.googleapis.com/auth/calendar.app.created"' in _wc, '1.0.16: app-created calendars only'
assert '"enabled": False' in _wc and 'BUNDLED_CLIENT' in _wc and 'def mark_expired(' in _wc, '1.0.16: off by default · bundled client · expiry'
assert 'S.data_dir(public_home())' in (ROOT/'live/wow_calendar_ui.py').read_text(encoding='utf-8'), '1.0.16: per-user data folder'
assert not (ROOT/'live/wow_calendar_client.json').exists() or 'live/wow_calendar_client.json' in (ROOT/'.gitignore').read_text(encoding='utf-8')
assert 'live/wow_calendar_client.json' in (ROOT/'.gitignore').read_text(encoding='utf-8'), '1.0.16: client json never committed'
assert 'NINI_GOOGLE_CLIENT_JSON' in (ROOT/'.github/workflows/build-release.yml').read_text(encoding='utf-8')
assert "'--hidden-import=live.wow_calendar_ui'" in (ROOT/'build_public_windows.py').read_text(encoding='utf-8')
assert '_wcal_ui.install_auto(self, lambda: self._public_log_dir)' in gui, '1.0.16: sidebar + auto sync'
assert '("1.0.16", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_16_wow_calendar.py').is_file()
print('1.0.16 GATE PASS — WoW calendar to Google (opt-in)')

# 1.0.17: GOLD V5.4.13 울라텍 큰 알 공대 공략 = 4:18 집기 · 4:30쯤 가마솥 (4:31 넘으면 늦음)
_u17=(ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8')
assert 'EGG_DEFECT_LATE_SEC = 271' in _u17 and 'BIG_EGG_HATCH_TO_PICK_ALERT = 84.0' in _u17, '1.0.17: big egg 4:18 / 4:30'
assert '("1.0.17", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_17_big_egg_plan.py').is_file()
print('1.0.17 GATE PASS — big egg plan 4:18 / 4:30')

# 1.0.18: 울라텍 알 깨짐(부패한 막) → 살무사 원인
_u18 = (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8')
assert 'EGG_MEMBRANE = 1301268' in _u18 and '"egg_ground"' in _u18, '1.0.18: egg break membrane'
assert '("1.0.18", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_18_egg_break.py').is_file()
print('1.0.18 GATE PASS — egg break membrane')

# 1.0.19: 공대 계획 (공대별 택틱) · 울라텍 남쪽 구역
_u19 = (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8')
assert 'VERSION = "ULATEK-CONCLUSION-V2.4"' in _u19 and 'def south_review(' in _u19 and 'RP.resolve(' in _u19, '1.0.19: south zone + raid plan'
assert (ROOT/'live/raid_plans.py').is_file() and (ROOT/'live/raid_plans_ui.py').is_file()
assert not (ROOT/'live/raid_plans.json').exists(), '1.0.19: no personal raid plan file in BLUE'
assert 'raid_plans_ui' in (ROOT/'nini_wcl_gui.py').read_text(encoding='utf-8')
assert 'live.raid_plans_ui' in (ROOT/'build_public_windows.py').read_text(encoding='utf-8')
assert '("1.0.19", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_19_raid_plans.py').is_file()
print('1.0.19 GATE PASS — raid plans + south zone')

# 1.0.20: 구글 캘린더 토큰 DPAPI 암호화 XML
_c20 = (ROOT/'live/wow_calendar_sync.py').read_text(encoding='utf-8')
assert 'def save_secret_json(' in _c20 and 'load_secret_json(self.ddir, "google_token"' in _c20, '1.0.20: dpapi token'
assert 'def write_verified(' in (ROOT/'live/dpapi_secret.py').read_text(encoding='utf-8')
assert 'live.dpapi_secret' in (ROOT/'build_public_windows.py').read_text(encoding='utf-8')
assert '("1.0.20", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_20_dpapi.py').is_file()
print('1.0.20 GATE PASS — dpapi secrets')

# 1.0.21: 울라텍 썩은 노른자 남은 채로 작은 알 집음 · 맹독성 분출 뒤 사망
_u21 = (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8')
assert 'YOLK = 1312150' in _u21 and 'def yolk_pick_review(' in _u21 and 'def egg_burst_deaths(' in _u21, '1.0.21: yolk/burst'
assert 'extra.append("노른자 " + yolk_pick_line(yp))' in _u21
assert '("1.0.21", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_21_yolk_burst.py').is_file()
print('1.0.21 GATE PASS — ulatek yolk + egg burst')

# 1.0.22: 울라텍 똥물(죽음의 맹독) · 붕괴 직전 사망 순서 · 독성 배양 탱
_u22 = (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8')
assert 'VENOM_LAKE = 1297338' in _u22 and 'def lake_review(' in _u22 and 'def pre_collapse_txt(' in _u22, '1.0.22: lake'
assert '"code": "VENOM_LAKE"' in _u22 and '"탱: 독성 배양' in _u22
assert 'def viper_dmg_share(' in _u22 and 'VIPER_HEART1_LAKE' in _u22, '1.0.22: viper heart lake'
assert '("1.0.22", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_22_venom_lake.py').is_file()
print('1.0.22 GATE PASS — ulatek venom lake + pre-collapse deaths')

# 1.0.23: 울라텍 송곳니 줄 늦게 끊음 (역병의 핏줄 중첩 연장)
_u23 = (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8')
assert 'VEIN_TICK = 1317955' in _u23 and 'FANG_LATE_SEC' in _u23 and 'def _vein_ticks(' in _u23, '1.0.23: fang late'
assert '"code": "FANG_LATE"' in _u23 and '"fang_late"' in _u23
assert '"code": "TANK_DEATH"' in _u23 and 'TANK_NEXT_BY_SOURCE' in _u23 and '"doomscale_tank"' in _u23, '1.0.23: tank death'
assert '("1.0.23", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_23_fang_late.py').is_file()
print('1.0.23 GATE PASS — ulatek fang late break + tank death')

# 1.0.24: 울라텍 살무사 원인 순서 · 알 든 채 사망 · 똥물 절반 사망 · 킬 분노 · 똬리 역병의 절단
_u24 = (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8')
assert 'VIPER_PRE_TOL' in _u24 and '"diedHolding"' in _u24 and 'LAKE_DEAD_BEFORE_SHARE' in _u24 and 'RAGE_ROUTINE' in _u24, '1.0.24: ulatek'
assert '"egg_died"' in _u24
_c24 = (ROOT/'live/coiled_wipe_cause_v1.py').read_text(encoding='utf-8')
assert 'PLAGUE_CLEAVE = "역병의 절단"' in _c24 and 'TANK_SPECS' in _c24, '1.0.24: coiled cleave'
assert '("1.0.24", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_24_review_1126.py').is_file()
print('1.0.24 GATE PASS — ulatek viper order + coiled plague cleave')

# 1.0.25: 알 든 채 사망 조언 (알 운반자 힐·생존기 우선 까지만)
_u25 = (ROOT/'live/ulatek_conclusion_v1.py').read_text(encoding='utf-8')
assert '알 운반자 힐·생존기 우선."' in _u25 and '알 넘기기' not in _u25 and '알 들지 않기' not in _u25, '1.0.25: egg advice'
assert '("1.0.25", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_25_egg_advice.py').is_file()
assert '무적·회피로 받음(인원 포함)' in (ROOT/'live/coiled_wipe_cause_v1.py').read_text(encoding='utf-8'), '1.0.25: soak count'
assert 'def wail_root(' in (ROOT/'live/coiled_wipe_cause_v1.py').read_text(encoding='utf-8') and '"WAIL_UNKICKED"' in (ROOT/'live/coiled_wipe_cause_v1.py').read_text(encoding='utf-8'), '1.0.25: wail'
assert (ROOT/'live/day_summary_v1.py').is_file() and (ROOT/'live/day_board.py').is_file(), '1.0.25: day summary'
assert '_day_board.build_panel(self, body, row=2)' in gui and '_day_board.apply(self, enc, pairs, session_date, model)' in gui, '1.0.25: day board wiring'
assert '멈춘 거 아님' in (ROOT/'live/day_board.py').read_text(encoding='utf-8'), '1.0.25: not-frozen note'
assert (ROOT/'qa/test_public_1_0_25_day_summary.py').is_file()
print('1.0.25 GATE PASS — egg advice + guillotine soak count + wail + day summary')
# 1.0.26: 1.0.25 다시 배포 (그날 정리 윈도우 경로)
assert 'return str(Path(d)) if d else ""' in (ROOT/'live/day_board.py').read_text(encoding='utf-8'), '1.0.26: day path'
assert '("1.0.26", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_26_day_paths.py').is_file() and (ROOT/'README_1_0_26.md').is_file()
print('1.0.26 GATE PASS — day summary windows path')
# 1.0.27: 칸 제목 「최종 정리」
assert '"그날 최종 정리' not in (ROOT/'live/day_board.py').read_text(encoding='utf-8'), '1.0.27: title'
assert '("1.0.27", [' in (ROOT/'live/public_onboarding.py').read_text(encoding='utf-8')
assert (ROOT/'qa/test_public_1_0_27_title.py').is_file() and (ROOT/'README_1_0_27.md').is_file()
assert 'voice = RoundPanel(body, "보완사항", ""' in gui and 'model.get("voiceOrdersFull")' in gui, '1.0.27: todo panel'
assert 'model["voiceOrdersFull"] = True' in (ROOT/'live/day_board.py').read_text(encoding='utf-8'), '1.0.27: todo full'
print('1.0.27 GATE PASS — final summary title + todo panel')
