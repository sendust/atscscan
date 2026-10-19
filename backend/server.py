#!/usr/bin/env python3
"""ATSC scan web backend.

- Serves the frontend in ../www and the channel list in ../channel.json
- Tunes with TSDuck (tsp -I dvb), collects PSIP/PSI tables as JSON lines
- Polls the DVB frontend (DVBv3 ioctls, read-only) for lock / SNR / strength / UCB

Python standard library only. Run as a user in the 'video' group.
"""
import argparse
import csv
import io
import zipfile
import datetime as dt
import fcntl
import json
import re
import shlex
import statistics
import os
import signal
import struct
import subprocess
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import dvbdev
import monitor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WWW = os.path.join(ROOT, 'www')
CHANNEL_FILE = os.path.join(ROOT, 'channel.json')
SAVED_FILE = os.path.join(ROOT, 'data', 'saved_channels.json')
SAVED_FILE_DEFAULT = SAVED_FILE
CAPTURE_DIR = os.path.join(ROOT, 'capture')
LOG_DIR = os.path.join(ROOT, 'logs')
SCAN_DIR = os.path.join(ROOT, 'data', 'scans')
CONFIG_FILE = os.path.join(ROOT, 'config.json')
SCHEDULE_FILE = os.path.join(ROOT, 'data', 'schedules.json')
ALERT_FILE = os.path.join(ROOT, 'data', 'alerts.json')
CHANNEL_LOG_DIR = os.path.join(ROOT, 'logs', 'channels')
SCAN_ID_RE = re.compile(r'[a-z]+_\d{8}-\d{6}')
SCAN_TIMEOUT_RANGE = (2, 10)      # seconds to wait for lock per frequency (auto scan)
SCAN_DWELL_MAX = 12               # max seconds to stay on a locked frequency during auto scan

KST = dt.timezone(dt.timedelta(hours=9), 'KST')
GPS_EPOCH = dt.datetime(1980, 1, 6, tzinfo=dt.timezone.utc)

MODES = {
    'terrestrial': {'label': '지상파', 'delivery': 'ATSC', 'modulations': ['8-VSB']},
    # in-house headend: the 135 cable channels are also transmitted as ATSC 8-VSB
    'catv': {'label': '케이블', 'delivery': 'ATSC', 'modulations': ['8-VSB']},
}

# ---------------------------------------------------------------------------
# WebRTC preview: tsp -P fork -> ffmpeg -> RTSP publish to mediamtx -> WebRTC

PREVIEW = {
    'enabled': True,
    'rtsp': 'rtsp://127.0.0.1:8554',   # mediamtx RTSP (publish)
    'path': 'atscscan',                # mediamtx path
    'webrtc_port': 8889,               # mediamtx WebRTC/HTTP (browser player)
    'encoder': None,                   # 'nvenc' | 'x264' | 'openh264' | 'vp8'  (chosen per session)
}

_ENC_CACHE = {}


def _ffmpeg_ok(args):
    try:
        r = subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=320x240:d=0.2',
                            '-frames:v', '2'] + args + ['-f', 'null', '-'], capture_output=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def select_encoder(nvidia):
    """config.json "nvidia": true -> NVENC if it actually works; false (or NVENC unusable) -> CPU encoder.

    CPU chain: libx264 -> libopenh264 (both H.264) -> libvpx (VP8). All are WebRTC-compatible in mediamtx.
    """
    key = 'nvidia' if nvidia else 'cpu'
    if key in _ENC_CACHE:
        return _ENC_CACHE[key]
    enc = None
    if nvidia:
        if _ffmpeg_ok(['-c:v', 'h264_nvenc']):
            enc = 'nvenc'
        else:
            print('NVENC not usable although config.json "nvidia" is true: falling back to CPU', flush=True)
    if enc is None:
        for name, args in (('x264', ['-c:v', 'libx264']), ('openh264', ['-c:v', 'libopenh264']),
                           ('vp8', ['-c:v', 'libvpx'])):
            if _ffmpeg_ok(args):
                enc = name
                break
    _ENC_CACHE[key] = enc or 'x264'
    return _ENC_CACHE[key]


def ffmpeg_cmd(encoder=None):
    """First video + first audio stream of the TS -> WebRTC-friendly video (no B-frames) + Opus."""
    enc = encoder or PREVIEW['encoder'] or 'x264'
    url = f"{PREVIEW['rtsp']}/{PREVIEW['path']}"
    v = ['ffmpeg', '-hide_banner', '-nostats', '-loglevel', 'error',
         '-fflags', '+genpts+discardcorrupt', '-probesize', '5000000', '-analyzeduration', '3000000']
    if enc == 'nvenc':
        # GPU: CUDA decode + CUDA deinterlace/scale + NVENC
        v += ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda', '-f', 'mpegts', '-i', 'pipe:0',
              '-map', '0:v:0', '-map', '0:a:0?',
              '-vf', 'yadif_cuda=0,scale_cuda=1280:720',
              '-c:v', 'h264_nvenc', '-preset', 'p4', '-tune', 'll', '-profile:v', 'main',
              '-bf', '0', '-g', '30', '-b:v', '3M', '-maxrate', '4M', '-bufsize', '2M']
    else:
        # CPU only: software decode, bwdif + 960x540, no CUDA/NVENC anywhere
        # (i7-6700K, MPEG-2 1080i: ~190% CPU vs ~400% for yadif + 720p + veryfast)
        v += ['-f', 'mpegts', '-i', 'pipe:0', '-map', '0:v:0', '-map', '0:a:0?',
              '-vf', 'bwdif=0,scale=960:540', '-pix_fmt', 'yuv420p', '-threads', '0']
        if enc == 'openh264':
            v += ['-c:v', 'libopenh264', '-g', '30', '-b:v', '2M', '-maxrate', '3M']
        elif enc == 'vp8':
            v += ['-c:v', 'libvpx', '-deadline', 'realtime', '-cpu-used', '8', '-g', '30',
                  '-b:v', '2M', '-maxrate', '3M', '-bufsize', '2M']
        else:
            v += ['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-profile:v', 'main',
                  '-bf', '0', '-g', '30', '-b:v', '2M', '-maxrate', '3M', '-bufsize', '2M']
    v += ['-c:a', 'libopus', '-b:a', '128k', '-ar', '48000', '-ac', '2',
          '-f', 'rtsp', '-rtsp_transport', 'tcp', url]
    return ' '.join(shlex.quote(x) for x in v)


def freeze_cmd(path, fz):
    """Per-frame mean absolute luma difference (0-255) of the first video stream for `analyze_sec` s -> path.

    tblend(difference) + signalstats YAVG, one value per frame; parse_freeze() smooths it and finds still runs.
    """
    vf = ("scale=480:-2,format=gray,tblend=all_mode=difference,signalstats,"
          f"metadata=mode=print:key=lavfi.signalstats.YAVG:file={path}")
    v = ['ffmpeg', '-hide_banner', '-nostats', '-loglevel', 'error', '-fflags', '+discardcorrupt',
         '-f', 'mpegts', '-probesize', '5000000', '-analyzeduration', '3000000', '-i', 'pipe:0',
         '-map', '0:v:0', '-an', '-t', str(fz.get('analyze_sec', 6)), '-vf', vf, '-f', 'null', '-']
    # the fork plugin runs the command through /bin/sh: mark completion with a .done file
    return ' '.join(shlex.quote(x) for x in v) + f'; echo done > {shlex.quote(path + ".done")}'


def parse_freeze(path, fz):
    """Find the longest 'still' run. Returns {'frozen', 'duration', 'start', 'window', 'to_end', 'diff', 'threshold'}.

    A headend encoder re-encoding a frozen picture produces small difference pulses on every GOP; a rolling median
    over `smooth_frames` frames removes them (measured: frozen ~0.26, normal video >= 3 on a 0-255 scale).
    """
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        return None
    ts, vals, t = [], [], None
    for line in text.splitlines():
        if line.startswith('frame:'):
            m = re.search(r'pts_time:([0-9.]+)', line)
            t = float(m.group(1)) if m else None
        elif line.startswith('lavfi.signalstats.YAVG=') and t is not None:
            ts.append(t)
            vals.append(float(line.split('=', 1)[1]))
    window = float(fz.get('analyze_sec', 6))
    if len(vals) < 5:
        return None
    w = max(1, int(fz.get('smooth_frames', 9)))
    h = w // 2
    smooth = [statistics.median(vals[max(0, i - h):i + h + 1]) for i in range(len(vals))]
    thr = float(fz.get('diff_threshold', 1.0))
    frame = (ts[-1] - ts[0]) / max(1, len(ts) - 1)
    best = (0.0, None, False)          # (duration, start, reaches_end)
    run_start = None
    for i, x in enumerate(smooth):
        if x < thr:
            if run_start is None:
                run_start = i
            dur = ts[i] - ts[run_start] + frame
            if dur > best[0]:
                best = (dur, ts[run_start] - ts[0], i == len(smooth) - 1)
        else:
            run_start = None
    seen = ts[-1] - ts[0] + frame
    # with a short window (e.g. 2 s lock wait) the run cannot be longer than the window itself
    need = min(float(fz.get('min_sec', 2.0)), 0.9 * min(window, seen))
    return {'frozen': best[0] >= need and best[0] > 0, 'duration': round(best[0], 1),
            'start': None if best[1] is None else round(best[1], 1), 'window': round(seen, 1),
            'to_end': best[2], 'diff': round(statistics.median(vals), 2), 'threshold': thr,
            'need': round(need, 1)}


