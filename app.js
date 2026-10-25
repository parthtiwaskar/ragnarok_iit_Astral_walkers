const corpusPaths = [
  "./data/runemark/manifest.json",
  "./03_Ragnarok%20Trial%20of%20the%20Nine%20Realms/dev_pack_E5/manifest.json",
];
const content = document.querySelector("#page-content");
const overviewMarkup = content.innerHTML;
const breadcrumb = document.querySelector("#breadcrumb-current");
const modal = document.querySelector("#modal-backdrop");
const toast = document.querySelector("#toast");
let toastTimeout;
let documents = [];
let activeQuestion = sessionStorage.getItem("ragnarisk-active-question") || document.querySelector("#investigation-query").value;

const pageTitles = {
  overview: "Overview",
  investigations: "Investigations",
  evidence: "Evidence explorer",
  council: "Agent council",
  briefing: "Executive briefing",
};

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => toast.classList.remove("show"), 3200);
}

function navigate(page) {
  if (!pageTitles[page]) return;
  if (page === "overview") {
    window.location.hash = "overview";
    if (breadcrumb.textContent === "Overview") renderPage(page);
    return;
  }
  window.location.hash = page;
  renderPage(page);
}

function renderPage(page) {
  if (!Object.hasOwn(pageTitles, page)) return;
  breadcrumb.textContent = pageTitles[page];
  document.querySelectorAll(".nav-link[data-page]").forEach((link) => {
    link.classList.toggle("active", link.dataset.page === page);
  });
  if (page === "overview") {
    content.innerHTML = overviewMarkup;
    const query = document.querySelector("#investigation-query");
    if (query) query.value = activeQuestion;
    return;
  }
  const pages = {
    investigations: renderInvestigations,
    evidence: renderEvidence,
    council: renderCouncil,
    briefing: renderBriefing,
  };
  pages[page]();
}

function renderInvestigations() {
  content.innerHTML = `
    <section class="page-heading">
      <div><div class="eyebrow"><span class="live-dot"></span> WORKSPACE <span class="eyebrow-divider">/</span> CASE REGISTER</div><h1>Investigations</h1><p class="page-subtitle">Track disruption cases, trial runs, and adjudicated findings.</p></div>
      <div class="heading-actions"><span class="asof-pill"><span class="calendar-icon">▦</span> 01 CASE IN WORKSPACE</span><button class="button button-primary" data-action="new-investigation">＋ New investigation</button></div>
    </section>
    <section class="panel register-panel">
      <div class="register-toolbar"><label class="register-search"><span>⌕</span><input id="case-search" type="search" placeholder="Filter investigations..." aria-label="Filter investigations"></label><span class="outline-pill">ALL CASES <strong>01</strong></span></div>
      <div class="table-scroll"><table class="investigations-table"><thead><tr><th>CASE ID &amp; INVESTIGATION</th><th>COUNTRIES</th><th>TRADE FLOWS</th><th>TRIALS</th><th>STATUS</th><th>AS-OF DATE</th><th></th></tr></thead><tbody id="case-rows">
        <tr data-case-row><td><div class="table-case-title"><span class="table-case-icon">↗</span><span><strong>Orvan grain corridor collapse</strong><small>E5 · Supply-chain disruption</small></span></div></td><td>Midgard · Tolan</td><td><span class="focus-tag">Wheat</span><span class="focus-tag">Maize</span></td><td class="date-cell">0 / 2 minimum</td><td><span class="status-badge status-pending"><span></span> Not started</span></td><td class="date-cell">25 Jan 2027</td><td><button class="row-arrow" data-action="open-case" aria-label="Open Orvan grain corridor investigation">↗</button></td></tr>
      </tbody></table></div>
      <div class="table-footer"><span>1 investigation · 76 documents in the E5 corpus</span><span>AS-OF DATE LOCKED · 25 JAN 2027</span></div>
    </section>
    <section class="register-note panel"><span class="note-icon">i</span><div><strong>Findings are not available yet.</strong><p>This workspace contains the E5 question and source corpus. Run the investigation to retrieve evidence and begin the adversarial trial.</p></div><button class="button button-primary" data-action="new-investigation">Start investigation <span>→</span></button></section>`;
}

