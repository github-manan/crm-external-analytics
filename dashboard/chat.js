// CRM Assistant - talks only to our own localhost:8420 /api/chat, which in
// turn talks only to a local Ollama model. No CRM data ever leaves this
// machine. Read-only: this page cannot write anything to Zoho.

// Redirects to login.html if not logged in; otherwise renders the
// "logged in as / Logout" pill. Scoping itself happens server-side in
// serve.py/chatbot.py, not here - this is just the page gate + UI.
requireSession();

const state = { history: [], uploadId: null, uploadFilename: null };
const MAX_UPLOAD_BYTES = 5 * 1024 * 1024; // keep in sync with documents.py's MAX_FILE_BYTES

const SUGGESTIONS = [
  'How many leads do we have and what%27s our conversion rate?',
  'Which rep has the highest revenue this year?',
  'How is the open pipeline looking by pipeline?',
  'What deals need attention right now?',
  'How are we tracking against this month%27s revenue target?',
];

const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// The model can misstate a number even when the tool data behind it is
// correct (verified: it has mis-transcribed a correctly pre-formatted
// figure). So every reply that used a tool shows the real returned data
// too, not just the model's prose - that's the actual source of truth.
function renderValue(v) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'object') return esc(JSON.stringify(v));
  return esc(String(v));
}

function renderFact(obj) {
  const entries = Object.entries(obj).filter(([k]) => !k.endsWith('_inr'));
  return `<table>${entries.map(([k, v]) =>
    `<tr><td class="fact-key">${esc(k.replace(/_/g, ' '))}</td><td>${renderValue(v)}</td></tr>`).join('')}</table>`;
}

