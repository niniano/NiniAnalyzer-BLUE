"""B31 evidence-based personal incidents and raid chains, computed from raw facts.

No fixture files are read here. Time proximity alone never creates an upstream
raid root. Raid links require persistent role/population loss plus low-health
collapse, reduced sharing, or a failed encounter end within 15 seconds.
"""
from collections import defaultdict
from statistics import median

from .fact_extractor import DAMAGE_EVENTS, _advanced_target_hp, _event_amount, _damage_overkill
from .mechanics import canonical, role, is_fake_pull
from .versions import INCIDENT_SCHEMA_VERSION

SAFETY = {1966: '교란', 31224: '그림자 망토', 185311: '진홍색 약병',
          45181: '구사일생', 45182: '구사일생', 6262: '생명석'}


def short(name):
    return str(name or '').split('-')[0]


def collect_event(event, fields, sec, sid, name, extra):
    src, dst = fields[1] if len(fields) > 1 else '', fields[5] if len(fields) > 5 else ''
    src_player, dst_player = src.startswith('Player-'), dst.startswith('Player-')
    hit = event in DAMAGE_EVENTS
    heal = event in {'SPELL_HEAL', 'SPELL_PERIODIC_HEAL'}
    safety = sid in SAFETY and ((event == 'SPELL_CAST_SUCCESS' and src_player) or
                               (event.startswith('SPELL_AURA_') and dst_player))
    share = sid == 1288282 and dst_player and event in {'SPELL_ABSORBED', 'SPELL_MISSED'}
    if not ((hit or heal) and dst_player or safety or share):
        return None
    # SPELL_MISSED dodge/resist is not an observed soak; only IMMUNE qualifies.
    if event == 'SPELL_MISSED' and 'IMMUNE' not in fields[extra:]:
        return None
    hp, maximum = _advanced_target_hp(fields, extra) if hit or heal else (None, None)
    # The advanced block can describe another actor in some event formats.
    if hp is not None and fields[extra] != dst:
        hp, maximum = None, None
    return {'sec': sec, 'event': event, 'spellID': sid, 'spellName': name,
            'sourceGUID': src, 'destGUID': dst,
            'destName': short(fields[6]) if dst_player else '',
            'playerGUID': src if event == 'SPELL_CAST_SUCCESS' else dst,
            'currentHP': hp, 'maxHP': maximum,
            'hpPct': 100 * hp / maximum if hp is not None and maximum else None,
            'amount': _event_amount(event, fields, extra) if hit or heal else None,
            'overkill': _damage_overkill(fields, extra) if hit else None}


def _groups(rows, window=.35):
    out = []
    for r in sorted(rows, key=lambda x: x['sec']):
        if not out or r['sec'] - out[-1][0]['sec'] > window:
            out.append([])
        out[-1].append(r)
    return out


def _hp_before(events, anchor):
    """Latest known HP strictly before an event, including same-time earlier lines.

    Never use the hit's post-damage HP as a pre-hit snapshot. Preserve reference
    and observation times; absent or >4s-old evidence stays unknown.
    """
    if anchor is None:
        return None
    earlier = [r for r in events if r.get('currentHP') is not None
               and (r['sec'] < anchor['sec'] or (r['sec'] == anchor['sec'] and r.get('_order', 0) < anchor.get('_order', 0)))]
    if not earlier:
        return None
    row = earlier[-1]
    age = anchor['sec'] - row['sec']
    if age > 4 or not row.get('maxHP'):
        return None
    pct = 100 * row['currentHP'] / row['maxHP']
    return {'hp': row['currentHP'], 'maxHP': row['maxHP'], 'hpPct': round(pct, 2),
            'displayPct': int(pct), 'snapshotTime': row['sec'],
            'referenceTime': anchor['sec'], 'ageSec': round(age, 3),
            'sourceEvent': row['event'], 'sourceSpellID': row['spellID']}


