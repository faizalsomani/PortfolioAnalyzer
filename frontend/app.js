const state = {
  activePage: "workflow",
  config: null,
  sectors: [],
  companies: [],
  queue: [],
  selectedCompanyId: null,
  promptTemplate: null,
  draftApprovals: {
    newSection: null,
    existingSections: {},
    template: null,
  },
  draftTestResult: null,
  templateTestResult: null,
};

const SECTION_TYPES = [
  "metric_cards",
  "key_value_table",
  "time_series_table",
  "narrative",
  "risk_list",
  "scenario_inputs",
];

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const payload = await response.json();
      message = payload.detail || message;
    } catch (_) {
      // Keep status text.
    }
    throw new Error(message);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fmt(value) {
  if (value === null || value === undefined || value === "") return "Not found";
  if (typeof value === "number") return value.toLocaleString();
  if (Array.isArray(value)) return value.map((item) => fmt(item)).join("; ");
  if (typeof value === "object") {
    if (Object.prototype.hasOwnProperty.call(value, "value")) {
      const rendered = fmt(value.value);
      return value.unit && rendered !== "Not found" ? `${rendered} ${value.unit}` : rendered;
    }
    return Object.entries(value)
      .filter(([key]) => !["evidence", "confidence", "document_id", "page", "snippet"].includes(key))
      .map(([key, item]) => `${labelText(key, key)}: ${fmt(item)}`)
      .join("; ");
  }
  return String(value);
}