function renderTable(rows) {
  if (!rows.length) return '<div class="empty">(no rows)</div>';
  const cols = Object.keys(rows[0]).filter(k => !k.endsWith('_inr'));
  return `<table><thead><tr>${cols.map(c => `<th>${esc(c.replace(/_/g, ' '))}</th>`).join('')}</tr></thead>
    <tbody>${rows.slice(0, 8).map(r => `<tr>${cols.map(c => `<td>${renderValue(r[c])}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

function renderToolData(toolCalls) {
  if (!toolCalls || !toolCalls.length) return '';
  return toolCalls.map(({ tool, data }) => {
    if (!data || typeof data !== 'object') return '';
    const sections = Object.entries(data).map(([key, value]) => {
      if (Array.isArray(value)) return `<div class="fact-label">${esc(key.replace(/_/g, ' '))}</div>${renderTable(value)}`;
      if (value && typeof value === 'object') return `<div class="fact-label">${esc(key.replace(/_/g, ' '))}</div>${renderFact(value)}`;
      if (key.endsWith('_inr')) return '';
      return `<div class="fact-row"><span class="fact-label">${esc(key.replace(/_/g, ' '))}:</span> ${renderValue(value)}</div>`;
    }).join('');
    return `<details class="verified-data"><summary>Verified data from <code>${esc(tool)}</code></summary>${sections}</details>`;
  }).join('');
}

// ---------- insights chart (get_insights only) ----------
//
// Chart.js, same library the other dashboard pages already use. Findings
// are pre-scored by insights.py (rate_pct, diff_points, favorable - never
// inferred here from a raw sign, since "above average" is good for a
// win/conversion rate but bad for a stuck-deal rate - see insights.py).
//
// Color alone does not carry favorable/unfavorable: the project's own
// --good/--warn tokens were checked with the dataviz skill's palette
// validator and found too close together for red-green color blindness
// (CVD Delta E below the safe floor, both light and dark mode) - a
// pre-existing gap in the design system, not something fixed here. The
// mandatory mitigation is secondary encoding: a ✓/! glyph is baked into
// every label, not just the color, so the status is never color-only.

let insightsChartCounter = 0;
const INSIGHTS_CHART_LIMIT = 8;

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function buildInsightsChartHtml(findings) {
  if (!findings || !findings.length) return '';
  const id = `insights-chart-${++insightsChartCounter}`;
  const shown = findings.slice(0, INSIGHTS_CHART_LIMIT);
  const more = findings.length > shown.length
    ? `<div class="chart-more-note">+${findings.length - shown.length} more in Verified data below</div>`
    : '';
  return `<div class="insights-chart-wrap"><canvas id="${esc(id)}" height="${Math.max(140, shown.length * 34)}"></canvas></div>${more}<div data-insights-canvas="${esc(id)}" hidden></div>`;
}

function renderInsightsChart(findings, canvasId) {
  const shown = findings.slice(0, INSIGHTS_CHART_LIMIT);
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;

  const good = cssVar('--good') || '#15803d';
  const warn = cssVar('--warn') || '#92400e';
  const textMuted = cssVar('--text-muted') || '#64748b';
  const gridColor = resolvedTheme() === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)';

  new Chart(ctx, {
    type: 'bar',
    data: {
      // Bare name only, e.g. "Renewal" not "Renewal (pipeline)" - the
      // full label with its dimension/category suffix is too long for a
      // chat-width chart and was clipping (measured, not assumed - the
      // first render showed "cturing & Heavy" missing its "Manufa"
      // prefix). The dimension/category still appears in the tooltip,
      // nothing is lost, just moved to where there's room for it.
      // The ✓/! glyph is the secondary encoding - it carries the
      // favorable/unfavorable status even if the bar color doesn't
      // read clearly (see module note above).
      labels: shown.map(f => `${f.favorable ? '✓' : '!'} ${f.label.replace(/\s*\([^)]*\)$/, '')}`),
      datasets: [{
        data: shown.map(f => f.diff_points),
        backgroundColor: shown.map(f => f.favorable ? good : warn),
        borderRadius: 4,
        borderSkipped: false,
        barThickness: 18,
      }],
    },
    options: {
      indexAxis: 'y',
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            // Full label (with its dimension/category suffix) in the
            // tooltip title - the chart's own y-axis label is shortened
            // to the bare name for space, this is where the rest lives.
            title: (items) => shown[items[0].dataIndex].label,
            label: (c) => {
              const f = shown[c.dataIndex];
              return [`${f.rate_pct} vs. ${f.company_rate_pct} company average`,
                      f.favorable ? '✓ favorable' : '! worth a look'];
            },
          },
        },
      },
      scales: {
        x: {
          grid: { color: gridColor },
          ticks: { color: textMuted, callback: (v) => `${v > 0 ? '+' : ''}${v}pts` },
          title: { display: true, text: 'Percentage points vs. company average', color: textMuted },
        },
        y: { grid: { display: false }, ticks: { color: textMuted } },
      },
    },
  });
}

function addMessage(role, text, { thinking = false, toolCalls = null, error = false, headline = null } = {}) {
  const log = document.getElementById('chat-log');
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  const bubbleClasses = ['bubble', thinking ? 'thinking' : '', error ? 'error' : ''].filter(Boolean).join(' ');
  const trace = toolCalls && toolCalls.length
    ? `<div class="tool-trace">checked: ${toolCalls.map(t => esc(t.tool)).join(', ')}</div>${renderToolData(toolCalls)}`
    : '';
  // The headline (if any) is code-computed from verified data - guaranteed
  // correct. The model's own sentence goes below it as commentary/context;
  // it has mis-stated a specific figure even when everything else about
  // the answer was right, so it is never the sole source for a number.
  const headlineHtml = headline
    ? `<div class="headline">${esc(headline)}</div><div class="headline-note">the figure above is computed directly, not generated by the model</div>`
    : '';

  // Insights get a chart, every other tool keeps the existing text+table
  // treatment - this is deliberately scoped to just get_insights, not a
  // general visualization system for every tool.
  const insightsCall = toolCalls && toolCalls.find(t => t.tool === 'get_insights' && t.data?.findings?.length);
  const chartHtml = insightsCall ? buildInsightsChartHtml(insightsCall.data.findings) : '';

  wrap.innerHTML = `${headlineHtml}<div class="${bubbleClasses}">${esc(text)}</div>${chartHtml}${trace}`;
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;

  if (insightsCall) {
    const marker = wrap.querySelector('[data-insights-canvas]');
    if (marker) renderInsightsChart(insightsCall.data.findings, marker.dataset.insightsCanvas);
  }

  return wrap;
}

async function send(message) {
  if (!message.trim()) return;
  addMessage('user', message);
  const sendBtn = document.getElementById('chat-send');
  sendBtn.disabled = true;
  const thinkingEl = addMessage('bot', 'Thinking', { thinking: true });

  try {
    const body = { message, history: state.history };
    if (state.uploadId) body.upload_id = state.uploadId;
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    thinkingEl.remove();

    if (!res.ok || data.error) {
      addMessage('bot', data.error || `Request failed (${res.status})`, { error: true });
      return;
    }

    addMessage('bot', data.reply, { toolCalls: data.tool_calls || [], headline: data.headline });
    state.history.push({ role: 'user', content: message });
    state.history.push({ role: 'assistant', content: data.reply });
    // Keep history bounded so the local model isn't fed an ever-growing prompt.
    if (state.history.length > 16) state.history = state.history.slice(-16);
  } catch (e) {
    thinkingEl.remove();
    addMessage('bot', `Couldn't reach the API: ${e.message}`, { error: true });
  } finally {
    sendBtn.disabled = false;
  }
}

