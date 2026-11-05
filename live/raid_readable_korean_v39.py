"""V3.9: small, display-only Korean text transformations.

Strictly presentation: does not classify mechanics, deaths, patches, accountability or causation.
No file access. Inputs are existing V3.5/V3.7/V3.8 rendered strings or matched V3.7 records.
Unknown text remains unchanged; unverified causes are always described as candidates.
"""
from __future__ import annotations

import math
import re


def _sec(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) and f >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def clock(v):
    x = _sec(v)
    return f'{int(x // 60)}:{x % 60:06.3f}' if x is not None else '시각 미확인'


def _clean(v, max_len=90):
    text = ' '.join(str(v or '').split())
    return text if len(text) <= max_len else text[:max_len - 1] + '…'


def personal_lines(raw):
    """Convert only V3.5-recognized messages, never infer individual fault."""
    if not isinstance(raw, (list, tuple)):
        return []
    vals = [str(x or '') for x in raw[:3]]
    while len(vals) < 3:
        vals.append('')
    a, b, c = vals
    replacements = {
        '검증된 공대 사고 관련 대상 · 개인 책임 미판정': '공대 사고에 내 캐릭터가 포함돼 있어. 개인 실수인지는 아직 알 수 없어.',
        '공대 사고 후보 관련 대상 기록 · 개인 책임 미판정': '공대 사고 후보에 내 캐릭터가 포함돼 있어. 개인 실수인지는 아직 알 수 없어.',
        '공대 사고 발생 · 내 캐릭터 개인 관련성 미확정': '공대 사고는 있었지만, 내 캐릭터가 관련됐는지는 확인되지 않았어.',
        '공대 첫 실수 미확정 · 개인 책임 판단 없음': '첫 실수는 아직 특정하지 못했어. 내 캐릭터 개인 책임도 판단하지 않았어.',
        '리셋/가짜 풀 · 개인 판정하지 않음': '리셋하거나 제외한 풀이라 개인 실수를 판단하지 않아.',
        '기믹 판정 최신화 대기 · 책임 판정 없음': '판정을 새로 고치기 전이라 개인 실수를 판단하지 않아.',
    }
    a = replacements.get(a, a)
    if b.startswith('개인 사망 원본 검증 완료 · 직접 사인 미확정 (마지막 치명타 미관측 · UNKNOWN 유지)'):
        rem = b.split('UNKNOWN 유지)', 1)[1]
        b = '사망은 확인됐어. 마지막 치명타가 기록되지 않아 정확한 사인은 몰라(UNKNOWN 유지).'
        hit = re.match(r'\s*·\s*마지막 관측 피해\s+(.+)$', rem)
        if hit:
            b += ' 마지막 관측 피해: ' + hit.group(1).strip()
    elif b.startswith('개인 사망 ') or b.startswith('사망 '):
        b = b.replace('개인 사망 ', '사망 ', 1)  # V3.5 now counts all source facts; do not infer from display text
        b = b.replace('쿨기 관측 ', '쿨기 ', 1)
    elif b == '개인 이벤트 저장 사실 미확인 (원본 실시간 재분석 안 함)':
        b = '내 캐릭터의 저장된 개인 기록을 찾지 못했어. 여기서 로그를 다시 분석하지는 않아.'
    if c == '시전·차단·왕파는 기존 상세 기록 참조 · 개인 실수/DPS 손실 자동 판정 없음':
        c = '시전·차단·왕파는 상세 기록에서 확인해. 개인 실수나 DPS 손실은 자동으로 판단하지 않아.'
    return [a, b, c]


