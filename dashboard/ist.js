// IST Dashboard - began as a copy of the native Zoho "IST Dashboard", then
// absorbed the Weekly Review (what moved, needs attention). Every section
// follows the Period filter: a preset or any From/To dates.
// GET only; never writes to Zoho. See ist.py for the "what's replicated vs.
// redefined" notes, and weekly.py's DEALS_AS_OF for how past periods work.

// Chart colors, defaults and baseGrid() come from charts.js.

const state = { owner: '', start: null, end: null, tab: 'stuck', attention: null, stageRows: [] };

// Local-date ISO (YYYY-MM-DD) - toISOString() would shift to UTC and land
// on the previous day for most of the IST morning.
const isoDate = (d) => d.toLocaleDateString('en-CA');
const todayISO = () => isoDate(new Date());
// A period that ended before today: deal state is rebuilt "as of" its end.
const isPast = () => state.end < todayISO();

function fmtCr(inr) {
  if (inr === null || inr === undefined) return '—';
  const cr = inr / 1e7;
  if (Math.abs(cr) >= 1) return `₹${cr.toLocaleString('en-IN', { maximumFractionDigits: 2 })} Cr`;
  const lakh = inr / 1e5;
  if (Math.abs(lakh) >= 1) return `₹${lakh.toLocaleString('en-IN', { maximumFractionDigits: 2 })} L`;
  return `₹${Math.round(inr).toLocaleString('en-IN')}`;
}
function fmtInt(n) { return (n ?? 0).toLocaleString('en-IN'); }
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function delta(pct) {
  if (pct === null || pct === undefined) return '<span class="delta flat">no prior period</span>';
  if (pct === 0) return '<span class="delta flat">flat vs previous period</span>';
  const up = pct > 0;
  return `<span class="delta ${up ? 'up' : 'down'}">${up ? '▲' : '▼'} ${Math.abs(pct)}%</span>` +
         ` <span class="card-note">vs previous period</span>`;
}

// Every call carries the Owner and Period filters; endpoints that don't use
// one simply ignore it.
async function getJSON(path) {
  const params = [];
  if (state.owner) params.push(`owner=${encodeURIComponent(state.owner)}`);
  if (state.start && state.end) params.push(`start=${state.start}`, `end=${state.end}`);
  const sep = path.includes('?') ? '&' : '?';
  const url = params.length ? `${path}${sep}${params.join('&')}` : path;
  // Tied to the current load (see loadAll): a newer filter change aborts
  // this request, so a slow old response can never overwrite a newer one.
  const res = await fetch(url, { signal: state.controller?.signal });
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return res.json();
}

