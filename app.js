// Ragnarisk dashboard. Every page reads from the local API (/api/*), which is backed by one SQLite database.
const content = document.querySelector("#page-content");
const overviewMarkup = content.innerHTML;
const breadcrumb = document.querySelector("#breadcrumb-current");
const modal = document.querySelector("#modal-backdrop");
const toast = document.querySelector("#toast");
const sheet = document.querySelector("#doc-sheet");
let toastTimeout;
let activeQuestion = sessionStorage.getItem("ragnarisk-active-question") || document.querySelector("#investigation-query").value;

const state = { caseId: sessionStorage.getItem("ragnarisk-case") || "", cases: [], running: null, canRun: false, live: [], lastSeq: -1, timer: null, feed: [], feedTimer: null, run: null };

const pageTitles = {
  overview: "Overview",
  investigations: "Investigations",
  evidence: "Evidence explorer",
  council: "Agent council",
  briefing: "Executive briefing",
};

// ---- helpers ----------------------------------------------------------------------
function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => toast.classList.remove("show"), 3600);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
}

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, { cache: "no-store", headers: { "Content-Type": "application/json" }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error || `Request failed (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return body;
}

const STATUS = { COMPLETE: ["complete", "Complete"], INCOMPLETE: ["incomplete", "Provisional"], OPEN: ["open", "Running"], REJECTED: ["rejected", "Event not verified"], ABORTED: ["rejected", "Aborted"] };
function statusBadge(status, running) {
  const [key, label] = running ? ["open", "Running"] : STATUS[status] || ["pending", status || "Not started"];
  return `<span class="status-badge status-${key}"><span></span> ${escapeHtml(label)}</span>`;
}
const fmtDate = (iso) => (iso ? new Date(iso + "T00:00:00Z").toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" }) : "—");
const fmtStamp = (seconds) => (seconds ? new Date(seconds * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");
const pad2 = (n) => String(n).padStart(2, "0");
const skeleton = () => '<div class="skeleton" aria-busy="true"><i></i><i></i><i></i><i></i></div>';
const errorBox = (title, message) => `<div class="state-box"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span><button class="button button-quiet" data-action="retry">Try again</button></div>`;
const caseTitle = (c) => (c.question || "").replace(/^As of [^,]+,\s*/i, "").replace(/^how exposed are /i, "Exposure of ").slice(0, 70);

async function refreshCases() {
  const data = await api("/cases");
  state.cases = data.cases;
  state.running = data.running;
  if (!state.caseId || !state.cases.some((c) => c.case_id === state.caseId)) state.caseId = (state.cases.find((c) => c.rulings > 0) || state.cases[0])?.case_id || "";
  return state.cases;
}

async function loadCase(id, force = false) {
  if (!force && state.run?.id === id) return state.run;
  const data = await api(`/cases/${encodeURIComponent(id)}`);
  state.run = { id, case: data };
  return state.run;
}

// ---- navigation -------------------------------------------------------------------
function navigate(page) {
  if (!pageTitles[page]) return;
  window.location.hash = page;
  renderPage(page);
}

function renderPage(page) {
  if (!Object.hasOwn(pageTitles, page)) return;
  (state.charts || []).forEach((c) => c.destroy());
  state.charts = [];
  breadcrumb.textContent = pageTitles[page];
  document.querySelectorAll(".nav-link[data-page]").forEach((link) => link.classList.toggle("active", link.dataset.page === page));
  if (page === "overview") {
    content.innerHTML = overviewMarkup;
    const query = document.querySelector("#investigation-query");
    if (query) query.value = activeQuestion;
    hydrateOverview();
    return;
  }
  ({ investigations: renderInvestigations, evidence: renderEvidence, council: renderCouncil, briefing: renderBriefing })[page]();
}

const refreshCurrent = () => renderPage(window.location.hash.slice(1) || "overview");

// ---- overview ---------------------------------------------------------------------
async function hydrateOverview() {
  try {
    const [summary] = await Promise.all([api("/summary"), refreshCases()]);
    state.canRun = summary.can_run;
    const latest = state.cases.find((c) => c.case_id === state.caseId) || state.cases[0];
    const set = (id, html) => { const el = document.querySelector(id); if (el) el.innerHTML = html; };
    const corpus = summary.corpus;
    set("#m-cases", `${pad2(state.cases.length)} <span class="metric-trend">in workspace</span>`);
    set("#m-cases-foot", latest ? escapeHtml(caseTitle(latest)) : "No investigation yet");
    document.querySelector("#m-cases-bar")?.style.setProperty("width", state.cases.length ? "100%" : "0%");
    set("#document-count", `${corpus.documents} <span class="metric-trend">documents</span>`);
    set("#m-docs-foot", `${corpus.trade_rows_ok} trade rows · ${corpus.quarantined} quarantined · ${corpus.duplicates} duplicates`);
    const runs = latest?.runs ?? 0;
    set("#m-runs", `${runs} <span class="metric-trend">/ 2 minimum</span>`);
    set("#m-runs-foot", latest ? (summary.running === latest.case_id ? "Trial running now" : STATUS[latest.status]?.[1] || latest.status) : "Awaiting first trial");
    document.querySelector("#m-runs-bar")?.style.setProperty("width", `${Math.min(runs / 2, 1) * 100}%`);
    set("#m-claims", `${latest?.rulings ?? "—"} <span class="metric-trend">claims ruled</span>`);
    set("#m-claims-foot", latest ? `${latest.claims} claims submitted` : "Odin's review pending");
    document.querySelector("#m-claims-bar")?.style.setProperty("width", latest?.claims ? `${(latest.rulings / latest.claims) * 100}%` : "0%");
    if (latest) {
      const detail = (await loadCase(latest.case_id)).case;
      const stages = [detail.verified !== null, detail.pool.length > 0, Object.keys(detail.claims).length > 0, Object.keys(detail.rulings).length > 0, !!detail.briefing?.executive_summary && !/fallback/i.test(detail.briefing.title || "")];
      const states = document.querySelectorAll(".workflow-step .step-state");
      document.querySelectorAll(".workflow-step").forEach((step, i) => {
        step.classList.toggle("active", !stages[i] && stages.slice(0, i).every(Boolean));
        if (states[i]) states[i].textContent = stages[i] ? "DONE" : "PENDING";
      });
      set("#pipeline-pill", `<span class="status-dot cyan-dot"></span> ${summary.running === latest.case_id ? "TRIAL RUNNING" : (STATUS[latest.status]?.[1] || latest.status).toUpperCase()}`);
      set("#workflow-note-text", detail.status === "COMPLETE" ? "The trial is complete. Odin judged the evidence sufficient." : escapeHtml(friendlyReason(detail.status_reasons)));
      set("#case-title", escapeHtml(detail.event_summary && detail.event_summary.length <= 60 ? detail.event_summary : caseTitle(latest)));
      set("#case-code", escapeHtml(detail.event_id || "—"));
      set("#case-status", statusBadge(latest.status, summary.running === latest.case_id));
      set("#case-asof", `AS OF ${fmtDate(latest.as_of).toUpperCase()}`);
      set("#case-question", escapeHtml(latest.question));
    }
    set("#recent-count", pad2(state.cases.length));
    document.querySelectorAll(".nav-count").forEach((el) => { el.textContent = pad2(state.cases.length); });
    set("#recent-rows", state.cases.length ? state.cases.slice(0, 5).map(caseRow).join("") : '<tr><td colspan="5" class="loading-row">No investigations yet. Ask a question above.</td></tr>');
    set("#recent-foot", `Showing <strong>${Math.min(state.cases.length, 5)}</strong> of ${state.cases.length} investigations`);
    loadFeed();
    renderTradeCard(document.querySelector("#trade-card"));
  } catch (error) {
    const el = document.querySelector("#workflow-note-text");
    if (el) el.textContent = `Could not reach the server (${error.message}). Start it with: python -m nine_realms.server`;
  }
}

function caseRow(c) {
  const focus = [...new Set((c.scope || []).map(([country]) => country))].map((x) => `<span class="focus-tag">${escapeHtml(x)}</span>`).join("");
  const sectors = [...new Set((c.scope || []).map(([, sector]) => sector))].join(" · ");
  return `<tr class="clickable" data-action="open-case-id" data-case="${escapeHtml(c.case_id)}"><td><div class="table-case-title"><span class="table-case-icon">↗</span><span><strong>${escapeHtml(caseTitle(c))}</strong><small>${escapeHtml(c.event_id || "—")} · ${c.runs} run${c.runs === 1 ? "" : "s"} · ${c.claims} claims</small></span></div></td><td>${focus}<small class="focus-detail">${escapeHtml(sectors)}</small></td><td>${statusBadge(c.status, state.running === c.case_id)}</td><td class="date-cell">${fmtStamp(c.updated_at)}</td><td><button class="row-arrow" data-action="open-case-id" data-case="${escapeHtml(c.case_id)}" aria-label="Open investigation">↗</button></td></tr>`;
}

function friendlyReason(reasons = []) {
  const text = reasons.join(" ");
  if (/usage limit|rate limit|max_tokens|max_llm_calls|case_timeout/i.test(text)) return "A usage or time limit was reached before the second trial run finished.";
  if (/unchallenged/i.test(text)) return "One side went unchallenged.";
  if (/new evidence/i.test(text)) return "A second run could not find new evidence.";
  return text ? text.replace(/\{.*$/s, "").slice(0, 140).trim() : "Some steps did not finish.";
}

// ---- live feed --------------------------------------------------------------------
async function loadFeed() {
  try {
    state.feed = (await api("/feed?limit=8")).items;
    renderFeed(state.feed.length);
  } catch { /* feed is optional */ }
}

function renderFeed(count) {
  const list = document.querySelector("#feed-list");
  if (!list) return;
  list.innerHTML = state.feed.slice(0, count).reverse().map((item) => `<li class="feed-item"><span class="feed-date">${escapeHtml(item.date)}<br>${escapeHtml(item.time || "")}</span><span><strong>${escapeHtml(item.headline)}</strong><small>${escapeHtml(item.outlet)} · Tier ${item.tier} · ${escapeHtml(item.country)} · ${escapeHtml(item.sector)}${item.flags.includes("RETRACTED") ? " · RETRACTED" : ""}</small></span><button class="row-arrow" data-action="open-doc" data-doc="${escapeHtml(item.doc_id)}" aria-label="Open ${escapeHtml(item.doc_id)}">↗</button></li>`).join("") || '<li class="feed-empty">No items.</li>';
}

function replayFeed() {
  clearInterval(state.feedTimer);
  let shown = 0;
  renderFeed(0);
  state.feedTimer = setInterval(() => {
    renderFeed(++shown);
    if (shown >= state.feed.length) clearInterval(state.feedTimer);
  }, 650);
}

// ---- investigations ---------------------------------------------------------------
async function renderInvestigations() {
  content.innerHTML = `
    <section class="page-heading">
      <div><div class="eyebrow"><span class="live-dot"></span> WORKSPACE <span class="eyebrow-divider">/</span> CASE REGISTER</div><h1>Investigations</h1><p class="page-subtitle">Every trial is saved with its claims, rulings and event log.</p></div>
      <div class="heading-actions"><button class="button button-primary" data-action="new-investigation">＋ New investigation</button></div>
    </section>
    <section class="panel register-panel">
      <div class="table-scroll"><table class="investigations-table"><thead><tr><th>INVESTIGATION</th><th>COUNTRIES</th><th>STATUS</th><th>UPDATED</th><th></th></tr></thead><tbody id="case-rows"><tr><td colspan="5" class="loading-row">Loading…</td></tr></tbody></table></div>
      <div class="table-footer" id="case-foot"><span>Loading…</span></div>
    </section>`;
  try {
    await refreshCases();
    if (!document.querySelector("#case-rows")) return; // the user navigated away while cases loaded
    document.querySelector("#case-rows").innerHTML = state.cases.length ? state.cases.map(caseRow).join("") : '<tr><td colspan="5" class="loading-row">No investigations yet.</td></tr>';
    document.querySelector("#case-foot").innerHTML = `<span>${state.cases.length} investigation${state.cases.length === 1 ? "" : "s"}</span><span>Stored in the local database</span>`;
  } catch (error) {
    document.querySelector("#case-rows").innerHTML = `<tr><td colspan="5" class="loading-row error-row">${escapeHtml(error.message)} <button class="button button-quiet" data-action="retry">Try again</button></td></tr>`;
  }
}

// ---- evidence explorer ------------------------------------------------------------
let docTimer;
function renderEvidence() {
  content.innerHTML = `
    <section class="page-heading">
      <div><div class="eyebrow"><span class="live-dot"></span> INTELLIGENCE <span class="eyebrow-divider">/</span> SOURCE REGISTER</div><h1>Evidence explorer</h1><p class="page-subtitle">Hybrid search: exact words plus meaning. Flags show retractions, duplicates and repaired files.</p></div>
      <div class="heading-actions"><span class="asof-pill"><span class="calendar-icon">▤</span> <strong id="evidence-count">—</strong> RECORDS</span></div>
    </section>
    <section class="panel chart-card" id="doc-timeline"></section>
    <section class="panel evidence-panel">
      <div class="evidence-toolbar"><label class="register-search"><span>⌕</span><input id="evidence-search" type="search" placeholder="Search by words or meaning (e.g. why did grain shipments fall)…" aria-label="Search corpus"></label>
        <select id="source-filter" aria-label="Source group"><option value="">All groups</option><option value="ATL">ATL · Atlas</option><option value="ORV">ORV · Regional</option><option value="WLD">WLD · Newswire</option></select>
        <select id="tier-filter" aria-label="Reliability tier"><option value="">All tiers</option><option value="1">Tier 1</option><option value="2">Tier 2</option><option value="3">Tier 3</option><option value="4">Tier 4</option></select>
        <select id="country-filter" aria-label="Country"><option value="">All countries</option></select></div>
      <div class="table-scroll"><table class="investigations-table evidence-table"><thead><tr><th>DOCUMENT ID</th><th>TITLE</th><th>SOURCE</th><th>COUNTRY</th><th>SECTOR</th><th>TIER</th><th>DATE</th></tr></thead><tbody id="evidence-rows"><tr><td colspan="7" class="loading-row">Loading…</td></tr></tbody></table></div>
      <div class="table-footer"><span id="evidence-result-count">Loading…</span><span>Click a row to read the source</span></div>
    </section>`;
  loadDocs(true);
  renderDocTimeline(document.querySelector("#doc-timeline"));
}

async function loadDocs(first = false) {
  const value = (id) => document.querySelector(id)?.value ?? "";
  const params = new URLSearchParams({ limit: "100" });
  if (state.month) { params.set("date_from", `${state.month}-01`); params.set("date_to", `${state.month}-31`); }
  [["text", "#evidence-search"], ["prefix", "#source-filter"], ["tier", "#tier-filter"], ["country", "#country-filter"]].forEach(([key, id]) => { if (value(id)) params.set(key, value(id)); });
  try {
    const data = await api(`/documents?${params}`);
    if (first) {
      const all = (await api("/documents?limit=500")).documents;
      document.querySelector("#country-filter").innerHTML += [...new Set(all.map((d) => d.country).filter(Boolean))].sort().map((c) => `<option>${escapeHtml(c)}</option>`).join("");
      document.querySelector("#evidence-count").textContent = all.length;
    }
    const rows = document.querySelector("#evidence-rows");
    if (!rows) return;
    rows.innerHTML = data.documents.length ? data.documents.map((d) => `
      <tr class="clickable" data-action="open-doc" data-doc="${escapeHtml(d.doc_id)}">
        <td class="document-id">${escapeHtml(d.doc_id)}</td>
        <td><strong class="evidence-title">${escapeHtml(d.title)}</strong><small class="evidence-format">${escapeHtml(d.format)} ${d.flags.map((f) => `<span class="flag-chip">${escapeHtml(f.replace(/_/g, " ").toLowerCase())}</span>`).join("")}${d.after_as_of ? '<span class="flag-chip">after as-of</span>' : ""}${(d.matched_by || []).map((m) => `<span class="match-chip ${m === "semantic" ? "sem" : ""}" title="${m === "semantic" ? "Matched by meaning" : "Matched by words"}">${m === "semantic" ? "meaning" : "words"}</span>`).join("")}</small></td>
        <td>${escapeHtml(d.outlet)}</td><td>${escapeHtml(d.country || "—")}</td><td><span class="focus-tag">${escapeHtml(d.sector || "general")}</span></td>
        <td><span class="reliability-tag">TIER ${d.tier}</span></td><td class="date-cell">${escapeHtml(d.date)}</td></tr>`).join("") : '<tr><td colspan="7" class="loading-row">No records match these filters.</td></tr>';
    document.querySelector("#evidence-result-count").textContent = `${data.documents.length} of ${data.total} matching records${Object.keys(data.quarantined).length ? ` · ${Object.keys(data.quarantined).length} quarantined: ${Object.keys(data.quarantined).join(", ")}` : ""}`;
  } catch (error) {
    const rows = document.querySelector("#evidence-rows");
    if (rows) rows.innerHTML = `<tr><td colspan="7" class="loading-row error-row">${escapeHtml(error.message)}</td></tr>`;
  }
}

// ---- source sheet (click-through) --------------------------------------------------
async function openDoc(id, locator = "", quote = "") {
  sheet.hidden = false;
  document.querySelector("#sheet-title").textContent = id;
  const body = document.querySelector("#sheet-body");
  body.innerHTML = '<p class="brief-loading">Loading…</p>';
  try {
    const d = await api(`/documents/${encodeURIComponent(id)}`);
    const anchor = (/P(\d+)/.exec(locator) || [])[1];
    const needle = quote.replace(/\[P\d+\]\s*/g, "").toLowerCase().slice(0, 40);
    const paras = d.paragraphs.map((p, i) => {
      const hit = (anchor && p.startsWith(`[P${anchor}]`)) || (needle.length > 12 && p.toLowerCase().includes(needle));
      return `<p class="para${hit ? " hit" : ""}">${escapeHtml(p)}</p>`;
    }).join("");
    const rows = d.trade_rows.length ? `<table class="mini-table"><thead><tr><th>ROW</th><th>PERIOD</th><th>VOLUME (t)</th><th>VALUE ($M)</th><th></th></tr></thead><tbody>${d.trade_rows.map((r) => `<tr class="${anchor && String(r.row) === anchor ? "hit" : ""}"><td>${r.row}</td><td>${r.year}-${pad2(r.month)}</td><td>${r.volume_t?.toLocaleString() ?? "—"}</td><td>${r.value_usd_m ?? "—"}</td><td>${r.quality === "ok" ? "" : escapeHtml(r.quality)}</td></tr>`).join("")}</tbody></table>` : "";
    body.innerHTML = `<h2>${escapeHtml(d.title)}</h2><div class="sheet-meta"><span class="reliability-tag">TIER ${d.tier}</span><span>${escapeHtml(d.outlet)}</span><span>${escapeHtml(d.date)}</span><span>${escapeHtml(d.format)}</span>${d.flags.map((f) => `<span class="flag-chip">${escapeHtml(f.replace(/_/g, " ").toLowerCase())}</span>`).join("")}${d.after_as_of ? '<span class="flag-chip">after as-of</span>' : ""}</div>${d.notes ? `<p class="sheet-note">${escapeHtml(d.notes)}</p>` : ""}${paras}${rows ? `<h3>Parsed trade rows</h3>${rows}` : ""}`;
    body.querySelector(".hit")?.scrollIntoView({ block: "center" });
  } catch (error) {
    body.innerHTML = `<p class="error-row">${escapeHtml(error.message)}</p>`;
  }
}

// ---- agent council / trial view ---------------------------------------------------
function describeEvent(e) {
  const who = (e.agent || "").charAt(0) + (e.agent || "").slice(1).toLowerCase();
  switch (e.type) {
    case "case_started": return "Case opened. Heimdall is gathering evidence.";
    case "case_opened": return `Heimdall ${e.verified ? "verified the event" : "could not verify the event"}.`;
    case "tool_call": return e.tool === "search_documents" ? `${who} searched: “${e.args?.query ?? ""}”` : `${who} read ${e.args?.doc_id || e.args?.table_id || e.tool}`;
    case "claim_submitted": return `${who} submitted a claim: ${(e.claim?.text || "").slice(0, 90)}`;
    case "attack_made": return `${who} attacked ${e.attack?.target_claim_id}.`;
    case "response_made": return `${who} ${e.action === "CONCEDE" ? "conceded" : "defended"} against ${e.attack_id}.`;
    case "ruling": return `Odin ruled ${e.claim_id}: ${e.status}.`;
    case "sufficiency": return e.sufficient ? "Odin judged the evidence sufficient." : "Odin sent the case back for more evidence.";
    case "remand": return "Second trial run is starting.";
    case "research_done": return `Heimdall found ${e.new_doc_ids?.length ?? 0} new document(s).`;
    case "injection_flagged": return "A document contained an instruction aimed at the AI. It was ignored.";
    case "case_finished": return `Trial finished: ${e.status}.`;
    default: return "";
  }
}

function debateLine(d) {
  if (!d || !d.attacks) return "";
  const bits = [`${d.attacks} attack${d.attacks === 1 ? "" : "s"}`, `${d.specific_attack_pct}% specific`, `${d.evidence_backed_pct}% evidence-backed`, `${d.defended} defended`, `${d.revised} revised`, `${d.conceded} conceded`];
  const warn = d.mass_concession ? '<span class="rpill r-rejected" title="Nearly every attacked claim was conceded: the debate may have agreed instead of tested">debate collapsed</span>' : d.tier12_facts_conceded?.length ? '<span class="rpill r-contested" title="A Tier 1-2 sourced fact was conceded">fact conceded</span>' : "";
  return `<p class="debate-line"><b>Debate</b> ${bits.join(" · ")} · concession rate ${Math.round(d.concession_rate * 100)}% ${warn}</p>`;
}

const rulingPill = (status) => `<span class="rpill r-${status}">${escapeHtml(status)}</span>`;

function claimCard(run, claim) {
  const ruling = run.rulings[claim.id];
  const status = ruling?.status || (claim.status === "conceded" ? "conceded" : claim.status === "amended" ? "revised" : "unruled");
  const est = claim.estimate ? `<span class="claim-est">${escapeHtml(claim.estimate.country)} · ${escapeHtml(claim.estimate.sector)}: <b>${fmtRange([claim.estimate.low, claim.estimate.high])}</b></span>` : "";
  const chips = [...new Map(claim.evidence.map((e) => [e.doc_id, e])).values()].slice(0, 4).map((e) => `<button class="chip-btn" data-action="open-doc" data-doc="${escapeHtml(e.doc_id)}" data-locator="${escapeHtml(e.locator)}" data-quote="${escapeHtml(e.quote)}">${escapeHtml(e.doc_id)}</button>`).join("");
  return `<article class="claim" data-status="${status}"><header><span class="claim-id">${escapeHtml(claim.id)}</span><span class="badge">${escapeHtml(claim.kind)}</span>${rulingPill(status)}</header><p class="clamp">${escapeHtml(claim.text)}</p>${claim.text.length > 260 ? '<button class="more-btn" data-action="toggle-clamp">Show more</button>' : ""}${est}<div class="cites">${chips}</div>${ruling?.rationale ? `<details class="why"><summary>Odin's reasoning</summary><p>${escapeHtml(ruling.rationale)}</p></details>` : ""}</article>`;
}

async function renderCouncil() {
  if (!state.caseId) {
    try { await refreshCases(); } catch { /* handled below */ }
  }
  if (!state.caseId) return councilEmpty();
  content.innerHTML = skeleton();
  try {
    const run = (await loadCase(state.caseId, true)).case;
    const claims = Object.values(run.claims);
    const col = (side) => claims.filter((c) => c.side === side).map((c) => claimCard(run, c)).join("") || '<p class="rating-note">No claims from this side.</p>';
    const running = state.running === state.caseId;
    const live = state.live.slice(-8).map((t) => `<li>${escapeHtml(t)}</li>`).join("");
    const attacks = Object.values(run.attacks).map((a) => {
      const r = run.responses[a.id];
      const target = run.claims[a.target_claim_id];
      return `<div class="xexam"><div><span class="who who-${a.by.toLowerCase()}">${escapeHtml(a.by)}</span> attacked <b>${escapeHtml(a.target_claim_id)}</b> <span class="badge">${escapeHtml(a.kind)}</span><p>${escapeHtml(a.text)}</p></div><div>${r ? `<span class="rpill r-${r.action === "CONCEDE" ? "conceded" : r.action === "REVISE" ? "revised" : "established"}">${r.action === "CONCEDE" ? "conceded" : r.action === "REVISE" ? "revised" : "defended"}</span><p>${escapeHtml(r.text)}${r.note ? ` <i>(${escapeHtml(r.note)})</i>` : ""}</p>` : '<span class="rpill r-unruled">unanswered</span>'}${target ? "" : ""}</div></div>`;
    }).join("");
    const flags = (run.red_flags || []).slice(0, 10).map((f) => `<span class="flag-chip" title="${escapeHtml(f.note || "")}">${escapeHtml(f.doc_id)} · ${escapeHtml(f.type)}</span>`).join("");
    const runs = run.runs.map((r) => `<div class="run-row"><b>Run ${r.run}</b><span>${r.claims.length} claims · ${r.attacks.length} attacks · ${r.rulings.length} rulings${r.new_doc_ids.length ? ` · ${r.new_doc_ids.length} new document(s)` : ""}</span><span class="rpill r-${r.sufficient ? "established" : r.sufficient === false ? "contested" : "unruled"}">${r.sufficient ? "sufficient" : r.sufficient === false ? "sent back" : "not judged"}</span>${r.deficiencies.length ? `<ul>${r.deficiencies.map((d) => `<li>${escapeHtml(d)}</li>`).join("")}</ul>` : ""}${r.failures.length ? `<p class="rating-note">${escapeHtml(friendlyReason(r.failures))}</p>` : ""}${debateLine(r.debate)}</div>`).join("");
    content.innerHTML = `
      <section class="page-heading">
        <div><div class="eyebrow"><span class="live-dot"></span> INTELLIGENCE <span class="eyebrow-divider">/</span> ADVERSARIAL REVIEW</div><h1>Agent council</h1><p class="page-subtitle">${escapeHtml(run.question)}</p></div>
        <div class="heading-actions">${statusBadge(run.status, running)}<button class="button button-primary" data-action="new-investigation">＋ New investigation</button></div>
      </section>
      <nav class="council-jump" aria-label="Sections"><a href="#council" data-jump="verify">Verification</a><a href="#council" data-jump="trial">Thor vs Loki</a><a href="#council" data-jump="xexam">Cross-examination</a><a href="#council" data-jump="rulings">Rulings</a></nav>
      ${running ? `<section class="live-strip panel"><span class="live-dot"></span><ul>${live || "<li>Starting…</li>"}</ul></section>` : ""}
      <section class="panel council-verify" id="j-verify"><div><span class="agent-avatar heimdall">H</span><b>Heimdall</b> · ${run.verified === null ? "verifying…" : run.verified ? "event verified" : "event NOT verified"} <span class="badge">${escapeHtml(run.event_id || "—")}</span></div><p class="clamp">${escapeHtml(run.verdict_reason || "")}</p>${(run.verdict_reason || "").length > 260 ? '<button class="more-btn" data-action="toggle-clamp">Show more</button>' : ""}<div class="cites">${flags}</div></section>
      ${claims.length ? statusBars(run) : ""}
      <section class="trial-cols" id="j-trial"><div><h2><span class="agent-avatar thor">T</span> Thor <small>argues high exposure</small></h2>${col("THOR")}</div>
        <div><h2><span class="agent-avatar loki">L</span> Loki <small>argues limited exposure</small></h2>${col("LOKI")}</div></section>
      ${attacks ? `<section class="brief-section" id="j-xexam"><h2>Cross-examination <em>${Object.keys(run.attacks).length}</em></h2><p class="hint">Each side attacks the other's claims. A defence needs new evidence or it is a concession.</p>${attacks}</section>` : ""}
      <section class="brief-section" id="j-rulings"><h2><span class="agent-avatar odin">O</span> Odin's rulings by run</h2>${runs || '<p class="rating-note">No run has finished yet.</p>'}<button class="button button-quiet" data-page="briefing" style="margin-top:12px">Open the briefing →</button></section>`;
  } catch (error) {
    content.innerHTML = errorBox("Could not load the trial", error.message);
  }
}