def _impact_context(victims, sid, by_player, eid):
    """Use first impact of the lethal burst, not UNIT_DIED or its final hit."""
    by_victim = {}
    for death in victims:
        hits = [r for r in by_player.get(death['guid'], [])
                if r['event'] in DAMAGE_EVENTS and canonical(eid, r['spellID'])[0] == sid
                and death['sec'] - 1 <= r['sec'] <= death['sec']]
        by_victim[short(death['name'])] = hits
    all_hits = [r for rows in by_victim.values() for r in rows]
    anchor = min(all_hits, key=lambda r: (r['sec'], r.get('_order', 0))) if all_hits else None
    hit_evidence = {name: {'count': len(rows), 'times': [r['sec'] for r in rows],
                           'intervalsSec': [round(b['sec']-a['sec'], 3) for a,b in zip(rows, rows[1:])]}
                    for name, rows in by_victim.items()}
    hp = {short(d['name']): _hp_before(by_player.get(d['guid'], []), anchor) for d in victims}
    return anchor, hp, hit_evidence


def _alive(facts, at):
    dead = set()
    changes = [(d['sec'], 0, d['guid']) for d in facts['deaths'] if d.get('sec') is not None]
    changes += [(r['sec'], 1, r['playerGUID']) for r in facts.get('playerReturns', [])]
    for t, returned, guid in sorted(changes):
        if t > at:
            break
        if returned:
            dead.discard(guid)
        else:
            dead.add(guid)
    return {r['guid'] for r in facts['roster']} - dead


def _share_context(groups, at):
    candidates = [g for g in groups if abs(g['t'] - at) <= .6]
    if not candidates:
        return {'shareCountObserved': None, 'shareCountBaseline': None, 'shareStatus': 'unknown'}
    current = min(candidates, key=lambda x: abs(x['t'] - at))
    previous = [g['count'] for g in groups if g['t'] < current['t'] - .6]
    baseline = {'previous': previous, 'range': [min(previous), max(previous)], 'median': median(previous)} if previous else None
    count = current['count']
    status = ('unknown' if baseline is None else 'under' if count < min(previous) else
              'normal_low' if count == min(previous) and count < median(previous) else 'normal')
    return {'shareCountObserved': count, 'shareCountBaseline': baseline, 'shareStatus': status}


def _defensive(events, root, death):
    state = {}
    for key, sid, cd in [('feint', 1966, 15), ('cloakOfShadows', 31224, 120)]:
        casts = [r for r in events if r['spellID'] == sid and r['event'] == 'SPELL_CAST_SUCCESS' and r['sec'] <= root]
        auras = [r for r in events if r['spellID'] == sid and r['event'].startswith('SPELL_AURA_') and r['sec'] <= root]
        last = casts[-1]['sec'] if casts else None
        active = bool(auras and auras[-1]['event'] in {'SPELL_AURA_APPLIED', 'SPELL_AURA_REFRESH'})
        expired = auras[-1]['sec'] if auras and auras[-1]['event'] == 'SPELL_AURA_REMOVED' else None
        state[key] = {'lastCast': last, 'activeAtRoot': active, 'expired': expired,
                      'expiredBeforeRootSec': round(root - expired, 3) if expired is not None else None,
                      'cooldownReadyAt': round(last + cd, 3) if last is not None else None,
                      'readyStatus': 'cooldown_ready_only' if last is not None and last + cd <= death else 'unknown_or_on_cooldown',
                      'note': '기본 쿨다운만 계산; 특성·자원·실제 사용 가능 여부는 확인 필요'}
    return state