function shortDate(iso) {
  if (!iso) return '—';
  return new Date(iso + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
}

function fmtDay(iso, withYear = false) {
  return new Date(iso + 'T00:00:00').toLocaleDateString('en-IN',
    withYear ? { day: 'numeric', month: 'short', year: 'numeric' } : { day: 'numeric', month: 'short' });
}

function periodLabel() {
  if (state.start === state.end) return fmtDay(state.start, true);
  const sameYear = state.start.slice(0, 4) === state.end.slice(0, 4);
  return `${fmtDay(state.start, !sameYear)} – ${fmtDay(state.end, true)}`;
}

// Last day of the period - the "as of" date for past-period views.
function periodEnd() {
  return fmtDay(state.end, true);
}

// ---------- top bar ----------

async function initTopbar() {
  const statusEl = document.getElementById('sync-status');
  try {
    const meta = await fetch('/api/meta').then(r => r.json());
    const lastSync = meta.last_sync?.map((r) => r.last_run_at).sort().pop();
    statusEl.textContent = lastSync
      ? `synced ${new Date(lastSync).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' })}`
      : 'connected';
    statusEl.classList.replace('pill-muted', 'pill-good');
    return true;
  } catch (e) {
    statusEl.textContent = 'API unreachable — run: python scripts/serve.py';
    console.error(e);
    return false;
  }
}

document.getElementById('notes-toggle').addEventListener('click', () => {
  document.getElementById('notes-panel').hidden = false;
});
document.getElementById('notes-close').addEventListener('click', () => {
  document.getElementById('notes-panel').hidden = true;
});

// ---------- KPI row ----------

function setKPI(index, value, subHtml) {
  const card = document.querySelectorAll('.kpi-card')[index];
  card.classList.remove('skeleton');
  card.querySelector('.kpi-value').textContent = value;
  let subEl = card.querySelector('.kpi-sub');
  if (!subEl) { subEl = document.createElement('span'); subEl.className = 'kpi-sub'; card.appendChild(subEl); }
  subEl.innerHTML = subHtml;
}

async function loadKPIs() {
  const { kpis: k, target: t, movement: m } = await getJSON('/api/weekly/summary');

  setKPI(0, fmtInt(k.leads.value), delta(k.leads.delta_pct));
  setKPI(1, fmtCr(k.revenue.value), `${k.revenue.deals} won &middot; ${delta(k.revenue.delta_pct)}`);
  setKPI(2, fmtInt(k.pipeline.deals), `${fmtCr(k.pipeline.value)} &middot; ${fmtCr(k.pipeline.weighted)} weighted`);
  setKPI(3, fmtInt(k.accounts.value), delta(k.accounts.delta_pct));

  renderTarget(t);
  renderMoveSummary(m);
}

// ---------- Deal movement (summary + what moved) ----------

function renderMoveSummary(m) {
  const cells = [
    ['New in pipeline', m.entered_pipeline, ''],
    ['Progressed', m.progressed, ''],
    ['Won', m.closed_won, fmtCr(m.won_value)],
    ['Lost', m.closed_lost, fmtCr(m.lost_value)],
  ];
  document.getElementById('move-summary').innerHTML = cells.map(([label, n, sub]) =>
    `<div><strong>${fmtInt(n)}</strong>${esc(label)}${sub ? ` &middot; ${sub}` : ''}</div>`).join('');
}

async function loadMovement() {
  const { rows } = await getJSON('/api/weekly/movement');
  const el = document.getElementById('move-list');
  if (!rows.length) { el.innerHTML = '<div class="empty">No stage changes in this period.</div>'; return; }
  el.innerHTML = rows.map((r) => {
    let cls = 'moved', label = 'Moved';
    if (!r.from_stage) { cls = 'new'; label = 'New'; }
    else if (r.to_stage === 'Closed Won') { cls = 'won'; label = 'Won'; }
    else if (String(r.to_stage).startsWith('Closed Lost')) { cls = 'lost'; label = 'Lost'; }
    const path = r.from_stage
      ? `${esc(r.from_stage)}<span class="arrow">→</span>${esc(r.to_stage)}`
      : `entered at ${esc(r.to_stage)}`;
    return `<div class="move-row">
      <div>
        <div class="move-name">${esc(r.deal_name)}</div>
        <div class="move-path">${path}</div>
      </div>
      <div style="text-align:right">
        <span class="mtag ${cls}"><span class="dot"></span>${label}</span>
        <div class="move-amt">${fmtCr(r.amount_inr)} &middot; ${esc(r.owner_name ?? '—')}</div>
      </div>
    </div>`;
  }).join('');
}

// ---------- Needs attention ----------

function renderAttention() {
  const el = document.getElementById('attention');
  const a = state.attention;
  if (!a) return;

  if (state.tab === 'stuck') {
    const rows = a.stuck;
    if (!rows.length) return void (el.innerHTML = '<div class="empty">Nothing stuck. Every open deal moved in the last 21 days.</div>');
    el.innerHTML = `<table><thead><tr>
        <th>Deal</th><th>Stage</th><th>Owner</th>
        <th class="num">Value</th><th class="num">Days since it moved</th></tr></thead><tbody>
      ${rows.map((r) => `<tr>
        <td>${esc(r.deal_name)}</td><td>${esc(r.stage)}</td><td>${esc(r.owner_name ?? '—')}</td>
        <td class="num">${fmtCr(r.amount_inr)}</td>
        <td class="num age ${r.days_stuck >= 90 ? 'bad' : r.days_stuck >= 45 ? 'warn' : ''}">${r.days_stuck ?? '—'}</td>
      </tr>`).join('')}</tbody></table>`;

  } else if (state.tab === 'slipped') {
    const rows = a.slipped;
    if (!rows.length) return void (el.innerHTML = '<div class="empty">No close dates pushed out in this period.</div>');
    el.innerHTML = `<table><thead><tr>
        <th>Deal</th><th>Stage</th><th>Owner</th>
        <th class="num">Was</th><th class="num">Now</th><th class="num">Slipped</th></tr></thead><tbody>
      ${rows.map((r) => `<tr>
        <td>${esc(r.deal_name)}</td><td>${esc(r.stage)}</td><td>${esc(r.owner_name ?? '—')}</td>
        <td class="num">${shortDate(r.was)}</td><td class="num">${shortDate(r.now)}</td>
        <td class="num age ${r.slipped_days >= 30 ? 'bad' : 'warn'}">+${r.slipped_days}d</td>
      </tr>`).join('')}</tbody></table>`;

  } else {
    const rows = a.closing_soon;
    if (!rows.length) return void (el.innerHTML = '<div class="empty">Nothing scheduled to close in the next 30 days.</div>');
    el.innerHTML = `<table><thead><tr>
        <th>Deal</th><th>Stage</th><th>Owner</th>
        <th class="num">Value</th><th class="num">Prob.</th><th class="num">Closes</th></tr></thead><tbody>
      ${rows.map((r) => `<tr>
        <td>${esc(r.deal_name)}</td><td>${esc(r.stage)}</td><td>${esc(r.owner_name ?? '—')}</td>
        <td class="num">${fmtCr(r.amount_inr)}</td>
        <td class="num">${r.probability ?? '—'}%</td>
        <td class="num">${shortDate(r.closing_date)} <span class="card-note">(${r.days_to_close}d)</span></td>
      </tr>`).join('')}</tbody></table>`;
  }
}

async function loadAttention() {
  state.attention = await getJSON('/api/weekly/attention');
  document.getElementById('attention-note').textContent = isPast()
    ? `Stuck and closing-soon as they stood on ${periodEnd()}`
    : 'The action list for the review call';
  renderAttention();
}

document.querySelectorAll('.tab').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((b) => b.setAttribute('aria-selected', String(b === btn)));
    state.tab = btn.dataset.tab;
    renderAttention();
  });
});