function councilEmpty() {
  content.innerHTML = `<section class="page-heading"><div><div class="eyebrow"><span class="live-dot"></span> INTELLIGENCE <span class="eyebrow-divider">/</span> ADVERSARIAL REVIEW</div><h1>Agent council</h1><p class="page-subtitle">Heimdall verifies, Thor and Loki argue, Odin rules.</p></div></section>
    <section class="briefing-empty panel"><div class="empty-seal">◇</div><h2>No trial yet.</h2><p>Ask a question to open a case. Claims, cross-examination and rulings appear here.</p><button class="button button-primary" data-action="new-investigation">Start an investigation <span>→</span></button></section>`;
}

// ---- running a trial --------------------------------------------------------------
async function submitInvestigation(question) {
  if (!question.trim()) return showToast("Enter an investigation question to continue.");
  closeDialog();
  activeQuestion = question.trim();
  sessionStorage.setItem("ragnarisk-active-question", activeQuestion);
  try {
    const { case_id: id } = await api("/cases", { method: "POST", body: JSON.stringify({ question: activeQuestion }) });
    state.caseId = id;
    state.run = null;
    state.live = [];
    sessionStorage.setItem("ragnarisk-case", id);
    showToast("Trial started. Watch it unfold here.");
    startPolling(id);
    navigate("council");
  } catch (error) {
    showToast(error.status === 409 ? "A trial is already running. Wait for it to finish." : error.message);
  }
}

