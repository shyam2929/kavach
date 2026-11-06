/* KAVACH dashboard: static page that reads data/*.json written by the engine
   and sends commands as GitHub issues ("kavach: <cmd> [arg]"). */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const inr = (x, d = 0) => (x == null || isNaN(x)) ? "–" : "₹" + Number(x).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d });
  const pct = (x, d = 1) => (x == null || isNaN(x)) ? "–" : (x * 100).toFixed(d) + "%";
  const num = (x, d = 1) => (x == null || isNaN(x)) ? "–" : Number(x).toFixed(d);
  const cls = (x) => x > 0 ? "pos" : x < 0 ? "neg" : "";

  // ---- repo detection: https://<owner>.github.io/<repo>/ ----
  const host = location.hostname;
  const OWNER = host.endsWith(".github.io") ? host.split(".")[0] : (window.KAVACH_OWNER || "d0paliwal");
  const REPO = host.endsWith(".github.io") ? (location.pathname.split("/")[1] || `${OWNER}.github.io`) : (window.KAVACH_REPO || "kavach");
  $("#repoLink").href = `https://github.com/${OWNER}/${REPO}/issues?q=is%3Aissue+kavach`;

  let S = null;
  const tokenKey = `kavach-token-${OWNER}-${REPO}`;
  const getToken = () => { try { return localStorage.getItem(tokenKey) || ""; } catch { return ""; } };

  async function j(path) {
    const r = await fetch(`${path}?t=${Date.now()}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`${path}: ${r.status}`);
    return r.json();
  }

  function ago(iso) {
    if (!iso) return "never";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 90) return `${Math.round(s)}s ago`;
    if (s < 5400) return `${Math.round(s / 60)} min ago`;
    if (s < 172800) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
  }

  // ---------------- charts (inline SVG, no deps) ----------------
  function lineChart(points, { w = 560, h = 200, zero = null, fmt = (v) => v, xfmt = (v) => v, markers = [] } = {}) {
    if (!points.length) return `<div class="empty">No data yet.</div>`;
    const pad = { l: 58, r: 10, t: 10, b: 22 };
    const xs = points.map((p) => p[0]), ys = points.map((p) => p[1]);
    let y0 = Math.min(...ys, ...(zero != null ? [zero] : [])), y1 = Math.max(...ys, ...(zero != null ? [zero] : []));
    markers.forEach((m) => { y0 = Math.min(y0, m.y ?? y0); y1 = Math.max(y1, m.y ?? y1); });
    const minSpan = Math.max(Math.abs(y1), Math.abs(y0)) * 0.01 || 1;
    if (y1 - y0 < minSpan) { const m = (y0 + y1) / 2; y0 = m - minSpan; y1 = m + minSpan; }
    const span = y1 - y0; y0 -= span * 0.06; y1 += span * 0.06;
    const x0 = xs[0], x1 = xs[xs.length - 1] === x0 ? x0 + 1 : xs[xs.length - 1];
    const X = (x) => pad.l + (x - x0) / (x1 - x0) * (w - pad.l - pad.r);
    const Y = (y) => pad.t + (1 - (y - y0) / (y1 - y0)) * (h - pad.t - pad.b);
    const path = points.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
    let g = "";
    for (let k = 0; k <= 4; k++) {
      const v = y0 + (y1 - y0) * k / 4;
      g += `<line x1="${pad.l}" x2="${w - pad.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)" stroke-width="1"/>`;
      g += `<text x="${pad.l - 6}" y="${Y(v) + 3}" text-anchor="end">${esc(fmt(v))}</text>`;
    }
    g += `<text x="${pad.l}" y="${h - 6}">${esc(xfmt(x0))}</text><text x="${w - pad.r}" y="${h - 6}" text-anchor="end">${esc(xfmt(x1))}</text>`;
    if (zero != null) g += `<line x1="${pad.l}" x2="${w - pad.r}" y1="${Y(zero)}" y2="${Y(zero)}" stroke="var(--muted)" stroke-dasharray="3 3"/>`;
    markers.forEach((m) => {
      if (m.x != null) g += `<line x1="${X(m.x)}" x2="${X(m.x)}" y1="${pad.t}" y2="${h - pad.b}" stroke="${m.color || "var(--amber)"}" stroke-dasharray="2 3"/><text x="${X(m.x) + 4}" y="${pad.t + 10}">${esc(m.label || "")}</text>`;
    });
    return `<svg viewBox="0 0 ${w} ${h}" role="img">${g}<path d="${path}" fill="none" stroke="var(--blue)" stroke-width="2"/></svg>`;
  }

  const payoffChart = (pts, spot) => lineChart(pts, { h: 190, zero: 0, fmt: (v) => (v / 1000).toFixed(0) + "k", xfmt: (v) => Math.round(v), markers: spot ? [{ x: spot, label: "spot" }] : [] });

  // ---------------- render ----------------
  function render(s) {
    S = s;
    const pills = [];
    pills.push(s.mode === "LIVE" ? `<span class="pill live">LIVE</span>` : `<span class="pill">PAPER</span>`);
    pills.push(`<span class="pill ${s.quotes === "upstox" || s.quotes === "upstox-live" ? "ok" : "warn"}">quotes: ${esc(s.quotes)}</span>`);
    pills.push(`<span class="pill ${s.market_open ? "ok" : ""}">NSE ${s.market_open ? "open" : "closed"}</span>`);
    pills.push(`<span class="pill">${esc(s.strategy)}</span>`);
    pills.push(`<span class="pill">approval: ${esc(s.approval)}</span>`);
    if (s.training) pills.push(`<span class="pill ok">training book on</span>`);
    if (s.crude) pills.push(`<span class="pill ${s.crude.mcx_open ? "ok" : ""}">MCX ${s.crude.mcx_open ? "open" : "closed"}</span>`);
    pills.push(`<span class="pill" title="${esc(s.updated)}">updated ${ago(s.updated)}</span>`);
    $("#pills").innerHTML = pills.join("");

    const b = [];
    if (s.halted) b.push(`<div class="banner bad"><b>HALTED</b><span>${esc(s.halt_reason)}. No new entries until you reset the halt (Controls → human-only).</span></div>`);
    if (s.paused) b.push(`<div class="banner warn"><b>Paused</b><span>New entries are paused. Open positions are still managed.</span></div>`);
    if (s.training && s.training.pending) b.push(`<div class="banner info"><b>Trade to accept</b><span>Training trade ${esc(s.training.pending.id)}: ${esc(s.training.pending.strategy)} ×1, expiry ${esc(s.training.pending.expiry)}, worst case ${inr(s.training.pending.max_loss)}. See the Training book below.</span></div>`);
    if (s.crude && s.crude.pending) for (const [bk, p] of Object.entries(s.crude.pending)) b.push(`<div class="banner info"><b>Crude trade to accept</b><span>${bk === "main" ? "Main" : "Training"} ${esc(p.id)}: condor ×1 Mini lot, options expiry ${esc(p.expiry)}, worst case ${inr(p.max_loss)}. See the Crude section.</span></div>`);
    if (s.pending) b.push(`<div class="banner info"><b>Proposal waiting</b><span>${esc(s.pending.strategy)} ×${s.pending.lots}, worst case ${inr(s.pending.max_loss)}. Review below.</span></div>`);
    if (s.quotes === "synthetic") b.push(`<div class="banner warn"><b>Synthetic quotes</b><span>Option prices are modelled from Nifty and VIX. Add the <code>UPSTOX_ANALYTICS_TOKEN</code> repository secret to paper-trade on real Upstox quotes (see README).</span></div>`);
    $("#banners").innerHTML = b.join("");

    const eqChg = s.equity - s.capital_base;
    $("#kpis").innerHTML = [
      ["Equity (marked)", inr(s.equity), `<span class="${cls(eqChg)}">${eqChg >= 0 ? "+" : ""}${inr(eqChg)}</span> vs start`],
      ["Drawdown", pct(s.drawdown), `halt at ${pct(s.limits.halt_at, 0)}`],
      ["Capital unlocked", pct(s.cap_frac, 0), "of risk budget (earned by evidence)"],
      ["Paper trades", `${s.paper_trades}/${s.paper_needed}`, "before live is allowed"],
      ["Loss streak", `${s.consecutive_losses}/${s.limits.max_consecutive_losses}`, s.cooldown_until ? `cooldown till ${esc(s.cooldown_until)}` : "cooldown after limit"],
    ].map(([l, v, sub]) => `<div class="kpi"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${sub}</div></div>`).join("");

    // signal
    const g = s.signal;
    if (!g) $("#signal").innerHTML = `<div class="empty">No signal yet.</div>`;
    else {
      const ivNow = g.atm_iv != null ? g.atm_iv * 100 : null;
      const edge = (ivNow ?? g.vix) - g.rv_fcst;
      const gates = [
        ["VIX 5-day change", g.vix_ch5, (v) => pct(v), (v) => v <= 0.12, "≤ +12%"],
        ["Nifty 5-day return", g.ret5, (v) => pct(v), (v) => v >= -0.024, "≥ −2.4%"],
        ["Below 20-day high", g.dd20, (v) => pct(v), (v) => v >= -0.05, "≥ −5%"],
        ["India VIX level", g.vix, (v) => num(v), (v) => v <= 28, "≤ 28"],
      ];
      $("#signal").innerHTML = `
        <div class="sig">
          <div><div class="l">India VIX</div><div class="v">${num(g.vix)}</div></div>
          <div><div class="l">Live ATM IV</div><div class="v">${ivNow != null ? num(ivNow) : "–"}</div></div>
          <div><div class="l">Forecast RV (5d)</div><div class="v">${num(g.rv_fcst)}</div></div>
        </div>
        <div class="gate">${gates.map(([n, v, f, ok, rule]) => `<div class="g"><span><span class="dot ${ok(v) ? "ok" : "bad"}"></span>${n} <span class="muted small">(${rule})</span></span><span>${f(v)}</span></div>`).join("")}</div>
        <div class="verdict">${g.stress ? "<b class='neg'>Stress regime.</b> Short-volatility entries are blocked." :
          edge > 0 ? `<b class="pos">Calm regime, premium present.</b> Implied vol exceeds the forecast by ${num(edge)} pts.` :
          `<b>Calm regime, no premium.</b> Implied vol is ${num(-edge)} pts below the forecast, so the system stands aside.`}
          <div class="muted small">Daily data as of ${esc(g.asof)}${g.spot ? ` · spot ${num(g.spot, 1)}` : g.spot_close ? ` · close ${num(g.spot_close, 1)}` : ""}</div></div>`;
    }

    $("#pauseBtn").hidden = s.paused; $("#resumeBtn").hidden = !s.paused;

    // proposal
    const p = s.pending;
    if (p) {
      const ageMin = (Date.now() - new Date(p.created).getTime()) / 60000;
      const left = Math.max(0, 45 - ageMin);
      $("#proposalWrap").innerHTML = `<article class="card"><h2>Proposal ${esc(p.id)} <span class="muted small">made ${ago(p.created)} · expires in ${Math.round(left)} min</span></h2>
        <div class="split"><div>
          ${legTable(p.legs.map((l) => ({ ...l, entry: l.limit })), false)}
          <div class="facts">
            <div><div class="l">Net credit</div><div class="v">${inr(p.credit)}</div></div>
            <div><div class="l">Worst case</div><div class="v neg">${inr(p.max_loss)}</div></div>
            <div><div class="l">Worst case / equity</div><div class="v ${p.max_loss / s.equity > s.limits.risk_per_trade * p.cap_frac ? "neg" : ""}">${pct(p.max_loss / s.equity)}</div></div>
            <div><div class="l">ATM IV · spot</div><div class="v">${num(p.atm_iv * 100)} · ${num(p.spot, 0)}</div></div>
          </div>
          ${p.max_loss / s.equity > s.limits.risk_per_trade * p.cap_frac ? `<p class="small neg">One lot is indivisible: this worst case is above the ${pct(s.limits.risk_per_trade * p.cap_frac)} target and is allowed only because it fits under the ${pct(s.limits.max_loss_pct, 0)} hard cap. On ₹1L, that's the cost of trading at all.</p>` : ""}
          <div class="row">
            <button class="btn good" data-cmd="approve" data-arg="${esc(p.id)}" data-confirm="Approve ${esc(p.strategy)} ×${p.lots}? The runner re-prices and re-checks the governor before sending.">Approve</button>
            <button class="btn" data-cmd="reject" data-arg="${esc(p.id)}">Reject</button>
          </div>
          <p class="muted small">Approval re-prices at current quotes. If spot has moved more than 0.6%, the runner re-proposes instead of entering.</p>
        </div><div class="chart">${payoffChart(p.payoff, p.spot)}<div class="muted small">P&amp;L at expiry vs Nifty</div></div></div></article>`;
    } else $("#proposalWrap").innerHTML = "";

    // position
    const pos = s.position;
    if (pos) {
      $("#positionWrap").innerHTML = `<article class="card"><h2>Open position · ${esc(pos.strategy)} ×${pos.lots} <span class="muted small">opened ${esc(pos.opened)} · expiry ${esc(pos.expiry)}</span></h2>
        <div class="split"><div>
          ${legTable(pos.legs, true)}
          <div class="facts">
            <div><div class="l">MTM (at bid/ask)</div><div class="v ${cls(pos.mtm)}">${inr(pos.mtm)}</div></div>
            <div><div class="l">Take profit at</div><div class="v">${inr(pos.tp_rs)}</div></div>
            <div><div class="l">Stop at</div><div class="v">${inr(-pos.stop_rs)}</div></div>
            <div><div class="l">Worst case</div><div class="v neg">${inr(-pos.max_loss)}</div></div>
          </div>
          <button class="btn danger" data-cmd="flatten" data-confirm="Close the whole position now at market quotes?">Flatten now</button>
        </div><div class="chart">${payoffChart(pos.payoff, s.signal && s.signal.spot)}<div class="muted small">P&amp;L at expiry vs Nifty</div></div></div></article>`;
    } else $("#positionWrap").innerHTML = `<article class="card"><h2>Main account position</h2><div class="empty">Flat. ${esc(s.note || "")}</div></article>`;
    $("#trainingWrap").innerHTML = trainingSection(s);
    $("#crudeWrap").innerHTML = crudeSection(s);

    // calibration
    const c = s.iv_calibration || { n: 0 };
    $("#calib").innerHTML = c.n ? `<div class="kv">
        <span>Samples</span><span>${c.n}</span>
        <span>Median ratio</span><span><b>${num(c.median_ratio, 3)}</b></span>
        <span>Middle 50%</span><span>${num(c.p25, 3)} – ${num(c.p75, 3)}</span>
        ${Object.entries(c.by_days_left || {}).map(([k, v]) => `<span>${k} trading days to expiry</span><span>${num(v, 3)}</span>`).join("")}
      </div><p class="muted small">This is the backtest's biggest unknown, measured live. Once there are a few hundred samples, set it as <code>iv_mult</code> and rerun research.</p>`
      : `<div class="empty">No samples yet. They're collected each market-hours step when Upstox quotes are connected.</div>`;

    // limits
    const L = s.limits;
    $("#limits").innerHTML = `<div class="kv">
      <span>Target worst case per trade</span><span>${pct(L.risk_per_trade, 0)} × capital unlocked</span>
      <span>Hard cap, worst case per trade</span><span>${pct(L.max_loss_pct, 0)}</span>
      <span>Max lots</span><span>${L.max_lots}</span>
      <span>Daily loss → flatten</span><span>${pct(L.daily_max_loss_pct, 0)}</span>
      <span>Drawdown halt</span><span>${pct(L.halt_at, 0)}</span>
      <span>Entry window (IST)</span><span>${esc(L.entry_window.join("–"))}</span>
      <span>Defined risk only</span><span>yes, wings bought first</span></div>`;
    $("#updatedFoot").textContent = ` · Last engine step ${ago(s.last_step)}.`;
    wireButtons();
  }

  function trainingSection(s) {
    const t = s.training;
    if (!t) return "";
    const p = t.pending;
    let h = `<article class="card"><h2>Training book <span class="muted small">one 1-lot trade proposed every trading day; gates are recorded, not enforced</span></h2>
      <div class="facts">
        <div><div class="l">Open trades</div><div class="v">${t.positions.length}</div></div>
        <div><div class="l">Open MTM</div><div class="v ${cls(t.open_mtm)}">${inr(t.open_mtm)}</div></div>
        <div><div class="l">Closed · win rate</div><div class="v">${t.closed} · ${t.win_rate == null ? "–" : pct(t.win_rate, 0)}</div></div>
        <div><div class="l">Realized P&amp;L</div><div class="v ${cls(t.realized)}">${inr(t.realized)}</div></div>
      </div>
      ${Object.keys(t.by_gate || {}).length ? `<p class="small">Gate study so far: ${Object.entries(t.by_gate).map(([k, v]) => `<b>${esc(k)}</b> ${v.n} trades, avg ${inr(v.avg)}`).join(" · ")}</p>` : ""}`;
    if (p) {
      h += `<div class="card" style="margin-top:12px"><h2>Training proposal ${esc(p.id)} <span class="muted small">made ${ago(p.created)} · valid until ${esc(p.valid_until.slice(11))} IST · expiry ${esc(p.expiry)} (${p.td_left} trading days)</span></h2>
        <div class="split"><div>
          ${legTable(p.legs.map((l) => ({ ...l, entry: l.limit })), false)}
          <div class="facts">
            <div><div class="l">Net credit</div><div class="v">${inr(p.credit)}</div></div>
            <div><div class="l">Worst case</div><div class="v neg">${inr(p.max_loss)}</div></div>
            <div><div class="l">ATM IV · spot</div><div class="v">${num(p.atm_iv * 100)} · ${num(p.spot, 0)}</div></div>
            <div><div class="l">Gates say</div><div class="v ${p.gate_open ? "pos" : "neg"}">${p.gate_open ? "trade" : "skip"}</div></div>
          </div>
          <p class="small">${p.gate_open ? "The main system would also take this trade." : `The main system would <b>not</b> trade here (${esc(p.gate_reason)}). It's still offered as training data.`}</p>
          <div class="row">
            <button class="btn good" data-cmd="approve" data-arg="${esc(p.id)}" data-confirm="Open training trade ${esc(p.id)}: ${esc(p.strategy)} ×1 on the ${esc(p.expiry)} expiry? Paper money; worst case ${inr(p.max_loss)}.">Accept trade</button>
            <button class="btn" data-cmd="reject" data-arg="${esc(p.id)}">Skip today</button>
          </div>
        </div><div class="chart">${payoffChart(p.payoff, p.spot)}<div class="muted small">P&amp;L at expiry vs Nifty</div></div></div></div>`;
    }
    for (const pos of t.positions) {
      h += `<div class="card" style="margin-top:12px"><h2>${esc(pos.id)} · ${esc(pos.strategy)} ×${pos.lots} <span class="muted small">opened ${esc(pos.opened)} · expiry ${esc(pos.expiry)} · ${pos.gate_open ? "gates open" : esc(pos.gate_reason)}</span></h2>
        <div class="split"><div>${legTable(pos.legs, true)}
          <div class="facts">
            <div><div class="l">MTM</div><div class="v ${cls(pos.mtm)}">${inr(pos.mtm)}</div></div>
            <div><div class="l">Credit</div><div class="v">${inr(pos.credit)}</div></div>
            <div><div class="l">Target</div><div class="v">${inr(pos.tp_rs)}</div></div>
            <div><div class="l">Worst case</div><div class="v neg">${inr(-pos.max_loss)}</div></div>
          </div>
          <button class="btn danger" data-cmd="flatten" data-arg="${esc(pos.id)}" data-confirm="Close training trade ${esc(pos.id)} now?">Close this trade</button>
        </div><div class="chart">${payoffChart(pos.payoff, s.signal && s.signal.spot)}</div></div></div>`;
    }
    if (!p && !t.positions.length) h += `<div class="empty">No training proposal right now. One is made each trading day from 09:45 IST.</div>`;
    return h + "</article>";
  }

  function crudeSection(s) {
    const c = s.crude;
    if (!c) return "";
    const g = c.signal || {};
    const sess = c.session ? `${c.session[0]}–${c.session[1]} IST` : "closed today";
    const jr = [...(c.journals.main || []), ...(c.journals.training || [])];
    const realized = jr.reduce((a, r) => a + (+r.pnl || 0), 0);
    let h = `<article class="card"><h2>Crude oil · MCX Crude Oil Mini options <span class="muted small">${c.mcx_open ? "MCX open" : "MCX closed"} · session ${esc(sess)} · quotes: ${esc(c.quotes)}</span></h2>
      <div class="sig">
        <div><div class="l">MCX crude (≈ WTI × ₹)</div><div class="v">₹${num(g.mcx, 0)}</div></div>
        <div><div class="l">WTI · USD/INR</div><div class="v">$${num(g.wti, 2)} · ${num(g.inr, 2)}</div></div>
        <div><div class="l">OVX (crude implied vol)</div><div class="v">${num(g.ovx)}</div></div>
      </div>
      <div class="gate">
        <div class="g"><span><span class="dot ${g.ovx_ch5 <= 0.15 ? "ok" : "bad"}"></span>OVX 5-day change <span class="muted small">(≤ +15%)</span></span><span>${pct(g.ovx_ch5)}</span></div>
        <div class="g"><span><span class="dot ${g.ret5 >= -0.05 ? "ok" : "bad"}"></span>Crude 5-day return <span class="muted small">(≥ −5%)</span></span><span>${pct(g.ret5)}</span></div>
        <div class="g"><span><span class="dot ${g.dd20 >= -0.10 ? "ok" : "bad"}"></span>Below 20-day high <span class="muted small">(≥ −10%)</span></span><span>${pct(g.dd20)}</span></div>
        <div class="g"><span><span class="dot ${g.ovx <= 70 ? "ok" : "bad"}"></span>OVX level <span class="muted small">(≤ 70)</span></span><span>${num(g.ovx)}</span></div>
      </div>
      <div class="verdict">${g.stress ? `<b class="neg">Stress regime.</b> The main crude book is blocked (${esc((g.stress_reasons || []).join("; "))}). Training trades are still offered, labelled.` : `<b class="pos">Gate open.</b> The main crude book may propose a monthly condor.`}
        <div class="muted small">Daily data as of ${esc(g.asof)} · main-book paper cycles: ${c.paper_trades}/20 · closed crude trades: ${jr.length}, realized ${inr(realized)}</div></div>`;
    for (const [book, p] of Object.entries(c.pending || {})) {
      h += `<div class="card" style="margin-top:12px"><h2>${book === "main" ? "Main" : "Training"} crude proposal ${esc(p.id)} <span class="muted small">valid until ${esc(p.valid_until.slice(11))} IST · options expiry ${esc(p.expiry)} (${p.td_left} trading days) · 1 Mini lot = 10 bbl</span></h2>
        <div class="split"><div>
          ${legTable(p.legs.map((l) => ({ ...l, entry: l.limit, lots: 1 })), false)}
          <div class="facts">
            <div><div class="l">Net credit</div><div class="v">${inr(p.credit)}</div></div>
            <div><div class="l">Worst case</div><div class="v neg">${inr(p.max_loss)}</div></div>
            <div><div class="l">ATM IV · futures</div><div class="v">${num(p.atm_iv * 100)} · ${num(p.spot, 0)}</div></div>
            <div><div class="l">Gates say</div><div class="v ${p.gate_open ? "pos" : "neg"}">${p.gate_open ? "trade" : "skip"}</div></div>
          </div>
          <p class="small">${p.gate_open ? "Gates open." : `Gates say skip (${esc(p.gate_reason)}). Offered as training data.`} Closed automatically from 17:00 IST two trading days before options expiry, so nothing turns into futures.</p>
          <div class="row">
            <button class="btn good" data-cmd="approve" data-arg="${esc(p.id)}" data-confirm="Open crude ${book} trade ${esc(p.id)} (1 Mini lot, paper)? Worst case ${inr(p.max_loss)}.">Accept trade</button>
            <button class="btn" data-cmd="reject" data-arg="${esc(p.id)}">Skip today</button>
          </div>
        </div><div class="chart">${payoffChart(p.payoff, p.spot)}<div class="muted small">P&amp;L at expiry vs MCX crude (₹/bbl)</div></div></div></div>`;
    }
    for (const pos of c.positions || []) {
      h += `<div class="card" style="margin-top:12px"><h2>${esc(pos.id)} · crude ${esc(pos.book)} ×1 <span class="muted small">opened ${esc(pos.opened)} · options expiry ${esc(pos.expiry)} · ${pos.gate_open ? "gates open" : esc(pos.gate_reason)}</span></h2>
        <div class="split"><div>${legTable(pos.legs, true)}
          <div class="facts">
            <div><div class="l">MTM</div><div class="v ${cls(pos.mtm)}">${inr(pos.mtm)}</div></div>
            <div><div class="l">Credit</div><div class="v">${inr(pos.credit)}</div></div>
            <div><div class="l">Target</div><div class="v">${inr(pos.tp_rs)}</div></div>
            <div><div class="l">Worst case</div><div class="v neg">${inr(-pos.max_loss)}</div></div>
          </div>
          <button class="btn danger" data-cmd="flatten" data-arg="${esc(pos.id)}" data-confirm="Close crude trade ${esc(pos.id)} now?">Close this trade</button>
        </div><div class="chart">${payoffChart(pos.payoff, g.mcx)}</div></div></div>`;
    }
    if (!Object.keys(c.pending || {}).length && !(c.positions || []).length) h += `<div class="empty">No crude proposal right now. Proposals come each MCX day from 10:00 to 22:00 IST (none on Wednesdays after 18:00, the EIA window).</div>`;
    if (jr.length) {
      const cols = ["closed", "id", "book", "expiry", "credit", "max_loss", "pnl", "reason", "gate_reason"];
      h += `<h2 style="margin-top:14px">Crude journal</h2><div class="tablewrap"><table><thead><tr>${cols.map((x) => `<th>${x.replace("_", " ")}</th>`).join("")}</tr></thead><tbody>${jr.slice().reverse().map((r) => `<tr>${cols.map((x) => `<td class="${x === "pnl" ? cls(+r[x]) : ""}">${["credit", "max_loss", "pnl"].includes(x) ? inr(+r[x]) : esc(r[x])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
    }
    return h + "</article>";
  }

  function legTable(legs, live) {
    return `<div class="tablewrap"><table><thead><tr><th>Leg</th><th>Lots</th><th>${live ? "Entry" : "Limit"}</th><th>Bid</th><th>Ask</th>${live ? "<th>P&amp;L</th>" : ""}</tr></thead><tbody>
      ${legs.map((l) => `<tr><td><span class="legside ${l.side}">${l.side}</span> ${l.cp === "C" ? "CE" : "PE"} ${Math.round(l.strike)}</td><td>${l.lots}</td>
      <td>${num(l.entry, 2)}</td><td>${num(l.bid, 2)}</td><td>${num(l.ask, 2)}</td>${live ? `<td class="${cls(l.pnl)}">${inr(l.pnl)}</td>` : ""}</tr>`).join("")}
      </tbody></table></div>`;
  }

  function renderJournal(rows) {
    if (!rows.length) { $("#journal").innerHTML = `<div class="empty">No closed trades yet.</div>`; return; }
    const cols = ["closed", "strategy", "expiry", "lots", "credit", "max_loss", "pnl", "reason", "mode"];
    $("#journal").innerHTML = `<table><thead><tr>${cols.map((c) => `<th>${c.replace("_", " ")}</th>`).join("")}</tr></thead><tbody>${rows.slice().reverse().map((r) => `<tr>${cols.map((c) => `<td class="${c === "pnl" ? cls(+r[c]) : ""}">${["credit", "max_loss", "pnl"].includes(c) ? inr(+r[c]) : esc(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  }

  function renderTJournal(rows) {
    if (!rows.length) { $("#tjournal").innerHTML = `<div class="empty">No closed training trades yet.</div>`; return; }
    const cols = ["closed", "id", "expiry", "td_left_at_entry", "credit", "max_loss", "pnl", "reason", "gate_reason", "vix", "rv_fcst"];
    $("#tjournal").innerHTML = `<table><thead><tr>${cols.map((c) => `<th>${c.replaceAll("_", " ")}</th>`).join("")}</tr></thead><tbody>${rows.slice().reverse().map((r) => `<tr>${cols.map((c) => `<td class="${c === "pnl" ? cls(+r[c]) : ""}">${["credit", "max_loss", "pnl"].includes(c) ? inr(+r[c]) : ["vix", "rv_fcst"].includes(c) ? num(+r[c]) : esc(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  }

  function renderEquity(series) {
    const pts = series.map(([t, v]) => [new Date(t).getTime(), v]);
    $("#eqNote").textContent = pts.length ? `${pts.length} marks` : "";
    $("#equityChart").innerHTML = lineChart(pts, { fmt: (v) => "₹" + (v / 1000).toFixed(0) + "k", xfmt: (x) => new Date(x).toLocaleDateString("en-IN", { day: "numeric", month: "short" }), zero: S ? S.capital_base : null });
  }

  // ---------------- commands ----------------
  function wireButtons() {
    document.querySelectorAll("[data-cmd]").forEach((btn) => {
      if (btn._wired) return; btn._wired = true;
      btn.addEventListener("click", async () => {
        const cmd = btn.dataset.cmd, arg = btn.dataset.arg || "";
        if (btn.dataset.confirm && !(await ask("Confirm", btn.dataset.confirm, null))) return;
        send(cmd, arg);
      });
    });
  }

  function ask(title, text, placeholder) {
    return new Promise((res) => {
      $("#modalTitle").textContent = title; $("#modalText").textContent = text;
      const inp = $("#modalInput"); inp.hidden = placeholder == null; inp.value = ""; inp.placeholder = placeholder || "";
      $("#modal").hidden = false; if (!inp.hidden) inp.focus();
      const done = (v) => { $("#modal").hidden = true; $("#modalOk").onclick = $("#modalCancel").onclick = null; res(v); };
      $("#modalOk").onclick = () => done(inp.hidden ? true : inp.value.trim());
      $("#modalCancel").onclick = () => done(null);
    });
  }

  async function send(cmd, arg) {
    const title = `kavach: ${cmd}${arg ? " " + arg : ""}`;
    const tok = getToken();
    const st = $("#cmdStatus");
    if (!tok) {
      const url = `https://github.com/${OWNER}/${REPO}/issues/new?title=${encodeURIComponent(title)}&body=${encodeURIComponent("Sent from the KAVACH dashboard. Submit to execute; the runner obeys issues from the repo owner only.")}`;
      window.open(url, "_blank", "noopener");
      st.innerHTML = `Opened GitHub with <code>${esc(title)}</code>. Press <b>Submit new issue</b>; the runner applies it in about 1–2 minutes.`;
      return;
    }
    st.textContent = `Sending ${title}…`;
    try {
      const r = await fetch(`https://api.github.com/repos/${OWNER}/${REPO}/issues`, {
        method: "POST", headers: { Authorization: `Bearer ${tok}`, Accept: "application/vnd.github+json", "Content-Type": "application/json" },
        body: JSON.stringify({ title, body: "Sent from the KAVACH dashboard." }),
      });
      if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 160)}`);
      const iss = await r.json();
      st.innerHTML = `Sent as <a href="${esc(iss.html_url)}" target="_blank" rel="noopener">issue #${iss.number}</a>. The runner applies it in about 1–2 minutes; this page refreshes itself.`;
    } catch (e) { st.innerHTML = `<span class="neg">Could not file the issue: ${esc(e.message)}</span>`; }
  }

  $("#resetHalt").addEventListener("click", async () => {
    const d = new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
    const v = await ask("Reset drawdown halt", `Clears the halt and resets the equity peak. Type exactly: RESET HALT ${d}`, `RESET HALT ${d}`);
    if (v) send("reset-halt", v);
  });
  $("#acceptLimits").addEventListener("click", async () => {
    const d = new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
    const v = await ask("Accept edited limits", `Tightening applies now. Loosening needs this phrase and takes effect tomorrow: I ACCEPT LOOSER LIMITS ${d} (leave blank if you are only tightening)`, "");
    if (v !== null) send("limits-accept", v);
  });
  $("#saveToken").addEventListener("click", () => { try { localStorage.setItem(tokenKey, $("#tokenInput").value.trim()); } catch {} $("#tokenInput").value = ""; tokenState(); });
  $("#clearToken").addEventListener("click", () => { try { localStorage.removeItem(tokenKey); } catch {} tokenState(); });
  function tokenState() {
    const t = getToken();
    $("#tokenState").textContent = t ? "Token saved in this browser: buttons file issues directly." : "No token: buttons open a pre-filled GitHub issue for you to submit.";
    $("#cmdHint").textContent = t ? "One-click mode." : "Buttons open a pre-filled GitHub issue; press Submit. (Optional token in Settings.)";
  }

  // ---------------- loop ----------------
  async function refresh() {
    try {
      const [s, jr, eq, lg] = await Promise.all([j("data/status.json"), j("data/journal.json"), j("data/equity.json"), j("data/log.json")]);
      render(s); renderJournal(jr); renderEquity(eq);
      j("data/training_journal.json").then(renderTJournal).catch(() => renderTJournal([]));
      $("#log").textContent = lg.slice(-120).reverse().join("\n");
    } catch (e) {
      $("#signal").innerHTML = `<div class="empty">Waiting for the first engine run… (${esc(e.message)})</div>`;
    }
  }
  tokenState(); refresh(); setInterval(refresh, 60000);
})();