// ---------- Revenue target ----------

function renderTarget(t) {
  const monthName = new Date(t.month + '-01T00:00:00')
    .toLocaleDateString('en-IN', { month: 'long', year: 'numeric' });
  document.getElementById('target-month').textContent = `${monthName} · ${t.owner === 'All' ? 'Entire Org' : t.owner}`;
  document.getElementById('target-actual').textContent = fmtCr(t.actual_inr);
  document.getElementById('target-of').textContent = t.configured ? `of ${fmtCr(t.target_inr)} target` : 'no target configured';

  const pct = t.achieved_pct ?? 0;
  const fill = document.getElementById('target-fill');
  fill.style.width = `${Math.min(pct, 100)}%`;
  fill.classList.toggle('met', pct >= 100);

  document.getElementById('target-pct').textContent = t.configured ? `${pct}% achieved` : '';
  document.getElementById('target-gap').textContent =
    t.configured ? (t.gap_inr > 0 ? `${fmtCr(t.gap_inr)} to go` : 'target met') : '';
}

// ---------- Lead journey ----------

function renderFunnel(j) {
  document.getElementById('funnel').innerHTML = j.steps.map((s, i) => `
    <div class="fstep">
      <div class="fbar" style="background:${CHART.ramp[i] ?? CHART.ramp[4]}"></div>
      <div class="fcount">${fmtInt(s.count)}</div>
      <div class="fname">${esc(s.stage)}</div>
      <div class="frate">${s.rate_from_prev !== null ? s.rate_from_prev + '% from previous' : '&nbsp;'}</div>
    </div>`).join('');
  document.getElementById('journey-note').textContent =
    `Lead → Contacted → Converted → Action → Won · overall ${j.overall_pct}%`;
}