function startPolling(id) {
  clearTimeout(state.timer);
  state.lastSeq = -1;
  state.running = id;
  const tick = async () => {
    try {
      const data = await api(`/cases/${encodeURIComponent(id)}/events?since=${state.lastSeq}&limit=300&types=case_started,case_opened,tool_call,claim_submitted,attack_made,response_made,ruling,sufficiency,remand,research_done,injection_flagged,case_finished`);
      if (data.events.length) {
        state.lastSeq = data.events[data.events.length - 1].seq;
        state.live.push(...data.events.map(describeEvent).filter(Boolean));
        state.live = state.live.slice(-30);
        if (["council", "briefing", "overview"].includes(window.location.hash.slice(1) || "overview")) { state.run = null; refreshCurrent(); }
      }
      if (!data.is_running) {
        state.running = null;
        state.run = null;
        showToast(`Trial finished: ${STATUS[data.status]?.[1] || data.status}.`);
        refreshCurrent();
        return;
      }
    } catch { /* transient: keep polling */ }
    state.timer = setTimeout(tick, 2500);
  };
  tick();
}

// ---- exposure rating & briefing ---------------------------------------------------
const RATING_BANDS = [
  { key: "low", label: "Low", max: 10 },
  { key: "moderate", label: "Moderate", max: 30 },
  { key: "high", label: "High", max: 55 },
  { key: "severe", label: "Severe", max: Infinity },
];
const magnitude = (range) => [Math.min(Math.abs(range[0]), Math.abs(range[1])), Math.max(Math.abs(range[0]), Math.abs(range[1]))];
const round1 = (value) => Math.round(value * 10) / 10;
const fmtRange = (range) => { if (!range) return "—"; const [a, b] = magnitude(range); return `${round1(a)}% to ${round1(b)}%`; };

