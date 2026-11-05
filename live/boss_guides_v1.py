"""잡은 네임드 6개 · 트라이 결론 다섯 줄 + 진행 칩 + 시간축 보스 시전 (읽기 전용) · BOSS-GUIDES-V1

대상: 스조라크(3420) · 쌍둥이 송곳니(3421) · 매장된 파수꾼(3445) · 악성의 바쉬니크(3455)
      · 영혼살무사 네크잘리(3470) · 길 잃은 탐험가(3497) · V1.5: 키식스(3513, 키식스 해방)
V1.6: 키식스 신화 보강 · 다른 공대 킬 4개 기준선 · 인벤 보스 이동 HP(65%·38%·25%) · 무효화 중첩 · 암흑 황폐 교대 · 공포 중 리혼/결의

기준: Viserio 쿨기 공략 7개 (사용자 제공 2026-10-21) + 이 PC 로그(파수꾼 신화 8트, 나머지 영웅)에서 찾은 주문 ID.
판정 재료: raid_facts.json(사망·마지막 피격 주문·적 시전 요약·로스터) + raw.log 의 보스 HP 만.
파일을 쓰지 않는다. 똬리·울라텍 모듈과 같은 원칙: 첫 실수와 전멸 원인을 나누고 [확정]/[추정]/[미확정]을 붙인다.

WCL 신화 킬 자료(boss_refs.zip)가 오면 킬 시간·단계 시각 기준선을 여기에 더한다 (지금은 공략 문장 기준).
"""
from __future__ import annotations

import csv
import gzip
import json
import math
from collections import Counter
from pathlib import Path

VERSION = "BOSS-GUIDES-V1.6"
BERSERK = {26662}               # 광폭화 (파수꾼 신화 7:00 관측)
HEALER_SPECS = {65, 105, 256, 257, 264, 270, 1468}

