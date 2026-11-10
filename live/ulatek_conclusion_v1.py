"""울라텍(3492) 트라이 결론 다섯 줄 · V1.1 (읽기 전용) · 인벤 신화 공략 기준선 반영

똬리와 같은 원칙: 첫 실수와 전멸 원인을 나누고, [확정]/[추정]/[미확정]을 붙이고, 파일을 쓰지 않는다.
판정 재료는 GOLD 엔진이 이미 저장한 mechanic_review.json(심장 노출, 막을 수 없는 분노,
고통스러운 절규 완료, 석회화한 시체)과 raid_review.json(붕괴), raid_facts.json(사망·로스터)이다.
raw.log 에서는 울라텍 HP, 절규(차단자·징표), 약화된 파멸비늘 생존, 결함: 약화(후보)만 추가로 읽는다.

공략 기준 (인벤 「신화 울라텍 공략」 + 「맹독심연 7,8넴 공략」(10/14 너프 후), 사용자 제공 2026-10-21):
  1심장 종료 보스 HP 마지노선 66% · 2심장에 약화된 파멸비늘을 심장에 붙여 녹인다(그렇게 해도 보스 약 40%)
  알은 3:50(늦어도 4:00)까지 가마솥, 큰 알은 4:27까지 · 절규 차단은 별·다야·엑스·해골 4조
  유체(역병비늘)가 오래 살면 광폭 → 「산성 배출」 · 절규는 2짤이 심장·파도와 겹쳐 가장 어려움(원딜 커버)

    어디까지 · 첫 실수 · 전멸 원인 · 딜(심장 진도) · 다음 풀  (+ 심장 · 절규 줄)
"""
from __future__ import annotations

import csv
import gzip
from collections import Counter
from pathlib import Path

VERSION = "ULATEK-CONCLUSION-V2.0"
BOSS_NPC = "-257758-"
CRY = 1305650
UNCHECKED_RAGE = 1301007
KILL_TIME_MYTHIC = "9:50~10:02 처치"   # 10/14 너프 후 다른 공대 신화 킬 10개 (WCL 2026-10-14~21)
TIME_LIMIT_SEC = 603            # 킬은 전부 9:50~10:02, 10:08·10:11 전멸은 보스 1% → 약 10:05가 사실상 시간 한계
# 10/14 너프 후 신화 전멸 85개(10개 공대)의 「전멸 시각 → 보스 HP」 중앙값 (30초 구간). 다른 공대 진도 참고선.
REF_WIPE_HP = [(0, 100.0), (30, 91.5), (60, 88.9), (90, 85.7), (120, 80.0), (150, 74.7), (180, 65.3), (210, 64.9),
               (240, 64.3), (270, 63.5), (300, 60.2), (330, 46.0), (360, 42.3), (390, 38.6), (450, 32.1),
               (480, 22.6), (510, 21.3), (540, 14.2)]
KILL_HEARTS_MYTHIC = 3          # 공개 신화 킬 표본 2개 모두 3심장 노출 후 처치 (엔진 규칙 파일과 같음)
# V1.8: 절규 시전 시간은 아직 실측 전(어느 로그에도 완료가 없음) → 늦은 차단은 표시만 하고 판정하지 않는다.
#       기준 = 다른 공대 신화 킬 3개 차단 39번: 시전 시작→차단 중앙 0.9초 · 최대 2.2초 (WCL boss_refs 2026-10-22)
LATE_CRY_SEC = 2.5
CONF = {"verified": "확정", "likely": "추정", "unknown": "미확정"}

# 인벤 신화 공략 기준선 (신화에만 씀)
HEART1_HP_LINE = 66.0           # 「심장딜 마지노선을 66% 정도로」
HEART2_HP_REF = 40.0            # 공략 「그렇게해도 40퍼」 · 다른 공대 6:00~7:00 전멸 HP 38~44%와 일치
HEART2_HP_WARN = 45.0           # 2심장 종료가 이보다 높으면 ⚠ (판정 원인으로는 안 씀)
EGG_SMALL_SEC, EGG_SMALL_MAX_SEC, EGG_BIG_SEC = 230, 240, 267   # 3:50 / 4:00 / 4:27
DOOMSCALE_NPC = "-269200-"      # 약화된 파멸비늘 (공개 신화 표본 절규 시전자)
ACID = 1313531                  # 산성 배출 · 유체(Blightscale Rawling)가 쓰면 광폭 = 실패 신호, 3페 비명꾼(Shrieker)이 쓰면 정상
RAWLING_NPC = "-261874-"        # 역병비늘 유체 (WCL 이벤트: 킬에서는 유체 산성 배출 0, 전멸 4:57·6:31 풀에서 유체가 시전)
SHRIEKER_NPC = "-273577-"       # 역병비늘 비명꾼 (3페, 킬에서도 8:36~9:16 산성 배출 정상)
EGG_DEFECT_LATE_SEC = 268       # 결함: 약화 첫 적용 · 다른 공대 킬 4:24·4:25, 전멸 풀 4:30·4:31 → 4:28 넘으면 늦음
WEAKNESS = 1303410              # 결함: 약화 · 큰 알 가마솥 신호 (WCL 다른 공대 5풀 모두 4:24~4:31 시작, 공략 4:27과 일치)
MARKERS = {1: "별", 2: "동그라미", 4: "다야", 8: "세모", 16: "달", 32: "네모", 64: "엑스", 128: "해골"}
GUIDE_GROUPS = ("별", "다야", "엑스", "해골")
# Viserio 신화 상위 킬 보스 타임라인 (2026-10-21 저장본): 심장 2:25/4:54/9:43 · 송곳니 끊기(역병의 핏줄) 3:26
# · 사이페 유령의 똬리 8번 5:36~5:54 · 3페 첫 뱀의 물어뜯기 6:42 · 사냥감 포위 7:20/8:11/9:12/10:07
# Viserio 울라텍 쿨기 공략 (사용자 제공 2026-10-21): 가장 많이 죽는 구간 = 2페 송곳니 두 번째 끊기(역병의 핏줄 3:26),
# 사이페 똬리 4쌍(5:37~), 3페 두 번째 사냥감 포위(두 번째 정화 직후 + 파도). WCL 다른 공대 사망 원인 1위 = 부식의 파도.
WAVE_SPELL = 1292403            # 부식의 파도 (피할 수 있는 파도)
VEIN_SPELL = 1311609            # 역병의 핏줄 (송곳니 끊기)
PREY_SPELL = 1315341            # 사냥감 포위
VEIN_REF_SEC, PREY2_REF_SEC = 206, 491    # 신화 기준 3:26 · 8:11
DEATH_WINDOWS = {
    "wave": ("파도 사망", "꼬리(분신) 파도 3개 방향과 앞파도를 미리 보고 피하기 · 앞파도는 점멸·소환진·종특으로 넘기."),
    "vein": ("송곳니 끊기", "송곳니 두 번째 끊기(그룹 끊기 콜)에 공대 뎀감을 양쪽 짝 맞춰 몰기 · 힐 쿨은 송곳니 박힐 때."),
    "coils": ("사이페 똬리", "사이페 똬리 4쌍: 첫 쌍에 긴 힐 쿨, 쌍마다 새 뎀감, 마지막 두 쌍용 힐 쿨 하나 남기기 · 똬리 조 자리 지키기."),
    "prey2": ("3페 2번째 포위", "두 번째 정화 직후 + 파도 겹치는 순간: 공대 뎀감 몰고, 내 정화는 다른 정화와 떨어져서."),
    # V1.7 와우보초 신성 성기사 울라텍 신화 공략 (사용자 제공 2026-10-22) · 주문으로만 센다 (시간 구간 아님)
    "egg": ("신화 알 운반", "알 운반자끼리 3미터 이상 떨어지기 (맹독성 분출 = 곧바로 부화) · 유독한 껍질이 떨어진 사람은 썩은 노른자 20초 동안 다시 들지 않기."),
    "brood": ("독성 배양·흉물", "독성 배양 독 줄기는 몸으로 막되 독성 화상 중첩 보고 교대 · 역병비늘 흉물 곪은 폭발은 10미터 밖을 치니 흉물 곁에 붙기."),
    "purge": ("불안정한 정화 겹침", "독을 흡수한 사람은 5초 뒤 7미터 폭발 · 흩어져 터뜨리기 (신화는 터진 자리에서 부식의 파도)."),
}
GUIDE_SPELLS = {"egg": {1307612, 1307635, 1312150}, "brood": {1302842, 1310763}, "purge": {1306086}}
GUIDE_NAMES = {"egg": ("유독한 껍질", "맹독성 분출", "썩은 노른자"), "brood": ("독성 화상", "곪은 폭발"),
               "purge": ("불안정한 정화",)}
MYTHIC_P3_SEC = 400             # 6:40 · 사이페와 3페 경계 (첫 뱀의 물어뜯기 6:42 직전)