function exposureRows(run) {
  const claims = Object.values(run.claims || {});
  const dead = (claim) => claim.status === "conceded" || ["conceded", "rejected"].includes(run.rulings?.[claim.id]?.status);
  const hull = (side, country, sector) => {
    const estimates = claims.filter((claim) => claim.side === side && !dead(claim) && claim.estimate?.country === country && claim.estimate?.sector === sector).map((claim) => claim.estimate);
    return estimates.length ? [Math.min(...estimates.map((e) => e.low)), Math.max(...estimates.map((e) => e.high))] : null;
  };
  return (run.scope || []).map(([country, sector]) => {
    const odin = (run.ranges || []).find((range) => range.country === country && range.sector === sector);
    return { country, sector, thor: hull("THOR", country, sector), loki: hull("LOKI", country, sector), odin: odin && odin.low != null ? [odin.low, odin.high] : null };
  });
}

function ratingCard(row, provisional, pending) {
  if (!row.odin) {
    return `<article class="rating-card rating-none"><header><h3>${escapeHtml(row.country)} · ${escapeHtml(row.sector)}<small>Import volume</small></h3><span class="rating-pill"><i></i>${pending ? "Pending" : "No data"}</span></header><div class="rating-range">—</div><p class="rating-note">${pending ? "Waiting for Odin's ruling. This card fills in when the trial finishes." : "Odin found no usable evidence for this flow in Runemark, so no range is given rather than a guess."}</p></article>`;
  }
  const [lo, hi] = magnitude(row.odin);
  const band = RATING_BANDS.find((b) => (lo + hi) / 2 < b.max);
  const width = hi - lo;
  const widthLabel = width <= 8 ? "Narrow" : width <= 20 ? "Moderate" : "Wide";
  const pos = (range) => { const [a, b] = magnitude(range); return `left:${Math.min(a, 100)}%;width:${Math.max(Math.min(b, 100) - Math.min(a, 100), 1)}%`; };
  let agreement = "Only one side gave a range.";
  if (row.thor && row.loki) {
    const [tl, th] = magnitude(row.thor), [ll, lh] = magnitude(row.loki);
    const gap = Math.max(tl - lh, ll - th, 0);
    agreement = gap === 0 ? "Thor and Loki overlap." : `Thor and Loki are ${Math.round(gap)} points apart.`;
  }
  return `<article class="rating-card rating-${band.key}">
    <header><h3>${escapeHtml(row.country)} · ${escapeHtml(row.sector)}<small>Expected fall in imports</small></h3><span class="rating-pill${provisional ? " provisional" : ""}" title="${provisional ? "Provisional. " : ""}Rating is based on the middle of Odin's range"><i></i>${band.label}</span></header>
    <div class="rating-range">${fmtRange(row.odin)}</div>
    <div><div class="gauge" aria-hidden="true"><span class="odin" style="${pos(row.odin)}"></span></div>
      <div class="gauge-lines" aria-hidden="true">${row.thor ? `<div class="gauge-line thor"><span style="${pos(row.thor)}"></span></div>` : ""}${row.loki ? `<div class="gauge-line loki"><span style="${pos(row.loki)}"></span></div>` : ""}</div></div>
    <div class="rating-meta"><span>Range width <b>${widthLabel}</b></span><span>${agreement}</span></div>
    <p class="rating-note">${row.thor ? `Thor ${fmtRange(row.thor)}` : ""}${row.thor && row.loki ? " · " : ""}${row.loki ? `Loki ${fmtRange(row.loki)}` : ""}</p>
  </article>`;
}

