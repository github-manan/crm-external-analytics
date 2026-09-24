/* Weekly Sales Review - reads the local read-only API. GET only; this page
   never writes anything, to Zoho or otherwise. */

const state = { owner: 'All', week: null, tab: 'stuck', attention: null };

async function getJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${res.status} on ${path}`);
  return res.json();
}

/* --- formatting ----------------------------------------------------------- */

// Indian money reads in crore and lakh; plain rupees below that.
function money(v) {
  if (v === null || v === undefined || v === 0) return '—';
  if (v >= 1e7) return `₹${(v / 1e7).toFixed(2)} Cr`;
  if (v >= 1e5) return `₹${(v / 1e5).toFixed(2)} L`;
  return `₹${Math.round(v).toLocaleString('en-IN')}`;
}

function shortDate(iso) {
  if (!iso) return '—';
  return new Date(iso + 'T00:00:00').toLocaleDateString('en-IN',
    { day: 'numeric', month: 'short' });
}

function weekLabel(startISO) {
  const start = new Date(startISO + 'T00:00:00');
  const end = new Date(start); end.setDate(end.getDate() + 6);
  const opts = { day: 'numeric', month: 'short' };
  return `${start.toLocaleDateString('en-IN', opts)} – ${end.toLocaleDateString('en-IN', opts)}`;
}

// Direction is not always "up is good" - delta() takes the caller's word for it.
function delta(pct, { goodWhenUp = true } = {}) {
  if (pct === null || pct === undefined) return '<span class="delta flat">no prior week</span>';
  if (pct === 0) return '<span class="delta flat">flat vs last week</span>';
  const up = pct > 0;
  const good = up === goodWhenUp;
  const arrow = up ? '▲' : '▼';
  return `<span class="delta ${good ? 'up' : 'down'}">${arrow} ${Math.abs(pct)}%</span>` +
         ` <span style="color:var(--ink-muted)">vs last week</span>`;
}

const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* --- sections ------------------------------------------------------------- */

function renderKPIs(k) {
  document.getElementById('kpi-leads').textContent = k.leads.value;
  document.getElementById('kpi-leads-sub').innerHTML = delta(k.leads.delta_pct);

  document.getElementById('kpi-revenue').textContent = money(k.revenue.value);
  document.getElementById('kpi-revenue-sub').innerHTML =
    `${k.revenue.deals} deal${k.revenue.deals === 1 ? '' : 's'} won · ${delta(k.revenue.delta_pct)}`;

  document.getElementById('kpi-pipeline').textContent = money(k.pipeline.value);
  document.getElementById('kpi-pipeline-sub').textContent =
    `${k.pipeline.deals} open · ${money(k.pipeline.weighted)} weighted`;

  document.getElementById('kpi-accounts').textContent = k.accounts.value;
  document.getElementById('kpi-accounts-sub').innerHTML = delta(k.accounts.delta_pct);
}

function renderTarget(t) {
  const monthName = new Date(t.month + '-01T00:00:00')
    .toLocaleDateString('en-IN', { month: 'long', year: 'numeric' });
  document.getElementById('target-month').textContent =
    `${monthName} · ${t.owner === 'All' ? 'company' : t.owner}`;
  document.getElementById('target-actual').textContent = money(t.actual_inr);
  document.getElementById('target-of').textContent = `of ${money(t.target_inr)} target`;

  const pct = t.achieved_pct ?? 0;
  const fill = document.getElementById('target-fill');
  fill.style.width = `${Math.min(pct, 100)}%`;
  fill.classList.toggle('met', pct >= 100);

  document.getElementById('target-pct').textContent = `${pct}% achieved`;
  document.getElementById('target-gap').textContent =
    t.gap_inr > 0 ? `${money(t.gap_inr)} to go` : 'target met';
}

function renderMoveSummary(m) {
  const cells = [
    ['New in pipeline', m.entered_pipeline, ''],
    ['Progressed', m.progressed, ''],
    ['Won', m.closed_won, money(m.won_value)],
    ['Lost', m.closed_lost, money(m.lost_value)],
  ];
  document.getElementById('move-summary').innerHTML = cells.map(([label, n, sub]) =>
    `<div><strong>${n}</strong>${esc(label)}${sub ? ` · ${sub}` : ''}</div>`).join('');
}

function renderFunnel(j) {
  const shades = ['--f1', '--f2', '--f3', '--f4', '--f5'];
  document.getElementById('funnel').innerHTML = j.steps.map((s, i) => `
    <div class="fstep">
      <div class="fbar" style="background:var(${shades[i]})"></div>
      <div class="fcount">${s.count.toLocaleString('en-IN')}</div>
      <div class="fname">${esc(s.stage)}</div>
      <div class="frate">${s.rate_from_prev !== null ? s.rate_from_prev + '% from previous' : '&nbsp;'}</div>
    </div>`).join('');
  document.getElementById('journey-hint').textContent =
    `Lead → Contacted → Converted → Action → Won. Overall lead-to-won ${j.overall_pct}%. ` +
    `A lead that converted counts as contacted, since status often isn't updated first.`;
}