# V1.9 공대 신화 공략 (2026-10-27) 판정
#  · 사이페 똬리 8대를 3개 조가 돌아가며 맞기 (1조 1·4·7타 · 2조 2·5·8타 · 3조 3·6타)
#    유령의 똬리 시전 1299010, 맞은 사람에게 영혼 압박자 1300685 (6초) — 영웅 10/3 킬 실측: 똬리마다 5~6명
#  · 2페 송곳니 줄 6개(움켜쥐는 송곳니 1311611) 동시에 끊기 — 영웅 10/3: 3:11~3:34 따로따로 끊김
#  · 물약: 풀 20초 전 선물약 → 4:46쯤 2심장 물약 → 3심장(9:44) 물약 (물약 쿨 5분)
#  · 알 든 사람끼리 스치면 살무사 (맹독성 분출 1307635, 신화만) · V2.0에서 실측 (아래)
COIL_CAST = 1299010
COIL_SOAK = 1300685
COILS_MYTHIC = 8
FANG = 1311611
FANG_SYNC_SEC = 1.5
EGG_BURST = 1307635
POTION_WORD = "물약"
# V2.0 알 운반 → 살무사 (2026-11-03 신화 44트 실측 · 공대 공략 「알 든 상태에서 알끼리 닿거나 파도·살무사 맞으면 신화는 1시간 디버프」)
#  · 유독한 껍질 1307612 = 알 들고 있는 동안 (떨어지면 썩은 노른자 1312150)
#  · 알 든 사람이 부식의 파도에 맞거나(14트) 알끼리 부딪히면(맹독성 분출 · 8트) 0.1~6초 안에 역병비늘 살무사가 나온다
#  · 살무사: 아무나 주시 → 점프해서 착지 범위 스턴 (석화의 독침 1305163) · 44트 중 35트가 살무사가 먼저 나온 뒤 붕괴
#  · 막을 수 없는 분노는 1:15~1:16(8트) · 1:25(12트)에 거의 같은 시각으로 반복 → 붕괴 20초보다 앞이면 첫 실수에서 뺀다
SHELL = 1307612
VIPER_NPC = "-261915-"           # 역병비늘 살무사
VIPER_STING = 1305163            # 석화의 독침 (살무사 주시 · 5초 뒤 그 자리에 착지)
# V5.3.6: 착지 스턴은 따로 1303414 (주시 대상 + 착지 범위 안 사람) · 11/8 신화 9트 실측 주시 → 스턴 5.0~5.1초
VIPER_STUN = 1303414
VIPER_MARK_TO_STUN = (3.5, 7.0)
VIPER_TRIGGER_SEC = 6.0          # 원인 → 살무사 출현 (실측 0.1~5.9초)
VIPER_CLUSTER_SEC = 4.0
VIPER_DROP_SEC = 1.0             # 파도·충돌이 없을 때: 살무사 1초 안쪽 직전에 알(유독한 껍질)이 사라진 사람 (44트 중 27무리)
VIPER_BEFORE_COLLAPSE_SEC = 25.0 # 살무사가 붕괴 25초 안쪽에서 먼저 나오면 붕괴 원인으로 본다
FIRST_MISTAKE_WINDOW_SEC = 20.0  # 첫 실수(막을 수 없는 분노)는 붕괴 20초 안쪽만
POTION_H2_SEC, POTION_H2_LATE_SEC = 286, 290      # 4:46 (+4초 여유)
POTION_H3_REF_SEC = 584                           # 9:44 막심장
POTION_CD_SEC = 300
TANK_SPECS = {66, 73, 104, 250, 268, 581}
HEALER_SPECS = {65, 105, 256, 257, 264, 270, 1468}

NEXT = {
    "unchecked_rage": "탱 교대·도발 공백 없이 울라텍 근접 대상을 유지하세요 (막을 수 없는 분노 0회).",
    "anguished_cry": "절규 차단: 파멸비늘마다 징 조(별·다야·엑스·해골)가 주시 잡고 브레스 직후 차단 · 2짤은 심장·파도와 겹치니 원딜 커버 2명.",
    "acid_purge": "유체 찍딜 콜(피 많은 유체 먼저) · 탱이 유체 뭉치기 · 광폭 「산성 배출」 시전엔 배정 CC(혼회·충파).",
    "heart1_dps": "1심장 66% 미달: 작쫄은 기력 보충용이라 광딸 금지, 심장 구간엔 심장 타겟 딜 집중.",
    "doomscale_alive": "약화된 파멸비늘을 심장에 최대한 붙여 파킹(웅덩이 감수)하고 2심장 동안 심장 타겟 광.",
    "calcified_corpse": "뱀의 물어뜯기 만료 전에 흡수 담당이 받으세요 (석회화한 시체 0명).",
    "egg_viper": "알 든 사람은 파도 피하기가 최우선 (꼬리 파도 방향 미리 · 이동기 아끼기) · 알끼리 3미터 · 살무사 주시 대상은 공대에서 떨어지고 나머지는 착지 자리 피하기 (석화의 독침 스턴).",
}
EGG_VIPER_KIND = "알 운반 → 살무사"


def short(name) -> str:
    return str(name or "").split("-")[0]


def mmss(sec) -> str:
    if sec is None:
        return "-"
    sec = max(0.0, float(sec))
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def read_json(path) -> dict:
    import json
    try:
        x = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _ts_sec(ts: str, start):
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