document.getElementById('chat-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const textEl = document.getElementById('chat-text');
  const text = textEl.value;
  textEl.value = '';
  textEl.style.height = 'auto';
  send(text);
});

document.getElementById('chat-text').addEventListener('input', (e) => {
  e.target.style.height = 'auto';
  e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px';
});
document.getElementById('chat-text').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    document.getElementById('chat-form').requestSubmit();
  }
});

document.getElementById('suggestions').innerHTML = SUGGESTIONS
  .map(s => `<button type="button">${esc(decodeURIComponent(s.replace(/%27/g, "'")))}</button>`).join('');
document.querySelectorAll('.suggestions button').forEach(btn => {
  btn.addEventListener('click', () => send(btn.textContent));
});

async function checkStatus() {
  const el = document.getElementById('sync-status');
  try {
    const res = await fetch('/api/meta');
    if (!res.ok) throw new Error();
    el.textContent = 'API connected';
    el.classList.replace('pill-muted', 'pill-good');
  } catch {
    el.textContent = 'API unreachable — run: python scripts/serve.py';
  }
}

// ---------- document upload ----------

function showAttachmentError(msg) {
  const el = document.getElementById('attachment-error');
  el.textContent = msg;
  el.hidden = false;
}
function clearAttachmentError() {
  document.getElementById('attachment-error').hidden = true;
}

function setAttachment(uploadId, filename) {
  state.uploadId = uploadId;
  state.uploadFilename = filename;
  document.getElementById('attachment-chip').textContent = `📎 ${filename}`;
  document.getElementById('attachment-row').hidden = false;
  document.getElementById('attachment-button').classList.add('has-file');
}
function clearAttachment() {
  state.uploadId = null;
  state.uploadFilename = null;
  document.getElementById('attachment-row').hidden = true;
  document.getElementById('attachment-button').classList.remove('has-file');
  document.getElementById('attachment-input').value = '';
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(',')[1]); // strip the data: URL prefix
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

document.getElementById('attachment-button').addEventListener('click', () => {
  document.getElementById('attachment-input').click();
});

document.getElementById('attachment-input').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  clearAttachmentError();

  if (file.size > MAX_UPLOAD_BYTES) {
    showAttachmentError(`"${file.name}" is too large - max ${MAX_UPLOAD_BYTES / (1024 * 1024)}MB.`);
    document.getElementById('attachment-input').value = '';
    return;
  }

  const button = document.getElementById('attachment-button');
  button.disabled = true;
  try {
    const content_base64 = await fileToBase64(file);
    const res = await fetch('/api/chat/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: file.name, content_base64 }),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      showAttachmentError(data.error || `Upload failed (${res.status})`);
      document.getElementById('attachment-input').value = '';
      return;
    }
    setAttachment(data.upload_id, data.filename);
    if (data.truncated) {
      showAttachmentError(`"${data.filename}" is long - only the first part was attached.`);
    }
  } catch (err) {
    showAttachmentError(`Couldn't upload "${file.name}": ${err.message}`);
  } finally {
    button.disabled = false;
  }
});

document.getElementById('attachment-remove').addEventListener('click', clearAttachment);

// Convenience for testing/linking: ?q=<question> auto-sends on load.
const preset = new URLSearchParams(location.search).get('q');
if (preset) send(preset);
checkStatus();
