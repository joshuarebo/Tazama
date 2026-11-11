/**
 * Verifies the standalone demo page that gets emailed out.
 *
 * It is a single file with no network access, so the things that can break are
 * exactly: a render that throws, a tab that renders nothing, an interaction that
 * does not fire, and a console error. This checks all four, over every tab and
 * every role, then writes screenshots for the email.
 *
 * Run: node tests/verify-demo-file.mjs
 */
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { pathToFileURL } from "node:url";
import path from "node:path";

const FILE = path.resolve("../docs/demo/tazama-demo.html");
const SHOTS = path.resolve("../artifacts/demo-screenshots");
mkdirSync(SHOTS, { recursive: true });

const TABS = ["how", "home", "incident", "complaints", "care", "explorer",
  "insights", "reports", "ai", "live"];

const problems = [];
let shots = 0;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1340, height: 950 } });

page.on("console", (m) => {
  if (m.type() === "error") problems.push(`console error: ${m.text()}`);
});
page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));

await page.goto(pathToFileURL(FILE).href);

// --- every tab renders real content, as admin -------------------------------
for (const id of TABS) {
  const tab = page.locator(`[data-tab="${id}"]`);
  if (!(await tab.count())) { problems.push(`tab missing: ${id}`); continue; }
  await tab.click();
  const text = (await page.locator("main").innerText()).trim();
  if (text.length < 400) problems.push(`tab ${id} rendered only ${text.length} chars`);
  if (/undefined|NaN|\[object Object\]/.test(text)) {
    problems.push(`tab ${id} contains a rendering artefact (undefined/NaN/[object Object])`);
  }
  await page.screenshot({ path: path.join(SHOTS, `${String(++shots).padStart(2, "0")}-${id}.png`),
    fullPage: true, animations: "disabled" });
}

// --- the diagnosis panel is opt-in and then shows the trace -----------------
await page.locator('[data-tab="incident"]').click();
if (await page.getByText(/What the agent did/i).count()) {
  problems.push("the agent trace was visible before Diagnose was pressed");
}
await page.locator("#diagnose").click();
await page.getByText(/What the agent did/i).waitFor({ timeout: 5000 });
for (const needed of [/planned_maintenance|planned maintenance/i, /0\.95/, /execute_runbook/,
  /get_topology/]) {
  if (!(await page.getByText(needed).count())) {
    problems.push(`diagnosis panel is missing ${needed}`);
  }
}
await page.screenshot({ path: path.join(SHOTS, "11-incident-diagnosed.png"), fullPage: true, animations: "disabled" });

// --- the explorer shows SQL before any result, and refuses the attack -------
await page.locator('[data-tab="explorer"]').click();
await page.locator('[data-ask="ok"]').click();
await page.getByText(/Would accept/i).waitFor({ timeout: 5000 });
if (await page.locator("table caption", { hasText: /Query result/i }).count()) {
  problems.push("a query result appeared before the human pressed Run");
}
await page.screenshot({ path: path.join(SHOTS, "12-explorer-suggestion.png"), fullPage: true, animations: "disabled" });

await page.locator("#use").click();
if (!(await page.locator("#sqlbox").inputValue()).toUpperCase().startsWith("SELECT")) {
  problems.push("Use this query did not load the SQL into the editor");
}
await page.locator("#run").click();
await page.locator("table caption", { hasText: /Query result/i }).waitFor({ timeout: 5000 });
if (!(await page.getByText(/6,074|6074/).count())) {
  problems.push("the executed result did not render its rows");
}
await page.screenshot({ path: path.join(SHOTS, "13-explorer-result.png"), fullPage: true, animations: "disabled" });

// a destructive statement typed by hand is still refused
await page.locator("#sqlbox").fill("DELETE FROM complaint");
await page.locator("#run").click();
await page.getByText(/Refused by the SQL guard/i).waitFor({ timeout: 5000 });

// the injected question is refused up front
await page.locator('[data-ask="bad"]').click();
await page.getByText(/Refused:/i).waitFor({ timeout: 5000 });
await page.screenshot({ path: path.join(SHOTS, "14-explorer-refused.png"), fullPage: true, animations: "disabled" });