def scan_raw(raw_path, start_ts: str) -> dict:
    """울라텍 HP 와 고통스러운 절규(시전 시작/완료/차단자), 소환수 주인만 읽는다."""
    out = {"hp": [], "cry": [], "petOwner": {}, "doomSeen": {}, "doomDied": {}, "weakness": [], "acid": [], "vein": [], "prey": [], "available": False,
           "coilCasts": [], "coilSoak": [], "fang": [], "eggBurst": [], "potions": [],
           "shell": [], "waveHits": [], "viperSeen": {}, "viperSting": [], "viperStun": [], "rage": []}
    p = Path(raw_path) if raw_path else None
    if p is None or not p.is_file():
        for name in ("raw.log.gz", "raw.log"):
            if p is not None and (p.parent / name).is_file():
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
    cry_tok = f",{CRY},".encode()
    weak_tok = f",{WEAKNESS},".encode()
    acid_tok = f",{ACID},".encode()
    vein_tok = f",{VEIN_SPELL},".encode()
    prey_tok = f",{PREY_SPELL},".encode()
    doom_tok = DOOMSCALE_NPC.encode()
    coil_tok, soak_tok = f",{COIL_CAST},".encode(), f",{COIL_SOAK},".encode()
    fang_tok, egg_tok = f",{FANG},".encode(), f",{EGG_BURST},".encode()
    pot_tok = POTION_WORD.encode()
    shell_tok, wave_tok = f",{SHELL},".encode(), f",{WAVE_SPELL},".encode()
    viper_tok, sting_tok, rage_tok = VIPER_NPC.encode(), f",{VIPER_STING},".encode(), f",{UNCHECKED_RAGE},".encode()
    opener = gzip.open if str(p).endswith(".gz") else open
    last_hp = -1.0
    with opener(p, "rb") as fh:
        for raw in fh:
            is_boss = BOSS_NPC.encode() in raw
            is_cry = cry_tok in raw
            is_pet = b"SPELL_CAST_SUCCESS,Pet-" in raw or b"SPELL_SUMMON,Player-" in raw
            is_doom = doom_tok in raw
            is_weak = weak_tok in raw
            is_acid = acid_tok in raw and (b"SPELL_CAST_START" in raw or b"SPELL_CAST_SUCCESS" in raw)
            is_vein = vein_tok in raw and b"SPELL_AURA_APPLIED" in raw
            is_prey = prey_tok in raw and b"SPELL_CAST_START" in raw
            is_plan = (coil_tok in raw or soak_tok in raw or fang_tok in raw or egg_tok in raw
                       or (pot_tok in raw and b"SPELL_AURA_" in raw))
            is_egg2 = (shell_tok in raw and b"SPELL_AURA_" in raw) or (wave_tok in raw and b",Player-" in raw) \
                or viper_tok in raw or (sting_tok in raw and b"SPELL_AURA_APPLIED" in raw) \
                or (rage_tok in raw and b"SPELL_CAST_SUCCESS" in raw)
            if not (is_boss or is_cry or is_pet or is_doom or is_weak or is_acid or is_vein or is_prey or is_plan or is_egg2):
                continue
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            head, _, payload = line.partition("  ")
            try:
                f = next(csv.reader([payload]))
            except Exception:
                continue
            ev = f[0] if f else ""
            s = _ts_sec(head, start)
            if s is None:
                continue
            if is_plan and len(f) > 10:
                sid = f[9]
                if sid == str(COIL_CAST) and ev == "SPELL_CAST_SUCCESS":
                    out["coilCasts"].append(round(s, 3))
                elif sid == str(COIL_SOAK) and ev in ("SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH", "SPELL_AURA_REMOVED"):
                    out["coilSoak"].append({"sec": round(s, 3), "event": ev, "target": short(f[6])})
                elif sid == str(FANG) and ev in ("SPELL_AURA_APPLIED", "SPELL_AURA_REMOVED"):
                    out["fang"].append({"sec": round(s, 3), "event": ev, "target": short(f[6])})
                elif sid == str(EGG_BURST):
                    out["eggBurst"].append({"sec": round(s, 3), "event": ev, "source": short(f[2]), "target": short(f[6])})
                elif (POTION_WORD in f[10] and ev in ("SPELL_AURA_APPLIED", "SPELL_AURA_REMOVED")
                      and f[1].startswith("Player-") and f[1] == f[5]):
                    out["potions"].append({"sec": round(s, 3), "event": ev, "player": short(f[2]), "name": f[10]})
            if is_egg2 and len(f) > 6:
                # V2.0 알 운반 → 살무사
                for g in (f[1], f[5]):
                    if VIPER_NPC in g:
                        out["viperSeen"].setdefault(g, round(s, 3))
                sid = f[9] if len(f) > 9 else ""
                if sid == str(SHELL) and ev in ("SPELL_AURA_APPLIED", "SPELL_AURA_REMOVED") and f[5].startswith("Player-"):
                    out["shell"].append({"sec": round(s, 3), "event": ev, "target": short(f[6])})
                elif sid == str(WAVE_SPELL) and ev.startswith("SPELL") and f[5].startswith("Player-"):
                    out["waveHits"].append({"sec": round(s, 3), "target": short(f[6])})
                elif sid == str(VIPER_STING) and ev == "SPELL_AURA_APPLIED" and f[5].startswith("Player-"):
                    out["viperSting"].append({"sec": round(s, 3), "target": short(f[6])})
                elif sid == str(VIPER_STUN) and ev == "SPELL_AURA_APPLIED" and f[5].startswith("Player-"):
                    out["viperStun"].append({"sec": round(s, 3), "target": short(f[6])})
                elif sid == str(UNCHECKED_RAGE) and ev == "SPELL_CAST_SUCCESS":
                    if not out["rage"] or s - out["rage"][-1] > 1.0:
                        out["rage"].append(round(s, 3))
            if is_pet and len(f) > 13:
                if ev == "SPELL_CAST_SUCCESS" and f[1].startswith("Pet-") and f[13].startswith("Player-"):
                    out["petOwner"].setdefault(f[1], f[13])
                elif ev == "SPELL_SUMMON" and f[1].startswith("Player-"):
                    out["petOwner"].setdefault(f[5], f[1])
            if is_doom and len(f) > 6:
                for g in (f[1], f[5]):
                    if DOOMSCALE_NPC in g:
                        out["doomSeen"].setdefault(g, round(s, 3))
                if ev == "UNIT_DIED" and DOOMSCALE_NPC in f[5]:
                    out["doomDied"].setdefault(f[5], round(s, 3))
            if is_acid and len(f) > 9 and f[9] == str(ACID) and ev in ("SPELL_CAST_START", "SPELL_CAST_SUCCESS") \
                    and SHRIEKER_NPC not in f[1]:
                out["acid"].append({"sec": round(s, 3), "kind": "start" if ev == "SPELL_CAST_START" else "done", "src": f[1][-10:],
                                    "rawling": RAWLING_NPC in f[1]})
            if is_vein and ev == "SPELL_AURA_APPLIED" and len(f) > 9 and f[9] == str(VEIN_SPELL):
                if not out["vein"] or s - out["vein"][-1] > 20:
                    out["vein"].append(round(s, 3))
            if is_prey and ev == "SPELL_CAST_START" and len(f) > 9 and f[9] == str(PREY_SPELL):
                if not out["prey"] or s - out["prey"][-1] > 20:
                    out["prey"].append(round(s, 3))
            if is_weak and ev == "SPELL_AURA_APPLIED" and len(f) > 9 and f[9] == str(WEAKNESS):
                out["weakness"].append({"sec": round(s, 3), "target": short(f[6])})
            if is_cry and len(f) > 9:
                if ev == "SPELL_CAST_START" and f[9] == str(CRY):
                    out["cry"].append({"sec": round(s, 3), "kind": "start", "add": f[1][-10:], "mark": marker_of(f[4])})
                elif ev == "SPELL_CAST_SUCCESS" and f[9] == str(CRY):
                    out["cry"].append({"sec": round(s, 3), "kind": "done", "add": f[1][-10:], "mark": marker_of(f[4])})
                elif ev == "SPELL_INTERRUPT" and len(f) > 12 and f[12] == str(CRY):
                    out["cry"].append({"sec": round(s, 3), "kind": "kick", "add": f[5][-10:], "mark": marker_of(f[8]),
                                       "by": short(f[2]), "byGuid": f[1]})
                continue
            if is_boss and s - last_hp >= 0.5 and len(f) > 15:
                base = 9 if ev.startswith("SWING_DAMAGE") else 12 if ev.startswith(("SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE")) else None
                if base is not None and len(f) > base + 3 and BOSS_NPC in f[base]:
                    try:
                        cur, mx = float(f[base + 2]), float(f[base + 3])
                    except ValueError:
                        continue
                    if mx > 0:
                        out["hp"].append((round(s, 3), round(cur * 100.0 / mx, 2)))
                        last_hp = s
    out["available"] = True
    return out


def ref_hp_at(sec):
    """다른 공대 같은 시각 전멸 HP 중앙값 (가장 가까운 앞 구간)."""
    if sec is None:
        return None
    best = None
    for t, v in REF_WIPE_HP:
        if float(sec) >= t:
            best = v
    return best


def marker_of(flags) -> str | None:
    try:
        v = int(str(flags), 16) & 0xFF
    except (TypeError, ValueError):
        return None
    return MARKERS.get(v)


def stage_at(sec, hearts, p3_sec=None) -> str:
    """공략 단계 이름: 1페 → 1심장 → 2페(알방) → 2심장 → 인터미션·3페 → 3심장."""
    if sec is None:
        return "-"
    sec = float(sec)
    for h in hearts:
        if h.get("startSec") is not None and h.get("endSec") is not None and float(h["startSec"]) <= sec <= float(h["endSec"]):
            return f"{h.get('index')}심장"
    ends = [float(h["endSec"]) for h in hearts if h.get("endSec") is not None]
    done = sum(1 for e in ends if e < sec)
    if done == 2 and p3_sec is not None:
        return "사이페" if sec < float(p3_sec) else "3페"
    return {0: "1페", 1: "2페(알방)", 2: "사이페·3페"}.get(done, "막페")


def _last_spell(d: dict):
    lh = d.get("lastHit") or {}
    try:
        sid = int(lh.get("canonicalSpellID") or lh.get("spellID") or 0)
    except (TypeError, ValueError):
        sid = 0
    return sid, str(lh.get("canonicalMechanic") or lh.get("spellName") or "")


def ulatek_stages(duration, hearts, p3_sec=None, kill=False, mythic=False) -> list[dict]:
    """공략 단계 (Viserio): ① 1페(알·꼬리·똬리 → 1심장) ② 2페(양쪽·송곳니·알 → 2심장) ③ 사이페(똬리 4쌍) ④ 3페(물어뜯기·포위 → 3심장)."""
    d = float(duration or 0)
    ends = {h.get("index"): h.get("endSec") for h in hearts if h.get("endSec") is not None}
    b1 = ends.get(1)
    b2 = ends.get(2)
    b3 = float(p3_sec) if p3_sec is not None and b2 is not None else None
    bounds = [("1페", 0.0, b1), ("2페", b1, b2)]
    # 신화는 사이페(똬리)·3페를 늘 따로 보여 준다 (3페 시작을 못 찾았으면 3페는 아직).
    bounds += [("사이페", b2, b3), ("3페", b3, None)] if (p3_sec is not None or mythic) else [("사이페·3페", b2, None)]
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


def death_windows(deaths, hearts, raw, mythic, cutoff=None) -> dict:
    """공략에서 가장 많이 죽는 구간별 사망 (붕괴가 시작된 뒤 3초까지만 센다)."""
    raw = raw or {}
    h2 = next((h for h in hearts if h.get("index") == 2), None)
    vein0 = (raw.get("vein") or [None])[0] or (VEIN_REF_SEC if mythic else None)
    preys = raw.get("prey") or []
    prey2 = preys[1] if len(preys) >= 2 else (PREY2_REF_SEC if mythic else None)
    coil_from = (float(h2["endSec"]) + 10) if h2 and h2.get("endSec") is not None else None
    coil_to = MYTHIC_P3_SEC if mythic else (coil_from + 40 if coil_from is not None else None)
    out = {k: [] for k in DEATH_WINDOWS}
    for d in deaths:
        sec = float(d.get("sec") or 0)
        if cutoff is not None and sec > float(cutoff) + 3:
            continue
        sid, sname = _last_spell(d)
        who = (short(d.get("name")), sec)
        guide = next((k for k, ids in GUIDE_SPELLS.items()
                      if sid in ids or any(n in sname for n in GUIDE_NAMES[k])), None)
        if guide is not None:
            out[guide].append(who)
        elif sid == WAVE_SPELL or "부식의 파도" in sname:
            out["wave"].append(who)
        elif sid == VEIN_SPELL or "핏줄" in sname or (vein0 is not None and vein0 - 2 <= sec <= vein0 + 20):
            out["vein"].append(who)
        elif coil_from is not None and coil_to is not None and coil_from <= sec <= coil_to:
            out["coils"].append(who)
        elif prey2 is not None and prey2 - 3 <= sec <= prey2 + 8:
            out["prey2"].append(who)
    return out


