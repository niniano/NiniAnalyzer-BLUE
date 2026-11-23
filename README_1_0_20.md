# NiniAnalyzer_Public 1.0.20

구글 캘린더 로그인 정보를 암호화해서 저장하는 판입니다.

## 받기
- **설치 파일 (추천)**: `NiniAnalyzer_Public_1.0.20_Setup_Windows_x64.exe`
- **설치 없이 쓰기**: `NiniAnalyzer_Public_1.0.20_Windows_x64_SAFE_FOLDER.zip` (압축 풀고 `NiniAnalyzer_Public.exe` 실행 · `_internal` 폴더는 그대로 두기)
- 파일이 맞는지 확인하려면 같은 이름의 `.sha256.txt`

## 업데이트
- 새 설치 파일로 그냥 덮어 설치하면 됩니다. 가져온 공대 기록과 설정은 그대로입니다.

## 바뀐 것
- **구글 캘린더 로그인 암호화**: 구글 연결 정보(토큰)를 Windows DPAPI 로 암호화한 파일(google_token.xml)로 저장합니다.
  · 이 PC · 이 Windows 계정에서만 풀리고, 파일을 다른 PC로 복사해도 쓸 수 없습니다.
  · 이미 연결한 분은 처음 동기화할 때 예전 파일(google_token.json)이 저절로 암호화 파일로 바뀌고, 확인되면 예전 파일은 지워집니다.
  · 다시 연결할 필요는 없습니다.

## 개인정보
- 로그는 이 PC 안에서만 읽고 어디로도 보내지 않습니다.
- 「WoW 달력 → 구글」을 직접 켠 경우에만 달력 초대(제목 · 시각 · 내 응답)를 내 구글 캘린더로 보냅니다.
