'use strict';
/* SBS 헤드엔드 모니터: alerts, schedules, RF trend charts. Uses helpers from app.js ($, esc, api, table, app). */

const mon = {
  counts: { active: 0, unacked: 0, critical: 0 },
  alertSig: '',
  schedules: [],
  editId: null,
  trMode: 'terrestrial',
  trDays: 7,
  trCh: null,
  summary: [],
  hist: null,
  hover: null,
};

const SEV = {
  critical: { icon: '✕', label: '위험', cls: 'sev-critical' },
  warning: { icon: '▲', label: '주의', cls: 'sev-warning' },
  good: { icon: '●', label: '정상', cls: 'sev-good' },
};
const KIND_TEXT = { nolock: 'lock 실패', weak: '불안정', snr_drop: 'SNR 하락', strength_drop: '품질 하락', freeze: '영상 정지' };
const sevTag = (sev) => `<span class="sev ${SEV[sev].cls}">${SEV[sev].icon} ${SEV[sev].label}</span>`;
const modeLabel = (m) => app.modes?.[m]?.label || m;
const parseT = (s) => (s ? new Date(s.replace(' ', 'T')) : null);
const show = (id, on) => { $('#' + id).hidden = !on; };

/* ---------------- status hook (called by app.js every second) ---------------- */

function onMonitorStatus(st) {
  const m = st?.monitor;
  if (!m) return;
  if (m.title && $('#appTitle').textContent !== m.title) { $('#appTitle').textContent = m.title; document.title = m.title; }
  mon.counts = m.alerts;
  const btn = $('#btnAlerts');
  $('#alertCnt').textContent = m.alerts.unacked;
  btn.classList.toggle('has-critical', m.alerts.critical > 0);
  btn.classList.toggle('has-alert', m.alerts.unacked > 0);
  const sig = JSON.stringify(m.alerts);
  if (sig !== mon.alertSig) {
    mon.alertSig = sig;
    loadAlerts();
    if (!$('#trendPanel').hidden) loadTrend(false);
  }
  if (!$('#schedPanel').hidden) renderSchedState(m.scheduler);
}

/* ---------------- alerts ---------------- */

async function loadAlerts() {
  const d = await api(`/api/alerts?state=${$('#alertState').value}`);
  const list = d.alerts || [];
  const open = list.filter((a) => !a.acked && !a.resolved);
  // banner: unacknowledged open alerts
  const crit = open.filter((a) => a.severity === 'critical');
  if (open.length) {
    const top = (crit[0] || open[0]);
    $('#alertBanner').innerHTML = `${sevTag(top.severity)} 확인하지 않은 경보 <b>${open.length}</b>건 ·
      ${esc(modeLabel(top.mode))} CH${top.channel} ${esc((top.virtual_channels || []).join(', '))} — ${esc(top.message)}
      <button class="ghost mini" id="bannerOpen">경보 보기</button>`;
    $('#alertBanner').className = 'alert-banner ' + (crit.length ? 'critical' : 'warning');
    $('#alertBanner').hidden = false;
    $('#bannerOpen').onclick = () => openPanel('alertPanel');
  } else {
    $('#alertBanner').hidden = true;
  }
  $('#alertSub').textContent = `(해제 안 됨 ${d.counts.active}건 · 미확인 ${d.counts.unacked}건)`;
  $('#alertBody').innerHTML = table(['시각', '심각도', '내용', '모드', 'CH', '주파수', '가상채널', '측정 SNR', '기준 SNR', '상태', ''],
    list.map((a) => `<tr class="${a.resolved ? 'dim' : ''}">
      <td class="m">${esc(a.time)}</td><td>${sevTag(a.severity)}</td>
      <td class="wrap"><b>${esc(KIND_TEXT[a.kind] || a.kind)}</b> · ${esc(a.message)}</td>
      <td>${esc(modeLabel(a.mode))}</td><td class="m">${a.channel}</td><td class="m">${a.freq_mhz} MHz</td>
      <td>${esc((a.virtual_channels || []).join(', ')) || '–'}</td>
      <td class="m">${a.snr == null ? '–' : a.snr + ' dB'}</td><td class="m">${a.baseline ? a.baseline.snr + ' dB' : '–'}</td>
      <td class="m">${a.resolved ? '해제 ' + esc(a.resolved.slice(5, 16)) : a.acked ? '확인 ' + esc(a.acked.slice(5, 16)) : '<b>미확인</b>'}</td>
      <td>${!a.acked && !a.resolved ? `<button class="ghost mini" data-ack="${a.id}">확인</button>` : ''}
          <button class="ghost mini" data-trend="${a.mode}:${a.channel}">추이</button></td></tr>`),
    $('#alertState').value === 'active' ? '현재 경보가 없습니다.' : '경보 기록이 없습니다.');
}