def _window_at(sec, dwin) -> str | None:
    """붕괴 시각이 어느 구간 사망 무리와 겹치나 (그 구간 마지막 사망 ±6초)."""
    if sec is None:
        return None
    for k in ("egg", "brood", "purge", "vein", "coils", "prey2"):
        if any(abs(float(sec) - t) <= 6 for _n, t in dwin.get(k) or []):
            return k
    return None


def _miss_txt(m: dict) -> str:
    return mmss(m.get("sec")) + (" %s짤" % m["nth"] if m.get("nth") else "")


def cry_review(raw: dict, guid_names: dict | None = None) -> dict:
    """절규를 누가 몇 초 만에 끊었는지, 완료(놓침)는 몇 번인지."""
    owners = (raw or {}).get("petOwner") or {}
    names = guid_names or {}
    starts = {}
    nth = Counter()
    kicks, misses = [], []
    for e in sorted((raw or {}).get("cry") or [], key=lambda x: x["sec"]):
        if e["kind"] == "start":
            starts[e["add"]] = e["sec"]
            nth[e["add"]] += 1
            continue
        e = dict(e, nth=nth.get(e["add"]) or None)
        if e["kind"] == "kick":
            by = names.get(owners.get(e.get("byGuid")), e["by"]) if e.get("byGuid") in owners else e["by"]
            st = starts.pop(e["add"], None)
            kicks.append({"sec": e["sec"], "by": by, "mark": e.get("mark"), "nth": e.get("nth"),
                          "delay": round(e["sec"] - st, 1) if st is not None else None})
        elif e["kind"] == "done":
            st = starts.pop(e["add"], None)
            misses.append({"sec": e["sec"], "mark": e.get("mark"), "nth": e.get("nth"), "castSec": round(e["sec"] - st, 1) if st is not None else None})
    who = Counter(k["by"] for k in kicks)
    groups = []
    marks = [m for m in GUIDE_GROUPS if any(x.get("mark") == m for x in kicks + misses)]
    marks += sorted({x.get("mark") or "징없음" for x in kicks + misses} - set(marks))
    for m in marks:
        ks = [k for k in kicks if (k.get("mark") or "징없음") == m]
        ms = [x for x in misses if (x.get("mark") or "징없음") == m]
        c = Counter(k["by"] for k in ks)
        groups.append(f"{m} " + (" ".join(f"{k}{v}" for k, v in c.most_common(3)) or "-") + (f" ❌{len(ms)}" if ms else ""))
    miss_nth = Counter(f"{m['nth']}짤" for m in misses if m.get("nth"))
    late = [k for k in kicks if LATE_CRY_SEC is not None and k.get("delay") is not None and k["delay"] >= LATE_CRY_SEC]
    late_txt = (f"늦은 차단({LATE_CRY_SEC:g}초+) {len(late)}번 (" + ", ".join(f"{k['by']} {mmss(k['sec'])} {k['delay']:.1f}초" for k in late[:3])
                + (" …" if len(late) > 3 else "") + ") · 다른 공대 킬 최대 2.2초 [표시만]") if late else None
    cast_secs = sorted(m["castSec"] for m in misses if m.get("castSec") is not None)
    return {"kicks": kicks, "misses": misses, "byPlayer": dict(who), "groups": groups, "missNth": dict(miss_nth),
            "late": late, "lateLine": late_txt, "castSecs": cast_secs,
            "line": (f"차단 {len(kicks)} · 놓침 {len(misses)}"
                     + (" (" + ", ".join(_miss_txt(m) for m in misses[:4]) + ")" if misses else "")
                     + (" · 차단자 " + ", ".join(f"{k} {v}" for k, v in who.most_common(6)) if who else ""))}


def _clusters(times, gap):
    out = []
    for t in sorted(times):
        if out and t - out[-1][-1] <= gap:
            out[-1].append(t)
        else:
            out.append([t])
    return out


def soul_coil_review(raw: dict, mythic: bool, duration: float) -> dict | None:
    """V1.9 사이페 똬리: 내려찍기마다 맞은 사람(영혼 압박자)과 3개 조 순서 (1조 1·4·7타 · 2조 2·5·8타 · 3조 3·6타)."""
    groups = [c for c in _clusters((raw or {}).get("coilCasts") or [], 8.0) if len(c) >= 4]
    if not groups:
        return None
    slams = groups[-1]
    soak = [x for x in raw.get("coilSoak") or [] if x["event"] in ("SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH")]
    per = []
    for t in slams:
        per.append(sorted({x["target"] for x in soak if t - 0.3 <= x["sec"] <= t + 0.6}))
    team = {0: Counter(), 1: Counter(), 2: Counter()}
    for k, names in enumerate(per):
        team[k % 3].update(names)
    owner = {}
    cross = []
    for name in sorted({n for names in per for n in names}):
        teams = [g for g in range(3) if team[g][name]]
        if len(teams) > 1:
            cross.append(f"{name}({'·'.join(str(g + 1) for g in teams)}조)")
        owner[name] = max(teams, key=lambda g: team[g][name])
    empty = [k + 1 for k, names in enumerate(per) if not names]
    expect = COILS_MYTHIC if mythic else None
    cut_short = bool(expect and len(slams) < expect and duration < slams[-1] + 6.0)
    missing = bool(expect and len(slams) < expect and not cut_short)
    ok = not empty and not missing          # 다른 조 차례에 스친 사람은 참고로만 (판정 안 함)
    def team_txt(g):
        idx = "·".join(str(k + 1) for k in range(len(per)) if k % 3 == g)
        names = sorted(n for n, o in owner.items() if o == g)
        return f"{g + 1}조({idx}타) " + (" ".join(names[:7]) + (" 외" if len(names) > 7 else "") if names else "-")
    line = (f"사이페 똬리 {len(slams)}" + (f"/{expect}" if expect else "") + f"타 {mmss(slams[0])}~{mmss(slams[-1])}"
            + (" (전멸로 끊김)" if cut_short else "")
            + " · 맞은 인원 " + "·".join(str(len(n)) for n in per)
            + (" ✅" if ok else " ❌")
            + (f" · 아무도 안 맞음 {', '.join(str(k) + '타' for k in empty)}" if empty else ""))
    return {"slams": slams, "soakers": per, "empty": empty, "cross": cross, "ok": ok, "line": line,
            "teamLine": " · ".join(team_txt(g) for g in range(3) if any(k % 3 == g for k in range(len(per))))
                        + (f" · 다른 조 차례에도 맞음: {', '.join(cross[:6])}" if cross else "")}


def fang_review(raw: dict) -> list[dict]:
    """V1.9 2페 송곳니 줄(움켜쥐는 송곳니): 줄마다 끊긴(풀린) 시각 · 공략은 6개 동시에."""
    rows = sorted((raw or {}).get("fang") or [], key=lambda x: x["sec"])
    applies = [x for x in rows if x["event"] == "SPELL_AURA_APPLIED"]
    out = []
    for grp in _clusters([x["sec"] for x in applies], 15.0):
        lo, hi = grp[0], grp[-1]
        tg = [x for x in applies if lo <= x["sec"] <= hi]
        broke = []
        for a in tg:
            r = next((x for x in rows if x["event"] == "SPELL_AURA_REMOVED" and x["target"] == a["target"] and x["sec"] >= a["sec"]), None)
            if r is not None:
                broke.append((r["sec"], a["target"]))
        broke.sort()
        spread = round(broke[-1][0] - broke[0][0], 1) if broke else None
        sync = bool(broke and len(broke) == len(tg) and spread <= FANG_SYNC_SEC)
        line = (f"송곳니 줄 {len(tg)}개 {mmss(lo)}" + (f" · 끊김 {mmss(broke[0][0])}~{mmss(broke[-1][0])} ({spread:.1f}초 차이)" if broke else " · 안 끊김")
                + (" · 동시 ✅" if sync else " · 따로 끊김 ❌ (" + ", ".join(f"{n} {mmss(t)}" for t, n in broke[:6]) + ")" if broke else "")
                + (f" · {len(tg) - len(broke)}개 안 끊김" if broke and len(broke) < len(tg) else ""))
        out.append({"sec": lo, "count": len(tg), "broken": len(broke), "spread": spread, "sync": sync, "line": line})
    return out