function citeChips(run, claimIds) {
  const seen = new Set();
  return claimIds.flatMap((id) => run.claims?.[id]?.evidence || []).filter((e) => !seen.has(e.doc_id + e.quote) && seen.add(e.doc_id + e.quote)).slice(0, 6).map((e) =>
    `<details class="cite"><summary>${escapeHtml(e.doc_id)} · ${escapeHtml(e.date || "undated")}</summary><div class="pop"><q>“${escapeHtml(e.quote)}”</q><small>${escapeHtml(e.doc_id)}${e.locator ? ` · ${escapeHtml(e.locator)}` : ""} · Tier ${escapeHtml(e.tier ?? "?")} · published ${escapeHtml(e.date || "undated")}</small> <button class="chip-btn" data-action="open-doc" data-doc="${escapeHtml(e.doc_id)}" data-locator="${escapeHtml(e.locator)}" data-quote="${escapeHtml(e.quote)}">Open source →</button></div></details>`).join("");
}

function findingsSection(run, title, hint, items, open) {
  if (!items?.length) return "";
  const rows = items.map((item) => {
    const basis = [...new Set(item.claim_ids.map((id) => run.rulings?.[id]?.basis).filter(Boolean))].map((b) => b.replace("_", " "));
    return `<div class="finding">${escapeHtml(item.text)}<span class="badges">${basis.map((b) => `<span class="badge">${escapeHtml(b)}</span>`).join("")}</span><div class="cites">${citeChips(run, item.claim_ids)}</div></div>`;
  }).join("");
  return `<details class="brief-section" ${open ? "open" : ""}><summary style="list-style:none;cursor:pointer"><h2 style="display:inline-flex">${title} <em>${items.length}</em></h2></summary><p class="hint">${hint}</p>${rows}</details>`;
}

