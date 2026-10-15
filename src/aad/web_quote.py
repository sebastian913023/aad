"""The customer-quote tab for the technician web UI (House Call pricing).

A port of the standalone "HouseCall Quote Terminal" calculator — same vehicle tiers,
same flat-rate roadside prices, same job presets, same math — wired to this service's
own lookups. The difference from the standalone page is that a line's labor hours or
parts price can be filled from a *cited* lookup (`/api/v1/labor-times`,
`/api/v1/parts/search`), and the quote then says where each figure came from.

The grounding rule holds here too. Nothing in this module invents a published figure:
a lookup that finds nothing shows the API's own "not available" notice, and a figure
is only copied onto a line when the user clicks "Use". Preset hours are the shop's own
planning numbers and are labelled "house preset", never presented as manufacturer time.

Class names follow BEM (`quote__element--modifier`) so they cannot collide with the
rest of the page's styles.
"""

from __future__ import annotations

QUOTE_CSS = """
.quote__grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,340px); gap:14px;
  align-items:start; }
@media (max-width:820px) { .quote__grid { grid-template-columns:minmax(0,1fr); } }
.quote__hint { color:var(--muted); font-size:13px; margin:-4px 0 10px; }
.quote__tiers, .quote__presets { display:flex; flex-wrap:wrap; gap:6px; }
.quote__tier { display:flex; flex-direction:column; align-items:flex-start; gap:2px;
  background:var(--panel2); color:var(--text); border:1px solid var(--line); font-weight:600;
  font-size:13px; }
.quote__tier--active { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent) inset; }
.quote__tier-mult { font-family:ui-monospace,monospace; font-size:12px; color:var(--muted);
  font-weight:500; }
.quote__preset { background:var(--panel2); color:var(--text); border:1px solid var(--line);
  font-weight:500; font-size:13px; }
.quote__preset-note { color:var(--muted); font-size:12px; }
.quote__detected { margin-top:10px; font-size:13px; color:var(--muted); }
.quote__tag { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px;
  background:var(--panel2); border:1px solid var(--line); color:var(--text); margin-right:4px; }
.quote__line { border:1px solid var(--line); border-radius:8px; padding:10px; margin-bottom:8px;
  background:var(--panel2); }
.quote__line-fields { display:grid; grid-template-columns:minmax(0,2fr) repeat(3,minmax(0,1fr)) auto;
  gap:8px; align-items:end; }
@media (max-width:820px) { .quote__line-fields { grid-template-columns:repeat(3,minmax(0,1fr)); }
  .quote__field--name { grid-column:1 / -1; } }
.quote__field { display:flex; flex-direction:column; gap:4px; font-size:12px; color:var(--muted);
  min-width:0; }
.quote__field input { width:100%; min-width:0; }
.quote__line-tools { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-top:8px; }
.quote__line-tools button { font-size:12px; padding:4px 9px; }
.quote__src { font-size:12px; color:var(--muted); }
.quote__src--cited { color:var(--good); }
.quote__lookup { margin-top:8px; }
.quote__lookup-item { display:flex; flex-wrap:wrap; gap:8px; align-items:center;
  justify-content:space-between; border:1px solid var(--line); border-radius:6px;
  padding:6px 9px; margin-top:6px; background:var(--panel); font-size:13px; }
.quote__settings { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }
.quote__toggle { display:flex; align-items:center; gap:8px; font-size:13px; color:var(--muted);
  margin-top:8px; }
.quote__ticket { position:sticky; top:64px; }
.quote__row { display:flex; justify-content:space-between; gap:12px; padding:5px 0;
  font-size:14px; color:var(--muted); }
.quote__row--strong { color:var(--text); border-top:1px solid var(--line); margin-top:4px;
  padding-top:8px; }
.quote__row--dim { font-size:12px; opacity:.8; }
.quote__val { font-family:ui-monospace,monospace; font-variant-numeric:tabular-nums; }
.quote__total { display:flex; justify-content:space-between; align-items:baseline; gap:12px;
  padding-top:10px; border-top:1px solid var(--line); margin-top:6px; }
.quote__total-val { font-family:ui-monospace,monospace; font-size:30px; font-weight:700;
  color:var(--accent); }
.quote__badge { font-family:ui-monospace,monospace; font-size:11px; text-transform:uppercase;
  letter-spacing:.06em; padding:3px 9px; border-radius:999px; border:1px solid var(--accent);
  color:var(--accent); }
.quote__actions { display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; }
.quote__actions button { flex:1; min-width:110px; }
.quote__flash { min-height:16px; font-size:12px; color:var(--good); margin-top:6px; }
.quote__note { color:var(--muted); font-size:12px; line-height:1.6; margin-top:12px; }
"""