def capture_cmd(path):
    """Decode only key frames and save the first one (1920x1080, deinterlaced) as JPEG."""
    # level+info: the input stream line gives the real frame rate (see video_rate_from_ffmpeg);
    # the other [info] lines are dropped by Session._read_tsp.
    v = ['ffmpeg', '-hide_banner', '-nostats', '-loglevel', 'level+info', '-skip_frame', 'nokey',
         '-f', 'mpegts', '-probesize', '5000000', '-analyzeduration', '3000000', '-i', 'pipe:0',
         '-map', '0:v:0', '-vf', 'yadif=0', '-frames:v', '1', '-update', '1', '-q:v', '3', '-y', path]
    return ' '.join(shlex.quote(x) for x in v)


_FFMPEG_INFO = re.compile(r'\[(info|verbose|debug)\] ')
_FFMPEG_LEVEL = re.compile(r'\[(error|warning|fatal|panic)\] ')
_FFMPEG_VIDEO = re.compile(r'Stream #\d+:\d+\[0x([0-9a-fA-F]+)\]: Video: (\w+).*?, ([\d.]+) fps')


def video_rate_from_ffmpeg(line):
    """'Stream #0:0[0x11]: Video: mpeg2video ..., 29.97 fps, ...' -> (0x11, '29.97')."""
    m = _FFMPEG_VIDEO.search(line)
    return (int(m.group(1), 16), m.group(3)) if m else None


def fix_video_description(desc, fps):
    """TSDuck 3.44 labels every MPEG-2 video '@25 Hz' whatever its frame_rate_code
    (checked with 23.976/25/29.97/30/59.94 test streams), so use the rate ffmpeg decoded."""
    if not desc:
        return desc
    if fps:
        return re.sub(r'@[\d.,]+ Hz', '@%s Hz' % fps, desc)
    if desc.startswith('MPEG-2 Video'):
        return re.sub(r', @[\d.,]+ Hz', '', desc)
    return desc


class ReceptionLog:
    """Append-only reception log: logs/reception.log (text) + logs/reception.jsonl (one JSON per line)."""

    def __init__(self, directory):
        self.dir = directory
        self.lock = threading.Lock()
        os.makedirs(directory, exist_ok=True)

    def write(self, e):
        with self.lock:
            with open(os.path.join(self.dir, 'reception.jsonl'), 'a', encoding='utf-8') as f:
                f.write(json.dumps(e, ensure_ascii=False) + '\n')
            v = lambda k, fmt='{}': '-' if e.get(k) is None else fmt.format(e[k])
            snr = '-' if e.get('snr_avg') is None else \
                f"{e['snr_avg']:.1f}dB(min {e['snr_min']:.1f}, last {v('snr_last', '{:.1f}')})"
            line = (f"{e['time']}  {e['source']:<6} {e['mode']:<11} CH{e['channel']:>3} {e['freq_mhz']:>6.1f}MHz "
                    f"{e['modulation']:<6} {e['result'].upper():<7} SNR={snr} STR={v('strength_pct', '{}%')} "
                    f"UCB={v('ucb')} rate={v('bitrate')} TSID={v('tsid')} "
                    f"VCH={','.join(e.get('virtual_channels') or []) or '-'} "
                    f"lock={v('lock_time_s', '{}s')} dur={e['duration_s']}s cap={e.get('capture') or '-'}")
            with open(os.path.join(self.dir, 'reception.log'), 'a', encoding='utf-8') as f:
                f.write(line + '\n')

    def tail(self, n=200):
        path = os.path.join(self.dir, 'reception.jsonl')
        try:
            with open(path, encoding='utf-8') as f:
                lines = deque(f, maxlen=n)
        except FileNotFoundError:
            return []
        out = []
        for l in lines:
            try:
                out.append(json.loads(l))
            except ValueError:
                pass
        return out[::-1]


# ---------------------------------------------------------------------------
# DVB device selection (statistics reader: dvbdev.py)

BASE_MODES = json.loads(json.dumps(MODES))
_MODES_SIG = None


def sync_modes(cfg):
    """Apply config.json "modes" overrides (delivery / modulations / label) to MODES in place."""
    global _MODES_SIG
    ov = cfg.get('modes') or {}
    sig = json.dumps(ov, sort_keys=True)
    if sig == _MODES_SIG:
        return
    _MODES_SIG = sig
    for k, base in BASE_MODES.items():
        m = dict(base)
        o = ov.get(k) or {}
        if o.get('delivery') in dvbdev.TSDUCK_DELSYS:
            m['delivery'] = o['delivery']
        if isinstance(o.get('modulations'), list) and o['modulations']:
            m['modulations'] = [str(x) for x in o['modulations']]
        if o.get('label'):
            m['label'] = str(o['label'])
        MODES[k] = m


def device_key(dev):
    """Identity of the card a measurement was made with (baselines are kept per card)."""
    if not dev:
        return None
    return f"{dev.get('usb_id') or '?'}|{dev.get('name') or '?'}"


_DEV_CACHE = {}


def pick_device(cfg, delivery):
    """Frontend for `delivery` per config.json "adapter"/"frontend" ("auto" or a number); cached 10 s."""
    key = (str(cfg.get('adapter', 'auto')), str(cfg.get('frontend', 'auto')), delivery)
    c = _DEV_CACHE.get(key)
    if c and time.time() - c[0] < 10:
        return c[1]
    dev = dvbdev.select(cfg.get('adapter', 'auto'), cfg.get('frontend', 'auto'), delivery)
    _DEV_CACHE[key] = (time.time(), dev)
    return dev


def device_info(cfg, delivery='ATSC'):
    dev = pick_device(cfg, delivery)
    label = cfg.get('device_label', '')
    if not dev:
        return {'name': label or '수신기 없음', 'present': False}
    return {'present': True, 'name': label or dev.get('usb') or dev.get('name'), 'frontend': dev.get('name'),
            'path': f"/dev/dvb/adapter{dev['adapter']}/frontend{dev['frontend']}", 'usb_id': dev.get('usb_id'),
            'freq_min_mhz': dev.get('freq_min_mhz'), 'freq_max_mhz': dev.get('freq_max_mhz'),
            'delsys': dev.get('delsys'), 'key': device_key(dev)}


# ---------------------------------------------------------------------------
# PSIP / PSI model built from TSDuck JSON tables

def _text(node, child=None):
    """Return first 'string' text under a node (optionally under named child)."""
    if node is None:
        return ''
    if child:
        node = next((n for n in node.get('#nodes', []) if n.get('#name') == child), None)
        if node is None:
            return ''
    for n in node.get('#nodes', []):
        if n.get('#name') == 'string':
            return n.get('text', '')
        t = _text(n)
        if t:
            return t
    return ''


def _children(node, name):
    return [n for n in node.get('#nodes', []) if n.get('#name') == name]


def _utc_str_to_kst(s):
    try:
        t = dt.datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=dt.timezone.utc)
        return t.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return s


STREAM_TYPES = {
    0x02: 'MPEG-2 Video', 0x1b: 'H.264 Video', 0x24: 'HEVC Video', 0x81: 'AC-3 Audio',
    0x87: 'E-AC-3 Audio', 0x0f: 'AAC Audio', 0x03: 'MPEG-1 Audio', 0x04: 'MPEG-2 Audio',
    0x05: 'Private sections', 0x06: 'PES private', 0x0b: 'DSM-CC', 0x0d: 'DSM-CC',
}


