const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "google_apps_script.gs"), "utf8");
const context = { URL };
vm.runInNewContext(source, context);

test("uses the automatically collected respondent email", () => {
  assert.equal(context.recipientFromForm_({ "Email Address": ["person@example.com"] }), "person@example.com");
});

test("keeps compatibility with the explicit recipient question and prioritizes it", () => {
  assert.equal(context.recipientFromForm_({
    "Recipient Email Address": ["reports@example.com"],
    "Email Address": ["respondent@example.com"],
  }), "reports@example.com");
});

test("accepts the alternate email header capitalization", () => {
  assert.equal(context.recipientFromForm_({ "Email address": ["person@example.com"] }), "person@example.com");
});

test("accepts a Flipkart product listing URL as the product identifier", () => {
  assert.equal(context.validProductUrl_("https://www.flipkart.com/audio/p/itm123"), true);
});

test("rejects an empty product URL", () => {
  assert.equal(context.validProductUrl_(""), false);
});

test("builds string-only workflow inputs with the product URL and no separate product name", () => {
  const inputs = context.buildWorkflowInputs_({
    requestId: "REQ-20261102-001",
    productUrl: "https://www.flipkart.com/audio/p/itm123",
    duration: 2,
    durationUnit: "weeks",
    recipientEmail: "person@example.com",
    startDate: "2026-10-19",
    endDate: "2026-11-02",
  });
  assert.deepEqual(Object.keys(inputs).sort(), [
    "DURATION", "DURATION_UNIT", "END_DATE", "PRODUCT_URL", "RECIPIENT_EMAIL", "REQUEST_ID", "START_DATE",
  ]);
  assert.ok(Object.values(inputs).every((v) => typeof v === "string"));
  assert.equal(inputs.DURATION, "2");
  assert.equal(inputs.PRODUCT_URL, "https://www.flipkart.com/audio/p/itm123");
  assert.equal(inputs.PRODUCT_NAME, undefined);
});

test("dispatches the workflow through the GitHub API with a bearer token", () => {
  const props = { GITHUB_REPOSITORY: "owner/repo", GITHUB_TOKEN: "tok" };
  let call;
  const ctx = {
    URL,
    PropertiesService: { getScriptProperties: () => ({ getProperty: (k) => props[k] || null }) },
    UrlFetchApp: { fetch: (url, opts) => { call = { url, opts }; return { getResponseCode: () => 204, getContentText: () => "" }; } },
  };
  vm.runInNewContext(source, ctx);
  ctx.dispatchWorkflow_({ REQUEST_ID: "REQ-20261102-001" });
  assert.equal(call.url, "https://api.github.com/repos/owner/repo/actions/workflows/flipkart-request.yml/dispatches");
  assert.equal(call.opts.headers.Authorization, "Bearer tok");
  assert.deepEqual(JSON.parse(call.opts.payload), { ref: "main", inputs: { REQUEST_ID: "REQ-20261102-001" } });
});

test("a non-204 GitHub response is reported as a failure without the token", () => {
  const props = { GITHUB_REPOSITORY: "owner/repo", GITHUB_TOKEN: "secret-token" };
  const ctx = {
    URL,
    PropertiesService: { getScriptProperties: () => ({ getProperty: (k) => props[k] || null }) },
    UrlFetchApp: { fetch: () => ({ getResponseCode: () => 404, getContentText: () => "Not Found" }) },
  };
  vm.runInNewContext(source, ctx);
  assert.throws(() => ctx.dispatchWorkflow_({}), (err) => /HTTP 404/.test(err.message) && !/secret-token/.test(err.message));
});