# ---------------------------------------------------------------- 보스별 공략 표
# stages: kind "cycle" = 기준 시전마다 새 구간 / "phase" = 정해진 시전이 처음 나올 때 다음 단계
#         kind "seq" = (이름, 주문, n번째, +초) · 같은 주문의 n번째 시전으로 단계를 나눈다 (V1.5 키식스 루프)
# mass: 사망 마지막 피격 주문 → (기믹 이름, 다음 풀 조언)   avoid: 첫 실수 후보(피할 수 있는 것)
# windows: (이름, 기준 시전, 시작 +초, 끝 +초, 조언) · abs_windows: (이름, 시작초, 끝초, 조언)
# timeline: 시간축 스킬별 줄에 올릴 보스 시전 · anchors: 점선(주기 기준)
SPECS: dict[int, dict] = {
    3445: {
        "name": "매장된 파수꾼", "bossNpcs": ("-258557-", "-258558-"),
        "stages": {"kind": "cycle", "anchors": (1284588,), "gap": 20, "first": "오프너", "label": "{n}주기",
                   "anchorStarts": False, "killChips": 5},
        "mass": {
            1284452: ("물방울 미흡수 → 맹독성 작렬", "유독한 물방울은 한 사람당 하나씩 꼭 밟기 · 정체 직전 마지막 물방울 세트 담당 확인 (안 밟으면 공대 전체 작렬)."),
            1284451: ("물방울 밟다 사망", "물방울 밟기 전 체력 확인 · 낮으면 다음 사람에게 넘기고 첫·마지막 세트엔 쫄 창 쿨 아래서."),
            1284258: ("쫄 창 오염", "맹독 응고물 쫄을 빨리 잡아 오염 시간을 줄이기 · 첫 쫄 창 공대 뎀감, 두 번째 쫄 창 힐 쿨."),
            1284813: ("정체 퍼즐 짝 실패 (나선의 독소)", "황산성 정지장: 중첩 합이 정확히 4가 되게 짝 맞추기 · 이동기로 짝에게 빨리."),
            1296882: ("원시맹독 접촉", "원시맹독 보유자끼리만 닿기 · 깨끗한 사람과 닿으면 폭발+넉백 · 보유 중 개인 생존기."),
            1284500: ("산성의 징표 누적", "징표 중첩은 주기 끝으로 갈수록 커짐 · 주기 끝·정체 구간에 개인 생존기와 힐 쿨."),
            1284506: ("피의 징표 누적", "징표 중첩은 주기 끝으로 갈수록 커짐 · 주기 끝·정체 구간에 개인 생존기와 힐 쿨."),
            1284941: ("배양된 파열", "배양된 파열 위치 확인 · 쫄 창과 겹칠 때 생존기."),
        },
        "avoid": (1284452, 1296882, 1284813, 1284451),
        "windows": (("정체 퍼즐", 1284588, -6, 12, "정체 직전 물방울 세트를 다 밟고 들어가기 · 짝 맞추기는 이동기로 빨리."),
                    ("쫄 창", 1284251, 0, 20, "쫄(맹독 응고물) 창마다 공대 생존기 · 쫄 빨리 처치.")),
        "abs_windows": (),
        "timeline": {1284588: "황산성 정지장", 1284251: "맹독 응고물", 1284434: "유독한 물방울", 1288232: "불안정한 독기",
                     1296878: "변화무쌍한 원시맹독", 1284483: "역병에 물든 피", 1284458: "강화의 격돌", 1284487: "피맹독 주입"},
        "anchors": (1284588,),
        "colors": {"광폭화": "red", "황산성 정지장": "blue", "맹독 응고물": "purple", "유독한 물방울": "green", "맹독성 작렬": "red",
                   "불안정한 독기": "yellow", "변화무쌍한 원시맹독": "orange", "역병에 물든 피": "yellow", "강화의 격돌": "gray",
                   "피맹독 주입": "gray", "나선의 독소": "red", "오염": "purple"},
    },
    3420: {
        "name": "스조라크", "bossNpcs": ("-257347-",),
        "stages": {"kind": "cycle", "anchors": (1286033,), "gap": 40, "offset": 25, "first": "1루프", "label": "{n}루프",
                   "labelStart": 2, "anchorStarts": False, "killChips": 3},
        "mass": {
            1287083: ("돌개바람 피격", "돌개바람(독 회오리)에 맞으면 바로 해제 · 뭉친 곳으로 회오리가 지나가지 않게 (가장 흔한 사망)."),
            1287205: ("사잇단계 낭종 터짐", "버티기 사잇단계 낭종(구슬)은 배정 인원만 · 바람 방향에 맞춰 터뜨려 자리 지키기."),
            1285965: ("밀려나 경계 밖 (울라텍의 존재감)", "교차의 바람 넉백은 이동기로 상쇄 · 경계(존재감) 쪽에 서지 않기 · 사잇단계 돌풍엔 낭종 넉백으로 버티기."),
            1297707: ("병독성 전이", "뱀의 격노 징표에 모였다가 돌진 직후 바로 산개 · 병독성 해제 때 주변 전염."),
            1277031: ("절단 맞아주기 인원 부족", "절단은 반반 맞아주기 · 사망자가 생겨 반쪽이 얇으면 그룹 쿨·생존기."),
            1305959: ("맹독성 쇄도 폭발", "맹독성 쇄도 대상은 멀리 · 터질 때 생존기 (구슬 자리 공략대로)."),
            1285453: ("교차의 바람 폭발", "격렬한 교차의 바람 대상끼리 떨어지기 · 넉백은 이동기로 상쇄."),
            1285616: ("교차의 바람 폭발", "격렬한 교차의 바람 대상끼리 떨어지기 · 넉백은 이동기로 상쇄."),
            1312219: ("교차의 바람 폭발", "격렬한 교차의 바람 대상끼리 떨어지기 · 넉백은 이동기로 상쇄."),
            1306120: ("맹독성 쇄도 폭발", "맹독성 쇄도 대상은 공대에서 멀리 (거리 감쇠) · 대상은 생존기 · 구슬 자리 공략대로."),
            1312156: ("맹독성 쇄도 폭발", "맹독성 쇄도 대상은 공대에서 멀리 (거리 감쇠) · 대상은 생존기 · 구슬 자리 공략대로."),
            1300089: ("병독성 전이", "뱀의 격노 징표에 모였다가 돌진 직후 바로 산개 · 병독성 해제 때 주변 전염."),
            1312189: ("병독성 전이", "뱀의 격노 징표에 모였다가 돌진 직후 바로 산개 · 병독성 해제 때 주변 전염."),
            1285998: ("절단 맞아주기 (절단된 상처)", "절단은 반반 맞아주기 · 상처 도트가 남은 채 병독성 창이 오니 맞은 쪽 먼저 채우기."),
            1285999: ("절단 맞아주기 인원 부족", "절단은 반반 맞아주기 · 사망자가 생겨 반쪽이 얇으면 그룹 쿨·생존기."),
        },
        "avoid": (1287083, 1287205, 1297707, 1300089, 1312189, 1285965),
        "windows": (("병독성 창", 1297707, -3, 20, "병독성 창: 모일 때 공대 뎀감+힐 쿨, 15~20초 뒤 꼬리에 두 번째 힐 쿨."),
                    ("사잇단계", 1286033, 0, 25, "사잇단계(버티기): 시작에 이동 쿨, 낭종 터질 때마다 이동 중 힐.")),
        "abs_windows": (("막 루프 4:55~5:35", 295, 335, "막 루프 5번째 쇄도 폭발~6번째 병독성: 개인 생존기 하나 남겨두기."),),
        "timeline": {1277027: "절단", 1277002: "찢어발기기", 1287072: "돌개바람", 1305959: "맹독성 쇄도",
                     1297707: "병독성", 1285453: "격렬한 교차의 바람", 1286033: "버티기"},
        "anchors": (1297707, 1286033),
        "colors": {"절단": "orange", "찢어발기기": "gray", "돌개바람": "red", "맹독성 쇄도": "purple", "병독성": "red",
                   "격렬한 교차의 바람": "blue", "버티기": "blue", "끈적거리는 낭종": "yellow"},
    },
    3421: {
        "name": "쌍둥이 송곳니", "bossNpcs": ("-257361-", "-257368-"),
        "stages": {"kind": "cycle", "anchors": (1306872,), "gap": 40, "offset": 18, "first": "1루프", "label": "{n}루프",
                   "labelStart": 2, "anchorStarts": False, "killChips": 3},
        "mass": {
            1290480: ("영원의 맹독 10중첩", "탐식 맞아주기로 중첩 지우기 · 구슬은 중첩 낮은 사람 · 5번째 창 대비 중첩 관리."),
            1310105: ("오염된 피 폭발 (샘 못 채움)", "탐식 뒤 샘(오염된 피)마다 힐러 나눠 8초 안에 치유로 채우기 · 뎀감으론 안 참."),
            1290956: ("심연 휘젓기", "쫄 창 시작에 긴 힐 쿨 → 휘젓기까지 유지 · 파도 피하기."),
            1294605: ("잠수 홍수", "잠수(핏빛 폭풍) 때 이동 쿨 · 홍수 경로 피하기."),
            1290516: ("탐식 맞아주기 실패", "탐식 1타마다 4명 이상 · 같은 사람이 두 번 맞지 않게 조 배정 · 탐식 직전 공대 뎀감."),
            1290809: ("휘감는 체액", "체액 원은 공대에서 멀리 · 뒤쪽 절반이 휘젓기와 겹치면 개인 생존기."),
            1290878: ("휘감는 체액", "체액 원은 공대에서 멀리 · 뒤쪽 절반이 휘젓기와 겹치면 개인 생존기."),
            1308386: ("자식 차단 실패 → 내장 파열", "이스라즈의 자식 내장 파열은 전부 차단 (구역별 배정 · 보호 성기사는 앞쪽 혼자 가능) · 놓치면 바로 백업."),
            1290338: ("산성 방울 (구슬)", "산성 방울은 중첩 낮은 한 명이 하나씩 · 안 먹으면 공대 전체 피해+중첩 · 4·5번째 세트가 가장 셈."),
            1289201: ("산성 방울 (구슬)", "산성 방울은 중첩 낮은 한 명이 하나씩 · 안 먹으면 공대 전체 피해+중첩 · 4·5번째 세트가 가장 셈."),
            1290662: ("탐식 맞아주기 실패", "탐식 1타마다 4명 이상 · 같은 사람이 두 번 맞지 않게 조 배정 · 탐식 직전 공대 뎀감."),
            1292806: ("심연 휘젓기", "쫄 창 시작에 긴 힐 쿨 → 휘젓기까지 유지 · 파도 피하기 (파도 맞으면 중첩+1)."),
            1289153: ("돌 파괴자", "돌 파괴자 맞아주기·넉백 위치 확인."),
        },
        "hard": ({"castSuccess": 1308385, "deaths": (1308386,), "min": 3, "label": "자식 차단 실패 → 내장 파열",
                  "advice": "이스라즈의 자식 내장 파열은 전부 차단 (구역별 배정 · 보호 성기사는 앞쪽 혼자 가능) · 놓치면 바로 백업."},),
        "kick": {"spell": 1308385, "label": "자식 차단"},
        "avoid": (1308386, 1294605, 1310105, 1290516, 1290662, 1292806),
        "windows": (("쫄 창", 1291404, 0, 40, "쫄 창: 출현 때 긴 힐 쿨 → 휘젓기까지 · 탐식 직전 공대 뎀감 · 샘은 치유로."),
                    ("잠수", 1306872, 0, 18, "잠수: 이동 쿨, 홍수 피하기.")),
        "abs_windows": (),
        "timeline": {1291404: "맹독의 출현", 1290809: "휘감는 체액", 1290956: "심연 휘젓기", 1290516: "걸신들린 탐식",
                     1289192: "부식의 폭우", 1288538: "돌 파괴자", 1306872: "핏빛 폭풍"},
        "anchors": (1291404,),
        "colors": {"맹독의 출현": "purple", "휘감는 체액": "yellow", "심연 휘젓기": "red", "걸신들린 탐식": "orange",
                   "부식의 폭우": "green", "돌 파괴자": "gray", "핏빛 폭풍": "blue", "영원의 맹독": "red", "오염된 피": "red"},
    },
    3455: {
        "name": "악성의 바쉬니크", "bossNpcs": ("-259181-",),
        "stages": {"kind": "cycle", "anchors": (1284663,), "gap": 30, "first": "오프너", "label": "{n}시음",
                   "anchorStarts": False, "killChips": None},
        "mass": {
            1295209: ("감염 해제 폭발 겹침", "폭발 전염병 해제는 정한 순서로 한 명씩 · 몰아서 해제는 공대 쿨 아래서만."),
            1286872: ("맹독이 중앙 도달 (악의 파열)", "타오르는 맹독을 중앙 도달 전에 처치 · 맹독 딜 배정."),
            1285979: ("맹독 처치 폭발 겹침", "맹독 처치 타이밍에 힐 쿨 · 첫 감염 파동과 겹침."),
            1282601: ("담즙 미흡수", "촉매성 담즙 줄기는 꼭 한 명 이상 맞기 (첫 파동 직전 특히)."),
            1295798: ("역병 파동 피격", "역병 거품이 끝날 때 십자 파동 피하기."),
            1298582: ("시음 폭발 (피)", "시음 2번째부터 공대 뎀감 · 4번째가 가장 큼 · 개인 생존기."),
            1298587: ("시음 폭발 (화염)", "시음 2번째부터 공대 뎀감 · 4번째가 가장 큼 · 개인 생존기."),
            1298583: ("시음 폭발 (암흑)", "시음 2번째부터 공대 뎀감 · 4번째가 가장 큼 · 개인 생존기."),
            1306820: ("종양 미처리", "역병 거품 파동으로 종양 다 없애기 · 두 번째 거품 세트가 마지막 기회."),
            1304459: ("종양(악성) 미처리", "역병 거품 파동으로 종양 다 없애기 · 두 번째 거품 세트가 마지막 기회."),
            1282616: ("촉매성 담즙", "담즙 줄기는 꼭 한 명 이상 맞기 · 맞을 사람은 체력 채우고 생존기."),
            1281925: ("역병 거품 주변 피해", "역병 거품 대상은 공대에서 떨어지기 · 끝날 때 십자 파동 피하기."),
            1284561: ("독성 증기 누적", "독성 증기는 시음마다 쌓이고 안 사라짐 · 막바지(5시음 뒤)에 남은 힐 쿨·개인 생존기 몰기."),
            1305901: ("타오르는 맹독 오라", "타오르는 맹독 빨리 처치 · 첫 감염 파동과 겹치지 않게."),
        },
        "avoid": (1286872, 1282601, 1295798),
        "windows": (("첫 감염 파동", 1284663, 3, 35, "시음 뒤 첫 감염 파동: 힐 쿨 (첫 파동·3번째 시음 뒤가 가장 셈)."),
                    ("시음", 1284663, -2, 3, "시음 2번째부터 공대 뎀감, 4번째에 가장 크게.")),
        "abs_windows": (),
        "timeline": {1284663: "시음", 1282516: "악성 촉매", 1280935: "독 맺힌 송곳니", 1281910: "역병 거품",
                     1295173: "폭발 전염병", 1295224: "흡수의 전염병", 1294994: "저승 전염병"},
        "anchors": (1284663,),
        "colors": {"시음": "blue", "악성 촉매": "purple", "독 맺힌 송곳니": "gray", "역병 거품": "green",
                   "폭발 전염병": "orange", "흡수의 전염병": "red", "저승 전염병": "purple", "부식성 폭발": "red"},
    },
    3470: {
        "name": "영혼살무사 네크잘리", "bossNpcs": ("-259927-",),
        "stages": {"kind": "phase", "phases": (("1단계", None), ("의식", 1295124), ("2단계", 1299673)), "killChips": 3},
        "mass": {
            1292248: ("영혼 이전 빔", "메아리가 나올 때 영혼 이전 빔에 서지 않기 (가장 흔한 사망)."),
            1289902: ("영혼 이전 빔", "메아리가 나올 때 영혼 이전 빔에 서지 않기 (가장 흔한 사망)."),
            1289855: ("장작더미", "장작더미는 절반이 맞아주기 · 두 번째 메아리 첫 장작더미에 힐 쿨 (이동 중 쓰는 것)."),
            1294742: ("장작더미", "장작더미는 절반이 맞아주기 · 두 번째 메아리 첫 장작더미에 힐 쿨 (이동 중 쓰는 것)."),
            1284103: ("귀신 들린 포화", "포화 탱은 공대 반대로 · 경로에 서지 않기 · 단계마다 두 번째 포화에 공대 뎀감."),
            1307939: ("시체 역병", "쫄 무리 한꺼번에 죽이지 않기 · 계획된 피해 전에 체력 채우기."),
            1288772: ("영혼똬리 의식", "의식 첫 몇 초가 가장 셈 · 두 번째 의식은 두 번째 포화 쿨 안에."),
            1295085: ("영혼 이전 빔", "메아리가 나올 때 영혼 이전 빔에 서지 않기 (가장 흔한 사망)."),
            1290390: ("영혼똬리 우물 (메아리)", "우물 메아리 처치조를 교대로 · 메아리가 오래 살수록 공대 피해가 커짐."),
            1294846: ("고뇌의 메아리 착지", "영혼똬리 점화 4초 동안 우물에서 떨어지는 고뇌의 메아리 착지 지점 피하기."),
            # 와우보초 신성 성기사 공략 (사용자 제공 2026-10-22): 죽은 아마니가 남긴 시체를 안 태우면 의식 때 강화돼 다시 깨어난다
            1297630: ("각성의 그릇 (시체 못 태움)", "죽은 잠 못 드는 아마니가 남긴 각성의 그릇 시체를 장작더미·기어가는 불꽃(화장)으로 태우기 · 안 태우면 각성의 의식 동안 강화돼 다시 깨어남."),
            1288554: ("잠복한 이교도 장판", "정수 분쇄 해제는 방 가장자리에서 · 해제 자리에 남는 잠복한 이교도 장판 쪽으로 움직이지 않기 (2단계도)."),
            1289875: ("화장 (기어가는 불꽃) 폭발", "기어가는 불꽃은 만료될 때 4미터 폭발 · 사람 말고 남은 시체 위에서 터뜨리기."),
        },
        "avoid": (1292248, 1289902, 1284103, 1288554, 1294846, 1295085),
        "windows": (("포화", 1284103, -2, 8, "단계마다 두 번째 포화에 공대 뎀감 (2단계 두 번째가 가장 셈)."),
                    ("메아리 등장", 1292248, -1, 6, "메아리 등장 때 영혼 이전 빔 피하기.")),
        "abs_windows": (),
        "timeline": {1288772: "영혼똬리 의식", 1293664: "영혼똬리 점화", 1284103: "귀신 들린 포화", 1287434: "정수 분쇄",
                     1295124: "각성의 의식", 1292248: "영혼 이전", 1289855: "굶주린 장작더미", 1299673: "기원"},
        "anchors": (1295124, 1299673),
        "colors": {"영혼똬리 의식": "yellow", "영혼똬리 점화": "yellow", "귀신 들린 포화": "red", "정수 분쇄": "orange",
                   "각성의 의식": "blue", "영혼 이전": "red", "굶주린 장작더미": "orange", "기원": "purple"},
    },
    3497: {
        "name": "길 잃은 탐험가", "bossNpcs": ("-261843-", "-261835-", "-261848-"),
        "stages": {"kind": "cycle", "anchors": (1297075,), "gap": 40, "first": "1강화", "label": "{n}강화",
                   "anchorStarts": True, "killChips": 3},
        "hard": ({"cast": 1292779, "deaths": (1292780,), "label": "물고기 늦게 던짐 → 최후의 승천",
                  "advice": "물고기는 떨어지고 바로 줍기 · 강화 64초 뒤 최후의 승천 전에 꼭 던지기 (물고기 담당 확인)."},),
        "mass": {
            1292780: ("최후의 승천 (물고기 늦음)", "물고기는 떨어지고 바로 줍기 · 최후의 승천 전에 꼭 던지기."),
            1294334: ("점멸 회오리", "점멸 대상은 공대에서 멀리 앞장 · 블링크마다 공대 뎀감 (풀 직후는 두 번째 블링크에 몰기)."),
            1296095: ("나마 후려치기", "후려치기 지점마다 뭉쳐 나눠 맞기 · 각 3연타에 공대 뎀감, 두 번째에 힐 쿨."),
            1296133: ("나마 후려치기", "후려치기 지점마다 뭉쳐 나눠 맞기 · 각 3연타에 공대 뎀감, 두 번째에 힐 쿨."),
            1296135: ("나마 후려치기", "후려치기 지점마다 뭉쳐 나눠 맞기 · 각 3연타에 공대 뎀감, 두 번째에 힐 쿨."),
            1295891: ("서리불꽃 해제 겹침", "반대 웅덩이로 해제 · 원 겹치지 않게 · 해제 콜에 공대 쿨."),
            1310028: ("상자 방치 폭발", "상자는 25초 안에 담당이 두 개씩 터뜨리기."),
            1310027: ("상자 방치 폭발 (유물 파열)", "상자는 25초 안에 담당이 두 개씩 터뜨리기 · 상자 담당 순서 확인."),
            1292764: ("사악한 눈", "사악한 눈 경로 피하기."),
        },
        "avoid": (1292764, 1310028, 1310027, 1295891),
        "windows": (("블링크", 1296021, -1, 5, "점멸 회오리마다 공대 뎀감 · 대상은 멀리 앞장."),
                    ("상자 웨이브", 1291933, 0, 20, "상자 웨이브(두 블링크 사이)에 힐 쿨.")),
        "abs_windows": (),
        "timeline": {1297075: "강화", 1296021: "점멸 회오리", 1291933: "쓰레기 투척", 1292490: "역겨운 물고기",
                     1296094: "강력한 후려치기", 1295891: "서리불꽃 연사", 1297625: "폭발적인 깜짝 선물", 1292779: "최후의 승천"},
        "anchors": (1297075,),
        "colors": {"강화": "blue", "점멸 회오리": "red", "쓰레기 투척": "yellow", "역겨운 물고기": "green",
                   "강력한 후려치기": "orange", "서리불꽃 연사": "purple", "폭발적인 깜짝 선물": "orange", "최후의 승천": "red"},
    },
    # V1.5 키식스 (키식스 해방 1보스): Viserio 쿨기 공략 + 인벤 신화 공략 + 와우보초 신성 성기사 공략 (사용자 제공 2026-10-22)
    # · 이 PC 영웅 로그 6풀(2026-10-16·17)에서 주문 ID 확인. 109초 루프: 쫄 웨이브(맹독 포효 -11초 = 0:05·1:54·3:43)
    # → 숨막히는 암흑(0:57·2:46, 리아드린 빛 안) → 25%에 2단계(여덟 번의 일월식, 마지막 일월식 전에 처치).
    3513: {
        "name": "키식스", "bossNpcs": ("-267861-",),
        "stages": {"kind": "seq", "gap": 30, "killChips": 6,
                   "seq": (("1웨이브", None, 1, 0), ("1암흑", 1302951, 1, 0), ("2웨이브", 1302884, 2, -11),
                           ("2암흑", 1302951, 2, 0), ("3웨이브", 1302884, 3, -11), ("2단계", 1301479, 1, -4))},
        "mass": {
            1303261: ("쫄·알 미처리 → 어둠의 광란", "숨막히는 암흑(기력 100) 전에 쫄과 알을 모두 처치 · 살아남은 쫄은 어둠의 광란으로 제어 면역+공대 피해."),
            1303165: ("숨막히는 암흑 (빛 밖)", "암흑이 오면 리아드린의 빛(광명의 보루) 안으로 · 밖에서는 질식으로 평정+매초 피해."),
            1303257: ("질식 (빛 밖)", "암흑 동안 리아드린의 빛 안에 서기 · 일월식 파편이 리아드린에 닿을수록 빛이 작아짐."),
            1304424: ("소멸 (빛의 은총 못 먹음)", "소멸 8초 시전 중 빛구슬(빛의 은총) 한 사람 하나씩 · 구슬 떨어질 원에 미리 여럿 들어가면 같이 먹음 · 못 먹었으면 면역기·강한 생존기."),
            1304047: ("압도적인 공포 비명", "형언할 수 없는 공포 대상은 6초 안에 공대 밖으로 · 밖에 나간 대상만 한 명씩 해제 · 암흑 속 공포 땐 공대 뎀감 (인벤: 이때 죽기 리혼·흑마 결의 금지)."),
            1304046: ("압도적인 공포 비명", "형언할 수 없는 공포 대상은 6초 안에 공대 밖으로 · 밖에 나간 대상만 한 명씩 해제 · 암흑 속 공포 땐 공대 뎀감 (인벤: 이때 죽기 리혼·흑마 결의 금지)."),
            1302319: ("소화액 중첩 (딱정벌레 몰아 터짐)", "폭탄 딱정벌레는 하나씩 맹독 거한 위로 끌어와 터뜨리기 · 소화액이 사라진 뒤 다음 · 나머지는 제어기로 묶기 · 줄 뒷부분에 개인 생존기."),
            1302336: ("공허술 (일몰 중첩)", "공허술사부터 처치 · 일몰 중첩이 높을 때 공허술 대상이면 개인 생존기."),
            1303685: ("뒤틀린 신체 부위 (촉수)", "촉수가 나오면 바닥 원을 보고 내려치기 전에 피하기."),
            1304950: ("공허흉터 밟음", "암흑 황폐 자리(공허흉터)는 방 한쪽 가장자리에 모으기 · 밟지 않기."),
            1308674: ("일월식 파편", "암흑 때 일월식 파편은 탱커 위주로 리아드린 앞에서 가로막기 · 맞을 땐 다른 사람과 3미터 떨어져 나눠 맞기."),
            1304946: ("암흑 황폐 (탱)", "암흑 황폐마다 도발 교대 · 대상 탱커는 공대에서 떨어져 맞기."),
            1304947: ("암흑 황폐 (탱)", "암흑 황폐마다 도발 교대 · 대상 탱커는 공대에서 떨어져 맞기."),
            1308877: ("무위조각 (무효화 중첩)", "2단계 무위 관문은 공대 밖에 서로 떨어뜨려 깔기 · 날아오는 무위조각 피하기 (신화는 무효화 3중첩 즉사)."),
            1320010: ("부정 만료", "부정 받은 탱커는 끝나기 전에 무위 관문에 닿아 지우기 (폭발적인 재고정 때 공대 생존기)."),
            1304040: ("정신독침 (알 미처리)", "맹독 거한·공허술사·딱정벌레가 남긴 알을 부화 전에 깨기."),
            1301668: ("공허무리", "웨이브 시작 공허무리+딱정벌레 줄이 가장 아픈 힐 구간 · 힐 쿨 배정."),
            1303406: ("심연의 손아귀 (흡수 못 지움)", "심연의 손아귀 대상은 9초 안에 치유로 흡수량 지우기 · 대상 딜러는 치유 물약·생석."),
            1308875: ("무효화 중첩 즉사", "무위조각 피하기 · 무효화 2중첩부터는 조각 경로에서 완전히 빠지기 (신화 3중첩 즉사)."),
        },
        # V1.6 인벤 신화 공략: 「1페가 끝나고 네임드가 움직이기 시작할 때 65%, 2페 이후 움직일 때 38%면 킬각 ·
        #   3페는 쫄 무시하고 25%까지 밀면 키식스가 자살」 (막페 15초 안에 딜러 5명 사망 가정 · 표시만, 원인 판정 안 씀)
        "hp_checks": (("2웨이브", 65.0, "1페 끝 보스 이동"), ("3웨이브", 38.0, "2페 끝 보스 이동")),
        "hp_final": ("2단계", 25.0),
        # V1.6 raw 에서 더 읽는 것 (WCL 다른 공대 신화 킬 4개 + 이 PC 영웅 로그로 주문 확인)
        #   무효화 1308875: 신화 3중첩 즉사 (다른 공대 킬은 2중첩까지가 흔하고 3~4중첩은 마지막 자살 몇 초에만)
        #   황폐 1304948: 암흑 황폐 뒤 탱 디버프 · 2중첩 = 도발 교대 놓침 (다른 공대 킬 1개에서도 나옴)
        #   형언할 수 없는 공포 1304045 → 압도적인 공포 1304046 대상이 리치의 혼(49039)·영원한 결의(104773):
        #   인벤 「이 공포 타이밍에 쓰면 바로 터져서 공대 전멸」 · 영웅 10/16 2트: 대상 아닌 사람이 쓴 결의는 아무 일 없음 → 대상 본인만 ⚠
        "raw_checks": {"stack": {1308875: ("무효화", 2, 3)}, "tankStack": {1304948: ("암흑 황폐", 2)},
                       "fear": (1304045, 1304046), "fearImmune": {49039: "리치의 혼", 104773: "영원한 결의"}},
        "hard": ({"cast": 1302951, "deaths": (1303261,), "min": 3, "atDeath": True, "label": "쫄·알 미처리 → 어둠의 광란",
                  "advice": "숨막히는 암흑(기력 100) 전에 쫄과 알을 모두 처치 · 웨이브 딜 배분(공허술사 먼저, 거한은 공허술사 위로) 확인."},
                 {"cast": 1304424, "deaths": (1304424,), "min": 3, "atDeath": True, "label": "빛의 은총 못 먹음 → 소멸",
                  "advice": "소멸 시전 중 빛구슬 한 사람 하나씩 (떨어질 원에 미리 들어가기) · 못 먹은 사람은 면역기·강한 생존기."}),
        "avoid": (1303685, 1304950, 1302336, 1304424, 1303165, 1303257, 1308877, 1304047, 1304046),
        "windows": (("웨이브 딱정벌레", 1302884, -11, 25, "웨이브 시작~딱정벌레 줄: 공허무리에 힐 쿨, 딱정벌레는 하나씩 · 줄 뒷부분에 개인 생존기 (2웨이브가 더 셈)."),
                    ("숨막히는 암흑", 1302951, 0, 50, "암흑 44초: 빛 안에 서기 · 소멸 빛구슬 · 공포는 밖으로 · 손아귀 흡수 지우기 · 비명(1:30·3:19)에 공대 뎀감.")),
        "abs_windows": (),
        "timeline": {1302884: "맹독 포효", 1302333: "공허술", 1304930: "암흑 황폐", 1304424: "소멸",
                     1302951: "숨막히는 암흑", 1301479: "여덟 번의 일월식", 1318467: "부정", 1304026: "부화",
                     1302705: "일몰", 1308873: "무위 관문", 1304045: "형언할 수 없는 공포", 1303406: "심연의 손아귀"},
        "anchors": (1302951, 1301479),
        "colors": {"맹독 포효": "orange", "공허술": "purple", "암흑 황폐": "orange", "소멸": "red", "숨막히는 암흑": "blue",
                   "여덟 번의 일월식": "red", "부정": "orange", "부화": "green", "일몰": "purple", "무위 관문": "yellow",
                   "형언할 수 없는 공포": "blue", "심연의 손아귀": "purple", "어둠의 광란": "red", "소화액": "green",
                   "공허흉터": "yellow", "일월식 파편": "yellow", "무위조각": "red"},
    },
}