QUOTE_TAB_BUTTON = '<button data-tab="quote">Quote</button>'

QUOTE_PANEL = """
  <div class="panel" id="panel-quote">
    <div class="quote__grid">
      <div>
        <div class="card">
          <h2>Describe the job</h2>
          <p class="quote__hint">Tell it what the customer told you. It matches plain-English job
            and vehicle keywords; review every line.</p>
          <div class="row" style="margin-bottom:6px">
            <label style="flex:1">Customer description
              <textarea id="quote-text" rows="3"
                placeholder="2021 BMW X5, grinding noise from front brakes, needs an oil change"></textarea>
            </label>
          </div>
          <div class="row" style="margin-bottom:0">
            <button id="quote-parse" type="button">Build quote from description</button>
            <button id="quote-clear-text" class="secondary" type="button">Clear text</button>
          </div>
          <div id="quote-detected" class="quote__detected" hidden></div>
        </div>

        <div class="card">
          <h2>Vehicle tier</h2>
          <p class="quote__hint">Scales labor on hourly lines. Follows the make in the vehicle bar
            above; pick one to override.</p>
          <div id="quote-tiers" class="quote__tiers"></div>
        </div>

        <div class="card">
          <h2>Roadside &amp; quick service</h2>
          <p class="quote__hint">Flat rate. Tap to add.</p>
          <div id="quote-roadside" class="quote__presets"></div>
        </div>

        <div class="card">
          <h2>Common repair jobs</h2>
          <p class="quote__hint">House preset hours, not manufacturer time. Use "Labor time" on a
            line to look up a cited figure.</p>
          <div id="quote-repairs" class="quote__presets"></div>
        </div>

        <div class="card">
          <h2>Job lines <span id="quote-line-count" class="quote__src"></span></h2>
          <div id="quote-lines"></div>
          <button id="quote-add-line" class="secondary" type="button">+ Add custom line</button>
        </div>

        <div class="card">
          <h2>Shop settings</h2>
          <div class="quote__settings">
            <label class="quote__field">Base labor rate ($/hr)
              <input id="quote-rate" type="number" min="0" step="1" value="95"></label>
            <label class="quote__field">Trip / mobile fee ($)
              <input id="quote-trip" type="number" min="0" step="1" value="0"></label>
            <label class="quote__field">Shop supplies (% of labor)
              <input id="quote-supplies" type="number" min="0" step="0.5" value="8"></label>
            <label class="quote__field">Sales tax (%)
              <input id="quote-tax" type="number" min="0" step="0.01" value="8.38"></label>
          </div>
          <label class="quote__toggle"><input id="quote-tax-parts" type="checkbox" checked>
            Tax parts</label>
          <label class="quote__toggle"><input id="quote-tax-labor" type="checkbox">
            Tax labor</label>
          <label class="quote__toggle"><input id="quote-waive-trip" type="checkbox" checked>
            Waive trip fee if job is booked</label>
        </div>
      </div>

      <div class="card quote__ticket">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          <h2 style="margin:0">Quote</h2>
          <span id="quote-badge" class="quote__badge"></span>
        </div>
        <div class="quote__row"><span>Labor</span><span class="quote__val" id="quote-sum-labor">$0.00</span></div>
        <div class="quote__row quote__row--dim" id="quote-adj-row"><span>Tier adjustment</span>
          <span class="quote__val" id="quote-sum-adj">+$0.00</span></div>
        <div class="quote__row"><span>Parts</span><span class="quote__val" id="quote-sum-parts">$0.00</span></div>
        <div class="quote__row"><span>Shop supplies</span><span class="quote__val" id="quote-sum-supplies">$0.00</span></div>
        <div class="quote__row"><span>Trip / mobile fee</span><span class="quote__val" id="quote-sum-trip">$0.00</span></div>
        <div class="quote__row quote__row--strong"><span>Subtotal</span>
          <span class="quote__val" id="quote-sum-subtotal">$0.00</span></div>
        <div class="quote__row"><span>Tax</span><span class="quote__val" id="quote-sum-tax">$0.00</span></div>
        <div class="quote__total"><span>Total</span><span class="quote__total-val" id="quote-sum-total">$0.00</span></div>
        <div class="quote__src" id="quote-hours-note">0.0 labor hrs across 0 lines</div>
        <div class="quote__actions">
          <button id="quote-clear" class="secondary" type="button">Clear all</button>
          <button id="quote-copy" type="button">Copy quote</button>
        </div>
        <div id="quote-flash" class="quote__flash" aria-live="polite"></div>
        <p class="quote__note">Quote only: final price is confirmed before work begins. The tier
          multiplier scales the labor line only; parts you enter already reflect real part cost.
          Sales-tax treatment of labor varies by state; verify Minnesota's current rule. Hours and
          prices marked "cited" came from a lookup in this service; "house preset" and "manual"
          figures did not.</p>
      </div>
    </div>
  </div>
"""

