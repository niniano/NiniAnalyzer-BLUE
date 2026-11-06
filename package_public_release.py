"""Seal actual Windows onedir folder as a safe-to-extract RC3 release."""
from __future__ import annotations
import hashlib
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent
FOLDER=ROOT/'dist'/'NiniAnalyzer_Public'
VERSION='1.0.0'
ZIP=ROOT/'dist'/f'NiniAnalyzer_Public_{VERSION}_Windows_x64_SAFE_FOLDER.zip'
SHA=ZIP.with_suffix(ZIP.suffix+'.sha256.txt')
RELEASE_GUIDE=ROOT/'START_HERE_PUBLIC.txt'
ALLOWED_ROOT={'NiniAnalyzer_Public.exe','_internal','START_HERE_PUBLIC.txt'}
BLOCKED_DIRS={'brand_personal','live_sessions','archive_outbox','niniwowsync','icon_cache','__pycache__','tests','qa'}
BLOCKED_FILES={'.env'}

def validate(folder:Path)->list[Path]:
    folder=Path(folder)
    if not (folder/'NiniAnalyzer_Public.exe').is_file():
        raise FileNotFoundError('Windows EXE missing; run Build_Public_EXE.cmd')
    internal=folder/'_internal'
    if not internal.is_dir() or internal.is_symlink():
        raise ValueError('RC3 requires an actual _internal directory')
    version=internal/'NiniAnalyzer_Public.version'
    if not version.is_file() or version.read_text(encoding='ascii').strip()!=VERSION:
        raise ValueError('This is not a genuine 1.0.0 build; rebuild instead of repackaging an older RC')
    if not (internal/'resources'/'brand_public'/'nini_analyzer.ico').is_file():
        raise FileNotFoundError('Bundled BLUE icon missing under _internal/resources')
    if not (internal/'live'/'boss_mechanics'/'rules').is_dir():
        raise FileNotFoundError('Bundled boss mechanic rules missing under _internal/live')
    for font in ('PyeojinGothic-Medium.ttf','Paperlogy-6SemiBold.ttf',
                 'LICENSE_PyeojinGothic_OFL.txt','LICENSE_Paperlogy_OFL.txt'):
        if not (internal/'assets'/'fonts'/font).is_file():
            raise FileNotFoundError(f'Bundled font/licence missing under _internal/assets/fonts: {font}')
    if not (internal/'data'/'skill_icons').is_dir():
        raise FileNotFoundError('Bundled skill icons missing under _internal/data')
    for child in folder.iterdir():
        if child.name not in ALLOWED_ROOT:
            raise ValueError(f'Unexpected release top-level item: {child.name}')
    files=sorted((p for p in folder.rglob('*') if p.is_file()),key=lambda p:p.as_posix())
    for p in folder.rglob('*'):
        if p.is_symlink():
            raise ValueError(f'Symlink not permitted in release: {p}')
    for p in files:
        rel=p.relative_to(folder)
        parts={part.casefold() for part in rel.parts}
        if parts & BLOCKED_DIRS or p.name.casefold() in BLOCKED_FILES or p.suffix.casefold() in {'.log','.wcla','.pyc'} or p.name.casefold().endswith('.wcla.zip'):
            raise ValueError(f'Private, source or runtime file found: {rel}')
        if len(rel.parts)>1 and rel.parts[0]!='_internal':
            raise ValueError(f'Bundled resources must live inside _internal: {rel}')
    return files

def package(folder:Path=FOLDER,destination:Path=ZIP,guide:Path=RELEASE_GUIDE):
    folder,destination,guide=Path(folder),Path(destination),Path(guide)
    if not guide.is_file(): raise FileNotFoundError(f'Welcome guide missing: {guide}')
    (folder/'START_HERE_PUBLIC.txt').write_bytes(guide.read_bytes())
    files=validate(folder)
    destination.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in files:
            z.write(p,arcname='NiniAnalyzer_Public/'+p.relative_to(folder).as_posix())
    with zipfile.ZipFile(destination) as z:
        bad=z.testzip()
        if bad: raise ValueError(f'Bad ZIP entry: {bad}')
        root={'/'.join(p.split('/')[:2]) for p in z.namelist()}
        expected={'NiniAnalyzer_Public/'+name for name in ALLOWED_ROOT}
        if root!=expected: raise ValueError(f'Unexpected ZIP layout: {root}')
    digest=hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(destination.suffix+'.sha256.txt').write_text(f'{digest} *{destination.name}\n',encoding='ascii')
    print(f'RELEASE ZIP: {destination}')
    print(f'FILES: {len(files)}')
    print(f'SHA256: {digest}')
    return destination

if __name__=='__main__': package()