def potion_review(raw: dict, hearts: list, facts: dict, duration: float) -> dict | None:
    """V1.9 물약 (딜러만): 선물약(풀 20초 전) → 2심장 4:46쯤 → 3심장 9:44. 쿨 5분이라 2심장 물약이 늦으면 3심장에 없다."""
    pots = (raw or {}).get("potions") or []
    if not raw.get("available"):
        return None
    roster = {short(r.get("name")): r.get("specID") for r in ((facts or {}).get("roster") or [])}
    dps = sorted(n for n, sp in roster.items() if sp is not None and int(sp) not in TANK_SPECS | HEALER_SPECS)
    if not dps:
        dps = sorted({p["player"] for p in pots})
    if not dps:
        return None
    h3 = next((h for h in hearts if h.get("index") == 3), None)
    h3_sec = float(h3["startSec"]) if h3 and h3.get("startSec") is not None else POTION_H3_REF_SEC
    by = {n: sorted([p for p in pots if p["player"] == n], key=lambda p: p["sec"]) for n in dps}
    pre, pull, h2_ok, h2_late, h2_none, h3_ok, h3_none = [], [], [], [], [], [], []
    for n, ev in by.items():
        applied = [p["sec"] for p in ev if p["event"] == "SPELL_AURA_APPLIED"]
        first_removed = next((p["sec"] for p in ev if p["event"] == "SPELL_AURA_REMOVED"), None)
        if first_removed is not None and first_removed <= 35 and not any(a <= first_removed for a in applied):
            pre.append(n)
        elif any(a <= 5 for a in applied):
            pull.append(n)
        mid = [a for a in applied if 180 <= a <= 340]
        if duration >= POTION_H2_LATE_SEC + 10:
            if mid and mid[0] <= POTION_H2_LATE_SEC:
                h2_ok.append(n)
            elif mid:
                h2_late.append((n, mid[0]))
            else:
                h2_none.append(n)
        if duration >= h3_sec + 10:
            if any(h3_sec - 30 <= a <= h3_sec + 30 for a in applied):
                h3_ok.append(n)
            else:
                h3_none.append(n)
    parts = [f"선물약 {len(pre)}/{len(dps)}" + (f" (풀 때 먹음 {', '.join(pull[:4])})" if pull else "")]
    if duration >= POTION_H2_LATE_SEC + 10:
        parts.append(f"2심장(4:46) {len(h2_ok)}/{len(dps)}"
                     + (" · 늦음 " + ", ".join(f"{n} {mmss(t)}(쿨 {mmss(t + POTION_CD_SEC)})" for n, t in h2_late[:5]) if h2_late else "")
                     + (" · 안 먹음 " + ", ".join(h2_none[:6]) if h2_none else ""))
    if duration >= h3_sec + 10:
        parts.append(f"3심장({mmss(h3_sec)}) {len(h3_ok)}/{len(dps)}" + (" · 안 먹음 " + ", ".join(h3_none[:6]) if h3_none else ""))
    return {"dps": len(dps), "pre": pre, "pull": pull, "h2Ok": h2_ok, "h2Late": [n for n, _ in h2_late], "h2None": h2_none,
            "h3Ok": h3_ok, "h3None": h3_none, "line": "딜러 물약 · " + " · ".join(parts)}


def viper_review(raw: dict) -> dict | None:
    """V2.0 살무사가 언제 나왔고, 바로 앞(6초)에 알 든 사람이 파도를 맞았는지 · 알끼리 부딪혔는지."""
    raw = raw or {}
    seen = sorted((raw.get("viperSeen") or {}).values())
    if not seen:
        return None
    carry = {}
    spans = {}
    for x in sorted(raw.get("shell") or [], key=lambda x: x["sec"]):
        if x["event"] == "SPELL_AURA_APPLIED":
            carry[x["target"]] = x["sec"]
        elif x["target"] in carry:
            spans.setdefault(x["target"], []).append((carry.pop(x["target"]), x["sec"]))
    for n, a in carry.items():
        spans.setdefault(n, []).append((a, float("inf")))

    def carrying(n, t):
        return any(a - 0.05 <= t <= b + 0.35 for a, b in spans.get(n, []))
    waves = []
    for w in sorted(raw.get("waveHits") or [], key=lambda x: x["sec"]):
        if carrying(w["target"], w["sec"]) and not any(v[1] == w["target"] and abs(v[0] - w["sec"]) < 2 for v in waves):
            waves.append((w["sec"], w["target"]))
    bursts = [(x["sec"], x["target"]) for x in raw.get("eggBurst") or []
              if x.get("event") == "SPELL_AURA_APPLIED" and x.get("target") and x["target"] != "nil"]
    drops = [(x["sec"], x["target"]) for x in raw.get("shell") or [] if x["event"] == "SPELL_AURA_REMOVED"]
    out = []
    for grp in _clusters(seen, VIPER_CLUSTER_SEC):
        t = grp[0]
        # V5.3.6: 무리(4초 안 연달아 나온 살무사) 안쪽에서 맞은 알 든 사람도 같이 적는다
        wv = [(s, n) for s, n in waves if t - VIPER_TRIGGER_SEC <= s <= grp[-1] + 0.5]
        bs = [(s, n) for s, n in bursts if t - VIPER_TRIGGER_SEC <= s <= grp[-1] + 0.5]
        dr = [(s, n) for s, n in drops if t - VIPER_DROP_SEC <= s <= t + 0.3]
        if wv:
            kind, names, tsec = "wave", sorted({n for _s, n in wv}), wv[0][0]
        elif bs:
            kind, names, tsec = "burst", sorted({n for _s, n in bs}), bs[0][0]
        elif dr:
            kind, names, tsec = "drop", sorted({n for _s, n in dr}), dr[0][0]
        else:
            kind, names, tsec = None, [], None
        out.append({"sec": t, "count": len(grp), "trigger": kind, "names": names, "triggerSec": tsec})
    stung = sorted({x["target"] for x in raw.get("viperSting") or []})
    return {"clusters": out, "waves": waves, "bursts": bursts, "stung": stung, "stingCount": len(raw.get("viperSting") or []),
            "marks": viper_marks(raw)}


def viper_marks(raw: dict) -> list[dict]:
    """V5.3.6 살무사 주시(석화의 독침 1305163) → 5초 뒤 착지 스턴(1303414). 스턴 걸린 사람을 주시 대상과 묶는다."""
    stuns = sorted((raw or {}).get("viperStun") or [], key=lambda x: x["sec"])
    groups = []
    for x in stuns:
        if groups and x["sec"] - groups[-1][0]["sec"] <= 0.6:
            groups[-1].append(x)
        else:
            groups.append([x])
    used = set()
    out = []
    for m in sorted((raw or {}).get("viperSting") or [], key=lambda x: x["sec"]):
        lo, hi = VIPER_MARK_TO_STUN
        hit = next((i for i, g in enumerate(groups) if i not in used and lo <= g[0]["sec"] - m["sec"] <= hi), None)
        if hit is None:
            out.append({"sec": m["sec"], "target": m["target"], "stunSec": None, "stunned": []})
            continue
        used.add(hit)
        names = []
        for x in groups[hit]:
            if x["target"] not in names:
                names.append(x["target"])
        if m["target"] in names:
            names.remove(m["target"])
            names.insert(0, m["target"])
        out.append({"sec": m["sec"], "target": m["target"], "stunSec": groups[hit][0]["sec"], "stunned": names})
    for i, g in enumerate(groups):   # 주시 기록 없이 스턴만 남은 경우
        if i not in used:
            names = []
            for x in g:
                if x["target"] not in names:
                    names.append(x["target"])
            out.append({"sec": None, "target": None, "stunSec": g[0]["sec"], "stunned": names})
    return sorted(out, key=lambda x: x["stunSec"] if x["stunSec"] is not None else x["sec"])


def egg_viper_notes(vr: dict | None, limit: int = 6) -> list[str]:
    """V5.3.6 화면 「참고」 줄: 알 든 사람 파도 전부 · 살무사 주시 → 스턴 전부."""
    if not vr:
        return []
    out = []
    cl = vr.get("clusters") or []
    total = sum(c["count"] for c in cl)
    waves = vr.get("waves") or []
    head = f"알 든 사람 부식의 파도 {len(waves)}번"
    if waves:
        head += " · " + ", ".join(f"{n} {mmss(s)}" for s, n in waves[:limit]) + (f" 외 {len(waves) - limit}" if len(waves) > limit else "")
    bursts = vr.get("bursts") or []
    if bursts:
        head += " · 알 충돌 " + "·".join(sorted({n for _s, n in bursts})[:4]) + f" {mmss(bursts[0][0])}"
    head += f" → 살무사 {total}마리 (" + " · ".join(f"{mmss(c['sec'])} {c['count']}" for c in cl[:limit]) + (" …" if len(cl) > limit else "") + ")"
    out.append(head)
    marks = [m for m in vr.get("marks") or [] if m.get("stunned") or m.get("target")]
    if marks:
        parts = []
        for m in marks[:limit]:
            t = m["stunSec"] if m.get("stunSec") is not None else m["sec"]
            if m.get("stunned"):
                parts.append(f"{mmss(t)} " + "+".join(m["stunned"][:4]) + (f" 외 {len(m['stunned']) - 4}" if len(m["stunned"]) > 4 else ""))
            else:
                parts.append(f"{mmss(t)} {m['target']} 주시(스턴 없음)")
        n_stun = sum(len(m.get("stunned") or []) for m in marks)
        out.append(f"살무사 주시→스턴 {len(marks)}번 · {n_stun}명 · " + ", ".join(parts) + (f" 외 {len(marks) - limit}번" if len(marks) > limit else ""))
    return out


