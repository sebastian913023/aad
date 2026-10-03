"""The technician-facing application shell.

One self-contained HTML page — no build step, no framework, no CDN — that calls the
JSON API already defined in `aad.api.app`. This exists because a bare JSON API is not
something a tech at a bay can use; it is not a second source of truth about what the
system does, which is why it never renders a result the API did not return. A result
carrying `unavailable_reason` or a fabrication notice from the grounding monitor is
rendered as plainly as a successful one — hiding a gap here would undo the whole point
of the backend refusing to guess.

Original design: no part of this is modeled on, or intended to resemble, any specific
commercial product's interface.
"""

from __future__ import annotations

_CSS = """
:root {
  --bg:#0f1115; --panel:#171a21; --panel2:#1d212b; --line:#252a34; --text:#e6e9ef;
  --muted:#98a2b3; --good:#2ecc71; --warn:#f5a623; --bad:#ff4d4f; --info:#4d9fff;
  --accent:#4d9fff;
}
@media (prefers-color-scheme: light) {
  :root { --bg:#f3f5f8; --panel:#fff; --panel2:#f6f7f9; --line:#e3e6ec; --text:#12151b;
    --muted:#5b6472; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 ui-sans-serif,
  system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
a { color:var(--accent); }
header.top { position:sticky; top:0; z-index:10; background:var(--panel);
  border-bottom:1px solid var(--line); padding:10px 16px; display:flex;
  flex-wrap:wrap; align-items:center; gap:14px; }
header.top h1 { font-size:15px; margin:0; white-space:nowrap; letter-spacing:-.01em; }
header.top .sub { font-size:12px; color:var(--muted); }
.vehicle-bar { display:flex; flex-wrap:wrap; gap:6px; align-items:center; flex:1; }
.vehicle-bar input { width:92px; }
.vehicle-bar input.vin { width:170px; font-family:ui-monospace,monospace; }
.vehicle-bar .label-pill { font-size:12px; padding:4px 10px; border-radius:999px;
  background:var(--panel2); border:1px solid var(--line); color:var(--muted); }
.vehicle-bar .label-pill.scoped { color:var(--good); border-color:var(--good); }
input, textarea, select, button { font:inherit; color:var(--text); background:var(--panel2);
  border:1px solid var(--line); border-radius:7px; padding:7px 10px; }
button { cursor:pointer; background:var(--accent); color:#fff; border-color:var(--accent);
  font-weight:600; }
button.secondary { background:var(--panel2); color:var(--text); border-color:var(--line);
  font-weight:500; }
button:disabled { opacity:.5; cursor:default; }
button:active { transform:translateY(1px); }
nav.tabs { display:flex; gap:4px; padding:8px 16px 0; flex-wrap:wrap;
  border-bottom:1px solid var(--line); background:var(--bg); }
nav.tabs button { background:transparent; border:none; color:var(--muted);
  padding:9px 13px; border-radius:7px 7px 0 0; font-weight:600; font-size:13px; }
nav.tabs button.active { color:var(--text); background:var(--panel); border:1px solid var(--line);
  border-bottom-color:var(--panel); margin-bottom:-1px; }
main { max-width:980px; margin:0 auto; padding:20px 16px 60px; }
.panel { display:none; }
.panel.active { display:block; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:16px; margin-bottom:14px; }
.card h2 { font-size:14px; margin:0 0 10px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:flex-end; margin-bottom:10px; }
.row label { display:flex; flex-direction:column; gap:4px; font-size:12px; color:var(--muted); }
.row label input, .row label textarea { min-width:160px; }
.row textarea { min-width:280px; flex:1; resize:vertical; min-height:38px; }
.empty { color:var(--muted); font-size:14px; border:1px dashed var(--line); border-radius:8px;
  padding:14px; }
.error { color:var(--bad); font-size:14px; border:1px solid var(--bad); border-radius:8px;
  padding:10px 12px; background:rgba(255,77,79,.08); margin-top:10px; }
.result { margin-top:14px; }
.citation { display:inline-flex; align-items:center; gap:5px; font-size:12px;
  color:var(--muted); background:var(--panel2); border:1px solid var(--line);
  border-radius:6px; padding:3px 8px; margin:2px 4px 2px 0; }
.citation .dot { width:5px; height:5px; border-radius:50%; background:var(--accent); }
.gap { border:1px solid var(--warn); background:rgba(245,166,35,.08); border-radius:8px;
  padding:10px 12px; margin:8px 0; font-size:14px; }
.gap b { color:var(--warn); }
.result-item { border:1px solid var(--line); border-radius:8px; padding:10px 12px;
  margin-bottom:8px; background:var(--panel2); }
.result-item .kv { font-size:13px; color:var(--muted); }
.result-item .value { font-size:17px; font-weight:650; }
.result-item .snippet { font-size:13px; color:var(--muted); font-style:italic; margin-top:6px; }
pre.json { background:var(--panel2); border:1px solid var(--line); border-radius:8px;
  padding:10px; font-size:12px; overflow-x:auto; white-space:pre-wrap; word-break:break-word; }
.pill { display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px;
  font-weight:600; }
.pill.grounded { background:rgba(46,204,113,.15); color:var(--good); }
.pill.abstained { background:rgba(245,166,35,.15); color:var(--warn); }
.pill.escalated { background:rgba(77,159,255,.15); color:var(--info); }
.pill.blocked { background:rgba(255,77,79,.15); color:var(--bad); }
.pill.no_claims { background:rgba(152,162,179,.15); color:var(--muted); }
.chat { display:flex; flex-direction:column; gap:12px; margin-bottom:14px; }
.turn { border-radius:10px; padding:12px 14px; max-width:92%; }
.turn.user { align-self:flex-end; background:var(--accent); color:#fff; }
.turn.assistant { align-self:flex-start; background:var(--panel); border:1px solid var(--line);
  width:100%; max-width:100%; }
.turn.assistant .banner { font-size:12px; margin-bottom:8px; }
.turn .answer { white-space:pre-wrap; }
.turn .meta { margin-top:10px; font-size:12px; color:var(--muted); }
.turn .toolcalls { margin-top:8px; font-size:12px; color:var(--muted); }
.spinner { display:inline-block; width:13px; height:13px; border:2px solid var(--line);
  border-top-color:var(--accent); border-radius:50%; animation:spin .7s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.linebuilder { border:1px solid var(--line); border-radius:8px; padding:10px; margin-bottom:8px; }
.linebuilder .row { margin-bottom:6px; }
.remove { background:transparent; border:none; color:var(--bad); font-weight:700;
  cursor:pointer; padding:4px 8px; }
.totals { font-size:14px; margin-top:10px; }
.totals .big { font-size:22px; font-weight:700; }
footer.app-footer { text-align:center; color:var(--muted); font-size:12px; padding:20px 0; }
footer.app-footer a { color:var(--muted); }
"""