async function loadMonitorInfo() {
  const d = await api('/api/monitor');
  const t = d.config.telegram;
  const last = d.telegram_last;
  $('#tgStatus').textContent = !t.configured ? '텔레그램: config.json 미설정'
    : !t.enabled ? '텔레그램: 꺼짐' : `텔레그램: 수신자 ${t.chat_count}명`
      + (last ? ` · 마지막 전송 ${last.time.slice(5, 16)} ${last.ok ? '성공' : '실패'}` : '');
  $('#tgStatus').title = last?.error || '';
  mon.config = d.config;
}

/* ---------------- schedules ---------------- */

async function loadSchedules() {
  const d = await api('/api/schedules');
  mon.schedules = d.schedules || [];
  renderSchedState(d.scheduler);
  $('#schedBody').innerHTML = table(['사용', '이름', 'cron', '다음 실행', '모드', '범위', 'lock 대기', '마지막 실행', '결과', ''],
    mon.schedules.map((s) => `<tr class="${s.enabled ? '' : 'dim'}">
      <td><input type="checkbox" data-toggle="${s.id}" ${s.enabled ? 'checked' : ''} aria-label="사용"></td>
      <td><b>${esc(s.name)}</b></td><td class="m">${esc(s.cron)}</td>
      <td class="m">${s.error ? `<span class="f-err">${esc(s.error)}</span>` : esc(s.next_run || '–')}</td>
      <td>${esc(modeLabel(s.mode))}</td><td class="m">CH ${s.from}~${s.to}${s.only_locked ? '<br><span class="muted">이전 lock만</span>' : ''}</td><td class="m">${s.timeout}초</td>
      <td class="m">${esc(s.last_run || '–')}</td><td>${esc(s.last_result || '–')}</td>
      <td class="m"><button class="ghost mini" data-run="${s.id}">지금 실행</button>
        <button class="ghost mini" data-edit="${s.id}">수정</button>
        <button class="danger mini" data-del="${s.id}">삭제</button></td></tr>`),
    '등록된 스케줄이 없습니다. "+ 새 스케줄"로 추가하세요.');
}

function renderSchedState(sch) {
  if (!sch) return;
  const name = (id) => mon.schedules.find((s) => s.id === id)?.name || id;
  const parts = [];
  if (!sch.enabled) parts.push('스케줄러 꺼짐(--no-scheduler)');
  if (sch.current) parts.push(`실행 중: ${name(sch.current)}`);
  if (sch.queue?.length) parts.push(`대기: ${sch.queue.map(name).join(', ')}`);
  $('#schedSub').textContent = parts.length ? `(${parts.join(' · ')})` : `(${mon.schedules.length}개)`;
}

function fillChannelSelect(sel, mode, value) {
  const list = app.channels[mode] || [];
  sel.innerHTML = list.map((c) => `<option value="${c.channel}">${c.channel} (${c.center_frequency_mhz} MHz)</option>`).join('');
  if (value != null) sel.value = value;
}

