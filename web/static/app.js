// ── DCA Dashboard — Frontend ──────────────────────────────────────────────
const API = '';  // same origin

// ── Clock ─────────────────────────────────────────────────────────────────
function tickClock() {
  const el = document.getElementById('clock');
  if (el) el.textContent = new Date().toUTCString().replace('GMT', 'UTC');
}
setInterval(tickClock, 1000);
tickClock();

// ── Helpers ───────────────────────────────────────────────────────────────
const fmt = (n, d = 2) =>
  n == null ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });

const fmtPct = (n, d = 2) => n == null ? '—' : `${n > 0 ? '+' : ''}${fmt(n, d)}%`;

const fmtSign = (n, d = 2) => n == null ? '—' : `${n >= 0 ? '+' : ''}${fmt(n, d)}`;

async function api(path, method = 'GET', body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(API + path, opts);
  return r.json();
}

// ── Modal ─────────────────────────────────────────────────────────────────
let _modalSymbol = '';

function openRefModal(symbol) {
  _modalSymbol = symbol;
  document.getElementById('ref-modal-symbol').textContent = symbol;
  document.getElementById('ref-manual-input').value = '';
  document.getElementById('ref-modal').classList.remove('hidden');
  document.getElementById('modal-overlay').classList.remove('hidden');
}
function closeModal() {
  document.getElementById('ref-modal').classList.add('hidden');
  document.getElementById('modal-overlay').classList.add('hidden');
}

document.getElementById('ref-cancel').addEventListener('click', closeModal);
document.getElementById('modal-overlay').addEventListener('click', closeModal);

document.getElementById('ref-use-live').addEventListener('click', async () => {
  await api('/api/set_reference', 'POST', { symbol: _modalSymbol });
  closeModal();
  refresh();
});

document.getElementById('ref-manual-set').addEventListener('click', async () => {
  const val = parseFloat(document.getElementById('ref-manual-input').value);
  if (!val || val <= 0) return alert('Enter a valid price.');
  await api('/api/set_reference', 'POST', { symbol: _modalSymbol, price: val });
  closeModal();
  refresh();
});

// ── Card builder ──────────────────────────────────────────────────────────
function buildCard(c) {
  const statusCls = {
    WAITING: 'pill-waiting',
    ACTIVE:  'pill-active',
    EXITED:  'pill-exited',
  }[c.status] || 'pill-waiting';

  // Dump badge
  let dumpHtml = '<span class="dump-label">Ref not set</span>';
  if (c.dump_pct != null) {
    const cls = c.dump_pct >= 0 ? 'dump-pos' : 'dump-neg';
    dumpHtml = `
      <span class="dump-label">Dump from ref</span>
      <span class="dump-val ${cls}">${fmtPct(c.dump_pct)}</span>`;
  }

  // Metrics
  let metricsHtml = '';
  if (c.status === 'ACTIVE') {
    const pnlCls = (c.pnl_usdt || 0) >= 0 ? 'metric-green' : 'metric-red';
    metricsHtml = `
      <div class="metrics-row">
        <div class="metric">
          <div class="metric-label">Avg Entry</div>
          <div class="metric-val metric-blue">$${fmt(c.average_entry, 2)}</div>
        </div>
        <div class="metric">
          <div class="metric-label">TP Target</div>
          <div class="metric-val metric-green">$${fmt(c.tp_price, 2)}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Invested</div>
          <div class="metric-val">$${fmt(c.total_invested, 2)}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Unrealised PnL</div>
          <div class="metric-val ${pnlCls}">${fmtSign(c.pnl_usdt)} USDT</div>
        </div>
        <div class="metric">
          <div class="metric-label">PnL %</div>
          <div class="metric-val ${pnlCls}">${fmtPct(c.pnl_pct)}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Quantity</div>
          <div class="metric-val">${fmt(c.total_quantity, 6)}</div>
        </div>
      </div>`;
  }

  // Steps
  const nextIdx = c.steps_done;
  const stepsHtml = c.steps.map((s, i) => {
    let cls = 'pending';
    if (s.hit) cls = 'hit';
    else if (i === nextIdx && c.dump_pct != null) cls = 'next-up';
    const tooltip = s.hit
      ? `title="Step ${i+1}: filled @ $${fmt(s.entry_price)}"`
      : `title="Step ${i+1}: buy $${fmt(s.order_size, 0)} at ${s.dump_level}% dump"`;
    return `<div class="step-box ${cls}" ${tooltip}>
      <span>${s.dump_level}%</span>
      <span class="step-dump">$${(s.order_size/1000).toFixed(1)}k</span>
    </div>`;
  }).join('');

  // Controls
  const engineBtn = c.engine_running
    ? `<button class="btn btn-danger btn-sm" onclick="stopEngine('${c.symbol}')">⏹ Stop Engine</button>`
    : `<button class="btn btn-success btn-sm" onclick="startEngine('${c.symbol}')">▶ Start Engine</button>`;

  return `
  <div class="coin-card" id="card-${c.symbol}">
    <div class="card-top">
      <div>
        <div class="card-coin">${c.coin} <span style="font-size:13px;color:var(--muted);font-weight:400">${c.symbol}</span></div>
        <div class="dump-row" style="margin-top:6px">${dumpHtml}</div>
      </div>
      <div style="text-align:right">
        <div class="card-price">$${fmt(c.price, 2)}</div>
        <div style="margin-top:6px">
          <span class="status-pill ${statusCls}">${c.status}</span>
          ${c.engine_running ? '<span class="status-pill" style="background:rgba(34,197,94,.1);color:#22c55e;margin-left:4px">● LIVE</span>' : ''}
        </div>
      </div>
    </div>

    ${metricsHtml}

    <div>
      <div class="steps-label">
        <span>DCA Steps — ${c.steps_done}/${c.step_count} hit</span>
        <span>Max capital: $${fmt(c.total_capital, 0)}</span>
      </div>
      <div class="steps-row">${stepsHtml}</div>
    </div>

    <div class="card-controls">
      ${engineBtn}
      <button class="btn btn-secondary btn-sm" onclick="openRefModal('${c.symbol}')">📍 Set Reference</button>
      ${c.status === 'ACTIVE' ? `<button class="btn btn-warn btn-sm" onclick="resetPos('${c.symbol}')">↺ Reset</button>` : ''}
    </div>
  </div>`;
}