# 우리 공대 신화 킬 (GOLD 기록 2026-09-30 · 10/12 · 10/18): 킬 시간과 30초마다 보스 HP 평균.
# 전멸 트라이 「딜·힐」 줄에 「같은 시각 우리 킬 HP」로 붙는다. 다른 공대 기준선은 WCL 자료 받은 뒤 따로.
OUR_KILLS = {
    3420: {"kills": (344, 349), "hp": ((30, 85.1), (60, 77.9), (90, 71.2), (120, 61.2), (150, 52.2), (180, 46.0), (210, 38.8),
                                      (240, 30.4), (270, 20.8), (300, 13.1), (330, 5.1))},
    3421: {"kills": (378, 376), "hp": ((30, 83.7), (60, 76.3), (90, 68.7), (120, 61.7), (150, 55.1), (180, 48.0), (210, 40.7),
                                      (240, 33.5), (270, 26.7), (300, 19.7), (330, 12.8), (360, 4.2))},
    3445: {"kills": (399,), "hp": ((30, 84.1), (60, 78.1), (90, 69.8), (120, 61.2), (150, 53.5), (180, 50.0), (210, 42.3),
                                  (240, 34.0), (270, 32.1), (300, 23.8), (330, 15.7), (360, 8.2), (390, 2.1))},
    3455: {"kills": (319, 318), "hp": ((30, 83.5), (60, 74.5), (90, 67.0), (120, 58.0), (150, 49.4), (180, 41.8), (210, 32.1),
                                      (240, 24.6), (270, 16.4), (300, 6.3))},
    3470: {"kills": (371, 347), "hp": ((30, 77.5), (60, 65.9), (90, 56.9), (120, 46.5), (150, 44.5), (180, 44.5), (210, 44.5),
                                      (240, 42.8), (270, 32.9), (300, 24.0), (330, 12.1), (360, 4.8))},
    3497: {"kills": (296, 286), "hp": ((30, 82.3), (60, 70.9), (90, 62.8), (120, 53.8), (150, 43.1), (180, 34.4), (210, 25.7),
                                      (240, 16.5), (270, 5.8))},
}