def chronology_lines(record):
    """Rephrase *matched* V3.7 saved facts. Never interpret causation as proven."""
    if not isinstance(record, dict) or not record:
        return None
    first = record.get('first') or {}
    causal = record.get('causal') or {}
    collapse = record.get('collapse') or {}
    ft = _sec(first.get('sec'))
    bt = _sec(causal.get('sec'))
    kt = _sec(collapse.get('sec'))
    count = collapse.get('count')
    count = count if type(count) is int and count > 0 else None
    events = []
    if ft is not None:
        events.append((ft, f'{clock(ft)} 첫 기믹 실패: {_clean(first.get("label"),35)}'))
    if bt is not None and (kt is None or abs(bt - kt) > 0.020):
        events.append((bt, f'{clock(bt)} 사망 관련 원인 후보: {_clean(causal.get("label"),35)}'))
    if kt is not None:
        ev = f'{clock(kt)} {count}명 잇따라 사망' if count else f'{clock(kt)} 공대 사망 집중'
        if bt is not None and abs(bt - kt) <= 0.020 and causal.get('label'):
            ev += f' (원인 후보: {_clean(causal["label"],30)})'
        events.append((kt, ev))
    events.sort(key=lambda z: z[0])
    line1 = ' → '.join(s for _, s in events) if events else '시간순으로 확인할 수 있는 사건이 아직 없어.'
    if ft is None:
        line1 = '첫 기믹 실패 미확인 · ' + line1
    if ft is not None and bt is not None and bt < ft - 1:
        line2 = f'사망 원인 후보가 첫 기믹 실패보다 {ft-bt:.3f}초 먼저 나왔어. 앞선 사망을 그 실수 탓으로 볼 수는 없어.'
    else:
        raw = record.get('rawObservations') or {} if record.get('coverage') == 'core9' else {}
        aura = _sec(raw.get('firstResonanceAuraSec')) if isinstance(raw, dict) else None
        if aura is not None and kt is not None and kt >= aura:
            line2 = f'첫 공명({clock(aura)})과 사망 집중 사이 {kt-aura:.3f}초. 두 사건의 직접 연결은 확인되지 않았어.'
        else:
            line2 = '사건 순서는 확인했지만, 무엇이 최종 전멸을 일으켰는지는 아직 확정할 수 없어.'
    if record.get('coverage') == 'core9':
        raw = record.get('rawObservations') or {}
        if not isinstance(raw, dict):
            raw = {}
        reassign = raw.get('deathLinkedSameGuidRetargets100ms')
        success = raw.get('wailSuccessEvents')
        groups = raw.get('wailSuccessTimeGroups20ms')
        extras = []
        if type(reassign) is int:
            extras.append(f'사망 직후 같은 현신 재지정 {reassign}건')
        if type(success) is int and type(groups) is int:
            extras.append(f'통곡 성공 이벤트 {success}건(시각 {groups}묶음)')
        line3 = '원본 이벤트 일부 대조 · ' + ' · '.join(extras) + ' · 충돌한 현신과 개인 책임은 아직 몰라.'
    else:
        line3 = '저장된 요약을 비교했어(원본 이벤트 미대조). 직접 원인과 개인 책임은 아직 몰라.'
    return [line1, line2, line3]


def collapse_wording(head, value, sub):
    """Only reword V3.8 *unconfirmed* mass-death wording; leave verified direct intact."""
    head, value, sub = str(head or ''), str(value or ''), str(sub or '')
    if head != '붕괴 관측':
        return head, value, sub
    match = re.fullmatch(r'(\d+)명 사망 집중 · 원인 미확정', value)
    if match:
        value = f'{match.group(1)}명 잇따라 사망 · 직접 원인 미확정'
    # Preserve source-label provenance as an *unverified candidate*.
    sub = sub.replace('B31 원인 후보:', '분석기 원인 후보:')
    sub = re.sub(r'첫 기믹 실패보다 (\d+\.\d+)초 앞서 사망 원인 후보 관측',
                 r'사망 원인 후보가 첫 기믹 실패보다 \1초 빠름', sub)
    return head, value, sub


def placeholder_wording(value, *, kind):
    value = str(value or '')
    if value == '없음':
        return '확인된 첫 실수 없음' if kind == 'first' else '확인된 주요 사고 없음' if kind == 'major' else value
    return value


CARD_HELP = '✓ 목표 충족 · ✕ 확인된 실패 · ? 아직 판단 못 함 · 숫자는 로그 기록'
TIMELINE_HELP = '아이콘=기믹 발생 · 세로선=페이즈 · 띠=지속 시간/공대 쿨기 · 표식=판정'
