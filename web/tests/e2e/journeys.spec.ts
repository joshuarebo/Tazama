/**
 * The five journeys from the Definition of Done, walked end to end.
 *
 * These run against the real API and the seeded database, which is the point: a journey
 * test with a mocked API proves the components render, not that the product works. Every
 * assertion below is about something a person would actually look for on the screen.
 */
import { expect, test } from "@playwright/test";

import { signIn } from "./helpers";

test.describe("Command Center", () => {
  test("a NOC engineer sees the seeded incidents and can open one", async ({ page }) => {
    await signIn(page, "NOC engineer");

    await expect(page.getByRole("heading", { name: "Command Center" })).toBeVisible();

    // The demo ribbon must be present: synthetic numbers presented as real is the single
    // most likely way this product misleads someone.
    await expect(page.getByText(/every subscriber, site and complaint here is synthetic/i))
      .toBeVisible();

    // Headline figures come from the API, so a number here proves the whole read path.
    const liveIncidents = page.getByText("Live incidents").locator("..");
    await expect(liveIncidents).toBeVisible();

    // The queue, worst first.
    const table = page.getByRole("table", { name: /live incidents/i });
    await expect(table).toBeVisible();
    const rows = table.locator("tbody tr");
    await expect(rows.first()).toBeVisible();
    expect(await rows.count()).toBeGreaterThan(0);

    // And the footer states what this is.
    await expect(page.getByText("Internal — Confidential")).toBeVisible();
  });

  test("the affected-cell map renders without a tile server", async ({ page }) => {
    await signIn(page, "NOC engineer");
    const map = page.getByRole("img", { name: /affected cells across/i });
    await expect(map).toBeVisible();
    await expect(page.getByText(/no basemap configured/i)).toBeVisible();
  });
});

test.describe("Incident card", () => {
  test("an engineer can read the evidence behind a diagnosis", async ({ page }) => {
    await signIn(page, "NOC engineer");

    const firstIncident = page
      .getByRole("table", { name: /live incidents/i })
      .locator("tbody tr")
      .first()
      .getByRole("link")
      .first();
    const title = (await firstIncident.textContent())?.trim() ?? "";
    await firstIncident.click();

    await expect(page.getByRole("heading", { name: title })).toBeVisible();

    // Impact in the units a manager asks about.
    await expect(page.getByText("Subscribers affected")).toBeVisible();
    await expect(page.getByText(/^Cells$/)).toBeVisible();

    // The evidence section is the reason this screen exists.
    await expect(page.getByText(/probable cause and evidence/i)).toBeVisible();

    // Either evidence rows, or an explicit statement that there are none. A silent empty
    // panel would be the failure: it reads as "no problem" rather than "not yet diagnosed".
    const evidence = page.getByText(/weight \d/).first();
    const noDiagnosis = page.getByText(/no diagnosis recorded/i);
    await expect(evidence.or(noDiagnosis)).toBeVisible();

    // The raw signals the conclusion rests on.
    await expect(page.getByText(/^Alarms$/)).toBeVisible();
    await expect(page.getByText(/KPI behaviour/i)).toBeVisible();
    await expect(page.getByText(/^Decisions$/)).toBeVisible();
  });
});

test.describe("Data Explorer", () => {
  test("a safe query runs and a dangerous one is refused", async ({ page }) => {
    await signIn(page, "Admin");
    await page.goto("/explorer");

    await expect(page.getByRole("heading", { name: "Data Explorer" })).toBeVisible();
    await expect(page.getByText(/allowlisted tables/i)).toBeVisible();

    // --- the safe query ---
    await page.getByRole("button", { name: /complaints per region/i }).click();
    await page.getByRole("button", { name: /run query/i }).click();

    await expect(page.getByRole("table", { name: /query result/i })).toBeVisible();
    await expect(page.getByText(/row\(s\) ·/)).toBeVisible();
    // The SQL the server actually ran is shown back, cap included.
    await expect(page.getByText(/SQL as executed/i)).toBeVisible();

    // --- the refusal ---
    await page.getByRole("button", { name: /try to delete data/i }).click();
    await page.getByRole("button", { name: /run query/i }).click();

    const refusal = page.getByText(/refused by the sql guard/i);
    await expect(refusal).toBeVisible();
    await expect(page.getByText(/never sent to the database/i)).toBeVisible();

    // A refusal is a successful outcome here, not an error state.
    await expect(page.getByRole("alert")).toHaveCount(0);
  });

  test("a stacked statement is refused too", async ({ page }) => {
    await signIn(page, "Admin");
    await page.goto("/explorer");
    await page.getByRole("button", { name: /try a stacked statement/i }).click();
    await page.getByRole("button", { name: /run query/i }).click();
    await expect(page.getByText(/refused by the sql guard/i)).toBeVisible();
  });
});