function renderBriefingResult(runInfo) {
  queueMicrotask(() => { renderExposureChart(document.querySelector("#exposure-chart"), runInfo.case); renderTradeCard(document.querySelector("#trade-card-brief"), runInfo.case.scope); });
  const run = runInfo.case;
  const brief = run.briefing || {};
  const complete = run.status === "COMPLETE";
  const rows = exposureRows(run);
  const docs = [...new Set(Object.values(run.claims || {}).flatMap((claim) => claim.evidence.map((e) => e.doc_id)))].sort();
  const scale = RATING_BANDS.map((b, i) => `<span class="rating-${b.key}"><i></i>${b.label} ${i === 0 ? "under 10%" : i === RATING_BANDS.length - 1 ? "over 55%" : `${RATING_BANDS[i - 1].max}–${b.max}%`}</span>`).join("");
  const options = state.cases.length > 1 ? `<select id="case-select" aria-label="Choose investigation">${state.cases.map((c) => `<option value="${escapeHtml(c.case_id)}" ${c.case_id === runInfo.id ? "selected" : ""}>${escapeHtml(c.case_id)} · ${escapeHtml((STATUS[c.status] || [0, c.status])[1])}</option>`).join("")}</select>` : "";
  content.innerHTML = `
    <section class="page-heading">
      <div><div class="eyebrow"><span class="live-dot"></span> OUTPUTS <span class="eyebrow-divider">/</span> ODIN'S RULINGS</div><h1>Executive briefing</h1><p class="page-subtitle">Case ${escapeHtml(runInfo.id)} · ${run.runs.length} trial run${run.runs.length === 1 ? "" : "s"}</p></div>
      <div class="heading-actions">${options}<span class="asof-pill"><span class="calendar-icon">▦</span> AS OF <strong>${escapeHtml(fmtDate(run.as_of).toUpperCase())}</strong></span><button class="button button-quiet" data-action="export-briefing">Download .md</button></div>
    </section>
    <p class="brief-question">${escapeHtml(run.question)}</p>
    <div class="brief-status ${complete ? "ok" : ""}"><span>${complete ? "✓" : "!"}</span><div><strong>${complete ? "Complete" : state.running === run.case_id ? "Running: results will fill in" : "Provisional: this analysis did not finish"}</strong><p>${complete ? "Odin judged the evidence sufficient." : state.running === run.case_id ? "The trial is still running. Ratings appear as Odin rules." : escapeHtml(friendlyReason(run.status_reasons)) + " Treat the ratings below as a first pass."}</p></div></div>
    <section class="rating-grid">${rows.length ? rows.map((row) => ratingCard(row, !complete, state.running === run.case_id || !Object.keys(run.rulings || {}).length)).join("") : '<p class="rating-note">No exposure ranges yet.</p>'}</section>
    <div class="rating-scale" title="Rating is based on the middle of Odin's range">${scale}</div>
    <section class="panel chart-card" id="exposure-chart"></section>
    <section class="panel chart-card" id="trade-card-brief"></section>
    ${brief.executive_summary ? `<section class="brief-section"><h2>Summary</h2><p class="brief-question" style="margin:6px 0 0">${escapeHtml(brief.executive_summary)}</p></section>` : ""}
    ${findingsSection(run, "Established", "Sourced facts and findings that survived cross-examination.", brief.established, true)}
    ${findingsSection(run, "Disputed", "Credible sources disagree. Not settled.", brief.contested, false)}
    ${findingsSection(run, "Withdrawn or rejected", "Claims a side conceded, or Odin rejected.", brief.conceded, false)}
    ${brief.outlook ? `<section class="brief-section"><h2>Next-quarter outlook</h2><div class="outlook"><p>${escapeHtml(brief.outlook.text)}</p><ul>${(brief.outlook.assumptions || []).map((a) => `<li>${escapeHtml(a)}</li>`).join("")}</ul></div></section>` : ""}
    <p class="sources-line">${docs.length} sources cited: ${docs.map((d) => `<button class="chip-btn" data-action="open-doc" data-doc="${escapeHtml(d)}">${escapeHtml(d)}</button>`).join(" ")}</p>`;
}

async function renderBriefing() {
  content.innerHTML = skeleton();
  try {
    await refreshCases();
    if (!state.caseId) throw new Error("no case");
    renderBriefingResult(await loadCase(state.caseId, true));
  } catch {
    briefingEmpty();
  }
}

function briefingEmpty() {
  content.innerHTML = `
    <section class="page-heading">
      <div><div class="eyebrow"><span class="live-dot"></span> OUTPUTS <span class="eyebrow-divider">/</span> ODIN'S RULINGS</div><h1>Executive briefing</h1><p class="page-subtitle">Decision-ready intelligence, authored from adjudicated claims with traceable citations.</p></div>
    </section>
    <section class="briefing-empty panel"><div class="empty-seal">◇</div><span class="section-kicker">NO BRIEFING YET</span><h2>No adjudicated briefing yet.</h2><p>Ask a question and Odin's briefing will appear here once the trial finishes.</p><button class="button button-primary" data-action="new-investigation">Begin investigation <span>→</span></button></section>`;
}

function downloadBriefing() {
  const md = state.run?.case?.briefing_md;
  if (!md) return showToast("No briefing available to download.");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
  link.download = `briefing-${state.run.id}.md`;
  link.click();
  URL.revokeObjectURL(link.href);
}


// ---- charts -----------------------------------------------------------------------
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const COLORS = { cyan: "#67ddf4", amber: "#ffca82", violet: "#b29aff", green: "#60ddb1", rose: "#ff8f9d", grey: "#5e6d83", orange: "#ff9f6b" };
const STATUS_COLORS = { established: COLORS.green, contested: COLORS.amber, conceded: COLORS.violet, rejected: COLORS.rose, revised: COLORS.cyan, unruled: COLORS.grey };
const nfmt = new Intl.NumberFormat("en-GB");
state.charts = [];
const track = (chart) => { state.charts.push(chart); return chart; };

async function loadTrade() {
  if (!state.trade) state.trade = await api("/trade");
  return state.trade;
}

