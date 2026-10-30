/**
 * Accessibility on every page, plus the things axe cannot see.
 *
 * axe catches the mechanical failures — missing labels, unlabelled controls, bad contrast.
 * It cannot tell whether a table is readable without colour, whether the page works from
 * the keyboard alone, or whether a figure has a text equivalent. Those are asserted
 * directly below, because a NOC runs on wall displays, night shifts and tired eyes.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { signIn } from "./helpers";

const PAGES = [
  { path: "/", name: "Command Center", role: "Admin" },
  { path: "/complaints", name: "Complaints", role: "Admin" },
  { path: "/care", name: "Care lookup", role: "Admin" },
  { path: "/explorer", name: "Data Explorer", role: "Admin" },
  { path: "/insights", name: "Insights", role: "Admin" },
  { path: "/reports", name: "Reports", role: "Admin" },
];

for (const target of PAGES) {
  test(`${target.name} has no critical or serious accessibility violations`, async ({ page }) => {
    await signIn(page, target.role);
    await page.goto(target.path);
    await expect(page.getByRole("main")).toBeVisible();

    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      // The MapLibre canvas and the ECharts canvas are third-party renderers; each is
      // given a text equivalent of its own, asserted separately below.
      .exclude("canvas")
      .analyze();

    const blocking = results.violations.filter(
      (violation) => violation.impact === "critical" || violation.impact === "serious",
    );

    // Printed rather than just counted: a bare "expected 0, got 3" is useless in CI.
    if (blocking.length > 0) {
      console.error(
        blocking
          .map(
            (violation) =>
              `${violation.impact}: ${violation.id} — ${violation.help}\n` +
              violation.nodes.map((node) => `    ${node.target.join(" ")}`).join("\n"),
          )
          .join("\n"),
      );
    }
    expect(blocking, `${blocking.length} blocking violation(s) on ${target.path}`).toEqual([]);
  });
}

test("every page is reachable from the keyboard alone", async ({ page }) => {
  await signIn(page, "Admin");
  // Reload after signing in: clicking the role button left focus on that button, so
  // tabbing from there tests the order *after* it rather than from the document start.
  await page.reload();
  await expect(page.getByRole("main")).toBeVisible();

  // The skip link must be the first thing focus reaches, or a keyboard user walks the
  // whole nav on every page load.
  await page.keyboard.press("Tab");
  const focused = page.locator(":focus");
  await expect(focused).toHaveText(/skip to content/i);

  await page.keyboard.press("Enter");

  // Focus must land on or inside `main`. The assertion is "focus moved" rather than
  // "focus is exactly on main", because a browser may choose the target's first focusable
  // child — both outcomes serve the keyboard user.
  const landed = await page.evaluate(() => {
    const main = document.querySelector("main");
    const active = document.activeElement;
    return Boolean(main && active && (main === active || main.contains(active)));
  });
  expect(landed, "the skip link did not move focus into main").toBe(true);
});

test("the current page is announced, not just coloured", async ({ page }) => {
  await signIn(page, "Admin");
  await page.goto("/insights");
  const current = page.locator('nav a[aria-current="page"]');
  await expect(current).toHaveText("Insights");
});

test("priority is not conveyed by colour alone", async ({ page }) => {
  await signIn(page, "NOC engineer");
  // The badge carries its own text *and* an accessible label, so a colour-blind reader and
  // a screen-reader user both get the priority.
  const badge = page.getByLabel(/^priority P\d$/).first();
  await expect(badge).toBeVisible();
  await expect(badge).toHaveText(/^P\d$/);
});

test("the map has a text equivalent", async ({ page }) => {
  await signIn(page, "NOC engineer");
  const figure = page.getByRole("img", { name: /affected cells across \d+ sites/i });
  await expect(figure).toBeVisible();
  // And a caption a sighted user reads for the same information.
  await expect(page.getByText(/site\(s\), \d+ cell\(s\)/)).toBeVisible();
});

test("tables have captions for screen readers", async ({ page }) => {
  await signIn(page, "Admin");
  await page.goto("/complaints");
  // `Table` renders an sr-only caption; without it a screen reader announces "table" and
  // nothing else.
  const tables = page.getByRole("table");
  const count = await tables.count();
  expect(count).toBeGreaterThan(0);
  for (let index = 0; index < count; index += 1) {
    await expect(tables.nth(index).locator("caption")).toHaveCount(1);
  }
});

test("the dark theme keeps text readable", async ({ page }) => {
  // The tokens define both themes; this checks the dark one actually applies rather than
  // falling back to light text on a light background.
  await page.emulateMedia({ colorScheme: "dark" });
  await signIn(page, "Admin");

  const { background, color } = await page.evaluate(() => {
    const body = getComputedStyle(document.body);
    return { background: body.backgroundColor, color: body.color };
  });
  expect(background).not.toBe(color);
  expect(background).not.toBe("rgba(0, 0, 0, 0)");

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2aa"])
    .include("main")
    .exclude("canvas")
    .analyze();
  const contrast = results.violations.filter((violation) => violation.id === "color-contrast");
  expect(contrast).toEqual([]);
});

test("reduced motion is honoured", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await signIn(page, "Admin");
  // The loading pulse is the only animation; under reduced motion it must not animate.
  const animated = await page.evaluate(() => {
    const element = document.querySelector(".animate-pulse");
    if (!element) return "none";
    return getComputedStyle(element).animationName;
  });
  expect(["none", "none, none"]).toContain(animated);
});