function openSchedForm(s) {
  mon.editId = s?.id || null;
  const mode = s?.mode || app.mode || 'terrestrial';
  $('#sfMode').innerHTML = Object.entries(app.modes).map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join('');
  $('#sfMode').value = mode;
  const list = app.channels[mode] || [];
  fillChannelSelect($('#sfFrom'), mode, s?.from ?? list[0]?.channel);
  fillChannelSelect($('#sfTo'), mode, s?.to ?? list[list.length - 1]?.channel);
  $('#sfTimeout').innerHTML = [2, 3, 4, 5, 6, 7, 8, 9, 10].map((v) => `<option value="${v}">${v}초</option>`).join('');
  $('#sfTimeout').value = s?.timeout ?? 4;
  $('#sfName').value = s?.name || '';
  $('#sfCron').value = s?.cron || '0 6 * * *';
  $('#sfEnabled').checked = s ? !!s.enabled : true;
  $('#sfOnlyLocked').checked = !!s?.only_locked;
  $('#sfPreset').value = '';
  $('#sfErr').textContent = '';
  show('schedForm', true);
  previewCron();
  $('#sfName').focus();
}

let cronTimer = null;
function previewCron() {
  clearTimeout(cronTimer);
  cronTimer = setTimeout(async () => {
    const d = await api('/api/schedules/preview', { cron: $('#sfCron').value });
    $('#sfPreview').innerHTML = d.error ? `<span class="f-err">${esc(d.error)}</span>`
      : `다음 실행: ${d.next.map(esc).join(' · ') || '없음'}`;
  }, 250);
}

async function saveSched(ev) {
  ev.preventDefault();
  const sched = {
    id: mon.editId, name: $('#sfName').value, cron: $('#sfCron').value, mode: $('#sfMode').value,
    from: Number($('#sfFrom').value), to: Number($('#sfTo').value), timeout: Number($('#sfTimeout').value),
    enabled: $('#sfEnabled').checked, only_locked: $('#sfOnlyLocked').checked,
  };
  const r = await api('/api/schedules/save', { schedule: sched });
  if (r.error) { $('#sfErr').textContent = r.error; return; }
  show('schedForm', false);
  loadSchedules();
}

/* ---------------- RF trend ---------------- */

async function loadTrend(resetSel = true) {
  const d = await api(`/api/history/summary?mode=${mon.trMode}`);
  mon.summary = d.channels || [];
  const thr = mon.config?.alerts?.snr_drop_db ?? 3;
  $('#trendSub').textContent = `(${modeLabel(mon.trMode)} · 수신 기록이 있는 채널 ${mon.summary.length}개)`;
  $('#trList').innerHTML = table(['상태', 'CH', '주파수', '가상채널', '마지막 측정', 'SNR', '기준', 'Δ', '품질', '최근 30회'],
    mon.summary.map((c) => {
      const l = c.last;
      const sev = c.alerts.some((a) => a.severity === 'critical') ? 'critical'
        : (c.alerts.length || l.result !== 'locked' || l.frozen) ? 'warning' : 'good';
      const delta = l.result === 'locked' && c.baseline && l.snr_avg != null ? +(l.snr_avg - c.baseline.snr).toFixed(1) : null;
      const deltaCls = delta == null ? '' : delta <= -thr ? 'neg' : '';
      return `<tr class="pick ${mon.trCh === c.channel ? 'sel' : ''}" data-ch="${c.channel}" tabindex="0">
        <td>${sevTag(sev)}</td><td class="m"><b>${c.channel}</b></td><td class="m">${c.freq_mhz} MHz</td>
        <td>${esc(c.virtual_channels.join(', ')) || '–'}</td>
        <td class="m">${esc((l.time || '').slice(5, 16))}</td>
        <td class="m">${l.result !== 'locked' ? `<span class="sev sev-critical">${esc(l.result)}</span>` : (l.snr_avg ?? '–') + ' dB'}</td>
        <td class="m">${c.baseline ? c.baseline.snr + ' dB' : '<span class="muted">–</span>'}</td>
        <td class="m ${deltaCls}">${delta == null ? '–' : (delta > 0 ? '+' : '') + delta}</td>
        <td class="m">${l.strength_pct == null ? '–' : l.strength_pct + '%'}</td>
        <td>${spark(c.spark)}</td></tr>`;
    }), '아직 수신 기록이 없습니다. 자동 스캔이나 튜닝을 하면 쌓입니다.');
  if (resetSel && !mon.summary.some((c) => c.channel === mon.trCh)) mon.trCh = mon.summary[0]?.channel ?? null;
  if (mon.trCh != null) loadHistory(mon.trCh);
}