class Psip:
    def __init__(self):
        self.lock = threading.Lock()
        self.rev = 0
        self.tables = {}          # singleton tables by name: MGT, TVCT, CVCT, STT, RRT, PAT
        self.pmts = {}            # service_id -> PMT json
        self.eits = {}            # (source_id, event_id) -> event dict
        self.etts = {}            # etm_id -> text
        self.counts = {}

    def add(self, j):
        name = j.get('#name', '?')
        with self.lock:
            self.counts[name] = self.counts.get(name, 0) + 1
            meta = next((n for n in j.get('#nodes', []) if n.get('#name') == 'metadata'), {})
            pid = meta.get('pid')
            if name == 'PMT':
                self.pmts[j.get('service_id')] = j
            elif name in ('ATSC_EIT', 'EIT'):
                src = j.get('source_id')
                for ev in _children(j, 'event'):
                    key = (src, ev.get('event_id'))
                    self.eits[key] = {
                        'source_id': src, 'event_id': ev.get('event_id'), 'pid': pid,
                        'start_utc': ev.get('start_time'),
                        'start': _utc_str_to_kst(ev.get('start_time')),
                        'duration': ev.get('length_in_seconds'),
                        'title': _text(ev, 'title_text'),
                        'etm_location': ev.get('etm_location'),
                    }
            elif name == 'ETT':
                self.etts[j.get('etm_id')] = _text(j, 'extended_text_message')
            else:
                j = dict(j)
                j['_pid'] = pid
                self.tables[name] = j
            self.rev += 1

    def model(self):
        with self.lock:
            t = self.tables
            out = {'rev': self.rev, 'counts': dict(self.counts)}

            mgt = t.get('MGT')
            if mgt:
                out['mgt'] = {
                    'version': mgt.get('version'), 'protocol_version': mgt.get('protocol_version'),
                    'tables': [{'type': n.get('type'), 'pid': n.get('pid'),
                                'version': n.get('version_number'), 'bytes': n.get('number_bytes')}
                               for n in _children(mgt, 'table')],
                }

            vct = t.get('TVCT') or t.get('CVCT')
            if vct:
                chans = []
                for c in _children(vct, 'channel'):
                    sld = next(iter(_children(c, 'service_location_descriptor')), {})
                    comps = [{'pid': x.get('elementary_pid'), 'stream_type': x.get('stream_type'),
                              'type': STREAM_TYPES.get(x.get('stream_type'), f"0x{x.get('stream_type', 0):02X}"),
                              'lang': x.get('iso_639_language_code', '')}
                             for x in _children(sld, 'component')]
                    chans.append({
                        'virtual': f"{c.get('major_channel_number')}-{c.get('minor_channel_number')}",
                        'short_name': (c.get('short_name') or '').strip(),
                        'long_name': _text(c, 'extended_channel_name_descriptor'),
                        'program': c.get('program_number'), 'source_id': c.get('source_id'),
                        'tsid': c.get('channel_tsid'), 'service_type': c.get('service_type'),
                        'modulation': c.get('modulation_mode'), 'access_controlled': c.get('access_controlled'),
                        'hidden': c.get('hidden'), 'pcr_pid': sld.get('pcr_pid'), 'components': comps,
                    })
                out['vct'] = {'kind': vct.get('#name'), 'tsid': vct.get('transport_stream_id'),
                              'version': vct.get('version'), 'channels': chans}

            stt = t.get('STT')
            if stt and stt.get('system_time') is not None:
                utc = GPS_EPOCH + dt.timedelta(seconds=stt['system_time'] - (stt.get('gps_utc_offset') or 0))
                out['stt'] = {'utc': utc.strftime('%Y-%m-%d %H:%M:%S'),
                              'kst': utc.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S'),
                              'gps_utc_offset': stt.get('gps_utc_offset'),
                              'ds_status': stt.get('ds_status'),
                              'system_time': stt.get('system_time')}

            rrt = t.get('RRT')
            if rrt:
                dims = []
                for d in _children(rrt, 'dimension'):
                    dims.append({'name': _text(d, 'dimension_name'),
                                 'values': [_text(v, 'abbrev_rating_value') or _text(v, 'rating_value')
                                            for v in _children(d, 'value')]})
                out['rrt'] = {'region': rrt.get('rating_region'),
                              'region_name': _text(rrt, 'rating_region_name'),
                              'version': rrt.get('version'), 'dimensions': dims}

            pat = t.get('PAT')
            if pat:
                out['pat'] = {'tsid': pat.get('transport_stream_id'), 'version': pat.get('version'),
                              'programs': [{'program': s.get('service_id'), 'pmt_pid': s.get('program_map_pid')}
                                           for s in _children(pat, 'service')]}
            if self.pmts:
                pm = []
                for sid, p in sorted(self.pmts.items(), key=lambda x: x[0] or 0):
                    pm.append({'program': sid, 'pcr_pid': p.get('pcr_pid'), 'version': p.get('version'),
                               'streams': [{'pid': c.get('elementary_pid'), 'stream_type': c.get('stream_type'),
                                            'type': STREAM_TYPES.get(c.get('stream_type'), f"0x{c.get('stream_type', 0):02X}"),
                                            'name': _text(c, 'component_name_descriptor'),
                                            'descriptors': [n.get('#name') for n in c.get('#nodes', [])]}
                                           for c in _children(p, 'component')]})
                out['pmt'] = pm

            if self.eits:
                names = {}
                if vct:
                    for c in out['vct']['channels']:
                        names[c['source_id']] = f"{c['virtual']} {c['short_name']}"
                evs = []
                for (src, eid), e in self.eits.items():
                    e = dict(e)
                    e['channel'] = names.get(src, f'source {src}')
                    if src is not None and eid is not None:
                        e['description'] = self.etts.get((src << 16) | (eid << 2) | 0x02, '')
                    evs.append(e)
                evs.sort(key=lambda e: (e['channel'], e.get('start_utc') or ''))
                out['eit'] = evs
            return out


# ---------------------------------------------------------------------------
# Tuning session: tsp process + frontend poller

