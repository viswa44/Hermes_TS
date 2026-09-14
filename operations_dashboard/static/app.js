"use strict";

const $ = id => document.getElementById(id);
let selectedDate = new URLSearchParams(window.location.search).get("date") || "";
let selectedTable = "observations";
let preview = null;
let refreshing = false;
let pendingRefresh = false;
let activeSequence = 0;
const number = value => value === null || value === undefined ? "—" : Number(value).toLocaleString("en-IN");
const dateText = (value, options = {}) => {
  if (!value) return "—";
  const parsed = new Date(value.length === 10 ? value + "T00:00:00+05:30" : value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return new Intl.DateTimeFormat("en-GB", {timeZone:"Asia/Kolkata", day:"2-digit", month:"short", ...options}).format(parsed);
};
const timestamp = value => dateText(value, {year:"numeric", hour:"2-digit", minute:"2-digit", hour12:false}) + (value ? " IST" : "");
const receiptTime = value => dateText(value, {year:"numeric", hour:"2-digit", minute:"2-digit", second:"2-digit", fractionalSecondDigits:3, hour12:false}) + " IST";
const write = (id, text) => { $(id).textContent = text; };
const element = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
const badge = (id, text, kind = "neutral") => { write(id, text); $(id).className = "badge " + kind; };
const link = (id, href) => { $(id).hidden = !href; if (href) $(id).href = href; };
const niceStatus = value => (value || "UNKNOWN").replaceAll("_", " ");

async function fetchJSON(path) {
  const response = await fetch(path, {cache:"no-store", signal:AbortSignal.timeout(15000)});
  if (!response.ok) throw new Error("The local dashboard could not read the latest evidence (HTTP " + response.status + ").");
  return response.json();
}

function renderCalendar(calendar, today) {
  const gate = calendar.today;
  const isOpen = gate.allowed === true;
  const isClosed = gate.status === "CLOSED";
  write("gate-date", dateText(today, {weekday:"short", year:"numeric"}).toUpperCase());
  write("gateway-heading", isOpen ? "Market session permitted" : isClosed ? "Market closed today" : "Calendar needs attention");
  badge("gate-badge", isOpen ? "JOBS PERMITTED" : "JOBS BLOCKED", isOpen ? "good" : isClosed ? "closed" : "warn");
  $("gate-indicator").className = "gate-indicator" + (isOpen ? " open" : "");
  write("gate-reason", gate.reason || "A verified calendar decision is required before market jobs can start.");
  write("gate-source-label", gate.circular_reference || "Official NSE market calendar");
  link("gate-source", gate.source_url);
  link("calendar-link", gate.source_url);
  write("next-session", gate.next_open_date ? dateText(gate.next_open_date, {weekday:"long", year:"numeric"}) : "Awaiting verified calendar");
  write("next-session-detail", gate.next_open_date ? "Collection 09:15 · Cleaning 15:45 IST" : "The gateway blocks jobs while the calendar is unknown");
  badge("calendar-badge", isOpen ? "OPEN" : isClosed ? "CLOSED" : "UNKNOWN", isOpen ? "good" : isClosed ? "neutral" : "warn");
  write("calendar-health", gate.fetched_at ? "Official cache · " + dateText(gate.fetched_at) : "Waiting for official calendar cache");
  write("calendar-cache-time", gate.fetched_at ? "Fetched " + timestamp(gate.fetched_at) : "No valid calendar cache");
  write("calendar-expiry", timestamp(gate.expires_at));
  write("calendar-refresh", niceStatus(calendar.refresh.status) + " · " + timestamp(calendar.refresh.attempted_at));
  const list = $("holiday-list"); list.replaceChildren();
  if (!calendar.upcoming_holidays.length) list.append(element("p", "empty", "No upcoming holidays are available in the validated cache."));
  calendar.upcoming_holidays.forEach(holiday => {
    const row = element("div", "holiday");
    const box = element("div", "holiday-date");
    box.append(element("span", "", dateText(holiday.date, {month:"short", day:undefined})), element("strong", "", holiday.date.slice(8)));
    const name = element("div", "holiday-name");
    name.append(element("strong", "", holiday.name || holiday.description || holiday.reason || "Market holiday"), element("span", "", dateText(holiday.date, {weekday:"long", year:"numeric"})));
    const special = /special|muhurat/i.test(holiday.session_type || holiday.type || "");
    if (special) name.append(element("span", "", "Special session · separate schedule required"));
    row.append(box, name, element("span", "badge " + (special ? "warn" : "neutral"), special ? "SPECIAL" : "CLOSED"));
    list.append(row);
  });
}

function renderMetrics(data) {
  const day = data.selected;
  const quality = day.quality || {};
  const rows = day.table_counts["observations.parquet"];
  write("clean-count", number(rows));
  write("clean-count-detail", rows === undefined ? "No local table manifest for this date" : "Observations " + number(rows) + " · Options " + number(day.table_counts["options.parquet"]));
  write("quality-state", quality.passed === true ? "Cleaning quality PASS" : quality.passed === false ? "Cleaning quality FAIL" : "Quality report unavailable");
  $("quality-indicator").className = "small-dot " + (quality.passed === true ? "green" : quality.passed === false ? "red" : "amber");
  write("quarantine-count", number(quality.quarantined_rows));
  write("quarantine-detail", quality.quarantined_rows === 0 ? "No rejected rows in this export" : "Rejected rows retained locally");
  const captured = day.coverage.reduce((sum, item) => sum + (item.captured_distinct_slots || 0), 0);
  const expected = day.coverage.reduce((sum, item) => sum + (item.expected_slots || 0), 0);
  const percent = expected ? captured / expected * 100 : null;
  write("coverage-value", percent === null ? "—" : percent.toFixed(2) + "%");
  write("coverage-detail", expected ? number(captured) + " / " + number(expected) + " slots · " + number(expected - captured) + " missing" : "Receipt slot report unavailable");
  $("coverage-bar").style.width = Math.min(100, percent || 0) + "%";
  const selectedGate = data.calendar.selected_day;
  write("selected-description", dateText(day.date, {weekday:"long", year:"numeric"}) + " · " + niceStatus(selectedGate.status).toLowerCase() + " session · " + (day.status === "PUBLISHED" ? "published to S3" : niceStatus(day.status).toLowerCase()));
  write("quality-note", "A cleaning PASS validates the rows received. " + (expected ? number(expected - captured) + " of " + number(expected) + " expected receipt slots are missing on this date. " : "Receipt coverage must be checked separately. ") + "Provider event time remains unverified.");
  write("iv-note", day.greeks_enabled === false ? "Raw IV is unavailable in this PostgreSQL flow. IV and Greeks remain null; days to expiry is calculated." : (day.iv_provenance || "Missing values remain null. Derived values require explicit model assumptions."));
  link("aws-link", day.s3_console_url);
}

function renderExports(data) {
  const body = $("export-body"); body.replaceChildren();
  write("export-count", data.days.length + " sessions");
  if (!data.days.length) { const row = element("tr"); const cell = element("td", "empty", "No export evidence has been recorded."); cell.colSpan = 5; row.append(cell); body.append(row); }
  data.days.forEach(day => {
    const row = element("tr", day.date === selectedDate ? "selected-row" : "");
    const dateCell = element("td"); const button = element("button", "date-button", dateText(day.date, {year:"numeric"})); button.type = "button"; button.addEventListener("click", () => selectDate(day.date)); dateCell.append(button);
    const rows = day.table_counts["observations.parquet"];
    const count = element("td", "", rows === undefined ? "—" : number(rows));
    const quality = element("td"); quality.append(element("span", "badge " + (day.quality.passed === true ? "good" : day.quality.passed === false ? "bad" : "neutral"), day.quality.passed === true ? "PASS" : day.quality.passed === false ? "FAIL" : "UNKNOWN"));
    const status = element("td"); status.append(element("span", "badge " + (day.status === "PUBLISHED" ? "good" : "warn"), niceStatus(day.status)));
    const target = element("td"); if (day.s3_console_url) { const anchor = element("a", "", "↗"); anchor.href = day.s3_console_url; anchor.target = "_blank"; anchor.rel = "noopener noreferrer"; anchor.setAttribute("aria-label", "Open " + day.date + " in AWS S3"); target.append(anchor); }
    row.append(dateCell, count, quality, status, target); body.append(row);
  });
  const verified = data.s3_verification.days.filter(day => day.checksums_verified === true).length;
  write("s3-evidence", verified ? "Downloaded checksums verified for " + verified + " sessions · historical evidence, no live S3 poll" : "Publication reflects saved commit evidence · no live S3 poll");
}

function renderOperations(data) {
  const closed = data.calendar.today.allowed !== true;
  const state = data.collector.state || "UNKNOWN";
  const age = data.collector.heartbeat_age_seconds;
  const fresh = age !== null && age !== undefined && age < 90;
  const running = state === "RUNNING" || state === "COLLECTING";
  badge("collector-badge", closed ? "GATE BLOCKED" : running && fresh ? "ACTIVE" : running ? "STALE" : niceStatus(state), closed ? "neutral" : running && fresh ? "good" : running ? "warn" : "neutral");
  const cleanState = data.cleaning.status || "UNKNOWN";
  badge("cleaner-badge", closed ? "GATE BLOCKED" : niceStatus(cleanState), /FAIL|ERROR/.test(cleanState) ? "bad" : "neutral");
  write("collector-heartbeat", timestamp(data.collector.heartbeat_at));
  write("collector-state", niceStatus(state) + (fresh ? " · recent heartbeat" : closed ? " · outside permitted session" : " · historical heartbeat"));
  write("cleaning-check", timestamp(data.cleaning.checked_at));
  for (const component of ["collector", "watchdog", "cleaner"]) {
    const gate = data.gates[component];
    write(component + "-gate", gate.checked_at ? niceStatus(gate.status) + " · " + timestamp(gate.checked_at) : "No startup check recorded");
  }
}

function renderPreview() {
  const table = preview && preview.tables[selectedTable];
  const head = $("preview-head"), body = $("preview-body"); head.replaceChildren(); body.replaceChildren();
  $("preview-empty").hidden = Boolean(table && table.rows.length);
  write("preview-empty", preview ? preview.reason || "No rows are available for this date." : "Loading local Parquet preview…");
  write("preview-description", selectedTable === "observations" ? "Timestamps, spot, supplied IV and volume, with source provenance." : "Raw OI, LTP and contract fields, plus days to expiry and optional model Greeks.");
  document.querySelectorAll(".tab").forEach(tab => { const active = tab.dataset.table === selectedTable; tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active)); });
  if (!table) { write("preview-summary", preview ? "Local cleaned Parquet preview unavailable" : "Loading local Parquet preview…"); return; }
  const header = element("tr"); table.columns.forEach(column => header.append(element("th", "", column === "timestamps" ? "timestamps (IST)" : column.replaceAll("_", " ")))); head.append(header);
  table.rows.forEach(record => { const row = element("tr"); table.columns.forEach(column => { const value = record[column]; const formatted = value === null || value === undefined ? "null" : column === "timestamps" ? receiptTime(value) : typeof value === "number" && !Number.isInteger(value) ? value.toLocaleString("en-US", {maximumFractionDigits:5, useGrouping:false}) : String(value); const cell = element("td", value === null ? "null" : column === "observation_id" ? "identity" : "", formatted); cell.title = value === null ? "null" : String(value); row.append(cell); }); body.append(row); });
  write("preview-summary", "Showing first " + table.rows.length + " of " + number(table.total_rows) + " rows · local export");
}

