import { defineConfig } from "vitest/config";

/**
 * Vitest ran with no configuration at all, so it globbed every `*.spec.ts` in the
 * repository, picked up the Playwright suites under `tests/e2e/`, and failed with
 * "Playwright Test did not expect test() to be called here". That is why the frontend
 * gate has been red since October: the two runners were fighting over the same files.
 *
 * They are different tools with different jobs. Playwright drives a real browser against
 * a running stack and is run by `make e2e`. Vitest is for unit tests of pure functions
 * and components, and owns only the paths below.
 */
export default defineConfig({
  test: {
    include: ["tests/unit/**/*.test.ts", "src/**/*.test.ts", "src/**/*.test.tsx"],
    // Playwright owns these. Excluded explicitly rather than relying on the include
    // pattern, so adding a `tests/e2e/foo.test.ts` cannot quietly break the gate again.
    exclude: ["tests/e2e/**", "node_modules/**", ".next/**"],
    // There are no unit tests yet; the Playwright journeys and the axe/token suites carry
    // the behavioural coverage. An empty run should report "no tests", not fail the
    // build, so the slot stays open for when component tests are written.
    passWithNoTests: true,
    environment: "node",
  },
});