function labelText(value, fallback = "Metric") {
  return String(value || fallback)
    .replaceAll("_", " ")
    .replaceAll("-", " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function cleanStatus(value) {
  return String(value || "unknown").replaceAll("_", " ");
}

function pill(text, tone = "") {
  return `<span class="pill ${tone}">${escapeHtml(text)}</span>`;
}

function statusTone(status) {
  const value = String(status || "");
  if (value.includes("failed")) return "bad";
  if (value.includes("processed")) return "good";
  return "warn";
}

function setNotice(message) {
  $("globalNotice").textContent = message || "";
  $("syncResult").textContent = message || "";
}

function setPage(page) {
  state.activePage = page;
  document.querySelectorAll(".page").forEach((el) => {
    el.classList.toggle("active", el.id === `page-${page}`);
  });
  document.querySelectorAll("[data-page-link]").forEach((el) => {
    el.classList.toggle("active", el.dataset.pageLink === page);
  });
}

async function loadAll() {
  const [config, sectors, queue, companies, promptTemplate] = await Promise.all([
    api("/api/config/status"),
    api("/api/sectors"),
    api("/api/processing/queue"),
    api("/api/companies"),
    api("/api/prompt-template"),
  ]);
  state.config = config;
  state.sectors = sectors;
  state.queue = queue;
  state.companies = companies;
  state.promptTemplate = promptTemplate;
  renderConfig();
  renderPromptTemplate();
  renderSectorSelects();
  renderQueue();
  renderCompanies();
  renderCompanySelects();
}

function renderConfig() {
  const { config } = state;
  const modeTone = config.mode === "openai" ? "good" : "warn";
  $("configStatus").innerHTML = [
    pill(`Mode: ${config.mode}`, modeTone),
    pill(`USE_AI: ${config.use_ai ? "true" : "false"}`, config.use_ai ? "good" : "warn"),
    pill(`Key: ${config.openai_key_configured ? "configured" : "empty"}`, config.openai_key_configured ? "good" : "warn"),
    pill(`Model: ${config.openai_model_configured ? "configured" : "empty"}`, config.openai_model_configured ? "good" : "warn"),
  ].join("");
  $("folderStatus").textContent = `Input: ${config.input_dir}`;
  $("useAiInput").checked = Boolean(config.use_ai);
  $("modelInput").value = config.openai_model || "";
}

function renderPromptTemplate() {
  if (!state.promptTemplate) return;
  $("templateDescription").value = state.promptTemplate.description || "";
  $("templateText").value = state.promptTemplate.template_text || "";
  $("templateVariables").innerHTML = (state.promptTemplate.variables || [])
    .map((name) => pill(`{{${name}}}`))
    .join("");
  updateTemplateSaveGuard();
}

function renderSectorSelects() {
  const selectedSectionSector = $("sectionSector").value;
  const selectedEditSector = $("editSector").value;
  const selectedTemplateSector = $("templateTestSector").value;
  const options = state.sectors
    .map((sector) => `<option value="${sector.id}">${escapeHtml(sector.name)}</option>`)
    .join("");
  $("sectionSector").innerHTML = options;
  $("editSector").innerHTML = options;
  $("templateTestSector").innerHTML = options;
  if (selectedSectionSector && state.sectors.some((sector) => String(sector.id) === selectedSectionSector)) {
    $("sectionSector").value = selectedSectionSector;
  }
  if (selectedEditSector && state.sectors.some((sector) => String(sector.id) === selectedEditSector)) {
    $("editSector").value = selectedEditSector;
  }
  if (selectedTemplateSector && state.sectors.some((sector) => String(sector.id) === selectedTemplateSector)) {
    $("templateTestSector").value = selectedTemplateSector;
  }
  if (state.sectors[0] && !$("editSector").value) {
    $("editSector").value = String(state.sectors[0].id);
  }
  if (state.sectors[0] && !$("templateTestSector").value) {
    $("templateTestSector").value = String(state.sectors[0].id);
  }
  renderSectorGuidance($("editSector").value || state.sectors[0]?.id);
  renderPromptSections($("editSector").value || state.sectors[0]?.id);
}

function renderSectorGuidance(sectorId) {
  const sector = state.sectors.find((item) => String(item.id) === String(sectorId));
  $("sectorGuidanceText").value = sector?.description || "";
}

function renderCompanySelects() {
  const selectedCompany = $("testCompany").value;
  const selectedTemplateCompany = $("templateTestCompany").value;
  const options = state.companies
    .map((company) => `<option value="${company.id}">${escapeHtml(company.display_name)}</option>`)
    .join("");
  $("testCompany").innerHTML = options || `<option value="">No companies available</option>`;
  $("templateTestCompany").innerHTML = options || `<option value="">No companies available</option>`;
  if (selectedCompany && state.companies.some((company) => String(company.id) === selectedCompany)) {
    $("testCompany").value = selectedCompany;
  }
  if (selectedTemplateCompany && state.companies.some((company) => String(company.id) === selectedTemplateCompany)) {
    $("templateTestCompany").value = selectedTemplateCompany;
  }
  refreshSaveGuards();
}

function renderQueue() {
  if (!state.queue.length) {
    $("queueList").innerHTML = `<div class="detail-empty">No companies found yet. Add PDFs under data/input or create demo data.</div>`;
    return;
  }

  $("queueList").innerHTML = state.queue
    .map((company) => {
      const sectorOptions = state.sectors
        .map(
          (sector) =>
            `<option value="${sector.id}" ${company.sector_id === sector.id ? "selected" : ""}>${escapeHtml(sector.name)}</option>`
        )
        .join("");
      return `
        <article class="queue-card">
          <div class="queue-row">
            <div>
              <strong>${escapeHtml(company.display_name)}</strong>
              <div class="meta-line">
                ${pill(cleanStatus(company.status), statusTone(company.status))}
                ${pill(`${company.document_count || 0} PDFs`)}
                ${company.sector_name ? pill(company.sector_name, "good") : pill("No sector", "warn")}
              </div>
            </div>
          </div>
          <label>
            Sector bundle
            <select data-sector-for="${company.id}">${sectorOptions}</select>
          </label>
          <div class="queue-actions">
            <button data-process="${company.id}">Process / reprocess</button>
            <button class="secondary" data-open="${company.id}">Open company</button>
          </div>
        </article>
      `;
    })
    .join("");

  document.querySelectorAll("[data-process]").forEach((button) => {
    button.addEventListener("click", () => processCompany(Number(button.dataset.process)));
  });
  document.querySelectorAll("[data-open]").forEach((button) => {
    button.addEventListener("click", () => {
      setPage("companies");
      showCompany(Number(button.dataset.open));
    });
  });
}

function renderCompanies() {
  if (!state.companies.length) {
    $("companyList").innerHTML = `<div class="detail-empty">No persisted companies yet.</div>`;
    return;
  }
  $("companyList").innerHTML = state.companies
    .map(
      (company) => `
        <article class="company-card" data-company-card="${company.id}">
          <h3>${escapeHtml(company.display_name)}</h3>
          <div class="meta-line">
            ${pill(company.sector_name || "No sector", company.sector_name ? "good" : "warn")}
            ${pill(cleanStatus(company.status), statusTone(company.status))}
            ${pill(`${company.document_count || 0} PDFs`)}
            ${pill(`${company.section_count || 0} sections`)}
          </div>
        </article>
      `
    )
    .join("");
  document.querySelectorAll("[data-company-card]").forEach((card) => {
    card.addEventListener("click", () => showCompany(Number(card.dataset.companyCard)));
  });
}

function sectionTypeOptions(selected) {
  return SECTION_TYPES.map(
    (type) => `<option value="${type}" ${type === selected ? "selected" : ""}>${type}</option>`
  ).join("");
}

function renderPromptSections(sectorId) {
  if (!sectorId) {
    $("promptSections").innerHTML = "";
    return;
  }
  api(`/api/sectors/${sectorId}/sections`).then((sections) => {
    $("promptSections").innerHTML = sections
      .map(
        (section) => `
          <form class="editable-section" data-section-edit="${section.id}">
            <div class="panel-header">
              <h3>${escapeHtml(section.name)}</h3>
              ${pill(`v${section.version}`)}
            </div>
            <div class="edit-grid">
              <label>
                Name
                <input name="name" value="${escapeHtml(section.name)}" />
              </label>
              <label>
                Type
                <select name="section_type">${sectionTypeOptions(section.section_type)}</select>
              </label>
              <label>
                Display order
                <input name="display_order" type="number" value="${Number(section.display_order || 0)}" />
              </label>
              <label class="checkbox-label inline-check">
                <input name="is_active" type="checkbox" ${section.is_active ? "checked" : ""} />
                Active
              </label>
            </div>
            <label>
              Prompt
              <textarea name="prompt" rows="5">${escapeHtml(section.prompt)}</textarea>
            </label>
            <div class="button-row">
              <button type="button" class="secondary" data-test-edit="${section.id}">Test draft</button>
              <button type="submit" data-save-existing="${section.id}" disabled>Save section</button>
            </div>
            <p class="muted small-copy" data-save-hint="${section.id}">Test this prompt version against a company before saving changes.</p>
          </form>
        `
      )
      .join("");

    document.querySelectorAll("[data-section-edit]").forEach((form) => {
      form.addEventListener("submit", (event) => savePromptSection(event).catch(showError));
      form.querySelectorAll("input, select, textarea").forEach((field) => {
        field.addEventListener("input", () => updateExistingSectionSaveGuard(form));
        field.addEventListener("change", () => updateExistingSectionSaveGuard(form));
      });
      updateExistingSectionSaveGuard(form);
    });
    document.querySelectorAll("[data-test-edit]").forEach((button) => {
      button.addEventListener("click", () => testExistingSection(button).catch(showError));
    });
  });
}

function draftSignature(payload) {
  return JSON.stringify({
    company_id: Number(payload.company_id),
    sector_id: Number(payload.sector_id),
    name: String(payload.name || "").trim(),
    section_type: String(payload.section_type || "narrative"),
    prompt: String(payload.prompt || "").trim(),
    output_schema: payload.output_schema || {},
    requires_evidence: Boolean(payload.requires_evidence),
  });
}

function templateSignature() {
  return JSON.stringify({
    description: $("templateDescription").value.trim(),
    template_text: $("templateText").value,
  });
}

function buildNewSectionPayload() {
  return {
    company_id: testCompanyId(),
    sector_id: Number($("sectionSector").value),
    name: $("sectionName").value.trim(),
    section_type: $("sectionType").value,
    prompt: $("sectionPrompt").value.trim(),
    output_schema: {},
    requires_evidence: true,
  };
}

function buildExistingSectionPayload(form) {
  const data = new FormData(form);
  return {
    company_id: testCompanyId(),
    sector_id: Number($("editSector").value),
    name: String(data.get("name") || "").trim(),
    section_type: String(data.get("section_type") || "narrative"),
    prompt: String(data.get("prompt") || "").trim(),
    output_schema: {},
    requires_evidence: true,
  };
}

function canApprovePayload(payload) {
  return Boolean(payload.company_id && payload.sector_id && payload.name && payload.prompt);
}

function updateNewSectionSaveGuard() {
  const button = $("saveNewSectionBtn");
  const hint = $("newSectionSaveHint");
  if (!button || !hint) return;
  let approved = false;
  try {
    const payload = buildNewSectionPayload();
    approved = canApprovePayload(payload) && state.draftApprovals.newSection === draftSignature(payload);
  } catch (_) {
    approved = false;
  }
  button.disabled = !approved;
  hint.textContent = approved
    ? "This exact draft has been tested. Save it when the result below looks right."
    : "Test this draft against a company before saving it to the sector bundle.";
}

function updateExistingSectionSaveGuard(form) {
  const sectionId = form.dataset.sectionEdit;
  const button = form.querySelector(`[data-save-existing="${sectionId}"]`);
  const hint = form.querySelector(`[data-save-hint="${sectionId}"]`);
  if (!button || !hint) return;
  let approved = false;
  try {
    const payload = buildExistingSectionPayload(form);
    approved = canApprovePayload(payload) && state.draftApprovals.existingSections[sectionId] === draftSignature(payload);
  } catch (_) {
    approved = false;
  }
  button.disabled = !approved;
  hint.textContent = approved
    ? "This exact edit has been tested. Save it when the result below looks right."
    : "Test this prompt version against a company before saving changes.";
}

function refreshSaveGuards() {
  updateNewSectionSaveGuard();
  updateTemplateSaveGuard();
  document.querySelectorAll("[data-section-edit]").forEach((form) => updateExistingSectionSaveGuard(form));
}

function updateTemplateSaveGuard() {
  const button = $("saveTemplateBtn");
  const hint = $("templateSaveHint");
  if (!button || !hint) return;
  const approved = Boolean($("templateText").value.trim()) && state.draftApprovals.template === templateSignature();
  button.disabled = !approved;
  hint.textContent = approved
    ? "This exact template has been tested. Save it when the result looks right."
    : "Test this exact template before saving.";
}

function setDraftTestLoading(payload) {
  $("draftTestMeta").textContent = `${payload.name} / ${companyNameById(payload.company_id)}`;
  $("draftTestResult").className = "draft-result loading-box";
  $("draftTestResult").innerHTML = `
    <div class="loader-row">
      <span class="spinner" aria-hidden="true"></span>
      <div>
        <strong>Testing draft prompt...</strong>
        <p class="muted">This may take a little while when OpenAI is enabled. The company analysis page will stay unchanged.</p>
      </div>
    </div>
  `;
}

function renderDraftTestResult(result, payload) {
  state.draftTestResult = { result, payload };
  $("draftTestMeta").textContent = `Run ${result.run_id} / ${cleanStatus(result.status)} / ${companyNameById(payload.company_id)}`;
  const sections = (result.payload?.sections || []).map((section) => renderAnalysisSection(section)).join("");
  $("draftTestResult").className = "draft-result analysis-grid";
  $("draftTestResult").innerHTML =
    sections || `<div class="detail-empty">The draft test completed, but no sections were returned.</div>`;
}

function companyNameById(companyId) {
  const company = state.companies.find((item) => Number(item.id) === Number(companyId));
  return company ? company.display_name : `Company ${companyId}`;
}

function setTestingButtonsDisabled(disabled) {
  document.querySelectorAll("[data-test-edit], #testNewSectionBtn, #testTemplateBtn").forEach((button) => {
    button.disabled = disabled;
  });
}

async function runWithButtonLoading(button, label, action) {
  const previousText = button.textContent;
  button.textContent = label;
  setTestingButtonsDisabled(true);
  try {
    return await action();
  } finally {
    button.textContent = previousText;
    setTestingButtonsDisabled(false);
    refreshSaveGuards();
  }
}

async function pollRun(runId, onTick) {
  for (let attempt = 0; attempt < 180; attempt += 1) {
    const run = await api(`/api/runs/${runId}`);
    if (onTick) onTick(run);
    if (run.status === "failed") {
      throw new Error(run.error || "Run failed.");
    }
    if (!["queued", "running"].includes(run.status)) {
      return {
        run_id: run.id,
        status: run.status,
        mode: run.mode,
        payload: run.parsed,
      };
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error("Run is still processing after 3 minutes.");
}

async function syncLocal() {
  $("syncResult").textContent = "Scanning local input folders. This registers PDFs but does not call OpenAI.";
  const result = await api("/api/sync/local", { method: "POST" });
  $("syncResult").textContent = `Sync complete: ${result.discovered_companies} new companies, ${result.discovered_documents} new documents, ${result.changed_documents} changed documents. Total: ${result.total_companies} companies / ${result.total_documents} PDFs.`;
  await loadAll();
}

async function createDemoData() {
  $("syncResult").textContent = "Creating sample company folders and text-based PDFs under data/input...";
  const result = await api("/api/demo-data", { method: "POST" });
  $("syncResult").textContent = `Demo data ready: ${result.created_companies.join(", ")}. AcmeCloud includes multiple PDFs to prove multi-document processing.`;
  await loadAll();
}

async function saveConfig(event) {
  event.preventDefault();
  const payload = {
    use_ai: $("useAiInput").checked,
    openai_model: $("modelInput").value.trim(),
    openai_api_key: $("apiKeyInput").value.trim() || null,
    clear_openai_api_key: $("clearKeyInput").checked,
  };
  const result = await api("/api/config", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  $("apiKeyInput").value = "";
  $("clearKeyInput").checked = false;
  state.config = result;
  renderConfig();
  $("syncResult").textContent = `AI config saved. Current processing mode: ${result.mode}.`;
}

async function processCompany(companyId) {
  const select = document.querySelector(`[data-sector-for="${companyId}"]`);
  const sectorId = Number(select.value);
  setNotice("Processing company. This can take a while when OpenAI is enabled.");
  const result = await api(`/api/companies/${companyId}/process`, {
    method: "POST",
    body: JSON.stringify({ sector_id: sectorId }),
  });
  await pollRun(result.run_id, (run) => {
    setNotice(`Processing run ${run.id}: ${cleanStatus(run.status)}...`);
  });
  setNotice(`Processing complete: run ${result.run_id}.`);
  await loadAll();
  setPage("companies");
  await showCompany(companyId);
}

async function showCompany(companyId) {
  state.selectedCompanyId = companyId;
  const detail = await api(`/api/companies/${companyId}`);
  const company = detail.company;
  $("detailTitle").textContent = company.display_name;
  $("detailMeta").textContent = `${company.sector_name || "No sector"} / ${cleanStatus(company.status)}`;

  const documents = detail.documents
    .map(
      (doc) => `
        <tr>
          <td>${escapeHtml(doc.filename)}</td>
          <td>${escapeHtml(doc.document_uid)}</td>
          <td>${escapeHtml(cleanStatus(doc.status))}</td>
          <td>${doc.page_count || ""}</td>
        </tr>
      `
    )
    .join("");

  const runNote = detail.latest_run
    ? `<div class="run-note">${pill(`Run ${detail.latest_run.id}`)} ${pill(cleanStatus(detail.latest_run.status), statusTone(detail.latest_run.status))} ${pill(`Mode: ${detail.latest_run.mode}`, detail.latest_run.mode === "openai" ? "good" : "warn")}</div>`
    : "";

  const sections = detail.sections.length
    ? detail.sections.map((section) => renderAnalysisSection(section.data)).join("")
    : `<div class="detail-empty">No sections yet. Process this company to generate analysis or dry-run prompt previews.</div>`;

  $("companyDetail").innerHTML = `
    <div class="analysis-grid">
      ${runNote}
      <article class="section-card">
        <h3>Documents included in processing</h3>
        <p class="muted">All PDFs registered under the company folder are converted to Markdown and included in the prompt context.</p>
        <table>
          <thead><tr><th>File</th><th>Document ID</th><th>Status</th><th>Pages</th></tr></thead>
          <tbody>${documents}</tbody>
        </table>
      </article>
      ${sections}
    </div>
  `;
}

function renderAnalysisSection(section) {
  const type = section.type || "narrative";
  if (type === "metric_cards") return renderMetricCards(section);
  if (type === "key_value_table") return renderKeyValueTable(section);
  if (type === "time_series_table") return renderTimeSeries(section);
  if (type === "risk_list") return renderRiskList(section);
  if (type === "scenario_inputs") return renderScenarioInputs(section);
  return renderNarrative(section);
}

function renderMetricCards(section) {
  const items = section.items || section.metrics || section.cards || [];
  return `
    <article class="section-card">
      <h3>${escapeHtml(section.title || "Metrics")}</h3>
      <div class="metric-grid">
        ${items
          .map(
            (item) => `
              <div class="metric-card">
                <div class="muted">${escapeHtml(labelText(item.label || item.name || "Metric"))}</div>
                <div class="value">${escapeHtml(fmt(item.value))}</div>
                <div class="muted">${escapeHtml([item.unit, item.period].filter(Boolean).join(" / "))}</div>
              </div>
            `
          )
          .join("") || `<p class="muted">No metrics returned.</p>`}
      </div>
    </article>
  `;
}

function renderKeyValueTable(section) {
  const rows = section.rows || section.items || [];
  return `
    <article class="section-card">
      <h3>${escapeHtml(section.title || "Table")}</h3>
      <table>
        <thead><tr><th>Key</th><th>Value</th></tr></thead>
        <tbody>
          ${rows
            .map((row) => `<tr><td>${escapeHtml(labelText(row.key || row.label || row.name, "Value"))}</td><td>${escapeHtml(fmt(row.value))}</td></tr>`)
            .join("")}
        </tbody>
      </table>
    </article>
  `;
}

function renderTimeSeries(section) {
  const columns = section.columns || [];
  const rows = section.rows || [];
  return `
    <article class="section-card">
      <h3>${escapeHtml(section.title || "Time Series")}</h3>
      <table>
        <thead><tr>${columns.map((col) => `<th>${escapeHtml(col)}</th>`).join("")}</tr></thead>
        <tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(fmt(cell))}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>
    </article>
  `;
}

function renderRiskList(section) {
  const items = section.items || section.risks || [];
  return `
    <article class="section-card">
      <h3>${escapeHtml(section.title || "Risks")}</h3>
      <table>
        <thead><tr><th>Risk</th><th>Severity</th></tr></thead>
        <tbody>
          ${items
            .map((item) => `<tr><td>${escapeHtml(fmt(item.risk || item.text || item.label))}</td><td>${escapeHtml(fmt(item.severity || ""))}</td></tr>`)
            .join("")}
        </tbody>
      </table>
    </article>
  `;
}

function renderScenarioInputs(section) {
  return renderKeyValueTable({ ...section, rows: section.items || [] });
}

function renderNarrative(section) {
  const body = section.body || section.text || JSON.stringify(section, null, 2);
  const isPromptPreview = Boolean(section.dry_run) || (section.id || "").includes("preview") || body.length > 2000;
  const subtitle = section.target_section_type ? `<div class="muted">Expected output renderer: ${escapeHtml(section.target_section_type)}</div>` : "";
  return `
    <article class="section-card">
      <div class="panel-header">
        <h3>${escapeHtml(section.title || "Narrative")}</h3>
        ${section.dry_run ? pill("dry run", "warn") : ""}
      </div>
      ${subtitle}
      ${isPromptPreview ? `<pre>${escapeHtml(body)}</pre>` : `<p>${escapeHtml(body)}</p>`}
    </article>
  `;
}

async function createSector(event) {
  event.preventDefault();
  const name = $("sectorName").value.trim();
  if (!name) return;
  await api("/api/sectors", {
    method: "POST",
    body: JSON.stringify({ name, description: $("sectorDescription").value.trim() }),
  });
  $("sectorName").value = "";
  $("sectorDescription").value = "";
  await loadAll();
}

async function saveSectorGuidance(event) {
  event.preventDefault();
  const sectorId = Number($("editSector").value);
  if (!sectorId) return;
  await api(`/api/sectors/${sectorId}`, {
    method: "PATCH",
    body: JSON.stringify({ description: $("sectorGuidanceText").value.trim() }),
  });
  await loadAll();
  $("editSector").value = String(sectorId);
  renderSectorGuidance(sectorId);
  renderPromptSections(sectorId);
  setNotice("Sector guidance saved.");
}

async function createSection(event) {
  event.preventDefault();
  const payload = buildNewSectionPayload();
  if (!canApprovePayload(payload)) return;
  if (state.draftApprovals.newSection !== draftSignature(payload)) {
    throw new Error("Test this exact prompt draft before saving it.");
  }
  await api(`/api/sectors/${payload.sector_id}/sections`, {
    method: "POST",
    body: JSON.stringify({
      name: payload.name,
      section_type: payload.section_type,
      prompt: payload.prompt,
      output_schema: {},
      requires_evidence: true,
      display_order: 50,
    }),
  });
  state.draftApprovals.newSection = null;
  $("sectionName").value = "";
  $("sectionPrompt").value = "";
  $("editSector").value = payload.sector_id;
  renderPromptSections(payload.sector_id);
  await loadAll();
  refreshSaveGuards();
}

function testCompanyId() {
  return requiredSelectedCompanyId("testCompany");
}

function requiredSelectedCompanyId(selectId) {
  const companyId = Number($(selectId).value);
  if (!companyId) {
    throw new Error("Select a company to test against first.");
  }
  return companyId;
}

async function testDraftSection(payload) {
  setDraftTestLoading(payload);
  const result = await api("/api/prompt-sections/test-draft", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  const completed = await pollRun(result.run_id, (run) => {
    $("draftTestMeta").textContent = `Run ${run.id} / ${cleanStatus(run.status)} / ${companyNameById(payload.company_id)}`;
  });
  renderDraftTestResult(completed, payload);
  setNotice("Draft test complete. Review the result below; the company analysis run was not replaced.");
  return completed;
}

function setTemplateTestLoading() {
  $("templateTestMeta").textContent = companyNameById(requiredSelectedCompanyId("templateTestCompany"));
  $("templateTestResult").className = "draft-result loading-box";
  $("templateTestResult").innerHTML = `
    <div class="loader-row">
      <span class="spinner" aria-hidden="true"></span>
      <div>
        <strong>Testing global template...</strong>
        <p class="muted">Polling the run while the model response is generated.</p>
      </div>
    </div>
  `;
}

function renderTemplateTestResult(result) {
  state.templateTestResult = result;
  $("templateTestMeta").textContent = `Run ${result.run_id} / ${cleanStatus(result.status)}`;
  $("templateTestResult").className = "draft-result analysis-grid";
  $("templateTestResult").innerHTML = (result.payload?.sections || [])
    .map((section) => renderAnalysisSection(section))
    .join("") || `<div class="detail-empty">The template test completed, but no sections were returned.</div>`;
}

async function testTemplate() {
  if (!$("templateText").value.trim()) throw new Error("Template text is required.");
  const sectorId = Number($("templateTestSector").value);
  const companyId = requiredSelectedCompanyId("templateTestCompany");
  if (!sectorId) throw new Error("Select a sector to test against first.");
  setTemplateTestLoading();
  const queued = await api("/api/prompt-template/test", {
    method: "POST",
    body: JSON.stringify({
      company_id: companyId,
      sector_id: sectorId,
      template_text: $("templateText").value,
    }),
  });
  const completed = await pollRun(queued.run_id, (run) => {
    $("templateTestMeta").textContent = `Run ${run.id} / ${cleanStatus(run.status)}`;
  });
  renderTemplateTestResult(completed);
  state.draftApprovals.template = templateSignature();
  updateTemplateSaveGuard();
}

async function saveTemplate(event) {
  event.preventDefault();
  if (state.draftApprovals.template !== templateSignature()) {
    throw new Error("Test this exact global template before saving it.");
  }
  state.promptTemplate = await api("/api/prompt-template", {
    method: "PATCH",
    body: JSON.stringify({
      description: $("templateDescription").value.trim(),
      template_text: $("templateText").value,
    }),
  });
  state.draftApprovals.template = null;
  renderPromptTemplate();
  setNotice("Global prompt template saved.");
}

async function testNewSection(button) {
  const payload = buildNewSectionPayload();
  if (!canApprovePayload(payload)) {
    throw new Error("Add a section name and prompt before testing.");
  }
  await runWithButtonLoading(button, "Testing...", () => testDraftSection(payload));
  state.draftApprovals.newSection = draftSignature(payload);
  refreshSaveGuards();
}

async function testExistingSection(button) {
  const form = button.closest("[data-section-edit]");
  const payload = buildExistingSectionPayload(form);
  if (!canApprovePayload(payload)) {
    throw new Error("Section name and prompt are required before testing.");
  }
  await runWithButtonLoading(button, "Testing...", () => testDraftSection(payload));
  state.draftApprovals.existingSections[form.dataset.sectionEdit] = draftSignature(payload);
  refreshSaveGuards();
}

async function savePromptSection(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const sectionId = form.dataset.sectionEdit;
  const data = new FormData(form);
  const payload = buildExistingSectionPayload(form);
  if (state.draftApprovals.existingSections[sectionId] !== draftSignature(payload)) {
    throw new Error("Test this exact prompt edit before saving it.");
  }
  await api(`/api/prompt-sections/${sectionId}`, {
    method: "PATCH",
    body: JSON.stringify({
      name: String(data.get("name") || "").trim(),
      section_type: String(data.get("section_type") || "narrative"),
      prompt: String(data.get("prompt") || "").trim(),
      display_order: Number(data.get("display_order") || 0),
      is_active: data.get("is_active") === "on",
    }),
  });
  delete state.draftApprovals.existingSections[sectionId];
  renderPromptSections($("editSector").value);
}

function wireEvents() {
  document.querySelectorAll("[data-page-link]").forEach((button) => {
    button.addEventListener("click", () => setPage(button.dataset.pageLink));
  });
  $("syncBtn").addEventListener("click", () => syncLocal().catch(showError));
  $("demoDataBtn").addEventListener("click", () => createDemoData().catch(showError));
  $("refreshBtn").addEventListener("click", () => loadAll().catch(showError));
  $("configForm").addEventListener("submit", (event) => saveConfig(event).catch(showError));
  $("templateForm").addEventListener("submit", (event) => saveTemplate(event).catch(showError));
  $("testTemplateBtn").addEventListener("click", () => testTemplate().catch(showError));
  $("sectorForm").addEventListener("submit", (event) => createSector(event).catch(showError));
  $("sectorGuidanceForm").addEventListener("submit", (event) => saveSectorGuidance(event).catch(showError));
  $("sectionForm").addEventListener("submit", (event) => createSection(event).catch(showError));
  $("testNewSectionBtn").addEventListener("click", (event) => testNewSection(event.currentTarget).catch(showError));
  $("editSector").addEventListener("change", (event) => {
    renderSectorGuidance(event.target.value);
    renderPromptSections(event.target.value);
  });
  $("testCompany").addEventListener("change", refreshSaveGuards);
  $("templateTestCompany").addEventListener("change", refreshSaveGuards);
  ["templateDescription", "templateText"].forEach((id) => {
    $(id).addEventListener("input", updateTemplateSaveGuard);
    $(id).addEventListener("change", updateTemplateSaveGuard);
  });
  ["sectionSector", "sectionName", "sectionType", "sectionPrompt"].forEach((id) => {
    $(id).addEventListener("input", updateNewSectionSaveGuard);
    $(id).addEventListener("change", updateNewSectionSaveGuard);
  });
}

function showError(error) {
  setNotice(`Error: ${error.message}`);
}

wireEvents();
loadAll()
  .then(() => {
    if (state.sectors[0]) renderPromptSections(state.sectors[0].id);
  })
  .catch(showError);
