/**
 * Shared sign-in for the journeys.
 *
 * The naive version — `goto`, then `isVisible()`, then click — fails for a reason worth
 * recording: the shell renders `null` until it has read the session from `sessionStorage`
 * in an effect, so immediately after `goto` neither the sign-in panel nor the signed-in
 * header exists. `isVisible()` answers `false` straight away, the helper concludes it is
 * already signed in, and every assertion afterwards times out against an empty page.
 *
 * So wait for the shell to have decided, *then* act on what it decided.
 *
 * Both waits carry an explicit, generous timeout. What they wait on is not a UI invariant
 * but a cold Next dev-server route compile plus an API round-trip, and the default 15s is
 * occasionally short for the first page a worker touches — which produced a flake that
 * moved between tests from run to run. The condition asserted is unchanged; only the
 * patience is. The round-trip itself is fast (the API logs every sign-in at ~13ms), so a
 * timeout here means the page never rendered, which is still a failure worth having.
 */
import { expect, type Page } from "@playwright/test";

export const ROLES = {
  noc: "NOC engineer",
  care: "Care agent",
  analyst: "Analyst",
  manager: "Manager",
  admin: "Admin",
} as const;

/** Long enough for a cold dev-server compile plus a round-trip; see the note above. */
const SETTLE_MS = 45_000;

export async function signIn(page: Page, role: string): Promise<void> {
  if (page.url() === "about:blank" || !page.url().includes("localhost")) {
    await page.goto("/");
  }

  const signInHeading = page.getByRole("heading", { name: "Sign in" });
  const signedIn = page.getByText(/signed in as/i);

  // One of the two must appear: the shell has either found a session or not.
  await expect(signInHeading.or(signedIn)).toBeVisible({ timeout: SETTLE_MS });

  if (await signedIn.isVisible()) return;

  await page.getByRole("button", { name: role, exact: true }).click();
  await expect(signedIn).toBeVisible({ timeout: SETTLE_MS });
}

/** Sign in and land on a path, in that order — the nav is role-filtered. */
export async function visit(page: Page, role: string, path: string): Promise<void> {
  await signIn(page, role);
  if (path !== "/") {
    await page.goto(path);
    await expect(page.getByRole("main")).toBeVisible();
  }
}
