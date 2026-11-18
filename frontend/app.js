/* Corvo Procurement Desk: vanilla JS single page app. No build step. */
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = n => n === "" || n == null ? "–" : "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const view = $("#view");
let STATUS = {};
const S = { user: localStorage.user || "", role: localStorage.role || "requester", sid: localStorage.sid || (localStorage.sid = "s" + Math.random().toString(36).slice(2, 10)), runId: null };

function toast(m) { const t = $("#toast"); t.textContent = m; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 2600); }
async function api(path, opts = {}) {
  opts.headers = { ...(opts.headers || {}), "X-User": S.user || "anonymous", "X-Role": S.role };
  if (opts.json) { opts.body = JSON.stringify(opts.json); opts.headers["Content-Type"] = "application/json"; opts.method = opts.method || "POST"; }
  const r = await fetch("/api" + path, opts);
  const ct = r.headers.get("content-type") || "";
  const d = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(typeof d === "object" ? (typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail)) : d);
  return d;
}
const guard = fn => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message); console.error(e); } };
const routeTag = r => `<span class="tag ${/_PO$/.test(r) ? "po" : r === "NOT_PROCESSED" ? "bad" : "src"}">${esc(r.replace(/_/g, " "))}</span>`;
const stTag = s => `<span class="tag ${s === "APPROVED" ? "po" : s === "OVERRIDDEN" ? "info" : "mute"}">${s.toLowerCase()}</span>`;

const PAGES = [["chat", "Raise a request"], ["batch", "Upload batch"], ["workspace", "Buyer workspace"], ["suppliers", "Preferred suppliers"], ["contracts", "Contracts"], ["policy", "Policy"], ["setup", "Setup & audit"]];
function nav() {
  const cur = (location.hash || "#chat").slice(1).split("/")[0];
  $("#nav").innerHTML = PAGES.map(([k, l]) => `<a href="#${k}" class="${k === cur ? "on" : ""}">${l}</a>`).join("");
}
$("#user").value = S.user; $("#role").value = S.role;
$("#user").onchange = e => { S.user = localStorage.user = e.target.value.trim(); };
$("#role").onchange = e => { S.role = localStorage.role = e.target.value; route(); };

async function route() {
  nav();
  try { STATUS = await api("/status"); } catch { }
  $("#polbadge").textContent = STATUS.active_policy ? "policy v" + STATUS.active_policy : "";
  $("#banner").innerHTML = !STATUS.master_loaded ? `Master data is not loaded yet. <a href="#setup">Open Setup</a> and upload the item master, supplier master, PO history and contracts.` : "";
  const page = (location.hash || "#chat").slice(1).split("/")[0];
  ({ chat: pChat, batch: pBatch, workspace: pWorkspace, suppliers: pSuppliers, contracts: pContracts, policy: pPolicy, setup: pSetup }[page] || pChat)();
}
window.onhashchange = route; route();

/* ---------------- chat ---------------- */
async function pChat() {
  view.innerHTML = `<h1>Raise a purchase request</h1><p class="sub">Tell me what you need, for example “need 200 nitrile gloves for Chakan by next Friday”. I’ll ask for anything missing.</p>
  <div class="grid2"><div class="card chat"><div class="msgs" id="msgs" aria-live="polite"></div><div class="chips" id="chips"></div>
  <form class="composer" id="cf"><input id="ci" autocomplete="off" aria-label="Message" placeholder="What do you need?"><button class="primary">Send</button></form></div>
  <div class="card"><h2>Your recent requests</h2><div id="mine" class="scroll"></div></div></div>`;
  const hist = await api("/chat/" + S.sid).catch(() => []);
  const paint = () => { $("#msgs").innerHTML = hist.map(m => `<div class="m ${m.role === "user" ? "u" : "a"}">${esc(m.text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")}</div>`).join("") || `<div class="empty">No messages yet.</div>`; $("#msgs").scrollTop = 1e9;
    const last = [...hist].reverse().find(m => m.role === "assistant"); $("#chips").innerHTML = (last?.chips || []).map(c => `<button type="button">${esc(c)}</button>`).join("");
    $("#chips").querySelectorAll("button").forEach(b => b.onclick = () => send(b.textContent)); };
  const send = guard(async text => { if (!text.trim()) return; hist.push({ role: "user", text }); paint(); $("#ci").value = "";
    const r = await api("/chat", { json: { session_id: S.sid, message: text } }); hist.push({ role: "assistant", text: r.reply, chips: r.chips }); paint(); if (r.pr) mine(); });
  $("#cf").onsubmit = e => { e.preventDefault(); send($("#ci").value); };
  const mine = guard(async () => { const l = await api("/live"); const rows = l.requirements.slice().reverse();
    $("#mine").innerHTML = rows.length ? `<table><tr><th>PR</th><th>Item</th><th>Outcome</th><th>Status</th></tr>${rows.map(r => `<tr><td>${esc(r.lines.map(x => x.pr_no).join(", "))}</td><td>${esc(r.item_description)}</td><td>${routeTag(r.route)}</td><td>${stTag(r.status)}</td></tr>`).join("")}</table>` : `<div class="empty">Requests you raise here appear for the buyer in the workspace.</div>`; });
  paint(); mine(); $("#ci").focus();
}

