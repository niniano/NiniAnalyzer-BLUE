"""공대 계획 (1.0.19) — 공대마다 택틱이 달라서 보스 판정 시각을 공대별로 둔다.

계획 파일 raid_plans.json (먼저 찾은 것 하나만 씀):
  1. 이 모듈 옆 (공대원 이름으로 공대 자동 구분 · 개발용)
  2. 공개판 데이터 폴더 (BLUE · 「공대 계획」 창에서 저장)

{
  "default": "mine",                       # 공대원이 안 맞을 때 쓸 계획 (없으면 계획 없음)
  "plans": [
    {"id": "mine", "name": "우리 공대",
     "members": ["이름", ...],             # 이 풀 공대원과 MATCH_MIN 명 이상 겹치면 이 계획
     "ulatek": {"bigEggPick": "4:18",      # 큰 알 집는 시각 (비우면 판정 안 함)
                "bigEggPut": "4:30",       # 큰 알 넣는 시각 (+1초 넘으면 늦음)
                "south": "big_first"}}     # 남쪽 구역: shield_first(1번 보호막 알 먼저) · big_first(2번 큰 알 먼저)
  ]
}
계획이 없으면 시각 판정은 하지 않고 보스 기믹으로 정해진 것만 판정한다.
"""
from __future__ import annotations

import json
from pathlib import Path

PLAN_FILE_NAME = "raid_plans.json"
MATCH_MIN = 8
SOUTH_TACTICS = {
    "shield_first": "1번 보호막 알 먼저",
    "big_first": "2번 큰 알 먼저",
}
SOUTH_DESC = {   # 남쪽 구역 = 큰 알 1개 + 보호막 알 2개 (갈래마다)
    "shield_first": "보호막 알 2개를 먼저 깸 → 큰 알 집기 (보호막 알은 다른 사람이 듦) → 같이 가마솥에 넣기 (유체는 넣을 때 나옴)",
    "big_first": "보호막 알은 두고 큰 알부터 집음 → 보호막 알이 산란무리 → 남쪽에서 유체 (유체 광폭 조심)",
}


def _short(name) -> str:
    return str(name or "").split("-")[0].strip()


def parse_mmss(v) -> float | None:
    """'4:18' · '4:18.5' · 258 → 초. 비었거나 이상하면 None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    s = str(v).strip()
    try:
        if ":" in s:
            m, sec = s.split(":", 1)
            out = int(m) * 60 + float(sec)
        else:
            out = float(s)
    except ValueError:
        return None
    return out if out > 0 else None


def mmss(sec) -> str:
    sec = float(sec)
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def plan_paths() -> list[Path]:
    paths = [Path(__file__).resolve().parent / PLAN_FILE_NAME]
    try:
        from live.public_paths import public_home   # BLUE 에만 있음
        paths.append(public_home() / PLAN_FILE_NAME)
    except Exception:
        pass
    return paths


def load(paths=None) -> dict:
    for p in (paths if paths is not None else plan_paths()):
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("plans"), list):
            return data
    return {"plans": []}


def save(data: dict, path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def ulatek_view(plan: dict | None, how: str = "") -> dict | None:
    """계획 하나 → 울라텍 판정에 쓰는 값 (초 단위)."""
    if not plan:
        return None
    u = plan.get("ulatek") or {}
    south = u.get("south") if u.get("south") in SOUTH_TACTICS else None
    return {"id": str(plan.get("id") or ""), "name": str(plan.get("name") or plan.get("id") or "공대 계획"),
            "how": how, "bigEggPick": parse_mmss(u.get("bigEggPick")), "bigEggPut": parse_mmss(u.get("bigEggPut")),
            "south": south}


def resolve(roster_names=None, data: dict | None = None) -> dict | None:
    """이 풀 공대원으로 계획 고르기 → 울라텍 값. 못 고르면 default 계획, 그것도 없으면 None."""
    data = load() if data is None else data
    plans = [p for p in data.get("plans") or [] if isinstance(p, dict)]
    if roster_names:
        present = {_short(n) for n in roster_names if n}
        best, best_n = None, 0
        for p in plans:
            n = len(present & {_short(m) for m in p.get("members") or []})
            if n > best_n:
                best, best_n = p, n
        if best is not None and best_n >= MATCH_MIN:
            return ulatek_view(best, "공대원")
    dflt = data.get("default")
    if dflt:
        p = next((p for p in plans if p.get("id") == dflt), None)
        if p:
            return ulatek_view(p, "기본")
    return None


def plan_line(view: dict | None) -> str:
    """화면 한 줄: 어떤 계획으로 판정했나."""
    if not view:
        return "공대 계획 없음 · 큰 알 시각 판정 안 함 (기믹만 판정)"
    bits = []
    if view.get("bigEggPick") is not None:
        bits.append(f"큰 알 {mmss(view['bigEggPick'])} 집기")
    if view.get("bigEggPut") is not None:
        bits.append(f"{mmss(view['bigEggPut'])}쯤 넣기")
    if view.get("south"):
        bits.append("남쪽 " + SOUTH_TACTICS[view["south"]])
    return f"{view['name']}" + (" · " + " · ".join(bits) if bits else " · 시각 미정")
