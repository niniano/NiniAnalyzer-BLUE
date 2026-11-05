"""똬리의 제단(3429) 트라이 결론 다섯 줄 · V1 (읽기 전용)

저장된 리뷰(mechanic_review.json, raid_review.json, raid_facts.json)와
선택적으로 raw.log(.gz)의 경계·HP 줄만 읽어 아래 다섯 줄을 만든다.

    어디까지 · 첫 실수 · 전멸 원인(누가) · 딜·힐 · 다음 풀

원칙
  * 첫 실수와 전멸 원인을 나눈다. 글레이브·탱 브레스 오피격은 첫 실수일 뿐 전멸 원인이 아니다.
  * 근거가 직접 보이면 [확정], 정황만 맞으면 [추정], 아니면 [미확정].
  * 개인 책임은 근거가 있는 사실(대상·사망·생존기 유무)만 적는다.
  * 파일을 쓰지 않는다.
"""
from __future__ import annotations

import csv
import gzip
import json
from collections import Counter
import re
from pathlib import Path

VERSION = "COILED-WIPE-CAUSE-V3.7"
BOSS_NPC = "-257911-"
# V3.5: P2부터는 줄잔(257911)이 죽고 사술 군주 말라크라스(259854)가 보스. 사이페에 체력이 다시 차고 막페에서 깎인다.
MALACRASS_NPC = "-259854-"
GUILLOTINE_MARK = 1283485
EXECUTE = 1283606
GUILLOTINE_SOAK = 1283594
# 사이페 이후(막페) 냉혹한 단두대: 10/19 로그에서 발견 (표식 1299266 · 막기 1299296 · 실패 냉혹한 처형 1299301)
P3_MARK = 1299266
P3_SOAK = 1299296
P3_EXECUTE = 1299301
GSETS = {"p1": (GUILLOTINE_MARK, GUILLOTINE_SOAK, EXECUTE), "p3": (P3_MARK, P3_SOAK, P3_EXECUTE)}
P1_LIMIT_SEC = 170.0               # 2:50. 엔진 phase1Transition.targetMaxSec 와 같은 값
DPS_WINDOW = (167.0, 177.0)        # 딜 체크 붕괴로 볼 붕괴 시작 구간
PULL_FAIL_SEC = 20.0
IMMUNITY = {
    1022: "보호의 축복",
    204018: "주문 수호의 축복",
    642: "천상의 보호막",
    45438: "얼음 방패",
    186265: "거북의 상",
    31224: "그림자 망토",
    196555: "황천걸음",
}
P2_NEXT = {
    "manifest_collision": "현신 자리를 미리 나눠 두고, 현신 대상끼리 붙지 않게 콜.",
    "wail_interrupt_failure": "통곡 차단 순번을 다시 확인하고 1차 차단 직후 다음 차단자 콜.",
    "gloombomb_failure": "암영폭탄 대상은 차단쫄 위치로만 이동, 추가 피격 0명.",
    "soul_sever_non_tank_hit": "영혼 절단 전방은 탱커만 맞게 자리 정리.",
    "prior_add_overlap": "이전 차단쫄을 다음 웨이브 전에 정리.",
}
HEALER_SPECS = {65, 105, 256, 257, 264, 270, 1468}
ATTRITION = {"무시무시한 존재감", "앙갚음의 원한", "무덤구속"}
P2_SHORT = {"manifest_collision": "현신", "wail_interrupt_failure": "통곡 차단", "gloombomb_failure": "암영폭탄",
            "soul_sever_non_tank_hit": "영혼 절단", "prior_add_overlap": "쫄 중첩"}
CONF = {"verified": "확정", "likely": "추정", "unknown": "미확정"}
# V3.3 와우보초 신성 성기사 똬리 신화 공략 (사용자 제공 2026-10-22) · 우리 신화 기록에서 판정이 없던 사망.
# 키 = 사망 마지막 피격 주문 이름 (로그 ID: 공명 1312132 · 도끼분쇄자 1285017 · 유독한 지면 1283290 · 돌연변이 맹독 1310691 · 과부의 입맞춤 1283623)
GUIDE_DEATHS = {
    "악의적인 공명": ("현신 둘 닿음 → 악의적인 공명",
                  "신화: 두려움의 현신 둘이 서로 닿으면 두 고정 대상에게 악의적인 공명 · 섬뜩한 집착 대상끼리 떨어져서 현신 끌기 (끌고 갈 자리 미리 나누기)."),
    "도끼분쇄자": ("도끼분쇄자 착지·떠도는 도끼",
               "도끼분쇄자 착지 지점 피하기 · 신화는 도끼가 안 사라지고 계속 늘어나니 떠도는 도끼 경로를 보고 자리 잡기."),
    "유독한 지면": ("유독한 지면 (제단 입구)", "똬리의 제단의 송곳니 때 제단 입구에서 넘치는 유독한 지면 피하기."),
    "돌연변이 맹독": ("돌연변이 맹독 (악성 돌연변이 구슬)",
                  "신화: 악성 돌연변이 구슬은 다른 구슬과 떨어뜨리고 다음 유독한 폭우 전에 절단으로 없애기 · 밟으면 8미터 안 구슬까지 터짐."),
    "과부의 입맞춤": ("과부의 입맞춤 (도끼 40미터 안)", "단두대 뒤 곧바로 도끼에서 40미터 밖으로 · 신화는 치명 표시라 안에 남으면 사망."),
    "죽음의 은총": ("죽음의 은총 (40미터 안)", "막페 냉혹한 단두대 뒤 곧바로 40미터 밖으로 · 신화는 치명 표시라 안에 남으면 사망."),
    "맹독송곳니": ("맹독송곳니 (독 도트)", "맹독송곳니는 독 효과라 해제 가능 · 여러 명에게 걸리면 빨리 해제."),
}
RESONANCE = 1312132
RESONANCE_NEXT = "현신 고정 대상끼리 떨어지기 (악의적인 공명)"
CATEGORY = {"mechanic": "기믹", "dps": "딜", "heal": "힐·생존", "pull": "풀", "none": "-"}


# ------------------------------------------------------------------ helpers
def safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def short(name) -> str:
    return str(name or "").split("-")[0]