async function renderTradeCard(root, scope) {
  if (!root || !window.Charts) return;
  root.innerHTML = `<div class="chart-head"><div><span class="section-kicker">TRADE FLOWS</span><h2>What the trade tables show</h2></div><div class="seg" role="group" aria-label="Metric"><button data-metric="volume_t" class="on">Volume</button><button data-metric="value_usd_m">Value</button></div></div><div class="route-chips"></div><div class="ch-slot"></div><div class="legend"></div><div class="stat-strip"></div><p class="chart-note"></p>`;
  let tables;
  try { tables = (await loadTrade()).tables.filter((t) => t.rows.length); } catch (error) { root.innerHTML = errorBox("Could not load trade data", error.message); return; }
  if (!tables.length) { root.innerHTML = '<div class="state-box"><strong>No trade tables in the corpus.</strong></div>'; return; }
  let list = tables;
  if (scope?.length) {
    const want = scope.map(([country, sector]) => `${country}|${sector}`.toLowerCase());
    const hit = tables.filter((t) => want.includes(`${t.to}|${t.product}`.toLowerCase()));
    if (hit.length) list = hit;
  }
  // open on the route with the sharpest year-on-year fall, so the story is visible straight away
  const change = (t) => {
    const rs = t.rows.map((r) => ({ y: +r.period.slice(0, 4), m: +r.period.slice(5), v: r.volume_t }));
    const y = Math.max(...rs.map((r) => r.y));
    const pairs = rs.filter((r) => r.y === y).map((r) => [r.v, rs.find((p) => p.y === y - 1 && p.m === r.m)?.v]).filter(([, p]) => p);
    return pairs.length ? pairs.reduce((a, [c]) => a + c, 0) / pairs.reduce((a, [, p]) => a + p, 0) - 1 : 0;
  };
  let sel = list.reduce((best, t) => (change(t) < change(best) ? t : best), list[0]).doc_id, metric = "volume_t";
  const hidden = new Set();
  const chart = track(window.Charts.lineChart(root.querySelector(".ch-slot")));
  const chips = root.querySelector(".route-chips"), legend = root.querySelector(".legend"), stats = root.querySelector(".stat-strip"), note = root.querySelector(".chart-note");

  const draw = () => {
    const t = list.find((x) => x.doc_id === sel);
    const rows = t.rows.map((r) => ({ ...r, y: +r.period.slice(0, 4), m: +r.period.slice(5) }));
    const year = Math.max(...rows.map((r) => r.y));
    const months = [...new Set(rows.filter((r) => r.y === year).map((r) => r.m))].sort((a, b) => a - b);
    const at = (y, m) => rows.find((r) => r.y === y && r.m === m);
    const cur = months.map((m) => at(year, m)), prev = months.map((m) => at(year - 1, m));
    const fmtV = (v) => (v == null ? "—" : metric === "volume_t" ? `${nfmt.format(v)} t` : `$${v}M`);
    const yoy = cur.map((c, i) => (c && prev[i] && prev[i][metric] ? (c[metric] / prev[i][metric] - 1) * 100 : null));
    const breakAt = yoy.findIndex((v) => v != null && v <= -25);
    chips.innerHTML = list.map((x) => `<button class="route-chip${x.doc_id === sel ? " on" : ""}" data-id="${x.doc_id}">${escapeHtml(x.from)} → ${escapeHtml(x.to)} · ${escapeHtml(x.product)}</button>`).join("");
    legend.innerHTML = [["cur", String(year), COLORS.cyan], ["prev", `${year - 1} (same months)`, COLORS.amber]].map(([k, n, c]) => `<button data-key="${k}" class="${hidden.has(k) ? "off" : ""}" style="--c:${c}"><i></i>${n}</button>`).join("");
    chart.update({
      label: `${t.from} to ${t.to} ${t.product}, monthly ${metric === "volume_t" ? "volume" : "value"}`,
      months: months.map((m) => MONTHS[m - 1]),
      series: [
        { key: "prev", name: String(year - 1), color: COLORS.amber, dash: "5 4", values: prev.map((r) => (r ? r[metric] : null)), hidden: hidden.has("prev") },
        { key: "cur", name: String(year), color: COLORS.cyan, area: true, values: cur.map((r) => (r ? r[metric] : null)), hidden: hidden.has("cur") },
      ],
      marker: breakAt >= 0 ? { index: breakAt, label: "imports break" } : null,
      tooltip: (i) => `<b>${MONTHS[months[i] - 1]}</b>${cur[i] ? `<div><span class="dot" style="background:${COLORS.cyan}"></span>${year}: <b>${fmtV(cur[i][metric])}</b></div>` : ""}${prev[i] ? `<div><span class="dot" style="background:${COLORS.amber}"></span>${year - 1}: ${fmtV(prev[i][metric])}</div>` : ""}${yoy[i] != null ? `<div class="${yoy[i] < 0 ? "neg" : "pos"}">${yoy[i] > 0 ? "+" : "−"}${Math.abs(yoy[i]).toFixed(1)}% vs last year</div>` : ""}<small>${escapeHtml(t.doc_id)} · row ${cur[i]?.row ?? "—"} · click to open</small>`,
      onPoint: (i) => cur[i] && openDoc(t.doc_id, `P${cur[i].row}`),
    });
    const both = cur.map((c, i) => [c, prev[i]]).filter(([c, p]) => c && p);
    const sumC = both.reduce((a, [c]) => a + c[metric], 0), sumP = both.reduce((a, [, p]) => a + p[metric], 0);
    const last3 = yoy.filter((v) => v != null).slice(-3);
    const worst = yoy.reduce((w, v, i) => (v != null && (w < 0 || v < yoy[w]) ? i : w), -1);
    const cls = (v) => (v < 0 ? "neg" : "pos");
    const pct = (v) => `${v > 0 ? "+" : "−"}${Math.abs(v).toFixed(0)}%`;
    stats.innerHTML = [
      sumP ? `<div class="stat"><span>${MONTHS[months[0] - 1]}–${MONTHS[months[months.length - 1] - 1]} ${year} vs ${year - 1}</span><b class="${cls(sumC / sumP - 1)}">${pct((sumC / sumP - 1) * 100)}</b></div>` : "",
      last3.length ? `<div class="stat"><span>Last ${last3.length} months, avg</span><b class="${cls(last3.reduce((a, b) => a + b, 0))}">${pct(last3.reduce((a, b) => a + b, 0) / last3.length)}</b></div>` : "",
      worst >= 0 ? `<div class="stat"><span>Worst month</span><b class="neg">${MONTHS[months[worst] - 1]} ${pct(yoy[worst])}</b></div>` : "",
      cur.filter(Boolean).length ? `<div class="stat"><span>Latest month</span><b>${fmtV(cur.filter(Boolean).slice(-1)[0][metric])}</b></div>` : "",
    ].join("");
    note.innerHTML = `Source: <button class="chip-btn" data-open="${escapeHtml(t.doc_id)}">${escapeHtml(t.doc_id)}</button> · Tier ${t.tier} · ${escapeHtml(t.outlet)}. Hover a month for exact numbers; click a point to open its table row.${t.damaged_rows ? ` ${t.damaged_rows} damaged row(s) excluded.` : ""}`;
  };
  root.addEventListener("click", (event) => {
    const el = event.target.closest("[data-metric], [data-id], [data-key], [data-open]");
    if (!el) return;
    if (el.dataset.metric) { metric = el.dataset.metric; root.querySelectorAll("[data-metric]").forEach((b) => b.classList.toggle("on", b === el)); }
    if (el.dataset.id) sel = el.dataset.id;
    if (el.dataset.key) hidden.has(el.dataset.key) ? hidden.delete(el.dataset.key) : hidden.add(el.dataset.key);
    if (el.dataset.open) return openDoc(el.dataset.open);
    draw();
  });
  draw();
}

function renderExposureChart(root, run) {
  if (!root || !window.Charts) return;
  const rows = exposureRows(run);
  if (!rows.length) { root.hidden = true; return; }
  root.innerHTML = `<div class="chart-head"><div><span class="section-kicker">THREE VIEWS</span><h2>How Thor, Loki and Odin compare</h2></div></div><div class="ch-slot"></div><div class="legend"></div><p class="chart-note">Each bar is a low-to-high range of expected fall in imports. Hover a row for the numbers. Click a legend item to hide it.</p>`;
  const bars = (row) => [
    { key: "thor", name: "Thor (high case)", color: COLORS.amber, range: row.thor && magnitude(row.thor) },
    { key: "loki", name: "Loki (limited case)", color: COLORS.violet, range: row.loki && magnitude(row.loki) },
    { key: "odin", name: "Odin (ruling)", color: COLORS.cyan, range: row.odin && magnitude(row.odin) },
  ];
  const hidden = new Set();
  const chart = track(window.Charts.rangeChart(root.querySelector(".ch-slot")));
  const draw = () => {
    root.querySelector(".legend").innerHTML = bars({}).map((b) => `<button data-key="${b.key}" class="${hidden.has(b.key) ? "off" : ""}" style="--c:${b.color}"><i></i>${b.name}</button>`).join("");
    chart.update({
      label: "Exposure ranges by flow", hidden,
      rows: rows.map((row) => ({ label: `${row.country} · ${row.sector}`, sub: row.odin ? "Odin's range in blue" : "no ruling yet", bars: bars(row) })),
      tooltip: (r) => `<b>${escapeHtml(r.label)}</b>${r.bars.map((b) => `<div><span class="dot" style="background:${b.color}"></span>${b.name.split(" ")[0]}: ${b.range ? `<b>${round1(b.range[0])}% to ${round1(b.range[1])}%</b>` : "no range"}</div>`).join("")}`,
    });
  };
  root.addEventListener("click", (event) => {
    const el = event.target.closest("[data-key]");
    if (!el) return;
    hidden.has(el.dataset.key) ? hidden.delete(el.dataset.key) : hidden.add(el.dataset.key);
    draw();
  });
  draw();
}