// ── Control actions ───────────────────────────────────────────────────────
async function startEngine(symbol) {
  const r = await api('/api/start_engine', 'POST', { symbol });
  if (r.error) alert(r.error);
  else refresh();
}
async function stopEngine(symbol) {
  await api('/api/stop_engine', 'POST', { symbol });
  refresh();
}
async function resetPos(symbol) {
  if (!confirm(`Reset position for ${symbol}? This clears all step history.`)) return;
  await api('/api/reset', 'POST', { symbol });
  refresh();
}

// ── Log fetcher ───────────────────────────────────────────────────────────
async function refreshLogs() {
  const data = await api('/api/logs?n=150');
  const box  = document.getElementById('log-box');
  const auto = document.getElementById('log-autoscroll').checked;

  box.innerHTML = (data.lines || []).map(line => {
    let cls = '';
    if (/BUY\s/.test(line))    cls = 'buy';
    else if (/SELL\s/.test(line))   cls = 'sell';
    else if (/PROFIT/.test(line))   cls = 'profit';
    else if (/RESET/.test(line))    cls = 'reset';
    else if (/WARNING|WARN/.test(line)) cls = 'warn';
    else if (/ERROR/.test(line))    cls = 'error';
    return `<div class="log-line ${cls}">${escHtml(line)}</div>`;
  }).join('');

  if (auto) box.scrollTop = box.scrollHeight;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Main refresh ──────────────────────────────────────────────────────────
async function refresh() {
  try {
    const data = await api('/api/status');
    const grid = document.getElementById('cards-grid');

    // Connection badge
    const badge = document.getElementById('conn-badge');
    if (data.connected) {
      badge.textContent = 'Binance Connected';
      badge.className   = 'badge badge-ok';
    } else {
      badge.textContent = 'Not Connected';
      badge.className   = 'badge badge-err';
    }

    // Balance
    const balEl = document.getElementById('usdt-balance');
    balEl.textContent = data.usdt_balance != null ? `$${fmt(data.usdt_balance, 2)}` : '—';

    // Cards
    grid.innerHTML = (data.coins || []).map(buildCard).join('');
  } catch (e) {
    console.error('Refresh error:', e);
  }

  refreshLogs();
}

// Poll every 5 seconds
refresh();
setInterval(refresh, 5000);
