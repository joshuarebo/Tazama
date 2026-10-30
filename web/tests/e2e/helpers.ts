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
 */
import { expect, type Page } from "@playwright/test";

export const ROLES = {
  noc: "NOC engineer",
  care: "Care agent",
  analyst: "Analyst",
  manager: "Manager",
  admin: "Admin",
} as const;

export async function signIn(page: Page, role: string): Promise<void> {
  if (page.url() === "about:blank" || !page.url().includes("localhost")) {
    await page.goto("/");
  }

  const signInHeading = page.getByRole("heading", { name: "Sign in" });
  const signedIn = page.getByText(/signed in as/i);

  // One of the two must appear: the shell has either found a session or not.
  await expect(signInHeading.or(signedIn)).toBeVisible();

  if (await signedIn.isVisible()) return;

  await page.getByRole("button", { name: role, exact: true }).click();
  await expect(signedIn).toBeVisible();
}

/** Sign in and land on a path, in that order — the nav is role-filtered. */
export async function visit(page: Page, role: string, path: string): Promise<void> {
  await signIn(page, role);
  if (path !== "/") {
    await page.goto(path);
    await expect(page.getByRole("main")).toBeVisible();
  }
}