async function loadJourney() {
  renderFunnel(await getJSON('/api/weekly/journey?scope=all'));
}

// ---------- Leads by source ----------

async function loadSources(scope) {
  const { rows } = await getJSON(`/api/weekly/sources?scope=${scope}`);
  const el = document.getElementById('source-list');
  if (!rows.length) {
    el.innerHTML = `<div class="empty">No leads created ${scope === 'week' ? 'in this period' : 'yet'}.</div>`;
    return;
  }
  // Ranked bars, top 10 + "Other": one hue, so length carries the
  // comparison instead of a 12-color donut.
  const top = rows.slice(0, 10);
  const rest = rows.slice(10).reduce((s, r) => s + r.n, 0);
  const items = top.map(r => ({ label: r.source, n: r.n })).concat(rest ? [{ label: 'Other', n: rest }] : []);
  const total = rows.reduce((s, r) => s + r.n, 0) || 1;
  const max = Math.max(...items.map(r => r.n));
  el.innerHTML = items.map(r => `
    <div class="hbar" title="${esc(r.label)}: ${fmtInt(r.n)} (${(r.n / total * 100).toFixed(1)}%)">
      <span class="hbar-label">${esc(r.label)}</span>
      <span class="hbar-track"><span class="hbar-fill" style="width:${(r.n / max * 100).toFixed(1)}%"></span></span>
      <span class="hbar-value">${fmtInt(r.n)} <span class="hbar-pct">${(r.n / total * 100).toFixed(0)}%</span></span>
    </div>`).join('');
}

// ---------- Deal movement (by stage, split by pipeline) ----------

let stagesChart;
function renderStages(rows, pipeline) {
  const filtered = rows.filter(r => r.pipeline === pipeline).sort((a, b) => a.stage_order - b.stage_order);
  const ctx = document.getElementById('chart-stages');
  // Horizontal, so long stage names read without tilting
  fitBarHeight(ctx, filtered.length);
  stagesChart?.destroy();
  stagesChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: filtered.map(r => r.stage),
      datasets: [{ label: 'Deals', data: filtered.map(r => r.deal_count), backgroundColor: CHART.series[0] }],
    },
    options: {
      indexAxis: 'y',
      interaction: { mode: 'nearest', axis: 'y', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (c) => [`Deals: ${fmtInt(filtered[c.dataIndex].deal_count)}`, `Value: ${fmtCr(filtered[c.dataIndex].value_inr)}`] } },
      },
      scales: {
        x: baseGrid({ title: { display: true, text: 'Deals', color: CHART.tick } }),
        y: baseGrid({ grid: { display: false } }),
      },
    },
  });
}

// Rows live in state so the pipeline dropdown always draws the latest
// fetch - a handler holding the first fetch's rows kept showing the
// original owner's data after the Owner filter changed.
const stageSel = document.getElementById('stage-pipeline');
stageSel.addEventListener('change', () => renderStages(state.stageRows, stageSel.value));