class Session:
    def __init__(self, adapter, mode, channel, freq_mhz, modulation, on_done,
                 signal_timeout=6, source='manual', capture=True, freeze=None, device=None):
        self.adapter = adapter
        self.device = device or {'adapter': adapter, 'frontend': 0, 'name': ''}
        self.freeze_cfg = freeze            # dict from config.json "freeze" -> analyse video (auto scans)
        self.freeze_path = None
        self.signal_timeout = signal_timeout
        self.source = source            # 'manual' | 'scan' | 'schedule'
        self.scan_id = None
        self.capture_name = None
        self.capture_path = None
        if capture:
            stamp = time.strftime('%Y%m%d-%H%M%S')
            self.capture_name = f'{mode}_ch{channel:03d}_{freq_mhz:g}MHz_{stamp}.jpg'
            self.capture_path = os.path.join(CAPTURE_DIR, self.capture_name)
        if freeze:
            os.makedirs(os.path.join(LOG_DIR, 'tmp'), exist_ok=True)
            self.freeze_path = os.path.join(LOG_DIR, 'tmp', f'freeze_{mode}_{channel}_{os.getpid()}_{id(self)}.txt')
        self.mode = mode
        self.channel = channel
        self.freq_mhz = freq_mhz
        self.modulation = modulation
        self.on_done = on_done
        self.state = 'tuning'
        self.message = '튜닝 중…'
        self.started = time.time()
        self.locked_at = None
        self.log = deque(maxlen=60)
        self.fe = None
        self.fe_hist = deque(maxlen=120)
        self.ucb_prev = None
        self.ucb_rate = None
        self.dvbstatus = None
        self.analyze = None
        self.video_fps = {}         # PID -> frame rate from the capture ffmpeg
        self.psip = Psip()
        self.proc = None
        self.stopping = False
        self._threads = []

    def cmd(self):
        delivery = MODES[self.mode]['delivery']
        return [
            'tsp', '--realtime',
            '-I', 'dvb', '--device-name', dvbdev.tsp_device(self.device),
            '--delivery-system', delivery,
            '--frequency', str(int(self.freq_mhz * 1_000_000)),
            '--modulation', self.modulation,
            '--signal-timeout', str(self.signal_timeout), '--receive-timeout', '5000',
            '--json-line=DVBSTATUS:', '--json-interval', '2',
            # no PID filter: EIT/ETT PIDs are announced by the MGT and differ per
            # broadcaster (KBS 0x1D00.., SBS 0x1A00..); sections are CRC-checked
            '-P', 'tables', '--log-json-line=TABLE:',
            '-P', 'analyze', '--interval', '3', '--json-line=ANALYZE:',
        ] + ([
            # one JPEG of the first service once data flows (i.e. after lock); ffmpeg exits by itself
            '-P', 'fork', '--ignore-abort', '--buffered-packets', '50', capture_cmd(self.capture_path),
        ] if self.capture_path else []) + ([
            # frozen-picture analysis of the first service (auto scans only)
            '-P', 'fork', '--ignore-abort', '--buffered-packets', '50', freeze_cmd(self.freeze_path, self.freeze_cfg),
        ] if self.freeze_path else []) + ([
            # WebRTC preview of the first service; --ignore-abort keeps the analysis
            # running even if ffmpeg/mediamtx fails
            '-P', 'fork', '--ignore-abort', '--buffered-packets', '100', ffmpeg_cmd(),
        ] if PREVIEW['enabled'] else []) + [
            '-O', 'drop',
        ]

    def captured(self):
        p = self.capture_path
        return bool(p and os.path.exists(p) and os.path.getsize(p) > 0)

    def freeze_done(self):
        return bool(self.freeze_path and os.path.exists(self.freeze_path + '.done'))

    def freeze_result(self):
        if not self.freeze_path:
            return None
        if not hasattr(self, '_freeze'):
            self._freeze = parse_freeze(self.freeze_path, self.freeze_cfg) if self.captured() else None
            if self.freeze_done() or self.proc.poll() is not None:
                for p in (self.freeze_path, self.freeze_path + '.done'):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        return self._freeze

    def error_rates(self):
        """BER from TSDuck if the driver reports one; TEI packet ratio from the TSDuck analysis."""
        ds = self.dvbstatus or {}
        ber = next((ds[k] for k in ('bit-error-rate', 'ber', 'post-ber', 'pre-ber') if k in ds), None)
        pk = ((self.analyze or {}).get('ts') or {}).get('packets') or {}
        tot, tei = pk.get('total'), pk.get('transport-errors')
        return {'ber': ber, 'tei': tei, 'per': (tei / tot) if tot and tei is not None else None}

    def has_identity(self):
        m = self.psip.model()
        return bool(m.get('vct') or m.get('pat'))

    def log_entry(self):
        # the demod reports 0 dB right after lock until its MSE is valid: ignore those samples
        locked = [h['snr'] for h in self.fe_hist if h.get('lock') and h.get('snr')]
        rfs = [h['rf'] for h in self.fe_hist if h.get('lock') and h.get('rf') is not None]
        bers = [h['ber'] for h in self.fe_hist if h.get('lock') and h.get('ber') is not None]
        fe = self.fe or {}
        dev = self.device
        a = (self.analyze or {}).get('ts') or {}
        m = self.psip.model()
        vct = (m.get('vct') or {}).get('channels', [])
        return {
            'time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started)),
            'source': self.source, 'mode': self.mode, 'channel': self.channel,
            'freq_mhz': self.freq_mhz, 'modulation': self.modulation,
            # 'weak': lock was reported but no PSIP / picture could be received (marginal signal)
            'result': ('locked' if (self.has_identity() or self.captured()) else 'weak') if self.locked_at
                      else ('error' if self.state == 'error' else 'nolock'),
            'lock_time_s': round(self.locked_at - self.started, 1) if self.locked_at else None,
            'duration_s': round(time.time() - self.started, 1),
            'snr_last': fe.get('snr_db') if self.locked_at else None,
            'snr_avg': round(sum(locked) / len(locked), 1) if locked else None,
            'snr_min': min(locked) if locked else None,
            'strength_pct': fe.get('strength_pct') if self.locked_at else None,
            'ucb': fe.get('ucb') if self.locked_at else None,
            'bitrate': a.get('bitrate'),
            'packets': (a.get('packets') or {}).get('total'),
            'tsid': (m.get('vct') or {}).get('tsid') or (m.get('pat') or {}).get('tsid'),
            'virtual_channels': [f"{c['virtual']} {c['short_name']}" for c in vct],
            'capture': self.capture_name if self.captured() else None,
            'ber': (sum(bers) / len(bers)) if bers else self.error_rates()['ber'],
            'ber_src': fe.get('ber_src') if bers else ('tsduck' if self.error_rates()['ber'] is not None else None),
            'rf_dbm_avg': round(sum(rfs) / len(rfs), 1) if rfs else None,
            'rf_dbm_min': min(rfs) if rfs else None,
            'snr_src': fe.get('snr_src'),
            'device': device_key(dev),
            'tei': self.error_rates()['tei'],
            'per': None if self.error_rates()['per'] is None else round(self.error_rates()['per'], 6),
            'freeze': self.freeze_result() if self.locked_at else None,
            'message': self.message,
        }

    def start(self):
        if self.capture_path:
            os.makedirs(CAPTURE_DIR, exist_ok=True)
        c = self.cmd()
        self.log.append('$ ' + ' '.join(c))
        self.proc = subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     bufsize=1, text=True, encoding='utf-8', errors='replace',
                                     start_new_session=True)
        for fn in (self._read_tsp, self._poll_fe):
            th = threading.Thread(target=fn, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self, wait=True):
        self.stopping = True
        p = self.proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            if wait:
                try:
                    p.wait(timeout=2)       # tsp blocked on a dead input ignores SIGINT
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
        for th in self._threads:
            if th is not threading.current_thread():
                th.join(timeout=3)

    def _read_tsp(self):
        for line in self.proc.stderr:
            line = line.rstrip('\n')
            for key in ('DVBSTATUS:', 'TABLE:', 'ANALYZE:'):
                i = line.find(key)
                if i >= 0:
                    try:
                        j = json.loads(line[i + len(key):])
                    except json.JSONDecodeError:
                        break
                    if key == 'TABLE:':
                        self.psip.add(j)
                    elif key == 'ANALYZE:':
                        self.analyze = j
                    else:
                        self.dvbstatus = j
                        if j.get('signal-locked') and self.state == 'tuning':
                            self.state = 'locked'
                            self.locked_at = time.time()
                            self.message = 'Lock — TS 분석 중'
                    break
            else:
                if _FFMPEG_INFO.search(line):
                    r = video_rate_from_ffmpeg(line)
                    if r:
                        self.video_fps[r[0]] = r[1]
                    continue
                line = _FFMPEG_LEVEL.sub('', line, count=1)
                if line.strip():
                    is_tsp = line.startswith('* ')
                    self.log.append(line if is_tsp or line.startswith('$') else '[ffmpeg] ' + line)
                    if not is_tsp:
                        continue
                    low = line.lower()
                    if 'no input signal lock' in low or 'signal not locked' in low:
                        self.state = 'nolock'
                        self.message = '신호 lock 실패'
                    elif 'error' in low and self.state == 'tuning':
                        self.message = line.strip()
        rc = self.proc.wait()
        if self.stopping:
            if self.state in ('tuning', 'locked'):
                self.state = 'stopped'
                self.message = '중지됨'
        elif self.state == 'locked':
            self.state = 'ended'
            self.message = f'수신 종료 (tsp exit {rc})'
        elif self.state == 'tuning':
            self.state = 'nolock' if rc == 0 else 'error'
            self.message = '신호 lock 실패' if rc == 0 else f'tsp 오류 (exit {rc})'
        self.on_done(self)

    def _poll_fe(self):
        fe = dvbdev.Frontend(self.device['adapter'], self.device['frontend'], self.device.get('name', ''))
        # wait until tsp has opened/tuned the frontend
        time.sleep(0.5)
        try:
            fe.open()
        except OSError as e:
            self.log.append(f'frontend open failed: {e}')
            return
        try:
            while self.proc.poll() is None and not self.stopping:
                r = fe.read()
                if r['ucb'] is not None and not r['locked']:
                    self.ucb_prev = None          # counter is meaningless before lock
                elif r['ucb'] is not None:
                    if self.ucb_prev is not None:
                        dtm = r['t'] - self.ucb_prev[0]
                        d = (r['ucb'] - self.ucb_prev[1]) % 65536
                        self.ucb_rate = round(d / dtm, 1) if dtm > 0 else None
                        r['ucb_delta'] = d
                    self.ucb_prev = (r['t'], r['ucb'])
                r['ucb_rate'] = self.ucb_rate
                r['strength_pct'] = r.get('quality_pct')        # UI / log field name kept
                self.fe = r
                self.fe_hist.append({'t': round(r['t'] - self.started, 1), 'snr': r['snr_db'], 'lock': r['locked'],
                                     'rf': r.get('signal_dbm'), 'ber': r.get('ber')})
                time.sleep(1.0)
        finally:
            fe.close()

    def summary(self):
        a = self.analyze or {}
        ts = a.get('ts') or {}
        pk = ts.get('packets') or {}
        svc = a.get('services') if isinstance(a.get('services'), list) else []
        pids = a.get('pids') if isinstance(a.get('pids'), list) else []
        return {
            'mode': self.mode, 'channel': self.channel, 'freq_mhz': self.freq_mhz,
            'modulation': self.modulation, 'state': self.state, 'message': self.message,
            'preview': {k: PREVIEW[k] for k in ('enabled', 'path', 'webrtc_port', 'encoder')},
            'started': self.started, 'source': self.source,
            'signal_timeout': self.signal_timeout,
            'capture': self.capture_name if self.captured() else None,
            'errors': self.error_rates(),
            'freeze_analyzing': bool(self.freeze_path and not self.freeze_done()),
            'elapsed': round(time.time() - self.started, 1),
            'frontend': self.fe, 'fe_hist': list(self.fe_hist),
            'dvbstatus': self.dvbstatus,
            'ts': {
                'id': ts.get('id'), 'bitrate': ts.get('bitrate'), 'pcr_bitrate': ts.get('pcr-bitrate'),
                'packets': pk.get('total'), 'invalid_syncs': pk.get('invalid-syncs'),
                'transport_errors': pk.get('transport-errors'),
                'services': [{'id': s.get('id'), 'name': s.get('name'), 'provider': s.get('provider'),
                              'bitrate': s.get('bitrate'), 'pmt_pid': s.get('pmt-pid'),
                              'pcr_pid': s.get('pcr-pid'), 'pids': s.get('pids')} for s in svc],
                'pids': [{'pid': p.get('id'),
                          'description': fix_video_description(p.get('description'),
                                                               self.video_fps.get(p.get('id'))),
                          'bitrate': p.get('bitrate'),
                          'packets': (p.get('packets') or {}).get('total'),
                          'discontinuities': (p.get('packets') or {}).get('discontinuities'),
                          'scrambled': p.get('is-scrambled')} for p in pids],
            } if a else None,
            'psip_rev': self.psip.rev,
            'log': list(self.log)[-15:],
        }