def _trigger_txt(c: dict) -> str:
    if c.get("trigger") == "wave":
        return f"알 든 {'·'.join(c['names'][:3])} 부식의 파도 {mmss(c['triggerSec'])}"
    if c.get("trigger") == "burst":
        return f"알 충돌 {'·'.join(c['names'][:3])} {mmss(c['triggerSec'])}"
    if c.get("trigger") == "drop":
        return f"알 든 {'·'.join(c['names'][:3])} 알 놓침 {mmss(c['triggerSec'])} (물·피격?)"
    return "원인 미확인 (알 운반 밖)"


def egg_burst_line(raw: dict) -> str | None:
    ev = [x for x in (raw or {}).get("eggBurst") or [] if x["event"] in ("SPELL_DAMAGE", "SPELL_AURA_APPLIED", "SPELL_CAST_SUCCESS")]
    if not ev:
        return None
    firsts = []
    for t in _clusters([x["sec"] for x in ev], 3.0):
        names = sorted({x["target"] for x in ev if t[0] <= x["sec"] <= t[-1] and x["target"] and x["target"] != "nil"})
        firsts.append(f"{mmss(t[0])}" + (f" {'·'.join(names[:3])}" if names else ""))
    return f"알 충돌(맹독성 분출) {len(firsts)}번 · " + ", ".join(firsts[:5]) + " → 곧바로 살무사 (V2.0 실측)"


