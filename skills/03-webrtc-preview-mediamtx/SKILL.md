---
name: webrtc-preview-mediamtx
description: 방송 TS(MPEG-2/AC-3)를 브라우저에서 1초 안팎 지연으로 보기 위해 tsp -P fork로 같은 TS를 ffmpeg에 넘겨 H.264(B-프레임 없음)+Opus로 바꾸고 mediamtx에 RTSP로 송출, 내장 WebRTC 플레이어를 iframe으로 붙인다. NVIDIA GPU 유무에 따라 인코더를 고른다. 수신 중인 방송을 웹 화면에 미리보기로 띄울 때 사용.
---

# TS → WebRTC 미리보기 (mediamtx)

## 왜 변환이 필요한가
브라우저는 MPEG-2 영상과 AC-3 음성을 재생하지 못하고, WebRTC는 **H.264/VP8/VP9 + Opus**만 받습니다(AAC 불가).
또 WebRTC 브라우저 디코더는 B-프레임을 싫어하므로 `-bf 0`.

## 구조
```
tsp -I dvb ... -P tables ... -P analyze ... -P fork --ignore-abort --buffered-packets 100 'ffmpeg ...' -O drop
ffmpeg -i pipe:0 -map 0:v:0 -map '0:a:0?'   # 첫 번째 영상/음성 = 첫 번째 서비스
       ... -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/<path>
브라우저: http://<서버>:8889/<path>/?controls=true&muted=true&autoplay=true&playsinline=true   (iframe)
```
- 수신기는 한 프로세스만 열 수 있으므로 **분석하는 tsp 안에서 fork**합니다. `--ignore-abort`가 없으면
  ffmpeg가 죽을 때 tsp(분석)도 같이 끝납니다.
- fork는 `/bin/sh -c`로 실행되므로 인자는 `shlex.quote`로 감쌉니다(`0:a:0?`의 `?` 등).
- ffmpeg stderr는 tsp stderr로 섞여 나옵니다. `-loglevel error`로 줄이고 `* `로 시작하지 않는 줄을 `[ffmpeg]`로 표시.
- tsp를 `start_new_session=True`로 띄우면 `killpg` 한 번에 ffmpeg까지 정리됩니다.

## GPU / CPU 경로 (설정 `"nvidia": true|false`)
| 경로 | 명령 핵심 | 부하 (i7-6700K, 1080i MPEG-2) |
|---|---|---|
| GPU | `-hwaccel cuda -hwaccel_output_format cuda` + `yadif_cuda=0,scale_cuda=1280:720` + `h264_nvenc -preset p4 -tune ll` | GPU 3~7%, ffmpeg CPU 약 11% |
| CPU | `bwdif=0,scale=960:540` + `libx264 -preset ultrafast -tune zerolatency` | 약 190% |
| (처음 CPU 설정) | `yadif=0,scale=1280:720` + `veryfast` | 약 400% |

- `true`여도 실제로 NVENC가 되는지 `ffmpeg -f lavfi -i color -c:v h264_nvenc -f null -`로 한 번 시험하고, 실패하면 CPU로.
- CPU 인코더는 libx264 → libopenh264 → libvpx(VP8) 순으로 실제 실행해 보고 고릅니다(빌드에 없는 경우가 있음).

## mediamtx
- 기본 설정이면 경로를 미리 만들 필요 없이 RTSP로 publish하면 바로 WebRTC로 재생됩니다(`all_others`).
- 내장 플레이어는 iframe 삽입을 막지 않고 `controls`, `muted`, `autoplay`, `playsinline` 쿼리를 지원합니다.
- 브라우저가 서버의 **UDP 8189**(미디어)와 **TCP 8889**(페이지)에 닿아야 합니다. 서버에 Docker·Tailscale 등
  여러 인터페이스가 있으면 ICE 후보가 많아집니다. 연결이 안 되면 `webrtcAdditionalHosts`·인터페이스 목록을 정리.
- 인증이 꺼진 기본 설정은 누구나 송출·재생할 수 있으니 공개망이면 읽기 인증을 거세요.

## 화면 연동
- 세션이 바뀔 때만 iframe `src`를 바꿉니다(매초 갱신하면 끊김). 중지·lock 실패 시 `about:blank`.
- 자동 재생 정책 때문에 음소거로 시작하고, 플레이어에서 음소거 해제.

## 검증 방법
headless Chromium(Playwright)에서 플레이어를 열고 `video.videoWidth`, `currentTime`이 증가하는지 확인
(`--autoplay-policy=no-user-gesture-required`). 07 스킬 참고.
