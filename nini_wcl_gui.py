from __future__ import annotations

import csv
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
import statistics
import zipfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# ---- B38 SPEED1: Tk objects collected on worker threads -------------------
# Garbage collection may run on any thread.  When it finalises a tk.Variable or
# tk.Image there, tkinter's __del__ makes several Tcl calls that must be run on
# the Tk thread, so the worker blocks until the UI loop serves them (measured:
# up to 16 s at startup).  Off the main thread we only queue the cleanup; the UI
# thread performs exactly the same Tcl cleanup from _pump_queue.
import threading as _b38_threading
_B38_TK_CLEANUP: list = []
_b38_orig_var_del = tk.Variable.__del__
_b38_orig_img_del = tk.Image.__del__


def _b38_var_del(self):
    if _b38_threading.current_thread() is _b38_threading.main_thread():
        return _b38_orig_var_del(self)
    tkapp = getattr(self, "_tk", None)
    if tkapp is None:
        return
    _B38_TK_CLEANUP.append(("var", tkapp, getattr(self, "_name", None), list(getattr(self, "_tclCommands", None) or [])))
    self._tk = None
    self._tclCommands = None


def _b38_img_del(self):
    if _b38_threading.current_thread() is _b38_threading.main_thread():
        return _b38_orig_img_del(self)
    name = getattr(self, "name", None)
    tkapp = getattr(self, "tk", None)
    if name and tkapp is not None:
        _B38_TK_CLEANUP.append(("img", tkapp, name, []))
        self.name = None


def _b38_drain_tk_cleanup(limit: int = 500) -> int:
    done = 0
    while _B38_TK_CLEANUP and done < limit:
        kind, tkapp, name, cmds = _B38_TK_CLEANUP.pop(0)
        done += 1
        try:
            if kind == "var":
                if name and tkapp.getboolean(tkapp.call("info", "exists", name)):
                    tkapp.globalunsetvar(name)
                for cmd in cmds:
                    tkapp.deletecommand(cmd)
            else:
                tkapp.call("image", "delete", name)
        except Exception:
            pass  # root destroyed / already gone: nothing left to clean
    return done


tk.Variable.__del__ = _b38_var_del
tk.Image.__del__ = _b38_img_del

# ---- B38 SPEED1: local WCL CSV aggregation cache ----------------------------
_B38_CSV_CACHE: dict = {}


def _b38_csv_cached(path, key, compute):
    """Return compute() result cached per (file path, mtime, size, key)."""
    try:
        st = Path(path).stat()
        ck = (str(path), int(st.st_mtime_ns), int(st.st_size), key)
    except OSError:
        return compute()
    if ck in _B38_CSV_CACHE:
        return _B38_CSV_CACHE[ck]
    value = compute()
    if len(_B38_CSV_CACHE) > 64:
        _B38_CSV_CACHE.clear()
    _B38_CSV_CACHE[ck] = value
    return value

from live.selection_context import (
    boss_context_key as make_boss_context_key,
    context_dict as selection_context_dict,
    context_tuple_from_state,
    live_context_key as make_live_context_key,
    new_selection_state,
    session_date_from_log_path,
    session_date_from_timestamp,
    selection_tokens_match,
)
try:
    from live.historical_precision import find_fights, cache_has_fight, dataset_source_log
except ImportError:  # RC5.3.1: 도적 정밀 분석 모듈은 공개판에 없다 (화면에서도 안 씀)
    def find_fights(*_a, **_k):
        return []

    def cache_has_fight(*_a, **_k):
        return False

    def dataset_source_log(*_a, **_k):
        return None
from live.versions import APP_BUILD, ENGINE_VERSION, MODULE_VERSION
from live.layout_models import build_series_layout_model, build_try_layout_model, render_try_summary, row_json
from live.b3641_ui import (
    build_generic_board_summary, build_simple_try_summary,
    build_briefing_model, build_try_priority_model, build_p2_relative_compare,
    build_shield_throughput_compare,
    canonicalize_rows, same_physical_pull, normalize_difficulty,
    fmt_clock as b3641_fmt_clock, load_mechanic_review, short_player as b3641_short_player,
)
from live.encounter_tracker import parse_combat_datetime
from live.public_paths import (public_home, public_sessions_root, read_public_settings,
                               remember_log_dir, discover_log_dir, latest_log_file)
from live.public_migration import migrate_legacy_public_sessions, cleanup_stale_active, MARKER
from live.player_identity import IDENTITY_SOURCE
from live.reset_policy import is_reset_pull, is_early_wipe
from live.boss_registry import boss_name as registry_boss_name
from live.session_summary import build_session_summary
from live.difficulty_scope import is_raid_row as is_raid_scope_row, display_difficulty_name
from live.timeline_glyphs import build_timeline_glyph_model, place_glyph_lanes
try:
    from live.timeline_skill_tooltip import build_skill_tooltip_view, place_tooltip_within_owner, format_tk_position
    from live.timeline_skill_tooltip import build_reference_tooltip_view, build_event_tooltip_view
except Exception:  # optional during partial patch application
    build_skill_tooltip_view = None
    build_reference_tooltip_view = None
    build_event_tooltip_view = None
    place_tooltip_within_owner = None
    format_tk_position = None
try:
    from live.boss_mechanics import supports_encounter as supports_boss_mechanic_encounter
except Exception:  # pragma: no cover
    def supports_boss_mechanic_encounter(_row):
        return False

try:
    from live.raid_review import LiveRaidReviewMonitor
except Exception:
    LiveRaidReviewMonitor = None

try:
    from engine.plan_vs_actual import compare_plan_vs_actual, COMBAT_POTION_SLOT_ID, COMBAT_POTION_SLOT_NAME
except Exception:
    compare_plan_vs_actual = None
    COMBAT_POTION_SLOT_ID = -900001
    COMBAT_POTION_SLOT_NAME = "전투 물약"

WORKDIR = Path(__file__).resolve().parent


def _startup_monitor_work_area():
    """Return the Windows work area for the monitor under the mouse cursor.

    Tk's winfo_screenwidth()/height() report the primary desktop on Windows,
    which is wrong when the analyzer is launched on a secondary portrait
    monitor.  Query User32 directly so the initial layout is based on the
    monitor the user is actually interacting with.  On non-Windows platforms
    or if the API call fails, return None and let Tk provide the fallback.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG),
            ]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        pt = POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return None
        # MONITOR_DEFAULTTONEAREST = 2
        monitor = user32.MonitorFromPoint(pt, 2)
        if not monitor:
            return None
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        left, top = int(info.rcWork.left), int(info.rcWork.top)
        right, bottom = int(info.rcWork.right), int(info.rcWork.bottom)
        width, height = right - left, bottom - top
        if width < 500 or height < 500:
            return None
        return {"x": left, "y": top, "width": width, "height": height}
    except Exception:
        return None


def _is_raid_navigation_row(row: dict) -> bool:
    """Keep default navigation raid-only while preserving source rows on disk."""
    return is_raid_scope_row(row)


def _ui_difficulty_name(row: dict | None, fallback: str = "") -> str:
    """Presentation-only difficulty label normalizer."""
    return display_difficulty_name(row or {}, fallback=fallback)

def _detect_logdir():
    env = os.getenv("WOW_LOG_DIR")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates += [
        Path(r"E:\World of Warcraft\_retail_\Logs"),
        Path(r"C:\Program Files (x86)\World of Warcraft\_retail_\Logs"),
        Path(r"C:\Program Files\World of Warcraft\_retail_\Logs"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0] if candidates else WORKDIR

LOGDIR = _detect_logdir()

_python_exe = Path(sys.executable)
if _python_exe.name.lower() == "pythonw.exe":
    _console_python = _python_exe.with_name("python.exe")
    PYTHON = _console_python if _console_python.exists() else _python_exe
else:
    PYTHON = _python_exe


# PACKAGER-ONLY bridge: frozen GUI delegates Python engine scripts to bundled worker.
if (WORKDIR / 'NiniAnalyzer_GOLD_Worker.exe').is_file():
    PYTHON = WORKDIR / 'NiniAnalyzer_GOLD_Worker.exe'

PIPELINE = WORKDIR / "nini_wcl_pipeline.py"
ENGINE = WORKDIR / "engine"
CHARACTERS = ()  # PUBLIC: private character registry disabled
DEFAULT_PLAYER = ""  # PUBLIC: no default personal identity
PUBLIC_APP_VERSION = "1.0.6"
WCL_DIFFICULTY_MAP = {15: 4, 16: 5, 233: 5}

STATUS_RANK = {"경고": 4, "주의": 3, "표본부족": 2, "정상": 1, "비교전용": 0}
STATUS_ICON = {"경고": "⚠", "주의": "△", "표본부족": "·", "정상": "✓", "비교전용": "·"}


LOG_INDEX_PATH = WORKDIR / "data" / "log_character_index.json"


def _read_simple_env(path: Path) -> dict[str, str]:
    values = {}
    try:
        if not path.exists():
            return values
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
                value = value[1:-1]
            if key:
                values[key] = value
    except Exception:
        return {}
    return values


def wcl_credentials_available() -> bool:
    root_env = _read_simple_env(WORKDIR / ".env")
    engine_env = _read_simple_env(ENGINE / ".env")
    def resolve(key: str):
        return root_env.get(key) or engine_env.get(key) or os.getenv(key)
    return bool(resolve("WCL_CLIENT_ID") and resolve("WCL_CLIENT_SECRET"))


def find_compatible_precision_cache(roots, encounter: int, difficulty: int) -> Path | None:
    pattern = f"{int(encounter)}_*_d{int(difficulty)}_top50_nonCNTW_v22phase.zip"
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        hits = []
        for candidate in root.glob(pattern):
            try:
                if candidate.is_file() and zipfile.is_zipfile(candidate):
                    hits.append(candidate)
            except OSError:
                continue
        if hits:
            hits.sort(key=lambda x: x.stat().st_mtime_ns if x.exists() else 0, reverse=True)
            return hits[0]
    return None


def _all_logs_newest_first() -> list[Path]:
    if not LOGDIR.exists():
        return []
    logs = list(LOGDIR.glob("WoWCombatLog-*.txt"))
    def key(p: Path):
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0
    return sorted(logs, key=key, reverse=True)


def newest_log() -> Path | None:
    logs = _all_logs_newest_first()
    return logs[0] if logs else None


def _load_log_index() -> dict:
    try:
        if LOG_INDEX_PATH.exists():
            data = json.loads(LOG_INDEX_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_log_index(data: dict):
    try:
        LOG_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_INDEX_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _log_fingerprint(log_path: Path):
    try:
        st = log_path.stat()
        return int(st.st_size), int(st.st_mtime_ns)
    except OSError:
        return None, None


def _known_players_from_analysis(log_path: Path) -> dict[str, bool] | None:
    """기존 scan/dataset 결과가 있으면 대용량 원본을 다시 읽지 않는다."""
    suffix = log_path.stem.replace("WoWCombatLog-", "", 1)
    scan_summary = WORKDIR / "data" / f"combatlog_scan_{suffix}" / "combatlog_summary.json"
    dataset_summary = WORKDIR / "data" / f"combatlog_dataset_{suffix}" / "dataset_summary.json"

    # dataset_summary가 가장 정확: 실제 player-related event 기준.
    if dataset_summary.exists():
        try:
            data = json.loads(dataset_summary.read_text(encoding="utf-8-sig"))
            pec = data.get("playerEventCounts") or {}
            result = {}
            for p in CHARACTERS:
                counts = pec.get(p) or {}
                if isinstance(counts, dict):
                    total = sum(int(v or 0) for v in counts.values())
                else:
                    total = int(counts or 0)
                result[p] = total > 0
            return result
        except Exception:
            pass

    # scan summary는 이름 언급 기준 fallback.
    if scan_summary.exists():
        try:
            data = json.loads(scan_summary.read_text(encoding="utf-8-sig"))
            mentions = data.get("playerMentions") or {}
            if any(p in mentions for p in CHARACTERS):
                return {p: int(mentions.get(p) or 0) > 0 for p in CHARACTERS}
        except Exception:
            pass

    return None


def _scan_log_players(log_path: Path) -> dict[str, bool]:
    """로그를 바이너리 스트리밍해서 3캐릭 이름을 한 번에 찾는다.

    CombatLog source/dest name은 따옴표로 시작하므로 '"이름' 패턴을 사용해
    일반 문자열 오탐을 줄인다. 모든 캐릭터가 발견되면 즉시 중단한다.
    """
    found = {p: False for p in CHARACTERS}
    needles = {p: ('"' + p).encode("utf-8") for p in CHARACTERS}
    overlap = max(len(v) for v in needles.values()) + 8
    tail = b""

    try:
        with log_path.open("rb") as f:
            while True:
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    break
                buf = tail + chunk
                for p, needle in needles.items():
                    if not found[p] and needle in buf:
                        found[p] = True
                if all(found.values()):
                    break
                tail = buf[-overlap:]
    except OSError:
        pass
    return found


def players_in_log(log_path: Path, force_rescan: bool = False) -> dict[str, bool]:
    """파일 fingerprint 기반 캐시 + 기존 분석 결과 + 실제 로그 스캔."""
    size, mtime_ns = _log_fingerprint(log_path)
    key = str(log_path.resolve()).lower()
    idx = _load_log_index()
    rec = idx.get(key) if isinstance(idx.get(key), dict) else None

    if (
        not force_rescan
        and rec
        and rec.get("size") == size
        and rec.get("mtimeNs") == mtime_ns
        and isinstance(rec.get("players"), dict)
    ):
        return {p: bool(rec["players"].get(p, False)) for p in CHARACTERS}

    known = _known_players_from_analysis(log_path)
    detected = known if known is not None else _scan_log_players(log_path)

    idx[key] = {
        "path": str(log_path),
        "size": size,
        "mtimeNs": mtime_ns,
        "players": detected,
    }
    _save_log_index(idx)
    return detected


def find_latest_log_for_player(player: str, force_rescan: bool = False) -> Path | None:
    """해당 캐릭터가 실제 포함된 가장 최신 WoWCombatLog."""
    if player not in CHARACTERS:
        return newest_log()

    for log_path in _all_logs_newest_first():
        detected = players_in_log(log_path, force_rescan=force_rescan)
        if detected.get(player):
            return log_path
    return None


def _summary_has_raid_results(summary: Path | None) -> bool:
    """통합 요약에 실제 영웅/신화 레이드 분석 대상이 하나라도 있는지 확인한다."""
    if not summary or not summary.exists():
        return False
    try:
        text = summary.read_text(encoding="utf-8", errors="replace")[:12000]
    except Exception:
        return False
    m = re.search(r"레이드 분석 대상:\s*(\d+)\s*Fight", text)
    return bool(m and int(m.group(1)) > 0)


def find_latest_analyzed_log_for_player(player: str) -> Path | None:
    """빈/추종자/던전 최신 로그 대신 가장 최근의 실제 레이드 분석 결과를 우선 표시한다."""
    any_summary = None
    for log_path in _all_logs_newest_first():
        dataset = dataset_from_log(log_path)
        summary = dataset / "characters" / player / "integrated_analysis" / "integrated_summary.txt"
        if not summary.exists():
            continue
        if any_summary is None:
            any_summary = log_path
        if _summary_has_raid_results(summary):
            return log_path
    return any_summary


def dataset_from_log(log_path: Path) -> Path:
    """캐릭터와 무관한 통로그 공유 데이터셋."""
    stem = log_path.stem
    suffix = stem.replace("WoWCombatLog-", "", 1)
    return WORKDIR / "data" / f"combatlog_dataset_{suffix}"


def character_root(dataset: Path | None, player: str) -> Path | None:
    if not dataset:
        return None
    return dataset / "characters" / player


def parse_count_detail(detail: str):
    import re
    m = re.search(
        r"로컬 중앙\s*([0-9.]+)회\s*/\s*비교군 중앙\s*([0-9.]+)회",
        detail or "",
    )
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def parse_seconds_detail(detail: str):
    import re
    m = re.search(
        r"로컬 중앙\s*([0-9.]+)s\s*/\s*비교군 중앙\s*([0-9.]+)s",
        detail or "",
    )
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def session_confirm_indices(boss_data: list[dict]) -> set[int]:
    """잠금 판정을 바꾸지 않고, 세션 요약에서 확인할 보스만 골라낸다."""
    confirm = set()

    # 첫 60초 어춤 회수: 로컬이 비교군 중앙보다 낮은 보스
    for idx, b in enumerate(boss_data):
        for r in b.get("rules", []):
            if r.get("rule") == "첫 60초 어둠의 춤 회수":
                pair = parse_count_detail(r.get("detail", ""))
                if pair and pair[0] < pair[1]:
                    confirm.add(idx)

    # 오프너: 세션 내 큰 상대적 이상치만 표시
    opener = []
    for idx, b in enumerate(boss_data):
        for r in b.get("rules", []):
            if r.get("rule") == "오프너 시작 — 첫 그림자 일격":
                pair = parse_seconds_detail(r.get("detail", ""))
                if pair:
                    local, peer = pair
                    opener.append((idx, local - peer))
    if opener:
        diffs = sorted(x[1] for x in opener)
        n = len(diffs)
        if n % 2:
            med = diffs[n // 2]
        else:
            med = (diffs[n // 2 - 1] + diffs[n // 2]) / 2
        for idx, diff in opener:
            if diff >= 0.75 and diff >= med + 0.75:
                confirm.add(idx)

    return confirm


def display_status_for_boss(b: dict, is_session_confirm: bool = False) -> str:
    rules = b.get("rules", [])
    if not b.get("lockedRuleAnalyzed"):
        return "미분석"
    if not rules:
        return "결과 없음"

    statuses = [r.get("status", "") for r in rules]
    if "경고" in statuses:
        return "경고"
    if "주의" in statuses:
        return "주의"
    if is_session_confirm:
        return "확인"

    has_normal = "정상" in statuses
    has_small = "표본부족" in statuses

    if has_normal and has_small:
        return "정상*"
    if has_normal and all(s in ("정상", "비교전용") for s in statuses):
        return "정상"
    if has_small:
        return "표본부족"
    return "확인"


def parse_percent_detail(detail: str):
    m = re.search(
        r"로컬\s*([0-9.]+)%.*?/.*?비교군.*?([0-9.]+)%",
        detail or "",
    )
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def parse_pair_with_allowed(detail: str):
    m = re.search(
        r"로컬 중앙\s*([0-9.]+)s\s*/\s*비교군 중앙\s*([0-9.]+)s\s*/\s*허용\s*([0-9.]+)s",
        detail or "",
    )
    if not m:
        return None
    return tuple(float(x) for x in m.groups())


def coaching_action(rule: str, detail: str = "", status: str = "") -> str:
    """잠금 판정을 바꾸지 않고, 해당 결과를 다음 풀 행동으로 번역한다."""
    low = rule.lower()

    if "엉겅퀴" in rule or "tea" in low:
        return (
            "다음 풀: 엉겅퀴 차를 누르기 직전 현재 기력을 먼저 보고, "
            "차 사용 직후 실제 기력 오버캡이 생기지 않게 사용 타이밍을 늦추거나 "
            "직전에 기력을 한 번 더 소비하세요. 차 자체를 줄이는 것이 목표는 아닙니다."
        )

    if ("심장" in rule and ("죽표" in rule or "deathmark" in low)):
        return (
            "다음 풀: 심장과 죽음표식을 같은 첫 쿨기 묶음에 두고, "
            "둘 사이의 빈 시간을 줄이세요. 엉겅퀴 차를 기다리느라 죽음표식/왕의 파멸을 "
            "밀지 않는지도 같이 확인하세요."
        )

    if "목조르기" in rule and ("오프너" in rule or "시작" in rule):
        return (
            "다음 풀: 풀카운트 전에 타깃·사거리·은신 상태를 준비하고, "
            "전투 시작과 동시에 첫 목조르기가 들어가게 맞추세요."
        )

    if "첫 그림자 일격" in rule or ("오프너" in rule and "그림자" in rule):
        return (
            "다음 풀: 풀카운트 전에 타깃과 사거리를 잡아두고, "
            "전투가 시작되자마자 첫 그림자 일격이 들어가게 준비하세요. "
            "끝난 뒤 비교군 시작 시간에 가까워졌는지 확인하세요."
        )

    if "첫 60초 어둠의 춤 회수" in rule:
        pair = parse_count_detail(detail)
        if pair:
            local, peer = pair
            gap = max(0.0, peer - local)
            if gap > 0:
                return (
                    f"다음 풀: 첫 60초 동안 어둠의 춤이 {gap:g}회 덜 들어갔습니다. "
                    f"비교군 중앙 {peer:g}회를 기준으로, 어춤을 아끼거나 다음 사용이 밀린 구간이 "
                    "없는지 확인하고 첫 1분 안에 회수를 챙기세요."
                )
        return (
            "다음 풀: 첫 60초 안에서 어둠의 춤을 아끼거나 회수가 밀리는 구간이 없는지 확인하세요."
        )

    if "어춤↔어칼" in rule:
        return (
            "다음 풀: 첫 어둠의 춤과 어둠의 칼날이 같은 버스트 묶음 안에서 바로 이어지는지 확인하세요."
        )

    if "심장↔어칼" in rule:
        return (
            "다음 풀: 심장과 어둠의 칼날을 첫 버스트 묶음에서 떨어뜨리지 말고 함께 사용하세요."
        )

    if "7CP 마무리" in rule:
        return (
            "다음 풀: 첫 60초 마무리 일격의 현재 콤보 포인트 운용을 유지하세요. "
            "이 항목은 잠금 기준이 정상이라면 억지로 바꾸지 않습니다."
        )

    if "7CP 독살" in rule:
        return (
            "다음 풀: 첫 60초 독살 콤보 포인트 운용을 비교군과 같은 기준으로 유지하세요. "
            "7CP 자체를 문제로 보지 말고 잠금 판정 결과를 따르세요."
        )

    return (
        "다음 풀: 이 항목의 로컬 수치가 비교군에서 벗어난 이유를 해당 보스 타임라인에서 확인하고, "
        "같은 상황에서 불필요한 지연이나 자원 손실을 줄이세요."
    )


def collect_session_issues(boss_data: list[dict]) -> dict[int, list[dict]]:
    """세션 참고 규칙에서 '확인'으로 올린 이유를 보스별로 구조화한다."""
    issues: dict[int, list[dict]] = {}

    # 첫 60초 어춤 회수 부족
    for idx, b in enumerate(boss_data):
        for r in b.get("rules", []):
            if r.get("rule") != "첫 60초 어둠의 춤 회수":
                continue
            pair = parse_count_detail(r.get("detail", ""))
            if pair and pair[0] < pair[1]:
                local, peer = pair
                issues.setdefault(idx, []).append({
                    "title": f"첫 60초 어둠의 춤 {local:g}회 / 비교군 {peer:g}회",
                    "action": coaching_action(r.get("rule", ""), r.get("detail", ""), "표본부족"),
                    "source": "세션 참고",
                })

    # 첫 그림자 일격 세션 이상치
    opener = []
    for idx, b in enumerate(boss_data):
        for r in b.get("rules", []):
            if r.get("rule") != "오프너 시작 — 첫 그림자 일격":
                continue
            pair = parse_seconds_detail(r.get("detail", ""))
            if pair:
                local, peer = pair
                opener.append((idx, local, peer, local - peer))
    if opener:
        diffs = sorted(x[3] for x in opener)
        n = len(diffs)
        med = diffs[n // 2] if n % 2 else (diffs[n // 2 - 1] + diffs[n // 2]) / 2
        for idx, local, peer, diff in opener:
            if diff >= 0.75 and diff >= med + 0.75:
                issues.setdefault(idx, []).append({
                    "title": f"오프너 첫 그림자 일격 {local:.2f}s / 비교군 {peer:.2f}s (+{diff:.2f}s)",
                    "action": coaching_action("오프너 시작 — 첫 그림자 일격", "", "표본부족"),
                    "source": "세션 참고",
                })

    return issues


def collect_locked_issues(boss_data: list[dict]) -> dict[int, list[dict]]:
    """잠금 규칙에서 실제 경고/주의만 행동 항목으로 변환한다."""
    issues: dict[int, list[dict]] = {}
    for idx, b in enumerate(boss_data):
        for r in b.get("rules", []):
            status = r.get("status", "")
            if status not in ("경고", "주의"):
                continue
            issues.setdefault(idx, []).append({
                "title": f"[{status}] {r.get('rule', '')}",
                "action": coaching_action(r.get("rule", ""), r.get("detail", ""), status),
                "source": "잠금 규칙",
            })
    return issues


def merge_issue_maps(*maps):
    out = {}
    for mp in maps:
        for idx, rows in mp.items():
            out.setdefault(idx, []).extend(rows)
    return out


def rule_note(rule: str, status: str) -> str:
    if status == "정상":
        if "7CP 마무리" in rule:
            return "유지: 현재 마무리 자원 사용은 잠금 기준에서 정상입니다."
        return "유지: 현재 잠금 기준에서 정상입니다."
    if status == "표본부족":
        if "심장↔어칼" in rule:
            return "보스 단독 표본 부족. 첫 쿨기 묶음 간격은 세션 요약과 함께 확인합니다."
        if "어춤↔어칼" in rule:
            return "보스 단독 표본 부족. 첫 어춤과 어칼 묶음은 세션 요약과 함께 확인합니다."
        if "어둠의 춤 회수" in rule:
            return "보스 단독 표본 부족. 첫 60초 어춤 회수 횟수를 비교군과 함께 확인합니다."
        if "첫 그림자 일격" in rule:
            return "보스 단독 표본 부족. 풀 직후 첫 그림자 일격 지연 여부를 확인합니다."
        return "보스 단독 표본이 부족해 확정 판정하지 않습니다."
    if status in ("경고", "주의"):
        if "엉겅퀴" in rule or "Tea" in rule:
            return "개선 후보: 엉겅퀴 차 사용 직후 실제 기력 오버캡이 생기지 않는지 확인합니다."
        if "심장" in rule and ("죽표" in rule or "Deathmark" in rule):
            return "개선 후보: 첫 쿨기 묶음의 심장-죽음표식 간격을 줄일 수 있는지 확인합니다."
        if "오프너" in rule or "그림자 일격" in rule or "목조르기" in rule:
            return "개선 후보: 풀 직후 첫 공격이 비교군보다 늦어지는 원인을 확인합니다."
        if "어둠의 춤" in rule:
            return "개선 후보: 첫 60초 안에 어둠의 춤 회수 손실이 있는지 확인합니다."
        return "개선 후보: 잠금 규칙에서 벗어난 원인을 이 전투의 타임라인으로 확인합니다."
    return ""



def path_is_fresh(path: Path | None, source: Path | None) -> bool:
    if not path or not source or not path.exists() or not source.exists():
        return False
    try:
        return path.stat().st_mtime >= source.stat().st_mtime
    except OSError:
        return False


def dataset_fights_path(dataset: Path | None) -> Path | None:
    if not dataset:
        return None
    return dataset / "metadata" / "fights.csv"


def dataset_has_all_tracked_players(dataset: Path | None) -> bool:
    if not dataset:
        return False
    p = dataset / "dataset_summary.json"
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        got = set(data.get("players") or [])
        return set(CHARACTERS).issubset(got)
    except Exception:
        return False



def precision_local_fight_meta(boss_root: Path):
    """정밀 분석기와 같은 정책으로 로컬 Fight를 미리 선택해 WCL 필터에 전달."""
    p = boss_root / "metadata" / "fights.csv"
    if not p.exists():
        return None, None
    rows = []
    try:
        with p.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    fight_no = int(float(r.get("fightNo") or 0))
                    dur = float(r.get("durationSec") or 0)
                    success = int(float(r.get("success") or 0))
                except Exception:
                    continue
                if dur > 0:
                    rows.append((fight_no, dur, success))
    except Exception:
        return None, None
    if not rows:
        return None, None
    kills = [r for r in rows if r[2] == 1]
    if kills:
        chosen = sorted(kills, key=lambda x: x[0])[-1]
        return chosen[1], "kill"
    chosen = sorted(rows, key=lambda x: (x[1], x[0]))[-1]
    return chosen[1], "wipe"


def _median(values):
    vals = []
    for v in values:
        try:
            f = float(v)
        except Exception:
            continue
        if f == f:
            vals.append(f)
    return statistics.median(vals) if vals else None


def _fmt_clock(seconds):
    if seconds is None:
        return "-"
    try:
        s = float(seconds)
    except Exception:
        return "-"
    m = int(s // 60)
    rem = int(round(s - m * 60))
    if rem >= 60:
        m += 1
        rem -= 60
    return f"{m}:{rem:02d}"


def _fmt_dps(value):
    if value is None:
        return "-"
    try:
        v = float(value)
    except Exception:
        return "-"
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if abs(v) >= 1000:
        return f"{v/1000:.0f}K"
    return f"{v:.0f}"


def _decode_json_list(value):
    if isinstance(value, list):
        return value
    try:
        x = json.loads(value)
        return x if isinstance(x, list) else []
    except Exception:
        return []


def _decode_local_damage_amount(extras):
    x = _decode_json_list(extras)
    # Advanced Combat Logging v22: 19-field advanced block + event tail.
    for idx in (19, 0):
        if idx < len(x):
            try:
                v = float(x[idx])
            except Exception:
                continue
            if v >= 0:
                return v
    return 0.0


def _select_local_fight_row(boss_root: Path):
    p = boss_root / "metadata" / "fights.csv"
    if not p.exists():
        return None
    rows = []
    try:
        with p.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    no = int(float(r.get("fightNo") or 0))
                    dur = float(r.get("durationSec") or 0)
                    success = int(float(r.get("success") or 0))
                except Exception:
                    continue
                # B32: preserve short resets in fights.csv, but do not select them
                # as the representative fight for normal dashboard/cooldown views.
                if dur <= 0:
                    continue
                rr = dict(r)
                rr["fightNo"] = no
                rr["durationSec"] = dur
                rr["success"] = success
                rows.append(rr)
    except Exception:
        return None
    if not rows:
        return None
    kills = [r for r in rows if r["success"] == 1]
    if kills:
        return sorted(kills, key=lambda r: r["fightNo"])[-1]
    # B37.3.5: short wipes are not automatically resets.  fights.csv does not
    # carry death/engagement audit fields, so use the conservative legacy rule:
    # only obvious sub-second artifacts are excluded here.
    normal_wipes = [r for r in rows if not is_reset_pull({
        "result": "Wipe", "durationSec": r["durationSec"]
    })]
    if not normal_wipes:
        return None
    return sorted(normal_wipes, key=lambda r: (r["durationSec"], r["fightNo"]))[-1]


def _read_json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return None


def compute_window_layout(screen_w: int, screen_h: int) -> dict:
    """Return a conservative layout that also fits portrait work areas.

    ``winfo_screenwidth()`` reports the full monitor width, not the Windows
    *work area*.  On a portrait 1440x2560 monitor a vertical taskbar can leave
    only ~1380 px usable.  B36.3.2 treated 1440 as a normal landscape-sized
    width and could therefore open a little wider than the work area.

    Portrait is now always compact and keeps a generous horizontal reserve for
    taskbars/window borders/DPI rounding.
    """
    screen_w = max(800, int(screen_w or 1480))
    screen_h = max(700, int(screen_h or 900))
    portrait = screen_h > screen_w
    compact = portrait or screen_w < 1280

    if compact:
        # 120 logical px reserve is deliberate: it covers a vertical Windows
        # taskbar as well as DPI rounding on 1440-wide portrait displays.
        margin_w = 120 if portrait else 72
        target_w = max(860, screen_w - margin_w)
        target_w = min(1320, target_w)
        target_h = min(1500 if portrait else 980, max(720, screen_h - 72))
        min_w = min(target_w, max(760, screen_w - 220))
        min_h = min(680, max(600, screen_h - 180))
        sidebar_w = 218 if portrait else 236
        content_pad = 8
    else:
        target_w = min(1480, max(1100, screen_w - 40))
        target_h = min(900, max(720, screen_h - 72))
        min_w = min(1180, max(960, screen_w - 120))
        min_h = min(720, max(640, screen_h - 100))
        sidebar_w = 292
        content_pad = 18

    return {
        "compact": compact,
        "portrait": portrait,
        "width": target_w,
        "height": target_h,
        "minWidth": min_w,
        "minHeight": min_h,
        "sidebarWidth": sidebar_w,
        "contentPad": content_pad,
    }


from live import ui_theme as _ui_theme
from live.ui_theme import RoundBox, RoundPanel, RoundLabelFrame, RoundButton, ScrollPage, install_scroll_notebook, install_vertical_wheel_target, px as _px

# B37.3.9.5: bundled fonts (기본 / 제목·강조) are registered before Tk starts.
_ui_theme.register_bundled_fonts()


# B37.4.36.3: smallest user-resized window, in design px (scaled by _px).
RESIZE_MIN_W = 1120
RESIZE_MIN_H = 640
# V4.5 R2: 세로 모니터에서 처음 여는 창 높이 (design px). 1640 = 트라이 복기가 붕괴 체인 아래
# 「B31 원본」 버튼까지 보이는 높이. 작업 영역보다 크면 작업 영역에 맞춘다.
TALL_DESIGN_H = 1640


def fixed_canvas_scale(work_w: int, work_h: int) -> float:
    """B37.4.19: scale for the fixed 1440 × 900 canvas on a monitor work area.

    1.0 whenever the canvas fits — including a portrait QHD whose work area is
    exactly 1440 wide (the frame's invisible borders may overlap the edge).
    Only a truly smaller work area shrinks the canvas, never below 0.6.  The
    title bar (~40 px) is reserved from the height.
    """
    try:
        w, h = float(work_w), float(work_h) - 40.0
    except (TypeError, ValueError):
        return 1.0
    s = min(1.0, w / _ui_theme.DESIGN_W, h / _ui_theme.DESIGN_H)
    s = int(s * 100) / 100.0
    return max(0.6, s)
FONT_BODY = "Segoe UI"
FONT_TITLE = "Segoe UI"


def _round_button(master, text="", command=None, style=None, state="normal", **_ignored):
    """B37.3.9.7: every action button is a rounded RoundButton; panel-aware corners."""
    outer = _ui_theme.PAGE_BG
    p = master
    while p is not None:
        if isinstance(p, (RoundBox, RoundLabelFrame)):
            outer = _ui_theme.PANEL_BG
            break
        try:
            if str(p.winfo_class()) == "Tk":
                break
            if isinstance(p, tk.Frame) and str(p.cget("bg")).lower() in {_ui_theme.PANEL_BG, "#181d23"}:
                outer = str(p.cget("bg"))
                break
        except Exception:
            pass
        p = getattr(p, "master", None)
    try:
        if str(master.cget("style")) == "Sidebar.TFrame":
            outer = "#181d23"
    except Exception:
        pass
    kind = "accent" if style == "Accent.TButton" else "default"
    return RoundButton(master, text=text, command=command, kind=kind, state=state,
                       font=(FONT_TITLE, 10) if kind == "accent" else (FONT_BODY, 10), outer=outer, pady=6)


def _ui_result_en(value, *, early=False, reset=False, fake=False):
    """Last-mile result label policy for visible UI.

    Internal data keeps its historical Korean/English values for compatibility;
    visible Try/result labels are normalized to English.
    """
    if fake:
        return "False Pull"
    if reset:
        return "Reset"
    if early:
        return "Reset"  # V4.3: 화면 표기 Early Wipe → Reset (내부 판정은 그대로)
    raw = str(value or "").strip()
    up = raw.upper()
    if up in {"KILL", "KILLED", "SUCCESS"} or raw == "킬":
        return "Kill"
    if up in {"WIPE", "ABORT"} or raw == "와이프":
        return "Wipe"
    if raw in ("초반 전멸", "Early Wipe"):
        return "Reset"
    if raw == "리셋":
        return "Reset"
    if raw == "가짜 풀":
        return "False Pull"
    return raw or "-"


class App(tk.Tk):
    edition = "full"
    raid_public_mode = False

    def __init__(self, *, edition: str = "full"):
        if str(edition or "").strip().lower() not in {"raid", "public", "public_raid"}:
            raise RuntimeError("Public distribution does not support personal mode")
        self.edition = str(edition or "full").strip().lower()
        self.raid_public_mode = self.edition in {"raid", "public", "public_raid"}
        super().__init__()
        if self.raid_public_mode:
            self.title(f"NiniAnalyzer v{PUBLIC_APP_VERSION}")
        else:
            self.title(f"NiniAnalyzer Personal · App {APP_BUILD}")
        # B36.3.4: Tk reports the primary monitor on Windows, which made a
        # secondary 2560x1440 monitor rotated to portrait look like landscape.
        # Use the work area of the monitor under the cursor at launch instead.
        # This also excludes the Windows taskbar from the usable size.
        self._startup_work_area = _startup_monitor_work_area()
        if self._startup_work_area:
            sw = int(self._startup_work_area["width"])
            sh = int(self._startup_work_area["height"])
        else:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
        layout = compute_window_layout(sw, sh)
        # B37.4: landscape = the approved 1440×900 design canvas, uniformly
        # scaled to the monitor work area (QHD 2560×1392 → ×1.54) and fixed.
        self._design_scale = 1.0
        self._fixed_canvas = False
        # B37.4.1: the window is the design canvas itself — 1440 × 900, fixed.
        # Long pages scroll inside the window.  Only a monitor narrower than the
        # canvas (portrait QHD = 1440 wide) keeps the legacy layout.
        # B37.4.19: every monitor gets the landscape 1440 × 900 canvas — also a
        # portrait QHD (work area exactly 1440 wide), which B37.4.1 sent to the
        # tall legacy layout because it subtracted 16 px of frame first.  A
        # monitor that is genuinely narrower/shorter shrinks the same canvas
        # uniformly instead of switching layouts.  WCLA_LAYOUT=legacy opts out.
        if os.environ.get("WCLA_LAYOUT", "") != "legacy":
            scale = fixed_canvas_scale(sw, sh)
            forced = os.environ.get("WCLA_SCALE")
            if forced:
                try:
                    scale = float(forced)
                except ValueError:
                    pass
            _ui_theme.set_scale(scale)
            self._design_scale = scale
            self._fixed_canvas = True
            try:
                base = float(self.winfo_fpixels("1i")) / 72.0
                self.tk.call("tk", "scaling", base * scale)
            except Exception:
                pass
            # Height is the fixed design height; extra monitor width widens the
            # main column (sidebar and panel proportions stay as designed).
            fixed_w = _px(_ui_theme.DESIGN_W)
            layout = dict(layout, compact=False, width=fixed_w, height=_px(_ui_theme.DESIGN_H),
                          minWidth=fixed_w, minHeight=_px(_ui_theme.DESIGN_H),
                          sidebarWidth=_px(_ui_theme.SIDEBAR_W), contentPad=_px(18),
                          portrait=False)
            # V4.5: 세로 모니터(작업 영역이 세로로 긴 경우)는 처음부터 세로로 길게 연다 — 붕괴 체인까지 한 화면.
            # 가로 모니터는 그대로 1440×900. WCLA_TALL=0 이면 끔.
            try:
                if os.environ.get("WCLA_TALL", "1") != "0" and float(sh) > float(sw) * 1.25:
                    # 제목줄·작업표시줄 여유 120px 를 남긴다 (창이 화면 위로 넘어가지 않게).
                    layout["height"] = max(int(layout["height"]), min(int(sh) - 120, _px(TALL_DESIGN_H)))
            except Exception:
                pass
        self._compact_ui = bool(layout["compact"])
        self._portrait_ui = bool(layout.get("portrait", False))
        if self._startup_work_area:
            work = self._startup_work_area
            x = int(work["x"]) + max(0, (int(work["width"]) - int(layout["width"])) // 2)
            y = int(work["y"]) + max(0, (int(work["height"]) - int(layout["height"])) // 2)
            if self._fixed_canvas and os.name == "nt":
                # Tk places the outer frame; Windows 10/11 frames carry an
                # invisible ~8 px resize border on each side.  Centre the
                # client area so a 1440-wide canvas sits flush on a 1440-wide
                # portrait monitor instead of spilling 8 px off its right edge.
                x = int(work["x"]) + (int(work["width"]) - int(layout["width"])) // 2 - 8
                x = max(int(work["x"]) - 8, x)
            self.geometry(f"{layout['width']}x{layout['height']}{x:+d}{y:+d}")
        else:
            self.geometry(f"{layout['width']}x{layout['height']}")
        # V4.5 R2: 창이 뜬 뒤 실제 창 테두리(제목줄 포함)를 그 모니터 작업 영역 안으로 맞춘다 (Windows).
        # 제목줄이 화면 위로 나가 창을 못 옮기는 일을 막는다. 실패해도 창은 그대로.
        try:
            from live.v45_timeline import keep_on_screen as _v45_keep_on_screen
            self.after(300, lambda: _v45_keep_on_screen(self))
            self.after(1500, lambda: _v45_keep_on_screen(self))
        except Exception:
            pass
        if self._fixed_canvas:
            # B37.4.36.3: the window always OPENS at the design canvas size
            # (nothing is remembered between runs), but it is a normal window
            # afterwards — drag a corner/edge or maximise.  The design scale
            # stays as set at startup; pages already stretch/scroll.  A floor
            # keeps the sidebar and fixed columns from being crushed.
            min_w = min(int(layout["width"]), _px(RESIZE_MIN_W))
            min_h = min(int(layout["height"]), _px(RESIZE_MIN_H))
            self.minsize(min_w, min_h)
            self.resizable(True, True)
        else:
            self.minsize(layout["minWidth"], layout["minHeight"])
        self._sidebar_width = int(layout["sidebarWidth"])
        self._content_pad = int(layout["contentPad"])
        self._target_window_width = int(layout["width"])
        self._target_window_height = int(layout["height"])
        self._configure_b34_theme()

        self.msgq = queue.Queue()
        self.proc = None
        self.batch_running = False
        self.batch_missing_players = []
        self.precision_running = False
        self.cooldown_running = False
        self.archive_running = False
        # External WCL: personal GOLD only, preserves existing GUI and live engine.
        self.external_wcl_running = False
        self.external_wcl_last_result = None
        self.cooldown_data = None
        self.cooldown_entries = {}
        self.cooldown_selected_ids = set()
        self.cooldown_icon_refs = []
        self.cooldown_icon_buttons = {}
        self.cooldown_selector_frames = []
        self.cooldown_heatmap_after_id = None
        self.cooldown_heatmap_tooltip = None
        # B14: 상위 쿨기/NSRT가 마지막 조회 보스에 남지 않도록
        # 현재 화면에 로드된 보스 컨텍스트를 별도로 추적한다.
        self.cooldown_context_key = None
        # PUBLIC session data must never share the developer's data/live_sessions.
        self._public_home = public_home() if self.raid_public_mode else None
        self._public_log_dir = (discover_log_dir(settings=read_public_settings(self._public_home))
                                if self.raid_public_mode else None)
        self.current_player = "" if self.raid_public_mode else DEFAULT_PLAYER
        self.character_logs: dict[str, Path | None] = {p: None for p in CHARACTERS}
        self.manual_log_players: set[str] = set()
        self.current_log: Path | None = None
        self.current_dataset: Path | None = None
        self.boss_data = []
        # B33: stable shared selection identity.  Never identify a boss by list
        # position: the same encounter can exist on another date/player/spec.
        self.nav_state = new_selection_state(self.current_player)
        self._nav_syncing = False
        # B37.3.9.14: the legacy recent-try Treeview is only a projection.
        # Programmatic refresh events must never steal shared boss/session selection.
        self._live_recent_syncing = False
        self._live_recent_user_click = False
        self.batch_origin_token = None
        self.precision_origin_token = None
        # B37.3.8.3: precision text must belong to the currently selected boss.
        # Keep the loaded precision context separate from async job ownership.
        self.precision_context_key = None
        self.cooldown_origin_token = None

        self.live_monitor = (
            LiveRaidReviewMonitor(
                (self._public_log_dir or WORKDIR) if self.raid_public_mode else LOGDIR,
                storage_root=(public_sessions_root(self._public_home) if self.raid_public_mode
                              else WORKDIR / "data" / "live_sessions"),
                player=None if self.raid_public_mode else self.current_player,
                tracked_players=None if self.raid_public_mode else CHARACTERS,
                raid_only=self.raid_public_mode,
            ) if LiveRaidReviewMonitor else None
        )
        self.live_poll_after_id = None
        self.live_running = False
        # B34.1: canonical session navigator rows are hydrated from disk and then
        # overlaid with fresher in-memory Live rows.  The hidden legacy list is a
        # projection only; shared navigation never depends on it.
        self.live_session_rows = []
        self.live_persisted_rows = []
        self._live_storage_loaded = False
        self._live_storage_revision = None
        # B34.1.2: old-session player ownership migration used to scan raw logs
        # synchronously on Tk's UI thread.  Large saved sessions could make
        # Windows mark the app as "Not responding" during startup.
        self._identity_audit_started = False
        self._identity_audit_running = False
        # B34.2.1: persisted B2/B3 reviews are upgraded in a separate daemon
        # worker after player-identity migration.  Raw parsing never blocks Tk.
        self._saved_reanalysis_started = False
        self._saved_reanalysis_running = False
        self._feign_recheck_running = False
        self._saved_reanalysis_total = 0
        self._saved_reanalysis_done = 0
        self._public_import_running = False
        self._public_import_cancel = None
        self._public_close_when_idle = False
        self._public_migration_running = bool(
            self.raid_public_mode and (WORKDIR / 'data' / 'live_sessions').is_dir()
            and not (public_sessions_root(self._public_home) / MARKER).exists()
        )
        # B34.2.4: background reanalysis pre-hydrates display-only review models.
        # The completion handoff can then rebuild Tk widgets without reopening
        # dozens of JSON files on the UI thread.
        self._prepared_review_models = {}
        self._prepared_series_models = {}

        self._build_ui()
        self._refresh_cooldown_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_log_label()
        self.after(75, self._refresh_live_recent)
        self.after(100, self._pump_queue)
        if self._public_migration_running:
            self.after(125, self._public_start_legacy_migration)
        # Full/dev edition keeps the historical character-bound source discovery.
        # Public raid mode intentionally starts without a character identity.
        if not self.raid_public_mode:
            self.after(150, lambda: self._switch_to_player_log(DEFAULT_PLAYER, refresh_results=True))

    def destroy(self):
        """B37.3.9.7: collect Tk-variable cycles on the main thread.

        Rounded panels and dynamic cards create closures over ``self``; if those
        cycles are left for the garbage collector they can be finalised later on
        a worker thread, where Tk variable cleanup blocks ("main thread is not in
        main loop").  Collect them here, while Tk is still owned by this thread.

        RC4.2: pending ``after`` callbacks (queue pump, ScrollPage watchers) are
        cancelled first, so closing never ends with Tcl "invalid command name"
        background errors.
        """
        try:
            for handle in self.tk.call("after", "info"):
                try:
                    self.after_cancel(handle)
                except tk.TclError:
                    pass
        except tk.TclError:
            pass
        # rc5.4 (GitHub): Windows Tk fires <<NotebookTabChanged>> while the selected tab is being
        # destroyed; the handler then touched already-destroyed widgets ("bad window path name") and
        # the in-flight callback made destroy fail ("can't delete Tcl command"). Stop listening first.
        self._closing = True
        try:
            self.notebook.unbind("<<NotebookTabChanged>>")
        except (AttributeError, tk.TclError):
            pass
        # rc5.4 (GitHub): ``root.after_cancel`` deletes the Tcl command of an ``after`` that a *child*
        # widget scheduled, but the child still lists it in ``_tclCommands`` -> its destroy() raises
        # "can't delete Tcl command" (seen on Windows where a child timer is still pending at close).
        self._purge_dead_tcl_commands()
        try:
            super().destroy()
        finally:
            import gc as _gc
            _gc.collect()

    def _configure_b34_theme(self):
        """B34 approved dark shell.  Analysis widgets keep their existing data paths."""
        global FONT_BODY, FONT_TITLE
        FONT_BODY, FONT_TITLE = _ui_theme.resolve_families(self)
        _ui_theme.BODY_FAMILY_ACTIVE = FONT_BODY
        _ui_theme.TITLE_FAMILY_ACTIVE = FONT_TITLE
        try:
            from tkinter import font as _tkfont
            for _name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                _tkfont.nametofont(_name).configure(family=FONT_BODY)
        except Exception:
            pass
        self.configure(background="#12151a")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        bg = _ui_theme.PAGE_BG
        panel = _ui_theme.PANEL_BG
        panel2 = "#20262e"
        border = "#343b45"
        fg = "#e9edf2"
        muted = "#a7b0ba"
        accent = "#f5a548"
        blue = "#78b9eb"
        style.configure("App.TFrame", background=bg)
        style.configure("Sidebar.TFrame", background="#181d23")
        style.configure("Card.TFrame", background=panel, relief="flat")
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg, font=(FONT_BODY, 10))
        style.configure("Sidebar.TLabel", background="#181d23", foreground=fg)
        style.configure("Muted.Sidebar.TLabel", background="#181d23", foreground=muted, font=(FONT_BODY, 9))
        style.configure("Title.Sidebar.TLabel", background="#181d23", foreground=fg, font=(FONT_TITLE, 16))
        style.configure("Header.TLabel", background=bg, foreground=fg, font=(FONT_TITLE, 11))
        style.configure("Title.TLabel", background=bg, foreground=fg, font=(FONT_TITLE, 21))
        style.configure("Muted.TLabel", background=bg, foreground=muted, font=(FONT_BODY, 9))
        style.configure("Card.TLabel", background=panel, foreground=fg)
        style.configure("CardTitle.TLabel", background=panel, foreground=fg, font=(FONT_TITLE, 11))
        style.configure("Accent.TButton", background=accent, foreground="#151515", bordercolor=accent, focusthickness=0, padding=_px((12, 9)), font=(FONT_BODY, 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#ffb45d"), ("disabled", "#7d654c")])
        style.configure("TButton", background=panel2, foreground=fg, bordercolor=border, lightcolor=panel2, darkcolor=panel2, focusthickness=0, padding=_px((9, 6)), font=(FONT_BODY, 10))
        style.map("TButton", background=[("active", "#2b333d"), ("pressed", "#313a45")], foreground=[("disabled", "#68727c")])
        style.configure("Nav.TButton", background=bg, foreground=muted, borderwidth=0, padding=_px((10, 7)), font=(FONT_BODY, 10, "bold"))
        style.configure("NavActive.TButton", background=bg, foreground=accent, borderwidth=0, padding=_px((10, 7)), font=(FONT_BODY, 10, "bold"))
        style.configure("TLabelframe", background=panel, foreground=fg, bordercolor=border, lightcolor=border, darkcolor=border)
        style.configure("TLabelframe.Label", background=panel, foreground=fg, font=(FONT_BODY, 10, "bold"))
        style.configure("TCombobox", fieldbackground=panel2, background=panel2, foreground=fg, arrowcolor=fg, bordercolor=border)
        style.map("TCombobox", fieldbackground=[("readonly", panel2)], foreground=[("readonly", fg)])
        style.configure("Treeview", background=panel, fieldbackground=panel, foreground=fg, bordercolor=panel, lightcolor=panel, darkcolor=panel, borderwidth=0, rowheight=_px(28), font=(FONT_BODY, 10))
        style.configure("Treeview.Heading", background=panel, foreground=muted, bordercolor=panel, lightcolor=panel, darkcolor=panel, relief="flat", font=(FONT_BODY, 9))
        style.map("Treeview", background=[("selected", "#2b3948")], foreground=[("selected", fg)])
        style.configure("Nav.Treeview", background="#181d23", fieldbackground="#181d23", foreground=fg, borderwidth=0, bordercolor="#181d23", lightcolor="#181d23", darkcolor="#181d23", rowheight=_px(30), font=(FONT_BODY, 10))
        style.map("Nav.Treeview", background=[("selected", "#2a3038")], foreground=[("selected", fg)])
        style.configure("Horizontal.TProgressbar", troughcolor="#0e2638", background=blue, bordercolor="#0e2638")
        try:
            style.layout("Hidden.TNotebook.Tab", [])
        except Exception:
            pass
        style.configure("Hidden.TNotebook", background=bg, borderwidth=0, bordercolor=bg, lightcolor=bg, darkcolor=bg)
        # B37.3.9.7: one border colour everywhere, thin rounded-looking scrollbars, card styles.
        cardbg = _ui_theme.CARD_BG
        cborder = _ui_theme.CARD_BORDER
        style.configure("Card.TFrame", background=panel)
        style.configure("Muted.Card.TLabel", background=panel, foreground=muted, font=(FONT_BODY, 9))
        style.configure("Title.Card.TLabel", background=panel, foreground=fg, font=(FONT_TITLE, 18))
        style.configure("Card.TCheckbutton", background=panel, foreground=fg, font=(FONT_BODY, 10), indicatorbackground=cardbg, indicatorforeground=accent, bordercolor=cborder, lightcolor=cborder, darkcolor=cborder)
        style.map("Card.TCheckbutton", background=[("active", panel)], indicatorbackground=[("selected", accent), ("active", cardbg)])
        style.configure("Card.TRadiobutton", background=panel, foreground=fg, font=(FONT_BODY, 10), indicatorbackground=cardbg, indicatorforeground=accent, bordercolor=cborder)
        style.map("Card.TRadiobutton", background=[("active", panel)])
        style.configure("Card.TSeparator", background=cborder)
        for _sb in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(_sb, troughcolor=panel, background="#3a414b", bordercolor=panel, lightcolor="#3a414b", darkcolor="#3a414b", arrowcolor=panel, gripcount=0, relief="flat", borderwidth=0, arrowsize=10, width=10)
            style.map(_sb, background=[("active", "#4a525d"), ("pressed", "#5a636f")])
        try:
            style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
            style.layout("Horizontal.TScrollbar", [("Horizontal.Scrollbar.trough", {"sticky": "we", "children": [("Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        except Exception:
            pass
        style.configure("TCombobox", fieldbackground=cardbg, background=cardbg, foreground=fg, arrowcolor=muted, bordercolor=cborder, lightcolor=cborder, darkcolor=cborder, padding=_px((8, 6)))
        style.map("TCombobox", fieldbackground=[("readonly", cardbg)], foreground=[("readonly", fg)], selectbackground=[("readonly", cardbg)], selectforeground=[("readonly", fg)], bordercolor=[("focus", cborder)])
        self.option_add("*TCombobox*Listbox.background", cardbg)
        self.option_add("*TCombobox*Listbox.foreground", fg)
        self.option_add("*TCombobox*Listbox.selectBackground", "#35536e")
        style.configure("Accent.TButton", bordercolor=accent, lightcolor=accent, darkcolor=accent)
        style.map("Accent.TButton", background=[("active", "#f7b465")], bordercolor=[("active", accent)])
        style.configure("TButton", bordercolor=cborder, lightcolor=panel2, darkcolor=panel2)
        style.map("TButton", background=[("active", "#2a313a"), ("disabled", panel)], foreground=[("disabled", "#5d6570")], bordercolor=[("active", cborder)])
        style.configure("Horizontal.TProgressbar", troughcolor="#0e2638", background=blue, bordercolor="#0e2638", lightcolor=blue, darkcolor=blue, thickness=6)
        style.configure("Hidden.TNotebook.Tab", background=bg, foreground=fg, borderwidth=0)
        self.option_add("*Text.background", panel)
        self.option_add("*Text.foreground", fg)
        self.option_add("*Text.insertBackground", fg)
        self.option_add("*Text.selectBackground", "#35536e")
        self.option_add("*Text.relief", "flat")
        self.option_add("*Canvas.background", panel)

    def _public_stage_placeholder(self, feature: str):
        """Visible placeholder for stage-4 import/settings wiring.

        Stage 1 must not call the character-bound WCL pipeline from the public
        edition.  Keeping a no-op button is safer than accidentally falling
        through to start_analysis()/pick_log().
        """
        if hasattr(self, "status_var"):
            self.status_var.set(f"{feature} · 배포 기반 단계에서 연결 예정")

    def _build_ui(self):
        self._build_ui_core()
        try:
            _ui_theme.fix_panel_children(self)
        except Exception:
            pass

    def _build_ui_core(self):
        outer = ttk.Frame(self, style="App.TFrame")
        outer.pack(fill="both", expand=True)

        # Left fixed navigation column from the approved B31.2 mockup.
        sidebar_pad_x = 10 if self._compact_ui else 13
        sidebar = ttk.Frame(outer, style="Sidebar.TFrame", width=self._sidebar_width, padding=(sidebar_pad_x, 16, sidebar_pad_x, 12))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        main = ttk.Frame(outer, style="App.TFrame")
        main.pack(side="left", fill="both", expand=True)

        # Both editions use the same 42px visual brand baseline; the icon and
        # wordmark are optically centered in their edition-specific PNG files.
        brand_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        brand_row.configure(height=_px(42))
        brand_row.pack(anchor="w", fill="x")
        brand_row.pack_propagate(False)
        if self.raid_public_mode:
            logo_png = WORKDIR / "resources" / "brand_public" / "nini_analyzer_sidebar_wordmark.png"
            if not logo_png.is_file():
                logo_png = WORKDIR / "resources" / "nini_analyzer_sidebar_wordmark.png"
        else:
            logo_png = WORKDIR / "resources" / "brand_personal" / "nini_analyzer_sidebar_wordmark.png"
        self._nini_sidebar_lockup = None
        if logo_png.is_file():
            try:
                self._nini_sidebar_lockup = tk.PhotoImage(file=str(logo_png))
                tk.Label(brand_row, image=self._nini_sidebar_lockup,
                         bg="#181d23", bd=0, highlightthickness=0).pack(side="left", anchor="center")
            except (tk.TclError, OSError):
                self._nini_sidebar_lockup = None
        if self._nini_sidebar_lockup is None:
            ttk.Label(brand_row, text="NiniAnalyzer",
                      style="Title.Sidebar.TLabel").pack(side="left", anchor="center")
        version_label = ttk.Label(
            sidebar,
            text=(f"v{PUBLIC_APP_VERSION}" if self.raid_public_mode else f"engine v0.4-B31  ·  App {APP_BUILD}"),
            style="Muted.Sidebar.TLabel",
        )
        version_label.pack(anchor="w", pady=_px((2, 14)))
        if self.raid_public_mode:
            # 1.0.3: 버전 글자를 누르면 새 소식 (최근 3개 버전)
            def _open_news(_event=None):
                try:
                    from live.public_onboarding import open_whats_new
                    open_whats_new(self)
                except Exception:
                    pass
            version_label.configure(cursor="hand2")
            version_label.bind("<Button-1>", _open_news)

        self.player_var = tk.StringVar(value=self.current_player)
        self.players_found_var = tk.StringVar(value="")
        if self.raid_public_mode:
            # Compatibility widget only: legacy full-edition methods keep their
            # references, but no character card is rendered in the public UI.
            self.player_combo = ttk.Combobox(sidebar, textvariable=self.player_var, values=(), state="disabled")
        else:
            _char_box = RoundBox(sidebar, fill=_ui_theme.PANEL_BG, outline=_ui_theme.PANEL_BORDER, outer="#181d23", radius=10, padx=12, pady=10)
            _char_box.pack(fill="x", pady=_px((0, 12)))
            char_card = _char_box.inner
            tk.Label(char_card, text="캐릭터", bg=_ui_theme.PANEL_BG, fg=_ui_theme.MUTED, font=(FONT_BODY, 9), anchor="w").pack(fill="x", pady=_px((0, 6)))
            self.player_combo = ttk.Combobox(char_card, textvariable=self.player_var, values=CHARACTERS, state="readonly")
            self.player_combo.pack(fill="x")
            self.player_combo.bind("<<ComboboxSelected>>", self.on_player_changed)
            ttk.Label(char_card, textvariable=self.players_found_var, style="Card.TLabel", font=(FONT_BODY, 8), wraplength=max(150, self._sidebar_width - 34)).pack(anchor="w", pady=_px((6, 0)))

        self.live_state_var = tk.StringVar(value="○ 감시 중지")
        self.b3796_live_box = RoundBox(sidebar, fill=_ui_theme.PANEL_BG, outline=_ui_theme.PANEL_BORDER, outer="#181d23", radius=10, padx=12, pady=10)
        self.b3796_live_box.pack(fill="x", pady=_px((0, 12)))
        live_card = self.b3796_live_box.inner
        ttk.Label(live_card, textvariable=self.live_state_var, style="CardTitle.TLabel").pack(anchor="w")
        self.sidebar_live_sub_var = tk.StringVar(value="다음 ENCOUNTER_START 대기")
        ttk.Label(
            live_card, textvariable=self.sidebar_live_sub_var, style="Card.TLabel",
            font=(FONT_BODY, 8), wraplength=max(180, self._sidebar_width - 48),
        ).pack(anchor="w", pady=_px((5, 6)))
        live_btns = ttk.Frame(live_card, style="Card.TFrame")
        live_btns.pack(fill="x")
        self.sidebar_live_start_btn = RoundButton(live_btns, text="감시 시작", command=self.start_live_monitor, font=(FONT_BODY, 10), outer=_ui_theme.PANEL_BG, pady=6)
        self.sidebar_live_start_btn.pack(side="left", fill="x", expand=True)
        self.sidebar_live_stop_btn = RoundButton(live_btns, text="중지", command=self.stop_live_monitor, state="disabled", font=(FONT_BODY, 10), outer=_ui_theme.PANEL_BG, pady=6)
        self.sidebar_live_stop_btn.pack(side="left", padx=_px((6, 0)))

        ttk.Label(sidebar, text="레이드 세션" if self.raid_public_mode else "세션 탐색", style="Sidebar.TLabel", font=(FONT_TITLE, 10)).pack(anchor="w", pady=_px((0, 5)))
        nav_wrap = ttk.Frame(sidebar, style="Sidebar.TFrame")
        nav_wrap.pack(fill="both", expand=True)
        self._nav_wrap = nav_wrap

        # B37.3.9.9: keep the Treeview as the stable navigation model/API, but
        # render the visible session explorer on a Canvas so it can match the
        # approved PDF board: date bar -> boss header -> aligned Try rows,
        # without Treeview connector lines/indent drift.
        self.nav_canvas = tk.Canvas(
            nav_wrap, bg="#181d23", highlightthickness=0, bd=0, height=_px(120),
            yscrollincrement=_px(20),  # B37.4.1: ~3 rows per wheel notch
        )
        self.nav_canvas.pack(side="left", fill="both", expand=True)
        nav_scroll = ttk.Scrollbar(nav_wrap, orient="vertical", command=self.nav_canvas.yview)
        nav_scroll.pack(side="right", fill="y")
        self.nav_canvas.configure(yscrollcommand=nav_scroll.set)
        self.nav_canvas.bind("<Button-1>", self._b3799_nav_canvas_click)
        self.nav_canvas.bind("<MouseWheel>", self._b3799_nav_canvas_wheel)
        self.nav_canvas.bind("<Button-4>", self._b3799_nav_canvas_wheel)
        self.nav_canvas.bind("<Button-5>", self._b3799_nav_canvas_wheel)
        self.nav_canvas.bind("<Configure>", lambda _e: self._b3799_render_session_nav(preserve_anchor=True))

        # Hidden compatibility/model tree. Existing selection/context code and
        # tests continue to use this exact object; only its visual surface is
        # replaced by the PDF-style canvas above.
        self.nav_tree = ttk.Treeview(nav_wrap, show="tree", selectmode="browse", style="Nav.Treeview")
        self.nav_tree.column("#0", width=max(190, self._sidebar_width - 40), stretch=True)
        self.nav_tree.bind("<<TreeviewSelect>>", self._on_nav_tree_selected)
        try:
            self.nav_tree.tag_configure("excluded", foreground="#747d87")
        except Exception:
            pass
        self.nav_tree_payload = {}
        self._b3799_nav_meta = {}
        self._b3799_nav_rows = []
        self._b3799_open_dates = set()
        self._b3799_nav_rendering = False
        self._shared_nav_signature = None

        # B34.2: when the selected character has no saved attempts, explain why
        # the tree is empty and offer one-click switches to characters that do.
        self.nav_empty_hint_var = tk.StringVar(value="")
        self.nav_empty_hint = ttk.Label(
            sidebar, textvariable=self.nav_empty_hint_var,
            style="Muted.Sidebar.TLabel", wraplength=max(150, self._sidebar_width - 34), justify="left",
        )
        self.nav_empty_actions = ttk.Frame(sidebar, style="Sidebar.TFrame")

        self.log_var = tk.StringVar()
        self.status_var = tk.StringVar(value="대기")
        self.progress_text_var = tk.StringVar(value="")
        # B37.4.1: the bottom group is allocated before the session explorer so a
        # fixed-height window never clips "로그 분석 실행" and the buttons below it.
        source_btns = ttk.Frame(sidebar, style="Sidebar.TFrame")
        if self.raid_public_mode:
            # bottom packing order is reversed visually: settings is packed
            # first so public UI reads top -> bottom as
            # latest log analysis / log file selection / settings.
            self.settings_sidebar_btn = RoundButton(
                sidebar, text="설정", command=self._public_show_settings,
                font=(FONT_BODY, 10), outer="#181d23"
            )
            self.settings_sidebar_btn.pack(side="bottom", fill="x", pady=_px((0, 5)), before=self._nav_wrap)
            source_btns.pack(side="bottom", fill="x", before=self._nav_wrap)
            self.archive_sidebar_btn = None
        else:
            source_btns.pack(side="bottom", fill="x", before=self._nav_wrap)
            log_btn = RoundButton(sidebar, text="실행 로그 · 데이터 정리", command=lambda: self._show_main_frame(self.log_frame), font=(FONT_BODY, 10), outer="#181d23")
            log_btn.pack(side="bottom", fill="x", pady=_px((0, 5)), before=self._nav_wrap)
            self.archive_sidebar_btn = RoundButton(
                sidebar, text="PRIVATE_ONLY WCL 연대기", command=self.show_nini_archive,
                font=(FONT_BODY, 10), outer="#181d23"
            )
            self.archive_sidebar_btn.pack(side="bottom", fill="x", pady=_px((0, 5)), before=self._nav_wrap)
        progress_card = ttk.Frame(sidebar, style="Sidebar.TFrame")
        self.progress = ttk.Progressbar(progress_card, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        ttk.Label(progress_card, textvariable=self.progress_text_var, style="Muted.Sidebar.TLabel", wraplength=max(150, self._sidebar_width - 34)).pack(anchor="w", pady=_px((4, 0)))
        self._progress_pct = 0.0
        self._progress_title = ""
        self._progress_stage = ""
        self._progress_owner = None

        if self.raid_public_mode:
            self.run_btn = RoundButton(
                sidebar, text="최신 로그 분석", kind="accent",
                command=self._public_import_latest,
                font=(FONT_TITLE, 11), outer="#181d23", pady=10,
            )
        else:
            self.run_btn = RoundButton(sidebar, text="로그 분석 실행", kind="accent", command=self.start_analysis, font=(FONT_TITLE, 11), outer="#181d23", pady=10)
        self.run_btn.pack(side="bottom", fill="x", pady=_px((8, 5)), before=self._nav_wrap)
        progress_card.pack(side="bottom", fill="x", pady=_px((10, 0)), before=self._nav_wrap)
        if self.raid_public_mode:
            self.public_log_pick_btn = RoundButton(
                source_btns, text="로그 파일 선택",
                command=self._public_import_choose,
                font=(FONT_BODY, 10), outer="#181d23",
            )
            self.public_log_pick_btn.pack(side="left", fill="x", expand=True)
        else:
            RoundButton(source_btns, text="최신 로그", command=self.pick_latest, font=(FONT_BODY, 10), outer="#181d23").pack(side="left", fill="x", expand=True)
            RoundButton(source_btns, text="로그 선택", command=self.pick_log, font=(FONT_BODY, 10), outer="#181d23").pack(side="left", fill="x", expand=True, padx=(5, 0))

        # Top breadcrumb and context actions.
        nav_header = ttk.Frame(main, style="App.TFrame", padding=(self._content_pad, 12, self._content_pad, 8))
        nav_header.pack(fill="x")
        self.nav_back_btn = _round_button(nav_header, text="← 뒤로", command=self._navigate_back_shared, state="disabled")
        self.nav_back_btn.pack(side="left")
        self.breadcrumb_var = tk.StringVar(value="분석 세션")
        ttk.Label(nav_header, textvariable=self.breadcrumb_var, style="Header.TLabel").pack(side="left", padx=_px((12, 0)), fill="x", expand=True)
        self.context_action_btn = _round_button(nav_header, text="세션 보고서", command=self._context_action)
        if not self.raid_public_mode:
            self.context_action_btn.pack(side="right")

        self.context_tabs = ttk.Frame(main, style="App.TFrame", padding=(self._content_pad, 0, self._content_pad, 0))
        self.context_tabs.pack(fill="x")
        self.context_tab_buttons = []

        self.notebook = ttk.Notebook(main, style="Hidden.TNotebook")
        install_scroll_notebook(self.notebook)
        if self._compact_ui:
            # ttk.Notebook otherwise requests the width of its widest *hidden*
            # page.  On portrait screens that request can make Windows enlarge
            # the top-level beyond the available work area even when geometry()
            # asked for a smaller window.  Give the notebook an explicit compact
            # requested width; pack(fill=both, expand=True) still lets it grow.
            compact_notebook_w = max(620, self._target_window_width - self._sidebar_width - (self._content_pad * 2) - 28)
            self.notebook.configure(width=compact_notebook_w)
        self.notebook.pack(fill="both", expand=True, padx=(self._content_pad, self._content_pad), pady=_px((6, 14)))

        self.coach_frame = ttk.Frame(self.notebook)
        self.dashboard_frame = ttk.Frame(self.notebook)
        self.boss_briefing_frame = ScrollPage(self.notebook, bg=_ui_theme.PAGE_BG)
        self.boss_board_frame = ScrollPage(self.notebook, bg=_ui_theme.PAGE_BG)
        self.live_frame = ScrollPage(self.notebook, bg=_ui_theme.PAGE_BG)
        self.quality_frame = ttk.Frame(self.notebook)
        self.burst_frame = ttk.Frame(self.notebook)
        self.timeline_frame = ttk.Frame(self.notebook)
        self.targets_frame = ttk.Frame(self.notebook)
        self.precision_frame = ttk.Frame(self.notebook)
        self.cooldown_frame = ScrollPage(self.notebook, bg=_ui_theme.PAGE_BG)
        self.brief_frame = ttk.Frame(self.notebook)
        self.boss_frame = ttk.Frame(self.notebook)
        self.result_frame = ttk.Frame(self.notebook)
        self.log_frame = ttk.Frame(self.notebook)
        self.archive_frame = ScrollPage(self.notebook, bg=_ui_theme.PAGE_BG)
        self.external_wcl_frame = ScrollPage(self.notebook, bg=_ui_theme.PAGE_BG) if not self.raid_public_mode else None

        if self.raid_public_mode:
            # Public v1 exposes exactly the raid-lead workflow.  Legacy/full
            # frames still exist as implementation detail so shared rendering
            # code stays single-source, but they are not Notebook pages.
            self.notebook.add(self.coach_frame, text="레이드 세션")
            self.notebook.add(self.boss_board_frame, text="트라이 보드")
            self.notebook.add(self.live_frame, text="트라이 복기")
        else:
            self.notebook.add(self.coach_frame, text="세션 개요")
            self.notebook.add(self.dashboard_frame, text="보스 개요")
            self.notebook.add(self.boss_briefing_frame, text="사전 브리핑")
            self.notebook.add(self.boss_board_frame, text="트라이 보드")
            self.notebook.add(self.live_frame, text="기믹 판정")
            self.notebook.add(self.quality_frame, text="회전")
            self.notebook.add(self.burst_frame, text="버스트")
            self.notebook.add(self.timeline_frame, text="타임라인")
            self.notebook.add(self.targets_frame, text="피해대상")
            self.notebook.add(self.precision_frame, text="정밀 분석")
            self.notebook.add(self.cooldown_frame, text="상위 쿨기 / NSRT")
            self.notebook.add(self.brief_frame, text="세션 요약")
            self.notebook.add(self.boss_frame, text="규칙 상세")
            self.notebook.add(self.result_frame, text="전체 보고서")
            self.notebook.add(self.log_frame, text="실행 로그")
            self.notebook.add(self.archive_frame, text="PRIVATE_ONLY WCL 연대기")
            self.notebook.add(self.external_wcl_frame, text="외부 WCL 로그")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_main_frame_changed)

        if not self.raid_public_mode:
            self._build_nini_archive_page()
            self._build_external_wcl_page()

        # Hidden controls remain the single stateful button refs expected by the
        # analysis jobs.  Navigation exposes the same commands through B34 tabs.
        hidden_controls = ttk.Frame(main, style="App.TFrame")
        self.precision_btn = _round_button(hidden_controls, text="정밀 분석", command=self.start_precision_analysis)
        self.cooldown_btn = _round_button(hidden_controls, text="쿨기/NSRT", command=self.start_cooldown_analysis)

        # v2.3 보스 비교 대시보드: 로컬 vs 선택 보스 상위 비교군
        dash_top = ttk.Frame(self.dashboard_frame, padding=_px((12, 10, 12, 5)))
        dash_top.pack(fill="x")
        ttk.Label(dash_top, text="보스:", font=(FONT_BODY, 10, "bold")).pack(side="left")
        self.dashboard_boss_var = tk.StringVar(value="")
        self.dashboard_boss_combo = ttk.Combobox(
            dash_top, textvariable=self.dashboard_boss_var, state="readonly", width=34 if self._compact_ui else 44
        )
        self.dashboard_boss_combo.pack(side="left", padx=_px((6, 8)))
        self.dashboard_boss_combo.bind("<<ComboboxSelected>>", self.on_dashboard_boss_changed)
        self.dashboard_precision_btn = _round_button(
            dash_top, text="상위30 정밀 분석", command=self.start_precision_analysis
        )
        self.dashboard_precision_btn.pack(side="left", padx=_px((0, 6)))
        self.dashboard_cooldown_btn = _round_button(
            dash_top, text="쿨기/NSRT 생성", command=self.start_cooldown_analysis
        )
        self.dashboard_cooldown_btn.pack(side="left")

        self.dashboard_title_var = tk.StringVar(value="분석 후 보스를 선택하세요.")
        ttk.Label(
            self.dashboard_frame, textvariable=self.dashboard_title_var,
            font=(FONT_TITLE, 18), padding=_px((14, 4, 14, 8)),
        ).pack(fill="x")

        boss_try_box = RoundLabelFrame(self.dashboard_frame, text="트라이 진행", padding=_px((8, 6)))
        boss_try_box.pack(fill="x", padx=_px(14), pady=_px((0, 10)))
        self.boss_try_tree = ttk.Treeview(
            boss_try_box,
            columns=("result", "duration", "root", "root_at", "personal"),
            show="tree headings",
            height=5,
        )
        self.boss_try_tree.heading("#0", text="Try")
        self.boss_try_tree.heading("result", text="결과")
        self.boss_try_tree.heading("duration", text="시간")
        self.boss_try_tree.heading("root", text="붕괴/개인 원인")
        self.boss_try_tree.heading("root_at", text="시점")
        self.boss_try_tree.heading("personal", text="개인 사망")
        self.boss_try_tree.column("#0", width=_px(70), anchor="center")
        self.boss_try_tree.column("result", width=_px(72), anchor="center")
        self.boss_try_tree.column("duration", width=_px(72), anchor="center")
        self.boss_try_tree.column("root", width=_px(250), anchor="w")
        self.boss_try_tree.column("root_at", width=_px(78), anchor="center")
        self.boss_try_tree.column("personal", width=_px(220), anchor="w")
        self.boss_try_tree.pack(fill="x", expand=True)
        self.boss_try_tree.bind("<Double-1>", self._open_boss_try_from_progress)
        self.boss_try_rows = {}

        self.dashboard_table = ttk.Treeview(
            self.dashboard_frame, columns=("mine", "peer"), show="tree headings", height=4
        )
        self.dashboard_table.heading("#0", text="항목")
        self.dashboard_table.heading("mine", text="나")
        self.dashboard_table.heading("peer", text="상위30")
        self.dashboard_table.column("#0", width=_px(170), anchor="w")
        self.dashboard_table.column("mine", width=_px(170), anchor="center")
        self.dashboard_table.column("peer", width=_px(220), anchor="center")
        self.dashboard_table.pack(fill="x", padx=_px(14), pady=_px((0, 10)))

        diff_box = RoundLabelFrame(self.dashboard_frame, text="이번 로그에서 먼저 고칠 것", padding=_px((10, 8)))
        diff_box.pack(fill="both", expand=True, padx=_px(14), pady=_px((0, 8)))
        self.dashboard_diff = tk.Text(
            diff_box, height=7, wrap="word", font=("Consolas", 11), padx=_px(8), pady=6
        )
        self.dashboard_diff.pack(fill="both", expand=True)
        self.dashboard_diff.tag_configure("warn", foreground="#c62828", font=("Consolas", 11, "bold"))
        self.dashboard_diff.tag_configure("caution", foreground="#ef6c00", font=("Consolas", 11, "bold"))
        self.dashboard_diff.tag_configure("ok", foreground="#2e7d32", font=("Consolas", 11, "bold"))
        self.dashboard_diff.tag_configure("note", foreground="#6d4c41")

        quick = ttk.Frame(self.dashboard_frame, padding=_px((14, 2, 14, 8)))
        quick.pack(fill="x")
        for label, frame in (
            ("회전", self.quality_frame),
            ("버스트", self.burst_frame),
            ("타임라인", self.timeline_frame),
            ("피해대상", self.targets_frame),
            ("상위 쿨기/NSRT", self.cooldown_frame),
        ):
            _round_button(quick, text=label, command=lambda f=frame: self.notebook.select(f)).pack(
                side="left", padx=(0, 6)
            )
        self.dashboard_note_var = tk.StringVar(value="")
        ttk.Label(quick, textvariable=self.dashboard_note_var, font=(FONT_BODY, 9)).pack(
            side="right"
        )

        # B36.4.1: mechanic-first boss tabs (briefing → board) are read-only
        # projections over B36.4 engine artifacts.
        self._build_b3641_boss_views()

        # Raid Wipe Review v0.4-B31 · B34 selected-try detail page.
        live_detail_head = ttk.Frame(self.live_frame, padding=_px((2, 8, 2, 10)))
        live_detail_head.pack(fill="x")
        self._b3741_register_tab_host(self.live_frame, live_detail_head)
        live_detail_text = ttk.Frame(live_detail_head)
        live_detail_text.pack(side="left", fill="x", expand=True)
        self.live_detail_title_var = tk.StringVar(value="트라이를 선택하세요")
        self.live_detail_badge_var = tk.StringVar(value="왼쪽 세션 탐색에서 Try를 누르면 전멸 복기를 엽니다.")
        ttk.Label(live_detail_text, textvariable=self.live_detail_title_var, style="Title.TLabel").pack(anchor="w")
        ttk.Label(live_detail_text, textvariable=self.live_detail_badge_var, style="Muted.TLabel").pack(anchor="w", pady=_px((3, 0)))
        # GOLD puller line (from BLUE RC4.1/RC4.3): first attacker / puller under
        # the title, independent of the review model; name in WoW class colour.
        self.live_pull_var = tk.StringVar(value="")
        pull_row = ttk.Frame(live_detail_text)
        pull_row.pack(anchor="w", pady=_px((1, 0)))
        self.live_pull_prefix_var = tk.StringVar(value="")
        self.live_pull_name_var = tk.StringVar(value="")
        self.live_pull_suffix_var = tk.StringVar(value="")
        ttk.Label(pull_row, textvariable=self.live_pull_prefix_var, style="Muted.TLabel").pack(side="left")
        self.live_pull_name_lbl = ttk.Label(pull_row, textvariable=self.live_pull_name_var, style="Muted.TLabel")
        self.live_pull_name_lbl.pack(side="left", padx=(_px(4), 0))
        ttk.Label(pull_row, textvariable=self.live_pull_suffix_var, style="Muted.TLabel").pack(side="left")
        live_detail_actions = ttk.Frame(live_detail_head)
        live_detail_actions.pack(side="right")
        self.live_detail_reset_btn = _round_button(live_detail_actions, text="정상 트라이로 포함", command=self._view_reset_as_normal, state="disabled")
        self.live_detail_reset_btn.pack(side="right")
        self.live_detail_folder_btn = _round_button(live_detail_actions, text="raw 폴더 열기", command=self.open_live_storage_folder, state="disabled")
        # B37.3.9.2: the top-right context action already opens the raw folder.
        # Keep the widget (state/test hooks) but do not show a second button.

        # B36.4.1: conclusion first → mechanic cards → timeline → evidence.
        self._build_b3641_try_priority_ui()
        # B37.5.0 STEP4.3: child labels/canvases inside ScrollPage receive the
        # Windows wheel event first.  Route wheel input raised anywhere on the
        # selected-Try page to the page's outer vertical scroll canvas.
        self._install_b3750_live_try_wheel()
        self._install_b3750_tooltip_scroll_hide()

        live_top = ttk.Frame(self.live_frame, padding=_px((14, 12, 14, 8)))
        live_top.pack(fill="x")
        self.live_start_btn = _round_button(live_top, text="감시 시작", command=self.start_live_monitor)
        self.live_start_btn.pack(side="left")
        self.live_stop_btn = _round_button(live_top, text="감시 중지", command=self.stop_live_monitor, state="disabled")
        self.live_stop_btn.pack(side="left", padx=_px((8, 0)))
        self.live_reconnect_btn = _round_button(live_top, text="최신 로그 다시 연결", command=self.reconnect_live_monitor)
        self.live_reconnect_btn.pack(side="left", padx=_px((8, 0)))
        ttk.Label(live_top, textvariable=self.live_state_var, font=(FONT_BODY, 10, "bold")).pack(side="right")

        live_info = RoundLabelFrame(self.live_frame, text="Live Log Tail", padding=_px((12, 8)))
        live_info.pack(fill="x", padx=_px(14), pady=_px((0, 8)))
        self.live_file_var = tk.StringVar(value=(f"로그 폴더: {self._public_log_dir or '폴더 선택 필요'}"
                if self.raid_public_mode else f"로그 폴더: {LOGDIR}"))
        self.live_offset_var = tk.StringVar(value="감시 시작 시 기존 파일 끝부터 읽습니다.")
        ttk.Label(live_info, textvariable=self.live_file_var).pack(anchor="w")
        ttk.Label(live_info, textvariable=self.live_offset_var, font=(FONT_BODY, 9)).pack(anchor="w", pady=_px((3, 0)))

        current_box = RoundLabelFrame(self.live_frame, text="현재 트라이", padding=_px((12, 8)))
        current_box.pack(fill="x", padx=_px(14), pady=_px((0, 8)))
        self.live_current_var = tk.StringVar(value="다음 ENCOUNTER_START 대기")
        self.live_current_sub_var = tk.StringVar(value="")
        ttk.Label(current_box, textvariable=self.live_current_var, font=(FONT_TITLE, 14)).pack(anchor="w")
        ttk.Label(current_box, textvariable=self.live_current_sub_var, font=("Consolas", 10)).pack(anchor="w", pady=_px((4, 0)))

        recent_box = RoundLabelFrame(self.live_frame, text="최근 트라이", padding=_px((10, 8)))
        recent_box.pack(fill="x", padx=_px(14), pady=_px((0, 8)))
        self.live_recent_tree = ttk.Treeview(
            recent_box,
            columns=("try", "boss", "diff", "result", "duration", "events"),
            show="headings",
            height=5,
        )
        for col, title, width, anchor in (
            ("try", "Try", 55, "center"),
            ("boss", "보스", 260, "w"),
            ("diff", "난이도", 70, "center"),
            ("result", "결과", 75, "center"),
            ("duration", "시간", 80, "center"),
            ("events", "이벤트", 90, "e"),
        ):
            self.live_recent_tree.heading(col, text=title)
            self.live_recent_tree.column(col, width=width, anchor=anchor)
        self.live_recent_tree.pack(side="left", fill="both", expand=True)
        live_recent_scroll = ttk.Scrollbar(recent_box, orient="vertical", command=self.live_recent_tree.yview)
        live_recent_scroll.pack(side="right", fill="y")
        self.live_recent_tree.configure(yscrollcommand=live_recent_scroll.set)
        self.live_recent_tree.bind("<ButtonPress-1>", self._mark_live_recent_user_click, add="+")
        self.live_recent_tree.bind("<<TreeviewSelect>>", self._on_live_recent_selected)
        self.live_recent_tree.bind("<Double-1>", lambda _e: self.open_live_raw_log())
        self.live_recent_rows = {}
        self.live_selected_key = None

        live_actions = ttk.Frame(self.live_frame, padding=_px((14, 0, 14, 8)))
        live_actions.pack(fill="x")
        self.live_selected_var = tk.StringVar(value="트라이를 선택하면 저장된 raw.log를 바로 열 수 있습니다.")
        ttk.Label(live_actions, textvariable=self.live_selected_var, font=(FONT_BODY, 9)).pack(
            side="left", fill="x", expand=True
        )
        self.live_open_raw_btn = _round_button(
            live_actions, text="원본 이벤트 열기", command=self.open_live_raw_log, state="disabled"
        )
        self.live_open_raw_btn.pack(side="right")
        self.live_open_folder_btn = _round_button(
            live_actions, text="저장 폴더 열기", command=self.open_live_storage_folder, state="disabled"
        )
        self.live_open_folder_btn.pack(side="right", padx=_px((0, 8)))
        self.live_open_facts_btn = _round_button(
            live_actions, text="사실 복기 열기", command=self.open_live_facts_txt, state="disabled"
        )
        self.live_open_facts_btn.pack(side="right", padx=_px((0, 8)))
        self.live_open_death_btn = _round_button(
            live_actions, text="개인 사망 열기", command=self.open_live_death_review_txt, state="disabled"
        )
        self.live_open_death_btn.pack(side="right", padx=_px((0, 8)))
        self.live_open_raid_btn = _round_button(
            live_actions, text="전멸 복기 열기", command=self.open_live_raid_facts_txt, state="disabled"
        )
        self.live_open_raid_btn.pack(side="right", padx=_px((0, 8)))
        self.live_reset_normal_btn = _round_button(
            live_actions, text="정상 트라이로 포함", command=self._view_reset_as_normal, state="disabled"
        )
        self.live_reset_normal_btn.pack(side="right", padx=_px((0, 8)))

        facts_box = RoundLabelFrame(self.live_frame, text="B31 원본 · 개인 사망 · 쿨기 비교 상세", padding=_px((8, 6)))
        self.live_facts_box = facts_box
        # B36.4.1 keeps the B31 body intact but collapsed by default.
        # _toggle_b3641_detail() packs it below the mechanic-first panels.
        self.live_facts = tk.Text(
            facts_box, wrap="word", height=13, font=("Consolas", 11), padx=_px(10), pady=8
        )
        self.live_facts.pack(side="left", fill="both", expand=True)
        facts_scroll = ttk.Scrollbar(facts_box, orient="vertical", command=self.live_facts.yview)
        facts_scroll.pack(side="right", fill="y")
        self.live_facts.configure(yscrollcommand=facts_scroll.set)
        self.live_facts.tag_configure("title", font=("Consolas", 12, "bold"), foreground="#f5a548")
        self.live_facts.tag_configure("note", foreground="#a7b0ba")
        self.live_facts.insert(
            "end",
            "트라이가 끝나면 공대 전체 사망/동시피해/주요 쿨기/타깃 전환/차단·해제를 먼저 재구성합니다.\n"
            "v0.3-A4는 공대 전체 사실·부활 후 행동 재개·selector exact cast를 기록하고, v0.4-B31이 이를 이용해 전멸 원인을 판정합니다.\n",
            "note",
        )

        self.live_log = tk.Text(
            self.live_frame, wrap="word", height=4, font=("Consolas", 9), padx=_px(12), pady=8
        )
        self.live_log.pack(fill="x", padx=_px(14), pady=_px((0, 10)))
        self.live_log.tag_configure("start", foreground="#1565c0", font=("Consolas", 9, "bold"))
        self.live_log.tag_configure("wipe", foreground="#c62828", font=("Consolas", 9, "bold"))
        self.live_log.tag_configure("kill", foreground="#2e7d32", font=("Consolas", 9, "bold"))
        self.live_log.tag_configure("note", foreground="#6d4c41")
        self.live_log.insert(
            "end",
            "v0.3-A4: raw.log 저장 → 공대 raid_facts + 부활 행동 재개 + selector exact cast + 우선 딜 구간 쿨기 확인 + 개인 상세를 백그라운드에서 자동 생성합니다.\n"
            "공대 전체 사실을 메인으로 표시하며, 울라텍은 맹독의 심장 직전 쿨기 소모 후보까지 표시합니다. 최종 원인/개선 판정은 다음 v0.3-B에서 붙입니다.\n"
            "전투 중 시작했다면 현재 전투는 잡지 않고 다음 ENCOUNTER_START부터 추적합니다.\n",
            "note",
        )

        # The approved layout keeps monitoring controls/status in the sidebar.
        # Keep these widgets alive because existing monitor code updates them, but
        # remove the duplicate operational panels from the selected-Try page.
        for _legacy_live_panel in (live_top, live_info, current_box, recent_box, live_actions, self.live_log):
            try:
                _legacy_live_panel.pack_forget()
            except Exception:
                pass

        # 회전 품질 · WowAnalyzer 스타일 로컬 CombatLog 진단
        self.quality = tk.Text(
            self.quality_frame, wrap="word", font=("Consolas", 10), padx=_px(14), pady=12
        )
        self.quality.pack(side="left", fill="both", expand=True)
        quality_scroll = ttk.Scrollbar(
            self.quality_frame, orient="vertical", command=self.quality.yview
        )
        quality_scroll.pack(side="right", fill="y")
        self.quality.configure(yscrollcommand=quality_scroll.set)
        self.quality.tag_configure("title", font=("Consolas", 13, "bold"))
        self.quality.tag_configure("section", font=("Consolas", 11, "bold"))
        self.quality.tag_configure("warn", foreground="#c62828", font=("Consolas", 10, "bold"))
        self.quality.tag_configure("caution", foreground="#ef6c00", font=("Consolas", 10, "bold"))
        self.quality.tag_configure("ok", foreground="#2e7d32", font=("Consolas", 10, "bold"))
        self.quality.tag_configure("note", foreground="#6d4c41")
        self.quality.insert(
            "end",
            "분석 후 '보스 상세'에서 보스를 선택하면 해당 캐릭터/보스의 회전 품질이 자동으로 표시됩니다.\n\n"
            "자원 오버캡, 마무리 CP, 암살 DoT 유지율/칼부 적중수, 잠행 어춤 창별 행동, "
            "쿨기 사용 시각, 전투 중 물약을 확인합니다.",
            "note",
        )

        # 버스트 창 · 어춤/죽표 하나씩 뜯어보기
        self.burst = tk.Text(
            self.burst_frame, wrap="word", font=("Consolas", 10), padx=_px(14), pady=12
        )
        self.burst.pack(side="left", fill="both", expand=True)
        burst_scroll = ttk.Scrollbar(
            self.burst_frame, orient="vertical", command=self.burst.yview
        )
        burst_scroll.pack(side="right", fill="y")
        self.burst.configure(yscrollcommand=burst_scroll.set)
        self.burst.tag_configure("title", font=("Consolas", 13, "bold"))
        self.burst.tag_configure("section", font=("Consolas", 11, "bold"))
        self.burst.tag_configure("warn", foreground="#c62828", font=("Consolas", 10, "bold"))
        self.burst.tag_configure("caution", foreground="#ef6c00", font=("Consolas", 10, "bold"))
        self.burst.tag_configure("ok", foreground="#2e7d32", font=("Consolas", 10, "bold"))
        self.burst.tag_configure("action", foreground="#1565c0")
        self.burst.tag_configure("note", foreground="#6d4c41")
        self.burst.insert(
            "end",
            "분석 후 보스를 선택하면 버스트 창을 하나씩 뜯어봅니다.\n"
            "잠행은 어둠의 춤, 암살은 죽음표식 창을 기준으로 진입 준비·쿨기 정렬·창 내부 행동을 보여줍니다.",
            "note",
        )

        # 선택 Fight 로컬 타임라인
        self.timeline = tk.Text(
            self.timeline_frame, wrap="none", font=("Consolas", 10), padx=_px(14), pady=12
        )
        self.timeline.pack(side="left", fill="both", expand=True)
        timeline_scroll = ttk.Scrollbar(self.timeline_frame, orient="vertical", command=self.timeline.yview)
        timeline_scroll.pack(side="right", fill="y")
        self.timeline.configure(yscrollcommand=timeline_scroll.set)
        self.timeline.tag_configure("title", font=("Consolas", 13, "bold"))
        self.timeline.tag_configure("cooldown", foreground="#1565c0", font=("Consolas", 10, "bold"))
        self.timeline.tag_configure("note", foreground="#6d4c41")
        self.timeline.insert("end", "보스를 선택하면 대표 Fight의 성공 캐스트 타임라인을 표시합니다.", "note")

        # 피해 대상 / 첫 타격
        self.targets = tk.Text(
            self.targets_frame, wrap="word", font=("Consolas", 10), padx=_px(14), pady=12
        )
        self.targets.pack(side="left", fill="both", expand=True)
        targets_scroll = ttk.Scrollbar(self.targets_frame, orient="vertical", command=self.targets.yview)
        targets_scroll.pack(side="right", fill="y")
        self.targets.configure(yscrollcommand=targets_scroll.set)
        self.targets.tag_configure("title", font=("Consolas", 13, "bold"))
        self.targets.tag_configure("section", font=("Consolas", 11, "bold"))
        self.targets.tag_configure("warn", foreground="#c62828", font=("Consolas", 10, "bold"))
        self.targets.tag_configure("note", foreground="#6d4c41")
        self.targets.insert("end", "보스를 선택하면 대표 Fight의 대상별 피해량과 첫 타격 시점을 표시합니다.", "note")

        # 상위 쿨기 / NSRT · B34.2 approved card layout
        cd_head = ttk.Frame(self.cooldown_frame, padding=_px((2, 8, 2, 8)))
        cd_head.pack(fill="x")
        self._b3741_register_tab_host(self.cooldown_frame, cd_head)
        cd_head_text = ttk.Frame(cd_head)
        cd_head_text.pack(side="left", fill="x", expand=True)
        self.cooldown_title_var = tk.StringVar(value="상위 쿨기 / NSRT")
        self.cooldown_badge_var = tk.StringVar(value="비교 계획을 만들거나 저장된 메모를 확인합니다.")
        ttk.Label(cd_head_text, textvariable=self.cooldown_title_var, style="Title.TLabel").pack(anchor="w")
        ttk.Label(cd_head_text, textvariable=self.cooldown_badge_var, style="Muted.TLabel").pack(anchor="w", pady=_px((3, 0)))
        _round_button(cd_head, text="비교군 새로 받기", command=self.start_cooldown_analysis).pack(side="right")

        # Existing-result top cards: compare group / target killtime / current plan.
        self.cooldown_card_row = ttk.Frame(self.cooldown_frame)
        self.cooldown_card_row.pack(fill="x", pady=_px((0, 10)))
        self.cooldown_compare_var = tk.StringVar(value="비교군 없음")
        self.cooldown_target_var = tk.StringVar(value="목표 킬타임 없음")
        self.cooldown_current_plan_var = tk.StringVar(value="비교 계획 미지정")
        for title, var in (
            ("비교군", self.cooldown_compare_var),
            ("목표 킬타임", self.cooldown_target_var),
            ("현재 비교 계획", self.cooldown_current_plan_var),
        ):
            card = RoundLabelFrame(self.cooldown_card_row, text=title, padding=_px((10, 8)))
            card.pack(side="left", fill="both", expand=True, padx=_px((0, 8)) if title != "현재 비교 계획" else 0)
            ttk.Label(card, textvariable=var, style="CardTitle.TLabel", wraplength=_px(300)).pack(anchor="w")

        # Page 4: result ready / memo-preview view.
        self.cooldown_ready_frame = ttk.Frame(self.cooldown_frame)
        self.cooldown_ready_frame.pack(fill="both", expand=True)

        cd_ready_left = ttk.Frame(self.cooldown_ready_frame)
        cd_ready_left.pack(side="left", fill="both", expand=True, padx=_px((0, 10)))
        cd_ready_right = ttk.Frame(self.cooldown_ready_frame, width=330)
        cd_ready_right.pack(side="right", fill="y")
        cd_ready_right.pack_propagate(False)

        preview_box = RoundLabelFrame(cd_ready_left, text="메모 미리보기", padding=_px((8, 6)))
        preview_box.pack(fill="both", expand=True)
        preview_head = ttk.Frame(preview_box, style="Card.TFrame")
        preview_head.pack(fill="x", pady=_px((0, 5)))
        self.cooldown_preview_label_var = tk.StringVar(value="대표 메모를 선택하세요.")
        self.cooldown_preview_note_var = tk.StringVar(value="")
        ttk.Label(preview_head, textvariable=self.cooldown_preview_label_var, style="CardTitle.TLabel").pack(side="left")
        ttk.Label(preview_head, textvariable=self.cooldown_preview_note_var, style="Card.TLabel").pack(side="right")

        self.cooldown_preview_tree = ttk.Treeview(
            preview_box,
            columns=("phase", "time", "spell", "note"),
            show="headings",
            height=10,
        )
        for col, title, width, anchor in (
            ("phase", "Phase", 58, "center"),
            ("time", "시각", 78, "center"),
            ("spell", "기술", 205, "w"),
            ("note", "메모", 250, "w"),
        ):
            self.cooldown_preview_tree.heading(col, text=title)
            self.cooldown_preview_tree.column(col, width=width, anchor=anchor)
        self.cooldown_preview_tree.pack(side="left", fill="both", expand=True)
        cd_preview_scroll = ttk.Scrollbar(preview_box, orient="vertical", command=self.cooldown_preview_tree.yview)
        cd_preview_scroll.pack(side="right", fill="y")
        self.cooldown_preview_tree.configure(yscrollcommand=cd_preview_scroll.set)

        preview_actions = ttk.Frame(cd_ready_left, padding=_px((0, 8, 0, 0)))
        preview_actions.pack(fill="x")
        _round_button(
            preview_actions, text="이 메모를 비교 계획으로 지정",
            style="Accent.TButton", command=self.set_current_cooldown_plan,
        ).pack(side="left")
        _round_button(preview_actions, text="NSRT 복사", command=self.copy_nsrt).pack(side="left", padx=(8, 0))
        _round_button(preview_actions, text="메모 파일 저장", command=self.save_nsrt_memo_file).pack(side="left", padx=(8, 0))
        _round_button(preview_actions, text="히트맵에서 보기 →", command=self.toggle_cooldown_advanced).pack(side="left", padx=(8, 0))

        plan_box = RoundLabelFrame(cd_ready_right, text="메모 만들기", padding=_px((10, 8)))
        plan_box.pack(fill="both", expand=True)
        ttk.Label(plan_box, text="① 비교군에서 대표 고르기", style="Card.TLabel").pack(anchor="w", pady=_px((0, 6)))
        self.cooldown_plan_choice_var = tk.StringVar(value="")
        self.cooldown_plan_choices_frame = ttk.Frame(plan_box, style="Card.TFrame")
        self.cooldown_plan_choices_frame.pack(fill="x")
        ttk.Separator(plan_box).pack(fill="x", pady=_px(8))
        ttk.Label(plan_box, text="② 또는 기존 메모 불러오기", style="Card.TLabel").pack(anchor="w", pady=_px((0, 5)))
        plan_load = ttk.Frame(plan_box, style="Card.TFrame")
        plan_load.pack(fill="x")
        _round_button(plan_load, text="파일 선택", command=self.load_existing_cooldown_memo).pack(side="left")
        _round_button(plan_load, text="저장된 계획", command=self.load_saved_cooldown_plan).pack(side="left", padx=(6, 0))

        option_box = RoundLabelFrame(cd_ready_right, text="메모 옵션", padding=_px((10, 8)))
        option_box.pack(fill="x", pady=_px((10, 0)))
        self.cooldown_opt_potion_var = tk.BooleanVar(value=False)
        self.cooldown_opt_feint_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            option_box, text="전투 물약 슬롯 포함",
            variable=self.cooldown_opt_potion_var,
            command=lambda: self._toggle_cooldown_option("potion"),
        ).pack(anchor="w")
        ttk.Checkbutton(
            option_box, text="교란 포함 (유틸 · 스냅 안 함)",
            variable=self.cooldown_opt_feint_var,
            command=lambda: self._toggle_cooldown_option("feint"),
        ).pack(anchor="w", pady=_px((4, 0)))
        self.cooldown_intermission_snap_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            option_box, text="인터미션 딜쿨 → 다음 ph 0.0",
            variable=self.cooldown_intermission_snap_var,
            command=lambda: self.render_cooldown_entry(self.cooldown_choice_var.get()),
        ).pack(anchor="w", pady=_px((4, 0)))

        # Page 5: in-progress boss / no plan yet.
        self.cooldown_empty_frame = ttk.Frame(self.cooldown_frame)
        self.cooldown_stage_row = ttk.Frame(self.cooldown_empty_frame)
        self.cooldown_stage_row.pack(fill="x", pady=_px((0, 10)))
        self.cooldown_stage_labels = []
        for idx, (title, sub) in enumerate((
            ("단계 1 · 비교군 수집", "WCL Top30 · API 필요"),
            ("단계 2 · 메모 생성", "대표 실제 로그 선택"),
            ("단계 3 · 확인 · 복사", "NSRT로 게임에 붙여넣기"),
            ("단계 4 · 비교 계획 지정", "다음 트라이 쿨기 비교"),
        )):
            hot = idx == 0
            cbg = _ui_theme.HILITE_BG if hot else _ui_theme.PANEL_BG
            box = RoundBox(self.cooldown_stage_row, fill=cbg, outline=_ui_theme.HILITE_BORDER if hot else _ui_theme.PANEL_BORDER, radius=10, padx=14, pady=12)
            box.pack(side="left", fill="both", expand=True, padx=_px((0, 8)) if idx < 3 else 0)
            card = box.inner
            tk.Label(card, text=f"단계 {idx + 1}", bg=cbg, fg=_ui_theme.ORANGE if hot else _ui_theme.MUTED, font=(FONT_BODY, 9), anchor="w").pack(fill="x")
            lbl = tk.Label(card, text=title.split(" · ", 1)[-1], bg=cbg, fg=_ui_theme.TEXT, font=(FONT_TITLE, 11), anchor="w")
            lbl.pack(fill="x", pady=_px((2, 0)))
            tk.Label(card, text=sub, bg=cbg, fg=_ui_theme.MUTED, font=(FONT_BODY, 9), anchor="w", wraplength=_px(220), justify="left").pack(fill="x", pady=_px((4, 0)))
            self.cooldown_stage_labels.append((card, lbl))

        empty_body = ttk.Frame(self.cooldown_empty_frame)
        empty_body.pack(fill="both", expand=True)
        empty_left = RoundLabelFrame(empty_body, text="비교 계획이 없습니다", padding=_px((12, 10)))
        empty_left.pack(side="left", fill="both", expand=True, padx=_px((0, 10)))
        empty_left._outer.subtitle_var.set("생성에 필요한 것은 비교군 하나")
        self.cooldown_empty_hint_var = tk.StringVar(value="페이즈 정책 미검증")
        for tag, tcolor, tbg, head, text_or_var in (
            ("필요", _ui_theme.ORANGE, _ui_theme.HILITE_BG, "비교군이 아직 없습니다", "메모를 만들려면 WCL에서 이 보스의 상위 로그를 먼저 받아야 해요. 이것만 해결하면 생성할 수 있어요."),
            ("안내", _ui_theme.BLUE, "#16273a", "킬 기록이 없어도 생성할 수 있어요", "목표 킬타임은 비교군 중앙값으로 잡고, 대표 로그가 우리 전투보다 짧으면 후반 경고를 띄웁니다."),
            ("주의", _ui_theme.MUTED, _ui_theme.CARD_BG, "페이즈 정책", self.cooldown_empty_hint_var),
        ):
            row_box = RoundBox(empty_left, fill=tbg, outline=tbg, radius=8, padx=14, pady=10)
            row_box.pack(fill="x", pady=_px((0, 8)))
            r_in = row_box.inner
            tk.Label(r_in, text=tag, bg=tbg, fg=tcolor, font=(FONT_TITLE, 9), width=4, anchor="nw").pack(side="left", anchor="n", pady=_px((2, 0)))
            col = tk.Frame(r_in, bg=tbg)
            col.pack(side="left", fill="x", expand=True)
            tk.Label(col, text=head, bg=tbg, fg=_ui_theme.TEXT, font=(FONT_TITLE, 11), anchor="w").pack(fill="x")
            if isinstance(text_or_var, tk.StringVar):
                tk.Label(col, textvariable=text_or_var, bg=tbg, fg="#c9cfd6", font=(FONT_BODY, 9), anchor="w", justify="left", wraplength=_px(620)).pack(fill="x", pady=_px((2, 0)))
            else:
                tk.Label(col, text=text_or_var, bg=tbg, fg="#c9cfd6", font=(FONT_BODY, 9), anchor="w", justify="left", wraplength=_px(620)).pack(fill="x", pady=_px((2, 0)))
        empty_actions = ttk.Frame(empty_left, style="Card.TFrame")
        empty_actions.pack(anchor="w", pady=_px((6, 0)))
        _round_button(
            empty_actions, text="비교군 수집 시작",
            style="Accent.TButton", command=self.start_cooldown_analysis,
        ).pack(side="left")
        _round_button(empty_actions, text="기존 메모 불러오기", command=self.load_existing_cooldown_memo).pack(side="left", padx=(8, 0))

        empty_right = RoundLabelFrame(empty_body, text="이 보스 트라이", padding=_px((8, 6)))
        empty_right.pack(side="right", fill="y")
        self.cooldown_live_tries_tree = ttk.Treeview(
            empty_right, columns=("result", "duration"), show="tree headings", height=8,
        )
        self.cooldown_live_tries_tree.heading("#0", text="Try")
        self.cooldown_live_tries_tree.heading("result", text="결과")
        self.cooldown_live_tries_tree.heading("duration", text="시간")
        self.cooldown_live_tries_tree.column("#0", width=_px(95), anchor="w")
        self.cooldown_live_tries_tree.column("result", width=_px(72), anchor="center")
        self.cooldown_live_tries_tree.column("duration", width=_px(75), anchor="center")
        self.cooldown_live_tries_tree.tag_configure("excluded", foreground="#747d87")
        self.cooldown_live_tries_tree.pack(fill="both", expand=True)

        # Legacy/detail controls remain intact behind an explicit "히트맵에서 보기"
        # disclosure.  B34.2 changes presentation only; calculation/export code stays.
        self.cooldown_advanced_wrap = RoundLabelFrame(
            self.cooldown_frame, text="상세 히트맵 / 원본 타임라인", padding=(8, 6)
        )
        self.cooldown_advanced_visible = False

        # Lorrgs selector bar: icons still drive the same selected-spell set.
        self.cooldown_selector_bar = tk.Frame(
            self.cooldown_advanced_wrap,
            bg="#171710",
            bd=0,
            padx=_px(8),
            pady=_px(6),
        )
        self.cooldown_selector_bar.pack(fill="x", pady=_px((0, 3)))

        cd_top = ttk.Frame(self.cooldown_advanced_wrap, padding=_px((0, 3, 0, 5)))
        cd_top.pack(fill="x")
        _round_button(
            cd_top, text="← 카드로 돌아가기",
            command=lambda: self.toggle_cooldown_advanced(force=False),
        ).pack(side="left", padx=_px((0, 8)))
        ttk.Label(cd_top, text="타임라인:", font=(FONT_BODY, 10, "bold")).pack(side="left")

        self.cooldown_choice_var = tk.StringVar(value="")
        self.cooldown_combo = ttk.Combobox(
            cd_top,
            textvariable=self.cooldown_choice_var,
            state="readonly",
            width=38,
        )
        self.cooldown_combo.pack(side="left", padx=_px((6, 8)))
        self.cooldown_combo.bind("<<ComboboxSelected>>", self.on_cooldown_choice)

        _round_button(cd_top, text="NSRT 메모 복사", command=self.copy_nsrt).pack(side="left")
        _round_button(cd_top, text="원본/Phase 복사", command=self.copy_mrt_debug).pack(side="left", padx=(6, 0))
        ttk.Label(
            cd_top, text="노란 테두리 = 포함 · 아이콘 클릭으로 ON/OFF", font=(FONT_BODY, 9)
        ).pack(side="left", padx=_px((12, 0)))

        self.cooldown_body_pane = tk.PanedWindow(
            self.cooldown_advanced_wrap,
            orient=tk.VERTICAL,
            sashwidth=6,
            sashrelief=tk.RAISED,
            bd=0,
            relief=tk.FLAT,
            bg="#d6d6d6",
        )
        self.cooldown_body_pane.pack(fill="both", expand=True)

        self.cooldown_heatmap_frame = RoundLabelFrame(
            self.cooldown_body_pane, text="상위 로그 쿨기 히트맵", padding=(6, 4, 6, 4)
        )
        heat_head = ttk.Frame(self.cooldown_heatmap_frame)
        heat_head.pack(fill="x", pady=_px((0, 2)))
        self.cooldown_sample_var = tk.StringVar(value="")
        self.cooldown_sample_label = ttk.Label(
            heat_head, textvariable=self.cooldown_sample_var, font=(FONT_BODY, 9, "bold")
        )
        self.cooldown_sample_label.pack(side="left")
        ttk.Label(
            heat_head,
            text="위=상위권 · 회색=개별 로그 · ○크기=표본 · 띠=분산 · 진한선=선택 로그 · ◆=내 실제",
            font=(FONT_BODY, 8),
        ).pack(side="right")
        self.cooldown_heatmap = tk.Canvas(
            self.cooldown_heatmap_frame,
            height=224,
            highlightthickness=0,
            bg="#fbfbfb",
        )
        self.cooldown_heatmap.pack(fill="both", expand=True)
        self.cooldown_heatmap.bind("<Configure>", lambda _e: self.schedule_cooldown_heatmap_redraw())

        self.cooldown_text_frame = ttk.Frame(self.cooldown_body_pane)
        self.cooldown_text = tk.Text(
            self.cooldown_text_frame,
            wrap="word",
            font=("Consolas", 10),
            padx=_px(14),
            pady=_px(12),
        )
        self.cooldown_text.pack(side="left", fill="both", expand=True)
        cd_scroll = ttk.Scrollbar(
            self.cooldown_text_frame, orient="vertical", command=self.cooldown_text.yview
        )
        cd_scroll.pack(side="right", fill="y")
        self.cooldown_text.configure(yscrollcommand=cd_scroll.set)
        self.cooldown_body_pane.add(getattr(self.cooldown_heatmap_frame, "_outer", self.cooldown_heatmap_frame), minsize=_px(260), stretch="never")
        self.cooldown_body_pane.add(getattr(self.cooldown_text_frame, "_outer", self.cooldown_text_frame), minsize=_px(120), stretch="always")
        self.cooldown_text.tag_configure("title", font=("Consolas", 12, "bold"))
        self.cooldown_text.tag_configure("spell", foreground="#d9b15d", font=("Consolas", 10, "bold"))
        self.cooldown_text.tag_configure("note", foreground="#a7b0ba")
        self.cooldown_text.tag_configure("ok", foreground="#76c67a", font=("Consolas", 10, "bold"))
        self.cooldown_text.tag_configure("caution", foreground="#f5a548", font=("Consolas", 10, "bold"))
        self.cooldown_text.tag_configure("warn", foreground="#ff756f", font=("Consolas", 10, "bold"))
        self.cooldown_text.insert(
            "end",
            "보스 상세에서 보스를 고른 뒤 [비교군 수집 시작]을 누르세요.\n"
            "대표 한 사람의 실제 시퀀스를 메모로 만들고, 지정된 계획만 다음 트라이 비교에 사용합니다.",
            "note",
        )

        # 정밀 분석 — B37.4.2: board-style page (head panel + run action + result panel)
        prec_head = ttk.Frame(self.precision_frame, padding=_px((2, 8, 2, 8)))
        prec_head.pack(fill="x")
        self._b3741_register_tab_host(self.precision_frame, prec_head)
        self.b3742_prec_title_var = tk.StringVar(value="정밀 분석")
        ttk.Label(prec_head, textvariable=self.b3742_prec_title_var, style="Title.TLabel").pack(anchor="w")
        self.b3742_prec_sub_var = tk.StringVar(value="상위30 비교군 대비 내 전투 정밀 비교")
        ttk.Label(prec_head, textvariable=self.b3742_prec_sub_var, style="Muted.TLabel").pack(anchor="w", pady=_px((2, 0)))
        prec_state = RoundPanel(self.precision_frame, "정밀 분석 실행", "선택한 보스 · 내 캐릭터", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        prec_state.pack(fill="x", pady=_px((0, 10)))
        self.b3742_prec_state_var = tk.StringVar(value="")
        state_row = tk.Frame(prec_state.body, bg=_ui_theme.PANEL_BG)
        state_row.pack(fill="x")
        tk.Label(state_row, textvariable=self.b3742_prec_state_var, bg=_ui_theme.PANEL_BG, fg=_ui_theme.TEXT,
                 font=(FONT_BODY, 10), anchor="w", justify="left", wraplength=_px(760)).pack(side="left", fill="x", expand=True)
        self.b3742_prec_run_btn = RoundButton(state_row, text="상위30 정밀 분석 실행", kind="accent", command=self.start_precision_analysis,
                                              font=(FONT_TITLE, 10), outer=_ui_theme.PANEL_BG, pady=6)
        self.b3742_prec_run_btn.pack(side="right")
        # Historical Precision HP1: controls live ONLY in the precision page.
        # Do not add widgets, wheel bindings, or refresh loops to live Try view.
        prec_pick = tk.Frame(prec_state.body, bg=_ui_theme.PANEL_BG)
        prec_pick.pack(fill="x", pady=_px((7, 0)))
        tk.Label(prec_pick, text="이 날짜의 전투", bg=_ui_theme.PANEL_BG, fg=_ui_theme.TEXT,
                 font=(FONT_BODY, 10)).pack(side="left", padx=_px((0, 8)))
        self.b3742_historical_choice_var = tk.StringVar(value="전투 선택…")
        self.b3742_historical_combo = ttk.Combobox(prec_pick, textvariable=self.b3742_historical_choice_var,
                                                   state="readonly", width=78)
        self.b3742_historical_combo.pack(side="left", fill="x", expand=True)
        self.b3742_historical_combo.bind("<<ComboboxSelected>>", self._b37509_precision_choice_changed, add="+")
        self._b37509_precision_context = None
        self._b37509_precision_items = {}
        prec_result = RoundPanel(self.precision_frame, "결과", "기믹·쿨기·딜 대상 비교", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        prec_result.pack(fill="both", expand=True)
        self.precision = tk.Text(
            prec_result.body, wrap="word", font=(FONT_BODY, 10), padx=_px(4), pady=_px(4),
            bg=_ui_theme.PANEL_BG, fg=_ui_theme.TEXT, relief="flat", highlightthickness=0, insertbackground=_ui_theme.TEXT,
        )
        self.precision.pack(side="left", fill="both", expand=True)
        precision_scroll = ttk.Scrollbar(prec_result.body, orient="vertical", command=self.precision.yview)
        precision_scroll.pack(side="right", fill="y")
        self.precision.configure(yscrollcommand=precision_scroll.set)
        self.precision.tag_configure("title", font=(FONT_TITLE, 12), foreground=_ui_theme.TEXT)
        self.precision.tag_configure("issue", foreground=_ui_theme.RED, font=(FONT_BODY, 10, "bold"))
        self.precision.tag_configure("action", foreground=_ui_theme.ORANGE, font=(FONT_BODY, 10, "bold"))
        self.precision.tag_configure("note", foreground=_ui_theme.MUTED)
        self.precision.insert(
            "end",
            "보스 상세에서 분석할 보스를 선택한 뒤 [선택 보스 정밀 분석]을 누르세요.\n\n"
            "처음 한 번은 전체 CombatLog에서 공대의 타깃 활성 구간을 스캔하므로 시간이 걸릴 수 있습니다.\n"
            "이후 같은 로그의 다른 보스는 캐시를 재사용합니다.",
            "note",
        )

        # B34 session overview: page 1 of the approved layout.
        session_head = ttk.Frame(self.coach_frame, padding=_px((2, 8, 2, 8)))
        session_head.pack(fill="x")
        self.session_title_var = tk.StringVar(value="레이드 세션")
        self.session_subtitle_var = tk.StringVar(value="분석된 트라이와 반복 원인을 한 화면에서 봅니다.")
        ttk.Label(session_head, textvariable=self.session_title_var, style="Title.TLabel").pack(anchor="w")
        ttk.Label(session_head, textvariable=self.session_subtitle_var, style="Muted.TLabel").pack(anchor="w", pady=_px((3, 0)))

        # B37.3.9.4: layout board 1 — left = boss panels with try cards,
        # right = 세션 반복 원인 (펼침) + 오늘의 교정 card.
        session_body = tk.Frame(self.coach_frame, bg=_ui_theme.PAGE_BG)
        session_body.pack(fill="both", expand=True)
        session_body.columnconfigure(0, weight=1)
        session_body.columnconfigure(1, weight=0, minsize=_px(320 if self._compact_ui else 360))
        session_body.rowconfigure(0, weight=1)
        left_wrap = tk.Frame(session_body, bg=_ui_theme.PAGE_BG)
        left_wrap.grid(row=0, column=0, sticky="nsew", padx=_px((0, 10)))
        self.b3794_left_canvas = tk.Canvas(left_wrap, bg=_ui_theme.PAGE_BG, highlightthickness=0, yscrollincrement=_px(20))
        left_scroll = ttk.Scrollbar(left_wrap, orient="vertical", command=self.b3794_left_canvas.yview)
        self.b3794_left_canvas.configure(yscrollcommand=left_scroll.set)
        left_scroll.pack(side="right", fill="y")
        self.b3794_left_canvas.pack(side="left", fill="both", expand=True)
        self.b3794_boss_list = tk.Frame(self.b3794_left_canvas, bg=_ui_theme.PAGE_BG)
        self._b3794_list_win = self.b3794_left_canvas.create_window((0, 0), window=self.b3794_boss_list, anchor="nw")
        self.b3794_boss_list.bind("<Configure>", lambda _e: self.b3794_left_canvas.configure(scrollregion=self.b3794_left_canvas.bbox("all")))
        self.b3794_left_canvas.bind("<Configure>", lambda e: self.b3794_left_canvas.itemconfigure(self._b3794_list_win, width=e.width))
        # B37.4.5: the session overview uses its own Canvas rather than a
        # ScrollPage.  Bind at application level so wheel events raised by the
        # embedded boss cards/labels are routed to this Canvas while the pointer
        # is inside the left session column.
        self._b3794_left_wheel_dispatch = install_vertical_wheel_target(
            self.b3794_left_canvas, self.b3794_left_canvas, units_per_step=3
        )

        right_col = tk.Frame(session_body, bg=_ui_theme.PAGE_BG)
        right_col.grid(row=0, column=1, sticky="nsew")
        right_summary = RoundPanel(right_col, "세션 반복 원인", "붕괴 시작점 기준", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        right_summary.pack(fill="x", pady=_px((0, 10)))
        self.b3794_repeat_list = tk.Frame(right_summary.body, bg=_ui_theme.PANEL_BG)
        self.b3794_repeat_list.pack(fill="x")
        self.b3794_repeat_open = None  # None = default (first repeated cause open)

        # Legacy trees stay constructed (navigation/tests/export) but are not shown.
        hidden = tk.Frame(self.coach_frame)
        self.session_try_tree = ttk.Treeview(
            hidden, columns=("boss", "result", "duration", "cause"), show="headings", height=5
        )
        for col, title in (("boss", "보스 / Try"), ("result", "결과"), ("duration", "시간"), ("cause", "핵심 원인")):
            self.session_try_tree.heading(col, text=title)
        self.session_try_tree.bind("<Double-1>", self._open_session_try_from_summary)
        self.session_try_rows = {}
        self.session_repeat_tree = ttk.Treeview(hidden, columns=("count", "detail"), show="tree headings", height=5)

        self.b3794_coach_title_var = tk.StringVar(value="오늘의 교정")
        coach_box = RoundPanel(right_col, "오늘의 교정", "", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        self.b3794_coach_box = coach_box
        coach_box.pack(fill="both", expand=True)
        self.coach = tk.Text(coach_box.body, wrap="word", font=(FONT_BODY, 10), padx=_px(2), pady=_px(2), height=12, width=40, bg=_ui_theme.PANEL_BG, relief="flat", highlightthickness=0)
        self.coach.pack(side="left", fill="both", expand=True)
        coach_scroll = ttk.Scrollbar(coach_box.body, orient="vertical", command=self.coach.yview)
        coach_scroll.pack(side="right", fill="y")
        self.coach.configure(yscrollcommand=coach_scroll.set)
        self.coach.tag_configure("title", font=(FONT_TITLE, 11), foreground="#e9edf2", spacing1=4, spacing3=4)
        self.coach.tag_configure("issue", foreground="#e9edf2", font=(FONT_BODY, 10, "bold"), spacing1=6)
        self.coach.tag_configure("check", foreground="#f5a548", font=(FONT_BODY, 10, "bold"))
        self.coach.tag_configure("action", foreground="#a7b0ba", font=(FONT_BODY, 9), lmargin1=18, lmargin2=18)
        self.coach.tag_configure("good", foreground="#79c99e", font=(FONT_BODY, 9))
        self.coach.tag_configure("note", foreground="#8a929c", font=(FONT_BODY, 8))
        self.b3794_keep_open = False

        # 한눈에 보기
        self.brief = tk.Text(
            self.brief_frame, wrap="word", font=("Consolas", 11), padx=_px(14), pady=12
        )
        self.brief.pack(side="left", fill="both", expand=True)
        bs = ttk.Scrollbar(self.brief_frame, orient="vertical", command=self.brief.yview)
        bs.pack(side="right", fill="y")
        self.brief.configure(yscrollcommand=bs.set)
        self.brief.tag_configure("warn", foreground="#c62828", font=("Consolas", 11, "bold"))
        self.brief.tag_configure("caution", foreground="#ef6c00", font=("Consolas", 11, "bold"))
        self.brief.tag_configure("ok", foreground="#2e7d32", font=("Consolas", 11, "bold"))
        self.brief.tag_configure("note", foreground="#6d4c41")
        self.brief.tag_configure("header", font=("Consolas", 12, "bold"))

        # 보스 상세: 왼쪽 목록 + 오른쪽 상세
        self.boss_frame.columnconfigure(1, weight=1)
        self.boss_frame.rowconfigure(0, weight=1)

        left = ttk.Frame(self.boss_frame, padding=_px((8, 8, 4, 8)))
        left.grid(row=0, column=0, sticky="ns")
        right = ttk.Frame(self.boss_frame, padding=_px((4, 8, 8, 8)))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        ttk.Label(left, text="레이드 보스", font=(FONT_TITLE, 11)).pack(anchor="w", pady=_px((0, 3)))
        ttk.Label(
            left,
            text="정상* = 정상 판정 + 일부 규칙 표본부족 / 확인 = 세션 확인 후보",
            font=(FONT_BODY, 8),
        ).pack(anchor="w", pady=_px((0, 6)))

        self.boss_tree = ttk.Treeview(
            left,
            columns=("diff", "spec", "status"),
            show="tree headings",
            height=24,
            selectmode="browse",
        )
        self.boss_tree.heading("#0", text="보스")
        self.boss_tree.heading("diff", text="난이도")
        self.boss_tree.heading("spec", text="전문화")
        self.boss_tree.heading("status", text="상태")
        self.boss_tree.column("#0", width=_px(205), minwidth=_px(150))
        self.boss_tree.column("diff", width=_px(58), anchor="center")
        self.boss_tree.column("spec", width=_px(58), anchor="center")
        self.boss_tree.column("status", width=_px(72), anchor="center")
        self.boss_tree.pack(side="left", fill="y")
        tree_scroll = ttk.Scrollbar(left, orient="vertical", command=self.boss_tree.yview)
        tree_scroll.pack(side="right", fill="y")
        self.boss_tree.configure(yscrollcommand=tree_scroll.set)
        self.boss_tree.bind("<<TreeviewSelect>>", self.on_boss_select)

        self.boss_title_var = tk.StringVar(value="분석 후 보스를 선택하세요.")
        ttk.Label(
            right,
            textvariable=self.boss_title_var,
            font=(FONT_TITLE, 14),
        ).grid(row=0, column=0, sticky="ew", pady=_px((0, 8)))

        self.boss_detail = tk.Text(
            right, wrap="word", font=("Consolas", 10), padx=_px(12), pady=10
        )
        self.boss_detail.grid(row=1, column=0, sticky="nsew")
        detail_scroll = ttk.Scrollbar(right, orient="vertical", command=self.boss_detail.yview)
        detail_scroll.grid(row=1, column=1, sticky="ns")
        self.boss_detail.configure(yscrollcommand=detail_scroll.set)
        self.boss_detail.tag_configure("warn", foreground="#c62828", font=("Consolas", 10, "bold"))
        self.boss_detail.tag_configure("caution", foreground="#ef6c00", font=("Consolas", 10, "bold"))
        self.boss_detail.tag_configure("ok", foreground="#2e7d32", font=("Consolas", 10, "bold"))
        self.boss_detail.tag_configure("sample", foreground="#8d6e00", font=("Consolas", 10, "bold"))
        self.boss_detail.tag_configure("label", font=("Consolas", 10, "bold"))
        self.boss_detail.tag_configure("note", foreground="#6d4c41")

        # 전체 보고서
        self.result = tk.Text(
            self.result_frame, wrap="word", font=("Consolas", 10), padx=_px(12), pady=10
        )
        self.result.pack(side="left", fill="both", expand=True)
        rs = ttk.Scrollbar(self.result_frame, orient="vertical", command=self.result.yview)
        rs.pack(side="right", fill="y")
        self.result.configure(yscrollcommand=rs.set)
        self.result.tag_configure("warn", foreground="#c62828", font=("Consolas", 10, "bold"))
        self.result.tag_configure("caution", foreground="#ef6c00", font=("Consolas", 10, "bold"))
        self.result.tag_configure("ok", foreground="#2e7d32")
        self.result.tag_configure("small", foreground="#8d6e00")
        self.result.tag_configure("header", font=("Consolas", 11, "bold"))

        # 실행 로그
        self.console = tk.Text(
            self.log_frame, wrap="none", font=("Consolas", 9), padx=_px(10), pady=8
        )
        self.console.pack(side="left", fill="both", expand=True)
        cs = ttk.Scrollbar(self.log_frame, orient="vertical", command=self.console.yview)
        cs.pack(side="right", fill="y")
        self.console.configure(yscrollcommand=cs.set)

    # ------------------------------------------------------------------
    # B36.4.1 · mechanic-first UI projection (screens 6 → 7 → 8)
    # ------------------------------------------------------------------
    def _build_b3641_boss_views(self):
        """Create the two new boss tabs without changing B36.4 judgments."""
        # Screen 8 · 사전 브리핑
        brief = self.boss_briefing_frame
        head = ttk.Frame(brief, padding=_px((4, 8, 4, 8)))
        head.pack(fill="x")
        self._b3741_register_tab_host(brief, head)
        self.b3641_brief_title_var = tk.StringVar(value="사전 브리핑")
        self.b3641_brief_scope_var = tk.StringVar(value="공대 계획 · 판정 기준")
        ttk.Label(head, textvariable=self.b3641_brief_title_var, style="Title.TLabel").pack(anchor="w")
        ttk.Label(head, textvariable=self.b3641_brief_scope_var, style="Muted.TLabel").pack(anchor="w", pady=_px((2, 0)))

        p1 = RoundLabelFrame(brief, text="P1 구슬 분배", padding=_px((8, 7)))
        p1.pack(fill="x", pady=_px((0, 8)))
        self.b3641_brief_p1_vars = []
        card_cols = 2 if self._compact_ui else 4
        for i in range(4):
            cell = tk.Frame(p1, bg="#20262e", highlightbackground="#343b45", highlightthickness=1)
            cell.grid(row=i // card_cols, column=i % card_cols, sticky="nsew", padx=_px(4), pady=_px(4))
            p1.columnconfigure(i % card_cols, weight=1)
            title = tk.StringVar(value="전략 슬롯")
            target = tk.StringVar(value="-")
            actual = tk.StringVar(value="표본 없음")
            tk.Label(cell, textvariable=title, bg="#20262e", fg="#a7b0ba", font=(FONT_BODY, 9)).pack(anchor="w", padx=_px(9), pady=_px((8, 1)))
            tk.Label(cell, textvariable=target, bg="#20262e", fg="#e9edf2", font=(FONT_TITLE, 14)).pack(anchor="w", padx=_px(9))
            tk.Label(cell, textvariable=actual, bg="#20262e", fg="#f5a548", font=(FONT_BODY, 9)).pack(anchor="w", padx=_px(9), pady=_px((2, 8)))
            self.b3641_brief_p1_vars.append((title, target, actual))
        self.b3641_brief_entry_var = tk.StringVar(value="P2 진입 목표 2:45 ~ 2:50")
        ttk.Label(p1, textvariable=self.b3641_brief_entry_var, style="Card.TLabel").grid(
            row=(3 // card_cols) + 1, column=0, columnspan=card_cols, sticky="w", padx=_px(4), pady=(7, 2)
        )

        p2 = RoundLabelFrame(brief, text="P2 배정", padding=_px((8, 7)))
        p2.pack(fill="x", pady=_px((0, 8)))
        self.b3641_brief_interrupt_var = tk.StringVar(value="통곡 차단 순서 · 미설정")
        self.b3641_brief_tanks_var = tk.StringVar(value="탱커 · 미설정")
        self.b3641_brief_bomb_var = tk.StringVar(value="암영폭탄 역할 · 근딜 2 + 원딜 2")
        self.b365_brief_handoff_var = tk.StringVar(value="2단두대 이후 잔여 구슬 · 탱커 처리")
        self.b365_brief_shield_var = tk.StringVar(value="P2 보호막 · +70s / +155s")
        for var in (
            self.b3641_brief_interrupt_var, self.b3641_brief_tanks_var, self.b3641_brief_bomb_var,
            self.b365_brief_handoff_var, self.b365_brief_shield_var,
        ):
            ttk.Label(p2, textvariable=var, style="Card.TLabel", wraplength=_px(880 if not self._compact_ui else 650)).pack(anchor="w", pady=_px(2))

        opening = RoundLabelFrame(brief, text="P2 오프닝 순서", padding=_px((8, 7)))
        opening.pack(fill="x", pady=_px((0, 8)))
        self.b3641_brief_opening = tk.Text(opening, height=7, wrap="word", font=(FONT_BODY, 10), padx=_px(8), pady=_px(6))
        self.b3641_brief_opening.pack(fill="x")
        self.b3641_brief_opening.configure(state="disabled")

        performance = RoundLabelFrame(brief, text="계획 대비 수행", padding=_px((8, 7)))
        performance.pack(fill="both", expand=True)
        self.b3641_brief_performance = tk.Text(performance, height=6, wrap="word", font=("Consolas", 10), padx=_px(8), pady=_px(6))
        self.b3641_brief_performance.pack(fill="both", expand=True)
        self.b3641_brief_performance.configure(state="disabled")

        # Screen 6 · 트라이 보드
        board = self.boss_board_frame
        bhead = ttk.Frame(board, padding=_px((4, 8, 4, 8)))
        bhead.pack(fill="x")
        self._b3741_register_tab_host(board, bhead)
        self.b3641_board_title_var = tk.StringVar(value="트라이 보드")
        self.b3641_board_scope_var = tk.StringVar(value="범위: 현재 세션")
        ttk.Label(bhead, textvariable=self.b3641_board_title_var, style="Title.TLabel").pack(anchor="w")
        # B37.4.1: board 6 shows chips instead of a scope line (var kept for export).

        # B37.3.9.3: layout board 6 — header chips, then two columns:
        # left = 트라이 진척 + 트라이 한눈에, right = 반복 실패 + 다음 풀 전에 말할 것.
        self.b3793_chip_frame = tk.Frame(bhead, bg=_ui_theme.PAGE_BG)
        self.b3793_chip_frame.pack(anchor="w", pady=_px((6, 0)))
        body = tk.Frame(board, bg=_ui_theme.PAGE_BG)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0, minsize=_px(300 if self._compact_ui else 340))
        body.rowconfigure(1, weight=1)
        left_top = RoundPanel(body, "트라이 진척", "", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        left_top.grid(row=0, column=0, sticky="nsew", padx=_px((0, 8)), pady=_px((0, 8)))
        right_top = RoundPanel(body, "반복 실패", "", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        right_top.grid(row=0, column=1, sticky="nsew", pady=_px((0, 8)))
        table_box = RoundPanel(body, "트라이 한눈에", "누르면 트라이 복기", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        table_box.grid(row=1, column=0, sticky="nsew", padx=_px((0, 8)))
        voice = RoundPanel(body, "다음 풀 전에 말할 것", "보이스 오더", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        voice.grid(row=1, column=1, sticky="new")

        self.b3793_progress_note_var = left_top.subtitle_var
        self.b3793_progress_note_var.set("막대 = 전투 길이 · 윗선 = 도달 구간 · 빨간 짧은 선 = 공대 붕괴 시작")
        self.b3641_board_canvas = tk.Canvas(left_top.body, height=_px(150), highlightthickness=0, bg=_ui_theme.PANEL_BG)
        self.b3641_board_canvas.pack(fill="x", expand=True)
        self.b3641_board_canvas.bind("<Configure>", lambda _e: self._draw_b3641_board_progress())
        self.b3793_stage_canvas = tk.Canvas(left_top.body, height=_px(34), highlightthickness=0, bg=_ui_theme.PANEL_BG)
        self.b3793_stage_canvas.pack(fill="x", expand=True, pady=_px((4, 0)))
        self.b3793_stage_canvas.bind("<Configure>", lambda _e: self._draw_b3793_stage_bar())

        self.b3793_repeat_scope_var = right_top.subtitle_var
        self.b3793_repeat_list = tk.Frame(right_top.body, bg=_ui_theme.PANEL_BG)
        self.b3793_repeat_list.pack(fill="both", expand=True)
        # legacy text widget kept off-screen for text export / compatibility
        self.b3641_board_repeat = tk.Text(right_top.body, width=34, height=1)
        self.b3641_board_repeat.configure(state="disabled")

        self.b3641_board_tree = ttk.Treeview(
            table_box.body, columns=("length", "progress", "issue"), show="tree headings", height=11
        )
        self.b3641_board_tree.heading("#0", text="트라이")
        self.b3641_board_tree.heading("length", text="결과 · 길이")
        self.b3641_board_tree.heading("progress", text="도달 구간")
        self.b3641_board_tree.heading("issue", text="핵심 실패 · 붕괴")
        self.b3641_board_tree.column("#0", width=_px(58), anchor="center", stretch=False)
        self.b3641_board_tree.column("length", width=_px(110), anchor="w", stretch=False)
        self.b3641_board_tree.column("progress", width=_px(170) if not self._compact_ui else 140, anchor="w", stretch=False)
        self.b3641_board_tree.column("issue", width=_px(420) if not self._compact_ui else 300, anchor="w")
        for tag, color in (("verified", _ui_theme.STATE_FAILURE), ("candidate", _ui_theme.STATE_CANDIDATE), ("pending", "#c4cbd3"), ("kill", _ui_theme.STATE_SUCCESS), ("early", _ui_theme.RESULT_EARLY_WIPE)):
            self.b3641_board_tree.tag_configure(tag, foreground=color)
        self.b3641_board_tree.pack(side="left", fill="both", expand=True)
        board_scroll = ttk.Scrollbar(table_box.body, orient="vertical", command=self.b3641_board_tree.yview)
        board_scroll.pack(side="right", fill="y")
        self.b3641_board_tree.configure(yscrollcommand=board_scroll.set)
        self.b3641_board_tree.bind("<Double-1>", self._open_b3641_board_try)
        self.b3641_board_tree.bind("<<TreeviewSelect>>", self._open_b3641_board_try)
        self.b3641_board_rows = {}

        self.b3793_voice_list = tk.Frame(voice.body, bg=_ui_theme.PANEL_BG)
        self.b3793_voice_list.pack(fill="x")
        self.b3641_board_voice = tk.Text(voice.body, height=1)
        self.b3641_board_voice.configure(state="disabled")
        self._b3641_board_model = {"rows": [], "stageCounts": [0, 0, 0, 0, 0]}

    def _build_b3641_try_priority_ui(self):
        wrap = ttk.Frame(self.live_frame)
        wrap.pack(fill="x", padx=_px(2), pady=_px((0, 7)))
        self.b3641_try_wrap = wrap
        self.b3641_try_scope_var = tk.StringVar(value="현재 트라이 · 검증된 원인 우선")
        ttk.Label(wrap, textvariable=self.b3641_try_scope_var, style="Muted.TLabel").pack(anchor="w", pady=_px((0, 5)))

        try_nav = ttk.Frame(wrap)
        try_nav.pack(fill="x", pady=_px((0, 6)))
        self.b3642_prev_try_btn = _round_button(try_nav, text="← 이전 트라이", command=lambda: self._move_b3642_try(-1), state="disabled")
        self.b3642_prev_try_btn.pack(side="left")
        self.b3642_next_try_btn = _round_button(try_nav, text="다음 트라이 →", command=lambda: self._move_b3642_try(1), state="disabled")
        self.b3642_next_try_btn.pack(side="left", padx=_px((6, 0)))
        self.b3642_nav_var = tk.StringVar(value="동일 보스·난이도 트라이 이동")
        ttk.Label(try_nav, textvariable=self.b3642_nav_var, style="Muted.TLabel").pack(side="right")

        # B37.3.9.2: layout board 7 — 첫 실수 | 주요 사고 | 붕괴 직결 side by side,
        # then one "→ 다음 풀" line.  Legacy StringVars stay for compatibility.
        self.b3641_first_var = tk.StringVar(value="첫 실수 · 확인 대기")
        self.b3641_major_var = tk.StringVar(value="주요 사고 · 확인 대기")
        self.b3641_collapse_var = tk.StringVar(value="붕괴 직결 · 확인 대기")
        self.b3641_footer_var = tk.StringVar(value="")
        concl_box = RoundBox(wrap, fill=_ui_theme.PANEL_BG, outline=_ui_theme.PANEL_BORDER, radius=10, padx=18, pady=14)
        concl_box.pack(fill="x", pady=_px((0, 10)))
        conclusion = concl_box.inner
        self.b3792_cells = {}
        pbg = _ui_theme.PANEL_BG
        heads = {"first": ("첫 실수", _ui_theme.MUTED), "major": ("주요 사고", _ui_theme.MUTED), "collapse": ("붕괴 직결", _ui_theme.MUTED)}
        for key in ("first", "major", "collapse"):
            name, hcolor = heads[key]
            line = tk.Frame(conclusion, bg=pbg)
            line.pack(fill="x", pady=_px((0, 6)))
            head_var = tk.StringVar(value=name)
            value_var = tk.StringVar(value="확인 대기")
            sub_var = tk.StringVar(value="")
            head = tk.Label(line, textvariable=head_var, bg=pbg, fg=hcolor, font=(FONT_TITLE, 10), width=9, anchor="w")
            head.pack(side="left", anchor="n", pady=(3 if key == "major" else 1, 0))
            value = tk.Label(line, textvariable=value_var, bg=pbg, fg=_ui_theme.TEXT, font=(FONT_TITLE, 15 if key == "major" else 12), anchor="w", justify="left")
            value.pack(side="left", anchor="w")
            sub = tk.Label(line, textvariable=sub_var, bg=pbg, fg=_ui_theme.MUTED, font=(FONT_BODY, 9), anchor="w", justify="left")
            sub.pack(side="left", anchor="s", padx=_px((8, 0)), pady=_px((0, 2)))
            self.b3792_cells[key] = {"frame": line, "head": head, "value": value, "sub": sub, "headVar": head_var, "valueVar": value_var, "subVar": sub_var}
        self.b3792_conclusion = conclusion
        tk.Frame(conclusion, bg=_ui_theme.PANEL_BORDER, height=1).pack(fill="x", pady=_px((4, 8)))
        self.b3792_next_var = tk.StringVar(value="")
        self.b3792_next_label = tk.Label(conclusion, textvariable=self.b3792_next_var, bg=pbg, fg=_ui_theme.TEXT, font=(FONT_BODY, 10), anchor="w", justify="left", wraplength=_px(900))
        self.b3792_next_label.pack(fill="x")
        concl_box.bind("<Configure>", lambda e: self.b3792_next_label.configure(wraplength=max(300, e.width - 60)), add="+")
        self._v35_concl_box = concl_box
        # V4.0 compact summary lives WITHIN the original conclusion RoundBox.
        # Keeping the original top-level widget packed preserves V3.5 positioning.
        self._v40_legacy_pack = [(c, c.pack_info()) for c in conclusion.winfo_children()]
        self.v40_overview_box = tk.Frame(conclusion, bg=pbg)
        _v40_inside = self.v40_overview_box
        _v40_head = tk.Frame(_v40_inside, bg=pbg)
        _v40_head.pack(fill="x", pady=_px((0, 6)))
        tk.Label(_v40_head, text="이번 트라이 · 한눈에 보기", bg=pbg,
                 fg=_ui_theme.TEXT, font=(FONT_TITLE, 13, "bold"), anchor="w").pack(side="left")
        self.v40_toggle_btn = tk.Button(_v40_head, text="판정 상세 펼치기 ▾", relief="flat", bd=0,
            bg=_ui_theme.CARD_BG, fg=_ui_theme.TEXT, activebackground=_ui_theme.HILITE_BG,
            activeforeground=_ui_theme.TEXT, font=(FONT_BODY, 9), padx=_px(8), pady=_px(3),
            command=self._v40_toggle_details)
        self.v40_toggle_btn.pack(side="right")
        self.v40_overview_vars = [tk.StringVar(value="") for _ in range(3)]
        self.v40_overview_labels = []
        # V4.1: a little breathing room makes the three findings scannable.
        for _v40_heading, _v40_value in zip(("무슨 일", "전멸과 관계", "다음 풀"), self.v40_overview_vars):
            _v40_row = tk.Frame(_v40_inside, bg=pbg)
            _v40_row.pack(fill="x", pady=_px((4, 6)))
            tk.Label(_v40_row, text=_v40_heading, bg=pbg, fg=_ui_theme.MUTED,
                font=(FONT_TITLE, 10), anchor="nw", width=10).pack(side="left", anchor="n")
            _v40_lbl = tk.Label(_v40_row, textvariable=_v40_value, bg=pbg,
                fg=_ui_theme.TEXT, font=(FONT_BODY, 11, "bold") if _v40_heading == "다음 풀" else (FONT_BODY, 11),
                anchor="w", justify="left", wraplength=_px(860))
            _v40_lbl.pack(side="left", fill="x", expand=True)
            self.v40_overview_labels.append(_v40_lbl)
        self.v40_overview_box.bind("<Configure>", lambda e: [lbl.configure(
            wraplength=max(250, e.width - _px(170))) for lbl in self.v40_overview_labels], add="+")
        self._v40_visible_key = None
        self._v40_details_open = False
        self._v40_chronology_available = False
        # Summary is packed only for ordinary, identified Wipe rows.

        # V3.5: 내 캐릭터 · 그 순간의 행동 (2~3 lines, read-only, raid analysis stays first).
        self.v35_personal_box = RoundBox(wrap, fill=_ui_theme.PANEL_BG, outline=_ui_theme.PANEL_BORDER, radius=10, padx=18, pady=10)
        _v35_inner = self.v35_personal_box.inner
        tk.Label(_v35_inner, text="내 캐릭터 · 그 순간의 행동", bg=_ui_theme.PANEL_BG, fg=_ui_theme.TEXT,
                 font=(FONT_TITLE, 11), anchor="w").pack(fill="x", pady=_px((0, 5)))
        self.v35_personal_vars = [tk.StringVar(value="") for _ in range(3)]
        self.v35_personal_labels = []
        for _v35_var in self.v35_personal_vars:
            _v35_lbl = tk.Label(_v35_inner, textvariable=_v35_var, bg=_ui_theme.PANEL_BG, fg=_ui_theme.TEXT,
                                font=(FONT_BODY, 10), anchor="w", justify="left", wraplength=_px(900))
            _v35_lbl.pack(fill="x", pady=_px((2, 3)))
            self.v35_personal_labels.append(_v35_lbl)
        self.v35_personal_box.bind("<Configure>", lambda e: [l.configure(wraplength=max(300, e.width - 60)) for l in self.v35_personal_labels], add="+")
        # packed only when a Try is shown (see _v35_update_personal_card)

        # V3.7: observed raid chronology, strictly independent of the four raid verdicts.
        self.v37_raid_time_box = RoundBox(wrap, fill=_ui_theme.PANEL_BG, outline=_ui_theme.PANEL_BORDER, radius=10, padx=18, pady=10)
        _v37_inside = self.v37_raid_time_box.inner
        tk.Label(_v37_inside, text="공대 사고 · 어떤 순서로 일어났나", bg=_ui_theme.PANEL_BG,
                 fg=_ui_theme.MUTED, font=(FONT_TITLE, 10), anchor="w").pack(fill="x", pady=_px((0, 2)))
        self.v37_raid_time_vars = [tk.StringVar(value="") for _ in range(3)]
        self.v37_raid_time_labels = []
        for _v37_var in self.v37_raid_time_vars:
            _v37_label = tk.Label(_v37_inside, textvariable=_v37_var, bg=_ui_theme.PANEL_BG,
                                  fg=_ui_theme.TEXT, font=(FONT_BODY, 9), anchor="w",
                                  justify="left", wraplength=_px(900))
            _v37_label.pack(fill="x")
            self.v37_raid_time_labels.append(_v37_label)
        self.v37_raid_time_box.bind("<Configure>",
            lambda e: [x.configure(wraplength=max(300, e.width - 60)) for x in self.v37_raid_time_labels], add="+")
        # Deliberately not packed until exact combat identity is established.

        cards_box = RoundPanel(wrap, "기믹 수행 체크", "✓ 충족 · ✗ 실패 · ! 주의 · ? 판단 못 함 · · 관측만 · – 이 난이도엔 없음", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        self.b3739_cards_box = cards_box
        cards_body = cards_box.body
        cards_box.pack(fill="x", pady=_px((0, 8)))
        self.b3641_try_cards = {}
        card_cols = 2 if self._compact_ui else 5
        card_keys = [
            "p2_entry", "p1_orbs", "p1_guillotine_soak", "mind_control", "gravebound", "gloombomb_add",
            "gloombomb_extra", "wail", "manifest", "soul_sever", "prior_add",
            "p2_entry_orbs",
        ]
        for i, key in enumerate(card_keys):
            frame = RoundBox(cards_body, fill=_ui_theme.CARD_BG, outline=_ui_theme.CARD_BORDER, radius=8, padx=10, pady=8)
            frame.grid(row=i // card_cols, column=i % card_cols, sticky="nsew", padx=_px(4), pady=_px(4))
            cards_body.columnconfigure(i % card_cols, weight=1, uniform="b3795cards")
            parent_frame = frame
            frame = frame.inner
            title_var = tk.StringVar(value=key)
            value_var = tk.StringVar(value="?")
            note_var = tk.StringVar(value="")
            title = tk.Label(frame, textvariable=title_var, bg="#20262e", fg="#a7b0ba", font=(FONT_BODY, 9), anchor="w", justify="left")
            title.pack(fill="x", padx=_px(7), pady=_px((6, 1)))
            value = tk.Label(frame, textvariable=value_var, bg="#20262e", fg="#e9edf2", font=(FONT_BODY, 11, "bold"), anchor="w", justify="left")
            value.pack(fill="x", padx=_px(7), pady=_px((0, 6)))
            # B37.4.36: mechanic cards are deliberately two visible lines.
            # Full note text remains in the folded 판정 근거 block.
            note = tk.Label(frame, textvariable=note_var, bg="#20262e", fg="#a7b0ba", font=(FONT_BODY, 7), anchor="w", justify="left", wraplength=_px(190 if not self._compact_ui else 290))
            frame = parent_frame
            self.b3641_try_cards[key] = {"frame": frame, "title": title, "value": value, "note": note, "titleVar": title_var, "valueVar": value_var, "noteVar": note_var}

        shield_box = RoundPanel(wrap, "P2 보호막 · 영원한 일몰 / 보호막 쿨기 정렬", "", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        self.b365_shield_box = shield_box
        shield_box.pack(fill="x", pady=_px((0, 8)))
        self.b365_shield_panel = tk.Text(shield_box.body, height=5, wrap="word", font=(FONT_BODY, 9), padx=_px(2), pady=_px(2), bg=_ui_theme.PANEL_BG, relief="flat", highlightthickness=0)
        self.b365_shield_panel.pack(fill="x")
        self.b365_shield_panel.configure(state="disabled")

        timeline_box = RoundPanel(wrap, "기믹 시간축", "아이콘=기믹 발생 · 세로선=페이즈 · 띠=지속 시간/공대 쿨기 · 표식=판정", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        self.b3739_timeline_box = timeline_box
        timeline_box.pack(fill="x", pady=_px((0, 8)))
        # B37.5.0: keep the legacy row timeline available until glyph parity is
        # proven across stored logs.  The compact glyph view is the default.
        self.b3641_timeline_mode = tk.StringVar(value="스킬별")
        timeline_mode_row = tk.Frame(timeline_box.body, bg=_ui_theme.PANEL_BG)
        timeline_mode_row.pack(fill="x", pady=_px((0, 4)))
        for _label, _value in (("스킬별", "스킬별"), ("한 줄", "아이콘"), ("상세(기존)", "상세")):
            _btn = tk.Button(
                timeline_mode_row, text=_label, relief="flat", bd=0,
                bg=_ui_theme.CARD_BG, fg=_ui_theme.TEXT,
                activebackground=_ui_theme.HILITE_BG, activeforeground=_ui_theme.TEXT,
                font=(FONT_BODY, 8), padx=_px(8), pady=_px(2),
                command=lambda v=_value: self._set_b3750_timeline_mode(v),
            )
            _btn.pack(side="left", padx=_px((0, 4)))
        # Preview4: a compact spell-reference strip owns the long mechanic
        # description. Timeline hover stays intentionally terse.
        self.b3750_skill_ref_canvas = tk.Canvas(timeline_box.body, height=_px(40), highlightthickness=0, bg=_ui_theme.PANEL_BG)
        self.b3750_skill_ref_canvas.pack(fill="x", pady=_px((0, 3)))
        self.b3750_skill_ref_canvas.bind("<Configure>", lambda _e: self._draw_b3641_try_timeline())
        self.b3641_try_timeline = tk.Canvas(timeline_box.body, height=_px(190), highlightthickness=0, bg=_ui_theme.PANEL_BG)
        self.b3641_try_timeline.pack(fill="x", expand=True)
        self.b3641_try_timeline.bind("<Configure>", lambda _e: self._draw_b3641_try_timeline())
        self._b3641_try_model = {"timeline": []}
        self._b3641_try_review = {}
        self.b3641_timeline_tooltip = None
        self.b3750_tooltip_hide_after_id = None
        # STEP4.5: remember the canvas item that owns the visible tooltip.
        # Mapping an override-redirect Toplevel can generate a synthetic Leave/Enter
        # pair on Windows even when the pointer never moved.  Source tracking lets
        # us ignore that pair instead of destroying/recreating the tooltip.
        self.b3750_tooltip_source_canvas = None
        self.b3750_tooltip_source_item = None
        self.b3750_tooltip_source_key = None
        self.b3750_timeline_icon_refs = []
        # B37.5.0 Preview3.1: dense judgment text defaults to a five-row
        # screenshot-friendly summary. Expansion is per selected Try.
        self._b3750_judgments_expanded = False
        self._b3750_judgment_try_key = None

        evidence_box = RoundPanel(wrap, "판정 근거 · 한계", "", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        self.b3641_evidence_box = evidence_box
        self.b3739_evidence_box = evidence_box
        evidence_box.pack(fill="x", pady=_px((0, 6)))
        self.b3641_try_evidence = tk.Text(evidence_box.body, height=4, wrap="word", font=(FONT_BODY, 9), padx=_px(2), pady=_px(2), bg=_ui_theme.PANEL_BG, relief="flat", highlightthickness=0)
        self.b3641_try_evidence.pack(fill="x")
        self.b3641_try_evidence.configure(state="disabled")

        compare_box = RoundPanel(wrap, "최근 킬 / 현재 트라이 · 비교", "", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        self.b3642_compare_box = compare_box
        self.b3739_compare_box = compare_box
        compare_box.pack(fill="x", pady=_px((0, 6)))
        self.b3642_compare_text = tk.Text(compare_box.body, height=7, wrap="word", font=(FONT_BODY, 9), padx=_px(2), pady=_px(2), bg=_ui_theme.PANEL_BG, relief="flat", highlightthickness=0)
        self.b3642_compare_text.pack(fill="x")
        self.b3642_compare_text.configure(state="disabled")

        self.b3739_evidence_open = False
        # B37.4.36.1: three-tier depth.  Mechanic cards / shield / timeline are
        # the 10-second review (tier 2) and are always shown above the fold.
        # Only tier-3 evidence (판정 근거 · 킬 비교) lives behind the button.
        self.b3836_always_boxes = []
        self.b3739_evidence_boxes = [evidence_box, compare_box]
        for _box in (cards_box, shield_box, timeline_box, evidence_box, compare_box):
            _box.pack_forget()
        self.b3739_evidence_btn = _round_button(
            wrap, text="판정 근거 · 킬 비교 펼치기 ▸", command=self._toggle_b3739_evidence
        )
        self.b3739_evidence_btn.pack(fill="x", pady=_px((0, 10)))

        self.b3641_detail_open = False
        self.b3641_detail_btn = _round_button(
            wrap, text="상세 복기 · B31 원본 / 개인 사망 / 쿨기 비교 ▸", command=self._toggle_b3641_detail
        )
        # B37.3.9.7: detail is always shown; button kept off-screen for compatibility.

        # B37.3.9.6: board 3 — 붕괴 체인 (time column + step cards) + 다음 트라이 / 연결.
        self.b3796_chain_wrap = tk.Frame(self.b3641_try_wrap.master, bg=_ui_theme.PAGE_BG)
        self.b3796_chain_wrap.columnconfigure(0, weight=1)
        self.b3796_chain_wrap.columnconfigure(1, weight=0, minsize=_px(320))
        self.b3796_chain_panel = RoundPanel(self.b3796_chain_wrap, "붕괴 체인", "연결 근거가 있는 사건만", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        self.b3796_chain_panel.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=_px((0, 10)))
        side = tk.Frame(self.b3796_chain_wrap, bg=_ui_theme.PAGE_BG)
        side.grid(row=0, column=1, sticky="new")
        self.b3796_next_panel = RoundPanel(side, "다음 트라이", "", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        self.b3796_next_panel.pack(fill="x", pady=_px((0, 10)))
        self.b3796_next_var = tk.StringVar(value="")
        tk.Label(self.b3796_next_panel.body, textvariable=self.b3796_next_var, bg=_ui_theme.PANEL_BG, fg=_ui_theme.TEXT, font=(FONT_BODY, 10), anchor="w", justify="left", wraplength=_px(280)).pack(fill="x")
        self.b3796_link_panel = RoundPanel(side, "연결", "", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        self.b3796_link_panel.pack(fill="x")

    @staticmethod
    def _b3641_status_colors(status):
        return {
            "success": ("#173825", "#79c99e", "✓"),
            "failed": ("#4a2222", "#ff756f", "✗"),
            "warning": ("#49331c", "#f5a548", "!"),
            "unknown": ("#252b33", "#a7b0ba", "?"),
        }.get(str(status or "unknown"), ("#252b33", "#a7b0ba", "?"))

    def _set_b3641_card(self, card):
        target = self.b3641_try_cards.get(card.get("key"))
        if not target:
            return
        target["frame"].grid()
        muted_for_difficulty = bool(card.get("mutedForDifficulty"))
        bg, fg, icon = self._b3641_status_colors(card.get("status"))
        _card_status = str(card.get("status") or "unknown")
        _raw_status = str(card.get("rawStatus") or "")
        _evidence = str(card.get("evidenceLevel") or "")
        _observed_unpromoted = (
            _raw_status == "observed"
            or (_evidence == "candidate" and _raw_status in {"success", "failed", "warning"})
        )
        if muted_for_difficulty:
            # Difficulty-inactive cards are informational, never failures.
            bg, fg, icon = _ui_theme.CARD_BG, _ui_theme.STATE_INACTIVE, "–"
        elif _card_status == "unknown" and _observed_unpromoted:
            # B37.4.4: an observed-but-unpromoted mechanic is not "판정 불가".
            # Candidate meaning stays neutral gray; pure observation also stays neutral.
            icon = "·"
            fg = _ui_theme.STATE_CANDIDATE if _evidence == "candidate" else "#c4cbd3"
        decided = (not muted_for_difficulty) and _card_status in {"success", "failed", "warning"}
        target["frame"].configure(bg=bg, highlightbackground=fg if decided else _ui_theme.CARD_BORDER)
        target["title"].configure(bg=bg, fg=_ui_theme.STATE_INACTIVE if muted_for_difficulty else "#c4cbd3")
        target["value"].configure(bg=bg, fg=fg)
        target["note"].configure(bg=bg, fg="#a7b0ba")
        target["titleVar"].set(card.get("title") or "-")
        target["valueVar"].set(f"{card.get('value') or '-'}   {icon}")
        target["noteVar"].set(card.get("note") or "")

    def _apply_b3739_evidence_state(self):
        boxes = list(getattr(self, "b3739_evidence_boxes", []) or [])
        btn = getattr(self, "b3739_evidence_btn", None)
        if btn is None:
            return
        always = list(getattr(self, "b3836_always_boxes", []) or [])
        for box in always + boxes:
            box.pack_forget()
        # tier 2 (cards · shield · timeline) always above the button
        for box in always:
            box.pack(fill="x", pady=_px((0, 10)), before=btn)
        if bool(getattr(self, "b3739_evidence_open", False)):
            prev = btn
            for box in boxes:
                box.pack(fill="x", pady=_px((0, 10)), after=prev)
                prev = box
            btn.configure(text="판정 근거 접기 ▾")
        else:
            btn.configure(text="판정 근거 · 킬 비교 펼치기 ▸")

    def _toggle_b3739_evidence(self):
        self.b3739_evidence_open = not bool(getattr(self, "b3739_evidence_open", False))
        # V4.2 TRY-SPEED: The comparison is not needed until the user expands
        # the evidence pane.  Populate it *before* displaying the pane, using
        # the selected Try and its already-loaded review.
        if (self.b3739_evidence_open
                and getattr(self, "b3739_compare_box", None) in getattr(self, "b3739_evidence_boxes", ())):
            row = self._selected_live_row()
            if row:
                self._render_b3642_compare(row, getattr(self, "_b3641_try_review", {}) or {})
        self._apply_b3739_evidence_state()

    def _toggle_b3641_detail(self):
        box = getattr(self, "live_facts_box", None)
        if box is None:
            return
        self.b3641_detail_open = not bool(getattr(self, "b3641_detail_open", False))
        chain = getattr(self, "b3796_chain_wrap", None)
        if self.b3641_detail_open:
            if chain is not None:
                chain.pack(fill="x", padx=_px(2), pady=_px((4, 8)), after=self.b3641_try_wrap)
                box.pack(fill="both", expand=True, padx=_px(2), pady=_px((4, 8)), after=chain)
            else:
                box.pack(fill="both", expand=True, padx=_px(2), pady=_px((4, 8)), after=self.b3641_try_wrap)
            self.b3641_detail_btn.configure(text="상세 복기 · B31 원본 / 개인 사망 / 쿨기 비교 ▾")
        else:
            if chain is not None:
                chain.pack_forget()
            box.pack_forget()
            self.b3641_detail_btn.configure(text="상세 복기 · B31 원본 / 개인 사망 / 쿨기 비교 ▸")

    def _b3641_context_rows(self, *, all_dates=False):
        enc = self.nav_state.get("encounterID")
        diff = self.nav_state.get("difficultyID")
        if enc is None:
            return []
        date = self._nav_session_date()
        out = []
        for row in self._all_live_rows():
            try:
                if int(row.get("encounterID") or -1) != int(enc):
                    continue
                if diff is not None:
                    row_diff = int(row.get("difficultyID") or -1)
                    nav_diff = int(diff)
                    # WCL Mythic (5), raw Mythic (16) and Flexible Mythic
                    # Nymrissa (233) are one raid difficulty for navigation.
                    if normalize_difficulty(row_diff) != normalize_difficulty(nav_diff):
                        continue
            except Exception:
                continue
            if not all_dates:
                row_date = session_date_from_timestamp(row.get("startTimestamp")) or str(row.get("sessionDate") or "")
                if date and row_date and row_date != date:
                    continue
            if self._live_row_is_fake(row) or self._live_row_is_reset(row):
                continue
            out.append(row)
        # B36.4.2: one physical pull may exist as both a WCL copy (difficulty 5)
        # and a local raw capture (difficulty 16).  Aggregate/navigation views
        # must count it once; when both exist canonicalize_rows() prefers the
        # richer local raw representative.
        return canonicalize_rows(out)

    def _v42_p2_reached_labels(self):
        """Memoize a display-only P2 label list, never raw reviews or verdicts.

        V4.1 load_mechanic_review deliberately returns deepcopy snapshots for
        safety. Repeating that for 40+ pulls on each Try click is unnecessary.
        File fingerprints follow the V4.1 cache's metadata policy and check
        both possible file sources, including missing-to-created transitions.
        Mutable inline reviews are never memoized.
        """
        rows = self._b3641_context_rows(all_dates=False)
        signature = None
        try:
            reuse_safe = True
            row_stamps = []
            for r in rows:
                if isinstance(r.get("mechanicReview"), dict):
                    reuse_safe = False
                paths = []
                if r.get("mechanicReviewJson"):
                    paths.append(str(r["mechanicReviewJson"]))
                if r.get("storageDir"):
                    paths.append(str(Path(str(r["storageDir"])) / "mechanic_review.json"))
                stamps = []
                for raw_path in paths:
                    try:
                        st = Path(raw_path).stat()
                        stamp = (st.st_mtime_ns, getattr(st, "st_ctime_ns", 0),
                                 st.st_size, st.st_dev, st.st_ino)
                    except OSError:
                        stamp = None
                    stamps.append((raw_path, stamp))
                row_stamps.append((self._live_row_key(r), bool(r.get("mechanicReviewStale")), tuple(stamps)))
            if reuse_safe:
                signature = (self._nav_context_key(), self._nav_session_date(),
                             getattr(self, "_live_storage_revision", None),
                             tuple(row_stamps))
                memo = getattr(self, "_v42_p2_reached_memo", None)
                if memo is not None and memo[0] == signature:
                    return list(memo[1])
        except Exception:
            signature = None  # Conservative: never skip reads on uncertain state.
        reached = []
        for r in rows:
            rev = load_mechanic_review(r)
            if (((rev.get("observations") or {}).get("phase2") or {}).get("reached")):
                reached.append(str(r.get("tryNo") or "?"))
        self._v42_p2_reached_memo = (signature, tuple(reached)) if signature is not None else None
        return reached

    def _b3641_review_pairs(self, *, all_dates=False):
        pairs = []
        for row in self._b3641_context_rows(all_dates=all_dates):
            review = load_mechanic_review(row)
            if review:
                pairs.append((row, review))
        return pairs

    def _refresh_b3641_boss_views(self):
        """Refresh the compact boss board and briefing for the selected boss.

        B37.3.9 makes the Try board boss-agnostic. Boss-specific deep strategy
        remains optional, but the primary board must work for every registered
        encounter and must never keep data from the previously selected boss.
        """
        if not hasattr(self, "b3641_board_tree"):
            return
        try:
            enc = int(self.nav_state.get("encounterID") or 0)
        except Exception:
            enc = 0

        boss_name = registry_boss_name(enc, "선택 보스")
        try:
            idx = self._find_boss_index_by_context(self._nav_context_key())
            if idx is not None:
                boss_name = str(self.boss_data[idx].get("boss") or boss_name)
        except Exception:
            pass

        session_date = self._nav_session_date() or "현재 세션"
        pairs = self._b3641_review_pairs(all_dates=False)
        all_pairs = self._b3641_review_pairs(all_dates=True)
        try:  # 1.0.6 (GOLD V5.2.9): 엔진 기믹 판정이 없는 보스(키식스 등)도 결론 5줄로 보드에 보인다
            from live.v529_board import add_guide_pairs as _v529_pairs
            pairs = _v529_pairs(pairs, self._b3641_context_rows(all_dates=False))
        except Exception:
            pass

        # B37.3.9.8: skip the rebuild (and flicker) when nothing shown changed.
        _pre_sig = ("board-pre", enc, session_date, tuple(self._b3798_row_signature(r) for r, _rev in pairs or []))
        if _pre_sig == getattr(self, "_b3798_board_pre_sig", None) and self.b3641_board_tree.get_children():
            return
        self._b3798_board_pre_sig = _pre_sig

        # Primary board: one line per pull, one reason, one next-pull direction.
        for iid in self.b3641_board_tree.get_children():
            self.b3641_board_tree.delete(iid)
        self.b3641_board_rows = {}

        model = build_generic_board_summary(pairs)
        try:  # 1.0.6 (GOLD V5.2.9)
            from live.v529_board import augment_board as _v529_aug
            model = _v529_aug(model)
        except Exception:
            pass
        self._b3641_board_model = model
        diff_name = ""
        for _r, _rev in pairs or []:
            diff_name = _ui_difficulty_name(_r).strip()
            if diff_name:
                break
        # B37.3.9.3: board 6 title = 보스 · 난이도; the scope line is replaced by chips.
        self.b3641_board_title_var.set(f"{boss_name} · {diff_name}" if diff_name else str(boss_name))
        self.b3641_board_scope_var.set("")

        self._render_b3793_board(model, pairs, session_date)
        self._draw_b3641_board_progress()

        # Briefing is still Coiled-Altar-specific. Other bosses must be cleared
        # rather than inheriting stale Coiled strategy content.
        if enc != 3429:
            self._set_unsupported_b3641_briefing(boss_name)
            return

        reviews = [r for _, r in all_pairs]
        brief_model = build_briefing_model(reviews)
        self.b3641_brief_title_var.set(f"{boss_name} · 신화")
        self.b3641_brief_scope_var.set(
            f"공대 계획 · 판정 기준으로 사용 · 범위: 전체 누적 · n={brief_model.get('reviewCount', 0)}"
        )
        for vars_, card in zip(self.b3641_brief_p1_vars, brief_model.get("cards") or []):
            title, target, actual = vars_
            title.set(card.get("label") or "전략 슬롯")
            target.set(card.get("target") or "-")
            med = card.get("median")
            actual.set("표본 없음" if med is None else f"실제 중앙 {med:g} · {card.get('samples', 0)}표본")
        self.b3641_brief_entry_var.set(f"P2 진입 목표 {brief_model.get('p2Target')} · 2:45 시점 줄잔 체력 같이 표시")
        ints = brief_model.get("interruptNames") or []
        self.b3641_brief_interrupt_var.set("통곡 차단 순서 · " + (" → ".join(b3641_short_player(x) for x in ints) if ints else "미설정 · raidPlan 이름 입력 시 표시"))
        tanks = brief_model.get("tanks") or []
        tank_source = str(brief_model.get("tankSource") or "")
        tank_source_label = "raidPlan 우선" if tank_source == "manual_raidplan" else "로스터 spec 자동 추론" if tank_source == "roster_spec" else "미확정"
        self.b3641_brief_tanks_var.set(
            "탱커 · "
            + (", ".join(b3641_short_player(x) for x in tanks) if tanks else "미설정")
            + f" · {tank_source_label}"
        )
        self.b3641_brief_bomb_var.set(f"암영폭탄 역할 · {brief_model.get('bombRole')} · 영혼살무사 99% 감소 해제 · 영혼 3개는 아직 확인 불가")
        hmax = brief_model.get("tankHandoffMax")
        self.b365_brief_handoff_var.set(
            "2단두대 이후 잔여 구슬 · 탱커 처리"
            + (f" · 권장 상한 {int(hmax)}개" if hmax is not None else " · 상한 미설정")
        )
        shield_bits = []
        for sw in brief_model.get("shieldWindows") or []:
            idx = sw.get("index") or "?"
            off = sw.get("targetOffsetSec")
            if off is not None:
                shield_bits.append(f"#{idx} P2 +{float(off):g}s")
        judge_count = int(brief_model.get("autoJudgeCount") or 0)
        observe_count = int(brief_model.get("autoObserveCount") or 0)
        auto_bits = []
        if judge_count:
            auto_bits.append(f"자동 raidPlan judge {judge_count}개")
        if observe_count:
            auto_bits.append(f"관측 {observe_count}개")
        self.b365_brief_shield_var.set(
            "P2 보호막 · " + (" / ".join(shield_bits) if shield_bits else "raidPlan 미설정")
            + " · 블러드/물약/지정 쿨기"
            + ((" · " + " / ".join(auto_bits)) if auto_bits else "")
        )
        opening = "\n".join(f"{i}. {line}" for i, line in enumerate(brief_model.get("opening") or [], 1))
        self._replace_readonly_text(self.b3641_brief_opening, opening)
        perf = []
        for card in brief_model.get("cards") or []:
            med = card.get("median")
            if med is not None:
                perf.append(f"{card.get('label')}: 목표 {card.get('target')} / 중앙 {med:g} ({card.get('samples')}표본)")
        self._replace_readonly_text(self.b3641_brief_performance, "\n".join(perf) if perf else "아직 누적 기믹 표본이 없습니다.")

    def _set_unsupported_b3641_briefing(self, boss_name: str):
        """Clear Coiled-Altar strategy defaults when another boss is selected."""
        self.b3641_brief_title_var.set(f"{boss_name} · 사전 브리핑")
        self.b3641_brief_scope_var.set("이 보스 전용 사전 브리핑은 아직 준비되지 않았습니다.")
        for slot, vars_ in enumerate(self.b3641_brief_p1_vars, 1):
            title, target, actual = vars_
            title.set(f"전략 슬롯 {slot}")
            target.set("적용 안 함")
            actual.set("현재 선택 보스 전용 브리핑 미지원")
        self.b3641_brief_entry_var.set(f"{boss_name} 전용 페이즈 목표 · 준비 중")
        self.b3641_brief_interrupt_var.set("차단 계획 · 이 보스 전용 브리핑에서 아직 미지원")
        self.b3641_brief_tanks_var.set("탱커 계획 · 이 보스 전용 브리핑에서 아직 미지원")
        self.b3641_brief_bomb_var.set("공대 역할 · 이 보스 전용 브리핑에서 아직 미지원")
        self.b365_brief_handoff_var.set("페이즈 인계 · 이 보스 전용 브리핑에서 아직 미지원")
        self.b365_brief_shield_var.set("쿨기 계획 · 보스 개요 / 쿨기·NSRT 탭을 사용하세요")
        self._replace_readonly_text(
            self.b3641_brief_opening,
            f"{boss_name} 전용 사전 브리핑은 아직 구현되지 않았습니다.\n"
            "이전 보스(똬리의 제단)의 전략 내용은 표시하지 않습니다.\n"
            "현재 보스의 기믹 판정·정밀 분석·쿨기/NSRT 결과는 각 전용 탭에서 확인하세요.",
        )
        self._replace_readonly_text(
            self.b3641_brief_performance,
            "현재 선택 보스 전용 누적 브리핑 표본은 아직 지원하지 않습니다.",
        )

    @staticmethod
    def _replace_readonly_text(widget, text):
        try:
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("end", text)
            widget.configure(state="disabled")
        except Exception:
            pass

    _B3793_STAGE_COLORS = ["#747d87", "#f5a548", "#e07a5f", "#78b9eb", "#79c99e"]

    @staticmethod
    def _b3793_stage_label(text):
        t = str(text or "").strip()
        for sep in (" · 종료", " · "):
            if sep in t:
                t = t.split(sep, 1)[0]
        return t or "-"

    def _b3793_clear(self, frame):
        for w in frame.winfo_children():
            w.destroy()

    def _b3793_issue(self, item, row):
        """Board issue cell = verified failure, else B31 collapse line tagged 원인 확인 중."""
        result = str(item.get("result") or "")
        issue = str(item.get("issue") or item.get("keyFailure") or "").strip()
        if result in {"킬", "Kill"}:
            return "킬 · 공대 붕괴 없음", "kill"
        unresolved = (not issue) or issue in {"확정 원인 없음", "확인 불가"} or issue.startswith("원인 확인 불가")
        if not unresolved:
            tag = "candidate" if "후보" in issue else "verified"
            return issue, tag
        if issue.startswith("원인 확인 불가"):
            # B31 collapse cluster text without a verified mechanic → neutral, not red.
            return issue.replace("원인 확인 불가 · ", "", 1) + " · [원인 확인 중]", "pending"
        b31 = {}
        try:
            b31 = self._review_model_for_row(row) or {} if row else {}
        except Exception:
            b31 = {}
        if str((row or {}).get("earlyWipe") or "") == "True" or (item.get("durationSec") or 999) < 30:
            base = b31.get("rootMechanic") or "초반 전멸"
            return f"{base} · [Reset]", "early"
        if b31.get("rootMechanic"):
            t = b31.get("rootTime")
            at = f"{self._fmt_live_relative(t)} " if t is not None else ""
            return f"{at}{b31.get('rootMechanic')} · [원인 확인 중]", "pending"
        return "원인 확인 중", "pending"

    def _render_b3793_board(self, model, pairs, session_date):
        rows = list(model.get("rows") or [])
        sig = ("board", session_date, tuple(self._b3798_row_signature(r) for r, _rev in pairs or []),
               tuple((x.get("tryNo"), x.get("issue"), x.get("progress"), x.get("durationSec")) for x in rows))
        if sig == getattr(self, "_b3798_board_sig", None) and self.b3641_board_tree.get_children():
            return
        self._b3798_board_sig = sig
        row_by_try = {}
        for r, _rev in pairs or []:
            row_by_try[str(r.get("tryNo"))] = r
        # Header chips: 오늘 N트 · 최고 도달 · 트라이 중앙
        self._b3793_clear(self.b3793_chip_frame)
        chips = [f"{session_date} · {len(rows)}트"]
        kills = [r for r in rows if str(r.get("result")) in {"킬", "Kill"}]
        if kills:
            chips.append(f"킬 {len(kills)}")
        wipes = [r for r in rows if str(r.get("result")) not in {"킬", "Kill"}]
        if wipes:
            best = max(wipes, key=lambda r: (int(r.get("stageIndex") or 0), float(r.get("durationSec") or 0)))
            chips.append(f"와이프 최고 도달 · {best.get('progress') or best.get('stage') or '-'} · #{best.get('tryNo')}")
        durs = sorted(float(r.get("durationSec") or 0) for r in rows if r.get("durationSec"))
        if durs:
            mid = durs[len(durs) // 2] if len(durs) % 2 else (durs[len(durs) // 2 - 1] + durs[len(durs) // 2]) / 2
            chips.append(f"트라이 중앙 약 {int(mid // 60)}:{int(mid % 60):02d}")
        for i, text in enumerate(chips):
            bg = "#1d3446" if i == 2 else "#252b33"
            tk.Label(self.b3793_chip_frame, text=text, bg=bg, fg="#e9edf2", font=(FONT_BODY, 9), padx=_px(8), pady=_px(3)).pack(side="left", padx=_px((0, 6)))

        # Table: newest first, issue cell with tag.
        self._b3793_rows_by_stage = {}
        for idx, item in enumerate(sorted(rows, key=lambda r: int(r.get("tryNo") or 0), reverse=True)):
            try_no = item.get("tryNo") or "?"
            row = row_by_try.get(str(try_no))
            issue, tag = self._b3793_issue(item, row)
            item["boardIssue"], item["boardTag"] = issue, tag
            iid = f"b3641-{idx}"
            self.b3641_board_rows[iid] = item
            self.b3641_board_tree.insert(
                "", "end", iid=iid, text=f"#{try_no}", tags=(tag,),
                values=(f"{item.get('result') or '-'} · {item.get('duration') or '-'}", item.get("progress") or "진도 확인 중", issue),
            )
            stage = int(item.get("stageIndex") or 0)
            self._b3793_rows_by_stage.setdefault(stage, set()).add(self._b3793_stage_label(item.get("stage") or item.get("progress")))

        # Right column: 반복 실패 cards
        self.b3793_repeat_scope_var.set(f"범위: {session_date} · {len(rows)}트")
        self._b3793_clear(self.b3793_repeat_list)
        repeats = model.get("repeats") or []
        repeat_lines = []
        if repeats:
            for item in repeats[:5]:
                tries = [str(x) for x in (item.get("tries") or []) if x is not None]
                count = int(item.get("count") or 0)
                hot = count >= 3
                box = RoundBox(self.b3793_repeat_list, fill=_ui_theme.HILITE_BG if hot else _ui_theme.CARD_BG, outline=_ui_theme.HILITE_BORDER if hot else _ui_theme.CARD_BORDER, radius=8, padx=12, pady=8)
                box.pack(fill="x", pady=_px((0, 6)))
                card = box.inner
                tk.Label(card, text=str(item.get("label") or "검증 실패"), bg=card["bg"], fg=_ui_theme.TEXT, font=(FONT_BODY, 11), anchor="w").pack(side="left", fill="x", expand=True)
                right = f"{count}트" + (f" · #{', #'.join(tries[:3])}" if tries and count <= 3 else "")
                tk.Label(card, text=right, bg=card["bg"], fg=_ui_theme.RED if hot else _ui_theme.ORANGE, font=(FONT_TITLE, 11)).pack(side="right")
                repeat_lines.append(f"{item.get('label')} · {count}회")
        else:
            tk.Label(self.b3793_repeat_list, text="반복되는 검증 실패 없음", bg=_ui_theme.PANEL_BG, fg=_ui_theme.MUTED, font=(FONT_BODY, 9), anchor="w", padx=_px(6), pady=_px(6)).pack(fill="x")
            repeat_lines = ["반복되는 검증 실패 없음"]
        self._replace_readonly_text(self.b3641_board_repeat, "\n".join(repeat_lines))

        # Right column: numbered voice orders (max 3)
        self._b3793_clear(self.b3793_voice_list)
        orders = [str(x) for x in (model.get("voiceOrders") or []) if str(x).strip()][:3] or ["다음 구간 진행 확인"]
        for i, line in enumerate(orders, 1):
            r = tk.Frame(self.b3793_voice_list, bg=_ui_theme.PANEL_BG)
            r.pack(fill="x", pady=_px((0, 8)))
            tk.Label(r, text=str(i), bg=_ui_theme.PANEL_BG, fg=_ui_theme.ORANGE, font=(FONT_TITLE, 11), width=2, anchor="n").pack(side="left", anchor="n")
            tk.Label(r, text=line, bg=_ui_theme.PANEL_BG, fg=_ui_theme.TEXT, font=(FONT_BODY, 10), anchor="w", justify="left", wraplength=_px(280)).pack(side="left", fill="x", expand=True)
        self._replace_readonly_text(self.b3641_board_voice, "\n".join(f"{i}. {x}" for i, x in enumerate(orders, 1)))
        self._draw_b3793_stage_bar()

    def _b3793_bar_click(self, event):
        for x0, x1, try_no in getattr(self, "_b3793_bar_hits", []) or []:
            if x0 <= event.x <= x1 and try_no is not None:
                for row in self._b3641_context_rows(all_dates=False):
                    if str(row.get("tryNo")) == str(try_no):
                        self._select_live_row_by_key(self._live_row_key(row))
                        return
                return

    def _draw_b3793_stage_bar(self):
        c = getattr(self, "b3793_stage_canvas", None)
        if c is None:
            return
        c.delete("all")
        model = getattr(self, "_b3641_board_model", {}) or {}
        counts = list(model.get("stageCounts") or [])
        total = sum(int(x or 0) for x in counts)
        if not total:
            return
        w = max(300, c.winfo_width() or 600)
        x = 0.0
        labels = getattr(self, "_b3793_rows_by_stage", {}) or {}
        legend = []
        for idx, n in enumerate(counts):
            n = int(n or 0)
            if n <= 0:
                continue
            seg = w * n / total
            color = self._B3793_STAGE_COLORS[min(idx, 4)]
            c.create_rectangle(x, 0, x + seg, 8, fill=color, outline="")
            x += seg
            name = " / ".join(sorted(labels.get(idx, {"-"})))
            legend.append((color, f"{name} {n}"))
        lx = 0
        for color, text in legend:
            c.create_rectangle(lx, 18, lx + 8, 26, fill=color, outline="")
            item = c.create_text(lx + 12, 22, anchor="w", text=text, fill="#c4cbd3", font=(FONT_BODY, 8))
            bbox = c.bbox(item)
            lx = (bbox[2] if bbox else lx + 80) + 14

    def _draw_b3641_board_progress(self):
        c = getattr(self, "b3641_board_canvas", None)
        if c is None:
            return
        c.delete("all")
        model = getattr(self, "_b3641_board_model", {}) or {}
        rows = model.get("rows") or []
        w = max(420, c.winfo_width() or 800)
        h = max(140, c.winfo_height() or 170)
        if not rows:
            c.create_text(12, 12, anchor="nw", fill="#a7b0ba", text="표시할 트라이가 없습니다.", font=(FONT_BODY, 9))
            return
        max_d = max(float(r.get("durationSec") or 1) for r in rows)
        max_d = max(30.0, max_d)
        self._b3793_bar_hits = []
        if not getattr(self, "_b3793_bar_bound", False):
            c.bind("<Button-1>", self._b3793_bar_click)
            self._b3793_bar_bound = True
        left, right, top, bottom = 22, w - 14, 12, h - 28
        slot = max(18, (right - left) / max(1, len(rows)))
        bar_w = min(24, max(8, slot * 0.52))
        stage_colors = self._B3793_STAGE_COLORS
        for i, r in enumerate(rows):
            x = left + i * slot + slot / 2
            dur = float(r.get("durationSec") or 0)
            bh = (dur / max_d) * (bottom - top)
            y0 = bottom - bh
            color = stage_colors[min(4, max(0, int(r.get("stageIndex") or 0)))]
            c.create_rectangle(x - bar_w/2, y0, x + bar_w/2, bottom, fill="#252b33", outline="#343b45")
            c.create_line(x - bar_w/2, y0, x + bar_w/2, y0, fill=color, width=3)
            # significant collapse line if available
            collapse = (((r.get("review") or {}).get("observations") or {}).get("collapseSnapshot") or {})
            if collapse.get("significantCluster") and collapse.get("collapseSec") is not None:
                cy = bottom - (float(collapse["collapseSec"]) / max_d) * (bottom - top)
                c.create_line(x - bar_w/2 - 2, cy, x + bar_w/2 + 2, cy, fill="#ff756f", width=2)
            c.create_text(x, bottom + 12, text=str(r.get("tryNo") or "?"), fill="#a7b0ba", font=(FONT_BODY, 7))
            self._b3793_bar_hits.append((x - slot / 2, x + slot / 2, r.get("tryNo")))

    def _open_b3641_board_try(self, _event=None):
        sel = self.b3641_board_tree.selection() if hasattr(self, "b3641_board_tree") else ()
        if not sel:
            return
        item = self.b3641_board_rows.get(sel[0])
        if not item:
            return
        try_no = item.get("tryNo")
        for row in self._b3641_context_rows(all_dates=False):
            if str(row.get("tryNo")) == str(try_no):
                self._select_live_row_by_key(self._live_row_key(row))
                break

    def _b3642_neighbor_context(self, row):
        rows = self._b3641_context_rows(all_dates=True) if row else []
        rows.sort(key=self._live_row_sort_key)
        idx = next((i for i, candidate in enumerate(rows) if same_physical_pull(candidate, row)), None)
        if idx is None:
            return rows, None
        return rows, idx

    def _move_b3642_try(self, delta):
        row = self._selected_live_row()
        rows, idx = self._b3642_neighbor_context(row)
        if idx is None:
            return
        target_idx = idx + int(delta)
        if target_idx < 0 or target_idx >= len(rows):
            return
        self._select_live_row_by_key(self._live_row_key(rows[target_idx]))

    def _b3642_recent_kill_row(self, row):
        if not row:
            return None
        rows, idx = self._b3642_neighbor_context(row)
        if idx is None:
            return None
        for candidate in reversed(rows[:idx]):
            if str(candidate.get("result") or "").upper() == "KILL":
                return candidate
        return None

    def _refresh_b3642_try_navigation(self, row):
        if not hasattr(self, "b3642_prev_try_btn"):
            return
        rows, idx = self._b3642_neighbor_context(row)
        prev_ok = idx is not None and idx > 0
        next_ok = idx is not None and idx < len(rows) - 1
        self.b3642_prev_try_btn.configure(state="normal" if prev_ok else "disabled")
        self.b3642_next_try_btn.configure(state="normal" if next_ok else "disabled")
        if idx is None:
            self.b3642_nav_var.set("동일 보스·난이도 트라이 이동")
        else:
            self.b3642_nav_var.set(f"동일 보스·난이도 · {idx + 1}/{len(rows)}")

    def _render_b3642_compare(self, row, review):
        if not hasattr(self, "b3642_compare_text"):
            return
        if not row or not review:
            self._replace_readonly_text(self.b3642_compare_text, "현재 트라이 비교 데이터 없음")
            return
        # V4.2 TRY-SPEED: This Text widget is hidden while evidence is folded.
        # Avoid loading every historical mechanic review for an unseen panel.
        # _toggle_b3739_evidence refreshes it on demand when opened.
        if not bool(getattr(self, "b3739_evidence_open", False)):
            return
        kill_row = self._b3642_recent_kill_row(row)
        kill_review = load_mechanic_review(kill_row) if kill_row else {}
        model = build_p2_relative_compare(review, kill_review or None, row, kill_row or {})
        reached_rows = self._v42_p2_reached_labels()
        scope = "범위: 로컬 세션"
        if reached_rows:
            scope += " · P2 도달 " + "·".join(reached_rows)
        scope += f" · n={len(reached_rows)}"
        lines = [scope, str(model.get("note") or "")]
        cur_no = model.get("currentTryNo") or "?"
        cur_anchor = model.get("currentAnchorSec")
        lines.append(
            f"현재 Try {cur_no}: "
            + (f"{model.get('currentBasis')} {b3641_fmt_clock(cur_anchor)} → P2 0:00" if cur_anchor is not None else "P2 기준 확인 불가")
        )
        for ev in (model.get("currentEvents") or [])[:10]:
            lines.append(f"  {ev.get('relative')}  {ev.get('label')}")
        if kill_row:
            kill_no = model.get("killTryNo") or "?"
            kill_anchor = model.get("killAnchorSec")
            lines.append(
                f"최근 킬 Try {kill_no}: "
                + (f"{model.get('killBasis')} {b3641_fmt_clock(kill_anchor)} → P2 0:00" if kill_anchor is not None else "P2 기준 확인 불가")
            )
            for ev in (model.get("killEvents") or [])[:10]:
                lines.append(f"  {ev.get('relative')}  {ev.get('label')}")
        else:
            lines.append("최근 킬: 현재 트라이 이전 동일 보스·난이도 킬 없음")

        shield_cmp = build_shield_throughput_compare(
            review, self._b3641_review_pairs(all_dates=True), current_row=row
        )
        if shield_cmp.get("eligible"):
            lines.append("")
            lines.append(str(shield_cmp.get("scope") or "보호막 처리량 비교"))
            if shield_cmp.get("baselineMedianRate") is not None:
                lines.append(
                    f"  현재 {float(shield_cmp.get('currentRate')):,.0f}/s · "
                    f"기준 중앙 {float(shield_cmp.get('baselineMedianRate')):,.0f}/s · "
                    f"편차 {float(shield_cmp.get('deltaPct')):+.1f}%"
                )
            lines.append("  " + str(shield_cmp.get("summary") or shield_cmp.get("note") or "D 비교 근거 확인 중"))
        self._replace_readonly_text(self.b3642_compare_text, "\n".join(lines))

    def _apply_b3739_simple_try_summary(self, row, review):
        summary = build_simple_try_summary(review or {}, row or {})
        reason = str(summary.get("reason") or "확정 원인 없음")
        evidence = [str(x).strip() for x in (summary.get("evidence") or []) if str(x).strip()]
        action = str(summary.get("action") or "다음 풀 · 현재 검증된 실패 없음")
        if not action.startswith("다음 풀"):
            action = "다음 풀 · " + action
        self.b3641_first_var.set("왜 끝났나 · " + reason)
        self.b3641_major_var.set("근거 · " + ("\n       ".join(evidence[:3]) if evidence else "검증된 근거 없음"))
        self.b3641_collapse_var.set(action)
        self.b3641_footer_var.set("상세 근거는 아래에서 펼쳐 확인합니다. 기본 화면은 원인 → 근거 → 다음 풀만 표시합니다.")

    def _render_b3641_try_priority(self, row):
        self._render_b3641_try_priority_core(row)
        try:
            self._apply_b3792_layout(row)
        except Exception:
            pass
        # V3.5: separate guard so the personal lines can never block the raid view.
        try:
            self._v35_update_personal_card(row)
        except Exception:
            try:
                self.v35_personal_box.pack_forget()
            except Exception:
                pass
        # V3.7: guard AFTER all other rendering. Failure never blocks raid or personal.
        try:
            self._v37_update_chronology_card(row)
        except Exception:
            try:
                self.v37_raid_time_box.pack_forget()
            except Exception:
                pass
        # V3.8: display-only correction after BOTH raid and chronology have rendered.
        # Failure is isolated: the original four raid cells remain usable.
        try:
            self._v38_refresh_collapse_wording(row)
        except Exception:
            pass
        # V3.9: last display-only wording pass. No data or engine changes.
        try:
            self._v39_refresh_readable_korean(row)
        except Exception:
            pass
        # V4.0: distil the final, already-rendered text; never touch engine facts.
        try:
            self._v40_refresh_glance(row)
        except Exception:
            try:
                self.v40_overview_box.pack_forget()
                self._v40_restore_legacy()
            except Exception:
                pass
        # V4.3: 2단 압축 레이아웃 + 화면 용어 (display only; failure keeps V4.2 layout).
        try:
            from live.v43_layout import apply as _v43_apply
            _v43_apply(self, row)
        except Exception:
            pass

    def _v528_no_judgment_text(self):
        """1.0.6 (GOLD V5.2.8): 판정 순서가 비었을 때 결론 줄([추정] 첫 실수 · 킬 사망 정리)과 엇갈리지 않는 문구."""
        try:
            from live.v528_review import no_judgment_text
            return no_judgment_text(getattr(self, "_b3641_try_row", None) or {})
        except Exception:
            return "검증된 실수 없음"

    def _v40_restore_legacy(self):
        """Restore V3.9's original conclusion rows with their exact packing."""
        for widget, pack in getattr(self, "_v40_legacy_pack", ()):
            if not widget.winfo_manager():
                widget.pack(**pack)

    @staticmethod
    def _coiled_hf4_factual_glance(result, row, summary, priority_model=None):
        """HF4: expose HF3's *observed* late-P1 rupture in the three V4 rows.

        This is presentation-only.  Never promote an observation to a B31
        collapseDirect verdict, an assigned player's fault, or a DPS failure.
        Unrelated bosses, non-mythic/kill/reset/stale rows and reviews without
        HF3's exact evidence contract retain all three original V4 lines.
        """
        if not isinstance(result, (tuple, list)) or len(result) != 3:
            return result
        if not isinstance(row, dict) or not isinstance(summary, dict):
            return result
        if (str(row.get("result") or "").lower() != "wipe"
                or row.get("mechanicReviewStale")):
            return result
        try:
            if int(row.get("encounterID") or 0) != 3429 or int(row.get("difficultyID") or 0) != 16:
                return result
        except (TypeError, ValueError):
            return result
        # Preserve any already verified *direct* cause over this observation.
        if isinstance(priority_model, dict) and priority_model.get("collapseDirect"):
            return result
        observation = summary.get("observedCollapse")
        if not isinstance(observation, dict):
            return result
        if (observation.get("kind") != "p1_late_rupture_observation"
                or observation.get("status") != "observed"
                or observation.get("causality") != "rupture_hit_and_death_time_order_only"):
            return result
        what = str(observation.get("observation") or "").strip()
        if not what or "맹독성 파열" not in what or "집단 사망" not in what:
            return result
        # Only the first two lines change. Keep V4.3's next-pull advice as-is.
        relation = "맹독성 파열 피해와 다수 사망이 같은 시간대에 관측됨 · 전환 지연의 근본 원인 미확정"
        return (what, relation, result[2])

    def _v40_refresh_glance(self, row):
        """Display only: reset/fake/Kill/stale or missing timestamp use legacy UI."""
        from live.raid_glance_v40 import summarize, source_identity
        box = getattr(self, "v40_overview_box", None)
        if box is None:
            return
        key = source_identity(row)
        if (key is None or str(row.get("result") or "").lower() != "wipe"
                or row.get("mechanicReviewStale")
                or self._live_row_is_fake(row) or self._live_row_is_reset(row)):
            box.pack_forget()
            self._v40_restore_legacy()
            self._v40_visible_key = None
            self._v40_details_open = False
            return
        cells = getattr(self, "b3792_cells", {}) or {}
        if not all(cells.get(k) for k in ("first", "major", "collapse")):
            box.pack_forget()
            self._v40_restore_legacy()
            return
        chronology = getattr(self, "v37_raid_time_box", None)
        has_time = bool(chronology is not None and chronology.winfo_manager())
        time_vars = getattr(self, "v37_raid_time_vars", ())
        explanation = time_vars[1].get() if has_time and len(time_vars) >= 2 else ""
        result = summarize(first=cells["first"]["valueVar"].get(),
                           major=cells["major"]["valueVar"].get(),
                           collapse_head=cells["collapse"]["headVar"].get(),
                           collapse=cells["collapse"]["valueVar"].get(),
                           next_action=self.b3792_next_var.get(),
                           chronology_explanation=explanation, chronology_visible=has_time)
        if result is None:
            box.pack_forget()
            self._v40_restore_legacy()
            return
        if key != self._v40_visible_key:
            self._v40_details_open = False
        self._v40_visible_key = key
        self._v40_chronology_available = has_time
        # V4.2: 42 verified saved pull identities only, display-only chronology.
        # V3.7 already uses the same small cached JSON; no raw log or engine read.
        # A failure leaves the original V4.0.1 summary completely unchanged.
        try:
            if (int(row.get("encounterID") or 0) == 3429
                    and int(row.get("difficultyID") or 0) == 16
                    and str(row.get("startTimestamp") or "").startswith("10/13/2026 ")):
                from live.raid_personal_context_v35 import _read_small_json
                from live.raid_chronology_glance_v42 import clarify_glance
                _v42_idx = _read_small_json(WORKDIR / "live" / "raid_chronology_index_v37.json")
                result = clarify_glance(result, row, _v42_idx)
        except Exception:
            pass
        # V4.3: Raid-first next-pull checks from already VERIFIED BossMechanic facts.
        # Presentation only; a generic root-cause candidate is never promoted.
        try:
            from live.raid_coiled_brief_v43 import refine_glance
            result = refine_glance(result, row, getattr(self, "_b3641_try_model", None))
        except Exception:
            pass  # Keep V4.2's original conclusion if the add-on is unavailable.
        # HF4: the legacy three-cell V4 path above only uses the simple summary's
        # action.  Reuse the *same saved review* already loaded by layout to show
        # HF3's independently verified collapse observation.  Never read raw logs
        # or modify the saved review/cache; failure keeps the original V4 text.
        try:
            if (int(row.get("encounterID") or 0) == 3429
                    and int(row.get("difficultyID") or 0) == 16):
                cached = getattr(self, "_v35_review_for", None)
                review = cached[1] if cached and cached[0] is row else load_mechanic_review(row)
                if isinstance(review, dict) and review:
                    hf3_summary = build_simple_try_summary(review, row)
                    result = self._coiled_hf4_factual_glance(
                        result, row, hf3_summary, getattr(self, "_b3641_try_model", None))
        except Exception:
            pass  # Keep existing V4.3 conclusion if saved HF3 evidence is absent.
        for var, value in zip(self.v40_overview_vars, result):
            var.set(value)
        if not box.winfo_manager():
            box.pack(fill="x", pady=_px((0, 3)))
        self._v40_apply_detail_visibility()

    def _v40_toggle_details(self):
        if not getattr(self, "_v40_visible_key", None):
            return
        self._v40_details_open = not self._v40_details_open
        self._v40_apply_detail_visibility()

    def _v40_apply_detail_visibility(self):
        if not self.v40_overview_box.winfo_manager():
            return
        chronology = getattr(self, "v37_raid_time_box", None)
        if self._v40_details_open:
            self._v40_restore_legacy()
            if (self._v40_chronology_available and chronology is not None
                    and not chronology.winfo_manager()):
                chronology.pack(fill="x", pady=_px((0, 10)), before=self.b3739_cards_box)
            self.v40_toggle_btn.configure(text="판정 상세 접기 ▴")
        else:
            for widget, _pack in self._v40_legacy_pack:
                if widget.winfo_manager():
                    widget.pack_forget()
            if chronology is not None and chronology.winfo_manager():
                chronology.pack_forget()
            self.v40_toggle_btn.configure(text="판정 상세 펼치기 ▾")

    def _v38_refresh_collapse_wording(self, row):
        """Correct an over-strong collapse label only; never change stored raid verdicts."""
        cell = (getattr(self, "b3792_cells", {}) or {}).get("collapse")
        if not cell:
            return
        # Every Try starts from the original header, so no V3.8 wording leaks over.
        cell["headVar"].set("붕괴 직결")
        # Kill / reset / fake pull / no selection: keep the existing screen exactly.
        if (not row or self._live_row_is_fake(row) or self._live_row_is_reset(row)
                or str(row.get("result") or "").lower() == "kill"):
            return
        from live.raid_personal_context_v35 import _read_small_json
        from live.raid_chronology_v37 import _lookup
        from live.raid_collapse_display_v38 import collapse_display
        index = _read_small_json(WORKDIR / "live" / "raid_chronology_index_v37.json")
        record = _lookup(row, index)
        model = getattr(self, "_b3641_try_model", None) or {}
        old_value = str(cell["valueVar"].get() or "")
        result = collapse_display(row, matched_record=record, model=model, old_value=old_value)
        if result.get("mode") != "change":
            return
        self._set_b3792_cell("collapse", result["value"], result["note"], "b31")
        cell["headVar"].set(result["head"])
        self.b3641_collapse_var.set(result["head"] + " · " + result["value"])

    def _v39_refresh_readable_korean(self, row):
        """Readability only: preserve underlying classification, source and verdict."""
        if not row:
            return
        from live.raid_readable_korean_v39 import (
            personal_lines, chronology_lines, collapse_wording, placeholder_wording)
        try:
            box = getattr(self, 'v35_personal_box', None)
            if box is not None and box.winfo_manager():
                vals = [v.get() for v in self.v35_personal_vars]
                for var, line in zip(self.v35_personal_vars, personal_lines(vals)):
                    var.set(line)
        except Exception:
            pass  # Personal wording must never block the raid conclusion.
        try:
            box = getattr(self, 'v37_raid_time_box', None)
            if box is not None and box.winfo_manager():
                from live.raid_personal_context_v35 import _read_small_json
                from live.raid_chronology_v37 import _lookup
                index = _read_small_json(WORKDIR / 'live' / 'raid_chronology_index_v37.json')
                matched = _lookup(row, index)
                lines = chronology_lines(matched)
                if lines is not None:
                    for var, line in zip(self.v37_raid_time_vars, lines):
                        var.set(line)
        except Exception:
            pass  # Chronology wording never changes facts or causes.
        try:
            if (str(row.get('result') or '').lower() == 'kill' or
                self._live_row_is_fake(row) or self._live_row_is_reset(row)):
                return  # Preserve Kill, reset and excluded-pull conclusions.
            cells = getattr(self, 'b3792_cells', {}) or {}
            collapse = cells.get('collapse')
            if collapse:
                head, value, sub = collapse_wording(
                    collapse['headVar'].get(), collapse['valueVar'].get(), collapse['subVar'].get())
                collapse['headVar'].set(head)
                collapse['valueVar'].set(value)
                collapse['subVar'].set(sub)
                self.b3641_collapse_var.set(head + ' · ' + value)
            for key, label in (('first', '첫 실수'), ('major', '주요 사고')):
                item = cells.get(key)
                if item:
                    value = placeholder_wording(item['valueVar'].get(), kind=key)
                    item['valueVar'].set(value)
                    legacy = self.b3641_first_var if key == 'first' else self.b3641_major_var
                    legacy.set(label + ' · ' + value)
        except Exception:
            pass  # Source verdicts remain untouched, even if UI wording fails.

    def _v35_update_personal_card(self, row):
        """Saved results only (facts.json + already loaded review). Never raw logs, never blame."""
        box = getattr(self, "v35_personal_box", None)
        if box is None:
            return
        if not row or self.raid_public_mode or str(self.current_player or "").split("-")[0] != "내 캐릭터":
            box.pack_forget()
            return
        cached = getattr(self, "_v35_review_for", None)
        review = cached[1] if cached and cached[0] is row else (load_mechanic_review(row) or {})
        if self._live_row_is_fake(row) or self._live_row_is_reset(row):
            lines = ["리셋/가짜 풀 · 개인 판정하지 않음", "원본 사실은 기존 상세에서 확인", "새로운 책임 판정 없음"]
        elif row.get("mechanicReviewStale"):
            lines = ["기믹 판정 최신화 대기 · 책임 판정 없음",
                     "개인 이벤트는 기존 상세 기록에서 확인",
                     "시전·차단·사망 원인 추정 안 함"]
        else:
            try:
                from live.raid_personal_context_v35 import build_context, _read_small_json
                audit = _read_small_json(WORKDIR / "live" / "raid_personal_audit_v35.json")
                lines = build_context(row, review, player="내 캐릭터", audit=audit).get("lines") or ["개인 맥락 확인 대기"]
            except Exception:
                lines = ["개인 기록 표시 불가 · 공대 원인 판정에는 영향 없음"]
        for var, line in zip(self.v35_personal_vars, list(lines) + [""] * 3):
            var.set(str(line))
        if not box.winfo_manager():
            box.pack(fill="x", pady=_px((0, 10)), after=self._v35_concl_box)

    def _v37_update_chronology_card(self, row):
        """Saved and cached V3.6 proof only; never reparse raw or infer player blame."""
        box = getattr(self, "v37_raid_time_box", None)
        if box is None:
            return
        if not row or self._live_row_is_fake(row) or self._live_row_is_reset(row) or row.get("mechanicReviewStale"):
            box.pack_forget()
            return
        from live.raid_personal_context_v35 import _read_small_json
        from live.raid_chronology_v37 import build_lines
        index = _read_small_json(WORKDIR / "live" / "raid_chronology_index_v37.json")
        result = build_lines(row, index)
        if not result.get("visible"):
            box.pack_forget()
            return
        for var, line in zip(self.v37_raid_time_vars, result["lines"]):
            var.set(line)
        if not box.winfo_manager():
            box.pack(fill="x", pady=_px((0, 10)), before=self.b3739_cards_box)

    def _b3792_wrap_conclusion(self, event=None):
        width = int(getattr(event, "width", 0) or 0)
        if width <= 0:
            return
        per = max(160, width // 3 - 30)
        for cell in (getattr(self, "b3792_cells", {}) or {}).values():
            cell["value"].configure(wraplength=_px(per))
            cell["sub"].configure(wraplength=_px(per))
        if hasattr(self, "b3792_next_label"):
            self.b3792_next_label.configure(wraplength=max(300, width - 30))

    _B3792_TAGS = {
        "verified": ("#4a2222", "#ff756f", "검증됨"),
        "candidate": (_ui_theme.CARD_BG, _ui_theme.STATE_CANDIDATE, "후보"),
        "b31": ("#20262e", "#e9edf2", "B31"),
        "kill": ("#173825", "#79c99e", "킬"),
        "none": ("#20262e", "#a7b0ba", ""),
    }

    def _set_b3792_cell(self, key, value, sub="", tag="none"):
        cell = (getattr(self, "b3792_cells", {}) or {}).get(key)
        if not cell:
            return
        _bg, fg, label = self._B3792_TAGS.get(tag, self._B3792_TAGS["none"])
        if tag in {"none", "b31"}:
            fg = _ui_theme.TEXT if tag == "b31" or key == "major" else _ui_theme.TEXT
        cell["value"].configure(fg=fg)
        cell["valueVar"].set(str(value or "-"))
        sub_parts = [x for x in (str(sub or "").strip(), label if label and tag in {"verified", "candidate"} else "") if x]
        cell["subVar"].set(" · ".join(sub_parts))


    @staticmethod
    def _b3792_compact_finding_text(item, fallback="검증 실패", max_summary=22):
        if not isinstance(item, dict):
            return str(fallback or "검증 실패")
        label = str(item.get("label") or item.get("title") or item.get("kind") or fallback or "검증 실패").strip()
        summary = str(item.get("summary") or "").strip()
        if summary and summary not in label and len(summary) <= int(max_summary):
            return f"{label} · {summary}"
        return label

    @staticmethod
    def _b3792_event_time(obj):
        if isinstance(obj, dict):
            for k in ("sec", "atSec", "startSec", "time"):
                if obj.get(k) is not None:
                    try:
                        return float(obj.get(k))
                    except (TypeError, ValueError):
                        return None
        return None

    def _apply_b3792_layout(self, row):
        """B37.3.9.2 · ③ 트라이 상세 layout (presentation only; engine untouched)."""
        row = row or {}
        review = load_mechanic_review(row) if row else {}
        model = getattr(self, "_b3641_try_model", None) or {}
        summary = build_simple_try_summary(review or {}, row) if review else {}
        is_kill = str(row.get("result") or "").lower() == "kill"
        fmt = self._fmt_live_relative
        mechanic_stale = bool(row.get("mechanicReviewStale"))
        self._v35_review_for = (row, review)

        if self._live_row_is_fake(row) or self._live_row_is_reset(row):
            kind = "가짜 풀" if self._live_row_is_fake(row) else "리셋"
            self._set_b3792_cell("first", "적용 안 함", f"{kind} · 기믹 판정 제외")
            self._set_b3792_cell("major", "적용 안 함", "일반 복기/반복 원인 집계 제외")
            self._set_b3792_cell("collapse", "적용 안 함", "원본 사실 관측만 보존")
            self.b3792_next_var.set("→ 다음 풀: 제외된 풀은 성공/실패 표본으로 사용하지 않습니다.")
            self.b3641_first_var.set("첫 실수 · 적용 안 함")
            self.b3641_major_var.set("주요 사고 · 적용 안 함")
            self.b3641_collapse_var.set("붕괴 직결 · 적용 안 함")
            self.b3641_footer_var.set(self.b3792_next_var.get())
            try:
                self._render_b3796_chain(row)
            except Exception:
                pass
            for box in (self.b3739_cards_box, self.b365_shield_box, self.b3739_timeline_box, self.b3739_evidence_box, self.b3739_compare_box):
                box.pack_forget()
            self.b3836_always_boxes = [self.b3739_timeline_box]
            self.b3739_evidence_boxes = [self.b3739_evidence_box]
            self._apply_b3739_evidence_state()
            if not getattr(self, "b3641_detail_open", False):
                self._toggle_b3641_detail()
            return

        # B37.3.9.13: never turn an old BossMechanic review into a current
        # "없음" verdict while the saved-review upgrader is still working.
        # The B31 collapse can remain visible as context, but the mechanic slots
        # clearly say that their current verdict is pending.
        if mechanic_stale:
            self._set_b3792_cell("first", "최신화 대기", "BossMechanic 구버전")
            self._set_b3792_cell("major", "최신화 대기", "선택 트라이 우선 처리")
        # 첫 실수 / 주요 사고 = verified BossMechanic only (candidates never promoted).
        first = None if mechanic_stale else model.get("firstMistake")
        if first:
            t = self._b3792_event_time(first)
            self._set_b3792_cell("first", self._b3792_compact_finding_text(first), fmt(t) if t is not None else "", "verified")
        elif not mechanic_stale:
            # B37.4.36.3: Kill 트라이는 B37.4.11 계약대로 firstMistake 슬롯을
            # 비워 두되, 살아남은 verified 실패 개수까지 0회로 지우지 않는다.
            # 가장 이른 실패는 '남은 개선점(회복됨)'으로 보존한다.
            recovered_count = int(model.get("verifiedFailureCount") or 0) if is_kill else 0
            recovered_first = model.get("firstRecoveredFailure") if isinstance(model.get("firstRecoveredFailure"), dict) else None
            if is_kill and recovered_count:
                sub = [f"검증 실패 {recovered_count}회", "Kill에서 회복"]
                t = self._b3792_event_time(recovered_first)
                if t is not None:
                    sub.append(f"첫 {fmt(t)}")
                self._set_b3792_cell("first", "없음", " · ".join(sub))
            else:
                self._set_b3792_cell("first", "없음", "검증 실패 0회")
        major = None if mechanic_stale else model.get("majorIncident")
        if major:
            t = self._b3792_event_time(major)
            self._set_b3792_cell("major", self._b3792_compact_finding_text(major, "검증 사고"), fmt(t) if t is not None else "", "verified")
        elif not mechanic_stale:
            self._set_b3792_cell("major", "없음", "검증된 주요 사고 없음")

        # 붕괴 직결 = B31 collapse (1초 안 4명 이상) — the header no longer repeats it.
        b31 = {}
        try:
            b31 = self._review_model_for_row(row) or {}
        except Exception:
            b31 = {}
        if is_kill:
            self._set_b3792_cell("collapse", "킬 성공 · 공대 붕괴 없음", "", "kill")
        elif model.get("collapseDirect"):
            t = self._b3792_event_time(model.get("collapseDirect"))
            self._set_b3792_cell("collapse", model.get("collapseText") or "붕괴 직결", fmt(t) if t is not None else "", "verified")
        elif b31.get("rootMechanic"):
            t = b31.get("rootTime")
            sub = []
            if t is not None:
                sub.append(fmt(t))
            if b31.get("confidence"):
                sub.append(f"B31 확신 {str(b31.get('confidence')).upper()}")
            sub.append(f"사망 {int(b31.get('deathCount') or 0)}명")
            root_value = str(b31.get("rootMechanic") or "B31 붕괴")
            if mechanic_stale:
                root_value = "B31 관측 · " + root_value
            self._set_b3792_cell("collapse", root_value, " · ".join(sub), "b31")
        else:
            self._set_b3792_cell("collapse", "확인 불가", "1초 안 4명 이상 사망 없음")

        action = str(summary.get("action") or model.get("nextPull") or "현재 검증된 실패 없음").strip()
        for prefix in ("다음 풀 · ", "다음 풀:", "다음 풀"):
            if action.startswith(prefix):
                action = action[len(prefix):].strip()
        if "→ 다음 풀:" in action:
            action = action.split("→ 다음 풀:", 1)[1].strip()
        self.b3792_next_var.set("→ 다음 풀: " + (action or "현재 검증된 실패 없음"))

        # Legacy vars (tests / text export) mirror the cells.
        for key, var, name in (("first", self.b3641_first_var, "첫 실수"), ("major", self.b3641_major_var, "주요 사고"), ("collapse", self.b3641_collapse_var, "붕괴 직결")):
            cell = self.b3792_cells.get(key) or {}
            if cell:
                var.set(f"{name} · {cell['valueVar'].get()}")
        self.b3641_footer_var.set(self.b3792_next_var.get())

        try:
            self._render_b3796_chain(row)
        except Exception:
            pass

        # Evidence membership: generic bosses drop the duplicate 보조 관측 box;
        # the kill compare box only appears when a kill sample exists.
        always = [self.b3739_cards_box]
        if not model.get("generic") and model.get("supported"):
            always.append(self.b365_shield_box)
        always.append(self.b3739_timeline_box)
        boxes = [self.b3739_evidence_box]
        has_kill = False
        if not model.get("generic") and model.get("supported"):
            try:
                has_kill = bool(self._b3642_recent_kill_row(row))
            except Exception:
                has_kill = False
        if has_kill:
            boxes.append(self.b3739_compare_box)
        for box in (self.b3739_cards_box, self.b365_shield_box, self.b3739_timeline_box, self.b3739_evidence_box, self.b3739_compare_box):
            box.pack_forget()
        self.b3836_always_boxes = always
        self.b3739_evidence_boxes = boxes
        # B37.4.36: preserve the user's folded/open state while moving between Trys.
        self._apply_b3739_evidence_state()
        if not getattr(self, "b3641_detail_open", False):
            self._toggle_b3641_detail()

    def _b3799_nav_selected_payload(self):
        if not hasattr(self, "nav_tree"):
            return None
        try:
            sel = self.nav_tree.selection()
            if sel:
                return self.nav_tree_payload.get(sel[0])
        except Exception:
            pass
        return None

    def _install_b3750_live_try_wheel(self):
        """Route wheel events from Try-detail children to its ScrollPage.

        Tk does not bubble mouse-wheel events through widget ancestors.  The
        selected-Try page is a ScrollPage containing many Labels/Frames/Canvas
        widgets, so hovering a mechanic card or timeline can otherwise leave
        the outer vertical scrollbar untouched.  The binding is application-
        wide but the handler is strictly scoped to live_try + live_frame.
        """
        if getattr(self, "_b3750_live_try_wheel_installed", False):
            return
        self._b3750_live_try_wheel_installed = True
        self.bind_all("<MouseWheel>", self._b3750_live_try_mousewheel, add="+")
        self.bind_all("<Button-4>", self._b3750_live_try_mousewheel, add="+")
        self.bind_all("<Button-5>", self._b3750_live_try_mousewheel, add="+")
        # B37.5.0.5 TryWheel: Windows may dispatch wheel input to the widget
        # that still has focus (e.g. the selected Try in the left navigator),
        # where Canvas/Treeview native bindings can return "break" *before*
        # Tk's global 'all' handlers ever run.  A first-position bindtag avoids
        # that preemption and routes based on the real pointer location.
        self.bind_class("NiniTryPointerWheel", "<MouseWheel>", self._b3750_live_try_mousewheel)
        self.bind_class("NiniTryPointerWheel", "<Button-4>", self._b3750_live_try_mousewheel)
        self.bind_class("NiniTryPointerWheel", "<Button-5>", self._b3750_live_try_mousewheel)
        self.bind_all("<Map>", self._b3750_tag_mapped_widget, add="+")
        # Wheel HF4: Tk on Windows may route <MouseWheel> to the *focused*
        # widget instead of the one under the mouse.  Sidebar/widgets created
        # after first paint can miss the early wheel bindtag; their native
        # Canvas/Treeview binding can then return "break" before an 'all'
        # handler runs.  Refresh the priority bindtag when focus/hover changes.
        self.bind_all("<FocusIn>", self._b3750_tag_mapped_widget, add="+")
        self.bind_all("<Enter>", self._b3750_tag_mapped_widget, add="+")
        # Also re-tag all visible/dynamic descendants as the pointer enters
        # the Try page from another tab/window.  This is on-demand, not a
        # repeated expensive poll; all normal wheel handlers are preserved.
        live_outer = getattr(getattr(self, "live_frame", None), "_outer", None)
        if live_outer is not None:
            live_outer.bind("<Enter>", self._b3750_schedule_wheel_retag, add="+")
        # Some native Tk Text/Treeview <Enter> bindings consume events before
        # an 'all' binding can run.  A dynamic Try rebuild triggers its owning
        # ScrollPage Configure, so retag widgets after idle regardless of
        # which child has native focus/hover.  Debounce rebuild bursts.
        live_page = getattr(self, "live_frame", None)
        if live_page is not None:
            live_page.bind("<Configure>", self._b3750_schedule_wheel_retag, add="+")
        self._b3750_schedule_wheel_retag()
        self._b3750_wheel_debug("wheel-router-installed")

    def _b3750_schedule_wheel_retag(self, _event=None):
        """Repair new widget bindings after one GUI layout/focus transition."""
        if getattr(self, "_b3750_retag_pending", False):
            return
        self._b3750_retag_pending = True
        self.after_idle(self._b3750_finish_wheel_retag)

    def _b3750_finish_wheel_retag(self):
        self._b3750_retag_pending = False
        self._b3750_tag_all_widgets()

    @staticmethod
    def _b3750_tag_wheel_widget(widget):
        """Place the pointer-aware wheel router before widget/native bindtags."""
        try:
            tags = tuple(widget.bindtags())
            tag = "NiniTryPointerWheel"
            if tag not in tags:
                widget.bindtags((tag,) + tags)
        except (AttributeError, ValueError, RuntimeError, tk.TclError):
            pass

    def _b3750_tag_mapped_widget(self, event):
        # Dynamic timeline/card children are destroyed/rebuilt as Trys change.
        widget = getattr(event, "widget", None)
        self._b3750_tag_wheel_widget(widget)

    def _b3750_tag_all_widgets(self):
        """Include pre-existing widgets and all descendants added by the GUI."""
        stack = [self]
        while stack:
            widget = stack.pop()
            self._b3750_tag_wheel_widget(widget)
            try:
                stack.extend(widget.winfo_children())
            except (AttributeError, RuntimeError, tk.TclError):
                pass

    def _install_b3750_tooltip_scroll_hide(self):
        """B37.5.0.1: a hover tooltip must not float over the page after the
        Try page scrolls away from its icon.  Re-check ownership on scroll."""
        canvas = getattr(getattr(self, "live_frame", None), "_canvas", None)
        page = getattr(self, "live_frame", None)
        if canvas is None or page is None or getattr(self, "_b3750_scroll_hide_installed", False):
            return
        self._b3750_scroll_hide_installed = True
        original = page._on_scroll

        def on_scroll(lo, hi):
            original(lo, hi)
            if getattr(self, "b3641_timeline_tooltip", None) is not None:
                self._schedule_b3750_tooltip_hide(delay_ms=40)

        canvas.configure(yscrollcommand=on_scroll)

    @staticmethod
    def _b3750_widget_is_descendant(widget, ancestor):
        current = widget
        for _ in range(64):
            if current is ancestor:
                return True
            current = getattr(current, "master", None)
            if current is None:
                break
        return False

    @staticmethod
    def _b3750_wheel_units(event, units_per_step=3):
        if getattr(event, "num", None) == 4:
            return -int(units_per_step)
        if getattr(event, "num", None) == 5:
            return int(units_per_step)
        try:
            delta = int(getattr(event, "delta", 0) or 0)
        except Exception:
            delta = 0
        if not delta:
            return 0
        # Ordinary Windows wheels report ±120.  High-resolution wheels and
        # touchpads can report smaller non-zero deltas; those must still move.
        steps = max(1, int(round(abs(delta) / 120.0)))
        return (-1 if delta > 0 else 1) * int(units_per_step) * steps

    @staticmethod
    def _b3750_widget_can_scroll(widget, units):
        try:
            first, last = widget.yview()
            first, last = float(first), float(last)
        except Exception:
            return False
        eps = 0.0005
        return first > eps if units < 0 else last < (1.0 - eps)

    def _b3750_live_page_scroll_target(self):
        cached = getattr(self, "_b3750_live_page_scroll_widget", None)
        if cached is not None:
            try:
                if cached.winfo_exists():
                    return cached
            except Exception:
                pass
            self._b3750_live_page_scroll_widget = None

        page = getattr(self, "live_frame", None)
        if page is None:
            return None
        excluded = {
            getattr(self, "b3641_try_timeline", None),
            getattr(self, "b3750_skill_ref_canvas", None),
            getattr(self, "nav_canvas", None),
            getattr(self, "b3794_left_canvas", None),
        }
        candidates = []

        # Some ScrollPage implementations expose their scrolling Canvas as an
        # attribute instead of a normal child.  Probe common attribute names.
        explicit = [page]
        for name in ("canvas", "_canvas", "scroll_canvas", "_scroll_canvas"):
            try:
                widget = getattr(page, name, None)
            except Exception:
                widget = None
            if widget is not None:
                explicit.append(widget)

        seen = set()

        def consider(widget, depth=0):
            if widget is None or widget in excluded:
                return
            ident = str(widget)
            if ident in seen:
                return
            seen.add(ident)
            try:
                klass = str(widget.winfo_class())
            except Exception:
                klass = ""
            # The page itself may proxy yview; otherwise the outer Canvas is
            # what we want.  Never pick embedded Text/Treeview scroll areas.
            if widget is page or klass == "Canvas":
                try:
                    first, last = widget.yview()
                    first, last = float(first), float(last)
                    span = last - first
                    height = int(widget.winfo_height() or 0)
                    if span < 0.9995:
                        candidates.append((height, -depth, widget))
                except Exception:
                    pass

        for widget in explicit:
            consider(widget, 0)

        def walk(parent, depth=0):
            if depth > 10:
                return
            try:
                children = parent.winfo_children()
            except Exception:
                return
            for child in children:
                consider(child, depth + 1)
                walk(child, depth + 1)

        walk(page)
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        target = candidates[0][2]
        self._b3750_live_page_scroll_widget = target
        return target

    def _b3750_wheel_debug(self, reason, event=None, **details):
        """Opt-in mouse-wheel trace; never enabled during normal gameplay."""
        if os.environ.get("NINI_WHEEL_DEBUG") != "1":
            return
        try:
            lines = [time.strftime("%Y-%m-%d %H:%M:%S"), "reason=" + str(reason)]
            if event is not None:
                lines += ["evw=" + str(getattr(event, "widget", None)),
                          "delta=" + str(getattr(event, "delta", None)),
                          "button=" + str(getattr(event, "num", None))]
            try:
                lines.append("focus=" + str(self.focus_get()))
            except (AttributeError, RuntimeError, tk.TclError):
                lines.append("focus=<unavailable>")
            try:
                lines.append("page=" + str((self.nav_state or {}).get("page")))
            except Exception:
                lines.append("page=<unavailable>")
            for name, value in details.items():
                lines.append(str(name) + "=" + str(value).replace("\r", " ").replace("\n", " ")[:180])
            dest = WORKDIR / "data" / "wheel_debug.log"
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("a", encoding="utf-8") as fh:
                fh.write(" ".join(lines) + "\n")
        except (OSError, ValueError, TypeError, RuntimeError, tk.TclError):
            pass

    def _b3750_live_try_mousewheel(self, event):
        """Route Try vertical wheel by pointer, with geometry fallback.

        Cursor-to-widget hit tests can temporarily return None or a tooltip
        window on mixed-DPI Windows desktops.  A point physically inside the
        visible Try pane is enough to scroll the page, but never scroll when
        the pointer is in the sidebar.  Diagnostics are strictly opt-in.
        """
        try:
            state_page = str((self.nav_state or {}).get("page") or "")
            if state_page != "live_try" and not getattr(getattr(self, "live_frame", None), "_outer", None):
                self._b3750_wheel_debug("page-not-try", event, state_page=state_page)
                return None
        except Exception as exc:
            self._b3750_wheel_debug("page-check-error", event, error=type(exc).__name__)
            return None
        page = getattr(self, "live_frame", None)
        outer = getattr(page, "_outer", None)
        if page is None or outer is None:
            self._b3750_wheel_debug("missing-live-container", event)
            return None
        try:
            if not outer.winfo_ismapped():
                self._b3750_wheel_debug("unmapped-try", event)
                return None
            px, py = int(self.winfo_pointerx()), int(self.winfo_pointery())
        except (AttributeError, TypeError, ValueError, RuntimeError, tk.TclError) as exc:
            self._b3750_wheel_debug("pointer-error", event, error=type(exc).__name__)
            return None
        try:
            pointer_widget = self.winfo_containing(px, py)
        except (AttributeError, RuntimeError, tk.TclError, KeyError):
            pointer_widget = None
        event_xy = None
        try:
            event_xy = (int(event.x_root), int(event.y_root))
        except (AttributeError, TypeError, ValueError, RuntimeError, tk.TclError):
            pass
        if pointer_widget is None and event_xy is not None:
            try:
                pointer_widget = self.winfo_containing(*event_xy)
            except (AttributeError, RuntimeError, tk.TclError, KeyError):
                pass
        inside_widget = (pointer_widget is not None
                         and self._b3750_widget_is_descendant(pointer_widget, outer))
        try:
            ox, oy = int(outer.winfo_rootx()), int(outer.winfo_rooty())
            ow, oh = int(outer.winfo_width()), int(outer.winfo_height())
            outer_rect = (ox, oy, ow, oh)
            inside_rect = (ow > 1 and oh > 1 and ox <= px < ox + ow and oy <= py < oy + oh)
            # Do NOT use event.x_root/y_root to extend the rectangle: Windows
            # delivers wheel input to the focused widget, so those coordinates
            # may describe a different control, not the physical cursor.
        except (AttributeError, RuntimeError, tk.TclError, ValueError):
            outer_rect = "unavailable"
            inside_rect = False
        details = {"ptr": (px, py), "event_xy": event_xy, "under": pointer_widget,
                   "outer": outer_rect, "widget_hit": inside_widget, "rect_hit": inside_rect}
        if not inside_widget and not inside_rect:
            self._b3750_wheel_debug("outside-try", event, **details)
            return None

        units = self._b3750_wheel_units(event, units_per_step=3)
        if not units:
            self._b3750_wheel_debug("zero-wheel-delta", event, **details)
            return None

        try:
            klass = str(pointer_widget.winfo_class()) if pointer_widget is not None else ""
        except (AttributeError, RuntimeError, tk.TclError):
            klass = ""
        if inside_widget and klass in {"Text", "Treeview", "Listbox"} and self._b3750_widget_can_scroll(pointer_widget, units):
            if getattr(event, "widget", None) is pointer_widget:
                self._b3750_wheel_debug("native-nested-scroll", event, **details)
                return None
            try:
                pointer_widget.yview_scroll(units, "units")
                self._b3750_wheel_debug("routed-nested-scroll", event, units=units, **details)
                return "break"
            except (AttributeError, RuntimeError, tk.TclError):
                pass

        target = getattr(page, "_canvas", None) or self._b3750_live_page_scroll_target()
        if target is None:
            self._b3750_live_page_scroll_widget = None
            self._b3750_wheel_debug("no-scroll-target", event, **details)
            return None
        try:
            y_before = target.yview()
            target.yview_scroll(units, "units")
            y_after = target.yview()
        except (AttributeError, RuntimeError, tk.TclError) as exc:
            self._b3750_live_page_scroll_widget = None
            self._b3750_wheel_debug("scroll-error", event, error=type(exc).__name__, **details)
            return None
        self._b3750_wheel_debug("scroll-handled", event, units=units,
                                y_before=y_before, y_after=y_after, **details)
        return "break"

    def _b3799_nav_canvas_wheel(self, event):
        if not hasattr(self, "nav_canvas"):
            return "break"
        try:
            if getattr(event, "num", None) == 4:
                units = -3
            elif getattr(event, "num", None) == 5:
                units = 3
            else:
                delta = int(getattr(event, "delta", 0) or 0)
                units = -int(delta / 120) * 3 if delta else 0
            if units:
                self.nav_canvas.yview_scroll(units, "units")
        except Exception:
            pass
        return "break"

    def _b3799_nav_canvas_click(self, event):
        if not hasattr(self, "nav_canvas"):
            return
        try:
            y = float(self.nav_canvas.canvasy(event.y))
            x = float(event.x)
        except Exception:
            return
        row = None
        for item in getattr(self, "_b3799_nav_rows", ()):  # few dozen rows only
            if item["y0"] <= y < item["y1"]:
                row = item
                break
        if not row:
            return
        iid = row.get("iid")
        payload = row.get("payload")
        if not iid or not payload:
            return

        # B37.4.34: separate navigation from the accordion affordance.
        # - disclosure chevron: expand/collapse only, never navigate
        # - date text: navigate to that session and make sure it is expanded
        # - selected date text clicked again: convenience toggle
        if payload[0] == "session":
            date = str(payload[1] or "")
            arrow_hit = x <= _px(22)
            if arrow_hit:
                if date in self._b3799_open_dates:
                    self._b3799_open_dates.discard(date)
                else:
                    self._b3799_open_dates.add(date)
                self._b3799_render_session_nav(preserve_anchor=True)
                return

            already_selected_date = (
                str(self.nav_state.get("page") or "") == "session"
                and str(self._nav_session_date() or "") == date
            )
            if already_selected_date:
                if date in self._b3799_open_dates:
                    self._b3799_open_dates.discard(date)
                else:
                    self._b3799_open_dates.add(date)
                self._b3799_render_session_nav(preserve_anchor=True)
                return

            # Navigation clicks never collapse the destination.
            self._b3799_open_dates.add(date)
            try:
                self.nav_tree.selection_set(iid)
                self.nav_tree.focus(iid)
            except Exception:
                return
            self._on_nav_tree_selected()
            self._b3799_render_session_nav(preserve_anchor=True)
            return

        try:
            self.nav_tree.selection_set(iid)
            self.nav_tree.focus(iid)
        except Exception:
            return
        self._on_nav_tree_selected()
        self._b3799_render_session_nav(preserve_anchor=True)

    def _b3799_nav_parse_try_text(self, text):
        parts = [p.strip() for p in str(text or "").split("·")]
        out = {"try": parts[0] if parts else "Try ?", "clock": "", "result": "", "duration": ""}
        if len(parts) >= 3:
            # Normal: Try N · result · m:ss
            # Duplicate try number: Try N · HH:MM:SS · result · m:ss
            # B37.4.35: since B37.4.24 results are English (Kill/Wipe/Early Wipe),
            # so the duplicate-Try form must not depend on the Korean labels;
            # otherwise the clock lands in the result column and the result in
            # the duration column (overlapping text in the sidebar).
            if len(parts) >= 4 and re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", parts[1]):
                out.update(clock=parts[1], result=parts[2], duration=parts[3])
            else:
                out.update(result=parts[1], duration=parts[2])
        return out

    def _b3799_nav_payload_date(self, payload):
        if not payload:
            return ""
        if payload[0] == "session":
            return str(payload[1] or "")
        if payload[0] in ("boss", "live_boss"):
            try:
                return str(tuple(payload[1] or ())[2] or "")
            except Exception:
                return ""
        if payload[0] == "live_try":
            # Find the parent boss/date in the hidden model tree; this avoids
            # teaching the canvas a second copy of identity logic.
            try:
                for iid, pl in self.nav_tree_payload.items():
                    if pl == payload:
                        parent = self.nav_tree.parent(iid)
                        if parent:
                            parent = self.nav_tree.parent(parent)
                        if parent:
                            return str((self.nav_tree_payload.get(parent) or (None, ""))[1] or "")
                        break
            except Exception:
                pass
        return ""

    def _b3799_render_session_nav(self, preserve_anchor=False):
        """Draw board-1 style session explorer while keeping Treeview as model.

        This intentionally changes presentation only.  Navigation payloads,
        selection ownership, live-stability rules and tree refresh logic remain
        exactly the B37.3.9.8 code path.
        """
        if not hasattr(self, "nav_canvas") or getattr(self, "_b3799_nav_rendering", False):
            return
        c = self.nav_canvas
        self._b3799_nav_rendering = True
        try:
            width = max(_px(170), int(c.winfo_width() or 0))
            if width <= _px(2):
                width = max(_px(170), self._sidebar_width - _px(28))

            selected_payload = self._b3799_nav_selected_payload()
            selected_date = self._b3799_nav_payload_date(selected_payload)

            # First render: board 1 opens the newest/current day only. Once the
            # user expands older days we preserve that state.
            top_nodes = list(self.nav_tree.get_children("")) if hasattr(self, "nav_tree") else []
            date_texts = [str(self.nav_tree.item(i, "text") or "") for i in top_nodes]
            if not getattr(self, "_b3799_nav_initialized", False):
                if date_texts:
                    self._b3799_open_dates.add(date_texts[0])
                if selected_date:
                    self._b3799_open_dates.add(selected_date)
                self._b3799_nav_initialized = True

            old_anchor_payload = None
            old_anchor_offset = None
            old_top = 0.0
            if preserve_anchor:
                try:
                    old_top = float(c.canvasy(0))
                    for old in getattr(self, "_b3799_nav_rows", ()):
                        if old.get("payload") == selected_payload:
                            old_anchor_payload = selected_payload
                            old_anchor_offset = float(old["y0"]) - old_top
                            break
                except Exception:
                    old_anchor_payload = None
                    old_anchor_offset = None

            c.delete("all")
            rows = []
            y = 0
            current_date = time.strftime("%Y-%m-%d")

            def selected(pl):
                return pl is not None and pl == selected_payload

            for date_iid in top_nodes:
                date = str(self.nav_tree.item(date_iid, "text") or "")
                date_pl = self.nav_tree_payload.get(date_iid)
                h = _px(36)
                bg = "#242a32" if not selected(date_pl) else "#2a3038"
                c.create_rectangle(0, y, width, y + h, fill=bg, outline="")
                is_open = date in self._b3799_open_dates
                # One disclosure glyph only.  The previous row showed two
                # different arrow shapes at opposite edges for one state.
                c.create_text(_px(7), y + h / 2, text="▾" if is_open else "▸", fill=_ui_theme.MUTED,
                              font=(FONT_BODY, 10), anchor="w")
                date_label = date + (" · 오늘" if date == current_date else "")
                c.create_text(_px(24), y + h / 2, text=date_label, fill=_ui_theme.TEXT,
                              font=(FONT_TITLE, 11), anchor="w")
                rows.append({"iid": date_iid, "payload": date_pl, "y0": y, "y1": y + h})
                y += h + _px(4)
                if not is_open:
                    continue

                # B37.4.7: the hidden Treeview can legitimately contain more
                # than one context node for the same boss (unknown/known spec,
                # Historical/local representations).  The visible sidebar is
                # boss-centric, so coalesce identical boss labels and hide
                # reset/fake marker rows from ordinary navigation.  Their count
                # is still reported in the session subtitle and raw data stays
                # preserved for audit.
                boss_groups = []
                boss_group_index = {}
                for boss_iid in self.nav_tree.get_children(date_iid):
                    boss_text = str(self.nav_tree.item(boss_iid, "text") or "")
                    group = boss_group_index.get(boss_text)
                    if group is None:
                        group = {
                            "text": boss_text,
                            "payload": self.nav_tree_payload.get(boss_iid),
                            "items": [],
                            "iids": [],
                        }
                        boss_group_index[boss_text] = group
                        boss_groups.append(group)
                    group["iids"].append(boss_iid)
                    for child_iid in self.nav_tree.get_children(boss_iid):
                        parsed = self._b3799_nav_parse_try_text(self.nav_tree.item(child_iid, "text"))
                        result = parsed.get("result") or ""
                        excluded = result in ("리셋", "가짜 풀") or "excluded" in tuple(self.nav_tree.item(child_iid, "tags") or ())
                        if excluded:
                            continue
                        group["items"].append((child_iid, parsed))

                for group in boss_groups:
                    if not group["items"]:
                        continue
                    boss_iid = group["iids"][0]
                    boss_pl = group["payload"]
                    boss_text = group["text"]
                    children = [x[0] for x in group["items"]]
                    child_parsed = [x[1] for x in group["items"]]
                    visible_try_counts = {}
                    for _parsed in child_parsed:
                        _try_text = str(_parsed.get("try") or "Try ?")
                        visible_try_counts[_try_text] = visible_try_counts.get(_try_text, 0) + 1
                    has_kill = any(_ui_result_en(x.get("result")) == "Kill" for x in child_parsed)
                    status = "Kill" if has_kill else ""
                    status_color = _ui_theme.RESULT_KILL if has_kill else _ui_theme.RESULT_WIPE
                    h = _px(30)
                    if selected(boss_pl):
                        c.create_rectangle(0, y, width, y + h, fill="#2a3038", outline="")
                    c.create_text(_px(10), y + h / 2, text=boss_text, fill=_ui_theme.TEXT,
                                  font=(FONT_BODY, 10), anchor="w")
                    if status:
                        c.create_text(width - _px(9), y + h / 2, text=status, fill=status_color,
                                      font=(FONT_BODY, 9), anchor="e")
                    rows.append({"iid": boss_iid, "payload": boss_pl, "y0": y, "y1": y + h})
                    y += h

                    for child_iid, parsed in zip(children, child_parsed):
                        pl = self.nav_tree_payload.get(child_iid)
                        result = parsed.get("result") or ""
                        excluded = False
                        h = _px(30)
                        if selected(pl):
                            c.create_rectangle(0, y, width, y + h, fill="#2a3038", outline="")
                        marker_x = _px(24)
                        marker_y = y + h / 2
                        if _ui_result_en(result) == "Kill":
                            marker_color = _ui_theme.RESULT_KILL
                            c.create_oval(marker_x - _px(4), marker_y - _px(4), marker_x + _px(4), marker_y + _px(4),
                                          fill=marker_color, outline="")
                        else:
                            display_result = _ui_result_en(result)
                            marker_color = "#F4B23A" if display_result == "Wipe" else (_ui_theme.RESULT_EARLY_WIPE if display_result in ("Early Wipe", "Reset") else "#4c5560")
                            c.create_rectangle(marker_x - _px(4), marker_y - _px(4), marker_x + _px(4), marker_y + _px(4),
                                               fill=marker_color, outline="")
                        fg = "#6f7883" if excluded else _ui_theme.TEXT
                        try_label = parsed.get("try") or "Try ?"
                        if parsed.get("clock") and visible_try_counts.get(str(parsed.get("try") or "Try ?"), 0) > 1:
                            try_label += f" · {parsed.get('clock')}"
                        c.create_text(_px(34), marker_y, text=try_label, fill=fg,
                                      font=(FONT_BODY, 10), anchor="w")
                        # B37.4.37: duplicate historical Try numbers must be visibly
                        # distinguishable.  Only duplicate rows carry the HH:MM suffix,
                        # so ordinary rows keep the compact three-column layout.
                        result_color = (_ui_theme.RESULT_WIPE if _ui_result_en(result) == "Wipe" else marker_color) if not excluded else "#66707b"
                        c.create_text(width - _px(65), marker_y, text=_ui_result_en(result), fill=result_color,
                                      font=(FONT_BODY, 9), anchor="e")
                        c.create_text(width - _px(8), marker_y, text=parsed.get("duration") or "", fill=fg,
                                      font=(FONT_BODY, 9), anchor="e")
                        rows.append({"iid": child_iid, "payload": pl, "y0": y, "y1": y + h})
                        y += h
                    y += _px(5)
                y += _px(2)

            self._b3799_nav_rows = rows
            c.configure(scrollregion=(0, 0, width, max(y, int(c.winfo_height() or 0))))

            # Same B37.3.9.8 guarantee, now applied to the visible PDF-style
            # surface: inserting rows above must not move the selected line.
            if preserve_anchor and old_anchor_payload is not None and old_anchor_offset is not None:
                new_row = next((r for r in rows if r.get("payload") == old_anchor_payload), None)
                if new_row:
                    total = max(1.0, float(max(y, c.winfo_height())))
                    desired_top = max(0.0, float(new_row["y0"]) - old_anchor_offset)
                    c.yview_moveto(min(1.0, desired_top / total))
            elif preserve_anchor and old_top > 0:
                total = max(1.0, float(max(y, c.winfo_height())))
                c.yview_moveto(min(1.0, old_top / total))
        finally:
            self._b3799_nav_rendering = False

    def _b3797_dot(self, result):
        """Small coloured dot per result for the 세션 탐색 tree (board 1)."""
        colors = {"Wipe": _ui_theme.ORANGE, "Kill": _ui_theme.BLUE, "Early Wipe": "#8a929c", "Reset": "#8a929c", "와이프": _ui_theme.ORANGE, "킬": _ui_theme.BLUE, "초반 전멸": "#8a929c"}
        color = colors.get(result, "#4a525d")
        cache = getattr(self, "_b3797_dots", None)
        if cache is None:
            cache = self._b3797_dots = {}
        if color not in cache:
            size = 16
            img = tk.PhotoImage(width=size, height=size)
            cx = cy = (size - 1) / 2
            for y in range(size):
                for x in range(size):
                    if ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 <= 4.6:
                        img.put(color, to=(x, y))
            cache[color] = img
        return cache[color]

    def _b3798_row_signature(self, row, extra=()):
        """Cheap identity for 'did anything shown for this Try change?'."""
        row = row or {}
        stamps = []
        for key in ("raidReviewJson", "mechanicReviewJson", "raidFactsJson", "raidSeriesReviewJson"):
            path = row.get(key)
            try:
                st = os.stat(str(path)) if path else None
                stamps.append((key, int(st.st_mtime_ns), st.st_size) if st else (key, None))
            except Exception:
                stamps.append((key, None))
        return (self._live_row_key(row), row.get("result"), row.get("durationSec"), row.get("raidReviewStatus"),
                row.get("factsStatus"), tuple(stamps)) + tuple(extra)

    def _b3796_clear(self, frame):
        for w in frame.winfo_children():
            w.destroy()

    def _b3796_fallback_events(self, row):
        """single_root / attrition reviews have no chainEvents: rebuild steps
        from the primary finding evidence + collapse cluster (same B31 data)."""
        import re as _re
        try:
            rv = row_json(row or {}, "raidReviewJson") or {}
        except Exception:
            rv = {}
        pf = rv.get("primaryFinding") or {}
        col = rv.get("collapse") or {}
        out = []
        start = col.get("startSec")
        for line in pf.get("evidence") or []:
            text = str(line).strip()
            m = _re.match(r"^(\d+):(\d+(?:\.\d+)?)\s+(.*)$", text)
            if m:
                t = int(m.group(1)) * 60 + float(m.group(2))
                body_text = m.group(3)
                follow = body_text.startswith("후속")
                hot = (not follow) and not any(e.get("hot") for e in out)
                out.append({"kind": "card", "t": t, "hot": hot,
                            "label": "공대 붕괴 시작" if hot else ("후속" if follow else "사건"),
                            "title": body_text.replace("후속 결과: ", "").strip(), "detail": ""})
            elif out:
                out[-1]["detail"] = (out[-1]["detail"] + "\n" if out[-1]["detail"] else "") + text
        if not out and pf.get("title"):
            out.append({"kind": "card", "t": pf.get("sec"), "hot": True, "label": "공대 붕괴 시작",
                        "title": str(pf.get("title")), "detail": str(pf.get("interpretation") or "")})
        if col.get("count"):
            spells = col.get("spells") or {}
            top = " · ".join(f"{k} {v}" for k, v in sorted(spells.items(), key=lambda kv: -kv[1])[:3])
            end = col.get("endSec")
            span = f"{self._fmt_live_relative(start)}~{self._fmt_live_relative(end)}" if start is not None and end is not None else ""
            out.append({"kind": "card", "t": end if end is not None else start, "hot": False,
                        "label": "결과 · 공대 붕괴", "title": f"{int(col.get('count'))}명 사망 {span}".strip(), "detail": top})
        return out

    def _render_b3796_chain(self, row):
        """Board 3 붕괴 체인 from the B31 try layout model (presentation only)."""
        if not hasattr(self, "b3796_chain_panel"):
            return
        sig = self._b3798_row_signature(row, extra=("chain", self.current_player))
        if sig == getattr(self, "_b3798_chain_sig", None):
            return
        self._b3798_chain_sig = sig
        body = self.b3796_chain_panel.body
        self._b3796_clear(body)
        links = self.b3796_link_panel.body
        self._b3796_clear(links)
        model = {}
        try:
            model = self._review_model_for_row(row) or {}
        except Exception:
            model = {}
        fmt = self._fmt_live_relative
        me = str(self.current_player or "").split("-")[0]
        events = list(model.get("chainEvents") or [])
        if not events:
            events = self._b3796_fallback_events(row)
        if not events:
            _v = model.get("verdict") or {}
            _msg = str(_v.get("title") or "연결 근거가 있는 체인 없음")
            _why = str(_v.get("interpretation") or "아래 B31 원본에서 확인")
            tk.Label(body, text=_msg, bg=_ui_theme.PANEL_BG, fg=_ui_theme.TEXT, font=(FONT_TITLE, 11), anchor="w", justify="left", wraplength=_px(560)).pack(fill="x")
            tk.Label(body, text=_why, bg=_ui_theme.PANEL_BG, fg=_ui_theme.MUTED, font=(FONT_BODY, 9), anchor="w", justify="left", wraplength=_px(560)).pack(fill="x", pady=_px((4, 0)))
        for ev in events:
            t = ev.get("t")
            kind, title, detail, link, hot = "후속", "", "", "", False
            if ev.get("kind") == "card":
                kind, title, detail, hot = ev.get("label") or "", ev.get("title") or "", ev.get("detail") or "", bool(ev.get("hot"))
                link = "타임라인 →" if hot else ""
            elif ev.get("kind") == "root":
                kind, hot = "공대 붕괴 시작", True
                title = f"{ev.get('title') or '원인'}" + (f" → {ev.get('detail')}" if ev.get("detail") else "")
                link = "타임라인 →"
            elif str(ev.get("type") or "").upper() == "WIPE":
                kind = "결과 · 전멸 확정"
                after = ev.get("secAfterRoot")
                title = "전멸 확정" + (f" · 붕괴 후 {float(after):.1f}초" if after is not None else "")
            elif ev.get("type") == "under_share":
                deaths = list(ev.get("deaths") or [])
                title = f"{ev.get('mechanic') or '분담'} 분담 {ev.get('shareCount', '?')}명 → {len(deaths)}명 추가 사망"
                detail = ", ".join(str(d).split("-")[0] for d in deaths)
                link = "타임라인 →"
            elif ev.get("player"):
                pname = str(ev.get("player")).split("-")[0]
                title = f"{pname} 사망"
                detail = str(ev.get("mechanic") or "")
                link = "개인 분석 →" if pname == me else ""
            else:
                title = str(ev.get("title") or ev.get("mechanic") or "사건")
                detail = str(ev.get("detail") or "")
            line = tk.Frame(body, bg=_ui_theme.PANEL_BG)
            line.pack(fill="x", pady=_px((0, 8)))
            tk.Label(line, text=fmt(t) if t is not None else "-", bg=_ui_theme.PANEL_BG, fg=_ui_theme.ORANGE if hot else _ui_theme.MUTED, font=("Consolas", 10), width=7, anchor="nw").pack(side="left", anchor="n", pady=_px((8, 0)))
            card = RoundBox(line, fill=_ui_theme.HILITE_BG if hot else _ui_theme.CARD_BG, outline=_ui_theme.HILITE_BORDER if hot else _ui_theme.CARD_BORDER, radius=8, padx=14, pady=10)
            card.pack(side="left", fill="x", expand=True)
            cbg = card.cget("bg")
            top = tk.Frame(card.inner, bg=cbg)
            top.pack(fill="x")
            tk.Label(top, text=kind, bg=cbg, fg=_ui_theme.ORANGE if hot else _ui_theme.MUTED, font=(FONT_BODY, 8), anchor="w").pack(side="left")
            if link:
                tk.Label(top, text=link, bg=cbg, fg=_ui_theme.BLUE, font=(FONT_BODY, 8)).pack(side="right")
            tk.Label(card.inner, text=title, bg=cbg, fg=_ui_theme.TEXT, font=(FONT_TITLE, 11), anchor="w", justify="left", wraplength=_px(560)).pack(fill="x", pady=_px((2, 0)))
            if detail:
                tk.Label(card.inner, text=detail, bg=cbg, fg="#c9cfd6", font=(FONT_BODY, 9), anchor="w", justify="left", wraplength=_px(560)).pack(fill="x", pady=_px((2, 0)))

        side_events = list(model.get("sideEvents") or [])
        if side_events:
            box = RoundBox(body, fill=_ui_theme.PANEL_BG, outline=_ui_theme.CARD_BORDER, radius=8, padx=14, pady=10)
            box.pack(fill="x", pady=_px((4, 0)))
            tk.Label(box.inner, text="같은 구간 사망 · 연결 근거 없음 (체인에 넣지 않음)", bg=_ui_theme.PANEL_BG, fg=_ui_theme.MUTED, font=(FONT_BODY, 8), anchor="w").pack(fill="x")
            for ev in side_events[:6]:
                r = tk.Frame(box.inner, bg=_ui_theme.PANEL_BG)
                r.pack(fill="x", pady=_px((4, 0)))
                who = str(ev.get("player") or "?").split("-")[0]
                tk.Label(r, text=f"{who} · {ev.get('mechanic') or '-'}", bg=_ui_theme.PANEL_BG, fg=_ui_theme.TEXT, font=(FONT_BODY, 9), anchor="w").pack(side="left")
                tk.Label(r, text=fmt(ev.get("t")) if ev.get("t") is not None else "-", bg=_ui_theme.PANEL_BG, fg=_ui_theme.MUTED, font=("Consolas", 9)).pack(side="right")

        verdict = model.get("verdict") or {}
        self.b3796_next_var.set(str(verdict.get("action") or "현재 검증된 다음 행동 없음"))

        def link_row(title, text):
            tk.Label(links, text=title, bg=_ui_theme.PANEL_BG, fg=_ui_theme.TEXT, font=(FONT_TITLE, 10), anchor="w").pack(fill="x", pady=_px((0, 2)))
            tk.Label(links, text=text, bg=_ui_theme.PANEL_BG, fg=_ui_theme.MUTED, font=(FONT_BODY, 9), anchor="w", justify="left", wraplength=_px(280)).pack(fill="x", pady=_px((0, 10)))

        root = str(model.get("rootMechanic") or "").strip()
        if root and not root.startswith("처치 성공"):
            same = []
            try:
                for other in self._b3641_context_rows(all_dates=False):
                    om = self._review_model_for_row(other) or {}
                    if str(om.get("rootMechanic") or "").strip() == root:
                        same.append(other)
            except Exception:
                same = []
            others = [f"Try {o.get('tryNo')}" for o in same if o.get("tryNo") != row.get("tryNo")]
            link_row(f"같은 원인 · {len(same)}회", (" · ".join(others[:6]) + " · 보스 트라이 보드에서 선택") if others else "이 세션에서 이 트라이만 해당")
        death = model.get("selectedPlayerDeath") or {}
        if death and death.get("sec") is not None:
            link_row(f"개인 · {me} 사망 체인", f"{fmt(death.get('sec'))} 사망 · 개인 탭에서 상세")
        elif me:
            link_row(f"개인 · {me}", "이 트라이에서 사망 없음")
        end = model.get("cooldownEndSec")
        if end is not None:
            reason = "개인 사망 이후 제외" if model.get("cooldownReason") == "player_death" else "전투 종료까지"
            link_row("쿨기 비교", f"비교 구간 0:00~{fmt(end)} · {reason}")

    def _render_b3641_try_priority_core(self, row):
        if not hasattr(self, "b3641_first_var"):
            return
        # B37.5 preview2: keep the selected persisted row so the compact
        # timeline can reuse raid_facts.json for named death tooltips without
        # changing/re-judging BossMechanic semantics.
        self._b3641_try_row = dict(row or {})
        _judgment_key = (
            str((row or {}).get("sourceKey") or ""),
            str((row or {}).get("date") or (row or {}).get("sessionDate") or ""),
            str((row or {}).get("bossKey") or (row or {}).get("encounterID") or ""),
            str((row or {}).get("tryNo") or ""),
            str((row or {}).get("startTimestamp") or (row or {}).get("startTime") or ""),
            str((row or {}).get("storageDir") or ""),
        )
        if _judgment_key != getattr(self, "_b3750_judgment_try_key", None):
            self._b3750_judgment_try_key = _judgment_key
            self._b3750_judgments_expanded = False
        self._refresh_b3642_try_navigation(row)
        # B37.4.7: reset/fake marker pulls are preserved for audit only.  A
        # zero-event marker cannot prove that a mechanic was handled correctly,
        # so never paint absence-based green success cards for excluded pulls.
        if row and (self._live_row_is_fake(row) or self._live_row_is_reset(row)):
            is_fake = self._live_row_is_fake(row)
            kind = "가짜 풀" if is_fake else "리셋"
            self._b3641_try_review = {}
            self._b3641_try_model = {
                "supported": False,
                "cards": [],
                "timeline": [],
                "evidence": [],
                "durationSec": float(row.get("durationSec") or 0.0),
            }
            self.b3641_try_scope_var.set(f"{kind} · 기본 집계/기믹 판정 제외")
            self.b3641_first_var.set("첫 실수 · 적용 안 함")
            self.b3641_major_var.set("주요 사고 · 적용 안 함")
            self.b3641_collapse_var.set("붕괴 직결 · 적용 안 함")
            self.b3641_footer_var.set("원본과 사실 관측은 보존하지만, 제외된 풀의 미관측을 성공 근거로 사용하지 않습니다.")
            for target in self.b3641_try_cards.values():
                target["titleVar"].set("판정 제외")
                target["valueVar"].set("적용 안 함")
                target["noteVar"].set("")
                target["frame"].grid_remove()
            if hasattr(self, "b365_shield_box"):
                self.b365_shield_box.configure(text="BossMechanic · 제외된 풀")
            if hasattr(self, "b3641_evidence_box"):
                self.b3641_evidence_box.configure(text="판정 근거 · 제외")
            if hasattr(self, "b3642_compare_box"):
                self.b3642_compare_box.configure(text="최근 킬 / 현재 트라이 · 비교 제외")
            self._replace_readonly_text(self.b365_shield_panel, f"{kind}은 기믹 성공/실패 표본에서 제외합니다.")
            self._replace_readonly_text(
                self.b3641_try_evidence,
                f"{kind} · duration {self._fmt_live_duration(row.get('durationSec'))} · 원본은 보존됨\n"
                "미관측 0회는 정상 처리로 승격하지 않습니다."
            )
            self._replace_readonly_text(self.b3642_compare_text, f"{kind}은 킬/와이프 기믹 비교 표본에서 제외합니다.")
            self._draw_b3641_try_timeline()
            return
        review = load_mechanic_review(row) if row else {}
        self._b3641_try_review = review if isinstance(review, dict) else {}
        model = build_try_priority_model(review, row or {}) if review else {"supported": False, "cards": [], "timeline": [], "evidence": []}
        try:  # 1.0.6 (GOLD V5.2.8): 전용 판정 없는 보스(키식스 등)도 시간축 끝 = 전투 시간
            from live.v528_review import fill_duration as _v528_fill
            model = _v528_fill(model, row, review)
        except Exception:
            pass
        self._b3641_try_model = model
        if not model.get("supported"):
            self.b3641_try_scope_var.set("현재 트라이 · 전용 기믹 판정 데이터 없음")
            self.b3641_first_var.set("첫 실수 · 확인 불가")
            self.b3641_major_var.set("주요 사고 · 확인 불가")
            self.b3641_collapse_var.set("붕괴 직결 · 확인 불가")
            self.b3641_footer_var.set("B31 상세 복기는 아래 펼치기에서 그대로 확인할 수 있습니다.")
            # V4.9: 엔진 기믹 규칙이 없는 보스(키식스 등)는 「판정 불가 ?」 칸 11개 대신 안내 칸 하나만 보인다.
            for _v49_i, target in enumerate(self.b3641_try_cards.values()):
                target["titleVar"].set("엔진 기믹 판정 없음" if _v49_i == 0 else "판정 불가")
                target["valueVar"].set("–" if _v49_i == 0 else "?   ?")
                target["noteVar"].set("결론 5줄·시간축 참고" if _v49_i == 0 else "")
                bg, fg, _ = self._b3641_status_colors("unknown")
                target["frame"].configure(bg=bg, highlightbackground=fg)
                for k in ("title", "value", "note"):
                    target[k].configure(bg=bg)
                if _v49_i == 0:
                    target["frame"].grid()
                else:
                    target["frame"].grid_remove()
            self._replace_readonly_text(self.b3641_try_evidence, "전용 기믹 판정 근거 없음")
            if hasattr(self, "b365_shield_panel"):
                self._replace_readonly_text(self.b365_shield_panel, "보조 관측 데이터 없음")
            self._apply_b3739_simple_try_summary(row, review)
            self._render_b3642_compare(row, review)
            self._draw_b3641_try_timeline()
            return

        # Always clear static card slots before painting another boss.  B36.4's
        # 11 physical slots are reused by B37 generic BossMechanic reviews, with
        # the visible title/value coming from each boss check.
        for target in self.b3641_try_cards.values():
            target["titleVar"].set("판정 불가")
            target["valueVar"].set("?   ?")
            target["noteVar"].set("")
            bg, fg, _ = self._b3641_status_colors("unknown")
            target["frame"].configure(bg=bg, highlightbackground=fg)
            for k in ("title", "value", "note"):
                target[k].configure(bg=bg)
            # Generic bosses often expose fewer than the 11 Coiled slots.
            # Hide unused physical slots instead of filling the screen with
            # meaningless "판정 불가 ?" cards; _set_b3641_card restores used slots.
            target["frame"].grid_remove()

        if model.get("generic"):
            if hasattr(self, "b365_shield_box"):
                self.b365_shield_box.configure(text="BossMechanic · 보조 관측")
            if hasattr(self, "b3641_evidence_box"):
                self.b3641_evidence_box.configure(text="판정 근거 · 한계")
            if hasattr(self, "b3642_compare_box"):
                self.b3642_compare_box.configure(text="최근 킬 / 현재 트라이 · 비교")
            self.b3641_try_scope_var.set(str(model.get("scopeText") or "BossMechanic · 범위: 현재 트라이"))
            self.b3641_first_var.set("첫 실수 · " + str(model.get("firstMistakeText") or "확인 불가"))
            self.b3641_major_var.set("주요 사고 · " + str(model.get("majorIncidentText") or "확인 불가"))
            self.b3641_collapse_var.set("붕괴 직결 · " + str(model.get("collapseText") or "확인 불가"))
            self.b3641_footer_var.set(str(model.get("footer") or ""))
            # 1.0.6 (GOLD NINI_GRID_HF1): compact the actual generic boss cards so Coiled-only
            # slots do not leave holes in the five-column layout.
            _visible_cols = 2 if self._compact_ui else 5
            _visible_slot = 0
            for card in model.get("cards") or []:
                _widget = self.b3641_try_cards.get(card.get("key"))
                if _widget is not None:
                    _widget["frame"].grid_configure(
                        row=_visible_slot // _visible_cols,
                        column=_visible_slot % _visible_cols)
                    _visible_slot += 1
                self._set_b3641_card(card)
            self._replace_readonly_text(self.b365_shield_panel, str(model.get("sidePanelText") or "보조 관측 없음"))
            self._replace_readonly_text(self.b3641_try_evidence, str(model.get("evidenceText") or "추가 근거 없음"))
            self._replace_readonly_text(
                self.b3642_compare_text,
                "최근 킬/현재 트라이 비교는 검증된 보스별 기준점만 표시합니다."
            )
            self._apply_b3739_simple_try_summary(row, review)
            self._draw_b3641_try_timeline()
            return

        # Coiled uses all fixed slots; restore them after a generic boss may
        # have hidden unused cards.
        # 1.0.6 (GOLD NINI_GRID_HF1): restore original Coiled slot positions when switching
        # back from a compacted generic boss. All existing Coiled cards remain.
        _coiled_cols = 2 if self._compact_ui else 5
        for _slot_idx, target in enumerate(self.b3641_try_cards.values()):
            target["frame"].grid_configure(
                row=_slot_idx // _coiled_cols, column=_slot_idx % _coiled_cols)
            target["frame"].grid()

        if hasattr(self, "b365_shield_box"):
            self.b365_shield_box.configure(text="P2 보호막 · 영원한 일몰 / 보호막 쿨기 정렬")
        if hasattr(self, "b3641_evidence_box"):
            self.b3641_evidence_box.configure(text="근거 · 현신 접촉")
        if hasattr(self, "b3642_compare_box"):
            self.b3642_compare_box.configure(text="최근 킬 / 현재 트라이 · P2 상대시간")

        # Local-session scope is intentionally kept separate from board/session aggregates.
        reached = []
        try:
            reached = self._v42_p2_reached_labels()
        except Exception:
            pass
        scope = f"P2 {int(model.get('focusCycleIndex') or 1)}사이클 · 범위: 로컬 세션"
        if reached:
            scope += " · P2 도달 " + "·".join(reached)
        scope += f" · n={len(reached)}"
        self.b3641_try_scope_var.set(scope)
        self.b3641_first_var.set("첫 실수 · " + str(model.get("firstMistakeText") or "확인 불가"))
        self.b3641_major_var.set("주요 사고 · " + str(model.get("majorIncidentText") or "확인 불가"))
        self.b3641_collapse_var.set("붕괴 직결 · " + str(model.get("collapseText") or "확인 불가"))
        self.b3641_footer_var.set(str(model.get("footer") or ""))
        for card in model.get("cards") or []:
            self._set_b3641_card(card)

        if hasattr(self, "b365_shield_panel"):
            panel = model.get("shieldPanel") or {}
            lines = [str(panel.get("summary") or "P2 보호막 판정 대기")]
            symbols = {"success": "✓", "failed": "✗", "warning": "!", "unknown": "?"}
            for cyc in panel.get("cycles") or []:
                status = str(cyc.get("status") or "unknown")
                lines.append(f"#{cyc.get('index')} {symbols.get(status, '?')} {cyc.get('summary') or ''}".rstrip())
                lines.append(f"  쿨기 정렬 {symbols.get(str(cyc.get('cooldownStatus') or 'unknown'), '?')} · {cyc.get('cooldownSummary') or '확인 불가'}")
                if cyc.get("autoCooldownDetail"):
                    lines.append(f"  자동 2분쿨 · {cyc.get('autoCooldownDetail')}")
            if not (panel.get("cycles") or []):
                for slot in (panel.get("plannedSlots") or [])[:2]:
                    idx = slot.get("index") or "?"
                    off = slot.get("targetOffsetSec")
                    state = str(slot.get("state") or "")
                    if state == "awaiting" and slot.get("timeToTargetAtEndSec") is not None:
                        lines.append(f"# {idx} 예정 · P2+{float(off):.0f}초 · 종료 시 약 {float(slot.get('timeToTargetAtEndSec')):.1f}초 남음")
                    elif state == "target_passed_unobserved":
                        lines.append(f"# {idx} 예정 · P2+{float(off):.0f}초 · 예상 구간 통과 / 신호 미관측")
                    elif off is not None:
                        lines.append(f"# {idx} 예정 · P2+{float(off):.0f}초 · 관측 대기")
                if panel.get("verificationFields"):
                    lines.append("관측 대기 · 흡수량 / 보호막 파괴 / 일몰 시전시간·차단 / NSRT 편차")
            self._replace_readonly_text(self.b365_shield_panel, "\n".join(lines))

        evidence_lines = []
        for ev in model.get("evidence") or []:
            pair = " ↔ ".join(ev.get("pair") or []) or "대상 확인 불가"
            deaths = ", ".join(ev.get("deaths") or []) or "사망 없음"
            line = f"{b3641_fmt_clock(ev.get('sec'))} Malevolent Resonance · {pair} → {deaths}"
            if ev.get("distanceStart") is not None and ev.get("distanceEnd") is not None:
                line += f"\n  접촉 5초 전 플레이어 proxy 거리 {float(ev['distanceStart']):.1f} → {float(ev['distanceEnd']):.1f}야드"
            if ev.get("movement"):
                line += " · 이동량 " + " / ".join(ev["movement"])
            line += "\n  " + str(ev.get("note") or "")
            evidence_lines.append(line)
        card_detail = str(model.get("evidenceText") or "").strip()
        if card_detail:
            evidence_lines.append(card_detail)
        self._replace_readonly_text(self.b3641_try_evidence, "\n\n".join(evidence_lines) if evidence_lines else "현신 접촉 근거 없음")
        self._apply_b3739_simple_try_summary(row, review)
        self._render_b3642_compare(row, review)
        self._draw_b3641_try_timeline()

    def _cancel_b3750_tooltip_hide(self, _event=None):
        after_id = getattr(self, "b3750_tooltip_hide_after_id", None)
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self.b3750_tooltip_hide_after_id = None

    def _hide_b3641_timeline_tooltip(self, _event=None):
        self._cancel_b3750_tooltip_hide()
        win = getattr(self, "b3641_timeline_tooltip", None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        self.b3641_timeline_tooltip = None
        self.b3750_tooltip_source_canvas = None
        self.b3750_tooltip_source_item = None
        self.b3750_tooltip_source_key = None

    def _b3750_pointer_still_owns_tooltip(self):
        """Return True while the pointer is geometrically over the source item/tooltip.

        Windows can emit <Leave> for a Canvas item merely because an
        override-redirect Toplevel is mapped.  Event state alone is therefore
        not reliable.  Check the actual pointer against the original canvas
        item's bbox before closing.
        """
        try:
            px, py = self.winfo_pointerxy()
            px, py = int(px), int(py)
        except Exception:
            return False

        canvas = getattr(self, "b3750_tooltip_source_canvas", None)
        item = getattr(self, "b3750_tooltip_source_item", None)
        if canvas is not None and item is not None:
            try:
                if int(canvas.winfo_ismapped()):
                    cx = float(canvas.canvasx(px - int(canvas.winfo_rootx())))
                    cy = float(canvas.canvasy(py - int(canvas.winfo_rooty())))
                    bbox = canvas.bbox(item)
                    if bbox and len(bbox) == 4:
                        x1, y1, x2, y2 = [float(v) for v in bbox]
                        # Small tolerance absorbs antialiased/icon-edge movement.
                        if x1 - 3 <= cx <= x2 + 3 and y1 - 3 <= cy <= y2 + 3:
                            return True
            except Exception:
                pass

        win = getattr(self, "b3641_timeline_tooltip", None)
        if win is not None:
            try:
                if int(win.winfo_exists()) and int(win.winfo_ismapped()):
                    wx, wy = int(win.winfo_rootx()), int(win.winfo_rooty())
                    ww, wh = int(win.winfo_width()), int(win.winfo_height())
                    if wx <= px < wx + max(1, ww) and wy <= py < wy + max(1, wh):
                        return True
            except Exception:
                pass
        return False

    def _b3750_maybe_hide_tooltip(self):
        self.b3750_tooltip_hide_after_id = None
        if self._b3750_pointer_still_owns_tooltip():
            return
        self._hide_b3641_timeline_tooltip()

    def _schedule_b3750_tooltip_hide(self, _event=None, delay_ms=160):
        """Delay close, then verify the pointer really left the source item."""
        self._cancel_b3750_tooltip_hide()
        try:
            self.b3750_tooltip_hide_after_id = self.after(
                int(delay_ms), self._b3750_maybe_hide_tooltip
            )
        except Exception:
            self._b3750_maybe_hide_tooltip()

    def _b3750_set_tooltip_source(self, canvas=None, item=None, key=None):
        self.b3750_tooltip_source_canvas = canvas
        self.b3750_tooltip_source_item = item
        self.b3750_tooltip_source_key = key

    def _b3750_same_tooltip_source(self, key):
        win = getattr(self, "b3641_timeline_tooltip", None)
        if win is None or key is None:
            return False
        try:
            alive = bool(int(win.winfo_exists()))
        except Exception:
            alive = False
        return alive and getattr(self, "b3750_tooltip_source_key", None) == key

    def _arm_b3750_tooltip_hover_guard(self, win):
        """Keep a tooltip alive while the pointer is over any tooltip child."""
        def bind_tree(widget):
            try:
                widget.bind("<Enter>", self._cancel_b3750_tooltip_hide, add="+")
                widget.bind("<Leave>", self._schedule_b3750_tooltip_hide, add="+")
                for child in widget.winfo_children():
                    bind_tree(child)
            except Exception:
                pass
        bind_tree(win)

    def _b3750_set_tooltip_position(self, win, x, y):
        """Move an override-redirect tooltip to an absolute virtual-desktop position.

        On Windows, Tk geometry strings are unreliable for a monitor whose root
        coordinates are negative.  Use SetWindowPos on the real toplevel frame so
        a left/top secondary monitor keeps its actual negative coordinates.
        """
        x, y = int(x), int(y)
        try:
            if sys.platform.startswith("win"):
                import ctypes
                win.update_idletasks()
                try:
                    raw = win.wm_frame()
                    hwnd = int(str(raw), 0) if raw else int(win.winfo_id())
                except Exception:
                    hwnd = int(win.winfo_id())
                # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW
                flags = 0x0001 | 0x0004 | 0x0010 | 0x0040
                if ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, 0, 0, flags):
                    return
        except Exception:
            pass
        # Cross-platform/failure fallback: never emit the invalid '+-123' form.
        try:
            pos = format_tk_position(x, y) if format_tk_position is not None else (
                (f"+{x}" if x >= 0 else str(x)) + (f"+{y}" if y >= 0 else str(y))
            )
            win.wm_geometry(pos)
        except Exception:
            # Last resort: keep the tooltip visible rather than losing the hover.
            win.wm_geometry(f"+{max(0, x)}+{max(0, y)}")

    def _b3750_place_tooltip_in_analyzer(self, event, win, *, offset=14):
        """Keep timeline tooltips inside this analyzer window on multi-monitor desktops."""
        try:
            px, py = int(event.x_root), int(event.y_root)
        except Exception:
            canvas = getattr(self, "b3641_try_timeline", None)
            if canvas is None:
                return None
            px, py = canvas.winfo_rootx() + 10, canvas.winfo_rooty() + 10
        try:
            self.update_idletasks()
            win.update_idletasks()
            ox, oy = int(self.winfo_rootx()), int(self.winfo_rooty())
            ow, oh = int(self.winfo_width()), int(self.winfo_height())
            tw, th = int(win.winfo_reqwidth()), int(win.winfo_reqheight())
            if place_tooltip_within_owner is not None:
                x, y = place_tooltip_within_owner(
                    px, py, tw, th, ox, oy, ow, oh, offset=offset, pad=max(4, _px(8))
                )
            else:
                x, y = px + offset, py + offset
        except Exception:
            x, y = px + offset, py + offset
        self._b3750_set_tooltip_position(win, x, y)
        return int(x), int(y)

    def _show_b3641_timeline_tooltip(self, event, text, source_canvas=None, source_item=None):
        if not text:
            return
        key = ("legacy", str(source_canvas), str(source_item) if source_item is not None else None, str(text))
        if self._b3750_same_tooltip_source(key):
            self._cancel_b3750_tooltip_hide()
            return
        self._hide_b3641_timeline_tooltip()
        win = tk.Toplevel(self)
        win.wm_overrideredirect(True)
        tk.Label(
            win, text=text, justify="left", bg="#111111", fg="#f4f4df",
            relief="solid", bd=1, padx=_px(7), pady=_px(5), font=(FONT_BODY, 9),
        ).pack()
        self._b3750_place_tooltip_in_analyzer(event, win, offset=20)
        self.b3641_timeline_tooltip = win
        self._b3750_set_tooltip_source(source_canvas, source_item, key)
        self._arm_b3750_tooltip_hover_guard(win)

    def _bind_b3641_timeline_tip(self, canvas, item, text):
        if not item or not text:
            return
        canvas.tag_bind(
            item, "<Enter>",
            lambda e, tip=text, c=canvas, iid=item: self._show_b3641_timeline_tooltip(
                e, tip, source_canvas=c, source_item=iid
            ),
        )
        canvas.tag_bind(item, "<Leave>", self._schedule_b3750_tooltip_hide)

    def _b3750_resolve_icon_path(self, item):
        """Prefer the bundled catalog icon, then keep the PREVIEW4 icon cache fallback."""
        catalog_raw = str((item or {}).get("catalogIconPath") or "").strip()
        if catalog_raw:
            try:
                catalog = Path(catalog_raw)
                if catalog.exists() and catalog.is_file() and catalog.stat().st_size > 100:
                    return catalog
            except Exception:
                pass
        icon_name = str((item or {}).get("icon") or "").strip()
        if not icon_name:
            return None
        try:
            p = self._icon_cache_path(icon_name)
            if p.exists() and p.stat().st_size > 100:
                return p
            safe = re.sub(r"[^0-9A-Za-z_-]+", "_", icon_name or "unknown")
            bundled_png = WORKDIR / "data" / "icon_cache" / f"{safe}.png"
            if bundled_png.exists() and bundled_png.stat().st_size > 100:
                return bundled_png
        except Exception:
            return None
        return None

    def _show_b3750_skill_tooltip(self, event, item, reference=False, source_canvas=None, source_item=None):
        """B37.5.0.2: two tooltip roles.

        - 스킬 참고 icon  → 제목 · 분류 · 무엇/결과/대처 · 이 트라이 관측 횟수
        - timeline icon  → 제목 · 시각 · n번째 · 이 트라이(실제 현상) · 판정
        """
        key = (
            "skill", str(source_canvas), str(source_item) if source_item is not None else None,
            bool(reference), str((item or {}).get("glyphKey") or ""),
            str((item or {}).get("spellID") or (item or {}).get("spellId") or ""),
            str((item or {}).get("title") or ""), str((item or {}).get("sec") or (item or {}).get("startSec") or ""),
        )
        if self._b3750_same_tooltip_source(key):
            self._cancel_b3750_tooltip_hide()
            return
        if reference:
            view = build_reference_tooltip_view(item) if build_reference_tooltip_view else None
            if not view:
                return self._show_b3641_timeline_tooltip(
                    event, self._b3750_skill_reference_tip(item), source_canvas=source_canvas, source_item=source_item
                )
        else:
            view = build_event_tooltip_view(item) if build_event_tooltip_view else None
            if not view:
                return self._show_b3641_timeline_tooltip(
                    event, self._b3750_glyph_tip(item), source_canvas=source_canvas, source_item=source_item
                )

        self._hide_b3641_timeline_tooltip()
        BG, MUTED, TEXT, LINE = "#090e13", "#aeb6bf", "#e3e8ed", "#35404a"
        win = tk.Toplevel(self)
        win.wm_overrideredirect(True)
        win.configure(bg=BG)
        outer = tk.Frame(win, bg=BG, highlightbackground="#47515c", highlightthickness=1, bd=0)
        outer.pack(fill="both", expand=True)
        wrap = max(300, _px(410 if reference else 360))

        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x", padx=_px(10), pady=(_px(9), _px(5)))
        icon_path = self._b3750_resolve_icon_path(item)
        if icon_path is not None:
            try:
                from PIL import Image, ImageTk
                side = max(26, _px(38 if reference else 30))
                img = Image.open(icon_path).convert("RGBA").resize((side, side))
                photo = ImageTk.PhotoImage(img)
                icon = tk.Label(head, image=photo, bg=BG, bd=0)
                icon.image = photo
                icon.pack(side="left", padx=(0, _px(8)), anchor="n")
                win._b3750_tooltip_icon = photo
            except Exception:
                pass
        title_box = tk.Frame(head, bg=BG)
        title_box.pack(side="left", fill="x", expand=True)
        title_line = tk.Frame(title_box, bg=BG)
        title_line.pack(fill="x")
        tk.Label(
            title_line, text=view["title"], anchor="w", justify="left", bg=BG,
            fg="#f4d14b", font=(FONT_BODY, 11, "bold"),
        ).pack(side="left")
        if not reference:
            when = view.get("when") or ""
            if view.get("occurrence"):
                when += f"  ·  {view['occurrence']}"
            tk.Label(title_line, text=when, bg=BG, fg=MUTED, font=(FONT_BODY, 9)).pack(side="left", padx=(_px(10), 0))
        chips = list(view.get("chips") or []) if reference else []
        if chips:
            chip_row = tk.Frame(title_box, bg=BG)
            chip_row.pack(fill="x", pady=(_px(3), 0))
            for chip in chips[:5]:
                tk.Label(
                    chip_row, text=chip, bg="#1b2129", fg="#c9d1d9", font=(FONT_BODY, 8),
                    padx=_px(5), pady=0, highlightbackground="#3a424c", highlightthickness=1,
                ).pack(side="left", padx=(0, _px(4)))

        tk.Frame(outer, bg=LINE, height=max(1, _px(1))).pack(fill="x", padx=_px(10), pady=(_px(1), _px(5)))

        def label_row(parent, label, text, fg_label=MUTED, fg_text=TEXT, width=5):
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", padx=_px(10), pady=_px(2))
            tk.Label(row, text=label, width=width, anchor="nw", justify="left", bg=BG,
                     fg=fg_label, font=(FONT_BODY, 9)).pack(side="left", anchor="n")
            tk.Label(row, text=text, anchor="nw", justify="left", bg=BG, fg=fg_text,
                     font=(FONT_BODY, 9), wraplength=wrap).pack(side="left", fill="x", expand=True)

        if reference:
            label_colors = {"무엇": MUTED, "결과": MUTED, "대처": "#4ed5d1"}
            for label, text in view["rows"]:
                label_row(outer, label, text, fg_label=label_colors.get(label, MUTED),
                          fg_text="#d6f3f4" if label == "대처" else TEXT)
            tk.Frame(outer, bg=LINE, height=max(1, _px(1))).pack(fill="x", padx=_px(10), pady=(_px(6), _px(4)))
            tk.Label(outer, text=view.get("footer") or "", anchor="w", justify="left", bg=BG,
                     fg="#7f8994", font=(FONT_BODY, 8), wraplength=wrap + _px(40)).pack(fill="x", padx=_px(10))
        else:
            label_row(outer, "이 트라이", "\n".join(view.get("lines") or []) or "관측됨", width=7)
            tone_fg = {
                "failure": "#ff8a84", "warning": "#f2c46b", "success": "#79c99e",
                "candidate": "#b7a3e0", "neutral": "#d9dee4",
            }.get(view.get("tone"), "#d9dee4")
            label_row(outer, "판정", view.get("judgment") or "", fg_text=tone_fg, width=7)
            tk.Label(outer, text="스킬 설명·대처는 위 '스킬 참고' 아이콘", anchor="w", bg=BG,
                     fg="#5d6670", font=(FONT_BODY, 8)).pack(fill="x", padx=_px(10), pady=(_px(4), 0))

        tk.Frame(outer, bg=BG, height=_px(6)).pack()
        self._b3750_place_tooltip_in_analyzer(event, win, offset=20)
        self.b3641_timeline_tooltip = win
        self._b3750_set_tooltip_source(source_canvas, source_item, key)
        self._arm_b3750_tooltip_hover_guard(win)

    def _bind_b3750_skill_tip(self, canvas, item_id, data, reference=False):
        if not item_id or not data:
            return
        canvas.tag_bind(
            item_id, "<Enter>",
            lambda e, payload=data, is_ref=reference, c=canvas, iid=item_id: self._show_b3750_skill_tooltip(
                e, payload, reference=is_ref, source_canvas=c, source_item=iid
            ),
        )
        canvas.tag_bind(item_id, "<Leave>", self._schedule_b3750_tooltip_hide)

    @staticmethod
    def _b3641_timeline_role(event):
        role = str(event.get("failureRole") or "")
        if role:
            return role
        status = str(event.get("status") or "unknown")
        raw = str(event.get("rawStatus") or status)
        evidence = str(event.get("evidenceLevel") or "")
        if evidence == "verified" and raw == "failed":
            return "additional_failure"
        return ""

    @classmethod
    def _b3641_timeline_lane(cls, event, generic=True):
        role = cls._b3641_timeline_role(event)
        if role or str(event.get("status") or "") in {"failed", "warning"}:
            return "incident"
        kind = str(event.get("kind") or "")
        if kind in {"p2_entry", "phase", "phase_entry", "checkpoint", "plan"}:
            return "plan"
        if not generic and kind in {"guillotine"}:
            return "plan"
        return "actual"

    @staticmethod
    def _b3641_timeline_style(event):
        """Return stable semantic color/marker.  Color means state, never mechanic type."""
        role = str(event.get("failureRole") or "")
        status = str(event.get("status") or "unknown")
        evidence = str(event.get("evidenceLevel") or "")
        kind = str(event.get("kind") or "")
        if role == "collapse_driver":
            return _ui_theme.STATE_FAILURE, "✕", 5
        if role == "recovered_on_kill":
            return _ui_theme.STATE_FAILURE, "○", 4
        if role == "first_mistake":
            return _ui_theme.STATE_FAILURE, "◆", 4
        if role == "additional_failure":
            return _ui_theme.STATE_FAILURE, "△", 3
        if status == "failed":
            return _ui_theme.STATE_FAILURE, "✕", 5
        if status == "warning":
            return _ui_theme.STATE_WARNING, "!", 3
        if evidence == "candidate":
            return _ui_theme.STATE_CANDIDATE, "·", 1
        if kind in {"p2_entry", "phase", "phase_entry"}:
            return _ui_theme.STATE_INFO, "▮", 4
        if status == "success":
            return _ui_theme.STATE_SUCCESS, "│", 2
        return "#a7b0ba", "│", 1

    @classmethod
    def _b3641_timeline_visible(cls, event, mode, generic=True):
        lane = cls._b3641_timeline_lane(event, generic=generic)
        role = cls._b3641_timeline_role(event)
        status = str(event.get("status") or "unknown")
        kind = str(event.get("kind") or "")
        if mode == "전체":
            return True
        if mode == "주요 기믹":
            return lane == "incident" or status == "success" or kind in {"p2_entry", "phase", "phase_entry", "breath", "guillotine", "shield", "shield_break", "manifest"}
        # 핵심: conclusions first; phase transitions are the only neutral labels.
        return bool(role) or status == "failed" or kind in {"p2_entry", "phase", "phase_entry", "nightfall_fail"}

    @staticmethod
    def _b3641_timeline_row_key(event):
        kind = str(event.get("kind") or "mechanic")
        aliases = {
            "p1_orb_failure": "breath",
            "gloombomb_failure": "gloombomb",
            "gloombomb": "gloombomb",
            "interrupt": "gloombomb",
            "wail_fail": "wail",
            "wail_interrupt_failure": "wail",
            "manifest_collision": "manifest",
            "shield_break": "shield",
            "nightfall_fail": "shield",
            "nightfall": "shield",
            "viscous_cyst_chain": "viscous_cyst",
            "crosswind_pair_miss": "crosswinds",
            "serpents_fury_enrage": "serpents_fury",
            "mutilate_under_soak": "mutilate",
        }
        return aliases.get(kind, kind)

    @staticmethod
    def _b3641_timeline_row_title(key, events):
        fixed = {
            "breath": "브레스", "guillotine": "단두대", "gloombomb": "암영폭탄",
            "wail": "통곡", "manifest": "현신 접촉", "shield": "보호막 / 일몰",
            "mutilate": "산개 소크", "viscous_cyst": "점성 낭종",
            "crosswinds": "격노하는 돌풍", "serpents_fury": "뱀의 격노",
            "brood": "새끼 깨우기", "visceral_burst": "내장 파열",
            "corrosive_orb": "부식성 구체", "eternal_venom": "영원의 맹독",
        }
        if key in fixed:
            return fixed[key]
        for e in events:
            title = str(e.get("rowTitle") or e.get("title") or e.get("label") or "").strip()
            if title:
                return title[:24]
        return str(key).replace("_", " ")[:24]

    @staticmethod
    def _b3641_timeline_judgments(events):
        out = []
        for e in sorted(events, key=lambda x: float(x.get("sec") or 0)):
            role = str(e.get("failureRole") or "")
            status = str(e.get("status") or "unknown")
            evidence = str(e.get("evidenceLevel") or e.get("meaningEvidenceLevel") or "")
            if role or (status in {"failed", "warning"} and evidence == "verified"):
                out.append(e)
        return out

    @staticmethod
    def _b3641_timeline_tip(event, grouped_count=1):
        sec = float(event.get("sec") or 0)
        title = str(event.get("label") or event.get("title") or "기믹")
        role = str(event.get("failureRoleLabel") or "")
        evidence = str(event.get("evidenceLevel") or "")
        summary = str(event.get("summary") or "")
        lines = [title, b3641_fmt_clock(sec)]
        if role:
            lines.append(role)
        elif evidence:
            lines.append(evidence)
        if summary and summary not in title:
            lines.append(summary)
        if grouped_count > 1:
            lines.append(f"가까운 사건 {grouped_count}건 묶음 · 전체 보기에서 개별 마커 확인")
        return "\n".join(lines)

    def _set_b3750_timeline_mode(self, mode):
        self.b3641_timeline_mode.set(str(mode) if str(mode) in ("상세", "스킬별") else "아이콘")
        self._draw_b3641_try_timeline()

    def _toggle_b3750_judgments(self, _event=None):
        self._b3750_judgments_expanded = not bool(getattr(self, "_b3750_judgments_expanded", False))
        self._draw_b3641_try_timeline()

    @staticmethod
    def _b3750_glyph_outline(glyph):
        status = str((glyph or {}).get("status") or "unknown")
        evidence = str((glyph or {}).get("evidenceLevel") or "")
        if status == "failed" and evidence == "verified":
            return _ui_theme.STATE_FAILURE
        if status == "warning" and evidence == "verified":
            return _ui_theme.STATE_WARNING
        if status == "success":
            return _ui_theme.STATE_SUCCESS
        if evidence == "candidate":
            return _ui_theme.STATE_CANDIDATE
        return "#7f8994"

    def _b3750_load_glyph_icon(self, glyph, size=20, outline="#7f8994"):
        """Load bundled catalog icon first, then the legacy cached WoW icon."""
        try:
            p = self._b3750_resolve_icon_path(glyph)
            if p is None:
                return None
            from PIL import Image, ImageTk, ImageDraw, ImageColor
            total = max(14, _px(size))
            # V4.5: 테두리 1px (아이콘이 더 크게 보이도록 안쪽 여백도 1px).
            inner = max(8, total - 2)
            src = Image.open(p).convert("RGBA").resize((inner, inner))
            mask = Image.new("L", (inner, inner), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, inner-1, inner-1), radius=max(2, _px(3)), fill=255)
            out = Image.new("RGBA", (total, total), (0, 0, 0, 0))
            out.paste(src, (1, 1), mask)
            draw = ImageDraw.Draw(out)
            rgb = ImageColor.getrgb(str(outline or "#7f8994"))
            draw.rounded_rectangle((0, 0, total-1, total-1), radius=max(3, _px(4)), outline=rgb + (255,), width=1)
            photo = ImageTk.PhotoImage(out)
            self.b3750_timeline_icon_refs.append(photo)
            return photo
        except Exception:
            return None


    def _b3750_make_fallback_badge(self, size=20, outline="#7f8994", fill="#29313a"):
        """Create a pixel-rounded fallback frame for text-only mechanic badges.

        Canvas smooth polygons still looked square at 18-22 px on Windows.
        Rendering the frame through PIL gives the fallback badge the same
        rounded silhouette as real WoW icons.  Text stays a Canvas item on top
        so Korean fallback labels remain crisp.
        """
        try:
            from PIL import Image, ImageTk, ImageDraw, ImageColor
            total = max(14, _px(size))
            img = Image.new("RGBA", (total, total), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            radius = max(3, _px(5))
            fill_rgb = ImageColor.getrgb(str(fill or "#29313a"))
            line_rgb = ImageColor.getrgb(str(outline or "#7f8994"))
            draw.rounded_rectangle(
                (0, 0, total-1, total-1),
                radius=radius,
                fill=fill_rgb + (255,),
                outline=line_rgb + (255,),
                width=1,
            )
            photo = ImageTk.PhotoImage(img)
            self.b3750_timeline_icon_refs.append(photo)
            return photo
        except Exception:
            return None


    def _b3750_prefetch_timeline_icons(self, glyphs):
        """Fetch missing timeline spell icons off the Tk thread, then redraw.

        The renderer remains offline-safe: fallbacks are shown immediately.
        Public downloads are cached under LOCALAPPDATA/NiniAnalyzer/Public/icon_cache.
        Bundled icons remain read-only fallbacks.
        """
        names = []
        pending = getattr(self, "_b3750_icon_fetch_pending", set())
        failed = getattr(self, "_b3750_icon_fetch_failed", set())
        for glyph in glyphs or []:
            # STEP4: bundled skill_tips icons are offline-first and need no fetch.
            catalog_raw = str((glyph or {}).get("catalogIconPath") or "").strip()
            if catalog_raw:
                try:
                    cp = Path(catalog_raw)
                    if cp.exists() and cp.is_file() and cp.stat().st_size > 100:
                        continue
                except Exception:
                    pass
            name = str((glyph or {}).get("icon") or "").strip()
            if not name or name in pending or name in failed:
                continue
            p = self._icon_cache_path(name)
            safe = re.sub(r"[^0-9A-Za-z_-]+", "_", name or "unknown")
            bundled = WORKDIR / "data" / "icon_cache" / f"{safe}.png"
            if (p.exists() and p.stat().st_size > 100) or (bundled.exists() and bundled.stat().st_size > 100):
                continue
            names.append(name)
        names = sorted(set(names))
        if not names:
            return
        pending = set(pending)
        pending.update(names)
        self._b3750_icon_fetch_pending = pending

        def worker(batch):
            ok, bad = [], []
            try:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                with ThreadPoolExecutor(max_workers=min(4, max(1, len(batch)))) as ex:
                    jobs = {ex.submit(self._download_icon, name): name for name in batch}
                    for fut in as_completed(jobs):
                        name = jobs[fut]
                        try:
                            path = fut.result()
                            (ok if path else bad).append(name)
                        except Exception:
                            bad.append(name)
            except Exception:
                bad = list(batch)
            self.msgq.put(("timeline_icons_ready", {"ok": ok, "failed": bad, "requested": list(batch)}))

        threading.Thread(target=worker, args=(names,), daemon=True, name="NiniTimelineIcons").start()

    def _b3750_current_raid_facts(self):
        row = getattr(self, "_b3641_try_row", {}) or {}
        candidates = []
        raw = row.get("raidFactsJson")
        if raw:
            rp = Path(str(raw))
            if not rp.is_absolute() and row.get("storageDir"):
                rp = Path(str(row.get("storageDir"))) / rp
            candidates.append(rp)
        if row.get("storageDir"):
            candidates.append(Path(str(row.get("storageDir"))) / "raid_facts.json")
        for path in candidates:
            try:
                if path.exists():
                    doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                    if isinstance(doc, dict):
                        return doc
            except Exception:
                continue
        return {}

    def _b3750_ulatek_supplemental_events(self, review):
        """Presentation-only Ula'tek casts from raid_facts enemyCastSummary.

        BossMechanic still owns verdicts. This only makes ordinary observed casts
        visible in the Lorrgs/Viserio-style timeline without reanalysis.
        """
        if str((review or {}).get("bossKey") or "") != "ulatek":
            return []
        facts = self._b3750_current_raid_facts()
        rows = list((facts or {}).get("enemyCastSummary") or [])
        spell_map = {
            1311807: ("caustic_waves", "부식성 파도"),
            1304012: ("call_of_the_serpent", "뱀의 부름"),
            1308927: ("spectral_coils", "망령의 똬리"),
            1296301: ("mephitic_thrash", "독성 후려치기"),
            1298367: ("mothers_wrath", "어미의 분노"),
            1286860: ("rage_of_the_shackled", "속박된 자의 분노"),
            1301213: ("shadow_molt", "암흑 탈피"),
            1302950: ("writhing_gestation", "꿈틀대는 잉태"),
            1290990: ("writhing_gestation", "꿈틀대는 잉태"),
            1301117: ("grasping_fangs", "휘감는 송곳니"),
            1305650: ("anguished_cry", "고통스러운 절규"),
        }
        grouped = {}
        for row in rows:
            try:
                sid = int(row.get("spellID") or 0)
            except Exception:
                continue
            if sid not in spell_map:
                continue
            event_name = str(row.get("event") or "")
            # Prefer cast-start rows. If a spell has no start row, success is
            # still a useful presentation event.
            rank = 0 if "CAST_START" in event_name else (1 if "CAST_SUCCESS" in event_name else 2)
            prev = grouped.get(sid)
            if prev is None or rank < prev[0]:
                grouped[sid] = (rank, row)
        out = []
        for sid, (_rank, row) in grouped.items():
            kind, title = spell_map[sid]
            for sec in list(row.get("times") or []):
                try:
                    fsec = float(sec)
                except Exception:
                    continue
                out.append({
                    "kind": kind, "sec": fsec, "title": title,
                    "spellIDs": [sid], "status": "observed", "evidenceLevel": "observed",
                    "logEvent": str(row.get("event") or ""),
                    "sourceTitle": str(row.get("spellName") or title),
                    "presentationSupplemental": True,
                })
        return out

    def _b3750_current_death_events(self, fallback=None):
        """Return named player deaths, preferring factual raid_facts.json.

        Older Coiled mechanic snapshots intentionally stored only the collapse
        time and could have player=None. raid_facts.json already contains every
        player death with name, so presentation can enrich the hover tooltip
        without modifying the mechanic verdict or requiring reanalysis.
        """
        row = getattr(self, "_b3641_try_row", {}) or {}
        candidates = []
        if row.get("raidFactsJson"):
            candidates.append(Path(str(row.get("raidFactsJson"))))
        if row.get("storageDir"):
            candidates.append(Path(str(row.get("storageDir"))) / "raid_facts.json")
        for path in candidates:
            try:
                if path.exists():
                    doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                    deaths = []
                    for d in list((doc or {}).get("deaths") or []):
                        if not isinstance(d, dict) or d.get("sec") is None:
                            continue
                        item = dict(d)
                        item["player"] = str(d.get("name") or d.get("player") or d.get("targetName") or d.get("destName") or "").strip() or None
                        deaths.append(item)
                    if deaths:
                        return sorted(deaths, key=lambda x: float(x.get("sec") or 0))
            except Exception:
                continue
        out = []
        for d in list(fallback or []):
            if not isinstance(d, dict) or d.get("sec") is None:
                continue
            item = dict(d)
            item["player"] = str(d.get("player") or d.get("name") or d.get("targetName") or d.get("destName") or "").strip() or None
            out.append(item)
        return sorted(out, key=lambda x: float(x.get("sec") or 0))

    @staticmethod
    def _b3750_group_deaths(deaths, window_sec=0.18):
        groups = []
        for d in sorted((dict(x) for x in deaths or [] if isinstance(x, dict) and x.get("sec") is not None), key=lambda x: float(x.get("sec") or 0)):
            sec = float(d.get("sec") or 0)
            if groups and sec - float(groups[-1][0].get("sec") or 0) <= float(window_sec):
                groups[-1].append(d)
            else:
                groups.append([d])
        return groups

    @staticmethod
    def _b3750_death_tip(group):
        rows = list(group or [])
        if not rows:
            return "사망"
        first_sec = float(rows[0].get("sec") or 0)
        lines = [f"사망 {len(rows)}명 · {b3641_fmt_clock(first_sec)}" if len(rows) > 1 else f"사망 · {b3641_fmt_clock(first_sec)}"]
        for d in rows[:12]:
            name = str(d.get("player") or d.get("name") or "이름 미확인")
            no = int(d.get("deathNo") or 1)
            suffix = f" ({no}차)" if no > 1 else ""
            line = f"{b3641_fmt_clock(float(d.get('sec') or 0))} · {name}{suffix}"
            hit = d.get("lastHit") if isinstance(d.get("lastHit"), dict) else {}
            spell = str(hit.get("spellName") or "").strip()
            if spell:
                line += f" ← {spell}"
            lines.append(line)
        if len(rows) > 12:
            lines.append(f"외 {len(rows)-12}명")
        return "\n".join(lines)

    @staticmethod
    def _b3750_inline_names(names, max_names=4):
        vals = []
        for raw in names or []:
            text = str(raw or "").strip()
            if text and text not in vals:
                vals.append(text)
        if not vals:
            return ""
        shown = ", ".join(vals[:max_names])
        if len(vals) > max_names:
            shown += f" 외 {len(vals)-max_names}명"
        return shown

    @staticmethod
    def _b3750_glyph_tip(glyph):
        title = str((glyph or {}).get("title") or "기믹")
        sec = b3641_fmt_clock(float((glyph or {}).get("sec") or 0))
        raw = str((glyph or {}).get("logEvent") or "").upper()
        log_names = {
            "SPELL_CAST_START": "시전 시작", "CAST_START": "시전 시작",
            "SPELL_CAST_SUCCESS": "시전 완료", "CAST_SUCCESS": "시전 완료",
            "SPELL_AURA_APPLIED": "효과 적용", "APPLYBUFF": "효과 적용",
            "SPELL_AURA_REMOVED": "효과 종료", "REMOVEBUFF": "효과 종료",
            "SPELL_DAMAGE": "피해", "DAMAGE": "피해",
        }
        source_title = str((glyph or {}).get("sourceTitle") or "").strip()
        log_label = log_names.get(raw)
        if not log_label:
            log_label = source_title if source_title and source_title != title else "기믹 이벤트"
        lines = [title, f"시간 · {sec}", f"로그 · {log_label}"]
        targets = list((glyph or {}).get("targets") or [])
        if targets:
            shown = ", ".join(str(x) for x in targets[:8])
            if len(targets) > 8:
                shown += f" 외 {len(targets)-8}명"
            lines.append(f"대상/피격 · {shown}")
        return "\n".join(lines)

    @staticmethod
    def _b3750_skill_reference_tip(ref):
        title = str((ref or {}).get("title") or "스킬")
        desc = str((ref or {}).get("description") or "").strip()
        return title + (("\n\n" + desc) if desc else "")

    @staticmethod
    def _b3750_canvas_round_rect(canvas, x0, y0, x1, y1, radius=4, **kwargs):
        r = max(1, min(float(radius), abs(x1-x0)/2, abs(y1-y0)/2))
        pts = [x0+r,y0, x1-r,y0, x1,y0, x1,y0+r, x1,y1-r, x1,y1,
               x1-r,y1, x0+r,y1, x0,y1, x0,y1-r, x0,y0+r, x0,y0]
        return canvas.create_polygon(pts, smooth=True, splinesteps=12, **kwargs)


    def _draw_b3641_try_timeline(self):
        if str(getattr(self, "b3641_timeline_mode", tk.StringVar(value="아이콘")).get()) == "상세":
            return self._draw_b3641_try_timeline_legacy()
        return self._draw_b3750_try_timeline()

    def _draw_b3750_skill_references(self, glyph_model):
        c = getattr(self, "b3750_skill_ref_canvas", None)
        if c is None:
            return
        c.delete("all")
        refs = list((glyph_model or {}).get("skillReferences") or [])
        if not refs:
            try: c.configure(height=_px(1))
            except Exception: pass
            return
        w = max(620, c.winfo_width() or 980)
        icon_size = 22
        left = 72
        step = _px(29)
        per_row = max(8, int((w-left-12) // step))
        rows = min(2, (len(refs) + per_row - 1)//per_row)
        h = 8 + rows * _px(28)
        try: c.configure(height=h)
        except Exception: pass
        c.create_text(8, 16, anchor="w", text="스킬 참고", fill="#aeb6bf", font=(FONT_BODY, 8, "bold"))
        self._b3750_prefetch_timeline_icons(refs)
        for idx, ref in enumerate(refs[:per_row*2]):
            row = idx // per_row; col = idx % per_row
            x = left + col*step + _px(icon_size)/2
            y = 6 + row*_px(28) + _px(icon_size)/2
            photo = self._b3750_load_glyph_icon(ref, icon_size, "#7f8994")
            tip = self._b3750_skill_reference_tip(ref)
            if photo:
                item = c.create_image(x, y, image=photo, tags=("b3750_skill_ref",))
                self._bind_b3750_skill_tip(c, item, ref, reference=True)
            else:
                fallback_photo = self._b3750_make_fallback_badge(icon_size, "#7f8994")
                if fallback_photo:
                    item = c.create_image(x, y, image=fallback_photo, tags=("b3750_skill_ref", "b3750_fallback_badge"))
                else:
                    item = self._b3750_canvas_round_rect(c, x-_px(icon_size)/2, y-_px(icon_size)/2, x+_px(icon_size)/2, y+_px(icon_size)/2, radius=_px(6), fill="#29313a", outline="#7f8994", width=2, tags=("b3750_skill_ref", "b3750_fallback_badge"))
                txt = c.create_text(x, y, text=str(ref.get("title") or "?")[:1], fill="#e9edf2", font=(FONT_BODY, 7, "bold"), tags=("b3750_skill_ref_text",))
                if ref.get("tooltipRows"):
                    self._bind_b3750_skill_tip(c, item, ref, reference=True); self._bind_b3750_skill_tip(c, txt, ref, reference=True)
                else:
                    self._bind_b3641_timeline_tip(c, item, tip); self._bind_b3641_timeline_tip(c, txt, tip)

    def _draw_b3750_try_timeline(self):
        c = getattr(self, "b3641_try_timeline", None)
        if c is None:
            return
        self._hide_b3641_timeline_tooltip()
        c.delete("all")
        self.b3750_timeline_icon_refs = []
        model = getattr(self, "_b3641_try_model", {}) or {}
        review = getattr(self, "_b3641_try_review", {}) or {}
        glyph_source = dict(model)
        supplemental = self._b3750_ulatek_supplemental_events(review)
        # V4.6: 잡은 네임드 6개 보스 시전(raid_facts 적 시전 요약)을 시간축 스킬별 줄에 올린다 (관측만, 판정 아님).
        try:
            from live import boss_guides_v1 as _v46_bg
            supplemental = list(supplemental or []) + _v46_bg.supplemental_events(self._b3750_current_raid_facts())
        except Exception:
            pass
        if supplemental:
            glyph_source["supplementalGlyphEvents"] = supplemental
        glyph_model = build_timeline_glyph_model(glyph_source, review)
        rows_mode = str(getattr(self, "b3641_timeline_mode", tk.StringVar(value="아이콘")).get()) == "스킬별"
        try:
            from live import v45_timeline as _v45
        except Exception:
            _v45, rows_mode = None, False
        # V4.5: 스킬별 줄은 왼쪽 이름 칸이 스킬 참고를 대신한다 (아이콘 hover 설명 그대로).
        self._draw_b3750_skill_references({} if rows_mode else glyph_model)
        duration = float(glyph_model.get("durationSec") or 0.0)
        if duration <= 0:
            c.create_text(12, 12, anchor="nw", fill="#a7b0ba", text="기믹 시간축 데이터 없음", font=(FONT_BODY, 8))
            return

        w = max(620, c.winfo_width() or 980)
        left = _v45.ROW_LEFT if rows_mode else 74
        right = w - 18
        top = 24
        lane_h = _v45.ROW_H if rows_mode else 25
        icon_size = _v45.ROW_ICON if rows_mode else 20

        def x_for(sec):
            return left + max(0.0, min(1.0, float(sec) / duration)) * (right - left)

        # Preview3.1: ordinary observations/successes use only two compact
        # lanes. Verified failures/warnings are still protected by
        # place_glyph_lanes() and may spill into extra lanes rather than hide.
        ability_rows = []
        if rows_mode:
            placed, overflow, ability_rows = _v45.ability_rows(glyph_model.get("glyphs") or [], x_for, icon_px=_px(icon_size))
        else:
            placed, overflow = place_glyph_lanes(glyph_model.get("glyphs") or [], x_for, icon_px=_px(icon_size), max_lanes=2)
        used_lanes = max([int(x.get("lane") or 0) for x in placed] + [0]) + 1
        overflow_lane = used_lanes if overflow else None
        glyph_lane_count = used_lanes + (1 if overflow else 0)

        cooldowns = list(glyph_model.get("cooldowns") or [])
        bloodlust_windows = [x for x in cooldowns if str(x.get("kind") or "").lower() == "bloodlust" or "블러드" in str(x.get("label") or "")]
        other_cooldowns = [x for x in cooldowns if x not in bloodlust_windows]
        all_ranges = list(glyph_model.get("ranges") or [])
        has_duration_bars = any(str(r.get("style") or "") == "bar" for r in all_ranges)

        def _range_overlaps_bl(rng):
            if rng.get("endSec") is None:
                return False
            a0, a1 = float(rng.get("startSec") or 0), float(rng.get("endSec") or 0)
            return any(max(a0, float(cd.get("startSec") or 0)) < min(a1, float(cd.get("endSec") or cd.get("startSec") or 0)) for cd in bloodlust_windows)

        # If a persistent boss window overlaps BL, reserve a second label row
        # so Soulbinding/BL captions never print on top of each other.
        has_band_bl_overlap = any(str(r.get("style") or "band") != "bar" and _range_overlaps_bl(r) for r in all_ranges)
        header_extra = 18 + (18 if has_band_bl_overlap else 0) + (12 if has_duration_bars else 0)
        glyph_top = top + header_extra
        glyph_bottom = glyph_top + glyph_lane_count * lane_h
        analysis_markers = list(glyph_model.get("analysisMarkers") or []) + list(glyph_model.get("standaloneMarkers") or [])
        analysis_y = glyph_bottom + 10 if analysis_markers else None
        cooldown_y = ((analysis_y + 24) if analysis_y is not None else (glyph_bottom + 12)) if other_cooldowns else None
        named_deaths = self._b3750_current_death_events(glyph_model.get("deathEvents") or [])
        death_y = ((cooldown_y + 25) if cooldown_y is not None else ((analysis_y + 24) if analysis_y is not None else (glyph_bottom + 13)))
        chart_bottom = death_y + (19 if named_deaths else 4)
        tick_y = chart_bottom + 4
        judgments = list(glyph_model.get("judgments") or [])
        judgment_top = tick_y + 18
        expanded_judgments = bool(getattr(self, "_b3750_judgments_expanded", False))
        shown_judgments = judgments if expanded_judgments else judgments[:5]
        has_judgment_toggle = len(judgments) > 5
        judgment_h = (18 + 18 * len(shown_judgments) + (20 if has_judgment_toggle else 0)) if judgments else 24
        h = judgment_top + judgment_h + 15
        try:
            c.configure(height=_px(h))
        except Exception:
            pass

        base_top = top
        base_bottom = chart_bottom - 2
        c.create_rectangle(left, base_top, right, base_bottom, fill="#18212a", outline="")

        # B37.5: persistent mechanic windows use two visual forms.
        # - full bands: strategic windows such as Soulbinding
        # - compact bars: bounded boss-object windows such as Ula'tek hearts
        full_ranges = [r for r in all_ranges if str(r.get("style") or "band") != "bar"]
        bar_ranges = [r for r in all_ranges if str(r.get("style") or "") == "bar"]
        for rng in full_ranges:
            x0 = x_for(rng.get("startSec") or 0)
            end = rng.get("endSec")
            label = str(rng.get("label") or "구간")
            point_sec = float(rng.get("startSec") or 0)
            point_in_bl = any(float(cd.get("startSec") or 0) <= point_sec <= float(cd.get("endSec") or cd.get("startSec") or 0) for cd in bloodlust_windows)
            label_row = 1 if (_range_overlaps_bl(rng) or point_in_bl) else 0
            label_y0 = base_top + 2 + 18 * label_row
            if end is None:
                c.create_line(x0, base_top, x0, base_bottom, fill="#36bfc3", width=2, dash=(3, 2))
                # High-contrast capsule: boss-window labels get a second row
                # when BL overlaps them, so neither caption becomes unreadable.
                tw = max(62, 12 + 8 * len(label))
                lx = min(max(left + 2, x0 + 4), right - tw - 2)
                c.create_rectangle(lx, label_y0, lx + tw, label_y0 + 15, fill="#10292c", outline="#45ced1", width=1, tags=("b3750_range_label",))
                c.create_text(lx + 5, label_y0 + 7, anchor="w", text=label, fill="#86edf0", font=(FONT_BODY, 7, "bold"), tags=("b3750_range_label",))
            else:
                x1 = x_for(end)
                c.create_rectangle(x0, base_top, x1, base_bottom, fill="#244e53", stipple="gray50", outline="")
                c.create_line(x0, base_top, x0, base_bottom, fill="#45ced1", width=1)
                c.create_line(x1, base_top, x1, base_bottom, fill="#45ced1", width=1)
                label_text = f"{label} {float(rng.get('durationSec') or 0):.0f}s"
                tw = max(72, 12 + 7 * len(label_text))
                lx = min(max(left + 2, (x0+x1)/2 - tw/2), right - tw - 2)
                c.create_rectangle(lx, label_y0, lx + tw, label_y0 + 15, fill="#10292c", outline="#45ced1", width=1, tags=("b3750_range_label",))
                c.create_text(lx + tw/2, label_y0 + 7, anchor="center", text=label_text, fill="#86edf0", font=(FONT_BODY, 7, "bold"), tags=("b3750_range_label",))

        # Ula'tek heart exposure is a duration, not two point events.  Keep it
        # compact as a bar above the glyph lanes instead of painting the whole
        # chart background.
        for idx, rng in enumerate(bar_ranges):
            x0 = x_for(rng.get("startSec") or 0)
            x1 = x_for(rng.get("endSec") if rng.get("endSec") is not None else rng.get("startSec") or 0)
            x1 = max(x0 + 3, x1)
            by = base_top + 19 + (18 if has_band_bl_overlap else 0) + idx * 9
            c.create_rectangle(x0, by, x1, by + 6, fill="#4f9f70", outline="#83d6a2", width=1, tags=("b3750_duration_bar",))
            label = f"{rng.get('label') or '구간'} {b3641_fmt_clock(rng.get('startSec'))}–{b3641_fmt_clock(rng.get('endSec'))}"
            item = c.create_text(min(x0 + 3, right - 95), by - 1, anchor="sw", text=str(rng.get("label") or "구간"), fill="#a7e8bb", font=(FONT_BODY, 7, "bold"))
            if rng.get("tooltipRows"):
                self._bind_b3750_skill_tip(c, item, rng, reference=False)
            else:
                self._bind_b3641_timeline_tip(c, item, f"{rng.get('label') or '구간'}\n{b3641_fmt_clock(rng.get('startSec'))}~{b3641_fmt_clock(rng.get('endSec'))}\n지속 {float(rng.get('durationSec') or 0):.1f}초")

        # Bloodlust is a full-height purple timing window.  Its faint fill sits
        # below collapse, then a bright edge/label is redrawn after collapse so
        # BL never disappears when the two windows overlap.
        for cd in bloodlust_windows:
            x0 = x_for(cd.get("startSec") or 0)
            x1 = x_for(cd.get("endSec") or cd.get("startSec") or 0)
            x1 = max(x0 + 2, x1)
            c.create_rectangle(x0, base_top, x1, base_bottom, fill="#3b2854", stipple="gray50", outline="", tags=("b3750_bloodlust_band",))

        collapse = glyph_model.get("collapseRange") or {}
        if collapse.get("startSec") is not None:
            cx = x_for(collapse.get("startSec"))
            c.create_rectangle(cx, base_top, right, base_bottom, fill="#4b2529", stipple="gray50", outline="")
            c.create_line(cx, base_top, cx, base_bottom, fill=_ui_theme.STATE_FAILURE, width=2)
            c.create_text(cx, base_top - 1, anchor="s", text="▼", fill=_ui_theme.STATE_FAILURE, font=(FONT_BODY, 9, "bold"))
            c.create_text(min(cx + 5, right - 75), base_top + 1, anchor="nw", text=f"붕괴 {b3641_fmt_clock(collapse.get('startSec'))}", fill="#ff827c", font=(FONT_BODY, 7, "bold"))

        # Reassert BL identity above any Soulbinding/collapse overlay.  The body
        # remains translucent, while the top accent and capsule stay readable.
        for cd in bloodlust_windows:
            x0 = x_for(cd.get("startSec") or 0)
            x1 = max(x0 + 2, x_for(cd.get("endSec") or cd.get("startSec") or 0))
            c.create_line(x0, base_top + 1, x1, base_top + 1, fill="#c69bff", width=3, tags=("b3750_bloodlust_edge",))
            c.create_line(x0, base_top, x0, base_bottom, fill="#b988ff", width=1)
            c.create_line(x1, base_top, x1, base_bottom, fill="#b988ff", width=1)
            label_text = f"BL {b3641_fmt_clock(cd.get('startSec'))}–{b3641_fmt_clock(cd.get('endSec'))}"
            tw = max(82, 12 + 7 * len(label_text))
            lx = min(max(left + 2, x0 + 4), right - tw - 2)
            box = c.create_rectangle(lx, base_top + 2, lx + tw, base_top + 17, fill="#241a31", outline="#b988ff", width=1, tags=("b3750_bloodlust_label",))
            item = c.create_text(lx + 5, base_top + 9, anchor="w", text=label_text, fill="#d3b5ff", font=(FONT_BODY, 7, "bold"), tags=("b3750_bloodlust_label",))
            tip = f"{cd.get('label') or '블러드'}\n{b3641_fmt_clock(cd.get('startSec'))}~{b3641_fmt_clock(cd.get('endSec'))}\n{cd.get('summary') or '공대 쿨기 관측'}"
            self._bind_b3641_timeline_tip(c, box, tip)
            self._bind_b3641_timeline_tip(c, item, tip)

        # Grid and top phase markers.  Phase is a vertical line, not a row/band.
        tick = 0
        while tick <= int(duration + 0.001):
            if tick % 30 == 0:
                x = x_for(tick)
                c.create_line(x, base_top, x, chart_bottom, fill="#2e3944", width=1)
                c.create_text(x, tick_y, anchor="n", text=f"{tick//60}:{tick%60:02d}", fill="#8f98a3", font=(FONT_BODY, 7))
            tick += 30
        for phase in glyph_model.get("phaseMarks") or []:
            sec = float(phase.get("sec") or 0)
            x = x_for(sec)
            c.create_line(x, base_top, x, chart_bottom, fill="#5ebc72", width=2)
            c.create_text(min(x + 3, right - 24), base_top - 2, anchor="sw", text=str(phase.get("phase") or "P"), fill="#76df89", font=(FONT_BODY, 8, "bold"))

        if rows_mode and ability_rows:
            _v45.draw_row_labels(self, c, ability_rows, glyph_top, lane_h, left, right,
                                 refs=glyph_model.get("skillReferences"), font_body=FONT_BODY, px=_px)
        else:
            c.create_text(left - 8, glyph_top + 2, anchor="ne", text="기믹", fill="#aeb6bf", font=(FONT_BODY, 8, "bold"))
        if analysis_y is not None:
            c.create_text(left - 8, analysis_y, anchor="e", text="판단", fill="#8f98a3", font=(FONT_BODY, 8))
        if cooldown_y is not None:
            c.create_text(left - 8, cooldown_y, anchor="e", text="공대 쿨기", fill="#aeb6bf", font=(FONT_BODY, 8))
        if named_deaths:
            c.create_text(left - 8, death_y, anchor="e", text="사망", fill="#aeb6bf", font=(FONT_BODY, 8))

        # Missing WoW icons are fetched/cached in the background. The current
        # draw remains instant with text fallbacks and redraws once the batch is ready.
        self._b3750_prefetch_timeline_icons(placed)

        # Spell glyphs.  Facts live in the square; judgment is only border + ①.
        for glyph in placed:
            lane = int(glyph.get("lane") or 0)
            x = float(glyph.get("x") or x_for(glyph.get("sec") or 0))
            y = glyph_top + lane * lane_h + lane_h/2
            outline = self._b3750_glyph_outline(glyph)
            if rows_mode:
                outline = _v45.row_outline(glyph, outline)
            photo = self._b3750_load_glyph_icon(glyph, icon_size, outline)
            if photo:
                item = c.create_image(x, y, image=photo)
                if glyph.get("tooltipRows"):
                    self._bind_b3750_skill_tip(c, item, glyph, reference=False)
                else:
                    self._bind_b3641_timeline_tip(c, item, self._b3750_glyph_tip(glyph))
            else:
                fallback_photo = self._b3750_make_fallback_badge(icon_size, outline)
                if fallback_photo:
                    item = c.create_image(x, y, image=fallback_photo, tags=("b3750_fallback_badge",))
                else:
                    x0, y0 = x-_px(icon_size)/2, y-_px(icon_size)/2
                    x1, y1 = x+_px(icon_size)/2, y+_px(icon_size)/2
                    item = self._b3750_canvas_round_rect(c, x0, y0, x1, y1, radius=_px(6), fill="#29313a", outline=outline, width=2, tags=("b3750_fallback_badge",))
                txt = c.create_text(x, y, text=str(glyph.get("fallback") or "?")[:2], fill="#e9edf2", font=(FONT_BODY, 7, "bold"), tags=("b3750_fallback_badge_text",))
                tip = self._b3750_glyph_tip(glyph)
                if glyph.get("tooltipRows"):
                    self._bind_b3750_skill_tip(c, item, glyph, reference=False)
                    self._bind_b3750_skill_tip(c, txt, glyph, reference=False)
                else:
                    self._bind_b3641_timeline_tip(c, item, tip)
                    self._bind_b3641_timeline_tip(c, txt, tip)
            if rows_mode and int(glyph.get("v528Count") or 1) > 1:   # 1.0.6 (GOLD V5.2.8): 겹친 같은 기믹은 하나로 · ×n
                c.create_text(x + _px(icon_size)/2 + 1, y + _px(icon_size)/2 + 1, anchor="sw", text=f"×{int(glyph.get('v528Count'))}",
                              fill="#e9edf2", font=(FONT_BODY, 7, "bold"))
            numbers = list(glyph.get("judgmentNumbers") or ([] if not glyph.get("judgmentNumber") else [glyph.get("judgmentNumber")]))
            if numbers:
                number_text = "·".join(str(n) for n in numbers[:3])
                if len(numbers) > 3:
                    number_text += f"+{len(numbers)-3}"
                c.create_text(x + _px(icon_size)/2 + 2, y - _px(icon_size)/2 - 2, anchor="sw", text=number_text, fill=outline, font=(FONT_BODY, 7, "bold"))

        # Only normal/observation glyphs may collapse into +N; failures/warnings
        # have already been protected by place_glyph_lanes().
        if overflow:
            groups = []
            for glyph in sorted(overflow, key=lambda x: float(x.get("x") or 0)):
                if not groups or abs(float(glyph.get("x") or 0) - groups[-1][0]) > _px(25):
                    groups.append([float(glyph.get("x") or 0), [glyph]])
                else:
                    groups[-1][1].append(glyph)
            for x, members in groups:
                y = glyph_top + int(overflow_lane or 0) * lane_h + lane_h/2
                item = c.create_rectangle(x-11, y-9, x+11, y+9, fill="#343c45", outline="#7f8994", width=1)
                txt = c.create_text(x, y, text=f"+{len(members)}", fill="#d7dde3", font=(FONT_BODY, 7, "bold"))
                lines = [f"겹친 관측 {len(members)}건"] + [f"{b3641_fmt_clock(m.get('sec'))} · {m.get('title')}" for m in members[:8]]
                tip = "\n".join(lines)
                self._bind_b3641_timeline_tip(c, item, tip)
                self._bind_b3641_timeline_tip(c, txt, tip)

        # Analyzer-only concepts use their own marker row and never masquerade as WoW spells.
        if analysis_y is not None:
            for marker in glyph_model.get("analysisMarkers") or []:
                x = x_for(marker.get("sec") or 0)
                item = c.create_text(x, analysis_y, text=str(marker.get("marker") or "◆"), fill="#8f98a3", font=(FONT_BODY, 9, "bold"))
                self._bind_b3641_timeline_tip(c, item, f"{marker.get('title')}\n{b3641_fmt_clock(marker.get('sec'))}\n{marker.get('summary') or ''}".rstrip())
            for marker in glyph_model.get("standaloneMarkers") or []:
                x = x_for(marker.get("sec") or 0)
                status = str(marker.get("status") or "")
                color = _ui_theme.STATE_FAILURE if status == "failed" else (_ui_theme.STATE_WARNING if status == "warning" else "#8f98a3")
                item = c.create_text(x, analysis_y, text=str(marker.get("marker") or "◆"), fill=color, font=(FONT_BODY, 9, "bold"))
                c.create_text(x+7, analysis_y-5, anchor="sw", text=str(marker.get("number") or ""), fill=color, font=(FONT_BODY, 7, "bold"))
                self._bind_b3641_timeline_tip(c, item, f"{marker.get('title')}\n{b3641_fmt_clock(marker.get('sec'))}\n{marker.get('summary') or ''}".rstrip())

        # Non-BL raid cooldowns remain duration bars. BL is the purple backdrop above.
        if cooldown_y is not None:
            for cd in other_cooldowns:
                x0 = x_for(cd.get("startSec") or 0)
                x1 = x_for(cd.get("endSec") or cd.get("startSec") or 0)
                c.create_rectangle(x0, cooldown_y-5, max(x0+3, x1), cooldown_y+5, fill="#9c6ade", outline="")
                item = c.create_text(x0+3, cooldown_y-7, anchor="sw", text=f"{cd.get('label')} {b3641_fmt_clock(cd.get('startSec'))}", fill="#bd91ff", font=(FONT_BODY, 7, "bold"))
                self._bind_b3641_timeline_tip(c, item, f"{cd.get('label')}\n{b3641_fmt_clock(cd.get('startSec'))}~{b3641_fmt_clock(cd.get('endSec'))}\n{cd.get('summary') or '공대 쿨기 관측'}")

        # Death ticks use raid_facts names when available. Near-simultaneous
        # deaths share one hover target so stacked lines do not hide each other.
        for group in self._b3750_group_deaths(named_deaths):
            sec = min(float(d.get("sec") or 0) for d in group)
            x = x_for(sec)
            item = c.create_line(x, death_y-7, x, death_y+7, fill="#ff756f", width=3 if len(group) > 1 else 2, tags=("b3750_death_tick",))
            if len(group) > 1:
                count_item = c.create_text(x+3, death_y-8, anchor="sw", text=str(len(group)), fill="#ff8d88", font=(FONT_BODY, 7, "bold"))
                self._bind_b3641_timeline_tip(c, count_item, self._b3750_death_tip(group))
            self._bind_b3641_timeline_tip(c, item, self._b3750_death_tip(group))

        # Judgment order remains plain text for screenshots/Discord sharing.
        # Dense pulls show five rows by default; the user can expand all rows
        # without moving to another panel or losing the numbered mapping.
        c.create_text(6, judgment_top, anchor="nw", text="판정 순서", fill="#cfd5db", font=(FONT_BODY, 8, "bold"))
        if not judgments:
            c.create_text(left, judgment_top, anchor="nw", text=self._v528_no_judgment_text(), fill="#8f98a3", font=(FONT_BODY, 8))
        else:
            role_name = {"collapse_driver":"붕괴 직결", "first_mistake":"첫 실수", "additional_failure":"추가 실수", "recovered_on_kill":"킬에서 회복"}
            for visual_idx, j in enumerate(shown_judgments, start=1):
                status = str(j.get("status") or "")
                color = _ui_theme.STATE_FAILURE if status == "failed" else _ui_theme.STATE_WARNING
                role = role_name.get(str(j.get("failureRole") or ""), "검증 실패" if status == "failed" else "경고")
                line = f"{j.get('number')}  {b3641_fmt_clock(j.get('sec'))}   {role} · {j.get('title')}"
                summary = str(j.get("summary") or "").strip()
                if summary and summary not in str(j.get("title") or ""):
                    line += f" · {summary}"
                names = self._b3750_inline_names(j.get("targets") or [], max_names=4)
                if names and names not in line:
                    line += f" · 피격자 {names}"
                _jfont = (FONT_BODY, 8)   # RC5.3 (GOLD V5.2): 판정 순서는 모두 보통 굵기
                _names = {}
                if _v45 is not None:
                    try:
                        _names = _v45.name_colors(getattr(self, "_b3641_try_row", None))
                    except Exception:
                        _names = {}
                if _names:
                    _v45.draw_rich_line(c, 18, judgment_top + 18*visual_idx, line, color, _jfont, _names, tags=("b3750_judgment_row",), name_font=(FONT_BODY, 8))
                else:
                    c.create_text(18, judgment_top + 18*visual_idx, anchor="nw", text=line, fill=color, font=_jfont, tags=("b3750_judgment_row",))
            if has_judgment_toggle:
                hidden = max(0, len(judgments) - 5)
                toggle_y = judgment_top + 18*(len(shown_judgments)+1)
                toggle_text = "접기 ▴" if expanded_judgments else f"+{hidden}개 더 보기 ▾"
                toggle = c.create_text(18, toggle_y, anchor="nw", text=toggle_text, fill="#7fb6e8", font=(FONT_BODY, 8, "bold"), tags=("b3750_judgment_toggle",))
                c.tag_bind(toggle, "<Button-1>", self._toggle_b3750_judgments)
                c.tag_bind(toggle, "<Enter>", lambda _e: c.configure(cursor="hand2"))
                c.tag_bind(toggle, "<Leave>", lambda _e: c.configure(cursor=""))

        legend = "실제 사건=스펠 아이콘 · 테두리=판정 · P선=페이즈 · 띠/바=지속구간 · 보라 배경=블러드 · 사망=이름 hover · ▼=붕괴 시작"
        c.create_text(left, h-3, anchor="sw", text=legend, fill="#8f98a3", font=(FONT_BODY, 7))

    def _draw_b3641_try_timeline_legacy(self):
        c = getattr(self, "b3641_try_timeline", None)
        if c is None:
            return
        self._hide_b3641_timeline_tooltip()
        c.delete("all")
        model = getattr(self, "_b3641_try_model", {}) or {}
        events = [dict(x) for x in (model.get("timeline") or []) if isinstance(x, dict) and x.get("sec") is not None]
        duration = float(model.get("durationSec") or 0)
        death_events = [dict(x) for x in (model.get("deathEvents") or []) if isinstance(x, dict) and x.get("sec") is not None]
        if events:
            duration = max(duration, max(float(e.get("sec") or 0) for e in events) + 2)
        if death_events:
            duration = max(duration, max(float(e.get("sec") or 0) for e in death_events) + 2)
        collapse_sec = model.get("collapseSec")
        if collapse_sec is not None:
            duration = max(duration, float(collapse_sec) + 2)
        if duration <= 0:
            c.create_text(12, 12, anchor="nw", fill="#a7b0ba", text="기믹 시간축 데이터 없음", font=(FONT_BODY, 8))
            return

        # Group every observation under a stable mechanic row.  Phase is a
        # background/checkpoint, not a separate mechanic row.
        phase_events = [e for e in events if str(e.get("kind") or "") in {"p2_entry", "phase", "phase_entry"}]
        mechanic_events = [e for e in events if e not in phase_events]
        groups = {}
        first_pos = {}
        for idx, e in enumerate(mechanic_events):
            key = self._b3641_timeline_row_key(e)
            groups.setdefault(key, []).append(e)
            first_pos.setdefault(key, idx)

        preferred = [
            "breath", "guillotine", "gloombomb", "wail", "manifest", "shield",
            "mutilate", "viscous_cyst", "crosswinds", "serpents_fury",
            "brood", "visceral_burst", "corrosive_orb", "eternal_venom",
        ]
        rank = {k: i for i, k in enumerate(preferred)}
        row_keys = sorted(groups, key=lambda k: (rank.get(k, 999), first_pos.get(k, 9999)))
        # Keep noise-only discovery rows out of the main chart.
        row_keys = [k for k in row_keys if k not in {"orb_wave", "candidate_pulse"}]

        w = max(620, c.winfo_width() or 980)
        left = 142
        right = w - 18
        top = 28
        row_h = 24
        tick_h = 18
        death_h = 24
        judgment_events = self._b3641_timeline_judgments(mechanic_events)
        judgment_h = (18 + 18 * len(judgment_events)) if judgment_events else 22
        chart_bottom = top + row_h * len(row_keys) + (death_h if death_events else 0)
        h = chart_bottom + tick_h + judgment_h + 16
        try:
            c.configure(height=_px(h))
        except Exception:
            pass

        def x_for(sec):
            return left + max(0.0, min(1.0, float(sec) / duration)) * (right - left)

        # Phase backgrounds: P1/P2 blue bands.  Collapse red band overlays them.
        p2_sec = None
        if phase_events:
            vals = [float(e.get("sec") or 0) for e in phase_events if e.get("sec") is not None]
            if vals:
                p2_sec = min(vals)
        base_top = top - 10
        base_bottom = chart_bottom - 2
        if p2_sec is not None:
            px = x_for(p2_sec)
            c.create_rectangle(left, base_top, px, base_bottom, fill="#172533", outline="")
            c.create_rectangle(px, base_top, right, base_bottom, fill="#1d3040", outline="")
            c.create_text(left + 4, base_top + 2, anchor="nw", text="P1", fill="#78b9eb", font=(FONT_BODY, 8, "bold"))
            c.create_text(px + 4, base_top + 2, anchor="nw", text="P2", fill="#78b9eb", font=(FONT_BODY, 8, "bold"))
            c.create_line(px, base_top, px, base_bottom, fill="#78b9eb", width=3)
        else:
            c.create_rectangle(left, base_top, right, base_bottom, fill="#172533", outline="")
        if collapse_sec is not None:
            cx = x_for(float(collapse_sec))
            c.create_rectangle(cx, base_top, right, base_bottom, fill="#3b2024", outline="")
            c.create_line(cx, base_top, cx, base_bottom, fill="#ff6464", width=2)
            c.create_text(min(cx + 5, right - 75), base_top + 2, anchor="nw", text=f"붕괴 {b3641_fmt_clock(float(collapse_sec))}", fill="#ff756f", font=(FONT_BODY, 8, "bold"))

        # 30-second grid/ticks.
        tick = 0
        while tick <= int(duration + 0.001):
            if tick % 30 == 0:
                x = x_for(tick)
                c.create_line(x, base_top, x, chart_bottom, fill="#33404c", width=1)
                c.create_text(x, chart_bottom + 4, anchor="n", text=f"{tick//60}:{tick%60:02d}", fill="#8f98a3", font=(FONT_BODY, 7))
            tick += 30

        judgment_number = {id(e): i + 1 for i, e in enumerate(judgment_events)}
        # Time/semantic match fallback because model events can be copied.
        def judgment_no(event):
            for i, j in enumerate(judgment_events, 1):
                if str(j.get("failureRole") or "") != str(event.get("failureRole") or ""):
                    continue
                try:
                    if abs(float(j.get("sec")) - float(event.get("sec"))) <= 0.12 and self._b3641_timeline_row_key(j) == self._b3641_timeline_row_key(event):
                        return i
                except Exception:
                    pass
            return None

        # Mechanic rows.  Text lives at the left; the chart itself is marker-only.
        for i, key in enumerate(row_keys):
            y = top + i * row_h + row_h / 2
            evs = groups[key]
            title = self._b3641_timeline_row_title(key, evs)
            c.create_text(left - 10, y, anchor="e", text=title, fill="#c6cbd1", font=(FONT_BODY, 8))
            c.create_line(left, y, right, y, fill="#4a535d", width=1)
            for e in sorted(evs, key=lambda x: float(x.get("sec") or 0)):
                x = x_for(e.get("sec"))
                color, marker, _priority = self._b3641_timeline_style(e)
                if marker in {"✕", "◆", "△", "○"}:
                    item = c.create_text(x, y, text=marker, fill=color, font=(FONT_BODY, 10, "bold"))
                elif marker == "▮":
                    item = c.create_rectangle(x-2, y-8, x+2, y+8, fill=color, outline=color)
                else:
                    width = 2 if str(e.get("status") or "") in {"failed", "warning"} else 1
                    item = c.create_line(x, y-7, x, y+7, fill=color, width=width)
                self._bind_b3641_timeline_tip(c, item, self._b3641_timeline_tip(e))
                no = judgment_no(e)
                if no is not None:
                    c.create_text(x + 7, y - 8, anchor="sw", text=str(no), fill=color, font=(FONT_BODY, 7, "bold"))

        # Player death row.
        if death_events:
            y = top + len(row_keys) * row_h + row_h / 2
            c.create_text(left - 10, y, anchor="e", text="사망", fill="#c6cbd1", font=(FONT_BODY, 8))
            c.create_line(left, y, right, y, fill="#4a535d", width=1)
            for d in death_events:
                x = x_for(d.get("sec"))
                item = c.create_line(x, y-7, x, y+7, fill="#ff756f", width=2)
                name = str(d.get("player") or d.get("name") or "사망")
                self._bind_b3641_timeline_tip(c, item, f"{name}\n{b3641_fmt_clock(float(d.get('sec') or 0))}")

        # Judgment list: no long labels on the chart itself, so overlap is impossible.
        jy = chart_bottom + tick_h + 2
        c.create_text(6, jy, anchor="nw", text="판정 순서", fill="#cfd5db", font=(FONT_BODY, 8, "bold"))
        if not judgment_events:
            c.create_text(left, jy, anchor="nw", text=self._v528_no_judgment_text(), fill="#8f98a3", font=(FONT_BODY, 8))
        else:
            role_name = {
                "collapse_driver": "붕괴 직결", "first_mistake": "첫 실수",
                "additional_failure": "추가 실수", "recovered_on_kill": "킬에서 회복",
            }
            for i, e in enumerate(judgment_events, 1):
                color, _marker, _priority = self._b3641_timeline_style(e)
                role = role_name.get(str(e.get("failureRole") or ""), "검증 실패" if str(e.get("status") or "") == "failed" else "경고")
                title = str(e.get("label") or e.get("title") or self._b3641_timeline_row_title(self._b3641_timeline_row_key(e), [e]))
                summary = str(e.get("summary") or "").strip()
                line = f"{i}  {b3641_fmt_clock(float(e.get('sec') or 0))}   {role} · {title}"
                if summary and summary not in title:
                    line += f" · {summary}"
                c.create_text(18, jy + 18 * i, anchor="nw", text=line, fill=color, font=(FONT_BODY, 8))

        legend_y = h - 4
        legend = "초록 정상 · 빨강 검증 실패 · 주황 검증 경고 · 회색 후보   ◆ 첫 실수  △ 추가  ○ 킬에서 회복  ✕ 붕괴 직결"
        c.create_text(left, legend_y, anchor="sw", text=legend, fill="#8f98a3", font=(FONT_BODY, 7))

    def _show_main_frame(self, frame):
        try:
            self.notebook.select(frame)
        except Exception:
            return
        if frame is getattr(self, "precision_frame", None):
            try:
                self._b3742_refresh_precision_page()
            except Exception:
                pass
        if frame is getattr(self, "cooldown_frame", None):
            b = self._current_cooldown_boss()
            if b:
                # B34.2.3: the shared navigation selection owns the cooldown
                # context.  Always sync (even when no matching dataset folder
                # exists) so an older boss result can never remain on screen.
                self.sync_cooldown_for_boss(b)
            else:
                self.cooldown_context_key = None
                self.cooldown_data = None
                self.cooldown_entries = {}
                self._refresh_cooldown_layout()
        self._refresh_context_tabs()

    def _b37509_precision_choice_changed(self, _event=None):
        fight = getattr(self, "_b37509_precision_items", {}).get(self.b3742_historical_choice_var.get())
        if fight is None:
            self.b3742_prec_state_var.set("날짜·보스·Fight를 선택해야 정밀분석을 실행할 수 있습니다.")
            self.b3742_prec_run_btn.configure(state="disabled")
            return
        self.b3742_prec_state_var.set(
            f"선택 확정 · {fight.session_date} · {fight.player} · {fight.display_name} "
            f"· Fight {fight.fight_no} · {'Kill' if fight.kill else 'Wipe'} · "
            "캐시 또는 해당 원본으로만 분석 (최근 로그 대체 금지)"
        )
        self.b3742_prec_run_btn.configure(state="disabled" if self.precision_running else "normal")
        summary = fight.result_dir / "precision_coach.txt"
        if summary.is_file():
            self.load_precision_result(summary)
        else:
            self.precision.delete("1.0", "end")
            self.precision.insert("end", f"{fight.label}\n선택한 Fight의 정밀분석 결과가 아직 없습니다.\n", "note")

    def _b3742_refresh_precision_page(self):
        """Date-scoped Fight picker. Never borrow the active/latest dataset."""
        ctx = self._nav_context_key()
        encounter, difficulty, date, player, _spec = (list(ctx) + [None] * 5)[:5]
        rows = self._b3641_context_rows(all_dates=False) if encounter is not None else []
        key = (encounter, difficulty, date, player)
        if key != getattr(self, "_b37509_precision_context", None):
            self._b37509_precision_context = key
            self._b37509_precision_items = {}
            self.b3742_historical_choice_var.set("전투 선택…")
            # A new date/boss must never display a prior Fight's result.
            self.precision.delete("1.0", "end")
            self.precision.insert("end", "선택한 날짜의 Fight를 고르세요. 기존 다른 날짜 결과를 표시하지 않습니다.\n", "note")
            if encounter is not None and date and player:
                choices = find_fights(WORKDIR, date, player, encounter, difficulty, rows)
                self._b37509_precision_items = {fight.label: fight for fight in choices}
            self.b3742_historical_combo.configure(values=tuple(self._b37509_precision_items))
        boss_name = str((rows[0].get("name") if rows else "") or registry_boss_name(int(encounter or 0), ""))
        self.b3742_prec_title_var.set(f"{date or ''} · {boss_name} · {_ui_difficulty_name({'difficultyID': difficulty})}".strip(" ·"))
        if self.precision_running:
            self.b3742_prec_state_var.set("정밀 분석 진행 중 · 완료 시 해당 Fight에 결과를 저장합니다.")
            self.b3742_prec_run_btn.configure(state="disabled")
        elif self.b3742_historical_choice_var.get() in self._b37509_precision_items:
            self._b37509_precision_choice_changed()
        else:
            n = len(self._b37509_precision_items)
            self.b3742_prec_state_var.set(
                f"이 날짜의 로컬 원본 Fight {n}개 확인 · 분석할 전투를 직접 선택하세요. "
                "선택한 날짜가 없으면 실행하지 않습니다." if n else
                "해당 날짜/캐릭터/보스에 연결할 로컬 Fight 메타데이터가 없습니다. "
                "원본 로그·분석 데이터셋을 확인하세요."
            )
            self.b3742_prec_run_btn.configure(state="disabled")

    def _purge_dead_tcl_commands(self):
        """Drop already-deleted Tcl command names from every widget before Tk destroy walks them."""
        stack = [self]
        while stack:
            w = stack.pop()
            try:
                stack.extend(list(w.children.values()))
            except Exception:
                pass
            names = getattr(w, "_tclCommands", None)
            if not names:
                continue
            alive = []
            for name in names:
                try:
                    if self.tk.call("info", "commands", name):
                        alive.append(name)
                except tk.TclError:
                    pass
            w._tclCommands = alive

    def _on_main_frame_changed(self, _event=None):
        if getattr(self, "_closing", False):
            return
        try:
            self._refresh_context_tabs()
        except tk.TclError:
            if not getattr(self, "_closing", False):
                raise

    def _context_action(self):
        if self.raid_public_mode:
            return
        page = self.nav_state.get("page")
        if page == "live_try":
            self.open_live_storage_folder()
            return
        if self.nav_state.get("encounterID") is not None:
            self._show_main_frame(self.result_frame)
            return
        self._show_main_frame(self.brief_frame)

    def _b3741_register_tab_host(self, page, head):
        frames = getattr(self, "_b3741_tab_frames", None)
        if frames is None:
            frames = self._b3741_tab_frames = {}
        tf = ttk.Frame(page, style="App.TFrame")
        tf._b3741_after = head
        frames[str(page)] = tf

    def _refresh_context_tabs(self):
        if not hasattr(self, "context_tabs"):
            return
        if self.raid_public_mode:
            # Session, boss board and Try review are separate top-level pages in
            # the public edition; there is no secondary tab strip at all.
            for child in self.context_tabs.winfo_children():
                child.destroy()
            self.context_tab_buttons = []
            if self.context_tabs.winfo_manager():
                self.context_tabs.pack_forget()
            for frame_ in (getattr(self, "_b3741_tab_frames", {}) or {}).values():
                for child in frame_.winfo_children():
                    child.destroy()
                frame_.pack_forget()
            return
        for child in self.context_tabs.winfo_children():
            child.destroy()
        self.context_tab_buttons = []
        try:
            current = self.notebook.nametowidget(self.notebook.select())
        except Exception:
            current = None

        page = self.nav_state.get("page")
        if page == "live_try":
            player = (self._selected_live_row() or {}).get("factsPlayer") or self.current_player
            specs = [
                ("트라이 복기", self.live_frame, None),
                (f"개인 · {str(player).split('-')[0]}", None, self.open_live_death_review_txt),
                ("쿨기 비교", self.cooldown_frame, None),
                ("상세 로그", None, self.open_live_raw_log),
            ]
        elif self.nav_state.get("encounterID") is not None:
            # B37.3.9: keep only the raid-lead workflow in primary navigation.
            # Legacy overview/rule pages remain available internally but no longer
            # compete with the board, cooldown plan, and precision view.
            specs = [("트라이 보드", self.boss_board_frame, None)]
            try:
                _enc = int(self.nav_state.get("encounterID") or 0)
            except Exception:
                _enc = 0
            # The current briefing implementation is Coiled-specific. Do not show
            # a dead/unsupported tab for every other boss.
            if _enc == 3429:
                specs.append(("사전 브리핑", self.boss_briefing_frame, None))
            specs.extend([
                ("상위 쿨기 / NSRT", self.cooldown_frame, None),
                ("정밀 분석", self.precision_frame, None),
            ])
        else:
            specs = [("세션 개요", self.coach_frame, None)]
        # B37.4.1: layout boards put the tabs *under* the page title/chips.
        hosts = getattr(self, "_b3741_tab_frames", {}) or {}
        for frame_ in hosts.values():
            for child in frame_.winfo_children():
                child.destroy()
            frame_.pack_forget()
        target = hosts.get(str(current)) if current is not None else None
        single_session = len(specs) == 1 and specs[0][1] is getattr(self, "coach_frame", None)
        if target is not None or single_session:
            if self.context_tabs.winfo_manager():
                self.context_tabs.pack_forget()
        elif not self.context_tabs.winfo_manager():
            self.context_tabs.pack(fill="x", before=self.notebook)
        if single_session:
            specs = []
        if target is not None and specs:
            target.pack(fill="x", padx=_px(2), pady=_px((0, 10)), after=target._b3741_after)
        tab_parent = target if target is not None else self.context_tabs
        for label, frame, command in specs:
            active = frame is not None and current is frame
            if command is None:
                command = (lambda f=frame: self._show_main_frame(f))
            btn = ttk.Button(tab_parent, text=label, command=command, style="NavActive.TButton" if active else "Nav.TButton")
            btn.pack(side="left", padx=_px((0, 4)))
            self.context_tab_buttons.append(btn)

        if hasattr(self, "context_action_btn"):
            if page == "live_try":
                self.context_action_btn.configure(text="raw 폴더 열기")
            elif self.nav_state.get("encounterID") is not None:
                self.context_action_btn.configure(text="보스 보고서")
            else:
                self.context_action_btn.configure(text="세션 보고서")

    @staticmethod
    def _prepared_row_cache_key(row):
        row = row or {}
        return str(row.get("storageDir") or row.get("metaJson") or "")

    def _build_review_model_uncached(self, row, player=None):
        # Never project a legacy B2/B3 causal verdict into the B34 screens.
        if (row or {}).get("raidReviewStale"):
            return {}
        review = row_json(row or {}, "raidReviewJson")
        if not review:
            return {}
        if str(review.get("engineVersion") or "") != ENGINE_VERSION:
            return {}
        try:
            if int(review.get("schemaVersion") or 0) < 4:
                return {}
        except Exception:
            return {}
        if self.raid_public_mode:
            player = ""
        else:
            player = player or (row or {}).get("factsPlayer") or self.current_player
        raid_facts = row_json(row or {}, "raidFactsJson")
        return build_try_layout_model(
            review, player, raid_facts=raid_facts, raid_death_count=(row or {}).get("deathCount")
        )

    def _review_model_for_row(self, row):
        cache_key = self._prepared_row_cache_key(row)
        prepared = getattr(self, "_prepared_review_models", {}).get(cache_key)
        if prepared is not None:
            return prepared
        return self._build_review_model_uncached(row)

    def _build_series_model_uncached(self, row):
        if (row or {}).get("seriesReviewStale"):
            return {}
        series = row_json(row or {}, "raidSeriesReviewJson")
        module = str((series or {}).get("moduleVersion") or "")
        if not series or not (module == MODULE_VERSION or module.endswith("-B31")):
            return {}
        try:
            if int(series.get("schemaVersion") or 0) < 3:
                return {}
        except Exception:
            return {}
        return build_series_layout_model(series)

    def _series_model_for_rows(self, rows):
        # Newest readable row wins only as a pointer to the same canonical file.
        for row in reversed(list(rows or [])):
            cache_key = self._prepared_row_cache_key(row)
            if cache_key in getattr(self, "_prepared_series_models", {}):
                prepared = self._prepared_series_models.get(cache_key)
                if prepared:
                    return prepared
                continue
            model = self._build_series_model_uncached(row)
            if model:
                return model
        return {}

    def _refresh_layout_session_summary(self):
        if not hasattr(self, "session_try_tree"):
            return
        date = self._nav_session_date()
        rows = [
            r for r in self._all_live_rows()
            if (session_date_from_timestamp(r.get("startTimestamp")) or r.get("sessionDate") or "날짜 미상") == date
        ]
        rows.sort(key=self._live_row_sort_key)
        kills = sum(1 for r in rows if str(r.get("result") or "").upper() == "KILL")
        excluded = sum(1 for r in rows if self._live_row_is_reset(r) or self._live_row_is_fake(r))
        early = sum(1 for r in rows if self._live_row_is_early_wipe(r))
        normal = len(rows) - excluded
        title = "레이드 세션" if date == "분석 세션" else f"{date} 레이드 세션"
        self.session_title_var.set(title)
        stale = sum(1 for r in rows if r.get("reanalysisRequired"))
        stale_note = f" · 재분석 {stale}개 진행/대기" if stale else ""
        early_note = f" · Reset {early}개" if early else ""
        self.session_subtitle_var.set(f"분석된 트라이 {normal}개 · 킬 {kills}{early_note} · 리셋/가짜 풀 {excluded}개 제외{stale_note}")

        for iid in self.session_try_tree.get_children():
            self.session_try_tree.delete(iid)
        self.session_try_rows = {}
        for n, row in enumerate(rows[-12:]):
            model = self._review_model_for_row(row)
            is_fake = self._live_row_is_fake(row)
            is_reset = self._live_row_is_reset(row)
            is_early = self._live_row_is_early_wipe(row)
            mechanic_line = str(row.get("mechanicSummaryLine") or "").strip()
            root = mechanic_line or model.get("rootMechanic") or (
                "가짜 풀" if is_fake else "리셋" if is_reset else
                "Reset · 분석 중" if is_early else
                "구버전 복기 · 재분석 중" if row.get("raidReviewStale") else "분석 중"
            )
            result = _ui_result_en(row.get("result"), fake=is_fake, reset=is_reset, early=is_early)
            iid = f"session-summary-{n}"
            self.session_try_rows[iid] = self._live_row_key(row)
            self.session_try_tree.insert("", "end", iid=iid, values=(
                f"{row.get('name','?')} · Try {row.get('tryNo','?')}",
                result,
                self._fmt_live_duration(row.get("durationSec")),
                root,
            ))

        for iid in self.session_repeat_tree.get_children():
            self.session_repeat_tree.delete(iid)
        by_context = {}
        for row in rows:
            if self._live_row_is_reset(row) or self._live_row_is_fake(row):
                continue
            by_context.setdefault(self._live_boss_group_key(row), []).append(row)
        repeat_i = 0
        for _, group in by_context.items():
            series = self._series_model_for_rows(group)
            if not series:
                continue
            for bucket in series.get("rootBuckets") or []:
                mechanic = bucket.get("mechanic") or "?"
                count = int(bucket.get("count") or 0)
                collapse = int(bucket.get("collapseCount") or 0)
                personal = int(bucket.get("personalCount") or 0)
                detail = f"공대 붕괴 {collapse}" if self.raid_public_mode else f"공대 붕괴 {collapse} · 개인 사건 {personal}"
                self.session_repeat_tree.insert("", "end", iid=f"repeat-{repeat_i}", text=mechanic, values=(count, detail))
                repeat_i += 1
        try:
            self._render_b3794_session(rows)
        except Exception:
            pass
        # B37.4.35: header line = session totals + raid clock span.
        summary = getattr(self, "_b3835_summary", None)
        if summary and summary.get("date") == date:
            line = summary["stats"]
            if summary.get("span"):
                line += "  ·  " + summary["span"]
            self.session_subtitle_var.set(line + stale_note)
        self._refresh_context_tabs()

    _B3794_RESULT_COLORS = {"Wipe": _ui_theme.RESULT_WIPE, "Kill": _ui_theme.RESULT_KILL, "Early Wipe": _ui_theme.RESULT_EARLY_WIPE, "Reset": _ui_theme.RESULT_EARLY_WIPE}

    def _b3794_card_text(self, row):
        """(result, cause, bottom, cause_tag) for one session try card."""
        model = {}
        try:
            model = self._review_model_for_row(row) or {}
        except Exception:
            model = {}
        is_kill = str(row.get("result") or "").upper() == "KILL"
        is_early = self._live_row_is_early_wipe(row)
        result = "Kill" if is_kill else ("Reset" if is_early else "Wipe")
        cause, tag = "", "pending"
        try:
            review = load_mechanic_review(row)
            pm = build_try_priority_model(review, row) if review else {}
        except Exception:
            pm = {}
        if pm.get("firstMistake"):
            cause, tag = str(pm.get("firstMistakeText") or ""), "verified"
        if not cause:
            cause = str(model.get("rootMechanic") or "")
        if is_kill and cause.startswith("처치 성공"):
            cause = ""
        if is_kill:
            death = model.get("selectedPlayerDeath") or {}
            if death and death.get("sec") is not None:
                pname = str(death.get("name") or death.get("player") or row.get("factsPlayer") or self.current_player or "").split("-")[0]
                bottom = f"개인 사망 {pname} {self._fmt_live_relative(death.get('sec'))}"
                if not cause:
                    cause = f"{pname} 사망"
            else:
                bottom = "공대 붕괴 없음"
            if not cause or cause in {"원인 판정 없음"}:
                cause = "킬 성공"
            tag = "kill" if tag != "verified" else tag
        else:
            t = model.get("rootTime")
            bottom = f"공대 붕괴 시작 {self._fmt_live_relative(t)}" if t is not None else ("Reset" if is_early else "붕괴 시각 확인 중")
            if not cause:
                cause = "원인 확인 중"
        return result, cause, bottom, tag

    def _render_b3794_session(self, rows):
        if not hasattr(self, "b3794_boss_list"):
            return
        sig = ("session", self.current_player, tuple(self._b3798_row_signature(r) for r in rows or []),
               tuple(sorted(self.b3794_repeat_open or ())) if self.b3794_repeat_open is not None else None)
        if sig == getattr(self, "_b3798_session_sig", None):
            return
        self._b3798_session_sig = sig
        for w in self.b3794_boss_list.winfo_children():
            w.destroy()
        # Repeat-cause column (right) keeps its B37.3.9.4 data source.
        roots = {}
        for row in rows or []:
            if self._live_row_is_reset(row) or self._live_row_is_fake(row) or self._live_row_is_early_wipe(row):
                continue
            result = "Kill" if str(row.get("result") or "").upper() == "KILL" else "Wipe"
            model = self._review_model_for_row(row) or {}
            rname = str(model.get("rootMechanic") or "").strip()
            death = model.get("selectedPlayerDeath") or {}
            skip = (not rname) or rname in {"원인 판정 없음", "None"} or rname.startswith("처치 성공")
            if result == "Kill" and not death:
                skip = True
            if not skip:
                roots.setdefault(rname, []).append((row, result, model.get("rootTime"), death))
        # B37.4.35: date session summary (read-only aggregation of Try judgments).
        summary = build_session_summary(self._nav_session_date(), self._b3835_entries(rows))
        self._b3835_summary = summary
        self._b3835_render_summary(summary)
        self._render_b3794_repeats(roots)

    # ---- B37.4.35 · date session summary --------------------------------
    def _b3835_priority_model(self, row):
        """Per-Try priority model, cached by the row's render signature."""
        cache = getattr(self, "_b3835_pm_cache", None)
        if cache is None:
            cache = self._b3835_pm_cache = {}
        ck = (self._prepared_row_cache_key(row), self._b3798_row_signature(row))
        if ck in cache:
            return cache[ck]
        pm = {}
        try:
            review = load_mechanic_review(row)
            pm = build_try_priority_model(review, row) if review else {}
        except Exception:
            pm = {}
        if not isinstance(pm, dict) or not pm.get("supported"):
            pm = {}
        if len(cache) > 2000:
            cache.clear()
        cache[ck] = pm
        return pm

    def _b3835_entries(self, rows):
        entries = []
        for row in rows or []:
            excluded = bool(self._live_row_is_reset(row) or self._live_row_is_fake(row))
            pending = bool(row.get("reanalysisRequired") or row.get("raidReviewStale"))
            try:
                supported = bool(supports_boss_mechanic_encounter(row))
            except Exception:
                supported = False
            pm = {} if (excluded or pending or not supported) else self._b3835_priority_model(row)
            entries.append({
                "row": row, "key": self._live_row_key(row), "group": self._live_boss_group_key(row),
                "context": self._live_context_key(row), "pm": pm, "supported": supported,
                "early": bool(self._live_row_is_early_wipe(row)), "excluded": excluded, "pending": pending,
            })
        return entries

    def _b3835_open_try(self, key):
        """Summary → Try detail; make sure that Try is visible in the sidebar."""
        row = self._find_live_row_by_key(key)
        if row is not None:
            try:
                date = self._b3799_nav_payload_date(("live_try", key)) or (
                    session_date_from_timestamp(row.get("startTimestamp")) or row.get("sessionDate") or "")
            except Exception:
                date = ""
            if date and hasattr(self, "_b3799_open_dates"):
                self._b3799_open_dates.add(str(date))
        return self._select_live_row_by_key(key)

    def _b3835_open_boss_board(self, ctx):
        if not ctx:
            return
        self._set_nav_context(tuple(ctx), page="boss", live_try_key=None)
        idx = self._find_boss_index_by_context(tuple(ctx))
        if idx is not None:
            self._select_shared_boss(tuple(ctx), select_page=True)
        else:
            self._refresh_b3641_boss_views()
            self._show_main_frame(self.boss_board_frame)
            self._update_breadcrumb()
            self._refresh_shared_nav_tree()

    @staticmethod
    def _b3835_bind_click(widgets, callback):
        for w in widgets:
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass
            w.bind("<Button-1>", lambda _e, cb=callback: cb())

    def _b3835_walk(self, widget):
        out = [widget]
        for child in widget.winfo_children():
            out.extend(self._b3835_walk(child))
        return out

    @staticmethod
    def _b3835_flow(parent, widgets, avail_w, gap=None):
        """Wrap chips onto new lines instead of squeezing them."""
        gap = _px(4) if gap is None else gap
        x = row = col = 0
        for w in widgets:
            need = int(w.winfo_reqwidth()) + gap
            if col and x + need > avail_w:
                row += 1
                x = col = 0
            w.grid(row=row, column=col, sticky="w", padx=(0, gap), pady=(0, gap))
            x += need
            col += 1
        return row + 1

    _B3835_TONE = {"failed": _ui_theme.STATE_FAILURE, "ok": _ui_theme.STATE_SUCCESS, "neutral": _ui_theme.STATE_UNKNOWN}
    _B3835_CHIP_LIMIT = 9

    def _b3835_render_summary(self, summary):
        parent = self.b3794_boss_list
        fixed = getattr(self, "_fixed_canvas", False)
        # ① 오늘의 주요 개선점 — verified only.
        imp = RoundPanel(parent, "오늘의 주요 개선점", "검증된 실패만 · 같은 기믹은 묶어서", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        imp.pack(fill="x", pady=_px((0, 12)))
        self.b3835_improvement_panel = imp
        circled = "①②③"
        if not summary["improvements"]:
            tk.Label(imp.body, text="검증된 개선점 없음 · 후보/관측 항목은 개선점으로 올리지 않음", bg=_ui_theme.PANEL_BG,
                     fg=_ui_theme.MUTED, font=(FONT_BODY, 10), anchor="w").pack(fill="x", pady=_px((0, 2)))
        for i, item in enumerate(summary["improvements"]):
            box = RoundBox(imp.body, fill=_ui_theme.CARD_BG, outline=_ui_theme.CARD_BORDER, radius=8, padx=12, pady=8, cursor="hand2")
            box.pack(fill="x", pady=_px((0, 6)))
            line = tk.Frame(box.inner, bg=_ui_theme.CARD_BG)
            line.pack(fill="x")
            tk.Label(line, text=circled[i], bg=_ui_theme.CARD_BG, fg=_ui_theme.STATE_FAILURE, font=(FONT_TITLE, 12)).pack(side="left", padx=_px((0, 8)))
            tk.Label(line, text=f"{item['boss']} · {item['label']}", bg=_ui_theme.CARD_BG, fg=_ui_theme.TEXT,
                     font=(FONT_TITLE, 11), anchor="w").pack(side="left")
            tk.Label(line, text=item["detail"], bg=_ui_theme.CARD_BG, fg=_ui_theme.MUTED, font=(FONT_BODY, 9), anchor="e").pack(side="right")
            key = item.get("key")
            self._b3835_bind_click([box, box._canvas] + self._b3835_walk(box.inner), lambda k=key: self._b3835_open_try(k))
        if summary["checks"]:
            tk.Label(imp.body, text="추가 확인 필요 · 후보 신호 (개선점 아님)", bg=_ui_theme.PANEL_BG, fg=_ui_theme.STATE_CANDIDATE,
                     font=(FONT_BODY, 9), anchor="w").pack(fill="x", pady=_px((6, 2)))
            for c in summary["checks"]:
                row = tk.Frame(imp.body, bg=_ui_theme.PANEL_BG, cursor="hand2")
                row.pack(fill="x", pady=_px((0, 2)))
                tk.Label(row, text=f"후보  {c['boss']} · {c['label']}", bg=_ui_theme.PANEL_BG, fg=_ui_theme.STATE_CANDIDATE,
                         font=(FONT_BODY, 10), anchor="w").pack(side="left")
                tk.Label(row, text=c["detail"], bg=_ui_theme.PANEL_BG, fg=_ui_theme.STATE_INACTIVE, font=(FONT_BODY, 9)).pack(side="right")
                key = c.get("key")
                self._b3835_bind_click(self._b3835_walk(row), lambda k=key: self._b3835_open_try(k))

        # ② 보스별 요약 — one row per boss, Try chips underneath.
        bp = RoundPanel(parent, "보스별 요약", "행 → 대표 Try 상세 · 칩 → 해당 Try", title_font=(FONT_TITLE, 12), body_font=(FONT_BODY, 9))
        bp.pack(fill="x", pady=_px((0, 12)))
        self.b3835_boss_panel = bp
        self.b3835_boss_rows = []
        widths = (_px(178), _px(64), _px(40), _px(78))

        def grid_cols(frame):
            for c, w in enumerate(widths):
                frame.columnconfigure(c, minsize=w, weight=0)
            frame.columnconfigure(4, weight=1)

        head = tk.Frame(bp.body, bg=_ui_theme.PANEL_BG)
        head.pack(fill="x", padx=_px(12), pady=_px((0, 4)))
        grid_cols(head)
        for c, text in enumerate(("보스", "결과", "Try", "시간/진도", "핵심 복기")):
            tk.Label(head, text=text, bg=_ui_theme.PANEL_BG, fg=_ui_theme.MUTED, font=(FONT_BODY, 9), anchor="w").grid(row=0, column=c, sticky="w")
        if not summary["bosses"]:
            tk.Label(bp.body, text="이 날짜에 분석된 트라이가 없습니다.", bg=_ui_theme.PANEL_BG, fg=_ui_theme.MUTED,
                     font=(FONT_BODY, 10), anchor="w").pack(fill="x", padx=_px(12), pady=_px(6))
        wrap = _px(300 if fixed else 260)
        try:
            canvas_w = int(self.b3794_left_canvas.winfo_width())
        except Exception:
            canvas_w = 0
        if canvas_w < _px(400):
            canvas_w = _px(720)
        # panel padding (16) + card padding (12) on both sides, plus margins
        avail_w = max(_px(240), canvas_w - _px(2 * 16 + 2 * 12 + 8))
        for b in summary["bosses"]:
            box = RoundBox(bp.body, fill=_ui_theme.CARD_BG, outline=_ui_theme.CARD_BORDER, radius=8, padx=12, pady=8, cursor="hand2")
            box.pack(fill="x", pady=_px((0, 6)))
            line = tk.Frame(box.inner, bg=_ui_theme.CARD_BG)
            line.pack(fill="x")
            grid_cols(line)
            name = b["name"] + (f"  {b['difficulty']}" if b["difficulty"] else "")
            cells = [
                tk.Label(line, text=b["name"], bg=_ui_theme.CARD_BG, fg=_ui_theme.TEXT, font=(FONT_TITLE, 11), anchor="w", justify="left", wraplength=widths[0] - _px(6)),
                tk.Label(line, text=b["result"], bg=_ui_theme.CARD_BG, fg=self._B3794_RESULT_COLORS.get(b["result"], _ui_theme.MUTED), font=(FONT_BODY, 10), anchor="w"),
                tk.Label(line, text=str(b["tryCount"]), bg=_ui_theme.CARD_BG, fg=_ui_theme.TEXT, font=(FONT_BODY, 10), anchor="w"),
                tk.Label(line, text=b["timeText"], bg=_ui_theme.CARD_BG, fg=_ui_theme.TEXT, font=(FONT_BODY, 10), anchor="w"),
                tk.Label(line, text=b["keyLine"], bg=_ui_theme.CARD_BG, fg=self._B3835_TONE.get(b["tone"], _ui_theme.MUTED),
                         font=(FONT_BODY, 10), anchor="w", justify="left", wraplength=wrap),
            ]
            for c, w in enumerate(cells):
                w.grid(row=0, column=c, sticky="nw", pady=_px((1, 0)))
            if b["difficulty"]:
                tk.Label(line, text=b["difficulty"], bg=_ui_theme.CARD_BG, fg=_ui_theme.MUTED, font=(FONT_BODY, 8), anchor="w").grid(row=1, column=0, sticky="w")
            key = b.get("targetKey")
            self._b3835_bind_click([box, box._canvas, box.inner, line] + cells, lambda k=key: self._b3835_open_try(k))
            chips_widgets = []
            if b["tryCount"] > 1:
                chips = tk.Frame(box.inner, bg=_ui_theme.CARD_BG)
                chips.pack(fill="x", pady=_px((6, 0)))
                shown = b["chips"][-self._B3835_CHIP_LIMIT:]
                hidden_n = len(b["chips"]) - len(shown)
                if hidden_n > 0:
                    more = tk.Label(chips, text=f"이전 {hidden_n}개 · Try 보드", bg="#1e232a", fg=_ui_theme.STATE_INFO,
                                    font=(FONT_BODY, 9), padx=_px(8), pady=_px(2))
                    self._b3835_bind_click([more], lambda ctx=b.get("context"): self._b3835_open_boss_board(ctx))
                    chips_widgets.append(more)
                for ch in shown:
                    short = "Reset" if ch["result"] in ("Early Wipe", "Reset") else ch["result"]
                    clock = f" · {ch['clock']}" if ch.get("clock") else ""
                    lab = tk.Label(chips, text=f"Try {ch['tryNo']}{clock} {short} {ch['duration']}", bg="#1e232a",
                                   fg=self._B3794_RESULT_COLORS.get(ch["result"], _ui_theme.MUTED),
                                   font=(FONT_BODY, 9), padx=_px(8), pady=_px(2))
                    self._b3835_bind_click([lab], lambda k=ch["key"]: self._b3835_open_try(k))
                    chips_widgets.append(lab)
                self._b3835_flow(chips, chips_widgets, avail_w)
            self.b3835_boss_rows.append({"boss": b, "box": box, "cells": cells, "chips": chips_widgets})
        self.b3835_other_chips = []
        if summary.get("others"):
            other = tk.Frame(bp.body, bg=_ui_theme.PANEL_BG)
            other.pack(fill="x", padx=_px(12), pady=_px((4, 0)))
            tk.Label(other, text="레이드 외 전투 · 집계 제외", bg=_ui_theme.PANEL_BG, fg=_ui_theme.MUTED,
                     font=(FONT_BODY, 9), anchor="w").grid(row=0, column=0, sticky="w", pady=(0, _px(4)))
            flow = tk.Frame(other, bg=_ui_theme.PANEL_BG)
            flow.grid(row=1, column=0, sticky="w")
            for o in summary["others"]:
                lab = tk.Label(flow, text=f"{o['name']} {o['result']} {o['duration']}", bg="#1e232a", fg=_ui_theme.STATE_INACTIVE,
                               font=(FONT_BODY, 9), padx=_px(8), pady=_px(2))
                self._b3835_bind_click([lab], lambda k=o["key"]: self._b3835_open_try(k))
                self.b3835_other_chips.append(lab)
            self._b3835_flow(flow, self.b3835_other_chips, avail_w + _px(24))

    def _render_b3794_repeats(self, roots):
        box = self.b3794_repeat_list
        for w in box.winfo_children():
            w.destroy()
        items = sorted(roots.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        if not items:
            tk.Label(box, text="반복 원인 없음", bg=_ui_theme.PANEL_BG, fg=_ui_theme.MUTED, font=(FONT_BODY, 9), anchor="w", padx=_px(6), pady=_px(6)).pack(fill="x")
            return
        if self.b3794_repeat_open is None:
            self.b3794_repeat_open = {items[0][0]} if len(items[0][1]) >= 2 else set()
        for idx, (name, entries) in enumerate(items[:5]):
            is_open = name in self.b3794_repeat_open
            hot = len(entries) >= 2 and is_open
            bg = _ui_theme.HILITE_BG if hot else _ui_theme.CARD_BG
            rbox = RoundBox(box, fill=bg, outline=_ui_theme.HILITE_BORDER if hot else _ui_theme.CARD_BORDER, radius=8, padx=12, pady=10)
            rbox.pack(fill="x", pady=_px((0, 8)))
            card = rbox.inner
            head = tk.Frame(card, bg=bg, cursor="hand2")
            head.pack(fill="x")
            tk.Label(head, text=name, bg=bg, fg=_ui_theme.TEXT, font=(FONT_TITLE, 11), anchor="w", justify="left", wraplength=_px(230)).pack(side="left")
            tk.Label(head, text=f"{len(entries)}회 {'▾' if is_open else '▸'}", bg=bg, fg=_ui_theme.ORANGE, font=(FONT_TITLE, 11)).pack(side="right")
            for w in [head] + list(head.winfo_children()):
                w.bind("<Button-1>", lambda _e, n=name: self._b3794_toggle_repeat(n))
            if not is_open:
                continue
            collapse = sum(1 for e in entries if e[1] == "Wipe")
            personal = len(entries) - collapse
            tk.Label(card, text=f"공대 붕괴 {collapse} · 개인 사건 {personal} · 근거 트라이를 골라 이동", bg=bg, fg="#c9cfd6", font=(FONT_BODY, 9), anchor="w").pack(fill="x", pady=_px((6, 4)))
            shown = entries[:5]
            for row, result, t, death in shown:
                lbox = RoundBox(card, fill="#1e232a", outline="#1e232a", radius=6, padx=10, pady=6, cursor="hand2")
                lbox.pack(fill="x", pady=_px((4, 0)))
                line = lbox.inner
                label = f"Try {row.get('tryNo', '?')} · 공대 붕괴 시작" if result == "Wipe" else f"Try {row.get('tryNo', '?')} Kill · 원인 피격"
                tk.Label(line, text=label, bg="#1e232a", fg=_ui_theme.TEXT, font=(FONT_BODY, 10), anchor="w").pack(side="left")
                tk.Label(line, text=self._fmt_live_relative(t) if t is not None else "-", bg="#1e232a", fg="#c9cfd6", font=("Consolas", 10)).pack(side="right")
                rk = self._live_row_key(row)
                for w in [lbox, lbox._canvas, line] + list(line.winfo_children()):
                    w.bind("<Button-1>", lambda _e, k=rk: self._select_live_row_by_key(k))
            if len(entries) > len(shown):
                tk.Label(card, text=f"외 {len(entries) - len(shown)}개 · 보스 트라이 보드에서 전체 보기", bg=bg, fg=_ui_theme.MUTED, font=(FONT_BODY, 8), anchor="w").pack(fill="x", pady=_px((6, 0)))

    def _b3794_toggle_repeat(self, name):
        if self.b3794_repeat_open is None:
            self.b3794_repeat_open = set()
        if name in self.b3794_repeat_open:
            self.b3794_repeat_open.discard(name)
        else:
            self.b3794_repeat_open.add(name)
        try:
            self._refresh_layout_session_summary()
        except Exception:
            pass

    def _refresh_boss_try_progress(self):
        if not hasattr(self, "boss_try_tree"):
            return
        for iid in self.boss_try_tree.get_children():
            self.boss_try_tree.delete(iid)
        self.boss_try_rows = {}
        rows = self._b3641_context_rows(all_dates=False)
        rows.sort(key=self._live_row_sort_key)
        # B34.2.4: keep a six-pull boss fully visible (especially the kill)
        # without making long-progress bosses dominate the page.
        try:
            self.boss_try_tree.configure(height=max(5, min(8, len(rows) or 5)))
        except Exception:
            pass
        for n, row in enumerate(rows):
            model = self._review_model_for_row(row)
            is_fake = self._live_row_is_fake(row)
            is_reset = self._live_row_is_reset(row)
            is_early = self._live_row_is_early_wipe(row)
            root = model.get("rootMechanic") or (
                "가짜 풀" if is_fake else "리셋" if is_reset else
                "Reset · 분석 중" if is_early else
                "구버전 복기 · 재분석 중" if row.get("raidReviewStale") else "분석 중"
            )
            root_at = self._fmt_live_relative(model.get("rootTime")) if model.get("rootTime") is not None else "-"
            personal = ""
            death = model.get("selectedPlayerDeath") or {}
            if death:
                pname = str(death.get("name") or death.get("player") or row.get("factsPlayer") or "?").split("-")[0]
                personal = f"{pname} {self._fmt_live_relative(death.get('sec'))}"
            result = _ui_result_en(row.get("result"), fake=is_fake, reset=is_reset, early=is_early)
            iid = f"boss-progress-{n}"
            self.boss_try_rows[iid] = self._live_row_key(row)
            self.boss_try_tree.insert("", "end", iid=iid, text=f"Try {row.get('tryNo','?')}", values=(
                result, self._fmt_live_duration(row.get("durationSec")), root, root_at, personal
            ))

    def _select_live_row_by_key(self, wanted):
        row = self._find_live_row_by_key(wanted)
        if row is None:
            return False
        key = self._live_row_key(row)
        self.live_selected_key = key
        self._set_nav_context(self._live_context_key(row), page="live_try", live_try_key=key)

        # Mirror the selection into the legacy recent widget only when this Try is
        # actually present there.  Older stored rows intentionally do not depend on
        # that widget.
        iid = self._find_live_tree_iid_by_key(key)
        if iid is not None:
            try:
                self.live_recent_tree.selection_set(iid)
                self.live_recent_tree.focus(iid)
                self.live_recent_tree.see(iid)
            except Exception:
                pass
        self._prioritize_saved_reanalysis_row(row)
        self._update_live_selection(row)
        self._render_live_facts(row)
        self._show_main_frame(self.live_frame)
        # B37.5.0.5 wheel regression guard: newly built Try cards must get
        # pointer-first bindtags even when the global <Map> event was consumed.
        self.after_idle(self._b3750_tag_all_widgets)
        self._update_breadcrumb()
        self._refresh_shared_nav_tree()
        return True

    def _open_session_try_from_summary(self, _event=None):
        sel = self.session_try_tree.selection() if hasattr(self, "session_try_tree") else ()
        if sel:
            self._select_live_row_by_key(self.session_try_rows.get(sel[0]))

    def _open_boss_try_from_progress(self, _event=None):
        sel = self.boss_try_tree.selection() if hasattr(self, "boss_try_tree") else ()
        if sel:
            self._select_live_row_by_key(self.boss_try_rows.get(sel[0]))

    def _refresh_available_players(self, choose_first_if_needed: bool = False):
        # 세 캐릭터는 항상 선택 가능. 우측에는 '현재 로그 한 개의 감지 결과'가 아니라
        # 캐릭터별 최신 로그 연결 상태를 표시한다.
        values = list(CHARACTERS)
        self.player_combo["values"] = values

        connected = []
        missing = []
        for p in CHARACTERS:
            log_path = self.character_logs.get(p)
            if log_path and log_path.exists():
                connected.append(p)
            else:
                missing.append(p)

        if connected:
            msg = "로그 연결: " + " / ".join(connected)
            if missing:
                msg += "  · 미연결: " + " / ".join(missing)
            self.players_found_var.set(msg)
        else:
            self.players_found_var.set("분석 전: 3캐릭 최신 로그 탐색")

        if self.current_player not in CHARACTERS:
            self.current_player = DEFAULT_PLAYER
            self.player_var.set(self.current_player)
        return values

    def _refresh_log_label(self):
        if self.raid_public_mode:
            self.current_dataset = None
            self.log_var.set("공대 모드 · 캐릭터 선택 없음")
            return
        if self.current_log:
            self.current_dataset = dataset_from_log(self.current_log)
            mode = "수동" if self.current_player in self.manual_log_players else "자동"
            self.log_var.set(f"{self.current_player} 로그({mode}): {self.current_log}")
        else:
            self.log_var.set(f"{self.current_player} 로그: 찾지 못함")
            self.current_dataset = None
        self._refresh_available_players(choose_first_if_needed=False)

    def _reset_shared_nav_for_source(self):
        """Reset selection when the backing character/log source really changes."""
        self.nav_state = new_selection_state(self.current_player)
        self.nav_state["sessionDate"] = self._dataset_session_date()
        self.live_selected_key = None

    def _switch_to_player_log(
        self,
        player: str,
        refresh_results: bool = True,
        force_find: bool = False,
    ):
        """캐릭터 전환 시 그 캐릭터가 포함된 최신 로그/데이터셋으로 이동."""
        if player not in CHARACTERS:
            return

        self.status_var.set(f"{player} 최신 로그 탐색 중...")
        self.update_idletasks()

        # 직접 고른 로그는 '최신 로그 다시 찾기' 전까지 해당 캐릭터에서 유지.
        chosen = None
        if not force_find and player in self.manual_log_players:
            p = self.character_logs.get(player)
            if p and p.exists():
                chosen = p

        if chosen is None:
            chosen = find_latest_log_for_player(player, force_rescan=force_find)
            self.character_logs[player] = chosen
            if force_find:
                self.manual_log_players.discard(player)

        previous_log = str(self.current_log or "")
        previous_player = str(self.nav_state.get("player") or "")
        self.current_log = chosen
        self._refresh_log_label()
        if previous_log != str(chosen or "") or previous_player != str(self.current_player):
            self._reset_shared_nav_for_source()

        if not chosen:
            self.status_var.set(f"{player} · 포함된 CombatLog를 찾지 못함")
            if refresh_results:
                self.coach.delete("1.0", "end")
                self.coach.insert(
                    "end",
                    f"{player}가 포함된 WoWCombatLog를 {LOGDIR}에서 찾지 못했습니다.\n"
                    "로그 선택으로 직접 지정할 수도 있습니다.",
                    "note",
                )
            return

        summary = self.summary_path()
        if refresh_results and summary and summary.exists():
            self.status_var.set(f"{player} · 기존 분석 결과 로드")
            self.load_results()
        elif refresh_results:
            self.status_var.set(f"{player} · 최신 로그 연결됨 · 분석 필요")
            self.coach.delete("1.0", "end")
            self.coach.insert(
                "end",
                f"{player} 최신 로그를 자동 선택했습니다.\n{chosen}\n\n"
                "[최신/선택 로그 분석 시작]을 누르면 이 로그를 분석합니다.",
                "note",
            )

    def on_player_changed(self, _event=None):
        if self.batch_running or (self.proc is not None and self.proc.poll() is None):
            self.player_var.set(self.current_player)
            messagebox.showinfo("분석 중", "3캐릭 일괄 분석이 끝난 뒤 캐릭터를 변경하세요.")
            return
        if self.precision_running or self.cooldown_running:
            self.player_var.set(self.current_player)
            messagebox.showinfo("분석 중", "정밀/쿨기 분석이 끝난 뒤 캐릭터를 변경하세요.")
            return

        player = self.player_var.get().strip()
        if player not in CHARACTERS:
            return
        self.current_player = player
        if self.live_monitor is not None:
            self.live_monitor.set_player(player)

        self.boss_data = []
        self._clear_boss_tree()
        self.coach.delete("1.0", "end")
        self.quality.delete("1.0", "end")
        self.burst.delete("1.0", "end")
        self.precision.delete("1.0", "end")
        self.cooldown_text.delete("1.0", "end")
        self.brief.delete("1.0", "end")
        self.result.delete("1.0", "end")
        self.boss_detail.delete("1.0", "end")
        self.cooldown_data = None
        self.cooldown_context_key = None
        self.cooldown_entries = {}
        self.cooldown_combo["values"] = ()
        self.cooldown_choice_var.set("")
        if hasattr(self, "cooldown_sample_var"):
            self.cooldown_sample_var.set("")
        if hasattr(self, "cooldown_heatmap"):
            self.cooldown_heatmap.delete("all")
        self._clear_cooldown_selectors()

        # 핵심: 표시 캐릭터만 바꾸는 게 아니라 해당 캐릭터의 최신 로그까지 같이 바꾼다.
        self._switch_to_player_log(player, refresh_results=True)
        # V4.7: 세션 탐색(왼쪽 트리)도 그 캐릭터의 트라이로 다시 그린다. 전에는 분석 결과가 없는 캐릭터로
        # 바꾸면 트리가 이전 캐릭터 그대로 남았다. 열려 있던 트라이가 다른 캐릭터 것이면 새 캐릭터의
        # 가장 최근 트라이를 연다. 실패해도 캐릭터 전환은 그대로.
        try:
            self._refresh_shared_nav_tree()
            shown = self._selected_live_row() or getattr(self, "_b3641_try_row", None)
            if shown is not None and not self._row_visible_for_current_player(shown):
                mine = self._all_live_rows()
                if mine:
                    try:
                        self._b3799_open_dates.add(session_date_from_timestamp(mine[-1].get("startTimestamp")) or "")
                    except Exception:
                        pass
                    self._select_live_row_by_key(self._live_row_key(mine[-1]))
                    self._update_live_selection(mine[-1])
                else:
                    self.live_selected_key = None
                    self._update_live_selection(None)
                self._refresh_shared_nav_tree()
        except Exception:
            pass
        try:
            self._refresh_layout_session_summary()
        except Exception:
            pass

    def _public_start_legacy_migration(self):
        if not self.raid_public_mode or not self._public_migration_running:
            return
        legacy = WORKDIR / 'data' / 'live_sessions'
        destination = public_sessions_root(self._public_home)
        self.status_var.set('기존 공대 세션 복사 중 · 원본 유지')
        def worker():
            try:
                cleanup_stale_active(destination)
                result = migrate_legacy_public_sessions(legacy, destination)
                self.msgq.put(('public_migration_done', result))
            except Exception as exc:
                self.msgq.put(('public_migration_error', str(exc)))
        threading.Thread(target=worker, name='NiniPublicLegacyMigration', daemon=True).start()

    def _public_select_log_directory(self):
        if not self.raid_public_mode:
            return None
        folder = filedialog.askdirectory(
            title="WoW _retail_\\Logs 폴더를 선택해 주세요",
            initialdir=str(self._public_log_dir) if self._public_log_dir and self._public_log_dir.is_dir()
                       else str(Path.home()),
        )
        if not folder:
            return None
        try:
            chosen = remember_log_dir(folder, self._public_home)
        except ValueError as exc:
            messagebox.showwarning("WoW 로그 폴더 확인", str(exc))
            return None
        self._public_log_dir = chosen
        if self.live_monitor and not self.live_running:
            self.live_monitor.tailer.log_dir = chosen
        if hasattr(self, 'live_file_var'):
            self.live_file_var.set(f"로그 폴더: {chosen}")
        return chosen

    def _public_show_settings(self):
        folder = str(self._public_log_dir) if self._public_log_dir else "미지정"
        change = messagebox.askyesno(
            "NiniAnalyzer 공개판 설정",
            f"공대 세션 저장: {public_sessions_root(self._public_home)}\n"
            f"WoW 로그 폴더: {folder}\n\n로그 폴더를 변경할까요?",
        )
        if change:
            if self.live_running or getattr(self, '_live_start_pending', False) or self._public_import_running:
                messagebox.showwarning("작업 진행 중", "감시/가져오기가 끝난 뒤 폴더를 변경해 주세요.")
            else:
                self._public_select_log_directory()

    def _public_import_latest(self):
        """Raid-only import, using remembered / discovered WoW log folder."""
        if not self.raid_public_mode:
            return
        folder = self._public_log_dir
        chosen = latest_log_file(folder)
        if chosen is None:
            folder = discover_log_dir(settings=read_public_settings(self._public_home))
            chosen = latest_log_file(folder)
        if chosen is None:
            self.status_var.set("공개판 · WoW 로그 폴더를 선택해 주세요")
            messagebox.showinfo("로그 폴더 선택", "WoW 로그 파일을 찾지 못했습니다.\n로그 폴더를 직접 선택해 주세요.")
            folder = self._public_select_log_directory()
            chosen = latest_log_file(folder)
        if chosen is None:
            self.status_var.set("WoWCombatLog 파일 없음 · 로그 파일 선택을 이용해 주세요")
            return
        self._public_import_start(chosen)

    def _public_import_choose(self):
        if not self.raid_public_mode:
            return
        chosen = filedialog.askopenfilename(
            title="공대 WoWCombatLog 선택",
            initialdir=str(self._public_log_dir if self._public_log_dir and self._public_log_dir.exists() else Path.home()),
            filetypes=[("WoW CombatLog", "WoWCombatLog-*.txt"), ("텍스트 로그", "*.txt")],
        )
        if chosen:
            self._public_import_start(Path(chosen))

    def _public_import_start(self, chosen):
        if not self.raid_public_mode:
            return
        if (self._public_import_running or self._public_migration_running or self.live_running
                or getattr(self, "_live_start_pending", False)):
            self.status_var.set("공개판 · 감시/가져오기 진행 중에는 다른 로그를 가져올 수 없습니다")
            return
        if (getattr(self, "_saved_reanalysis_running", False)
                or getattr(self, "_identity_audit_running", False)):
            self.status_var.set("기존 기록 확인/재분석이 끝난 뒤 로그를 가져와 주세요")
            return
        chosen = Path(chosen)
        if not chosen.is_file():
            messagebox.showerror("로그 읽기 실패", str(chosen))
            return
        # A manually imported TXT may come from Downloads or another guild
        # member. It must NOT change the real-time monitoring Logs directory.
        # Only the settings workflow (_public_select_log_directory) persists
        # that location and updates the monitor.
        chosen = chosen.resolve()
        self._public_import_cancel = threading.Event()
        self._public_import_running = True
        self.run_btn.configure(state="disabled")
        self.public_log_pick_btn.configure(state="disabled")
        self.status_var.set(f"공대 로그 가져오기 준비 · {chosen.name}")
        self._progress_begin("공대 로그 가져오기", "public_import")
        self._progress_set(0, "공대 로그 분석 중", owner="public_import")
        storage_root = Path(self.live_monitor.storage_root)
        def worker():
            try:
                from live.public_import import import_public_combat_log
                result = import_public_combat_log(
                    chosen, storage_root,
                    progress=lambda payload: self.msgq.put(("public_import_progress", payload)),
                    cancel_event=self._public_import_cancel,
                )
                self.msgq.put(("public_import_done", result))
            except Exception as exc:
                self.msgq.put(("public_import_error", str(exc)))
        threading.Thread(target=worker, name="NiniPublicRaidImport", daemon=True).start()

    def _public_start_legacy_migration(self):
        if not self.raid_public_mode or not self._public_migration_running:
            return
        legacy = WORKDIR / 'data' / 'live_sessions'
        destination = public_sessions_root(self._public_home)
        self.status_var.set('기존 공대 세션 복사 중 · 원본 유지')
        def worker():
            try:
                cleanup_stale_active(destination)
                result = migrate_legacy_public_sessions(legacy, destination)
                self.msgq.put(('public_migration_done', result))
            except Exception as exc:
                self.msgq.put(('public_migration_error', str(exc)))
        threading.Thread(target=worker, name='NiniPublicLegacyMigration', daemon=True).start()

    def _public_select_log_directory(self):
        if not self.raid_public_mode:
            return None
        folder = filedialog.askdirectory(
            title="WoW _retail_\\Logs 폴더를 선택해 주세요",
            initialdir=str(self._public_log_dir) if self._public_log_dir and self._public_log_dir.is_dir()
                       else str(Path.home()),
        )
        if not folder:
            return None
        try:
            chosen = remember_log_dir(folder, self._public_home)
        except ValueError as exc:
            messagebox.showwarning("WoW 로그 폴더 확인", str(exc))
            return None
        self._public_log_dir = chosen
        if self.live_monitor and not self.live_running:
            self.live_monitor.tailer.log_dir = chosen
        if hasattr(self, 'live_file_var'):
            self.live_file_var.set(f"로그 폴더: {chosen}")
        return chosen

    def _public_show_settings(self):
        """RC4.2: settings window (log folder + opt-in Feign Death recheck)."""
        from live.public_feign_ui import open_settings
        return open_settings(self)

    def _public_import_latest(self):
        """Raid-only import, using remembered / discovered WoW log folder."""
        if not self.raid_public_mode:
            return
        folder = self._public_log_dir
        chosen = latest_log_file(folder)
        if chosen is None:
            folder = discover_log_dir(settings=read_public_settings(self._public_home))
            chosen = latest_log_file(folder)
        if chosen is None:
            self.status_var.set("공개판 · WoW 로그 폴더를 선택해 주세요")
            messagebox.showinfo("로그 폴더 선택", "WoW 로그 파일을 찾지 못했습니다.\n로그 폴더를 직접 선택해 주세요.")
            folder = self._public_select_log_directory()
            chosen = latest_log_file(folder)
        if chosen is None:
            self.status_var.set("WoWCombatLog 파일 없음 · 로그 파일 선택을 이용해 주세요")
            return
        self._public_import_start(chosen)

    def _public_import_choose(self):
        if not self.raid_public_mode:
            return
        chosen = filedialog.askopenfilename(
            title="공대 WoWCombatLog 선택",
            initialdir=str(self._public_log_dir if self._public_log_dir and self._public_log_dir.exists() else Path.home()),
            filetypes=[("WoW CombatLog", "WoWCombatLog-*.txt"), ("텍스트 로그", "*.txt")],
        )
        if chosen:
            self._public_import_start(Path(chosen))

    def _public_import_start(self, chosen):
        if not self.raid_public_mode:
            return
        if (self._public_import_running or self._public_migration_running or self.live_running
                or getattr(self, "_live_start_pending", False)):
            self.status_var.set("공개판 · 감시/가져오기 진행 중에는 다른 로그를 가져올 수 없습니다")
            return
        if (getattr(self, "_saved_reanalysis_running", False)
                or getattr(self, "_identity_audit_running", False)
                or getattr(self, "_feign_recheck_running", False)):
            self.status_var.set("기존 기록 확인/재분석이 끝난 뒤 로그를 가져와 주세요")
            return
        chosen = Path(chosen)
        if not chosen.is_file():
            messagebox.showerror("로그 읽기 실패", str(chosen))
            return
        # A manually imported TXT may come from Downloads or another guild
        # member. It must NOT change the real-time monitoring Logs directory.
        # Only the settings workflow (_public_select_log_directory) persists
        # that location and updates the monitor.
        chosen = chosen.resolve()
        self._public_import_cancel = threading.Event()
        self._public_import_running = True
        self.run_btn.configure(state="disabled")
        self.public_log_pick_btn.configure(state="disabled")
        self.status_var.set(f"공대 로그 가져오기 준비 · {chosen.name}")
        self._progress_begin("공대 로그 가져오기", "public_import")
        self._progress_set(0, "공대 로그 분석 중", owner="public_import")
        storage_root = Path(self.live_monitor.storage_root)
        def worker():
            try:
                from live.public_import import import_public_combat_log
                result = import_public_combat_log(
                    chosen, storage_root,
                    progress=lambda payload: self.msgq.put(("public_import_progress", payload)),
                    cancel_event=self._public_import_cancel,
                )
                self.msgq.put(("public_import_done", result))
            except Exception as exc:
                self.msgq.put(("public_import_error", str(exc)))
        threading.Thread(target=worker, name="NiniPublicRaidImport", daemon=True).start()

    def pick_latest(self):
        # 현재 선택 캐릭터 기준으로 실제 포함된 최신 로그를 다시 탐색.
        self._switch_to_player_log(
            self.current_player,
            refresh_results=True,
            force_find=True,
        )

    def pick_log(self):
        initial = str(LOGDIR) if LOGDIR.exists() else str(WORKDIR)
        p = filedialog.askopenfilename(
            title="WoWCombatLog 선택",
            initialdir=initial,
            filetypes=[("WoW Combat Log", "WoWCombatLog-*.txt"), ("Text", "*.txt")],
        )
        if p:
            chosen = Path(p)
            previous_log = str(self.current_log or "")
            self.current_log = chosen
            self.character_logs[self.current_player] = chosen
            self.manual_log_players.add(self.current_player)
            self._refresh_log_label()
            if previous_log != str(chosen):
                self._reset_shared_nav_for_source()
            detected = players_in_log(chosen)
            if not detected.get(self.current_player, False):
                messagebox.showwarning(
                    "캐릭터 미감지",
                    f"선택한 로그에서 {self.current_player} 이름을 찾지 못했습니다.\n"
                    "그래도 수동 선택은 유지합니다.",
                )
            summary = self.summary_path()
            if summary and summary.exists():
                self.load_results()
            else:
                self.status_var.set(f"{self.current_player} · 수동 로그 연결됨 · 분석 필요")

    def multi_manifest_path(self) -> Path | None:
        if not self.current_dataset:
            return None
        return self.current_dataset / "multi_character_manifest.json"

    def manifest_is_current(self) -> bool:
        p = self.multi_manifest_path()
        if not p or not p.exists():
            return False
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            return str(data.get("version")) in {"2.1", "2.2-clean", "2.2.3", "2.2.4", "2.2.5", "2.2.6", "2.2.7", "2.3.0-beta1"}
        except Exception:
            return False

    def _resolve_batch_jobs(self, force_find: bool = False):
        """3캐릭 최신 로그를 찾고 같은 로그는 한 작업으로 합친다."""
        mapping: dict[str, Path | None] = {}

        for player in CHARACTERS:
            chosen = None

            # 사용자가 직접 지정한 로그는 해당 캐릭터에 한해 우선한다.
            if not force_find and player in self.manual_log_players:
                p = self.character_logs.get(player)
                if p and p.exists():
                    chosen = p

            if chosen is None:
                chosen = find_latest_log_for_player(
                    player,
                    force_rescan=force_find,
                )
                self.character_logs[player] = chosen
                if force_find:
                    self.manual_log_players.discard(player)

            mapping[player] = chosen

        grouped: dict[str, dict] = {}
        missing = []
        for player in CHARACTERS:
            log_path = mapping.get(player)
            if not log_path or not log_path.exists():
                missing.append(player)
                continue

            try:
                key = str(log_path.resolve()).lower()
            except Exception:
                key = str(log_path).lower()

            if key not in grouped:
                grouped[key] = {
                    "log": log_path,
                    "players": [],
                }
            grouped[key]["players"].append(player)

        # 최신 로그부터 처리. 같은 로그에 여러 캐릭터가 있으면 1회만 실행.
        jobs = list(grouped.values())
        jobs.sort(
            key=lambda j: j["log"].stat().st_mtime if j["log"].exists() else 0,
            reverse=True,
        )
        return jobs, missing

    def _command_for_log_job(self, log_path: Path, force: bool, analysis_players: list[str]):
        """공유 scan/dataset은 3캐릭을 유지하되 실제 분석은 이 배치 담당만 실행."""
        dataset = dataset_from_log(log_path)
        fights = dataset_fights_path(dataset)

        if (
            not force
            and path_is_fresh(fights, log_path)
            and dataset_has_all_tracked_players(dataset)
        ):
            cmd = [
                str(PYTHON), str(PIPELINE),
                "--dataset", str(dataset),
                "--all-players",
                "--analysis-players", *analysis_players,
            ]
            source = "dataset-cache"
        else:
            cmd = [
                str(PYTHON), str(PIPELINE),
                "--log", str(log_path),
                "--all-players",
                "--analysis-players", *analysis_players,
            ]
            source = "combatlog"

        if force:
            cmd.append("--force")
        return cmd, source

    @staticmethod
    def _fmt_live_bytes(value):
        try:
            n = float(value)
        except Exception:
            return "-"
        if n >= 1024 ** 3:
            return f"{n / (1024 ** 3):.2f} GiB"
        if n >= 1024 ** 2:
            return f"{n / (1024 ** 2):.1f} MiB"
        if n >= 1024:
            return f"{n / 1024:.1f} KiB"
        return f"{int(n)} B"

    @staticmethod
    def _fmt_live_duration(seconds):
        if seconds is None:
            return "-"
        try:
            # Approved layout uses truncation: 409.685 -> 6:49, never 6:50.
            total = max(0, int(float(seconds)))
        except Exception:
            return "-"
        return f"{total // 60}:{total % 60:02d}"

    @staticmethod
    def _fmt_live_relative(seconds):
        if seconds is None:
            return "-"
        try:
            value = float(seconds)
        except Exception:
            return "-"
        if value < 0:
            return f"{value:.3f}s"
        minute = int(value // 60)
        remain = value - minute * 60
        return f"{minute}:{remain:04.1f}"

    def _append_live_log(self, text: str, tag=None):
        if not hasattr(self, "live_log"):
            return
        self.live_log.insert("end", text.rstrip() + "\n", tag if tag else ())
        try:
            lines = int(self.live_log.index("end-1c").split(".")[0])
            if lines > 250:
                self.live_log.delete("1.0", f"{lines - 220}.0")
        except Exception:
            pass
        self.live_log.see("end")

    def start_live_monitor(self):
        if self.live_monitor is None:
            messagebox.showerror("Live 모듈 없음", "live/ 모듈을 불러오지 못했습니다.")
            return
        if self.live_running or getattr(self, "_live_start_pending", False):
            return
        if (getattr(self, "_public_import_running", False) or getattr(self, "_public_migration_running", False)
                or getattr(self, "_feign_recheck_running", False)):
            if hasattr(self, "status_var"):
                self.status_var.set("공대 로그 가져오기 완료 후 실시간 감시를 시작하세요")
            return
        if (self.raid_public_mode and getattr(self.live_monitor, 'tailer', None) is not None
                and (self._public_log_dir is None or not self._public_log_dir.is_dir())):
            if self._public_select_log_directory() is None:
                self.status_var.set("WoW 로그 폴더를 선택해야 감시할 수 있습니다")
                return
        # The monitor's start() also checks/synchronizes pending archives.
        # Run it in the background; Tk is touched only by the completion poll.
        self._live_start_pending = True
        self._live_start_cancelled = False
        self._live_start_results = queue.Queue(maxsize=1)
        if hasattr(self, "live_start_btn"):
            self.live_start_btn.configure(state="disabled")
            self.live_stop_btn.configure(state="normal")
        if hasattr(self, "sidebar_live_start_btn"):
            self.sidebar_live_start_btn.configure(state="disabled")
        if hasattr(self, "sidebar_live_stop_btn"):
            self.sidebar_live_stop_btn.configure(state="normal")
        self.live_state_var.set("◌ 감시 준비 중")
        if hasattr(self, "sidebar_live_sub_var"):
            self.sidebar_live_sub_var.set("로그 연결 및 보관함 동기화 중…")
        # Workers must never call Tk.after(), widget methods or messageboxes.
        monitor = self.live_monitor
        player = None if self.raid_public_mode else self.current_player
        results = self._live_start_results

        def _worker():
            try:
                monitor.set_player(player)
                results.put((True, monitor.start(start_at_end=True)))
            except Exception as exc:
                results.put((False, str(exc)))
        threading.Thread(target=_worker, name="NiniLiveMonitorStart", daemon=True).start()
        self.after(50, self._complete_live_monitor_start)

    def _complete_live_monitor_start(self):
        if not getattr(self, "_live_start_pending", False):
            return
        try:
            success, payload = self._live_start_results.get_nowait()
        except queue.Empty:
            self.after(50, self._complete_live_monitor_start)
            return
        self._live_start_pending = False
        if getattr(self, "_live_start_cancelled", False):
            # User clicked Stop while starting. Don't launch a polling loop.
            if success:
                self.live_monitor.stop()
            self._live_start_cancelled = False
            if hasattr(self, "live_start_btn"):
                self.live_start_btn.configure(state="normal")
                self.live_stop_btn.configure(state="disabled")
            if hasattr(self, "sidebar_live_start_btn"):
                self.sidebar_live_start_btn.configure(state="normal")
            if hasattr(self, "sidebar_live_stop_btn"):
                self.sidebar_live_stop_btn.configure(state="disabled")
            if hasattr(self, "sidebar_live_sub_var"):
                self.sidebar_live_sub_var.set("다음 ENCOUNTER_START 대기")
            return
        if not success:
            self.live_state_var.set("⚠ 감시 시작 실패")
            if hasattr(self, "sidebar_live_sub_var"):
                self.sidebar_live_sub_var.set("감시 시작을 다시 시도해 주세요")
            if hasattr(self, "live_start_btn"):
                self.live_start_btn.configure(state="normal")
                self.live_stop_btn.configure(state="disabled")
            if hasattr(self, "sidebar_live_start_btn"):
                self.sidebar_live_start_btn.configure(state="normal")
            if hasattr(self, "sidebar_live_stop_btn"):
                self.sidebar_live_stop_btn.configure(state="disabled")
            self._append_live_log(f"[ERROR] 감시 시작 실패: {payload}", "wipe")
            return
        self.live_running = True
        self.live_state_var.set("● 감시 중")
        if hasattr(self, "sidebar_live_sub_var"):
            self.sidebar_live_sub_var.set("다음 ENCOUNTER_START 대기")
        self.notebook.select(self.live_frame)
        for event in payload:
            self._handle_live_event(event)
        self._refresh_live_snapshot()
        self._schedule_live_poll()

    def stop_live_monitor(self):
        if getattr(self, "_live_start_pending", False):
            self._live_start_cancelled = True
            self.live_state_var.set("○ 감시 중지")
            if hasattr(self, "sidebar_live_sub_var"):
                self.sidebar_live_sub_var.set("시작 취소 요청 · 정리 중")
            if hasattr(self, "live_stop_btn"):
                self.live_stop_btn.configure(state="disabled")
            if hasattr(self, "sidebar_live_stop_btn"):
                self.sidebar_live_stop_btn.configure(state="disabled")
            return
        if self.live_poll_after_id is not None:
            try:
                self.after_cancel(self.live_poll_after_id)
            except Exception:
                pass
            self.live_poll_after_id = None
        if self.live_monitor is not None and self.live_running:
            for event in self.live_monitor.stop():
                self._handle_live_event(event)
        self.live_running = False
        if hasattr(self, "live_start_btn"):
            self.live_start_btn.configure(state="normal")
            self.live_stop_btn.configure(state="disabled")
        if hasattr(self, "sidebar_live_start_btn"):
            self.sidebar_live_start_btn.configure(state="normal")
        if hasattr(self, "sidebar_live_stop_btn"):
            self.sidebar_live_stop_btn.configure(state="disabled")
        self.live_state_var.set("○ 감시 중지")
        try:
            self.b3796_live_box.configure(bg=_ui_theme.PANEL_BG, highlightbackground=_ui_theme.PANEL_BORDER)
        except Exception:
            pass
        if hasattr(self, "sidebar_live_sub_var"):
            self.sidebar_live_sub_var.set("다음 ENCOUNTER_START 대기")
        self._refresh_live_snapshot()

    def reconnect_live_monitor(self):
        if self.live_monitor is None:
            messagebox.showerror("Live 모듈 없음", "live/ 모듈을 불러오지 못했습니다.")
            return
        if not self.live_running:
            messagebox.showinfo("Live 복기", "먼저 [감시 시작]을 눌러주세요.")
            return
        for event in self.live_monitor.reconnect_latest(start_at_end=True):
            self._handle_live_event(event)
        self._refresh_live_snapshot()

    def _schedule_live_poll(self):
        if not self.live_running:
            return
        if self.live_poll_after_id is not None:
            try:
                self.after_cancel(self.live_poll_after_id)
            except Exception:
                pass
        self.live_poll_after_id = self.after(350, self._poll_live_monitor)

    def _poll_live_monitor(self):
        self.live_poll_after_id = None
        if not self.live_running or self.live_monitor is None:
            return
        self._b3750_in_live_poll = True
        try:
            events = self.live_monitor.poll()
            for event in events:
                self._handle_live_event(event)
            self._refresh_live_snapshot()
        except Exception as exc:
            self._append_live_log(f"[ERROR] Live 감시 오류: {exc}", "wipe")
            self.live_state_var.set("⚠ 감시 오류")
        finally:
            self._b3750_in_live_poll = False
            self._schedule_live_poll()

    def _handle_live_event(self, event: dict):
        kind = event.get("type")
        if kind == "monitor_start":
            file_path = event.get("file")
            if file_path:
                self._append_live_log(
                    f"[LIVE] {Path(file_path).name} 끝에서 감시 시작 · offset {event.get('offset', 0):,}",
                    "start",
                )
                if event.get("storageRoot"):
                    self._append_live_log(f"[STORE] {event.get('storageRoot')}", "note")
            else:
                self._append_live_log(f"[LIVE] 로그 파일 대기 중 · {self._public_log_dir if self.raid_public_mode else LOGDIR}", "note")
        elif kind == "monitor_stop":
            self._append_live_log("[LIVE] 감시 중지", "note")
        elif kind == "file_switch":
            file_path = event.get("file")
            reason = event.get("reason") or "switch"
            if file_path:
                self._append_live_log(
                    f"[LOG] {Path(file_path).name} 연결 · {reason} · offset {event.get('offset', 0):,}",
                    "start",
                )
            else:
                self._append_live_log(f"[LOG] 로그 파일 없음 · {reason}", "note")
        elif kind == "encounter_start":
            enc = event.get("encounter") or {}
            self._append_live_log(
                f"[START] Try {enc.get('tryNo','?')} · {enc.get('name','?')} · {_ui_difficulty_name(enc, "-")}",
                "start",
            )
        elif kind == "encounter_end":
            enc = event.get("encounter") or {}
            result = enc.get("result") or "종료"
            tag = "kill" if result == "Kill" else "wipe" if result == "Wipe" else "note"
            self._append_live_log(
                f"[{result.upper()}] Try {enc.get('tryNo','?')} · {enc.get('name','?')} · "
                f"{self._fmt_live_duration(enc.get('durationSec'))} · {int(enc.get('eventCount') or 0):,} events",
                tag,
            )
            if enc.get("rawLog"):
                self._append_live_log(
                    f"[SAVE] raw {int(enc.get('rawLineCount') or 0):,}줄 · {enc.get('rawLog')}",
                    "note",
                )
                self._append_live_log(
                    ("[RAID] 트라이 공대 복기 생성 중..." if self.raid_public_mode else f"[FACT] {enc.get('livePlayer') or self.current_player} 트라이 사실 추출 중..."),
                    "note",
                )
            self._refresh_live_recent()
        elif kind == "facts_ready":
            enc = event.get("encounter") or {}
            self._append_live_log(
                ("[RAID] 공대 복기 완료" if self.raid_public_mode else f"[FACT] 완료 · {enc.get('factsPlayer') or '?'} · {Path(enc.get('factsTxt')).name if enc.get('factsTxt') else 'facts.txt'}"),
                "kill",
            )
            if enc.get("raidReviewTxt"):
                self._append_live_log(
                    f"[WIPE] 전멸 원인 복기 완료 · {Path(enc.get('raidReviewTxt')).name}",
                    "kill",
                )
            if enc.get("raidSeriesReviewTxt"):
                self._append_live_log(
                    f"[SERIES] 반복 문제 집계 완료 · {Path(enc.get('raidSeriesReviewTxt')).name}",
                    "kill",
                )
            if enc.get("raidFactsTxt"):
                self._append_live_log(
                    f"[RAID] 공대 전체 사실 완료 · {Path(enc.get('raidFactsTxt')).name}",
                    "kill",
                )
            if enc.get("deathReviewTxt"):
                self._append_live_log(
                    f"[DEATH] 개인 사망 상세 완료 · {Path(enc.get('deathReviewTxt')).name}",
                    "kill",
                )
            for warning in enc.get("raidReviewWarnings") or []:
                self._append_live_log(f"[RULE WARN] {warning}", "wipe")
            for warning in enc.get("raidSeriesWarnings") or []:
                self._append_live_log(f"[SERIES WARN] {warning}", "wipe")
            completed_key = self._live_row_key(enc)
            selected_key = getattr(self, "live_selected_key", None)
            if selected_key is None:
                # Keep the old convenience for an untouched Live view, but never
                # steal selection after the user has moved to another try.
                self.live_selected_key = completed_key
                selected_key = completed_key
            elif self._same_live_try_key(selected_key, completed_key):
                # factsSpecID may have appeared asynchronously; migrate the key.
                self.live_selected_key = completed_key
                selected_key = completed_key
            self._refresh_live_recent()
            if self._same_live_try_key(selected_key, completed_key):
                current = next(
                    (r for r in self.live_recent_rows.values() if self._live_row_key(r) == completed_key),
                    enc,
                )
                self._render_live_facts(current, force=True)
        elif kind == "facts_error":
            enc = event.get("encounter") or {}
            self._append_live_log(f"[FACT ERROR] {event.get('error') or enc.get('factsError') or 'unknown'}", "wipe")
            self._refresh_live_recent()
        elif kind == "encounter_abort":
            enc = event.get("encounter") or {}
            self._append_live_log(
                f"[중단] Try {enc.get('tryNo','?')} · {enc.get('name','?')} · 로그 전환/재연결",
                "note",
            )
            if enc.get("rawLog"):
                self._append_live_log(
                    f"[SAVE] 중단 트라이 raw {int(enc.get('rawLineCount') or 0):,}줄 · {enc.get('rawLog')}",
                    "note",
                )
            self._refresh_live_recent()

    def _refresh_live_snapshot(self):
        if self.live_monitor is None or not hasattr(self, "live_file_var"):
            return
        snap = self.live_monitor.snapshot()
        file_path = snap.get("file")
        if file_path:
            self.live_file_var.set(f"로그: {file_path}")
        else:
            waiting = self._public_log_dir if self.raid_public_mode else LOGDIR
            self.live_file_var.set(f"로그 대기: {waiting or '폴더 선택 필요'}")
        self.live_offset_var.set(
            f"offset {int(snap.get('offset') or 0):,} ({self._fmt_live_bytes(snap.get('offset') or 0)})"
            f"  · 이번 읽기 +{int(snap.get('lastNewLines') or 0):,}줄"
            f"  · 감시 후 누적 {int(snap.get('totalLines') or 0):,}줄"
            f"  · 사실 추출 {int(snap.get('pendingFactJobs') or 0):,}건"
        )
        cur = snap.get("current") or None
        if cur:
            elapsed = self.live_monitor.current_elapsed()
            self.live_current_var.set(
                f"{cur.get('name','?')} · {_ui_difficulty_name(cur, "-")} · Try {cur.get('tryNo','?')} · {self._fmt_live_duration(elapsed)}"
            )
            self.live_current_sub_var.set(
                f"EncounterID {cur.get('encounterID','-')}  ·  공대 {cur.get('groupSize','-')}명"
                f"  ·  현재 {int(cur.get('eventCount') or 0):,} events"
            )
            if hasattr(self, "sidebar_live_sub_var"):
                self.sidebar_live_sub_var.set(
                    f"Try {cur.get('tryNo','?')} · {cur.get('name','?')} · {self._fmt_live_duration(elapsed)}"
                )
        else:
            self.live_current_var.set("다음 ENCOUNTER_START 대기")
            self.live_current_sub_var.set(
                "전투 중에 감시를 시작했다면 현재 전투는 건너뛰고 다음 트라이부터 잡습니다."
            )
            if hasattr(self, "sidebar_live_sub_var"):
                pending = int(snap.get("pendingFactJobs") or 0)
                self.sidebar_live_sub_var.set(
                    f"복기 생성 중 · {pending}건" if pending else "다음 ENCOUNTER_START 대기"
                )
        self._update_live_progress(snap)
        self._refresh_live_recent()

    @staticmethod
    def _stored_row_identity(row):
        row = row or {}
        storage = str(row.get("storageDir") or "").strip()
        if storage:
            return ("storage", storage)
        return (
            "try", row.get("encounterID"), row.get("difficultyID"),
            row.get("tryNo"), row.get("startTimestamp"),
            row.get("factsPlayer") or row.get("livePlayer"),
        )

    def _row_visible_for_current_player(self, row):
        if self.raid_public_mode:
            return True
        owner = str((row or {}).get("factsPlayer") or (row or {}).get("livePlayer") or "").strip()
        return not owner or owner == str(self.current_player or "").strip()

    @staticmethod
    def _live_row_sort_key(row):
        dt = parse_combat_datetime(str((row or {}).get("startTimestamp") or ""))
        if dt is not None:
            return (dt.timestamp(), int((row or {}).get("tryNo") or 0))
        return (0.0, int((row or {}).get("tryNo") or 0))

    def _all_live_rows(self):
        rows = [
            r for r in getattr(self, "live_session_rows", [])
            if self._row_visible_for_current_player(r) and _is_raid_navigation_row(r)
        ]
        # B37.4.7: one physical pull can be hydrated from both Historical and
        # local/Live storage (and legacy slug aliases).  The sidebar/session UI
        # must show the pull once, not once per stored representation.
        rows = canonicalize_rows(rows)
        rows.sort(key=self._live_row_sort_key)
        return rows

    def _find_live_row_by_key(self, wanted):
        if wanted is None:
            return None
        for row in self._all_live_rows():
            if self._same_live_try_key(self._live_row_key(row), wanted):
                return row
        return None

    def _find_live_tree_iid_by_key(self, wanted):
        if wanted is None:
            return None
        for iid, row in getattr(self, "live_recent_rows", {}).items():
            if self._same_live_try_key(self._live_row_key(row), wanted):
                return iid
        return None

    def _start_saved_identity_audit(self, rows=None):
        """Run legacy raw-log player migration off the Tk UI thread.

        Startup first hydrates meta.json only, so the window and navigator render
        immediately.  Only sessions that do not yet have the v1 identity marker
        are rescanned in this daemon worker.  The worker never touches Tk; it
        posts the fully re-hydrated rows back through ``msgq``.
        """
        if self.live_monitor is None or self._identity_audit_started:
            return
        if self.raid_public_mode:
            # Public raid mode has no character identity to migrate.
            self._identity_audit_started = True
            return
        rows = list(rows if rows is not None else getattr(self, "live_persisted_rows", []) or [])
        pending = [r for r in rows if str((r or {}).get("playerIdentitySource") or "") != IDENTITY_SOURCE]
        if not pending:
            self._identity_audit_started = True
            return

        self._identity_audit_started = True
        self._identity_audit_running = True
        pending_dirs = {str(r.get("storageDir") or "") for r in pending}
        if hasattr(self, "status_var"):
            self.status_var.set(f"저장 기록 확인 중 · {len(pending)}개 · 화면은 계속 사용할 수 있습니다")

        monitor = self.live_monitor

        def worker():
            started = time.perf_counter()
            try:
                audited = list(monitor.saved_rows(migrate_identity=True))
                corrected = sum(
                    1 for r in audited
                    if str(r.get("storageDir") or "") in pending_dirs
                    and bool(r.get("playerIdentityCorrected"))
                )
                self.msgq.put(("identity_audit_done", {
                    "rows": audited,
                    "checked": len(pending_dirs),
                    "corrected": corrected,
                    "elapsedSec": time.perf_counter() - started,
                }))
            except Exception as exc:
                self.msgq.put(("identity_audit_error", {
                    "error": str(exc),
                    "elapsedSec": time.perf_counter() - started,
                }))

        threading.Thread(
            target=worker,
            name="nini-saved-identity-audit",
            daemon=True,
        ).start()

    def _start_saved_reanalysis(self, rows=None):
        """Upgrade stale persisted reviews without blocking Tk.

        Identity migration owns the first pass because personal facts must be
        rebuilt for the actual logged character.  Once that worker is finished,
        legacy B2/B3 raid reviews and stale-player personal reviews are processed
        sequentially in a daemon thread using the same pipeline as a new Live pull.
        """
        if self.live_monitor is None or self._saved_reanalysis_started:
            return
        if getattr(self, "_identity_audit_running", False):
            return
        if getattr(self, "_feign_recheck_running", False):
            return  # RC4.2: never rebuild the same Try folders concurrently
        rows = list(rows if rows is not None else getattr(self, "live_persisted_rows", []) or [])
        pending = [r for r in rows if bool((r or {}).get("reanalysisRequired")) and (
            not self.raid_public_mode or bool(
                set((r or {}).get("reanalysisReasons") or []) - {"stale-player"}
            ) or not (r or {}).get("reanalysisReasons")
        )]
        if not pending:
            self._saved_reanalysis_started = True
            return

        self._saved_reanalysis_started = True
        self._saved_reanalysis_running = True
        self._saved_reanalysis_total = len(pending)
        self._saved_reanalysis_done = 0
        if hasattr(self, "status_var"):
            self.status_var.set(
                f"저장 복기 최신화 · {len(pending)}개 · 백그라운드 처리 중"
            )
        if not self._main_task_running() and getattr(self, "_progress_owner", None) in (None, "saved_reanalysis"):
            self._progress_begin("저장 복기 최신화", "saved_reanalysis")
            self._progress_set(0.0, f"재분석 0/{len(pending)}", owner="saved_reanalysis")

        monitor = self.live_monitor
        storage_root = Path(monitor.storage_root)

        def worker():
            from live.reanalysis import rebuild_saved_row
            started = time.perf_counter()
            errors = []
            rebuilt = 0
            remaining = list(pending)
            while remaining:
                # B37.3.9.13: if the user opens a stale saved Try while the
                # background upgrade is running, make that Try the next job.
                # This avoids showing an old BossMechanic result for minutes
                # merely because hundreds of older rows were queued first.
                priority_key = getattr(self, "_saved_reanalysis_priority_key", None)
                chosen_idx = 0
                if priority_key is not None:
                    for pos, candidate in enumerate(remaining):
                        if self._same_live_try_key(self._live_row_key(candidate), priority_key):
                            chosen_idx = pos
                            break
                row = remaining.pop(chosen_idx)
                if priority_key is not None and self._same_live_try_key(self._live_row_key(row), priority_key):
                    self._saved_reanalysis_priority_key = None
                index = rebuilt + len(errors) + 1
                label = f"{row.get('name','?')} Try {row.get('tryNo','?')}"
                self.msgq.put(("saved_reanalysis_progress", {
                    "index": index, "total": len(pending), "label": label,
                    "phase": "start", "storageDir": row.get("storageDir"),
                }))
                try:
                    fresh_row = (rebuild_saved_row(row, storage_root, raid_only=True) if self.raid_public_mode else rebuild_saved_row(row, storage_root))
                    rebuilt += 1
                    self.msgq.put(("saved_reanalysis_progress", {
                        "index": index, "total": len(pending), "label": label,
                        "phase": "done", "storageDir": row.get("storageDir"),
                        "row": fresh_row,
                    }))
                except Exception as exc:
                    errors.append({"storageDir": row.get("storageDir"), "error": str(exc)})
                    self.msgq.put(("saved_reanalysis_progress", {
                        "index": index, "total": len(pending), "label": label,
                        "phase": "error", "error": str(exc), "storageDir": row.get("storageDir"),
                    }))
            try:
                fresh_rows = list(monitor.saved_rows(migrate_identity=False))
            except Exception:
                fresh_rows = []

            # B34.2.4: all disk-heavy display hydration stays in this worker.
            # Completion should only hand prepared dictionaries to Tk.
            prepared_reviews = {}
            prepared_series = {}
            player_snapshot = str(self.current_player or "")
            for fresh in fresh_rows:
                cache_key = self._prepared_row_cache_key(fresh)
                if not cache_key:
                    continue
                try:
                    prepared_reviews[cache_key] = self._build_review_model_uncached(
                        fresh, player=(None if self.raid_public_mode else ((fresh or {}).get("factsPlayer") or player_snapshot))
                    )
                except Exception:
                    prepared_reviews[cache_key] = {}
                try:
                    prepared_series[cache_key] = self._build_series_model_uncached(fresh)
                except Exception:
                    prepared_series[cache_key] = {}
            self.msgq.put(("saved_reanalysis_done", {
                "rows": fresh_rows,
                "preparedReviews": prepared_reviews,
                "preparedSeries": prepared_series,
                "rebuilt": rebuilt,
                "total": len(pending),
                "errors": errors,
                "elapsedSec": time.perf_counter() - started,
            }))

        threading.Thread(
            target=worker,
            name="nini-saved-review-upgrade",
            daemon=True,
        ).start()

    def _refresh_live_recent(self, force_storage_reload=False):
        if self.live_monitor is None or not hasattr(self, "live_recent_tree"):
            return
        if force_storage_reload and not getattr(self, "_saved_reanalysis_running", False):
            # Tests/manual root changes can point the app at a different persisted
            # store after construction; allow one freshness pass for that store.
            self._saved_reanalysis_started = False
        snap = self.live_monitor.snapshot()
        recent_rows = list(snap.get("recent") or [])
        monitor_revision = getattr(self.live_monitor, "storage_revision", None)
        if (force_storage_reload
                or not getattr(self, "_live_storage_loaded", False)
                or monitor_revision != getattr(self, "_live_storage_revision", None)):
            # A new/changed stored pull invalidates any pre-hydrated display model.
            # The saved-reanalysis completion path sets the revision before calling
            # this method, so its freshly prepared cache survives that handoff.
            self._prepared_review_models = {}
            self._prepared_series_models = {}
            try:
                # B34.1.2: meta-only hydration must stay cheap.  Legacy raw-log
                # identity correction is scheduled below on a background thread.
                self.live_persisted_rows = list(self.live_monitor.saved_rows(migrate_identity=False))
            except Exception:
                self.live_persisted_rows = []
            self._live_storage_loaded = True
            self._live_storage_revision = monitor_revision
        persisted_rows = list(getattr(self, "live_persisted_rows", []) or [])
        if not getattr(self, '_public_migration_running', False):
            self._start_saved_identity_audit(persisted_rows)
            self._start_saved_reanalysis(persisted_rows)

        # Persisted storage is canonical; current-process rows overlay it so async
        # analysis status/spec enrichment appears immediately without losing old
        # sessions after an app restart.
        merged = {}
        fallback_order = []
        for row in persisted_rows + recent_rows:
            ident = self._stored_row_identity(row)
            if ident not in merged:
                merged[ident] = dict(row)
                fallback_order.append(ident)
            else:
                combined = dict(merged[ident])
                combined.update(row)
                merged[ident] = combined
        self.live_session_rows = [merged[k] for k in fallback_order]

        # B37.5.0.1: the live monitor calls this every 350 ms.  When nothing the
        # sidebar/session/Try views are built from has changed, skip the whole
        # rebuild.  Re-hydrating every stored review per poll kept Tk busy
        # (~0.4 s per poll), which made the mouse wheel feel dead and redrew the
        # open Try timeline under the pointer (tooltip flicker).
        refresh_sig = self._b3750_live_refresh_signature()
        if (getattr(self, "_b3750_in_live_poll", False) and not force_storage_reload
                and refresh_sig is not None and refresh_sig == getattr(self, "_b3750_live_refresh_sig", None)):
            return
        self._b3750_live_refresh_sig = refresh_sig

        selected_key = getattr(self, "live_selected_key", None)
        self.live_recent_rows = {}
        for item in self.live_recent_tree.get_children():
            self.live_recent_tree.delete(item)

        # Legacy list remains a small recent projection.  Navigation and session
        # summaries use all hydrated rows, not this 30-row widget.
        visible = self._all_live_rows()
        recent_projection = list(reversed(visible[-30:]))
        selected_iid = None
        for idx, row in enumerate(recent_projection):
            is_reset = self._live_row_is_reset(row)
            is_early = self._live_row_is_early_wipe(row)
            result = _ui_result_en(row.get("result"), reset=is_reset, early=is_early)
            iid = f"live-{idx}"
            self.live_recent_rows[iid] = row
            self.live_recent_tree.insert(
                "", "end", iid=iid,
                values=(
                    row.get("tryNo", "-"), row.get("name", "-"),
                    _ui_difficulty_name(row, "-"), result,
                    self._fmt_live_duration(row.get("durationSec")),
                    f"{int(row.get('eventCount') or 0):,}",
                ),
            )
            row_key = self._live_row_key(row)
            if selected_key is not None and self._same_live_try_key(row_key, selected_key):
                selected_iid = iid

        selected_row = self._find_live_row_by_key(selected_key) if selected_key is not None else None
        if selected_row is not None:
            migrated_key = self._live_row_key(selected_row)
            self.live_selected_key = migrated_key
            if (self.nav_state.get("page") == "live_try"
                    and self._same_live_try_key(self.nav_state.get("liveTryKey"), migrated_key)):
                self._set_nav_context(self._live_context_key(selected_row), page="live_try", live_try_key=migrated_key)
            if (selected_iid and self.nav_state.get("page") == "live_try"
                    and self._same_live_try_key(self.nav_state.get("liveTryKey"), migrated_key)):
                self._live_recent_syncing = True
                try:
                    self.live_recent_tree.selection_set(selected_iid)
                    self.live_recent_tree.focus(selected_iid)
                except Exception:
                    pass
                finally:
                    # Keep the guard through Tk's queued virtual-select event.
                    try:
                        self.after_idle(lambda: setattr(self, "_live_recent_syncing", False))
                    except Exception:
                        self._live_recent_syncing = False
            if (self.nav_state.get("page") == "live_try"
                    and self._same_live_try_key(self.nav_state.get("liveTryKey"), migrated_key)):
                # B37.5.0.1: the live monitor polls every 350 ms.  Re-render the
                # open Try only when its data actually changed; a full rebuild
                # on every poll destroyed hover tooltips (flicker) and fought
                # the mouse wheel on the Try page.
                if (not getattr(self, "_b3750_in_live_poll", False)
                        or self._b3750_live_render_signature(selected_row) != getattr(self, "_b3750_rendered_live_sig", None)):
                    self._update_live_selection(selected_row)
        elif selected_key is not None:
            self.live_selected_key = None
            self._update_live_selection(None)

        self._refresh_shared_nav_tree(visible)
        self._refresh_layout_session_summary()
        self._refresh_boss_try_progress()
        self._refresh_b3641_boss_views()
        self._update_breadcrumb()
        # RC4: PUBLIC first screen opens the newest raid session instead of an
        # empty "0 Try" page.  Runs once; never overrides a user's navigation.
        self._public_auto_open_latest_session(visible)

    def _prioritize_saved_reanalysis_row(self, row):
        """Make an opened stale Try the next background reanalysis job."""
        if not row or not getattr(self, "_saved_reanalysis_running", False):
            return False
        if not bool(row.get("mechanicReviewStale") or row.get("reanalysisRequired")):
            return False
        self._saved_reanalysis_priority_key = self._live_row_key(row)
        if hasattr(self, "status_var"):
            self.status_var.set(
                f"선택 트라이 우선 최신화 대기 · {row.get('name','?')} Try {row.get('tryNo','?')}"
            )
        return True

    def _selected_live_row(self):
        # B37.3.9.14: shared selection is the sole owner of the active Try.
        # Never fall back to the legacy recent Treeview while a boss/session is
        # selected; that stale fallback was what snapped the UI back to Try 2.
        if self.nav_state.get("page") != "live_try":
            return None
        wanted = self.nav_state.get("liveTryKey")
        return self._find_live_row_by_key(wanted)

    @staticmethod
    def _live_row_is_reset(row):
        if not row or str(row.get("analysisOverride") or "").lower() == "normal":
            return False
        # B37.3.5: legacy rows without death/engagement evidence are conservative:
        # only obvious sub-second artifacts are auto-hidden.
        if bool(row.get("resetPull") or row.get("analysisExcluded") == "reset_pull"):
            return True
        try:
            return bool(is_reset_pull(row))
        except Exception:
            return False

    @staticmethod
    def _live_row_is_early_wipe(row):
        if not row:
            return False
        if str(row.get("result") or "").upper() == "KILL":
            return False
        if App._live_row_is_reset(row) or App._live_row_is_fake(row):
            return False
        if bool(row.get("earlyWipe")):
            return True
        try:
            return bool(is_early_wipe(row))
        except Exception:
            return False

    @staticmethod
    def _live_row_is_fake(row):
        if not row or str(row.get("analysisOverride") or "").lower() == "normal":
            return False
        if str(row.get("analysisExcluded") or "").lower() == "fake_pull":
            return True
        try:
            duration = row.get("durationSec")
            if duration is None:
                return False
            value = float(duration)
            return str(row.get("result") or "").upper() != "KILL" and 0.0 <= value < 1.0
        except Exception:
            return False

    @staticmethod
    def _live_start_clock(row):
        dt = parse_combat_datetime(str((row or {}).get("startTimestamp") or ""))
        return dt.strftime("%H:%M") if dt is not None else "--:--"

    @staticmethod
    def _pull_spell_display(row):
        row = row or {}
        name = str(row.get("spellName") or "?")
        if row.get("isTaunt") and "(도발)" not in name:
            return f"{name} (도발)"
        return name

    def _pull_badge_text(self, row):
        """RC4: show who started the pull on every Try (not only reset pulls).

        Only a *confirmed* pre-ENCOUNTER_START attacker is named.  The pull
        countdown is not in the combat log, so this never claims "early pull";
        it states who attacked first and how long before the encounter began.
        """
        try:
            pull = (row or {}).get("pullStart") or {}
            confirmed = bool(pull.get("pullerConfirmed"))
            who_row = (pull.get("puller") if confirmed else None) or pull.get("firstObservedAttack") or {}
            if not who_row:
                return ""
            if not confirmed and pull.get("ambiguity") == "near-simultaneous-prestart-attacks":
                return " · 풀러 판단 보류(동시 공격)"
            who = str(who_row.get("playerName") or "?").split("-")[0]
            if who_row.get("actorType") == "pet":
                who += " [펫]"
            sec = float(who_row.get("sec") or 0.0)
            when = f"{abs(sec):.1f}초 전" if sec < 0 else f"+{sec:.1f}초"
            label = "풀러" if confirmed else "첫 공격"
            return f" · {label} {who} ({when} · {self._pull_spell_display(who_row)})"
        except Exception:
            return ""

    def _pull_line_parts(self, row):
        """RC4.3: (prefix, name, suffix, colour) for the first-attacker line.

        Only a named attacker gets a colour (WoW class colour from the Try's
        roster; pets use their owner's class).  Everything else is plain text.
        """
        text = self._pull_line_text(row)
        try:
            pull = (row or {}).get("pullStart") or {}
            confirmed = bool(pull.get("pullerConfirmed"))
            who_row = (pull.get("puller") if confirmed else None) or pull.get("firstObservedAttack") or {}
            if not who_row or (not confirmed and pull.get("ambiguity") == "near-simultaneous-prestart-attacks"):
                return text, "", "", None
            name = str(who_row.get("playerName") or "?").split("-")[0]
            if who_row.get("actorType") == "pet":
                name += " [펫]"
            label = "풀러" if confirmed else "첫 공격"
            prefix = f"{label} "
            if not text.startswith(prefix + name):
                return text, "", "", None
            from live.class_colors import color_for_player
            colour = color_for_player(row, who_row.get("playerGUID") or who_row.get("sourceGUID"))
            return prefix.rstrip(), name, text[len(prefix + name):], colour
        except Exception:
            return text, "", "", None

    def _render_pull_line(self, row):
        if not hasattr(self, "live_pull_name_lbl"):
            return
        prefix, name, suffix, colour = self._pull_line_parts(row) if row else ("", "", "", None)
        self.live_pull_prefix_var.set(prefix)
        self.live_pull_name_var.set(name)
        self.live_pull_suffix_var.set(suffix)
        try:
            self.live_pull_name_lbl.configure(foreground=colour or "")
        except tk.TclError:
            pass

    def _pull_line_text(self, row):
        """RC4.1: text for the dedicated first-attacker line (never needs the review model)."""
        if not row:
            return ""
        text = self._pull_badge_text(row).lstrip(" ·").strip()
        if text:
            return text
        if row.get("pullStart") is None:
            status = str(row.get("factsStatus") or row.get("raidFactsStatus") or "")
            if status in ("analyzing", "pending", "queued", ""):
                return "첫 공격자 · 로그 분석 후 표시"
            if status == "ready":
                return "첫 공격자 · 기존 기록 — 재분석 후 표시"
        return "첫 공격자 · 로그에서 확인되지 않음"

    def _b3750_live_refresh_signature(self):
        try:
            rows = json.dumps(getattr(self, "live_session_rows", []) or [], sort_keys=True, default=str, ensure_ascii=False)
            nav = json.dumps(getattr(self, "nav_state", {}) or {}, sort_keys=True, default=str, ensure_ascii=False)
        except Exception:
            return None
        prepared = getattr(self, "_prepared_review_models", None)
        series = getattr(self, "_prepared_series_models", None)
        return (
            hash(rows), hash(nav), repr(getattr(self, "live_selected_key", None)),
            str(getattr(self, "current_player", "") or ""),
            getattr(self, "_live_storage_revision", None),
            id(prepared), len(prepared or ()), id(series), len(series or ()),
            bool(getattr(self, "_saved_reanalysis_running", False)),
        )

    def _b3750_live_render_signature(self, row):
        """Cheap fingerprint of everything the open Try page is drawn from."""
        if not row:
            return None
        try:
            body = json.dumps(row, sort_keys=True, default=str, ensure_ascii=False)
        except Exception:
            body = repr(sorted((str(k), str(v)) for k, v in dict(row).items()))
        stamps = []
        for field in ("raidReviewJson", "raidSeriesReviewJson", "raidFactsJson", "metaJson"):
            raw = row.get(field)
            if not raw:
                continue
            try:
                st = Path(str(raw)).stat()
                stamps.append((field, st.st_mtime_ns, st.st_size))
            except Exception:
                stamps.append((field, None, None))
        try:
            prepared = getattr(self, "_prepared_review_models", {}).get(self._prepared_row_cache_key(row))
        except Exception:
            prepared = None
        return (
            hash(body), tuple(stamps), id(prepared) if prepared is not None else None,
            getattr(self, "_live_storage_revision", None), str(getattr(self, "current_player", "") or ""),
        )

    def _pull_badge_text(self, row):
        """RC4: show who started the pull on every Try (not only reset pulls).

        Only a *confirmed* pre-ENCOUNTER_START attacker is named.  The pull
        countdown is not in the combat log, so this never claims "early pull";
        it states who attacked first and how long before the encounter began.
        """
        try:
            pull = (row or {}).get("pullStart") or {}
            confirmed = bool(pull.get("pullerConfirmed"))
            who_row = (pull.get("puller") if confirmed else None) or pull.get("firstObservedAttack") or {}
            if not who_row:
                return ""
            if not confirmed and pull.get("ambiguity") == "near-simultaneous-prestart-attacks":
                return " · 풀러 판단 보류(동시 공격)"
            who = str(who_row.get("playerName") or "?").split("-")[0]
            if who_row.get("actorType") == "pet":
                who += " [펫]"
            sec = float(who_row.get("sec") or 0.0)
            when = f"{abs(sec):.1f}초 전" if sec < 0 else f"+{sec:.1f}초"
            label = "풀러" if confirmed else "첫 공격"
            return f" · {label} {who} ({when} · {self._pull_spell_display(who_row)})"
        except Exception:
            return ""

    def _pull_line_parts(self, row):
        """RC4.3: (prefix, name, suffix, colour) for the first-attacker line.

        Only a named attacker gets a colour (WoW class colour from the Try's
        roster; pets use their owner's class).  Everything else is plain text.
        """
        text = self._pull_line_text(row)
        try:
            pull = (row or {}).get("pullStart") or {}
            confirmed = bool(pull.get("pullerConfirmed"))
            who_row = (pull.get("puller") if confirmed else None) or pull.get("firstObservedAttack") or {}
            if not who_row or (not confirmed and pull.get("ambiguity") == "near-simultaneous-prestart-attacks"):
                return text, "", "", None
            name = str(who_row.get("playerName") or "?").split("-")[0]
            if who_row.get("actorType") == "pet":
                name += " [펫]"
            label = "풀러" if confirmed else "첫 공격"
            prefix = f"{label} "
            if not text.startswith(prefix + name):
                return text, "", "", None
            from live.class_colors import color_for_player
            colour = color_for_player(row, who_row.get("playerGUID") or who_row.get("sourceGUID"))
            return prefix.rstrip(), name, text[len(prefix + name):], colour
        except Exception:
            return text, "", "", None

    def _render_pull_line(self, row):
        if not hasattr(self, "live_pull_name_lbl"):
            return
        prefix, name, suffix, colour = self._pull_line_parts(row) if row else ("", "", "", None)
        self.live_pull_prefix_var.set(prefix)
        self.live_pull_name_var.set(name)
        self.live_pull_suffix_var.set(suffix)
        try:
            self.live_pull_name_lbl.configure(foreground=colour or "")
        except tk.TclError:
            pass

    def _pull_line_text(self, row):
        """RC4.1: text for the dedicated first-attacker line (never needs the review model)."""
        if not row:
            return ""
        text = self._pull_badge_text(row).lstrip(" ·").strip()
        if text:
            return text
        if row.get("pullStart") is None:
            status = str(row.get("factsStatus") or row.get("raidFactsStatus") or "")
            if status in ("analyzing", "pending", "queued", ""):
                return "첫 공격자 · 로그 분석 후 표시"
            if status == "ready":
                return "첫 공격자 · 기존 기록 — 재분석 후 표시"
        return "첫 공격자 · 로그에서 확인되지 않음"

    def _update_live_selection(self, row):
        try:
            self._b3750_rendered_live_sig = self._b3750_live_render_signature(row)
        except Exception:
            self._b3750_rendered_live_sig = None
        raw = Path(row.get("rawLog")) if row and row.get("rawLog") else None
        folder = Path(row.get("storageDir")) if row and row.get("storageDir") else None
        if raw is not None:
            from live.rawlog import resolve_raw
            raw = resolve_raw(raw)
        raw_ok = bool(raw and raw.exists())
        folder_ok = bool(folder and folder.exists())
        facts = Path(row.get("factsTxt")) if row and row.get("factsTxt") else None
        facts_ok = bool(facts and facts.exists())
        death_review = Path(row.get("deathReviewTxt")) if row and row.get("deathReviewTxt") else None
        death_ok = bool(death_review and death_review.exists())
        raid_facts = Path(row.get("raidFactsTxt")) if row and row.get("raidFactsTxt") else None
        raid_review = Path(row.get("raidReviewTxt")) if row and row.get("raidReviewTxt") else None
        raid_ok = bool((raid_review and raid_review.exists()) or (raid_facts and raid_facts.exists()))
        if hasattr(self, "live_open_raw_btn"):
            self.live_open_raw_btn.configure(state="normal" if raw_ok else "disabled")
        if hasattr(self, "live_open_folder_btn"):
            self.live_open_folder_btn.configure(state="normal" if folder_ok else "disabled")
        if hasattr(self, "live_open_facts_btn"):
            self.live_open_facts_btn.configure(state="normal" if facts_ok else "disabled")
        if hasattr(self, "live_open_death_btn"):
            self.live_open_death_btn.configure(state="normal" if death_ok else "disabled")
        if hasattr(self, "live_open_raid_btn"):
            self.live_open_raid_btn.configure(state="normal" if raid_ok else "disabled")
        is_fake = self._live_row_is_fake(row)
        is_reset = self._live_row_is_reset(row)
        is_early = self._live_row_is_early_wipe(row)
        if hasattr(self, "live_reset_normal_btn"):
            self.live_reset_normal_btn.configure(state="normal" if is_reset else "disabled")
        if hasattr(self, "live_detail_reset_btn"):
            self.live_detail_reset_btn.configure(state="normal" if is_reset else "disabled")
        if hasattr(self, "live_detail_folder_btn"):
            self.live_detail_folder_btn.configure(state="normal" if folder_ok else "disabled")
        self._render_b3641_try_priority(row)
        if not hasattr(self, "live_selected_var"):
            return
        if not row:
            self.live_selected_var.set("트라이를 선택하면 저장된 raw.log를 바로 열 수 있습니다.")
            if hasattr(self, "live_detail_title_var"):
                self.live_detail_title_var.set("트라이를 선택하세요")
            if hasattr(self, "live_detail_badge_var"):
                self.live_detail_badge_var.set("왼쪽 세션 탐색에서 Try를 누르면 전멸 복기를 엽니다.")
            if hasattr(self, "live_pull_var"):
                self.live_pull_var.set("")
                self._render_pull_line(None)
            return
        saved = f"저장 {int(row.get('rawLineCount') or 0):,}줄" if row.get("rawLog") else "저장 정보 없음"
        path_text = str(raw) if raw else "-"
        fact_status = row.get("factsStatus") or "-"
        fact_label = "사실완료" if fact_status == "ready" else "사실분석중" if fact_status == "analyzing" else fact_status
        death_label = " · 개인사망완료" if row.get("deathReviewStatus") == "ready" else ""
        raid_label = " · 전멸복기완료" if row.get("raidReviewStatus") == "ready" else (" · 공대사실완료" if row.get("raidFactsStatus") == "ready" else "")
        display_result = _ui_result_en(row.get("result"), fake=is_fake, reset=is_reset, early=is_early)
        reset_note = " · 기본 집계 제외" if (is_reset or is_fake) else (" · 집계 유지" if is_early else "")
        self.live_selected_var.set(
            f"Try {row.get('tryNo','-')} · {row.get('name','-')} · {display_result}{reset_note} · {saved} · {fact_label}{raid_label}{death_label} · {path_text}"
        )
        if hasattr(self, "live_detail_title_var"):
            self.live_detail_title_var.set(
                f"Try {row.get('tryNo','?')} · {display_result} {self._fmt_live_duration(row.get('durationSec'))}"
            )
        if hasattr(self, "live_pull_var"):
            self.live_pull_var.set(self._pull_line_text(row))
            self._render_pull_line(row)
        if hasattr(self, "live_detail_badge_var"):
            if is_fake or is_reset:
                kind = "가짜 풀" if is_fake else "리셋"
                self.live_detail_badge_var.set(
                    f"{kind} · 사망 {int(row.get('deathCount') or 0)}명 · 기본 집계 제외"
                )
            elif is_early:
                self.live_detail_badge_var.set(
                    f"Reset · 사망 {int(row.get('deathCount') or 0)}명 · 집계 유지"
                )
            else:
                if row.get("raidReviewStale"):
                    old_engine = row.get("storedReviewEngineVersion") or "구버전"
                    self.live_detail_badge_var.set(
                        f"구버전 복기({old_engine}) · v0.4-B31 재분석 대기/진행 중"
                    )
                else:
                    model = self._review_model_for_row(row)
                    if model:
                        root = model.get("rootMechanic") or "원인 판정 없음"
                        root_at = (
                            f" · {self._fmt_live_relative(model.get('rootTime'))}"
                            if model.get("rootTime") is not None else ""
                        )
                        confidence = str(model.get("confidence") or "-").upper()
                        # B37.3.9.2: the B31 cause lives in the 붕괴 직결 cell, not the header.
                        self.live_detail_badge_var.set(
                            f"사망 {int(model.get('deathCount') or 0)}명"
                        )
                    else:
                        self.live_detail_badge_var.set(
                            f"{fact_label}{raid_label}{death_label} · 전멸 복기 결과 대기"
                        )

    def _mark_live_recent_user_click(self, _event=None):
        self._live_recent_user_click = True

    def _on_live_recent_selected(self, _event=None):
        # B37.3.9.14: TreeviewSelect is also emitted by selection_set() during
        # live refresh.  Only a real mouse click may use the legacy recent list
        # to enter a Try from another shared-navigation page.
        if getattr(self, "_live_recent_syncing", False):
            return
        if self.nav_state.get("page") != "live_try" and not getattr(self, "_live_recent_user_click", False):
            return
        try:
            sel = self.live_recent_tree.selection() if hasattr(self, "live_recent_tree") else ()
            row = getattr(self, "live_recent_rows", {}).get(sel[0]) if sel else None
            if not row:
                return
            key = self._live_row_key(row)
            if self.nav_state.get("page") == "live_try" and self._same_live_try_key(key, self.nav_state.get("liveTryKey")):
                return
            self.live_selected_key = key
            self._set_nav_context(self._live_context_key(row), page="live_try", live_try_key=key)
            self._update_breadcrumb()
            self._update_live_selection(row)
            self._render_live_facts(row)
        finally:
            self._live_recent_user_click = False

    def _live_facts_render_token_for(self, row):
        if not row:
            return (None,)
        paths = []
        for key in ("mechanicReviewJson", "mechanicReviewTxt", "raidReviewJson", "raidReviewTxt", "raidSeriesReviewTxt", "raidFactsTxt", "deathReviewTxt", "factsTxt"):
            value = row.get(key)
            if not value:
                paths.append((key, None, None))
                continue
            path = Path(value)
            try:
                stamp = path.stat().st_mtime_ns if path.exists() else None
            except Exception:
                stamp = None
            paths.append((key, str(path), stamp))
        return (
            self._live_row_key(row),
            row.get("factsStatus"), row.get("raidFactsStatus"), row.get("raidReviewStatus"), row.get("raidSeriesReviewStatus"), row.get("deathReviewStatus"),
            row.get("factsError"), row.get("analysisExcluded"), row.get("resetPull"),
            row.get("analysisOverride"), row.get("analysisOverrideAt"),
            row.get("raidReviewStale"), row.get("personalReviewStale"), row.get("reanalysisRequired"),
            row.get("storedReviewEngineVersion"), tuple(paths),
        )

    def _view_reset_as_normal(self):
        row = self._selected_live_row()
        if not row or not self._live_row_is_reset(row):
            return
        if self.live_monitor is None:
            messagebox.showerror("정상 트라이 포함", "Live 모니터가 없어 저장 상태를 바꿀 수 없습니다.")
            return
        try:
            promoted = self.live_monitor.promote_reset_to_normal(row)
        except Exception as exc:
            messagebox.showerror("정상 트라이 포함 실패", str(exc))
            return
        row.update(promoted)
        self.status_var.set(f"Try {row.get('tryNo','?')} · 정상 트라이로 포함 · 전체 복기 재분석 중")
        self._update_live_selection(row)
        self._render_live_facts(row, force=True)
        self._refresh_shared_nav_tree()

    def _render_live_facts(self, row, force=False):
        if not hasattr(self, "live_facts"):
            return
        token = self._live_facts_render_token_for(row)
        # The recent-pull tree is refreshed every 350ms.  Re-selecting the same row
        # used to fire <<TreeviewSelect>>, rebuild this Text widget and force it back
        # to line 1, making mouse-wheel scrolling impossible.  Do not redraw unless
        # the selected pull or one of its generated review files actually changed.
        if not force and token == getattr(self, "_live_facts_render_token", None):
            return
        self._live_facts_render_token = token
        self.live_facts.delete("1.0", "end")
        if not row:
            self.live_facts.insert("end", "트라이를 선택하면 사실 추출 결과를 표시합니다.\n", "note")
            return
        status = row.get("factsStatus")
        if status == "stale-player":
            old = row.get("factsPlayerOriginal") or "이전 캐릭터"
            now = row.get("factsPlayer") or self.current_player
            self.live_facts.insert(
                "end",
                f"캐릭터 소속 보정: {old} → {now} · 기존 개인 분석은 이전 캐릭터 기준이라 재분석이 필요합니다.\n\n",
                "note",
            )
        if status == "analyzing":
            self.live_facts.insert(
                "end",
                f"{row.get('name','?')} Try {row.get('tryNo','?')} · {row.get('factsPlayer') or self.current_player}\n\n사실 추출 중...\n",
                "note",
            )
            return
        if status == "error":
            self.live_facts.insert("end", f"사실 추출 오류: {row.get('factsError') or 'unknown'}\n", "note")
            return
        is_fake = self._live_row_is_fake(row)
        is_reset = self._live_row_is_reset(row)
        if is_fake or is_reset:
            pull = row.get("pullStart") or {}
            first = pull.get("firstObservedAttack") or {}
            self.live_facts.insert("end", ("가짜 풀 · 기본 집계 제외\n" if is_fake else "리셋 트라이 · 기본 집계 제외\n"), "title")
            self.live_facts.insert(
                "end",
                f"Try {row.get('tryNo','?')} · {row.get('name','?')} · {self._fmt_live_duration(row.get('durationSec'))}\n"
                f"사망 {int(row.get('deathCount') or 0)}명\n",
            )
            pre_sec = float(pull.get("prerollSec") or row.get("prerollSec") or 0.0)
            pre_window = float(pull.get("prerollWindowSec") or 10.0)
            preroll_path = Path(str(row.get("prerollLog") or "")) if row.get("prerollLog") else None
            preroll_exists = bool(preroll_path and preroll_path.exists())
            preroll_available = bool(pull.get("prerollAvailable") or preroll_exists)
            legacy_without_preroll = not preroll_available and not row.get("prerollFile")
            if preroll_available:
                if pre_sec + 0.05 < pre_window:
                    self.live_facts.insert("end", f"사전 로그: 풀 시작 전 {pre_sec:.1f}초까지만 확인됨 · 그 이전은 확인 불가\n\n", "note")
                else:
                    self.live_facts.insert("end", f"사전 로그: 풀 시작 전 {pre_window:.1f}초 확보\n\n", "note")
            elif legacy_without_preroll:
                self.live_facts.insert("end", "사전 로그 없음 (B33.1 이전 기록)\n\n", "note")
            else:
                self.live_facts.insert("end", "사전 로그 없음\n\n", "note")
            if first:
                mode = "풀러 확인" if pull.get("pullerConfirmed") else "첫 관측 공격"
                who = str(first.get("playerName") or "?").split("-")[0]
                if first.get("actorType") == "pet":
                    pet_name = str(first.get("sourceName") or "?").split("-")[0]
                    who += f" [펫:{pet_name}]"
                self.live_facts.insert(
                    "end",
                    f"{mode}: {self._fmt_live_relative(first.get('sec'))} · {who} · "
                    f"{self._pull_spell_display(first)} → {first.get('targetName') or '?'}\n",
                )
                near = pull.get("nearSimultaneousAttacks") or []
                if len(near) > 1:
                    self.live_facts.insert("end", "\n근접 시점 공격 근거:\n", "title")
                    for hit in near[:8]:
                        pname = str(hit.get("playerName") or "?").split("-")[0]
                        if hit.get("actorType") == "pet":
                            pet_name = str(hit.get("sourceName") or "?").split("-")[0]
                            pname += f" [펫:{pet_name}]"
                        self.live_facts.insert(
                            "end",
                            f"- {self._fmt_live_relative(hit.get('sec'))} · {pname} · "
                            f"{self._pull_spell_display(hit)} → {hit.get('targetName') or '?'}\n",
                        )
            else:
                self.live_facts.insert("end", "첫 공격 근거 없음\n")
            self.live_facts.insert(
                "end",
                "\n※ 원본 Try 번호와 raw 로그는 그대로 보존합니다. 반복 원인/기본 집계/쿨기 비교에서는 제외됩니다.\n"
                "[정상 트라이로 포함]을 누르면 수동 정상 판정을 meta.json에 저장하고 전체 복기를 다시 생성합니다.\n",
                "note",
            )
            self.live_facts.see("1.0")
            return
        if row.get("raidReviewStale"):
            old_engine = row.get("storedReviewEngineVersion") or "버전 정보 없음"
            reasons = ", ".join(row.get("reanalysisReasons") or [])
            self.live_facts.insert("end", "구버전 복기 · 현재 화면에서 사용하지 않음\n", "title")
            self.live_facts.insert(
                "end",
                f"저장된 복기: {old_engine} · 현재 엔진: {ENGINE_VERSION}\n"
                f"상태: 백그라운드 재분석 대기/진행 중{(' · ' + reasons) if reasons else ''}\n\n"
                "※ 구버전 원인 판정과 사망 수/쿨기 범위는 새 화면에 투영하지 않습니다. "
                "재분석이 끝나면 이 화면이 현재 엔진 결과로 자동 갱신됩니다.\n",
                "note",
            )
            self.live_facts.see("1.0")
            return
        mechanic_txt = Path(row.get("mechanicReviewTxt")) if row.get("mechanicReviewTxt") else None
        if mechanic_txt is None:
            storage_dir = row.get("storageDir")
            if storage_dir:
                candidate = Path(storage_dir) / "mechanic_review.txt"
                if candidate.exists():
                    mechanic_txt = candidate
        review_txt = Path(row.get("raidReviewTxt")) if row.get("raidReviewTxt") else None
        series_txt = Path(row.get("raidSeriesReviewTxt")) if row.get("raidSeriesReviewTxt") else None
        raid_txt = Path(row.get("raidFactsTxt")) if row.get("raidFactsTxt") else None
        death_txt = Path(row.get("deathReviewTxt")) if row.get("deathReviewTxt") else None
        facts_txt = Path(row.get("factsTxt")) if row.get("factsTxt") else None
        wrote = False

        # B35: boss-specific mechanic layer sits above the generic B31 collapse
        # review. It is a sidecar only and never mutates B31 causal artifacts.
        if mechanic_txt and mechanic_txt.exists():
            try:
                text = mechanic_txt.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                text = f"mechanic_review.txt 읽기 실패: {exc}\n"
            self.live_facts.insert("end", text)
            self.live_facts.insert("end", "\n" + "=" * 72 + "\n\n")
            wrote = True

        # B34 layout summary is a read-only projection of current engine artifacts.
        # It never re-decides causal membership: collapseChain is rendered exactly
        # as B31 stored it, and causal changes belong in the engine/fixture together.
        review_json = row_json(row, "raidReviewJson")
        if review_json:
            player = row.get("factsPlayer") or self.current_player
            model = build_try_layout_model(review_json, player)
            summary = render_try_summary(model, player)
            if summary:
                self.live_facts.insert("end", "확정 시안 · 전멸 복기\n", "title")
                self.live_facts.insert("end", summary)
                self.live_facts.insert("end", "\n" + "=" * 72 + "\n\n")
                wrote = True

        # B2 promotes causal review first, then scoped/cached repeated problems.
        for path, label in ((review_txt, "raid_review.txt"), (series_txt, "raid_series_review.txt")):
            if path and path.exists():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:
                    text = f"{label} 읽기 실패: {exc}\n"
                self.live_facts.insert("end", self._localize_review_text(text))
                self.live_facts.insert("end", "\n" + "=" * 72 + "\n\n")
                wrote = True
        # Raid-wide factual evidence remains immediately below the causal summary.
        if raid_txt and raid_txt.exists():
            try:
                text = raid_txt.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                text = f"raid_facts.txt 읽기 실패: {exc}\n"
            self.live_facts.insert("end", text)
            self.live_facts.insert("end", "\n" + "=" * 72 + "\n\n")
            wrote = True
        if death_txt and death_txt.exists():
            try:
                text = death_txt.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                text = f"death_review.txt 읽기 실패: {exc}\n"
            self.live_facts.insert("end", text)
            self.live_facts.insert("end", "\n" + "=" * 72 + "\n\n")
            wrote = True
        if facts_txt and facts_txt.exists():
            try:
                text = facts_txt.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                text = f"facts.txt 읽기 실패: {exc}\n"
            self.live_facts.insert("end", text)
            wrote = True
        if wrote:
            self.live_facts.see("1.0")
            return
        self.live_facts.insert(
            "end",
            "이 트라이는 아직 v0.4-B31 결과가 없습니다. 새로 끝난 Wipe/Kill부터 raid_review + 누적 series + raid_facts가 자동 생성됩니다.\n",
            "note",
        )

    @staticmethod
    def _localize_review_text(text):
        """Translate legacy/internal evidence phrases only at display time.

        Stored engine artifacts remain byte-for-byte compatible with B31 fixtures;
        this is a B34 presentation-layer cleanup.
        """
        import re
        value = str(text or "")
        value = re.sub(r"share (\d+) < baseline (\d+)", r"분담 \1명 (기준 \2명)", value)
        value = re.sub(
            r"failed encounter ended ([0-9.]+)s after root \(reset inferred\)",
            r"root 후 \1초 만에 전투 종료 (리셋으로 추정)", value,
        )
        value = re.sub(
            r"alive (\d+)→(\d+) before ([0-9.]+)",
            r"생존 인원 \1→\2 · \3초 이전", value,
        )
        value = re.sub(
            r"(\d+)/(\d+) victims <=50% HP before first (.+?) impact",
            r"피해 대상 \2명 중 \1명 HP 50% 이하 · 첫 \3 피격 직전", value,
        )
        return value

    @staticmethod
    def _open_path_external(path: Path):
        if os.name == "nt":
            os.startfile(str(path))
            return
        try:
            subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            pass

    def open_live_raid_facts_txt(self):
        row = self._selected_live_row()
        review = Path(row.get("raidReviewTxt")) if row and row.get("raidReviewTxt") else None
        if not review or not review.exists():
            review = Path(row.get("raidFactsTxt")) if row and row.get("raidFactsTxt") else None
        if not review or not review.exists():
            messagebox.showinfo("Live 복기", "선택한 트라이의 전멸 복기/공대 사실 파일이 아직 없습니다.")
            return
        self._open_path_external(review)

    def open_live_death_review_txt(self):
        row = self._selected_live_row()
        review = Path(row.get("deathReviewTxt")) if row and row.get("deathReviewTxt") else None
        if not review or not review.exists():
            messagebox.showinfo("Live 복기", "선택한 트라이의 death_review.txt가 아직 없습니다.")
            return
        self._open_path_external(review)

    def open_live_facts_txt(self):
        row = self._selected_live_row()
        facts = Path(row.get("factsTxt")) if row and row.get("factsTxt") else None
        if not facts or not facts.exists():
            messagebox.showinfo("Live 복기", "선택한 트라이의 facts.txt가 아직 없습니다.")
            return
        self._open_path_external(facts)

    def open_live_raw_log(self):
        row = self._selected_live_row()
        raw = Path(row.get("rawLog")) if row and row.get("rawLog") else None
        if raw is not None:
            from live.rawlog import resolve_raw
            raw = resolve_raw(raw)
        if not raw or not raw.exists():
            messagebox.showinfo("Live 복기", "선택한 트라이의 raw.log가 아직 없습니다.")
            return
        if str(raw).endswith(".gz"):
            # B31.2: 압축 보관된 원본은 폴더를 열어 보여준다.
            self._open_path_external(raw.parent)
            return
        self._open_path_external(raw)

    def open_live_storage_folder(self):
        row = self._selected_live_row()
        folder = Path(row.get("storageDir")) if row and row.get("storageDir") else None
        if not folder or not folder.exists():
            messagebox.showinfo("Live 복기", "선택한 트라이의 저장 폴더가 아직 없습니다.")
            return
        self._open_path_external(folder)

    def _on_close(self):
        if getattr(self, 'raid_public_mode', False) and getattr(self, '_public_import_running', False):
            if messagebox.askyesno(
                "공대 로그 가져오기 진행 중",
                "가져오기를 중단한 후 종료할까요?\n"
                "완료된 트라이는 유지하며, 저장 중인 트라이는 안전하게 정리한 뒤 종료합니다.",
            ):
                self._public_close_when_idle = True
                self._public_import_cancel.set()
                self.status_var.set("가져오기 중단 요청 · 작업 정리 후 종료합니다")
            return
        if getattr(self, 'raid_public_mode', False) and getattr(self, '_public_migration_running', False):
            messagebox.showinfo("기존 기록 복사 중", "기존 세션 복사가 끝난 후 종료해 주세요. 원본은 변경되지 않습니다.")
            return
        if getattr(self, '_feign_recheck_running', False):
            messagebox.showinfo("죽척 재검사 중", "재검사가 끝난 후 종료해 주세요. 원본 로그는 변경되지 않습니다.")
            return
        try:
            self.stop_live_monitor()
        except Exception:
            pass
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass
        self.destroy()

    def start_analysis(self, force: bool = False):
        if self.batch_running or (self.proc is not None and self.proc.poll() is None):
            messagebox.showinfo("분석 중", "이미 3캐릭 일괄 분석이 진행 중입니다.")
            return
        if not PYTHON.exists():
            messagebox.showerror("Python 없음", str(PYTHON))
            return
        if not PIPELINE.exists():
            messagebox.showerror("파이프라인 없음", str(PIPELINE))
            return

        self.status_var.set("3캐릭 최신 로그 탐색 중...")
        self.update_idletasks()

        # 일반 분석은 수동 로그를 존중한다. 강제 재분석은 최신 로그를 다시 탐색한다.
        jobs, missing = self._resolve_batch_jobs(force_find=force)
        self.batch_missing_players = list(missing)

        if not jobs:
            messagebox.showerror(
                "분석할 로그 없음",
                f"{LOGDIR}\n\nPRIVATE_ONLY / PRIVATE_ONLY / PRIVATE_ONLY가 포함된 WoWCombatLog를 찾지 못했습니다.",
            )
            self.status_var.set("3캐릭 로그 없음")
            self._refresh_available_players()
            return

        # 현재 선택 캐릭터의 로그도 이번에 확정된 매핑으로 갱신.
        current_mapped = self.character_logs.get(self.current_player)
        previous_log = str(self.current_log or "")
        if current_mapped and current_mapped.exists():
            self.current_log = current_mapped
            self.current_dataset = dataset_from_log(current_mapped)
        self._refresh_log_label()
        if previous_log != str(self.current_log or ""):
            self._reset_shared_nav_for_source()
        # B33 result ownership: completion may refresh the UI only if the user is
        # still on the same selection that launched this batch.
        self.batch_origin_token = self._selection_token()

        batch = []
        for j in jobs:
            cmd, source = self._command_for_log_job(j["log"], force, list(j["players"]))
            batch.append({
                "log": j["log"],
                "players": list(j["players"]),
                "cmd": cmd,
                "source": source,
            })

        self.console.delete("1.0", "end")
        self.console.insert(
            "end",
            "[ONE-CLICK] PRIVATE_ONLY / PRIVATE_ONLY / PRIVATE_ONLY 최신 로그 일괄 분석\n"
            f"[PLAN] 캐릭터 3명 → 고유 로그 {len(batch)}개\n",
        )
        for idx, job in enumerate(batch, 1):
            self.console.insert(
                "end",
                f"  {idx}. {' / '.join(job['players'])} → {job['log'].name}"
                f" [{job['source']}]\n",
            )
        if missing:
            self.console.insert(
                "end",
                "[MISSING] 포함 로그를 찾지 못한 캐릭터: " + " / ".join(missing) + "\n",
            )
        self.console.insert("end", "\n")
        self.notebook.select(self.log_frame)

        self.coach.delete("1.0", "end")
        self.quality.delete("1.0", "end")
        self.burst.delete("1.0", "end")
        self.brief.delete("1.0", "end")
        self.result.delete("1.0", "end")
        self.boss_detail.delete("1.0", "end")
        self._clear_boss_tree()

        self.batch_running = True
        self._progress_begin("일괄 분석", "batch")
        self.run_btn.configure(state="disabled")
        self.precision_btn.configure(state="disabled")
        self.cooldown_btn.configure(state="disabled")
        self.status_var.set(f"3캐릭 일괄 분석 시작 · 고유 로그 {len(batch)}개")

        threading.Thread(
            target=self._run_batch_process,
            args=(batch, self.batch_origin_token),
            daemon=True,
        ).start()

    def _run_batch_process(self, batch, origin_token=None):
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"

            completed = 0
            for idx, job in enumerate(batch, 1):
                self.msgq.put((
                    "batch_job_start",
                    {
                        "index": idx,
                        "total": len(batch),
                        "log": str(job["log"]),
                        "players": job["players"],
                        "source": job["source"],
                    },
                ))

                self.proc = subprocess.Popen(
                    job["cmd"],
                    cwd=str(WORKDIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creationflags,
                    env=env,
                )

                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    prog = self._parse_progress_line(line)
                    if prog is not None:
                        inner, stage = prog
                        overall = ((idx - 1) + max(0.0, min(100.0, inner)) / 100.0) / max(1, len(batch)) * 100.0
                        label = f"[{idx}/{len(batch)}] {stage}" if len(batch) > 1 else stage
                        self.msgq.put(("progress", (overall, label, "batch")))
                        continue
                    self.msgq.put(("log", line))

                code = self.proc.wait()
                if code != 0:
                    self.msgq.put((
                        "batch_done",
                        {
                            "code": code,
                            "completed": completed,
                            "total": len(batch),
                            "failedLog": str(job["log"]),
                            "token": origin_token,
                        },
                    ))
                    return

                completed += 1
                self.msgq.put((
                    "batch_job_done",
                    {
                        "index": idx,
                        "total": len(batch),
                        "log": str(job["log"]),
                    },
                ))

            self.proc = None
            self.msgq.put((
                "batch_done",
                {
                    "code": 0,
                    "completed": completed,
                    "total": len(batch),
                    "failedLog": None,
                    "token": origin_token,
                },
            ))
        except Exception as e:
            self.proc = None
            self.msgq.put(("batch_error", {"error": str(e), "token": origin_token}))

    # ---- B31.1 progress helpers -------------------------------------------------
    PROGRESS_LINE_RE = re.compile(r"^@@PROGRESS\s+([0-9]+(?:\.[0-9]+)?)\s*(.*)$")
    FETCH_INDEX_RE = re.compile(r"^\s*\[(\d+)/(\d+)\]\s+rank")

    def _progress_begin(self, title: str, owner: str):
        self._progress_owner = owner
        self._progress_title = title
        self._progress_pct = 0.0
        self._progress_stage = "준비"
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self.progress_text_var.set(f"{title} 중… 0% — 준비")

    def _progress_set(self, pct, stage=None, owner=None):
        if owner is not None and owner != self._progress_owner:
            return
        pct = max(self._progress_pct, min(99.0, float(pct)))
        self._progress_pct = pct
        if stage:
            self._progress_stage = str(stage)
        self.progress.configure(value=pct)
        self.progress_text_var.set(f"{self._progress_title} 중… {pct:.0f}% — {self._progress_stage}")

    def _progress_end(self, ok: bool = True, text: str | None = None, owner=None):
        if owner is not None and owner != self._progress_owner:
            return
        self.progress.stop()
        if ok:
            self._progress_pct = 100.0
            self.progress.configure(value=100)
        self.progress_text_var.set(
            text or (f"{self._progress_title} 완료 100%" if ok else f"{self._progress_title} 중단 · {self._progress_pct:.0f}%")
        )
        self._progress_owner = None

    def _main_task_running(self) -> bool:
        return bool(
            getattr(self, "batch_running", False)
            or getattr(self, "precision_running", False)
            or getattr(self, "cooldown_running", False)
        )

    @classmethod
    def _parse_progress_line(cls, line: str):
        m = cls.PROGRESS_LINE_RE.match(line.strip())
        if not m:
            return None
        return float(m.group(1)), m.group(2).strip()

    def _update_live_progress(self, snap: dict):
        """Drive the shared bar from the live review job when nothing else owns it."""
        job = snap.get("factJobProgress")
        if job:
            if self._main_task_running():
                return
            if self._progress_owner != "live":
                self._progress_begin("라이브 복기", "live")
            label = job.get("label") or "전투"
            queued = int(job.get("queued") or 0)
            extra = f" · 대기 {queued}건" if queued else ""
            self._progress_set(float(job.get("pct") or 0), f"{job.get('stage') or ''} · {label}{extra}", owner="live")
        elif self._progress_owner == "live":
            self._progress_end(True, "라이브 복기 완료 100%", owner="live")

    # B37.4.22: NiniArchive is a persistent, append-only WCL chronology cache.
    def _build_nini_archive_page(self):
        page = self.archive_frame
        top = ttk.Frame(page, style="App.TFrame", padding=_px((16, 14, 16, 8)))
        top.pack(fill="x")
        ttk.Label(top, text="PRIVATE_ONLY WCL 연대기", style="Header.TLabel", font=(FONT_TITLE, 16)).pack(side="left")
        self.archive_sync_btn = _round_button(top, text="WCL 동기화", command=self.start_nini_archive_sync)
        self.archive_sync_btn.pack(side="right")

        self.archive_status_var = tk.StringVar(value="로컬 연대기 확인 중")
        ttk.Label(page, textvariable=self.archive_status_var, style="Muted.TLabel", padding=_px((16, 0, 16, 8))).pack(fill="x")

        note = RoundBox(page, fill=_ui_theme.PANEL_BG, outline=_ui_theme.PANEL_BORDER,
                        outer=_ui_theme.PAGE_BG, radius=12, padx=14, pady=12)
        note.pack(fill="x", padx=_px(16), pady=_px((0, 10)))
        self.archive_summary_var = tk.StringVar(value="아직 동기화된 연대기 데이터가 없습니다.")
        ttk.Label(note.inner, textvariable=self.archive_summary_var, style="Card.TLabel",
                  justify="left", wraplength=max(640, self._target_window_width - self._sidebar_width - 120)).pack(anchor="w", fill="x")

        list_box = RoundBox(page, fill=_ui_theme.PANEL_BG, outline=_ui_theme.PANEL_BORDER,
                            outer=_ui_theme.PAGE_BG, radius=12, padx=14, pady=12)
        list_box.pack(fill="both", expand=True, padx=_px(16), pady=_px((0, 16)))
        ttk.Label(list_box.inner, text="최근 고유 전투", style="CardTitle.TLabel", font=(FONT_TITLE, 11)).pack(anchor="w", pady=_px((0, 8)))
        self.archive_text = tk.Text(
            list_box.inner, height=20, wrap="none", bg=_ui_theme.PANEL_BG, fg=_ui_theme.TEXT,
            insertbackground=_ui_theme.TEXT, relief="flat", bd=0, font=(FONT_BODY, 9),
        )
        self.archive_text.pack(fill="both", expand=True)
        try:
            self.archive_text.configure(state="disabled")
        except Exception:
            pass
        self.refresh_nini_archive_view()

    def _build_external_wcl_page(self):
        page = self.external_wcl_frame
        top = ttk.Frame(page, style="App.TFrame", padding=_px((16, 14, 16, 8)))
        top.pack(fill="x")
        ttk.Label(top, text="외부 WCL 로그", style="Header.TLabel", font=(FONT_TITLE, 16)).pack(side="left")

        note = RoundBox(page, fill=_ui_theme.PANEL_BG, outline=_ui_theme.PANEL_BORDER,
                        outer=_ui_theme.PAGE_BG, radius=12, padx=14, pady=12)
        note.pack(fill="x", padx=_px(16), pady=_px((0, 10)))
        ttk.Label(
            note.inner,
            text=("Warcraft Logs 링크 하나를 붙여넣으면 해당 fight의 도적을 직접 읽어 "
                  "어칼·어춤·은기·피아귀·소멸과 죽추/어어밤/고대기술 상태를 저장합니다.\n"
                  "도적이 여러 명이면 플레이어 이름을 적어 주세요. 비워 두면 잠행 도적이 한 명일 때 자동 선택합니다."),
            style="Card.TLabel", justify="left",
            wraplength=max(640, self._target_window_width - self._sidebar_width - 120),
        ).pack(anchor="w", fill="x")

        form = RoundBox(page, fill=_ui_theme.PANEL_BG, outline=_ui_theme.PANEL_BORDER,
                        outer=_ui_theme.PAGE_BG, radius=12, padx=14, pady=12)
        form.pack(fill="x", padx=_px(16), pady=_px((0, 10)))
        form.inner.columnconfigure(1, weight=1)

        ttk.Label(form.inner, text="WCL 링크", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", padx=_px((0, 10)), pady=_px((0, 7)))
        self.external_wcl_url_var = tk.StringVar(value="")
        self.external_wcl_url_entry = ttk.Entry(form.inner, textvariable=self.external_wcl_url_var)
        self.external_wcl_url_entry.grid(row=0, column=1, columnspan=3, sticky="ew", pady=_px((0, 7)))

        ttk.Label(form.inner, text="fight", style="CardTitle.TLabel").grid(row=1, column=0, sticky="w", padx=_px((0, 10)))
        self.external_wcl_fight_var = tk.StringVar(value="")
        ttk.Entry(form.inner, textvariable=self.external_wcl_fight_var, width=8).grid(row=1, column=1, sticky="w")
        ttk.Label(form.inner, text="플레이어", style="CardTitle.TLabel").grid(row=1, column=2, sticky="e", padx=_px((12, 8)))
        self.external_wcl_player_var = tk.StringVar(value="")
        ttk.Entry(form.inner, textvariable=self.external_wcl_player_var, width=22).grid(row=1, column=3, sticky="ew")

        actions = ttk.Frame(form.inner, style="Card.TFrame")
        actions.grid(row=2, column=0, columnspan=4, sticky="ew", pady=_px((10, 0)))
        self.external_wcl_run_btn = _round_button(actions, text="WCL 링크 분석", command=self.start_external_wcl_import)
        self.external_wcl_run_btn.pack(side="left")
        self.external_wcl_open_btn = _round_button(actions, text="결과 폴더 열기", command=self.open_external_wcl_folder, state="disabled")
        self.external_wcl_open_btn.pack(side="left", padx=_px((8, 0)))
        self.external_wcl_replay_btn = _round_button(actions, text="WCL 리플레이", command=self.open_external_wcl_replay)
        self.external_wcl_replay_btn.pack(side="left", padx=_px((8, 0)))
        self.external_wcl_library_btn = _round_button(actions, text="상위 도적 분석 창", command=self.open_elite_research_window)
        self.external_wcl_library_btn.pack(side="left", padx=_px((8, 0)))
        self.external_wcl_drive_btn = _round_button(actions, text="Drive 저장 경로", command=self.configure_external_wcl_drive)
        self.external_wcl_drive_btn.pack(side="left", padx=_px((8, 0)))
        self.external_wcl_drive_status_btn = _round_button(actions, text="Drive 상태", command=self.show_external_wcl_drive_status)
        self.external_wcl_drive_status_btn.pack(side="left", padx=_px((8, 0)))
        self.external_wcl_status_var = tk.StringVar(value="대기")
        ttk.Label(actions, textvariable=self.external_wcl_status_var, style="Muted.TLabel").pack(side="left", padx=_px((12, 0)))
        # Drive research destination is separate from the active WoWCombatLog.
        self.external_wcl_drive_path_var = tk.StringVar(value="Drive 경로: 미설정")
        ttk.Label(form.inner, textvariable=self.external_wcl_drive_path_var,
                  style="Muted.TLabel", justify="left", anchor="w",
                  wraplength=max(600, self._target_window_width - self._sidebar_width - 130)
                  ).grid(row=3, column=0, columnspan=4, sticky="ew", pady=_px((7, 0)))
        self._refresh_external_wcl_drive_path()

        result_box = RoundBox(page, fill=_ui_theme.PANEL_BG, outline=_ui_theme.PANEL_BORDER,
                              outer=_ui_theme.PAGE_BG, radius=12, padx=14, pady=12)
        result_box.pack(fill="both", expand=True, padx=_px(16), pady=_px((0, 16)))
        ttk.Label(result_box.inner, text="분석 결과", style="CardTitle.TLabel", font=(FONT_TITLE, 11)).pack(anchor="w", pady=_px((0, 8)))
        self.external_wcl_text = tk.Text(
            result_box.inner, height=26, wrap="none", bg=_ui_theme.PANEL_BG, fg=_ui_theme.TEXT,
            insertbackground=_ui_theme.TEXT, relief="flat", bd=0, font=(FONT_BODY, 9),
        )
        self.external_wcl_text.pack(fill="both", expand=True)
        self.external_wcl_text.insert("1.0", "WCL 링크를 넣고 [WCL 링크 분석]을 누르세요.")
        self.external_wcl_text.configure(state="disabled")

    def show_external_wcl(self):
        self._refresh_external_wcl_drive_path()
        self._show_main_frame(self.external_wcl_frame)
        self.breadcrumb_var.set("외부 WCL 로그")
        try:
            self.external_wcl_url_entry.focus_set()
        except Exception:
            pass

    def _external_wcl_dir(self) -> Path:
        return WORKDIR / "data" / "external_wcl"

    def _set_external_wcl_text(self, text):
        try:
            self.external_wcl_text.configure(state="normal")
            self.external_wcl_text.delete("1.0", "end")
            self.external_wcl_text.insert("end", str(text or ""))
            self.external_wcl_text.configure(state="disabled")
        except Exception:
            pass

    def open_external_wcl_replay(self):
        import webbrowser
        result = self.external_wcl_last_result or {}
        url = result.get("replayURL") if isinstance(result, dict) else None
        if not url:
            try:
                from engine.external_wcl_report import parse_wcl_report_ref, wcl_links
                ref = parse_wcl_report_ref(self.external_wcl_url_var.get(),
                                          fight_id=int(self.external_wcl_fight_var.get()) if self.external_wcl_fight_var.get().strip() else None)
                if ref.fight_id:
                    url = wcl_links(ref.report_code, ref.fight_id)["replay"]
            except Exception:
                url = None
        if not url:
            messagebox.showinfo("WCL 리플레이", "먼저 fight 번호가 들어간 WCL 링크를 입력해 주세요.")
            return
        webbrowser.open(url)

    def _refresh_external_wcl_drive_path(self):
        """Display previously approved path without setting or syncing anything."""
        from engine.external_wcl_report import elite_drive_saved_folder
        previous = elite_drive_saved_folder(WORKDIR)
        if previous:
            state = "연결됨" if Path(previous).is_dir() else "현재 접근 불가 · 설정 보존"
            label = f"Drive 저장 경로: {previous} ({state})"
        else:
            label = "Drive 저장 경로: 미설정 (WoW 로그 폴더와 별도)"
        if hasattr(self, "external_wcl_drive_path_var"):
            self.external_wcl_drive_path_var.set(label)

    def configure_external_wcl_drive(self):
        from engine.external_wcl_report import elite_drive_picker_initial_dir
        # Tk's default asks from the last OS file dialog, often the WoW log
        # folder. The research destination must use its OWN persisted path.
        start_folder = elite_drive_picker_initial_dir(WORKDIR)
        target = filedialog.askdirectory(
            title="내 Google Drive의 NiniAnalyzer_EliteRogues 동기화 폴더 선택",
            initialdir=str(start_folder), mustexist=True)
        if not target:
            return
        if not target:
            return
        approved = messagebox.askyesno(
            "Drive 공유 권한 확인",
            "선택한 폴더가 비공개이고, 사용할 Google 계정에 접근 권한이 있는지\n"
            "Google Drive 웹에서 직접 확인했나요?\n\n"
            "NiniAnalyzer_EliteRogues 전용 폴더만 선택해 주세요.\n"
            "확인을 누르면 앞으로 수집하는 경량 ZIP이 이 폴더로 복사됩니다."
        )
        if not approved:
            return
        try:
            from engine.external_wcl_report import configure_elite_drive_folder, _sync_archive_outbox
            path = configure_elite_drive_folder(WORKDIR, target, privacy_confirmed=True)
            self._refresh_external_wcl_drive_path()
            result = _sync_archive_outbox(self._external_wcl_dir(), WORKDIR)
            status = result.get('status') or '알 수 없음'
            messagebox.showinfo("상위 도적 보관함", f"Drive 폴더 지정 완료\n{target}\n\n저장 설정: {path}\n경량 ZIP 동기화: {status}")
        except Exception as exc:
            messagebox.showerror("Drive 폴더 설정 실패", str(exc))

    def show_external_wcl_drive_status(self):
        try:
            from engine.external_wcl_report import elite_drive_status, _sync_archive_outbox
            info = elite_drive_status(WORKDIR, self._external_wcl_dir())
            self._refresh_external_wcl_drive_path()
            size_mib = info['pendingLiteBytes'] / (1024 * 1024)
            if not info['configured']:
                status = ('저장된 Drive 경로에 현재 접근 불가 · 설정 보존' if info.get('savedFolder')
                          else '미설정 — 로컬에만 보관 중')
            else:
                result = _sync_archive_outbox(self._external_wcl_dir(), WORKDIR)
                status = result.get('status') or '확인 불가'
            messagebox.showinfo('상위 도적 Drive 상태',
                f"로컬 경량 ZIP 누적: {size_mib:.2f} MiB\n"
                f"자료 수: {info['pendingRecords']}\n"
                f"저장된 폴더: {info.get('savedFolder') or '없음'}\n"
                f"폴더 접근: {'가능' if info['folderAccessible'] else '불가'}\n"
                f"동기화: {status}\n\n"
                "Google 계정 접근 권한/온라인 업로드 완료 여부는 Drive 웹에서 확인하세요."
            )
        except Exception as exc:
            messagebox.showerror('Drive 상태 확인 실패', str(exc))

    def open_elite_research_window(self):
        """PERSONAL GOLD: read-only independent archive research window."""
        if self.raid_public_mode:
            return
        try:
            from engine.elite_rogue_viewer import open_elite_research
            self._elite_research_window = open_elite_research(
                self, WORKDIR, getattr(self, "_elite_research_window", None))
        except Exception as exc:
            messagebox.showerror("상위 도적 분석 창", str(exc))

    def show_external_wcl_library(self):
        path = self._external_wcl_dir() / "elite_rogues_catalog.json"
        if not path.exists():
            self._set_external_wcl_text("저장된 상위 도적 자료가 아직 없습니다.\nWCL 로그를 먼저 분석하세요.")
            return
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            lines = ["=== 상위 도적 로그 보관함 ===", f"저장된 원본: {len(rows)}개", ""]
            for row in reversed(rows[-200:]):
                links=row.get('links') or {}
                lines.append(f"{row.get('player')} · {row.get('boss')} · fight {row.get('fightID')} · {row.get('reportCode')}")
                lines.append(f"  Drive 경량 ZIP: {row.get('archiveFile')}")
                lines.append(f"  PC 상세 ZIP: {row.get('fullLocalArchiveFile') or '(이전 버전)'}")
                lines.append(f"  Replay: {links.get('replay','')}")
            lines.extend(["", "Drive 전송 대기 경량 ZIP: data/external_wcl/EliteArchiveOutbox/",
                          "Drive 연결 시: NiniAnalyzer_EliteRogues/<보스>/<플레이어>/",
                          "Drive ZIP은 시전·자원·오라 원본 + 초당 피해 집계만 포함. 상세 피해 이벤트는 PC에 보존.",
                          "WCL 리플레이는 사이트 링크이며 동영상 자체가 저장되는 것은 아닙니다."])
            self._set_external_wcl_text("\n".join(lines))
        except Exception as exc:
            self._set_external_wcl_text(f"보관함 읽기 실패: {exc}")

    def start_external_wcl_import(self):
        if self.external_wcl_running:
            messagebox.showinfo("외부 WCL", "이미 외부 WCL 로그를 분석하고 있습니다.")
            return
        if not wcl_credentials_available():
            messagebox.showerror("WCL 인증 필요", "WCL_CLIENT_ID / WCL_CLIENT_SECRET가 없습니다.\nWCL_API_Setup.cmd로 먼저 인증을 설정하세요.")
            return
        raw_url = self.external_wcl_url_var.get().strip()
        if not raw_url:
            messagebox.showinfo("외부 WCL", "Warcraft Logs 링크를 붙여넣어 주세요.")
            return
        fight_text = self.external_wcl_fight_var.get().strip()
        try:
            fight_id = int(fight_text) if fight_text else None
        except Exception:
            messagebox.showerror("외부 WCL", "fight 번호는 숫자로 입력하세요.")
            return
        player_name = self.external_wcl_player_var.get().strip() or None
        self.external_wcl_running = True
        self.external_wcl_run_btn.configure(state="disabled")
        self.external_wcl_open_btn.configure(state="disabled")
        self.external_wcl_status_var.set("WCL 메타데이터 확인 중")
        self._set_external_wcl_text("외부 WCL 로그를 불러오는 중입니다...\n")
        self._show_main_frame(self.external_wcl_frame)

        def worker():
            try:
                from engine.external_wcl_report import import_external_report, parse_wcl_report_ref
                # URL에 fight가 있으면 별도 칸을 비워 둬도 그대로 사용한다.
                ref = parse_wcl_report_ref(raw_url, fight_id=fight_id)
                def progress(payload):
                    self.msgq.put(("external_wcl_progress", payload))
                result = import_external_report(
                    raw_url, fight_id=ref.fight_id, player_name=player_name,
                    out_root=self._external_wcl_dir(), progress=progress,
                )
                self.msgq.put(("external_wcl_done", result))
            except Exception as exc:
                self.msgq.put(("external_wcl_error", str(exc)))
        threading.Thread(target=worker, daemon=True, name="ExternalWCLImport").start()

    def open_external_wcl_folder(self):
        result = self.external_wcl_last_result or {}
        folder = Path(result.get("snapshotDir") or "") if result else None
        if not folder or not folder.exists():
            messagebox.showinfo("외부 WCL", "아직 저장된 외부 WCL 분석 결과가 없습니다.")
            return
        self._open_path_external(folder)

    def _archive_dir(self) -> Path:
        return WORKDIR / "data" / "nini_archive"

    def refresh_nini_archive_view(self):
        from engine.nini_archive import summarize_archive
        p = self._archive_dir() / "index.json"
        if not p.exists():
            if hasattr(self, "archive_summary_var"):
                self.archive_summary_var.set("아직 동기화된 연대기 데이터가 없습니다.\n[WCL 동기화]를 누르면 최신 WCL 보고서를 로컬 누적 캐시에 저장합니다.")
            if hasattr(self, "archive_status_var"):
                self.archive_status_var.set("대기 · 로컬 캐시 없음")
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            self.archive_summary_var.set(f"연대기 index.json 읽기 실패: {exc}")
            return
        self.archive_summary_var.set(summarize_archive(data))
        fights = list(data.get("careerUniqueFights") or [])
        fights.sort(key=lambda r: float(r.get("absoluteStartTime") or 0.0), reverse=True)
        lines = []
        for row in fights[:250]:
            try:
                ts = float(row.get("absoluteStartTime") or 0.0) / 1000.0
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "---- -- -- --:--"
            except Exception:
                when = "---- -- -- --:--"
            result = "KILL" if row.get("kill") else "WIPE"
            diff = row.get("difficulty")
            diff_label = {3: "일반", 4: "영웅", 5: "신화", 8: "쐐기"}.get(diff, str(diff or "?"))
            uploads = int(row.get("uploadCount") or 1)
            dup = f" · 업로드 {uploads}" if uploads > 1 else ""
            lines.append(f"{when} · {row.get('name') or '전투'} · {diff_label} · {result}{dup}")
        text = "\n".join(lines) if lines else "캐릭터가 실제 참여한 전투가 아직 저장되지 않았습니다."
        try:
            self.archive_text.configure(state="normal")
            self.archive_text.delete("1.0", "end")
            self.archive_text.insert("end", text)
            self.archive_text.configure(state="disabled")
        except Exception:
            pass
        self.archive_status_var.set("로컬 연대기 로드 완료")

    def show_nini_archive(self):
        self.refresh_nini_archive_view()
        self._show_main_frame(self.archive_frame)
        self.breadcrumb_var.set("PRIVATE_ONLY WCL 연대기")

    def start_nini_archive_sync(self):
        if self.archive_running:
            messagebox.showinfo("NiniArchive", "이미 WCL 연대기를 동기화하고 있습니다.")
            return
        if not wcl_credentials_available():
            messagebox.showerror("WCL 인증 필요", "WCL_CLIENT_ID / WCL_CLIENT_SECRET가 없습니다.\nWCL_API_Setup.cmd로 먼저 인증을 설정하세요.")
            return
        self.archive_running = True
        self.archive_sync_btn.configure(state="disabled")
        self.archive_status_var.set("WCL 연대기 동기화 시작")
        self._show_main_frame(self.archive_frame)

        def worker():
            try:
                from engine.nini_archive import sync_character_archive
                def progress(payload):
                    self.msgq.put(("archive_progress", payload))
                result = sync_character_archive(
                    out_dir=self._archive_dir(),
                    character_name="PRIVATE_ONLY", server_slug="azshara", region="kr",
                    progress=progress,
                )
                self.msgq.put(("archive_done", result))
            except Exception as exc:
                self.msgq.put(("archive_error", str(exc)))
        threading.Thread(target=worker, daemon=True, name="NiniArchiveSync").start()

    def _pump_queue(self):
        if _B38_TK_CLEANUP:
            _b38_drain_tk_cleanup()
        try:
            while True:
                kind, data = self.msgq.get_nowait()
                if kind == "public_migration_done":
                    self._public_migration_running = False
                    self._refresh_live_recent(force_storage_reload=True)
                    self.status_var.set(f"공개판 기존 공대 세션 복사 완료 · {int(data.get('copied') or 0)}개")
                    continue
                if kind == "public_migration_error":
                    self._public_migration_running = False
                    self.status_var.set(f"기존 세션 복사 오류 · {data}")
                    continue
                if kind == "public_import_progress":
                    if self._public_import_running:
                        payload = data if isinstance(data, dict) else {}
                        pct = float(payload.get("percent") or 0)
                        label = f"로그 분석 중 · {int(payload.get('lines') or 0):,}줄 · 트라이 {int(payload.get('imported') or 0)}개"
                        self.status_var.set(label)
                        self._progress_set(pct, label, owner="public_import")
                    continue
                if kind in ("public_import_done", "public_import_error"):
                    self._public_import_running = False
                    self._public_import_cancel = None
                    self.run_btn.configure(state="normal")
                    self.public_log_pick_btn.configure(state="normal")
                    if kind == "public_import_error":
                        self._progress_end(False, "공대 로그 가져오기 오류", owner="public_import")
                        self.status_var.set(f"공대 로그 가져오기 실패 · {data}")
                        messagebox.showerror("공대 로그 가져오기 실패", str(data))
                    else:
                        errors = int(data.get("errors") or 0)
                        self._progress_end(not errors, "공대 로그 가져오기 완료" if not errors else "일부 트라이 판정 오류", owner="public_import")
                        self._refresh_live_recent(force_storage_reload=True)
                        imported = int(data.get("imported") or 0)
                        skipped = int(data.get("skipped") or 0)
                        error_label = f" · 판정 오류 {errors}개" if errors else ""
                        stopped = "중단됨 · " if data.get('cancelled') else ""
                        self.status_var.set(f"{stopped}공대 로그 분석 완료 · 새 트라이 {imported}개 · 기존 {skipped}개 건너뜀{error_label}")
                    if self._public_close_when_idle:
                        self._public_close_when_idle = False
                        self.after_idle(self._on_close)
                    continue
                if kind == "timeline_icons_ready":
                    payload = data if isinstance(data, dict) else {}
                    requested = set(payload.get("requested") or [])
                    pending = set(getattr(self, "_b3750_icon_fetch_pending", set()))
                    pending.difference_update(requested)
                    self._b3750_icon_fetch_pending = pending
                    failed = set(getattr(self, "_b3750_icon_fetch_failed", set()))
                    failed.update(payload.get("failed") or [])
                    self._b3750_icon_fetch_failed = failed
                    if str(getattr(self, "b3641_timeline_mode", tk.StringVar(value="아이콘")).get()) == "아이콘":
                        try:
                            self._draw_b3750_try_timeline()
                        except Exception:
                            pass
                    continue
                if kind == "external_wcl_progress":
                    payload = data if isinstance(data, dict) else {"message": str(data)}
                    msg = str(payload.get("message") or "외부 WCL 분석 중")
                    if hasattr(self, "external_wcl_status_var"):
                        self.external_wcl_status_var.set(msg)
                    if hasattr(self, "status_var"):
                        self.status_var.set("외부 WCL · " + msg)
                    continue
                elif kind == "external_wcl_done":
                    self.external_wcl_running = False
                    self.external_wcl_last_result = data if isinstance(data, dict) else None
                    if hasattr(self, "external_wcl_run_btn"):
                        self.external_wcl_run_btn.configure(state="normal")
                    if hasattr(self, "external_wcl_open_btn"):
                        self.external_wcl_open_btn.configure(state="normal")
                    if hasattr(self, "external_wcl_status_var"):
                        sync_ok=isinstance(data,dict) and data.get("driveSyncStatus")=="synced_local_drive_folder"
                        self.external_wcl_status_var.set("분석 완료 · Drive 복사" if sync_ok else "분석 완료 · Drive 대기 (로컬 ZIP 보관)")
                    if isinstance(data, dict):
                        self._set_external_wcl_text(data.get("summary") or "분석 완료")
                        player = (data.get("player") or {}).get("name") or "도적"
                        fight = data.get("fight") or {}
                        # URL에서 읽힌 fight를 다음 재분석용으로 화면에도 명시한다.
                        if hasattr(self, "external_wcl_fight_var"):
                            self.external_wcl_fight_var.set(str(data.get("fightID") or ""))
                        if hasattr(self, "external_wcl_player_var"):
                            self.external_wcl_player_var.set(str(player))
                        self.status_var.set(f"외부 WCL 완료 · {player} · {fight.get('name') or 'fight'}")
                    continue
                elif kind == "external_wcl_error":
                    self.external_wcl_running = False
                    if hasattr(self, "external_wcl_run_btn"):
                        self.external_wcl_run_btn.configure(state="normal")
                    if hasattr(self, "external_wcl_status_var"):
                        self.external_wcl_status_var.set("분석 실패")
                    self.status_var.set("외부 WCL 분석 실패")
                    self._set_external_wcl_text("분석 실패\n\n" + str(data))
                    messagebox.showerror("외부 WCL 분석 실패", str(data))
                if kind == "archive_progress":
                    payload = data if isinstance(data, dict) else {"message": str(data)}
                    if hasattr(self, "archive_status_var"):
                        self.archive_status_var.set(str(payload.get("message") or "WCL 연대기 동기화 중"))
                    if hasattr(self, "status_var"):
                        self.status_var.set("NiniArchive · " + str(payload.get("message") or "동기화 중"))
                    continue
                elif kind == "archive_done":
                    self.archive_running = False
                    if hasattr(self, "archive_sync_btn"):
                        self.archive_sync_btn.configure(state="normal")
                    self.refresh_nini_archive_view()
                    cov = (data or {}).get("coverage") or {} if isinstance(data, dict) else {}
                    self.status_var.set(
                        f"NiniArchive 완료 · reports {int(cov.get('localCachedReports') or 0)} · fights {int(cov.get('localCachedFights') or 0)}"
                    )
                    continue
                elif kind == "archive_error":
                    self.archive_running = False
                    if hasattr(self, "archive_sync_btn"):
                        self.archive_sync_btn.configure(state="normal")
                    if hasattr(self, "archive_status_var"):
                        self.archive_status_var.set("동기화 실패")
                    self.status_var.set("NiniArchive 동기화 실패")
                    messagebox.showerror("NiniArchive 동기화 실패", str(data))
                    continue
                elif kind == "identity_audit_done":
                    self._identity_audit_running = False
                    self.live_persisted_rows = list(data.get("rows") or [])
                    self._live_storage_loaded = True
                    if self.live_monitor is not None:
                        self._live_storage_revision = getattr(self.live_monitor, "storage_revision", None)
                    # Rebuild from corrected persisted rows without rescanning raw
                    # logs on the UI thread.  This also moves corrected pulls to
                    # the proper character tree as soon as the worker finishes.
                    self._refresh_live_recent(force_storage_reload=False)
                    checked = int(data.get("checked") or 0)
                    corrected = int(data.get("corrected") or 0)
                    elapsed = float(data.get("elapsedSec") or 0.0)
                    if hasattr(self, "status_var"):
                        self.status_var.set(
                            f"저장 기록 확인 완료 · {checked}개 확인 · {corrected}개 보정 · {elapsed:.1f}초"
                        )
                    continue
                elif kind == "identity_audit_error":
                    self._identity_audit_running = False
                    if hasattr(self, "status_var"):
                        self.status_var.set(f"저장 기록 확인 실패 · {data.get('error') or 'unknown'}")
                    continue
                elif kind == "saved_reanalysis_progress":
                    index = int(data.get("index") or 0)
                    total = max(1, int(data.get("total") or 1))
                    label = data.get("label") or "트라이"
                    phase = data.get("phase") or "start"
                    if phase == "done":
                        self._saved_reanalysis_done = max(self._saved_reanalysis_done, index)
                        status = f"저장 복기 최신화 {index}/{total} 완료 · {label}"
                        fresh_row = data.get("row") if isinstance(data.get("row"), dict) else None
                        if fresh_row:
                            fresh_key = self._live_row_key(fresh_row)
                            replaced = False
                            rows_now = list(getattr(self, "live_persisted_rows", []) or [])
                            for pos, existing in enumerate(rows_now):
                                if self._same_live_try_key(self._live_row_key(existing), fresh_key):
                                    rows_now[pos] = fresh_row
                                    replaced = True
                                    break
                            if not replaced:
                                rows_now.append(fresh_row)
                            self.live_persisted_rows = rows_now
                            if self.nav_state.get("page") == "live_try" and self._same_live_try_key(
                                fresh_key, self.nav_state.get("liveTryKey")
                            ):
                                self._prepared_review_models.pop(self._prepared_row_cache_key(fresh_row), None)
                                self._prepared_series_models.pop(self._prepared_row_cache_key(fresh_row), None)
                                self._update_live_selection(fresh_row)
                                self._render_live_facts(fresh_row)
                                self._update_breadcrumb()
                    elif phase == "error":
                        status = f"저장 복기 최신화 {index}/{total} 오류 · {label}"
                    else:
                        status = f"저장 복기 최신화 {index}/{total} · {label}"
                    if hasattr(self, "status_var"):
                        self.status_var.set(status)
                    if getattr(self, "_progress_owner", None) == "saved_reanalysis":
                        pct = ((index - 1) if phase == "start" else index) * 100.0 / total
                        self._progress_set(pct, status, owner="saved_reanalysis")
                    continue
                elif kind == "saved_reanalysis_done":
                    self._saved_reanalysis_running = False
                    rows = list(data.get("rows") or [])
                    if rows:
                        self.live_persisted_rows = rows
                        self._live_storage_loaded = True
                        if self.live_monitor is not None:
                            self._live_storage_revision = getattr(self.live_monitor, "storage_revision", None)
                    self._prepared_review_models = dict(data.get("preparedReviews") or {})
                    self._prepared_series_models = dict(data.get("preparedSeries") or {})
                    self._refresh_live_recent(force_storage_reload=False)
                    rebuilt = int(data.get("rebuilt") or 0)
                    total = int(data.get("total") or 0)
                    errors = list(data.get("errors") or [])
                    elapsed = float(data.get("elapsedSec") or 0.0)
                    if getattr(self, "_progress_owner", None) == "saved_reanalysis":
                        self._progress_end(not errors, f"저장 복기 최신화 완료 · {rebuilt}/{total}", owner="saved_reanalysis")
                    if hasattr(self, "status_var"):
                        suffix = f" · 오류 {len(errors)}개" if errors else ""
                        self.status_var.set(
                            f"저장 복기 최신화 완료 · {rebuilt}/{total} · {elapsed:.1f}초{suffix}"
                        )
                    continue
                elif kind == "batch_job_start":
                    idx = data.get("index", "?")
                    total = data.get("total", "?")
                    who = " / ".join(data.get("players") or [])
                    log_name = Path(str(data.get("log", ""))).name
                    self.console.insert(
                        "end",
                        f"\n{'='*72}\n[BATCH {idx}/{total}] {who} · {log_name}\n",
                    )
                    self.console.see("end")
                    self.status_var.set(f"일괄 분석 {idx}/{total} · {who}")
                elif kind == "batch_job_done":
                    idx = data.get("index", "?")
                    total = data.get("total", "?")
                    self.console.insert("end", f"[BATCH {idx}/{total}] 완료\n")
                    self.console.see("end")
                elif kind == "log":
                    self.console.insert("end", data)
                    self.console.see("end")
                    line = data.strip()
                    if "[CHAR]" in line:
                        m = re.search(r"\[CHAR\]\s+([^·]+)", line)
                        who = m.group(1).strip() if m else "캐릭터"
                        self.status_var.set(f"{who} 분석 중...")
                    elif "[SCAN]" in line:
                        self.status_var.set("통로그 encounter 스캔 · 3캐릭 감지 중...")
                    elif "[EXTRACT]" in line:
                        self.status_var.set("통로그 → 3캐릭 동시 분리 중...")
                    elif "[CACHE]" in line or "[재사용]" in line:
                        self.status_var.set("캐시 재사용 · 분석 중...")
                    elif "[WCL 재사용]" in line:
                        self.status_var.set("WCL 비교 캐시 재사용 중...")
                    elif "fetch_" in line and "comparators" in line:
                        self.status_var.set("WCL 비교군 가져오는 중...")
                    elif "[암살 259]" in line or "[잠행 261]" in line:
                        self.status_var.set("보스별 분석 중...")
                elif kind == "batch_done":
                    self._progress_end(int(data.get("code", 1)) == 0)
                    self.batch_running = False
                    self.proc = None
                    self.run_btn.configure(state="normal")
                    self.precision_btn.configure(state="normal")
                    self.cooldown_btn.configure(state="normal")

                    code = int(data.get("code", 1))
                    completed = int(data.get("completed", 0))
                    total = int(data.get("total", 0))
                    owner_token = data.get("token")
                    stale_result = owner_token is not None and not self._result_belongs_to_current_selection(owner_token)

                    if code == 0 and stale_result:
                        # The files are valid, but the user has navigated elsewhere.
                        # Never replace that newer screen with an older job result.
                        self.status_var.set("일괄 분석 완료 · 현재 선택 유지")
                        continue

                    if code == 0:
                        # 현재 캐릭터가 가리키는 로그/데이터셋으로 복귀해서 결과 표시.
                        mapped = self.character_logs.get(self.current_player)
                        if mapped and mapped.exists():
                            self.current_log = mapped
                            self.current_dataset = dataset_from_log(mapped)
                        self._refresh_log_label()

                        summary = self.summary_path()
                        # 방금 선택된 최신 로그가 빈 Live 로그이거나 추종자/던전 전용이라
                        # 레이드 결과가 0이면, 기존에 존재하는 가장 최근 레이드 분석을 표시한다.
                        if not (summary and summary.exists()) or not _summary_has_raid_results(summary):
                            fallback_log = find_latest_analyzed_log_for_player(self.current_player)
                            if fallback_log and fallback_log.exists():
                                self.current_log = fallback_log
                                self.current_dataset = dataset_from_log(fallback_log)
                                self._refresh_log_label()
                                summary = self.summary_path()

                        if summary and summary.exists():
                            self.status_var.set(
                                f"3캐릭 일괄 분석 완료 · {self.current_player} 표시"
                            )
                            self.load_results()
                        else:
                            self.status_var.set(
                                f"3캐릭 일괄 분석 완료 · {self.current_player} 결과 없음"
                            )
                            self.notebook.select(self.log_frame)

                        missing = list(self.batch_missing_players)
                        if missing:
                            messagebox.showinfo(
                                "일괄 분석 완료",
                                f"고유 로그 {completed}개 분석을 완료했습니다.\n\n"
                                "포함 로그를 찾지 못한 캐릭터: " + " / ".join(missing),
                            )
                    else:
                        self.status_var.set(f"일괄 분석 실패 (exit {code})")
                        self.notebook.select(self.log_frame)
                        all_log = self.console.get("1.0", "end").strip().splitlines()
                        tail = "\n".join(all_log[-16:])
                        messagebox.showerror(
                            "일괄 분석 실패",
                            f"{completed}/{total}개 로그 처리 후 중단되었습니다.\n"
                            f"exit code: {code}\n\n마지막 로그:\n{tail}",
                        )
                elif kind == "batch_error":
                    self._progress_end(False)
                    self.batch_running = False
                    self.proc = None
                    self.run_btn.configure(state="normal")
                    self.precision_btn.configure(state="normal")
                    self.cooldown_btn.configure(state="normal")
                    payload = data if isinstance(data, dict) else {"error": str(data), "token": None}
                    stale_result = payload.get("token") is not None and not self._result_belongs_to_current_selection(payload.get("token"))
                    self.status_var.set("3캐릭 일괄 분석 오류" + (" · 현재 선택 유지" if stale_result else ""))
                    if not stale_result:
                        self.notebook.select(self.log_frame)
                    messagebox.showerror("일괄 분석 오류", str(payload.get("error") or "unknown"))
                elif kind == "progress":
                    pct, stage, owner = data
                    self._progress_set(pct, stage, owner=owner)
                elif kind == "cooldown_status":
                    self.status_var.set(str(data))
                elif kind == "cooldown_done":
                    self._progress_end(True)
                    self.cooldown_running = False
                    self.cooldown_btn.configure(state="normal")
                    self.precision_btn.configure(state="normal")
                    self.run_btn.configure(state="normal")
                    payload = data if isinstance(data, dict) else {"path": str(data), "token": None}
                    if payload.get("token") is not None and not self._result_belongs_to_current_selection(payload.get("token")):
                        self.status_var.set("쿨기/NSRT 생성 완료 · 현재 선택 유지")
                        continue
                    self.status_var.set("쿨기/NSRT 생성 완료")
                    self.load_cooldown_result(Path(str(payload.get("path") or "")))
                    b = self.selected_boss()
                    if b:
                        self.cooldown_context_key = self._boss_cooldown_context_key(b, self.current_player)
                    self.notebook.select(self.cooldown_frame)
                elif kind == "cooldown_error":
                    self._progress_end(False)
                    self.cooldown_running = False
                    self.cooldown_btn.configure(state="normal")
                    self.precision_btn.configure(state="normal")
                    self.run_btn.configure(state="normal")
                    payload = data if isinstance(data, dict) else {"error": str(data), "token": None}
                    stale_result = payload.get("token") is not None and not self._result_belongs_to_current_selection(payload.get("token"))
                    self.status_var.set("쿨기/NSRT 오류" + (" · 현재 선택 유지" if stale_result else ""))
                    if not stale_result:
                        self.notebook.select(self.log_frame)
                    messagebox.showerror("쿨기/NSRT 오류", str(payload.get("error") or "unknown"))
                elif kind == "precision_status":
                    self.status_var.set(str(data))
                elif kind == "precision_done":
                    self._progress_end(True)
                    self.precision_running = False
                    self.precision_btn.configure(state="normal")
                    self.run_btn.configure(state="normal")
                    payload = data if isinstance(data, dict) else {"path": str(data), "token": None}
                    if payload.get("token") is not None and not self._result_belongs_to_current_selection(payload.get("token")):
                        self.status_var.set("정밀 분석 완료 · 현재 선택 유지")
                        continue
                    partial = bool(payload.get("partial"))
                    if partial:
                        self.status_var.set("정밀 분석 부분 완료 · " + str(payload.get("status") or "비교군 없음"))
                    else:
                        self.status_var.set("정밀 분석 완료")
                    p = Path(str(payload.get("path") or ""))
                    self.load_precision_result(p)
                    b = self.selected_boss()
                    if b:
                        self.precision_context_key = self._boss_context_key(b)
                    if payload.get("historical"):
                        self.notebook.select(self.precision_frame)
                        self._b37509_precision_choice_changed()
                        continue
                    if b and not partial:
                        self.load_boss_dashboard_for_boss(b)
                        self.load_targets_for_boss(b)
                        # B14: 정밀 분석 완료 후 보스 분석 화면으로 이동할 때도
                        # 상위 쿨기/NSRT가 같은 보스를 가리키는지 재확인한다.
                        self.sync_cooldown_for_boss(b)
                    self.notebook.select(self.precision_frame if partial else (self.dashboard_frame if b else self.precision_frame))
                elif kind == "precision_error":
                    self._progress_end(False)
                    self.precision_running = False
                    self._b37509_precision_choice_changed()
                    self.precision_btn.configure(state="normal")
                    self.run_btn.configure(state="normal")
                    payload = data if isinstance(data, dict) else {"error": str(data), "token": None}
                    stale_result = payload.get("token") is not None and not self._result_belongs_to_current_selection(payload.get("token"))
                    self.status_var.set("정밀 분석 오류" + (" · 현재 선택 유지" if stale_result else ""))
                    if not stale_result:
                        self.notebook.select(self.log_frame)
                    messagebox.showerror("정밀 분석 오류", str(payload.get("error") or "unknown"))
                elif kind == "error":
                    self._progress_end(False)
                    self.run_btn.configure(state="normal")
                    self.status_var.set("오류")
                    messagebox.showerror("실행 오류", data)
        except queue.Empty:
            pass
        self.after(100, self._pump_queue)

    def report_dir(self) -> Path | None:
        if not self.current_dataset:
            return None
        modern = self.current_dataset / "characters" / self.current_player / "integrated_analysis"
        if modern.exists() or (self.current_dataset / "characters").exists():
            return modern
        # v1.8 이전 PRIVATE_ONLY 단일 데이터셋 하위호환
        if self.current_player == "PRIVATE_ONLY":
            return self.current_dataset / "integrated_analysis"
        return modern

    def brief_path(self) -> Path | None:
        d = self.report_dir()
        return d / "integrated_brief.txt" if d else None

    def summary_path(self) -> Path | None:
        d = self.report_dir()
        return d / "integrated_summary.txt" if d else None

    def boss_json_path(self) -> Path | None:
        d = self.report_dir()
        return d / "boss_details.json" if d else None

    def _insert_colored(self, widget: tk.Text, text: str, brief: bool = False):
        widget.delete("1.0", "end")
        for line in text.splitlines(True):
            tag = None
            s = line.strip()
            if s.startswith("===") or (s.startswith("[") and s.endswith("]")):
                tag = "header"
            elif s.startswith("⚠") or "[경고]" in line or "반복 경고:" in line or "보스별 경고:" in line:
                tag = "warn"
            elif s.startswith("△") or "[주의]" in line or "주의:" in line:
                tag = "caution"
            elif s.startswith("✓") or "[정상]" in line or "현재 정상:" in line:
                tag = "ok"
            elif "[세션 참고]" in line or s.startswith("※") or "표본부족" in line or "추가 표본" in line:
                tag = "note" if brief else "small"
            widget.insert("end", line, tag if tag else ())
        widget.see("1.0")

    def load_results(self):
        brief_p = self.brief_path()
        summary_p = self.summary_path()

        if brief_p and brief_p.exists():
            self._insert_colored(
                self.brief,
                brief_p.read_text(encoding="utf-8", errors="replace"),
                brief=True,
            )
        else:
            self.brief.delete("1.0", "end")
            self.brief.insert("end", "아직 integrated_brief.txt가 없습니다.\n분석을 다시 실행하세요.")

        if summary_p and summary_p.exists():
            self._insert_colored(
                self.result,
                summary_p.read_text(encoding="utf-8", errors="replace"),
                brief=False,
            )
        else:
            self.result.delete("1.0", "end")
            self.result.insert("end", "아직 integrated_summary.txt가 없습니다.\n분석을 먼저 실행하세요.")

        self.load_boss_details()
        self.build_coaching_view()
        if self.boss_data:
            self.notebook.select(self.dashboard_frame)
        elif brief_p and brief_p.exists():
            self.notebook.select(self.brief_frame)

    # v0.3 호환용
    def load_summary(self):
        self.load_results()

    def _clear_boss_tree(self):
        for iid in self.boss_tree.get_children():
            self.boss_tree.delete(iid)
        self.boss_data = []

    def load_boss_details(self):
        wanted_context = self._nav_context_key()
        wanted_page = str(self.nav_state.get("page") or "session")
        self._clear_boss_tree()
        p = self.boss_json_path()
        if not p or not p.exists():
            self.boss_title_var.set("보스 상세 데이터 없음")
            self.dashboard_boss_combo["values"] = []
            self.dashboard_boss_var.set("")
            self.dashboard_title_var.set("보스 상세 데이터 없음")
            self.boss_detail.delete("1.0", "end")
            self.boss_detail.insert(
                "end",
                "v0.4로 분석을 한 번 실행하면 보스 상세 데이터가 생성됩니다.",
            )
            self._refresh_shared_nav_tree()
            self._update_breadcrumb()
            return

        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            self.boss_title_var.set("보스 상세 읽기 실패")
            self.dashboard_boss_combo["values"] = []
            self.dashboard_boss_var.set("")
            self.dashboard_title_var.set("보스 상세 읽기 실패")
            self.boss_detail.delete("1.0", "end")
            self.boss_detail.insert("end", str(e))
            self._refresh_shared_nav_tree()
            self._update_breadcrumb()
            return

        self.boss_data = payload.get("bosses", [])
        self.dashboard_boss_combo["values"] = [self._boss_combo_label(b) for b in self.boss_data]
        confirm_indices = session_confirm_indices(self.boss_data)

        for idx, b in enumerate(self.boss_data):
            status = display_status_for_boss(b, idx in confirm_indices)

            self.boss_tree.insert(
                "",
                "end",
                iid=str(idx),
                text=b.get("boss", "?"),
                values=(
                    _ui_difficulty_name({"difficultyID": b.get("difficulty"), "difficultyName": b.get("difficultyName")}, f"d{b.get('difficulty', '?')}"),
                    b.get("spec", "?"),
                    status,
                ),
            )

        children = self.boss_tree.get_children()
        if children:
            restore_idx = self._find_boss_index_by_context(wanted_context) if wanted_page == "boss" else None
            target_iid = str(restore_idx) if restore_idx is not None else children[0]
            self.boss_tree.selection_set(target_iid)
            self.boss_tree.focus(target_iid)
            # Populate the detail panels, but do not let a reload steal a newer
            # session/live selection.  Only an existing boss selection owns nav.
            self.on_boss_select(sync_nav=(restore_idx is not None and wanted_page == "boss"))
        self._refresh_shared_nav_tree()
        self._update_breadcrumb()

    def build_coaching_view(self):
        self.coach.delete("1.0", "end")
        if hasattr(self, "b3794_coach_box"):
            self.b3794_coach_box.configure(text=f"오늘의 교정 · {self.current_player}" if self.current_player else "오늘의 교정")
        self._refresh_layout_session_summary()
        if not self.boss_data:
            self.coach.insert("end", "분석 후 이번 세션에서 먼저 고칠 항목을 보여줍니다.")
            return

        locked_map = collect_locked_issues(self.boss_data)
        session_map = collect_session_issues(self.boss_data)
        issue_map = merge_issue_maps(locked_map, session_map)

        self.coach.insert("end", "먼저 고칠 것\n", "title")

        ranked = []
        for idx, rows in issue_map.items():
            b = self.boss_data[idx]
            # 잠금 경고/주의를 세션 참고보다 우선
            severity = 0
            for row in rows:
                if row["source"] == "잠금 규칙":
                    if "[경고]" in row["title"]:
                        severity = max(severity, 3)
                    elif "[주의]" in row["title"]:
                        severity = max(severity, 2)
                else:
                    severity = max(severity, 1)
            ranked.append((severity, idx, rows, b))
        ranked.sort(key=lambda x: (-x[0], x[3].get("boss", "")))

        if not ranked:
            self.coach.insert(
                "end",
                "✓ 이번 세션에서 잠금 규칙상 즉시 고칠 항목이 없습니다.\n"
                "  현재 정상으로 확인된 습관을 유지하면서 다음 로그에서 같은 항목이 반복되는지만 보세요.\n\n",
                "good",
            )
        else:
            for n, (_, idx, rows, b) in enumerate(ranked, 1):
                boss = b.get("boss", "?")
                diff = _ui_difficulty_name({"difficultyID": b.get("difficulty"), "difficultyName": b.get("difficultyName")})
                spec = b.get("spec", "")
                self.coach.insert("end", f"{n}. {boss} · {diff} · {spec}\n", "issue")
                for row in rows:
                    tag = "issue" if row["source"] == "잠금 규칙" else "check"
                    self.coach.insert("end", f"   • {row['title']}\n", tag)
                    self.coach.insert("end", f"     {row['action']}\n", "action")
                self.coach.insert(
                    "end",
                    "     확인법: 다음 로그에서 같은 보스를 다시 눌러 로컬 수치가 비교군 쪽으로 개선됐는지 확인.\n\n",
                    "note",
                )

        # 세션에서 유지할 것 — B37.3.9.4: folded by default (board 1).
        self.coach.insert("end", "\n")
        keep_start = self.coach.index("end-1c")
        self.coach.insert("end", "계속 유지할 것\n", "title")

        # 7CP 정상 집계
        for target_rule, label in (
            ("7CP 마무리 — 첫 60초", "7CP 마무리"),
            ("7CP 독살 — 첫 60초", "7CP 독살"),
        ):
            rows = []
            for b in self.boss_data:
                for r in b.get("rules", []):
                    if r.get("rule") == target_rule:
                        rows.append(r)
            if rows:
                normal = sum(1 for r in rows if r.get("status") == "정상")
                if normal:
                    self.coach.insert(
                        "end",
                        f"✓ {label}: 정상 {normal}/{len(rows)}보스 — 현재 운용을 억지로 바꾸지 마세요.\n",
                        "good",
                    )

        # 쿨기 묶음은 허용범위 내 보스 수를 집계
        for target_rule, label in (
            ("심장↔어칼 첫 묶음", "심장↔어칼 첫 묶음"),
            ("어춤↔어칼 첫 묶음", "어춤↔어칼 첫 묶음"),
        ):
            vals = []
            for b in self.boss_data:
                for r in b.get("rules", []):
                    if r.get("rule") == target_rule:
                        p = parse_pair_with_allowed(r.get("detail", ""))
                        if p:
                            vals.append(p)
            if vals:
                within = sum(1 for local, _peer, allowed in vals if local <= allowed)
                if within:
                    self.coach.insert(
                        "end",
                        f"✓ {label}: {within}/{len(vals)}보스 허용범위 내 — 현재 묶는 습관 유지.\n",
                        "good",
                    )

        keep_end = self.coach.index("end-1c")
        self.coach.tag_add("keep", keep_start, keep_end)
        self.coach.tag_configure("keep", elide=not getattr(self, "b3794_keep_open", False))
        self.coach.insert("end", "\n")
        self.coach.insert("end", "계속 유지할 것 펼치기 ▸" if not getattr(self, "b3794_keep_open", False) else "계속 유지할 것 접기 ▾", "keeptoggle")
        self.coach.tag_configure("keeptoggle", foreground="#78b9eb", font=("Segoe UI", 9, "underline"))
        self.coach.tag_bind("keeptoggle", "<Button-1>", lambda _e: self._b3794_toggle_keep())
        self.coach.insert(
            "end",
            "\n\nDPS 차이는 결과값입니다. 위 행동을 먼저 교정합니다.\n"
            "※ 잠금 규칙의 경고/주의와 세션 확인 후보만 올리며, [표본부족]만 있는 항목은 문제로 판정하지 않습니다.\n",
            "note",
        )
        self.coach.see("1.0")

    def _b3794_toggle_keep(self):
        self.b3794_keep_open = not getattr(self, "b3794_keep_open", False)
        try:
            self.build_coaching_view()
        except Exception:
            pass

    @staticmethod
    def _spec_display(spec_id, fallback=""):
        names = {259: "암살", 260: "무법", 261: "잠행"}
        try:
            sid = int(spec_id) if spec_id is not None else None
        except (TypeError, ValueError):
            sid = None
        if sid in names:
            return names[sid]
        text = str(fallback or "").strip()
        if text and not text.lower().startswith("spec "):
            return text
        return f"전문화 {sid}" if sid is not None else text

    def _boss_combo_label(self, b: dict):
        # B36.3.2: keep the navigation name stable and concise.  Spec is part of
        # the context key, not the visible boss name; detailed analysis still
        # shows the specialization where it matters.
        return f"{b.get('boss', '?')} · {_ui_difficulty_name({'difficultyID': b.get('difficulty'), 'difficultyName': b.get('difficultyName')})}".rstrip(" ·")

    def _dataset_session_date(self):
        return session_date_from_log_path(self.current_log) or "분석 세션"

    def _public_auto_open_latest_session(self, rows=None):
        if not getattr(self, "raid_public_mode", False) or getattr(self, "_public_auto_session_done", False):
            return
        try:
            if str(self.nav_state.get("page") or "session") != "session" or self.nav_state.get("sessionDate"):
                self._public_auto_session_done = True
                return
            dates = []
            for row in list(rows or []) + list(getattr(self, "live_persisted_rows", []) or []):
                d = session_date_from_timestamp((row or {}).get("startTimestamp")) or str((row or {}).get("sessionDate") or "")
                if d:
                    dates.append(d)
            if not dates:
                return  # keep waiting until the first import/migration produces rows
            latest = max(dates)
            self._public_auto_session_done = True
            self._set_nav_context((None, None, latest, self.current_player, None), page="session", live_try_key=None)
            self.notebook.select(self.coach_frame)
            self._update_breadcrumb()
            self._refresh_layout_session_summary()
            try:
                self._b3799_render_session_nav(preserve_anchor=True)
            except Exception:
                pass
        except Exception:
            self._public_auto_session_done = True

    def _nav_session_date(self):
        return str(self.nav_state.get("sessionDate") or self._dataset_session_date())

    def _boss_context_key(self, b: dict):
        return make_boss_context_key(b, self._dataset_session_date(), self.current_player)

    def _live_context_key(self, row):
        if not row:
            return (None, None, "", "" if self.raid_public_mode else str(self.current_player), None)
        row_date = session_date_from_timestamp(row.get("startTimestamp")) or str(row.get("sessionDate") or "")
        if self.raid_public_mode:
            try:
                enc = int(row.get("encounterID"))
            except Exception:
                enc = None
            try:
                diff = int(row.get("difficultyID") if row.get("difficultyID") is not None else row.get("difficulty"))
            except Exception:
                diff = None
            # Public raid navigation is date -> boss -> Try. Character/spec are
            # intentionally removed from identity so historical rows captured by
            # different characters still collapse into one boss node.
            return (enc, diff, row_date, "", None)
        fallback_spec = None
        try:
            enc = int(row.get("encounterID"))
            diff = int(row.get("difficultyID"))
        except Exception:
            enc = diff = None

        # First reuse an unambiguous analyzed-dataset spec when this Live pull is
        # on the same raid night.
        if row_date and row_date == self._dataset_session_date():
            specs = {
                int(b.get("specID")) for b in self.boss_data
                if enc is not None and diff is not None
                and int(b.get("encounterID") or -1) == enc
                and int(b.get("difficulty") or -1) == diff
                and b.get("specID") is not None
            }
            if len(specs) == 1:
                fallback_spec = next(iter(specs))

        # B34.2.1: very short/aborted pulls can contain too few player events to
        # discover specID.  Do not create a second boss node just because spec is
        # unknown: if the same date/player/encounter/difficulty has exactly one
        # known Live spec, inherit that spec for navigation only.  If the player
        # genuinely swapped between two specs, ambiguity is preserved as None.
        direct_spec = row.get("factsSpecID")
        if direct_spec is None:
            direct_spec = row.get("liveSpecID") if row.get("liveSpecID") is not None else row.get("specID")
        if direct_spec is None and fallback_spec is None and enc is not None and diff is not None:
            owner = str(row.get("factsPlayer") or row.get("livePlayer") or self.current_player or "").split("-")[0]
            live_specs = set()
            for other in getattr(self, "live_session_rows", []) or []:
                try:
                    if int(other.get("encounterID")) != enc or int(other.get("difficultyID")) != diff:
                        continue
                except Exception:
                    continue
                other_date = session_date_from_timestamp(other.get("startTimestamp")) or str(other.get("sessionDate") or "")
                other_owner = str(other.get("factsPlayer") or other.get("livePlayer") or "").split("-")[0]
                if other_date != row_date or other_owner != owner:
                    continue
                spec = other.get("factsSpecID")
                if spec is None:
                    spec = other.get("liveSpecID") if other.get("liveSpecID") is not None else other.get("specID")
                try:
                    if spec is not None:
                        live_specs.add(int(spec))
                except Exception:
                    pass
            if len(live_specs) == 1:
                fallback_spec = next(iter(live_specs))
        return make_live_context_key(row, fallback_player=self.current_player, fallback_spec_id=fallback_spec)

    def _live_boss_group_key(self, row):
        """Boss/date/player identity for presentation, intentionally spec-agnostic.

        The analysis context keeps specID because personal rotation views need it,
        but the raid navigator is date -> boss -> Try.  Unknown/late spec enrichment
        must not split one boss into several identical visible headings.
        """
        ctx = self._live_context_key(row)
        diff = normalize_difficulty(ctx[1])
        return (ctx[0], diff, ctx[2], ctx[3])

    def _nav_context_key(self):
        return context_tuple_from_state(self.nav_state)

    def _set_nav_context(self, key, *, page=None, live_try_key=None):
        self.nav_state.update(selection_context_dict(key))
        if page is not None:
            self.nav_state["page"] = page
        self.nav_state["liveTryKey"] = live_try_key
        # B37.3.9.14: shared navigation owns the active Try.  Once the user
        # leaves a Try for a boss/session, an old legacy-recent selection must
        # not remain eligible to pull the UI back on the next live refresh.
        if hasattr(self, "live_selected_key"):
            self.live_selected_key = live_try_key if page == "live_try" else None

    def _selection_token(self):
        return (
            str(self.current_dataset or ""),
            str(self.current_log or ""),
            str(self.current_player or ""),
            str(self.nav_state.get("page") or ""),
            self._nav_context_key(),
            self.nav_state.get("liveTryKey"),
        )

    def _result_belongs_to_current_selection(self, token) -> bool:
        return selection_tokens_match(token, self._selection_token())

    def _live_row_key(self, row):
        if not row:
            return None
        return (
            *self._live_context_key(row),
            row.get("tryNo"),
            row.get("startTimestamp"),
        )

    @staticmethod
    def _same_live_try_key(left, right) -> bool:
        """Match the same stored Try while allowing async spec enrichment.

        factsSpecID can appear only after the background raid-facts job finishes.
        The Try identity itself is still stable by encounter/difficulty/date/player/
        Try/start timestamp, so migrate the selected key instead of dropping the
        user's selection when spec None becomes a concrete value.
        """
        if left is None or right is None:
            return False
        try:
            a, b = tuple(left), tuple(right)
            if len(a) < 7 or len(b) < 7:
                return a == b
            # encounter, difficulty, date, player, tryNo, startTimestamp
            stable_a = (a[0], a[1], a[2], a[3], a[5], a[6])
            stable_b = (b[0], b[1], b[2], b[3], b[5], b[6])
            if stable_a != stable_b:
                return False
            # A known spec must never silently cross into another known spec.
            return a[4] is None or b[4] is None or a[4] == b[4]
        except Exception:
            return left == right

    def _find_boss_index_by_context(self, key):
        if not key:
            return None
        for idx, b in enumerate(self.boss_data):
            if self._boss_context_key(b) == tuple(key):
                return idx
        return None

    def _find_boss_index_for_live_row(self, row):
        return self._find_boss_index_by_context(self._live_context_key(row))

    def _switch_to_saved_player(self, player: str):
        player = str(player or "").strip()
        if player not in CHARACTERS or player == self.current_player:
            return
        self.player_var.set(player)
        self.on_player_changed()
        # on_player_changed also tries to bind a WoWCombatLog.  Persisted Live
        # sessions remain usable even when that local Logs folder is absent.
        self.after(50, lambda: self._refresh_live_recent(force_storage_reload=True))

    def _refresh_nav_empty_hint(self, visible_rows=None):
        if "nav_empty_hint_var" not in self.__dict__:
            return
        if self.raid_public_mode:
            self.nav_empty_hint_var.set("")
            self.nav_empty_hint.pack_forget()
            self.nav_empty_actions.pack_forget()
            return
        visible_rows = list(visible_rows if visible_rows is not None else self._all_live_rows())
        for child in self.nav_empty_actions.winfo_children():
            child.destroy()
        self.nav_empty_hint.pack_forget()
        self.nav_empty_actions.pack_forget()
        if visible_rows:
            self.nav_empty_hint_var.set("")
            return

        all_rows = list(getattr(self, "live_session_rows", []) or [])
        if not all_rows:
            return
        wanted_date = self._nav_session_date()
        scoped = [
            r for r in all_rows
            if (session_date_from_timestamp(r.get("startTimestamp")) or r.get("sessionDate") or "날짜 미상") == wanted_date
        ]
        if not scoped:
            scoped = all_rows

        counts = {}
        for row in scoped:
            owner = str(row.get("factsPlayer") or row.get("livePlayer") or "").strip()
            if owner and owner != self.current_player:
                counts[owner] = counts.get(owner, 0) + 1
        if not counts:
            return

        parts = [f"{name} {count}" for name, count in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
        self.nav_empty_hint_var.set(
            f"{self.current_player} 기록이 없습니다. 다른 캐릭터 기록: " + " · ".join(parts)
        )
        self.nav_empty_hint.pack(fill="x", pady=_px((6, 2)), before=self.progress.master if hasattr(self, "progress") else None)
        self.nav_empty_actions.pack(fill="x", pady=_px((0, 5)), before=self.progress.master if hasattr(self, "progress") else None)
        for name, count in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            _round_button(
                self.nav_empty_actions,
                text=f"{name} {count}",
                command=lambda p=name: self._switch_to_saved_player(p),
            ).pack(side="left", padx=_px((0, 4)))

    def _b3798_nav_visible_items(self):
        out = []
        t = self.nav_tree

        def walk(parent):
            for iid in t.get_children(parent):
                out.append(iid)
                if t.item(iid, "open"):
                    walk(iid)
        walk("")
        return out

    def _b3798_nav_first_visible(self, vis=None):
        vis = vis if vis is not None else self._b3798_nav_visible_items()
        try:
            top = self.nav_tree.identify_row(2)
            if top:
                return top
        except Exception:
            pass
        frac = (self.nav_tree.yview() or (0.0,))[0]
        return vis[int(round(frac * len(vis)))] if vis else None

    def _refresh_shared_nav_tree(self, live_rows=None):
        if not hasattr(self, "nav_tree"):
            return
        if live_rows is None:
            live_rows = self._all_live_rows()
        self._refresh_nav_empty_hint(live_rows)

        # B34.1.3: passive refreshes must not steal the user's scroll position.
        # Keep both the viewport and the logical selection before rebuilding.
        try:
            old_yview = self.nav_tree.yview()
        except Exception:
            old_yview = ()
        old_selection = self.nav_tree.selection()
        old_payload = self.nav_tree_payload.get(old_selection[0]) if old_selection else None
        # B37.3.9.8: anchor the viewport to the selected row's screen position,
        # not to a scroll fraction — a live pull added above would shift it.
        old_sel_row_offset = None
        if old_selection:
            try:
                vis = self._b3798_nav_visible_items()
                first = self._b3798_nav_first_visible(vis)
                if old_selection[0] in vis and first in vis:
                    old_sel_row_offset = vis.index(old_selection[0]) - vis.index(first)
            except Exception:
                old_sel_row_offset = None

        boss_sig = tuple((self._boss_context_key(b), b.get("boss"), b.get("difficultyName")) for b in self.boss_data)
        live_sig = tuple(
            (self._live_row_key(r), r.get("result"), r.get("resetPull"), r.get("analysisExcluded"), r.get("durationSec"), r.get("analysisOverride"), r.get("factsPlayer"), r.get("factsSpecID"), r.get("startTimestamp"))
            for r in (live_rows or [])
        )
        signature = (boss_sig, live_sig)
        tree_rebuilt = signature != getattr(self, "_shared_nav_signature", None)
        if tree_rebuilt:
            self._shared_nav_signature = signature
            for iid in self.nav_tree.get_children():
                self.nav_tree.delete(iid)
            self.nav_tree_payload = {}

            dates = {self._dataset_session_date()} if self.boss_data else set()
            for row in live_rows or []:
                dates.add(session_date_from_timestamp(row.get("startTimestamp")) or "날짜 미상")
            if not dates:
                dates.add(self._dataset_session_date())
            date_nodes = {}
            for n, date in enumerate(sorted(dates, reverse=True)):
                iid = f"nav-date-{n}"
                self.nav_tree.insert("", "end", iid=iid, text=date, open=True)
                self.nav_tree_payload[iid] = ("session", date)
                date_nodes[date] = iid

            boss_nodes = {}
            for idx, b in enumerate(self.boss_data):
                key = self._boss_context_key(b)
                date = key[2] or self._dataset_session_date()
                parent = date_nodes.get(date)
                if parent is None:
                    continue
                iid = f"nav-boss-{idx}"
                label = f"{b.get('boss','?')} · {_ui_difficulty_name({'difficultyID': b.get('difficulty'), 'difficultyName': b.get('difficultyName')})}".rstrip(" ·")
                self.nav_tree.insert(parent, "end", iid=iid, text=label, open=True)
                self.nav_tree_payload[iid] = ("boss", key)
                boss_nodes[key] = iid

            duplicate_try_counts = {}
            for _row in (live_rows or []):
                _ctx = self._live_context_key(_row)
                _dup_key = (_ctx[:4], _row.get("tryNo"))
                duplicate_try_counts[_dup_key] = duplicate_try_counts.get(_dup_key, 0) + 1

            orphan_nodes = {}
            for n, row in enumerate(live_rows or []):
                context_key = self._live_context_key(row)
                date = context_key[2] or "날짜 미상"
                parent = boss_nodes.get(context_key)
                if parent is None:
                    orphan_key = (*context_key, row.get("name"))
                    parent = orphan_nodes.get(orphan_key)
                    if parent is None:
                        date_parent = date_nodes.get(date)
                        if date_parent is None:
                            continue
                        parent = f"nav-liveboss-{len(orphan_nodes)}"
                        self.nav_tree.insert(
                            date_parent, "end", iid=parent,
                            text=f"{row.get('name','?')} · {_ui_difficulty_name(row)}".rstrip(" ·"), open=True,
                        )
                        self.nav_tree_payload[parent] = ("live_boss", context_key)
                        orphan_nodes[orphan_key] = parent
                is_fake = self._live_row_is_fake(row)
                is_reset = self._live_row_is_reset(row)
                is_early = self._live_row_is_early_wipe(row)
                if row.get("analysisOverride") == "normal":
                    is_reset = False
                    is_fake = False
                    is_early = self._live_row_is_early_wipe(row)
                result = _ui_result_en(row.get("result"), fake=is_fake, reset=is_reset, early=is_early)
                key = self._live_row_key(row)
                iid = f"nav-live-{n}"
                duplicate = duplicate_try_counts.get((context_key[:4], row.get("tryNo")), 0) > 1
                clock = f" · {self._live_start_clock(row)}" if duplicate else ""
                tags = ("excluded",) if (is_reset or is_fake) else ()
                self.nav_tree.insert(
                    parent, "end", iid=iid, tags=tags, image=self._b3797_dot(result),
                    text=f"Try {row.get('tryNo','?')}{clock} · {result} · {self._fmt_live_duration(row.get('durationSec'))}",
                )
                self.nav_tree_payload[iid] = ("live_try", key)

        target = None
        if self.nav_state.get("page") == "live_try" and self.nav_state.get("liveTryKey") is not None:
            wanted = self.nav_state.get("liveTryKey")
            for iid, payload in self.nav_tree_payload.items():
                if payload == ("live_try", wanted):
                    target = iid
                    break
        elif self.nav_state.get("encounterID") is not None:
            wanted = self._nav_context_key()
            for iid, payload in self.nav_tree_payload.items():
                if payload == ("boss", wanted):
                    target = iid
                    break
        else:
            wanted_date = self._nav_session_date()
            for iid, payload in self.nav_tree_payload.items():
                if payload == ("session", wanted_date):
                    target = iid
                    break

        if target:
            target_payload = self.nav_tree_payload.get(target)
            current_selection = self.nav_tree.selection()
            current = current_selection[0] if current_selection else None
            logical_selection_unchanged = old_payload is not None and old_payload == target_payload

            self._nav_syncing = True
            try:
                if current != target:
                    self.nav_tree.selection_set(target)
                    self.nav_tree.focus(target)

                if tree_rebuilt and logical_selection_unchanged and old_sel_row_offset is not None:
                    # Keep the selected Try at the same row on screen.
                    try:
                        vis = self._b3798_nav_visible_items()
                        idx = vis.index(target)
                        top = max(0, idx - old_sel_row_offset)
                        self.nav_tree.yview_moveto(top / max(1, len(vis)))
                    except Exception:
                        if old_yview:
                            self.nav_tree.yview_moveto(old_yview[0])
                elif tree_rebuilt and logical_selection_unchanged and old_yview:
                    # A background/live refresh rebuilt the tree. Restore where the
                    # user was reading instead of scrolling back to the selected Try.
                    self.nav_tree.yview_moveto(old_yview[0])
                elif current != target and not logical_selection_unchanged:
                    # Only an actual navigation change should bring the target into view.
                    self.nav_tree.see(target)
            finally:
                self._nav_syncing = False

        # Visible board-1 session explorer.  Preserve the selected row's screen
        # position across live refreshes just like B37.3.9.8 did for Treeview.
        try:
            self._b3799_render_session_nav(preserve_anchor=tree_rebuilt)
        except Exception:
            pass

    def _show_live_boss_dashboard(self, context_key):
        """Render a stored-Live boss even when no WCL dataset is loaded."""
        context_key = tuple(context_key or ())
        rows = [r for r in self._all_live_rows() if self._live_context_key(r) == context_key]
        rows.sort(key=self._live_row_sort_key)
        row = rows[-1] if rows else {}
        label = f"{row.get('name','저장 보스')} · {_ui_difficulty_name(row)}".rstrip(" ·")
        self.dashboard_boss_var.set(label)
        self.dashboard_title_var.set(label)
        try:
            self.dashboard_boss_combo.configure(state="disabled")
            self.dashboard_precision_btn.configure(state="disabled")
            self.dashboard_cooldown_btn.configure(state="disabled")
        except Exception:
            pass

        for iid in self.dashboard_table.get_children():
            self.dashboard_table.delete(iid)
        kills = sum(1 for r in rows if str(r.get("result") or "").upper() == "KILL")
        excluded = sum(1 for r in rows if self._live_row_is_fake(r) or self._live_row_is_reset(r))
        self.dashboard_table.insert("", "end", text="저장 트라이", values=(str(len(rows)), "WCL 데이터 없음"))
        self.dashboard_table.insert("", "end", text="킬", values=(str(kills), "-"))
        self.dashboard_table.insert("", "end", text="리셋/가짜 풀", values=(str(excluded), "-"))
        self.dashboard_diff.delete("1.0", "end")
        self.dashboard_diff.insert(
            "end",
            "저장된 Live 트라이 기준 보스입니다. 왼쪽 트라이 진행에서 복기를 열 수 있습니다.\n"
            "WCL 분석 데이터셋을 불러오면 상위30/쿨기 비교 카드가 같은 보스 컨텍스트에 연결됩니다.",
            "note",
        )
        self.dashboard_note_var.set("저장 Live 세션 기준")
        self._refresh_boss_try_progress()
        self._refresh_context_tabs()

    def _on_nav_tree_selected(self, _event=None):
        if self._nav_syncing or not hasattr(self, "nav_tree"):
            return
        sel = self.nav_tree.selection()
        if not sel:
            return
        kind, value = self.nav_tree_payload.get(sel[0], (None, None))
        # B37.3.9.8: Treeview delivers <<TreeviewSelect>> asynchronously, so a
        # background/live rebuild that restores the current selection arrives
        # here after _nav_syncing is cleared.  Re-selecting what is already shown
        # must not re-render (flicker) or re-navigate (jump).
        page = self.nav_state.get("page")
        if kind == "live_try" and page == "live_try" and self._same_live_try_key(value, self.nav_state.get("liveTryKey")):
            return
        if kind == "session" and page == "session" and value == self._nav_session_date():
            return
        if kind == "live_boss" and page == "boss" and tuple(value or ()) == tuple(self._nav_context_key() or ()):
            return
        if kind == "session":
            self._set_nav_context((None, None, value or "", self.current_player, None), page="session", live_try_key=None)
            self.notebook.select(self.coach_frame)
            self._update_breadcrumb()
            try:
                self._refresh_layout_session_summary()
            except Exception:
                pass
            return
        if kind == "boss":
            self._select_shared_boss(value, select_page=True)
            return
        if kind == "live_boss":
            self._set_nav_context(tuple(value), page="boss", live_try_key=None)
            idx = self._find_boss_index_by_context(tuple(value))
            if idx is not None:
                self._select_shared_boss(tuple(value), select_page=True)
            else:
                # Saved live-only bosses still open the same compact Try board.
                self._refresh_b3641_boss_views()
                self._show_main_frame(self.boss_board_frame)
                self._update_breadcrumb()
                self._refresh_shared_nav_tree()
            return
        if kind == "live_try":
            self._select_live_row_by_key(value)

    def _select_shared_boss(self, boss_ref, select_page=False):
        if isinstance(boss_ref, (tuple, list)):
            idx = self._find_boss_index_by_context(tuple(boss_ref))
        else:
            try:
                idx = int(boss_ref)
            except Exception:
                idx = None
        if idx is None or idx < 0 or idx >= len(self.boss_data):
            return
        b = self.boss_data[idx]
        self._set_nav_context(self._boss_context_key(b), page="boss", live_try_key=None)
        iid = str(idx)
        self._nav_syncing = True
        try:
            if iid in self.boss_tree.get_children():
                self.boss_tree.selection_set(iid)
                self.boss_tree.focus(iid)
                self.boss_tree.see(iid)
        finally:
            self._nav_syncing = False
        self.on_boss_select(_b38_allow_skip=True)
        self._refresh_boss_try_progress()
        self._refresh_b3641_boss_views()
        if select_page:
            # B37.3.9: every boss lands on the compact Try board first.
            self.notebook.select(self.boss_board_frame)
        self._update_breadcrumb()
        self._refresh_shared_nav_tree()
        self._refresh_context_tabs()

    def _update_breadcrumb(self):
        if not hasattr(self, "breadcrumb_var"):
            return
        parts = [self._nav_session_date()]
        row = self._selected_live_row() if hasattr(self, "live_recent_tree") else None
        if self.nav_state.get("page") == "live_try" and row and self._live_row_key(row) == self.nav_state.get("liveTryKey"):
            parts.append(f"{row.get('name','?')} · {_ui_difficulty_name(row)}")
            parts.append(f"Try {row.get('tryNo','?')}")
        elif self.nav_state.get("encounterID") is not None:
            idx = self._find_boss_index_by_context(self._nav_context_key())
            if idx is not None:
                b = self.boss_data[idx]
                parts.append(f"{b.get('boss','?')} · {_ui_difficulty_name({'difficultyID': b.get('difficulty'), 'difficultyName': b.get('difficultyName')})}".rstrip(" ·"))
            else:
                for live_row in self._all_live_rows():
                    if self._live_context_key(live_row) == self._nav_context_key():
                        parts.append(f"{live_row.get('name','?')} · {_ui_difficulty_name(live_row)}".rstrip(" ·"))
                        break
        self.breadcrumb_var.set("  /  ".join(parts))
        if hasattr(self, "nav_back_btn"):
            self.nav_back_btn.configure(state="normal" if len(parts) > 1 else "disabled")
        self._refresh_context_tabs()

    def _navigate_back_shared(self):
        page = self.nav_state.get("page")
        if page == "live_try":
            row = self._selected_live_row()
            idx = self._find_boss_index_for_live_row(row)
            if idx is not None:
                self._select_shared_boss(self._boss_context_key(self.boss_data[idx]), select_page=True)
            else:
                if row:
                    context_key = self._live_context_key(row)
                    self._set_nav_context(context_key, page="boss", live_try_key=None)
                    self._refresh_b3641_boss_views()
                    self._show_main_frame(self.boss_board_frame)
                    self._update_breadcrumb()
                    self._refresh_shared_nav_tree()
                else:
                    date = self._nav_session_date()
                    self._set_nav_context((None, None, date, self.current_player, None), page="session", live_try_key=None)
                    self.notebook.select(self.coach_frame)
                    self._update_breadcrumb()
                    self._refresh_shared_nav_tree()
            return
        date = self._nav_session_date()
        self._set_nav_context((None, None, date, self.current_player, None), page="session", live_try_key=None)
        self.notebook.select(self.coach_frame)
        self._update_breadcrumb()
        self._refresh_shared_nav_tree()

    def on_dashboard_boss_changed(self, _event=None):
        idx = self.dashboard_boss_combo.current()
        if idx < 0 or idx >= len(self.boss_data):
            return
        self._select_shared_boss(idx, select_page=True)

    def _player_data_file(self, boss_root: Path, filename: str) -> Path | None:
        exact = boss_root / "players" / self.current_player / filename
        if exact.exists():
            return exact
        players = boss_root / "players"
        if not players.exists():
            return None
        hits = sorted(players.glob(f"*/{filename}"))
        return hits[0] if hits else None

    def _local_dashboard_metrics(self, boss_root: Path, spec_id: int):
        fight = _select_local_fight_row(boss_root)
        if not fight:
            return {}
        fight_no = int(fight["fightNo"])
        duration = float(fight["durationSec"])
        opener_sid = 185438 if spec_id == 261 else 703
        cooldown_sid = 185313 if spec_id == 261 else 360194
        cooldown_label = "어춤" if spec_id == 261 else "죽표"

        opener = None
        cooldown_count = 0
        casts_path = self._player_data_file(boss_root, "casts.csv")

        def _cast_metrics():
            opener = None
            cooldown_count = 0
            try:
                with casts_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
                    for r in csv.DictReader(f):
                        try:
                            if int(float(r.get("fightNo") or -1)) != fight_no:
                                continue
                        except Exception:
                            continue
                        if "source" not in str(r.get("relation") or ""):
                            continue
                        if r.get("event") != "SPELL_CAST_SUCCESS":
                            continue
                        try:
                            sid = int(float(r.get("spellID") or 0))
                            t = float(r.get("tMs") or 0) / 1000.0
                        except Exception:
                            continue
                        if sid == opener_sid and (opener is None or t < opener):
                            opener = t
                        if sid == cooldown_sid and 0.0 <= t <= 60.0:
                            cooldown_count += 1
            except Exception:
                pass
            return opener, cooldown_count

        if casts_path and casts_path.exists():
            opener, cooldown_count = _b38_csv_cached(
                casts_path, ("dash_casts", fight_no, opener_sid, cooldown_sid), _cast_metrics)

        total_damage = 0.0
        damage_path = self._player_data_file(boss_root, "damage_done.csv")

        def _damage_total():
            total_damage = 0.0
            try:
                with damage_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
                    for r in csv.DictReader(f):
                        try:
                            if int(float(r.get("fightNo") or -1)) != fight_no:
                                continue
                        except Exception:
                            continue
                        if "source" not in str(r.get("relation") or ""):
                            continue
                        total_damage += _decode_local_damage_amount(r.get("extras"))
            except Exception:
                pass
            return total_damage

        if damage_path and damage_path.exists():
            total_damage = _b38_csv_cached(damage_path, ("dash_damage", fight_no), _damage_total)

        return {
            "fightNo": fight_no,
            "duration": duration,
            "outcome": "킬" if int(fight.get("success") or 0) == 1 else "와이프",
            "dps": (total_damage / duration) if duration > 0 and total_damage > 0 else None,
            "opener": opener,
            "cooldownCount": cooldown_count,
            "cooldownLabel": cooldown_label,
            "openerLabel": "그림자 일격" if spec_id == 261 else "목조르기",
        }

    # ---- B31.2 shared comparator cache ------------------------------------------
    # Comparator ZIPs used to live only inside each combatlog_dataset_* folder, so
    # deleting an old dataset threw away expensive WCL downloads.  New downloads go
    # to data/<kind>/spec_<id>/ (shared by every dataset); legacy per-dataset
    # caches are still found first so nothing is fetched twice.
    def _comparator_roots(self, kind: str, sid: int, dataset: Path | None = None):
        roots = []
        source_dataset = dataset if dataset is not None else self.current_dataset
        if source_dataset:
            roots.append(Path(source_dataset) / kind / f"spec_{sid}")
        roots.append(WORKDIR / "data" / kind / f"spec_{sid}")
        return roots

    def _comparator_base(self, kind: str, sid: int, stem: str, dataset: Path | None = None) -> Path:
        roots = self._comparator_roots(kind, sid, dataset=dataset)
        for root in roots:
            if Path(str(root / stem) + ".zip").exists():
                return root / stem
        shared = roots[-1]
        shared.mkdir(parents=True, exist_ok=True)
        return shared / stem

    def _comparator_zip_for_boss(self, b: dict):
        if not self.current_dataset:
            return None, None
        sid = int(b.get("specID") or 0)
        encounter = int(b.get("encounterID") or 0)
        diff = int(b.get("difficulty") or 0)

        for precision_root in self._comparator_roots("precision_cache", sid):
            if precision_root.exists():
                hits = sorted(precision_root.glob(f"{encounter}_*_d{diff}_top50_nonCNTW_v22phase.zip"))
                if hits:
                    return hits[-1], "top30"

        for basic_root in self._comparator_roots("wcl_cache", sid):
            if basic_root.exists():
                hits = sorted(basic_root.glob(f"{encounter}_*_d{diff}.zip"))
                if hits:
                    return hits[-1], "basic"
        return None, None

    def _peer_dashboard_metrics(self, zip_path: Path | None, spec_id: int):
        if not zip_path or not zip_path.exists():
            return {}
        opener_sid = 185438 if spec_id == 261 else 703
        cooldown_sid = 185313 if spec_id == 261 else 360194
        rows = []
        try:
            with zipfile.ZipFile(zip_path) as zf:
                meta_names = sorted(n for n in zf.namelist() if n.endswith("/meta.json"))
                for mn in meta_names:
                    try:
                        meta = json.loads(zf.read(mn).decode("utf-8"))
                    except Exception:
                        continue
                    fight = meta.get("fight") or {}
                    start = fight.get("startTime")
                    duration = meta.get("durationSec")
                    if duration is None:
                        duration = meta.get("rankingDuration")
                        try:
                            duration = float(duration) / 1000.0 if float(duration) > 10000 else float(duration)
                        except Exception:
                            duration = None
                    folder = mn.rsplit("/", 1)[0]
                    rank_row = {}
                    rank_name = folder + "/ranking_row.json"
                    if rank_name in zf.namelist():
                        try:
                            rank_row = json.loads(zf.read(rank_name).decode("utf-8"))
                        except Exception:
                            rank_row = {}
                    dps = meta.get("rankingAmount")
                    if dps is None:
                        dps = rank_row.get("amount")
                    if duration is None:
                        raw_dur = rank_row.get("duration")
                        try:
                            duration = float(raw_dur) / 1000.0 if float(raw_dur) > 10000 else float(raw_dur)
                        except Exception:
                            pass
                    opener = None
                    cd_count = None
                    casts_name = folder + "/casts.jsonl"
                    if casts_name in zf.namelist() and start is not None:
                        cd_count = 0
                        try:
                            raw = zf.read(casts_name).decode("utf-8", errors="replace")
                            for line in raw.splitlines():
                                if not line.strip():
                                    continue
                                try:
                                    e = json.loads(line)
                                except Exception:
                                    continue
                                try:
                                    sid = int(e.get("abilityGameID") or 0)
                                    ts = float(e.get("timestamp"))
                                except Exception:
                                    continue
                                if sid == opener_sid:
                                    sec = (ts - float(start)) / 1000.0
                                    if opener is None or sec < opener:
                                        opener = sec
                                if sid == cooldown_sid:
                                    sec = (ts - float(start)) / 1000.0
                                    if 0.0 <= sec <= 60.0:
                                        cd_count += 1
                        except Exception:
                            pass
                    rows.append({"dps": dps, "duration": duration, "opener": opener, "cooldownCount": cd_count})
        except Exception:
            return {}
        if not rows:
            return {}
        return {
            "n": len(rows),
            "dps": _median(r.get("dps") for r in rows),
            "duration": _median(r.get("duration") for r in rows),
            "opener": _median(r.get("opener") for r in rows),
            "cooldownCount": _median(r.get("cooldownCount") for r in rows),
        }

    def _precision_switch_metric(self, boss_root: Path):
        p = boss_root / "analysis" / self.current_player / "precision_adds" / "precision_results.json"
        if not p.exists():
            return None
        data = _read_json_file(p)
        if not isinstance(data, dict):
            return None
        best = None
        for row in data.get("results") or []:
            local = row.get("local") or {}
            peer = row.get("peer") or {}
            try:
                lv = float(local.get("switchDelay")) / 1000.0
                pv = float(peer.get("switchDelay")) / 1000.0
            except Exception:
                continue
            delta = lv - pv
            cand = {"label": row.get("label") or "쫄/보조 타깃", "local": lv, "peer": pv, "delta": delta}
            if best is None or delta > best["delta"]:
                best = cand
        return best

    @staticmethod
    def _fmt_action_time(sec):
        if sec is None:
            return "-"
        sec = float(sec)
        return f"{int(sec//60)}:{sec%60:04.1f}"

    def _dashboard_plan_vs_actual(self, boss_root: Path):
        """Build the same representative-vs-actual comparison used by cooldown UI.

        This reads the selected boss' own cooldown result directly, so the Action
        Summary can never accidentally reuse the previous boss context.
        """
        if compare_plan_vs_actual is None:
            return None
        p = self.cooldown_result_path(boss_root)
        if not p.exists():
            return None
        data = _read_json_file(p)
        if not isinstance(data, dict):
            return None
        plan = data.get("representative") or None
        saved_plan = self._read_saved_cooldown_plan()
        if saved_plan:
            wanted_folder = str(saved_plan.get("folder") or "")
            wanted_label = str(saved_plan.get("label") or "")
            candidates = [data.get("representative") or {}] + list(data.get("peers") or [])
            for candidate in candidates:
                if not candidate:
                    continue
                if wanted_folder and str(candidate.get("folder") or "") == wanted_folder:
                    plan = candidate
                    break
                if wanted_label and str(candidate.get("label") or "") == wanted_label:
                    plan = candidate
                    break
        if not plan:
            return None
        selected_source = (saved_plan or {}).get("selectedSpellIDs") or data.get("defaultSelectedSpellIDs") or []
        selected = {int(x) for x in selected_source}
        selected_real = {int(x) for x in (data.get("defaultExpandedSpellIDs") or [])}
        if not selected_real:
            selected_real = set(selected)
        live = data.get("liveActual") or None
        actual = None
        if live:
            tracked = {int(x) for x in (live.get("trackedSpellIDs") or [])}
            if not selected_real or selected_real.issubset(tracked):
                actual = live
        if actual is None:
            actual = data.get("local") or None
        if not actual:
            return None
        spell_names = {
            int(x.get("spellID")): x.get("spell")
            for x in (data.get("trackedSpells") or [])
            if x.get("spellID") is not None
        }
        spell_kinds = {
            int(x.get("spellID")): str(x.get("kind") or "spell")
            for x in (data.get("trackedSpells") or [])
            if x.get("spellID") is not None
        }
        for e in (actual.get("timeline") or []):
            if e.get("spellID") is not None and e.get("spell"):
                spell_names[int(e.get("spellID"))] = e.get("spell")
        try:
            return compare_plan_vs_actual(
                plan, actual,
                phase_policy=data.get("phasePolicy") or {},
                selected_ids=selected or None,
                spell_names=spell_names,
                spell_kinds=spell_kinds,
                consensus_clusters=data.get("consensusClusters") or [],
                peers=data.get("peers") or [],
            )
        except Exception:
            return None

    def _dashboard_action_summary(self, boss_root: Path, b: dict, local: dict, peer: dict):
        """Merge actionable findings from precision, burst and Plan-vs-Actual.

        Result metrics such as DPS are deliberately not primary causes.  The
        ranking favors lost cooldown uses, then concrete timing/target-switch
        behavior.  Charge-based Shadow Dance-only incidents are already
        de-emphasized by the Plan-vs-Actual incident core.
        """
        items = []

        # 1) Plan vs Actual burst incidents. Loss of an actual cast outranks
        # simple timing drift. Incident grouping is done in engine.plan_vs_actual.
        pva = self._dashboard_plan_vs_actual(boss_root)
        if pva:
            for inc in (pva.get("incidents") or [])[:10]:
                if not bool(inc.get("mainActionEligible")):
                    continue
                loss = int(inc.get("lossCount") or 0)
                raw = float(inc.get("score") or 0.0)
                confidence = inc.get("confidence") or "확인 필요"
                if inc.get("incidentType") == "personal_death_chain":
                    priority = 1500.0
                elif loss > 0:
                    priority = 1200.0 + min(250.0, max(0.0, raw - 1000.0))
                elif int(inc.get("timingCheckCount") or 0) <= 0:
                    # EXTRA-only incident. Especially common for charge skills;
                    # keep it out of the main 1~3 unless there is nothing else.
                    priority = 250.0 + min(80.0, raw)
                    confidence = inc.get("confidence") or "확인 필요"
                elif int(inc.get("primarySpellID") or -1) == 185313:
                    # Shadow Dance is charge-based and intentionally noisy at
                    # row level. A standalone Dance timing event is secondary.
                    priority = 450.0 + min(120.0, raw)
                    confidence = inc.get("confidence") or "확인 필요"
                else:
                    # Timing only: meaningful long-CD incident, but below a lost
                    # cast and usually below a very clear target-switch issue.
                    priority = 700.0 + min(180.0, raw)
                    confidence = inc.get("confidence") or "확신 높음"
                items.append({
                    "priority": priority,
                    "kind": "cooldown_incident",
                    "title": inc.get("title") or "쿨기 버스트 타이밍 확인",
                    "evidence": (
                        f"Plan vs Actual · 실제 버스트 {self._fmt_action_time(inc.get('actualTime'))}"
                        if inc.get("actualTime") is not None else
                        f"Plan vs Actual · 계획 {self._fmt_action_time(inc.get('planTime'))}"
                    ),
                    "action": inc.get("action") or "다음 풀에서 계획 버스트 위치를 다시 맞추세요.",
                    "confidence": confidence,
                })

        # 2) Add/wave precision findings already carry a tested evidence/action.
        precision_p = boss_root / "analysis" / self.current_player / "precision_adds" / "precision_results.json"
        precision = _read_json_file(precision_p) if precision_p.exists() else None
        if isinstance(precision, dict):
            for row in (precision.get("topIssues") or [])[:5]:
                title = str(row.get("title") or "웨이브 행동 차이")
                wave = str(row.get("wave") or "").strip()
                evidence = str(row.get("evidence") or "").strip()
                action = str(row.get("action") or "").strip()
                score = float(row.get("score") or 0.0)
                items.append({
                    "priority": 850.0 + score,
                    "kind": "precision",
                    "title": f"{wave} · {title}" if wave else title,
                    "evidence": evidence,
                    "action": action,
                    "confidence": "확신 높음" if score >= 80 else "확신 중간",
                })

            # Main-target share: use the named boss when possible, otherwise the
            # dominant >50% peer target. This is evidence of target allocation,
            # not a raw-DPS verdict.
            target_rows = ((precision.get("targetShareSummary") or {}).get("rows") or [])
            boss_name = str(b.get("boss") or "").lower()
            candidates = []
            for tr in target_rows:
                ps = tr.get("peerMedianShare")
                ls = tr.get("localShare")
                if ps is None or ls is None:
                    continue
                name = str(tr.get("name") or "")
                name_match = bool(boss_name and (boss_name in name.lower() or name.lower() in boss_name))
                if name_match or float(ps) >= 0.50:
                    candidates.append((1 if name_match else 0, float(ps), tr))
            if candidates:
                _nm, _ps, tr = max(candidates, key=lambda x: (x[0], x[1]))
                ls = float(tr.get("localShare") or 0.0)
                ps = float(tr.get("peerMedianShare") or 0.0)
                gap = ps - ls
                hits = int(tr.get("peerHitCount") or 0)
                total_n = int(tr.get("peerCount") or 0)
                if gap >= 0.05 and hits >= 8:
                    items.append({
                        "priority": 740.0 + gap * 500.0,
                        "kind": "target_share",
                        "title": f"주요 대상 딜 비중 부족 · {gap*100:.1f}%p 낮음",
                        "evidence": f"나 {ls*100:.1f}% / 상위 중앙 {ps*100:.1f}% · 표본 {hits}/{total_n}",
                        "action": "웨이브 처리 뒤 주요 대상으로 복귀가 늦거나 보조대상에 불필요하게 남는 구간이 있는지 확인하세요.",
                        "confidence": "확신 중간",
                    })

        # 3) Burst-window local state. These are local heuristics, so present them
        # as confirmation-required rather than hard conclusions.
        burst_p = boss_root / "analysis" / self.current_player / "burst_windows" / "burst_windows.json"
        burst = _read_json_file(burst_p) if burst_p.exists() else None
        if isinstance(burst, dict):
            flagged = [w for w in (burst.get("windows") or []) if w.get("flags")]
            for w in flagged[:2]:
                flags = [str(x) for x in (w.get("flags") or [])]
                # Dot-state / missing major-CD flags are much more actionable than
                # a small resource-overcap warning.
                important = [x for x in flags if ("미활성" in x or "없음" in x)] or flags
                if not important:
                    continue
                start = float(w.get("start") or 0.0)
                actions = [str(x) for x in (w.get("actions") or [])]
                items.append({
                    "priority": 660.0,
                    "kind": "burst_state",
                    "title": f"{self._fmt_action_time(start)} 버스트 진입 상태 · " + " / ".join(important[:3]),
                    "evidence": f"버스트 창 #{int(w.get('no') or 0)} 로컬 상태",
                    "action": " / ".join(actions[:2]) if actions else "버스트 진입 직전 핵심 상태를 다시 확인하세요.",
                    "confidence": "확인 필요",
                })

        # Deduplicate near-identical source rows and sort.
        seen = set()
        out = []
        for x in sorted(items, key=lambda r: -float(r.get("priority") or 0.0)):
            key = (x.get("kind"), x.get("title"))
            if key in seen:
                continue
            seen.add(key)
            out.append(x)

        maintains = []
        lo, po = local.get("opener"), peer.get("opener")
        if lo is not None and po is not None and float(lo) <= float(po) + 0.5:
            maintains.append(f"오프너 시작 속도 · 나 {float(lo):.2f}s / 상위 중앙 {float(po):.2f}s")
        lc, pc = local.get("cooldownCount"), peer.get("cooldownCount")
        if lc is not None and pc is not None and abs(float(lc) - float(pc)) < 0.5:
            label = local.get("cooldownLabel") or "핵심 쿨기"
            maintains.append(f"{label} 첫 60초 사용 횟수 · 나 {int(lc)}회 / 상위 중앙 {float(pc):.0f}회")
        return out, maintains, pva

    def load_boss_dashboard_for_boss(self, b: dict):
        for iid in self.dashboard_table.get_children():
            self.dashboard_table.delete(iid)
        self.dashboard_diff.delete("1.0", "end")
        root = self.find_boss_root(b)
        if not root:
            self.dashboard_title_var.set("보스 데이터 폴더를 찾지 못했습니다.")
            self.dashboard_note_var.set("")
            return

        sid = int(b.get("specID") or 0)
        local = self._local_dashboard_metrics(root, sid)
        comp_zip, comp_mode = self._comparator_zip_for_boss(b)
        peer = self._peer_dashboard_metrics(comp_zip, sid)
        n = int(peer.get("n") or 0)
        if comp_mode == "top30":
            peer_heading = "상위30" if n == 30 else f"상위30({n}명)"
            note = f"Top50 모집 → 필터 후 상세 비교군 {n}명"
        elif n:
            peer_heading = f"기본 비교군({n}명)"
            note = "상위30 비교는 [상위30 정밀 분석]을 한 번 실행하면 교체됩니다."
        else:
            peer_heading = "상위30"
            note = "상위 비교군 없음 · [상위30 정밀 분석] 실행 필요"
        self.dashboard_table.heading("peer", text=peer_heading)
        self.dashboard_note_var.set(note)
        self.dashboard_title_var.set(
            f"[{self.current_player}]  {b.get('spec')} {b.get('specID')}  {_ui_difficulty_name({'difficultyID': b.get('difficulty'), 'difficultyName': b.get('difficultyName')})} · {b.get('boss')}"
        )

        cd_label = local.get("cooldownLabel") or ("어춤" if sid == 261 else "죽표")
        mine_rows = [
            ("DPS", _fmt_dps(local.get("dps")), _fmt_dps(peer.get("dps"))),
            ("킬타임", _fmt_clock(local.get("duration")), _fmt_clock(peer.get("duration"))),
            ("오프너", "-" if local.get("opener") is None else f"{local['opener']:.2f}s", "-" if peer.get("opener") is None else f"{peer['opener']:.2f}s"),
            (cd_label, f"{int(local.get('cooldownCount') or 0)}회", "-" if peer.get("cooldownCount") is None else f"{peer['cooldownCount']:.0f}회"),
        ]
        for i, (label, mine, theirs) in enumerate(mine_rows):
            self.dashboard_table.insert("", "end", iid=f"m{i}", text=label, values=(mine, theirs))

        actions, maintains, pva = self._dashboard_action_summary(root, b, local, peer)
        if actions:
            for n, item in enumerate(actions[:3], 1):
                conf = item.get("confidence") or "확인"
                tag = "warn" if n == 1 else "caution"
                self.dashboard_diff.insert("end", f"{n}. {item.get('title')} [{conf}]\n", tag)
                if item.get("evidence"):
                    self.dashboard_diff.insert("end", f"   근거: {item.get('evidence')}\n", "note")
                if item.get("action"):
                    self.dashboard_diff.insert("end", f"   → {item.get('action')}\n\n", "note")
        else:
            self.dashboard_diff.insert(
                "end",
                "✓ 현재 확보된 행동 지표에서 우선 교정할 문제가 없습니다.\n",
                "ok",
            )

        if actions and actions[0].get("action"):
            self.dashboard_diff.insert(
                "end",
                f"한 줄 목표 → {actions[0].get('action')}\n\n",
                "warn",
            )

        if maintains:
            self.dashboard_diff.insert("end", "지금 고칠 필요 없는 것\n", "ok")
            for text in maintains[:3]:
                self.dashboard_diff.insert("end", f"   ✓ {text}\n", "ok")

        ld, pd = local.get("dps"), peer.get("dps")
        if ld and pd:
            pct_gap = (float(pd) - float(ld)) / float(pd) * 100.0
            if pct_gap > 1.0:
                self.dashboard_diff.insert(
                    "end",
                    f"\n※ DPS {pct_gap:.1f}% 차이는 결과값입니다. 위 행동 원인을 먼저 교정한 뒤 다시 확인합니다.\n",
                    "note",
                )
        if comp_mode != "top30":
            self.dashboard_diff.insert(
                "end",
                "\n※ 지금 값은 기본 비교군입니다. 상위30 정밀 분석 완료 후 이 카드가 자동으로 갱신됩니다.\n",
                "note",
            )

    def load_timeline_for_boss(self, b: dict):
        self.timeline.delete("1.0", "end")
        root = self.find_boss_root(b)
        if not root:
            self.timeline.insert("end", "보스 데이터 폴더를 찾지 못했습니다.", "note")
            return
        fight = _select_local_fight_row(root)
        casts_path = self._player_data_file(root, "casts.csv")
        if not fight or not casts_path:
            self.timeline.insert("end", "대표 Fight 캐스트 데이터를 찾지 못했습니다.", "note")
            return
        fight_no = int(fight["fightNo"])
        sid = int(b.get("specID") or 0)
        key_cds = {185313, 121471, 1856, 1297761, 360194, 381623, 1236994}
        rows = []
        try:
            with casts_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
                for r in csv.DictReader(f):
                    try:
                        if int(float(r.get("fightNo") or -1)) != fight_no:
                            continue
                    except Exception:
                        continue
                    if r.get("event") != "SPELL_CAST_SUCCESS" or "source" not in str(r.get("relation") or ""):
                        continue
                    try:
                        t = float(r.get("tMs") or 0) / 1000.0
                        spell = int(float(r.get("spellID") or 0))
                    except Exception:
                        continue
                    rows.append((t, spell, r.get("spellName") or str(spell)))
        except Exception:
            rows = []
        self.timeline.insert(
            "end", f"=== {b.get('boss')} · Fight {fight_no} · {fight['durationSec']:.1f}s ===\n", "title"
        )
        self.timeline.insert("end", "시간       주문\n", "note")
        for t, spell, name in rows[:600]:
            tag = "cooldown" if spell in key_cds else None
            self.timeline.insert("end", f"{t:7.2f}s   {name}\n", tag if tag else ())
        if len(rows) > 600:
            self.timeline.insert("end", f"\n※ {len(rows)-600}개 캐스트는 화면 과밀 방지를 위해 생략했습니다.", "note")
        self.timeline.see("1.0")

    def load_targets_for_boss(self, b: dict):
        self.targets.delete("1.0", "end")
        root = self.find_boss_root(b)
        if not root:
            self.targets.insert("end", "보스 데이터 폴더를 찾지 못했습니다.", "note")
            return
        fight = _select_local_fight_row(root)
        damage_path = self._player_data_file(root, "damage_done.csv")
        if not fight or not damage_path:
            self.targets.insert("end", "대표 Fight 피해 데이터를 찾지 못했습니다.", "note")
            return
        fight_no = int(fight["fightNo"])

        def _aggregate():
            agg = {}
            total = 0.0
            try:
                with damage_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
                    for r in csv.DictReader(f):
                        try:
                            if int(float(r.get("fightNo") or -1)) != fight_no:
                                continue
                            t = float(r.get("tMs") or 0) / 1000.0
                        except Exception:
                            continue
                        if "source" not in str(r.get("relation") or ""):
                            continue
                        amount = _decode_local_damage_amount(r.get("extras"))
                        name = str(r.get("destName") or "알 수 없음")
                        row = agg.setdefault(name, {"damage": 0.0, "first": t})
                        row["damage"] += amount
                        row["first"] = min(float(row["first"]), t)
                        total += amount
            except Exception:
                pass
            return agg, total

        agg, total = _b38_csv_cached(damage_path, ("targets", fight_no), _aggregate)
        agg = {k: dict(v) for k, v in agg.items()}
        self.targets.insert("end", f"=== {b.get('boss')} · Fight {fight_no} 피해대상 ===\n", "title")
        self.targets.insert("end", "대상                              피해량        비중      첫 타격\n", "section")
        for name, row in sorted(agg.items(), key=lambda kv: -kv[1]["damage"])[:20]:
            share = row["damage"] / total * 100.0 if total else 0.0
            self.targets.insert("end", f"{name[:28]:<28}  {_fmt_dps(row['damage']):>8}   {share:6.1f}%   {row['first']:7.2f}s\n")

        p = root / "analysis" / self.current_player / "precision_adds" / "precision_results.json"
        data = _read_json_file(p) if p.exists() else None
        target_summary = data.get("targetShareSummary") if isinstance(data, dict) else None
        target_rows = (target_summary or {}).get("rows") or []
        if target_rows:
            peer_n = int((target_summary or {}).get("peerCount") or 0)
            self.targets.insert("end", "\n=== 피해대상 비중 · 상위 비교 ===\n", "title")
            self.targets.insert(
                "end",
                f"대상                         나       상위 중앙    중앙50%         차이      표본\n",
                "section",
            )
            for tr in target_rows[:20]:
                name = str(tr.get("name") or "알 수 없음")[:24]
                ls = tr.get("localShare")
                ps = tr.get("peerMedianShare")
                q25 = tr.get("peerQ25Share")
                q75 = tr.get("peerQ75Share")
                hits = int(tr.get("peerHitCount") or 0)
                total_peers = int(tr.get("peerCount") or peer_n)
                try:
                    mine = f"{float(ls)*100:5.1f}%"
                except Exception:
                    mine = "    - "
                try:
                    theirs = f"{float(ps)*100:5.1f}%"
                    delta = float(ls or 0.0) - float(ps)
                    dtext = f"{delta*100:+5.1f}p"
                    tag = "warn" if abs(delta) >= 0.08 else None
                except Exception:
                    theirs = "    - "
                    dtext = "    - "
                    tag = None
                try:
                    rng = f"{float(q25)*100:.1f}~{float(q75)*100:.1f}%"
                except Exception:
                    rng = "-"
                line = f"{name:<24} {mine:>7}   {theirs:>7}   {rng:>12}   {dtext:>7}   {hits}/{total_peers}\n"
                self.targets.insert("end", line, tag if tag else ())
            self.targets.insert(
                "end",
                "※ 표본은 해당 NPC를 실제로 타격한 비교 로그 수입니다. 쫄을 안 친 로그를 0%로 강제 계산하지 않습니다.\n",
                "note",
            )
        elif isinstance(data, dict):
            self.targets.insert(
                "end",
                "\n※ 현재 정밀결과는 피해대상 비중 비교가 없는 구버전입니다. [상위30 정밀 분석]을 다시 실행하면 추가됩니다.\n",
                "note",
            )
        else:
            self.targets.insert(
                "end", "\n※ 피해대상 상위 비교는 [상위30 정밀 분석] 후 여기에 추가됩니다.\n", "note"
            )

        if isinstance(data, dict) and data.get("results"):
            self.targets.insert("end", "\n=== 쫄/보조 타깃 전환 비교 ===\n", "title")
            for row in data.get("results") or []:
                local = row.get("local") or {}
                peer = row.get("peer") or {}
                try:
                    lv = float(local.get("switchDelay")) / 1000.0
                    pv = float(peer.get("switchDelay")) / 1000.0
                    delta = lv - pv
                    tag = "warn" if delta > 0.7 else None
                    self.targets.insert(
                        "end", f"{row.get('label','웨이브')} : 나 {lv:.2f}s / 상위 중앙 {pv:.2f}s / {delta:+.2f}s\n", tag if tag else ()
                    )
                except Exception:
                    continue
        elif isinstance(data, dict):
            diag = data.get("waveDiagnostics") or []
            if diag:
                self.targets.insert("end", "\n=== 쫄 전환 비교 상태 ===\n", "title")
                for d in diag[:10]:
                    self.targets.insert(
                        "end",
                        f"{d.get('label','웨이브')} : 비교군 매칭 {d.get('matchedPeers',0)}/{(target_summary or {}).get('peerCount',0)}명\n",
                        "note",
                    )
                self.targets.insert("end", "※ 전환 지연 판정은 8명 이상 웨이브 매칭 시에만 표시합니다.\n", "note")
        self.targets.see("1.0")

    def on_boss_select(self, _event=None, sync_nav=True, _b38_allow_skip=False):
        sel = self.boss_tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
            b = self.boss_data[idx]
        except Exception:
            return

        if sync_nav:
            self._set_nav_context(self._boss_context_key(b), page="boss", live_try_key=None)
        self._update_breadcrumb()
        self._refresh_shared_nav_tree()

        self.boss_title_var.set(
            f"{b.get('boss')} · {_ui_difficulty_name({'difficultyID': b.get('difficulty'), 'difficultyName': b.get('difficultyName')})} · {b.get('spec')}({b.get('specID')})"
        )
        self.dashboard_boss_var.set(self._boss_combo_label(b))
        try:
            self.dashboard_boss_combo.configure(state="readonly")
            self.dashboard_precision_btn.configure(state="normal")
            self.dashboard_cooldown_btn.configure(state="normal")
        except Exception:
            pass
        # B38 SPEED2: a programmatic selection_set() is followed by an
        # asynchronous <<TreeviewSelect>> echo, and nav-tree sync selects the
        # same boss again, so one startup rendered the same boss 6 times.  Skip
        # only the heavy panel rebuild when the SAME boss with the SAME data was
        # rendered moments ago and this call is an echo / nav re-select.
        # Explicit refresh calls (data reload, analysis finished) always render.
        sig = (idx, id(b), id(self.boss_data), str(getattr(self, "current_player", "")))
        now = time.monotonic()
        last = getattr(self, "_b38_boss_render", None)
        if ((_event is not None or _b38_allow_skip) and last is not None
                and last[0] == sig and now - last[1] < 5.0):
            return
        self._b38_boss_render = (sig, now)
        self.boss_detail.delete("1.0", "end")
        self.load_boss_dashboard_for_boss(b)
        self.load_rotation_quality_for_boss(b)
        self.load_burst_windows_for_boss(b)
        self.load_timeline_for_boss(b)
        self.load_targets_for_boss(b)
        # B14: 보스 선택은 모든 하위 분석 탭의 공통 컨텍스트다.
        # 쿨기/NSRT와 정밀 분석도 같은 보스로 즉시 동기화하고, 결과가 없으면
        # 이전 보스 화면을 비운다.
        self.sync_cooldown_for_boss(b)
        self.sync_precision_for_boss(b)

        med = b.get("medianDurationSec")
        med_text = "-" if med is None else f"{med:.1f}s"

        locked_map = collect_locked_issues(self.boss_data)
        session_map = collect_session_issues(self.boss_data)
        issues = locked_map.get(idx, []) + session_map.get(idx, [])

        # 1. 가장 먼저 행동할 것
        self.boss_detail.insert("end", "이번 전투에서 고칠 것\n", "label")
        if issues:
            for n, row in enumerate(issues, 1):
                tag = "warn" if row["source"] == "잠금 규칙" else "caution"
                self.boss_detail.insert("end", f"{n}. {row['title']}\n", tag)
                self.boss_detail.insert("end", f"   {row['action']}\n", "note")
                self.boss_detail.insert(
                    "end",
                    "   → 다음 로그에서 같은 항목의 로컬 수치가 비교군에 가까워졌는지 확인.\n\n",
                    "note",
                )
        else:
            self.boss_detail.insert(
                "end",
                "✓ 잠금 규칙상 지금 바로 고칠 항목은 없습니다.\n"
                "  아래의 정상 항목을 유지하고, 표본부족 항목은 다음 로그를 더 모아 확인하세요.\n\n",
                "ok",
            )

        # 2. 전투 요약
        self.boss_detail.insert("end", "전투 요약\n", "label")
        self.boss_detail.insert(
            "end",
            f"{b.get('fights', 0)} Fight / {b.get('kills', 0)}킬 / {b.get('wipes', 0)}와이프"
            f" / 중앙 전투시간 {med_text}\n\n",
        )

        if not b.get("lockedRuleAnalyzed"):
            self.boss_detail.insert(
                "end",
                "아직 같은 난이도 WCL 비교/잠금 규칙 판정 결과가 없습니다.",
                "note",
            )
            return

        rules = b.get("rules", [])
        if not rules:
            self.boss_detail.insert("end", "표시할 규칙 결과가 없습니다.", "note")
            return

        # 3. 잘하고 있는 것
        normals = [r for r in rules if r.get("status") == "정상"]
        self.boss_detail.insert("end", "잘하고 있는 것\n", "label")
        if normals:
            for r in normals:
                self.boss_detail.insert(
                    "end",
                    f"✓ {r.get('rule', '')}\n",
                    "ok",
                )
                if r.get("detail"):
                    self.boss_detail.insert("end", f"   {r.get('detail')}\n")
                self.boss_detail.insert(
                    "end",
                    f"   → {rule_note(r.get('rule',''), '정상')}\n\n",
                    "note",
                )
        else:
            self.boss_detail.insert("end", "현재 정상으로 확정된 잠금 항목 없음.\n\n", "note")

        # 4. 참고/표본부족
        refs = [r for r in rules if r.get("status") in ("표본부족", "비교전용")]
        if refs:
            self.boss_detail.insert("end", "더 모아서 볼 것\n", "label")
            for r in refs:
                status = r.get("status", "")
                self.boss_detail.insert(
                    "end",
                    f"· [{status}] {r.get('rule', '')}\n",
                    "sample",
                )
                if r.get("detail"):
                    self.boss_detail.insert("end", f"   {r.get('detail')}\n")
                note = rule_note(r.get("rule", ""), status)
                if note:
                    self.boss_detail.insert("end", f"   → {note}\n", "note")
                self.boss_detail.insert("end", "\n")

        # 5. 실제 잠금 경고/주의 원문은 마지막에
        flagged = [r for r in rules if r.get("status") in ("경고", "주의")]
        if flagged:
            self.boss_detail.insert("end", "잠금 규칙 경고/주의 원문\n", "label")
            for r in flagged:
                status = r.get("status", "")
                tag = "warn" if status == "경고" else "caution"
                self.boss_detail.insert(
                    "end",
                    f"{STATUS_ICON.get(status, '·')} [{status}] {r.get('rule', '')}\n",
                    tag,
                )
                if r.get("detail"):
                    self.boss_detail.insert("end", f"   {r.get('detail')}\n")
                self.boss_detail.insert("end", "\n")

        self.boss_detail.see("1.0")

    def selected_boss(self):
        sel = self.boss_tree.selection()
        if not sel:
            return None
        try:
            return self.boss_data[int(sel[0])]
        except Exception:
            return None

    def find_boss_root(self, b: dict) -> Path | None:
        if not self.current_dataset:
            return None
        sid = int(b.get("specID"))
        spec = str(b.get("spec"))
        encounter = int(b.get("encounterID"))
        diff = int(b.get("difficulty"))
        broot = self.current_dataset / "characters" / self.current_player / f"spec_{sid}_{spec}" / "bosses"
        if not broot.exists() and self.current_player == "PRIVATE_ONLY":
            # v1.8 이전 결과 하위호환
            broot = self.current_dataset / f"spec_{sid}_{spec}" / "bosses"
        if not broot.exists():
            return None
        candidates = sorted(broot.glob(f"{encounter}_*_d{diff}"))
        return candidates[0] if candidates else None

    def rotation_quality_path(self, boss_root: Path) -> Path:
        return boss_root / "analysis" / self.current_player / "rotation_quality" / "quality.txt"

    def load_rotation_quality_for_boss(self, b: dict):
        self.quality.delete("1.0", "end")
        root = self.find_boss_root(b)
        if not root:
            self.quality.insert("end", "보스 데이터 폴더를 찾지 못했습니다.", "note")
            return
        p = self.rotation_quality_path(root)
        if not p.exists():
            self.quality.insert(
                "end",
                "회전 품질 결과가 아직 없습니다.\n"
                "v2.1로 통로그 분석을 한 번 실행하면 자동 생성됩니다.",
                "note",
            )
            return

        text = p.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines(True):
            s = line.strip()
            tag = None
            if s.startswith("==="):
                tag = "title"
            elif s.startswith("---") or s.startswith("[대표 Fight]"):
                tag = "section"
            elif "⚠" in s or "[경고]" in s:
                tag = "warn"
            elif "△" in s or "[주의]" in s:
                tag = "caution"
            elif s.startswith("✓"):
                tag = "ok"
            elif s.startswith("※") or "선택 기준:" in s:
                tag = "note"
            self.quality.insert("end", line, tag if tag else ())
        self.quality.see("1.0")

    def burst_windows_path(self, boss_root: Path) -> Path:
        return boss_root / "analysis" / self.current_player / "burst_windows" / "burst_windows.txt"

    def load_burst_windows_for_boss(self, b: dict):
        self.burst.delete("1.0", "end")
        root = self.find_boss_root(b)
        if not root:
            self.burst.insert("end", "보스 데이터 폴더를 찾지 못했습니다.", "note")
            return
        p = self.burst_windows_path(root)
        if not p.exists():
            self.burst.insert(
                "end",
                "버스트 창 결과가 아직 없습니다.\n"
                "v2.1로 통로그 분석을 한 번 실행하면 자동 생성됩니다.",
                "note",
            )
            return

        text = p.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines(True):
            s = line.strip()
            tag = None
            if s.startswith("==="):
                tag = "title"
            elif s.startswith("[죽표 #") or s.startswith("[어춤 #") or s.startswith("[대표 Fight]"):
                tag = "section"
            elif "⚠" in s or "[경고]" in s:
                tag = "warn"
            elif s.startswith("△") or "확인:" in s:
                tag = "caution"
            elif s.startswith("✓"):
                tag = "ok"
            elif s.startswith("→") or s.startswith("  →"):
                tag = "action"
            elif s.startswith("-") or s.startswith("※"):
                tag = "note"
            self.burst.insert("end", line, tag if tag else ())
        self.burst.see("1.0")

    def precision_summary_path(self, boss_root: Path) -> Path:
        return boss_root / "analysis" / self.current_player / "precision_adds" / "precision_coach.txt"

    def _clear_precision_view_for_boss(self, b: dict, message: str | None = None):
        self.precision.delete("1.0", "end")
        boss_name = str(b.get("boss") or "선택 보스")
        diff_name = _ui_difficulty_name({"difficultyID": b.get("difficulty"), "difficultyName": b.get("difficultyName")})
        self.precision.insert("end", f"=== {boss_name} · {diff_name} · 정밀 분석 ===\n", "title")
        self.precision.insert(
            "end",
            (message or "이 보스의 정밀 분석 결과가 아직 없습니다.\n[상위30 정밀 분석]을 실행하면 현재 선택 보스로 생성합니다.\n"),
            "note",
        )
        self.precision.see("1.0")

    def sync_precision_for_boss(self, b: dict, force: bool = False):
        """B37.3.8.3: 정밀 분석 화면을 현재 선택 보스 컨텍스트에 고정한다.

        저장 결과가 있으면 해당 보스 폴더에서만 읽고, 없으면 이전 보스의
        precision_coach 텍스트를 절대 남겨두지 않는다.  신규 결과의
        precision_results.json context가 현재 encounter와 충돌하면 표시도 막는다.
        """
        key = self._boss_context_key(b)
        if not force and self.precision_context_key == key:
            return
        self.precision_context_key = key
        root = self.find_boss_root(b)
        if root is None:
            self._clear_precision_view_for_boss(b, "이 보스의 로컬 분석 폴더가 없어 정밀 분석 결과를 표시할 수 없습니다.\n")
            return
        summary = self.precision_summary_path(root)
        result_json = root / "analysis" / self.current_player / "precision_adds" / "precision_results.json"
        if result_json.exists():
            try:
                payload = json.loads(result_json.read_text(encoding="utf-8"))
                ctx = payload.get("context") or {}
                stored_enc = ctx.get("encounterID")
                stored_spec = ctx.get("specID")
                if stored_enc is not None and int(stored_enc) != int(b.get("encounterID") or -1):
                    self._clear_precision_view_for_boss(
                        b,
                        "저장된 정밀 분석 결과의 encounterID가 현재 선택 보스와 달라 표시하지 않습니다.\n"
                        "[상위30 정밀 분석]을 다시 실행하세요.\n",
                    )
                    return
                if stored_spec is not None and int(stored_spec) != int(b.get("specID") or -1):
                    self._clear_precision_view_for_boss(
                        b,
                        "저장된 정밀 분석 결과의 전문화가 현재 선택과 달라 표시하지 않습니다.\n"
                        "[상위30 정밀 분석]을 다시 실행하세요.\n",
                    )
                    return
            except Exception:
                pass
        if summary.exists():
            self.load_precision_result(summary)
            self.precision_context_key = key
            return
        self._clear_precision_view_for_boss(b)

    def load_precision_result(self, path: Path):
        self.precision.delete("1.0", "end")
        if not path.exists():
            self.precision.insert("end", f"정밀 분석 결과 파일이 없습니다:\n{path}", "note")
            return
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines(True):
            s = line.strip()
            tag = None
            if s.startswith("==="):
                tag = "title"
            elif re.match(r"^\d+\. \[", s) or s.startswith("△"):
                tag = "issue"
            elif "다음 풀:" in s:
                tag = "action"
            elif s.startswith("-") or s.startswith("※"):
                tag = "note"
            self.precision.insert("end", line, tag if tag else ())
        self.precision.see("1.0")

    def _precision_cmd(self, cmd, status_text: str, span=None, owner=None):
        """Run an engine step.  B31.1: `span=(lo, hi)` maps the step onto the shared
        progress bar using @@PROGRESS lines, or the comparator fetch's [i/n] lines."""
        self.msgq.put(("precision_status", status_text))
        lo, hi = span if span else (None, None)
        stage_name = status_text.split("·")[-1].strip() if "·" in status_text else status_text
        fetch_phase = None
        if span:
            self.msgq.put(("progress", (lo, stage_name, owner)))
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        cp = subprocess.Popen(
            cmd,
            cwd=str(WORKDIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=creationflags,
        )
        assert cp.stdout is not None
        for line in cp.stdout:
            if span:
                prog = self._parse_progress_line(line)
                if prog is not None:
                    inner, stage = prog
                    self.msgq.put(("progress", (lo + (hi - lo) * max(0.0, min(100.0, inner)) / 100.0, stage or stage_name, owner)))
                    continue
                if "[METADATA]" in line:
                    fetch_phase = "meta"
                elif "[FILTER]" in line:
                    fetch_phase = "detail"
                m = self.FETCH_INDEX_RE.match(line)
                if m and fetch_phase:
                    i, n = int(m.group(1)), max(1, int(m.group(2)))
                    base, width = (0.0, 0.3) if fetch_phase == "meta" else (0.3, 0.65)
                    frac = base + width * (i - 1) / n
                    label = "상위 랭킹 확인" if fetch_phase == "meta" else "상위 로그 상세 수집"
                    self.msgq.put(("progress", (lo + (hi - lo) * frac, f"{label} {i}/{n}", owner)))
                elif "[DONE]" in line and fetch_phase:
                    self.msgq.put(("progress", (lo + (hi - lo) * 0.98, "비교군 저장", owner)))
            self.msgq.put(("log", line))
        code = cp.wait()
        if code != 0:
            raise RuntimeError(f"{status_text} 실패 (exit {code}). 실행 로그를 확인하세요.")
        if span:
            self.msgq.put(("progress", (hi, stage_name, owner)))

    def start_precision_analysis(self):
        if self.cooldown_running or self.precision_running or (self.proc is not None and self.proc.poll() is None):
            messagebox.showinfo("분석 진행 중", "현재 분석 작업이 끝난 뒤 정밀분석을 실행하세요.")
            return
        fight = getattr(self, "_b37509_precision_items", {}).get(self.b3742_historical_choice_var.get())
        if fight is None:
            messagebox.showinfo("원본 Fight 선택", "선택한 날짜의 전투를 먼저 고르세요. 최근 로그를 대신 분석하지 않습니다.")
            return
        # The visible navigator must still own exactly the session + boss selected.
        enc, diff, date, player, _spec = (list(self._nav_context_key()) + [None] * 5)[:5]
        if (enc != fight.encounter_id or normalize_difficulty(diff) != normalize_difficulty(fight.difficulty)
                or date != fight.session_date or player != fight.player):
            messagebox.showerror("분석 대상 변경", "선택 컨텍스트가 달라졌습니다. 전투를 다시 선택하세요.")
            return
        if normalize_difficulty(fight.difficulty) not in (15, 16):
            messagebox.showinfo("분석 제외", "영웅/신화 레이드 보스만 정밀 분석합니다.")
            return
        needed = [ENGINE / "scan_target_activity.py", ENGINE / "fetch_precision_comparators.py",
                  ENGINE / "analyze_precision_adds.py", ENGINE / "wcl_api.py"]
        missing = [str(x) for x in needed if not x.is_file()]
        if missing:
            messagebox.showerror("정밀 엔진 없음", "필수 파일이 없습니다:\n" + "\n".join(missing))
            return
        # Source mapping is based on exact dataset identity, NEVER latest log.
        matched_log = dataset_source_log(fight.dataset, self.current_log, LOGDIR)
        ta = fight.dataset / "precision" / "target_activity.csv"
        if not cache_has_fight(ta, fight.fight_no) and matched_log is None:
            messagebox.showerror("원본/타깃 캐시 없음",
                                 f"{fight.session_date} Fight {fight.fight_no}의 타깃 캐시가 없고 원본 통로그도 없습니다.\n"
                                 "최근 로그로 대신 실행하지 않습니다. 해당 날짜 원본을 복원하세요.")
            return
        spec = fight.spec_id
        b = {"boss":fight.display_name, "encounterID":fight.encounter_id,
             "difficulty":fight.difficulty, "specID":spec, "spec":fight.spec_name}
        self.precision_running = True
        self.precision_btn.configure(state="disabled")
        self.b3742_prec_run_btn.configure(state="disabled")
        self.run_btn.configure(state="disabled")
        self._progress_begin("과거 정밀 분석", "precision")
        self.precision.delete("1.0", "end")
        self.precision.insert("end", f"{fight.session_date} · {fight.label}\n정밀분석을 시작합니다...\n", "note")
        self.notebook.select(self.precision_frame)
        job = {"token":self._selection_token(), "dataset":fight.dataset, "log":matched_log,
               "player":fight.player, "bossKey":self._nav_context_key(),
               "historical":True, "fightNo":fight.fight_no,
               "out":fight.result_dir, "fightDuration":fight.duration,
               "fightOutcome":"kill" if fight.kill else "wipe"}
        self.precision_origin_token = job["token"]
        threading.Thread(target=self._precision_worker, args=(b, fight.boss_root, job), daemon=True).start()

    def _precision_worker(self, b: dict, boss_root: Path, job: dict):
        try:
            dataset = Path(job["dataset"])
            log_path = Path(job["log"]) if job.get("log") else None
            player = str(job["player"])
            sid = int(b.get("specID"))
            encounter = int(b.get("encounterID"))
            local_diff = int(b.get("difficulty"))
            wcl_diff = WCL_DIFFICULTY_MAP[local_diff]
            safe = re.sub(r'[<>:"/\\|?* ]+', "_", str(b.get("boss", "boss"))).strip("_") or "boss"

            ta = dataset / "precision" / "target_activity.csv"
            ta_meta = dataset / "precision" / "target_activity_meta.json"
            # Historical cache is dataset-local and MUST contain the selected Fight.
            ta_fresh = cache_has_fight(ta, int(job["fightNo"]))
            if not ta_fresh:
                if log_path is None or not log_path.is_file():
                    raise RuntimeError("선택한 Fight의 원본 로그/타깃 캐시가 없습니다. 최근 로그 대체 금지.")
                self._precision_cmd(
                    [str(PYTHON), str(ENGINE / "scan_target_activity.py"),
                     "--dataset", str(dataset), "--log", str(log_path)],
                    "정밀 1/3 · 공대 타깃 구간 1회 스캔 중...",
                    span=(0.0, 25.0), owner="precision",
                )
                if not cache_has_fight(ta, int(job["fightNo"])):
                    raise RuntimeError("타깃 스캔 완료 후에도 선택 Fight가 없습니다. 결과 저장을 중단합니다.")
            else:
                self.msgq.put(("precision_status", "정밀 1/3 · 타깃 구간 캐시 재사용"))
                self.msgq.put(("progress", (25.0, "타깃 구간 캐시 재사용", "precision")))

            # v1.4: 상위 1~50위를 모집한 뒤 CN/TW 제외 + 킬타임 필터.
            # 상세 이벤트는 필터를 통과한 표본 중 기본 30명(목표 25~40) 수집.
            out_base = self._comparator_base("precision_cache", sid, f"{encounter}_{safe}_d{local_diff}_top50_nonCNTW_v22phase", dataset=dataset)
            comp_zip = Path(str(out_base) + ".zip")
            if not comp_zip.exists():
                compatible = find_compatible_precision_cache(
                    self._comparator_roots("precision_cache", sid, dataset=dataset),
                    encounter, local_diff,
                )
                if compatible is not None:
                    comp_zip = compatible
                    self.msgq.put(("precision_status", "정밀 2/3 · 같은 보스/난이도/전문화 기존 비교군 캐시 재사용"))
                    self.msgq.put(("log", f"[CACHE FALLBACK] {compatible}\n"))
                    self.msgq.put(("progress", (80.0, "호환 비교군 캐시 재사용", "precision")))
                elif not wcl_credentials_available():
                    out_dir = Path(job["out"])
                    out_dir.mkdir(parents=True, exist_ok=True)
                    summary = out_dir / "precision_coach.txt"
                    summary.write_text(
                        "=== 정밀 분석 ===\n"
                        "비교군 없음 · WCL 인증 필요\n\n"
                        "- 로컬 타깃 구간 스캔은 완료되었습니다.\n"
                        "- 같은 보스/난이도/전문화의 저장된 Top50 비교군 캐시를 찾지 못했습니다.\n"
                        "- WCL_API_Setup.cmd로 인증정보를 설정한 뒤 다시 실행하면 비교군을 새로 수집합니다.\n"
                        "- 기본 분석/기믹 판정 결과는 그대로 사용할 수 있습니다.\n",
                        encoding="utf-8",
                    )
                    self.msgq.put(("precision_status", "정밀 분석 부분 완료 · 비교군 없음 · WCL 인증 필요"))
                    self.msgq.put(("log", "[SKIP] 비교군 없음 · WCL 인증 필요. traceback 없이 부분 완료합니다.\n"))
                    self.msgq.put(("progress", (100.0, "비교군 없음 · WCL 인증 필요", "precision")))
                    self.msgq.put(("precision_done", {
                        "path": str(summary), "token": job.get("token"), "bossKey": job.get("bossKey"),
                        "partial": True, "status": "비교군 없음 · WCL 인증 필요", "historical":True,
                    }))
                    return
                else:
                    local_duration = job["fightDuration"]
                    local_outcome = job["fightOutcome"]
                    cmd = [
                        str(PYTHON), str(ENGINE / "fetch_precision_comparators.py"),
                        "--encounter", str(encounter), "--difficulty", str(wcl_diff),
                        "--spec", str(sid),
                        "--rank-pool", "50",
                        "--detail-count", "30",
                        "--min-count", "15",
                        "--out", str(out_base),
                    ]
                    if local_duration is not None:
                        cmd += ["--local-duration", f"{local_duration:.3f}"]
                    if local_outcome:
                        cmd += ["--local-outcome", local_outcome]
                    self._precision_cmd(
                        cmd,
                        "정밀 2/3 · WCL 상위 1~50위 모집 → CN/TW 제외 → 비교군 필터/수집 중...",
                        span=(25.0, 80.0), owner="precision",
                    )
            else:
                self.msgq.put(("precision_status", "정밀 2/3 · Top50 필터 비교군 캐시 재사용"))
                self.msgq.put(("progress", (80.0, "비교군 캐시 재사용", "precision")))

            self._precision_cmd(
                [str(PYTHON), str(ENGINE / "analyze_precision_adds.py"),
                 "--root", str(boss_root), "--player", player, "--spec", str(sid),
                 "--comparators", str(comp_zip), "--target-activity", str(ta), "--min-wave-peers", "8",
                 "--boss-name", str(b.get("boss") or safe),
                 "--encounter-id", str(encounter),
                 "--difficulty-name", _ui_difficulty_name({"difficultyID": local_diff, "difficultyName": b.get("difficultyName")}, f"d{local_diff}"),
                 "--fight-no", str(job["fightNo"]), "--out", str(job["out"])],
                "정밀 3/3 · 원인 분해 및 다음 풀 코칭 생성 중...",
                span=(80.0, 99.0), owner="precision",
            )
            summary = Path(job["out"]) / "precision_coach.txt"
            self.msgq.put(("precision_done", {"path": str(summary), "token": job.get("token"), "bossKey": job.get("bossKey"), "historical":True}))
        except Exception as e:
            self.msgq.put(("precision_error", {"error": str(e), "token": job.get("token")}))

    def _boss_cooldown_context_key(self, b: dict, player: str):
        # Same five-dimensional identity as the shared navigation state.
        return make_boss_context_key(b, self._dataset_session_date(), player)

    def _clear_cooldown_view_for_boss(self, b: dict, message: str):
        self.cooldown_data = None
        self.cooldown_entries = {}
        self.cooldown_selected_ids = set()
        self.cooldown_combo["values"] = ()
        self.cooldown_choice_var.set("")
        if hasattr(self, "cooldown_sample_var"):
            self.cooldown_sample_var.set("")
        self._clear_cooldown_selectors()
        if hasattr(self, "cooldown_heatmap"):
            self.cooldown_heatmap.delete("all")
        self.cooldown_text.delete("1.0", "end")
        self.cooldown_text.insert(
            "end",
            f"=== {b.get('boss')} · {_ui_difficulty_name({'difficultyID': b.get('difficulty'), 'difficultyName': b.get('difficultyName')})} · {b.get('spec')}({b.get('specID')}) ===\n\n",
            "title",
        )
        self.cooldown_text.insert("end", message, "note")
        self._refresh_cooldown_layout()

    def sync_cooldown_for_boss(self, b: dict, force: bool = False):
        """B14: 현재 보스 선택과 상위 쿨기/NSRT 화면을 항상 같은 컨텍스트로 맞춘다.

        저장된 결과가 있으면 즉시 로드하고, 없으면 이전 보스 결과를 절대 남겨두지 않는다.
        """
        key = self._boss_cooldown_context_key(b, self.current_player)
        if not force and self.cooldown_context_key == key and self.cooldown_data is not None:
            return
        self.cooldown_context_key = key
        root = self.find_boss_root(b)
        p = self.cooldown_result_path_for_boss(b, root)
        if p.exists():
            self.load_cooldown_result(p)
            # load_cooldown_result는 파일 내용만 읽으므로 보스 컨텍스트는 여기서 확정한다.
            self.cooldown_context_key = key
            return
        note = (
            "이 보스의 쿨기/NSRT 결과는 아직 생성되지 않았습니다.\n"
            "[선택 보스 쿨기/NSRT]를 누르면 현재 선택한 보스로 생성합니다.\n"
        )
        if root is None:
            note += "로컬 보스 subset이 없어도 WCL 비교군만으로 메모를 만들 수 있습니다.\n"
        self._clear_cooldown_view_for_boss(b, note)

    def cooldown_result_path(self, boss_root: Path) -> Path:
        return boss_root / "analysis" / self.current_player / "cooldown_nsrt" / "cooldown_timeline.json"

    def comparator_only_cooldown_root(self, b: dict) -> Path:
        """Stable synthetic root for comparator-only cooldown/NSRT builds.

        Historical/Live session rows can exist without an analyzed ``bosses/<encounter>``
        subset.  ``build_cooldown_nsrt.py`` only needs ``--root`` for optional local
        fight/cast enrichment; when that subset is absent we give it an empty, stable
        root and persist the actual result below ``data/cooldown_nsrt_results``.
        """
        sid = int(b.get("specID") or 0)
        encounter = int(b.get("encounterID") or 0)
        diff = int(b.get("difficulty") or 0)
        safe_player = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", str(self.current_player or "player"))
        root = WORKDIR / "data" / "cooldown_nsrt_results" / f"spec_{sid}" / f"encounter_{encounter}_d{diff}" / safe_player
        root.mkdir(parents=True, exist_ok=True)
        return root

    def cooldown_result_path_for_boss(self, b: dict, boss_root: Path | None = None) -> Path:
        if boss_root is not None:
            return self.cooldown_result_path(boss_root)
        return self.comparator_only_cooldown_root(b) / "cooldown_timeline.json"

    def _icon_cache_path(self, icon_name: str) -> Path:
        # Bundled icons are read-only; downloaded public icons belong to the
        # user profile, not the EXE/Program Files installation directory.
        cache = (self._public_home if self.raid_public_mode else WORKDIR / "data") / "icon_cache"
        cache.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^0-9A-Za-z_-]+", "_", icon_name or "unknown")
        return cache / f"{safe}.jpg"

    def _download_icon(self, icon_name: str) -> Path | None:
        if not icon_name:
            return None
        p = self._icon_cache_path(icon_name)
        if p.exists() and p.stat().st_size > 100:
            return p
        url = f"https://wow.zamimg.com/images/wow/icons/medium/{icon_name}.jpg"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 NiniWCLAnalyzer/1.7"},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                data = r.read()
            if len(data) > 100:
                p.write_bytes(data)
                return p
        except Exception:
            return None
        return None

    def _load_icon_image(self, icon_name: str, selected: bool):
        p = self._download_icon(icon_name)
        if not p:
            return None
        try:
            from PIL import Image, ImageEnhance, ImageTk
            im = Image.open(p).convert("RGB").resize((34, 34))
            if not selected:
                im = ImageEnhance.Brightness(im).enhance(0.38)
            photo = ImageTk.PhotoImage(im)
            self.cooldown_icon_refs.append(photo)
            return photo
        except Exception:
            return None

    def _clear_cooldown_selectors(self):
        for w in list(self.cooldown_selector_bar.winfo_children()):
            w.destroy()
        self.cooldown_icon_refs = []
        self.cooldown_icon_buttons = {}
        self.cooldown_selector_frames = []

    def _tooltip(self, widget, text):
        state = {"win": None}

        def show(_event=None):
            if state["win"] or not text:
                return
            x = widget.winfo_rootx()
            y = widget.winfo_rooty() + widget.winfo_height() + 3
            win = tk.Toplevel(widget)
            win.wm_overrideredirect(True)
            win.wm_geometry(f"+{x}+{y}")
            tk.Label(
                win,
                text=text,
                bg="#111111",
                fg="#f0f0d0",
                relief="solid",
                bd=1,
                padx=_px(5),
                pady=_px(2),
                font=(FONT_BODY, 9),
            ).pack()
            state["win"] = win

        def hide(_event=None):
            if state["win"]:
                state["win"].destroy()
                state["win"] = None

        widget.bind("<Enter>", show, add="+")
        widget.bind("<Leave>", hide, add="+")

    def _cooldown_selector_items_by_id(self, data=None):
        data = data or self.cooldown_data or {}
        out = {}
        for group in (data.get("selectorGroups") or []):
            for item in (group.get("items") or []):
                try:
                    out[int(item.get("spellID"))] = item
                except Exception:
                    continue
        return out

    def _cooldown_selected_real_ids(self, data=None, selected_ids=None):
        """Expand virtual selector slots to real WCL spell IDs."""
        data = data or self.cooldown_data or {}
        selected = {int(x) for x in (selected_ids if selected_ids is not None else (self.cooldown_selected_ids or set()))}
        items = self._cooldown_selector_items_by_id(data)
        real = set()
        for sid in selected:
            item = items.get(sid) or {}
            variants = item.get("variantSpellIDs") or []
            if item.get("virtual") or variants:
                for vsid in variants:
                    try:
                        real.add(int(vsid))
                    except Exception:
                        pass
            elif sid >= 0:
                real.add(sid)
        return real

    def _cooldown_variant_to_selected_slot(self, data=None, selected_ids=None):
        data = data or self.cooldown_data or {}
        selected = {int(x) for x in (selected_ids if selected_ids is not None else (self.cooldown_selected_ids or set()))}
        mapping = {}
        for sid, item in self._cooldown_selector_items_by_id(data).items():
            if sid not in selected:
                continue
            for vsid in (item.get("variantSpellIDs") or []):
                try:
                    mapping[int(vsid)] = int(sid)
                except Exception:
                    pass
        return mapping

    def build_cooldown_selectors(self):
        self._clear_cooldown_selectors()
        data = self.cooldown_data or {}
        groups = data.get("selectorGroups") or []

        # 처음 로드 시 기본 ON, 이후 같은 데이터 안에서는 사용자의 선택 유지.
        known_ids = {
            int(item.get("spellID"))
            for group in groups
            for item in (group.get("items") or [])
            if item.get("spellID") is not None
        }
        if not self.cooldown_selected_ids.intersection(known_ids):
            self.cooldown_selected_ids = {
                int(item["spellID"])
                for group in groups
                for item in (group.get("items") or [])
                if item.get("spellID") is not None and item.get("defaultSelected", True)
            }
        else:
            self.cooldown_selected_ids &= known_ids

        for group in groups:
            items = group.get("items") or []
            # 물약이 실제 비교군에서 검출되지 않았으면 공간을 낭비하지 않는다.
            if not items:
                continue

            gf = tk.Frame(self.cooldown_selector_bar, bg="#171710")
            gf.pack(side="left", padx=_px((0, 13)))
            self.cooldown_selector_frames.append(gf)

            tk.Label(
                gf,
                text=group.get("label") or "",
                bg="#171710",
                fg="#fff24c",
                font=(FONT_BODY, 9, "bold"),
            ).pack(anchor="w")

            row = tk.Frame(gf, bg="#303030", padx=_px(2), pady=_px(2))
            row.pack(anchor="w")

            for item in items:
                sid = int(item["spellID"])
                selected = sid in self.cooldown_selected_ids

                border = tk.Frame(
                    row,
                    bg="#f6e312" if selected else "#555555",
                    padx=_px(2),
                    pady=_px(2),
                )
                border.pack(side="left", padx=_px(1))

                photo = self._load_icon_image(item.get("icon"), selected)
                short = (item.get("name") or str(sid))[:4]
                btn = tk.Button(
                    border,
                    image=photo if photo else "",
                    text="" if photo else short,
                    compound="center",
                    width=34 if not photo else 34,
                    height=34 if not photo else 34,
                    bg="#171710",
                    fg="white",
                    activebackground="#272720",
                    activeforeground="white",
                    relief="flat",
                    bd=0,
                    padx=_px(0),
                    pady=_px(0),
                    command=lambda x=sid: self.toggle_cooldown_spell(x),
                )
                btn.pack()
                self._tooltip(
                    btn,
                    f"{item.get('name')}  ({sid})\\n클릭: 타임라인/MRT {'제외' if selected else '포함'}",
                )
                self.cooldown_icon_buttons[sid] = (border, btn, item)

    def toggle_cooldown_spell(self, sid: int):
        sid = int(sid)
        if sid in self.cooldown_selected_ids:
            self.cooldown_selected_ids.remove(sid)
        else:
            self.cooldown_selected_ids.add(sid)
        # 이미지는 OFF일 때 어둡게 다시 만들기 때문에 바 전체를 가볍게 재구성.
        self.build_cooldown_selectors()
        self.render_cooldown_entry(self.cooldown_choice_var.get())

    def schedule_cooldown_heatmap_redraw(self):
        if self.cooldown_heatmap_after_id is not None:
            try:
                self.after_cancel(self.cooldown_heatmap_after_id)
            except Exception:
                pass
        self.cooldown_heatmap_after_id = self.after(60, self.draw_cooldown_heatmap)

    @staticmethod
    def _cooldown_fmt_axis_time(sec):
        sec = max(0.0, float(sec or 0.0))
        m = int(sec // 60)
        s = int(sec - m * 60)
        return f"{m}:{s:02d}"

    def _hide_cooldown_heatmap_tooltip(self, _event=None):
        win = getattr(self, "cooldown_heatmap_tooltip", None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        self.cooldown_heatmap_tooltip = None

    def _show_cooldown_heatmap_tooltip(self, event, text):
        self._hide_cooldown_heatmap_tooltip()
        if not text:
            return
        try:
            x = int(event.x_root) + 12
            y = int(event.y_root) + 12
        except Exception:
            canvas = getattr(self, "cooldown_heatmap", None)
            if canvas is None:
                return
            x = canvas.winfo_rootx() + 20
            y = canvas.winfo_rooty() + 20
        win = tk.Toplevel(self)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x}+{y}")
        tk.Label(
            win,
            text=text,
            justify="left",
            bg="#111111",
            fg="#f4f4df",
            relief="solid",
            bd=1,
            padx=_px(6),
            pady=_px(4),
            font=(FONT_BODY, 9),
        ).pack()
        self.cooldown_heatmap_tooltip = win

    def _bind_cooldown_heatmap_tip(self, canvas, item, text):
        if not item or not text:
            return
        canvas.tag_bind(
            item,
            "<Enter>",
            lambda e, tip=text: self._show_cooldown_heatmap_tooltip(e, tip),
        )
        canvas.tag_bind(item, "<Leave>", self._hide_cooldown_heatmap_tooltip)

    @staticmethod
    def _cooldown_peer_sort_key(peer):
        # peer 세로 위치는 항상 비교군(WCL ranking) 순서로 고정한다.
        # 추천 점수순으로 GUI 후보 목록이 바뀌어도 히트맵의 행 위치는 변하지 않는다.
        rank = peer.get("rank")
        try:
            rank_key = int(rank)
        except Exception:
            rank_key = 10**9
        return (rank_key, str(peer.get("name") or ""), str(peer.get("folder") or ""))

    @staticmethod
    def _cooldown_heatmap_axis_end(data, entry, local_entry):
        """Heatmap x-axis end: target / selected representative / local + small margin.

        Deliberately ignores the longest peer. A single long kill should not compress
        the useful early/mid-fight region.
        """
        candidates = []
        for value in (
            data.get("targetDurationSec"),
            (entry or {}).get("durationSec") if entry else None,
            (local_entry or {}).get("durationSec") if local_entry else None,
        ):
            try:
                if value is not None and float(value) > 0:
                    candidates.append(float(value))
            except Exception:
                pass

        # 합의 지도를 선택한 경우에는 선택 엔트리에 duration이 없으므로
        # 기본 추천 대표의 실제 킬 시간을 축 후보로 사용한다.
        if entry and str(entry.get("entryType") or "") == "consensus":
            try:
                rep_d = float((data.get("representative") or {}).get("durationSec") or 0.0)
                if rep_d > 0:
                    candidates.append(rep_d)
            except Exception:
                pass

        base = max(candidates) if candidates else 60.0
        # 3% 또는 8초 중 큰 값만큼 여유를 두되 과도하게 늘리지 않는다.
        margin = max(8.0, base * 0.03)
        return max(60.0, base + margin)

    @staticmethod
    def _cooldown_sparse_sample_cutoff(peers):
        """Return (cutoff_sec, threshold, total) when continuing fights drop below 1/3.

        For 30 peers this means the shaded region starts after the 10th-longest
        fight ends, i.e. when fewer than 10 logs still have combat data.
        """
        durations = []
        for peer in peers or []:
            try:
                d = float(peer.get("durationSec"))
                if d > 0:
                    durations.append(d)
            except Exception:
                pass
        total = len(durations)
        if total < 2:
            return (None, 0, total)
        import math
        threshold = max(1, int(math.ceil(total / 3.0)))
        ordered = sorted(durations, reverse=True)
        cutoff = ordered[threshold - 1]
        return (float(cutoff), threshold, total)

    def _cooldown_actual_entry(self, data):
        """Prefer same-fight Live exact casts only when all selected spells were tracked.

        A historical raid_facts file may have been captured before the selector
        catalog learned a newly selected spell.  Falling back to WCL local avoids
        falsely calling such an untracked spell "MISSED".
        """
        live = (data or {}).get("liveActual") or None
        if live:
            tracked = {int(x) for x in (live.get("trackedSpellIDs") or [])}
            selected = self._cooldown_selected_real_ids(data)
            if selected.issubset(tracked):
                return live
        local = (data or {}).get('local') or None
        if local and live and live.get('sameFightAsLocal'):
            return {**local, **{k: live[k] for k in ('deathIntervals', 'deathIncidents', 'raidFactsPath', 'deathContextStartTimestamp', 'deathContextPlayer') if k in live}}
        return local

    def draw_cooldown_heatmap(self):
        self.cooldown_heatmap_after_id = None
        self._hide_cooldown_heatmap_tooltip()
        canvas = getattr(self, "cooldown_heatmap", None)
        if canvas is None:
            return
        canvas.delete("all")
        data = self.cooldown_data or {}
        label = self.cooldown_choice_var.get() if hasattr(self, "cooldown_choice_var") else ""
        entry = self.cooldown_entries.get(label) if label else None
        peers = sorted((data.get("peers") or []), key=self._cooldown_peer_sort_key)
        selected = {int(x) for x in (self.cooldown_selected_ids or set())}
        if not peers or not selected:
            canvas.create_text(10, 15, anchor="w", text="히트맵 데이터가 없습니다.", fill="#666666")
            return

        variant_to_slot = self._cooldown_variant_to_selected_slot(data, selected)
        def row_spell_id(sid):
            sid = int(sid)
            return int(variant_to_slot.get(sid, sid))

        spell_names = {
            int(x.get("spellID")): (x.get("spell") or str(x.get("spellID")))
            for x in (data.get("trackedSpells") or [])
            if x.get("spellID") is not None
        }
        for sid, item in self._cooldown_selector_items_by_id(data).items():
            spell_names[int(sid)] = item.get("name") or spell_names.get(int(sid), str(sid))
        order = []
        for group in (data.get("selectorGroups") or []):
            for item in (group.get("items") or []):
                sid = int(item.get("spellID") or -1)
                if sid in selected and sid not in order:
                    order.append(sid)
        for sid in sorted(selected):
            if sid not in order:
                order.append(sid)
        if not order:
            return

        # Same-fight Live exact casts are the preferred Actual overlay.  Fall back
        # to the WCL-local fight when no matching Live capture exists.
        local_entry = self._cooldown_actual_entry(data)
        max_time = self._cooldown_heatmap_axis_end(data, entry, local_entry)

        width = max(520, int(canvas.winfo_width() or 0))
        left, right, top, bottom = 126, 18, 38, 26
        # 로컬 actual 보조 레인을 위해 B6보다 행을 조금 넓힌다.
        row_h = 38 if local_entry else 32
        height = top + len(order) * row_h + bottom
        canvas.configure(height=height)
        pane = getattr(self, "cooldown_body_pane", None)
        frame = getattr(self, "cooldown_heatmap_frame", None)
        if pane is not None and frame is not None:
            try:
                # header + LabelFrame 여백까지 포함. 1080p에서는 이 pane가 먼저
                # 필요한 높이를 확보하고 아래 Text가 나머지를 사용한다.
                pane.paneconfigure(getattr(frame, "_outer", frame), minsize=max(250, min(460, height + 42)))
            except Exception:
                pass
        plot_w = max(80.0, width - left - right)

        def xx(t):
            return left + (max(0.0, min(max_time, float(t))) / max_time) * plot_w

        # 비교군의 1/3 미만만 전투가 이어지는 후반은 배경을 옅게 칠한다.
        # late cluster의 eligible 대비 지지율이 높아 보여도 전체 표본은 적다는 사실을
        # 히트맵 자체에서 바로 읽을 수 있게 하는 장치다.
        sparse_start, sparse_threshold, sparse_total = self._cooldown_sparse_sample_cutoff(peers)
        if sparse_start is not None and sparse_start < max_time:
            sx = xx(sparse_start)
            canvas.create_rectangle(
                sx, top - 5, width - right, height - bottom + 2,
                fill="#f3f3f3", outline="",
            )
            sparse_label = f"표본 부족 (<{sparse_threshold}/{sparse_total})"
            # B10: 목표/후보 킬 라벨과 겹치지 않도록 표본 부족은 별도 아래쪽 라벨 레인을 사용한다.
            sparse_label_y = 28
            # B9: 음영 폭이 라벨보다 좁으면 음영 왼쪽 바깥에 오른쪽 정렬.
            # 왼쪽 여백도 부족하면 잘린 라벨을 억지로 그리지 않고 생략한다.
            shade_width = max(0.0, (width - right) - sx)
            approx_label_width = max(88.0, len(sparse_label) * 6.5)
            if shade_width >= approx_label_width + 8:
                canvas.create_text(
                    sx + 5, sparse_label_y, anchor="w",
                    text=sparse_label,
                    fill="#999999", font=(FONT_BODY, 8),
                )
            elif (sx - left) >= approx_label_width + 8:
                canvas.create_text(
                    sx - 5, sparse_label_y, anchor="e",
                    text=sparse_label,
                    fill="#999999", font=(FONT_BODY, 8),
                )

        # 시간축: 30초 간격.
        tick = 30
        t = 0
        while t <= max_time + 0.001:
            x = xx(t)
            canvas.create_line(x, top - 5, x, height - bottom + 2, fill="#ededed")
            canvas.create_text(
                x, height - 7,
                text=self._cooldown_fmt_axis_time(t),
                fill="#777777",
                font=(FONT_BODY, 8),
            )
            t += tick

        # 상/하 위치가 비교군 순위를 뜻한다는 힌트를 작게 둔다.
        canvas.create_text(left - 8, 8, anchor="e", text="상위↑", fill="#777777", font=(FONT_BODY, 8))

        diffuse_by_sid = {}
        for row in (data.get("diffuseUsageClusters") or []):
            sid = row_spell_id(int(row.get("spellID") or -1))
            diffuse_by_sid.setdefault(sid, []).append(row)

        row_y = {}
        for i, sid in enumerate(order):
            y = top + i * row_h + row_h / 2
            # local actual 레인이 있는 경우 본 peer 점구름 중심은 약간 위로 올린다.
            peer_center_y = y - (4 if local_entry else 0)
            row_y[sid] = peer_center_y
            canvas.create_text(
                left - 8, peer_center_y,
                anchor="e",
                text=spell_names.get(sid, str(sid)),
                fill="#333333",
                font=(FONT_BODY, 9, "bold"),
            )
            canvas.create_line(left, peer_center_y, width - right, peer_center_y, fill="#d8d8d8")
            for d in diffuse_by_sid.get(sid, []):
                raw1 = float(d.get("windowStart") if d.get("windowStart") is not None else d.get("time") or 0)
                raw2 = float(d.get("windowEnd") if d.get("windowEnd") is not None else d.get("time") or 0)
                lo, hi = sorted((raw1, raw2))
                if lo > max_time:
                    continue
                x1, x2 = xx(lo), xx(min(hi, max_time))
                canvas.create_rectangle(
                    x1, peer_center_y - 10, x2, peer_center_y + 10,
                    fill="#dcecff", outline="#6c9fd2",
                )

        # 비교군 순위 → 고정된 y 좌표. peer[0]이 가장 위다.
        peer_den = max(1, len(peers) - 1)
        peer_offset_by_folder = {}
        peer_offset_by_identity = {}
        peer_spread = 14.0
        for pi, p in enumerate(peers):
            offset = ((pi / peer_den) - 0.5) * peer_spread if len(peers) > 1 else 0.0
            folder = p.get("folder")
            if folder is not None:
                peer_offset_by_folder[str(folder)] = offset
            peer_offset_by_identity[(str(p.get("name") or ""), str(p.get("region") or ""), p.get("rank"))] = offset

        # 선택한 후보가 실제 비교군에서 차지하는 원래 행을 먼저 살짝 강조한다.
        selected_offset = None
        selected_peer_rank = None
        if entry and str(entry.get("entryType") or "") != "local":
            folder = entry.get("folder")
            if folder is not None:
                selected_offset = peer_offset_by_folder.get(str(folder))
            if selected_offset is None:
                ident = (str(entry.get("name") or ""), str(entry.get("region") or ""), entry.get("rank"))
                selected_offset = peer_offset_by_identity.get(ident)
            selected_peer_rank = entry.get("rank")

        if selected_offset is not None:
            for sid in order:
                base_y = row_y[sid]
                yy = base_y + selected_offset
                canvas.create_line(
                    left, yy, width - right, yy,
                    fill="#b9d4ea", width=2,
                )

        # 원본 peer 시전 점. 순위를 고정해 상위 로그가 항상 위에 온다.
        for pi, p in enumerate(peers):
            offset = ((pi / peer_den) - 0.5) * peer_spread if len(peers) > 1 else 0.0
            for e in (p.get("timeline") or []):
                sid = row_spell_id(int(e.get("spellID") or -1))
                base_y = row_y.get(sid)
                if base_y is None:
                    continue
                et = float(e.get("time") or 0.0)
                if et > max_time:
                    continue
                x = xx(et)
                yy = base_y + offset
                canvas.create_oval(x-1.5, yy-1.5, x+1.5, yy+1.5, fill="#b9b9b9", outline="")

        # 좁은 합의 클러스터: q25~q75 범위 + 중심 원형 마커 + hover 수치.
        peer_count = max(1, int(data.get("peerCount") or len(peers)))
        for c in (data.get("consensusClusters") or []):
            sid = row_spell_id(int(c.get("spellID") or -1))
            y = row_y.get(sid)
            if y is None:
                continue
            center = float(c.get("time") or 0.0)
            q25 = float(c.get("q25") if c.get("q25") is not None else center)
            q75 = float(c.get("q75") if c.get("q75") is not None else center)
            support = float(c.get("support") or 0.0)
            n = int(c.get("n") or 0)
            eligible = max(1, int(c.get("eligible") or peer_count))
            if n < 2 or center > max_time:
                continue
            x1, x2, xc = xx(min(q25, max_time)), xx(min(q75, max_time)), xx(center)
            line_id = canvas.create_line(x1, y, x2, y, fill="#d49528", width=2)
            # B8: 원 크기는 eligible 내부 지지율이 아니라 전체 비교군 중 실제 n명 비율.
            # 예: 후반 2/2(100%)라도 전체 30명 중 2명이면 작게 보여야 한다.
            total_ratio = min(1.0, max(0.0, n / float(peer_count)))
            r = 1.8 + 4.5 * total_ratio
            oval_id = canvas.create_oval(xc-r, y-r, xc+r, y+r, fill="#fff2b2", outline="#c67b00", width=1)
            spell = c.get("spell") or spell_names.get(sid, str(sid))
            # 툴팁은 기존 의미를 유지: 해당 시점까지 살아 있는 eligible 대비 n명.
            tip = (
                f"{spell} · 중앙 {self._cooldown_fmt_axis_time(center)} · "
                f"{n}/{eligible} ({support*100:.0f}%)\n"
                f"전체 표본 {n}/{peer_count} ({total_ratio*100:.0f}%) · "
                f"25~75% {self._cooldown_fmt_axis_time(q25)}~{self._cooldown_fmt_axis_time(q75)}"
            )
            self._bind_cooldown_heatmap_tip(canvas, oval_id, tip)
            self._bind_cooldown_heatmap_tip(canvas, line_id, tip)

        # 현재 선택한 대표/후보의 실제 시전은 진한 막대로 표시한다.
        if entry and str(entry.get("entryType") or "") != "consensus":
            for e in (entry.get("timeline") or []):
                sid = row_spell_id(int(e.get("spellID") or -1))
                y = row_y.get(sid)
                if y is None:
                    continue
                et = float(e.get("time") or 0.0)
                if et > max_time:
                    continue
                x = xx(et)
                canvas.create_line(x, y - 11, x, y + 11, fill="#1f4e79", width=3)

        # 기존 분석 결과의 local이 있으면 Plan vs Actual 미리보기로 별도 레인에 얹는다.
        # live exact-cast 저장 확장 전에도 현재 확보된 로컬 데이터로 UI 형태를 검증할 수 있다.
        if local_entry and (not entry or str(entry.get("entryType") or "") != "local"):
            for sid in order:
                base_y = row_y[sid]
                actual_y = base_y + 12
                canvas.create_line(left, actual_y, width - right, actual_y, fill="#eadcf0", width=1)
            for e in (local_entry.get("timeline") or []):
                sid = row_spell_id(int(e.get("spellID") or -1))
                base_y = row_y.get(sid)
                if base_y is None:
                    continue
                et = float(e.get("time") or 0.0)
                if et > max_time:
                    continue
                x = xx(et)
                actual_y = base_y + 12
                # 마름모: 선택 대표의 막대와 모양까지 다르게 해서 겹쳐도 구분한다.
                pts = [x, actual_y-4, x+4, actual_y, x, actual_y+4, x-4, actual_y]
                canvas.create_polygon(pts, fill="#8e3f8f", outline="#6f2c70")

        # B9: 선택한 대표/후보의 실제 킬 종료 시점을 그림에도 표시한다.
        # 이후 시전이 없는 이유가 "미사용"인지 "이미 전투 종료"인지 즉시 구분할 수 있다.
        candidate_kill_x = None
        candidate_kill_sec = None
        entry_type = str((entry or {}).get("entryType") or "")
        if entry and entry_type in ("representative", "peer"):
            try:
                candidate_kill_sec = float(entry.get("durationSec") or 0.0)
            except Exception:
                candidate_kill_sec = 0.0
            if candidate_kill_sec and candidate_kill_sec <= max_time + 0.001:
                candidate_kill_x = xx(candidate_kill_sec)
                canvas.create_line(
                    candidate_kill_x, top - 5, candidate_kill_x, height - bottom + 2,
                    fill="#5f6f7a", width=1, dash=(2, 2),
                )
                canvas.create_text(
                    candidate_kill_x, 18,
                    text=f"후보 킬 {self._cooldown_fmt_axis_time(candidate_kill_sec)}",
                    fill="#5f6f7a", font=(FONT_BODY, 8),
                )

        target = data.get("targetDurationSec")
        if target is not None:
            tx = xx(float(target))
            canvas.create_line(tx, top - 6, tx, height - bottom + 2, fill="#8a4b08", width=2, dash=(4, 3))
            # 후보 킬과 목표가 너무 가까우면 라벨이 겹치지 않게 목표를 한 줄 위로 올린다.
            target_y = 7 if (candidate_kill_x is not None and abs(tx - candidate_kill_x) < 80) else 9
            canvas.create_text(tx, target_y, text="목표", fill="#8a4b08", font=(FONT_BODY, 8, "bold"))

    def filtered_cooldown_timeline(self, entry):
        selected = self._cooldown_selected_real_ids()
        return [
            e for e in (entry.get("timeline") or [])
            if int(e.get("spellID", -1)) in selected
        ]

    def filtered_nsrt_timeline(self, entry):
        selected = self._cooldown_selected_real_ids()
        snap_intermission = bool(
            getattr(self, "cooldown_intermission_snap_var", None)
            and self.cooldown_intermission_snap_var.get()
        )
        if snap_intermission and entry.get("nsrtTimelineIntermissionSnap"):
            source = entry.get("nsrtTimelineIntermissionSnap") or []
        else:
            source = entry.get("nsrtTimeline") or entry.get("timeline") or []
        return [
            e for e in source
            if int(e.get("spellID", -1)) in selected
        ]

    def build_filtered_nsrt(self, entry):
        data = self.cooldown_data or {}
        spec_id = int(data.get("specID") or 0)
        lines = [
            f"EncounterID:{data.get('encounterID')};"
            f"Difficulty:{data.get('difficulty')};"
            f"Name:{data.get('boss')};"
        ]
        order = {
            1856: 10,       # 소멸
            426591: 15,     # Goremaw's Bite
            1236616: 18,    # Light's Potential
            121471: 20,     # 어둠의 칼날
            360194: 20,     # 죽음표식
            185313: 30,     # 어둠의 춤
            385627: 30,     # 왕의 파멸
        }
        timeline = sorted(
            self.filtered_nsrt_timeline(entry),
            key=lambda e: (
                int(e.get("phaseOrdinal") or 1),
                float(e.get("phaseTime") if e.get("phaseTime") is not None else e.get("time") or 0),
                order.get(int(e.get("spellID") or 0), 50),
                int(e.get("spellID") or 0),
            ),
        )
        for e in timeline:
            phase = str(e.get("phase") or "1")
            rel = float(
                e.get("phaseTime")
                if e.get("phaseTime") is not None
                else e.get("time") or 0
            )
            lines.append(
                f"ph:{phase};time:{rel:.1f};tag:{spec_id};"
                f"spellid:{int(e.get('spellID'))};"
            )
        return "\r\n".join(lines)

    def build_filtered_mrt(self, entry):
        data = self.cooldown_data or {}
        spec_id = int(data.get("specID") or 0)
        lines = [
            f"EncounterID:{data.get('encounterID')}",
            f"Difficulty:{data.get('difficulty')}",
            f"Spec:{spec_id}",
            "",
        ]
        for s in (entry.get("phaseSegments") or []):
            lines.append(
                f"Phase:{s.get('phase','1')};"
                f"Start:{float(s.get('start') or 0):.1f}"
            )
        if entry.get("phaseSegments"):
            lines.append("")

        timeline = sorted(
            self.filtered_cooldown_timeline(entry),
            key=lambda e: float(e.get("time") or 0),
        )
        for e in timeline:
            sec = max(0.0, float(e.get("time") or 0))
            mm = int(sec // 60)
            ss = sec - mm * 60
            lines.append(
                f"{{time:{mm:02d}:{ss:04.1f}}} -  "
                f"{{spell:{int(e.get('spellID'))}}}"
            )
        return "\r\n".join(lines)

    def _current_cooldown_boss(self):
        """Return the boss owned by the shared navigation context.

        B34.2.3: ``boss_tree`` is now a hidden legacy control.  Its selection can
        legitimately lag behind the visible date/boss/Try tree, so it must not
        win when the user opens the cooldown tab from a Live boss or Try.
        """
        wanted = self._nav_context_key()
        try:
            wanted_encounter = wanted[0]
        except Exception:
            wanted_encounter = None

        if wanted_encounter is not None:
            # Prefer an analyzed-dataset boss only when it is the exact visible
            # navigation context (encounter/difficulty/date/player/spec).
            idx = self._find_boss_index_by_context(wanted)
            if idx is not None:
                return self.boss_data[idx]

            # Stored Live-only boss: keep the visible selection even when no WCL
            # dataset has been generated for it yet.  sync_cooldown_for_boss()
            # will clear any previous boss result if no matching root exists.
            for row in self._all_live_rows():
                if self._live_context_key(row) == wanted:
                    spec_id = wanted[4]
                    return {
                        "boss": row.get("name") or "보스",
                        "encounterID": row.get("encounterID"),
                        "difficulty": row.get("difficultyID"),
                        "difficultyName": row.get("difficultyName") or "",
                        "specID": spec_id,
                        "spec": self._spec_display(spec_id),
                    }
            return None

        # Legacy fallback for pages that have no shared boss context yet.
        return self.selected_boss()

    def _cooldown_plan_path(self):
        root = Path(getattr(self, "cooldown_plan_root", WORKDIR / "data" / "cooldown_plans"))
        key = self._nav_context_key()
        enc, diff, date, player, spec = (list(key) + [None] * 5)[:5]
        safe_player = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", str(player or self.current_player or "player"))
        safe_date = re.sub(r"[^0-9-]+", "_", str(date or "session"))
        return root / f"{safe_date}_{enc or 'boss'}_d{diff or 0}_{safe_player}_s{spec or 0}.json"

    def _read_saved_cooldown_plan(self):
        path = self._cooldown_plan_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                return data if isinstance(data, dict) else None
        except Exception:
            pass
        return None

    def _apply_saved_cooldown_plan(self, *, show_message=False):
        plan = self._read_saved_cooldown_plan()
        if not plan:
            if show_message:
                messagebox.showinfo("저장된 계획", "이 보스/캐릭터/세션에 저장된 비교 계획이 없습니다.")
            return False
        label = str(plan.get("label") or "")
        if label and label in self.cooldown_entries:
            ids = plan.get("selectedSpellIDs")
            if isinstance(ids, list):
                try:
                    self.cooldown_selected_ids = {int(x) for x in ids}
                except Exception:
                    pass
            if hasattr(self, "cooldown_intermission_snap_var"):
                self.cooldown_intermission_snap_var.set(bool(plan.get("intermissionSnap", False)))
            self.cooldown_choice_var.set(label)
            self.cooldown_plan_choice_var.set(label)
            self.build_cooldown_selectors()
            self._sync_modern_cooldown_options()
            self.render_cooldown_entry(label)
            self.cooldown_current_plan_var.set(f"{label} · 저장 계획")
            return True
        self.cooldown_current_plan_var.set(f"{label or '저장 계획'} · 현재 비교군에서 찾지 못함")
        if show_message:
            messagebox.showinfo("저장된 계획", "저장된 대표 로그가 현재 비교군에는 없습니다. 비교군을 새로 받은 뒤 다시 지정하세요.")
        return False

    def load_saved_cooldown_plan(self):
        self._apply_saved_cooldown_plan(show_message=True)

    def set_current_cooldown_plan(self):
        label = str(self.cooldown_choice_var.get() or "").strip()
        entry = self.cooldown_entries.get(label)
        if not entry:
            messagebox.showinfo("비교 계획", "먼저 대표 메모를 선택하세요.")
            return
        if not entry.get("nsrtExportAllowed", True):
            messagebox.showinfo("비교 계획", "합의 지도는 비교 계획으로 지정할 수 없습니다. 한 사람의 실제 로그를 선택하세요.")
            return
        payload = {
            "version": "cooldown-plan-v1",
            "savedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "context": selection_context_dict(self._nav_context_key()),
            "label": label,
            "folder": entry.get("folder"),
            "selectedSpellIDs": sorted(int(x) for x in self.cooldown_selected_ids),
            "intermissionSnap": bool(self.cooldown_intermission_snap_var.get()),
        }
        path = self._cooldown_plan_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        self.cooldown_current_plan_var.set(f"{label} · 지정됨")
        self.status_var.set(f"비교 계획 지정 완료 · {label}")

    def save_nsrt_memo_file(self):
        label = str(self.cooldown_choice_var.get() or "").strip()
        entry = self.cooldown_entries.get(label)
        if not entry:
            messagebox.showinfo("메모 저장", "먼저 대표 메모를 선택하세요.")
            return
        if not entry.get("nsrtExportAllowed", True):
            messagebox.showinfo("메모 저장", "합의 지도는 NSRT 메모로 저장하지 않습니다.")
            return
        nsrt = self.build_filtered_nsrt(entry)
        b = self._current_cooldown_boss()
        root = self.find_boss_root(b) if b else None
        if root:
            out = root / "analysis" / self.current_player / "cooldown_nsrt" / "selected_nsrt.txt"
        else:
            chosen = filedialog.asksaveasfilename(
                title="NSRT 메모 저장",
                defaultextension=".txt",
                filetypes=[("Text", "*.txt"), ("All files", "*.*")],
                initialfile="selected_nsrt.txt",
            )
            if not chosen:
                return
            out = Path(chosen)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(nsrt + "\n", encoding="utf-8")
        self.status_var.set(f"NSRT 메모 저장 완료 · {out.name}")

    def load_existing_cooldown_memo(self):
        chosen = filedialog.askopenfilename(
            title="기존 메모/결과 불러오기",
            filetypes=[("Cooldown result", "*.json"), ("NSRT memo", "*.txt"), ("All files", "*.*")],
        )
        if not chosen:
            return
        path = Path(chosen)
        if path.suffix.lower() == ".json":
            self.load_cooldown_result(path)
            self.status_var.set(f"쿨기 결과 불러옴 · {path.name}")
            return
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            messagebox.showerror("메모 읽기", str(exc))
            return
        self.toggle_cooldown_advanced(force=True)
        self.cooldown_text.delete("1.0", "end")
        self.cooldown_text.insert("end", f"=== 외부 NSRT 메모 · {path.name} ===\\n\\n", "title")
        self.cooldown_text.insert("end", raw)
        self.cooldown_current_plan_var.set(f"외부 메모 · {path.name} · 미지정")

    def toggle_cooldown_advanced(self, force=None):
        show = (not getattr(self, "cooldown_advanced_visible", False)) if force is None else bool(force)
        self.cooldown_advanced_visible = show
        self._refresh_cooldown_layout()

    def _toggle_cooldown_option(self, option):
        items = self._cooldown_selector_items_by_id()
        wanted = None
        enabled = False
        if option == "feint":
            wanted = 1966
            enabled = bool(self.cooldown_opt_feint_var.get())
        elif option == "potion":
            enabled = bool(self.cooldown_opt_potion_var.get())
            for sid, item in items.items():
                if item.get("kind") == "potion_slot" or item.get("virtual") and item.get("variantSpellIDs"):
                    wanted = sid
                    break
        if wanted is None:
            return
        if enabled:
            self.cooldown_selected_ids.add(int(wanted))
        else:
            self.cooldown_selected_ids.discard(int(wanted))
        self.build_cooldown_selectors()
        label = self.cooldown_choice_var.get()
        if label:
            self.render_cooldown_entry(label)

    def _sync_modern_cooldown_options(self):
        if not hasattr(self, "cooldown_opt_potion_var"):
            return
        items = self._cooldown_selector_items_by_id()
        potion_ids = {
            sid for sid, item in items.items()
            if item.get("kind") == "potion_slot" or item.get("virtual") and item.get("variantSpellIDs")
        }
        self.cooldown_opt_potion_var.set(bool(potion_ids.intersection(self.cooldown_selected_ids)))
        self.cooldown_opt_feint_var.set(1966 in self.cooldown_selected_ids)

    def _refresh_cooldown_plan_choices(self):
        if not hasattr(self, "cooldown_plan_choices_frame"):
            return
        for child in self.cooldown_plan_choices_frame.winfo_children():
            child.destroy()
        labels = [
            label for label, entry in self.cooldown_entries.items()
            if str(entry.get("entryType") or "") in ("representative", "peer")
        ][:5]
        if not labels:
            labels = list(self.cooldown_entries.keys())[:5]
        current = self.cooldown_choice_var.get()
        if current not in labels and labels:
            current = labels[0]
        self.cooldown_plan_choice_var.set(current or "")
        for label in labels:
            entry = self.cooldown_entries.get(label) or {}
            display = label
            if len(display) > 42:
                display = display[:39] + "..."
            # B37.3.9.7: board 4 option cards (selected = 갈색 배경 + 주황 테두리).
            selected = label == (current or "")
            fill = _ui_theme.HILITE_BG if selected else _ui_theme.CARD_BG
            opt = RoundBox(self.cooldown_plan_choices_frame, fill=fill, outline=_ui_theme.HILITE_BORDER if selected else _ui_theme.CARD_BORDER, radius=8, padx=12, pady=9, cursor="hand2")
            opt.pack(fill="x", pady=_px((0, 6)))
            mark = tk.Label(opt.inner, text="●" if selected else "○", bg=fill, fg=_ui_theme.ORANGE if selected else _ui_theme.MUTED, font=(FONT_BODY, 10), cursor="hand2")
            mark.pack(side="left")
            txt = tk.Label(opt.inner, text=display, bg=fill, fg=_ui_theme.TEXT, font=(FONT_BODY, 10), anchor="w", cursor="hand2")
            txt.pack(side="left", fill="x", expand=True, padx=_px((8, 0)))

            def _pick(_e=None, value=label):
                self.cooldown_plan_choice_var.set(value)
                self._on_modern_cooldown_choice()
                try:
                    self._refresh_cooldown_plan_choices()
                except Exception:
                    pass
            for w in (opt, opt._canvas, opt.inner, mark, txt):
                w.bind("<Button-1>", _pick)

    def _on_modern_cooldown_choice(self):
        label = self.cooldown_plan_choice_var.get()
        if label not in self.cooldown_entries:
            return
        self.cooldown_choice_var.set(label)
        self.render_cooldown_entry(label)

    def _refresh_cooldown_modern_preview(self, entry=None, label=None):
        if not hasattr(self, "cooldown_preview_tree"):
            return
        for iid in self.cooldown_preview_tree.get_children():
            self.cooldown_preview_tree.delete(iid)
        label = label if label is not None else self.cooldown_choice_var.get()
        entry = entry or self.cooldown_entries.get(label)
        if not entry:
            self.cooldown_preview_label_var.set("대표 메모를 선택하세요.")
            self.cooldown_preview_note_var.set("")
            return
        self.cooldown_preview_label_var.set(label or "메모")
        sel = entry.get("selection") or {}
        preview_bits = []
        if sel:
            preview_bits.append(
                f"합의 {float(sel.get('consensusScore') or 0)*100:.0f}% · 킬 {self._fmt_live_duration(entry.get('durationSec'))}"
            )
        else:
            if entry.get("durationSec") is not None:
                preview_bits.append(f"킬 {self._fmt_live_duration(entry.get('durationSec'))}")
        audit = entry.get("phaseExportAudit") or (self.cooldown_data or {}).get("phaseExportAudit") or {}
        if audit.get("messageKo"):
            preview_bits.append(str(audit.get("messageKo")))
        self.cooldown_preview_note_var.set(" · ".join(x for x in preview_bits if x))

        timeline = sorted(self.filtered_nsrt_timeline(entry), key=lambda e: float(e.get("time") or 0))
        for n, event in enumerate(timeline[:80]):
            phase = str(event.get("phase") or "1")
            phase_time = event.get("phaseTime")
            if phase_time is None:
                phase_time = event.get("time") or 0
            sec = float(phase_time or 0)
            note = ""
            support = event.get("support")
            if float(sec) <= 1.0 and phase not in ("1", "ph1"):
                note = "페이즈 시작 버스트"
            elif event.get("intermissionSnapApplied") or event.get("snappedFromIntermission"):
                note = "인터미션 스냅"
            elif support is not None:
                try:
                    note = f"합의 {float(support)*100:.0f}%"
                except Exception:
                    pass
            self.cooldown_preview_tree.insert(
                "", "end", iid=f"cd-preview-{n}",
                values=(f"ph{phase}" if not phase.startswith("ph") else phase, f"{sec:.1f}", event.get("spell") or "?", note),
            )

    def _refresh_cooldown_live_try_list(self):
        if not hasattr(self, "cooldown_live_tries_tree"):
            return
        for iid in self.cooldown_live_tries_tree.get_children():
            self.cooldown_live_tries_tree.delete(iid)
        wanted = self._nav_context_key()
        rows = [r for r in self._all_live_rows() if self._live_context_key(r) == wanted]
        rows.sort(key=self._live_row_sort_key)
        for n, row in enumerate(rows[-12:]):
            is_fake = self._live_row_is_fake(row)
            is_reset = self._live_row_is_reset(row)
            is_early = self._live_row_is_early_wipe(row)
            result = _ui_result_en(row.get("result"), fake=is_fake, reset=is_reset, early=is_early)
            self.cooldown_live_tries_tree.insert(
                "", "end", iid=f"cd-live-{n}", text=f"Try {row.get('tryNo','?')}",
                values=(result, self._fmt_live_duration(row.get("durationSec"))),
                tags=("excluded",) if (is_fake or is_reset) else (),
            )

    def _refresh_cooldown_layout(self):
        if not hasattr(self, "cooldown_ready_frame"):
            return
        b = self._current_cooldown_boss()
        if b:
            spec = self._spec_display(b.get("specID"), b.get("spec"))
            self.cooldown_title_var.set(f"{b.get('boss','?')} · {_ui_difficulty_name({'difficultyID': b.get('difficulty'), 'difficultyName': b.get('difficultyName')})}")
            self.cooldown_badge_var.set(f"{spec} · 대표 실제 로그 한 사람의 시퀀스로 NSRT 메모를 만듭니다.")
        else:
            self.cooldown_title_var.set("상위 쿨기 / NSRT")
            self.cooldown_badge_var.set("왼쪽에서 보스를 선택하세요.")

        self.cooldown_ready_frame.pack_forget()
        self.cooldown_empty_frame.pack_forget()
        self.cooldown_advanced_wrap.pack_forget()

        if getattr(self, "cooldown_advanced_visible", False):
            self.cooldown_card_row.pack_forget()
            self.cooldown_advanced_wrap.pack(fill="both", expand=True)
            return

        data = self.cooldown_data or {}
        if data and self.cooldown_entries:
            if not self.cooldown_card_row.winfo_manager():
                self.cooldown_card_row.pack(fill="x", pady=_px((0, 10)))
            self.cooldown_ready_frame.pack(fill="both", expand=True)
            peer_count = int(data.get("peerCount") or 0)
            self.cooldown_compare_var.set(f"Top{peer_count} · {self._spec_display(data.get('specID'), data.get('spec'))} ({data.get('specID','-')})")
            target = data.get("targetDurationSec")
            source = str(data.get("targetDurationSource") or "비교군 기준")
            self.cooldown_target_var.set(
                f"{self._fmt_live_duration(target)} · {source}" if target is not None else source
            )
            plan = self._read_saved_cooldown_plan()
            if plan and plan.get("label"):
                self.cooldown_current_plan_var.set(f"{plan.get('label')} · 지정됨")
            else:
                rep = data.get("representative") or {}
                self.cooldown_current_plan_var.set(
                    f"미지정 · 추천 {rep.get('label') or '대표 로그'}"
                )
            self._refresh_cooldown_plan_choices()
            self._sync_modern_cooldown_options()
            self._refresh_cooldown_modern_preview()
        else:
            self.cooldown_card_row.pack_forget()
            self.cooldown_empty_frame.pack(fill="both", expand=True)
            self._refresh_cooldown_live_try_list()
            rows = [
                r for r in self._all_live_rows()
                if self._live_context_key(r) == self._nav_context_key()
            ]
            kills = sum(1 for r in rows if str(r.get("result") or "").upper() == "KILL")
            self.cooldown_badge_var.set(
                f"진행 중 · 트라이 {len(rows)} · 킬 기록 {'있음' if kills else '없음'} · 페이즈 정책은 메모 생성 후 검증"
            )
            self.cooldown_empty_hint_var.set(
                "킬 기록이 없으면 비교군 중앙값으로 목표 시간을 만들고, 생성 결과에서 Phase 정책을 확인하세요."
            )

    def load_cooldown_result(self, path: Path):
        if not path.exists():
            self.cooldown_data = None
            self.cooldown_entries = {}
            self.cooldown_text.delete("1.0", "end")
            self.cooldown_text.insert("end", f"쿨기 결과 파일이 없습니다:\n{path}", "note")
            self._refresh_cooldown_layout()
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            self.cooldown_data = None
            self.cooldown_entries = {}
            self.cooldown_text.delete("1.0", "end")
            self.cooldown_text.insert("end", f"쿨기 결과 읽기 실패: {e}", "note")
            self._refresh_cooldown_layout()
            return

        self.cooldown_data = data
        peer_count = int(data.get("peerCount") or 0)
        if peer_count < 20:
            self.cooldown_sample_var.set(
                data.get("sampleWarning")
                or f"⚠ 비교군 {peer_count}명 · 표본 적음: 35%/70% 문턱이 소수 인원에 민감"
            )
        else:
            self.cooldown_sample_var.set(f"비교군 {peer_count}명")
        self.cooldown_entries = {}
        self.cooldown_selected_ids = set()
        self.build_cooldown_selectors()

        representative = data.get("representative") or {}
        rep_folder = representative.get("folder") if representative else None
        if representative:
            rep_label = representative.get("label") or "추천 대표 로그"
            self.cooldown_entries[rep_label] = representative

        # B5: 추천 결과 바로 아래에 TOP 후보를 점수순으로 배치한다.
        # 기존에도 모든 peer를 고를 수 있었지만 점수/경고가 보이지 않아 수동 대표 선택용 UX로는 부족했다.
        peers = data.get("peers") or []
        peer_by_folder = {
            str(p.get("folder")): p
            for p in peers
            if p.get("folder") is not None
        }
        candidate_folders = set()
        for order, meta in enumerate(data.get("representativeCandidates") or [], 1):
            folder = meta.get("folder")
            if not folder or folder == rep_folder:
                continue
            peer = peer_by_folder.get(str(folder))
            if not peer:
                continue
            candidate_folders.add(str(folder))
            warn = " ⚠" if (meta.get("warnings") or meta.get("coreMisses")) else ""
            label = (
                f"후보 {order} · {peer.get('label') or peer.get('name') or 'peer'} · "
                f"{float(meta.get('score') or 0)*100:.1f}점{warn}"
            )
            self.cooldown_entries[label] = peer

        common = data.get("common") or {}
        if common:
            common_label = common.get("label") or "합의 지도"
            self.cooldown_entries[common_label] = common

        local = data.get("local")
        if local:
            self.cooldown_entries[local.get("label") or f"{self.current_player} (로컬)"] = local

        # TOP 후보 밖의 로그도 계속 수동 선택 가능하게 유지한다.
        for p in peers:
            folder = str(p.get("folder")) if p.get("folder") is not None else None
            if (rep_folder and folder == str(rep_folder)) or (folder and folder in candidate_folders):
                continue
            label = p.get("label") or p.get("name") or "peer"
            self.cooldown_entries[label] = p

        values = list(self.cooldown_entries.keys())
        self.cooldown_combo["values"] = values
        if values:
            self.cooldown_choice_var.set(values[0])
            self.cooldown_plan_choice_var.set(values[0])
            self.render_cooldown_entry(values[0])
        if not self._apply_saved_cooldown_plan(show_message=False):
            self._refresh_cooldown_layout()

    def render_cooldown_entry(self, label: str):
        self.cooldown_text.delete("1.0", "end")
        entry = self.cooldown_entries.get(label)
        data = self.cooldown_data or {}
        if not entry:
            self.cooldown_text.insert("end", "선택한 타임라인을 찾지 못했습니다.", "note")
            return

        self.cooldown_text.insert(
            "end",
            f"=== {label} ===\n",
            "title",
        )
        self.cooldown_text.insert(
            "end",
            f"{data.get('boss')} · {data.get('difficulty')} · {data.get('spec')}({data.get('specID')})\n"
            f"비교군 {data.get('peerCount', 0)}명\n",
        )
        if data.get("sampleQuality") == "low" or int(data.get("peerCount") or 0) < 20:
            self.cooldown_text.insert(
                "end",
                f"⚠ {data.get('sampleWarning') or '비교군 20명 미만 · 표본 적음: 지지율 문턱 해석에 주의'}\n",
                "note",
            )
        self.cooldown_text.insert("end", "\n")

        timeline = self.filtered_cooldown_timeline(entry)
        entry_type = str(entry.get("entryType") or "")
        if entry_type in ("representative", "peer"):
            sel = entry.get("selection") or {}
            target = data.get("targetDurationSec")
            target_source = data.get("targetDurationSource") or "-"
            if sel:
                score_label = "대표점수" if entry_type == "representative" else "후보점수"
                self.cooldown_text.insert(
                    "end",
                    f"{score_label} {float(sel.get('score') or 0)*100:.1f} · "
                    f"기술균형 합의 {float(sel.get('consensusScore') or 0)*100:.1f}% · "
                    f"킬 {float(entry.get('durationSec') or 0):.1f}s",
                    "note",
                )
                if target is not None:
                    self.cooldown_text.insert(
                        "end",
                        f" · 목표 {float(target):.1f}s ({target_source})",
                        "note",
                    )
                self.cooldown_text.insert("end", "\n", "note")
                comparison_order = sel.get("comparisonOrder")
                if comparison_order is not None:
                    self.cooldown_text.insert(
                        "end",
                        f"비교군 내 순서 {int(comparison_order)}/{int(data.get('peerCount') or 0)} · "
                        f"WCL 원순위 #{sel.get('rank') if sel.get('rank') is not None else '-'} · "
                        f"순위점수 {float(sel.get('rankScore') or 0)*100:.1f}%\n",
                        "note",
                    )
                per_spell = sel.get("perSpellConsensus") or {}
                if per_spell:
                    spell_names = {
                        str(x.get("spellID")): x.get("spell")
                        for x in (data.get("trackedSpells") or [])
                    }
                    parts = []
                    for sid, row in per_spell.items():
                        name = spell_names.get(str(sid)) or f"spell {sid}"
                        parts.append(f"{name} {float((row or {}).get('score') or 0)*100:.0f}%")
                    if parts:
                        self.cooldown_text.insert(
                            "end",
                            "기술별 합의 · " + " · ".join(parts) + "\n",
                            "note",
                        )
                diffuse_rows = data.get("diffuseUsageClusters") or []
                if diffuse_rows:
                    for row in diffuse_rows:
                        sid = int(row.get("spellID") or 0)
                        spell_names = {
                            int(x.get("spellID")): x.get("spell")
                            for x in (data.get("trackedSpells") or [])
                            if x.get("spellID") is not None
                        }
                        name = spell_names.get(sid) or row.get("spell") or f"spell {sid}"
                        center = float(row.get("time") or 0)
                        start = float(row.get("windowStart") or center)
                        end = float(row.get("windowEnd") or center)
                        self.cooldown_text.insert(
                            "end",
                            f"분산 사용 보정 · {name} {center:.1f}s 중심 · "
                            f"구간 {start:.1f}~{end:.1f}s · "
                            f"{int(row.get('n') or 0)}/{int(row.get('eligible') or 0)} "
                            f"({float(row.get('support') or 0)*100:.0f}%)\n",
                            "note",
                        )
                for warning in (sel.get("warnings") or []):
                    self.cooldown_text.insert("end", f"⚠ {warning}\n", "note")
                self.cooldown_text.insert("end", "\n", "note")

            for e in timeline:
                sec = float(e.get("time") or 0)
                m = int(sec // 60)
                ss = sec - m * 60
                self.cooldown_text.insert(
                    "end",
                    f"{m:02d}:{ss:04.1f}  {e.get('spell')}  ",
                    "spell",
                )
                if e.get("consensusMatched"):
                    support = float(e.get("support") or 0)
                    n = int(e.get("consensusN") or 0)
                    eligible = int(e.get("consensusEligible") or 0)
                    center = float(e.get("consensusTime") or sec)
                    delta = sec - center
                    confidence_word = "합의" if support >= 0.35 else "분산"
                    self.cooldown_text.insert(
                        "end",
                        f"| {confidence_word} {n}/{eligible} ({support*100:.0f}%) · "
                        f"공통점 대비 {delta:+.1f}s\n",
                    )
                else:
                    self.cooldown_text.insert("end", "| 합의 클러스터 없음\n", "note")
        elif entry_type == "consensus" or label.startswith("상위 공통") or label.startswith("합의 지도"):
            for e in timeline:
                sec = float(e.get("time") or 0)
                m = int(sec // 60)
                ss = sec - m * 60
                self.cooldown_text.insert(
                    "end",
                    f"{m:02d}:{ss:04.1f}  {e.get('spell')} #{e.get('occurrence')}  ",
                    "spell",
                )
                self.cooldown_text.insert(
                    "end",
                    f"| 중앙 {sec:.1f}s | 25~75% {float(e.get('q25') or sec):.1f}~"
                    f"{float(e.get('q75') or sec):.1f}s | 사용 {e.get('n')}/{e.get('eligible')} "
                    f"({float(e.get('support') or 0)*100:.0f}%)\n",
                )
        else:
            for e in timeline:
                sec = float(e.get("time") or 0)
                m = int(sec // 60)
                ss = sec - m * 60
                self.cooldown_text.insert(
                    "end",
                    f"{m:02d}:{ss:04.1f}  {e.get('spell')}\n",
                    "spell",
                )

        # B16 Plan vs Actual v3: conservative causal flow correction
        # with the same-fight Live exact casts when available, otherwise WCL local.
        if entry_type in ("representative", "peer") and compare_plan_vs_actual is not None:
            actual_entry = self._cooldown_actual_entry(data)
            if actual_entry:
                spell_names = {
                    int(x.get("spellID")): x.get("spell")
                    for x in (data.get("trackedSpells") or [])
                    if x.get("spellID") is not None
                }
                spell_kinds = {
                    int(x.get("spellID")): str(x.get("kind") or "spell")
                    for x in (data.get("trackedSpells") or [])
                    if x.get("spellID") is not None
                }
                for e in (actual_entry.get("timeline") or []):
                    if e.get("spellID") is not None and e.get("spell"):
                        spell_names[int(e.get("spellID"))] = e.get("spell")
                try:
                    pva = compare_plan_vs_actual(
                        entry, actual_entry,
                        phase_policy=data.get("phasePolicy") or {},
                        selected_ids=self.cooldown_selected_ids,
                        spell_names=spell_names,
                        spell_kinds=spell_kinds,
                        consensus_clusters=data.get("consensusClusters") or [],
                        peers=data.get("peers") or [],
                    )
                except Exception as exc:
                    pva = None
                    self.cooldown_text.insert("end", f"\nPlan vs Actual 계산 실패: {exc}\n", "note")
                if pva:
                    ps = pva.get("summary") or {}
                    self.cooldown_text.insert("end", "\n=== Plan vs Actual ===\n", "title")
                    actual_source = pva.get("actualSource") or "local"
                    source_label = "Live exact cast" if actual_source == "live-selector-exact-cast" else "WCL local"
                    self.cooldown_text.insert(
                        "end",
                        f"계획: {pva.get('planLabel')}\n실제: {pva.get('actualLabel')} · {source_label}\n",
                        "note",
                    )
                    self.cooldown_text.insert(
                        "end",
                        f"매칭 {ps.get('matched',0)} · 정상 {ps.get('ok',0)} · 타이밍 점검 {ps.get('timingCheck',0)} · "
                        f"미사용 {ps.get('missed',0)} · 추가사용 {ps.get('extra',0)} · "
                        f"속도차 판정보류 {ps.get('uncertainSpeed',0)} · "
                        f"사망구간 제외 {ps.get('afterPlayerDeath',0)} · 전투종료 제외 {ps.get('afterActualEnd',0)} · 계획종료 뒤 제외 {ps.get('afterPlanEnd',0)}\n",
                        "note",
                    )
                    if pva.get("coverageNote"):
                        self.cooldown_text.insert("end", f"※ {pva.get('coverageNote')}\n", "note")
                    if pva.get("speedNote"):
                        self.cooldown_text.insert("end", f"※ {pva.get('speedNote')}\n", "caution")
                    self.cooldown_text.insert(
                        "end",
                        "※ 판정: 검증된 phase 상대시간 > 최근 60초의 과거·정상 시전 3기술 이상 흐름 보정 > (공백 없는 경우만) 직전 동일 기술 간격 > 절대시간 순입니다.\n※ 흐름값에는 미래 실제 시전, 같은 버스트(±1.5초), 절대 허용오차를 넘긴 CHECK 시전을 넣지 않습니다.\n",
                        "note",
                    )
                    if (pva.get("potionSlotPolicy") or {}).get("enabled"):
                        self.cooldown_text.insert(
                            "end",
                            "※ 공격용 물약은 종류가 달라도 '전투 물약' 한 슬롯으로 비교합니다. 생명력 물약은 제외됩니다.\n",
                            "note",
                        )
                    self.cooldown_text.insert("end", "\n", "note")
                    incidents = pva.get("incidents") or []
                    main_incidents = [x for x in incidents if x.get("mainActionEligible")]
                    ref_incidents = [x for x in incidents if not x.get("mainActionEligible")]
                    if main_incidents:
                        self.cooldown_text.insert("end", "=== 먼저 볼 사건 ===\n", "title")
                        for n, inc in enumerate(main_incidents[:5], 1):
                            tag = "warn" if int(inc.get("lossCount") or 0) > 0 else "caution"
                            self.cooldown_text.insert("end", f"{n}. {inc.get('title')}\n", tag)
                            if inc.get("action"):
                                self.cooldown_text.insert("end", f"   → {inc.get('action')}\n", "note")
                        self.cooldown_text.insert("end", "\n", "note")
                    if ref_incidents:
                        self.cooldown_text.insert("end", "=== 참고 (우선 교정 아님) ===\n", "title")
                        for inc in ref_incidents[:4]:
                            self.cooldown_text.insert("end", f"· {inc.get('title')}\n", "note")
                        self.cooldown_text.insert("end", "\n", "note")

                    self.cooldown_text.insert("end", "=== 시전별 상세 ===\n", "title")
                    basis_name = {"absolute":"절대", "interval":"간격", "phase":"페이즈", "flow":"흐름보정"}
                    for row in pva.get("rows") or []:
                        kind = row.get("kind")
                        status = row.get("status")
                        row_label = row.get("spell")
                        if row.get("spellDetail") and row.get("spellDetail") != row_label:
                            row_label = f"{row_label} ({row.get('spellDetail')})"
                        if status == "AFTER_PLAYER_DEATH":
                            self.cooldown_text.insert("end", f"{row_label} · 사망 구간 · 비교 제외\n", "note")
                            continue
                        if kind == "matched":
                            pt = float(row.get("planTime") or 0)
                            at = float(row.get("actualTime") or 0)
                            pm, ps2 = int(pt//60), pt%60
                            am, as2 = int(at//60), at%60
                            if status == "UNCERTAIN_SPEED":
                                self.cooldown_text.insert(
                                    "end",
                                    f"{row_label}  {pm:02d}:{ps2:04.1f} → {am:02d}:{as2:04.1f}  "
                                    f"| 킬 속도 차이로 판정 보류 (원판정 {row.get('originalStatus') or '-'})\n",
                                    "note",
                                )
                                continue
                            if status == "AFTER_PLAN_END":
                                self.cooldown_text.insert(
                                    "end",
                                    f"{row_label}  {pm:02d}:{ps2:04.1f} → {am:02d}:{as2:04.1f}  "
                                    "| 대표 로그 종료 뒤 사용 · 비교 제외\n",
                                    "note",
                                )
                                continue
                            judge = float(row.get("judgeDeltaSec") or 0)
                            absd = float(row.get("absoluteDeltaSec") or 0)
                            bname = basis_name.get(row.get("judgeBasis"), str(row.get("judgeBasis") or "-"))
                            tag = "ok" if status == "OK" else "caution"
                            flow_txt = ""
                            if row.get("judgeBasis") == "flow" and row.get("flowShiftSec") is not None:
                                flow_txt = (
                                    f" (흐름 {float(row.get('flowShiftSec')):+.1f}s · "
                                    f"{int(row.get('flowSpellCount') or 0)}기술/{int(row.get('flowSampleCount') or 0)}시전)"
                                )
                            self.cooldown_text.insert(
                                "end",
                                f"{row_label}  {pm:02d}:{ps2:04.1f} → {am:02d}:{as2:04.1f}  "
                                f"| 절대 {absd:+.1f}s · {bname} {judge:+.1f}s{flow_txt} · {status}\n",
                                tag,
                            )
                        elif status == "UNCERTAIN_SPEED":
                            pt = row.get("planTime")
                            at = row.get("actualTime")
                            if pt is not None and at is not None:
                                pt = float(pt); at = float(at)
                                self.cooldown_text.insert(
                                    "end",
                                    f"{row_label}  {int(pt//60):02d}:{pt%60:04.1f} → 대응 {int(at//60):02d}:{at%60:04.1f}  "
                                    "| 킬 속도 차이로 판정 보류\n",
                                    "note",
                                )
                            elif pt is not None:
                                pt = float(pt)
                                self.cooldown_text.insert(
                                    "end", f"{row_label}  {int(pt//60):02d}:{pt%60:04.1f} · 킬 속도 차이로 미사용 판정 보류\n", "note"
                                )
                            elif at is not None:
                                at = float(at)
                                self.cooldown_text.insert(
                                    "end", f"{row_label}  실제 {int(at//60):02d}:{at%60:04.1f} · 킬 속도 차이로 추가사용 판정 보류\n", "note"
                                )
                        elif status == "MISSED":
                            pt = float(row.get("planTime") or 0)
                            self.cooldown_text.insert(
                                "end",
                                f"{row_label}  {int(pt//60):02d}:{pt%60:04.1f} → 미사용 · MISSED\n",
                                "warn",
                            )
                        elif status == "EXTRA":
                            at = float(row.get("actualTime") or 0)
                            self.cooldown_text.insert(
                                "end",
                                f"{row_label}  계획 없음 → {int(at//60):02d}:{at%60:04.1f} · EXTRA\n",
                                "caution",
                            )

        phase_segments = entry.get("phaseSegments") or []
        phase_audit = entry.get("phaseExportAudit") or data.get("phaseExportAudit") or {}
        if phase_audit.get("messageKo"):
            self.cooldown_text.insert("end", "\n=== NSRT Phase 출력 상태 ===\n", "title")
            style = "caution" if not phase_audit.get("verified") else "note"
            self.cooldown_text.insert("end", str(phase_audit.get("messageKo")) + "\n", style)
            if phase_audit.get("riskKo"):
                self.cooldown_text.insert("end", "⚠ " + str(phase_audit.get("riskKo")) + "\n", "caution")

        if phase_segments:
            self.cooldown_text.insert("end", "\n=== Phase 시작점 ===\n", "title")
            for s in phase_segments:
                semantic = s.get("semanticPhaseID", s.get("phaseID"))
                semantic_occ = s.get("semanticOccurrence")
                extra = ""
                if semantic is not None:
                    extra = f" · WCL id {semantic}"
                    if semantic_occ:
                        extra += f" ({semantic_occ}번째)"
                self.cooldown_text.insert(
                    "end",
                    f"ph:{s.get('phase','1')}  start {float(s.get('start') or 0):.1f}s{extra}\n",
                    "note",
                )

        phase_candidate = data.get("phaseValidationCandidate") or {}
        if phase_candidate:
            self.cooldown_text.insert("end", "\n=== Phase 검증 후보 ===\n", "title")
            self.cooldown_text.insert("end", "아직 phase-relative NSRT로 자동 승격하지 않았습니다.\n", "caution")
            raw = entry.get("rawPhaseTransitions") or []
            raw_starts = [float(x.get("startSec") or 0.0) for x in raw if x.get("inFightRange", True)]
            chk = data.get("representativePhaseCandidateCheck") or {}
            ordinal_starts = [float(x) for x in (chk.get("actualStartsSec") or [])]
            if raw_starts:
                self.cooldown_text.insert("end", "WCL rawPhaseTransitions 원본: " + ", ".join(f"{x:.1f}s" for x in raw_starts) + "\n", "note")
            if ordinal_starts:
                self.cooldown_text.insert("end", "ordinal 검증 시작점: " + ", ".join(f"{x:.1f}s" for x in ordinal_starts) + "\n", "note")
            if entry.get("entryType") == "representative" and chk.get("status"):
                self.cooldown_text.insert("end", f"역산 기대값 대조: {chk.get('status')}\n", "note")
                if chk.get("status") == "NO_EXPECTATION_SOURCE":
                    self.cooldown_text.insert("end", "역산 기준 reportCode/fightID가 없어 다른 대표와 시각 대조하지 않습니다.\n", "note")
                elif chk.get("status") == "DIFFERENT_REPRESENTATIVE":
                    self.cooldown_text.insert("end", "현재 대표가 역산 기준 로그와 달라 MATCH 판정을 생략합니다.\n", "note")
            if data.get("phaseCoverageWarning"):
                self.cooldown_text.insert("end", f"⚠ {data.get('phaseCoverageWarning')}\n", "caution")
        elif (data.get("phasePolicy") or {}).get("mode") in ("transition_labels", "transition_profiles", "semantic_id_ordinal"):
            self.cooldown_text.insert("end", "\n=== Phase 정책 · VERIFIED ===\n", "title")
            ref_src = (data.get("phasePolicy") or {}).get("referenceSource")
            if ref_src:
                self.cooldown_text.insert("end", f"NSRT label reference: {ref_src}\n", "note")
            self.cooldown_text.insert("end", "※ WCL phase 관측값과 NSRT ph 라벨은 별도 계층으로 매핑합니다.\n", "note")
            chk = data.get("verifiedPhaseReferenceCheck") or {}
            if entry.get("entryType") == "representative" and chk.get("status"):
                self.cooldown_text.insert("end", f"검증 기준 로그 대조: {chk.get('status')}\n", "note")
            if data.get("phaseCoverageWarning"):
                self.cooldown_text.insert("end", f"⚠ {data.get('phaseCoverageWarning')}\n", "caution")

        inter_rows = entry.get("intermissionCasts") or []
        if inter_rows:
            self.cooldown_text.insert("end", "\n=== 인터미션 시전 주의 ===\n", "title")
            self.cooldown_text.insert(
                "end",
                "NSRT ph 번호에는 포함하지 않는 WCL sub-phase 안의 시전입니다. "
                "기본 출력은 Logs와 같이 이전 ph 기준 시간을 유지합니다.\n",
                "caution",
            )
            for ir in inter_rows[:10]:
                self.cooldown_text.insert(
                    "end",
                    f"{ir.get('spell')} · 절대 {self._cooldown_fmt_axis_time(float(ir.get('time') or 0))} · "
                    f"ph:{ir.get('phase')} +{float(ir.get('phaseTime') or 0):.1f}s · "
                    f"다음 ph:{ir.get('nextPhaseOrdinal')}까지 {float(ir.get('secondsToNextPhase') or 0):.1f}s\n",
                    "caution",
                )
            if getattr(self, "cooldown_intermission_snap_var", None) and self.cooldown_intermission_snap_var.get():
                self.cooldown_text.insert(
                    "end",
                    "✓ 현재 복사 옵션: 인터미션 후반부의 딜 쿨기만 다음 phase 0.0초로 스냅합니다. Utility/생존기는 원래 위치를 유지합니다.\n",
                    "note",
                )
            else:
                self.cooldown_text.insert(
                    "end",
                    "※ 필요하면 위 [인터미션 딜쿨→다음 ph 0.0] 옵션을 켤 수 있습니다. 같은 기술은 인터미션당 다음 phase에 가장 가까운 1회만 이동합니다.\n",
                    "note",
                )

        self.cooldown_text.insert("end", "\n=== 최종 NSRT 메모 ===\n", "title")
        if entry.get("nsrtExportAllowed", True):
            self.cooldown_text.insert("end", self.build_filtered_nsrt(entry) + "\n")
        else:
            self.cooldown_text.insert(
                "end",
                "합의 지도는 서로 다른 사람의 대안 타이밍이 섞일 수 있어 NSRT로 직접 내보내지 않습니다.\n"
                "맨 위의 [추천 대표] 또는 개별 비교 로그를 선택하세요.\n",
                "note",
            )
        self.cooldown_text.insert(
            "end",
            "\n※ 추천 대표/후보 NSRT는 한 사람의 실제 로그 시퀀스라 쿨타임/충전 제약을 깨지 않습니다.\n"
            "※ 합의 점수는 기술별로 먼저 평균해 어춤처럼 사용 횟수가 많은 기술의 과대 가중을 막습니다.\n"
            "※ 소멸처럼 중반 사용 시점이 넓게 퍼지는 경우 분산 사용 구간을 복원해 대표 선정에만 반영합니다.\n"
            "※ 지지율 70% 이상 핵심 타이밍 또는 다수의 분산 사용 구간을 놓친 후보는 경고와 감점이 표시됩니다.\n"
            "※ 순위점수는 WCL 원순위 숫자가 아니라 현재 비교군 안에서의 상대 순서로 계산합니다.\n"
            "※ 합의 지도는 대표 선정과 타이밍 신뢰도 확인에만 사용합니다.\n"
            "※ NSRT ph:는 WCL 내부 phaseTransitions를 자동 변환하지 않습니다.\n"
            "※ 검증되지 않은 보스는 ph:1 + 전투 시작 기준 절대시간으로 fallback합니다. NSRT가 현재 phase 조건만 읽으면 P1 이후 알림이 누락될 수 있어 미리보기에 경고합니다.\n"
            "※ 실제 phase-relative 형식이 확인된 보스만 검증 프로필에 따라 시간을 리셋합니다.\n"
            "※ transition-label 정책에서 짧은 풀로 후반 phase가 미관측된 경우, 허용된 보스는 확인된 앞쪽 label까지만 prefix로 출력합니다.\n"
            "※ semantic ordinal 보스의 제외 sub-phase(인터미션) 시전은 기본적으로 이전 ph 기준을 유지합니다. 옵션 스냅은 인터미션 후반부 딜 쿨기에만 적용되고 Utility/생존기는 이동하지 않습니다.\n"
            "※ 위 아이콘에서 켠 항목만 NSRT에 포함됩니다. 물약은 기본 OFF이며 필요하면 직접 켤 수 있습니다.\n"
            "※ [원본/Phase 복사]는 절대시간 + 검증된 Phase 시작점 디버그용입니다.\n",
            "note",
        )
        self.cooldown_text.see("1.0")
        self._refresh_cooldown_modern_preview(entry, label)
        self._sync_modern_cooldown_options()
        self.schedule_cooldown_heatmap_redraw()

    def on_cooldown_choice(self, _event=None):
        label = self.cooldown_choice_var.get()
        if hasattr(self, "cooldown_plan_choice_var"):
            self.cooldown_plan_choice_var.set(label)
        self.render_cooldown_entry(label)

    def copy_nsrt(self):
        label = self.cooldown_choice_var.get()
        entry = self.cooldown_entries.get(label)
        if not entry:
            messagebox.showinfo("NSRT", "먼저 쿨기 타임라인을 생성하세요.")
            return
        if not entry.get("nsrtExportAllowed", True):
            messagebox.showinfo(
                "합의 지도",
                "합의 지도는 여러 사람의 대안 타이밍이 섞일 수 있어 NSRT로 복사하지 않습니다.\n"
                "추천 대표 또는 개별 로그를 선택하세요.",
            )
            return
        nsrt = self.build_filtered_nsrt(entry)
        self.clipboard_clear()
        self.clipboard_append(nsrt)
        self.update()
        audit = entry.get("phaseExportAudit") or (self.cooldown_data or {}).get("phaseExportAudit") or {}
        if audit.get("verified") is False and audit.get("messageKo"):
            self.status_var.set(f"NSRT 메모 복사 완료 · ⚠ {audit.get('messageKo')}")
        else:
            self.status_var.set(f"NSRT 메모 복사 완료 · {label}")

    def copy_mrt_debug(self):
        label = self.cooldown_choice_var.get()
        entry = self.cooldown_entries.get(label)
        if not entry:
            messagebox.showinfo("Phase", "먼저 쿨기 타임라인을 생성하세요.")
            return
        raw = self.build_filtered_mrt(entry)
        self.clipboard_clear()
        self.clipboard_append(raw)
        self.update()
        self.status_var.set(f"원본/Phase 복사 완료 · {label}")

    def start_cooldown_analysis(self):
        if self.cooldown_running:
            messagebox.showinfo("쿨기 분석 중", "이미 쿨기/NSRT 생성이 진행 중입니다.")
            return
        if self.precision_running:
            messagebox.showinfo("정밀 분석 중", "정밀 분석이 끝난 뒤 쿨기/NSRT를 생성하세요.")
            return
        if self.proc is not None and self.proc.poll() is None:
            messagebox.showinfo("기본 분석 중", "기본 분석이 끝난 뒤 쿨기/NSRT를 생성하세요.")
            return

        b = self._current_cooldown_boss()
        if not b:
            messagebox.showinfo("보스 선택", "왼쪽 세션 탐색에서 보스를 먼저 선택하세요.")
            return
        if int(b.get("difficulty", 0)) not in WCL_DIFFICULTY_MAP:
            messagebox.showinfo("분석 제외", "영웅/신화 레이드 보스만 지원합니다.")
            return
        boss_root = self.find_boss_root(b)

        needed = [
            ENGINE / "fetch_precision_comparators.py",
            ENGINE / "build_cooldown_nsrt.py",
            ENGINE / "wcl_api.py",
        ]
        missing = [str(x) for x in needed if not x.exists()]
        if missing:
            messagebox.showerror("쿨기 엔진 없음", "필수 파일이 없습니다:\n" + "\n".join(missing))
            return

        self.cooldown_running = True
        self.cooldown_btn.configure(state="disabled")
        self.precision_btn.configure(state="disabled")
        self.run_btn.configure(state="disabled")
        self._progress_begin("쿨기/NSRT", "cooldown")
        self.cooldown_text.delete("1.0", "end")
        self.cooldown_text.insert("end", f"{b.get('boss')} 상위 쿨기 타임라인을 생성합니다...\n", "note")
        self.notebook.select(self.cooldown_frame)
        job = {
            "token": self._selection_token(),
            "dataset": Path(self.current_dataset) if self.current_dataset else None,
            "player": str(self.current_player),
            "bossKey": self._boss_context_key(b),
        }
        self.cooldown_origin_token = job["token"]

        threading.Thread(
            target=self._cooldown_worker,
            args=(dict(b), boss_root, job),
            daemon=True,
        ).start()

    def _cooldown_worker(self, b: dict, boss_root: Path | None, job: dict):
        try:
            dataset = Path(job["dataset"]) if job.get("dataset") else None
            player = str(job["player"])
            sid = int(b.get("specID"))
            encounter = int(b.get("encounterID"))
            local_diff = int(b.get("difficulty"))
            wcl_diff = WCL_DIFFICULTY_MAP[local_diff]
            difficulty_name = "Mythic" if local_diff in {16, 233} else "Heroic"
            boss_name = str(b.get("boss") or "Boss")
            safe = re.sub(r'[<>:"/\\|?* ]+', "_", boss_name).strip("_") or "boss"

            out_base = self._comparator_base("precision_cache", sid, f"{encounter}_{safe}_d{local_diff}_top50_nonCNTW_v22phase", dataset=dataset)
            comp_zip = Path(str(out_base) + ".zip")

            if not comp_zip.exists():
                # B37.3.0: Historical/Live navigation can legitimately have no
                # analyzed local boss subset. Comparator collection must stay
                # usable in that mode; local duration/outcome are optional
                # enrichment only. Do not pass None into Path arithmetic.
                local_duration = None
                local_outcome = None
                if boss_root is not None:
                    local_duration, local_outcome = precision_local_fight_meta(boss_root)
                cmd = [
                    str(PYTHON), str(ENGINE / "fetch_precision_comparators.py"),
                    "--encounter", str(encounter),
                    "--difficulty", str(wcl_diff),
                    "--spec", str(sid),
                    "--rank-pool", "50",
                    "--detail-count", "30",
                    "--min-count", "15",
                    "--out", str(out_base),
                ]
                if local_duration is not None:
                    cmd += ["--local-duration", f"{local_duration:.3f}"]
                if local_outcome:
                    cmd += ["--local-outcome", local_outcome]
                self._precision_cmd(
                    cmd,
                    "쿨기/NSRT · Top50 비교군 수집 중...",
                    span=(0.0, 70.0), owner="cooldown",
                )
                build_span = (70.0, 99.0)
            else:
                self.msgq.put(("cooldown_status", "쿨기/NSRT · 기존 Top50 비교군 캐시 재사용"))
                self.msgq.put(("progress", (3.0, "비교군 캐시 재사용", "cooldown")))
                build_span = (3.0, 99.0)

            if boss_root is not None:
                build_root = boss_root
                out_dir = boss_root / "analysis" / player / "cooldown_nsrt"
            else:
                build_root = self.comparator_only_cooldown_root(b)
                out_dir = build_root
                self.msgq.put(("cooldown_status", "쿨기/NSRT · 로컬 보스 폴더 없음 · WCL 비교군 전용 생성"))
            self._precision_cmd(
                [
                    str(PYTHON), str(ENGINE / "build_cooldown_nsrt.py"),
                    "--root", str(build_root),
                    "--player", player,
                    "--spec", str(sid),
                    "--comparators", str(comp_zip),
                    "--encounter", str(encounter),
                    "--difficulty", difficulty_name,
                    "--name", boss_name,
                    "--out", str(out_dir),
                ],
                "쿨기/NSRT · 합의 점수로 대표 실제 로그 선정 중...",
                span=build_span, owner="cooldown",
            )

            self.msgq.put(("cooldown_done", {"path": str(out_dir / "cooldown_timeline.json"), "token": job.get("token"), "bossKey": job.get("bossKey")}))
        except Exception as e:
            self.msgq.put(("cooldown_error", {"error": str(e), "token": job.get("token")}))

    def open_report_folder(self):
        folder = self.report_dir()
        if not folder or not folder.exists():
            messagebox.showinfo("결과 없음", "아직 결과 폴더가 없습니다.")
            return
        try:
            os.startfile(str(folder))
        except Exception as e:
            messagebox.showerror("폴더 열기 실패", str(e))


def _run_personal_edition():
    # The FULL/PRIVATE edition has a separate Windows taskbar identity.
    # Public launcher uses brand_public through nini_raid_gui.py.
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NINI.Analyzer.Personal")
        except Exception:
            pass
    app = App(edition="full")
    # Apply the approved GOLD icon to both Tk and the mapped Windows HWND.
    from _nini_win_icon import install_icon
    install_icon(app, WORKDIR / "resources" / "brand_personal")
    app.mainloop()


if __name__ == "__main__":
    raise SystemExit("Use nini_raid_gui.py to launch this public-only distribution")