# 다른 공대 신화 킬 (WCL boss_refs 2026-10-22 · 사용자 지정 킬 로그): 킬 시간, 단계 시작(HP로 넘어가는 단계만 의미 있음).
OTHER_KILLS = {
    3420: {"kills": (302, 310, 326)},
    3421: {"kills": (333, 337, 341)},
    3445: {"kills": (300, 337)},
    3455: {"kills": (298, 384, 410)},
    3470: {"kills": (256, 321, 357), "stages": {"의식": (74, 105, 114), "2단계": (168, 217, 231)}},
    3497: {"kills": (262, 266, 270)},
    # V1.6 키식스 신화 킬 4개 (boss_refs): 웨이브·암흑은 시간 고정(1:55·2:41·3:44) · 2단계(일월식 -4초)만 공대마다 다름
    3513: {"kills": (295, 302, 306, 313), "stages": {"2단계": (250, 252, 254, 257)}},
}


def other_kill_txt(enc: int) -> str | None:
    ref = OTHER_KILLS.get(int(enc or 0))
    if not ref:
        return None
    ks = sorted(ref["kills"])
    return f"다른 공대 킬 {mmss(ks[0])}~{mmss(ks[-1])}" if len(ks) > 1 else f"다른 공대 킬 {mmss(ks[0])}"