function spark(vals) {
  const W = 90, H = 22, v = vals.filter((x) => x != null);
  if (!v.length) return '<span class="muted">–</span>';
  const lo = Math.min(...v) - 1, hi = Math.max(...v) + 1;
  const pts = vals.map((x, i) => (x == null ? null : [(i / Math.max(1, vals.length - 1)) * (W - 4) + 2, H - 2 - ((x - lo) / (hi - lo)) * (H - 4)]));
  let d = '', pen = false;
  for (const p of pts) { if (!p) { pen = false; continue; } d += (pen ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1); pen = true; }
  const miss = pts.map((p, i) => (p ? '' : `<line x1="${(i / Math.max(1, vals.length - 1)) * (W - 4) + 2}" x2="${(i / Math.max(1, vals.length - 1)) * (W - 4) + 2}" y1="${H - 5}" y2="${H}" class="sp-miss"/>`)).join('');
  return `<svg class="spark-mini" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true"><path d="${d}" class="sp-line"/>${miss}</svg>`;
}

async function loadHistory(ch) {
  mon.trCh = ch;
  document.querySelectorAll('#trList tr.pick').forEach((r) => r.classList.toggle('sel', Number(r.dataset.ch) === ch));
  const d = await api(`/api/history?mode=${mon.trMode}&channel=${ch}&days=${mon.trDays}`);
  mon.hist = d.entries || [];
  const c = mon.summary.find((x) => x.channel === ch);
  $('#trTitle').innerHTML = `<b>CH ${ch}</b> · ${c ? c.freq_mhz + ' MHz' : ''} · ${esc(c?.virtual_channels.join(', ') || '')}
    <span class="muted">· ${mon.trDays ? '최근 ' + mon.trDays + '일' : '전체'} ${mon.hist.length}회 측정</span>`;
  drawCharts();
  if (!$('#trTable').hidden) renderTrendTable();
}

function niceTicks(lo, hi, n = 4) {
  const span = hi - lo || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}

function drawCharts() {
  const h = mon.hist || [];
  const thr = mon.config?.alerts?.snr_drop_db ?? 3;
  const base = [...h].reverse().find((e) => e.baseline)?.baseline || mon.summary.find((x) => x.channel === mon.trCh)?.baseline;
  chart($('#chSnr'), h, { key: 'snr_avg', label: 'SNR (평균)', unit: 'dB', fixed: [0, 40], baseline: base?.snr,
    threshold: base ? base.snr - thr : null });
  chart($('#chStr'), h, { key: 'strength_pct', label: '신호 품질', unit: '%', fixed: [0, 100], baseline: base?.strength });
  const fails = h.filter((e) => e.result !== 'locked').length;
  $('#trNote').textContent = h.length
    ? `기준선 = 최근 lock ${base?.count ?? 0}회 중앙값 · 점선(주의) = 기준 −${thr} dB · ✕ = lock 실패/불안정 ${fails}회 · ❄ = 영상 정지 ${h.filter((e) => e.freeze?.frozen).length}회`
    : '';
}