def _personal(facts, events, by_player, shares):
    eid = facts['encounter'].get('encounterID')
    incidents = []
    for death in facts['deaths']:
        guid, t = death['guid'], death['sec']
        history = by_player.get(guid, [])
        last = death.get('lastHit') or {}
        previous_deaths = [d['sec'] for d in facts['deaths'] if d['guid'] == guid and d['sec'] < t]
        lower = max(t - 15, max(previous_deaths, default=-1))
        candidates = [r for r in history if lower < r['sec'] < t - .5
                      and r['event'] in DAMAGE_EVENTS and r.get('hpPct') is not None
                      and 0 < r['hpPct'] <= 15 and r.get('maxHP')
                      and (r.get('amount') or 0) >= r['maxHP'] * .3]
        root = None
        for r in candidates:
            # Recovery breaks the chain; avoid blaming an unrelated old hit.
            recovered = any(x['sec'] > r['sec'] and x['sec'] < t and (x.get('hpPct') or 0) >= 70 for x in history)
            same_mechanic = [x for x in history if x['event'] in DAMAGE_EVENTS
                             and r['sec'] <= x['sec'] < t
                             and canonical(eid, x['spellID'])[0] == canonical(eid, r['spellID'])[0]]
            ongoing = (len(same_mechanic) >= 3 and t - same_mechanic[-1]['sec'] <= 2
                       and max(b['sec']-a['sec'] for a,b in zip(same_mechanic, same_mechanic[1:])) <= 2)
            if not recovered or ongoing:
                root = r
                break
        if root is None:
            continue
        root_id, root_name = canonical(eid, root['spellID'], root['spellName'])
        kill_id, kill_name = canonical(eid, last.get('spellID'), last.get('spellName'))
        safety = [r for r in events if r['playerGUID'] == guid and root['sec'] - 8 <= r['sec'] <= t and r['spellID'] in SAFETY]
        chain, seen = [], set()
        for r in safety:
            label = None
            if r['event'] == 'SPELL_CAST_SUCCESS' and r['spellID'] in {185311, 6262}:
                label = SAFETY[r['spellID']]
            elif r['spellID'] == 1966 and r['event'] == 'SPELL_AURA_REMOVED':
                label = '교란 종료'
            elif r['spellID'] in {45181, 45182} and r['event'] == 'SPELL_AURA_APPLIED':
                label = '구사일생 발동'
            if label and (label, round(r['sec'], 1)) not in seen:
                seen.add((label, round(r['sec'], 1)))
                chain.append({'t': r['sec'], 'event': label})
        chain += [{'t': root['sec'], 'event': root_name + ' 착탄'}, {'t': t, 'event': '사망'}]
        chain.sort(key=lambda x: x['t'])
        killing = next((r for r in reversed(history) if r['event'] in DAMAGE_EVENTS and r['sec'] <= t), None)
        before = _hp_before(history, killing)
        targets = sorted({r['destName'] for r in events if r['event'] in DAMAGE_EVENTS and r['destGUID'] != guid
                          and abs(r['sec'] - root['sec']) <= .35 and canonical(eid, r['spellID'])[0] == root_id})
        defense = _defensive([r for r in events if r['playerGUID'] == guid], root['sec'], t)
        ctx = _share_context(shares, float(last.get('sec') or t))
        incidents.append({'scope': 'personal_death_chain', 'player': short(death['name']), 'playerGUID': guid,
                          'rootMechanic': root_name, 'rootSpellID': root_id, 'rootTime': root['sec'],
                          'rootType': 'personal_death_chain', 'rootDetail': {'hpAfterHit': root['currentHP'], 'maxHP': root['maxHP'], 'otherTargets': targets},
                          'deathTime': t, 'killingSpellID': kill_id, 'killingMechanic': kill_name,
                          'hpBeforeKillingBlow': {'hp': before['hp'], 'maxHP': last.get('maxHP') or before['maxHP']} if before else None,
                          'hpBeforePressure': None, 'hpBeforeRoot': {short(death['name']): _hp_before(history, root)},
                          'defensiveState': defense, 'safetyChain': chain, **ctx,
                          'pvaExpected': {'afterPlayerDeathFrom': t, 'noMissedAfterDeath': True},
                          'actionSummaryIncidents': 1, 'confidence': 'high',
                          'sentence': _personal_sentence(root_name, root['sec'], t, defense, ctx, chain)})
    return incidents


def _time(t):
    return f'{int(t // 60)}:{t % 60:04.1f}'