function renderEvidence() {
  content.innerHTML = `
    <section class="page-heading">
      <div><div class="eyebrow"><span class="live-dot"></span> INTELLIGENCE <span class="eyebrow-divider">/</span> SOURCE REGISTER</div><h1>Evidence explorer</h1><p class="page-subtitle">Browse the E5 source manifest. Corpus records are not findings until reviewed in trial.</p></div>
      <div class="heading-actions"><span class="asof-pill"><span class="calendar-icon">▤</span> <strong id="evidence-count">${documents.length || 76}</strong> MANIFEST RECORDS</span></div>
    </section>
    <section class="panel evidence-panel">
      <div class="evidence-toolbar"><label class="register-search"><span>⌕</span><input id="evidence-search" type="search" placeholder="Search ID, title, source, country, or sector..." aria-label="Search corpus records"></label><select id="source-filter" aria-label="Filter by source group"><option value="">All source groups</option><option value="ATL">ATL · Runemark Atlas</option><option value="ORV">ORV · Regional notices</option><option value="WLD">WLD · General newswire</option></select><span class="integrity-pill"><span class="status-dot green-dot"></span> MANIFEST SHA-256</span></div>
      <div class="table-scroll"><table class="investigations-table evidence-table"><thead><tr><th>DOCUMENT ID</th><th>TITLE</th><th>SOURCE</th><th>COUNTRY</th><th>SECTOR</th><th>RELIABILITY</th><th>PUBLICATION DATE</th></tr></thead><tbody id="evidence-rows"><tr><td colspan="7" class="loading-row">Loading source manifest…</td></tr></tbody></table></div>
      <div class="table-footer"><span id="evidence-result-count">Loading source records…</span><span>AS-OF DATE · 25 JAN 2027</span></div>
    </section>
    <section class="source-hygiene panel"><span class="hygiene-icon">◇</span><div><strong>Source hygiene is part of the trial.</strong><p>Outlet reliability, retractions, decoys, and publication dates must be checked before claims are admitted as established evidence.</p></div><button data-page="council">See review process <span>→</span></button></section>`;
  loadManifest();
}