def mmss(sec) -> str:
    if sec is None:
        return "-"
    sec = max(0.0, float(sec))
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def read_json(path) -> dict:
    try:
        x = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _open_raw(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rb")
    return open(path, "rb")


def _ts_sec(ts: str, start) -> float | None:
    from datetime import datetime
    try:
        d, t = ts.split()[:2]
        mo, dd, yy = d.split("/")
        hh, mi, ss = t.split(":")
        sec, frac = (ss.split(".") + ["0"])[:2]
        dt = datetime(int(yy), int(mo), int(dd), int(hh), int(mi), int(sec), int(frac.ljust(6, "0")[:6]))
        return (dt - start).total_seconds()
    except Exception:
        return None


def _ts_sec_fast(ts: str, start, start_day, start_us) -> float | None:
    """_ts_sec 와 같은 값 (같은 날이면 datetime 없이 계산)."""
    try:
        d, t = ts.split()[:2]
        mo, dd, yy = d.split("/")
        if (int(yy), int(mo), int(dd)) != start_day:
            return _ts_sec(ts, start)
        hh, mi, ss = t.split(":")
        sec, frac = (ss.split(".") + ["0"])[:2]
        us = (int(hh) * 3600 + int(mi) * 60 + int(sec)) * 1_000_000 + int(frac.ljust(6, "0")[:6])
        return (us - start_us) / 10 ** 6
    except Exception:
        return None


def scan_raw(raw_path, start_ts: str) -> dict:
    """raw에서 보스 HP와 단두대 대상·생존기만 뽑는다 (해당 줄만 해석)."""
    out = {"hp": [], "hp2": [], "rez": [], "guillotines": [], "immunity": [], "guillotineHits": [], "wail": [],
           "petOwner": {}, "discovery": [], "available": False}
    if not raw_path:
        return out
    p = Path(raw_path)
    if not p.is_file():
        for name in ("raw.log.gz", "raw.log"):
            if (p.parent / name).is_file():
                p = p.parent / name
                break
        else:
            return out
    from datetime import datetime
    try:
        d, t = start_ts.split()[:2]
        mo, dd, yy = d.split("/")
        hh, mi, ss = t.split(":")
        sec, frac = (ss.split(".") + ["0"])[:2]
        start = datetime(int(yy), int(mo), int(dd), int(hh), int(mi), int(sec), int(frac.ljust(6, "0")[:6]))
    except Exception:
        return out
    imm_tokens = tuple(f",{k},".encode() for k in IMMUNITY)
    g_tokens = (f",{GUILLOTINE_MARK},".encode(), f",{P3_MARK},".encode())
    h_tokens = (f",{P3_SOAK},".encode(), f",{P3_EXECUTE},".encode())
    last_hp_sec = -1.0
    last_hp2_sec = -1.0
    mal_token = MALACRASS_NPC.encode()
    hit_tokens = (f",{GUILLOTINE_SOAK},".encode(), f",{EXECUTE},".encode())
    # GOLD V5.2.2 속도 (V3.7에 맞춤): 아래 조건에 걸릴 수 있는 줄만 남기는 한 번짜리 거르개 (결과는 예전과 같다)
    prefilter = re.compile(b"|".join(re.escape(t) for t in (
        (BOSS_NPC.encode(), WAIL_TOKEN, b"SPELL_CAST_SUCCESS,Pet-", b"SPELL_SUMMON,Player-", mal_token, b"SPELL_RESURRECT,")
        + g_tokens + h_tokens + imm_tokens + tuple(DISCOVERY_TOKENS))))
    start_day = (start.year, start.month, start.day)
    start_us = ((start.hour * 3600 + start.minute * 60 + start.second) * 1_000_000 + start.microsecond)
    boss_b = BOSS_NPC.encode()
    with _open_raw(p) as fh:
        for raw in fh:
            if prefilter.search(raw) is None:
                continue
            is_boss = boss_b in raw
            is_g = b"SPELL_AURA_" in raw and any(t in raw for t in g_tokens)
            is_h3 = any(t in raw for t in h_tokens)
            is_imm = b"SPELL_AURA_" in raw and any(tok in raw for tok in imm_tokens)
            is_wail = WAIL_TOKEN in raw
            is_pet = b"SPELL_CAST_SUCCESS,Pet-" in raw or b"SPELL_SUMMON,Player-" in raw
            is_disc = any(tok in raw for tok in DISCOVERY_TOKENS)
            is_mal = mal_token in raw
            is_rez = b"SPELL_RESURRECT," in raw
            if not (is_boss or is_g or is_imm or is_wail or is_pet or is_disc or is_h3 or is_mal or is_rez):
                continue
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            head, _, payload = line.partition("  ")
            if not payload:
                continue
            sec = _ts_sec_fast(head, start, start_day, start_us)
            if sec is None:
                continue
            others = is_g or is_imm or is_wail or is_pet or is_disc or is_h3 or is_rez
            if (is_boss and not is_mal and not others and sec - last_hp_sec < 0.5
                    and not any(t in raw for t in hit_tokens)):
                continue  # 보스 줄이지만 HP 0.5초 간격 전이고 단두대 피격도 아님 → 해석할 필요 없음
            if is_mal and not is_boss and not others and sec - last_hp2_sec < 0.5:
                continue  # 말라크라스 줄이지만 HP 0.5초 간격 전 → 해석할 필요 없음
            try:
                f = next(csv.reader([payload]))
            except Exception:
                continue
            ev = f[0]
            if is_disc and len(f) > 10 and ev in DISCOVERY_EVENTS:
                try:
                    sid = int(f[9])
                except ValueError:
                    sid = None
                if (sid is not None and sid not in KNOWN_GUILLOTINE_IDS and any(t in f[10] for t in DISCOVERY_NAMES)
                        and not f[1].startswith(("Player-", "Pet-"))):
                    out["discovery"].append({"sec": round(sec, 3), "event": ev, "spellID": sid, "name": f[10],
                                             "target": short(f[6]), "source": short(f[2])})
            if is_rez and ev == "SPELL_RESURRECT" and len(f) > 6:
                out["rez"].append({"sec": round(sec, 3), "target": short(f[6])})
                continue
            if is_pet and len(f) > 13:
                if ev == "SPELL_CAST_SUCCESS" and f[1].startswith("Pet-") and f[13].startswith("Player-"):
                    out["petOwner"].setdefault(f[1], f[13])
                    continue
                if ev == "SPELL_SUMMON" and f[1].startswith("Player-"):
                    out["petOwner"].setdefault(f[5], f[1])
                    continue
            if is_wail and len(f) > 9:
                w = _wail_event(ev, f, sec)
                if w:
                    out["wail"].append(w)
                    continue
            if is_g and ev in ("SPELL_AURA_APPLIED", "SPELL_AURA_REMOVED") and len(f) > 9 and f[9] in (str(GUILLOTINE_MARK), str(P3_MARK)):
                out["guillotines"].append({"sec": round(sec, 3), "event": ev, "target": short(f[6]),
                                           "set": "p3" if f[9] == str(P3_MARK) else "p1"})
                continue
            if is_h3 and ev in ("SPELL_DAMAGE", "SPELL_MISSED") and len(f) > 9 and f[9] in (str(P3_SOAK), str(P3_EXECUTE)):
                out["guillotineHits"].append({"sec": round(sec, 3), "spellID": int(f[9]), "target": short(f[6]),
                                              "missed": ev == "SPELL_MISSED", "missType": f[12] if ev == "SPELL_MISSED" and len(f) > 12 else None})
                continue
            if is_imm and ev in ("SPELL_AURA_APPLIED", "SPELL_AURA_REMOVED") and len(f) > 9:
                try:
                    sid = int(f[9])
                except ValueError:
                    sid = None
                if sid in IMMUNITY:
                    out["immunity"].append({"sec": round(sec, 3), "event": ev, "target": short(f[6]),
                                            "spell": IMMUNITY[sid], "spellID": sid, "source": short(f[2])})
                continue
            if is_boss and ev in ("SPELL_DAMAGE", "SPELL_MISSED") and len(f) > 9 and f[9] in (str(GUILLOTINE_SOAK), str(EXECUTE)):
                out["guillotineHits"].append({"sec": round(sec, 3), "spellID": int(f[9]), "target": short(f[6]),
                                              "missed": ev == "SPELL_MISSED", "missType": f[12] if ev == "SPELL_MISSED" and len(f) > 12 else None})
                continue
            if is_boss and sec - last_hp_sec >= 0.5:
                if ev.startswith("SWING_DAMAGE"):
                    base = 9
                elif ev.startswith(("SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE")):
                    base = 12
                else:
                    continue
                if len(f) > base + 3 and BOSS_NPC in f[base]:
                    try:
                        cur, mx = float(f[base + 2]), float(f[base + 3])
                    except ValueError:
                        continue
                    if mx > 0:
                        out["hp"].append((round(sec, 3), round(cur * 100.0 / mx, 2)))
                        last_hp_sec = sec
                        continue
            if is_mal and sec - last_hp2_sec >= 0.5:
                if ev.startswith("SWING_DAMAGE"):
                    base = 9
                elif ev.startswith(("SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE")):
                    base = 12
                else:
                    continue
                if len(f) > base + 3 and MALACRASS_NPC in f[base]:
                    try:
                        cur, mx = float(f[base + 2]), float(f[base + 3])
                    except ValueError:
                        continue
                    if mx > 0:
                        out["hp2"].append((round(sec, 3), round(cur * 100.0 / mx, 2)))
                        last_hp2_sec = sec
    out["available"] = True
    return out


# 사이페 이후 단두대 계열은 ID가 아직 없다: 이름으로 「발견」만 하고 판정에는 쓰지 않는다.
# Viserio 신화 상위 킬 보스 타임라인 (사용자 제공 2026-10-21 저장본): P2 진입 2:49 · 사이페(섬뜩한 재생 1304033) 6:22
# · 영혼 말소 1287722 6:27 · 막페 냉혹한 단두대 7:21 / 8:26 / 10:06. 상위 공대 기준이라 「얼마나 늦나」 참고용.
REF_TOP_SOUL_SEC = 382
REF_KILL_TXT = "8:42~9:47 (중앙 9:10)"
# WCL 다른 공대 신화 이벤트 5풀 (10/14 너프 후, 킬 2 · 전멸 3): P2 진입 2:21~2:36 · 사이페 4:51~6:05 (킬은 4:51·5:31)
# · 막페 = 사이페 +35초 · 냉혹한 단두대 = 막페 +23초 / +1:28 · 막페 전멸은 2단두대 뒤 맹독성 파열 연쇄 (7:59, 9:09)
REF_P2_ENTRY_TXT = "2:21~2:36"
REF_SOUL_KILL_TXT = "4:51~5:31"
UNNERVING_FIXATION = 1285911    # 섬뜩한 집착 = 유령 주시 (WCL 이벤트로 확인)
SPIRIT_ERASURE = 1300665        # 영혼 말소 디버프 (사이페 직후)   # 10/14 너프 후 다른 공대 신화 킬 8개 (WCL 2026-10-14~21)
SOUL_ERASURE = 1287722
DISCOVERY_NAMES = ("단두대", "처형")
DISCOVERY_TOKENS = tuple(n.encode() for n in DISCOVERY_NAMES)
DISCOVERY_EVENTS = {"SPELL_CAST_START", "SPELL_CAST_SUCCESS", "SPELL_AURA_APPLIED", "SPELL_DAMAGE", "SPELL_MISSED"}
KNOWN_GUILLOTINE_IDS = {1283485, 1283489, 1283594, 1283606, 1307425, 1306604, 1306624,
                        1299266, 1299267, 1299296, 1299301, 1307652}

WAIL = 1286399
LATE_KICK_SEC = 8.0      # 통곡 시전 약 10초. 8초 넘어 끊으면 「끝짤」(공포 위험)
WAIL_TOKEN = b",1286399,"


def _flags_marked(v) -> bool:
    try:
        return (int(str(v), 16) & 0xFF) != 0
    except ValueError:
        return False


def _wail_event(ev: str, f: list, sec: float):
    """통곡(1286399) 시전 시작 / 완료 / 차단만 남긴다. add 는 GUID 끝 6자리로 구분."""
    if ev == "SPELL_CAST_START" and f[9] == str(WAIL):
        return {"sec": round(sec, 3), "kind": "start", "add": f[1][-10:], "marked": _flags_marked(f[4])}
    if ev == "SPELL_CAST_SUCCESS" and f[9] == str(WAIL):
        return {"sec": round(sec, 3), "kind": "done", "add": f[1][-10:], "marked": _flags_marked(f[4])}
    if ev == "SPELL_INTERRUPT" and len(f) > 12 and f[12] == str(WAIL):
        return {"sec": round(sec, 3), "kind": "kick", "add": f[5][-10:], "marked": _flags_marked(f[8]),
                "by": short(f[2]), "byGuid": f[1]}
    return None


def parse_kick_note(text: str) -> list[list[str]]:
    """NSRT 차단 메모: 첫 intstart 블록의 줄들(이름 목록). intend·빈 메모 머리줄에서 끝."""
    lines, inside = [], False
    for raw in str(text or "").splitlines():
        t = raw.strip()
        if not inside:
            if t.lower() == "intstart":
                inside = True
            continue
        if t.lower() == "intend" or t.lower().startswith("encounterid:"):
            break
        if not t:
            if lines:
                break
            continue
        lines.append(t.split())
    return lines


def resolve_kickers(raw: dict, guid_names: dict | None) -> list[dict]:
    """통곡 이벤트를 시간순으로 정렬하고, 소환수 차단을 주인 이름으로 바꾼 복사본을 돌려준다."""
    owners = (raw or {}).get("petOwner") or {}
    guid_names = guid_names or {}
    out = []
    for e in sorted((raw or {}).get("wail") or [], key=lambda x: x["sec"]):
        e = dict(e)
        if e["kind"] == "kick" and e.get("byGuid") in owners:
            owner = guid_names.get(owners[e["byGuid"]])
            if owner:
                e["by"] = owner
        out.append(e)
    return out


def infer_kick_lines(tries: list[dict], min_obs: int = 3, min_share: float = 0.5) -> dict:
    """메모 없이 그날 기록으로 차단 담당을 추정한다.

    tries = [{"events": resolve_kickers(...) 결과, "p2End": 사이페 시각}]
    P2 웨이브에서 각 쫄의 k번째 차단을 누가 했는지 모아, 칸마다 가장 많이 끊은 사람을 담당으로 본다.
    징표가 로그에 없는 쫄은 첫 차단자가 어느 줄의 1번 담당과 같은지로 줄을 정한다.
    """
    known, unknown = [], []
    for t in tries:
        ev = t.get("events") or []
        p2_end = t.get("p2End")
        adds = {}
        for e in ev:
            if p2_end is not None and e["sec"] >= float(p2_end):
                continue
            a = adds.setdefault(e["add"], {"marked": False, "seq": []})
            a["marked"] = a["marked"] or bool(e.get("marked"))
            if e["kind"] == "kick":
                a["seq"].append(e["by"])
        firsts = {}
        for e in ev:
            if e["kind"] == "start":
                firsts.setdefault(e["add"], e["sec"])
        waves = []
        for add, t0 in sorted(firsts.items(), key=lambda kv: kv[1]):
            if waves and t0 - waves[-1]["sec"] <= 3.0:
                waves[-1]["adds"].append(add)
            else:
                waves.append({"sec": t0, "adds": [add]})
        for w in waves:
            items = [adds[a] for a in w["adds"] if a in adds and adds[a]["seq"]]
            if any(i["marked"] for i in items) or len(w["adds"]) == 1:
                known += [(1 if i["marked"] else 0, i["seq"]) for i in items]
            else:
                unknown += [i["seq"] for i in items]

    def build(entries):
        lines = {0: [], 1: []}
        for which in (0, 1):
            seqs = [sq for m, sq in entries if m == which]
            pos = 0
            while True:
                obs = [sq[pos] for sq in seqs if len(sq) > pos and sq[pos]]
                if len(obs) < min_obs:
                    break
                cnt = Counter(obs)
                name, n = cnt.most_common(1)[0]
                lines[which].append({"name": name if n / len(obs) >= min_share else None,
                                     "share": round(n / len(obs), 2), "obs": len(obs)})
                pos += 1
        return lines

    lines = build(known)
    lead = {w: (lines[w][0]["name"] if lines[w] else None) for w in (0, 1)}
    for sq in unknown:
        if sq and lead[1] and sq[0] == lead[1]:
            known.append((1, sq))
        elif sq and lead[0] and sq[0] == lead[0]:
            known.append((0, sq))
    lines = build(known)
    return {"lines": [[x["name"] or "?" for x in lines[0]], [x["name"] or "?" for x in lines[1]]],
            "detail": lines}


# V3.4 공대 차단 규칙 (kick_rules_3429.json). 없으면 예전 판정 그대로.
#   backupLine: 메모 몇째 줄이 보조인지 (1부터). 보조가 끊으면 정상, 담당 순번 계산에서는 뺀다.
#   firstAdd "late": 웨이브 첫째 쫄은 끝짤이 공략 → 8초+ 차단을 ⚠로 보지 않는다 (놓침만 실패).
#   secondAdd "beforeBomb": 폭탄과 차단 시각을 같이 보여 준다 (폭탄직전 / 폭탄중 / 폭발 뒤).
#     V3.4.3: 10/25 24트 실측 - 디버프 중 차단 12번 중 10번은 뎀감 해제 성공, 실패 7번은 전부 「추가 피격」과 같이 남.
#     → 차단 시각으로는 ⚠를 붙이지 않고, 폭탄 결과(뎀감 해제·추가 피격)를 bombCycles 로 따로 판정한다.
BOMB_WINDOW_SEC = 5.0     # 암영폭탄 디버프 → 폭발 (로그 실측 5.0초)
BOMB_BEFORE_SEC = 8.0     # 폭발 8초 전까지의 차단만 「폭탄 직전」으로 표시
BOMB_AFTER_SEC = 3.0      # 폭발 3초 뒤까지의 차단은 「폭발 뒤」로 표시 (끝짤 ⚠ 안 붙임)
FIRST_EARLY_SEC = 6.0     # 첫째 쫄을 이보다 일찍 끊으면 「일찍」 표시만


def parse_kick_rules(obj) -> dict:
    """kick_rules json → {backupLine, firstAdd, secondAdd, bombWindowSec}. 모르는 값은 버린다."""
    obj = obj if isinstance(obj, dict) else {}
    out = {}
    b = safe_int(obj.get("backupLine"))
    if b and b > 0:
        out["backupLine"] = b
    if str(obj.get("firstAdd") or "") == "late":
        out["firstAdd"] = "late"
    if str(obj.get("secondAdd") or "") == "beforeBomb":
        out["secondAdd"] = "beforeBomb"
    try:
        w = float(obj.get("bombWindowSec"))
        if 0 < w <= 15:
            out["bombWindowSec"] = w
    except (TypeError, ValueError):
        pass
    return out


def bomb_cycles(obs: dict) -> list[dict]:
    """V3.4.3: P2 주기별 암영폭탄 결과 - 뎀감 해제 n/2 · 추가 피격 · 폭탄 대상 (엔진 기록 그대로)."""
    out = []
    p2 = (obs or {}).get("phase2") if isinstance(obs, dict) else None
    for c in ((p2 or {}).get("cycles") or []) if isinstance(p2, dict) else []:
        gb = c.get("gloombomb") if isinstance(c, dict) else None
        if not isinstance(gb, dict) or gb.get("damageSec") is None:
            continue
        try:
            out.append({"index": safe_int(c.get("index")), "applySec": gb.get("applySec"),
                        "sec": round(float(gb["damageSec"]), 1),
                        "removed": safe_int(gb.get("soulcoilerVulnerabilityRemovedCount")) or 0,
                        "expected": safe_int(gb.get("soulcoilerExpectedCount")) or 0,
                        "known": safe_int(gb.get("soulcoilerVulnerabilityKnownCount")) or 0,
                        "extra": [short(x) for x in (gb.get("extraHitPlayers") or [])],
                        "targets": [short(x) for x in (gb.get("players") or [])]})
        except (TypeError, ValueError):
            continue
    return out


def bomb_cycle_line(b: dict) -> str:
    ok = b["known"] and b["removed"] >= b["expected"] > 0
    head = f"{b['index']}번 {mmss(b['sec'])} · 쫄 뎀감 해제 {b['removed']}/{b['expected'] or '?'} {'✅' if ok else '❌' if b['known'] else '?'}"
    if b["extra"]:
        head += f" · 추가 피격 {len(b['extra'])}명 ({', '.join(b['extra'][:4])})"
    return head + f" · 폭탄 대상 {', '.join(b['targets'][:4])}"


def bomb_explosions(obs: dict) -> list[float]:
    """P2 주기별 암영폭탄 폭발 시각 (없으면 디버프+5초)."""
    out = []
    p2 = (obs or {}).get("phase2") if isinstance(obs, dict) else None
    for c in ((p2 or {}).get("cycles") or []) if isinstance(p2, dict) else []:
        gb = c.get("gloombomb") if isinstance(c, dict) else None
        if not isinstance(gb, dict):
            continue
        try:
            sec = gb.get("damageSec")
            if sec is None and gb.get("applySec") is not None:
                sec = float(gb["applySec"]) + 5.0
            if sec is not None:
                out.append(round(float(sec), 1))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def _bomb_order_only(fm: dict) -> bool:
    """엔진 첫 실수가 암영폭탄 「1차단 전 폭발」 하나뿐인지 (뎀감 해제 실패·추가 피격 없음)."""
    fm = fm or {}
    return (str(fm.get("kind")) == "gloombomb_failure"
            and set(fm.get("subkinds") or []) == {"gloombomb_before_first_interrupt"})


def kick_review(raw: dict, note_lines: list[list[str]], roster: set[str], phase3_sec: float | None,
                guid_names: dict | None = None, wrap: bool = True, rules: dict | None = None,
                bombs: list | None = None, dead_at=None) -> dict:
    """NSRT 규칙대로 통곡 담당을 계산해 실제 차단자와 비교한다.

    * 웨이브 = 3초 안에 처음 통곡을 시작한 add 묶음. 사이페 이후 웨이브는 3·4줄(다음은 5·6줄).
    * 무징표 add = 1줄, 징표 add = 2줄. 메모 이름 중 공대에 없는 사람은 빠진다(NSRT와 같음).
    * 담당 = 줄[(그 add가 지금까지 차단당한 횟수) % 줄 길이]. 완료된 시전은 순번을 넘기지 않는다.
    * V3.4 rules: 보조 줄 · 웨이브 첫째 쫄 끝짤 · 둘째 쫄 암영폭탄 직전 (parse_kick_rules 참고).
      첫째/둘째 = 웨이브 안에서 통곡을 먼저 시작한 순서 [추정: 등장 순서와 같다고 봄].
    """
    ev = resolve_kickers(raw, guid_names)     # 흑마 지옥사냥개 등 소환수 차단은 주인 이름으로
    if not ev or not note_lines:
        return {"available": False, "lines": [], "issues": []}
    rules = rules or {}
    backup = []
    b_line = rules.get("backupLine")
    if b_line and b_line <= len(note_lines):
        backup = [n for n in note_lines[b_line - 1] if not roster or n in roster]
        note_lines = [x for i, x in enumerate(note_lines) if i != b_line - 1]
    bombs = sorted(float(b) for b in (bombs or []))
    window = float(rules.get("bombWindowSec") or BOMB_WINDOW_SEC)

    def bomb_gap(sec):
        """가장 가까운 폭발까지 초 (+ = 폭발 전). 폭발 8초 전~3초 뒤 차단만, 아니면 None."""
        near = [round(b - sec, 1) for b in bombs if -BOMB_AFTER_SEC <= b - sec <= BOMB_BEFORE_SEC]
        return min(near, key=abs) if near else None
    marked = {}
    first_cast = {}
    for e in ev:
        marked[e["add"]] = marked.get(e["add"], False) or e["marked"]
        if e["kind"] == "start":
            first_cast.setdefault(e["add"], e["sec"])
    waves = []
    for add, t in sorted(first_cast.items(), key=lambda kv: kv[1]):
        if waves and t - waves[-1]["sec"] <= 3.0:
            waves[-1]["adds"].append(add)
        else:
            waves.append({"sec": t, "adds": [add]})
    def line(i):
        names = note_lines[i] if i < len(note_lines) else []
        return [n for n in names if n == "?" or not roster or n in roster]
    p2_w = p3_w = 0
    out_lines, issues, slots = [], [], []
    for w in waves:
        p3 = phase3_sec is not None and w["sec"] >= phase3_sec
        if p3:
            pair = (2 + 2 * p3_w, 3 + 2 * p3_w)
            p3_w += 1
            tag = f"사이페 이후 {p3_w}웨이브"
        else:
            pair = (0, 1)
            p2_w += 1
            tag = f"P2 {p2_w}웨이브"
        segs = []
        guessed = set()
        if len(w["adds"]) == 2 and not any(marked.get(a) for a in w["adds"]):
            # 징표가 로그에 안 찍힌 웨이브: 2줄 사람이 더 많이 끊은 쪽을 징표 쫄로 본다.
            l2 = set(line(pair[1]))
            score = {a: sum(1 for e in ev if e["add"] == a and e["kind"] == "kick" and e["by"] in l2) for a in w["adds"]}
            best = max(w["adds"], key=lambda a: score[a])
            if score[best] > 0 and score[best] != min(score.values()):
                marked[best] = True
                guessed.add(best)
        for order, add in enumerate(w["adds"]):
            is_m = marked.get(add, False)
            role = ("first" if order == 0 else "second") if len(w["adds"]) >= 2 else None
            late_ok = role == "first" and rules.get("firstAdd") == "late"
            bomb_rule = rules.get("secondAdd") == "beforeBomb" and bool(bombs)
            names = line(pair[1] if is_m else pair[0])
            kicks = 0
            seq = []
            cast_start = None
            line_label = "원딜쫄" if is_m else "근딜쫄"
            for e in ev:
                if e["add"] != add:
                    continue
                if e["kind"] == "start":
                    cast_start = e["sec"]
                    continue
                idx = (kicks % len(names)) if (names and wrap) else kicks
                want = names[idx] if names and idx < len(names) else None
                if want == "?":
                    want = None
                pos = idx + 1 if names else None
                delay = round(e["sec"] - cast_start, 1) if cast_start is not None else None
                late = delay is not None and delay >= LATE_KICK_SEC
                if e["kind"] == "kick":
                    ok = want is None or e["by"] == want
                    by_backup = (not ok) and e["by"] in backup
                    gap = bomb_gap(e["sec"]) if bomb_rule else None
                    bomb_bad = False   # V3.4.3: 차단 시각만으로는 실패 아님 (bombCycles 결과로 판정)
                    note = ""
                    if late_ok:
                        late = False
                        if delay is not None:
                            note = f"끝짤{delay:.1f}초" if delay >= LATE_KICK_SEC else (f"일찍{delay:.1f}초" if delay < FIRST_EARLY_SEC else "")
                    if gap is not None:
                        if gap <= 0:
                            late = False             # 폭탄 맞힌 뒤 끝짤 = 공략
                            note = f"폭발{-gap:.1f}초뒤"
                        elif gap <= window:
                            note = f"폭탄중{gap:.1f}초전"
                        else:
                            note = "폭탄직전"
                    mark = "⚠" if (late or bomb_bad) else "✅"
                    # V3.5: 담당이 이미 죽어 있으면 순번 어긋남이 아니라 「대신 끊음」
                    cover = (not ok) and (not by_backup) and bool(want) and dead_at is not None and dead_at(want, e["sec"])
                    who = (f"{e['by']}(보조){mark}" if by_backup else
                           (f"{e['by']}{mark}" if ok else f"{want}(사망)→{e['by']}" if cover else f"{want}→{e['by']}"))
                    seq.append(who + (f"{delay:.1f}초" if late else "") + (f"·{note}" if note else ""))
                    if not ok and not by_backup:
                        issues.append({"sec": e["sec"], "kind": "cover" if cover else "swap", "wave": tag,
                                       "marked": is_m, "want": want, "by": e["by"]})
                    if late:
                        issues.append({"sec": e["sec"], "kind": "late", "wave": tag, "marked": is_m,
                                       "want": want, "by": e["by"], "delay": delay})
                    if bomb_bad:
                        issues.append({"sec": e["sec"], "kind": "bomb", "wave": tag, "marked": is_m,
                                       "want": want, "by": e["by"], "gap": gap})
                    result = ("late" if late else "bomb" if bomb_bad else "backup" if by_backup
                              else "ok" if ok else "cover" if cover else "swap")
                    slots.append({"line": line_label, "pos": pos, "want": want, "by": e["by"], "delay": delay,
                                  "result": result, "wave": tag, "role": role, "bombGap": gap})
                    kicks += 1
                else:
                    dead = bool(want) and dead_at is not None and dead_at(want, e["sec"])
                    seq.append(f"{want}(사망)❌" if dead else f"{want or '?'}❌")
                    issues.append({"sec": e["sec"], "kind": "dead" if dead else "miss", "wave": tag,
                                   "marked": is_m, "want": want})
                    slots.append({"line": line_label, "pos": pos, "want": want, "by": None, "delay": delay,
                                  "result": "dead" if dead else "miss", "wave": tag, "role": role})
                cast_start = None
            label = ("원딜쫄(징표 추정)" if add in guessed else "원딜쫄") if is_m else "근딜쫄"
            if role and (late_ok or bomb_rule or rules.get("secondAdd") == "beforeBomb"):
                label += "·첫째" if role == "first" else "·둘째"
            if not names:
                label += "(메모 줄 없음)"
            segs.append(f"{label}: " + (" ".join(seq) if seq else "시전 전 처치"))
        if len(w["adds"]) >= 2 and not any(marked.get(a) for a in w["adds"]):
            segs.append("⚠ 징표 안 찍힘")
        elif guessed:
            segs.append("⚠ 로그에 징표 없음 → 차단자로 추정")
        out_lines.append(f"{tag} {mmss(w['sec'])} · " + " | ".join(segs))
    return {"available": True, "lines": out_lines, "issues": issues, "slots": slots,
            "missCount": sum(1 for i in issues if i["kind"] == "miss"),
            "swapCount": sum(1 for i in issues if i["kind"] == "swap"),
            "deadCount": sum(1 for i in issues if i["kind"] in ("dead", "cover")),
            "bombCount": sum(1 for i in issues if i["kind"] == "bomb"),
            "backupCount": sum(1 for s in slots if s["result"] == "backup"), "rules": dict(rules)}


def make_dead_at(death_times: dict | None, rez: dict | None):
    """V3.5: name, sec → 그 시각에 죽어 있었나 (그 전에 죽었고, 죽은 뒤 전투 부활이 없었으면 True)."""
    death_times = death_times or {}
    rez = rez or {}

    def dead_at(name, sec) -> bool:
        if not name or sec is None:
            return False
        past = [float(d) for d in death_times.get(name) or [] if float(d) < float(sec)]
        if not past:
            return False
        last = max(past)
        return not any(last < float(x) <= float(sec) for x in rez.get(name) or [])
    return dead_at


def death_times_of(deaths: list) -> dict:
    out = {}
    for d in deaths or []:
        out.setdefault(short(d.get("name")), []).append(round(float(d.get("sec") or 0), 3))
    return out


def rez_times_of(raw: dict) -> dict:
    out = {}
    for x in (raw or {}).get("rez") or []:
        out.setdefault(x["target"], []).append(x["sec"])
    return out


def hp_at(hp: list, sec: float, max_gap: float = 3.0):
    best = None
    for s, pct in hp:
        if abs(s - sec) <= max_gap and (best is None or abs(s - sec) < abs(best[0] - sec)):
            best = (s, pct)
    return best


# ------------------------------------------------------------------ main
SOUL_LINK_DEAD_PCT = 0.5   # 이 아래면 처치로 본다 (HP 표본 0.5초 간격)


def soul_link_order(hp: list, hp2: list, p2_end, duration: float, col_start=None) -> dict | None:
    """V3.7: 막페 영혼결속 — 두 보스를 같이 잡아야 하는데 한쪽이 먼저 0%가 되고 전멸했으면 그 순서를 돌려준다.

    hp = 줄잔(사이페 뒤 되살아남), hp2 = 말라크라스. 마지막 표본이 전멸 15초 안에 있어야 한다.
    """
    if p2_end is None or not hp or not hp2:
        return None
    zl = [x for x in hp if float(x[0]) > float(p2_end)]
    ml = [x for x in hp2 if float(x[0]) > float(p2_end)]
    if not zl or not ml or duration - float(zl[-1][0]) > 15.0 and duration - float(ml[-1][0]) > 15.0:
        return None
    p3_from = float(p2_end) + 30.0          # 사이페(약 35초) 동안 HP가 다시 차므로 막페부터만 본다
    def zero_at(series):
        return next((float(t) for t, v in series if float(t) >= p3_from and float(v) <= SOUL_LINK_DEAD_PCT), None)
    z0, m0 = zero_at(zl), zero_at(ml)
    if (z0 is None) == (m0 is None):        # 둘 다 살아 있거나 둘 다 0% → 순서 문제 아님
        return None
    first, other, sec, left = (("줄잔", "말라크라스", z0, ml[-1][1]) if z0 is not None
                               else ("말라크라스", "줄잔", m0, zl[-1][1]))
    if col_start is not None and float(col_start) < sec - 5.0:   # 이미 다른 이유로 무너지던 중
        return None
    return {"first": first, "other": other, "sec": round(sec, 3), "otherPct": float(left),
            "after": round(duration - sec, 1)}


def _death_spell(d: dict) -> tuple[int, str]:
    lh = d.get("lastHit") or {}
    return safe_int(lh.get("spellID")) or 0, str(lh.get("spellName") or lh.get("canonicalMechanic") or "")


def resonance_pairs(deaths: list[dict], start: float | None = None, end: float | None = None) -> list[dict]:
    """악의적인 공명 사망을 2초 안 묶음으로 (현신 둘이 닿으면 고정 대상 둘이 같이 죽는다)."""
    hits = [d for d in deaths if (_death_spell(d)[0] == RESONANCE or "악의적인 공명" in _death_spell(d)[1])
            and (start is None or float(d.get("sec") or 0) >= start) and (end is None or float(d.get("sec") or 0) < end)]
    out = []
    for d in hits:
        sec = float(d.get("sec") or 0)
        if out and sec - out[-1]["sec"] <= 2.0:
            out[-1]["names"].append(short(d.get("name")))
        else:
            out.append({"sec": sec, "names": [short(d.get("name"))]})
    return out


def guillotine_repeats(raw: dict, which: str = "p1") -> dict:
    """신화 참수당함은 안 사라진다: 앞 단두대를 맞은 사람이 또 맞았는지 (단두대 번호 → 이름)."""
    seen, out = set(), {}
    for g in guillotine_detail(raw, which):
        rep = sorted(set(g["soakers"]) & seen)
        if rep:
            out[g["index"]] = rep
        seen |= set(g["soakers"])
    return out


def build(meta: dict, mech: dict, raid: dict, facts: dict, raw: dict | None = None,
          kick_note: list[list[str]] | None = None, kick_rules: dict | None = None) -> dict:
    raw = raw or {}
    obs = (mech or {}).get("observations") or {}
    pt = obs.get("phase1Transition") or {}
    duration = float(meta.get("durationSec") or 0)
    result = str(meta.get("result") or "").lower()
    collapse = (raid or {}).get("collapse") or {}
    col_start = collapse.get("startSec")
    col_spells = collapse.get("spells") or {}
    col_count = int(collapse.get("count") or 0)
    deaths = sorted([d for d in ((facts or {}).get("deaths") or []) if isinstance(d, dict)],
                    key=lambda d: float(d.get("sec") or 0))
    hp = raw.get("hp") or []

    if _excluded(meta):
        return {
            "version": VERSION, "tryNo": meta.get("tryNo"), "excluded": True,
            "lines": {"어디까지": f"가짜/리셋 풀 · {duration:.1f}초", "첫 실수": "-",
                      "전멸 원인": "판정 제외", "딜·힐": "-", "다음 풀": "-"},
            "cause": {"group": "판정 제외", "code": "EXCLUDED", "label": "판정 제외", "confidence": "verified", "category": "none"},
            "category": "-", "rawUsed": bool(raw.get("available")), "hpSeries": [],
        }

    # ---- 1. 어디까지
    p2_entry = pt.get("entrySec")
    malacrass = pt.get("malacrassActivationSec")
    in_p2 = p2_entry is not None or malacrass is not None or bool((obs.get("phase2") or {}).get("cycles"))
    _rup = int(col_spells.get("맹독성 파열") or 0) + int(col_spells.get("똬리의 제단의 송곳니") or 0)
    # 2:50을 넘겨 보스가 0%가 됐고, 그 직전에 2:50 붕괴가 시작됐으면 P2 진입이 아니라 딜 체크 실패다.
    late_entry = (p2_entry is not None and float(p2_entry) > P1_LIMIT_SEC + 0.5 and col_start is not None
                  and DPS_WINDOW[0] <= float(col_start) <= float(p2_entry) + 1.0 and _rup > 0)
    if late_entry:
        in_p2 = False
    end_hp = hp[-1][1] if hp and (not in_p2) and not late_entry else None
    # V3.5: P2 이후 전멸은 말라크라스 HP (마지막 표본이 전멸 15초 안일 때만)
    hp2 = raw.get("hp2") or []
    boss_hp = end_hp
    boss_parts = {}
    if in_p2 and hp2 and duration - float(hp2[-1][0]) <= 15.0:
        boss_hp = hp2[-1][1]
        boss_parts["말라크라스"] = boss_hp
    # V3.6: 막페에는 줄잔이 되살아나 두 보스를 같이 잡는다 (영혼결속). 사이페 뒤 줄잔 HP도 같이 적는다.
    _p2_end_hp = (obs.get("phase2") or {}).get("endSec")
    if in_p2 and _p2_end_hp is not None and hp:
        zl = [x for x in hp if float(x[0]) > float(_p2_end_hp)]
        if zl and duration - float(zl[-1][0]) <= 15.0:
            boss_parts = {"줄잔": zl[-1][1], **boss_parts}
    if len(boss_parts) == 2:
        boss_hp = round(sum(boss_parts.values()) / 2.0, 2)   # 막페 진행도 = 두 보스 평균
    if result == "kill":
        reach = f"킬 {mmss(duration)}"
    elif late_entry:
        reach = (f"P1 · {mmss(duration)} 전멸 · 보스 0%가 {mmss(p2_entry)} "
                 f"(2:50보다 {float(p2_entry) - P1_LIMIT_SEC:.0f}초 늦음)")
    elif in_p2:
        p2_end = (obs.get("phase2") or {}).get("endSec")
        reach = (f"{'사이페(영혼결속) 이후' if p2_end is not None else 'P2'} · {mmss(duration)} 전멸"
                 + (f" (P2 진입 {mmss(p2_entry)}" if p2_entry is not None else " (")
                 + (f", 사이페 {mmss(p2_end)})" if p2_end is not None else ")")).replace(" ()", "")
        if len(boss_parts) == 2 and result != "kill":
            reach += " · 보스 HP " + " · ".join(f"{k} {v:.1f}%" for k, v in boss_parts.items())
        elif boss_hp is not None and result != "kill":
            reach += f" · 말라크라스 HP {boss_hp:.1f}%"
    else:
        reach = f"P1 · {mmss(duration)} 전멸" + (f" · 보스 HP {end_hp:.1f}%" if end_hp is not None else "")

    # ---- 2. 첫 실수
    fm = (mech or {}).get("firstMistake") or {}
    bomb_order_ok = False
    if _bomb_order_only(fm) and (kick_rules or {}).get("secondAdd") == "beforeBomb":
        # V3.4.2: 엔진 「1차단 전 폭발」은 이 공대 공략(폭탄 맞힌 뒤 끝짤)에서는 정상 → 첫 실수로 안 씀
        fm, bomb_order_ok = {}, True
    if fm.get("sec") is not None:
        who = ", ".join(short(p) for p in (fm.get("players") or [])[:2])
        first = f"{mmss(fm['sec'])} {fm.get('label') or fm.get('kind')}" + (f" · {who}" if who else "")
        if fm.get("kind") == "gloombomb_failure":
            first += _bomb_detail(fm, obs)
    else:
        first = "확정된 첫 실수 없음" + (" (암영폭탄 「1차단 전 폭발」은 공대 공략대로라 제외)" if bomb_order_ok else "")
        _rp = resonance_pairs(deaths, end=float(collapse.get("startSec")) if collapse.get("startSec") is not None else None)
        if _rp:
            first = f"{mmss(_rp[0]['sec'])} 현신 둘 닿음 → 악의적인 공명 · {', '.join(_rp[0]['names'][:2])} [추정]"

    # ---- 3. 전멸 원인
    cause = {"group": "원인 미확정", "code": "UNKNOWN", "label": "전멸 원인 미확정", "detail": "", "confidence": "unknown",
             "category": "none", "sec": col_start, "next": "붕괴 직전 10초를 직접 확인하세요."}
    execute_n = int(col_spells.get("처형") or 0)
    rupture_n = int(col_spells.get("맹독성 파열") or 0)
    altar_n = int(col_spells.get("똬리의 제단의 송곳니") or 0)
    markers = obs.get("guillotineMarkers") or []
    g_hit = None
    if col_start is not None:
        near = [m for m in markers if 0 <= float(col_start) - float(m.get("sec") or 0) <= 8]
        g_hit = max(near, key=lambda m: float(m.get("sec") or 0)) if near else None

    if result == "kill":
        cause = {"group": "킬", "code": "KILL", "label": "킬", "detail": "", "confidence": "verified", "category": "none",
                 "sec": None, "next": "-"}
    elif in_p2:
        rc = (raid or {}).get("verdict") or {}
        p2_first = (((obs.get("phase2") or {}).get("summary") or {}).get("firstMistake") or {})
        if bomb_order_ok and _bomb_order_only(p2_first):
            p2_first = {}
        if not p2_first and str(fm.get("kind")) in P2_NEXT:
            p2_first = fm
        p2_end = (obs.get("phase2") or {}).get("endSec")
        # V3.7: 붕괴 시작이 없으면(col_start None) 전멸 시각으로 단계를 정한다 (예전엔 P2로 떨어짐)
        _stage_ref = col_start if col_start is not None else duration
        stage = "사이페 이후" if (p2_end is not None and _stage_ref >= p2_end) else "P2"
        soul = soul_link_order(hp, hp2, p2_end, duration, col_start)
        top, n = (max(col_spells.items(), key=lambda kv: kv[1]) if col_spells else (None, 0))
        healers = {short(r.get("name")) for r in ((facts or {}).get("roster") or []) if safe_int(r.get("specID")) in HEALER_SPECS}
        p2_from = float(p2_entry if p2_entry is not None else (malacrass or 0))
        cut = float(col_start) if col_start is not None else duration
        if soul:
            cut = min(cut, float(soul["sec"]))   # 광폭 뒤 사망은 「붕괴 전」에서 뺀다
        before = [d for d in deaths if p2_from <= float(d.get("sec") or 0) < cut]
        healer_dead = list(dict.fromkeys(short(d.get("name")) for d in before if short(d.get("name")) in healers))
        loss = f"붕괴 전 P2 사망 {len(before)}명" + (f" (힐러 {', '.join(healer_dead)})" if healer_dead else "")
        group, conf, cat = "P2 전멸", "likely", "mechanic"
        nxt = P2_NEXT.get(str(p2_first.get("kind")), "P2 첫 실수부터 확인하세요.")
        if soul:
            label = (f"막페 영혼결속 순서 실패 · {soul['first']} 먼저 처치 {mmss(soul['sec'])} → "
                     f"{soul['other']} {soul['otherPct']:.1f}% 남기고 {soul['after']:.0f}초 뒤 전멸")
            group, conf, cat = "막페 영혼결속 (한쪽 먼저 처치)", "verified", "mechanic"
            nxt = (f"{soul['other']} 먼저 · 두 보스 HP 맞춰서 같이 처치 "
                   f"(한쪽이 먼저 죽으면 남은 보스 광폭 · 이번 {soul['other']} {soul['otherPct']:.1f}% 남음).")
        elif top in ATTRITION and len(before) < 2:
            label = f"{stage} · {top} {n}명 동시 사망 ({mmss(col_start)}) · 앞선 사망 거의 없음"
            group = f"{stage} {top} 동시 사망"
        elif top in ATTRITION:
            label = f"{stage} 누적 붕괴 · {top} {n}명으로 마무리 ({mmss(col_start)})"
            group, cat = f"{stage} 누적 붕괴", "heal"
            if before:
                first_d = before[0]
                nxt = (f"첫 사망 {short(first_d.get('name'))} {mmss(first_d.get('sec'))}부터 힐·생존기 배분 점검"
                       + (f"; 첫 실수({P2_SHORT.get(str(p2_first.get('kind')), '기믹')})도 같이" if p2_first else ""))
        elif top == "냉혹한 처형" and col_start is not None:
            p3g = [g for g in guillotine_detail(raw, "p3") if -1.0 <= float(col_start) - g["resolveSec"] <= 8.0]
            g3 = p3g[-1] if p3g else None
            if g3:
                label = f"막페 {g3['index']}단두대 실패 → 냉혹한 처형 {n}명 ({mmss(col_start)})"
                loss = (f"대상 {g3['target']} · 막은 사람 {len(g3['soakers'])}명"
                        + (f" ({', '.join(g3['soakers'])})" if g3['soakers'] else "") + " · " + loss)
                conf = "verified"
                nxt = f"막페 {g3['index']}단두대 막는 조 자리 확인 (이번 {len(g3['soakers'])}명)."
            else:
                label = f"{stage} · 냉혹한 처형 {n}명 ({mmss(col_start)})"
            group = "막페 단두대 실패"
        elif top == "영원한 일몰":
            label = f"{stage} · 영원한 일몰 {n}명 (보호막 못 깸, {mmss(col_start)})"
            group = f"{stage} 영원한 일몰"
            cyc = _shield_for(obs, col_start)
            if cyc:
                loss = shield_summary(cyc) + " · " + loss
            nxt = ("황혼의 장막 딜 집중·쿨기 배분 확인"
                   + (f"; 첫 실수({P2_SHORT.get(str(p2_first.get('kind')), '기믹')})도 같이" if p2_first else "") + ".")
        elif top == "영혼 말소" and stage == "사이페 이후":
            label = f"사이페 조각 밟기 실패 추정 → 영혼 말소 {n}명 ({mmss(col_start)})"
            group, conf = "사이페 영혼 말소", "likely"
            nxt = ("사이페 조각은 간격 두고 밟기 (몰아 밟으면 영혼 말소 피해가 쌓임) · 같이 밟을 땐 콜로 공대 뎀감 먼저 · "
                   "힐 쿨은 사이페 35초 내내 나눠서 (Viserio 공략).")
        elif top in ("맹독성 파열", "응결된 맹독") and stage == "사이페 이후":
            p3g = [g for g in guillotine_detail(raw, "p3") if col_start is not None and g["resolveSec"] <= float(col_start)]
            after = f" · {p3g[-1]['index']}단두대 {float(col_start) - p3g[-1]['resolveSec']:.0f}초 뒤" if p3g else ""
            label = f"막페 구슬 처리 실패 추정 → {top} {n}명 연쇄 ({mmss(col_start)}{after})"
            group, conf = "막페 구슬(파열) 연쇄", "likely"
            nxt = ("막페 구슬 순서 확인: 1보호막 후 센터 배달 → 1브레스(구슬+영혼) → 2단두대 뒤 2브레스로 남은 구슬. "
                   "보라구슬 특임이 패턴 대상자 걸리기 전에 옮겼는지.")
        elif top in GUIDE_DEATHS:
            g_label, g_next = GUIDE_DEATHS[top]
            label = f"{stage} · {g_label} {n}명 ({mmss(col_start)})"
            group, nxt = f"{stage} {top}", g_next
        elif top:
            label = f"{stage} · {top} {n}명 중심 붕괴 ({mmss(col_start)})"
            group = f"{stage} {top}"
            if stage == "사이페 이후":
                conf = "unknown"
                nxt = f"사이페 이후 「{top}」 기믹 처리 확인 (아직 자동 판정 없음)."
        else:
            label = f"{stage} · {rc.get('title') or '전멸'}"
        # V3.3: 붕괴 전 악의적인 공명 (현신 둘 닿음) 묶음은 원인 줄과 다음 풀에 따로 적는다.
        pairs = resonance_pairs(deaths, p2_from, cut)
        if pairs and top != "악의적인 공명" and not soul:
            n_res = sum(len(p["names"]) for p in pairs)
            loss += f" · 붕괴 전 공명 사망 {n_res}명 (첫 {mmss(pairs[0]['sec'])} {'·'.join(pairs[0]['names'][:2])})"
            if "현신" not in nxt:   # 엔진 「현신 접촉」 조언이 이미 같은 기믹을 말하면 덧붙이지 않는다
                nxt = nxt.rstrip(".") + f"; {RESONANCE_NEXT}"
        cause = {"group": group, "code": "SOUL_LINK_ORDER" if soul else "P2", "label": label, "detail": loss,
                 "confidence": conf, "category": cat, "sec": col_start or rc.get("sec") or (soul or {}).get("sec"),
                 "next": nxt}
        if soul:
            cause["soulLink"] = soul
    elif col_start is not None and col_start <= PULL_FAIL_SEC and col_count >= 4:
        top = max(col_spells.items(), key=lambda kv: kv[1])[0] if col_spells else "?"
        cause = {"group": "풀 직후 붕괴", "code": "PULL_FAIL", "label": f"풀 직후 붕괴 · {top} {col_spells.get(top, 0)}명",
                 "detail": "전멸 통계에서 따로 셉니다.", "confidence": "verified", "category": "pull",
                 "sec": col_start, "next": "풀 위치·시작 콜을 다시 맞추세요."}
    elif execute_n >= 3 and g_hit is not None:
        gi = int(g_hit.get("index") or 0)
        target = None
        removed = [g for g in raw.get("guillotines") or [] if g["event"] == "SPELL_AURA_REMOVED"]
        if gi >= 1 and len(removed) >= gi:
            target = removed[gi - 1]
        detail = ""
        conf = "verified"
        on_target = None
        if target:
            t_sec = target["sec"]
            on_target = _immunity_active(raw.get("immunity") or [], target["target"], t_sec)
            if on_target:
                detail = f"대상 {target['target']} · {on_target} 있었음 (다른 원인 확인 필요)"
                conf = "likely"
            else:
                detail = f"대상 {target['target']} · 보축/면역 없음"
        else:
            detail = "대상·보축 여부는 raw가 있어야 확인"
        gd = next((g for g in guillotine_detail(raw) if g["index"] == gi), None)
        if gd is not None:
            if target and on_target and not gd["lineNames"]:
                detail = f"대상 {target['target']} · {on_target} 정상 · 뒤에서 막은 사람 0명"
                conf = "verified"
            else:
                detail += f" · 단두대 맞은 인원 {len(gd['soakers'])}명"
            _rep = guillotine_repeats(raw).get(gi) if int(meta.get("difficultyID") or 0) == 16 else None
            if _rep:
                detail += f" · 앞 단두대를 또 맞은 사람 {', '.join(_rep)} (신화 참수당함은 안 사라짐)"
        cause = {"group": "단두대 실패 → 처형", "code": f"GUILLOTINE{gi}_EXECUTE", "label": f"{gi}번째 단두대 실패 → 처형 {execute_n}명",
                 "detail": detail, "confidence": conf, "category": "mechanic", "sec": col_start,
                 "target": (target or {}).get("target"),
                 "next": (f"{gi}단두대 대상{(' ' + target['target']) if target else ''} 뒤로 막는 인원 다시 배치"
                          + (f" (이번 {len(gd['soakers'])}명)" if gd is not None else "") + ", 보축 타이밍 확인.")}
    elif col_start is not None and DPS_WINDOW[0] <= col_start <= DPS_WINDOW[1] and (rupture_n or altar_n):
        hp_check = (pt.get("healthAtCheckpoint") or {})
        hp_pct = hp_check.get("healthPct") if hp_check.get("available") else None
        if hp_pct is None:
            near = hp_at(hp, 165.0)
            hp_pct = near[1] if near else None
        before = [d for d in deaths if float(d.get("sec") or 0) < col_start]
        names = ", ".join(short(d.get("name")) for d in before[:3]) + (" 외" if len(before) > 3 else "")
        detail = (f"2:45 보스 HP {hp_pct:.1f}%" if hp_pct is not None else "2:45 HP 확인 불가")
        if late_entry:
            detail = f"보스 0%가 {mmss(p2_entry)} · {float(p2_entry) - P1_LIMIT_SEC:.1f}초 늦음 · " + detail
        if before:
            detail += f" · 체크 전 사망 {len(before)}명 ({names})"
        cause = {"group": "2:50 딜 체크 실패", "code": "DPS_CHECK_FAIL", "label": "2:50 딜 체크 실패",
                 "detail": detail, "confidence": "verified" if (hp_pct is not None or late_entry) else "likely",
                 "category": "dps", "sec": col_start, "deathsBefore": len(before),
                 "next": "체크 전 사망을 줄이고 2:30부터 보스 딜 집중 콜."}
    elif col_start is not None and rupture_n >= max(4, col_count // 2):
        chain = obs.get("orbCausalChainCandidate") or {}
        verified = chain.get("evidenceLevel") == "verified"
        cause = {"group": "맹독성 파열 연쇄", "code": "RUPTURE_CHAIN", "label": f"맹독성 파열 연쇄 {rupture_n}명 (구슬 충돌 추정)",
                 "detail": "2:50 전이라 딜 체크가 아님", "confidence": "verified" if verified else "likely",
                 "category": "mechanic", "sec": col_start,
                 "next": "구슬 이동 경로를 나누고 파열 디버프 중 겹치지 않게 콜."}
    elif col_start is not None and col_spells and max(col_spells.items(), key=lambda kv: kv[1])[0] in GUIDE_DEATHS:
        top, n = max(col_spells.items(), key=lambda kv: kv[1])
        g_label, g_next = GUIDE_DEATHS[top]
        cause.update({"group": f"P1 {top}", "code": "P1_GUIDE", "label": f"P1 · {g_label} {n}명 ({mmss(col_start)})",
                      "confidence": "likely", "category": "mechanic", "next": g_next})
    elif col_start is not None:
        rc = (raid or {}).get("verdict") or {}
        cause.update({"label": rc.get("title") or cause["label"], "confidence": "unknown",
                      "category": "heal" if "누적" in str(rc.get("title")) else "none"})

    # ---- 4. 딜·힐
    if result == "kill":
        dps = "-"
    elif cause["code"] == "DPS_CHECK_FAIL" and late_entry:
        dps = f"딜 부족 · 아슬아슬 ({float(p2_entry) - P1_LIMIT_SEC:.1f}초 늦게 0%)"
    elif in_p2:
        dps = f"P1 딜 체크 통과" + (f" ({mmss(p2_entry)} P2 진입 · 다른 공대 {REF_P2_ENTRY_TXT})" if p2_entry is not None else "")
        _p2e = (obs.get("phase2") or {}).get("endSec")
        if _p2e is not None:
            dps += f" · 사이페 {mmss(_p2e)} (다른 공대 킬 {REF_SOUL_KILL_TXT}) · 다른 공대 킬 {REF_KILL_TXT}"
        elif duration > REF_TOP_SOUL_SEC:
            dps += f" · {mmss(duration)}까지 사이페 못 감 (다른 공대 킬 {REF_SOUL_KILL_TXT})"
    elif cause["code"] == "DPS_CHECK_FAIL":
        dps = f"딜 부족 · {cause['detail'].split(' · ')[0]}"
    elif duration < P1_LIMIT_SEC:
        dps = f"딜 체크 전 전멸 ({mmss(duration)})"
    else:
        dps = "판단 불가"
    if cause["category"] == "heal" and not in_p2:
        dps += " · 누적 사망형 (힐·생존기 확인)"
    elif cause["category"] == "heal" and in_p2:
        dps += f" · 힐: {cause.get('detail') or '누적 사망형'}"

    conf_txt = CONF[cause["confidence"]]
    cause_line = f"{cause['label']} [{conf_txt}]" + (f" · {cause['detail']}" if cause.get("detail") else "")
    return {
        "version": VERSION,
        "tryNo": meta.get("tryNo"),
        "lines": {
            "어디까지": reach,
            "첫 실수": first,
            "전멸 원인": cause_line,
            "딜·힐": dps,
            "다음 풀": cause["next"],
        },
        "cause": cause,
        "category": CATEGORY[cause["category"]],
        "rawUsed": bool(raw.get("available")),
        "inP2": bool(in_p2),
        "durationSec": duration,
        "endHpPct": end_hp,
        "bossHpPct": boss_hp,
        "bossHpParts": boss_parts,
        "hpSeries": hp[::4] if not in_p2 else hp[:: 4],
        "guillotineLines": guillotine_lines(raw),
        "guillotines": guillotine_detail(raw) + guillotine_detail(raw, "p3"),
        "rosterNames": sorted({short(r.get("name")) for r in ((facts or {}).get("roster") or [])}),
        "discoveryLines": discovery_lines(raw, (obs.get("phase2") or {}).get("endSec")),
        "shieldLines": shield_lines(obs, (obs.get("phase2") or {}).get("endSec")),
        "shieldCycles": [{k: c.get(k) for k in ("index", "status", "startSec", "breakSec", "breakDurationSec",
                                                "remainingPct", "absorbedPerSec", "initialAbsorb")}
                         for c in (obs.get("shieldCycles") or [])],
        "kicks": kick_review(raw, kick_note or [],
                             {short(r.get("name")) for r in ((facts or {}).get("roster") or [])},
                             (obs.get("phase2") or {}).get("endSec"),
                             {r.get("guid"): short(r.get("name")) for r in ((facts or {}).get("roster") or [])},
                             rules=kick_rules, bombs=bomb_explosions(obs),
                             dead_at=make_dead_at(death_times_of(deaths), rez_times_of(raw))),
        "bombSecs": bomb_explosions(obs),
        "bombCycles": bomb_cycles(obs),
        "deathSec": {short(d.get("name")): float(d.get("sec") or 0) for d in reversed(deaths)},
        "deathTimes": death_times_of(deaths),
        "rezSec": rez_times_of(raw),
        "wailEvents": resolve_kickers(raw, {r.get("guid"): short(r.get("name")) for r in ((facts or {}).get("roster") or [])}),
        "p2EndSec": (obs.get("phase2") or {}).get("endSec"),
        "stages": coiled_stages(duration, p2_entry if in_p2 else None, (obs.get("phase2") or {}).get("endSec"), result == "kill"),
    }


def coiled_stages(duration, p2_entry, p2_end, kill=False) -> list[dict]:
    """공략 단계 진행 (Viserio): ① 줄잔 ② 말라크라스 ③ 사이페(약 35초) ④ 막페. status = done/fail/todo."""
    d = float(duration or 0)
    soul_end = float(p2_end) + 35.0 if p2_end is not None else None
    bounds = [("줄잔", 0.0, p2_entry), ("말라크라스", p2_entry, p2_end), ("사이페", p2_end, soul_end), ("막페", soul_end, None)]
    out = []
    for name, start, end in bounds:
        if start is None or (not kill and d < float(start)):
            status = "todo"
        elif kill or (end is not None and d > float(end) + 0.5):
            status = "done"
        else:
            status = "fail"
        out.append({"name": name, "start": start, "end": end if status == "done" else (d if status == "fail" else None),
                    "status": status})
    return out


def guillotine_detail(raw: dict, which: str = "p1") -> list[dict]:
    """단두대마다: 대상, 대상에게 걸린 면역기(시작·끝), 단두대 맞은 인원, 처형 맞은 인원.
    which = p1(줄잔 단두대) / p3(사이페 이후 냉혹한 단두대)."""
    if not raw or not raw.get("available"):
        return []
    _, soak_id, exec_id = GSETS[which]
    marks = sorted([g for g in raw.get("guillotines") or [] if g.get("set", "p1") == which], key=lambda g: g["sec"])
    applied = [g for g in marks if g["event"] == "SPELL_AURA_APPLIED"]
    removed = [g for g in marks if g["event"] == "SPELL_AURA_REMOVED"]
    hits = raw.get("guillotineHits") or []
    imm = sorted(raw.get("immunity") or [], key=lambda x: x["sec"])
    out = []
    for i, ap in enumerate(applied):
        rm = next((r for r in removed if r["target"] == ap["target"] and r["sec"] >= ap["sec"]), None)
        if rm:
            end = rm["sec"]
            window = [h for h in hits if end - 0.5 <= h["sec"] <= end + 2.0]
        else:
            window = [h for h in hits if ap["sec"] + 2.0 <= h["sec"] <= ap["sec"] + 9.0
                      and h["spellID"] in (soak_id, exec_id)]
            end = min((h["sec"] for h in window), default=ap["sec"] + 5.0)
            window = [h for h in window if h["sec"] <= end + 2.0]
        # V3.7: 보호막이 다 흡수한 피격(ABSORB)도 단두대를 같이 맞은 것 → 맞은 인원에 넣는다 (absorbed 로 따로도 적음)
        soak = sorted({h["target"] for h in window if h["spellID"] == soak_id
                       and (not h["missed"] or h.get("missType") == "ABSORB")})
        absorbed = sorted({h["target"] for h in window if h["spellID"] == soak_id and h["missed"]
                           and h.get("missType") == "ABSORB"} - {h["target"] for h in window
                                                                 if h["spellID"] == soak_id and not h["missed"]})
        soak_missed = sorted({f"{h['target']}({h['missType']})" for h in window if h["spellID"] == soak_id and h["missed"]
                              and h.get("missType") != "ABSORB"})
        execute = sorted({h["target"] for h in window if h["spellID"] == exec_id})
        on_target = []
        for x in imm:
            if x["target"] != ap["target"] or x["event"] != "SPELL_AURA_APPLIED":
                continue
            off = next((y["sec"] for y in imm if y["target"] == x["target"] and y["spell"] == x["spell"]
                        and y["event"] == "SPELL_AURA_REMOVED" and y["sec"] >= x["sec"]), None)
            if x["sec"] <= end + 0.5 and (off is None or off >= ap["sec"] - 0.5):
                on_target.append({"spell": x["spell"], "by": x["source"], "on": x["sec"], "off": off})
        line_names = sorted(({h["target"] for h in window if h["spellID"] == soak_id}) - {ap["target"]})
        out.append({"index": i + 1, "phase": which, "target": ap["target"], "markSec": ap["sec"], "resolveSec": end,
                    "lineNames": line_names,
                    "targetImmunity": on_target, "soakers": soak, "soakAbsorbed": absorbed, "soakMissed": soak_missed,
                    "executeHits": len(execute)})
    return out


def discovery_lines(raw: dict, after_sec) -> list[str]:
    """사이페 이후 처음 보는 단두대/처형 계열 주문: ID·이벤트·대상 수·처음 시각 (판정 아님)."""
    if after_sec is None:
        return []
    rows = [d for d in (raw or {}).get("discovery") or [] if d["sec"] >= float(after_sec)]
    groups = {}
    for d in rows:
        key = (d["spellID"], d["name"], d["event"])
        g = groups.setdefault(key, {"first": d["sec"], "targets": set(), "n": 0})
        g["n"] += 1
        if d["target"] and d["target"] != "nil":
            g["targets"].add(d["target"])
    out = []
    for (sid, name, ev), g in sorted(groups.items(), key=lambda kv: kv[1]["first"]):
        tg = sorted(g["targets"])
        out.append(f"{mmss(g['first'])} {name} {sid} {ev} ×{g['n']}"
                   + (f" · 대상 {len(tg)}명: {', '.join(tg[:6])}{' 외' if len(tg) > 6 else ''}" if tg else ""))
    return out[:12]


def guillotine_lines(raw: dict) -> list[str]:
    lines = []
    for g in guillotine_detail(raw) + guillotine_detail(raw, "p3"):
        imm = ", ".join(f"{x['spell']}({x['by']}) {mmss(x['on'])}~{mmss(x['off']) if x['off'] is not None else '?'}"
                        for x in g["targetImmunity"]) or "면역기 없음"
        # MISS/IMMUNE = 그림자 망토·거북의 상·보호막(무적) 같은 무적기로 받은 것 (공대 확인 10/27). 나눠 맞은 인원으로 치는지는 아직 몰라서 따로 적는다.
        miss = f" · 무적기로 받음(인원 제외) {', '.join(g['soakMissed'])}" if g["soakMissed"] else ""
        tag = f"막페{g['index']}" if g["phase"] == "p3" else f"{g['index']}"
        lines.append(f"{tag}단두대 {mmss(g['markSec'])}→{mmss(g['resolveSec'])} · 대상 {g['target']} · {imm}"
                     f" · 단두대 맞은 인원 {len(g['soakers'])}"
                     + (f" (보호막 흡수 {len(g['soakAbsorbed'])} 포함)" if g.get("soakAbsorbed") else "")
                     + f"{miss} · 처형 {g['executeHits']}명")
    return lines


def _bomb_detail(fm: dict, obs: dict) -> str:
    """암영폭탄 실패: 무엇이 틀렸는지 + 관련 이름 (추가 피격자 / 폭탄 대상)."""
    cyc = next((c for c in ((obs.get("phase2") or {}).get("cycles") or [])
                if int(c.get("index") or 0) == int(fm.get("cycleIndex") or -1)), {})
    gb = cyc.get("gloombomb") or {}
    parts = []
    if fm.get("summary"):
        parts.append(str(fm["summary"]))
    subs = set(fm.get("subkinds") or [])
    extra = [short(x) for x in (gb.get("extraHitPlayers") or [])]
    if "gloombomb_extra_hit" in subs and extra:
        parts.append("추가 피격: " + ", ".join(extra[:4]) + (" 외" if len(extra) > 4 else ""))
    if subs & {"gloombomb_soulcoiler_hit_missing", "gloombomb_before_first_interrupt"}:
        targets = [short(x) for x in (gb.get("players") or [])]
        if targets:
            parts.append("폭탄 대상: " + ", ".join(targets[:4]))
    return (" · " + " · ".join(parts)) if parts else ""


def _shield_for(obs: dict, sec):
    cycles = [c for c in (obs.get("shieldCycles") or []) if c.get("startSec") is not None]
    if sec is None or not cycles:
        return None
    before = [c for c in cycles if float(c["startSec"]) <= float(sec) + 1.0]
    return max(before, key=lambda c: float(c["startSec"])) if before else None


def _cd_bits(cyc: dict) -> list[str]:
    bits = []
    al = cyc.get("cooldownAlignment") or {}
    for o in al.get("observations") or []:
        k = o.get("kind")
        if k == "bloodlust" and o.get("status") != "not_required":
            bits.append("블러드 ✅" if o.get("status") == "success" else "블러드 ❌")
        elif k == "potion" and o.get("denominator"):
            bits.append(f"물약 {o.get('usedCount')}/{o.get('denominator')}")
    auto = al.get("autoSpecCooldowns") or {}
    if auto.get("denominator"):
        txt = f"자동 쿨기 {auto.get('successCount', '?')}/{auto.get('denominator')}"
        if auto.get("lateCount"):
            txt += f" (늦음 {auto['lateCount']})"
        if auto.get("missingCount"):
            miss = [short(x.get("player") if isinstance(x, dict) else x) for x in (auto.get("missing") or [])][:4]
            txt += f" · 안 씀 {auto['missingCount']}" + (f" ({', '.join(m for m in miss if m)})" if any(miss) else "")
        bits.append(txt)
    return bits


def shield_summary(cyc: dict) -> str:
    """보호막(황혼의 장막) 한 주기: 깼는지, 얼마나 남았는지, 쿨기, 그 사이 사망."""
    parts = [f"보호막 {cyc.get('index')}번 {mmss(cyc.get('startSec'))}"]
    if cyc.get("breakSec") is not None:
        parts.append(f"깸 {mmss(cyc['breakSec'])} ({float(cyc.get('breakDurationSec') or 0):.0f}초)")
    else:
        rp = cyc.get("remainingPct")
        parts.append("못 깸" + (f" · {float(rp):.0f}% 남음" if rp is not None else ""))
    if cyc.get("absorbedPerSec"):
        parts.append(f"초당 {float(cyc['absorbedPerSec']) / 1e6:.1f}M")
    parts += _cd_bits(cyc)
    dd = cyc.get("deathsDuring") or []
    if dd:
        parts.append(f"그 사이 사망 {len(dd)}명")
    if cyc.get("causeLabel") and cyc.get("status") == "failed":
        parts.append(str(cyc["causeLabel"]))
    return " · ".join(parts)


def shield_lines(obs: dict, p2_end) -> list[str]:
    out = []
    for c in obs.get("shieldCycles") or []:
        if c.get("startSec") is None:
            continue
        tag = "막페" if (p2_end is not None and float(c["startSec"]) >= float(p2_end)) else "P2"
        out.append(f"{tag} " + shield_summary(c))
    return out


def _excluded(meta: dict) -> bool:
    if meta.get("analysisExcluded") or meta.get("resetPull"):
        return True
    try:
        return 0 <= float(meta.get("durationSec")) < 1.0
    except (TypeError, ValueError):
        return False


def pace_against(reference_series: list[list], sec: float, hp_pct: float | None):
    """같은 밤 P2까지 간 풀들의 같은 시각 보스 HP 중앙값과 비교한다."""
    if hp_pct is None or not reference_series:
        return None
    vals = []
    for series in reference_series:
        near = hp_at(series, sec, max_gap=3.0)
        if near:
            vals.append(near[1])
    if len(vals) < 1:
        return None
    vals.sort()
    ref = vals[len(vals) // 2]
    return {"refHpPct": ref, "diff": round(hp_pct - ref, 1), "refCount": len(vals)}


def _immunity_active(rows: list, target: str, sec: float) -> str | None:
    state = {}
    for r in sorted(rows, key=lambda x: x["sec"]):
        if r["sec"] > sec + 0.05:
            break
        if r["target"] != target:
            continue
        state[r["spell"]] = r["event"] == "SPELL_AURA_APPLIED"
    on = [k for k, v in state.items() if v]
    return ", ".join(on) if on else None


def build_from_dir(try_dir, use_raw: bool = True, kick_note=None, kick_rules=None) -> dict:
    d = Path(try_dir)
    meta = read_json(d / "meta.json")
    mech = read_json(d / "mechanic_review.json")
    raid = read_json(d / "raid_wipe_review.json") or read_json(d / "raid_review.json")
    facts = read_json(d / "raid_facts.json")
    raw = scan_raw(d / str(meta.get("rawFile") or "raw.log.gz"), str(meta.get("startTimestamp") or "")) if use_raw else {}
    return build(meta, mech, raid, facts, raw, kick_note, kick_rules)
