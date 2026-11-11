/**
 * Checks the standalone demo page that gets emailed out.
 *
 * It is one file with no network access, so what can break is: a tab that
 * renders nothing, the walkthrough not advancing, a console error, or an
 * accidental external request. It also fails on em dashes, which read as
 * machine written.
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

const TABS = ["why", "how", "app", "next"];
const bad = [];

// --- read the source before opening it --------------------------------------
const src = readFileSync(FILE, "utf8");
const dashes = (src.match(/—/g) || []).length;
if (dashes) bad.push(`${dashes} em dash(es) in the source; use plain punctuation`);
for (const scheme of ["http://", "https://"]) {
  const re = new RegExp(`(src|href)\\s*=\\s*["']${scheme}`, "i");
  if (re.test(src)) bad.push(`an external ${scheme} asset is referenced`);
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1180, height: 900 } });

const outside = [];
page.on("request", (r) => {
  if (!r.url().startsWith("file:") && !r.url().startsWith("data:")) outside.push(r.url());
});
page.on("console", (m) => { if (m.type() === "error") bad.push(`console: ${m.text()}`); });
page.on("pageerror", (e) => bad.push(`page error: ${e.message}`));

await page.goto(pathToFileURL(FILE).href);

// --- every tab renders real content -----------------------------------------
let shot = 0;
for (const id of TABS) {
  await page.locator(`[data-tab="${id}"]`).click();
  const text = (await page.locator("main").innerText()).trim();
  if (text.length < 300) bad.push(`tab ${id} rendered only ${text.length} characters`);
  if (/undefined|NaN|\[object Object\]/.test(text)) {
    bad.push(`tab ${id} shows a rendering artefact`);
  }
  await page.screenshot({
    path: path.join(SHOTS, `${String(++shot).padStart(2, "0")}-${id}.png`),
    fullPage: true, animations: "disabled",
  });
}

// --- the walkthrough advances and stops at both ends ------------------------
await page.locator('[data-tab="how"]').click();
await page.getByText(/Step 1 of 7/i).waitFor({ timeout: 5000 });
if (!(await page.locator("#prev").isDisabled())) bad.push("Back is enabled on the first step");

for (let i = 0; i < 6; i += 1) await page.locator("#next").click();
await page.getByText(/Step 7 of 7/i).waitFor({ timeout: 5000 });
if (!(await page.locator("#next").isDisabled())) bad.push("Next is enabled on the last step");
await page.screenshot({ path: path.join(SHOTS, "05-step-7.png"), fullPage: true,
  animations: "disabled" });

// jumping straight to a step works too
await page.locator('[data-step="2"]').click();
await page.getByText(/Step 3 of 7/i).waitFor({ timeout: 5000 });
if ((await page.locator('[data-step="2"]').getAttribute("aria-pressed")) !== "true") {
  bad.push("the chosen step is not marked as current");
}
await page.screenshot({ path: path.join(SHOTS, "06-step-3.png"), fullPage: true,
  animations: "disabled" });

// --- it must work at phone width --------------------------------------------
await page.setViewportSize({ width: 390, height: 844 });
const overflow = await page.evaluate(() =>
  document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
if (overflow) bad.push("the page scrolls sideways at 390px wide");
await page.screenshot({ path: path.join(SHOTS, "07-phone.png"), fullPage: true,
  animations: "disabled" });

if (outside.length) bad.push(`${outside.length} external request(s): ${outside.slice(0, 3)}`);

await browser.close();

console.log(`\nscreenshots in ${SHOTS}`);
if (bad.length) {
  console.error(`\nFAILED, ${bad.length} problem(s):`);
  for (const b of bad) console.error("  - " + b);
  process.exit(1);
}
console.log("\nOK: four tabs render, the walkthrough works, no console errors,");
console.log("    no external requests, no em dashes, no sideways scroll on a phone.");
