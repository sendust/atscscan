'use strict';

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const hex = (n) => (n == null ? '–' : '0x' + Number(n).toString(16).toUpperCase().padStart(4, '0'));
const fmtRate = (b) => (b == null ? '–' : (b / 1e6).toFixed(3) + ' Mbps');
const fmtDur = (s) => {
  if (s == null) return '–';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}시간 ${m}분` : `${m}분`;
};

const app = {
  modes: {}, channels: {}, mode: null, active: null,
  status: null, psip: null, psipRev: -1, tab: 'vct', timer: null,
};

const STATE_TEXT = {
  tuning: '튜닝 중', locked: 'LOCK', nolock: 'NO LOCK', error: '오류', stopped: '중지됨', ended: '종료',
};
const BITS = ['SIGNAL', 'CARRIER', 'VITERBI', 'SYNC', 'LOCK'];

async function api(path, body) {
  const opt = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opt);
  return r.json();
}

/* ---------------- views ---------------- */

function showMode() {
  app.mode = null;
  $('#viewMode').hidden = false;
  $('#viewScan').hidden = true;
  $('#btnHome').hidden = true;
  try { localStorage.removeItem('atscscan.mode'); } catch (e) { /* ignore */ }
}

function showScan(mode) {
  app.mode = mode;
  try { localStorage.setItem('atscscan.mode', mode); } catch (e) { /* ignore */ }
  $('#viewMode').hidden = true;
  $('#viewScan').hidden = false;
  $('#btnHome').hidden = false;
  const m = app.modes[mode];
  $('#modeTitle').textContent = `${m.label} · ${app.channels[mode].length}개 주파수`;
  const sel = $('#modSel');
  sel.innerHTML = m.modulations.map((x) => `<option>${x}</option>`).join('');
  $('#modWrap').hidden = m.modulations.length < 2;
  renderGrid();
  cancelClear();
  renderSaved(true);
  fillRange();
  app.scanView = ''; scanSig = '';
  loadScanList();
}

function renderGrid() {
  const list = app.channels[app.mode] || [];
  const res = app.status?.results || {};
  const s = app.status?.session;
  $('#grid').innerHTML = list.map((c) => {
    const key = `${app.mode}:${c.channel}`;
    const r = res[key];
    const cls = ['ch'];
    if (r) cls.push('r-' + r.state);
    if (r?.frozen) cls.push('r-frozen');
    if (s && s.mode === app.mode && s.channel === c.channel) {
      cls.push('active');
      if (s.state === 'tuning' || s.state === 'locked') cls.push('busy');
    }
    const nm = r?.names?.length ? r.names.join(', ') : '';
    return `<button class="${cls.join(' ')}" data-ch="${c.channel}" data-f="${c.center_frequency_mhz}" title="${esc(nm)}">
      <div class="n">CH ${c.channel}</div>
      <div class="f">${c.center_frequency_mhz} MHz</div>
      <div class="nm">${esc(nm)}</div>${r?.frozen ? '<span class="fzm" title="최근 스캔에서 영상 정지">❄</span>' : ''}${r?.saved ? '<span class="sv">💾</span>' : ''}</button>`;
  }).join('');
}

/* ---------------- status ---------------- */

function renderStatus() {
  const s = app.status?.session;
  if (!s || s.mode !== app.mode) { $('#detail').hidden = true; $('#btnStop').disabled = true; return; }
  $('#detail').hidden = false;
  $('#btnStop').disabled = !(s.state === 'tuning' || s.state === 'locked' || app.status?.scan?.running);
  $('#dhCh').textContent = `CH ${s.channel}  ·  ${app.modes[s.mode].label}`;
  $('#dhFreq').textContent = `${s.freq_mhz} MHz · ${s.modulation}`;
  const st = $('#dhState');
  st.className = 'state s-' + s.state;
  const label = STATE_TEXT[s.state] || s.state;
  const msg = (s.message || '').replace(/…$/, '');
  st.textContent = msg && s.state !== 'locked' && msg !== label ? `${label} — ${s.message}` : label;

  renderPreview(s);

  const fe = s.frontend;
  const on = new Set(fe?.bits || []);
  $('#feBits').innerHTML = BITS.map((b) => `<span class="bit ${on.has(b) ? 'on' : ''} ${b === 'LOCK' ? 'lock' : ''}">${b}</span>`).join('');
  const snr = fe?.snr_db;
  $('#feSnr').textContent = snr != null ? `${snr.toFixed(1)} dB`
    : (fe?.snr_src === 'raw' && fe?.snr_raw != null ? `raw ${fe.snr_raw}` : '–');
  $('#feSnr').title = fe?.snr_src === 'v5' ? 'DVBv5 CNR' : fe?.snr_src === 'v3' ? 'DVBv3 SNR (드라이버 단위 환산)' : fe?.snr_src === 'raw' ? '드라이버 고유 단위 (dB 아님)' : '';
  $('#feSnrBar').style.width = snr == null ? '0' : Math.min(100, (snr / 40) * 100) + '%';
  $('#feSnrBar').style.background = snr == null ? '' : snr >= 20 ? 'var(--ok)' : snr >= 15.5 ? 'var(--warn)' : 'var(--bad)';
  if (fe?.signal_dbm != null) {                 // card reports the real RF input level
    $('#feStrLabel').textContent = 'RF 레벨';
    $('#feStr').textContent = `${fe.signal_dbm.toFixed(1)} dBm`;
    $('#feStrBar').style.width = Math.max(0, Math.min(100, ((fe.signal_dbm + 90) / 70) * 100)) + '%';
  } else {
    const str = fe?.strength_pct;
    $('#feStrLabel').textContent = '신호 품질';
    $('#feStr').textContent = str == null ? '–' : `${str.toFixed(0)} %`;
    $('#feStrBar').style.width = (str || 0) + '%';
  }
  $('#feUcb').textContent = fe?.ucb ?? '–';
  $('#feUcbRate').textContent = fe?.ucb_rate == null ? '–' : `${fe.ucb_rate} /s`;
  const er = s.errors || {};
  $('#feBer').textContent = fe?.ber != null ? `${fmtBer(fe.ber)} (${fe.ber_src})`
    : er.ber != null ? `${er.ber} (TSDuck)` : (fe ? 'N/A (이 카드·드라이버 미제공)' : '–');
  $('#feTei').textContent = er.tei == null ? '–' : `${er.tei.toLocaleString()} (${(er.per * 100).toFixed(3)}%)`;
  $('#tsRate').textContent = fmtRate(s.ts?.bitrate);
  $('#tsPk').textContent = s.ts ? `${(s.ts.packets ?? 0).toLocaleString()} / sync ${s.ts.invalid_syncs ?? 0}, TEI ${s.ts.transport_errors ?? 0}` : '–';
  $('#feElapsed').textContent = `${s.elapsed.toFixed(0)} s`;
  $('#feCapture').innerHTML = (s.capture
    ? `<a href="/capture/${encodeURIComponent(s.capture)}" target="_blank" rel="noopener">${esc(s.capture)}</a>`
    : (s.state === 'locked' ? '저장 중…' : '–')) + (s.freeze_analyzing && s.state === 'locked' ? ' <span class="muted">· 영상 정지 분석 중…</span>' : '');
  drawSpark(s.fe_hist || []);

  const log = $('#log');
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.textContent = (s.log || []).join('\n');
  if (atBottom) log.scrollTop = log.scrollHeight;
}

let previewKey = '';
function renderPreview(s) {
  const pv = s.preview || {};
  const live = pv.enabled && (s.state === 'tuning' || s.state === 'locked');
  const key = live ? `${s.mode}:${s.channel}:${s.started}` : '';
  $('#videoWrap').hidden = !live;
  if (key === previewKey) return;
  previewKey = key;
  const player = $('#player');
  if (!live) { player.src = 'about:blank'; return; }
  const base = `${location.protocol}//${location.hostname}:${pv.webrtc_port}/${encodeURIComponent(pv.path)}/`;
  player.src = `${base}?controls=true&muted=true&autoplay=true&playsinline=true`;
  const ENC = { nvenc: 'H.264 720p · GPU NVENC', x264: 'H.264 540p · CPU x264', openh264: 'H.264 540p · CPU OpenH264', vp8: 'VP8 540p · CPU libvpx' };
  $('#videoNote').innerHTML = `WebRTC 미리보기 · 첫 번째 서비스 · ${esc(ENC[pv.encoder] || pv.encoder)} + Opus · ` +
    `소리는 플레이어에서 음소거 해제 · <a href="${esc(base)}" target="_blank" rel="noopener">새 창으로 열기</a>`;
}

