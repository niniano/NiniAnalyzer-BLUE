"""V3.7 saved-event raid chronology. No causal attribution, no file I/O in build_lines.

A prebuilt index from V3.6 is attached only to an EXACT pull identity.
Calls on Try switches read a single cached small JSON file through V3.5 cache;
never read or parse raw.log/.wcla or invoke the engine.
"""
from __future__ import annotations
import math
from typing import Any

TITLE = '공대 사고 · 시간순 대조 (원인 분리)'

def _sec(v):
    if isinstance(v,bool):return None
    try:
        f=float(v)
        if not math.isfinite(f) or f<0:return None
        return f
    except (TypeError,ValueError,OverflowError):return None

def _clock(v):
    f=_sec(v)
    if f is None:return '시각 미관측'
    return f'{int(f // 60)}:{f % 60:06.3f}'

def _same_identity(row:dict, entry:dict)->bool:
    if not isinstance(row,dict) or not isinstance(entry,dict):return False
    if str(row.get('startTimestamp') or '')!=str(entry.get('startTimestamp') or ''):return False
    try:
        if int(row['encounterID'])!=3429 or int(row['difficultyID'])!=16:return False
        if int(entry['encounterID'])!=3429 or int(entry['difficultyID'])!=16:return False
        if row.get('tryNo') is not None and int(row['tryNo'])!=int(entry['tryNo']):return False
    except (TypeError,ValueError,KeyError):return False
    rkey=str(row.get('sourceKey') or '')
    ekey=str(entry.get('sourceKey') or '')
    if rkey:
        return bool(ekey) and rkey==ekey
    # Live-session rows can omit sourceKey. Require exact complete start and near-exact duration.
    # This is a CONTEXT-only match; no raw personal cause is promoted this way.
    rd=_sec(row.get('durationSec'));ed=_sec(entry.get('durationSec'))
    return rd is not None and ed is not None and abs(rd-ed)<=.005

def _lookup(row,index):
    if not isinstance(row,dict) or not isinstance(index,dict):return None
    if index.get('schemaVersion')!=1 or index.get('combatCount')!=42:return None
    records=index.get('rows')
    if not isinstance(records,dict) or len(records)!=42:return None
    rkey=str(row.get('sourceKey') or '')
    if rkey:
        entry=records.get(rkey)
        return entry if _same_identity(row,entry) else None
    eligible=[x for x in records.values() if _same_identity(row,x)]
    return eligible[0] if len(eligible)==1 else None

def build_lines(row,index):
    """Return {visible, status, lines}; lines are strictly observed chronology, not blame."""
    if not isinstance(row,dict) or not row or row.get('isFake') or row.get('isReset') or row.get('resetExcluded'):
        return {'visible':False,'status':'EXCLUDED','lines':[]}
    record=_lookup(row,index)
    if record is None:return {'visible':False,'status':'NO_MATCH','lines':[]}
    first=record.get('first') or {};causal=record.get('causal') or {};collapse=record.get('collapse') or {}
    ft=_sec(first.get('sec'));ct=_sec(causal.get('sec'));kt=_sec(collapse.get('sec'))
    f=f'첫 검증 기믹 실패 {_clock(ft)} {first.get("label") or ""}' if ft is not None else '첫 검증 기믹 실패 미관측'
    c=f'B31 다수사망 원인 후보 {_clock(ct)} {causal.get("label") or ""}' if ct is not None else 'B31 다수사망 원인 후보 시각 미관측'
    a=f + ' · ' + c
    b=f'붕괴 군집 {_clock(kt)}' if kt is not None else '붕괴 군집 미관측'
    if kt is not None and collapse.get('count') is not None:
        b+=f' ({collapse["count"]}명, 저장 요약 기준)'
    if ft is not None and ct is not None and ct<ft-1.0:
        b+=f' · 선후 역전: 원인 후보가 첫 기믹 실패보다 {ft-ct:.3f}초 앞섬'
    r=record.get('rawObservations') if record.get('coverage')=='core9' else None
    if isinstance(r,dict):
        aura=_sec(r.get('firstResonanceAuraSec'))
        if aura is not None:
            b+=f' · 최초 공명 오라 {_clock(aura)}'
            if kt is not None and kt>=aura:
                b+=f' (붕괴까지 {kt-aura:.3f}초)'
        b+=f' · 사망 근접 동일 GUID 재지정 {int(r.get("deathLinkedSameGuidRetargets100ms") or 0)}건'
        wail=int(r.get('wailSuccessEvents') or 0)
        group=int(r.get('wailSuccessTimeGroups20ms') or 0)
        if wail:
            b+=f' · 통곡 성공 {wail}건/시각 묶음 {group}개'
        proof='V3.6 선별 원본 코어 이벤트 대조'
    else:proof='저장 요약만 대조 (원본 이벤트 미재검증)'
    warning='시각의 선후는 직접 인과관계가 아님 · 충돌한 현신 쌍/개인 책임 미판정 · 게임 빌드 미확인'
    return {'visible':True,'status':'CHRONOLOGY_WARNING' if ft is not None and ct is not None and ct<ft-1.0 else 'OBSERVATIONS_ONLY',
            'lines':[a,b,proof+' · '+warning], 'coverage':record.get('coverage'),
            'gameBuildVerified':False,'directCausationVerified':False}
