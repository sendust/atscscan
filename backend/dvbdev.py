"""Linux DVB device discovery and frontend statistics for many tuner cards.

- enumerate /dev/dvb/adapterN/frontendM, read FE_GET_INFO and the supported delivery systems (DTV_ENUM_DELSYS)
- pick the frontend that supports the wanted delivery system (e.g. ATSC on a multi-standard card)
- read statistics: DVBv5 (DTV_STAT_*: CNR dB, signal dBm, pre/post BER counters, block errors) first,
  DVBv3 ioctls as fallback with per-driver scaling where known

Read-only opens are used everywhere, so this works while tsp holds the frontend.
Standard library only (ctypes for FE_GET_PROPERTY).
"""
import ctypes
import fcntl
import glob
import os
import re
import struct
import time


def _ior(nr, size):
    return (2 << 30) | (size << 16) | (ord('o') << 8) | nr


# DVBv3
FE_GET_INFO = _ior(61, 168)
FE_READ_STATUS = _ior(69, 4)
FE_READ_BER = _ior(70, 4)
FE_READ_SIGNAL_STRENGTH = _ior(71, 2)
FE_READ_SNR = _ior(72, 2)
FE_READ_UNCORRECTED_BLOCKS = _ior(73, 4)

# DVBv5 properties
DTV_DELIVERY_SYSTEM = 17
DTV_ENUM_DELSYS = 44
DTV_STAT_SIGNAL_STRENGTH = 62
DTV_STAT_CNR = 63
DTV_STAT_PRE_ERROR_BIT_COUNT = 64
DTV_STAT_PRE_TOTAL_BIT_COUNT = 65
DTV_STAT_POST_ERROR_BIT_COUNT = 66
DTV_STAT_POST_TOTAL_BIT_COUNT = 67
DTV_STAT_ERROR_BLOCK_COUNT = 68
DTV_STAT_TOTAL_BLOCK_COUNT = 69
FE_SCALE_NOT_AVAILABLE, FE_SCALE_DECIBEL, FE_SCALE_RELATIVE, FE_SCALE_COUNTER = 0, 1, 2, 3

DELSYS = {0: 'UNDEFINED', 1: 'DVB-C/A', 2: 'DVB-C/B', 3: 'DVB-T', 4: 'DSS', 5: 'DVB-S', 6: 'DVB-S2', 7: 'DVB-H',
          8: 'ISDB-T', 9: 'ISDB-S', 10: 'ISDB-C', 11: 'ATSC', 12: 'ATSC-M/H', 13: 'DTMB', 14: 'CMMB', 15: 'DAB',
          16: 'DVB-T2', 17: 'TURBO', 18: 'DVB-C/C', 19: 'DVB-C2'}
# names used by TSDuck --delivery-system  ->  kernel SYS_* value
TSDUCK_DELSYS = {'ATSC': 11, 'DVB-C/B': 2, 'DVB-C/A': 1, 'DVB-C/C': 18, 'DVB-T': 3, 'DVB-T2': 16, 'ISDB-T': 8}

FE_BITS = [(0x01, 'SIGNAL'), (0x02, 'CARRIER'), (0x04, 'VITERBI'), (0x08, 'SYNC'), (0x10, 'LOCK')]

# DVBv3 SNR units differ per driver. Only the ones verified from kernel sources are converted to dB.
V3_SNR_TENTH_DB = ('LGDT3304', 'LGDT3305', 'LGDT3306A', 'AU8522', 'LGDT330')


class _Stat(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('scale', ctypes.c_uint8), ('value', ctypes.c_int64)]


class _FeStats(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('len', ctypes.c_uint8), ('stat', _Stat * 4)]


class _Buffer(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('data', ctypes.c_uint8 * 32), ('len', ctypes.c_uint32), ('reserved1', ctypes.c_uint32 * 3),
                ('reserved2', ctypes.c_void_p)]


class _U(ctypes.Union):
    _pack_ = 1
    _fields_ = [('data', ctypes.c_uint32), ('st', _FeStats), ('buffer', _Buffer)]


class _Prop(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('cmd', ctypes.c_uint32), ('reserved', ctypes.c_uint32 * 3), ('u', _U), ('result', ctypes.c_int)]


class _Props(ctypes.Structure):
    _fields_ = [('num', ctypes.c_uint32), ('props', ctypes.POINTER(_Prop))]


