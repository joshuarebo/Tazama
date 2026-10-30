/**
 * Contrast assertions on the design tokens themselves.
 *
 * axe catches contrast failures on whatever happens to be rendered. This checks the
 * *tokens*, which is where a brand swap will break it: the placeholder green was
 * originally #3fa72f, which measured 3.10:1 against white and failed every page. When the
 * official palette lands, this file is what says whether it can be used for button text.
 */
import { expect, test } from "@playwright/test";

/** WCAG 2.1 relative luminance. */
function luminance(rgb: [number, number, number]): number {
  const [r, g, b] = rgb.map((channel) => {
    const c = channel / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: [number, number, number], b: [number, number, number]): number {
  const [la, lb] = [luminance(a), luminance(b)];
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** Resolve a CSS colour the browser has already computed, in any notation it returns. */
function parse(value: string): [number, number, number] {
  const numbers = value.match(/[\d.]+/g);
  if (!numbers || numbers.length < 3) throw new Error(`cannot parse colour: ${value}`);
  return [Number(numbers[0]), Number(numbers[1]), Number(numbers[2])];
}

async function tokens(page: import("@playwright/test").Page, names: string[]) {
  return page.evaluate((list) => {
    const style = getComputedStyle(document.documentElement);
    const out: Record<string, string> = {};
    for (const name of list) {
      // Resolve each token through a probe element so the browser normalises the value.
      const probe = document.createElement("span");
      probe.style.color = `var(${name})`;
      document.body.appendChild(probe);
      out[name] = getComputedStyle(probe).color;
      probe.remove();
    }
    void style;
    return out;
  }, names);
}

const PAIRS: { label: string; fg: string; bg: string; min: number }[] = [
  // Button text on the primary fill — the one that was failing.
  { label: "inverse text on brand", fg: "--text-inverse", bg: "--brand-green", min: 4.5 },
  { label: "inverse text on brand-strong", fg: "--text-inverse", bg: "--brand-green-strong", min: 4.5 },
  // The active nav item.
  { label: "brand-strong on brand-soft", fg: "--brand-green-strong", bg: "--brand-green-soft", min: 4.5 },
  // Body and muted text on both surfaces.
  { label: "text on surface", fg: "--text", bg: "--surface", min: 4.5 },
  { label: "muted text on surface", fg: "--text-muted", bg: "--surface", min: 4.5 },
  { label: "text on sunken surface", fg: "--text", bg: "--surface-sunken", min: 4.5 },
  { label: "muted text on sunken surface", fg: "--text-muted", bg: "--surface-sunken", min: 4.5 },
  // The status colours sit on the sunken surface far more often than on white — the
  // Reports and Complaints pages are where axe caught `--warn` at 4.45:1.
  { label: "warn on sunken surface", fg: "--warn", bg: "--surface-sunken", min: 4.5 },
  { label: "danger on sunken surface", fg: "--danger", bg: "--surface-sunken", min: 4.5 },
  { label: "ok on sunken surface", fg: "--ok", bg: "--surface-sunken", min: 4.5 },
  { label: "info on sunken surface", fg: "--info", bg: "--surface-sunken", min: 4.5 },
  // Status colours, used for small bold labels.
  { label: "danger on danger-soft", fg: "--danger", bg: "--danger-soft", min: 4.5 },
  { label: "warn on warn-soft", fg: "--warn", bg: "--warn-soft", min: 4.5 },
  { label: "ok on ok-soft", fg: "--ok", bg: "--ok-soft", min: 4.5 },
  { label: "info on info-soft", fg: "--info", bg: "--info-soft", min: 4.5 },
];

for (const scheme of ["light", "dark"] as const) {
  test(`the ${scheme} tokens meet WCAG AA for text`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto("/");

    const names = [...new Set(PAIRS.flatMap((pair) => [pair.fg, pair.bg]))];
    const resolved = await tokens(page, names);

    const failures: string[] = [];
    for (const pair of PAIRS) {
      const ratio = contrast(parse(resolved[pair.fg]!), parse(resolved[pair.bg]!));
      if (ratio < pair.min) {
        failures.push(
          `${pair.label}: ${ratio.toFixed(2)}:1 (needs ${pair.min}:1) — ` +
            `${pair.fg}=${resolved[pair.fg]} on ${pair.bg}=${resolved[pair.bg]}`,
        );
      }
    }
    expect(failures, failures.join("\n")).toEqual([]);
  });
}

test("the brand tokens are still the documented placeholders", async ({ page }) => {
  // A deliberate tripwire. When the official palette lands this test fails, which is the
  // prompt to re-run the contrast pairs above against the real values and to remove the
  // UNVERIFIED banner from tokens.css.
  await page.goto("/");
  const resolved = await tokens(page, ["--brand-green"]);
  expect(
    parse(resolved["--brand-green"]!),
    "brand green changed — recheck contrast and clear the UNVERIFIED note in tokens.css",
  ).toEqual([47, 125, 35]);
});