/* ---------------- batch ---------------- */
async function pBatch() {
  const vers = (await api("/policies")).filter(p => p.status === "approved");
  view.innerHTML = `<h1>Upload a PR batch</h1><p class="sub">Upload a CSV in the sample format. Every line is decided under the policy in force on its requirement date.</p>
  <div class="card"><div class="row"><input type="file" id="f" accept=".csv" aria-label="PR batch CSV">
  <select id="pm" aria-label="Policy to apply"><option value="by_date">Policy in force on each requirement date</option><option value="latest">Latest approved policy for all lines</option>${vers.map(v => `<option value="v:${esc(v.version)}">Force v${esc(v.version)} (effective ${esc(v.effective_from)})</option>`).join("")}</select>
  <button class="primary" id="go">Process batch</button></div></div><div id="res"></div>`;
  $("#go").onclick = guard(async () => {
    const f = $("#f").files[0]; if (!f) return toast("Choose a CSV file first");
    const fd = new FormData(); fd.append("file", f); const pm = $("#pm").value;
    if (pm.startsWith("v:")) fd.append("policy_version", pm.slice(2)); else fd.append("policy_mode", pm);
    $("#go").disabled = true; $("#go").textContent = "Processing…";
    try { const r = await api("/process-batch", { method: "POST", body: fd }); S.runId = r.run_id; showBatch(r); } finally { $("#go").disabled = false; $("#go").textContent = "Process batch"; }
  });
  function showBatch(r) {
    const s = r.summary;
    $("#res").innerHTML = `<div class="card"><div class="row sp"><h2>Run #${r.run_id} · policy ${esc(s.policy_versions.join(", "))}</h2>
      <div class="row"><a class="btn" href="${r.export_url}">Download decisions CSV</a><a class="btn primary" href="#workspace/${r.run_id}">Review in buyer workspace</a></div></div>
      <div class="row">${[["PR lines", s.lines], ["Requirements", s.requirements], ["POs", s.pos], ["Not processed", s.not_processed]].map(([a, b]) => `<div class="stat"><b>${b}</b>${a}</div>`).join("")}</div>
      <div class="row" style="margin-top:10px">${Object.entries(s.routes).map(([k, v]) => `${routeTag(k)} ${v}`).join(" &nbsp; ")}</div></div>
      <div class="card scroll"><table><tr><th>PR</th><th>Item</th><th>Group</th><th>PO</th><th>Route</th><th>Supplier / suggested</th><th class="n">Value</th></tr>
      ${r.decisions.map(d => `<tr><td>${esc(d.pr_no)}/${esc(d.pr_line)}</td><td>${esc(d.resolved_item_code)}</td><td>${esc(d.group_id)}</td><td>${esc(d.po_id)}</td><td>${routeTag(d.route)}</td><td>${esc(d.supplier_id || d.suggested_supplier_ids)}</td><td class="n">${inr(d.group_value_inr)}</td></tr>`).join("")}</table></div>`;
  }
}

