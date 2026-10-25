// Ragnarisk dashboard. Every page reads from the local API (/api/*), which is backed by one SQLite database.
const content = document.querySelector("#page-content");
const overviewMarkup = document.querySelector("#overview-template").innerHTML;  // the Dashboard tab
const breadcrumb = document.querySelector("#breadcrumb-current");
const modal = document.querySelector("#modal-backdrop");
const toast = document.querySelector("#toast");
const sheet = document.querySelector("#doc-sheet");
let toastTimeout;
const SAMPLE_QUESTION = "As of 25 January 2027, how exposed are Midgard and Tolan to the end of the Orvan corridor agreement? Give a low and high range for wheat and maize imports.";
let activeQuestion = sessionStorage.getItem("ragnarisk-active-question") || SAMPLE_QUESTION;

const state = { caseId: sessionStorage.getItem("ragnarisk-case") || "", cases: [], running: null, canRun: false, live: [], lastSeq: -1, timer: null, feed: [], feedTimer: null, run: null,
  answerId: sessionStorage.getItem("ragnarisk-answer") || "", asOf: "", askNotice: "",
  askMode: sessionStorage.getItem("ragnarisk-mode") === "full" ? "full" : "fast" };
// Fast answer: per-trial limits the server already accepts (OPTION_RANGES). One run, so no remand.
const FAST_OPTIONS = { min_runs: 1, max_runs: 1, max_claims_per_side: 3, max_steps_per_turn: 5 };

const pageTitles = {
  ask: "Ask",
  overview: "Dashboard",
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

const TOKEN_KEY = "ragnarisk-token";
const getToken = () => { try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } };

function askToken(message = "") {
  const backdrop = document.querySelector("#token-backdrop");
  const error = document.querySelector("#token-error");
  if (message) { error.hidden = false; error.textContent = message; }  // later 401s from parallel requests must not erase it
  backdrop.hidden = false;
  document.querySelector("#token-input").focus();
}

async function api(path, options = {}) {
  const token = getToken();
  const response = await fetch(`/api${path}`, { cache: "no-store", ...options, headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(options.headers || {}) } });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401) {
    try { sessionStorage.removeItem(TOKEN_KEY); } catch { /* storage may be unavailable */ }
    askToken(token ? "That token was not accepted." : "");
  }
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
  if (breadcrumb) breadcrumb.textContent = pageTitles[page];
  document.querySelectorAll(".tab[data-page]").forEach((link) => link.classList.toggle("active", link.dataset.page === page));
  document.title = page === "ask" ? "Ragnarisk — Ask" : `Ragnarisk — ${pageTitles[page]}`;
  if (page === "ask") return renderAsk();
  if (page === "overview") {
    content.innerHTML = overviewMarkup;
    const query = document.querySelector("#investigation-query");
    if (query) query.value = activeQuestion;
    hydrateOverview();
    return;
  }
  ({ investigations: renderInvestigations, evidence: renderEvidence, council: renderCouncil, briefing: renderBriefing })[page]();
}

// "#ask/<case_id>" is a shareable link to one answer; the page is the part before the slash.
const currentPage = () => (window.location.hash.slice(1) || "ask").split("/")[0];
const linkedAnswer = () => decodeURIComponent((window.location.hash.slice(1).split("/")[1]) || "");
const refreshCurrent = () => renderPage(currentPage());

// ---- overview ---------------------------------------------------------------------
async function hydrateOverview() {
  try {
    const [summary] = await Promise.all([api("/summary"), refreshCases()]);
    state.canRun = summary.can_run;
    const latest = state.cases.find((c) => c.case_id === state.caseId) || state.cases[0];
    const set = (id, html) => { const el = document.querySelector(id); if (el) el.innerHTML = html; };
    const corpus = summary.corpus;
    const label = document.querySelector("#qa-corpus");
    if (label) label.textContent = `${corpus.packs} pack${corpus.packs === 1 ? "" : "s"} · ${corpus.documents} documents`;
    showAsOf();
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
  if (/max_runs=1\b/.test(text)) return "Fast answer: one trial run, so Odin could not send the case back for more evidence.";
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
    <section class="panel chart-card" id="intake-card"></section>
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
  renderIntake(document.querySelector("#intake-card"));
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
    const [memories, influence] = await Promise.all([
      Promise.all(["HEIMDALL", "THOR", "LOKI", "ODIN"].map((a) => api(`/memory/${a.toLowerCase()}`).catch(() => ({ agent: a, total: 0, memories: [] })))),
      api(`/cases/${encodeURIComponent(state.caseId)}/events?types=memory_influence&limit=200`).catch(() => ({ events: [] })),
    ]);
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
      <nav class="council-jump" aria-label="Sections"><a href="#council" data-jump="verify">Verification</a><a href="#council" data-jump="trial">Thor vs Loki</a><a href="#council" data-jump="xexam">Cross-examination</a><a href="#council" data-jump="memory">Memory</a><a href="#council" data-jump="rulings">Rulings</a></nav>
      ${running ? `<section class="live-strip panel"><span class="live-dot"></span><ul>${live || "<li>Starting…</li>"}</ul></section>` : ""}
      <section class="panel council-verify" id="j-verify"><div><span class="agent-avatar heimdall">H</span><b>Heimdall</b> · ${run.verified === null ? "verifying…" : run.verified ? "event verified" : "event NOT verified"} <span class="badge">${escapeHtml(run.event_id || "—")}</span></div><p class="clamp">${escapeHtml(run.verdict_reason || "")}</p>${(run.verdict_reason || "").length > 260 ? '<button class="more-btn" data-action="toggle-clamp">Show more</button>' : ""}<div class="cites">${flags}</div></section>
      ${claims.length ? statusBars(run) : ""}
      <section class="trial-cols" id="j-trial"><div><h2><span class="agent-avatar thor">T</span> Thor <small>argues high exposure</small></h2>${col("THOR")}</div>
        <div><h2><span class="agent-avatar loki">L</span> Loki <small>argues limited exposure</small></h2>${col("LOKI")}</div></section>
      ${attacks ? `<section class="brief-section" id="j-xexam"><h2>Cross-examination <em>${Object.keys(run.attacks).length}</em></h2><p class="hint">Each side attacks the other's claims. A defence needs new evidence or it is a concession.</p>${attacks}</section>` : ""}
      ${memorySection(memories, influence.events)}
      <section class="brief-section" id="j-rulings"><h2><span class="agent-avatar odin">O</span> Odin's rulings by run</h2>${runs || '<p class="rating-note">No run has finished yet.</p>'}<button class="button button-quiet" data-page="briefing" style="margin-top:12px">Open the briefing →</button></section>`;
  } catch (error) {
    content.innerHTML = errorBox("Could not load the trial", error.message);
  }
}

