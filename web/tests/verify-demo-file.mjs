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

const FILE = path.resolve("../artifacts/tazama-demo.html");
const SHOTS = path.resolve("../artifacts/screenshots");
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
    fullPage: true });
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
await page.screenshot({ path: path.join(SHOTS, "11-incident-diagnosed.png"), fullPage: true });

// --- the explorer shows SQL before any result, and refuses the attack -------
await page.locator('[data-tab="explorer"]').click();
await page.locator('[data-ask="ok"]').click();
await page.getByText(/Would accept/i).waitFor({ timeout: 5000 });
if (await page.locator("table caption", { hasText: /Query result/i }).count()) {
  problems.push("a query result appeared before the human pressed Run");
}
await page.screenshot({ path: path.join(SHOTS, "12-explorer-suggestion.png"), fullPage: true });

await page.locator("#use").click();
if (!(await page.locator("#sqlbox").inputValue()).toUpperCase().startsWith("SELECT")) {
  problems.push("Use this query did not load the SQL into the editor");
}
await page.locator("#run").click();
await page.locator("table caption", { hasText: /Query result/i }).waitFor({ timeout: 5000 });
if (!(await page.getByText(/6,074|6074/).count())) {
  problems.push("the executed result did not render its rows");
}
await page.screenshot({ path: path.join(SHOTS, "13-explorer-result.png"), fullPage: true });

// a destructive statement typed by hand is still refused
await page.locator("#sqlbox").fill("DELETE FROM complaint");
await page.locator("#run").click();
await page.getByText(/Refused by the SQL guard/i).waitFor({ timeout: 5000 });

// the injected question is refused up front
await page.locator('[data-ask="bad"]').click();
await page.getByText(/Refused:/i).waitFor({ timeout: 5000 });
await page.screenshot({ path: path.join(SHOTS, "14-explorer-refused.png"), fullPage: true });

// --- role filtering hides what it hides in the real console -----------------
await page.selectOption("#role", "care");
if (await page.locator('[data-tab="explorer"]').count()) {
  problems.push("a care agent can still see the Data Explorer tab");
}
if (!(await page.locator('[data-tab="care"]').count())) {
  problems.push("a care agent cannot see Care lookup");
}
await page.selectOption("#role", "manager");
if (await page.locator('[data-tab="care"]').count()) {
  problems.push("a manager can see Care lookup, which holds subscriber data");
}
await page.selectOption("#role", "admin");

// --- the dark theme renders -------------------------------------------------
await page.locator("#theme").click();
if ((await page.locator("html").getAttribute("data-theme")) !== "dark") {
  problems.push("the theme toggle did not switch to dark");
}
await page.locator('[data-tab="home"]').click();
await page.screenshot({ path: path.join(SHOTS, "15-dark-theme.png"), fullPage: true });

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