// --- role filtering hides what it hides in the real console -----------------
await page.selectOption("#role", "care_agent");
if (await page.locator('[data-tab="explorer"]').count()) {
  problems.push("a care agent can still see the Data Explorer tab");
}
for (const t of ["care", "home", "complaints"]) {
  // incident:read and complaint:read DO include care_agent in core/security.py
  if (!(await page.locator(`[data-tab="${t}"]`).count())) {
    problems.push(`a care agent cannot see ${t}, but holds the capability for it`);
  }
}
await page.selectOption("#role", "manager");
if (await page.locator('[data-tab="care"]').count()) {
  problems.push("a manager can see Care lookup, which holds subscriber data");
}
if (!(await page.locator('[data-tab="complaints"]').count())) {
  problems.push("a manager cannot see Complaints, but holds complaint:read");
}
await page.selectOption("#role", "noc_engineer");
if (await page.locator('[data-tab="explorer"]').count()) {
  problems.push("a NOC engineer can see the Data Explorer; explorer:query is analyst/admin");
}
await page.selectOption("#role", "admin");

// --- a different incident row opens that incident, not a hardcoded one ------
await page.locator('[data-tab="home"]').click();
await page.locator('[data-incident="INC_N92B2N4AFKE2BQ"]').click();
if (!(await page.getByText(/INC_N92B2N4AFKE2BQ/).count())) {
  problems.push("clicking the second incident did not open that incident");
}
if (await page.getByText(/INC_K8Y583VK8Q1CRF/).count()) {
  problems.push("the incident card still shows the first incident's id");
}
if (!(await page.getByText(/11,114/).count())) {
  problems.push("the incident card did not render the clicked row's own figures");
}

// --- the region filter actually filters -------------------------------------
await page.locator('[data-tab="home"]').click();
await page.locator('[data-region="Nairobi"]').click();
if (await page.locator('[data-incident="INC_N92B2N4AFKE2BQ"]').count()) {
  problems.push("filtering to Nairobi still lists a Mombasa incident");
}
await page.locator('[data-region="all"]').click();

// --- the lag chart is interactive and reports the real value ----------------
await page.locator('[data-tab="insights"]').click();
await page.locator('[data-lag="0"]').click();
if (!(await page.getByText(/0\.0135/).count())) {
  problems.push("selecting the first lag did not show its correlation");
}
await page.screenshot({ path: path.join(SHOTS, "16-insights-lag.png"), fullPage: true, animations: "disabled" });

// --- a hand-typed SELECT is accepted but invents no rows --------------------
await page.locator('[data-tab="explorer"]').click();
await page.locator("#sqlbox").fill("SELECT cell_id FROM kpi_cell");
await page.locator("#run").click();
await page.getByText(/guard would accept this/i).waitFor({ timeout: 5000 });
if (await page.locator("table caption", { hasText: /Query result/i }).count()) {
  problems.push("an arbitrary SELECT produced invented result rows");
}
// a stacked statement is refused
await page.locator("#sqlbox").fill("SELECT 1; DROP TABLE complaint");
await page.locator("#run").click();
await page.getByText(/single statement/i).waitFor({ timeout: 5000 });

// --- the dark theme renders -------------------------------------------------
await page.locator("#theme").click();
if ((await page.locator("html").getAttribute("data-theme")) !== "dark") {
  problems.push("the theme toggle did not switch to dark");
}
await page.locator('[data-tab="home"]').click();
await page.screenshot({ path: path.join(SHOTS, "15-dark-theme.png"), fullPage: true, animations: "disabled" });

// --- it must be genuinely self-contained ------------------------------------
const requests = [];
page.on("request", (r) => {
  if (!r.url().startsWith("file:") && !r.url().startsWith("data:")) requests.push(r.url());
});
await page.reload();
for (const id of TABS) await page.locator(`[data-tab="${id}"]`).click();
if (requests.length) {
  problems.push(`the page made ${requests.length} external request(s): ${requests.slice(0, 3)}`);
}

await browser.close();

console.log(`\nscreenshots written to ${SHOTS} (${shots + 5} files)`);
if (problems.length) {
  console.error(`\nFAILED with ${problems.length} problem(s):`);
  for (const p of problems) console.error("  - " + p);
  process.exit(1);
}
console.log("\nOK: every tab renders, every interaction fires, no console errors,");
console.log("    no external requests, role filtering and both themes work.");