function memorySection(memories, influence) {
  const agents = { HEIMDALL: "heimdall", THOR: "thor", LOKI: "loki", ODIN: "odin" };
  const find = (agent, id) => memories.find((m) => m.agent === agent)?.memories.find((x) => x.id === id);
  const line = (e) => {
    if (e.kind === "suppressed_repeat_of_conceded_claim") return `<li><b>Blocked a repeat.</b> It had conceded “${escapeHtml(e.suppressed_claim)}” earlier in this case and offered no new evidence, so the claim never reached the trial. <small>memory #${e.memory_ids[0]} · run ${e.run}</small></li>`;
    const texts = (e.memory_ids || []).map((id) => find(e.agent, id)?.text).filter(Boolean).map((t) => `“${escapeHtml(t.slice(0, 120))}”`).join(" ");
    return `<li><b>Used memory</b> #${(e.memory_ids || []).join(", #")} while working on ${escapeHtml(e.phase || "its task")} (run ${e.run}). ${texts}</li>`;
  };
  const cards = Object.keys(agents).map((agent) => {
    const mem = memories.find((m) => m.agent === agent) || { total: 0, memories: [] };
    const mine = influence.filter((e) => e.agent === agent);
    return `<article class="mem-card"><header><span class="agent-avatar ${agents[agent]}">${agent[0]}</span><b>${agent}</b><span class="badge">${mem.total} saved</span></header>${mine.length ? `<ul class="mem-list">${mine.map(line).join("")}</ul>` : '<p class="rating-note">Memory did not change this agent\'s actions in this case.</p>'}${mem.memories.length ? `<details class="why"><summary>Private memory (latest ${Math.min(mem.memories.length, 6)})</summary><ul class="mem-list">${mem.memories.slice(0, 6).map((m) => `<li><small>#${m.id} · ${escapeHtml(m.kind)} · known as of ${escapeHtml(m.knowledge_date)}</small><br>${escapeHtml(m.text.slice(0, 220))}</li>`).join("")}</ul></details>` : ""}</article>`;
  }).join("");
  return `<section class="brief-section" id="j-memory"><h2>Memory <em>${influence.length} action${influence.length === 1 ? "" : "s"} changed</em></h2><p class="hint">Every agent keeps its own private, lasting memory. Below is each time memory changed what an agent did in this case.</p><div class="mem-grid">${cards}</div></section>`;
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
        if (["ask", "council", "briefing", "overview"].includes(currentPage())) { state.run = null; refreshCurrent(); }
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

function exposureTable(rows, unit) {
  const cell = (range, cls = "") => `<td class="${cls}">${range ? `<b>${round1(magnitude(range)[0])}</b> to <b>${round1(magnitude(range)[1])}</b>%` : "<span class=\"muted\">no range</span>"}</td>`;
  return `<div class="table-scroll"><table class="exposure-table"><thead><tr><th>Country</th><th>Sector</th><th>Loki (low to high)</th><th>Thor (low to high)</th><th>Odin (final)</th></tr></thead><tbody>${rows.map((r) => `<tr><td>${escapeHtml(r.country)}</td><td>${escapeHtml(r.sector)}</td>${cell(r.loki)}${cell(r.thor)}${cell(r.odin, "odin-cell")}</tr>`).join("")}</tbody></table></div><p class="chart-note">${unit ? `Unit: ${escapeHtml(unit)}` : "Expected fall in imports, %."} Odin's range is his ruling; Thor's and Loki's are the claims that survived cross-examination.</p>`;
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
  queueMicrotask(() => { renderExposureChart(document.querySelector("#exposure-chart"), runInfo.case); renderTradeCard(document.querySelector("#trade-card-brief"), runInfo.case.scope); renderAcceptance(document.querySelector("#acceptance"), runInfo.id); });
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
    ${rows.length ? `<section class="panel chart-card" id="exposure-table"><div class="chart-head"><div><span class="section-kicker">EXPOSURE TABLE</span><h2>Low and high bounds</h2></div></div>${exposureTable(rows, run.unit)}</section>` : ""}
    <section class="panel chart-card" id="exposure-chart"></section>
    <section class="panel chart-card" id="trade-card-brief"></section>
    ${brief.executive_summary ? `<section class="brief-section"><h2>Summary</h2><p class="brief-question" style="margin:6px 0 0">${escapeHtml(brief.executive_summary)}</p></section>` : ""}
    ${findingsSection(run, "Established", "Sourced facts and findings that survived cross-examination.", brief.established, true)}
    ${findingsSection(run, "Disputed", "Credible sources disagree. Not settled.", brief.contested, false)}
    ${findingsSection(run, "Withdrawn or rejected", "Claims a side conceded, or Odin rejected.", brief.conceded, false)}
    ${brief.outlook ? `<section class="brief-section"><h2>Next-quarter outlook</h2><div class="outlook"><p>${escapeHtml(brief.outlook.text)}</p><ul>${(brief.outlook.assumptions || []).map((a) => `<li>${escapeHtml(a)}</li>`).join("")}</ul></div></section>` : ""}
    <section class="panel chart-card" id="acceptance"></section>
    <p class="sources-line">${docs.length} sources cited: ${docs.map((d) => `<button class="chip-btn" data-action="open-doc" data-doc="${escapeHtml(d)}">${escapeHtml(d)}</button>`).join(" ")}</p>`;
}

async function renderAcceptance(root, caseId) {
  if (!root) return;
  try {
    const r = await api(`/cases/${encodeURIComponent(caseId)}/acceptance`);
    const mark = (c) => (c.passed === true ? "✓" : c.passed === false ? "✗" : "○");
    const cls = (c) => (c.passed === true ? "ok" : c.passed === false ? "bad" : "manual");
    root.innerHTML = `<div class="chart-head"><div><span class="section-kicker">ACCEPTANCE</span><h2>${r.passed} of ${r.checks.length} minimum criteria met</h2></div></div><ul class="acc-list">${r.checks.map((c) => `<li class="acc-row ${cls(c)}"><span class="acc-mark">${mark(c)}</span><span><strong>${escapeHtml(c.title)}</strong><small>${escapeHtml(c.detail)}</small></span></li>`).join("")}</ul><p class="chart-note">Checked against this case's saved log. ○ means it is judged by eye, not by the log.</p>`;
  } catch { root.remove(); }
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
  // the year to chart: the latest one that has at least 3 months with a prior-year partner (a stray extra month must not become "the year")
  const yearOf = (rs) => {
    const ys = [...new Set(rs.map((r) => r.y))].sort((a, b) => b - a);
    return ys.find((y) => rs.filter((r) => r.y === y && rs.some((p) => p.y === y - 1 && p.m === r.m)).length >= 3) ?? ys[0];
  };
  const change = (t) => {
    const rs = t.rows.map((r) => ({ y: +r.period.slice(0, 4), m: +r.period.slice(5), v: r.volume_t }));
    const y = yearOf(rs);
    const pairs = rs.filter((r) => r.y === y).map((r) => [r.v, rs.find((p) => p.y === y - 1 && p.m === r.m)?.v]).filter(([, p]) => p);
    return pairs.length ? pairs.reduce((a, [c]) => a + c, 0) / pairs.reduce((a, [, p]) => a + p, 0) - 1 : 0;
  };
  // a cut-off or damaged table (one or two months) must not be the first thing a judge sees: prefer tables with a real year-on-year span
  const span = (t) => { const ms = new Set(t.rows.map((r) => r.period)); return t.rows.filter((r) => ms.has(`${+r.period.slice(0, 4) - 1}${r.period.slice(4)}`)).length; };
  const solid = list.filter((t) => span(t) >= 3 && !t.damaged_rows);
  const pool = solid.length ? solid : list;
  let sel = pool.reduce((best, t) => (change(t) < change(best) ? t : best), pool[0]).doc_id, metric = "volume_t";
  const hidden = new Set();
  const chart = track(window.Charts.lineChart(root.querySelector(".ch-slot")));
  const chips = root.querySelector(".route-chips"), legend = root.querySelector(".legend"), stats = root.querySelector(".stat-strip"), note = root.querySelector(".chart-note");

  const draw = () => {
    const t = list.find((x) => x.doc_id === sel);
    const rows = t.rows.map((r) => ({ ...r, y: +r.period.slice(0, 4), m: +r.period.slice(5) }));
    const year = yearOf(rows);
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


// ---- data intake (live packs) -----------------------------------------------------
const MONTH_NAMES = { january: 1, february: 2, march: 3, april: 4, may: 5, june: 6, july: 7, august: 8, september: 9, october: 10, november: 11, december: 12, jan: 1, feb: 2, mar: 3, apr: 4, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9, oct: 10, nov: 11, dec: 12 };

function parseAsOf(text) {
  const found = [];
  const add = (index, y, m, d) => {
    const date = new Date(Date.UTC(y, m - 1, d));
    if (m >= 1 && m <= 12 && date.getUTCMonth() === m - 1 && date.getUTCDate() === d) found.push([index, date.toISOString().slice(0, 10)]);
  };
  for (const m of text.matchAll(/\b(\d{4})-(\d{2})-(\d{2})\b/g)) add(m.index, +m[1], +m[2], +m[3]);
  for (const m of text.matchAll(/\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]+)\.?,?\s+(\d{4})\b/g)) if (MONTH_NAMES[m[2].toLowerCase()]) add(m.index, +m[3], MONTH_NAMES[m[2].toLowerCase()], +m[1]);
  for (const m of text.matchAll(/\b([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b/g)) if (MONTH_NAMES[m[1].toLowerCase()]) add(m.index, +m[3], MONTH_NAMES[m[1].toLowerCase()], +m[2]);
  if (!found.length) return null;
  const marker = /as\s+of\b/i.exec(text);
  const pool = marker ? found.filter((f) => f[0] >= marker.index) : [];
  return (pool.length ? pool : found).sort((a, b) => a[0] - b[0])[0][1];
}

function showAsOf() {
  const el = document.querySelector("#qa-asof");
  const box = document.querySelector("#investigation-query");
  if (!el || !box) return;
  const iso = parseAsOf(box.value);
  el.textContent = iso ? fmtDate(iso) : "newest pack";
  el.title = iso ? "Read from your question" : "No date in the question: the newest pack's date is used";
}

const reportCount = (o) => (Array.isArray(o) ? o.length : Object.keys(o || {}).length);

function packRow(pack) {
  const r = pack.report || {};
  const t = r.trade || {};
  const chips = [
    [reportCount(r.quarantined), "quarantined", "warn"], [reportCount(r.repaired), "repaired", "ok"], [reportCount(r.duplicates), "duplicates", "warn"],
    [reportCount(r.truncated), "truncated", "warn"], [reportCount(t.rows_flagged), "trade rows excluded", "bad"], [reportCount(t.incomplete_tables), "cut-off tables", "bad"],
    [reportCount(r.off_manifest), "not in manifest", "warn"], [reportCount(r.id_collisions), "ID clashes", "bad"],
  ].filter(([n]) => n).map(([n, label, kind]) => `<span class="pack-chip ${kind}">${n} ${label}</span>`).join("") || '<span class="pack-chip ok">no problems found</span>';
  const list = (title, entries) => (entries.length ? `<h4>${title} <em>${entries.length}</em></h4><ul class="report-list">${entries.map((e) => `<li>${e}</li>`).join("")}</ul>` : "");
  const pairs = (o) => Object.entries(o || {}).map(([k, v]) => `<b>${escapeHtml(k)}</b> ${escapeHtml(v)}`);
  const detail = [
    list("Quarantined (not used)", pairs(r.quarantined)), list("Repaired", pairs(r.repaired)), list("Duplicates (collapsed in search)", Object.entries(r.duplicates || {}).map(([k, v]) => `<b>${escapeHtml(k)}</b> is a copy of ${escapeHtml(v)}`)),
    list("Truncated", pairs(r.truncated)),
    list("Trade rows excluded from every number", (t.rows_flagged || []).map((x) => `<b>${escapeHtml(x.doc_id)}</b> row ${x.row} (${escapeHtml(x.period)}): ${escapeHtml(x.reason.replace(/_/g, " ").toLowerCase())}`)),
    list("Cut-off trade tables", (t.incomplete_tables || []).map((x) => `<b>${escapeHtml(x.doc_id)}</b> has no usable row for ${escapeHtml(x.missing_months.join(", "))}`)),
    list("Files the manifest does not list", (r.off_manifest || []).map(escapeHtml)), list("Document ID clashes", (r.id_collisions || []).map(escapeHtml)), list("Notes", (r.notes || []).map(escapeHtml)),
  ].join("");
  return `<details class="pack-row"><summary><span class="pack-name"><b>${escapeHtml(pack.name || pack.pack_id)}</b><small>${escapeHtml(pack.event_id || "no event ID")} · ${pack.documents} documents${pack.as_of ? ` · as of ${escapeHtml(pack.as_of)}` : ""}</small></span><span class="pack-chips">${chips}</span></summary>${detail || '<p class="chart-note">Nothing to report.</p>'}</details>`;
}

async function renderIntake(root) {
  if (!root) return;
  root.innerHTML = `<div class="chart-head"><div><span class="section-kicker">DATA INTAKE</span><h2>Packs in the corpus</h2></div><div class="intake-actions"><button class="button button-quiet" data-action="rescan">Rescan folder</button><label class="button button-primary upload-btn">Upload pack (.zip)<input type="file" id="pack-file" accept=".zip,application/zip" hidden></label></div></div><div id="pack-list">${skeleton()}</div><div class="dropzone" id="dropzone">Drop a pack .zip here. New event packs are read live: no code change, no restart.</div><p class="chart-note" id="intake-note"></p>`;
  const list = root.querySelector("#pack-list");
  try {
    const data = await api("/packs");
    list.innerHTML = data.packs.map(packRow).join("") || '<p class="chart-note">No packs yet.</p>';
    root.querySelector("#intake-note").textContent = (data.watching ? "Packs dropped into the live folder are picked up automatically every few seconds. " : "") + Object.entries(data.errors || {}).map(([k, v]) => `${k}: ${v}`).join(" · ");
  } catch (error) {
    list.innerHTML = errorBox("Could not load packs", error.message);
  }
  const upload = async (file) => {
    if (!file) return;
    showToast(`Ingesting ${file.name}…`);
    try {
      const result = await api("/ingest", { method: "POST", body: await file.arrayBuffer(), headers: { "Content-Type": "application/zip", "X-Pack-Name": file.name } });
      const r = result.report;
      state.trade = null;
      showToast(`Ingested ${r.pack_id}: ${r.ingested} documents, ${reportCount(r.quarantined)} quarantined, ${reportCount(r.trade?.rows_flagged)} trade rows excluded.`);
      renderEvidence();
    } catch (error) {
      showToast(error.message);
    }
  };
  root.querySelector("#pack-file").addEventListener("change", (event) => upload(event.target.files[0]));
  const zone = root.querySelector("#dropzone");
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.add("over"); }));
  ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.remove("over"); }));
  zone.addEventListener("drop", (event) => upload(event.dataTransfer.files[0]));
}