def build(meta: dict, mech: dict, raid: dict, facts: dict, raw: dict | None = None) -> dict:
    raw = raw or {}
    obs = (mech or {}).get("observations") or {}
    duration = float(meta.get("durationSec") or 0)
    result = str(meta.get("result") or (mech.get("encounter") or {}).get("result") or "").lower()
    diff = int(meta.get("difficultyID") or (mech.get("encounter") or {}).get("difficultyID") or 0)
    if meta.get("analysisExcluded") or meta.get("resetPull") or 0 <= duration < 1.0:
        return {"version": VERSION, "tryNo": meta.get("tryNo"), "excluded": True, "durationSec": duration,
                "lines": {"어디까지": f"가짜/리셋 풀 · {duration:.1f}초", "첫 실수": "-", "전멸 원인": "판정 제외",
                          "딜·힐": "-", "다음 풀": "-"},
                "cause": {"group": "판정 제외", "code": "EXCLUDED"}, "hearts": 0, "extraLines": []}
    hearts = obs.get("heartWindows") or []
    hp = raw.get("hp") or []
    end_hp = hp[-1][1] if hp and result != "kill" else None
    # V5.3.7: 심장 도중 전멸이면 그 심장 창의 끝 = 전멸 시각. 「종료 HP · 미달」로 읽으면 안 된다 (11/9 월 6트).
    def _heart_done(h) -> bool:
        return result == "kill" or (h.get("endSec") is not None and float(h["endSec"]) < duration - 1.0)
    proj = (obs.get("heartProjection") or {}).get("label")

    # 1. 어디까지
    mythic = diff == 16
    p3 = MYTHIC_P3_SEC if mythic else None
    if result == "kill":
        reach = f"킬 {mmss(duration)} · 심장 {len(hearts)}회"
    else:
        _stage = stage_at(duration - 0.5, hearts, p3)
        _cut = hearts and not _heart_done(hearts[-1])
        reach = (f"{_stage}{' 도중' if _cut else ''}" + ("" if _stage == f"{len(hearts)}심장" else f" · {len(hearts)}심장")
                 + f" · {mmss(duration)} 전멸" + (f" · 보스 HP {end_hp:.1f}%" if end_hp is not None else ""))

    # 공략 기준선 (신화)
    h1 = next((h for h in hearts if h.get("index") == 1), None)
    h2 = next((h for h in hearts if h.get("index") == 2), None)
    h1_hp = float(h1["endBossHpPct"]) if h1 and h1.get("endBossHpPct") is not None and _heart_done(h1) else None
    h1_cut = bool(h1 and not _heart_done(h1))
    h1_short = bool(mythic and h1_hp is not None and h1_hp > HEART1_HP_LINE)
    doom_alive = None
    if raw.get("available") and h2 and h2.get("endSec") is not None and raw.get("doomSeen"):
        cut = float(h2["endSec"]) + 2.0
        seen = [g for g, t in raw["doomSeen"].items() if t <= cut]
        died = raw.get("doomDied") or {}
        doom_alive = {"total": len(seen), "alive": sum(1 for g in seen if not (g in died and died[g] <= cut))}
    acid_done = [a for a in (raw.get("acid") or []) if a["kind"] == "done"]
    egg_late = bool(mythic and raw.get("weakness") and float(raw["weakness"][0]["sec"]) > EGG_DEFECT_LATE_SEC)

    # 2. 첫 실수
    fm = obs.get("firstVerifiedFailure") or {}
    if fm.get("sec") is not None:
        who = ", ".join(short(p) for p in (fm.get("players") or [])[:2])
        first = (f"{stage_at(fm['sec'], hearts, p3)} {mmss(fm['sec'])} {fm.get('label') or fm.get('kind')}"
                 + (f" · {who}" if who else ""))
    else:
        first = "확정된 첫 실수 없음"
    deaths = sorted([d for d in ((facts or {}).get("deaths") or []) if isinstance(d, dict)], key=lambda d: float(d.get("sec") or 0))
    _col0 = ((raid or {}).get("collapse") or {}).get("startSec")
    dwin = death_windows(deaths, hearts, raw, mythic, cutoff=_col0 if result != "kill" else None)
    if fm.get("sec") is None and dwin["wave"]:
        w_name, w_sec = dwin["wave"][0]
        first = f"{stage_at(w_sec, hearts, p3)} {mmss(w_sec)} 부식의 파도 사망 · {w_name} [추정]"
    first_kind = fm.get("kind")

    # V2.0 알 운반 → 살무사: 붕괴 25초 안쪽에서 살무사가 먼저 나왔으면 그게 뿌리
    vr = viper_review(raw) if (raw.get("available") and mythic) else None
    vchain = None
    if vr and result != "kill" and _col0 is not None:
        inwin = [c for c in vr["clusters"]
                 if float(_col0) - VIPER_BEFORE_COLLAPSE_SEC <= c["sec"] <= float(_col0) + 1.0]
        if inwin:
            vchain = inwin[0]   # 붕괴 25초 안쪽 첫 살무사가 뿌리
    if vchain:
        t0 = vchain["triggerSec"] if vchain.get("triggerSec") is not None else vchain["sec"]
        first = (f"{stage_at(t0, hearts, p3)} {mmss(t0)} {_trigger_txt(vchain)} → 살무사 {mmss(vchain['sec'])}"
                 + (" [확정]" if vchain.get("trigger") in ("wave", "burst") else " [추정]"))
        first_kind = EGG_VIPER_KIND
    elif (fm.get("kind") == "unchecked_rage" and fm.get("sec") is not None and _col0 is not None
          and float(fm["sec"]) < float(_col0) - FIRST_MISTAKE_WINDOW_SEC and result != "kill"):
        # 1:16 · 1:25 처럼 붕괴보다 한참 앞선 막을 수 없는 분노는 첫 실수로 치지 않는다 (참고로만)
        near_fail = [f for f in (obs.get("verifiedFailures") or []) if f.get("sec") is not None
                     and float(_col0) - FIRST_MISTAKE_WINDOW_SEC <= float(f["sec"]) <= float(_col0) + 1.0]
        if near_fail:
            nf = near_fail[0]
            who2 = ", ".join(short(x) for x in (nf.get("players") or [])[:2])
            first = (f"{stage_at(nf['sec'], hearts, p3)} {mmss(nf['sec'])} {nf.get('label') or nf.get('kind')}"
                     + (f" · {who2}" if who2 else ""))
            first_kind = nf.get("kind")
        elif dwin["wave"]:
            w_name, w_sec = dwin["wave"][0]
            first = f"{stage_at(w_sec, hearts, p3)} {mmss(w_sec)} 부식의 파도 사망 · {w_name} [추정]"
            first_kind = "wave_death"
        else:
            first = f"붕괴({mmss(_col0)}) 20초 안 확정 실패 없음"
            first_kind = None
        first += f" (막을 수 없는 분노 {mmss(fm['sec'])}는 붕괴보다 한참 앞이라 참고만)"

    # 3. 전멸 원인
    collapse = (raid or {}).get("collapse") or {}
    col_start = collapse.get("startSec")
    spells = collapse.get("spells") or {}
    failures = obs.get("verifiedFailures") or []
    if result == "kill":
        cause = {"group": "킬", "code": "KILL", "label": "킬", "confidence": "verified", "next": "-"}
    elif vchain:
        top_txt = ""
        if spells:
            top, n = max(spells.items(), key=lambda kv: kv[1])
            top_txt = f" {top} {n}명"
        rage_mid = [t for t in (raw.get("rage") or []) if vchain["sec"] <= t <= float(col_start) + 1.0]
        cause = {"group": EGG_VIPER_KIND, "code": "EGG_VIPER",
                 "confidence": "verified" if vchain.get("trigger") in ("wave", "burst") else "likely",
                 "label": (f"{_trigger_txt(vchain)} → 살무사 {vchain['count']}마리 {mmss(vchain['sec'])} → 붕괴{top_txt} ({mmss(col_start)})"
                           + (f" · 그 사이 막을 수 없는 분노 {len(rage_mid)}회" if rage_mid else "")),
                 "next": NEXT["egg_viper"]}
    elif spells:
        top, n = max(spells.items(), key=lambda kv: kv[1])
        near = [f for f in failures if f.get("sec") is not None and col_start is not None
                and 0 <= float(col_start) - float(f["sec"]) <= 12]
        link = near[-1] if near else None
        if top == "산성 배출" and not link:
            cause = {"group": "유체 광폭(산성 배출)", "code": "ACID_PURGE", "confidence": "likely",
                     "label": f"유체 광폭 산성 배출 → {top} {n}명 ({mmss(col_start)})"}
            link = {"kind": "acid_purge"}
        elif top in ("막을 수 없는 분노",) or (link and link.get("kind") == "unchecked_rage"):
            cause = {"group": "막을 수 없는 분노", "code": "UNCHECKED_RAGE", "confidence": "verified",
                     "label": f"막을 수 없는 분노 → {top} {n}명 ({mmss(col_start)})"}
        elif link:
            cause = {"group": str(link.get("label")), "code": str(link.get("kind")).upper(), "confidence": "likely",
                     "label": f"{link.get('label')} {mmss(link.get('sec'))} → {top} {n}명 ({mmss(col_start)})"}
        else:
            win = _window_at(col_start, dwin)
            if win:
                title, advice = DEATH_WINDOWS[win]
                cause = {"group": f"{title} 붕괴", "code": "WINDOW_" + win.upper(), "confidence": "likely",
                         "label": f"{title} 구간 붕괴 · {top} {n}명 ({mmss(col_start)}) · 그 전에 {title} 사망 {len(dwin[win])}명"}
                link = {"kind": "window_" + win}
                NEXT["window_" + win] = advice
            else:
                cause = {"group": f"{top} 중심 붕괴", "code": "COLLAPSE", "confidence": "unknown",
                         "label": f"{top} {n}명 중심 붕괴 ({mmss(col_start)})"}
        cause["next"] = NEXT.get(str((link or fm).get("kind")), "붕괴 직전 10초를 직접 확인하세요.")
    elif failures:
        last = max(failures, key=lambda f: float(f.get("sec") or 0))
        cause = {"group": str(last.get("label")), "code": str(last.get("kind")).upper(), "confidence": "likely",
                 "label": f"{last.get('label')} ({mmss(last.get('sec'))}) → {mmss(duration)} 전멸 · 붕괴 기록 없음"}
        cause["next"] = NEXT.get(str(last.get("kind")), "붕괴 직전 10초를 직접 확인하세요.")
    elif acid_done and result != "kill":
        cause = {"group": "유체 광폭(산성 배출)", "code": "ACID_PURGE", "confidence": "likely",
                 "label": f"유체 광폭 산성 배출 {len(acid_done)}회 (첫 {stage_at(acid_done[0]['sec'], hearts, p3)} {mmss(acid_done[0]['sec'])}) → {mmss(duration)} 전멸"}
        cause["next"] = NEXT["acid_purge"]
    elif egg_late and duration < float((h2 or {}).get("endSec") or 330) + 30:
        cause = {"group": "큰 알 가마솥 늦음", "code": "EGG_LATE", "confidence": "likely",
                 "label": f"큰 알 가마솥 {mmss(raw['weakness'][0]['sec'])} (공략 4:27까지) → {stage_at(duration - 0.5, hearts, p3)} {mmss(duration)} 전멸"}
        cause["next"] = "작은 알은 3:50(늦어도 4:00), 큰 알은 4:15쯤 집어 4:27까지 가마솥에. 남쪽방 출발 데드라인을 정하세요."
    elif doom_alive and doom_alive["alive"] and duration > float(h2["endSec"]):
        cause = {"group": "파멸비늘 미처치", "code": "DOOMSCALE_ALIVE", "confidence": "likely",
                 "label": f"약화된 파멸비늘 {doom_alive['alive']}/{doom_alive['total']}마리 2심장 끝까지 생존 → {stage_at(duration - 0.5, hearts, p3)} {mmss(duration)} 전멸"}
        cause["next"] = NEXT["doomscale_alive"]
    elif h1_short and len(hearts) < KILL_HEARTS_MYTHIC:
        cause = {"group": "1심장 딜 미달", "code": "HEART1_DPS", "confidence": "likely",
                 "label": f"1심장 종료 보스 {h1_hp:.1f}% (공략 66%) → {stage_at(duration - 0.5, hearts, p3)} {mmss(duration)} 전멸"}
        cause["next"] = NEXT["heart1_dps"]
    elif mythic and duration >= TIME_LIMIT_SEC:
        cause = {"group": "시간 초과(딜 부족)", "code": "TIME_LIMIT", "confidence": "likely",
                 "label": f"{mmss(duration)} 전멸 · 다른 공대 킬은 전부 9:50~10:02 → 시간 안에 못 잡음 (딜 부족)"}
        cause["next"] = "3심장(9:43) 전에 보스를 더 깎아야 합니다: 1심장 66%·2심장 40%를 먼저 확인하고, 3심장에 블러드·물약."
    elif len(hearts) >= KILL_HEARTS_MYTHIC and diff == 16:
        cause = {"group": "3심장 후 마무리 실패", "code": "LATE_DPS", "confidence": "unknown",
                 "label": f"{len(hearts)}심장 후 {mmss(duration)} 전멸 · 확정 실패 없음 → 딜 부족 의심"}
        cause["next"] = "3심장 직후 쿨기·물약을 몰아서 마무리 딜을 당기세요."
    elif max((len(v) for k, v in dwin.items() if k != "wave"), default=0) >= 3:
        win = max((k for k in dwin if k != "wave"), key=lambda k: len(dwin[k]))
        title, advice = DEATH_WINDOWS[win]
        cause = {"group": f"{title} 사망", "code": "WINDOW_" + win.upper(), "confidence": "likely",
                 "label": f"{title} 구간 사망 {len(dwin[win])}명 (첫 {dwin[win][0][0]} {mmss(dwin[win][0][1])}) → {mmss(duration)} 전멸"}
        cause["next"] = advice
    else:
        cause = {"group": "원인 미확정", "code": "UNKNOWN", "confidence": "unknown",
                 "label": f"{len(hearts)}심장 · {mmss(duration)} 전멸 · 확정 실패 없음"}
        cause["next"] = "붕괴 직전 10초를 직접 확인하세요."
    if col_start is not None:
        before = [d for d in deaths if float(d.get("sec") or 0) < float(col_start)]
        if before:
            cause["label"] += f" · 붕괴 전 사망 {len(before)}명 (첫 {short(before[0].get('name'))} {mmss(before[0].get('sec'))})"
    cause_line = f"{cause['label']} [{CONF[cause['confidence']]}]"

    # 4. 딜 = 심장 진도
    bits = []
    if result == "kill":
        bits.append("킬")
    elif not hearts:
        bits.append("심장 전 전멸" + (f" · 보스 HP {end_hp:.1f}%" if end_hp is not None else ""))
    if h1_hp is not None:
        if mythic:
            gap = h1_hp - HEART1_HP_LINE
            bits.append(f"1심장 종료 {h1_hp:.1f}% ({'미달 ' + format(gap, '+.1f') + '%p' if gap > 0 else '66% 통과'})")
        else:
            bits.append(f"1심장 종료 {h1_hp:.1f}%")
    if h1_cut:
        bits.append(f"1심장 도중 전멸 ({mmss(h1.get('startSec'))} 시작 · {float(h1.get('durationSec') or 0):.0f}초 만) · 딜 판정 안 함")
    if h2 and not _heart_done(h2):
        bits.append(f"2심장 도중 전멸 ({mmss(h2.get('startSec'))} 시작) · 딜 판정 안 함")
    elif h2 and h2.get("endBossHpPct") is not None:
        _h2 = float(h2['endBossHpPct'])
        bits.append(f"2심장 종료 {_h2:.1f}%" + ((" ⚠" if _h2 > HEART2_HP_WARN else "") + " (다른 공대 약 40%)" if mythic else ""))
    if doom_alive:
        bits.append(f"파멸비늘 {doom_alive['total'] - doom_alive['alive']}/{doom_alive['total']} 처치" + (" ⚠" if doom_alive["alive"] else ""))
    if result != "kill" and mythic and end_hp is not None:
        ref = ref_hp_at(duration)
        if ref is not None:
            bits.append(f"같은 시각 다른 공대 전멸 HP 중앙 {ref:.0f}% (우리 {end_hp - ref:+.0f}%p)")
    if result != "kill" and hearts and mythic:
        bits.append(f"다른 공대 킬은 {KILL_TIME_MYTHIC}")
    if result == "kill" and proj:
        bits.append(proj)
    if hearts and not bits:
        bits.append(f"{len(hearts)}심장 도달")
    dps = " · ".join(bits)
    # 확정 실패로 원인을 잡았어도 딜 미달이면 다음 풀에 한 줄 더
    if h1_short and cause.get("code") not in ("HEART1_DPS", "KILL"):
        cause["next"] += " / " + NEXT["heart1_dps"]

    extra = []
    notes = []   # V1.8 화면 「참고」 줄
    for h in hearts:
        hp_txt = (f" · 보스 {float(h['startBossHpPct']):.1f}%→{float(h['endBossHpPct']):.1f}%"
                  if h.get("startBossHpPct") is not None and h.get("endBossHpPct") is not None else "")
        dps_txt = f" · 심장 딜 {float(h['heartDps']) / 1e6:.1f}M/s" if h.get("heartDps") else ""
        extra.append(f"심장   {h.get('index')}심장 {mmss(h.get('startSec'))}~{mmss(h.get('endSec'))}{hp_txt}{dps_txt}")
    cr = cry_review(raw, {r.get("guid"): short(r.get("name")) for r in ((facts or {}).get("roster") or [])})
    if raw.get("available") and (cr["kicks"] or cr["misses"]):
        extra.append("절규   " + cr["line"])
        if cr["groups"]:
            extra.append("절규조 " + " · ".join(cr["groups"]))
        if cr.get("lateLine"):
            extra.append("절규   " + cr["lateLine"])
            notes.append("절규 " + cr["lateLine"])
        if cr.get("castSecs"):
            extra.append(f"절규   놓친 시전 길이 실측 {cr['castSecs'][0]:.1f}초" + (f"~{cr['castSecs'][-1]:.1f}초" if len(cr["castSecs"]) > 1 else "")
                         + " (늦은 차단 기준을 이 값으로 바꿀 수 있음)")
            notes.append(extra[-1].replace("절규   ", "절규 ", 1))
    if any(dwin.values()):
        extra.append("사망 구간 " + " · ".join(f"{DEATH_WINDOWS[k][0]} {len(v)}" for k, v in dwin.items() if v)
                     + (" (파도: " + ", ".join(f"{n} {mmss(t)}" for n, t in dwin["wave"][:3]) + ")" if dwin["wave"] else ""))
    if acid_done:
        extra.append(f"유체   광폭 산성 배출 {len(acid_done)}회 (" + ", ".join(mmss(a["sec"]) for a in acid_done[:4]) + ") → 유체 딜·CC 확인")
    if cr.get("missNth"):
        extra.append("절규짤 놓친 순번 " + " · ".join(f"{k} {v}" for k, v in sorted(cr["missNth"].items())))
    wk = raw.get("weakness") or []
    if wk and mythic:
        late = float(wk[0]["sec"]) > EGG_DEFECT_LATE_SEC
        extra.append(f"알     큰 알 가마솥(결함: 약화) 첫 {mmss(wk[0]['sec'])}" + (" ⚠ 늦음" if late else " ✅")
                     + " (공략 4:27 · 다른 공대 킬 4:24~4:25)")
    # V1.9 공대 공략 판정 (신화 공략이라 신화에서만)
    plan = {}
    if raw.get("available") and mythic:
        fr = fang_review(raw)
        for x in fr:
            extra.append("줄     " + x["line"])
        if fr:
            plan["fang"] = [{"sync": x["sync"], "spread": x["spread"], "count": x["count"]} for x in fr]
        sc = soul_coil_review(raw, mythic, duration)
        if sc:
            extra.append("똬리   " + sc["line"])
            extra.append("똬리조 " + sc["teamLine"])
            plan["coil"] = {"slams": len(sc["slams"]), "ok": sc["ok"], "empty": sc["empty"], "cross": sc["cross"]}
            if sc["empty"] and cause.get("code") not in ("KILL",):
                cause["next"] += f" / 사이페 똬리 {', '.join(str(k) + '타' for k in sc['empty'])}를 아무도 안 맞음: 조 순서(1조 1·4·7 · 2조 2·5·8 · 3조 3·6) 확인."
        pr = potion_review(raw, hearts, facts, duration)
        if pr and (pr["pre"] or pr["pull"] or pr["h2Ok"] or pr["h2Late"] or pr["h3Ok"]):
            extra.append("물약   " + pr["line"])
            plan["potion"] = {k: pr[k] for k in ("dps", "pre", "pull", "h2Ok", "h2Late", "h2None", "h3Ok", "h3None")}
        eb = egg_burst_line(raw)
        if eb:
            extra.append("알충돌 " + eb)
        if vr:
            cl = vr["clusters"]
            extra.append(f"살무사 첫 {mmss(cl[0]['sec'])} · {sum(c['count'] for c in cl)}마리 · "
                         + ", ".join(f"{mmss(c['sec'])} {c['count']}마리 ← {_trigger_txt(c)}" for c in cl[:8])
                         + (f" · 석화의 독침 스턴 {vr['stingCount']}번" if vr.get("stingCount") else ""))
            plan["viper"] = {"first": cl[0]["sec"], "chain": bool(vchain),
                             "trigger": (vchain or {}).get("trigger"), "names": (vchain or {}).get("names") or [],
                             "waveCarriers": [n for _s, n in vr["waves"]], "bursts": [n for _s, n in vr["bursts"]],
                             "stuns": [{"sec": m["stunSec"], "target": m["target"], "stunned": m["stunned"]} for m in vr.get("marks") or []]}
            notes[:0] = egg_viper_notes(vr)   # 화면 참고 칸은 4줄까지라 알·살무사를 앞에
    ac = obs.get("anguishedCry") or {}
    if not raw.get("available") and (ac.get("startCount") or ac.get("completedCount")):
        extra.append(f"절규   시전 {ac.get('startCount', 0)} · 완료(놓침) {ac.get('completedCount', 0)}")
    ur = (obs.get("uncheckedRage") or {}).get("count")
    if ur:
        rg = raw.get("rage") or []
        extra.append(f"분노   막을 수 없는 분노 {ur}회 (탱 공백)" + (" · " + ", ".join(mmss(t) for t in rg[:6]) if rg else ""))
    cc = (obs.get("calcifiedCorpse") or {}).get("count")
    if cc:
        names = sorted({short(x.get("target") or x.get("player") or x.get("targetName")) for x in (obs.get("calcifiedCorpse") or {}).get("events") or [] if isinstance(x, dict)})
        extra.append(f"석회화 {cc}명" + (f" ({', '.join(n for n in names if n)})" if any(names) else ""))

    return {
        "version": VERSION, "tryNo": meta.get("tryNo"), "durationSec": duration,
        "lines": {"어디까지": reach, "첫 실수": first, "전멸 원인": cause_line, "딜·힐": dps, "다음 풀": cause["next"]},
        "cause": cause, "hearts": len(hearts), "heartWindows": hearts, "extraLines": extra, "notes": notes,
        "cry": {"kicks": len(cr["kicks"]), "misses": len(cr["misses"]), "byPlayer": cr["byPlayer"], "missNth": cr.get("missNth") or {}, "late": len(cr.get("late") or [])},
        "firstKind": first_kind, "heart1EndHp": h1_hp, "rosterNames": sorted({short(r.get("name")) for r in ((facts or {}).get("roster") or [])}), "heart1Short": h1_short, "doomscale": doom_alive, "stages": ulatek_stages(duration, hearts, p3, result == "kill", mythic), "acidCount": len(acid_done), "deathWindows": {k: len(v) for k, v in dwin.items()},
        "plan": plan,
    }


def build_from_dir(try_dir, use_raw: bool = True) -> dict:
    d = Path(try_dir)
    meta = read_json(d / "meta.json")
    mech = read_json(d / "mechanic_review.json")
    raid = read_json(d / "raid_wipe_review.json") or read_json(d / "raid_review.json")
    facts = read_json(d / "raid_facts.json")
    raw = scan_raw(d / str(meta.get("rawFile") or "raw.log.gz"), str(meta.get("startTimestamp") or "")) if use_raw else {}
    return build(meta, mech, raid, facts, raw)