class SavedStore:
    """Locked channels persisted to data/saved_channels.json (survives restarts)."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.items = {}     # "mode:channel" -> record
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            self.items = {f"{r['mode']}:{r['channel']}": r for r in data.get('channels', [])}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError) as e:
            print(f'warning: cannot load {path}: {e}', flush=True)

    def _write(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        recs = sorted(self.items.values(), key=lambda r: (r['mode'], r['freq_mhz']))
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'channels': recs}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def upsert(self, rec):
        key = f"{rec['mode']}:{rec['channel']}"
        with self.lock:
            old = self.items.get(key)
            rec['first_locked'] = (old or {}).get('first_locked') or rec['last_locked']
            if not rec.get('capture'):
                rec['capture'] = (old or {}).get('capture')
            # only rewrite the file when something other than timestamps / snr changed,
            # or once a minute to refresh last_locked
            same = old and {k: v for k, v in old.items() if k not in ('last_locked', 'snr_db', 'first_locked')} == \
                {k: v for k, v in rec.items() if k not in ('last_locked', 'snr_db', 'first_locked')}
            self.items[key] = rec
            if not same or rec['last_locked'] - (old or {}).get('last_locked', 0) > 60:
                self._write()

    def delete(self, mode, channel):
        with self.lock:
            n = 1 if self.items.pop(f'{mode}:{channel}', None) else 0
            if n:
                self._write()
            return n

    def clear(self, mode=None):
        with self.lock:
            keys = [k for k, r in self.items.items() if mode is None or r['mode'] == mode]
            for k in keys:
                del self.items[k]
            self._write()
            return len(keys)

    def snapshot(self):
        with self.lock:
            return dict(self.items)


def _record(s, model):
    vct = model.get('vct') or {}
    return {
        'mode': s.mode, 'channel': s.channel, 'freq_mhz': s.freq_mhz, 'modulation': s.modulation,
        'tsid': vct.get('tsid') if vct else (model.get('pat') or {}).get('tsid'),
        'vct_kind': vct.get('kind'),
        'channels': [{k: c.get(k) for k in ('virtual', 'short_name', 'long_name', 'program',
                                              'source_id', 'service_type', 'access_controlled')}
                     for c in vct.get('channels', [])],
        'snr_db': (s.fe or {}).get('snr_db'),
        'last_locked': round(time.time()),
        'capture': s.capture_name if s.captured() else None,
    }


EXPORT_COLUMNS = [
    ('mode', '모드'), ('channel', '채널'), ('freq_mhz', '주파수(MHz)'), ('modulation', '변조'),
    ('result', '결과'), ('lock_time_s', 'lock 시간(s)'), ('snr_avg', 'SNR 평균(dB)'), ('snr_min', 'SNR 최소(dB)'),
    ('snr_last', 'SNR 마지막(dB)'), ('strength_pct', '신호 품질(%)'), ('ucb', 'UCB'), ('bitrate', '비트레이트(bps)'),
    ('rf_dbm_avg', 'RF 레벨(dBm)'), ('packets', '패킷 수'), ('tei', 'TEI 패킷'), ('ber', 'BER'), ('tsid', 'TSID'), ('virtual_channels', '가상채널'),
    ('freeze', '영상 정지(s)'), ('capture', '캡처'),
    ('time', '측정 시각'), ('duration_s', '측정 시간(s)'), ('source', '구분'),
]


def scan_to_csv(sc):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"# scan {sc.get('id')}", f"mode={sc.get('mode')}", f"timeout={sc.get('timeout')}s",
                f"range=CH{sc.get('from')}~CH{sc.get('to')}", f"locked={sc.get('locked')}/{len(sc.get('results', []))}",
                'cancelled' if sc.get('cancelled') else 'completed'])
    w.writerow([h for _, h in EXPORT_COLUMNS])
    for e in sc.get('results', []):
        row = []
        for k, _ in EXPORT_COLUMNS:
            v = e.get(k)
            if isinstance(v, list):
                v = ' / '.join(v)
            elif k == 'freeze':
                v = '' if not v else (v['duration'] if v.get('frozen') else 0)
            row.append('' if v is None else v)
        w.writerow(row)
    return '\ufeff' + buf.getvalue()          # BOM so Excel opens UTF-8 Korean correctly


class ScanStore:
    """Finished auto scans persisted as data/scans/<id>.json."""

    def __init__(self, directory):
        self.dir = directory

    def save(self, sc):
        os.makedirs(self.dir, exist_ok=True)
        tmp = os.path.join(self.dir, sc['id'] + '.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(sc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(self.dir, sc['id'] + '.json'))

    def get(self, sid):
        if not SCAN_ID_RE.fullmatch(sid or ''):
            return None
        try:
            with open(os.path.join(self.dir, sid + '.json'), encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def list(self):
        out = []
        try:
            names = sorted(os.listdir(self.dir), reverse=True)
        except FileNotFoundError:
            return out
        for n in names:
            if not n.endswith('.json'):
                continue
            sc = self.get(n[:-5])
            if sc:
                out.append({k: sc.get(k) for k in ('id', 'mode', 'timeout', 'from', 'to', 'total', 'locked',
                                                     'started', 'finished', 'cancelled')}
                           | {'done': len(sc.get('results', []))})
        return out

    def delete(self, sid):
        if not SCAN_ID_RE.fullmatch(sid or ''):
            return False
        try:
            os.remove(os.path.join(self.dir, sid + '.json'))
            return True
        except OSError:
            return False


class Controller:
    def __init__(self, adapter, saved_path, log_dir=None, scans_dir=None, scheduler=True):
        log_dir = log_dir or LOG_DIR
        scans_dir = scans_dir or SCAN_DIR
        self.adapter = adapter
        self.lock = threading.Lock()
        self.session = None
        self.rlog = ReceptionLog(log_dir)
        self.res_lock = threading.Lock()
        self.scan = None            # current / last auto scan state (dict)
        self.scans = ScanStore(scans_dir)
        self.config = monitor.Config(CONFIG_FILE)
        self.history = monitor.ChannelHistory(CHANNEL_LOG_DIR)
        n = self.history.import_reception_log(os.path.join(log_dir, 'reception.jsonl'))
        if n:
            print(f'imported {n} measurements from reception.jsonl into {CHANNEL_LOG_DIR}', flush=True)
        self.alerts = monitor.AlertStore(ALERT_FILE)
        self.telegram = monitor.Telegram(self.config)
        self.schedules = monitor.ScheduleStore(SCHEDULE_FILE)
        self.scheduler = monitor.Scheduler(self.schedules, self.run_scheduled, enabled=scheduler)
        self._scan_stop = threading.Event()
        self._scan_thread = None
        self.results = {}   # in-memory results of this run ("mode:channel" -> {state, ...})
        self.saved = SavedStore(saved_path)
        self.suppress = set()   # keys deleted by the user while still tuned

    def _save_if_locked(self, s):
        key = f'{s.mode}:{s.channel}'
        if key in self.suppress or not s.locked_at:
            return
        m = s.psip.model()
        # save once the channel identity is known (VCT, or at least PAT)
        if m.get('vct') or m.get('pat'):
            self.saved.upsert(_record(s, m))

    def _done(self, s):
        self._save_if_locked(s)
        key = f'{s.mode}:{s.channel}'
        e = s.log_entry()
        a = self.config.get()['alerts']
        e['scan_id'] = s.scan_id
        e['baseline'] = self.history.baseline(s.mode, s.channel, a['baseline_count'], a['min_baseline'],
                                              device=e.get('device'))
        try:
            self.history.append(e)
        except OSError as err:
            print(f'warning: cannot write channel log: {err}', flush=True)
        if s.source == 'manual' and a.get('alert_on_manual'):
            self._check_alerts(e, [], [])
        with self.res_lock:
            s.finalized = True
            self.results[key] = {'state': e['result'], 't': time.time(),
                                 'frozen': bool((e.get('freeze') or {}).get('frozen'))}
        try:
            self.rlog.write(e)
        except OSError as err:
            print(f'warning: cannot write reception log: {err}', flush=True)
        s.log_entry_written = e

    def _tune(self, mode, channel, freq_mhz, modulation, **kw):
        with self.lock:
            if self.session:
                self.session.stop()
            self.suppress.discard(f'{mode}:{channel}')
            cfg = self.config.get()
            if PREVIEW['enabled']:
                PREVIEW['encoder'] = select_encoder(bool(cfg.get('nvidia', True)))
            dev = pick_device(cfg, MODES[mode]['delivery'])
            s = Session(self.adapter, mode, channel, freq_mhz, modulation, self._done, device=dev, **kw)
            self.session = s
            s.start()
            return s

    def tune(self, mode, channel, freq_mhz, modulation):
        self.stop_scan()
        return self._tune(mode, channel, freq_mhz, modulation)

    def stop(self):
        self.stop_scan()
        with self.lock:
            if self.session:
                self.session.stop()

    # ---- auto scan ----
    def start_scan(self, mode, timeout, channels, source='scan', schedule=None, only_locked=False):
        self.stop_scan()
        self._scan_stop.clear()
        self.scan = {'id': f"{mode}_{time.strftime('%Y%m%d-%H%M%S')}",
                     'source': source, 'schedule_id': (schedule or {}).get('id'),
                     'schedule_name': (schedule or {}).get('name'),
                     'new_alerts': 0, 'recovered': 0, 'frozen': 0, 'only_locked': only_locked,
                     'from': channels[0]['channel'] if channels else None,
                     'to': channels[-1]['channel'] if channels else None,
                     'running': True, 'mode': mode, 'timeout': timeout, 'total': len(channels),
                     'index': 0, 'current': None, 'started': time.time(), 'finished': None,
                     'locked': 0, 'results': []}
        self._scan_thread = threading.Thread(target=self._scan_run, args=(mode, timeout, channels, source),
                                             daemon=True)
        self._scan_thread.start()

    def stop_scan(self):
        th = self._scan_thread
        if th and th.is_alive():
            self._scan_stop.set()
            th.join(timeout=20)

    def _scan_run(self, mode, timeout, channels, source='scan'):
        sc = self.scan
        mod = MODES[mode]['modulations'][0]
        new_alerts, recovered = [], []
        try:
            for i, c in enumerate(channels):
                if self._scan_stop.is_set():
                    break
                ch, f = c['channel'], c['center_frequency_mhz']
                sc.update(index=i + 1, current={'channel': ch, 'freq_mhz': f})
                # frozen-picture analysis window = the scan's lock wait time (2-10 s)
                fz = dict(self.config.get().get('freeze') or {}, analyze_sec=timeout)
                s = self._tune(mode, ch, f, mod, signal_timeout=timeout, source=source,
                               freeze=fz if fz.get('enabled', True) else None)
                s.scan_id = sc['id']
                # wait for lock (tsp gives up after `timeout` s) or the end of the session
                t_end = time.time() + timeout + 4
                while not self._scan_stop.is_set() and time.time() < t_end and s.state == 'tuning':
                    time.sleep(0.2)
                if s.state == 'locked':
                    # stay until PSIP identity and one frame are in, at most SCAN_DWELL_MAX s
                    # (+ the frozen-picture analysis window when enabled)
                    dwell = max(SCAN_DWELL_MAX, float(fz.get('analyze_sec', 6)) + 8) if s.freeze_path else SCAN_DWELL_MAX
                    t_end = time.time() + dwell
                    unlocked_since = None
                    while not self._scan_stop.is_set() and time.time() < t_end and s.state == 'locked' \
                            and not (s.has_identity() and s.captured() and (not s.freeze_path or s.freeze_done())):
                        if s.psip.rev == 0 and time.time() - s.locked_at > 5:
                            break                                 # locked but not a single PSI section: unusable
                        if s.fe and not s.fe.get('locked'):       # lock dropped again: marginal signal
                            unlocked_since = unlocked_since or time.time()
                            if time.time() - unlocked_since > 3:
                                break
                        else:
                            unlocked_since = None
                        time.sleep(0.3)
                    time.sleep(1.0)          # a few more SNR samples for the log
                with self.lock:
                    s.stop()
                for _ in range(50):          # _done() runs in the reader thread
                    if getattr(s, 'log_entry_written', None):
                        break
                    time.sleep(0.1)
                e = getattr(s, 'log_entry_written', None) or s.log_entry()
                sc['results'].append(e)
                if e['result'] == 'locked':
                    sc['locked'] += 1
                if (e.get('freeze') or {}).get('frozen'):
                    sc['frozen'] += 1
                self._check_alerts(e, new_alerts, recovered)
                sc['new_alerts'], sc['recovered'] = len(new_alerts), len(recovered)
        finally:
            sc.update(running=False, current=None, finished=time.time(),
                      cancelled=self._scan_stop.is_set())
            try:
                self.scans.save(sc)
            except OSError as e:
                print(f'warning: cannot save scan {sc.get("id")}: {e}', flush=True)
            self._notify_scan(sc, new_alerts, recovered)

    def note_live(self):
        s = self.session
        if s and s.state == 'locked' and not s.stopping:
            with self.res_lock:
                if not getattr(s, 'finalized', False):
                    self.results[f'{s.mode}:{s.channel}'] = {'state': 'locked', 't': time.time()}
            self._save_if_locked(s)

    def delete_saved(self, mode, channel):
        s = self.session
        if s and s.mode == mode and s.channel == channel:
            self.suppress.add(f'{mode}:{channel}')
        self.results.pop(f'{mode}:{channel}', None)
        return self.saved.delete(mode, channel)

    def clear_saved(self, mode=None):
        s = self.session
        if s and (mode is None or s.mode == mode):
            self.suppress.add(f'{s.mode}:{s.channel}')
        for k in [k for k in self.results if mode is None or k.startswith(mode + ':')]:
            if self.results[k]['state'] == 'locked':
                del self.results[k]
        return self.saved.clear(mode)

    # ---- monitoring ----
    def _check_alerts(self, e, new_alerts, recovered):
        """Compare one measurement with its baseline; open / resolve alerts for the channel."""
        try:
            found = monitor.evaluate(e, self.config.get())
            active = self.alerts.active_for(e['mode'], e['channel'])
            if found:
                # condition changed (e.g. nolock -> snr_drop): close the old kinds, keep the ones still true
                have = self.alerts.resolve_other_kinds(e['mode'], e['channel'], {k for k, _, _ in found},
                                                       time.strftime('%Y-%m-%d %H:%M:%S'))
                for kind, sev, msg in found:
                    if kind in have:
                        continue                      # still open: do not re-alert every scan
                    al = monitor.make_alert(e, kind, sev, msg)
                    self.alerts.add(al)
                    new_alerts.append(al)
            elif e['result'] == 'locked' and active:
                done = self.alerts.resolve(e['mode'], e['channel'], time.strftime('%Y-%m-%d %H:%M:%S'))
                recovered.extend([dict(a, recovered_snr=e.get('snr_avg')) for a in done])
        except Exception as ex:                       # never break a scan because of alerting
            print(f'alert check failed: {ex}', flush=True)

    def _notify_scan(self, sc, new_alerts, recovered):
        cfg = self.config.get()
        t = cfg['telegram']
        scheduled = sc.get('source') == 'schedule'
        if not new_alerts and not (recovered and t['notify_recovery']) and not (scheduled and t['send_scan_summary']):
            return
        E = monitor.esc
        mode = MODES[sc['mode']]['label']
        head = (f"<b>{E(cfg['title'])}</b>\n{mode} CH {sc.get('from')}~{sc.get('to')} 스캔"
                + (f" · {E(sc['schedule_name'])}" if sc.get('schedule_name') else '')
                + f"\n{time.strftime('%Y-%m-%d %H:%M', time.localtime(sc['started']))}"
                + (" (중단됨)" if sc.get('cancelled') else ''))
        lines = []
        for a in new_alerts:
            icon = '🔴' if a['severity'] == 'critical' else '🟠'
            vch = ', '.join(a['virtual_channels']) or '-'
            lines.append(f"{icon} CH{a['channel']} {a['freq_mhz']:g}MHz {E(vch)}\n    {E(a['message'])}")
        if recovered and t['notify_recovery']:
            for a in recovered:
                lines.append(f"✅ 복구 CH{a['channel']} {a['freq_mhz']:g}MHz "
                             f"{E(', '.join(a['virtual_channels']) or '-')} (SNR {a.get('recovered_snr')} dB)")
        summary = f"lock {sc['locked']}/{len(sc['results'])} · 신규 경보 {len(new_alerts)} · 복구 {len(recovered)}"
        title = '🚨 경보' if new_alerts else ('✅ 복구' if recovered else 'ℹ️ 스캔 완료')
        text = f"{title}\n{head}\n\n" + ('\n'.join(lines) + '\n\n' if lines else '') + summary
        self.telegram.send(text[:4000])

    def run_scheduled(self, sched, reason='schedule'):
        """Called by the scheduler worker; blocks until the scan is finished."""
        sync_modes(self.config.get())
        t0 = time.time()
        while self.scan and self.scan.get('running') and time.time() - t0 < 4 * 3600:
            time.sleep(5)                             # let a running scan finish first
        lo, hi = sched.get('from'), sched.get('to')
        chans, skipped = self.filter_channels(sched['mode'], lo, hi, bool(sched.get('only_locked')), with_skipped=True)
        if not chans:
            self.schedules.update(sched['id'], last_run=time.strftime('%Y-%m-%d %H:%M:%S'),
                                  last_result='스캔할 채널 없음' + (' (이전 lock 채널 없음)' if sched.get('only_locked') else ''))
            return
        print(f"schedule {sched['id']} ({sched.get('name')}): {sched['mode']} CH{lo}~{hi} ({reason})", flush=True)
        self.start_scan(sched['mode'], int(sched.get('timeout') or 4), chans, source='schedule', schedule=sched,
                        only_locked=bool(sched.get('only_locked')))
        sc = self.scan
        sc['skipped'] = skipped
        self.schedules.update(sched['id'], last_run=time.strftime('%Y-%m-%d %H:%M:%S'), last_scan_id=sc['id'],
                              last_result='실행 중')
        th = self._scan_thread
        if th:
            th.join()
        self.schedules.update(sched['id'], last_result=(
            f"{'중단' if sc.get('cancelled') else '완료'} · lock {sc['locked']}/{len(sc['results'])} · "
            f"경보 {sc.get('new_alerts', 0)}"))

    def locked_channels(self, mode):
        """Channels that have been received and locked before (channel history or saved channels)."""
        chans = {ch for ch in self.history.channels(mode)
                 if any(e.get('result') == 'locked' for e in self.history.read(mode, ch))}
        chans |= {r['channel'] for r in self.saved.snapshot().values() if r['mode'] == mode}
        return chans

    def filter_channels(self, mode, lo=None, hi=None, only_locked=False, with_skipped=False):
        with open(CHANNEL_FILE, encoding='utf-8') as f:
            chans = json.load(f).get(mode, [])
        chans = [c for c in chans if (lo is None or c['channel'] >= lo) and (hi is None or c['channel'] <= hi)]
        if only_locked:
            ok = self.locked_channels(mode)
            chans = [c for c in chans if c['channel'] in ok]
        # frequencies the current card cannot tune (e.g. 861 MHz on a 858 MHz frontend) are left out
        dev = pick_device(self.config.get(), MODES[mode]['delivery']) or {}
        fmin, fmax = dev.get('freq_min_mhz'), dev.get('freq_max_mhz')
        ok_rng = lambda f: (not fmin or f >= fmin) and (not fmax or f <= fmax)
        skipped = [c['channel'] for c in chans if not ok_rng(c['center_frequency_mhz'])]
        chans = [c for c in chans if ok_rng(c['center_frequency_mhz'])]
        return (chans, skipped) if with_skipped else chans

    def history_summary(self, mode):
        a = self.config.get()['alerts']
        with open(CHANNEL_FILE, encoding='utf-8') as f:
            plan = {c['channel']: c['center_frequency_mhz'] for c in json.load(f).get(mode, [])}
        out = []
        for ch in self.history.channels(mode):
            items = self.history.read(mode, ch)
            if not items:
                continue
            last = items[-1]
            locked = [x for x in items if x.get('result') == 'locked']
            if not locked:
                continue                              # never received: not a monitored channel
            base = self.history.baseline(mode, ch, a['baseline_count'], a['min_baseline'],
                                         device=device_info(self.config.get(), MODES[mode]['delivery']).get('key'))
            spark = [x.get('snr_avg') if x.get('result') == 'locked' else None for x in items[-30:]]
            out.append({'channel': ch, 'freq_mhz': plan.get(ch, last.get('freq_mhz')),
                        'virtual_channels': (locked[-1].get('virtual_channels') or []),
                        'count': len(items), 'locked_count': len(locked),
                        'last': {k: last.get(k) for k in ('time', 'result', 'snr_avg', 'strength_pct', 'ucb', 'source',
                                                          'rf_dbm_avg', 'ber', 'device')}
                                | {'frozen': bool((last.get('freeze') or {}).get('frozen'))},
                        'baseline': base, 'spark': spark,
                        'alerts': [{k: x[k] for k in ('kind', 'severity', 'message')}
                                   for x in self.alerts.active_for(mode, ch)]})
        return out

    def results_view(self):
        """Merge saved channels (persistent) with this run's no-lock results."""
        out = {k: dict(v) for k, v in self.results.items()}
        for k, r in self.saved.snapshot().items():
            e = out.setdefault(k, {})
            e.update({'state': 'locked', 'saved': True,
                      'names': [f"{c['virtual']} {c['short_name']}" for c in r.get('channels', [])],
                      'snr': r.get('snr_db'), 't': r.get('last_locked')})
        return out


