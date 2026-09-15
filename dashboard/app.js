// MitKat Sales Analytics — reads the local read-only JSON API only.
// See README.md "Read this before writing any query" before changing any query here.

const API = '';

const PALETTE = ['#1d4ed8', '#0891b2', '#d97706', '#7c3aed', '#dc2626', '#059669'];
const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
const GRID_COLOR = dark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)';
const TICK_COLOR = dark ? '#94a0b8' : '#64748b';

Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
Chart.defaults.font.size = 12;
Chart.defaults.color = TICK_COLOR;
Chart.defaults.plugins.legend.labels.usePointStyle = true;

function fmtCr(inr) {
  if (inr === null || inr === undefined) return '—';
  const cr = inr / 1e7;
  if (Math.abs(cr) >= 1) return `₹${cr.toLocaleString('en-IN', { maximumFractionDigits: 2 })} Cr`;
  const lakh = inr / 1e5;
  return `₹${lakh.toLocaleString('en-IN', { maximumFractionDigits: 2 })} L`;
}

function fmtInt(n) {
  return (n ?? 0).toLocaleString('en-IN');
}

function monthName(m) {
  return ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][m - 1] ?? m;
}

async function getJSON(path) {
  const res = await fetch(API + path);
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

function baseGrid(axis) {
  return { grid: { color: GRID_COLOR }, ticks: { color: TICK_COLOR }, ...axis };
}

// ---------- top bar: connectivity + data notes ----------

async function initTopbar() {
  const statusEl = document.getElementById('sync-status');
  try {
    const meta = await getJSON('/api/meta');
    const lastSync = meta.last_sync?.[0]?.last_run_at;
    statusEl.textContent = lastSync
      ? `synced ${new Date(lastSync).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' })}`
      : 'connected';
    statusEl.classList.replace('pill-muted', 'pill-good');

    const list = document.getElementById('notes-list');
    list.innerHTML = meta.caveats.map(c => `<li>${c}</li>`).join('');
    return meta;
  } catch (e) {
    statusEl.textContent = 'API unreachable — run: python scripts/serve.py';
    console.error(e);
    return null;
  }
}

document.getElementById('notes-toggle').addEventListener('click', () => {
  document.getElementById('notes-panel').hidden = false;
});
document.getElementById('notes-close').addEventListener('click', () => {
  document.getElementById('notes-panel').hidden = true;
});

// ---------- KPI row ----------

function setKPI(index, value, sub) {
  const card = document.querySelectorAll('.kpi-card')[index];
  card.classList.remove('skeleton');
  card.querySelector('.kpi-value').textContent = value;
  if (sub) {
    let subEl = card.querySelector('.kpi-sub');
    if (!subEl) {
      subEl = document.createElement('span');
      subEl.className = 'kpi-sub';
      card.appendChild(subEl);
    }
    subEl.textContent = sub;
  }
}

async function loadKPIs(pipelines, fiscal) {
  const wonTotal = pipelines.reduce((s, p) => s + p.won_inr, 0);
  const openTotal = pipelines.reduce((s, p) => s + p.open_inr, 0);
  const openDeals = pipelines.reduce((s, p) => s + p.open_deals, 0);
  const currentFY = fiscal[fiscal.length - 1];

  setKPI(0, fmtCr(wonTotal), `${fmtInt(pipelines.reduce((s, p) => s + p.won, 0))} deals won, all-time`);
  setKPI(1, fmtCr(currentFY.revenue_inr), `${currentFY.fiscal_year} · ${fmtInt(currentFY.deals_won)} deals so far`);
  setKPI(2, fmtCr(openTotal), 'across all 4 pipelines');
  setKPI(3, fmtInt(openDeals), 'currently open');
}

// ---------- bookings by fiscal year ----------

let fiscalChart;
function renderFiscalChart(rows) {
  const ctx = document.getElementById('chart-fiscal');
  fiscalChart?.destroy();
  fiscalChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: rows.map(r => r.fiscal_year),
      datasets: [{
        label: 'Revenue (Cr)',
        data: rows.map(r => +(r.revenue_inr / 1e7).toFixed(2)),
        backgroundColor: PALETTE[0],
        borderRadius: 4,
      }],
    },
    options: {
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (c) => {
              const r = rows[c.dataIndex];
              return [`Revenue: ${fmtCr(r.revenue_inr)}`, `Deals won: ${fmtInt(r.deals_won)}`];
            },
          },
        },
      },
      scales: {
        x: baseGrid({ grid: { display: false } }),
        y: baseGrid({ title: { display: true, text: '₹ Crore', color: TICK_COLOR } }),
      },
    },
  });
}

// ---------- pipeline mix ----------