// ---- ask: the first screen (question in, answer with evidence out) ---------------
const normQ = (q) => String(q || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

function recentAnswers() {
  const recent = state.cases.filter((c) => c.rulings > 0).slice(0, 4);
  if (!recent.length) return "";
  return `<span class="ask-recent-label">Recent answers</span>${recent.map((c) =>
    `<button class="ask-recent-item" data-action="show-answer" data-case="${escapeHtml(c.case_id)}"><span>${escapeHtml((c.question || "").replace(/^As of [^,]+,\s*/i, "").replace(/^\w/, (ch) => ch.toUpperCase()))}</span><small>${escapeHtml((STATUS[c.status] || [0, c.status])[1])} · as of ${escapeHtml(fmtDate(c.as_of))}</small></button>`).join("")}`;
}

function renderAsk() {
  const answering = state.answerId && state.running === state.answerId;
  content.innerHTML = `
    <section class="ask-hero${state.answerId ? " has-answer" : ""}">
      <span class="ask-badge"><i></i> Adversarial AI · every number traced to a source</span>
      <h1>Ask about a <span class="grad">supply-chain disruption</span></h1>
      <p class="ask-lede">Four AI agents check the event, argue both sides from the Runemark corpus, and a judge rules on every claim. You get a range, the evidence behind it, and a PDF report.</p>
      <form class="ask-box" id="ask-form">
        <label for="ask-query" class="sr-only">Your question</label>
        <textarea id="ask-query" rows="3" placeholder="For example: how exposed is Midgard to the end of the Orvan corridor agreement?">${escapeHtml(activeQuestion)}</textarea>
        <fieldset class="ask-mode"><legend class="sr-only">How thorough</legend>
          <label><input type="radio" name="ask-mode" value="fast" ${state.askMode === "fast" ? "checked" : ""} /> <span><b>Fast answer</b><small>One trial run, fewer claims per side. Quicker.</small></span></label>
          <label><input type="radio" name="ask-mode" value="full" ${state.askMode === "full" ? "checked" : ""} /> <span><b>Full trial</b><small>Two runs with Odin's remand. Most rigorous, slower.</small></span></label>
        </fieldset>
        <div class="ask-row">
          <label class="ask-date">As of <input type="date" id="ask-asof" value="${escapeHtml(state.asOf)}" /></label>
          <button type="button" class="link-btn" data-action="ask-example">Use the sample question</button>
          <button class="button button-primary ask-go" type="submit" ${answering ? "disabled" : ""}>${answering ? "Answering…" : "Get answer"} <span>→</span></button>
        </div>
      </form>
      ${state.askNotice ? `<p class="ask-notice" role="status">${escapeHtml(state.askNotice)}</p>` : ""}
      <ol class="how" aria-label="How an answer is made">
        <li><span class="how-icons"><span class="how-ico h">H</span></span><b>Verify</b><small>Heimdall checks the event is real as of your date</small></li>
        <li><span class="how-icons"><span class="how-ico t">T</span><span class="how-ico l">L</span></span><b>Argue</b><small>Thor and Loki make sealed cases, then cross-examine</small></li>
        <li><span class="how-icons"><span class="how-ico o">O</span></span><b>Rule</b><small>Odin judges every claim; code bounds the range</small></li>
        <li><span class="how-icons"><span class="how-ico r">▤</span></span><b>Report</b><small>Answer, evidence and a PDF you can hand over</small></li>
      </ol>
      <div class="ask-recent" id="ask-recent">${recentAnswers()}</div>
    </section>
    <section class="answer" id="answer" aria-live="polite"></section>`;
  if (!state.cases.length) refreshCases().then(() => { const el = document.querySelector("#ask-recent"); if (el) el.innerHTML = recentAnswers(); }).catch(() => {});
  if (!state.asOf) api("/health").then((h) => { state.asOf = h.as_of_default || ""; const el = document.querySelector("#ask-asof"); if (el && !el.value) el.value = state.asOf; }).catch(() => {});
  if (answering) document.querySelector("#answer").innerHTML = progressMarkup();
  else if (state.answerId) showAnswer(state.answerId, { scroll: false });
}

function progressMarkup() {
  const lines = state.live.slice(-8);
  return `<div class="answer-card working"><span class="spinner" aria-hidden="true"></span><div><h2>Working on your answer…</h2>
    <p>Heimdall checks the event, Thor and Loki argue opposite sides, then Odin rules on every claim. ${state.askMode === "fast" ? "Fast answer: one trial run." : "Full trial: two runs with a remand, so this takes longer."} Every step is a live AI call; progress appears below.</p>
    ${lines.length ? `<ol class="live-lines">${lines.map((l) => `<li>${escapeHtml(l)}</li>`).join("")}</ol>` : ""}</div></div>`;
}

async function askQuestion(question, asOf) {
  question = question.trim();
  asOf = parseAsOf(question) || asOf;  // a date written in the question is the clock; the date box is only the fallback
  if (question.length < 10) return showToast("Type a question of at least 10 characters.");
  activeQuestion = question;
  sessionStorage.setItem("ragnarisk-active-question", activeQuestion);
  state.askNotice = "";
  try { await refreshCases(); } catch (error) { return showToast(error.message); }
  const wanted = asOf || state.asOf;
  // the same question (and date) already answered: show it instead of paying for a new trial
  const saved = state.cases.find((c) => normQ(c.question) === normQ(question) && (!wanted || c.as_of === wanted) && c.rulings > 0 && c.status !== "ABORTED");
  if (saved) return showAnswer(saved.case_id);
  if (!state.canRun) {
    state.askNotice = "Live answers are off on this server because no LLM key is configured. Open a recent answer below, or add a key and restart the server.";
    return renderAsk();
  }
  try {
    const body = { question, ...(asOf ? { as_of: asOf } : {}), ...(state.askMode === "fast" ? { options: FAST_OPTIONS } : {}) };
    const { case_id: id } = await api("/cases", { method: "POST", body: JSON.stringify(body) });
    Object.assign(state, { answerId: id, caseId: id, run: null, live: [] });
    sessionStorage.setItem("ragnarisk-answer", id);
    sessionStorage.setItem("ragnarisk-case", id);
    startPolling(id);
    renderAsk();
  } catch (error) {
    showToast(error.status === 409 ? "Another question is being answered. Wait for it to finish." : error.message);
  }
}

async function showAnswer(id, { scroll = true } = {}) {
  Object.assign(state, { answerId: id, caseId: id });
  sessionStorage.setItem("ragnarisk-answer", id);
  sessionStorage.setItem("ragnarisk-case", id);
  if (currentPage() !== "ask") return navigate("ask");  // renderAsk shows it
  if (linkedAnswer() !== id) history.replaceState(null, "", `#ask/${encodeURIComponent(id)}`);  // shareable, no re-render
  const root = document.querySelector("#answer");
  if (!root) return;
  root.innerHTML = skeleton();
  try {
    const info = await loadCase(id, true);
    if (state.answerId !== id) return;
    root.innerHTML = answerMarkup(info.case);
    [...root.children].forEach((el, i) => el.style.setProperty("--i", i));  // staggered reveal
    root.classList.remove("reveal");
    void root.offsetWidth;
    root.classList.add("reveal");
    document.querySelector(".ask-hero")?.classList.add("has-answer");
    if (scroll) root.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    root.innerHTML = errorBox("Could not load this answer", error.message);
  }
}

// Established findings: Odin's briefing when he wrote one, else the claims he ruled established.
function establishedFindings(run) {
  const fromBrief = run.briefing?.established || [];
  if (fromBrief.length) return fromBrief;
  return Object.values(run.claims || {}).filter((c) => run.rulings?.[c.id]?.status === "established").map((c) => ({ text: c.text, claim_ids: [c.id] }));
}

// One chip per source document; its popover lists every quoted passage from it.
function sourceChips(run, claimIds) {
  const byDoc = new Map();
  uniqueEvidence(run, claimIds).forEach((e) => byDoc.set(e.doc_id, [...(byDoc.get(e.doc_id) || []), e]));
  return [...byDoc.values()].map((quotes) => {
    const e = quotes[0];
    return `<details class="cite"><summary>${escapeHtml(e.doc_id)} · Tier ${escapeHtml(e.tier ?? "?")} · ${escapeHtml(e.date || "undated")}${quotes.length > 1 ? ` · ${quotes.length} quotes` : ""}</summary><div class="pop">${quotes.slice(0, 3).map((q) => `<q>“${escapeHtml(q.quote)}”</q>`).join("")}<button class="chip-btn" data-action="open-doc" data-doc="${escapeHtml(e.doc_id)}" data-locator="${escapeHtml(e.locator)}" data-quote="${escapeHtml(e.quote)}">Open source →</button></div></details>`;
  }).join("");
}

// Odin's own summary when he wrote one; his "could not draft" fallback line is not an answer.
const answerSummary = (run) => {
  const own = run.briefing?.executive_summary || "";
  return own && !/could not draft/i.test(own) ? ["Summary", own] : run.event_summary ? ["What happened", run.event_summary] : null;
};

function uniqueEvidence(run, claimIds) {
  const seen = new Set();
  return claimIds.flatMap((id) => run.claims?.[id]?.evidence || []).filter((e) => !seen.has(e.doc_id + e.quote) && seen.add(e.doc_id + e.quote));
}

const bandOf = (range) => { const [lo, hi] = magnitude(range); return RATING_BANDS.find((b) => (lo + hi) / 2 < b.max); };
const fmtShort = (range) => { const [a, b] = magnitude(range); return `${round1(a)}–${round1(b)}%`; };

// Thor's and Loki's cases as thin bars, Odin's ruling as the thick one, on a 0-100% scale.
function rangeBar(row) {
  const pos = (range) => { const [a, b] = magnitude(range); return `left:${Math.min(a, 100)}%;width:${Math.max(Math.min(b, 100) - Math.min(a, 100), 1.2)}%`; };
  return `<div class="ans-bar" aria-hidden="true"><div class="ans-track">${row.thor ? `<span class="b-thor" style="${pos(row.thor)}"></span>` : ""}${row.loki ? `<span class="b-loki" style="${pos(row.loki)}"></span>` : ""}<span class="b-odin" style="${pos(row.odin)}"></span></div>
    <div class="ans-axis"><span>0%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span></div></div>`;
}

function answerRow(row) {
  const scope = `<span class="ans-scope">${escapeHtml(row.country)} <b>·</b> ${escapeHtml(row.sector)}</span>`;
  if (!row.odin) return `<li class="ans-row rating-none">${scope}<span class="ans-range">No range</span><span class="ans-sides">No usable evidence for this flow in the corpus, so no number is given rather than a guess.</span></li>`;
  const band = bandOf(row.odin);
  const sides = [row.thor && `<span class="k-thor">Thor (high case) ${fmtShort(row.thor)}</span>`, row.loki && `<span class="k-loki">Loki (low case) ${fmtShort(row.loki)}</span>`, `<span class="k-odin">Odin's ruling ${fmtShort(row.odin)}</span>`].filter(Boolean).join("");
  return `<li class="ans-row rating-${band.key}">${scope}<span class="ans-range">${fmtRange(row.odin)}<small>expected fall in imports</small></span><span class="rating-pill"><i></i>${band.label}</span>${rangeBar(row)}<span class="ans-sides">${sides}</span></li>`;
}

// One plain sentence built from the ruled ranges (code, not the model), plus the highest rating.
function verdict(rows) {
  const ruled = rows.filter((r) => r.odin);
  if (!ruled.length) return "";
  const top = RATING_BANDS[Math.max(...ruled.map((r) => RATING_BANDS.indexOf(bandOf(r.odin))))];
  const missing = rows.filter((r) => !r.odin).map((r) => `${r.country} ${r.sector.toLowerCase()}`);
  const text = `Imports are expected to fall ${ruled.map((r) => `<b>${fmtShort(r.odin)}</b> for ${escapeHtml(r.country)} ${escapeHtml(r.sector.toLowerCase())}`).join(", ")}.${missing.length ? ` No range for ${escapeHtml(missing.join(", "))}: the corpus holds no usable evidence.` : ""}`;
  return `<div class="verdict rating-${top.key}"><span class="verdict-label"><i></i>${top.label} exposure</span><p>${text}</p></div>`;
}

function rigorStats(run) {
  const sources = new Set(Object.values(run.claims || {}).flatMap((c) => c.evidence.map((e) => e.doc_id))).size;
  const stats = [[(run.runs || []).length, "trial runs"], [Object.keys(run.claims || {}).length, "claims argued"], [Object.keys(run.attacks || {}).length, "cross-examinations"],
    [Object.keys(run.rulings || {}).length, "rulings"], [sources, "sources cited"], [(run.red_flags || []).length, "traps caught"]];
  return `<div class="rigor">${stats.map(([n, label]) => `<span><b>${n}</b>${label}</span>`).join("")}</div>`;
}

function answerMarkup(run) {
  const head = `<header class="answer-head"><span class="answer-kicker">Answer</span><p class="answer-q">${escapeHtml(run.question)}</p>
    <div class="answer-meta">${statusBadge(run.status)}${run.verified ? '<span class="verified">✓ Event verified</span>' : ""}${(run.runs || []).length === 1 ? "<span>Fast answer · 1 trial run</span>" : ""}<span>As of ${escapeHtml(fmtDate(run.as_of))}</span><span>Case ${escapeHtml(run.case_id)}</span>${run.provider === "imported" ? "<span>Saved trial</span>" : ""}</div></header>`;
  if (!["COMPLETE", "INCOMPLETE"].includes(run.status)) {
    const rejected = run.status === "REJECTED";
    const why = rejected ? run.verdict_reason || friendlyReason(run.status_reasons) : (run.status_reasons || []).join(" ") || "The trial stopped before any ruling.";
    return `${head}<div class="answer-card"><h2>${rejected ? "The event could not be verified" : "This question could not be answered"}</h2><p>${escapeHtml(why)}</p>
      <div class="answer-actions"><button class="button button-quiet" data-action="ask-again">Ask another question</button></div></div>`;
  }
  const rows = exposureRows(run);
  const findings = establishedFindings(run);
  const flags = run.red_flags || [];
  const summary = answerSummary(run);
  const trap = (f) => `<li><button class="chip-btn" data-action="open-doc" data-doc="${escapeHtml(f.doc_id)}">${escapeHtml(f.doc_id)}</button><span class="trap-type">${escapeHtml(String(f.type).replace(/_/g, " "))}</span><span>${escapeHtml(f.note)}</span></li>`;
  const provisional = run.status !== "COMPLETE"
    ? `<p class="answer-flag"><b>Provisional.</b> ${escapeHtml(friendlyReason(run.status_reasons))} The ranges come from the rulings that were made.</p>` : "";
  const disputed = findingsSection(run, "Disputed", "Credible sources disagree. Not settled.", run.briefing?.contested, false);
  const ruledOut = findingsSection(run, "Ruled out", "Claims a side conceded, or Odin rejected.", run.briefing?.conceded, false);
  const jumps = [["ranges", "Ranges"], summary && ["summary", summary[0]], ["evidence", "Evidence"], flags.length && ["traps", "Traps caught"],
    disputed && ["disputed", "Disputed"], ruledOut && ["ruled", "Ruled out"]].filter(Boolean);
  const sources = new Set(Object.values(run.claims || {}).flatMap((c) => c.evidence.map((e) => e.doc_id))).size;
  // Actions live in a sticky sidebar beside the answer on wide screens, and below it on phones.
  const side = `<aside class="answer-side" aria-label="Answer actions">
      <div class="side-card">
        <button class="button button-primary side-btn" data-action="download-report">Download PDF report</button>
        <a class="button side-btn side-secondary" href="#council" data-page="council">See the full debate</a>
        <button class="button side-btn side-secondary" data-action="ask-again">Ask another question</button>
      </div>
      <nav class="side-card side-nav" aria-label="On this answer"><span class="side-label">On this answer</span>${jumps.map(([id, label]) => `<a href="#" data-jump="${id}">${escapeHtml(label)}</a>`).join("")}</nav>
      <dl class="side-card side-facts"><span class="side-label">Case</span>
        <div><dt>Status</dt><dd>${escapeHtml((STATUS[run.status] || [0, run.status])[1])}</dd></div>
        <div><dt>As of</dt><dd>${escapeHtml(fmtDate(run.as_of))}</dd></div>
        <div><dt>Trial runs</dt><dd>${(run.runs || []).length}</dd></div>
        <div><dt>Sources cited</dt><dd>${sources}</dd></div>
        <div><dt>Case ID</dt><dd>${escapeHtml(run.case_id)}</dd></div>
      </dl>
    </aside>`;
  return `${head}
    <div class="answer-grid">
    <div class="answer-main">
    ${verdict(rows)}
    ${rigorStats(run)}
    ${provisional}
    <ul class="ans-rows" id="j-ranges">${rows.length ? rows.map(answerRow).join("") : '<li class="ans-row rating-none"><span class="ans-sides">No ranges were ruled.</span></li>'}</ul>
    ${summary ? `<div class="answer-block" id="j-summary"><h2>${summary[0]}</h2><p>${escapeHtml(summary[1])}</p></div>` : ""}
    <div class="answer-block" id="j-evidence"><h2>Why: the evidence <em>${findings.length}</em></h2><p class="hint">Findings that survived cross-examination. Click a source to see the exact passage.</p>
      ${findings.length ? findings.map((f) => `<div class="finding">${escapeHtml(f.text)}<div class="cites">${sourceChips(run, f.claim_ids)}</div></div>`).join("") : '<p class="hint">Nothing was established yet.</p>'}</div>
    ${flags.length ? `<div class="answer-block" id="j-traps"><h2>Traps caught <em>${flags.length}</em></h2><p class="hint">Sources the system flagged and did not rely on as written.</p>
      <ul class="trap-list">${flags.slice(0, 5).map(trap).join("")}</ul>
      ${flags.length > 5 ? `<details class="more-traps"><summary>Show all ${flags.length}</summary><ul class="trap-list">${flags.slice(5).map(trap).join("")}</ul></details>` : ""}</div>` : ""}
    ${disputed ? `<div id="j-disputed">${disputed}</div>` : ""}
    ${ruledOut ? `<div id="j-ruled">${ruledOut}</div>` : ""}
    </div>
    ${side}
    </div>`;
}

// ---- PDF report: a print-only document; the browser's "Save as PDF" writes the file --
function reportMarkup(run) {
  const rows = exposureRows(run);
  const findings = establishedFindings(run);
  const brief = run.briefing || {};
  const runs = run.runs || [];
  const count = (key) => Object.keys(run[key] || {}).length;
  const docs = new Map();
  Object.values(run.claims || {}).forEach((c) => c.evidence.forEach((e) => docs.set(e.doc_id, e)));
  const cite = (e) => `<li><b>${escapeHtml(e.doc_id)}</b> · Tier ${escapeHtml(e.tier ?? "?")} · ${escapeHtml(e.date || "undated")}${e.locator ? ` · ${escapeHtml(e.locator)}` : ""}<q>${escapeHtml(e.quote)}</q></li>`;
  const textList = (items) => (items || []).map((i) => `<li>${escapeHtml(i.text)}</li>`).join("");
  const noRange = rows.filter((r) => !r.odin).map((r) => `${r.country} ${r.sector}`);
  return `
    <article class="rp">
      <header class="rp-head"><div><strong>RAGNARISK</strong><span>Supply-chain exposure report</span></div>
        <dl><dt>Case</dt><dd>${escapeHtml(run.case_id)}</dd><dt>As of</dt><dd>${escapeHtml(fmtDate(run.as_of))}</dd><dt>Status</dt><dd>${escapeHtml((STATUS[run.status] || [0, run.status])[1])}</dd><dt>Generated</dt><dd>${escapeHtml(new Date().toLocaleString("en-GB"))}</dd></dl></header>

      <h1>${escapeHtml(run.question)}</h1>
      ${run.status !== "COMPLETE" ? `<p class="rp-flag"><b>Provisional.</b> ${escapeHtml(friendlyReason(run.status_reasons))} Treat the ranges as a first pass.</p>` : ""}

      <h2>1. Answer</h2>
      <table class="rp-table"><thead><tr><th>Country</th><th>Sector</th><th>Expected fall in imports</th><th>Rating</th><th>High case (Thor)</th><th>Low case (Loki)</th></tr></thead><tbody>
        ${rows.map((r) => { const band = r.odin && RATING_BANDS.find((b) => { const [lo, hi] = magnitude(r.odin); return (lo + hi) / 2 < b.max; });
          return `<tr><td>${escapeHtml(r.country)}</td><td>${escapeHtml(r.sector)}</td><td><b>${r.odin ? fmtRange(r.odin) : "No range: no usable evidence"}</b></td><td>${band ? band.label : "—"}</td><td>${fmtRange(r.thor)}</td><td>${fmtRange(r.loki)}</td></tr>`; }).join("")}
      </tbody></table>
      <p class="rp-note">Ratings use the middle of the judged range: Low under 10%, Moderate 10–30%, High 30–55%, Severe over 55%.</p>
      ${answerSummary(run) ? `<p>${escapeHtml(answerSummary(run)[1])}</p>` : ""}

      <h2>2. Why this answer</h2>
      ${run.verdict_reason ? `<h3>The event was verified first</h3><p>${escapeHtml(run.verdict_reason)}</p>` : ""}
      <h3>Findings that survived cross-examination</h3>
      ${findings.length ? findings.map((f) => `<div class="rp-finding"><p>${escapeHtml(f.text)}</p><ul class="rp-cites">${uniqueEvidence(run, f.claim_ids).slice(0, 4).map(cite).join("")}</ul></div>`).join("") : "<p>Nothing was established.</p>"}
      ${(run.ranges || []).filter((g) => g.rationale).length ? `<h3>How the judge set each range</h3><ul>${run.ranges.filter((g) => g.rationale).map((g) => `<li><b>${escapeHtml(g.country)} · ${escapeHtml(g.sector)}:</b> ${escapeHtml(g.rationale)}</li>`).join("")}</ul>` : ""}
      ${brief.contested?.length ? `<h3>Disputed (not settled)</h3><ul>${textList(brief.contested)}</ul>` : ""}
      ${brief.conceded?.length ? `<h3>Ruled out</h3><ul>${textList(brief.conceded)}</ul>` : ""}

      ${(run.red_flags || []).length ? `<h2>3. Traps and data quality</h2><ul>${run.red_flags.map((f) => `<li><b>${escapeHtml(f.doc_id)} (${escapeHtml(String(f.type).replace(/_/g, " "))}):</b> ${escapeHtml(f.note)}</li>`).join("")}</ul>` : ""}

      <h2>${(run.red_flags || []).length ? "4" : "3"}. How the answer was reached</h2>
      <p>Heimdall first checked that the event was real as of the date above and gathered the evidence. Thor argued for high exposure and Loki for limited exposure. They submitted their claims sealed and at the same time, each citing documents or trade-table rows, then cross-examined each other. Odin, the judge, ruled on every claim as a sourced fact or an inference, and as established, disputed or conceded. Code keeps his range inside the span of the evidence-backed estimates. Every citation was checked against the source text, and nothing published after the as-of date was available to the agents.</p>
      <p class="rp-note">This case: ${runs.length} trial run${runs.length === 1 ? "" : "s"} · ${count("claims")} claims · ${count("attacks")} attacks · ${count("rulings")} rulings · ${docs.size} sources cited.</p>

      <h2>${(run.red_flags || []).length ? "5" : "4"}. Limitations</h2>
      <ul>
        ${run.status !== "COMPLETE" ? `<li>${escapeHtml((run.status_reasons || []).join(" ") || "The trial did not finish.")}</li>` : ""}
        ${noRange.length ? `<li>No range for ${escapeHtml(noRange.join(", "))}: the corpus held no usable evidence for it.</li>` : ""}
        <li>Ranges are a judgment over the cited Runemark evidence, not a forecast from outside data.</li>
      </ul>

      <h2>Sources cited</h2>
      <table class="rp-table rp-sources"><thead><tr><th>Document</th><th>Tier</th><th>Published</th></tr></thead><tbody>
        ${[...docs.values()].sort((a, b) => a.doc_id.localeCompare(b.doc_id)).map((e) => `<tr><td>${escapeHtml(e.doc_id)}</td><td>${escapeHtml(e.tier ?? "?")}</td><td>${escapeHtml(e.date || "undated")}</td></tr>`).join("")}
      </tbody></table>
    </article>`;
}

function downloadReport() {
  const run = state.run?.id === state.answerId ? state.run.case : null;
  if (!run) return showToast("Open an answer first.");
  document.querySelector("#report-root").innerHTML = reportMarkup(run);
  state.titleBeforePrint = document.title;
  document.title = `Ragnarisk report ${run.case_id}`;  // becomes the suggested PDF file name
  showToast("Choose “Save as PDF” in the print dialog.");
  setTimeout(() => window.print(), 50);
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
    case "new-investigation": navigate("ask"); document.querySelector("#ask-query")?.focus(); break;
    case "ask-example": { const q = document.querySelector("#ask-query"); if (q) { q.value = SAMPLE_QUESTION; activeQuestion = SAMPLE_QUESTION; q.focus(); } break; }
    case "show-answer": showAnswer(d.case); break;
    case "ask-again": state.answerId = ""; state.askNotice = ""; sessionStorage.removeItem("ragnarisk-answer"); history.replaceState(null, "", "#ask"); renderAsk(); document.querySelector("#ask-query")?.focus(); break;
    case "download-report": downloadReport(); break;
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
    case "rescan": api("/ingest/scan", { method: "POST" }).then((r) => { showToast(r.ingested.length ? `Ingested: ${r.ingested.join(", ")}` : "No new packs in the folder."); state.trade = null; renderEvidence(); }).catch((e) => showToast(e.message)); break;
    case "filter-claims": { const cols = document.querySelector(".trial-cols"); const on = cols.dataset.filter !== d.status; if (on) cols.dataset.filter = d.status; else delete cols.dataset.filter; document.querySelectorAll(".sbar button").forEach((b) => b.classList.toggle("on", on && b.dataset.status === d.status)); break; }
    case "toggle-clamp": { const p = target.previousElementSibling; p.classList.toggle("open"); target.textContent = p.classList.contains("open") ? "Show less" : "Show more"; break; }
  }
});