# ---------------------------------------------------------------------------
# HTTP

class Handler(SimpleHTTPRequestHandler):
    ctl: Controller = None

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WWW, **kw)

    def end_headers(self):
        # static UI files: always revalidate (cheap 304) so a changed app.js is never served from a stale cache
        if not self.path.startswith(('/api/', '/capture/')):
            self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def _index(self):
        """index.html with ?v=<mtime> on its scripts/styles, so browsers fetch new versions after an update."""
        with open(os.path.join(WWW, 'index.html'), encoding='utf-8') as f:
            html = f.read()

        def ver(m):
            name = m.group(2)
            try:
                v = int(os.path.getmtime(os.path.join(WWW, name)))
            except OSError:
                return m.group(0)
            return f'{m.group(1)}{name}?v={v}"'
        html = re.sub(r'((?:src|href)=")([A-Za-z0-9_.-]+\.(?:js|css))"', ver, html)
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if not self.path.startswith('/api/status'):
            super().log_message(fmt, *args)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        sync_modes(self.ctl.config.get())
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            return self._index()
        if path == '/api/channels':
            with open(CHANNEL_FILE, encoding='utf-8') as f:
                data = json.load(f)
            return self._json({'modes': MODES, 'channels': data})
        if path == '/api/status':
            self.ctl.note_live()
            s = self.ctl.session
            return self._json({'session': s.summary() if s else None, 'results': self.ctl.results_view(),
                               'scan': self.ctl.scan, 'scan_timeout_range': SCAN_TIMEOUT_RANGE,
                               'monitor': {'alerts': self.ctl.alerts.counts(),
                                           'scheduler': self.ctl.scheduler.state(),
                                           'title': self.ctl.config.get()['title'],
                                           'device': device_info(self.ctl.config.get(),
                                                                 MODES[(s.mode if s else 'terrestrial')]['delivery'])},
                               'server_time': time.time()})
        q = parse_qs(urlparse(self.path).query)
        qv = lambda k, d=None: (q.get(k) or [d])[0]
        if path == '/api/devices':
            cfg = self.ctl.config.get()
            return self._json({'devices': dvbdev.list_devices(),
                               'selected': {m: device_info(cfg, v['delivery']) for m, v in MODES.items()},
                               'config': {k: cfg.get(k, 'auto') for k in ('adapter', 'frontend')}})
        if path == '/api/monitor':
            return self._json({'config': self.ctl.config.public(), 'alerts': self.ctl.alerts.counts(),
                               'scheduler': self.ctl.scheduler.state(), 'telegram_last': self.ctl.telegram.last})
        if path == '/api/alerts':
            return self._json({'alerts': self.ctl.alerts.list(qv('state', 'active'), int(qv('limit', '300'))),
                               'counts': self.ctl.alerts.counts()})
        if path == '/api/schedules':
            return self._json({'schedules': self.ctl.schedules.list(), 'scheduler': self.ctl.scheduler.state(),
                               'now': time.strftime('%Y-%m-%d %H:%M:%S')})
        if path == '/api/history/summary':
            mode = qv('mode')
            if mode not in MODES:
                return self._json({'error': 'bad mode'}, HTTPStatus.BAD_REQUEST)
            return self._json({'channels': self.ctl.history_summary(mode)})
        if path == '/api/history':
            mode = qv('mode')
            try:
                ch = int(qv('channel'))
                days = float(qv('days', '30'))
            except (TypeError, ValueError):
                return self._json({'error': 'mode/channel required'}, HTTPStatus.BAD_REQUEST)
            if mode not in MODES:
                return self._json({'error': 'bad mode'}, HTTPStatus.BAD_REQUEST)
            since = None if days <= 0 else time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - days * 86400))
            keep = ('time', 'source', 'result', 'snr_avg', 'snr_min', 'snr_last', 'strength_pct', 'ucb', 'bitrate',
                    'rf_dbm_avg', 'rf_dbm_min', 'ber', 'ber_src', 'device',
                    'tsid', 'virtual_channels', 'capture', 'baseline', 'scan_id', 'lock_time_s', 'freeze', 'tei', 'ber')
            items = [{k: e.get(k) for k in keep} for e in self.ctl.history.read(mode, ch, since)]
            return self._json({'mode': mode, 'channel': ch, 'entries': items})
        if path == '/api/scans':
            return self._json({'scans': self.ctl.scans.list()})
        m = re.fullmatch(r'/api/scans/([a-z]+_\d{8}-\d{6})(/export)?', path)
        if m:
            sid, export = m.group(1), m.group(2)
            cur = self.ctl.scan
            sc = cur if cur and cur.get('id') == sid else self.ctl.scans.get(sid)
            if not sc:
                return self._json({'error': 'scan not found'}, HTTPStatus.NOT_FOUND)
            if not export:
                return self._json(sc)
            fmt = (parse_qs(urlparse(self.path).query).get('format') or ['csv'])[0]
            return self._export(sc, fmt)
        if path == '/api/logs':
            q = parse_qs(urlparse(self.path).query)
            try:
                n = max(1, min(2000, int((q.get('limit') or ['200'])[0])))
            except ValueError:
                n = 200
            return self._json({'entries': self.ctl.rlog.tail(n)})
        if path.startswith('/capture/'):
            name = path[len('/capture/'):]
            if not re.fullmatch(r'[A-Za-z0-9_.\-]+\.jpg', name):
                return self._json({'error': 'bad name'}, HTTPStatus.BAD_REQUEST)
            fp = os.path.join(CAPTURE_DIR, name)
            try:
                with open(fp, 'rb') as f:
                    data = f.read()
            except OSError:
                return self._json({'error': 'not found'}, HTTPStatus.NOT_FOUND)
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Cache-Control', 'max-age=86400')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None
        if path == '/api/saved':
            items = sorted(self.ctl.saved.snapshot().values(), key=lambda r: (r['mode'], r['freq_mhz']))
            return self._json({'channels': items})
        if path == '/api/psip':
            s = self.ctl.session
            return self._json(s.psip.model() if s else {})
        return super().do_GET()

    def _download(self, data, ctype, filename):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _export(self, sc, fmt):
        base = f"atscscan_{sc['id']}"
        if fmt == 'json':
            return self._download(json.dumps(sc, ensure_ascii=False, indent=1).encode('utf-8'),
                                  'application/json; charset=utf-8', base + '.json')
        if fmt == 'csv':
            return self._download(scan_to_csv(sc).encode('utf-8'), 'text/csv; charset=utf-8', base + '.csv')
        if fmt == 'zip':
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
                z.writestr(base + '.csv', scan_to_csv(sc).encode('utf-8'))
                z.writestr(base + '.json', json.dumps(sc, ensure_ascii=False, indent=1))
                for e in sc.get('results', []):
                    name = e.get('capture')
                    fp = os.path.join(CAPTURE_DIR, name) if name else None
                    if fp and re.fullmatch(r'[A-Za-z0-9_.\-]+\.jpg', name) and os.path.exists(fp):
                        z.write(fp, 'capture/' + name, compress_type=zipfile.ZIP_STORED)
            return self._download(buf.getvalue(), 'application/zip', base + '.zip')
        return self._json({'error': 'format must be csv, json or zip'}, HTTPStatus.BAD_REQUEST)

    def do_POST(self):
        sync_modes(self.ctl.config.get())
        path = urlparse(self.path).path
        b = self._body()
        if path == '/api/tune':
            mode = b.get('mode')
            if mode not in MODES:
                return self._json({'error': 'bad mode'}, HTTPStatus.BAD_REQUEST)
            mod = b.get('modulation') or MODES[mode]['modulations'][0]
            if mod not in MODES[mode]['modulations']:
                mod = MODES[mode]['modulations'][0]
            try:
                ch = int(b['channel'])
                f = float(b['freq_mhz'])
            except (KeyError, TypeError, ValueError):
                return self._json({'error': 'bad channel/frequency'}, HTTPStatus.BAD_REQUEST)
            s = self.ctl.tune(mode, ch, f, mod)
            return self._json({'ok': True, 'session': s.summary()})
        if path == '/api/stop':
            self.ctl.stop()
            return self._json({'ok': True})
        if path == '/api/scan/start':
            mode = b.get('mode')
            if mode not in MODES:
                return self._json({'error': 'bad mode'}, HTTPStatus.BAD_REQUEST)
            try:
                timeout = int(b.get('timeout', 4))
            except (TypeError, ValueError):
                timeout = 4
            lo, hi = SCAN_TIMEOUT_RANGE
            if not lo <= timeout <= hi:
                return self._json({'error': f'timeout must be {lo}..{hi} s'}, HTTPStatus.BAD_REQUEST)
            try:   # optional channel range
                lo_ch = int(b['from']) if b.get('from') not in (None, '') else None
                hi_ch = int(b['to']) if b.get('to') not in (None, '') else None
            except (TypeError, ValueError):
                return self._json({'error': 'bad from/to'}, HTTPStatus.BAD_REQUEST)
            only = bool(b.get('only_locked'))
            chans, skipped = self.ctl.filter_channels(mode, lo_ch, hi_ch, only, with_skipped=True)
            if not chans:
                return self._json({'error': '스캔할 채널이 없습니다' + (' (이 범위에 이전에 lock된 채널 없음)' if only else '')},
                                  HTTPStatus.BAD_REQUEST)
            self.ctl.start_scan(mode, timeout, chans, only_locked=only)
            self.ctl.scan['skipped'] = skipped
            return self._json({'ok': True, 'scan': self.ctl.scan})
        if path == '/api/alerts/ack':
            return self._json({'ok': True, 'acked': self.ctl.alerts.ack(None if b.get('all') else b.get('id'))})
        if path == '/api/telegram/test':
            cfg = self.ctl.config.get()
            r = self.ctl.telegram.send(f"✅ <b>{monitor.esc(cfg['title'])}</b>\n텔레그램 테스트 메시지입니다. "
                                       f"({time.strftime('%Y-%m-%d %H:%M:%S')})", wait=True)
            return self._json(r)
        if path == '/api/schedules/save':
            sc = b.get('schedule') or {}
            try:
                mode = sc['mode']
                if mode not in MODES:
                    raise ValueError('bad mode')
                item = {'id': sc.get('id') or None, 'name': (sc.get('name') or '').strip() or sc['cron'],
                        'cron': sc['cron'].strip(), 'mode': mode,
                        'from': int(sc['from']), 'to': int(sc['to']), 'timeout': int(sc.get('timeout', 4)),
                        'enabled': bool(sc.get('enabled', True)), 'only_locked': bool(sc.get('only_locked'))}
                lo, hi = SCAN_TIMEOUT_RANGE
                if not lo <= item['timeout'] <= hi:
                    raise ValueError(f'timeout must be {lo}..{hi} s')
                if item['from'] > item['to']:
                    item['from'], item['to'] = item['to'], item['from']
                saved = self.ctl.schedules.save(item)
            except KeyError as e:
                return self._json({'error': f'missing {e}'}, HTTPStatus.BAD_REQUEST)
            except ValueError as e:
                return self._json({'error': str(e)}, HTTPStatus.BAD_REQUEST)
            return self._json({'ok': True, 'schedule': saved})
        if path == '/api/schedules/delete':
            return self._json({'ok': self.ctl.schedules.delete(b.get('id'))})
        if path == '/api/schedules/run':
            if not self.ctl.schedules.get(b.get('id')):
                return self._json({'error': 'not found'}, HTTPStatus.NOT_FOUND)
            return self._json({'ok': self.ctl.scheduler.enqueue(b['id'], 'manual')})
        if path == '/api/schedules/preview':
            try:
                c = monitor.CronExpr(b.get('cron', ''))
            except ValueError as e:
                return self._json({'error': str(e)})
            t, runs = dt.datetime.now(), []
            for _ in range(5):
                t = c.next_after(t)
                if not t:
                    break
                runs.append(t.strftime('%Y-%m-%d (%a) %H:%M'))
            return self._json({'next': runs})
        if path == '/api/scans/delete':
            sid = b.get('id')
            if self.ctl.scan and self.ctl.scan.get('id') == sid and self.ctl.scan.get('running'):
                return self._json({'error': 'scan is running'}, HTTPStatus.CONFLICT)
            ok = self.ctl.scans.delete(sid)
            if self.ctl.scan and self.ctl.scan.get('id') == sid:
                self.ctl.scan = None
            return self._json({'ok': ok})
        if path == '/api/scan/stop':
            self.ctl.stop_scan()
            return self._json({'ok': True, 'scan': self.ctl.scan})
        if path == '/api/saved/delete':
            try:
                n = self.ctl.delete_saved(b['mode'], int(b['channel']))
            except (KeyError, TypeError, ValueError):
                return self._json({'error': 'mode/channel required'}, HTTPStatus.BAD_REQUEST)
            return self._json({'ok': True, 'deleted': n})
        if path == '/api/saved/clear':
            mode = b.get('mode')
            if mode is not None and mode not in MODES:
                return self._json({'error': 'bad mode'}, HTTPStatus.BAD_REQUEST)
            return self._json({'ok': True, 'deleted': self.ctl.clear_saved(mode)})
        return self._json({'error': 'not found'}, HTTPStatus.NOT_FOUND)


