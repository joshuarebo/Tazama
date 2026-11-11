/**
 * Checks the standalone console replica that gets emailed out.
 *
 * It is one file with no network access, so what can break is: a page that
 * renders nothing, an interaction that does not fire, a console error, or an
 * accidental external request. It also fails on em dashes.
 *
 * Run from web/:  node tests/verify-demo-file.mjs
 */
import { chromium } from "@playwright/test";
import { mkdirSync, readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import path from "node:path";

const FILE = path.resolve("../docs/demo/tazama-demo.html");
const SHOTS = path.resolve("../artifacts/demo-screenshots");
mkdirSync(SHOTS, { recursive: true });

const PAGES = ["home", "complaints", "care", "explorer", "insights", "reports", "ai", "arch"];
const bad = [];

const src = readFileSync(FILE, "utf8");
const dashes = (src.match(/—/g) || []).length;
if (dashes) bad.push(`${dashes} em dash(es) in the source; use plain punctuation`);
for (const scheme of ["http://", "https://"]) {
  if (new RegExp(`(src|href)\s*=\s*["']${scheme}`, "i").test(src)) {
    bad.push(`an external ${scheme} asset is referenced`);
  }
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
const outside = [];
page.on("request", (r) => {
  if (!r.url().startsWith("file:") && !r.url().startsWith("data:")) outside.push(r.url());
});
page.on("console", (m) => { if (m.type() === "error") bad.push(`console: ${m.text()}`); });
page.on("pageerror", (e) => bad.push(`page error: ${e.message}`));

await page.goto(pathToFileURL(FILE).href);

let shot = 0;
for (const id of PAGES) {
  await page.locator(`[data-p="${id}"]`).click();
  const text = (await page.locator("main").innerText()).trim();
  if (text.length < 300) bad.push(`page ${id} rendered only ${text.length} characters`);
  if (/undefined|NaN|\[object Object\]/.test(text)) bad.push(`page ${id} shows an artefact`);
  await page.screenshot({ path: path.join(SHOTS, `${String(++shot).padStart(2, "0")}-${id}.png`),
    fullPage: true, animations: "disabled" });
}

// an incident row opens that incident, with its own figures
await page.locator('[data-p="home"]').click();
await page.locator('[data-inc="INC_N92B2N4AFKE2BQ"]').click();
for (const want of [/INC_N92B2N4AFKE2BQ/, /11,114/]) {
  if (!(await page.getByText(want).count())) bad.push(`the incident card is missing ${want}`);
}
if (await page.getByText(/INC_K8Y583VK8Q1CRF/).count()) {
  bad.push("the incident card still shows the first incident");
}

// the agent panel is opt-in, then shows the trace
await page.locator('[data-p="home"]').click();
await page.locator('[data-inc="INC_K8Y583VK8Q1CRF"]').click();
if (await page.getByText(/What the agent did/i).count()) {
  bad.push("the agent trace was visible before the button was pressed");
}
await page.locator("#diagnose").click();
await page.getByText(/What the agent did/i).waitFor({ timeout: 5000 });
for (const want of [/execute_runbook/, /get_topology/, /never/]) {
  if (!(await page.getByText(want).count())) bad.push(`the trace is missing ${want}`);
}
await page.screenshot({ path: path.join(SHOTS, "09-incident.png"), fullPage: true,
  animations: "disabled" });

// the guard refuses a destructive statement and runs the sample
await page.locator('[data-p="explorer"]').click();
await page.locator("#danger").click();
await page.locator("#run").click();
await page.getByText(/Refused by the SQL guard/i).waitFor({ timeout: 5000 });
await page.screenshot({ path: path.join(SHOTS, "10-explorer-refused.png"), fullPage: true,
  animations: "disabled" });
await page.locator("#sample").click();
await page.locator("#run").click();
await page.getByText(/6,074/).waitFor({ timeout: 5000 });

// the lag chart reports the real value
await page.locator('[data-p="insights"]').click();
await page.locator('[data-lag="0"]').click();
if (!(await page.getByText(/0\.0135/).count())) bad.push("the lag chart did not update");

// dark theme, then phone width
await page.locator("#theme").click();
if ((await page.locator("html").getAttribute("data-theme")) !== "dark") {
  bad.push("the theme toggle did not switch");
}
await page.locator('[data-p="home"]').click();
await page.screenshot({ path: path.join(SHOTS, "11-dark.png"), fullPage: true,
  animations: "disabled" });
await page.locator("#theme").click();

await page.setViewportSize({ width: 390, height: 844 });
if (await page.evaluate(() =>
  document.documentElement.scrollWidth > document.documentElement.clientWidth + 1)) {
  bad.push("the page scrolls sideways at 390px wide");
}
await page.screenshot({ path: path.join(SHOTS, "12-phone.png"), fullPage: true,
  animations: "disabled" });

if (outside.length) bad.push(`${outside.length} external request(s)`);
await browser.close();

console.log(`\nscreenshots in ${SHOTS}`);
if (bad.length) {
  console.error(`\nFAILED, ${bad.length} problem(s):`);
  for (const b of bad) console.error("  - " + b);
  process.exit(1);
}
console.log("\nOK: nine screens render, incidents open individually, the agent panel is");
console.log("    opt-in, the SQL guard refuses writes, no console errors, no network.");