def other_stage_txt(enc: int, stage: str, start) -> str | None:
    ref = (OTHER_KILLS.get(int(enc or 0)) or {}).get("stages") or {}
    xs = ref.get(stage)
    if not xs or start is None:
        return None
    return f"{stage} 진입 {mmss(start)} (다른 공대 {mmss(min(xs))}~{mmss(max(xs))})"


def our_kill_hp(enc: int, sec: float):
    ref = OUR_KILLS.get(int(enc or 0))
    if not ref or sec is None:
        return None
    pts = [(0, 100.0)] + list(ref["hp"])
    if sec >= pts[-1][0]:
        return pts[-1][1]
    for (t0, h0), (t1, h1) in zip(pts, pts[1:]):
        if t0 <= sec <= t1:
            return round(h0 + (h1 - h0) * (sec - t0) / (t1 - t0), 1)
    return None


def mechanic_colors() -> dict:
    """시간축·결론 줄 색 표에 더할 이름 → 색 그룹."""
    out = {}
    for spec in SPECS.values():
        out.update(spec.get("colors") or {})
        for label, _advice in (spec.get("mass") or {}).values():
            out.setdefault(label.split(" (")[0].split(" →")[0], "red")
    return out


def anchor_spells() -> set:
    return {sid for spec in SPECS.values() for sid in (spec.get("anchors") or ())}


# ---------------------------------------------------------------- 공통 도구

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


def cast_times(facts: dict, spell_ids, gap: float = 3.0, event: str | None = None) -> list[float]:
    """raid_facts.enemyCastSummary 에서 그 주문 시전 시각 (시작 우선, 가까운 건 하나로). event="SUCCESS" 면 완료만."""
    rows = [r for r in ((facts or {}).get("enemyCastSummary") or []) if int(r.get("spellID") or 0) in set(spell_ids)]
    if event:
        use = [r for r in rows if event in str(r.get("event") or "")]
    else:
        starts = [r for r in rows if "START" in str(r.get("event") or "")]
        use = starts or rows
    times = sorted(float(t) for r in use for t in (r.get("times") or []) if t is not None)
    out: list[float] = []
    for t in times:
        if not out or t - out[-1] > gap:
            out.append(round(t, 3))
    return out


def deaths_of(facts: dict) -> list[dict]:
    out = []
    for d in (facts or {}).get("deaths") or []:
        lh = d.get("lastHit") or {}
        sid = lh.get("canonicalSpellID") or lh.get("spellID")
        try:
            sid = int(sid) if sid is not None else None
        except (TypeError, ValueError):
            sid = None
        out.append({"sec": float(d.get("sec") or 0), "name": short(d.get("name")), "guid": d.get("guid"),
                    "spellID": sid, "rawSpellID": lh.get("spellID"), "spell": str(lh.get("canonicalMechanic") or lh.get("spellName") or "근접/환경")})
    return sorted(out, key=lambda x: x["sec"])


def scan_boss_hp(raw_path, start_ts: str, npcs) -> list:
    """raw.log 에서 보스(들) HP. 여러 보스면 마지막으로 본 각자 HP 의 평균."""
    p = Path(raw_path)
    if not p.is_file() or not npcs:
        return []
    from datetime import datetime
    try:
        d, t = start_ts.split()[:2]
        mo, dd, yy = d.split("/")
        hh, mi, ss = t.split(":")
        s, frac = (ss.split(".") + ["0"])[:2]
        start = datetime(int(yy), int(mo), int(dd), int(hh), int(mi), int(s), int(frac.ljust(6, "0")[:6]))
    except Exception:
        return []
    toks = [n.encode() for n in npcs]
    last: dict[str, float] = {}
    out = []
    last_sec = -1.0
    opener = gzip.open if str(p).endswith(".gz") else open
    try:
        with opener(p, "rb") as fh:
            for raw in fh:
                if not any(t in raw for t in toks):
                    continue
                if b"_DAMAGE" not in raw:
                    continue
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                head, _, payload = line.partition("  ")
                try:
                    f = next(csv.reader([payload]))
                except Exception:
                    continue
                ev = f[0]
                if ev.startswith("SWING_DAMAGE"):
                    base = 9
                elif ev.startswith(("SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE")):
                    base = 12
                else:
                    continue
                if len(f) <= base + 3:
                    continue
                who = next((n for n in npcs if n in f[base]), None)
                if who is None:
                    continue
                try:
                    cur, mx = float(f[base + 2]), float(f[base + 3])
                except ValueError:
                    continue
                if mx <= 0:
                    continue
                last[who] = cur * 100.0 / mx
                try:
                    dd_, tt = head.split()[:2]
                    mo, da, yy = dd_.split("/")
                    hh, mi, ss = tt.split(":")
                    s2, fr = (ss.split(".") + ["0"])[:2]
                    sec = (datetime(int(yy), int(mo), int(da), int(hh), int(mi), int(s2), int(fr.ljust(6, "0")[:6])) - start).total_seconds()
                except Exception:
                    continue
                if sec - last_sec >= 1.0:
                    out.append((round(sec, 3), round(sum(last.values()) / len(last), 2)))
                    last_sec = sec
    except Exception:
        return out
    return out


def _ts_of(text: str):
    from datetime import datetime
    try:
        d, t = str(text).split()[:2]
        mo, dd, yy = d.split("/")
        hh, mi, ss = t.split(":")
        s, frac = (ss.split(".") + ["0"])[:2]
        return datetime(int(yy), int(mo), int(dd), int(hh), int(mi), int(s), int(frac.ljust(6, "0")[:6]))
    except Exception:
        return None