/* ---------------- buyer workspace ---------------- */
async function pWorkspace() {
  const runs = await api("/runs");
  const want = +(location.hash.split("/")[1]) || S.runId || (runs.find(r => r.kind === "batch") || runs[0] || {}).id;
  if (!runs.length) { view.innerHTML = `<h1>Buyer workspace</h1><div class="card empty">Nothing to review yet. <a href="#batch">Upload a PR batch</a> or have a requester raise one in chat.</div>`; return; }
  S.runId = want; const run = await api("/runs/" + want);
  const state = { f: "PENDING", route: "" };
  view.innerHTML = `<div class="row sp"><div><h1>Buyer workspace</h1><p class="sub" style="margin:0">Open a requirement to see why it was decided, then approve or override.</p></div>
    <select id="rs" aria-label="Run">${runs.map(r => `<option value="${r.id}" ${r.id === want ? "selected" : ""}>#${r.id} ${esc(r.label)} (${r.kind}, ${r.summary.lines ?? 0} lines)</option>`).join("")}</select></div>
    <div class="card"><div class="row sp"><div class="row" id="flt"></div><button class="primary" id="bulk">Approve all pending PO decisions</button></div></div>
    <div class="card scroll" id="tbl"></div><div id="dr"></div>`;
  $("#rs").onchange = e => { location.hash = "#workspace/" + e.target.value; };
  const routes = [...new Set(run.requirements.map(r => r.route))];
  const draw = () => {
    $("#flt").innerHTML = ["PENDING", "APPROVED", "OVERRIDDEN", "ALL"].map(s => `<button data-f="${s}" class="${state.f === s ? "primary" : ""}">${s.toLowerCase()} (${s === "ALL" ? run.requirements.length : run.requirements.filter(r => r.status === s).length})</button>`).join("") +
      `<select id="rt" aria-label="Route"><option value="">All routes</option>${routes.map(r => `<option ${state.route === r ? "selected" : ""}>${r}</option>`).join("")}</select>`;
    $("#flt").querySelectorAll("button").forEach(b => b.onclick = () => { state.f = b.dataset.f; draw(); }); $("#rt").onchange = e => { state.route = e.target.value; draw(); };
    const rows = run.requirements.filter(r => (state.f === "ALL" || r.status === state.f) && (!state.route || r.route === state.route));
    $("#tbl").innerHTML = rows.length ? `<table><tr><th>Requirement</th><th>Item</th><th>Plant</th><th>Lines</th><th class="n">Value</th><th>Route</th><th>Supplier / suggested</th><th>Status</th></tr>
      ${rows.map(r => `<tr class="click" data-g="${esc(r.group_id)}"><td>${esc(r.group_id)}${r.emergency ? ' <span class="tag bad">emergency</span>' : ""}</td><td>${esc(r.item_description)}<br><small>${esc(r.item_code || "NEW")}</small></td><td>${esc(r.plant)}</td><td>${r.lines.length}</td><td class="n">${inr(r.value)}</td><td>${routeTag(r.route)}</td><td>${esc(r.supplier_id || (r.suggested || []).join(", "))}</td><td>${stTag(r.status)}</td></tr>`).join("")}</table>` : `<div class="empty">No requirements match this filter.</div>`;
    $("#tbl").querySelectorAll("tr.click").forEach(tr => tr.onclick = () => open(run.requirements.find(r => r.group_id === tr.dataset.g)));
  };
  $("#bulk").onclick = guard(async () => {
    const todo = run.requirements.filter(r => r.status === "PENDING" && r.decision === "PO"); if (!todo.length) return toast("No pending PO decisions");
    if (!confirm(`Approve ${todo.length} PO decisions and create their POs?`)) return;
    for (const r of todo) await api(`/runs/${want}/requirements/${encodeURIComponent(r.group_id)}/approve`, { json: {} });
    toast(`${todo.length} POs created`); pWorkspace();
  });
  function open(r) {
    const pend = r.status === "PENDING", can = ["buyer", "procurement"].includes(S.role);
    const v = r.valuation || {};
    $("#dr").innerHTML = `<div class="scrim" id="sc"></div><aside class="drawer" aria-label="Requirement detail">
      <div class="row sp"><h2>${esc(r.group_id)} · ${esc(r.item_description)}</h2><button id="x">Close</button></div>
      <p>${routeTag(r.route)} ${stTag(r.status)} ${r.document_no ? `<span class="tag info">${esc(r.document_no)}</span>` : ""}</p>
      <div class="card" style="background:#f7f9fb"><b>Why:</b> ${esc(r.reason)}</div>
      ${(r.flags || []).length ? `<p class="no">⚠ ${esc(r.flags.join("; "))}</p>` : ""}
      <dl class="kv"><dt>Decision</dt><dd>${esc(r.decision)} / ${esc(r.route)}</dd><dt>Policy</dt><dd>v${esc(r.policy_version)}, rule ${esc(r.rule_id)} (${esc(r.rule_name || "")}), requirement date ${esc(r.req_date)}</dd>
      <dt>Value</dt><dd>${inr(r.value)} = ${v.qty} × ${inr(v.unit_price)} <small>(${esc(v.source)})</small></dd>
      <dt>Supplier</dt><dd>${r.decision === "PO" ? `${esc(r.supplier_id)} at ${inr(r.unit_price)}` : esc((r.suggested || []).join(", ") || "none: buyer finds suppliers")}</dd>
      ${r.po_id ? `<dt>PO</dt><dd>${esc(r.po_id)}</dd>` : ""}${r.needs_note ? `<dt>Note</dt><dd>${esc(r.needs_note)}</dd>` : ""}</dl>
      <h3>PR lines in this requirement</h3><table><tr><th>PR</th><th>Created</th><th>Need by</th><th class="n">Qty</th><th>Text</th></tr>${r.lines.map(l => `<tr><td>${esc(l.pr_no)}/${esc(l.pr_line)}</td><td>${esc(l.created_at)}</td><td>${esc(l.need_by)}</td><td class="n">${l.qty}</td><td>${esc(l.description)}<br><small>${esc(l.match_note)}</small></td></tr>`).join("")}</table>
      <h3>Rules checked, in order</h3><ul class="trace">${r.trace.map(t => `<li class="${t.matched ? "hit" : ""}"><b>${t.matched ? "✔ applied" : "✘"} ${esc(t.rule)} ${esc(t.name || "")}</b>${(t.conditions || []).map(c => `<small><span class="${c.passed ? "ok" : "no"}">${c.passed ? "✓" : "✗"}</span> ${esc(c.text)} (now: ${esc(c.actual)})</small>`).join("")}${t.note ? `<small>${esc(t.note)}</small>` : ""}</li>`).join("")}</ul>
      ${(r.contract_options || []).length ? `<h3>Contracts considered</h3><table><tr><th>Contract</th><th>Supplier</th><th class="n">Rate</th><th class="n">Room left</th><th class="n">This need</th><th>Usable</th></tr>${r.contract_options.map(o => `<tr><td>${esc(o.contract_id)} <small>${esc(o.source)}</small></td><td>${esc(o.supplier_id)}</td><td class="n">${inr(o.rate)}</td><td class="n">${inr(o.room)}</td><td class="n">${inr(o.value_at_rate)}</td><td>${o.usable ? '<span class="ok">yes</span>' : '<span class="no">' + (o.supplier_active ? "no ceiling" : "supplier not Active") + "</span>"}</td></tr>`).join("")}</table>` : ""}
      ${(r.preferred || []).length ? `<h3>Preferred suppliers for this item</h3>${prefTable(r.preferred)}` : ""}
      ${r.history.length ? `<h3>Actions</h3>${r.history.map(h => `<p><b>${esc(h.action)}</b> by ${esc(h.actor)} (${esc(h.role)}) at ${esc(h.at)}${h.comment ? `: “${esc(h.comment)}”` : ""}<br><small>${esc(JSON.stringify(h.payload.before))}${h.payload.after ? " → " + esc(JSON.stringify(h.payload.after)) : ""}</small></p>`).join("")}` : ""}
      ${pend ? `<div class="card" style="margin-top:16px"><div class="row">${can ? `<button class="primary" id="ap">${r.decision === "PO" ? "Approve and create PO" : "Approve and create " + esc(r.route.replace(/_/g, " ")) }</button><button id="ov">Override…</button>` : `<span class="sub">Switch role to Buyer to act on this.</span>`}</div><div id="ovf"></div></div>` : ""}</aside>`;
    $("#x").onclick = $("#sc").onclick = () => { $("#dr").innerHTML = ""; };
    if ($("#ap")) $("#ap").onclick = guard(async () => { const x = await api(`/runs/${want}/requirements/${encodeURIComponent(r.group_id)}/approve`, { json: {} }); toast("Created " + x.document_no); pWorkspace(); });
    if ($("#ov")) $("#ov").onclick = () => {
      $("#ovf").innerHTML = `<label>New route</label><select id="o_r">${["CONTRACT_PO", "EMERGENCY_PO", "SPOT_PO", "RFQ", "RFP", "CAPEX_SOURCING", "NEW_ITEM_SOURCING"].map(x => `<option ${x === r.route ? "selected" : ""}>${x}</option>`).join("")}</select>
      <label>Supplier (POs) or suggested suppliers separated by ; (sourcing)</label><input id="o_s" value="${esc(r.decision === "PO" ? r.supplier_id : (r.suggested || []).join(";"))}">
      <label>Unit price (POs only)</label><input id="o_p" type="number" step="any" value="${r.decision === "PO" ? r.unit_price : ""}">
      <label>Reason (required)</label><textarea id="o_c" placeholder="Why are you changing the decision?"></textarea><p><button class="danger" id="o_go">Save override</button></p>`;
      $("#o_go").onclick = guard(async () => { const rt = $("#o_r").value, isPO = /_PO$/.test(rt), s = $("#o_s").value.trim();
        const ov = { route: rt, decision: isPO ? "PO" : "SOURCING_REQUEST" }; if (isPO) { ov.supplier_id = s; ov.unit_price = +$("#o_p").value || null; } else ov.suggested = s.split(";").map(x => x.trim()).filter(Boolean);
        const x = await api(`/runs/${want}/requirements/${encodeURIComponent(r.group_id)}/override`, { json: { comment: $("#o_c").value, override: ov } }); toast("Override saved: " + x.document_no); pWorkspace(); });
    };
  }
  draw();
}
function prefTable(list) {
  return `<div class="scroll"><table><tr><th>#</th><th>Supplier</th><th class="n">Score</th><th class="n">POs</th><th>Last PO</th><th>Contract</th><th class="n">On-time</th><th class="n">Quality</th><th class="n">Ref. price</th></tr>${list.map(p => `<tr><td>${p.rank}</td><td>${esc(p.supplier_id)} ${esc(p.name)}</td><td class="n">${p.score}<br><span class="bar" style="width:${Math.round(p.score * 70)}px"></span></td><td class="n">${p.po_count}</td><td>${esc(p.last_po_date || "–")}</td><td>${esc((p.contract_ids || []).join(", ") || "no")}</td><td class="n">${p.on_time_pct}%</td><td class="n">${p.quality}</td><td class="n">${inr(p.reference_price)}<br><small>${esc(p.reference_source)}</small></td></tr>`).join("")}</table></div>`;
}

