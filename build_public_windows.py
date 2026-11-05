"""Windows-only standalone PUBLIC onedir build; GOLD is never modified."""
from __future__ import annotations
import os, sys, subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parent
VERSION = "1.0.0-rc5.5"

def build_command(root=ROOT, python=None, data_sep=None):
    root=Path(root)
    python=sys.executable if python is None else python
    data_sep=os.pathsep if data_sep is None else data_sep
    data=[('resources/brand_public','resources/brand_public'),
          ('data','data'),('live/boss_mechanics/rules','live/boss_mechanics/rules'),
          ('live/boss_mechanics/default_strategies.json','live/boss_mechanics'),
          ('live/raid_rules','live/raid_rules'),
          ('live/raid_series_settings.json','live'),
          # RC4: bundled OFL fonts (process-private load) + licence notices
          ('assets/fonts','assets/fonts'),
          # RC5.3: 똬리 공대 차단 규칙 (v52_screen.kick_rules 가 _internal 에서 읽음)
          ('kick_rules_3429.json','.')]
    # PyInstaller onedir: exe is visible, bundled libs/rules/icons live in _internal.
    args=[python, '-m', 'PyInstaller', '--noconfirm', '--clean',
          '--onedir', '--windowed', '--noupx',
          '--contents-directory', '_internal',
          '--name', 'NiniAnalyzer_Public',
          '--icon', str(root/'resources/brand_public/nini_analyzer.ico'),
          '--distpath', str(root/'dist'), '--workpath', str(root/'build'),
          '--specpath', str(root),
          # PUBLIC source does not import numpy; prevent optional hooks bloating ZIP.
          # If runtime regression reveals a transitive requirement, remove exclusion.
          '--exclude-module=numpy']
    for source,target in data:
        path=root/source
        if not path.exists(): raise FileNotFoundError(path)
        args.extend(['--add-data', f'{path}{data_sep}{target}'])
    args += ['--hidden-import=public_errorlog','--hidden-import=public_first_run',
             '--hidden-import=live.public_import','--hidden-import=live.reanalysis',
             '--hidden-import=live.raid_review','--hidden-import=live.boss_mechanics.engine',
             '--hidden-import=live.skill_tips','--hidden-import=live.timeline_skill_tooltip',
             '--hidden-import=live.feign_recheck','--hidden-import=live.public_feign_ui','--hidden-import=live.class_colors',
             # RC5: GOLD V4.7 화면 (결론 5줄·단계 칩·스킬별 시간축·보스 공략 판정). v43_layout loads boss modules by name.
             '--hidden-import=live.v43_layout','--hidden-import=live.v45_timeline','--hidden-import=live.boss_guides_v1',
             '--hidden-import=live.coiled_wipe_cause_v1','--hidden-import=live.ulatek_conclusion_v1',
             '--hidden-import=live.raid_personal_context_v35',
             '--hidden-import=live.raid_chronology_v37','--hidden-import=live.raid_chronology_glance_v42',
             '--hidden-import=live.raid_collapse_display_v38','--hidden-import=live.raid_readable_korean_v39',
             '--hidden-import=live.raid_glance_v40',
             # RC5.3: GOLD V5.2 트라이 화면 (링크 · P2 보호막 칸 · 암영폭탄/현신 접촉 카드)
             '--hidden-import=live.v52_screen',str(root/'nini_raid_gui.py')]
    return args

def main():
    if os.name != 'nt':
        raise SystemExit('Windows EXE build must run on Windows')
    command=build_command()
    subprocess.run(command, cwd=ROOT, check=True)
    folder=ROOT/'dist'/'NiniAnalyzer_Public'
    exe=folder/'NiniAnalyzer_Public.exe'
    internal=folder/'_internal'
    if not exe.is_file() or not internal.is_dir():
        raise RuntimeError(f'Expected onedir EXE and _internal folder: {folder}')
    for font in ('PyeojinGothic-Medium.ttf','Paperlogy-6SemiBold.ttf'):
        if not (internal/'assets'/'fonts'/font).is_file():
            raise RuntimeError(f'Bundled font missing: {font}')
    if not (internal/'resources'/'brand_public'/'nini_analyzer.ico').is_file():
        raise RuntimeError('BLUE bundled icon missing; _internal asset layout is invalid')
    (internal/'NiniAnalyzer_Public.version').write_text(VERSION+'\n',encoding='ascii')
    print(f'Windows RC5 build OK: {exe}')

if __name__=='__main__': main()