def scan_raw_checks(raw_path, start_ts: str, checks: dict | None) -> dict:
    """V1.6 · 보스별 추가 raw 확인 (중첩 디버프 · 탱 중첩 · 공포 구간 · 공포 중 면역기). 판정 재료만 모은다."""
    out = {"available": False, "stack": {}, "tankStack": [], "fear": [], "fearImmune": []}
    p = Path(raw_path)
    checks = checks or {}
    start = _ts_of(start_ts)
    if not checks or not p.is_file() or start is None:
        return out
    stack_ids = {int(k): v for k, v in (checks.get("stack") or {}).items()}
    tank_ids = {int(k): v for k, v in (checks.get("tankStack") or {}).items()}
    fear_cfg = checks.get("fear") or ()
    fear_ids = {int(x) for x in (fear_cfg if isinstance(fear_cfg, (tuple, list, set)) else (fear_cfg,)) if x}
    immune = {int(k): v for k, v in (checks.get("fearImmune") or {}).items()}
    want = set(stack_ids) | set(tank_ids) | set(immune) | fear_ids
    toks = [f",{i},".encode() for i in want]
    fear_on: dict[str, set] = {}
    fear_spans: list[list[float]] = []
    imm_raw = []
    opener = gzip.open if str(p).endswith(".gz") else open
    try:
        with opener(p, "rb") as fh:
            for raw in fh:
                if b"SPELL_" not in raw or not any(t in raw for t in toks):
                    continue
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                head, _, payload = line.partition("  ")
                try:
                    f = next(csv.reader([payload]))
                    sid = int(f[9])
                except Exception:
                    continue
                ev = f[0]
                t = _ts_of(head)
                if t is None:
                    continue
                sec = round((t - start).total_seconds(), 3)
                who = short(f[6])
                if sid in stack_ids and ev in ("SPELL_AURA_APPLIED", "SPELL_AURA_APPLIED_DOSE"):
                    n = 1 if ev == "SPELL_AURA_APPLIED" else int(float(f[13] or 1)) if len(f) > 13 else 1
                    rec = out["stack"].setdefault(sid, {})
                    cur = rec.get(who)
                    if cur is None or n > cur["max"]:
                        rec[who] = {"max": n, "sec": sec}
                elif sid in tank_ids and ev == "SPELL_AURA_APPLIED_DOSE":
                    n = int(float(f[13] or 2)) if len(f) > 13 else 2
                    if n >= tank_ids[sid][1]:
                        out["tankStack"].append({"sec": sec, "name": who, "stack": n, "label": tank_ids[sid][0]})
                elif sid in fear_ids and ev == "SPELL_AURA_APPLIED":
                    fear_on.setdefault(who, set()).add(sid)
                    if fear_spans and sec - fear_spans[-1][1] <= 10:
                        fear_spans[-1][1] = max(fear_spans[-1][1], sec)
                    else:
                        fear_spans.append([sec, sec])
                elif sid in fear_ids and ev == "SPELL_AURA_REMOVED":
                    if fear_spans and sec - fear_spans[-1][1] <= 15:
                        fear_spans[-1][1] = max(fear_spans[-1][1], sec)
                    fear_on.get(who, set()).discard(sid)
                elif sid in immune and ev in ("SPELL_CAST_SUCCESS", "SPELL_AURA_APPLIED"):
                    caster = short(f[2]) if ev == "SPELL_CAST_SUCCESS" else who
                    imm_raw.append({"sec": sec, "name": caster, "spell": immune[sid], "target": bool(fear_on.get(caster))})
        out["available"] = True
    except Exception:
        return out
    out["fear"] = [(round(a, 1), round(b, 1)) for a, b in fear_spans]
    seen = {}
    for e in sorted(imm_raw, key=lambda x: x["sec"]):
        if any(a - 1.0 <= e["sec"] <= b + 1.0 for a, b in fear_spans):
            k = (e["name"], e["spell"])
            if k not in seen or e["sec"] - seen[k] > 3:
                out["fearImmune"].append(dict(e))
            elif e.get("target"):
                out["fearImmune"][-1]["target"] = True
            seen[k] = e["sec"]
    return out


def hp_at(hp: list | None, sec) -> float | None:
    if not hp or sec is None:
        return None
    best = None
    for t, h in hp:
        if t <= float(sec) + 1.0:
            best = h
        else:
            break
    return best


# ---------------------------------------------------------------- 단계 · 사망 구간

def stages(spec: dict, facts: dict, duration: float, kill: bool) -> list[dict]:
    st = spec.get("stages") or {}
    d = float(duration or 0)
    bounds: list[tuple[str, float]] = []
    if st.get("kind") == "seq":
        for label, sid, nth, off in st.get("seq") or ():
            if sid is None:
                bounds.append((label, 0.0))
                continue
            ts = cast_times(facts, (sid,), gap=float(st.get("gap") or 20))
            t = ts[int(nth) - 1] + float(off) if len(ts) >= int(nth) else None
            bounds.append((label, max(0.0, t) if t is not None else None))
        # 뒤 단계가 앞 단계보다 먼저 잡히면 (예: 2단계가 3웨이브 전에) 빈 단계는 건너뛴다
        last = -1.0
        fixed = []
        for label, t in bounds:
            if t is not None and t < last:
                t = None
            if t is not None:
                last = t
            fixed.append((label, t))
        bounds = fixed
    elif st.get("kind") == "phase":
        for label, sid in st.get("phases") or ():
            if sid is None:
                bounds.append((label, 0.0))
            else:
                ts = cast_times(facts, (sid,))
                bounds.append((label, ts[0] if ts else None))
    else:
        anchors = cast_times(facts, st.get("anchors") or (), gap=float(st.get("gap") or 20))
        off = float(st.get("offset") or 0)
        if st.get("anchorStarts"):
            starts = [a for a in anchors] or [0.0]
            if starts[0] > 15:
                starts = [0.0] + starts
            starts[0] = 0.0
        else:
            starts = [0.0] + [a + off for a in anchors if a + off < d + 1 or kill]
        n0 = int(st.get("labelStart") or 1)

        def name_at(i):
            if i == 0 and st.get("first"):
                return st["first"]
            n = i + 1 if st.get("anchorStarts") else (i + n0 - 1 if st.get("labelStart") else i)
            return str(st.get("label") or "{n}").format(n=n)
        for i, s in enumerate(starts):
            bounds.append((name_at(i), s))
        want = st.get("killChips")
        if want:
            for j in range(len(bounds), int(want)):
                bounds.append((name_at(j), None))
        elif not kill:
            bounds.append((name_at(len(bounds)), None))
    out = []
    for i, (label, start) in enumerate(bounds):
        end = next((b[1] for b in bounds[i + 1:] if b[1] is not None), None)
        if start is None or (not kill and d < start):
            status = "todo"
        elif kill or (end is not None and d >= end):
            status = "done"
        else:
            status = "fail"
        out.append({"name": label, "start": start, "end": (end if status == "done" else (d if status == "fail" else None)),
                    "status": status})
    if kill:
        out = [x for x in out if x["start"] is not None]
        if out:
            out[-1]["end"] = d
    return out


def windows_of(spec: dict, facts: dict, deaths: list[dict], cutoff: float | None) -> list[dict]:
    res = []
    for name, sid, a, b, advice in spec.get("windows") or ():
        spans = [(t + a, t + b) for t in cast_times(facts, (sid,), gap=10)]
        hit = [x for x in deaths if any(s0 <= x["sec"] <= s1 for s0, s1 in spans) and (cutoff is None or x["sec"] < cutoff)]
        res.append({"name": name, "count": len(hit), "advice": advice, "deaths": hit})
    for name, s0, s1, advice in spec.get("abs_windows") or ():
        hit = [x for x in deaths if s0 <= x["sec"] <= s1 and (cutoff is None or x["sec"] < cutoff)]
        res.append({"name": name, "count": len(hit), "advice": advice, "deaths": hit})
    return res


def collapse_at(deaths: list[dict], roster_n: int) -> float | None:
    """3초 안에 3명 이상 · 아니면 로스터 30% 째 사망 시각.

    V1.5: 초반 3명 동시 사망 뒤 공대가 다시 버틴 경우(그 뒤 사망 사이가 30초 넘게 빔)는 붕괴가 아니다.
    그 뒤로 사망이 30초 넘게 끊기지 않고 전멸까지 이어지는 첫 무리를 붕괴로 본다 (없으면 예전처럼 첫 무리)."""
    clusters = []
    for i in range(len(deaths)):
        j = i
        while j + 1 < len(deaths) and deaths[j + 1]["sec"] - deaths[i]["sec"] <= 3.0:
            j += 1
        if j - i + 1 >= 3:
            clusters.append(i)
    for i in clusters:
        tail = [d["sec"] for d in deaths[i:]]
        if all(b - a <= 30.0 for a, b in zip(tail, tail[1:])):
            return deaths[i]["sec"]
    if clusters:
        return deaths[clusters[0]]["sec"]
    k = max(5, int(math.ceil(roster_n * 0.3)))
    return deaths[k - 1]["sec"] if len(deaths) >= k else None