QUOTE_JS = r"""
// --- quote tab ---------------------------------------------------------------------
const QUOTE = (() => {
  const TIERS = [
    { key:"standard", name:"Domestic / Asian", mult:1.00, brands:["ford","chevrolet","chevy","gmc",
      "dodge","ram","chrysler","jeep","buick","cadillac","lincoln","toyota","honda","nissan","mazda",
      "subaru","hyundai","kia","mitsubishi","suzuki","lexus","acura","infiniti"] },
    { key:"truck", name:"Truck / Diesel", mult:1.10, brands:["duramax","cummins","powerstroke",
      "freightliner","international","f-250","f250","f-350","f350"] },
    { key:"euro", name:"Euro Premium", mult:1.25, brands:["bmw","mercedes","mercedes-benz","audi",
      "volkswagen","vw","volvo","land rover","landrover","range rover","jaguar","mini","fiat",
      "alfa romeo","saab"] },
    { key:"exotic", name:"Exotic / Ultra-luxury", mult:1.45, brands:["porsche","ferrari",
      "lamborghini","maserati","bentley","rolls-royce","rolls royce","aston martin","mclaren","tesla"] },
  ];
  const ROADSIDE = [
    { name:"Jump start", flat:49, note:"flat" },
    { name:"Lockout", flat:59, note:"flat" },
    { name:"Flat tire swap", flat:59, note:"flat" },
    { name:"Fuel delivery", flat:45, note:"+ gas" },
    { name:"Battery replacement", flat:150, note:"$120–200 typ." },
  ];
  const REPAIR = [
    { name:"Oil change & filter", hours:0.5, kw:["oil change","oil and filter","oil service"] },
    { name:"Brake pads (per axle)", hours:1.0, kw:["brake pad","brake pads","pads","squeaking brakes","squealing brakes"] },
    { name:"Pads + rotors (per axle)", hours:1.5, kw:["rotor","rotors","grinding brakes","grinding noise","warped rotor"] },
    { name:"Alternator replacement", hours:1.5, kw:["alternator","battery light","not charging","charging issue"] },
    { name:"Starter replacement", hours:1.3, kw:["starter","no crank","clicking when starting","won't start","wont start"] },
    { name:"Serpentine belt", hours:0.7, kw:["serpentine belt","squealing belt","belt noise"] },
    { name:"Spark plugs", hours:1.0, kw:["spark plug","spark plugs","misfire","rough idle"] },
    { name:"AC recharge", hours:1.0, kw:["ac recharge","a/c recharge","air conditioning","ac not cold","ac blowing warm"] },
    { name:"Check-engine diagnosis", hours:1.0, kw:["check engine","check-engine","cel","diagnostic","diagnosis","engine light"] },
    { name:"Coolant flush", hours:0.8, kw:["coolant flush","coolant leak","overheating","radiator"] },
    { name:"Water pump", hours:2.5, kw:["water pump"] },
    { name:"Timing belt", hours:4.0, kw:["timing belt","timing chain"] },
  ];
  const ROADSIDE_KW = {
    "Jump start":["jump start","dead battery","won't start","battery dead"],
    "Lockout":["lockout","locked out","keys locked"],
    "Flat tire swap":["flat tire","flat","spare tire"],
    "Fuel delivery":["ran out of gas","out of fuel","empty tank"],
    "Battery replacement":["new battery","battery replacement","bad battery"],
  };
  const SETTINGS_KEY = "aad.quote.settings";

  let lines = [];
  let seq = 0;
  let tier = TIERS[0];

  const money = (n, sign) => {
    if (!isFinite(n)) n = 0;
    const s = n < 0 ? "-" : (sign && n > 0 ? "+" : "");
    return s + "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits:2, maximumFractionDigits:2 });
  };
  const num = (id) => { const v = parseFloat($("#" + id).value); return isFinite(v) ? v : 0; };

  function tierFor(text) {
    const t = (text || "").toLowerCase();
    if (!t.trim()) return null;
    for (const cand of TIERS.slice().sort((a, b) => b.mult - a.mult)) {
      const hit = cand.brands.find(b => t.includes(b));
      if (hit) return { tier: cand, brand: hit };
    }
    return null;
  }

  function setTier(key) {
    const t = TIERS.find(x => x.key === key);
    if (!t) return;
    tier = t;
    $$(".quote__tier").forEach(b => b.classList.toggle("quote__tier--active", b.dataset.tier === key));
    calc();
  }

  function buildTiers() {
    const host = $("#quote-tiers");
    host.innerHTML = "";
    TIERS.forEach(t => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "quote__tier" + (t.key === tier.key ? " quote__tier--active" : "");
      b.dataset.tier = t.key;
      b.innerHTML = `<span>${esc(t.name)}</span><span class="quote__tier-mult">${t.mult.toFixed(2)}×</span>`;
      b.addEventListener("click", () => setTier(t.key));
      host.appendChild(b);
    });
  }

  function buildPresets() {
    const r = $("#quote-roadside");
    ROADSIDE.forEach(p => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "quote__preset secondary";
      b.innerHTML = `${esc(p.name)} <span class="quote__preset-note">— ${money(p.flat)} ${esc(p.note)}</span>`;
      b.addEventListener("click", () => addLine({ name:p.name, mode:"flat", flat:p.flat, hours:0.25,
        parts:0, qty:1, tiered:false, hoursSource:null, partsSource:null }));
      r.appendChild(b);
    });
    const j = $("#quote-repairs");
    REPAIR.forEach(p => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "quote__preset secondary";
      b.innerHTML = `${esc(p.name)} <span class="quote__preset-note">— ${p.hours.toFixed(1)} hr</span>`;
      b.addEventListener("click", () => addRepair(p));
      j.appendChild(b);
    });
  }

  function addRepair(p) {
    return addLine({ name:p.name, mode:"hourly", hours:p.hours, parts:0, qty:1, tiered:true,
      hoursSource:"house preset", partsSource:null });
  }

  function addLine(data) {
    const id = "ql" + (seq++);
    lines.push(Object.assign({ id, tiered:true, hoursSource:null, partsSource:null }, data));
    renderLines();
    calc();
    return id;
  }

  function findLine(id) { return lines.find(l => l.id === id); }

  function sourceBadge(src, kind) {
    if (!src) return `<span class="quote__src">${kind}: manual</span>`;
    if (typeof src === "string") return `<span class="quote__src">${kind}: ${esc(src)}</span>`;
    return `<span class="quote__src quote__src--cited">${kind}: cited — ${esc(src.label)}</span>`;
  }

  function renderLines() {
    const host = $("#quote-lines");
    host.innerHTML = "";
    $("#quote-line-count").textContent = lines.length + (lines.length === 1 ? " line" : " lines");
    if (!lines.length) {
      host.innerHTML = `<div class="empty">No lines yet. Describe the job above, or tap a preset.</div>`;
      return;
    }
    lines.forEach(line => {
      const row = document.createElement("div");
      row.className = "quote__line";
      row.dataset.id = line.id;
      const flat = line.mode === "flat";
      row.innerHTML = `
        <div class="quote__line-fields">
          <label class="quote__field quote__field--name">Job
            <input type="text" data-f="name" value="${esc(line.name)}"></label>
          <label class="quote__field">${flat ? "Flat price ($)" : "Hours"}
            <input type="number" min="0" step="${flat ? 1 : 0.1}" data-f="${flat ? "flat" : "hours"}"
              value="${flat ? line.flat : line.hours}"></label>
          <label class="quote__field">Parts ($ each)
            <input type="number" min="0" step="1" data-f="parts" value="${line.parts}"></label>
          <label class="quote__field">Qty
            <input type="number" min="1" step="1" data-f="qty" value="${line.qty}"></label>
          <button class="remove" type="button" data-act="remove" aria-label="Remove line">✕</button>
        </div>
        <div class="quote__line-tools">
          ${flat ? "" : `<button class="secondary" type="button" data-act="labor">Labor time</button>`}
          <button class="secondary" type="button" data-act="parts">Parts price</button>
          ${flat ? "" : sourceBadge(line.hoursSource, "hours")}
          ${line.parts > 0 || line.partsSource ? sourceBadge(line.partsSource, "parts") : ""}
        </div>
        <div class="quote__lookup" data-lookup></div>`;
      row.querySelectorAll("input").forEach(inp => inp.addEventListener("input", () => {
        const l = findLine(line.id); if (!l) return;
        const f = inp.dataset.f;
        if (f === "name") l.name = inp.value;
        else {
          l[f] = parseFloat(inp.value) || 0;
          if (f === "hours") l.hoursSource = null;
          if (f === "parts") l.partsSource = null;
        }
        calc();
      }));
      row.querySelector('[data-act="remove"]').addEventListener("click", () => {
        lines = lines.filter(l => l.id !== line.id);
        renderLines(); calc();
      });
      const laborBtn = row.querySelector('[data-act="labor"]');
      if (laborBtn) laborBtn.addEventListener("click", () => lookupLabor(line.id, row));
      row.querySelector('[data-act="parts"]').addEventListener("click", () => lookupParts(line.id, row));
      host.appendChild(row);
    });
  }

  // --- cited lookups: shown, never auto-applied ---------------------------------------
  function citationLabel(c) {
    if (!c) return "";
    return [c.source, c.section, c.page != null ? "p." + c.page : null].filter(Boolean).join(" · ");
  }

  function lookupError(box, res) {
    box.innerHTML = `<div class="error">${esc(res.data.detail || res.data.error || ("HTTP " + res.status))}</div>`;
  }

  async function lookupLabor(id, row) {
    const line = findLine(id); if (!line) return;
    const box = row.querySelector("[data-lookup]");
    box.innerHTML = `<div class="empty"><span class="spinner"></span> looking up published time…</div>`;
    const res = await api("/api/v1/labor-times", { operation: line.name, vehicle: vehiclePayload() });
    if (!res.ok) return lookupError(box, res);
    const d = res.data;
    let html = renderGap(d);
    (d.labor_times || []).forEach((t, i) => {
      const cites = t.citations && t.citations.length ? t.citations : (t.citation ? [t.citation] : []);
      const label = [d.source, citationLabel(cites[0])].filter(Boolean).join(" · ") || "lookup";
      const warranty = t.is_warranty_time === true;
      html += `<div class="quote__lookup-item">
        <span><b>${esc(t.hours)} hrs</b> ${esc(t.operation || "")}
          ${warranty ? ' <span class="quote__tag">warranty time, not for customer pricing</span>' : ""}
          <div class="quote__src">${esc(label)}</div></span>
        ${warranty ? "" : `<button type="button" data-use="${i}">Use ${esc(t.hours)} hrs</button>`}</div>`;
    });
    if (d.provider_verified === false) {
      html = `<div class="gap"><b>Unverified provider mapping.</b> Confirm this time against the
        labor guide before quoting it.</div>` + html;
    }
    box.innerHTML = html || `<div class="empty">No result.</div>`;
    box.querySelectorAll("[data-use]").forEach(btn => btn.addEventListener("click", () => {
      const t = d.labor_times[parseInt(btn.dataset.use, 10)];
      const cites = t.citations && t.citations.length ? t.citations : (t.citation ? [t.citation] : []);
      const l = findLine(id); if (!l) return;
      l.hours = Number(t.hours);
      l.hoursSource = { label: [d.source, citationLabel(cites[0])].filter(Boolean).join(" · ") || "lookup" };
      renderLines(); calc();
    }));
  }

  async function lookupParts(id, row) {
    const line = findLine(id); if (!line) return;
    const box = row.querySelector("[data-lookup]");
    box.innerHTML = `<div class="empty"><span class="spinner"></span> looking up parts…</div>`;
    const res = await api("/api/v1/parts/search", { query: line.name, vehicle: vehiclePayload(), limit: 6 });
    if (!res.ok) return lookupError(box, res);
    const d = res.data;
    let html = renderGap(d);
    (d.parts || []).forEach((p, i) => {
      const priced = typeof p.price === "number";
      html += `<div class="quote__lookup-item">
        <span><b>${esc(p.description || p.part_number)}</b>${priced ? " — " + money(p.price) : ""}
          <div class="quote__src">${esc([p.brand, p.part_number, p.availability, d.source].filter(Boolean).join(" · "))}</div></span>
        ${priced ? `<button type="button" data-use="${i}">Use ${money(p.price)}</button>` : '<span class="quote__src">no price returned</span>'}</div>`;
    });
    if (d.provider_verified === false) {
      html = `<div class="gap"><b>Unverified provider mapping.</b> Confirm price and fitment with the
        supplier before quoting.</div>` + html;
    }
    box.innerHTML = html || `<div class="empty">No result.</div>`;
    box.querySelectorAll("[data-use]").forEach(btn => btn.addEventListener("click", () => {
      const p = d.parts[parseInt(btn.dataset.use, 10)];
      const l = findLine(id); if (!l) return;
      l.parts = p.price;
      l.partsSource = { label: [p.part_number, d.source].filter(Boolean).join(" · ") || "lookup" };
      renderLines(); calc();
    }));
  }

  // --- description parser --------------------------------------------------------------
  function parseDescription() {
    const text = $("#quote-text").value.toLowerCase();
    const box = $("#quote-detected");
    if (!text.trim()) { box.hidden = true; return; }
    const found = tierFor(text);
    const added = [];
    REPAIR.forEach(p => {
      if (p.kw.some(k => text.includes(k))) { addRepair(p); added.push(p.name); }
    });
    Object.keys(ROADSIDE_KW).forEach(name => {
      if (ROADSIDE_KW[name].some(k => text.includes(k))) {
        const pre = ROADSIDE.find(r => r.name === name);
        addLine({ name, mode:"flat", flat:pre.flat, hours:0.25, parts:0, qty:1, tiered:false });
        added.push(name);
      }
    });
    if (found) setTier(found.tier.key);
    let html = found
      ? `<b>Vehicle:</b> detected "${esc(found.brand)}" → <span class="quote__tag">${esc(found.tier.name)} · ${found.tier.mult.toFixed(2)}×</span><br>`
      : `<b>Vehicle:</b> no make detected, left on ${esc(tier.name)}. Adjust manually if needed.<br>`;
    html += added.length
      ? `<b>Jobs added:</b> ` + added.map(j => `<span class="quote__tag">${esc(j)}</span>`).join("")
      : `<b>Jobs added:</b> none matched. Add lines with the presets or a custom line.`;
    html += `<br>Set each line's parts cost; the description can't know real part prices.`;
    box.innerHTML = html;
    box.hidden = false;
  }

  // --- math (same as the standalone Quote Terminal) -------------------------------------
  function totals() {
    const rate = num("quote-rate"), tripFee = num("quote-trip"), suppliesPct = num("quote-supplies");
    const taxPct = num("quote-tax");
    const taxParts = $("#quote-tax-parts").checked, taxLabor = $("#quote-tax-labor").checked;
    const waive = $("#quote-waive-trip").checked;
    let baseLabor = 0, tierAdj = 0, partsTotal = 0, hours = 0;
    lines.forEach(l => {
      const qty = l.qty > 0 ? l.qty : 1;
      const raw = l.mode === "flat" ? (l.flat || 0) : (l.hours || 0) * rate;
      const mult = (l.mode === "hourly" && l.tiered) ? tier.mult : 1;
      baseLabor += raw * qty;
      tierAdj += (raw * mult - raw) * qty;
      partsTotal += (l.parts || 0) * qty;
      if (l.mode === "hourly") hours += (l.hours || 0) * qty;
    });
    const labor = baseLabor + tierAdj;
    const supplies = labor * (suppliesPct / 100);
    const trip = (waive && lines.length > 0) ? 0 : tripFee;
    const subtotal = labor + partsTotal + supplies + trip;
    const taxable = (taxParts ? partsTotal : 0) + (taxLabor ? labor + supplies : 0);
    const tax = taxable * (taxPct / 100);
    return { baseLabor, tierAdj, partsTotal, supplies, trip, subtotal, tax, total: subtotal + tax, hours, rate };
  }

  function calc() {
    const t = totals();
    $("#quote-sum-labor").textContent = money(t.baseLabor);
    $("#quote-adj-row").hidden = Math.abs(t.tierAdj) <= 0.004;
    $("#quote-sum-adj").textContent = money(t.tierAdj, true);
    $("#quote-sum-parts").textContent = money(t.partsTotal);
    $("#quote-sum-supplies").textContent = money(t.supplies);
    $("#quote-sum-trip").textContent = money(t.trip);
    $("#quote-sum-subtotal").textContent = money(t.subtotal);
    $("#quote-sum-tax").textContent = money(t.tax);
    $("#quote-sum-total").textContent = money(t.total);
    $("#quote-hours-note").textContent =
      t.hours.toFixed(1) + " labor hrs across " + lines.length + (lines.length === 1 ? " line" : " lines");
    $("#quote-badge").textContent = tier.name + " · " + tier.mult.toFixed(2) + "×";
    saveSettings();
  }

  function saveSettings() {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify({
        rate: $("#quote-rate").value, trip: $("#quote-trip").value,
        supplies: $("#quote-supplies").value, tax: $("#quote-tax").value,
        taxParts: $("#quote-tax-parts").checked, taxLabor: $("#quote-tax-labor").checked,
        waive: $("#quote-waive-trip").checked, tier: tier.key,
      }));
    } catch (e) { /* storage unavailable: settings just won't persist */ }
  }

  function loadSettings() {
    try {
      const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "null");
      if (!s) return;
      if (s.rate != null) $("#quote-rate").value = s.rate;
      if (s.trip != null) $("#quote-trip").value = s.trip;
      if (s.supplies != null) $("#quote-supplies").value = s.supplies;
      if (s.tax != null) $("#quote-tax").value = s.tax;
      if (typeof s.taxParts === "boolean") $("#quote-tax-parts").checked = s.taxParts;
      if (typeof s.taxLabor === "boolean") $("#quote-tax-labor").checked = s.taxLabor;
      if (typeof s.waive === "boolean") $("#quote-waive-trip").checked = s.waive;
      const t = TIERS.find(x => x.key === s.tier);
      if (t) tier = t;
    } catch (e) { /* corrupt stored value: keep defaults */ }
  }

  // --- copy ------------------------------------------------------------------------------
  function quoteText() {
    const t = totals();
    const v = vehicleLabel(vehiclePayload());
    const out = ["HouseCall Automotive Repairs — Quote", "(218) 491-1832"];
    if (v) out.push("Vehicle: " + v);
    out.push("Vehicle tier: " + tier.name + " (" + tier.mult.toFixed(2) + "x labor)", "");
    lines.forEach(l => {
      const qty = l.qty > 0 ? l.qty : 1;
      const raw = l.mode === "flat" ? (l.flat || 0) : (l.hours || 0) * t.rate;
      const mult = (l.mode === "hourly" && l.tiered) ? tier.mult : 1;
      const total = (raw * mult + (l.parts || 0)) * qty;
      out.push("- " + l.name + (qty > 1 ? " x" + qty : "") + ": " + money(total));
    });
    out.push("", "Subtotal: " + money(t.subtotal), "Tax: " + money(t.tax), "Total: " + money(t.total), "",
      "Quote only — final price confirmed before work begins.");
    return out.join("\n");
  }

  function flash(msg) {
    const el = $("#quote-flash");
    el.textContent = msg;
    setTimeout(() => { el.textContent = ""; }, 3000);
  }

  async function copyQuote() {
    const text = quoteText();
    try {
      await navigator.clipboard.writeText(text);
      flash("Copied. Paste into a text or email.");
    } catch (e) {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (e2) { ok = false; }
      document.body.removeChild(ta);
      flash(ok ? "Copied. Paste into a text or email." : "Couldn't copy automatically. Select the total and copy it.");
    }
  }

  function follow() {
    // Keep the tier in step with the vehicle bar's make/model, unless nothing matches.
    const found = tierFor(($("#v-make").value + " " + $("#v-model").value));
    if (found && found.tier.key !== tier.key) setTier(found.tier.key);
  }

  function init() {
    loadSettings();
    buildTiers();
    buildPresets();
    renderLines();
    $("#quote-parse").addEventListener("click", parseDescription);
    $("#quote-clear-text").addEventListener("click", () => { $("#quote-text").value = ""; $("#quote-detected").hidden = true; });
    $("#quote-add-line").addEventListener("click", () =>
      addLine({ name:"Custom job", mode:"hourly", hours:1, parts:0, qty:1, tiered:true }));
    $("#quote-clear").addEventListener("click", () => { lines = []; renderLines(); calc(); });
    $("#quote-copy").addEventListener("click", copyQuote);
    ["quote-rate","quote-trip","quote-supplies","quote-tax"].forEach(id => $("#" + id).addEventListener("input", calc));
    ["quote-tax-parts","quote-tax-labor","quote-waive-trip"].forEach(id => $("#" + id).addEventListener("change", calc));
    ["v-make","v-model"].forEach(id => $("#" + id).addEventListener("change", follow));
    // VIN decode fills the vehicle bar without firing change events; sync when the tab opens.
    $$('nav.tabs button[data-tab="quote"]').forEach(b => b.addEventListener("click", follow));
    calc();
  }
  return { init, totals, quoteText, _state: () => ({ lines, tier }) };
})();
window.addEventListener("DOMContentLoaded", () => QUOTE.init());
"""
