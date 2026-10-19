---
name: ui-verification-playwright
description: headless Ubuntu 서버에서 Playwright + Chromium으로 웹 UI를 실제로 클릭하며 검증한다. 설치, 데스크톱·폰 폭 캡처, 콘솔 오류 수집, 가로 넘침 검사, WebRTC 재생 확인, 요청 가로채기로 실제 장비를 움직이지 않고 프론트엔드가 보내는 값만 확인하는 방법을 다룬다. 브라우저 없는 서버에서 만든 웹 화면을 검증할 때 사용.
---

# Playwright로 화면 검증 (headless 서버)

## 설치
```sh
python3 -m venv ~/.venvs/playwright
~/.venvs/playwright/bin/pip install playwright
sudo ~/.venvs/playwright/bin/playwright install-deps chromium   # 시스템 라이브러리 (apt)
~/.venvs/playwright/bin/playwright install chromium
fc-list :lang=ko | head     # 한글 폰트 확인 (없으면 fonts-noto-cjk)
```

## 기본 점검 스크립트에 넣을 것
```python
pg.on('pageerror', lambda e: errs.append(str(e)))
pg.on('console', lambda m: errs.append(m.text) if m.type == 'error' else None)
hs = pg.evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth')  # 0이어야 함
pg.set_viewport_size({'width': 400, 'height': 900})   # 폰 폭에서 한 번 더
pg.locator('#panel').screenshot(path='panel.png')      # 요소 단위 캡처가 비교하기 쉬움
```
- 전체 페이지 캡처는 sticky 헤더가 중간에 그려져 보일 수 있습니다. 헤더가 내용을 가리는지는 **뷰포트 캡처**와
  `getBoundingClientRect()`로 확인.
- 캡처는 반드시 직접 열어 봅니다. 기능 테스트가 통과해도 라벨 겹침, 세로로 꺾인 표 머리글, 넘침은 눈으로만 보입니다.

## 실제로 찾은 문제들
- 폰 폭에서 표 머리글·짧은 칸이 한 글자씩 세로로 꺾임 → `white-space: nowrap` + 표 컨테이너 가로 스크롤.
- 툴바 버튼이 늘면서 폰에서 545px 넘침 → flex-wrap.
- 그래프 x축 눈금 라벨 중복(같은 날짜 두 번), 좁은 화면에서 라벨 겹침 → 범위에 따라 형식·개수 조절.
- 상태 문구 중복("중지됨 — 중지됨").

## WebRTC 재생 확인
```python
b = p.chromium.launch(args=['--autoplay-policy=no-user-gesture-required'])
v = pg.frame_locator('#player').locator('video')
v.evaluate('v => [v.videoWidth, v.videoHeight, v.currentTime]')   # 몇 초 뒤 currentTime 증가 확인
```

## 장비를 움직이지 않고 프론트엔드만 확인
운영 중인 서비스에서 "버튼을 누르면 어떤 요청이 가는지"만 보고 싶을 때:
```python
def handle(route):
    sent.append(json.loads(route.request.post_data or '{}'))
    route.fulfill(status=400, content_type='application/json', body='{"error":"test: blocked"}')
pg.route('**/api/scan/start', handle)
```
→ 실제 스캔은 시작되지 않고, 화면이 `{"only_locked": true, ...}`를 보내는지 확인할 수 있습니다.

## 테스트 데이터
- 운영과 분리된 테스트 백엔드(`--data-root`)를 쓰고, 표시 확인에 필요한 특수 결과(예: 영상 정지)는
  테스트 데이터 루트에만 넣습니다. 테스트 루트에는 실제 캡처 이미지가 없어서 썸네일 404가 나는 것은 정상.
- 브라우저 `confirm()`은 `pg.on('dialog', lambda d: d.accept())`로 처리.
- 같은 이름의 테스트 데이터가 이전 실행에서 남아 있으면 locator가 여러 개 잡힙니다(strict mode). 시작 전에 정리하거나 `.first`.
