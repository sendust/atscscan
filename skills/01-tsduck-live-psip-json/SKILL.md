---
name: tsduck-live-psip-json
description: TSDuck tsp를 백그라운드 프로세스로 실행해 DVB 수신 상태(dvbstatus), PSI/PSIP 테이블, TS 분석 결과를 JSON 줄로 실시간 수집하고, ATSC PSIP(MGT, TVCT/CVCT, STT, EIT, ETT, RRT)를 화면용 모델로 정리한다. 웹 UI나 모니터링 프로그램에서 TSDuck 결과를 프로그램으로 다룰 때 사용.
---

# TSDuck 실시간 JSON 수집과 ATSC PSIP 해석

## 파이프라인
```sh
tsp --realtime \
  -I dvb --adapter 0 --delivery-system ATSC --frequency 479000000 --modulation 8-VSB \
         --signal-timeout 6 --receive-timeout 5000 \
         --json-line='DVBSTATUS:' --json-interval 2 \
  -P tables --log-json-line='TABLE:' \
  -P analyze --interval 3 --json-line='ANALYZE:' \
  -O drop
```
- 모든 JSON은 **stderr**에 `* dvb: DVBSTATUS:{...}`, `* tables: TABLE:{...}` 형태의 한 줄로 나옵니다.
  접두사를 찾아 그 뒤를 `json.loads`. 접두사 없는 줄은 일반 로그(오류 포함)로 표시합니다.
- `tables`는 같은 버전의 테이블은 한 번만 내보냅니다(버전이 바뀌면 다시).
- lock 실패는 `* Error: dvb: no input signal lock after N milliseconds` 후 정상 종료(exit 0)로 나타납니다.
- Python에서는 `Popen(..., stderr=PIPE, text=True, bufsize=1, start_new_session=True)`로 띄우고
  한 줄씩 읽습니다. 끝낼 때는 `os.killpg(pid, SIGINT)` → 2초 안에 안 끝나면 SIGKILL(입력이 끊긴 tsp는 SIGINT를 늦게 처리함).

## PID 필터를 두지 말 것
`--pid 0x1FFB --pid 0x1D00-0x1EFF`처럼 고정하면 방송사마다 다른 EIT/ETT PID를 놓칩니다
(KBS 0x1D00~, **SBS 0x1A00~**). EIT/ETT PID는 MGT가 알려줍니다. 필터 없이 전체 PID를 넣어도
섹션 CRC 검사 덕분에 영상·음성 PID에서 엉뚱한 테이블은 나오지 않았고, CPU는 9% 정도였습니다.

## JSON 구조 (XML→JSON 자동 변환)
노드는 `{"#name": "...", "#nodes": [...], 속성...}` 형태입니다. 첫 노드 `metadata`에 PID가 있습니다.
문자열은 `..._text > string{language, text}` 안에 있으므로 재귀로 첫 `string`을 찾는 함수가 편합니다.

| 테이블 | 쓸 만한 필드 |
|---|---|
| MGT | `table{type, pid, version_number, number_bytes}` — EIT-0..n, ETT-n의 PID 목록 |
| TVCT / CVCT | `channel{major_channel_number, minor_channel_number, short_name, program_number, source_id, channel_tsid, service_type, modulation_mode}`, `service_location_descriptor > component{elementary_pid, stream_type}`, `extended_channel_name_descriptor` |
| STT | `system_time`(GPS 초), `gps_utc_offset` |
| ATSC_EIT | 최상위 `source_id`, `event{event_id, start_time(UTC 문자열), length_in_seconds, title_text}` |
| ETT | `etm_id`, `extended_text_message` |
| RRT | `rating_region`, `rating_region_name`, `dimension`(없을 수도 있음 — 국내 방송은 지역 이름만 보내기도 함) |
| PAT / PMT | `service{service_id, program_map_pid}` / `component{elementary_pid, stream_type}` |

## 변환 규칙
- **STT**: UTC = 1980-01-06T00:00:00Z + (system_time − gps_utc_offset) 초. 화면은 KST(+9)로.
- **EIT 시작 시각**: TSDuck이 이미 UTC 문자열로 바꿔 줌 → KST로 변환.
- **ETT ↔ EIT**: `etm_id = (source_id << 16) | (event_id << 2) | 0x2` 로 이벤트 상세 설명을 연결.
- **채널 이름**: EIT의 `source_id`를 VCT의 `source_id`와 맞춰 "9-1 KBS1"처럼 표시.
- **stream_type**: 0x02 MPEG-2 Video, 0x81 AC-3, 0x1B H.264 등.

## analyze JSON
`ts{bitrate, pcr-bitrate, packets{total, invalid-syncs, transport-errors}}`, `services[{id, name, provider, bitrate, pmt-pid, pcr-pid, pids}]`,
`pids[{id, description, bitrate, packets{total, discontinuities}}]`. `--interval 3`이면 3초마다 누적값이 갱신됩니다.

## 주의
- TSDuck dvbstatus에 `snr`, `strength`, `bit-error-rate`가 오려면 드라이버가 **DVBv5 통계**를 제공해야 합니다.
  lgdt3305처럼 DVBv3만 있는 드라이버면 비어 있습니다 → 02 스킬로 직접 읽음.
- `tsscan`의 korea 지역에는 UHF 정의가 없는 버전이 있습니다. 한국 지상파는 미국과 같은 6 MHz 채널표라 `--usa` 사용.

## TSDuck의 MPEG-2 프레임 레이트 표시 오류
TSDuck 3.44의 `analyze`와 `pes --video-attributes`는 MPEG-2 비디오를 frame_rate_code와 관계없이 항상
`1920x1080i, @25 Hz`로 표시합니다(23.976/25/29.97/30/59.94 fps 시험 스트림이 모두 25 Hz로 나옴. 해상도·비트레이트는 정상).
같은 TS를 받는 캡처용 ffmpeg를 `-loglevel level+info`로 돌려 입력 스트림 줄
`Stream #0:0[0x11]: Video: mpeg2video ..., 29.97 fps`에서 PID별 프레임 레이트를 얻고, 그 값으로 설명을 고칩니다.
`[info]` 줄은 로그에서 빼고, `[error]`/`[warning]` 표시만 떼어 로그에 남깁니다.

**TSDuck 3.45-4798에서 고쳐졌습니다.** 같은 시험 스트림이 23.97/25/29.97/30/59.94 Hz로 나오고, 실제 수신(29.97)도 맞게 나옵니다.
새 버전을 설치하기 전에 deb를 `dpkg-deb -x`로 풀어 `LD_LIBRARY_PATH=<x>/usr/lib TSPLUGINS_PATH=<x>/usr/lib/tsduck <x>/usr/bin/tsp`로
돌려 보면 설치된 버전을 건드리지 않고 비교할 수 있습니다(strace로 새 라이브러리·플러그인을 여는지 확인).