test.describe("Care lookup", () => {
  test("an agent sees the known issue and the approved wording", async ({ page }) => {
    await signIn(page, "Care agent");
    await page.goto("/care");

    await expect(page.getByRole("heading", { name: "Care lookup" })).toBeVisible();

    // The demo affordance: seeded tokens to click, because raw numbers are never stored.
    const token = page.getByRole("button", { name: /^SUB-/ }).first();
    await expect(token).toBeVisible();
    await token.click();

    await expect(page.getByText(/^Subscriber$/)).toBeVisible();
    await expect(page.getByText(/^Known issue$/)).toBeVisible();

    // Either approved wording to read out, or an explicit "no live incident". Both are
    // correct answers; a blank panel is not.
    const wording = page.getByRole("button", { name: /copy wording/i });
    const noIssue = page.getByText(/no live incident affects this subscriber/i);
    await expect(wording.or(noIssue)).toBeVisible();

    // There must be no free-text box for a message to the customer. The agent reads an
    // approved line; they do not compose one.
    await expect(page.locator("textarea")).toHaveCount(0);
  });

  test("a care agent cannot reach the Data Explorer", async ({ page }) => {
    await signIn(page, "Care agent");
    // The nav does not offer it…
    await expect(page.getByRole("link", { name: "Data Explorer" })).toHaveCount(0);
    // …and going there directly is refused by the server, not just hidden.
    await page.goto("/explorer");
    await expect(page.getByRole("alert").or(page.getByText(/forbidden|not allowed|403/i)))
      .toBeVisible();
  });
});

test.describe("Insights", () => {
  test("lead-lag is shown with its caveats", async ({ page }) => {
    await signIn(page, "Analyst");
    await page.goto("/insights");

    await expect(page.getByRole("heading", { name: "Insights" })).toBeVisible();
    await expect(page.getByText(/^Complaint rate$/)).toBeVisible();
    await expect(page.getByText(/^Lead and lag$/)).toBeVisible();

    // The reading in words, so the panel does not require remembering the sign convention.
    await expect(
      page.getByText(/before the complaint rate|move together|customers notice first|no usable/i),
    ).toBeVisible();

    // A positive lag means the KPI moved first — stated on the page.
    await expect(page.getByText(/a positive lag means the kpi moved first/i)).toBeVisible();

    // Candidate drivers must be labelled as candidates, not as drivers.
    await expect(page.getByText(/candidates, not drivers/i)).toBeVisible();
    // And PCA must say that variance is not relevance.
    await expect(page.getByText(/variance is not relevance/i)).toBeVisible();
  });
});

test.describe("Reports", () => {
  test("the Teams digest preview shows what would be posted, and says nothing was", async ({
    page,
  }) => {
    await signIn(page, "Manager");
    await page.goto("/reports");

    await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible();

    // Dry run must be visible, not buried.
    await expect(page.getByText(/TEAMS_DRY_RUN = true/)).toBeVisible();
    await expect(page.getByText(/nothing has been posted to teams/i)).toBeVisible();

    // The rendered card…
    await expect(page.getByText(/teams card preview/i)).toBeVisible();
    await expect(page.getByText(/DEMO DATA/)).toBeVisible();

    // …and the payload itself, which is what makes the preview a real check.
    await expect(page.getByText(/adaptive card payload/i)).toBeVisible();
    await expect(page.getByText(/"type": "AdaptiveCard"/)).toBeVisible();

    // There is no send button, because there is no send endpoint.
    await expect(page.getByRole("button", { name: /^send/i })).toHaveCount(0);

    // Weekly works too.
    await page.getByRole("button", { name: "weekly", exact: true }).click();
    await expect(page.getByText(/weekly network-care summary/i)).toBeVisible();
  });
});
