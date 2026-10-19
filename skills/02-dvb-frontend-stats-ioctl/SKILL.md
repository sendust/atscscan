---
name: dvb-frontend-stats-ioctl
description: 다른 프로그램(tsp, dvbv5-zap 등)이 DVB 장치를 쓰는 동안 frontend를 읽기 전용으로 열어 Python fcntl ioctl(DVBv3)로 lock 비트, SNR, 신호 세기, UCB, BER을 읽는다. 드라이버가 DVBv5 통계를 주지 않아 TSDuck에 수신 레벨이 안 나올 때 사용.
---

# DVB frontend 통계 직접 읽기 (DVBv3 ioctl)

## 왜
TSDuck·libdvbv5는 DVBv5 통계(`DTV_STAT_*`)를 읽습니다. 커널 `lgdt3305`처럼 DVBv3 콜백만 있는 드라이버는
v5 통계가 비어 있어서 TSDuck dvbstatus에 SNR/세기가 없습니다(dvbv5-zap은 v3로 대신 읽어 보여줌).

## 동시 접근
DVB frontend는 **쓰기 열기는 하나, 읽기 전용 열기는 여러 개**를 허용합니다. tsp가 튜닝해서 잡고 있는 동안
`os.open('/dev/dvb/adapter0/frontend0', os.O_RDONLY | os.O_NONBLOCK)`로 열어 상태만 읽을 수 있습니다.
tsp가 튜닝을 마친 뒤(0.5초 정도 지나서) 열고, tsp가 끝나면 닫습니다.

## ioctl 번호 (x86_64/arm64, `_IOR('o', nr, size)`)
```python
def _ior(nr, size): return (2 << 30) | (size << 16) | (ord('o') << 8) | nr
FE_READ_STATUS             = _ior(69, 4)   # fe_status_t (bit 0x01 SIGNAL 0x02 CARRIER 0x04 VITERBI 0x08 SYNC 0x10 LOCK)
FE_READ_BER                = _ior(70, 4)
FE_READ_SIGNAL_STRENGTH    = _ior(71, 2)
FE_READ_SNR                = _ior(72, 2)
FE_READ_UNCORRECTED_BLOCKS = _ior(73, 4)
buf = bytearray(4); fcntl.ioctl(fd, FE_READ_STATUS, buf); status = struct.unpack('I', buf)[0]
```

## 단위는 드라이버마다 다름 — 소스를 확인할 것
| 값 | lgdt3305 기준 |
|---|---|
| SNR | 0.1 dB 단위 (`*snr = state->snr / ((1 << 24) / 10)`) |
| 신호 세기 | RF 레벨이 아니라 **SNR 0~35 dB를 0~65535로 환산**한 값 → "신호 품질 %"로 표시 |
| UCB | FEC 패킷 오류 16비트 카운터. lock 전 값은 의미 없음 → lock된 측정끼리만 증가율 계산(65536 순환) |
| BER | 구현 안 됨, **항상 0** → 화면에는 N/A |

## BER 대체 지표
- TSDuck `analyze`의 `transport-errors`(TEI 표시 패킷 수) / `packets.total` = 패킷 오류율.
- 관찰: 이 수신기에서는 수신 시작 시 TEI가 매번 같은 수(357)만큼 나오는 패턴이 있었음 — 시작 직후 값은 따로 보는 게 좋음.

## lock 직후 0 dB
LGDT3304는 lock 직후 MSE가 유효해질 때까지 SNR을 0으로 돌려줍니다. 통계(평균·최소)에서 SNR=0 측정은 뺍니다.

## 약한 신호 판정
lock 비트는 켜졌는데 TS가 안 오는 경우가 있습니다(SNR 1~2 dB). lock 후 5초 안에 PSI 섹션이 하나도 없으면
"불안정(weak)"으로 분류하고 다음 채널로 넘어갑니다. lock 비트만으로 "수신 성공"을 판단하지 않습니다.
