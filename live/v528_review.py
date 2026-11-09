"""V5.2.8 · 트라이 화면 보조 (화면 전용 · 판정/저장 데이터 불변).

- 「판정 순서」가 비었을 때 결론 줄([추정] 첫 실수 · 킬 판 사망 정리)과 말이 엇갈리지 않게 안내 문구를 고른다.
- 결론 5줄을 내는 보스(똬리·울라텍·잡은 네임드·키식스)만 해당. 나머지는 예전 문구 그대로.
"""
from __future__ import annotations

VERSION = "V5.2.8-REVIEW"
FIVE_LINE_BOSSES = {3429, 3492, 3420, 3421, 3445, 3455, 3470, 3497, 3513}


def no_judgment_text(row: dict | None) -> str:
    row = row or {}
    try:
        enc = int(row.get("encounterID") or 0)
    except (TypeError, ValueError):
        enc = 0
    if enc not in FIVE_LINE_BOSSES:
        return "검증된 실수 없음"
    if str(row.get("result") or "").lower() == "kill":
        return "엔진이 검증한 실수 없음 · 킬 판 사망은 위 「사망 정리」"
    return "엔진이 검증한 실수 없음 · [추정] 첫 실수는 위 결론 줄"


def fill_duration(model: dict | None, row: dict | None, review: dict | None) -> dict:
    """전용 기믹 판정이 없는 보스(키식스 등)는 모델에 전투 시간이 없어 시간축이 마지막 보스 시전에서 끝났다.
    → 시간축 끝 = 전투 시간 (사망 표시가 오른쪽 끝에 뭉개지지 않게)."""
    model = dict(model or {})
    if model.get("durationSec"):
        return model
    dur = (row or {}).get("durationSec")
    if dur is None:
        dur = ((review or {}).get("encounter") or {}).get("durationSec")
    try:
        if dur is not None and float(dur) > 0:
            model["durationSec"] = float(dur)
    except (TypeError, ValueError):
        pass
    return model