async function loadManifest() {
  let lastError;
  try {
    for (const corpusPath of corpusPaths) {
      try {
        const response = await fetch(corpusPath);
        if (!response.ok) throw new Error(`Manifest request failed (${response.status}): ${corpusPath}`);
        const manifest = await response.json();
        if (!Array.isArray(manifest.documents)) throw new Error(`The source manifest has no document list: ${corpusPath}`);
        documents = manifest.documents;
        renderEvidenceRows(documents);
        return;
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError || new Error("No source manifest paths are configured.");
  } catch (error) {
    const rows = document.querySelector("#evidence-rows");
    const resultCount = document.querySelector("#evidence-result-count");
    if (rows) rows.innerHTML = `<tr><td colspan="7" class="loading-row error-row">Could not load the source manifest. ${escapeHtml(error.message)} Open this UI from a local web server to browse corpus records.</td></tr>`;
    if (resultCount) resultCount.textContent = "Source manifest unavailable";
  }
}

function renderEvidenceRows(records) {
  const rows = document.querySelector("#evidence-rows");
  if (!rows) return;
  const search = document.querySelector("#evidence-search")?.value.trim().toLowerCase() ?? "";
  const group = document.querySelector("#source-filter")?.value ?? "";
  const filtered = records.filter((record) => {
    const values = [record.DOC_ID, record.TITLE, record.SOURCE_NAME, record.COUNTRY, record.SECTOR, record.SOURCE_TYPE].join(" ").toLowerCase();
    return values.includes(search) && (!group || record.DOC_ID.startsWith(group));
  });
  if (!filtered.length) {
    rows.innerHTML = '<tr><td colspan="7" class="loading-row">No source records match this filter.</td></tr>';
  } else {
    rows.innerHTML = filtered.slice(0, 100).map((record) => `
      <tr>
        <td class="document-id">${escapeHtml(record.DOC_ID)}</td>
        <td><strong class="evidence-title">${escapeHtml(record.TITLE)}</strong><small class="evidence-format">${escapeHtml(record.FORMAT || "Document")} · ${escapeHtml(record.LANGUAGE || "en")}</small></td>
        <td>${escapeHtml(record.SOURCE_NAME || "Not specified")}</td>
        <td>${escapeHtml(record.COUNTRY || "—")}</td>
        <td><span class="focus-tag">${escapeHtml(record.SECTOR || "general")}</span></td>
        <td><span class="reliability-tag">TIER ${escapeHtml(record.RELIABILITY || "—")}</span></td>
        <td class="date-cell">${escapeHtml(record.PUB_DATE || "—")}</td>
      </tr>`).join("");
  }
  const resultCount = document.querySelector("#evidence-result-count");
  if (resultCount) resultCount.textContent = `Showing ${Math.min(filtered.length, 100)} of ${filtered.length} matching records · ${records.length} total in E5`;
  const count = document.querySelector("#evidence-count");
  if (count) count.textContent = String(records.length);
}

function renderCouncil() {
  content.innerHTML = `
    <section class="page-heading">
      <div><div class="eyebrow"><span class="live-dot"></span> INTELLIGENCE <span class="eyebrow-divider">/</span> ADVERSARIAL REVIEW</div><h1>Agent council</h1><p class="page-subtitle">Independent agents verify, challenge, and adjudicate each disruption case.</p></div>
      <div class="heading-actions"><span class="asof-pill"><span class="calendar-icon">◷</span> AS OF <strong>25 JAN 2027</strong></span><span class="outline-pill"><span class="status-dot cyan-dot"></span> 4 AGENTS READY</span></div>
    </section>
    <section class="council-flow">
      <article class="council-agent panel"><div class="council-agent-heading"><span class="agent-avatar heimdall">H</span><span class="agent-ready"><i></i> READY</span></div><span class="section-kicker">PHASE 01 · GATEKEEPER</span><h2>Heimdall</h2><h3>Event verification</h3><p>Checks that the disruption is real and active as of the investigation date. Filters decoys, retractions, planted instructions, and clock violations before a case is admitted.</p><div class="responsibility"><span>RESPONSIBILITY</span><strong>Establish whether the event is real.</strong></div></article>
      <div class="council-branch"><article class="council-agent panel"><div class="council-agent-heading"><span class="agent-avatar thor">T</span><span class="agent-ready"><i></i> READY</span></div><span class="section-kicker">PHASE 02 · PROSECUTION</span><h2>Thor</h2><h3>High-exposure case</h3><p>Builds a specific, document-grounded case for higher commercial exposure, then responds to counter-evidence during cross-examination.</p><div class="responsibility"><span>POSITION</span><strong>Argue the strongest supported high case.</strong></div></article>
        <article class="council-agent panel"><div class="council-agent-heading"><span class="agent-avatar loki">L</span><span class="agent-ready"><i></i> READY</span></div><span class="section-kicker">PHASE 02 · DEFENSE</span><h2>Loki</h2><h3>Limited-exposure case</h3><p>Challenges inflated assumptions with route alternatives, price adjustments, and source quality checks. Concedes claims when the evidence warrants it.</p><div class="responsibility"><span>POSITION</span><strong>Argue the strongest supported low case.</strong></div></article></div>
      <article class="council-agent panel"><div class="council-agent-heading"><span class="agent-avatar odin">O</span><span class="agent-ready"><i></i> READY</span></div><span class="section-kicker">PHASE 03 · JUDICIARY</span><h2>Odin</h2><h3>Judge &amp; briefing author</h3><p>Rules on claims as Established, Contested, or Conceded; classifies facts and inferences; requests another retrieval round when evidence is insufficient.</p><div class="responsibility"><span>RESPONSIBILITY</span><strong>Author the briefing from rulings only.</strong></div></article>
    </section>
    <section class="panel trial-progress"><div class="section-heading"><div><span class="section-kicker">REPRODUCIBILITY REQUIREMENT</span><h2>Trial run record</h2></div><span class="outline-pill">0 / 2 RUNS COMPLETE</span></div><div class="trial-slots"><div><span class="trial-number">01</span><span><strong>Trial run one</strong><small>Pending investigation start</small></span><span class="status-badge status-pending"><span></span> Pending</span></div><div><span class="trial-number">02</span><span><strong>Trial run two</strong><small>Minimum second evidence pass</small></span><span class="status-badge status-pending"><span></span> Pending</span></div></div><button class="button button-primary" data-action="new-investigation">Start the investigation <span>→</span></button></section>`;
}

function renderBriefing() {
  content.innerHTML = `
    <section class="page-heading">
      <div><div class="eyebrow"><span class="live-dot"></span> OUTPUTS <span class="eyebrow-divider">/</span> ODIN'S RULINGS</div><h1>Executive briefing</h1><p class="page-subtitle">Decision-ready intelligence, authored from adjudicated claims with traceable citations.</p></div>
      <div class="heading-actions"><span class="asof-pill"><span class="calendar-icon">▦</span> AS OF <strong>25 JAN 2027</strong></span><button class="button button-quiet" data-action="export-briefing" disabled>Export briefing</button></div>
    </section>
    <section class="briefing-empty panel"><div class="empty-seal">◇</div><span class="section-kicker">E5 · ORVAN GRAIN CORRIDOR</span><h2>No adjudicated briefing yet.</h2><p>Odin's briefing will appear here after event verification, evidence retrieval, opposing arguments, cross-examination, and judicial rulings are complete.</p><div class="briefing-checklist"><span><i>01</i> Verify event</span><span><i>02</i> Retrieve sources</span><span><i>03</i> Debate both cases</span><span><i>04</i> Record rulings</span></div><button class="button button-primary" data-action="new-investigation">Begin investigation <span>→</span></button></section>
    <section class="briefing-outline panel"><div class="section-heading"><div><span class="section-kicker">BRIEFING FORMAT</span><h2>What Odin will deliver</h2></div><span class="outline-pill">CITATION-LED</span></div><div class="briefing-parts"><article><span>01</span><div><strong>Event verification</strong><p>Event status at the as-of date, supported by verified sources.</p></div></article><article><span>02</span><div><strong>Exposure ranges</strong><p>Separate low and high wheat and maize import ranges for Midgard and Tolan: Thor, Loki, and Odin.</p></div></article><article><span>03</span><div><strong>Executive assessment</strong><p>Paragraph-level citations, material uncertainties, concessions, and a next-quarter outlook.</p></div></article></div></section>`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function openDialog() {
  modal.hidden = false;
  document.querySelector("#dialog-query").focus();
}

function closeDialog() {
  modal.hidden = true;
}

function submitInvestigation(question) {
  if (!question.trim()) {
    showToast("Enter an investigation question to continue.");
    return;
  }
  closeDialog();
  activeQuestion = question.trim();
  sessionStorage.setItem("ragnarisk-active-question", activeQuestion);
  navigate("council");
  showToast("Investigation setup is ready. Agent execution requires a connected backend.");
}

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-page], [data-action]");
  if (!target) return;
  if (target.dataset.page) {
    event.preventDefault();
    navigate(target.dataset.page);
    return;
  }
  switch (target.dataset.action) {
    case "new-investigation":
      openDialog();
      break;
    case "close-dialog":
      closeDialog();
      break;
    case "clear-query":
      document.querySelector("#investigation-query").value = "";
      break;
    case "submit-dialog":
      submitInvestigation(document.querySelector("#dialog-query").value);
      break;
    case "open-case":
      navigate("council");
      break;
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
  if (event.target.id === "evidence-search") renderEvidenceRows(documents);
  if (event.target.id === "case-search") {
    const row = document.querySelector("[data-case-row]");
    if (row) row.hidden = !row.textContent.toLowerCase().includes(event.target.value.toLowerCase());
  }
});

document.addEventListener("change", (event) => {
  if (event.target.id === "source-filter") renderEvidenceRows(documents);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !modal.hidden) closeDialog();
  if (event.key === "Enter" && event.target.id === "dialog-query" && (event.metaKey || event.ctrlKey)) {
    submitInvestigation(event.target.value);
  }
});

window.addEventListener("hashchange", () => {
  const page = window.location.hash.slice(1) || "overview";
  if (Object.hasOwn(pageTitles, page)) renderPage(page);
});

const initialPage = window.location.hash.slice(1);
if (Object.hasOwn(pageTitles, initialPage) && initialPage !== "overview") renderPage(initialPage);