/* ---------------- preferred suppliers ---------------- */
async function pSuppliers() {
  const items = await api("/items"); const today = new Date().toISOString().slice(0, 10);
  view.innerHTML = `<h1>Preferred suppliers</h1><p class="sub">Built from PO history and contracts. The score blends how often, how recently, contract history, on-time delivery and quality; the weights come from the active policy.</p>
  <div class="card"><div class="row"><select id="it" aria-label="Item">${items.map(i => `<option value="${i.item_code}">${i.item_code} · ${esc(i.description)}</option>`).join("")}</select>
  <label style="margin:0">As of <input type="date" id="as" value="${today}"></label><button class="primary" id="go">Show ranking</button>
  <a class="btn" id="ex" href="#">Export top 3 for all items (CSV)</a></div></div><div id="out"></div>`;
  const upd = () => { $("#ex").href = "/api/preferred/export.csv?as_of=" + $("#as").value; };
  $("#as").onchange = upd; upd();
  $("#go").onclick = guard(async () => { const r = await api(`/preferred?item_code=${$("#it").value}&as_of=${$("#as").value}`);
    const w = r.weights; $("#out").innerHTML = `<div class="card"><h2>${esc(r.item.description)} as of ${r.as_of} (policy v${esc(r.policy_version)})</h2>
    <p class="sub">Score = ${Object.entries(w).map(([k, v]) => `${v} × ${k.replace("_", "-")}`).join(" + ")}. Frequency is PO count relative to the busiest supplier on the list. Recency: 1 within 90 days, 0.5 within a year, else 0. Blocked and Inactive suppliers are excluded.</p>
    ${r.list.length ? prefTable(r.list) : `<div class="empty">No supplier qualifies: no Active supplier has a PO in the window or a contract started on this date. Sourcing events top up from the category.</div>`}</div>`; });
  $("#go").click();
}

