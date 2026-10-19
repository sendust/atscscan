# atscscan(SBS 헤드엔드 모니터)을 만드는 데 쓴 기술 (skills)

DVB/ATSC 수신기 한 대로 헤드엔드 출력을 매일 점검하는 웹 시스템을 만들면서 필요했던 기술을 주제별로 정리했습니다.
각 폴더의 `SKILL.md`는 Claude Code 스킬 형식(name/description 머리말)이라, 사람이 읽는 문서로도 쓰고
`~/.claude/skills/`나 프로젝트의 `.claude/skills/`에 복사하면 Claude Code 스킬로도 쓸 수 있습니다.

수신기 드라이버를 만든 과정은 [skytv_hdusbmini 저장소의 skills](https://github.com/sendust/skytv_hdusbmini/tree/main/skills)에 따로 있습니다.

| # | 스킬 | 다루는 것 |
|---|---|---|
| 1 | [tsduck-live-psip-json](01-tsduck-live-psip-json/SKILL.md) | `tsp`를 백그라운드로 돌리며 dvbstatus·PSIP 테이블·분석 결과를 JSON 줄로 실시간 수집, ATSC PSIP 해석(MGT/VCT/STT/EIT/ETT) |
| 2 | [dvb-frontend-stats-ioctl](02-dvb-frontend-stats-ioctl/SKILL.md) | tsp가 장치를 쓰는 동안 frontend를 읽기 전용으로 열어 DVBv3 ioctl로 lock·SNR·세기·UCB 읽기, BER 대체 지표 |
| 3 | [webrtc-preview-mediamtx](03-webrtc-preview-mediamtx/SKILL.md) | `tsp -P fork` → ffmpeg → mediamtx → 브라우저 WebRTC, GPU(NVENC)/CPU 경로, 코덱 제약 |
| 4 | [freeze-frame-detection](04-freeze-frame-detection/SKILL.md) | 헤드엔드 출력의 영상 정지 판정: freezedetect가 놓친 이유와 이동 중앙값 방식, 실측 기준값 |
| 5 | [headend-monitoring-alerts](05-headend-monitoring-alerts/SKILL.md) | 채널별 이력, 중앙값 기준선, 경보 생성·중복 방지·자동 해제, 텔레그램 묶음 전송, cron 스케줄러 |
| 6 | [single-tuner-web-backend](06-single-tuner-web-backend/SKILL.md) | 수신기 한 대를 공유하는 표준 라이브러리 백엔드: 세션 교체, 고아 프로세스 정리, 캐시 무효화, 테스트 격리, systemd |
| 7 | [ui-verification-playwright](07-ui-verification-playwright/SKILL.md) | headless Chromium으로 화면 검증, 요청 가로채기, 폰 폭 점검, 실제 장비를 건드리지 않는 시험 방법 |

## 이번 작업에서 실제로 빠졌던 함정 (요약)

1. **EIT PID를 고정했다가 SBS 편성표를 놓침**: KBS는 0x1D00~, SBS는 0x1A00~. PID는 MGT가 정하므로 필터를 두지 않거나 MGT를 따라가야 함.
2. **lgdt3305는 DVBv5 통계를 주지 않음**: TSDuck dvbstatus에 SNR·세기·BER이 비어 있음. DVBv3 ioctl로 따로 읽고, BER 대신 TEI 비율을 씀.
3. **`freezedetect` 기본값으로는 헤드엔드 정지 화면을 못 잡음**: 재인코딩 잡음(평소 0.26, 키프레임마다 0.4~1.2)이 정지 구간을 끊음. 이동 중앙값 + 임계값 1.0으로 해결.
4. **브라우저가 예전 app.js를 캐시**: 새 HTML은 받고 JS는 캐시를 써서 새 옵션이 전달되지 않음. `no-cache` + `?v=<mtime>`.
5. **테스트 백엔드가 운영 중인 수신을 끊음**: 시작 시 고아 tsp 정리 기능이 운영 서비스의 tsp까지 정리. 테스트 전에 `/api/status` 확인 필수.
6. **`pkill -f 패턴`이 자기 셸을 죽임**: 명령줄에 같은 문자열이 들어 있으면 함께 매칭됨. PID를 정확히 골라 `kill`.
7. **테스트 기록이 운영 기준선을 오염**: `--data-root`로 설정·이력·경보 경로를 통째로 분리.
8. **약한 신호의 "lock"**: lock 비트만 켜지고 TS가 없는 채널 → 5초 안에 PSI가 하나도 없으면 "불안정(weak)"으로 분류.