function drawSpark(h) {
  const svg = $('#spark');
  if (!h.length) { svg.innerHTML = ''; return; }
  const W = 300, H = 60, n = 120;
  const pts = h.slice(-n).map((p, i, a) => {
    const x = a.length < 2 ? W : (i / (a.length - 1)) * W;
    const y = H - Math.max(0, Math.min(40, p.snr ?? 0)) / 40 * H;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const y15 = H - (15.5 / 40) * H;
  svg.innerHTML = `<line x1="0" x2="${W}" y1="${y15}" y2="${y15}" stroke="#d29922" stroke-dasharray="3 3" stroke-width="1" opacity=".6"/>
    <polyline points="${pts.join(' ')}" fill="none" stroke="#4aa3ff" stroke-width="1.6" vector-effect="non-scaling-stroke"/>`;
}

/* ---------------- PSIP ---------------- */

const TABS = [
  ['vct', 'VCT (가상채널)'], ['eit', 'EIT (편성표)'], ['stt', 'STT (시각)'], ['mgt', 'MGT'],
  ['rrt', 'RRT (등급)'], ['pmt', 'PAT / PMT'], ['ts', 'TS 분석'],
];

let psipSig = '';
function renderPsip(force) {
  const s = app.status?.session;
  const p = app.psip;
  const sig = [s?.mode, s?.channel, s?.state, p?.rev, app.tab, app.tab === 'ts' ? s?.ts?.packets : 0].join('|');
  if (!force && sig === psipSig) return;
  psipSig = sig;
  const has = s && s.mode === app.mode && (s.state === 'locked' || (p && p.rev > 0));
  $('#psip').hidden = !has;
  if (!has) return;
  const cnt = {
    vct: p?.vct?.channels?.length, eit: p?.eit?.length, stt: p?.stt ? 1 : 0, mgt: p?.mgt?.tables?.length,
    rrt: p?.rrt ? 1 : 0, pmt: p?.pmt?.length, ts: s.ts?.pids?.length,
  };
  $('#tabs').innerHTML = TABS.map(([k, t]) =>
    `<button class="tab ${app.tab === k ? 'on' : ''}" data-tab="${k}">${t}<span class="cnt">${cnt[k] || ''}</span></button>`).join('');
  $('#tabBody').innerHTML = (RENDER[app.tab] || (() => ''))(p || {}, s);
}

const table = (head, rows, empty = '아직 수신되지 않았습니다…') => rows.length
  ? `<table class="t"><thead><tr>${head.map((h) => `<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`
  : `<div class="empty">${empty}</div>`;

const RENDER = {
  vct(p) {
    const v = p.vct;
    if (!v) return '<div class="empty">VCT 수신 대기 중…</div>';
    return `<h4>${v.kind} · TSID ${v.tsid} (${hex(v.tsid)}) · version ${v.version}</h4>` + table(
      ['가상채널', 'Short name', 'Long name', 'Program', 'Source ID', 'Service', '변조', 'PCR PID', '구성 스트림', 'CA', 'Hidden'],
      v.channels.map((c) => `<tr>
        <td class="m"><b>${esc(c.virtual)}</b></td><td>${esc(c.short_name)}</td><td>${esc(c.long_name)}</td>
        <td class="m">${c.program}</td><td class="m">${c.source_id}</td><td>${esc(c.service_type)}</td>
        <td>${esc(c.modulation)}</td><td class="m">${hex(c.pcr_pid)}</td>
        <td class="wrap">${c.components.map((x) => `<span class="pill">${hex(x.pid)} ${esc(x.type)}${x.lang ? ' ' + esc(x.lang) : ''}</span>`).join('')}</td>
        <td>${c.access_controlled ? '예' : '–'}</td><td>${c.hidden ? '예' : '–'}</td></tr>`));
  },
  eit(p) {
    const ev = p.eit || [];
    const now = p.stt ? Date.parse(p.stt.kst.replace(' ', 'T') + '+09:00') : Date.now();
    return table(['채널', '시작 (KST)', '길이', '제목', '상세 (ETT)', 'Event ID', 'PID'],
      ev.map((e) => {
        const st = Date.parse((e.start || '').replace(' ', 'T') + '+09:00');
        const isNow = st <= now && now < st + (e.duration || 0) * 1000;
        return `<tr class="${isNow ? 'now' : ''}">
          <td>${esc(e.channel)}</td><td class="m">${esc(e.start)}</td><td class="m">${fmtDur(e.duration)}</td>
          <td class="wrap"><b>${esc(e.title)}</b>${isNow ? ' <span class="pill">방송 중</span>' : ''}</td>
          <td class="desc">${esc(e.description)}</td><td class="m">${e.event_id}</td><td class="m">${hex(e.pid)}</td></tr>`;
      }));
  },
  stt(p) {
    const t = p.stt;
    if (!t) return '<div class="empty">STT 수신 대기 중…</div>';
    return `<div class="big-time">${esc(t.kst)} KST</div>
      <table class="t" style="max-width:520px;margin-top:10px"><tbody>
      <tr><th>UTC</th><td class="m">${esc(t.utc)}</td></tr>
      <tr><th>system_time (GPS 초)</th><td class="m">${t.system_time}</td></tr>
      <tr><th>GPS-UTC offset</th><td class="m">${t.gps_utc_offset} s</td></tr>
      <tr><th>Daylight saving</th><td class="m">${t.ds_status ? 'on' : 'off'}</td></tr></tbody></table>`;
  },
  mgt(p) {
    const m = p.mgt;
    if (!m) return '<div class="empty">MGT 수신 대기 중…</div>';
    return `<h4>MGT version ${m.version}</h4>` + table(['Table type', 'PID', 'Version', 'Bytes'],
      m.tables.map((t) => `<tr><td>${esc(t.type)}</td><td class="m">${hex(t.pid)}</td><td class="m">${t.version}</td><td class="m">${t.bytes}</td></tr>`));
  },
  rrt(p) {
    const r = p.rrt;
    if (!r) return '<div class="empty">RRT는 전송 주기가 길어 수신에 시간이 걸릴 수 있습니다…</div>';
    return `<h4>Region ${r.region} · ${esc(r.region_name)} · version ${r.version}</h4>` + table(['Dimension', '등급 값'],
      r.dimensions.map((d) => `<tr><td>${esc(d.name)}</td><td class="wrap">${d.values.map((v) => `<span class="pill">${esc(v)}</span>`).join('')}</td></tr>`),
      '이 방송의 RRT에는 등급 차원(dimension) 정보가 없습니다 (지역 이름만 전송).');
  },
  pmt(p) {
    let h = '';
    if (p.pat) {
      h += `<h4>PAT · TSID ${p.pat.tsid} · version ${p.pat.version}</h4>` + table(['Program', 'PMT PID'],
        p.pat.programs.map((x) => `<tr><td class="m">${x.program}</td><td class="m">${hex(x.pmt_pid)}</td></tr>`));
    }
    for (const m of p.pmt || []) {
      h += `<h4>PMT · program ${m.program} · PCR ${hex(m.pcr_pid)} · version ${m.version}</h4>` + table(['PID', 'Stream type', '종류', '이름', 'Descriptors'],
        m.streams.map((x) => `<tr><td class="m">${hex(x.pid)}</td><td class="m">0x${(x.stream_type ?? 0).toString(16).padStart(2, '0')}</td>
          <td>${esc(x.type)}</td><td>${esc(x.name)}</td><td class="wrap">${x.descriptors.map((d) => `<span class="pill">${esc(d)}</span>`).join('')}</td></tr>`));
    }
    return h || '<div class="empty">PAT/PMT 수신 대기 중…</div>';
  },
  ts(p, s) {
    const t = s.ts;
    if (!t) return '<div class="empty">TSDuck analyze 결과 대기 중 (3초 간격)…</div>';
    let h = `<h4>TS ID ${t.id} · ${fmtRate(t.bitrate)} (PCR ${fmtRate(t.pcr_bitrate)}) · ${(t.packets ?? 0).toLocaleString()} packets</h4>`;
    h += table(['Service ID', '이름', 'Provider', '비트레이트', 'PMT PID', 'PCR PID', 'PIDs'],
      t.services.map((x) => `<tr><td class="m">${x.id}</td><td>${esc(x.name)}</td><td>${esc(x.provider)}</td><td class="m">${fmtRate(x.bitrate)}</td>
        <td class="m">${hex(x.pmt_pid)}</td><td class="m">${hex(x.pcr_pid)}</td><td class="wrap">${(x.pids || []).map((q) => `<span class="pill">${hex(q)}</span>`).join('')}</td></tr>`));
    h += '<h4>PID</h4>' + table(['PID', '설명', '비트레이트', '패킷', 'CC 오류', 'Scrambled'],
      t.pids.map((x) => `<tr><td class="m">${hex(x.pid)}</td><td>${esc(x.description)}</td><td class="m">${fmtRate(x.bitrate)}</td>
        <td class="m">${(x.packets ?? 0).toLocaleString()}</td><td class="m">${x.discontinuities ?? 0}</td><td>${x.scrambled ? '예' : '–'}</td></tr>`));
    return h;
  },
};

/* ---------------- saved channels ---------------- */

const MODE_LABEL = (m) => app.modes[m]?.label || m;
const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleString('ko-KR', { hour12: false }) : '–');
let savedSig = '';
let pendingClear = null;   // null | 'mode' | 'all'

async function loadSaved() {
  const d = await api('/api/saved');
  app.saved = d.channels || [];
  renderSaved(true);
}

function renderSaved(force) {
  const all = app.saved || [];
  for (const k of Object.keys(app.modes)) {
    const el = document.querySelector(`[data-saved="${k}"]`);
    const n = all.filter((r) => r.mode === k).length;
    if (el) el.textContent = n ? `저장된 채널 ${n}개` : '';
  }
  if (!app.mode) return;
  const showAll = $('#savedAll').checked;
  const list = all.filter((r) => showAll || r.mode === app.mode);
  $('#savedCnt').textContent = all.filter((r) => r.mode === app.mode).length;
  const sig = JSON.stringify([showAll, app.mode, list.map((r) => [r.mode, r.channel, r.last_locked, r.channels])]);
  if (!force && sig === savedSig) return;
  savedSig = sig;
  $('#savedSub').textContent = showAll ? `(전체 ${list.length}개)` : `(${MODE_LABEL(app.mode)} ${list.length}개)`;
  $('#btnClearMode').textContent = `${MODE_LABEL(app.mode)} 전체 삭제`;
  if (!list.length) {
    $('#savedBody').innerHTML = '<div class="empty" style="padding:12px 14px">저장된 채널이 없습니다. 주파수를 튜닝해 lock이 잡히면 자동으로 저장됩니다.</div>';
    return;
  }
  $('#savedBody').innerHTML = table(
    ['캡처', '모드', 'CH', '주파수', '변조', '가상채널', '채널 이름', 'Program', 'TSID', 'SNR', '최초 lock', '마지막 lock', ''],
    list.map((r) => {
      const ch = r.channels || [];
      const virt = ch.map((c) => `<b>${esc(c.virtual)}</b>`).join('<br>') || '–';
      const names = ch.map((c) => esc(c.short_name) + (c.long_name && c.long_name !== c.short_name ? ` <span class="muted">${esc(c.long_name)}</span>` : '')).join('<br>') || '<span class="muted">(VCT 없음)</span>';
      const prog = ch.map((c) => c.program).join('<br>') || '–';
      return `<tr>
        <td>${thumb(r.capture)}</td>
        <td>${esc(MODE_LABEL(r.mode))}</td><td class="m">${r.channel}</td><td class="m">${r.freq_mhz} MHz</td><td>${esc(r.modulation)}</td>
        <td class="m">${virt}</td><td>${names}</td><td class="m">${prog}</td>
        <td class="m">${r.tsid ?? '–'}</td><td class="m">${r.snr_db == null ? '–' : r.snr_db.toFixed(1) + ' dB'}</td>
        <td class="m">${fmtTime(r.first_locked)}</td><td class="m">${fmtTime(r.last_locked)}</td>
        <td class="m"><button class="ghost mini" data-act="tune" data-mode="${r.mode}" data-ch="${r.channel}" data-f="${r.freq_mhz}" data-mod="${esc(r.modulation)}">튜닝</button>
          <button class="danger mini" data-act="del" data-mode="${r.mode}" data-ch="${r.channel}">삭제</button></td></tr>`;
    }));
}

function askClear(kind) {
  pendingClear = kind;
  const n = (app.saved || []).filter((r) => kind === 'all' || r.mode === app.mode).length;
  $('#clearMsg').textContent = kind === 'all' ? `저장된 채널 ${n}개를 모두 삭제할까요?` : `${MODE_LABEL(app.mode)} 저장 채널 ${n}개를 삭제할까요?`;
  $('#clearBox').hidden = true;
  $('#clearConfirm').hidden = false;
}

function cancelClear() {
  pendingClear = null;
  $('#clearBox').hidden = false;
  $('#clearConfirm').hidden = true;
}

async function doClear() {
  const kind = pendingClear;
  cancelClear();
  await api('/api/saved/clear', kind === 'all' ? {} : { mode: app.mode });
  await loadSaved();
}

/* ---------------- auto scan / reception log ---------------- */

const RES_TEXT = { locked: 'LOCK', weak: '불안정', nolock: 'no lock', error: '오류' };
const thumb = (name) => name
  ? `<a href="/capture/${encodeURIComponent(name)}" target="_blank" rel="noopener"><img class="thumb" loading="lazy" src="/capture/${encodeURIComponent(name)}" alt="${esc(name)}"></a>`
  : '<span class="muted">–</span>';
const fzBadge = (fz) => !fz ? '<span class="muted">–</span>'
  : fz.frozen ? `<span class="fz">❄ 정지 ${fz.duration.toFixed(1)}초${fz.to_end ? '+' : ''}</span>`
  : `<span class="fz-ok">정상</span> <span class="muted">${fz.window.toFixed(0)}초 분석</span>`;
const thumbF = (name, fz) => fz?.frozen
  ? `<span class="thumb-wrap">${thumb(name)}<span class="thumb-fz">❄ 정지</span></span>` : thumb(name);
const fmtBer = (b) => (b == null ? '–' : b === 0 ? '0' : Number(b).toExponential(2));
const fmtRf = (v) => (v == null ? '–' : `${Number(v).toFixed(1)} dBm`);
const fmtSnr = (e) => e.snr_avg == null ? '–' : `${e.snr_avg.toFixed(1)} dB <span class="muted">(min ${e.snr_min.toFixed(1)})</span>`;
let scanSig = '';
let scanHidden = false;
app.scanView = '';          // '' = current/latest scan of this mode, otherwise a saved scan id
app.scanCache = {};
app.scanList = [];

const fmtStamp = (t) => (t ? new Date(t * 1000).toLocaleString('ko-KR', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '');

async function loadScanList() {
  const d = await api('/api/scans');
  app.scanList = d.scans || [];
  renderScanHist();
}

function renderScanHist() {
  const live = app.status?.scan;
  const list = app.scanList.filter((x) => x.mode === app.mode && (!live || x.id !== live.id));
  const opts = [];
  if (live && live.mode === app.mode) opts.push(`<option value="">${live.running ? '진행 중인 스캔' : '최근 스캔'} · ${fmtStamp(live.started)}</option>`);
  for (const x of list) {
    opts.push(`<option value="${esc(x.id)}">${fmtStamp(x.started)} · CH ${x.from}~${x.to} · lock ${x.locked}/${x.done}${x.cancelled ? ' (중단)' : ''}</option>`);
  }
  const sel = $('#scanHist');
  const keep = app.scanView;
  sel.innerHTML = opts.join('') || '<option value="">스캔 기록 없음</option>';
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
  else { app.scanView = sel.options[0]?.value || ''; sel.value = app.scanView; }
}

function viewedScan() {
  const live = app.status?.scan;
  if (!app.scanView) return live && live.mode === app.mode ? live : null;
  return app.scanCache[app.scanView] || null;
}

function renderScan() {
  const live = app.status?.scan;
  const btn = $('#btnScan');
  btn.textContent = live?.running ? '■ 스캔 중지' : '▶ 자동 스캔';
  btn.classList.toggle('running', !!live?.running);
  for (const id of ['#scanFrom', '#scanTo', '#scanTimeout', '#scanOnlyLocked']) $(id).disabled = !!live?.running;
  if (scanHidden && !live?.running) { $('#scanPanel').hidden = true; return; }
  const sc = viewedScan();
  if (!sc) {
    $('#scanPanel').hidden = scanHidden || !app.scanList.some((x) => x.mode === app.mode);
    if (!$('#scanPanel').hidden && app.scanView) fetchScan(app.scanView);
    return;
  }
  $('#scanPanel').hidden = false;
  const done = sc.results.length;
  $('#scanBar').style.width = `${sc.total ? (done / sc.total) * 100 : 0}%`;
  const elapsed = ((sc.finished || app.status.server_time) - sc.started).toFixed(0);
  $('#scanTitle').textContent = sc.running
    ? `자동 스캔 중 · ${sc.index}/${sc.total}` + (sc.current ? ` · CH ${sc.current.channel} (${sc.current.freq_mhz} MHz)` : '')
    : `자동 스캔 ${sc.cancelled ? '중단됨' : '완료'} · ${done}/${sc.total}`;
  $('#scanInfo').innerHTML = esc(`${MODE_LABEL(sc.mode)} · CH ${sc.from ?? '?'}~${sc.to ?? '?'}${sc.only_locked ? ' (이전 lock 채널만)' : ''} · lock 대기 ${sc.timeout}초 · lock ${sc.locked}개 · ${elapsed}초`)
    + (sc.frozen ? ` · <span class="fz">❄ 영상 정지 ${sc.frozen}개</span>` : '')
    + (sc.skipped?.length ? ` · <span class="muted" title="CH ${sc.skipped.join(', ')}">수신기 주파수 범위 밖 ${sc.skipped.length}개 제외</span>` : '');
  for (const [id, fmt] of [['#expCsv', 'csv'], ['#expJson', 'json'], ['#expZip', 'zip']]) {
    const a = $(id);
    a.href = sc.id ? `/api/scans/${encodeURIComponent(sc.id)}/export?format=${fmt}` : '#';
    a.classList.toggle('disabled', !sc.id || !done);
  }
  $('#btnScanDel').disabled = !!sc.running || !app.scanList.some((x) => x.id === sc.id);
  const sig = `${sc.id}|${done}|${sc.running}|${sc.frozen}`;
  if (sig === scanSig) return;
  scanSig = sig;
  const rows = sc.results.slice().reverse();
  $('#scanBody').innerHTML = table(['캡처', 'CH', '주파수', '결과', '영상', 'lock 시간', 'SNR (평균)', 'RF 레벨', '신호 품질', 'BER', 'UCB', 'TEI', '가상채널', 'TSID'],
    rows.map((e) => `<tr class="${e.freeze?.frozen ? 'frozen' : ''}">
      <td>${thumbF(e.capture, e.freeze)}</td><td class="m">${e.channel}</td><td class="m">${e.freq_mhz} MHz</td>
      <td class="res-${e.result}">${RES_TEXT[e.result] || e.result}</td><td>${e.result === 'locked' ? fzBadge(e.freeze) : '<span class="muted">–</span>'}</td>
      <td class="m">${e.lock_time_s == null ? '–' : e.lock_time_s + ' s'}</td><td class="m">${fmtSnr(e)}</td>
      <td class="m">${fmtRf(e.rf_dbm_avg)}</td>
      <td class="m">${e.strength_pct == null ? '–' : e.strength_pct + ' %'}</td><td class="m">${fmtBer(e.ber)}</td><td class="m">${e.ucb ?? '–'}</td>
      <td class="m">${e.tei ?? '–'}</td>
      <td class="m">${(e.virtual_channels || []).map(esc).join('<br>') || '–'}</td><td class="m">${e.tsid ?? '–'}</td></tr>`),
    sc.running ? '첫 주파수를 스캔하는 중…' : '결과 없음');
}

async function fetchScan(id) {
  if (!id || app.scanCache[id] || fetchScan.busy === id) return;
  fetchScan.busy = id;
  try { app.scanCache[id] = await api(`/api/scans/${encodeURIComponent(id)}`); } finally { fetchScan.busy = null; }
  renderScan();
}

function fillRange() {
  const list = app.channels[app.mode] || [];
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('atscscan.range.' + app.mode) || '{}'); } catch (e) { /* ignore */ }
  const opts = list.map((c) => `<option value="${c.channel}">${c.channel} (${c.center_frequency_mhz} MHz)</option>`).join('');
  $('#scanFrom').innerHTML = opts;
  $('#scanTo').innerHTML = opts;
  const has = (v) => list.some((c) => String(c.channel) === String(v));
  $('#scanFrom').value = has(saved.from) ? saved.from : list[0]?.channel;
  $('#scanTo').value = has(saved.to) ? saved.to : list[list.length - 1]?.channel;
  $('#scanOnlyLocked').checked = !!saved.onlyLocked;
}

function saveRange() {
  try {
    localStorage.setItem('atscscan.range.' + app.mode, JSON.stringify({ from: $('#scanFrom').value, to: $('#scanTo').value,
      onlyLocked: $('#scanOnlyLocked').checked }));
  } catch (e) { /* ignore */ }
}

async function loadLogs() {
  const d = await api('/api/logs?limit=200');
  $('#logBody').innerHTML = table(['시각', '구분', '모드', 'CH', '주파수', '결과', '영상', 'SNR (평균)', '마지막 SNR', 'RF 레벨', '신호 품질', 'BER', 'UCB', '비트레이트', '가상채널', 'TSID', '캡처'],
    (d.entries || []).map((e) => `<tr>
      <td class="m">${esc(e.time)}</td><td>${e.source === 'schedule' ? '스케줄' : e.source === 'scan' ? '자동' : '수동'}</td><td>${esc(MODE_LABEL(e.mode))}</td>
      <td class="m">${e.channel}</td><td class="m">${e.freq_mhz} MHz</td>
      <td class="res-${e.result}">${RES_TEXT[e.result] || e.result}</td><td>${e.freeze ? fzBadge(e.freeze) : '<span class="muted">–</span>'}</td>
      <td class="m">${fmtSnr(e)}</td><td class="m">${e.snr_last == null ? '–' : e.snr_last.toFixed(1) + ' dB'}</td>
      <td class="m">${fmtRf(e.rf_dbm_avg)}</td>
      <td class="m">${e.strength_pct == null ? '–' : e.strength_pct + ' %'}</td><td class="m">${fmtBer(e.ber)}</td><td class="m">${e.ucb ?? '–'}</td>
      <td class="m">${fmtRate(e.bitrate)}</td><td class="m">${(e.virtual_channels || []).map(esc).join(', ') || '–'}</td>
      <td class="m">${e.tsid ?? '–'}</td><td>${thumbF(e.capture, e.freeze)}</td></tr>`),
    '아직 기록이 없습니다.');
}

async function toggleScan() {
  if (app.status?.scan?.running) { await api('/api/scan/stop', {}); return; }
  scanHidden = false;
  let from = Number($('#scanFrom').value), to = Number($('#scanTo').value);
  if (from > to) { [from, to] = [to, from]; $('#scanFrom').value = from; $('#scanTo').value = to; saveRange(); }
  app.scanView = '';
  const r = await api('/api/scan/start', { mode: app.mode, timeout: Number($('#scanTimeout').value), from, to,
    only_locked: $('#scanOnlyLocked').checked });
  if (r.error) { alert(r.error); return; }
  app.status = { ...(app.status || {}), scan: r.scan };
  renderScan();
}

/* ---------------- loop ---------------- */

async function poll() {
  try {
    app.status = await api('/api/status');
    const s = app.status.session;
    if (s && s.psip_rev !== app.psipRev) {
      app.psip = await api('/api/psip');
      app.psipRev = s.psip_rev;
    }
    const savedKeys = Object.entries(app.status.results || {}).filter(([, v]) => v.saved).map(([k, v]) => k + '@' + (v.names || []).join()).join('|');
    if (savedKeys !== app.savedKeys) { app.savedKeys = savedKeys; await loadSaved(); }
    const scanKey = `${app.status.scan?.id}|${app.status.scan?.running}`;
    if (scanKey !== app.scanKey) { app.scanKey = scanKey; if (app.mode) loadScanList(); }
    const nres = (app.status.scan?.results || []).length + '|' + (s ? s.started + s.state : '');
    if (nres !== app.lastRes) { app.lastRes = nres; if (!$('#logPanel').hidden) loadLogs(); }
    if (app.mode) { renderGrid(); renderStatus(); renderPsip(); renderScan(); }
    if (typeof onMonitorStatus === 'function') onMonitorStatus(app.status);
  } catch (e) {
    $('#subtitle').textContent = '백엔드 연결 끊김 — 재시도 중…';
  }
  app.timer = setTimeout(poll, 1000);
}

async function tune(ch, f, mode, modulation) {
  app.psip = null; app.psipRev = -1;
  if (mode && mode !== app.mode) showScan(mode);
  if (modulation) $('#modSel').value = modulation;
  const r = await api('/api/tune', { mode: app.mode, channel: ch, freq_mhz: f, modulation: $('#modSel').value });
  if (r.error) { alert(r.error); return; }
  app.status = { ...(app.status || {}), session: r.session };
  renderGrid(); renderStatus(); renderPsip();
  $('#detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function init() {
  const d = await api('/api/channels');
  app.modes = d.modes; app.channels = d.channels;
  for (const k of Object.keys(d.channels)) {
    const el = document.querySelector(`[data-count="${k}"]`);
    if (el) el.textContent = d.channels[k].length;
  }
  document.querySelectorAll('.mode-card').forEach((b) => b.addEventListener('click', () => showScan(b.dataset.mode)));
  $('#btnHome').addEventListener('click', showMode);
  $('#btnStop').addEventListener('click', () => api('/api/stop', {}));
  $('#grid').addEventListener('click', (e) => {
    const b = e.target.closest('.ch');
    if (b) tune(Number(b.dataset.ch), Number(b.dataset.f));
  });
  $('#btnSaved').addEventListener('click', () => { $('#savedPanel').hidden = !$('#savedPanel').hidden; });
  const [lo, hi] = [2, 10];
  let tsel = 4;
  try { tsel = Number(localStorage.getItem('atscscan.timeout')) || 4; } catch (e) { /* ignore */ }
  $('#scanTimeout').innerHTML = Array.from({ length: hi - lo + 1 }, (_, i) => lo + i)
    .map((v) => `<option value="${v}" ${v === tsel ? 'selected' : ''}>${v}초</option>`).join('');
  $('#scanTimeout').addEventListener('change', (e) => {
    try { localStorage.setItem('atscscan.timeout', e.target.value); } catch (err) { /* ignore */ }
  });
  $('#btnScan').addEventListener('click', toggleScan);
  $('#scanFrom').addEventListener('change', saveRange);
  $('#scanOnlyLocked').addEventListener('change', saveRange);
  $('#scanTo').addEventListener('change', saveRange);
  $('#btnScanView').addEventListener('click', () => {
    scanHidden = !$('#scanPanel').hidden;
    if (!scanHidden) loadScanList();
    renderScan();
  });
  $('#scanHist').addEventListener('change', (e) => { app.scanView = e.target.value; scanSig = ''; fetchScan(app.scanView); renderScan(); });
  $('#btnScanDel').addEventListener('click', async () => {
    const sc = viewedScan();
    if (!sc?.id || !confirm(`스캔 기록 ${sc.id}을(를) 삭제할까요? (캡처 이미지와 수신 로그는 남습니다)`)) return;
    const r = await api('/api/scans/delete', { id: sc.id });
    if (r.error) { alert(r.error); return; }
    delete app.scanCache[sc.id];
    if (app.status?.scan?.id === sc.id) app.status.scan = null;
    app.scanView = ''; scanSig = '';
    await loadScanList();
    renderScan();
  });
  $('#btnScanHide').addEventListener('click', () => { scanHidden = true; $('#scanPanel').hidden = true; });
  $('#btnLogs').addEventListener('click', () => { const p = $('#logPanel'); p.hidden = !p.hidden; if (!p.hidden) loadLogs(); });
  $('#btnLogRefresh').addEventListener('click', loadLogs);
  $('#savedAll').addEventListener('change', () => renderSaved(true));
  $('#btnClearMode').addEventListener('click', () => askClear('mode'));
  $('#btnClearAll').addEventListener('click', () => askClear('all'));
  $('#btnClearYes').addEventListener('click', doClear);
  $('#btnClearNo').addEventListener('click', cancelClear);
  $('#savedBody').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    if (b.dataset.act === 'tune') {
      tune(Number(b.dataset.ch), Number(b.dataset.f), b.dataset.mode, b.dataset.mod);
    } else if (b.dataset.act === 'del') {
      b.disabled = true;
      await api('/api/saved/delete', { mode: b.dataset.mode, channel: Number(b.dataset.ch) });
      await loadSaved();
    }
  });
  $('#tabs').addEventListener('click', (e) => {
    const b = e.target.closest('.tab');
    if (b) { app.tab = b.dataset.tab; renderPsip(true); }
  });
  let saved = null;
  try { saved = localStorage.getItem('atscscan.mode'); } catch (e) { /* ignore */ }
  await loadSaved();
  if (saved && app.modes[saved]) showScan(saved);
  poll();
}

init();