const TIER_COLORS = { 1: COLORS.green, 2: COLORS.cyan, 3: COLORS.amber, 4: COLORS.rose };
async function renderDocTimeline(root) {
  if (!root || !window.Charts) return;
  root.innerHTML = `<div class="chart-head"><div><span class="section-kicker">TIMELINE</span><h2>When sources were published</h2></div><span id="month-chip"></span></div><div class="ch-slot"></div><div class="legend">${[1, 2, 3, 4].map((t) => `<span style="display:inline-flex;align-items:center;gap:7px;color:var(--muted);font-size:12px"><i style="width:10px;height:10px;border-radius:3px;background:${TIER_COLORS[t]}"></i>Tier ${t}</span>`).join("")}</div><p class="chart-note">Click a bar to filter the list to that month. Atlas reference sheets are left out.</p>`;
  let docs;
  try { docs = (await api("/documents?limit=500&exclude_type=atlas")).documents; } catch { root.hidden = true; return; }
  const key = (d) => d.date.slice(0, 7);
  const keys = docs.map(key).sort();
  const months = [];
  for (let [y, m] = keys[0].split("-").map(Number); `${y}-${pad2(m)}` <= keys[keys.length - 1]; m === 12 ? (y++, (m = 1)) : m++) months.push(`${y}-${pad2(m)}`);
  const full = months.map((id) => ({ id, label: `${MONTHS[+id.slice(5) - 1]}${id.slice(5) === "01" || id === months[0] ? " " + id.slice(2, 4) : ""}`, values: docs.filter((d) => key(d) === id).reduce((a, d) => ((a[d.tier] = (a[d.tier] || 0) + 1), a), {}) }));
  // collapse runs of 3+ empty months into one "···" marker so busy months get the space
  const data = [];
  for (let i = 0; i < full.length; i++) {
    if (Object.keys(full[i].values).length) { data.push(full[i]); continue; }
    let j = i;
    while (j < full.length && !Object.keys(full[j].values).length) j++;
    if (j - i >= 3) { data.push({ id: `gap-${i}`, label: "···", values: {}, gap: j - i }); i = j - 1; } else data.push(full[i]);
  }
  const chart = track(window.Charts.stackedBars(root.querySelector(".ch-slot")));
  const draw = () => {
    chart.update({
      label: "Documents per month by reliability tier", selected: state.month || null, data,
      stacks: [1, 2, 3, 4].map((t) => ({ key: t, name: `Tier ${t}`, color: TIER_COLORS[t] })),
      tooltip: (d) => d.gap ? `<b>${d.gap} months</b><div>No documents published</div>` : `<b>${MONTHS[+d.id.slice(5) - 1]} ${d.id.slice(0, 4)}</b>${[1, 2, 3, 4].filter((t) => d.values[t]).map((t) => `<div><span class="dot" style="background:${TIER_COLORS[t]}"></span>Tier ${t}: ${d.values[t]}</div>`).join("") || "<div>No documents</div>"}`,
      onSelect: (id) => { if (id && id.startsWith("gap-")) return; state.month = id; draw(); loadDocs(); },
    });
    root.querySelector("#month-chip").innerHTML = state.month ? `<span class="filter-chip">${MONTHS[+state.month.slice(5) - 1]} ${state.month.slice(0, 4)}<button data-clear-month aria-label="Clear month filter">✕</button></span>` : "";
  };
  root.addEventListener("click", (event) => { if (event.target.closest("[data-clear-month]")) { state.month = null; draw(); loadDocs(); } });
  draw();
}

function statusBars(run) {
  const claims = Object.values(run.claims);
  const status = (c) => run.rulings[c.id]?.status || (c.status === "conceded" ? "conceded" : c.status === "amended" ? "revised" : "unruled");
  const row = (side, label) => {
    const counts = {};
    claims.filter((c) => c.side === side).forEach((c) => { const s = status(c); counts[s] = (counts[s] || 0) + 1; });
    const segs = Object.keys(STATUS_COLORS).filter((s) => counts[s]).map((s) => `<button style="--c:${STATUS_COLORS[s]};--n:${counts[s]}" data-action="filter-claims" data-status="${s}" title="${counts[s]} ${s}">${counts[s]} ${s}</button>`).join("");
    return `<div class="sbar"><span>${label}</span><div>${segs || '<em style="padding:6px 10px;color:var(--subtle)">no claims</em>'}</div></div>`;
  };
  return `<div class="status-bars" aria-label="Claims by ruling">${row("THOR", "THOR")}${row("LOKI", "LOKI")}<p class="chart-note" style="margin:0">Click a segment to show only those claims. Click again to clear.</p></div>`;
}

// ---- dialog & events --------------------------------------------------------------
function openDialog() { modal.hidden = false; document.querySelector("#dialog-query").value = activeQuestion; document.querySelector("#dialog-query").focus(); }
function closeDialog() { modal.hidden = true; }
function closeSheet() { sheet.hidden = true; }

document.addEventListener("click", (event) => {
  const jump = event.target.closest("[data-jump]");
  if (jump) { event.preventDefault(); document.querySelector(`#j-${jump.dataset.jump}`)?.scrollIntoView({ behavior: "smooth", block: "start" }); return; }
  const target = event.target.closest("[data-page], [data-action]");
  if (!target) return;
  if (target.dataset.page) {
    event.preventDefault();
    navigate(target.dataset.page);
    return;
  }
  const d = target.dataset;
  switch (d.action) {
    case "new-investigation": openDialog(); break;
    case "close-dialog": closeDialog(); break;
    case "clear-query": document.querySelector("#investigation-query").value = ""; break;
    case "submit-dialog": submitInvestigation(document.querySelector("#dialog-query").value); break;
    case "open-case-id": state.caseId = d.case; sessionStorage.setItem("ragnarisk-case", d.case); state.run = null; navigate("council"); break;
    case "open-latest": navigate("council"); break;
    case "replay-feed": replayFeed(); break;
    case "open-doc": openDoc(d.doc, d.locator || "", d.quote || ""); break;
    case "close-sheet": closeSheet(); break;
    case "export-briefing": downloadBriefing(); break;
    case "retry": refreshCurrent(); break;
    case "filter-claims": { const cols = document.querySelector(".trial-cols"); const on = cols.dataset.filter !== d.status; if (on) cols.dataset.filter = d.status; else delete cols.dataset.filter; document.querySelectorAll(".sbar button").forEach((b) => b.classList.toggle("on", on && b.dataset.status === d.status)); break; }
    case "toggle-clamp": { const p = target.previousElementSibling; p.classList.toggle("open"); target.textContent = p.classList.contains("open") ? "Show less" : "Show more"; break; }
  }
});

document.addEventListener("submit", (event) => {
  if (event.target.id !== "investigation-form") return;
  event.preventDefault();
  submitInvestigation(document.querySelector("#investigation-query").value);
});

document.addEventListener("input", (event) => {
  if (event.target.id === "investigation-query") {
    activeQuestion = event.target.value;
    sessionStorage.setItem("ragnarisk-active-question", activeQuestion);
  }
  if (event.target.id === "evidence-search") { clearTimeout(docTimer); docTimer = setTimeout(() => loadDocs(), 250); }
});

document.addEventListener("change", (event) => {
  if (["source-filter", "tier-filter", "country-filter"].includes(event.target.id)) loadDocs();
  if (event.target.id === "case-select") { state.caseId = event.target.value; sessionStorage.setItem("ragnarisk-case", state.caseId); state.run = null; renderBriefing(); }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") { if (!modal.hidden) closeDialog(); if (!sheet.hidden) closeSheet(); }
  if (event.key === "Enter" && event.target.id === "dialog-query" && (event.metaKey || event.ctrlKey)) submitInvestigation(event.target.value);
});

window.addEventListener("hashchange", () => {
  const page = window.location.hash.slice(1) || "overview";
  if (Object.hasOwn(pageTitles, page)) renderPage(page);
});

(async function boot() {
  const initialPage = window.location.hash.slice(1);
  if (Object.hasOwn(pageTitles, initialPage) && initialPage !== "overview") renderPage(initialPage);
  else hydrateOverview();
  try {
    const health = await api("/health");
    state.canRun = !!health.provider;
    if (health.running) { state.caseId = health.running; startPolling(health.running); }
  } catch { /* server unreachable: pages show their own errors */ }
})();