def _personal_sentence(name, root, death, defense, share, chain):
    feint = defense['feint']
    context = ''
    if not feint['activeAtRoot'] and feint['expiredBeforeRootSec'] is not None:
        ready_at = feint.get('cooldownReadyAt')
        last_cast = feint.get('lastCast')
        # A long-expired Feint is not meaningfully described as merely "expired".
        # If its base cooldown had already recovered before the root impact, say
        # that the button was available but unused.  This uses only the same base
        # cooldown evidence already stored in defensiveState; talents/resources
        # remain outside the claim.
        if ready_at is not None and float(ready_at) <= float(root) + 1e-6:
            last = f" (마지막 사용 {_time(float(last_cast))})" if last_cast is not None else ''
            context = f"을 교란 사용 가능 상태였으나 미사용{last}"
        else:
            context = f"을 교란 없이 맞음(교란 {feint['expiredBeforeRootSec']:.1f}초 전 만료)"
    else:
        context = ' 후 저체력'
    saved = ' → 구사일생 소모' if any(x['event'] == '구사일생 발동' for x in chain) else ''
    sharing = f"정상 {share['shareCountObserved']}인 분담에서 " if share['shareStatus'] in {'normal', 'normal_low'} else ''
    return f'{_time(root)} {name}{context}{saved} → 도트 중 저체력 → {_time(death)} {sharing}사망'