def _label(spec: dict, d: dict) -> tuple[str, str | None]:
    m = (spec.get("mass") or {}).get(d.get("spellID")) or (spec.get("mass") or {}).get(d.get("rawSpellID"))
    if m:
        return m[0], m[1]
    return d.get("spell") or "?", None


# ---------------------------------------------------------------- 결론 다섯 줄

def build(meta: dict, facts: dict, hp: list | None = None, rawc: dict | None = None) -> dict:
    enc = int(meta.get("encounterID") or (facts.get("encounter") or {}).get("encounterID") or 0)
    spec = SPECS.get(enc)
    # 우리 킬 기준선은 신화 킬로 만든 것이라 신화 트라이에만 붙인다 (영웅·일반은 비교 안 함).
    mythic = int(meta.get("difficultyID") or (facts.get("encounter") or {}).get("difficultyID") or 0) == 16
    duration = float(meta.get("durationSec") or 0)
    result = str(meta.get("result") or "").lower()
    kill = result == "kill"
    if spec is None:
        return {"version": VERSION, "supported": False, "lines": {}}
    if meta.get("analysisExcluded") or meta.get("resetPull") or duration < 1.0:
        return {"version": VERSION, "excluded": True, "stages": [],
                "lines": {"어디까지": f"가짜/리셋 풀 · {duration:.1f}초", "첫 실수": "-", "전멸 원인": "판정 제외",
                          "딜·힐": "-", "다음 풀": "-"}}
    roster = facts.get("roster") or []
    healers = {short(r.get("name")) for r in roster if int(r.get("specID") or 0) in HEALER_SPECS}
    deaths = deaths_of(facts)
    chips = stages(spec, facts, duration, kill)
    cur = next((c for c in chips if c["status"] == "fail"), None) or (chips[-1] if chips else None)
    end_hp = hp[-1][1] if hp else None
    hp_txt = f" · 보스 HP {end_hp:.1f}%" if end_hp is not None and not kill else ""
    where = (f"처치 {mmss(duration)}" if kill else
             f"{cur['name'] if cur else '?'} · {mmss(duration)} 전멸{hp_txt}")

    collapse = None if kill else collapse_at(deaths, len(roster) or 20)
    before = [d for d in deaths if collapse is None or d["sec"] < collapse - 0.01]
    avoid = set(spec.get("avoid") or ())

    # 첫 실수
    first = next((d for d in before if d.get("spellID") in avoid or d.get("rawSpellID") in avoid), None)
    def died(lab):
        return lab if lab.endswith("사망") else f"{lab} 사망"
    if kill:
        first_txt = (f"첫 사망 {mmss(deaths[0]['sec'])} {died(_label(spec, deaths[0])[0])} · {deaths[0]['name']}"
                     if deaths else "사망 없음")
    elif first is not None:
        lab, _ = _label(spec, first)
        first_txt = f"{mmss(first['sec'])} {died(lab)} · {first['name']} [추정]"
    elif before:
        lab, _ = _label(spec, before[0])
        first_txt = f"{mmss(before[0]['sec'])} {died(lab)} · {before[0]['name']} [추정]"
    else:
        first_txt = "붕괴 전 사망 없음"

    # 전멸 원인
    cause, conf, advice, group = None, "미확정", None, None
    casts = {int(r.get("spellID") or 0) for r in (facts.get("enemyCastSummary") or [])}
    if not kill:
        bz = cast_times(facts, BERSERK)
        if bz:
            cause, conf, group = f"광폭화 {mmss(bz[0])} · 시간 안에 못 잡음 (딜 부족)", "확정", "광폭(딜 부족)"
            advice = "딜 부족: 쫄·전환 딜 배분 확인, 블러드·물약 타이밍, 붕괴 전 딜러 사망 줄이기."
        for h in spec.get("hard") or ():
            hit = [d for d in deaths if d.get("spellID") in h["deaths"] or d.get("rawSpellID") in h["deaths"]]
            if cause is not None or len(hit) < int(h.get("min") or 1):
                continue
            if h.get("cast") and h["cast"] in casts:
                t = cast_times(facts, (h["cast"],))
                at = hit[0]["sec"] if h.get("atDeath") else (t[0] if t else None)
                cause = f"{h['label']} {len(hit)}명 ({mmss(at)})" if h.get("atDeath") else f"{h['label']} ({mmss(at) if at is not None else '?'})"
                conf, group, advice = "확정", h["label"], h["advice"]
            elif h.get("castSuccess"):
                done = cast_times(facts, (h["castSuccess"],), gap=0.3, event="SUCCESS")
                n_done = sum(int(r.get("count") or len(r.get("times") or [])) for r in (facts.get("enemyCastSummary") or [])
                             if int(r.get("spellID") or 0) == h["castSuccess"] and "SUCCESS" in str(r.get("event")))
                if done:
                    cause = f"{h['label']} {len(hit)}명 · 끊지 못한 시전 {max(n_done, len(done))}번 (첫 {mmss(done[0])})"
                    conf, group, advice = "확정", h["label"], h["advice"]
        if cause is None and collapse is not None:
            burst = [d for d in deaths if collapse - 10 <= d["sec"] <= collapse + 6]
            cnt = Counter(_label(spec, d)[0] for d in burst)
            if cnt:
                lab, n = cnt.most_common(1)[0]
                known = any(_label(spec, d)[1] for d in burst if _label(spec, d)[0] == lab)
                adv = next((_label(spec, d)[1] for d in burst if _label(spec, d)[0] == lab and _label(spec, d)[1]), None)
                cause = f"{lab} {n}명 ({mmss(collapse)} 붕괴)"
                conf = "추정" if known and n >= 2 else "미확정"
                group, advice = lab, adv
        if cause is None and len(deaths) < max(3, int((len(roster) or 20) * 0.3)):
            cause, conf, group = f"사망 {len(deaths)}명뿐 · 공대가 직접 끊은 리셋으로 보임", "추정", "리셋"
            advice = "리셋한 이유(첫 사망·딜 진도)를 메모에 남겨 두면 다음 판정에 씁니다."
        if cause is None:
            cause, group = "붕괴 구간을 찾지 못함 (사망이 흩어짐)", "원인 미확정"
    else:
        cause, conf, group = "처치", "확정", "처치"

    # V1.6 공포 중 리혼·결의 → 바로 뒤 붕괴 (인벤 신화 공략) [추정]
    rawc = rawc or {}
    imm = rawc.get("fearImmune") or []
    if not kill and conf != "확정" and collapse is not None:
        hit_imm = next((e for e in imm if e.get("target") and e["sec"] - 1.0 <= collapse <= e["sec"] + 10.0), None)
        if hit_imm:
            n_c = len([d for d in deaths if collapse - 1 <= d["sec"] <= collapse + 6])
            cause = f"공포 대상이 {hit_imm['spell']} ({hit_imm['name']} {mmss(hit_imm['sec'])}) → {mmss(collapse)} 붕괴 {n_c}명"
            conf, group = "추정", "공포 중 면역기"
            advice = "형언할 수 없는 공포 동안 리치의 혼·영원한 결의 금지 (인벤: 바로 터져서 공대 전멸) · 공대에 미리 공지."
    # 사망 구간
    win = windows_of(spec, facts, deaths, collapse)
    if not kill and conf != "확정":
        hot = max(win, key=lambda w: w["count"], default=None)
        if hot and hot["count"] >= 3 and (group in (None, "원인 미확정") or conf == "미확정"):
            cause = f"{hot['name']} 구간 사망 {hot['count']}명 → {cause}"
            conf, group, advice = "추정", hot["name"], hot["advice"]

    # 딜·힐
    dps = []
    if end_hp is not None and not kill:
        ref_hp = our_kill_hp(enc, duration) if mythic else None
        if ref_hp is not None:
            diff = end_hp - ref_hp
            pace = "느림" if diff > 3 else ("비슷" if diff >= -3 else "빠름")
            dps.append(f"보스 HP {end_hp:.1f}% · 같은 시각 우리 킬 {ref_hp:.0f}% ({diff:+.0f}%p {pace})")
        else:
            dps.append(f"보스 HP {end_hp:.1f}%")
    if not kill and mythic:
        for c in chips:
            t = other_stage_txt(enc, c["name"], c.get("start")) if c.get("status") in ("done", "fail") and c.get("start") else None
            if t:
                dps.append(t)
    hp_marks = []
    if mythic and hp:
        for name_, line_, why in spec.get("hp_checks") or ():
            c = next((x for x in chips if x["name"] == name_ and x.get("start") is not None and x.get("status") in ("done", "fail")), None)
            h_ = hp_at(hp, c["start"]) if c else None
            if h_ is not None:
                hp_marks.append(f"{name_} {mmss(c['start'])} HP {h_:.0f}% " + ("✅" if h_ <= line_ else "⚠") + f" (공략 {line_:.0f}% · {why})")
        fin = spec.get("hp_final")
        if fin:
            c = next((x for x in chips if x["name"] == fin[0] and x.get("start") is not None and x.get("status") in ("done", "fail")), None)
            if c is not None:
                h0, h1 = hp_at(hp, c["start"]), (hp[-1][1] if not kill else None)
                hp_marks.append(f"{fin[0]} 진입 HP {h0:.0f}%" if h0 is not None else f"{fin[0]} 진입")
                if h1 is not None:
                    hp_marks[-1] += f" → 전멸 {h1:.0f}% (공략: {fin[1]:.0f}%까지 밀면 자살)"
    if hp_marks and not kill:
        dps.extend(hp_marks)
    pre_heal = sorted({d["name"] for d in before if d["name"] in healers})
    dps.append(f"붕괴 전 사망 {len(before)}명" + (f" (힐러 {', '.join(pre_heal)})" if pre_heal else ""))
    if kill:
        ks = ((OUR_KILLS.get(enc) or {}).get("kills") or ()) if mythic else ()
        best = min(ks) if ks else None
        other = other_kill_txt(enc) if mythic else None
        refs_ = [x for x in ((f"우리 최고 {mmss(best)}" if best else None), other) if x]
        heal_dead = sorted({d['name'] for d in deaths if d['name'] in healers})
        dps = [f"처치 {mmss(duration)}" + (f" ({' · '.join(refs_)})" if refs_ else "") + f" · 사망 {len(deaths)}명"
               + (f" (힐러 {', '.join(heal_dead)})" if heal_dead else "")]

    # 다음 풀
    if kill:
        top = Counter(_label(spec, d)[0] for d in deaths).most_common(1)
        adv = next((_label(spec, d)[1] for d in deaths if top and _label(spec, d)[0] == top[0][0] and _label(spec, d)[1]), None)
        advice = (f"킬에서도 {top[0][0]} {top[0][1]}명: " + adv) if top and adv else None
    nxt = advice or ("붕괴 직전 10초 사망 순서를 붕괴 체인에서 확인하세요." if not kill else "사망 없이 더 빠른 킬: 같은 단계 시각을 다른 공대와 비교 (WCL 자료 받은 뒤).")
    if first is not None and not kill:
        fl, fa = _label(spec, first)
        if fa and fa != advice and fl not in str(cause):
            nxt += f"; 첫 실수({fl})도 같이"

    extra = []
    k = spec.get("kick")
    if k:
        rows_ = [r for r in (facts.get("enemyCastSummary") or []) if int(r.get("spellID") or 0) == k["spell"]]
        starts_ = sorted(float(t) for r in rows_ if "START" in str(r.get("event")) for t in (r.get("times") or []))
        done_ = sorted(float(t) for r in rows_ if "SUCCESS" in str(r.get("event")) for t in (r.get("times") or []))
        kicked = sum(1 for i in (facts.get("interrupts") or []) if int(i.get("interruptedSpellID") or 0) == k["spell"])
        n_start = sum(int(r.get("count") or len(r.get("times") or [])) for r in rows_ if "START" in str(r.get("event")))
        n_done = sum(int(r.get("count") or len(r.get("times") or [])) for r in rows_ if "SUCCESS" in str(r.get("event")))
        if n_start:  # times 는 30개까지만 저장되므로 횟수는 count 로
            extra.append(f"{k['label']}  시전 {n_start} · 차단 {kicked} · 놓침 {n_done}" + (f" (첫 놓침 {mmss(done_[0])})" if done_ else ""))
            if n_done and not kill:
                dps.append(f"{k['label']} 놓침 {n_done}번")
    if any(w["count"] for w in win):
        extra.append("사망 구간  " + " · ".join(f"{w['name']} {w['count']}명" for w in win if w["count"]))
    if hp_marks and kill:
        extra.append("보스 HP  " + " · ".join(hp_marks))
    n_before_notes = len(extra)
    rc = spec.get("raw_checks") or {}
    end_cut = duration - 20 if kill else (collapse if collapse is not None else duration)
    for sid, (lab, warn, lethal) in ((rc.get("stack") or {}).items()):
        rec = (rawc.get("stack") or {}).get(int(sid)) or {}
        hi = {n: v for n, v in rec.items() if v["max"] >= warn and v["sec"] <= end_cut}
        if hi:
            top = sorted(hi.items(), key=lambda kv: (-kv[1]["max"], kv[1]["sec"]))
            n_l = sum(1 for _n, v in hi.items() if v["max"] >= lethal)
            extra.append(f"{lab}  {warn}중첩 이상 {len(hi)}명" + (f" · {lethal}중첩 {n_l}명" + (" (신화 즉사)" if mythic else "") if n_l else "")
                         + " · " + ", ".join(f"{n} {v['max']}중 {mmss(v['sec'])}" for n, v in top[:4]))
    for e in (rawc.get("tankStack") or [])[:3]:
        extra.append(f"탱 교대  {e['label']} {e['stack']}중첩 {e['name']} {mmss(e['sec'])} → 도발 교대 놓침 [추정]")
    for e in imm[:3]:
        extra.append(f"공포 중 면역기  {mmss(e['sec'])} {e['name']} {e['spell']} "
                     + ("⚠ 공포 대상 본인 (인벤: 공포 타이밍 리혼·결의 금지)" if e.get("target") else "· 공포 대상 아님 (참고)"))
    return {
        "version": VERSION, "encounterID": enc, "boss": spec["name"], "kill": kill, "durationSec": duration,
        "stages": chips, "collapseSec": collapse, "endHpPct": end_hp,
        "cause": {"text": cause, "confidence": conf, "group": group},
        "lines": {"어디까지": where, "첫 실수": first_txt, "전멸 원인": f"{cause} [{conf}]" if not kill else "처치",
                  "딜·힐": " · ".join(dps), "다음 풀": nxt},
        "extraLines": extra, "deathWindows": {w["name"]: w["count"] for w in win}, "hpChecks": hp_marks,
        "notes": extra[n_before_notes:],   # V1.6 화면 「참고」 줄: 무효화 중첩 · 탱 교대 · 공포 중 면역기
    }