/* ---------------- contracts ---------------- */
async function pContracts() {
  const cs = await api("/contracts");
  view.innerHTML = `<h1>Rate contracts</h1><p class="sub">The register is updated monthly. Contracts signed since then exist only as PDFs: add them here and they apply from their start date, with ceiling consumption starting at zero.</p>
  <div class="card"><h2>Add a contract from its signed PDF</h2><div class="row"><input type="file" id="pf" accept=".pdf" aria-label="Contract PDF"><button class="primary" id="ex">Read the PDF</button></div><div id="form"></div></div>
  <div class="card scroll"><table><tr><th>Contract</th><th>Supplier</th><th>Valid</th><th class="n">Ceiling</th><th class="n">Consumed (at last update)</th><th>Items</th><th>Source</th></tr>${cs.map(c => `<tr><td>${esc(c.contract_id)}</td><td>${esc(c.supplier_id)} ${esc(c.supplier_name || "")}</td><td>${esc(c.start_date)} to ${esc(c.end_date)}</td><td class="n">${inr(c.value_cap)}</td><td class="n">${inr(c.consumed_value)}</td><td>${c.lines.map(l => esc(l.item_code)).join(", ")}</td><td><span class="tag ${c.source === "register" ? "mute" : "info"}">${esc(c.source)}</span></td></tr>`).join("")}</table></div>`;
  $("#ex").onclick = guard(async () => { const f = $("#pf").files[0]; if (!f) return toast("Choose the contract PDF first");
    const fd = new FormData(); fd.append("file", f); const e = await api("/contracts/extract-pdf", { method: "POST", body: fd }); form(e, f.name); });
  function form(e, name) {
    $("#form").innerHTML = `${e.warnings?.length ? `<p class="no">${esc(e.warnings.join(" "))}</p>` : `<p class="ok">Read ${e.lines.length} item lines. Check the values, then save.</p>`}
    <div class="row"><div><label>Contract no.</label><input id="c_id" value="${esc(e.contract_id || "")}"></div><div><label>Supplier ID</label><input id="c_s" value="${esc(e.supplier_id || "")}"></div>
    <div><label>Start</label><input id="c_a" type="date" value="${esc(e.start_date || "")}"></div><div><label>End</label><input id="c_b" type="date" value="${esc(e.end_date || "")}"></div>
    <div><label>Ceiling (₹)</label><input id="c_v" type="number" value="${e.value_cap || ""}"></div></div>
    <label>Item lines (item code, rate per line)</label><textarea id="c_l">${e.lines.map(l => `${l.item_code},${l.rate}`).join("\n")}</textarea><p><button class="primary" id="sv">Save contract</button></p>`;
    $("#sv").onclick = guard(async () => { const lines = $("#c_l").value.split("\n").map(x => x.trim()).filter(Boolean).map(x => { const [i, r] = x.split(/[,\s]+/); return { item_code: i, rate: +r }; });
      await api("/contracts", { json: { contract_id: $("#c_id").value.trim(), supplier_id: $("#c_s").value.trim(), start_date: $("#c_a").value, end_date: $("#c_b").value, value_cap: +$("#c_v").value, lines, source: name ? "pdf" : "manual", pdf_name: name } }); toast("Contract saved"); pContracts(); });
  }
}