def kill_stale_tsp():
    """Kill tsp processes left over by a previous backend (they hold the frontend)."""
    me = os.getpid()
    for pid in os.listdir('/proc'):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                argv = f.read().split(b'\0')
        except OSError:
            continue
        if argv and os.path.basename(argv[0]) == b'tsp' and b'--json-line=DVBSTATUS:' in argv:
            try:
                os.kill(int(pid), signal.SIGKILL)
                print(f'killed stale tsp pid {pid}', flush=True)
            except OSError:
                pass


def main():
    global CAPTURE_DIR, LOG_DIR, SCAN_DIR, CONFIG_FILE, SCHEDULE_FILE, ALERT_FILE, CHANNEL_LOG_DIR, SAVED_FILE
    ap = argparse.ArgumentParser(description='ATSC scan web backend')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8088)
    ap.add_argument('--adapter', type=int, default=0)
    ap.add_argument('--saved', default=SAVED_FILE, help='saved channel file (default: data/saved_channels.json)')
    ap.add_argument('--scans-dir', default=None, help='auto scan records (default: data/scans)')
    ap.add_argument('--data-root', default=None,
                    help='put config.json, data/, logs/, capture/ under this directory instead (testing)')
    ap.add_argument('--no-scheduler', action='store_true', help='do not run scheduled scans (testing)')
    ap.add_argument('--no-preview', action='store_true', help='disable WebRTC preview (ffmpeg -> mediamtx)')
    ap.add_argument('--rtsp', default=PREVIEW['rtsp'], help='mediamtx RTSP base URL for publishing')
    ap.add_argument('--preview-path', default=PREVIEW['path'], help='mediamtx path name')
    ap.add_argument('--webrtc-port', type=int, default=PREVIEW['webrtc_port'], help='mediamtx WebRTC port (browser)')
    a = ap.parse_args()

    PREVIEW.update(enabled=not a.no_preview, rtsp=a.rtsp.rstrip('/'), path=a.preview_path,
                   webrtc_port=a.webrtc_port)
    if a.data_root:
        r = os.path.abspath(a.data_root)
        CAPTURE_DIR, LOG_DIR = os.path.join(r, 'capture'), os.path.join(r, 'logs')
        SCAN_DIR, CONFIG_FILE = os.path.join(r, 'data', 'scans'), os.path.join(r, 'config.json')
        SCHEDULE_FILE, ALERT_FILE = os.path.join(r, 'data', 'schedules.json'), os.path.join(r, 'data', 'alerts.json')
        CHANNEL_LOG_DIR, SAVED_FILE = os.path.join(r, 'logs', 'channels'), os.path.join(r, 'data', 'saved_channels.json')
        if a.saved == SAVED_FILE_DEFAULT:
            a.saved = SAVED_FILE
    os.makedirs(LOG_DIR, exist_ok=True)
    kill_stale_tsp()
    Handler.ctl = Controller(a.adapter, a.saved, scans_dir=a.scans_dir, scheduler=not a.no_scheduler)
    if PREVIEW['enabled']:
        PREVIEW['encoder'] = select_encoder(bool(Handler.ctl.config.get().get('nvidia', True)))
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f'ATSC scan backend on http://{a.host}:{a.port}/  (adapter {a.adapter})', flush=True)
    if PREVIEW['enabled']:
        print(f"WebRTC preview: {PREVIEW['rtsp']}/{PREVIEW['path']} (encoder {PREVIEW['encoder']}), "
              f"player :{PREVIEW['webrtc_port']}/{PREVIEW['path']}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        Handler.ctl.stop()
        srv.server_close()


if __name__ == '__main__':
    main()
