"""V4.0: three-line, read-only raid-glance projection.

The values are ALREADY RENDERED by V3.8/V3.9. No log I/O and no new verdict.
Return 'None' for unsupported/empty cases: the original detail remains visible.
"""
from __future__ import annotations
import re


def _text(value):
    return " ".join(str(value or "").split())


def _short_detail(value):
    """Keep event names, numbers and uncertainty; never silently truncate evidence."""
    return _text(value)


def summarize(*, first, major, collapse_head, collapse, next_action,
              chronology_explanation="", chronology_visible=False):
    """A textual layout projection, not a classification engine.

    A leading candidate is never promoted to cause. Proven direct collapse stays
    word-for-word. The time-order warning is shown only if the existing card
    explicitly said it (no new causal inference in V4).
    """
    first = _short_detail(first)
    major = _short_detail(major)
    collapse_head = _text(collapse_head)
    collapse = _short_detail(collapse)
    action = _short_detail(next_action)
    explanation = _short_detail(chronology_explanation) if chronology_visible else ""
    if not (first or major or collapse or action):
        return None

    # First mistake and the next row are often the same event; don't repeat it.
    placeholders = {'없음', '확인된 첫 실수 없음', '확인된 주요 사고 없음', '검증 실패 0회', '확인 대기'}
    if first not in placeholders:
        incident = first
    elif major and major not in placeholders:
        incident = major
    else:
        incident = '첫 기믹 실패는 아직 확인되지 않았어.'
    if (major and major not in placeholders and major != first
            and major.split(' · ', 1)[0] != first.split(' · ', 1)[0]):
        # Existing 'major' is independent; retain it without presenting it as a cause.
        incident += f' · 주요 사고: {major}'

    if collapse_head == '붕괴 직결':
        relation = f'직접 연결된 사고: {collapse}' if collapse else '직접 연결 근거를 확인해야 해.'
    elif collapse and collapse not in placeholders:
        relation = collapse
        if '원인' not in relation and '후보' not in relation:
            relation += ' · 전멸과의 직접 연결은 미확정'
    else:
        relation = '대량 사망의 직접 원인은 아직 확인되지 않았어.'

    # A demonstrated earlier-death counterexample must stay visible at first glance.
    if explanation and ('그 실수 탓' in explanation or '직접 연결' in explanation):
        if explanation not in relation:
            relation = relation.rstrip('. ') + '. ' + explanation
    if not action:
        action = '다음 풀에서 확인할 행동을 아직 정하지 못했어.'
    action = re.sub(r'^\s*(?:→|➡)\s*', '', action)
    action = re.sub(r'^다음\s*풀\s*[:：]\s*', '', action).strip()
    if not action:
        action = '다음 풀에서 확인할 행동을 아직 정하지 못했어.'
    return (incident, relation, action)


def source_identity(row):
    """Never use Try number alone to distinguish encounters or display states."""
    if not isinstance(row, dict):
        return None
    try:
        enc = int(row.get('encounterID') or row.get('encounterId'))
        diff = int(row.get('difficultyID') or row.get('difficultyId'))
    except (ValueError, TypeError):
        return None
    timestamp = row.get('startTimestamp')
    if not timestamp or not isinstance(timestamp, str):
        return None
    return f'{enc}|{diff}|{timestamp}'