def build_from_dir(try_dir, use_raw: bool = True) -> dict:
    d = Path(try_dir)
    meta = read_json(d / "meta.json")
    facts = read_json(d / str(meta.get("raidFactsJson") or "raid_facts.json")) or read_json(d / "raid_facts.json")
    enc = int(meta.get("encounterID") or 0)
    spec = SPECS.get(enc) or {}
    raw_path = d / str(meta.get("rawFile") or "raw.log.gz")
    hp = scan_boss_hp(raw_path, str(meta.get("startTimestamp") or ""), spec.get("bossNpcs") or ()) if use_raw and spec else []
    rawc = scan_raw_checks(raw_path, str(meta.get("startTimestamp") or ""), spec.get("raw_checks")) if use_raw and spec.get("raw_checks") else None
    return build(meta, facts, hp, rawc)


# ---------------------------------------------------------------- 시간축 보스 시전 (화면용)

def supplemental_events(facts: dict) -> list[dict]:
    """raid_facts 적 시전 요약 → 시간축 스킬별 줄 이벤트 (판정 아님, 관측만)."""
    enc = int(((facts or {}).get("encounter") or {}).get("encounterID") or 0)
    spec = SPECS.get(enc)
    if not spec:
        return []
    out = []
    timeline = dict(spec.get("timeline") or {})
    for sid in BERSERK:
        timeline.setdefault(sid, "광폭화")
    for sid, title in timeline.items():
        for t in cast_times(facts, (sid,), gap=2.0):
            out.append({"kind": f"bg_{sid}", "sec": t, "title": title, "spellIDs": [sid], "status": "observed",
                        "evidenceLevel": "observed", "presentationSupplemental": True, "sourceTitle": title})
    return out