def _raid(facts, events, by_player, shares):
    enc = facts['encounter']
    if str(enc.get('result', '')).upper() != 'WIPE' or is_fake_pull(enc):
        return None
    eid, end = enc.get('encounterID'), float(enc.get('durationSec') or 0)
    roster = {r['guid']: r for r in facts['roster']}
    deaths = [d for d in facts['deaths'] if d.get('sec') is not None]
    clusters = _groups(deaths, .5)
    for cluster in clusters:
        # One root requires a shared mechanic, not merely simultaneous deaths.
        by_mech = defaultdict(list)
        for d in cluster:
            hit = d.get('lastHit') or {}
            by_mech[canonical(eid, hit.get('spellID'), hit.get('spellName'))].append(d)
        for (sid, name), victims in by_mech.items():
            if sid is None:
                continue
            t = min(d['sec'] for d in victims)
            roles = [{'player': short(d['name']), 'specID': roster.get(d['guid'], {}).get('specID'), 'role': role(roster.get(d['guid'], {}).get('specID'))}
                     for d in victims if role(roster.get(d['guid'], {}).get('specID')) in {'healer', 'tank'}]
            if len(victims) < 3 and not roles:
                continue
            later = [d for d in deaths if t + .6 < d['sec'] <= min(end, t + 20)]
            root_anchor, root_hp, root_hits = _impact_context(victims, sid, by_player, eid)
            low = {player: hp['hpPct'] for player, hp in root_hp.items() if hp is not None}
            pressure = []
            pressure_groups = defaultdict(list)
            for r in events:
                if r['event'] in DAMAGE_EVENTS and t - 4 <= r['sec'] < t - .5:
                    csid, cname = canonical(eid, r['spellID'], r['spellName'])
                    if csid != sid and csid == 1284813:
                        pressure_groups[(csid, cname)].append(r)
            for (psid, pname), rows in pressure_groups.items():
                for group in _groups(rows):
                    n = len({x['destGUID'] for x in group})
                    if n >= max(5, len(roster) // 2):
                        pressure.append({'mechanic': pname, 'spellID': psid, 't': group[0]['sec'], 'playersHit': n, 'secBeforeRoot': round(t-group[0]['sec'], 3)})
            pressure_anchor = None
            if pressure:
                latest_pressure = max(pressure, key=lambda x: x['t'])
                pressure_anchor = next((r for r in events if r['sec'] == latest_pressure['t']
                                        and canonical(eid, r['spellID'])[0] == latest_pressure['spellID']
                                        and r['event'] in DAMAGE_EVENTS), None)
            pressure_hp = ({short(d['name']): _hp_before(by_player.get(d['guid'], []), pressure_anchor) for d in victims}
                           if pressure_anchor is not None else None)
            low_cascade = len(victims) >= 3 and len(low) >= len(victims) / 2 and sum(v <= 55 for v in low.values()) >= len(victims) / 2 and bool(pressure)
            root_type = 'low_health_cascade' if low_cascade else 'multi_death' if len(victims) >= 3 else 'critical_role'
            before = _alive(facts, t - .01)
            downstream, evidence = [], []
            # Persistent loss must hold at the later event; resurrection offers do not count.
            persistent = [d for d in victims if d['guid'] not in _alive(facts, min(end, t + 6))]
            if roles and persistent:
                for role_name in sorted({x['role'] for x in roles}):
                    count_before = sum(role(roster[g].get('specID')) == role_name for g in before)
                    lost = sum(role(roster.get(d['guid'], {}).get('specID')) == role_name for d in persistent)
                    if lost:
                        evidence.append(f'{role_name} {count_before}→{count_before-lost}')
            later_big = None
            later_by_mech = defaultdict(list)
            for d in later:
                hit = d.get('lastHit') or {}
                later_by_mech[canonical(eid, hit.get('spellID'), hit.get('spellName'))].append(d)
            later_groups = [g for rows in later_by_mech.values() for g in _groups(rows, 1.0)]
            for group in later_groups:
                at = group[0]['sec']
                hit = group[0].get('lastHit') or {}
                dsid, dname = canonical(eid, hit.get('spellID'), hit.get('spellName'))
                group_anchor, group_hp, group_hits = _impact_context(group, dsid, by_player, eid)
                hp_map = {player: hp['hpPct'] for player, hp in group_hp.items() if hp is not None}
                low_count = sum(v <= 50 for v in hp_map.values())
                if len(group) >= 3 and low_count > len(group) / 2 and any(d['guid'] not in _alive(facts, at-.01) for d in persistent):
                    later_big = at
                    evidence.append(f'alive {len(before)}→{len(_alive(facts, at-.01))} before {at:.1f}')
                    evidence.append(f'{low_count}/{len(group)} victims <=50% HP before first {dname} impact')
                if len(group) >= 3:
                    hit = group[0].get('lastHit') or {}
                    dsid, dname = canonical(eid, hit.get('spellID'), hit.get('spellName'))
                    downstream.append({'t': at, 'type': 'multi_death', 'mechanic': dname, 'spellID': dsid, 'victims': len(group),
                                       'hpBeforePressure': {short(d['name']): _hp_before(by_player.get(d['guid'], []), root_anchor) for d in group},
                                       'hpBeforeRoot': group_hp, 'rootHits': group_hits,
                                       'pressureMechanic': name,
                                       'roleLosses': [{'player': short(d['name']), 'specID': roster.get(d['guid'], {}).get('specID'), 'role': role(roster.get(d['guid'], {}).get('specID'))} for d in group if role(roster.get(d['guid'], {}).get('specID')) in {'healer', 'tank'}]})
                else:
                    for d in group:
                        hit = d.get('lastHit') or {}
                        dsid, dname = canonical(eid, hit.get('spellID'), hit.get('spellName'))
                        downstream.append({'t': d['sec'], 'player': short(d['name']), 'mechanic': dname, 'role': role(roster.get(d['guid'], {}).get('specID')), **({'shareCount': _share_context(shares, d['sec'])['shareCountObserved']} if dsid == 1288282 else {})})
            share_after = next((g for g in shares if t + .6 < g['t'] <= t + 15), None)
            ctx = _share_context(shares, share_after['t']) if share_after else _share_context([], t)
            under = ctx['shareStatus'] == 'under' and any(d['guid'] not in _alive(facts, share_after['t']-.01) for d in persistent)
            if under:
                ctx['shareStatus'] = 'under_consequence'
                evidence.append(f"share {ctx['shareCountObserved']} < baseline {ctx['shareCountBaseline']['range'][0]}")
                downstream = [d for d in downstream if abs(d['t'] - share_after['t']) > .6]
                downstream.append({'t': share_after['t'], 'type': 'under_share', 'mechanic': '불안정한 독기', 'shareCount': ctx['shareCountObserved'],
                                   'deaths': [short(d['name']) for d in later if abs(d['sec'] - share_after['t']) < .6]})
            wipe_called = 0 <= end - t <= 15 and any(d['guid'] not in _alive(facts, end) for d in persistent)
            linked = wipe_called or under or (later_big is not None and bool(roles))
            if not linked:
                continue
            if not later and not wipe_called:
                continue
            downstream.append({'type': 'WIPE_CALLED' if not later else 'WIPE', 't': end, 'secAfterRoot': round(end-t, 3)})
            if wipe_called:
                evidence.append(f'failed encounter ended {end-t:.1f}s after root (reset inferred)')
            targets = {}
            for r in events:
                if r['event'] in DAMAGE_EVENTS and abs(r['sec'] - t) <= .35 and canonical(eid, r['spellID'])[0] == sid:
                    targets[r['destName']] = 'died' if any(d['guid'] == r['destGUID'] for d in victims) else f"survived_{int(r['currentHP'])}" if r.get('currentHP') is not None else 'survived_unknown'
            prior = []
            for d in deaths:
                if d['sec'] >= t - .5:
                    continue
                hit = d.get('lastHit') or {}
                item = {'t': d['sec'], 'player': short(d['name']), 'mechanic': canonical(eid, hit.get('spellID'), hit.get('spellName') or '관측 없음')[1]}
                rez = next((r for r in facts.get('resurrections', []) if r['targetGUID'] == d['guid'] and d['sec'] < r['sec'] < t), None)
                if rez:
                    item['resurrectedCastAt'] = rez['sec']
                prior.append(item)
            hp_evidence = []
            if low_cascade:
                values = [hp['displayPct'] for hp in root_hp.values() if hp is not None]
                two_hits = len(root_hits) == len(victims) and all(x['count'] == 2 for x in root_hits.values())
                gaps = [x for row in root_hits.values() for x in row['intervalsSec']]
                if values:
                    detail = f"{min(values)}~{max(values)}% 상태에서 {name}"
                    if two_hits and gaps:
                        detail += f" 2타(피격 간격 {min(gaps):.3f}~{max(gaps):.3f}초)"
                    else:
                        detail += ' 피해 후 다수 사망'
                    hp_evidence.append(detail)
                for d in victims:
                    player = short(d['name'])
                    a = (pressure_hp or {}).get(player)
                    b = root_hp.get(player)
                    if a is not None and b is not None:
                        hp_evidence.append(f"{player}: {pressure[-1]['mechanic']} 전 {a['displayPct']}% → {name} 첫 타 직전 {b['displayPct']}%")
            return {'scope': 'raid_collapse' , 'rootMechanic': name, 'rootSpellID': sid, 'rootTime': t,
                    'rootType': root_type, 'rootVictimCount': len(victims), 'rootVictims': {player: hp['displayPct'] for player, hp in root_hp.items() if hp is not None},
                    'victims': [short(d['name']) for d in victims],
                    'hpBeforePressure': pressure_hp, 'hpBeforeRoot': root_hp, 'rootHits': root_hits,
                    'hpEvidence': hp_evidence, 'rootFirstImpactTime': root_anchor['sec'] if root_anchor else None,
                    'rootTargets': targets, 'rootRoleLosses': roles, 'aliveBeforeRoot': len(before),
                    'precedingPressure': pressure if low_cascade else [], 'priorLosses': prior,
                    'downstreamEvents': sorted(downstream, key=lambda x: x['t']),
                    'stateWorseningEvidence': evidence, **ctx,
                    'sessionBucket': '저체력 연속 광역' if low_cascade else name,
                    'confidence': 'medium' if low_cascade else 'high'}
    return None


def build_incident_analysis(facts, events):
    enc = facts['encounter']
    if is_fake_pull(enc):
        return {'incidentSchemaVersion': INCIDENT_SCHEMA_VERSION, 'analysisExcluded': 'fake_pull', 'deathIncidents': [], 'collapseChain': None, 'shareGroups': []}
    by_player = defaultdict(list)
    for index, r in enumerate(events):
        r["_order"] = index
        if r.get('currentHP') is not None:
            by_player[r['destGUID']].append(r)
    shares = []
    # Restrict boss-specific share semantics to the mapped encounter.
    if int(enc.get('encounterID') or 0) == 3445:
        for group in _groups([r for r in events if r['spellID'] == 1288282 and r['event'] in DAMAGE_EVENTS | {'SPELL_ABSORBED', 'SPELL_MISSED'}]):
            shares.append({'t': group[0]['sec'], 'count': len({r['destGUID'] for r in group}),
                           'participants': sorted({r['destGUID'] for r in group}), 'eventTypes': sorted({r['event'] for r in group})})
    return {'incidentSchemaVersion': INCIDENT_SCHEMA_VERSION, 'deathIncidents': _personal(facts, events, by_player, shares),
            'collapseChain': _raid(facts, events, by_player, shares) if int(enc.get('encounterID') or 0) == 3445 else None,
            'shareGroups': shares}