function chart(el, h, o) {
  const W = Math.max(300, el.clientWidth || 600), H = 190, L = 44, R = 70, T = 24, B = 26;
  if (!h.length) { el.innerHTML = `<div class="empty">측정 기록 없음</div>`; return; }
  const ts = h.map((e) => parseT(e.time).getTime());
  let x0 = Math.min(...ts), x1 = Math.max(...ts);
  if (x1 - x0 < 3600e3) { x0 -= 1800e3; x1 += 1800e3; }
  const vals = h.map((e) => (e.result === 'locked' ? e[o.key] : null)).filter((v) => v != null);
  let lo = o.fixed[0], hi = o.fixed[1];
  if (vals.length) {           // zoom to the data but keep the baseline visible
    const all = vals.concat(o.baseline != null ? [o.baseline] : [], o.threshold != null ? [o.threshold] : []);
    lo = Math.max(o.fixed[0], Math.floor(Math.min(...all) - 3));
    hi = Math.min(o.fixed[1], Math.ceil(Math.max(...all) + 3));
  }
  const X = (t) => L + ((t - x0) / (x1 - x0)) * (W - L - R);
  const Y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const yt = niceTicks(lo, hi);
  const days = (x1 - x0) / 86400e3;
  const hhmm = (d) => `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  const fmtX = (t) => {
    const d = new Date(t);
    if (days > 5) return `${d.getMonth() + 1}/${d.getDate()}`;
    if (days > 0.9) return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}시`;
    return hhmm(d);
  };
  const xt = [];
  const nt = W < 520 ? 3 : 5;                  // fewer ticks on narrow screens
  for (let i = 0; i < nt; i++) {               // drop ticks whose label repeats the previous one
    const t = x0 + (i / (nt - 1)) * (x1 - x0);
    if (!xt.length || fmtX(t) !== fmtX(xt[xt.length - 1])) xt.push(t);
  }
  let path = '', pen = false;
  const pts = [];
  h.forEach((e, i) => {
    const v = e.result === 'locked' ? e[o.key] : null;
    if (v == null) { pen = false; return; }
    const p = [X(ts[i]), Y(v)];
    pts.push({ i, x: p[0], y: p[1] });
    path += (pen ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1);
    pen = true;
  });
  const fails = h.map((e, i) => (e.result !== 'locked' ? `<text x="${X(ts[i]).toFixed(1)}" y="${H - B - 4}" class="ev-fail" text-anchor="middle">✕</text>` : '')).join('')
    + (o.key === 'snr_avg' ? h.map((e, i) => (e.freeze?.frozen ? `<text x="${X(ts[i]).toFixed(1)}" y="${T + 12}" class="ev-fz" text-anchor="middle">❄</text>` : '')).join('') : '');
  const dots = pts.length <= 80 ? pts.map((p) => `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3.5" class="dot"/>`).join('') : '';
  const bl = o.baseline != null ? `<line x1="${L}" x2="${W - R}" y1="${Y(o.baseline)}" y2="${Y(o.baseline)}" class="base"/>
    <text x="${W - R + 6}" y="${Y(o.baseline) + 4}" class="lab">기준 ${o.baseline}${o.unit === '%' ? '%' : ''}</text>` : '';
  const th = o.threshold != null && o.threshold > lo ? `<line x1="${L}" x2="${W - R}" y1="${Y(o.threshold)}" y2="${Y(o.threshold)}" class="thr"/>
    <text x="${W - R + 6}" y="${Y(o.threshold) + 4}" class="lab thr-lab">주의 ${o.threshold.toFixed(1)}</text>` : '';
  el.innerHTML = `<div class="ch-label">${esc(o.label)} <span class="muted">(${o.unit})</span></div>
  <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(o.label)} 추이">
    ${yt.map((v) => `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" class="grid"/><text x="${L - 6}" y="${Y(v) + 4}" class="ax" text-anchor="end">${v}</text>`).join('')}
    ${xt.map((t) => `<text x="${X(t)}" y="${H - 6}" class="ax" text-anchor="middle">${fmtX(t)}</text>`).join('')}
    ${th}${bl}
    <path d="${path}" class="series"/>${dots}${fails}
    <line class="xhair" y1="${T}" y2="${H - B}" x1="-10" x2="-10"/>
    <circle class="hdot" r="5" cx="-10" cy="-10"/>
    <rect class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}"/>
  </svg><div class="tip" hidden></div>`;
  const svg = el.querySelector('svg'), tip = el.querySelector('.tip');
  const hit = el.querySelector('.hit');
  const move = (ev) => {
    const r = svg.getBoundingClientRect();
    const px = (ev.clientX - r.left) * (W / r.width);
    let best = 0, bd = Infinity;
    ts.forEach((t, i) => { const dd = Math.abs(X(t) - px); if (dd < bd) { bd = dd; best = i; } });
    syncHover(best);
  };
  hit.addEventListener('mousemove', move);
  hit.addEventListener('mouseleave', () => syncHover(null));
  el._hover = (i) => {
    const xl = el.querySelector('.xhair'), hd = el.querySelector('.hdot');
    if (i == null) { xl.setAttribute('x1', -10); xl.setAttribute('x2', -10); hd.setAttribute('cx', -10); tip.hidden = true; return; }
    const e = h[i], x = X(ts[i]), v = e.result === 'locked' ? e[o.key] : null;
    xl.setAttribute('x1', x); xl.setAttribute('x2', x);
    hd.setAttribute('cx', v == null ? -10 : x); hd.setAttribute('cy', v == null ? -10 : Y(v));
    tip.innerHTML = `<b>${esc(e.time)}</b><br>${e.result === 'locked' ? `${esc(o.label)} <b>${v ?? '–'}${o.unit === '%' ? '%' : ' dB'}</b>` : `<span class="sev sev-critical">✕ ${esc(e.result)}</span>`}
      ${o.key === 'snr_avg' && e.snr_min != null ? `<br>최소 ${e.snr_min} dB` : ''}
      ${e.freeze?.frozen ? `<br><span class="fz">❄ 영상 정지 ${e.freeze.duration}초</span>` : ''}
      <br><span class="muted">${e.source === 'schedule' ? '스케줄' : e.source === 'scan' ? '자동 스캔' : '수동'}${e.ucb != null ? ' · UCB ' + e.ucb : ''}</span>`;
    tip.hidden = false;
    const r = svg.getBoundingClientRect();
    const left = (x / W) * r.width;
    tip.style.left = Math.min(r.width - 170, Math.max(0, left + 12)) + 'px';
    tip.style.top = '28px';
  };
}

function syncHover(i) {
  for (const id of ['#chSnr', '#chStr']) $(id)._hover?.(i);
}

function renderTrendTable() {
  const h = (mon.hist || []).slice().reverse();
  $('#trTable').innerHTML = table(['시각', '구분', '결과', '영상', 'SNR 평균', 'SNR 최소', '품질', 'UCB', '기준 SNR', '캡처'],
    h.map((e) => `<tr class="${e.freeze?.frozen ? 'frozen' : ''}"><td class="m">${esc(e.time)}</td><td>${e.source === 'schedule' ? '스케줄' : e.source === 'scan' ? '자동' : '수동'}</td>
      <td class="res-${e.result}">${esc(RES_TEXT[e.result] || e.result)}</td><td>${e.freeze ? fzBadge(e.freeze) : '<span class="muted">–</span>'}</td>
      <td class="m">${e.snr_avg ?? '–'}</td><td class="m">${e.snr_min ?? '–'}</td><td class="m">${e.strength_pct ?? '–'}</td>
      <td class="m">${e.ucb ?? '–'}</td><td class="m">${e.baseline ? e.baseline.snr : '–'}</td><td>${thumbF(e.capture, e.freeze)}</td></tr>`));
}

/* ---------------- panels & events ---------------- */

function openPanel(id) {
  for (const p of ['alertPanel', 'schedPanel', 'trendPanel']) show(p, p === id && $('#' + p).hidden);
  if (!$('#alertPanel').hidden) { loadMonitorInfo(); loadAlerts(); }
  if (!$('#schedPanel').hidden) loadSchedules();
  if (!$('#trendPanel').hidden) {
    $('#trMode').innerHTML = Object.entries(app.modes).map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join('');
    $('#trMode').value = mon.trMode;
    loadMonitorInfo().then(() => loadTrend());
  }
  if (!$('#' + id).hidden) $('#' + id).scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function initMonitor() {
  if (!app.modes || !Object.keys(app.modes).length) { setTimeout(initMonitor, 200); return; }
  if (app.mode) mon.trMode = app.mode;
  $('#btnAlerts').addEventListener('click', () => openPanel('alertPanel'));
  $('#btnSched').addEventListener('click', () => openPanel('schedPanel'));
  $('#btnTrend').addEventListener('click', () => openPanel('trendPanel'));
  document.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => show(b.dataset.close, false)));
  $('#alertState').addEventListener('change', loadAlerts);
  $('#btnAckAll').addEventListener('click', async () => { await api('/api/alerts/ack', { all: true }); loadAlerts(); });
  $('#btnTgTest').addEventListener('click', async (ev) => {
    ev.target.disabled = true;
    const r = await api('/api/telegram/test', {});
    ev.target.disabled = false;
    $('#tgStatus').textContent = r.ok ? '텔레그램 테스트 전송 성공' : `텔레그램 실패: ${r.error}`;
  });
  $('#alertBody').addEventListener('click', async (ev) => {
    const b = ev.target.closest('button');
    if (!b) return;
    if (b.dataset.ack) { await api('/api/alerts/ack', { id: b.dataset.ack }); loadAlerts(); }
    if (b.dataset.trend) {
      const [m, c] = b.dataset.trend.split(':');
      mon.trMode = m; mon.trCh = Number(c);
      show('trendPanel', false); openPanel('trendPanel');
    }
  });
  // schedules
  $('#btnSchedNew').addEventListener('click', () => openSchedForm(null));
  $('#sfCancel').addEventListener('click', () => show('schedForm', false));
  $('#schedForm').addEventListener('submit', saveSched);
  $('#sfCron').addEventListener('input', () => { $('#sfPreset').value = ''; previewCron(); });
  $('#sfPreset').addEventListener('change', (e) => { if (e.target.value) { $('#sfCron').value = e.target.value; previewCron(); } });
  $('#sfMode').addEventListener('change', (e) => {
    const list = app.channels[e.target.value] || [];
    fillChannelSelect($('#sfFrom'), e.target.value, list[0]?.channel);
    fillChannelSelect($('#sfTo'), e.target.value, list[list.length - 1]?.channel);
  });
  $('#schedBody').addEventListener('click', async (ev) => {
    const t = ev.target;
    if (t.dataset.toggle) {
      const s = mon.schedules.find((x) => x.id === t.dataset.toggle);
      await api('/api/schedules/save', { schedule: { ...s, enabled: t.checked } });
      return loadSchedules();
    }
    if (t.dataset.run) { const r = await api('/api/schedules/run', { id: t.dataset.run }); if (!r.ok) alert('이미 실행 중이거나 대기 중입니다'); return loadSchedules(); }
    if (t.dataset.edit) return openSchedForm(mon.schedules.find((x) => x.id === t.dataset.edit));
    if (t.dataset.del) {
      const s = mon.schedules.find((x) => x.id === t.dataset.del);
      if (!confirm(`스케줄 "${s.name}"을(를) 삭제할까요?`)) return;
      await api('/api/schedules/delete', { id: s.id });
      return loadSchedules();
    }
  });
  // trend
  $('#trMode').addEventListener('change', (e) => { mon.trMode = e.target.value; mon.trCh = null; loadTrend(); });
  $('#trDays').addEventListener('change', (e) => { mon.trDays = Number(e.target.value); if (mon.trCh != null) loadHistory(mon.trCh); });
  $('#btnTrRefresh').addEventListener('click', () => loadTrend(false));
  $('#trList').addEventListener('click', (ev) => { const r = ev.target.closest('tr.pick'); if (r) loadHistory(Number(r.dataset.ch)); });
  $('#trList').addEventListener('keydown', (ev) => { const r = ev.target.closest('tr.pick'); if (r && ev.key === 'Enter') loadHistory(Number(r.dataset.ch)); });
  $('#btnTrTable').addEventListener('click', () => {
    const t = $('#trTable'); t.hidden = !t.hidden;
    $('#btnTrTable').textContent = t.hidden ? '표 보기' : '표 닫기';
    if (!t.hidden) renderTrendTable();
  });
  let rz = null;
  window.addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(() => { if (!$('#trendPanel').hidden) drawCharts(); }, 150); });
  loadMonitorInfo();
  loadAlerts();
}

initMonitor();
