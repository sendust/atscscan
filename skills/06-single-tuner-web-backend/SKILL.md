---
name: single-tuner-web-backend
description: 장치 하나(DVB 수신기 등)를 여러 사용자·스케줄이 공유하는 Python 표준 라이브러리 웹 백엔드를 만든다. 세션 교체, 외부 프로세스 정리, 고아 프로세스 처리, 정적 파일 캐시 무효화, 테스트 인스턴스 격리, systemd 서비스화를 다룬다. 하드웨어를 제어하는 작은 사내 웹 도구를 만들 때 사용.
---

# 장치 한 대를 공유하는 웹 백엔드

## 구성
- `http.server.ThreadingHTTPServer` + `SimpleHTTPRequestHandler(directory=www)` — 외부 패키지 없음.
- 프론트엔드는 1초 폴링(`/api/status`)으로 작은 요약만 받고, 큰 데이터(PSIP, 이력)는 **리비전 번호가 바뀔 때만** 따로 요청.
- 상태 객체: 현재 세션 1개 + 자동 스캔 상태 + 결과 캐시. 새 튜닝은 항상 "이전 세션 정지 → 새 세션" (잠금 안에서).

## 외부 프로세스
- `Popen(..., start_new_session=True)` → 정지 시 `os.killpg(pid, SIGINT)`, 2초 기다린 뒤 `SIGKILL`.
  입력이 끊겨 블록된 프로세스는 SIGINT를 늦게 처리하므로 기다리는 시간을 짧게.
- stderr를 읽는 스레드가 종료 처리(로그 기록, 결과 저장)까지 맡고, 상태 조회 스레드와 경쟁하지 않도록
  결과 쓰기는 잠금 + "종료 처리 완료" 표시로 보호(조회가 종료 직후 값을 덮어쓰는 문제가 실제로 있었음).

## 고아 프로세스
- 백엔드가 비정상 종료되면 별도 세션으로 띄운 tsp가 남아 장치를 계속 잡습니다("Device or resource busy").
- 시작할 때 `/proc/*/cmdline`에서 **자기만의 시그니처**(예: `--json-line=DVBSTATUS:`)가 있는 tsp만 골라 종료.
- 주의: 같은 시그니처를 쓰는 **다른 인스턴스(운영 서비스)의 tsp도 죽입니다.** 테스트 인스턴스를 띄우기 전에
  운영 쪽 `/api/status`로 수신 중인지 확인하고, 수신 중이면 멈추고 나중에 되돌리기.
- 셸에서 `pkill -f 패턴`은 같은 문자열을 가진 자기 셸까지 죽일 수 있습니다. `ps -eo pid,args | awk '$2==…'`로 정확히 고르기.

## 정적 파일 캐시
- 기본 핸들러는 Last-Modified만 보내서, 브라우저가 **새 HTML + 예전 JS**를 조합하는 일이 생깁니다
  (실제로 새 체크박스 값이 서버에 전달되지 않았음).
- 해결: 정적 응답에 `Cache-Control: no-cache`(재검증, 304라 가벼움) + `index.html`을 내려줄 때
  `app.js?v=<mtime>`처럼 자산 주소에 수정 시각을 붙임. API 응답은 `no-store`.

## 설정 파일
- `config.json`은 mtime이 바뀌면 다시 읽기(재시작 없이 반영), 기본값과 깊은 병합. 없으면 기본값으로 만들고 권한 600.
- 비밀값(토큰)이 든 파일은 정적 디렉터리 밖에 두고, 공개용 요약 API에서는 빼고, 저장소에는 `config.example.json`만.

## 테스트 격리
- `--port 8099 --data-root <임시 디렉터리> --no-scheduler`처럼 **설정·이력·경보·캡처 경로를 통째로 바꾸는 옵션**을 둡니다.
  테스트 측정이 운영 기준선에 섞이면 경보 판정이 틀어집니다.
- 장치는 하나뿐이므로 운영 서비스가 쉬고 있을 때만 장치를 쓰는 테스트를 합니다.

## 상시 실행 (systemd)
```ini
[Service]
User=sendust
SupplementaryGroups=video
WorkingDirectory=/home/sendust/atscscan
ExecStart=/usr/bin/python3 backend/server.py
Restart=always
RestartSec=5
```
- 스케줄 기능은 백엔드가 항상 떠 있어야 의미가 있습니다. 셸에서 `nohup`으로 띄운 것은 재부팅·세션 종료에 취약.
- SIGTERM을 받으면 현재 세션을 정리하고 종료하도록 처리.
- 장치를 USB로 뺐다 꽂으면 진행 중이던 세션은 끝나고(tsp 종료) 서비스는 계속 살아 있어야 합니다.