async function loadStages() {
  const { rows } = await getJSON('/api/ist/deals_by_stage');
  state.stageRows = rows;
  const pipelines = [...new Set(rows.map(r => r.pipeline))].sort();
  const keep = pipelines.includes(stageSel.value) ? stageSel.value : pipelines[0];
  stageSel.innerHTML = pipelines.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
  stageSel.value = keep ?? '';
  document.getElementById('stage-note').textContent = (isPast()
    ? `Every deal's stage as of ${periodEnd()}, rebuilt from deal history`
    : 'All deals by stage (open and closed)') +
    ', for the selected pipeline — never blended across pipelines.';
  renderStages(rows, keep);
}

// ---------- Last 3 weeks performance monitor ----------

async function loadPerformance() {
  const { rows } = await getJSON('/api/ist/performance?weeks=3');
  const weekLabel = (r) => {
    const s = new Date(r.week_start + 'T00:00:00'), e = new Date(r.week_end + 'T00:00:00');
    const opts = { day: '2-digit', month: '2-digit', year: 'numeric' };
    return `${s.toLocaleDateString('en-GB', opts)} -<br>${e.toLocaleDateString('en-GB', opts)}`;
  };

  document.getElementById('perf-head').innerHTML =
    '<th></th>' + rows.map(weekLabel).map(l => `<th class="num">${l}</th>`).join('');

  const metrics = [
    ['Leads Created', r => fmtInt(r.leads_created)],
    ['Deals Entering Pipeline', r => fmtInt(r.deals_entered)],
    ['Deals Won', r => fmtInt(r.deals_won)],
    ['Revenue Won', r => fmtCr(r.revenue_won_inr)],
    ['New Open Value', r => fmtCr(r.open_value_inr)],
  ];
  document.getElementById('perf-body').innerHTML = metrics.map(([label, fn]) => `
    <tr><td>${label}</td>${rows.map(r => `<td class="num">${fn(r)}</td>`).join('')}</tr>
  `).join('');
}

// ---------- Leads by lead status ----------

let statusChart;
async function loadLeadStatus() {
  const { rows } = await getJSON('/api/ist/lead_status');
  document.getElementById('status-note').textContent = isPast()
    ? `Leads created by ${periodEnd()}, with today's status (Zoho keeps no status history)` : '';
  const ctx = document.getElementById('chart-status');
  fitBarHeight(ctx, rows.length, 22);
  statusChart?.destroy();
  statusChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: rows.map(r => r.status),
      datasets: [{ label: 'Leads', data: rows.map(r => r.n), backgroundColor: CHART.series[0] }],
    },
    options: {
      indexAxis: 'y',
      interaction: { mode: 'index', axis: 'y', intersect: false },
      plugins: { legend: { display: false } },
      scales: { x: baseGrid({ title: { display: true, text: 'Leads', color: CHART.tick } }), y: baseGrid({ grid: { display: false } }) },
    },
  });
}

// ---------- owner filter ----------

// "Admin MitKat" owns 3,341 of 10,547 leads at a 38% conversion rate vs.
// 1-9% for every named rep with real volume (found during the ML baseline
// experiment) - almost certainly a bulk-import/default queue, not a
// person. Labeled here too so filtering by it doesn't read as filtering
// to an individual rep.
const NON_INDIVIDUAL_OWNERS = new Set(['Admin MitKat']);
function ownerLabel(name) {
  return NON_INDIVIDUAL_OWNERS.has(name) ? `${name} (not an individual — bulk/default queue)` : name;
}

// Period presets -> [start, end] as local dates. Weeks run Monday to Sunday
// and the fiscal year April to March, matching the rest of the app.
function presetRange(preset) {
  const t = new Date(); t.setHours(0, 0, 0, 0);
  const day = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const monday = day(t, -((t.getDay() + 6) % 7));
  switch (preset) {
    case 'last-week': return [day(monday, -7), day(monday, -1)];
    case 'this-month': return [new Date(t.getFullYear(), t.getMonth(), 1), new Date(t.getFullYear(), t.getMonth() + 1, 0)];
    case 'last-month': return [new Date(t.getFullYear(), t.getMonth() - 1, 1), new Date(t.getFullYear(), t.getMonth(), 0)];
    case 'last-90': return [day(t, -89), t];
    case 'this-fy': {
      const fyStart = t.getMonth() >= 3 ? t.getFullYear() : t.getFullYear() - 1;
      return [new Date(fyStart, 3, 1), new Date(fyStart + 1, 2, 31)];
    }
    default: return [monday, day(monday, 6)]; // this-week
  }
}