FE_GET_PROPERTY = _ior(83, ctypes.sizeof(_Props))


def _get_props(fd, cmds):
    arr = (_Prop * len(cmds))()
    for i, c in enumerate(cmds):
        arr[i].cmd = c
    p = _Props(len(cmds), ctypes.cast(arr, ctypes.POINTER(_Prop)))
    fcntl.ioctl(fd, FE_GET_PROPERTY, p)
    return arr


def frontend_path(adapter, frontend):
    return f'/dev/dvb/adapter{adapter}/frontend{frontend}'


def probe_frontend(adapter, frontend):
    """FE_GET_INFO + supported delivery systems for one frontend (None if it cannot be opened)."""
    try:
        fd = os.open(frontend_path(adapter, frontend), os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = {'adapter': adapter, 'frontend': frontend, 'name': None, 'freq_min_mhz': None,
                'freq_max_mhz': None, 'delsys': []}
        try:
            buf = bytearray(168)
            fcntl.ioctl(fd, FE_GET_INFO, buf)
            info['name'] = buf[:128].split(b'\0', 1)[0].decode('utf-8', 'replace').strip() or None
            fmin, fmax = struct.unpack_from('II', buf, 132)
            info['freq_min_mhz'], info['freq_max_mhz'] = round(fmin / 1e6, 3), round(fmax / 1e6, 3)
        except OSError:
            pass
        try:
            arr = _get_props(fd, [DTV_ENUM_DELSYS])
            b = arr[0].u.buffer
            info['delsys'] = [DELSYS.get(b.data[i], str(b.data[i])) for i in range(min(b.len, 32))]
        except OSError:
            pass
        return info
    finally:
        os.close(fd)


def usb_info(adapter, frontend=0):
    try:
        d = os.path.realpath(f'/sys/class/dvb/dvb{adapter}.frontend{frontend}/device')
    except OSError:
        return None, None
    for base in (d, os.path.dirname(d), os.path.dirname(os.path.dirname(d))):
        if os.path.exists(os.path.join(base, 'idVendor')):
            def rd(n):
                try:
                    with open(os.path.join(base, n)) as f:
                        return f.read().strip()
                except OSError:
                    return ''
            name = ' '.join(x for x in (rd('manufacturer'), rd('product')) if x) or None
            return name, f"{rd('idVendor')}:{rd('idProduct')}"
    return None, None


def list_devices():
    out = []
    for p in sorted(glob.glob('/dev/dvb/adapter*/frontend*')):
        m = re.search(r'adapter(\d+)/frontend(\d+)$', p)
        if not m:
            continue
        info = probe_frontend(int(m.group(1)), int(m.group(2)))
        if info:
            info['usb'], info['usb_id'] = usb_info(info['adapter'], info['frontend'])
            out.append(info)
    return out


def select(adapter='auto', frontend='auto', delivery='ATSC'):
    """Pick (adapter, frontend) supporting `delivery` (TSDuck name). Explicit numbers in config win."""
    devs = list_devices()
    want = DELSYS.get(TSDUCK_DELSYS.get(delivery, -1), delivery)
    cand = [d for d in devs if (adapter in ('auto', None, '') or d['adapter'] == int(adapter))
            and (frontend in ('auto', None, '') or d['frontend'] == int(frontend))]
    good = [d for d in cand if want in d['delsys']] or [d for d in cand if not d['delsys']]
    return (good or cand or [None])[0]


def tsp_device(dev):
    """TSDuck --device-name. demux/dvr numbers are forced to 0 (most cards have a single demux)."""
    return f"/dev/dvb/adapter{dev['adapter']}:{dev['frontend']}:0:0"


class Frontend:
    """Read-only statistics reader. read() returns a dict that is the same for every card:

    snr_db, snr_src ('v5' | 'v3' | 'raw'), snr_raw, signal_dbm, quality_pct, ber, ber_src, ucb, bits, locked
    """

    def __init__(self, adapter, frontend=0, name=''):
        self.path = frontend_path(adapter, frontend)
        self.name = (name or '').upper()
        self.fd = None
        self._prev_bits = None      # (post_err, post_tot, pre_err, pre_tot) for BER deltas
        self.v5_ok = True

    def open(self):
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _v3(self, req, fmt):
        buf = bytearray(struct.calcsize(fmt))
        try:
            fcntl.ioctl(self.fd, req, buf)
        except OSError:
            return None
        return struct.unpack(fmt, buf)[0]

    def _v5(self):
        if not self.v5_ok:
            return {}
        cmds = [DTV_STAT_SIGNAL_STRENGTH, DTV_STAT_CNR, DTV_STAT_PRE_ERROR_BIT_COUNT, DTV_STAT_PRE_TOTAL_BIT_COUNT,
                DTV_STAT_POST_ERROR_BIT_COUNT, DTV_STAT_POST_TOTAL_BIT_COUNT, DTV_STAT_ERROR_BLOCK_COUNT,
                DTV_STAT_TOTAL_BLOCK_COUNT]
        try:
            arr = _get_props(self.fd, cmds)
        except OSError:
            self.v5_ok = False
            return {}
        out = {}
        for c, pr in zip(cmds, arr):
            st = pr.u.st
            if st.len >= 1 and st.stat[0].scale != FE_SCALE_NOT_AVAILABLE:
                out[c] = (st.stat[0].scale, st.stat[0].value)
        return out

    def read(self):
        st = self._v3(FE_READ_STATUS, 'I') or 0
        r = {'t': time.time(), 'status': st, 'bits': [n for b, n in FE_BITS if st & b], 'locked': bool(st & 0x10),
             'snr_db': None, 'snr_src': None, 'snr_raw': None, 'signal_dbm': None, 'quality_pct': None,
             'strength_pct': None, 'ber': None, 'ber_src': None, 'ucb': None}
        v5 = self._v5()
        # CNR
        if DTV_STAT_CNR in v5 and v5[DTV_STAT_CNR][0] == FE_SCALE_DECIBEL:
            r['snr_db'], r['snr_src'] = round(v5[DTV_STAT_CNR][1] / 1000.0, 1), 'v5'
        # signal: dBm (real RF level) or relative
        if DTV_STAT_SIGNAL_STRENGTH in v5:
            sc, val = v5[DTV_STAT_SIGNAL_STRENGTH]
            if sc == FE_SCALE_DECIBEL:
                r['signal_dbm'] = round(val / 1000.0, 1)
            elif sc == FE_SCALE_RELATIVE:
                r['quality_pct'] = round((val & 0xffff) * 100 / 65535, 1)
        # BER from cumulative counters (post-FEC preferred), as delta between two readings
        pe, pt = v5.get(DTV_STAT_POST_ERROR_BIT_COUNT), v5.get(DTV_STAT_POST_TOTAL_BIT_COUNT)
        qe, qt = v5.get(DTV_STAT_PRE_ERROR_BIT_COUNT), v5.get(DTV_STAT_PRE_TOTAL_BIT_COUNT)
        cur = tuple(x[1] if x else None for x in (pe, pt, qe, qt))
        if self._prev_bits:
            for (ei, ti, src) in ((0, 1, 'v5-post'), (2, 3, 'v5-pre')):
                if None not in (cur[ei], cur[ti], self._prev_bits[ei], self._prev_bits[ti]):
                    de, dt_ = cur[ei] - self._prev_bits[ei], cur[ti] - self._prev_bits[ti]
                    if dt_ > 0 and de >= 0:
                        r['ber'], r['ber_src'] = de / dt_, src
                        break
        self._prev_bits = cur
        if DTV_STAT_ERROR_BLOCK_COUNT in v5:
            r['ucb'] = int(v5[DTV_STAT_ERROR_BLOCK_COUNT][1])
        # DVBv3 fallbacks
        snr3 = self._v3(FE_READ_SNR, 'H')
        r['snr_raw'] = snr3
        if r['snr_db'] is None and snr3 is not None:
            if any(k in self.name for k in V3_SNR_TENTH_DB):
                r['snr_db'], r['snr_src'] = round(snr3 / 10.0, 1), 'v3'
            else:
                r['snr_src'] = 'raw'            # unknown unit: shown as a raw number only
        s3 = self._v3(FE_READ_SIGNAL_STRENGTH, 'H')
        if s3 is not None:
            r['strength_pct'] = round(s3 * 100 / 65535, 1)
            if r['quality_pct'] is None:
                r['quality_pct'] = r['strength_pct']
        if r['ucb'] is None:
            r['ucb'] = self._v3(FE_READ_UNCORRECTED_BLOCKS, 'I')
        # DVBv3 BER has no defined unit and many drivers return 0 or 0xFFFFFFFF: never shown as BER
        return r