async function renderDatabase(day, sequence) {
  write("raw-count", "—"); write("raw-count-detail", "Checking selected-date PostgreSQL receipts"); write("db-state", "Read-only query"); $("db-indicator").className = "small-dot";
  try {
    const result = await fetchJSON("/api/database?date=" + encodeURIComponent(day));
    if (sequence !== activeSequence) return;
    if (result.status === "CONNECTED") {
      write("raw-count", number(result.option_rows)); write("raw-count-detail", number(result.market_rows) + " spot rows · whole IST date"); write("db-state", "PostgreSQL connected · read-only" + (result.cached ? " · cached" : "")); $("db-indicator").className = "small-dot green";
    } else { write("raw-count-detail", "Live database check " + result.status.toLowerCase()); write("db-state", "Local export evidence remains available"); $("db-indicator").className = "small-dot amber"; }
  } catch { if (sequence !== activeSequence) return; write("raw-count-detail", "Database check unavailable"); write("db-state", "Local evidence shown separately"); $("db-indicator").className = "small-dot amber"; }
}

async function refresh() {
  if (refreshing) return;
  pendingRefresh = false;
  refreshing = true; $("refresh-button").disabled = true;
  const sequence = ++activeSequence;
  try {
    const result = await fetchJSON("/api/status" + (selectedDate ? "?date=" + encodeURIComponent(selectedDate) : ""));
    if (sequence !== activeSequence) return;
    selectedDate = result.selected_date; $("date-picker").value = selectedDate;
    $("error-banner").hidden = true;
    renderCalendar(result.calendar, result.today); renderMetrics(result); renderExports(result); renderOperations(result);
    write("refresh-state", "Evidence updated"); write("refresh-time", dateText(result.refreshed_at, {hour:"2-digit", minute:"2-digit", second:"2-digit", day:undefined, month:undefined}) + " IST · every 30s");
    preview = null; renderPreview();
    const samples = fetchJSON("/api/day?date=" + encodeURIComponent(selectedDate) + "&limit=5").then(value => { if (sequence === activeSequence) { preview = value; renderPreview(); } }).catch(() => { if (sequence === activeSequence) { preview = {tables:{}, reason:"Local Parquet preview could not be loaded."}; renderPreview(); } });
    await Promise.all([samples, renderDatabase(selectedDate, sequence)]);
  } catch (error) {
    write("refresh-state", "Refresh unavailable"); write("error-banner", error.message + " Previously displayed values may be stale."); $("error-banner").hidden = false;
  } finally {
    refreshing = false; $("refresh-button").disabled = false;
    if (pendingRefresh) queueMicrotask(refresh);
  }
}

function selectDate(day) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return;
  selectedDate = day; $("date-picker").value = day;
  const url = new URL(window.location.href); url.searchParams.set("date", day); window.history.replaceState({}, "", url);
  if (refreshing) { activeSequence++; pendingRefresh = true; } else refresh();
}

$("refresh-button").addEventListener("click", refresh);
$("date-picker").addEventListener("change", event => selectDate(event.target.value));
document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => { selectedTable = tab.dataset.table; renderPreview(); }));
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 30000);
