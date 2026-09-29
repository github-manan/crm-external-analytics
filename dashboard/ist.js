// IST Dashboard - replicates a native Zoho CRM Analytics dashboard using our
// own read-only API. GET only; never writes to Zoho. See ist.py for the
// "what's replicated vs. redefined" notes behind each section.

const PALETTE = ['#1d4ed8', '#0891b2', '#d97706', '#7c3aed', '#dc2626', '#059669',
                 '#be185d', '#65a30d', '#0284c7', '#ca8a04', '#9333ea', '#0d9488'];
const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
const GRID_COLOR = dark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)';
const TICK_COLOR = dark ? '#94a0b8' : '#64748b';

Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
Chart.defaults.font.size = 12;
Chart.defaults.color = TICK_COLOR;

const state = { owner: '' };

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
  if (pct === null || pct === undefined) return '<span class="delta flat">no prior week</span>';
  if (pct === 0) return '<span class="delta flat">flat vs last week</span>';
  const up = pct > 0;
  return `<span class="delta ${up ? 'up' : 'down'}">${up ? '▲' : '▼'} ${Math.abs(pct)}%</span>` +
         ` <span class="card-note">vs last week</span>`;
}

async function getJSON(path) {
  const sep = path.includes('?') ? '&' : '?';
  const url = state.owner ? `${path}${sep}owner=${encodeURIComponent(state.owner)}` : path;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return res.json();
}

function baseGrid(axis) {
  return { grid: { color: GRID_COLOR }, ticks: { color: TICK_COLOR }, ...axis };
}

// ---------- top bar ----------

async function initTopbar() {
  const statusEl = document.getElementById('sync-status');
  try {
    const meta = await fetch('/api/meta').then(r => r.json());
    const lastSync = meta.last_sync?.[0]?.last_run_at;
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
  return m;
}

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
      <div class="fbar" style="background:${PALETTE[i % PALETTE.length]}"></div>
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

let sourcesChart;
async function loadSources(scope) {
  const { rows } = await getJSON(`/api/weekly/sources?scope=${scope}`);
  const top = rows.slice(0, 10);
  const rest = rows.slice(10).reduce((s, r) => s + r.n, 0);
  const labels = top.map(r => r.source).concat(rest ? ['Other'] : []);
  const data = top.map(r => r.n).concat(rest ? [rest] : []);
  const total = rows.reduce((s, r) => s + r.n, 0) || 1;

  const ctx = document.getElementById('chart-sources');
  sourcesChart?.destroy();
  sourcesChart = new Chart(ctx, {
    type: 'doughnut',
    data: { labels, datasets: [{ data, backgroundColor: labels.map((_, i) => PALETTE[i % PALETTE.length]) }] },
    options: { plugins: { legend: { display: false } }, cutout: '60%' },
  });

  document.getElementById('source-list').innerHTML = labels.map((label, i) => `
    <div class="src-row">
      <span><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${PALETTE[i % PALETTE.length]};margin-right:6px;"></span>${esc(label)}</span>
      <span>${fmtInt(data[i])} (${(data[i] / total * 100).toFixed(1)}%)</span>
    </div>`).join('');
}

// ---------- Deal movement (by stage, split by pipeline) ----------

let stagesChart;
function renderStages(rows, pipeline) {
  const filtered = rows.filter(r => r.pipeline === pipeline).sort((a, b) => a.stage_order - b.stage_order);
  const ctx = document.getElementById('chart-stages');
  stagesChart?.destroy();
  stagesChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: filtered.map(r => r.stage),
      datasets: [{ label: 'Deals', data: filtered.map(r => r.deal_count), backgroundColor: PALETTE[2], borderRadius: 4 }],
    },
    options: {
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (c) => [`Deals: ${fmtInt(filtered[c.dataIndex].deal_count)}`, `Value: ${fmtCr(filtered[c.dataIndex].value_inr)}`] } },
      },
      scales: { x: baseGrid({ grid: { display: false } }), y: baseGrid({ title: { display: true, text: 'Record Count', color: TICK_COLOR } }) },
    },
  });
}

async function loadStages() {
  const { rows } = await getJSON('/api/ist/deals_by_stage');
  const pipelines = [...new Set(rows.map(r => r.pipeline))].sort();
  const sel = document.getElementById('stage-pipeline');
  if (!sel.dataset.filled) {
    sel.innerHTML = pipelines.map(p => `<option value="${p}">${p}</option>`).join('');
    sel.dataset.filled = '1';
    sel.addEventListener('change', () => renderStages(rows, sel.value));
  }
  renderStages(rows, sel.value || pipelines[0]);
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
  const ctx = document.getElementById('chart-status');
  statusChart?.destroy();
  statusChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: rows.map(r => r.status),
      datasets: [{ label: 'Leads', data: rows.map(r => r.n), backgroundColor: PALETTE[5], borderRadius: 4 }],
    },
    options: {
      indexAxis: 'y',
      plugins: { legend: { display: false } },
      scales: { x: baseGrid({ title: { display: true, text: 'Record Count', color: TICK_COLOR } }), y: baseGrid({ grid: { display: false } }) },
    },
  });
}

// ---------- owner filter ----------

async function initOwnerFilter() {
  const { owners } = await getJSON('/api/weekly/options');
  const sel = document.getElementById('owner');
  sel.innerHTML = '<option value="">All Users</option>' + owners.map(o => `<option value="${o}">${o}</option>`).join('');
  sel.addEventListener('change', async () => {
    state.owner = sel.value;
    await loadAll();
  });
}

document.getElementById('source-scope').addEventListener('change', (e) => loadSources(e.target.value));

// ---------- orchestration ----------

async function loadAll() {
  await Promise.all([
    loadKPIs(),
    loadJourney(),
    loadSources(document.getElementById('source-scope').value),
    loadStages(),
    loadPerformance(),
    loadLeadStatus(),
  ]);
}

async function main() {
  const ok = await initTopbar();
  if (!ok) return;
  await initOwnerFilter();
  await loadAll();
}

main().catch(err => {
  console.error(err);
  document.getElementById('sync-status').textContent = 'Failed to load — see console';
});