async function initFilters() {
  const { owners } = await getJSON('/api/weekly/options');

  const sel = document.getElementById('owner');
  sel.innerHTML = '<option value="">All Users</option>' + owners.map(o => `<option value="${esc(o)}">${esc(ownerLabel(o))}</option>`).join('');
  sel.addEventListener('change', async () => {
    state.owner = sel.value;
    await loadAll();
  });

  const periodSel = document.getElementById('period');
  const from = document.getElementById('date-from');
  const to = document.getElementById('date-to');
  const applyPreset = () => {
    const [a, b] = presetRange(periodSel.value);
    state.start = from.value = isoDate(a);
    state.end = to.value = isoDate(b);
  };
  applyPreset();

  periodSel.addEventListener('change', async () => {
    if (periodSel.value === 'custom') { from.focus(); return; }
    applyPreset();
    await loadAll();
  });
  // Picking a date switches the preset to "Custom range". Only a complete
  // pair reloads; From after To is swapped rather than rejected.
  // Debounced, so setting From and then To in quick succession is one load.
  let dateTimer;
  const onDate = () => {
    clearTimeout(dateTimer);
    dateTimer = setTimeout(() => {
      if (!from.value || !to.value) return;
      periodSel.value = 'custom';
      [state.start, state.end] = from.value <= to.value ? [from.value, to.value] : [to.value, from.value];
      from.value = state.start; to.value = state.end;
      loadAll();
    }, 400);
  };
  from.addEventListener('change', onDate);
  to.addEventListener('change', onDate);
}

function renderSubtitle() {
  const who = state.owner ? ownerLabel(state.owner) : 'all reps';
  document.getElementById('subtitle').textContent = `${periodLabel()} · ${who}`;
}

document.getElementById('source-scope').addEventListener('change', (e) => loadSources(e.target.value));

// ---------- hot leads ----------

