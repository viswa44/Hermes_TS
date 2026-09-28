"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { status: null, run: null, runId: null, kind: "observations", offset: 0, limit: 25, total: 0, runVersion: 0, tableVersion: 0, chartVersion: 0, polling: null, refreshing: false, launching: false, jobSignature: "", chart: null };
  const stages = [{ id: "align_timestamps", label: "Alignment" }, { id: "detect_events", label: "Events" }, { id: "agent1_discover", label: "Discovery" }, { id: "agent2_scan", label: "Scan history" }, { id: "agent3_evidence_qa", label: "Integrity QA" }, { id: "register_evidence", label: "Registry" }];
  const number = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
  const integer = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
  const timeFormat = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3, hour12: false });
  const dayFormat = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short" });
  const dateFormat = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", year: "numeric" });
  const activeStatuses = new Set(["QUEUED", "RUNNING", "STARTING", "PENDING", "RESUMING"]);
  const interruptedStatuses = new Set(["INTERRUPTED", "FAILED", "ERROR"]);
  const labelNames = { UP_MOMENTUM: "UP ≥50", DOWN_MOMENTUM: "DOWN ≥50", UP_50_80: "UP 50–80", DOWN_50_80: "DOWN 50–80", UP_OVER_80: "UP >80", DOWN_OVER_80: "DOWN >80", OTHER: "OTHER", UNKNOWN: "UNKNOWN" };
  const headings = { observation_id: "Observation ID", timestamps: "Receipt time", timestamp: "Receipt time", trading_date: "Session", optiontype: "Side", option_type: "Side", option_side: "Side", side: "Side", symbol: "Contract", underlying: "Underlying", spot: "Spot", iv: "IV (decimal)", iv_decimal: "IV (decimal)", source_iv: "Source IV", oi: "Open interest", volume: "Volume", strike: "Strike", expiry: "Expiry", anchor_at: "Anchor (IST)", start_at: "Start (IST)", end_at: "End (IST)", condition_at: "Condition cutoff", point_change: "Change (pts)", spot_start: "Start spot", spot_end: "End spot", start_spot: "Start spot", end_spot: "End spot", label: "Outcome", target: "Target", sample_id: "Window ID", condition_id: "Condition ID", outcome_status: "Outcome status", partition: "Partition", success: "Target matched", known_outcome: "Known outcome", feature_value: "Feature value", provider_timestamp: "Provider time" };
  const numericColumns = /^(spot|start_spot|end_spot|spot_start|spot_end|iv|source_iv|iv_decimal|oi|volume|strike|point_change|feature_value|bid|ask|ltp|delta|gamma|theta|vega|rho|duration_seconds|receipt_duration_seconds|price|mid)$/;

  function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
  function setText(id, value) { $(id).textContent = value; }
  function statusOf(value) { return String(value || "").toUpperCase(); }
  function finite(value) { return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)); }
  function fmt(value, decimals = false) { return finite(value) ? (decimals ? number : integer).format(Number(value)) : "—"; }
  function percent(value) { return finite(value) ? `${(Number(value) * 100).toFixed(2)}%` : "—"; }
  function validDate(value) { if (!value) return null; const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? null : parsed; }
  function day(value, year = false) { const parsed = validDate(value); return parsed ? (year ? dateFormat : dayFormat).format(parsed) : String(value || "—"); }
  function time(value) { const parsed = validDate(value); return parsed ? timeFormat.format(parsed) : String(value || "—"); }
  function preciseTime(value) {
    const rendered = time(value);
    const fraction = String(value).match(/\.(\d+)(?:Z|[+-]\d{2}:?\d{2})$/)?.[1];
    return rendered.replace(/\.\d{3}$/, fraction ? `.${fraction}` : "");
  }
  function humanize(value) { return String(value || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()); }
  function datesIn(value) { return Array.isArray(value) ? [...new Set(value.filter(Boolean).map(String))].sort() : []; }
  function coverage(dates) { if (!dates.length) return "No saved sessions available"; return dates.length === 1 ? day(dates[0], true) : `${day(dates[0])} – ${day(dates[dates.length - 1], true)}`; }
  function exactNumber(value) { return finite(value) ? String(value) : "—"; }
  function predicatesFor(condition) { return Array.isArray(condition.predicates) ? condition.predicates : condition.feature ? [{ feature: condition.feature, operator: condition.operator, threshold: condition.threshold }] : []; }
  function featureLabel(feature) {
    return String(feature || "").replace(/^CE_/, "CE · ").replace(/^PE_/, "PE · ").replace(/iv_decimal/g, "IV (decimal)").replace(/_/g, " ");
  }
  function baselineRate(stats) { return typeof stats?.baseline === "object" && stats.baseline !== null ? stats.baseline.event_rate : stats?.baseline; }
  function jointResearch() { return state.run?.research?.mode === "joint" || Boolean(state.run?.conditions?.some((condition) => Array.isArray(condition.predicates))); }
  function detailString(value) { return typeof value === "string" ? value : value === undefined || value === null ? "" : JSON.stringify(value, null, 2); }
  function showError(message) { setText("global-alert", message); $("global-alert").hidden = false; }
  function clearError() { $("global-alert").hidden = true; }
  function toast(message) { setText("toast", message); $("toast").hidden = false; window.clearTimeout(toast.timeout); toast.timeout = window.setTimeout(() => { $("toast").hidden = true; }, 4500); }
  async function request(url, options = {}) {
    const response = await fetch(url, { cache: "no-store", ...options });
    let payload;
    try { payload = await response.json(); } catch { throw new Error(`The dashboard returned an unreadable response (${response.status}).`); }
    if (!response.ok) {
      const message = payload.error || payload.detail || payload.message || `Request failed (${response.status})`;
      throw new Error(typeof message === "object" ? (message.message || JSON.stringify(message)) : String(message));
    }
    return payload;
  }
  function isActive(job) { return Boolean(job && activeStatuses.has(statusOf(job.status))); }
  function badge(text, variant) { return element("span", `badge${variant ? ` ${variant}` : ""}`, text); }
  function badgeVariant(status) { const value = statusOf(status); return ["PASS", "PASSED", "VERIFIED", "COMPLETE", "COMPLETED", "EXPLORATORY", "REGISTERED"].includes(value) ? "pass" : ["FAIL", "FAILED", "ERROR"].includes(value) ? "fail" : ["INSUFFICIENT_DATA", "INTERRUPTED", "UNKNOWN", "QUARANTINED"].includes(value) ? "warn" : ""; }
  function setOptions(select, values, blankLabel, preferred) {
    const previous = preferred === undefined ? select.value : preferred;
    select.replaceChildren();
    if (blankLabel !== null) { const option = element("option", null, blankLabel); option.value = ""; select.append(option); }
    for (const value of values) { const option = element("option", null, day(value, true)); option.value = value; select.append(option); }
    if (values.includes(previous) || (previous === "" && blankLabel !== null)) select.value = previous;
    else if (values.length) select.value = blankLabel === null ? values[values.length - 1] : "";
  }
  function renderSource() {
    const source = state.status?.source || {};
    const dates = datesIn(source.dates);
    const label = coverage(dates);
    const selected = state.run?.run_id === state.runId ? state.run : null;
    const selectedDates = datesIn(selected?.source?.dates || selected?.dates);
    const selectedSessions = selected?.source?.sessions ?? selectedDates.length;
    const haveSelection = Boolean(state.runId || selected);
    const selectedLabel = selectedDates.length ? coverage(selectedDates) : haveSelection ? "Awaiting this run's pinned source" : label;
    setText("source-coverage-label", haveSelection ? "SELECTED RUN DATASET" : "NEWEST AVAILABLE DATASET");
    setText("sidebar-source-label", haveSelection ? "SELECTED RUN DATASET" : "AVAILABLE DATASET");
    setText("source-coverage", selectedLabel);
    setText("sidebar-coverage", haveSelection ? `${fmt(selectedSessions)} sessions · ${selectedLabel}` : `${fmt(source.sessions ?? dates.length)} sessions · ${label}`);
    setText("source-status", haveSelection ? "PINNED INPUTS" : source.status || (source.available ? "AVAILABLE" : "UNAVAILABLE"));
    setText("dialog-source", `${label} · ${fmt(source.sessions ?? dates.length)} sessions · ${fmt(source.rows)} source rows`);
    const latestSelected = selectedDates[selectedDates.length - 1];
    const newer = latestSelected ? dates.filter((value) => value > latestSelected) : [];
    $("newer-source-notice").hidden = !newer.length;
    setText("newer-source-detail", newer.length ? `This run ends on ${day(latestSelected, true)}. The latest verified catalog includes ${fmt(newer.length)} additional ${newer.length === 1 ? "session" : "sessions"}, through ${day(dates[dates.length - 1], true)}. Start a new run to include them.` : "");
    $("source-issues").hidden = Boolean(source.available);
    setText("source-issues-title", statusOf(source.status) === "INVALID" ? "Newest source catalog needs attention" : "Newest source data is unavailable");
    const latestCandidate = source.latest_candidate_date ? ` Latest cleaner candidate: ${day(source.latest_candidate_date, true)}.` : "";
    setText("source-issues-summary", `Starting a run is paused until every selected source passes local verification.${latestCandidate} Existing saved research remains available.`);
    const issues = Array.isArray(source.issues) ? source.issues : [];
    const issueList = $("source-issue-list"); issueList.replaceChildren();
    for (const issue of issues) issueList.append(element("li", null, `${issue.date ? `${day(issue.date, true)}: ` : ""}${issue.reason || "Source verification failed."}`));
    if (!issues.length) issueList.append(element("li", null, source.reason || "No verified local source snapshots are available."));
    $("discovery-end").min = dates[0] || "";
    $("discovery-end").max = dates[dates.length - 1] || "";
    const defaults = state.status?.defaults || {};
    const bucket = defaults.s3_bucket || defaults.bucket || defaults.publish_bucket;
    setText("publish-label", bucket ? `Upload evidence to the existing ${bucket} bucket after QA passes.` : "Upload to heremesv0-cleaned-data/evidence-engine after QA passes.");
    updateLaunchButtons();
  }
  function updateLaunchButtons() {
    const blocked = state.launching || isActive(state.status?.active_job) || !state.status?.source?.available;
    $("start-run").disabled = blocked;
    $("start-first-run").disabled = blocked;
    $("start-latest-run").disabled = blocked;
    $("start-run").title = !state.status?.source?.available ? "The latest verified historical source is unavailable" : isActive(state.status?.active_job) ? "A research run is already in progress" : "Start joint CE + PE + IV research using the newest verified historical data";
  }
  function renderRunOptions() {
    const runs = state.status.runs || [];
    const select = $("run-select");
    select.replaceChildren();
    if (!runs.length) { const option = element("option", null, "No saved research runs"); option.value = ""; select.append(option); }
    for (const run of runs) {
      const option = element("option", null, run.run_id);
      option.value = run.run_id;
      option.title = `${run.status || "Saved"} · ${run.executed_at ? day(run.executed_at, true) : "Saved run"}`;
      select.append(option);
    }
    if (state.runId && !runs.some((run) => run.run_id === state.runId)) {
      const option = element("option", null, state.runId); option.value = state.runId; select.prepend(option);
    }
    if (state.runId) select.value = state.runId;
  }
  function renderJob() {
    const job = state.status?.active_job;
    const selected = state.run;
    const selectedInterrupted = interruptedStatuses.has(statusOf(selected?.status));
    const visible = Boolean(job && (isActive(job) || interruptedStatuses.has(statusOf(job.status)))) || selectedInterrupted;
    $("job-banner").hidden = !visible;
    if (!visible) return;
    const relevant = job && (isActive(job) || job.run_id === state.runId) ? job : selectedInterrupted ? selected : job;
    const active = isActive(relevant);
    setText("job-title", active ? `Research running · ${relevant.run_id}` : `Run ${statusOf(relevant.status) === "INTERRUPTED" ? "interrupted" : "needs attention"} · ${relevant.run_id}`);
    setText("job-message", relevant.error || relevant.message || (active ? "Aligning observations and building reproducible evidence. Progress updates automatically." : "Saved checkpoints may allow this run to continue. Resume rechecks the original inputs and configuration."));
    $("resume-run").hidden = active || Boolean(job && isActive(job)) || !(job?.run_id === relevant.run_id && job?.resumable);
    $("resume-run").dataset.runId = relevant.run_id;
    $("resume-run").disabled = state.launching;
  }
  function renderPipeline() {
    const run = state.run;
    const job = state.status?.active_job;
    const matchingJob = job?.run_id === state.runId ? job : null;
    const reported = Array.isArray(run?.pipeline) && run.pipeline.length ? run.pipeline : stages;
    const aliases = { alignment: "align", load: "align", align_data: "align", labeling: "events", agent1: "discover", discovery: "discover", agent2: "scan", agent3: "qa", publish: "registry", register: "registry" };
    const current = aliases[matchingJob?.stage] || matchingJob?.stage;
    const currentIndex = reported.findIndex((stage) => (aliases[stage.id] || stage.id) === current);
    const list = $("pipeline"); list.replaceChildren();
    reported.forEach((stage, index) => {
      let stageStatus = statusOf(stage.status);
      if (matchingJob && isActive(matchingJob) && currentIndex >= 0) stageStatus = index < currentIndex ? "COMPLETE" : index === currentIndex ? "RUNNING" : stageStatus;
      const complete = ["COMPLETE", "COMPLETED", "PASS", "PASSED", "DONE", "EXPLORATORY"].includes(stageStatus);
      const running = ["RUNNING", "ACTIVE", "IN_PROGRESS"].includes(stageStatus);
      const failed = ["FAIL", "FAILED", "ERROR", "QUARANTINED"].includes(stageStatus);
      const item = element("li", complete ? "complete" : running ? "running" : failed ? "failed" : "pending");
      item.title = `${stage.label || humanize(stage.id)}: ${humanize(stageStatus || "Pending")}`;
      item.append(element("span", "stage-marker", complete ? "✓" : failed ? "!" : String(index + 1)), element("span", null, stage.label || humanize(stage.id)));
      list.append(item);
    });
  }
  function renderRun() {
    const run = state.run;
    if (!run) return;
    const metrics = run.metrics || {};
    const checks = run.qa?.checks || [];
    const qa = run.qa_status || run.qa?.status || "PENDING";
    setText("metric-observations", fmt(metrics.eligible_rows));
    setText("metric-observations-note", `${fmt(metrics.source_rows)} source rows · ${fmt(metrics.excluded_rows)} excluded`);
    setText("metric-events", fmt(metrics.event_count));
    setText("metric-events-note", !run.research && !run.dates?.length ? "Awaiting saved target definition" : jointResearch() ? "≥50 point moves · 5 minutes · no upper cap" : "50–80 point moves · 5 minute windows");
    setText("metric-conditions", fmt(metrics.condition_count));
    setText("metric-conditions-note", `${fmt(metrics.sample_count)} windows · ${fmt(metrics.unknown_count)} unknown`);
    setText("metric-qa", qa === "INSUFFICIENT_DATA" ? "Insufficient data" : humanize(qa));
    $("metric-qa").className = `metric-value qa-value ${badgeVariant(qa)}`;
    setText("metric-qa-note", checks.length ? `${fmt(checks.filter((check) => check.passed === true).length)} of ${fmt(checks.length)} integrity checks passed` : "QA results appear when the stage completes");
    setText("run-timestamp", run.executed_at ? `Executed ${day(run.executed_at, true)} · ${time(run.executed_at)} IST` : "Run is initializing");
    const dates = datesIn(run.source?.dates || run.dates);
    setOptions($("chart-date"), dates, null);
    setOptions($("filter-date"), dates, "All sessions");
    renderConditions();
    renderResearch();
    renderSource();
    renderExplorerNote();
    renderChecks();
    renderPublication();
    renderPipeline();
    renderJob();
  }
  function renderConditions() {
    const conditions = state.run?.conditions || [];
    const container = $("condition-list"); container.replaceChildren();
    const previous = $("filter-condition").value;
    $("filter-condition").replaceChildren();
    const all = element("option", null, "All conditions"); all.value = ""; $("filter-condition").append(all);
    const partition = state.run?.partition || {};
    const note = $("partition-note"); note.replaceChildren();
    for (const [name, dates] of [["Discovery", datesIn(partition.discovery_dates)], ["Evaluation", datesIn(partition.evaluation_dates)]]) {
      const span = element("span"); span.append(element("strong", null, `${name}: `), document.createTextNode(dates.length ? coverage(dates) : "Awaiting partition")); note.append(span);
    }
    if (!conditions.length) { container.append(element("p", "assurance-copy", "No frozen conditions are available for this run yet. Discovery retains only rules that meet the configured support and event-rate criteria.")); return; }
    conditions.forEach((condition, index) => {
      const name = `C${String(index + 1).padStart(2, "0")}`;
      const predicates = predicatesFor(condition);
      const joint = Array.isArray(condition.predicates);
      const option = element("option", null, `${name} · ${joint ? "Joint CE + PE + IV" : condition.feature}`); option.value = condition.condition_id; $("filter-condition").append(option);
      const card = element("article", "condition-card");
      const header = element("div", "condition-card-header");
      const identity = element("span", "condition-number", `${name} / ${String(condition.condition_id).slice(0, 10)}`); identity.title = condition.condition_id;
      header.append(identity, badge(labelNames[condition.target] || condition.target || "TARGET", String(condition.target).startsWith("DOWN") ? "down" : "up"));
      const rule = element("div", `condition-rule${joint ? " joint-rule" : ""}`);
      if (joint) rule.append(element("div", "conjunction-label", `${condition.combination === "ALL" ? "ALL" : condition.combination || "ALL"} OF THESE CONDITIONS AT THE SAME CUTOFF`));
      predicates.forEach((predicate, predicateIndex) => {
        const line = element("div", "predicate-line");
        if (joint) line.append(element("span", "predicate-join", predicateIndex ? "AND" : "IF"));
        const label = element("span", "predicate-feature", featureLabel(predicate.feature)); label.title = predicate.feature;
        line.append(label, element("span", "predicate-operator", predicate.operator), element("span", "predicate-threshold", exactNumber(predicate.threshold)));
        rule.append(line);
      });
      const values = element("div", "condition-values");
      for (const [key, title] of [["discovery", "DISCOVERY"], ["evaluation", "EVALUATION"]]) {
        const stats = condition[key] || {};
        const baseline = baselineRate(stats);
        const group = element("div");
        group.append(element("h3", null, title));
        const rate = element("div", "condition-rate", percent(stats.event_rate)); rate.title = `Exact event rate: ${exactNumber(stats.event_rate)}; exact baseline: ${exactNumber(baseline)}`; rate.append(element("span", null, ` / ${percent(baseline)} baseline`));
        group.append(rate, element("p", null, `${fmt(stats.successes)} successes / ${fmt(stats.known_outcomes)} known`), element("p", null, `${fmt(stats.matches)} matches · ${fmt(stats.unknown_outcomes)} unknown`));
        if (finite(stats.feature_available) && finite(stats.all_anchors)) {
          group.append(element("p", null, `Inputs available: ${fmt(stats.feature_available)} / ${fmt(stats.all_anchors)} windows`),
            element("p", null, `${fmt(stats.feature_unknown)} windows lack required inputs`));
        }
        if (stats.nonoverlapping) {
          group.append(element("p", null, `Spaced matches: ${fmt(stats.nonoverlapping.successes)} successes / ${fmt(stats.nonoverlapping.known_outcomes)} known · ${fmt(stats.nonoverlapping.unknown_outcomes)} unknown`));
        }
        values.append(group);
      }
      const inspect = element("button", "condition-match-link"); inspect.type = "button"; inspect.append(element("span", null, "Inspect every matching window"), element("span", null, "↗"));
      inspect.addEventListener("click", () => { $("filter-condition").value = condition.condition_id; $("filter-date").value = ""; $("filter-label").value = ""; $("search").value = ""; selectTab("occurrences"); $("observations").scrollIntoView({ behavior: "smooth", block: "start" }); });
      const assessment = element("p", "condition-assessment", assessmentFor(condition));
      card.append(header, rule, values, assessment, inspect); container.append(card);
    });
    if (conditions.some((condition) => condition.condition_id === previous)) $("filter-condition").value = previous;
  }
  function assessmentFor(condition) {
    const stats = condition.evaluation || {};
    const baseline = baselineRate(stats);
    if (!finite(stats.known_outcomes) || Number(stats.known_outcomes) === 0) return "No known evaluation matches are available. This rule has no observed evaluation success rate.";
    if (!finite(stats.event_rate) || !finite(baseline)) return "Evaluation evidence is incomplete. The known-outcome rate cannot yet be compared with its baseline.";
    const difference = (Number(stats.event_rate) - Number(baseline)) * 100;
    const comparison = Math.abs(difference) < 0.000001 ? "equal to" : `${Math.abs(difference).toFixed(2)} percentage points ${difference > 0 ? "above" : "below"}`;
    return `Observed evaluation rate: ${comparison} the feature-eligible baseline across ${fmt(stats.known_outcomes)} known matches. Descriptive evidence; forecasting value is unproven.`;
  }
  function renderResearch() {
    const research = state.run?.research || {};
    const joint = jointResearch();
    if (!state.run?.research && !state.run?.dates?.length) {
      setText("research-mode", "RESEARCH INITIALIZING"); setText("research-target", "Awaiting this run's saved target definition");
      setText("research-interpretation", "The pinned research configuration will appear as this run initializes."); setText("research-versions", "");
      setText("tab-samples", "All windows"); return;
    }
    const target = research.target_description || (joint ? "At least 50 spot points upward or downward over 5 minutes; no upper cap." : "Legacy target: 50–80 spot points upward or downward over 5 minutes.");
    setText("research-mode", joint ? "JOINT CE + PE + IV" : "LEGACY SINGLE FEATURE");
    setText("research-target", target);
    const interpretation = research.interpretation || (joint ? "Every frozen rule combines call and put observations with both IV values available before one cutoff. Evaluation uses later whole sessions. Observed rates are descriptive and do not establish predictive usefulness." : "These saved rules examine individual features. They retain the original target definition and do not represent joint call-and-put momentum research.");
    setText("research-interpretation", detailString(interpretation));
    const trialCount = Array.isArray(research.tested_predicates) ? research.tested_predicates.length : research.tested_predicates;
    const metadata = [];
    if (finite(trialCount)) metadata.push(`${fmt(trialCount)} predicates tested`);
    if (research.discovery_version) metadata.push(`Discovery: ${research.discovery_version}`);
    if (research.detector_version) metadata.push(`Detector: ${research.detector_version}`);
    setText("research-versions", metadata.join(" · "));
    setText("tab-samples", joint ? "Joint observations" : "All windows");
  }
  function renderExplorerNote() {
    const notes = {
      observations: "Saved contract observations with their receipt times and source identity. CE and PE retain separate observation IDs.",
      events: jointResearch() ? "Five-minute endpoint moves of at least 50 spot points, upward or downward, with no upper cap. Unknown windows are retained in Joint observations." : "Legacy qualifying windows: 50–80 point endpoint moves. Other complete windows and unknown outcomes remain in All windows.",
      samples: "One row per historical window: CE and PE observations, both IV values, and the shared availability cutoff. Scroll across to inspect every field; unavailable values stay empty.",
      occurrences: "Every frozen-rule match, including failed targets and unknown outcomes. Joint feature values show the full combination that matched at the cutoff."
    };
    setText("explorer-note", notes[state.kind]);
  }
  function renderChecks() {
    const qa = state.run?.qa || {};
    const status = qa.status || state.run?.qa_status || "PENDING";
    setText("qa-badge", status.replaceAll("_", " "));
    $("qa-badge").className = `badge ${badgeVariant(status)}`;
    const integrity = state.run?.integrity || {};
    setText("integrity-reason", integrity.reason || "Checks verify the saved research artifacts and their source lineage.");
    const list = $("qa-checks");
    const opened = new Set([...list.querySelectorAll("details[open]")].map((item) => item.dataset.check));
    list.replaceChildren();
    const checks = qa.checks || [];
    if (!checks.length) { list.append(element("p", "assurance-copy", "Checks will appear after the integrity QA stage.")); return; }
    checks.forEach((check) => {
      const row = element("details", "qa-check"); row.dataset.check = check.check; row.open = opened.has(check.check);
      const summary = element("summary"); summary.append(element("span", `check-icon${check.passed === false ? " failed" : ""}`, check.passed === true ? "✓" : check.passed === false ? "!" : "○"), element("span", null, humanize(check.check)), element("span", "sr-only", check.passed === true ? "Passed" : "Not passed"));
      row.append(summary, element("p", null, detailString(check.detail) || (check.passed ? "This check passed for the saved run artifacts." : "Inspect this run's QA report for details."))); list.append(row);
    });
  }
  function renderPublication() {
    const publication = state.run?.publication || {};
    setText("publication-status", publication.published ? "Published to S3" : state.run?.status === "REGISTERED" ? "Stored locally" : "Not registered");
    let detail = publication.published ? "This run has saved publication evidence." : state.run?.status === "REGISTERED" ? "Research artifacts remain in the local evidence registry. S3 publication can be enabled when starting a run." : "Evidence enters the registry only after the run finishes and passes integrity QA.";
    if (publication.verified_at) detail = `Historical saved verification · ${day(publication.verified_at, true)} at ${time(publication.verified_at)} IST. This dashboard does not recheck remote objects.`;
    else if (publication.published) detail += " No downloaded-checksum verification time is recorded.";
    setText("publication-detail", detail);
    const uri = publication.manifest_uri;
    $("publication-uri").hidden = !uri;
    setText("publication-uri", uri || "");
  }
  async function loadRun(runId, { keepTable = false } = {}) {
    const version = ++state.runVersion;
    const changing = state.runId !== runId;
    state.runId = runId;
    if (changing) { state.offset = 0; state.tableVersion++; state.chartVersion++; $("filter-date").value = ""; $("chart-date").replaceChildren(); $("filter-condition").value = ""; }
    $("empty-run").hidden = true;
    $("research-content").hidden = false;
    try {
      const run = await request(`/api/runs/${encodeURIComponent(runId)}`);
      if (version !== state.runVersion) return;
      state.run = run; clearError(); renderRun();
      if (!keepTable || changing) await Promise.all([loadTable(), loadChart()]);
    } catch (error) {
      if (version !== state.runVersion) return;
      if (state.status?.active_job?.run_id === runId && isActive(state.status.active_job)) {
        state.run = { run_id: runId, status: state.status.active_job.status, metrics: {}, conditions: [], dates: [] };
        renderRun();
        setText("table-empty", "Observations become available after this run finishes and passes integrity checks."); $("table-empty").hidden = false; $("table-head").replaceChildren(); $("table-body").replaceChildren();
        $("spot-chart").replaceChildren(); $("chart-empty").hidden = false; setText("chart-empty", "Waiting for aligned observations…");
      } else { showError(`Could not load the selected run: ${error.message}`); }
    }
  }
  async function refresh({ force = false } = {}) {
    if (state.refreshing) return;
    state.refreshing = true; $("refresh").disabled = true;
    try {
      const previousJob = state.status?.active_job;
      state.status = await request("/api/status");
      renderSource();
      const available = state.status.runs || [];
      const desired = state.runId || (isActive(state.status.active_job) ? state.status.active_job.run_id : null) || state.status.selected_run_id || available[0]?.run_id;
      const signature = JSON.stringify([state.status.active_job?.run_id, state.status.active_job?.status, state.status.active_job?.stage, state.status.active_job?.finished_at]);
      const changedJob = signature !== state.jobSignature;
      state.jobSignature = signature;
      renderRunOptions(); renderJob(); renderPipeline();
      if (desired) {
        const activeSelected = state.status.active_job?.run_id === desired;
        const completedSelected = previousJob?.run_id === desired && isActive(previousJob) && !isActive(state.status.active_job);
        if (force || !state.run || state.runId !== desired || activeSelected || completedSelected || (changedJob && activeSelected)) await loadRun(desired);
        $("run-select").value = desired;
      } else {
        $("research-content").hidden = true; $("empty-run").hidden = false;
      }
    } catch (error) { showError(`Dashboard connection unavailable: ${error.message}`); }
    finally {
      state.refreshing = false; $("refresh").disabled = false;
      window.clearTimeout(state.polling);
      state.polling = window.setTimeout(() => refresh(), isActive(state.status?.active_job) ? 3000 : 15000);
    }
  }
  function selectTab(kind) {
    state.kind = kind; state.offset = 0;
    document.querySelectorAll(".tab").forEach((tab) => { const active = tab.dataset.kind === kind; tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active ? 0 : -1; });
    $("table-panel").setAttribute("aria-labelledby", `tab-${kind}`);
    $("side-filter-wrap").hidden = kind !== "observations";
    $("label-filter-wrap").hidden = kind === "observations";
    $("condition-filter-wrap").hidden = kind !== "occurrences";
    $("search").placeholder = kind === "observations" ? "Search observation or contract…" : "Search window, condition, or observation…";
    $("search").setAttribute("aria-label", $("search").placeholder.replace("…", ""));
    renderExplorerNote();
    if (state.runId) loadTable();
  }
  function cellValue(column, value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (typeof value === "object") return JSON.stringify(value);
    if ((column.includes("timestamp") || column.endsWith("_at") || column === "time") && validDate(value)) return preciseTime(value);
    if ((column === "trading_date" || column === "expiry" || column === "date") && validDate(value)) return day(value, true);
    if (typeof value === "number") {
      if (/^(iv|iv_decimal|source_iv|delta|gamma|theta|vega|rho|feature_value)$/.test(column) || /^(CE|PE)_/.test(column)) return String(value);
      return Number.isInteger(value) ? integer.format(value) : String(value);
    }
    return String(value);
  }
  function renderTable(payload) {
    state.total = Number(payload.total) || 0;
    const columns = payload.columns || [];
    const rows = payload.rows || [];
    const head = $("table-head"); const body = $("table-body"); head.replaceChildren(); body.replaceChildren();
    const header = element("tr");
    columns.forEach((column) => { const th = element("th", numericColumns.test(column) ? "numeric" : "", column === "feature_values_json" ? "Joint feature values" : headings[column] || (/^(CE|PE)_/.test(column) ? featureLabel(column) : humanize(column))); th.scope = "col"; header.append(th); }); head.append(header);
    rows.forEach((row) => {
      const tr = element("tr");
      columns.forEach((column) => {
        const value = row[column];
        const td = element("td", numericColumns.test(column) ? "numeric" : column.endsWith("_id") ? "identity" : "");
        const text = cellValue(column, value);
        td.title = typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? "Unavailable");
        if (["label", "target"].includes(column) && value) td.append(badge(labelNames[value] || text, String(value).startsWith("UP") ? "up" : String(value).startsWith("DOWN") ? "down" : value === "UNKNOWN" ? "warn" : ""));
        else if (["optiontype", "option_type", "option_side", "side"].includes(column) && ["CE", "PE"].includes(value)) td.append(badge(value, value.toLowerCase()));
        else if (column === "outcome_status" && value) td.append(badge(text, badgeVariant(value)));
        else if (["feature_values_json", "features_json"].includes(column) && value) renderFeatureValues(td, value);
        else td.textContent = column.endsWith("_id") && text.length > 21 ? `${text.slice(0, 10)}…${text.slice(-7)}` : text;
        tr.append(td);
      });
      body.append(tr);
    });
    $("table-empty").hidden = rows.length > 0;
    setText("table-empty", payload.integrity?.status && payload.integrity.status !== "VERIFIED" ? payload.integrity.reason : "No records match these filters. Try another session, outcome, or search term.");
    setText("table-total", `${fmt(state.total)} records`);
    setText("page-summary", state.total ? `Showing ${fmt(state.offset + 1)}–${fmt(state.offset + rows.length)} of ${fmt(state.total)}` : "0 matching records");
    setText("page-number", `${Math.floor(state.offset / state.limit) + 1} / ${Math.max(1, Math.ceil(state.total / state.limit))}`);
    $("page-prev").disabled = state.offset === 0;
    $("page-next").disabled = state.offset + state.limit >= state.total;
  }
  function renderFeatureValues(cell, value) {
    let values;
    try { values = typeof value === "string" ? JSON.parse(value) : value; } catch { cell.textContent = String(value); return; }
    if (!values || Array.isArray(values) || typeof values !== "object") { cell.textContent = detailString(value); return; }
    cell.classList.add("feature-values-cell");
    const list = element("dl", "feature-values");
    for (const [feature, featureValue] of Object.entries(values)) {
      list.append(element("dt", null, featureLabel(feature)), element("dd", null, featureValue === null || featureValue === undefined ? "—" : typeof featureValue === "object" ? JSON.stringify(featureValue) : String(featureValue)));
    }
    cell.append(list);
  }
  async function loadTable() {
    if (!state.runId) return;
    const version = ++state.tableVersion;
    const params = new URLSearchParams({ kind: state.kind, offset: String(state.offset), limit: String(state.limit) });
    if ($("filter-date").value) params.set("date", $("filter-date").value);
    if (state.kind === "observations" && $("filter-side").value) params.set("side", $("filter-side").value);
    if (state.kind !== "observations" && $("filter-label").value) params.set("label", $("filter-label").value);
    if (state.kind === "occurrences" && $("filter-condition").value) params.set("condition_id", $("filter-condition").value);
    if ($("search").value.trim()) params.set("q", $("search").value.trim());
    $("table-panel").setAttribute("aria-busy", "true");
    $("page-prev").disabled = true; $("page-next").disabled = true;
    try {
      const payload = await request(`/api/runs/${encodeURIComponent(state.runId)}/observations?${params}`);
      if (version !== state.tableVersion) return;
      renderTable(payload);
    } catch (error) {
      if (version !== state.tableVersion) return;
      $("table-head").replaceChildren(); $("table-body").replaceChildren(); $("table-empty").hidden = false;
      setText("table-empty", `Evidence table unavailable: ${error.message}`); setText("table-total", "Unavailable"); setText("page-summary", "Use refresh to try again");
    } finally { if (version === state.tableVersion) $("table-panel").setAttribute("aria-busy", "false"); }
  }
  const svgNS = "http://www.w3.org/2000/svg";
  function svgElement(tag, attrs, text) { const node = document.createElementNS(svgNS, tag); for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value)); if (text !== undefined) node.textContent = text; return node; }
  function renderChart(payload) {
    const svg = $("spot-chart"); svg.replaceChildren(); $("chart-tooltip").hidden = true;
    const points = (payload.points || []).filter((point) => validDate(point.time) && finite(point.spot)).map((point) => ({ time: point.time, timestamp: new Date(point.time).getTime(), spot: Number(point.spot), segment: point.segment })).sort((a, b) => a.timestamp - b.timestamp);
    const events = (payload.events || []).filter((event) => validDate(event.time));
    state.chart = null;
    $("chart-empty").hidden = points.length > 0;
    if (!points.length) { setText("chart-empty", "No spot observations are available for this session."); setText("chart-summary", "No saved spot points for this selection"); return; }
    const width = 1100, height = 240, left = 68, right = 24, top = 16, bottom = 33;
    const start = points[0].timestamp; const end = points[points.length - 1].timestamp;
    let min = Infinity, max = -Infinity;
    for (const point of points) { min = Math.min(min, point.spot); max = Math.max(max, point.spot); }
    const pad = Math.max((max - min) * 0.13, 1); min -= pad; max += pad;
    const x = (stamp) => left + ((stamp - start) / (end - start || 1)) * (width - left - right);
    const y = (spot) => top + ((max - spot) / (max - min)) * (height - top - bottom);
    const defs = svgElement("defs"); const gradient = svgElement("linearGradient", { id: "spot-fill", x1: "0", x2: "0", y1: "0", y2: "1" }); gradient.append(svgElement("stop", { offset: "0%", "stop-color": "#49a588", "stop-opacity": .14 }), svgElement("stop", { offset: "100%", "stop-color": "#49a588", "stop-opacity": .01 })); defs.append(gradient); svg.append(defs);
    for (let i = 0; i <= 4; i++) { const value = min + ((max - min) * i) / 4; const at = y(value); svg.append(svgElement("line", { x1: left, x2: width - right, y1: at, y2: at, class: "chart-grid" }), svgElement("text", { x: left - 13, y: at + 3, "text-anchor": "end", class: "chart-axis-text" }, integer.format(value))); }
    for (let i = 0; i <= 6; i++) { const stamp = start + ((end - start) * i) / 6; svg.append(svgElement("text", { x: x(stamp), y: height - 9, "text-anchor": "middle", class: "chart-axis-text" }, time(new Date(stamp)).slice(0, 5))); }
    for (const event of events) { const at = new Date(event.time).getTime(); const ending = validDate(event.end_time)?.getTime() || at; if (at < start || at > end) continue; const band = svgElement("rect", { x: x(at), y: top, width: Math.max(2, x(Math.min(ending, end)) - x(at)), height: height - top - bottom, fill: "#e3c778", opacity: .2 }); band.append(svgElement("title", {}, `${labelNames[event.label] || event.label} · ${time(event.time)} · ${fmt(event.point_change, true)} points`)); svg.append(band); }
    const segments = []; let segment = [];
    points.forEach((point, index) => { if (index && (point.segment !== undefined ? point.segment !== points[index - 1].segment : point.timestamp - points[index - 1].timestamp > 15000)) { if (segment.length) segments.push(segment); segment = []; } segment.push(point); }); if (segment.length) segments.push(segment);
    for (const chunk of segments) {
      const path = chunk.map((point, index) => `${index ? "L" : "M"}${x(point.timestamp).toFixed(2)},${y(point.spot).toFixed(2)}`).join(" ");
      svg.append(svgElement("path", { d: `${path} L${x(chunk[chunk.length - 1].timestamp)},${height - bottom} L${x(chunk[0].timestamp)},${height - bottom} Z`, fill: "url(#spot-fill)" }), svgElement("path", { d: path, class: "chart-line" }));
      if (chunk.length === 1) svg.append(svgElement("circle", { cx: x(chunk[0].timestamp), cy: y(chunk[0].spot), r: 1.8, fill: "#228775" }));
    }
    const hoverLine = svgElement("line", { x1: left, x2: left, y1: top, y2: height - bottom, class: "chart-hover-line", visibility: "hidden" });
    const hoverDot = svgElement("circle", { cx: left, cy: top, r: 3.5, fill: "#097f75", stroke: "white", "stroke-width": 2, visibility: "hidden" }); svg.append(hoverLine, hoverDot);
    state.chart = { points, events, start, end, x, y, left, right, width, hoverLine, hoverDot, index: 0 };
    svg.setAttribute("tabindex", "0"); svg.setAttribute("aria-label", `NIFTY spot observations for ${day(payload.date || $("chart-date").value, true)}. ${integer.format(points.length)} displayed points, ${integer.format(events.length)} qualifying windows. Use arrow keys to inspect points.`);
    const summary = `${day(payload.date || $("chart-date").value, true)} · ${fmt(points.length)} displayed points · ${fmt(events.length)} qualifying windows`;
    setText("chart-summary", summary);
  }
  function showChartPoint(index) {
    const chart = state.chart; if (!chart) return;
    index = Math.max(0, Math.min(index, chart.points.length - 1)); chart.index = index;
    const point = chart.points[index]; const x = chart.x(point.timestamp); const y = chart.y(point.spot);
    chart.hoverLine.setAttribute("x1", x); chart.hoverLine.setAttribute("x2", x); chart.hoverLine.setAttribute("visibility", "visible");
    chart.hoverDot.setAttribute("cx", x); chart.hoverDot.setAttribute("cy", y); chart.hoverDot.setAttribute("visibility", "visible");
    const tooltip = $("chart-tooltip"); tooltip.replaceChildren(element("strong", null, `${number.format(point.spot)} NIFTY`), element("div", null, `${time(point.time)} IST`));
    const matched = chart.events.find((event) => point.timestamp >= new Date(event.time).getTime() && point.timestamp <= (validDate(event.end_time)?.getTime() || new Date(event.time).getTime()));
    if (matched) tooltip.append(element("div", null, `${labelNames[matched.label] || matched.label} · ${fmt(matched.point_change, true)} pts`));
    tooltip.hidden = false;
    const area = $("chart-area").getBoundingClientRect(); const plot = $("spot-chart").getBoundingClientRect();
    tooltip.style.left = `${Math.max(5, Math.min((x / chart.width) * plot.width + plot.left - area.left + 12, area.width - tooltip.offsetWidth - 8))}px`;
    tooltip.style.top = `${Math.max(8, (y / 240) * plot.height - tooltip.offsetHeight - 10)}px`;
  }
  async function loadChart() {
    if (!state.runId) return;
    const version = ++state.chartVersion;
    const params = new URLSearchParams(); if ($("chart-date").value) params.set("date", $("chart-date").value);
    try {
      const payload = await request(`/api/runs/${encodeURIComponent(state.runId)}/series?${params}`);
      if (version !== state.chartVersion) return; renderChart(payload);
    } catch (error) { if (version !== state.chartVersion) return; state.chart = null; $("spot-chart").replaceChildren(); $("chart-empty").hidden = false; setText("chart-empty", `Spot observations unavailable: ${error.message}`); }
  }
  function openDialog() {
    if (state.launching || isActive(state.status?.active_job)) return;
    const defaults = state.status?.defaults || {};
    $("min-support").value = defaults.min_support ?? 5;
    $("max-conditions").value = defaults.max_conditions ?? 6;
    $("discovery-end").value = defaults.discovery_end || "";
    $("publish-s3").checked = false;
    $("run-form-error").hidden = true;
    $("run-dialog").showModal();
  }
  async function launchRun(event) {
    event.preventDefault(); if (state.launching || !$("run-form").reportValidity()) return;
    state.launching = true; updateLaunchButtons(); $("submit-run").disabled = true; $("run-form-error").hidden = true;
    const body = { discovery_end: $("discovery-end").value || null, min_support: Number($("min-support").value), max_conditions: Number($("max-conditions").value), publish_to_s3: $("publish-s3").checked };
    try {
      const payload = await request("/api/runs", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.status.csrf_token }, body: JSON.stringify(body) });
      state.run = null; state.runId = payload.run_id || payload.job?.run_id;
      if (payload.job) state.status.active_job = payload.job;
      $("run-dialog").close(); toast("Research run started. Progress updates automatically."); renderJob(); renderPipeline();
      await refresh({ force: true });
    } catch (error) { setText("run-form-error", error.message); $("run-form-error").hidden = false; }
    finally { state.launching = false; $("submit-run").disabled = false; updateLaunchButtons(); }
  }
  async function resumeRun() {
    const id = $("resume-run").dataset.runId; if (!id || state.launching) return;
    state.launching = true; $("resume-run").disabled = true; updateLaunchButtons();
    try {
      const payload = await request(`/api/runs/${encodeURIComponent(id)}/resume`, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.status.csrf_token }, body: "{}" });
      state.runId = payload.run_id || payload.job?.run_id || id; state.run = null;
      if (payload.job) state.status.active_job = payload.job;
      toast("Resuming research from its saved checkpoint."); await refresh({ force: true });
    } catch (error) { showError(`Could not resume this run: ${error.message}`); }
    finally { state.launching = false; $("resume-run").disabled = false; updateLaunchButtons(); }
  }

  $("refresh").addEventListener("click", () => refresh({ force: true }));
  $("run-select").addEventListener("change", (event) => { if (event.target.value) { clearError(); loadRun(event.target.value); } });
  $("start-run").addEventListener("click", openDialog); $("start-first-run").addEventListener("click", openDialog); $("start-latest-run").addEventListener("click", openDialog);
  $("close-dialog").addEventListener("click", () => $("run-dialog").close());
  $("run-dialog").addEventListener("click", (event) => { if (event.target === $("run-dialog")) { const rect = $("run-dialog").getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) $("run-dialog").close(); } });
  $("run-form").addEventListener("submit", launchRun); $("resume-run").addEventListener("click", resumeRun);
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => selectTab(tab.dataset.kind));
    tab.addEventListener("keydown", (event) => { const tabs = [...document.querySelectorAll(".tab")]; const index = tabs.indexOf(tab); let next; if (event.key === "ArrowRight") next = (index + 1) % tabs.length; if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length; if (event.key === "Home") next = 0; if (event.key === "End") next = tabs.length - 1; if (next !== undefined) { event.preventDefault(); tabs[next].focus(); selectTab(tabs[next].dataset.kind); } });
  });
  ["filter-date", "filter-side", "filter-label", "filter-condition"].forEach((id) => $(id).addEventListener("change", () => { state.offset = 0; loadTable(); }));
  let searchTimer; $("search").addEventListener("input", () => { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(() => { state.offset = 0; loadTable(); }, 250); });
  $("page-prev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); loadTable(); });
  $("page-next").addEventListener("click", () => { if (state.offset + state.limit < state.total) { state.offset += state.limit; loadTable(); } });
  $("chart-date").addEventListener("change", loadChart);
  $("spot-chart").addEventListener("pointermove", (event) => {
    const chart = state.chart; if (!chart) return;
    const rect = $("spot-chart").getBoundingClientRect(); const x = ((event.clientX - rect.left) / rect.width) * chart.width;
    const stamp = chart.start + ((x - chart.left) / (chart.width - chart.left - chart.right)) * (chart.end - chart.start);
    let lo = 0, hi = chart.points.length - 1; while (lo < hi) { const mid = Math.floor((lo + hi) / 2); if (chart.points[mid].timestamp < stamp) lo = mid + 1; else hi = mid; }
    if (lo > 0 && stamp - chart.points[lo - 1].timestamp < chart.points[lo].timestamp - stamp) lo--; showChartPoint(lo);
  });
  function hideChartPoint() { $("chart-tooltip").hidden = true; if (state.chart) { state.chart.hoverLine.setAttribute("visibility", "hidden"); state.chart.hoverDot.setAttribute("visibility", "hidden"); } }
  $("spot-chart").addEventListener("pointerleave", hideChartPoint); $("spot-chart").addEventListener("blur", hideChartPoint);
  $("spot-chart").addEventListener("focus", () => showChartPoint(state.chart?.index || 0));
  $("spot-chart").addEventListener("keydown", (event) => { if (!state.chart) return; if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); const step = event.shiftKey ? 60 : 1; showChartPoint(event.key === "Home" ? 0 : event.key === "End" ? state.chart.points.length - 1 : state.chart.index + (event.key === "ArrowRight" ? step : -step)); } });
  document.querySelectorAll(".nav-link").forEach((link) => link.addEventListener("click", () => { document.querySelectorAll(".nav-link").forEach((item) => item.classList.toggle("active", item === link)); }));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  $("start-run").disabled = true; $("start-first-run").disabled = true; renderPipeline(); refresh();
})();
