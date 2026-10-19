---
name: dvb-frontend-stats-ioctl
description: 다른 프로그램(tsp, dvbv5-zap 등)이 DVB 장치를 쓰는 동안 frontend를 읽기 전용으로 열어, DVBv5 통계(FE_GET_PROPERTY, ctypes)와 DVBv3 ioctl로 lock, CNR, RF 레벨(dBm), BER, UCB를 카드와 무관한 형태로 읽고, 여러 프론트엔드 중 ATSC 지원 프론트엔드를 고른다. 여러 종류의 DVB 수신기를 지원하는 모니터링 프로그램을 만들 때 사용.
---

# DVB frontend 통계 직접 읽기 (DVBv3 · DVBv5)

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

## DVBv5 통계 직접 읽기 (여러 카드 지원) — `backend/dvbdev.py`
DVBv3만으로는 카드마다 단위가 달라 비교가 안 됩니다. 최근 드라이버는 대부분 DVBv5 통계를 주므로 그것을 우선합니다.

```python
class _Stat(ctypes.Structure):      _pack_ = 1; _fields_ = [('scale', c_uint8), ('value', c_int64)]
class _FeStats(ctypes.Structure):   _pack_ = 1; _fields_ = [('len', c_uint8), ('stat', _Stat * 4)]
class _Buffer(ctypes.Structure):    _pack_ = 1; _fields_ = [('data', c_uint8 * 32), ('len', c_uint32),
                                                           ('reserved1', c_uint32 * 3), ('reserved2', c_void_p)]
class _U(ctypes.Union):             _pack_ = 1; _fields_ = [('data', c_uint32), ('st', _FeStats), ('buffer', _Buffer)]
class _Prop(ctypes.Structure):      _pack_ = 1; _fields_ = [('cmd', c_uint32), ('reserved', c_uint32 * 3), ('u', _U), ('result', c_int)]
class _Props(ctypes.Structure):     _fields_ = [('num', c_uint32), ('props', POINTER(_Prop))]   # 패딩 있음(16바이트)
FE_GET_PROPERTY = _ior(83, ctypes.sizeof(_Props))
```
- 명령: `DTV_ENUM_DELSYS`(44, buffer.data에 SYS_* 목록), `DTV_STAT_SIGNAL_STRENGTH`(62), `DTV_STAT_CNR`(63),
  `PRE/POST_ERROR_BIT_COUNT`·`PRE/POST_TOTAL_BIT_COUNT`(64~67), `ERROR_BLOCK_COUNT`(68), `TOTAL_BLOCK_COUNT`(69).
- scale: 1 = dB(값은 0.001 dB 단위, 세기는 dBm), 2 = 상대값(0~65535), 3 = 카운터, 0 = 없음.
- BER = (오류 비트 증가량) / (전체 비트 증가량). 카운터는 누적이라 두 번 읽은 차이로 계산합니다.
- 읽기 전용 fd에서도 `FE_GET_PROPERTY`는 동작합니다(쓰기는 `FE_SET_PROPERTY`만 필요).
- `FE_GET_INFO`(nr 61, 168바이트): 이름 128바이트 + type, frequency_min/max(Hz) 등 → 주파수 범위 밖 채널을 미리 제외.

## 카드별 실측
| 카드 | SNR | 세기 | BER |
|---|---|---|---|
| SKYTV HD USB mini (lgdt3305/LGDT3304) | v3, 0.1 dB | v3, SNR 환산 | v3 항상 0, v5 없음 |
| WinTV-HVR-975 (lgdt3306a + si2157) | v5 CNR dB | **v5 dBm (실제 RF 레벨)** | v3 0xFFFFFFFF 고정, v5 없음 |
| DViCO FusionHDTV7 USB (au0828 + au8522 + xc5000) | v3, 0.1 dB | v3, SNR 환산 | v5 없음 |

USB 장치에 manufacturer/product 문자열이 없는 카드(FusionHDTV7 `0fe9:d620`)는 sysfs로 이름을 얻을 수 없어서
커널 보드 목록의 이름을 USB ID 표(`KNOWN_USB`)로 둡니다.

카드를 바꾸면 SNR·품질 값의 의미가 달라지므로 기준값은 카드별로 따로 계산해야 합니다(같은 채널이 SNR 9.6 → 31.8 dB로 바뀐 예가 있었음).