let pipelinesChart;
function renderPipelinesChart(rows) {
  const ctx = document.getElementById('chart-pipelines');
  pipelinesChart?.destroy();
  pipelinesChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: rows.map(r => r.pipeline),
      datasets: [
        { label: 'Won', data: rows.map(r => +(r.won_inr / 1e7).toFixed(2)), backgroundColor: PALETTE[0], borderRadius: 4 },
        { label: 'Open', data: rows.map(r => +(r.open_inr / 1e7).toFixed(2)), backgroundColor: PALETTE[2], borderRadius: 4 },
      ],
    },
    options: {
      plugins: { legend: { position: 'bottom' } },
      scales: {
        x: baseGrid({ grid: { display: false } }),
        y: baseGrid({ title: { display: true, text: '₹ Crore', color: TICK_COLOR } }),
      },
    },
  });
}

// ---------- monthly trend (pipeline filter) ----------

let monthlyChart;
async function loadMonthlyChart(pipeline) {
  const q = pipeline ? `?pipeline=${encodeURIComponent(pipeline)}` : '';
  const { rows } = await getJSON('/api/bookings/monthly' + q);
  const ctx = document.getElementById('chart-monthly');
  monthlyChart?.destroy();
  monthlyChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: rows.map(r => r.closing_month),
      datasets: [{
        label: 'Revenue (Cr)',
        data: rows.map(r => +(r.revenue_inr / 1e7).toFixed(2)),
        borderColor: PALETTE[0],
        backgroundColor: PALETTE[0] + '22',
        fill: true,
        tension: 0.25,
        pointRadius: 0,
      }],
    },
    options: {
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (c) => {
              const r = rows[c.dataIndex];
              return [`Revenue: ${fmtCr(r.revenue_inr)}`, `Deals won: ${fmtInt(r.deals_won)}`, r.fiscal_year];
            },
          },
        },
      },
      scales: {
        x: baseGrid({ ticks: { color: TICK_COLOR, maxTicksLimit: 14 }, grid: { display: false } }),
        y: baseGrid({ title: { display: true, text: '₹ Crore', color: TICK_COLOR } }),
      },
    },
  });
}

// ---------- open pipeline by stage (pipeline filter) ----------

let stagesChart;
async function loadStagesChart(pipeline, allRows) {
  const rows = allRows
    .filter(r => r.pipeline === pipeline)
    .sort((a, b) => a.stage_order - b.stage_order);

  const ctx = document.getElementById('chart-stages');
  stagesChart?.destroy();
  stagesChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: rows.map(r => r.stage),
      datasets: [{
        label: 'Open value (Cr)',
        data: rows.map(r => +(r.value_inr / 1e7).toFixed(2)),
        backgroundColor: PALETTE[1],
        borderRadius: 4,
      }],
    },
    options: {
      indexAxis: 'y',
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (c) => {
              const r = rows[c.dataIndex];
              return [`Value: ${fmtCr(r.value_inr)}`, `Weighted: ${fmtCr(r.weighted_inr)}`, `Deals: ${fmtInt(r.deal_count)}`];
            },
          },
        },
      },
      scales: {
        x: baseGrid({ title: { display: true, text: '₹ Crore', color: TICK_COLOR } }),
        y: baseGrid({ grid: { display: false } }),
      },
    },
  });
}

// ---------- seasonality ----------

let seasonalityChart;
function renderSeasonalityChart(rows) {
  const ctx = document.getElementById('chart-seasonality');
  seasonalityChart?.destroy();
  seasonalityChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: rows.map(r => monthName(r.month)),
      datasets: [{
        data: rows.map(r => +(r.revenue_inr / 1e7).toFixed(2)),
        backgroundColor: rows.map(r => (r.month === 4 || r.month === 3) ? PALETTE[2] : PALETTE[0]),
        borderRadius: 4,
      }],
    },
    options: {
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (c) => {
              const r = rows[c.dataIndex];
              return [`Revenue: ${fmtCr(r.revenue_inr)}`, `Deals won: ${fmtInt(r.deals_won)}`];
            },
          },
        },
      },
      scales: {
        x: baseGrid({ grid: { display: false } }),
        y: baseGrid({ title: { display: true, text: '₹ Crore', color: TICK_COLOR } }),
      },
    },
  });
}

// ---------- open pipeline history ----------

