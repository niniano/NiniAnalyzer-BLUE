"""V3.8 display-only wording for collapse row; NEVER promotes B31 candidates to direct cause.

Input is cached, previously saved V3.7 per-pull index and optional existing UI model.
No file I/O, no combat log processing, no player blame. The 4-line layout is unchanged.
"""
from __future__ import annotations
import math
from typing import Any


def _num(value):
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
        return f if math.isfinite(f) and f >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def clock(value):
    f = _num(value)
    return f'{int(f // 60)}:{f % 60:06.3f}' if f is not None else '시각 미확인'


def _compact(value: Any, limit=34):
    s = ' '.join(str(value or '').split())
    return s if len(s) <= limit else s[:limit - 1] + '…'


def collapse_display(row: dict, *, matched_record=None, model=None, old_value='') -> dict:
    """Return new display for non-confirmed causal claim, or mode='leave' for proven direct.

    Evidence standards:
      - Explicit collapseDirect must be verified and link >=4 deaths within 1s.
      - B31 single_root/raidPrimaryFinding means a *candidate*, never direct cause.
      - V3.7 record is accepted only after strict sourceKey/identity matching in caller.
    """
    if not isinstance(row, dict) or not row:
        return {'mode': 'skip'}
    result = str(row.get('result') or '').casefold()
    if result == 'kill' or row.get('isFake') or row.get('isReset') or row.get('resetExcluded'):
        return {'mode': 'skip'}
    model = model if isinstance(model, dict) else {}
    direct = model.get('collapseDirect')
    if isinstance(direct, dict) and str(direct.get('evidenceLevel') or '').lower() == 'verified':
        # Only trust an explicit death connection: no over-promotion of generic label.
        deaths = direct.get('linkedDeaths')
        if isinstance(deaths, list) and len(deaths) >= 4:
            points = sorted((t, name) for d in deaths if isinstance(d, dict)
                            for t, name in [(_num(d.get('deathSec')), str(d.get('player') or ''))]
                            if t is not None and name)
            for t, _ in points:
                if len({name for tt, name in points if t <= tt <= t + 1.0}) >= 4:
                    return {'mode': 'leave'}

    if isinstance(matched_record, dict):
        collapse = matched_record.get('collapse') or {}
        first = matched_record.get('first') or {}
        causal = matched_record.get('causal') or {}
        ct = _num(collapse.get('sec'))
        ft = _num(first.get('sec'))
        bt = _num(causal.get('sec'))
        count = collapse.get('count')
        if isinstance(count, int) and not isinstance(count, bool) and count > 0 and ct is not None:
            value = f'{count}명 사망 집중 · 원인 미확정'
            note = clock(ct)
            if bt is not None and ft is not None and bt < ft - 1:
                note += f' · 첫 기믹 실패보다 {ft - bt:.3f}초 앞서 사망 원인 후보 관측'
            elif causal.get('label'):
                note += ' · B31 원인 후보: ' + _compact(causal['label'], 29)
            else:
                note += ' · 원인 연결 근거 없음'
            return {'mode': 'change', 'head': '붕괴 관측', 'value': value, 'note': note}
        if causal.get('label'):
            return {'mode': 'change', 'head': '붕괴 관측', 'value': '공대 사망 원인 미확정',
                    'note': 'B31 원인 후보: ' + _compact(causal['label'], 31) + ' · 대량 사망 시각 미관측'}
        return {'mode': 'change', 'head': '붕괴 관측', 'value': '붕괴 원인 확인 불가', 'note': '전투 원본과 직접 인과 연결 미확정'}

    # Other dates/bosses: do not borrow V3.7 entries from a same-number different pull.
    if str(old_value or '').strip() and str(old_value).strip() not in ('확인 불가', '확인 대기', '적용 안 함'):
        return {'mode': 'change', 'head': '붕괴 관측', 'value': '공대 붕괴 원인 미확정',
                'note': '기존 분석 후보: ' + _compact(old_value, 30)}
    return {'mode': 'change', 'head': '붕괴 관측', 'value': '원인 확인 불가',
            'note': '검증된 직접 인과관계 없음'}
