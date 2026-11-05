# NiniAnalyzer Public 1.0.0-rc5.5

## 바뀐 것
- **아이콘 · 로고를 Nini Brand Kit R3로 교체**
  - 실행 파일 · 작업 표시줄 · 설치 파일 아이콘: 남색 원 + 파란 한 줄 N (예전 흰 네모 없음)
  - 16~48px(작업 표시줄 등)은 모양은 그대로 두고 선만 조금 굵게
  - 왼쪽 위 로고: 둥근 N + NiniAnalyzer, 글자 가운데를 아이콘 가운데 높이에 맞춤
- 분석 기능 · 판정은 rc5.4와 같습니다 (rc5.4의 창 닫을 때 오류 수정 포함).

## 확인
- `qa/test_public_rc5_5_brand.py`: 아이콘 색 · 투명 바탕 · ICO 크기 · 로고 가운데 정렬
- `verify_release.py`: RC5.5 GATE