let historyChart;
function renderHistoryChart(rows) {
  const ctx = document.getElementById('chart-history');
  historyChart?.destroy();

  if (rows.length < 2) {
    ctx.getContext('2d').save();
    historyChart = new Chart(ctx, {
      type: 'line',
      data: { labels: rows.map(r => r.snapshot_date), datasets: [{ label: 'Open value (Cr)', data: rows.map(r => +(r.value_inr / 1e7).toFixed(2)), borderColor: PALETTE[3], backgroundColor: PALETTE[3], pointRadius: 5 }] },
      options: {
        plugins: {
          legend: { display: false },
          subtitle: { display: true, text: 'Only one day captured so far — this fills in day by day.', color: TICK_COLOR },
        },
        scales: { x: baseGrid({ grid: { display: false } }), y: baseGrid({ title: { display: true, text: '₹ Crore', color: TICK_COLOR } }) },
      },
    });
    return;
  }

  historyChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: rows.map(r => r.snapshot_date),
      datasets: [{
        label: 'Open value (Cr)',
        data: rows.map(r => +(r.value_inr / 1e7).toFixed(2)),
        borderColor: PALETTE[3],
        backgroundColor: PALETTE[3] + '22',
        fill: true,
        tension: 0.2,
      }],
    },
    options: {
      plugins: { legend: { display: false } },
      scales: { x: baseGrid({ grid: { display: false } }), y: baseGrid({ title: { display: true, text: '₹ Crore', color: TICK_COLOR } }) },
    },
  });
}

// ---------- reps table ----------

async function loadRepsTable(fiscalYear) {
  const q = fiscalYear ? `?fiscal_year=${encodeURIComponent(fiscalYear)}` : '';
  const { rows } = await getJSON('/api/reps' + q);
  const tbody = document.querySelector('#reps-table tbody');

  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-row">No reps with activity in this period.</td></tr>';
    return;
  }

  const maxRevenue = Math.max(...rows.map(r => r.revenue_inr), 1);
  tbody.innerHTML = rows
    .sort((a, b) => b.revenue_inr - a.revenue_inr)
    .map(r => `
      <tr>
        <td>${r.owner_name}</td>
        <td class="num">${fmtInt(r.deals_won)}</td>
        <td class="num">${fmtCr(r.revenue_inr)}
          <span class="rank-bar" style="width:${Math.max(4, (r.revenue_inr / maxRevenue) * 60)}px"></span>
        </td>
        <td class="num">${fmtInt(r.deals_open)}</td>
        <td class="num">${fmtCr(r.pipeline_inr)}</td>
      </tr>
    `).join('');
}

// ---------- wire up filters ----------

function populateSelect(el, values, { withAll = true, allLabel = 'All' } = {}) {
  const opts = withAll ? [`<option value="">${allLabel}</option>`] : [];
  opts.push(...values.map(v => `<option value="${v}">${v}</option>`));
  el.innerHTML = opts.join('');
}

async function main() {
  const meta = await initTopbar();
  if (!meta) return; // API down — nothing else will load either

  const [{ rows: fiscalRows }, { rows: pipelineRows }, { rows: openStageRows }, { rows: seasonalityRows }, { rows: historyRows }] =
    await Promise.all([
      getJSON('/api/bookings/fiscal'),
      getJSON('/api/pipelines'),
      getJSON('/api/pipeline/open'),
      getJSON('/api/seasonality'),
      getJSON('/api/pipeline/history'),
    ]);

  loadKPIs(pipelineRows, fiscalRows);
  renderFiscalChart(fiscalRows);
  renderPipelinesChart(pipelineRows);
  renderSeasonalityChart(seasonalityRows);
  renderHistoryChart(historyRows);

  // pipeline filter (monthly trend)
  const pipelineNames = pipelineRows.map(p => p.pipeline).sort();
  const pipelineFilter = document.getElementById('pipeline-filter');
  populateSelect(pipelineFilter, pipelineNames, { allLabel: 'All pipelines' });
  pipelineFilter.addEventListener('change', () => loadMonthlyChart(pipelineFilter.value));
  await loadMonthlyChart('');

  // pipeline filter (stage funnel) — defaults to the largest pipeline by deal count
  const stageFilter = document.getElementById('stage-pipeline-filter');
  populateSelect(stageFilter, pipelineNames, { withAll: false });
  const defaultPipeline = [...pipelineRows].sort((a, b) => b.deals - a.deals)[0]?.pipeline;
  stageFilter.value = defaultPipeline;
  stageFilter.addEventListener('change', () => loadStagesChart(stageFilter.value, openStageRows));
  await loadStagesChart(defaultPipeline, openStageRows);

  // fiscal year filter (reps table)
  const fyFilter = document.getElementById('fy-filter');
  populateSelect(fyFilter, fiscalRows.map(r => r.fiscal_year).reverse(), { allLabel: 'All years' });
  fyFilter.addEventListener('change', () => loadRepsTable(fyFilter.value));
  await loadRepsTable('');
}

main().catch(err => {
  console.error(err);
  document.getElementById('sync-status').textContent = 'Failed to load — see console';
});
