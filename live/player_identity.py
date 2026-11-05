from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from .rawlog import open_raw_text, resolve_raw

IDENTITY_SOURCE = "raw-source-player-v1"
EARLY_CONFIRM_EVENTS = 12


def short_player(value: object) -> str:
    return str(value or "").strip().strip('"').split("-")[0].strip()


def _source_player_from_line(raw: str) -> str | None:
    """Read only the common source GUID/name fields; no full CSV parse required."""
    try:
        payload = raw.split("  ", 1)[1]
    except IndexError:
        return None
    fields = payload.split(",", 3)
    if len(fields) < 3 or not str(fields[1]).strip().startswith("Player-"):
        return None
    return short_player(fields[2]) or None


def infer_tracked_player(raw_path: Path | str, tracked_players) -> dict:
    """Infer which tracked character actually participated in one stored pull.

    Only *source* actors with Player-* GUIDs count. Destination mentions do not.
    Tracked characters are alts of the same user, so once one character has a
    modest run of source events while the others remain at zero we can stop early;
    otherwise the scan continues to EOF and requires a unique highest count.
    """
    tracked = tuple(dict.fromkeys(short_player(p) for p in (tracked_players or ()) if short_player(p)))
    counts = Counter({p: 0 for p in tracked})
    path = resolve_raw(raw_path)
    base = {
        "player": None,
        "counts": dict(counts),
        "ambiguous": False,
        "source": IDENTITY_SOURCE,
        "checkedAt": datetime.now().isoformat(timespec="seconds"),
        "rawAvailable": bool(path.exists()),
        "scannedLines": 0,
        "earlyConfirmed": False,
    }
    if not tracked or not path.exists():
        return base

    wanted = set(tracked)
    scanned = 0
    early_player = None
    try:
        with open_raw_text(path) as fh:
            for raw in fh:
                scanned += 1
                source_player = _source_player_from_line(raw)
                if source_player not in wanted:
                    continue
                counts[source_player] += 1
                positives = [p for p in tracked if counts[p] > 0]
                if len(positives) == 1 and counts[source_player] >= EARLY_CONFIRM_EVENTS:
                    early_player = source_player
                    break
    except (OSError, EOFError):
        pass

    if early_player:
        player = early_player
        ambiguous = False
        early = True
    else:
        positive = sorted(((n, p) for p, n in counts.items() if n > 0), reverse=True)
        if not positive:
            player = None
            ambiguous = False
        elif len(positive) == 1 or positive[0][0] > positive[1][0]:
            player = positive[0][1]
            ambiguous = False
        else:
            player = None
            ambiguous = True
        early = False

    return {
        **base,
        "player": player,
        "counts": {p: int(counts[p]) for p in tracked},
        "ambiguous": ambiguous,
        "scannedLines": scanned,
        "earlyConfirmed": early,
    }


def infer_spec_from_raid_facts(try_dir: Path | str, player: str, meta: dict | None = None):
    """Recover a corrected player's spec from the raid-wide roster when present."""
    try_dir = Path(try_dir)
    meta = meta or {}
    value = meta.get("raidFactsJson") or "raid_facts.json"
    path = Path(str(value))
    if not path.is_absolute():
        path = try_dir / path
    if not path.exists():
        fallback = try_dir / "raid_facts.json"
        path = fallback if fallback.exists() else path
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    wanted = short_player(player)
    for member in (data or {}).get("roster") or []:
        if short_player(member.get("name")) != wanted:
            continue
        try:
            return int(member.get("specID")) if member.get("specID") is not None else None
        except (TypeError, ValueError):
            return None
    return None


def apply_identity_to_meta(meta: dict, result: dict, *, try_dir: Path | str | None = None) -> tuple[dict, bool]:
    """Return migrated metadata and whether it changed.

    Old personal artifacts are not silently rewritten. When the player changes,
    mark them stale so a later explicit analysis can regenerate them against the
    corrected character, while raid-wide artifacts remain valid.
    """
    out = dict(meta or {})
    result = result or {}
    player = short_player(result.get("player"))
    if not player and not result.get("rawAvailable"):
        return out, False

    out["playerIdentitySource"] = result.get("source") or IDENTITY_SOURCE
    out["playerIdentityCounts"] = dict(result.get("counts") or {})
    out["playerIdentityAmbiguous"] = bool(result.get("ambiguous"))
    out["playerIdentityScannedLines"] = int(result.get("scannedLines") or 0)
    out["playerIdentityEarlyConfirmed"] = bool(result.get("earlyConfirmed"))
    if result.get("checkedAt"):
        out["playerIdentityCheckedAt"] = result.get("checkedAt")
    if not player:
        return out, out != (meta or {})

    previous = short_player(out.get("factsPlayer") or out.get("livePlayer"))
    changed_player = bool(previous and previous != player)
    if changed_player and not out.get("factsPlayerOriginal"):
        out["factsPlayerOriginal"] = previous
    out["factsPlayer"] = player
    out["playerIdentityCorrected"] = bool(changed_player)

    if try_dir is not None:
        spec = infer_spec_from_raid_facts(try_dir, player, out)
        if spec is not None:
            out["factsSpecID"] = spec

    if changed_player:
        # facts/death review are player-specific; raid_facts / raid review are not.
        if out.get("factsJson") or out.get("factsTxt"):
            out["factsStatus"] = "stale-player"
        if out.get("deathReviewJson") or out.get("deathReviewTxt"):
            out["deathReviewStatus"] = "stale-player"
        out["playerIdentityReanalysisRequired"] = True
    else:
        out.setdefault("playerIdentityReanalysisRequired", False)
    return out, out != (meta or {})