_JS = r"""
const $ = (sel, root=document) => root.querySelector(sel);
const $$ = (sel, root=document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",
  '"':"&quot;","'":"&#39;"}[c]));

function vehiclePayload() {
  const vin = $("#vin").value.trim();
  const year = $("#v-year").value.trim();
  const make = $("#v-make").value.trim();
  const model = $("#v-model").value.trim();
  const engine = $("#v-engine").value.trim();
  const v = {};
  if (vin) v.vin = vin;
  if (year) v.year = parseInt(year, 10);
  if (make) v.make = make;
  if (model) v.model = model;
  if (engine) v.engine = engine;
  return v;
}

function vehicleLabel(v) {
  if (v.year && v.make && v.model) return `${v.year} ${v.make} ${v.model}${v.engine ? " " + v.engine : ""}`;
  if (v.vin) return v.vin;
  return null;
}

function saveVehicle() {
  localStorage.setItem("aad.vehicle", JSON.stringify(vehiclePayload()));
  refreshVehiclePill();
}

function loadVehicle() {
  try {
    const v = JSON.parse(localStorage.getItem("aad.vehicle") || "{}");
    if (v.vin) $("#vin").value = v.vin;
    if (v.year) $("#v-year").value = v.year;
    if (v.make) $("#v-make").value = v.make;
    if (v.model) $("#v-model").value = v.model;
    if (v.engine) $("#v-engine").value = v.engine;
  } catch (e) { /* ignore a corrupt stored value */ }
  refreshVehiclePill();
}

function refreshVehiclePill() {
  const v = vehiclePayload();
  const pill = $("#vehicle-pill");
  const label = vehicleLabel(v);
  if (label) {
    pill.textContent = label;
    pill.classList.add("scoped");
  } else {
    pill.textContent = "no vehicle scoped";
    pill.classList.remove("scoped");
  }
}

async function decodeVin() {
  const vin = $("#vin").value.trim();
  if (!vin) return;
  const btn = $("#decode-btn");
  btn.disabled = true;
  try {
    const res = await api("/api/v1/vin/decode", { vin });
    if (res.ok && res.data.vehicle) {
      const vh = res.data.vehicle;
      if (vh.year) $("#v-year").value = vh.year;
      if (vh.make) $("#v-make").value = vh.make;
      if (vh.model) $("#v-model").value = vh.model;
      if (vh.engine) $("#v-engine").value = vh.engine;
      saveVehicle();
    } else {
      alert("VIN decode: " + (res.data.detail || res.data.error || "failed"));
    }
  } finally {
    btn.disabled = false;
  }
}

// When the server has AAD_API_TOKEN set, /api/v1/* needs it. Stored in this browser only.
function authHeaders(hasBody) {
  const h = {};
  if (hasBody) h["content-type"] = "application/json";
  let token = null;
  try { token = localStorage.getItem("aad.token"); } catch (e) {}
  if (token) h["authorization"] = "Bearer " + token;
  return h;
}

async function api(path, body, method="POST") {
  try {
    const res = await fetch(path, {
      method,
      headers: authHeaders(body !== undefined),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data;
    try { data = await res.json(); } catch (e) { data = {}; }
    return { ok: res.ok, status: res.status, data };
  } catch (e) {
    return { ok: false, status: 0, data: { detail: "network error: " + e.message } };
  }
}

function switchTab(name) {
  $$("nav.tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$("main .panel").forEach(p => p.classList.toggle("active", p.id === "panel-" + name));
  localStorage.setItem("aad.tab", name);
}

// --- generic, honest result rendering --------------------------------------------
// Renders exactly what the API returned. A citation object becomes a small pill; an
// unavailable_reason/instruction pair becomes a highlighted gap notice; anything else
// falls back to readable JSON. Nothing here infers or fills in a value the API did
// not send.

function renderCitation(c) {
  if (!c || !c.chunk_id) return "";
  const loc = [c.source, c.section, c.page != null ? "p." + c.page : null].filter(Boolean).join(" · ");
  return `<span class="citation"><span class="dot"></span>${esc(loc || c.source || "source")}</span>`;
}

function renderGap(data) {
  if (!data.unavailable_reason) return "";
  return `<div class="gap"><b>Not available.</b> ${esc(data.unavailable_reason)}` +
    (data.instruction ? `<div style="margin-top:4px;color:var(--muted)">${esc(data.instruction)}</div>` : "") +
    `</div>`;
}

function renderList(items, pickTitle, pickBody) {
  if (!items || !items.length) return "";
  return items.map(it => `<div class="result-item">
      <div class="value">${esc(pickTitle(it))}</div>
      ${pickBody(it)}
      ${it.citation ? renderCitation(it.citation) : ""}
    </div>`).join("");
}

function renderResult(toolName, data) {
  let html = renderGap(data);
  if (toolName === "torque" && data.specs) {
    html += renderList(data.specs,
      s => `${s.value}${s.value_high ? "–" + s.value_high : ""} ${s.unit || ""}`.trim(),
      s => `<div class="kv">${esc(s.component || "")}${s.bolt_size ? " · " + esc(s.bolt_size) : ""}
            ${s.sequence ? " · sequence noted" : ""}</div>
            ${s.verbatim_source_text ? `<div class="snippet">"${esc(s.verbatim_source_text)}"</div>` : ""}`);
  } else if (toolName === "labor" && data.labor_times) {
    html += renderList(data.labor_times,
      l => `${l.hours} hrs`,
      l => `<div class="kv">${esc(l.operation || "")}</div>
            ${l.verbatim_source_text ? `<div class="snippet">"${esc(l.verbatim_source_text)}"</div>` : ""}`);
  } else if (toolName === "wiring" && data.diagrams) {
    html += renderList(data.diagrams,
      d => esc(d.pin || d.wire_color || d.circuit || "pinout"),
      d => `<div class="kv">${esc(JSON.stringify(d).slice(0, 180))}</div>`);
  } else if (toolName === "dtc") {
    if (data.valid === false) {
      html += `<div class="gap"><b>Invalid code.</b> ${esc(data.error || "")}</div>`;
    } else if (data.valid) {
      html += `<div class="result-item"><div class="value">${esc(data.code)}</div>
        <div class="kv">${esc(data.system || "")} · ${esc(data.code_type || "")}
        ${data.code_type_note ? " (" + esc(data.code_type_note) + ")" : ""}</div></div>`;
    }
  } else if (toolName === "tsb") {
    html += renderList(data.recalls,
      r => esc(r.component || r.summary || "recall"),
      r => `<div class="kv">${esc((r.summary || "").slice(0, 300))}</div>`);
  } else if (toolName === "parts" && data.parts) {
    html += renderList(data.parts,
      p => `${p.description || p.part_number || "part"}${p.price ? " — $" + p.price : ""}`,
      p => `<div class="kv">${esc(p.part_number || "")}</div>`);
  } else if (toolName === "scan" && data.codes) {
    html += renderList(data.codes, c => esc(c.code || c), () => "");
  }
  if (data.supporting_text) {
    html += data.supporting_text.map(s =>
      `<div class="result-item"><div class="snippet">"${esc((s.text || "").slice(0, 300))}"</div>
       ${renderCitation(s.citation)}</div>`).join("");
  }
  if (!html.trim()) {
    html = `<pre class="json">${esc(JSON.stringify(data, null, 2))}</pre>`;
  }
  if (data.provider_verified === false) {
    html = `<div class="gap"><b>Unverified provider mapping.</b> This vendor's response
      shape has not been confirmed against a real account — see "providers show".</div>` + html;
  }
  return html;
}

async function runLookup(toolName, path, payload, resultSelector) {
  const out = $(resultSelector);
  out.innerHTML = `<div class="empty"><span class="spinner"></span> looking up…</div>`;
  const res = await api(path, { ...payload, vehicle: vehiclePayload() });
  if (!res.ok) {
    out.innerHTML = `<div class="error">${esc(res.data.detail || res.data.error || ("HTTP " + res.status))}</div>`;
    return;
  }
  out.innerHTML = `<div class="result">${renderResult(toolName, res.data)}</div>`;
}

// --- diagnose chat ----------------------------------------------------------------

let chatHistory = [];

function renderMonitorBanner(monitor) {
  if (!monitor) return "";
  const cls = monitor.verdict;
  const label = {
    grounded: "grounded — every claim checked against its citation",
    abstained: "abstained — " + (monitor.abstention_reason || "").replace(/_/g, " "),
    escalated: "escalated for human review",
    blocked: "blocked — a claim was not found in any cited source",
    no_claims: "no checkable specifications in this answer",
  }[cls] || cls;
  return `<div class="banner"><span class="pill ${esc(cls)}">${esc(label)}</span></div>`;
}

async function sendDiagnose() {
  const input = $("#diagnose-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  const chat = $("#chat");
  chat.insertAdjacentHTML("beforeend", `<div class="turn user">${esc(question)}</div>`);
  const thinkingId = "t" + Date.now();
  chat.insertAdjacentHTML("beforeend",
    `<div class="turn assistant" id="${thinkingId}"><span class="spinner"></span> thinking…</div>`);
  chat.scrollTop = chat.scrollHeight;

  const res = await api("/api/v1/diagnose", {
    question, vehicle: vehiclePayload(), history: chatHistory,
  });
  const node = $("#" + thinkingId);
  if (!res.ok) {
    node.outerHTML = `<div class="turn assistant"><div class="error">
      ${esc(res.data.detail || res.data.error || ("HTTP " + res.status))}</div></div>`;
    return;
  }
  const d = res.data;
  const citations = (d.citations || []).map(renderCitation).join(" ");
  const tools = (d.tool_calls || []).map(t => t.name).join(", ");
  node.outerHTML = `<div class="turn assistant">
    ${renderMonitorBanner(d.monitor)}
    <div class="answer">${esc(d.answer)}</div>
    ${citations ? `<div class="meta">${citations}</div>` : ""}
    ${tools ? `<div class="toolcalls">tools used: ${esc(tools)}</div>` : ""}
  </div>`;
  chatHistory.push({ role: "user", content: question });
  chatHistory.push({ role: "assistant", content: d.answer });
  chat.scrollTop = chat.scrollHeight;
}

// --- estimate builder --------------------------------------------------------------

function addLine(containerId, fields) {
  const container = $(containerId);
  const row = document.createElement("div");
  row.className = "linebuilder";
  row.innerHTML = `<div class="row">${fields.map(f =>
    `<label>${esc(f.label)}<input data-k="${f.key}" type="${f.type || "text"}" placeholder="${esc(f.placeholder || "")}"></label>`
  ).join("")}<button class="remove" type="button" onclick="this.closest('.linebuilder').remove()">✕</button></div>`;
  container.appendChild(row);
}

function collectLines(containerId) {
  return $$(containerId + " .linebuilder").map(row => {
    const out = {};
    $$("input", row).forEach(inp => {
      if (inp.value === "") return;
      out[inp.dataset.k] = inp.type === "number" ? parseFloat(inp.value) : inp.value;
    });
    return out;
  }).filter(o => Object.keys(o).length);
}

async function buildEstimate() {
  const payload = {
    vehicle: vehiclePayload(),
    labor_items: collectLines("#labor-lines"),
    part_items: collectLines("#part-lines"),
    fees: collectLines("#fee-lines"),
  };
  const out = $("#estimate-result");
  out.innerHTML = `<div class="empty"><span class="spinner"></span> calculating…</div>`;
  const res = await api("/api/v1/estimates", payload);
  if (!res.ok) {
    out.innerHTML = `<div class="error">${esc(res.data.detail || "failed")}</div>`;
    return;
  }
  const d = res.data;
  out.innerHTML = `
    <div class="totals">
      labor subtotal: $${(d.labor_subtotal ?? 0).toFixed(2)} ·
      parts subtotal: $${(d.parts_subtotal ?? 0).toFixed(2)}<br>
      <span class="big">total: $${(d.total ?? 0).toFixed(2)}</span>
    </div>
    ${(d.disclaimers || []).map(x => `<div class="gap">${esc(x)}</div>`).join("")}
    <pre class="json">${esc(JSON.stringify(d.lines || [], null, 2))}</pre>`;
}

// --- index stats footer -------------------------------------------------------------

async function loadFooterStats() {
  const res = await api("/api/v1/index/stats", undefined, "GET");
  const el = $("#footer-stats");
  if (res.status === 401) {
    const t = window.prompt("This server requires an API token:");
    if (t) { try { localStorage.setItem("aad.token", t.trim()); } catch (e) {} location.reload(); }
    el.textContent = "API token required";
    return;
  }
  if (!res.ok) { el.textContent = "index status unavailable"; return; }
  const n = res.data.chunks ?? 0;
  el.textContent = n === 0
    ? "0 chunks indexed — run `aad ingest add` against licensed documentation"
    : `${n} chunk(s) indexed across ${Object.keys(res.data.sources || {}).length} source(s)`;
}

window.addEventListener("DOMContentLoaded", () => {
  loadVehicle();
  $$("#vin, #v-year, #v-make, #v-model, #v-engine").forEach(() => {});
  ["vin", "v-year", "v-make", "v-model", "v-engine"].forEach(id => {
    $("#" + id).addEventListener("change", saveVehicle);
  });
  $("#decode-btn").addEventListener("click", decodeVin);

  $$("nav.tabs button").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  switchTab(localStorage.getItem("aad.tab") || "diagnose");

  $("#diagnose-send").addEventListener("click", sendDiagnose);
  $("#diagnose-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendDiagnose(); }
  });

  $("#torque-run").addEventListener("click", () =>
    runLookup("torque", "/api/v1/torque-specs", { component: $("#torque-component").value }, "#torque-result"));
  $("#labor-run").addEventListener("click", () =>
    runLookup("labor", "/api/v1/labor-times", { operation: $("#labor-operation").value }, "#labor-result"));
  $("#wiring-run").addEventListener("click", () =>
    runLookup("wiring", "/api/v1/wiring-diagrams", { circuit: $("#wiring-circuit").value }, "#wiring-result"));
  $("#dtc-run").addEventListener("click", () =>
    runLookup("dtc", "/api/v1/dtc/lookup", { code: $("#dtc-code").value }, "#dtc-result"));
  $("#tsb-run").addEventListener("click", () =>
    runLookup("tsb", "/api/v1/tsb/search", { symptom: $("#tsb-symptom").value }, "#tsb-result"));
  $("#parts-run").addEventListener("click", () =>
    runLookup("parts", "/api/v1/parts/search", { query: $("#parts-query").value }, "#parts-result"));
  $("#scan-run").addEventListener("click", () =>
    runLookup("scan", "/api/v1/obd2/scan", {}, "#scan-result"));

  $("#labor-add").addEventListener("click", () => addLine("#labor-lines",
    [{ key: "operation", label: "Operation", placeholder: "replace CMP sensor" },
     { key: "hours", label: "Hours", type: "number" }]));
  $("#part-add").addEventListener("click", () => addLine("#part-lines",
    [{ key: "description", label: "Description", placeholder: "CMP sensor" },
     { key: "unit_price", label: "Unit price", type: "number" },
     { key: "quantity", label: "Qty", type: "number" }]));
  $("#fee-add").addEventListener("click", () => addLine("#fee-lines",
    [{ key: "description", label: "Description", placeholder: "shop supplies" },
     { key: "amount", label: "Amount", type: "number" }]));
  $("#estimate-run").addEventListener("click", buildEstimate);

  loadFooterStats();
});
"""


