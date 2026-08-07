# 두산 베어스 시즌 대시보드 · 자동 갱신

GitHub Actions가 매일 08:30 KST에 KBO 순위·득실·최근10·상대전적·일정을 수집하고,
가을야구 확률(피타고리안+실적 talent, 잔여경기 몬테카를로 10만회)을 재계산해
`index.html`을 다시 만들어 커밋합니다. GitHub Pages가 그 파일을 서빙하고,
노션 메인 페이지에는 그 Pages URL을 `embed`로 한 번만 걸어둡니다.
→ **PC를 켜둘 필요 없이 완전 무인 자동.**

## 구성 파일
- `build_dashboard.py` — 수집 + 계산 + `index.html` 생성 (Playwright 헤드리스 Chromium)
- `template.html` — 대시보드 HTML 원본. 스크립트의 `/*__INJECT_DATA__*/ null` 자리에 데이터가 주입됨
- `.github/workflows/update.yml` — 매일 08:30 KST 실행 워크플로 (+ 수동 실행 버튼)
- `requirements.txt`

## 최초 설정 (한 번만)
1. **저장소 생성**: GitHub에서 새 public 저장소를 만들고 이 폴더의 파일을 전부 올립니다.
   (public이어야 GitHub Pages가 무료. 내용은 KBO 공개 기록이라 민감하지 않음.)
2. **Pages 켜기**: 저장소 → Settings → Pages → Source를 **Deploy from a branch**,
   Branch를 **main / (root)** 으로 저장. 잠시 뒤 `https://<아이디>.github.io/<저장소명>/` 주소가 나옵니다.
3. **첫 실행**: 저장소 → Actions 탭 → `update-doosan-dashboard` → **Run workflow**(수동)로 한 번 돌립니다.
   초록불이면 `index.html`이 커밋되고 위 Pages 주소에서 대시보드가 보입니다.
4. **노션 embed 교체**: 노션 메인 페이지 "⚾ 두산 야구 분석" 상단의 기존 대시보드 embed를 지우고,
   `/embed` 블록으로 위 Pages 주소를 붙여넣습니다. 이후엔 매일 자동으로 최신 화면이 뜹니다.

## 동작 방식 · 정직 원칙
- 수집이 **부분 실패**하면 그 항목만 비우고(빈 값) 나머지는 진행합니다. **값을 지어내지 않습니다.**
- 10팀 순위를 못 얻으면 **에러로 종료**해 오래된/깨진 대시보드를 밀지 않습니다(기존 화면 유지).
- 로그인이 필요한 세이버메트릭스(wRC+·FIP·WAR·팀WAA)는 대시보드에서 제외합니다.

## 최초 실행 시 주의 (스크래핑 특성)
사이트 DOM이 바뀌면 `build_dashboard.py`의 표 파싱 셀렉터를 한 번 손봐야 할 수 있습니다.
로컬에서 `python build_dashboard.py --debug`로 돌리면 수집된 각 표의 헤더·첫 행을 출력하므로
어떤 열이 어디로 들어오는지 바로 확인·조정할 수 있습니다.

## 참고
- GitHub 예약 실행은 부하에 따라 몇 분~십몇 분 지연될 수 있습니다(정상).
- 저장소가 60일 무활동이면 예약이 비활성화되지만, 매일 커밋이 들어가므로 유지됩니다.
- 갱신 주기를 바꾸려면 `update.yml`의 cron을 수정하세요(UTC 기준, `30 23`=08:30 KST).
