from __future__ import annotations

"""Public-only user data and WoW CombatLog discovery (no personal paths)."""

import json
import os
import sys
from pathlib import Path

from .io_utils import atomic_write_json


def public_home(environ=None, platform=None) -> Path:
    env = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    custom = env.get('NINI_PUBLIC_HOME')
    if custom:
        return Path(custom).expanduser()
    if platform == 'win32':
        base = env.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
    else:
        base = env.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share')
    return Path(base).expanduser() / 'NiniAnalyzer' / 'Public'


def public_sessions_root(home=None) -> Path:
    return Path(home) / 'data' / 'live_sessions' if home is not None else public_home() / 'data' / 'live_sessions'


def read_public_settings(home=None) -> dict:
    path = (Path(home) if home is not None else public_home()) / 'settings.json'
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def remember_log_dir(folder: Path | str, home=None) -> Path:
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir() or folder.name.casefold() != 'logs':
        raise ValueError('WoW의 Logs 폴더를 선택해 주세요. (_retail_\\Logs)')
    root = Path(home) if home is not None else public_home()
    settings = read_public_settings(root)
    settings['lastLogDir'] = str(folder)
    atomic_write_json(root / 'settings.json', settings)
    return folder


def _wow_registry_candidates():
    """Best-effort: Battle.net launch options and WoW uninstall records.

    Registry access is optional, never the only way to locate a log folder.
    """
    if os.name != 'nt':
        return []
    try:
        import winreg
    except ImportError:
        return []
    locations = [
        (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Blizzard Entertainment\Battle.net\Launch Options\wow'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\World of Warcraft'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\World of Warcraft'),
    ]
    results = []
    for hive, key_name in locations:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for field in ('INSTALLPATH', 'InstallLocation', 'Path'):
                    try:
                        value, _ = winreg.QueryValueEx(key, field)
                    except OSError:
                        continue
                    if isinstance(value, str) and value.strip():
                        results.append(Path(os.path.expandvars(value.strip('" '))))
        except OSError:
            continue
    return results


def candidate_log_dirs(settings=None, environ=None, registry_paths=None):
    env = os.environ if environ is None else environ
    settings = read_public_settings() if settings is None else settings
    base_paths = []
    if env.get('WOW_LOG_DIR'):
        base_paths.append(Path(env['WOW_LOG_DIR']))
    if settings.get('lastLogDir'):
        base_paths.append(Path(settings['lastLogDir']))
    base_paths += list(_wow_registry_candidates() if registry_paths is None else registry_paths)
    for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)'):
        if env.get(key):
            base_paths.append(Path(env[key]) / 'World of Warcraft')
    # Bounded common paths. Do not prefer a private E: installation or display
    # such a path when nothing was found.
    for drive in ('C', 'D', 'E', 'F') if os.name == 'nt' else ():
        base_paths.append(Path(f'{drive}:\\World of Warcraft'))
    found, seen = [], set()
    for candidate in base_paths:
        # Only a genuine Logs directory can be used for tailing. A WoW
        # installation directory exists before combat logging is enabled;
        # watching that parent silently misses every later log creation.
        if candidate.name.casefold() == 'logs':
            variants = (candidate,)
        elif candidate.name.casefold() == '_retail_':
            variants = (candidate / 'Logs',)
        else:
            variants = (candidate / '_retail_' / 'Logs', candidate / 'Logs')
        for path in variants:
            marker = str(path).casefold()
            if marker not in seen:
                seen.add(marker)
                if path.is_dir():
                    found.append(path)
    return found


def discover_log_dir(settings=None, environ=None, registry_paths=None) -> Path | None:
    candidates = candidate_log_dirs(settings=settings, environ=environ, registry_paths=registry_paths)
    for path in candidates:
        if any(path.glob('WoWCombatLog-*.txt')):
            return path
    return candidates[0] if candidates else None


def latest_log_file(folder) -> Path | None:
    if folder is None or not Path(folder).is_dir():
        return None
    paths = Path(folder).glob('WoWCombatLog-*.txt')
    return max(paths, key=lambda p: p.stat().st_mtime_ns, default=None)
