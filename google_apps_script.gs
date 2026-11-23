/**
 * Installable on-form-submit trigger for the linked Form Responses sheet.
 * Validates the request and starts the GitHub Actions workflow directly through the GitHub API
 * (workflow_dispatch). No Gmail listener or Task Scheduler job is needed.
 * Configure every Script Property listed in README; never put credentials in this source.
 *
 * Script Properties used here:
 *   SPREADSHEET_ID, RESPONSE_SHEET_NAME, SHEETS_CALLBACK_SECRET   (as before)
 *   GITHUB_TOKEN        fine-grained token, this repo only, Actions: read and write
 *   GITHUB_REPOSITORY   owner/repository
 *   GITHUB_WORKFLOW     optional, default flipkart-request.yml
 *   GITHUB_REF          optional, default main
 */
const STATUS_HEADERS = ["Request ID", "Start Date", "End Date", "Request Status", "GitHub Actions Run", "Status Details", "Updated At"];

function onFormSubmit(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  let sheet, row, cols;
  try {
    if (!e || !e.range) throw new Error("Installable spreadsheet form-submit event is required");
    sheet = e.range.getSheet();
    row = e.range.getRow();
    cols = ensureStatusColumns_(sheet);
    const vals = e.namedValues || {};
    const pick = (label) => (vals[label] && vals[label][0] || "").trim();
    const durationRaw = pick("Duration");
    const unit = pick("Duration Unit").toLowerCase();
    const recipient = recipientFromForm_(vals);
    const productUrl = pick("Flipkart Product URL");
    if (!productUrl || !validProductUrl_(productUrl)) throw new Error("Enter a valid HTTPS Flipkart product URL");
    if (!/^\d+$/.test(durationRaw) || Number(durationRaw) < 1 || Number(durationRaw) > 120) throw new Error("Duration must be an integer from 1 to 120");
    if (!["weeks", "months", "years"].includes(unit)) throw new Error("Duration Unit must be Weeks, Months, or Years");
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(recipient)) throw new Error("Collect respondent email addresses in Google Forms or provide a valid Recipient Email Address");

    const now = new Date();
    const tz = Session.getScriptTimeZone();
    const end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const start = new Date(end);
    const n = Number(durationRaw);
    if (unit === "weeks") start.setDate(start.getDate() - 7 * n);
    else subtractMonthsClamped_(start, unit === "months" ? n : n * 12);
    const startDate = Utilities.formatDate(start, tz, "yyyy-MM-dd");
    const endDate = Utilities.formatDate(end, tz, "yyyy-MM-dd");
    const id = nextRequestId_(now, tz);
    writeStatus_(sheet, row, cols, {"Request ID": id, "Start Date": startDate, "End Date": endDate,
      "Request Status": "Pending", "GitHub Actions Run": "", "Status Details": "", "Updated At": new Date()});
    const inputs = buildWorkflowInputs_({requestId: id, productUrl: productUrl,
      duration: n, durationUnit: unit, recipientEmail: recipient, startDate: startDate, endDate: endDate});
    dispatchWorkflow_(inputs);
    writeStatus_(sheet, row, cols, {"Request Status": "Queued", "GitHub Actions Run": "",
      "Status Details": "Workflow dispatched to GitHub Actions; waiting for the self-hosted runner", "Updated At": new Date()});
  } catch (err) {
    if (sheet && row && cols) writeStatus_(sheet, row, cols, {
      "Request Status": "Failed", "Status Details": String(err && err.message || err).slice(0, 500), "Updated At": new Date()});
    throw err;
  } finally {
    lock.releaseLock();
  }
}

function doPost(e) {
  const props = PropertiesService.getScriptProperties();
  const expected = props.getProperty("SHEETS_CALLBACK_SECRET");
  const payload = JSON.parse(e && e.postData && e.postData.contents || "{}");
  if (!expected || !payload.secret || payload.secret !== expected) return json_({ok: false, error: "unauthorized"});
  const id = String(payload.request_id || "");
  if (!/^REQ-\d{8}-[A-Za-z0-9_-]{1,40}$/.test(id)) return json_({ok: false, error: "invalid request id"});
  const ss = SpreadsheetApp.openById(requiredProp_("SPREADSHEET_ID"));
  const sheet = ss.getSheetByName(requiredProp_("RESPONSE_SHEET_NAME"));
  if (!sheet) return json_({ok: false, error: "response sheet not found"});
  const cols = ensureStatusColumns_(sheet);
  const idCol = cols["Request ID"];
  const found = sheet.getRange(2, idCol, Math.max(sheet.getLastRow() - 1, 1), 1).createTextFinder(id).matchEntireCell(true).findNext();
  if (!found) return json_({ok: false, error: "request not found"});
  writeStatus_(sheet, found.getRow(), cols, {"Request Status": String(payload.status || "Failed"),
    "Status Details": String(payload.details || "").slice(0, 500), "GitHub Actions Run": String(payload.job_url || ""), "Updated At": new Date()});
  return json_({ok: true});
}