/* ---------------- policy ---------------- */
async function pPolicy() {
  const ps = await api("/policies"); const active = ps.filter(p => p.status === "approved").sort((a, b) => (b.effective_from + b.version).localeCompare(a.effective_from + a.version))[0];
  const can = S.role === "procurement";
  view.innerHTML = `<h1>Procurement policy</h1><p class="sub">The decision engine reads these rules at run time. Upload a new version or describe a change; you’ll see what changes in plain language before anything is activated.</p>
  <div class="grid2"><div>
  <div class="card"><h2>Propose a change</h2>${can ? "" : `<p class="no">Switch role to “Procurement head” to propose or approve policy changes.</p>`}
  <label>Policy or amendment document (PDF, DOCX, text, or JSON)</label><input type="file" id="pf" accept=".pdf,.docx,.txt,.md,.json">
  <label>…or describe the change <small>(if you fill both, both are read together)</small></label><textarea id="pi" placeholder="e.g. From 1 Dec 2026 raise the spot PO limit to ₹3,00,000 and the emergency limit to ₹7,50,000."></textarea>
  <p class="sub">${STATUS.llm ? "AI reading is on (" + esc(STATUS.model) + ")." : "AI reading is off: the built-in reader handles limits, windows and dates; you can add other rules in the editor."}</p>
  <button class="primary" id="pr" ${can ? "" : "disabled"}>Read and preview changes</button></div><div id="draft"></div></div>
  <div class="card"><h2>Versions</h2>${ps.map(p => `<div style="margin-bottom:12px"><b>v${esc(p.version)}</b> ${p.status === "approved" ? (p === active ? '<span class="tag po">active</span>' : '<span class="tag mute">approved</span>') : `<span class="tag ${p.status === "draft" ? "info" : "bad"}">${p.status}</span>`}<br><small>effective ${esc(p.effective_from)} · ${esc(p.source_name || "")}${p.approved_by ? " · approved by " + esc(p.approved_by) + " " + esc(p.approved_at) : ""}</small>${p.status === "draft" ? ` <a href="#" data-d="${p.id}">open draft</a>` : ""}</div>`).join("")}</div></div>
  <div class="card"><h2>Rules in the active version (v${esc(active.version)}), applied in this order</h2><ol>${active.rules_text.map(t => `<li>${esc(t)}</li>`).join("")}</ol></div>`;
  view.querySelectorAll("[data-d]").forEach(a => a.onclick = guard(async e => { e.preventDefault(); draft(await api("/policies/" + a.dataset.d)); }));
  $("#pr").onclick = guard(async () => { const fd = new FormData(); const f = $("#pf").files[0]; if (f) fd.append("file", f); fd.append("instruction", $("#pi").value);
    $("#pr").disabled = true; try { draft(await api("/policies/propose", { method: "POST", body: fd })); $("#pf").value = ""; } finally { $("#pr").disabled = !can; } });
  async function draft(p) {
    const imp = await api(`/policies/${p.id}/impact`).catch(() => ({ available: false })); const sch = await api("/policy-schema");
    $("#draft").innerHTML = `<div class="card"><h2>Draft v${esc(p.version)} · effective ${esc(p.effective_from)}</h2>
    <p class="sub">Read by: ${esc(p.source_kind)}</p>
    ${p.effective_from <= active.effective_from ? `<p class="no"><b>Check the effective date.</b> It is on or before the active version's date (${esc(active.effective_from)}), so this draft would override the active policy for all existing requirements. Is this an amendment or a full policy?</p>` : ""}
    <h3>What will change</h3>${p.changes.length ? `<ul class="diff">${p.changes.map(c => `<li class="${c.type}">${esc(c.text)}</li>`).join("")}</ul>` : `<p class="no">No change detected. Use the editor below to add or edit rules.</p>`}
    ${p.notes.filter(Boolean).length ? `<h3>Notes</h3><ul>${p.notes.filter(Boolean).map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
    ${imp.available ? `<h3>Impact on the last batch (run #${imp.run_id})</h3><p>${imp.changed} of ${imp.lines} lines would be decided differently.</p>${imp.examples.length ? `<div class="scroll"><table><tr><th>PR</th><th>Was</th><th>Becomes</th></tr>${imp.examples.map(x => `<tr><td>${esc(x.pr)}</td><td>${esc(x.from)} ${esc(x.supplier_from || "")}</td><td>${esc(x.to)} ${esc(x.supplier_to || "")}</td></tr>`).join("")}</table></div>` : ""}` : ""}
    <h3>Version and effective date</h3><div class="row"><input id="dv" value="${esc(p.version)}" aria-label="Version"><input id="de" type="date" value="${esc(p.effective_from)}" aria-label="Effective date"><button id="dsv">Save</button></div>
    <h3>Add a rule (runs in the position you choose)</h3>${ruleBuilder(p, sch)}
    <details><summary>Advanced: edit the policy JSON</summary><textarea id="dj" style="min-height:240px;font-family:monospace;font-size:12px">${esc(JSON.stringify(p.doc, null, 2))}</textarea><p><button id="dsj">Save JSON</button></p></details>
    <div class="row" style="margin-top:14px"><button class="primary" id="ap">Approve and activate from ${esc(p.effective_from)}</button><button id="rj">Discard draft</button></div></div>`;
    const save = guard(async doc => { draft(await api("/policies/" + p.id, { method: "PUT", json: { doc } })); toast("Draft updated"); });
    $("#dsv").onclick = () => save({ ...p.doc, version: $("#dv").value.trim(), effective_from: $("#de").value });
    $("#dsj").onclick = () => save(JSON.parse($("#dj").value));
    $("#ap").onclick = guard(async () => { const r = await api(`/policies/${p.id}/approve`, { method: "POST" }); toast(`v${r.version} approved, effective ${r.effective_from}`); pPolicy(); });
    $("#rj").onclick = guard(async () => { await api(`/policies/${p.id}/reject`, { method: "POST" }); pPolicy(); });
    wireBuilder(p, sch, save);
    $("#draft").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}
function ruleBuilder(p, sch) {
  const facts = Object.entries(sch.facts), ops = Object.entries(sch.operators);
  const cond = i => `<div class="cond" data-c><select data-f>${facts.map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join("")}</select><select data-o>${ops.map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select><input data-v placeholder="value (e.g. 300000, CHK1, true)"></div>`;
  return `<div class="row"><input id="rb_id" placeholder="Rule id, e.g. 5.3A"><input id="rb_n" placeholder="Name"><select id="rb_b"><option value="">At the very start</option>${p.doc.rules.map(r => `<option value="${esc(r.id)}">Before rule ${esc(r.id)} ${esc(r.name || "")}</option>`).join("")}<option value="__end">At the end</option></select></div>
  <p class="sub" style="margin:8px 0 2px">When all of these are true:</p>${cond(0)}${cond(1)}<div class="cond"><button type="button" id="rb_add">+ condition</button></div>
  <div class="row"><select id="rb_d"><option value="PO">Place a PO</option><option value="SOURCING_REQUEST">Start a sourcing request</option></select><input id="rb_r" placeholder="Route name, e.g. SPOT_PO or RFQ">
  <select id="rb_m">${sch.modes.map(m => `<option>${m}</option>`).join("")}</select><input id="rb_k" type="number" value="1" style="width:70px" aria-label="rank or count"></div>
  <p><button id="rb_go">Add rule to draft</button></p>`;
}
function wireBuilder(p, sch, save) {
  $("#rb_add").onclick = () => { const d = document.createElement("div"); d.className = "cond"; d.dataset.c = ""; d.innerHTML = $("[data-c]").innerHTML; $("#rb_add").parentNode.before(d); };
  $("#rb_go").onclick = guard(async () => {
    const conds = [...view.querySelectorAll("[data-c]")].map(r => { const f = $("[data-f]", r), o = $("[data-o]", r), v = $("[data-v]", r); if (!v || !v.value.trim()) return null;
      let val = v.value.trim(); if (/^(true|false)$/i.test(val)) val = val.toLowerCase() === "true"; else if (sch.facts[f.value].type === "num") val = +val; else if (["in", "not_in"].includes(o.value)) val = val.split(/[;,]/).map(x => x.trim());
      return { fact: f.value, op: o.value, value: val }; }).filter(Boolean);
    const id = $("#rb_id").value.trim(), route = $("#rb_r").value.trim().toUpperCase(); if (!id || !route) return toast("Give the rule an id and a route name");
    const mode = $("#rb_m").value, k = +$("#rb_k").value || 1, sup = { mode }; if (mode === "preferred") sup.rank = k; if (mode === "suggest") sup.count = k;
    const rule = { id, clause: id, name: $("#rb_n").value || id, when: conds.length ? { all: conds } : {}, then: { decision: $("#rb_d").value, route, supplier: sup } };
    const rules = p.doc.rules.slice(), b = $("#rb_b").value; const at = b === "__end" ? rules.length : b ? rules.findIndex(r => r.id === b) : 0; rules.splice(at, 0, rule);
    save({ ...p.doc, rules });
  });
}

/* ---------------- setup & audit ---------------- */
async function pSetup() {
  const au = await api("/audit?limit=80");
  view.innerHTML = `<h1>Setup &amp; audit</h1><div class="grid2"><div class="card"><h2>Master data</h2><p class="sub">Loaded: ${Object.entries(STATUS.counts || {}).map(([k, v]) => `${v} ${k.replace("_", " ")}`).join(", ")}.</p>
  <p>Upload the kit’s CSVs (item_master, supplier_master, plants, po_history, contract_register, contract_lines) or one .zip containing them.</p>
  <input type="file" id="mf" multiple accept=".csv,.zip" aria-label="Master data files"><p><button class="primary" id="mu">Load master data</button></p></div>
  <div class="card"><h2>Environment</h2><p>AI reading of policies and chat: <b>${STATUS.llm ? "on (" + esc(STATUS.model) + ")" : "off"}</b></p><p>Active policy: <b>v${esc(STATUS.active_policy)}</b></p></div></div>
  <div class="card scroll"><h2>Audit log</h2><table><tr><th>When</th><th>Who</th><th>Role</th><th>Event</th><th>Detail</th></tr>${au.map(a => `<tr><td>${esc(a.at)}</td><td>${esc(a.actor)}</td><td>${esc(a.role)}</td><td>${esc(a.event)}</td><td><small>${esc(a.detail)}</small></td></tr>`).join("")}</table></div>`;
  $("#mu").onclick = guard(async () => { const fs = $("#mf").files; if (!fs.length) return toast("Choose files first"); const fd = new FormData(); for (const f of fs) fd.append("files", f);
    const r = await api("/master-data", { method: "POST", body: fd }); toast("Loaded: " + JSON.stringify(r)); route(); });
}