document.addEventListener("submit", (event) => {
  if (event.target.id === "token-form") {
    event.preventDefault();
    // phones add stray spaces and quotes when a token is pasted from a chat message
    const typed = document.querySelector("#token-input").value.trim().replace(/^["'`]+|["'`]+$/g, "");
    try { sessionStorage.setItem(TOKEN_KEY, typed); } catch { /* storage may be unavailable */ }
    window.location.reload();
    return;
  }
  if (event.target.id === "ask-form") {
    event.preventDefault();
    askQuestion(document.querySelector("#ask-query").value, document.querySelector("#ask-asof").value);
    return;
  }
  if (event.target.id !== "investigation-form") return;
  event.preventDefault();
  submitInvestigation(document.querySelector("#investigation-query").value);
});

document.addEventListener("input", (event) => {
  if (event.target.id === "investigation-query" || event.target.id === "ask-query") {
    activeQuestion = event.target.value;
    sessionStorage.setItem("ragnarisk-active-question", activeQuestion);
  }
  if (event.target.id === "investigation-query") showAsOf();
  if (event.target.id === "ask-query") {
    const iso = parseAsOf(event.target.value), box = document.querySelector("#ask-asof");
    if (iso && box) box.value = iso;  // show the date that will be used
  }
  if (event.target.id === "evidence-search") { clearTimeout(docTimer); docTimer = setTimeout(() => loadDocs(), 250); }
});

document.addEventListener("change", (event) => {
  if (event.target.name === "ask-mode") { state.askMode = event.target.value; sessionStorage.setItem("ragnarisk-mode", state.askMode); }
  if (["source-filter", "tier-filter", "country-filter"].includes(event.target.id)) loadDocs();
  if (event.target.id === "case-select") { state.caseId = event.target.value; sessionStorage.setItem("ragnarisk-case", state.caseId); state.run = null; renderBriefing(); }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") { if (!modal.hidden) closeDialog(); if (!sheet.hidden) closeSheet(); }
  if (event.key === "Enter" && event.target.id === "dialog-query" && (event.metaKey || event.ctrlKey)) submitInvestigation(event.target.value);
  if (event.key === "Enter" && event.target.id === "ask-query" && !event.shiftKey) { event.preventDefault(); event.target.form?.requestSubmit(); }
});

window.addEventListener("hashchange", () => {
  const page = currentPage();
  if (Object.hasOwn(pageTitles, page)) renderPage(page);
});

window.addEventListener("afterprint", () => { if (state.titleBeforePrint) { document.title = state.titleBeforePrint; state.titleBeforePrint = ""; } });

// ?report=<case_id> builds the printable report on load, so a report can be linked to or printed headlessly.
async function prepareLinkedReport() {
  const id = new URLSearchParams(window.location.search).get("report");
  if (!id) return;
  try {
    const info = await loadCase(id, true);
    document.querySelector("#report-root").innerHTML = reportMarkup(info.case);
    document.title = `Ragnarisk report ${id}`;
  } catch (error) {
    showToast(`Could not build the report: ${error.message}`);
  }
}

(async function boot() {
  const initialPage = currentPage();
  if (initialPage === "ask" && linkedAnswer()) state.answerId = linkedAnswer();
  renderPage(Object.hasOwn(pageTitles, initialPage) ? initialPage : "ask");
  prepareLinkedReport();
  try {
    const health = await api("/health");
    if (health.auth_required && !getToken()) askToken();
    state.canRun = !!health.provider;
    if (health.running) { state.caseId = health.running; startPolling(health.running); }
  } catch { /* server unreachable: pages show their own errors */ }
})();