// workflow_dispatch input values must all be strings.
function buildWorkflowInputs_(fields) {
  return {
    REQUEST_ID: String(fields.requestId),
    PRODUCT_URL: String(fields.productUrl),
    DURATION: String(fields.duration),
    DURATION_UNIT: String(fields.durationUnit),
    RECIPIENT_EMAIL: String(fields.recipientEmail),
    START_DATE: String(fields.startDate),
    END_DATE: String(fields.endDate)
  };
}

function dispatchWorkflow_(inputs) {
  const props = PropertiesService.getScriptProperties();
  const repo = requiredProp_("GITHUB_REPOSITORY");
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) throw new Error("GITHUB_REPOSITORY must look like owner/repository");
  const workflow = props.getProperty("GITHUB_WORKFLOW") || "flipkart-request.yml";
  const ref = props.getProperty("GITHUB_REF") || "main";
  const url = "https://api.github.com/repos/" + repo + "/actions/workflows/" + encodeURIComponent(workflow) + "/dispatches";
  const response = UrlFetchApp.fetch(url, {
    method: "post",
    contentType: "application/json",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: "Bearer " + requiredProp_("GITHUB_TOKEN"),
      "X-GitHub-Api-Version": "2022-11-28"
    },
    payload: JSON.stringify({ref: ref, inputs: inputs}),
    muteHttpExceptions: true
  });
  const code = response.getResponseCode();
  if (code !== 204) {
    throw new Error("GitHub rejected the workflow dispatch (HTTP " + code + "): " + response.getContentText().slice(0, 300));
  }
}

function recipientFromForm_(values) {
  const supplied = (values["Recipient Email Address"] || [""])[0].trim();
  const collected = (values["Email Address"] || values["Email address"] || [""])[0].trim();
  return supplied || collected;
}

function validProductUrl_(value) {
  try {
    const u = new URL(value);
    return u.protocol === "https:" && !u.username && !u.password && (u.port === "" || u.port === "443") &&
      (u.hostname === "flipkart.com" || u.hostname.endsWith(".flipkart.com")) && /\/p\//.test(u.pathname);
  } catch (_) { return false; }
}

function ensureStatusColumns_(sheet) {
  const width = Math.max(sheet.getLastColumn(), 1);
  const headers = sheet.getRange(1, 1, 1, width).getValues()[0].map(String);
  STATUS_HEADERS.forEach(h => { if (headers.indexOf(h) < 0) { headers.push(h); sheet.getRange(1, headers.length).setValue(h); } });
  const map = {}; headers.forEach((h, i) => { if (STATUS_HEADERS.indexOf(h) >= 0) map[h] = i + 1; });
  return map;
}

function writeStatus_(sheet, row, cols, values) {
  Object.keys(values).forEach(k => { if (cols[k]) sheet.getRange(row, cols[k]).setValue(values[k]); });
}

function nextRequestId_(now, tz) {
  const day = Utilities.formatDate(now, tz, "yyyyMMdd");
  const props = PropertiesService.getScriptProperties();
  const key = "REQUEST_COUNTER_" + day;
  const next = Number(props.getProperty(key) || "0") + 1;
  props.setProperty(key, String(next));
  return "REQ-" + day + "-" + String(next).padStart(3, "0");
}

function subtractMonthsClamped_(day, months) {
  const originalDay = day.getDate();
  day.setDate(1);
  day.setMonth(day.getMonth() - months);
  const last = new Date(day.getFullYear(), day.getMonth() + 1, 0).getDate();
  day.setDate(Math.min(originalDay, last));
}

function requiredProp_(name) {
  const value = PropertiesService.getScriptProperties().getProperty(name);
  if (!value) throw new Error("Missing Apps Script property: " + name);
  return value;
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}
