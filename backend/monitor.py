"""Headend monitoring: config, cron schedules, per-channel RF history, alerts, Telegram.

Standard library only. Imported by server.py.
"""
import datetime as dt
import json
import os
import statistics
import threading
import time
import urllib.parse
import urllib.request
import uuid

DEFAULT_CONFIG = {
    "title": "SBS 헤드엔드 모니터 시스템",
    "nvidia": True,                 # true: NVIDIA NVENC + CUDA filters for the WebRTC preview, false: CPU only
    "freeze": {
        "enabled": True,            # analyse the first service's video during auto / scheduled scans
        "min_sec": 2.0,             # still picture at least this long -> flagged (capped at 90% of the window)
        "diff_threshold": 1.0,      # mean abs luma difference (0-255) below which a frame counts as still
        "smooth_frames": 9,         # rolling median over this many frames removes encoder GOP pulses
        "alert": True               # raise a warning alert (and Telegram) for frozen video
        # analysis window = the auto scan's lock wait time (2-10 s)
    },
    "telegram": {
        "enabled": False,
        "bot_token": "",            # 123456789:ABC... (from @BotFather)
        "chat_ids": [],             # user / group chat ids that receive alerts
        "notify_recovery": True,    # also send a message when an alerted channel is back to normal
        "send_scan_summary": False  # send a short summary after every scheduled scan, even without alerts
    },
    "alerts": {
        "baseline_count": 7,        # baseline = median of the last N locked measurements of the channel
        "min_baseline": 3,          # no alert until a channel has at least this many locked measurements
        "snr_drop_db": 3.0,         # warning when SNR is this much below the baseline
        "snr_critical_drop_db": 6.0,
        "strength_drop_pct": 15.0,  # warning when signal quality (%) drops this many points
        "alert_on_manual": False    # evaluate manual tunes too (default: auto/scheduled scans only)
    }
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


class Config:
    """config.json, re-read automatically when the file changes. Created with defaults (mode 600) if missing."""

    def __init__(self, path):
        self.path = path
        self._mtime = None
        self._data = DEFAULT_CONFIG
        if not os.path.exists(path):
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            os.chmod(path, 0o600)

    def get(self):
        try:
            m = os.path.getmtime(self.path)
            if m != self._mtime:
                with open(self.path, encoding='utf-8') as f:
                    self._data = _merge(DEFAULT_CONFIG, json.load(f))
                self._mtime = m
        except (OSError, ValueError) as e:
            print(f'warning: config {self.path}: {e}', flush=True)
        return self._data

    def public(self):
        c = self.get()
        t = c['telegram']
        return {'title': c['title'], 'alerts': c['alerts'], 'nvidia': bool(c.get('nvidia', True)),
                'freeze': c['freeze'],
                'telegram': {'enabled': bool(t['enabled'] and t['bot_token'] and t['chat_ids']),
                             'configured': bool(t['bot_token']), 'chat_count': len(t['chat_ids'] or []),
                             'notify_recovery': t['notify_recovery'], 'send_scan_summary': t['send_scan_summary']}}


# ---------------------------------------------------------------------------
# cron

_DOW_NAMES = {'sun': 0, 'mon': 1, 'tue': 2, 'wed': 3, 'thu': 4, 'fri': 5, 'sat': 6}
_MON_NAMES = {m: i + 1 for i, m in enumerate('jan feb mar apr may jun jul aug sep oct nov dec'.split())}
_FIELDS = [('minute', 0, 59, {}), ('hour', 0, 23, {}), ('dom', 1, 31, {}), ('month', 1, 12, _MON_NAMES),
           ('dow', 0, 7, _DOW_NAMES)]
_ALIASES = {'@daily': '0 0 * * *', '@midnight': '0 0 * * *', '@hourly': '0 * * * *',
            '@weekly': '0 0 * * 0', '@monthly': '0 0 1 * *'}


class CronExpr:
    """Standard 5-field cron: minute hour day-of-month month day-of-week (*, a,b, a-b, */n, a-b/n, names)."""

    def __init__(self, expr):
        self.expr = expr.strip()
        text = _ALIASES.get(self.expr.lower(), self.expr)
        parts = text.split()
        if len(parts) != 5:
            raise ValueError('cron 식은 "분 시 일 월 요일" 5개 필드여야 합니다')
        self.sets = []
        self.star = []
        for part, (name, lo, hi, names) in zip(parts, _FIELDS):
            vals = set()
            for item in part.split(','):
                step = 1
                if '/' in item:
                    item, s = item.split('/', 1)
                    step = int(s)
                    if step < 1:
                        raise ValueError(f'{name}: 간격은 1 이상')
                if item == '*':
                    a, b = lo, hi
                elif '-' in item:
                    a, b = (self._num(x, names) for x in item.split('-', 1))
                else:
                    a = self._num(item, names)
                    b = hi if step > 1 else a
                if not (lo <= a <= hi and lo <= b <= hi and a <= b):
                    raise ValueError(f'{name}: 범위 {lo}-{hi}를 벗어남 ({part})')
                vals.update(range(a, b + 1, step))
            if name == 'dow' and 7 in vals:
                vals.discard(7)
                vals.add(0)
            self.sets.append(vals)
            self.star.append(part == '*' or part.startswith('*/'))

    @staticmethod
    def _num(x, names):
        x = x.strip().lower()
        if x in names:
            return names[x]
        return int(x)

    def matches(self, t):
        mi, hr, dom, mon, dow = self.sets
        if t.minute not in mi or t.hour not in hr or t.month not in mon:
            return False
        d_ok = t.day in dom
        w_ok = (t.isoweekday() % 7) in dow
        if self.star[2] and self.star[4]:
            return True
        if self.star[2]:
            return w_ok
        if self.star[4]:
            return d_ok
        return d_ok or w_ok      # cron: both restricted -> either matches

    def next_after(self, t):
        t = t.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        end = t + dt.timedelta(days=366)
        while t < end:
            if t.month not in self.sets[3]:
                t = (t.replace(day=1, hour=0, minute=0) + dt.timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(t):
                t = t.replace(hour=0, minute=0) + dt.timedelta(days=1)
                continue
            if t.hour not in self.sets[1]:
                t = t.replace(minute=0) + dt.timedelta(hours=1)
                continue
            if t.minute in self.sets[0]:
                return t
            t += dt.timedelta(minutes=1)
        return None

    def _day_ok(self, t):
        d_ok = t.day in self.sets[2]
        w_ok = (t.isoweekday() % 7) in self.sets[4]
        if self.star[2] and self.star[4]:
            return True
        if self.star[2]:
            return w_ok
        if self.star[4]:
            return d_ok
        return d_ok or w_ok


# ---------------------------------------------------------------------------
# per-channel history

class ChannelHistory:
    """logs/channels/<mode>_ch<NNN>.jsonl - one JSON line per measurement of that channel."""

    def __init__(self, directory):
        self.dir = directory
        self.lock = threading.Lock()
        os.makedirs(directory, exist_ok=True)

    def path(self, mode, channel):
        return os.path.join(self.dir, f'{mode}_ch{int(channel):03d}.jsonl')

    def append(self, e):
        with self.lock:
            with open(self.path(e['mode'], e['channel']), 'a', encoding='utf-8') as f:
                f.write(json.dumps(e, ensure_ascii=False) + '\n')

    def read(self, mode, channel, since=None):
        out = []
        try:
            with open(self.path(mode, channel), encoding='utf-8') as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if since and e.get('time', '') < since:
                        continue
                    out.append(e)
        except FileNotFoundError:
            pass
        return out

    def channels(self, mode):
        res = []
        for n in os.listdir(self.dir):
            if n.startswith(mode + '_ch') and n.endswith('.jsonl'):
                try:
                    res.append(int(n[len(mode) + 3:-6]))
                except ValueError:
                    pass
        return sorted(res)

    def baseline(self, mode, channel, n, min_n):
        """Median SNR / quality of the last n locked measurements (None if fewer than min_n)."""
        locked = [e for e in self.read(mode, channel) if e.get('result') == 'locked' and e.get('snr_avg') is not None]
        locked = locked[-n:]
        if len(locked) < min_n:
            return None
        strengths = [e['strength_pct'] for e in locked if e.get('strength_pct') is not None]
        return {'count': len(locked),
                'snr': round(statistics.median(e['snr_avg'] for e in locked), 1),
                'strength': round(statistics.median(strengths), 1) if strengths else None}

    def import_reception_log(self, path):
        """One-time backfill from logs/reception.jsonl when the per-channel directory is empty."""
        if any(n.endswith('.jsonl') for n in os.listdir(self.dir)) or not os.path.exists(path):
            return 0
        n = 0
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if 'mode' in e and 'channel' in e:
                    self.append(e)
                    n += 1
        return n


# ---------------------------------------------------------------------------
# alerts

class AlertStore:
    """data/alerts.json: list of alerts (newest last), at most `keep` entries."""

    def __init__(self, path, keep=1000):
        self.path = path
        self.keep = keep
        self.lock = threading.Lock()
        try:
            with open(path, encoding='utf-8') as f:
                self.items = json.load(f)
        except (OSError, ValueError):
            self.items = []

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.items[-self.keep:], f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def active_for(self, mode, channel):
        return [a for a in self.items if a['mode'] == mode and a['channel'] == channel and not a.get('resolved')]

    def add(self, a):
        with self.lock:
            self.items.append(a)
            self._save()

    def resolve(self, mode, channel, when):
        with self.lock:
            done = []
            for a in self.items:
                if a['mode'] == mode and a['channel'] == channel and not a.get('resolved'):
                    a['resolved'] = when
                    done.append(a)
            if done:
                self._save()
            return done

    def resolve_other_kinds(self, mode, channel, keep, when):
        """Resolve open alerts of the channel whose kind is not in `keep`; return kinds still open."""
        with self.lock:
            changed, still = False, set()
            for a in self.items:
                if a['mode'] == mode and a['channel'] == channel and not a.get('resolved'):
                    if a['kind'] in keep:
                        still.add(a['kind'])
                    else:
                        a['resolved'] = when
                        changed = True
            if changed:
                self._save()
            return still

    def ack(self, aid=None):
        with self.lock:
            n = 0
            for a in self.items:
                if (aid is None or a['id'] == aid) and not a.get('acked'):
                    a['acked'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    n += 1
            if n:
                self._save()
            return n

    def list(self, state='active', limit=300):
        items = self.items
        if state == 'active':
            items = [a for a in items if not a.get('resolved')]
        elif state == 'unacked':
            items = [a for a in items if not a.get('acked') and not a.get('resolved')]
        return items[::-1][:limit]

    def counts(self):
        act = [a for a in self.items if not a.get('resolved')]
        return {'active': len(act), 'unacked': sum(1 for a in act if not a.get('acked')),
                'critical': sum(1 for a in act if a['severity'] == 'critical' and not a.get('acked'))}


def evaluate(e, cfg):
    """Return a list of (kind, severity, message) for a measurement entry that carries its baseline."""
    out = []
    fz = e.get('freeze') or {}
    if e.get('result') == 'locked' and fz.get('frozen') and cfg.get('freeze', {}).get('alert', True):
        out.append(('freeze', 'warning', f"영상 정지 {fz['duration']:.1f}초 감지 ({fz['window']:.0f}초 분석"
                                         f"{' · 분석 끝까지 정지' if fz.get('to_end') else ''}"
                                         f"{', 프레임 차이 ' + str(fz['diff']) if fz.get('diff') is not None else ''})"))
    b = e.get('baseline')
    if not b:
        return out
    a = cfg['alerts']
    if e['result'] in ('nolock', 'error'):
        out.append(('nolock', 'critical', f"lock 실패 (평소 SNR {b['snr']} dB)"))
    elif e['result'] == 'weak':
        out.append(('weak', 'critical', f"신호 불안정 — lock 비트만 켜지고 TS 없음 (평소 SNR {b['snr']} dB)"))
    else:
        if e.get('snr_avg') is not None:
            drop = round(b['snr'] - e['snr_avg'], 1)
            if drop >= a['snr_drop_db']:
                sev = 'critical' if drop >= a['snr_critical_drop_db'] else 'warning'
                out.append(('snr_drop', sev, f"SNR {e['snr_avg']} dB — 평소 {b['snr']} dB보다 {drop} dB 낮음"))
        if e.get('strength_pct') is not None and b.get('strength') is not None:
            drop = round(b['strength'] - e['strength_pct'], 1)
            if drop >= a['strength_drop_pct']:
                out.append(('strength_drop', 'warning',
                            f"신호 품질 {e['strength_pct']}% — 평소 {b['strength']}%보다 {drop}%p 낮음"))
    return out


def make_alert(e, kind, severity, message):
    return {'id': uuid.uuid4().hex[:12], 'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'mode': e['mode'], 'channel': e['channel'], 'freq_mhz': e['freq_mhz'],
            'virtual_channels': e.get('virtual_channels') or [], 'kind': kind, 'severity': severity,
            'message': message, 'snr': e.get('snr_avg'), 'strength': e.get('strength_pct'),
            'baseline': e.get('baseline'), 'scan_id': e.get('scan_id'), 'source': e.get('source'),
            'measured': e.get('time'), 'acked': None, 'resolved': None}


# ---------------------------------------------------------------------------
# Telegram

class Telegram:
    def __init__(self, config):
        self.config = config
        self.last = None        # {'time', 'ok', 'error'}

    def send(self, text, wait=False):
        c = self.config.get()['telegram']
        if not (c['enabled'] and c['bot_token'] and c['chat_ids']):
            return {'ok': False, 'error': 'telegram 비활성 (config.json의 telegram.enabled, bot_token, chat_ids 확인)'}

        def run():
            errors = []
            for cid in c['chat_ids']:
                data = urllib.parse.urlencode({'chat_id': cid, 'text': text, 'parse_mode': 'HTML',
                                               'disable_web_page_preview': 'true'}).encode()
                url = f"https://api.telegram.org/bot{c['bot_token']}/sendMessage"
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
                        body = json.loads(r.read() or b'{}')
                        if not body.get('ok'):
                            errors.append(f"{cid}: {body.get('description')}")
                except Exception as ex:      # network / HTTP errors: report, never crash the monitor
                    msg = str(ex)
                    if hasattr(ex, 'read'):
                        try:
                            msg = json.loads(ex.read()).get('description', msg)
                        except Exception:
                            pass
                    errors.append(f'{cid}: {msg}')
            self.last = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'ok': not errors,
                         'error': '; '.join(errors) or None}
            if errors:
                print(f'telegram: {self.last["error"]}', flush=True)
            return self.last

        if wait:
            return run()
        threading.Thread(target=run, daemon=True).start()
        return {'ok': True, 'queued': True}


def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


# ---------------------------------------------------------------------------
# schedules

class ScheduleStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path, encoding='utf-8') as f:
                self.items = json.load(f)
        except (OSError, ValueError):
            self.items = []

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def list(self):
        now = dt.datetime.now()
        out = []
        for s in self.items:
            s = dict(s)
            try:
                nxt = CronExpr(s['cron']).next_after(now) if s.get('enabled') else None
                s['next_run'] = nxt.strftime('%Y-%m-%d %H:%M') if nxt else None
            except ValueError as e:
                s['next_run'] = None
                s['error'] = str(e)
            out.append(s)
        return out

    def get(self, sid):
        return next((s for s in self.items if s['id'] == sid), None)

    def save(self, s):
        CronExpr(s['cron'])          # validate
        with self.lock:
            if not s.get('id'):
                s['id'] = uuid.uuid4().hex[:10]
                s['created'] = time.strftime('%Y-%m-%d %H:%M:%S')
                self.items.append(s)
            else:
                old = self.get(s['id'])
                if not old:
                    raise KeyError(s['id'])
                for k in ('created', 'last_run', 'last_scan_id', 'last_result'):
                    s.setdefault(k, old.get(k))
                self.items[self.items.index(old)] = s
            self._save()
            return s

    def update(self, sid, **kw):
        with self.lock:
            s = self.get(sid)
            if s:
                s.update(kw)
                self._save()

    def delete(self, sid):
        with self.lock:
            s = self.get(sid)
            if s:
                self.items.remove(s)
                self._save()
            return bool(s)


class Scheduler:
    """Checks the schedules every few seconds; due scans are queued and run one after another."""

    def __init__(self, store, run_scan, enabled=True):
        self.store = store
        self.enabled = enabled
        self.run_scan = run_scan       # callable(schedule) -> blocks until the scan is finished
        self.queue = []
        self.qlock = threading.Lock()
        self.current = None
        self._last_minute = None
        threading.Thread(target=self._tick, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()

    def enqueue(self, sid, reason='schedule'):
        with self.qlock:
            if sid not in [q[0] for q in self.queue] and (not self.current or self.current[0] != sid):
                self.queue.append((sid, reason))
                return True
            return False

    def _tick(self):
        while True:
            now = dt.datetime.now().replace(second=0, microsecond=0)
            if self.enabled and now != self._last_minute:
                self._last_minute = now
                for s in list(self.store.items):
                    if not s.get('enabled'):
                        continue
                    try:
                        if CronExpr(s['cron']).matches(now):
                            self.enqueue(s['id'])
                    except ValueError:
                        pass
            time.sleep(5)

    def _worker(self):
        while True:
            item = None
            with self.qlock:
                if self.queue:
                    item = self.queue.pop(0)
                    self.current = item
            if not item:
                time.sleep(1)
                continue
            sid, reason = item
            s = self.store.get(sid)
            try:
                if s:
                    self.run_scan(dict(s), reason)
            except Exception as e:           # keep the scheduler alive
                print(f'scheduler: {sid}: {e}', flush=True)
            finally:
                with self.qlock:
                    self.current = None

    def state(self):
        with self.qlock:
            return {'queue': [q[0] for q in self.queue], 'current': self.current[0] if self.current else None,
                    'enabled': self.enabled}