function renderSources(rows, scopeLabel) {
  const el = document.getElementById('sources');
  document.getElementById('sources-hint').textContent = scopeLabel;
  if (!rows.length) { el.innerHTML = '<div class="empty">No leads created this week.</div>'; return; }
  const max = Math.max(...rows.map((r) => r.n));
  el.innerHTML = rows.slice(0, 10).map((r) => `
    <div class="bar-row" title="${esc(r.source)}: ${r.n}">
      <div class="bname">${esc(r.source)}</div>
      <div class="btrack"><div class="bfill" style="width:${(r.n / max * 100).toFixed(1)}%"></div></div>
      <div class="bval">${r.n}</div>
    </div>`).join('');
}

function renderMoveList(rows) {
  const el = document.getElementById('move-list');
  if (!rows.length) { el.innerHTML = '<div class="empty">No stage changes this week.</div>'; return; }
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
        <span class="pill ${cls}"><span class="dot"></span>${label}</span>
        <div class="move-amt">${money(r.amount_inr)} · ${esc(r.owner_name ?? '—')}</div>
      </div>
    </div>`;
  }).join('');
}

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
        <td class="num">${money(r.amount_inr)}</td>
        <td class="num age ${r.days_stuck >= 90 ? 'bad' : r.days_stuck >= 45 ? 'warn' : ''}">${r.days_stuck ?? '—'}</td>
      </tr>`).join('')}</tbody></table>`;

  } else if (state.tab === 'slipped') {
    const rows = a.slipped;
    if (!rows.length) return void (el.innerHTML = '<div class="empty">No close dates pushed out this week.</div>');
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
        <td class="num">${money(r.amount_inr)}</td>
        <td class="num">${r.probability ?? '—'}%</td>
        <td class="num">${shortDate(r.closing_date)} <span style="color:var(--ink-muted)">(${r.days_to_close}d)</span></td>
      </tr>`).join('')}</tbody></table>`;
  }
}

/* --- load ----------------------------------------------------------------- */

async function load() {
  const q = `?week=${state.week}&owner=${encodeURIComponent(state.owner)}`;
  const [summary, journey, sources, movement, attention] = await Promise.all([
    getJSON('/api/weekly/summary' + q),
    getJSON('/api/weekly/journey' + q),
    getJSON('/api/weekly/sources' + q),
    getJSON('/api/weekly/movement' + q),
    getJSON('/api/weekly/attention' + q),
  ]);

  renderKPIs(summary.kpis);
  renderTarget(summary.target);
  renderMoveSummary(summary.movement);
  renderFunnel(journey);
  renderSources(sources.rows, 'Leads created during the selected week.');
  renderMoveList(movement.rows);
  state.attention = attention;
  renderAttention();

  document.getElementById('subtitle').textContent =
    `${weekLabel(state.week)} · ${state.owner === 'All' ? 'all reps' : state.owner}`;
}

async function main() {
  const opts = await getJSON('/api/weekly/options');
  state.week = opts.current_week;

  const ownerSel = document.getElementById('owner');
  ownerSel.innerHTML = '<option value="All">All reps</option>' +
    opts.owners.map((o) => `<option value="${esc(o)}">${esc(o)}</option>`).join('');

  const weekSel = document.getElementById('week');
  weekSel.innerHTML = opts.weeks.map((w, i) =>
    `<option value="${w}">${weekLabel(w)}${i === 0 ? ' (this week)' : ''}</option>`).join('');

  ownerSel.addEventListener('change', (e) => { state.owner = e.target.value; load(); });
  weekSel.addEventListener('change', (e) => { state.week = e.target.value; load(); });

  document.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((b) =>
        b.setAttribute('aria-selected', String(b === btn)));
      state.tab = btn.dataset.tab;
      renderAttention();
    });
  });

  const meta = await getJSON('/api/meta');
  const last = meta.last_sync?.map((s) => s.last_run_at).sort().pop();
  document.getElementById('freshness').textContent =
    last ? `Data last refreshed ${new Date(last).toLocaleString('en-IN')}` : '';

  await load();
}

main().catch((e) => {
  document.getElementById('subtitle').textContent =
    'API unreachable — start it with: python3 scripts/serve.py';
  console.error(e);
});