async function loadHotLeads() {
  const { rows } = await getJSON('/api/leads/hot?limit=15');
  document.getElementById('hot-note').textContent = isPast()
    ? `Leads still open on ${periodEnd()} · scored as of today` : 'Respects the Owner filter above';
  const tbody = document.querySelector('#hot-leads-table tbody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-row">No open leads to score for this owner.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${esc(r.name)}</td>
      <td>${esc(r.company)}</td>
      <td>${esc(r.lead_source)}</td>
      <td>${esc(r.industry)}</td>
      <td class="num">${r.days_since_update.toFixed(0)}d ago</td>
      <td class="num">${r.score}</td>
    </tr>`).join('');
}

// ---------- segment lookup ----------

async function runSegmentLookup() {
  const dimension = document.getElementById('segment-dimension').value;
  const value = document.getElementById('segment-value').value.trim();
  const out = document.getElementById('segment-result');
  if (!value) {
    out.innerHTML = '<span class="seg-error">Enter a value to look up.</span>';
    return;
  }
  out.innerHTML = '<span class="card-note">looking up&hellip;</span>';
  try {
    const data = await fetch(`/api/segments/diagnostic?dimension=${encodeURIComponent(dimension)}&value=${encodeURIComponent(value)}`).then(r => r.json());
    if (data.error) {
      out.innerHTML = `<span class="seg-error">${esc(data.error)}</span>`;
      return;
    }
    const isPipeline = data.dimension === 'pipeline';
    const rate = isPipeline ? data.win_rate : data.conversion_rate;
    const companyRate = isPipeline ? data.company_win_rate : data.company_conversion_rate;
    const metricLabel = isPipeline ? 'win rate' : 'conversion rate';
    const variantsNote = data.matched_variants
      ? `<div class="seg-note">Matched variants merged: ${data.matched_variants.map(esc).join(', ')}</div>` : '';
    const warnNote = data.sample_size_warning ? `<div class="seg-warn">${esc(data.sample_size_warning)}</div>` : '';
    out.innerHTML = `
      <div class="seg-headline">${esc(data.value)}: ${(rate * 100).toFixed(1)}% ${metricLabel}
        vs ${(companyRate * 100).toFixed(1)}% company average (${data.vs_company})</div>
      <div class="seg-note">${esc(data.note || '')}</div>
      ${variantsNote}${warnNote}
    `;
  } catch (e) {
    out.innerHTML = '<span class="seg-error">Lookup failed - see console.</span>';
    console.error(e);
  }
}
document.getElementById('segment-go').addEventListener('click', runSegmentLookup);
document.getElementById('segment-value').addEventListener('keydown', (e) => { if (e.key === 'Enter') runSegmentLookup(); });

// ---------- orchestration ----------

async function loadAll() {
  state.controller?.abort();
  state.controller = new AbortController();
  renderSubtitle();
  // allSettled, not all: one failing section shouldn't leave every other
  // section stuck on its loading state.
  const results = await Promise.allSettled([
    loadKPIs(),
    loadMovement(),
    loadAttention(),
    loadJourney(),
    loadSources(document.getElementById('source-scope').value),
    loadStages(),
    loadPerformance(),
    loadLeadStatus(),
    loadHotLeads(),
  ]);
  // An aborted load was superseded by a newer one - not a failure.
  const failed = results.filter((r) => r.status === 'rejected' && r.reason?.name !== 'AbortError');
  failed.forEach((r) => console.error(r.reason));
  if (failed.length) {
    document.getElementById('sync-status').textContent =
      `${failed.length} section${failed.length === 1 ? '' : 's'} failed to load — see console`;
  }
}

async function main() {
  const session = await requireSession();
  if (!session) return; // already redirected to login.html

  const ok = await initTopbar();
  if (!ok) return;
  await initFilters();

  if (!session.is_admin) {
    // The server always scopes every endpoint on this page to the
    // logged-in user's own data regardless of this dropdown (see
    // serve.py's scoped_owner()) - lock it to avoid implying a choice
    // that isn't real.
    const sel = document.getElementById('owner');
    sel.value = session.owner_name;
    sel.disabled = true;
    state.owner = session.owner_name;
  }

  initRefreshButtons();
  await loadAll();
}

// One refresh button per card, each re-running only that card's loader.
// The KPI tiles, Revenue Target and Deal Movement all come from one
// summary request, so refreshing any of them redraws all three.
function initRefreshButtons() {
  const card = (id) => document.getElementById(id);
  document.querySelectorAll('.kpi-card').forEach((tile) => addRefresh(tile, loadKPIs));
  addRefresh(card('target-actual'), loadKPIs);
  addRefresh(card('move-summary'), loadKPIs);
  addRefresh(card('source-list'), () => loadSources(card('source-scope').value));
  addRefresh(card('funnel'), loadJourney);
  addRefresh(card('move-list'), loadMovement);
  addRefresh(card('attention'), loadAttention);
  addRefresh(card('chart-stages'), loadStages);
  addRefresh(card('perf-table'), loadPerformance);
  addRefresh(card('chart-status'), loadLeadStatus);
  addRefresh(card('hot-leads-table'), loadHotLeads);
  addRefresh(card('segment-result'), () => (card('segment-value').value.trim() ? runSegmentLookup() : Promise.resolve()));
}

main().catch(err => {
  console.error(err);
  document.getElementById('sync-status').textContent = 'Failed to load — see console';
});