def _tool_tab(
    tab_id: str,
    title: str,
    field_id: str,
    field_label: str,
    placeholder: str,
    button_id: str,
    result_id: str,
    *,
    extra_note: str = "",
) -> str:
    return f"""
    <div class="panel" id="panel-{tab_id}">
      <div class="card">
        <h2>{title}</h2>
        {f'<p style="color:var(--muted);font-size:13px;margin-top:-4px">{extra_note}</p>' if extra_note else ""}
        <div class="row">
          <label>{field_label}
            <textarea id="{field_id}" rows="1" placeholder="{placeholder}"></textarea>
          </label>
          <button id="{button_id}">Look up</button>
        </div>
        <div id="{result_id}"></div>
      </div>
    </div>"""


def render_app() -> str:
    """The technician UI: vehicle context, diagnose chat, per-tool lookups, estimate
    builder. Every result panel renders what the API actually returned — nothing here
    guesses at a value, a citation, or a reason a lookup came back empty.
    """
    tool_tabs = "".join(
        [
            _tool_tab(
                "torque",
                "Torque specs",
                "torque-component",
                "Component",
                "camshaft position sensor retaining bolt",
                "torque-run",
                "torque-result",
            ),
            _tool_tab(
                "labor",
                "Labor times",
                "labor-operation",
                "Operation",
                "camshaft position sensor replacement",
                "labor-run",
                "labor-result",
            ),
            _tool_tab(
                "wiring",
                "Wiring / pinouts",
                "wiring-circuit",
                "Circuit",
                "CMP sensor signal circuit",
                "wiring-run",
                "wiring-result",
            ),
            _tool_tab(
                "dtc",
                "DTC lookup",
                "dtc-code",
                "Code",
                "P0340",
                "dtc-run",
                "dtc-result",
            ),
            _tool_tab(
                "tsb",
                "TSBs / recalls",
                "tsb-symptom",
                "Symptom",
                "intermittent long crank",
                "tsb-run",
                "tsb-result",
            ),
            _tool_tab(
                "parts",
                "Parts search",
                "parts-query",
                "Part",
                "camshaft position sensor",
                "parts-run",
                "parts-result",
                extra_note="Requires a configured parts supplier — reports unconfigured otherwise.",
            ),
        ]
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aad — auto mechanic diagnostic assistant</title>
<link rel="icon" href="data:,">
<style>{_CSS}</style></head>
<body>
<header class="top">
  <h1>aad</h1>
  <span class="sub">grounded diagnostic assistant</span>
  <div class="vehicle-bar">
    <input id="vin" class="vin" placeholder="VIN" maxlength="17">
    <button id="decode-btn" class="secondary">Decode</button>
    <input id="v-year" placeholder="Year">
    <input id="v-make" placeholder="Make">
    <input id="v-model" placeholder="Model">
    <input id="v-engine" placeholder="Engine">
    <span id="vehicle-pill" class="label-pill">no vehicle scoped</span>
  </div>
  <a href="/monitor" class="secondary" style="text-decoration:none;padding:7px 10px;
     border:1px solid var(--line);border-radius:7px;font-size:13px">Grounding monitor</a>
</header>

<nav class="tabs">
  <button data-tab="diagnose" class="active">Diagnose</button>
  <button data-tab="torque">Torque</button>
  <button data-tab="labor">Labor</button>
  <button data-tab="wiring">Wiring</button>
  <button data-tab="dtc">DTC</button>
  <button data-tab="tsb">TSB / Recalls</button>
  <button data-tab="parts">Parts</button>
  <button data-tab="scan">OBD2 Scan</button>
  <button data-tab="estimate">Estimate</button>
</nav>

<main>
  <div class="panel active" id="panel-diagnose">
    <div class="card">
      <h2>Diagnose</h2>
      <div id="chat" class="chat" style="max-height:52vh;overflow-y:auto"></div>
      <div class="row" style="margin-bottom:0">
        <label style="flex:1">Question
          <textarea id="diagnose-input" rows="2"
            placeholder="What is the camshaft position sensor bolt torque?"></textarea>
        </label>
        <button id="diagnose-send">Send</button>
      </div>
    </div>
  </div>

  {tool_tabs}

  <div class="panel" id="panel-scan">
    <div class="card">
      <h2>OBD2 scan</h2>
      <p style="color:var(--muted);font-size:13px;margin-top:-4px">
        Requires a configured scan service — reports unconfigured otherwise.</p>
      <div class="row"><button id="scan-run">Run scan</button></div>
      <div id="scan-result"></div>
    </div>
  </div>

  <div class="panel" id="panel-estimate">
    <div class="card">
      <h2>Labor</h2>
      <div id="labor-lines"></div>
      <button id="labor-add" class="secondary" type="button">+ labor line</button>
    </div>
    <div class="card">
      <h2>Parts</h2>
      <div id="part-lines"></div>
      <button id="part-add" class="secondary" type="button">+ part line</button>
    </div>
    <div class="card">
      <h2>Fees</h2>
      <div id="fee-lines"></div>
      <button id="fee-add" class="secondary" type="button">+ fee line</button>
    </div>
    <div class="card">
      <button id="estimate-run">Build estimate</button>
      <div id="estimate-result"></div>
    </div>
  </div>
</main>

<footer class="app-footer">
  <span id="footer-stats">loading index status…</span> ·
  <a href="/monitor">grounding monitor</a> ·
  <a href="/healthz">health</a>
</footer>

<script>{_JS}</script>
</body></html>"""
