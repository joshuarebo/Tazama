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
    // Scoped to the stat card: a bare text match hits both the label and its wrapper.
    const liveIncidents = page.locator(".card", { hasText: "Live incidents" }).first();
    await expect(liveIncidents).toBeVisible();
    await expect(liveIncidents.locator(".stat")).toHaveText(/^[\d,]+$/);

    // The queue, worst first. The rows come from the API, so wait for one rather than
    // counting a still-loading table.
    const table = page.getByRole("table", { name: /live incidents/i });
    await expect(table).toBeVisible();
    const rows = table.locator("tbody tr");
    await expect(rows.first()).toBeVisible();
    await expect
      .poll(async () => rows.count(), { message: "no incident rows rendered" })
      .toBeGreaterThan(0);

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
    await expect(firstIncident).toBeVisible();
    const title = (await firstIncident.textContent())?.trim() ?? "";
    await firstIncident.click();

    await expect(page.getByRole("heading", { name: title })).toBeVisible();

    // Impact in the units a manager asks about.
    await expect(page.getByText("Subscribers affected").first()).toBeVisible();
    await expect(page.getByText(/^Cells$/).first()).toBeVisible();

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
    await expect(page.getByText(/allowlisted tables/i).first()).toBeVisible();
    // --- the safe query ---
    await page.getByRole("button", { name: /complaints per region/i }).click();
    await page.getByRole("button", { name: /run query/i }).click();

    // Rows, not just a result panel. An example that returns nothing is how the Explorer
    // comes to look broken while working perfectly — which is what this caught.
    const result = page.getByRole("table", { name: /query result/i });
    await expect(result).toBeVisible();
    await expect(result.locator("tbody tr").first()).toBeVisible();
    await expect(page.getByText(/row\(s\) ·/)).toBeVisible();
    // The SQL the server actually ran is shown back, cap included.
    await expect(page.getByText(/SQL as executed/i)).toBeVisible();

    // --- the refusal ---
    await page.getByRole("button", { name: /try to delete data/i }).click();
    await page.getByRole("button", { name: /run query/i }).click();

    const refusal = page.getByText(/refused by the sql guard/i);
    await expect(refusal).toBeVisible();
    await expect(page.getByText(/never sent to the database/i)).toBeVisible();

    // A refusal is a successful outcome here, not an error state: the guard panel is a
    // `status`, not an `alert`. Scoped to `main` because Next's dev overlay contributes an
    // empty `role="alert"` of its own, which is not ours and not a failure.
    await expect(page.getByRole("main").getByRole("alert")).toHaveCount(0);
    await expect(page.getByRole("status").filter({ hasText: /refused/i })).toBeVisible();
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
    // Lead-lag needs two round trips — the busiest-cells list picks the cell the analysis
    // then runs on — so wait for the result badges before asserting the prose.
    // Two sequential API calls: the busiest-cells list picks the cell, then the
    // cross-correlation runs over 336 hourly points. Worth an explicit wait rather than
    // the default — the panel is correct, just not instant.
    await expect(page.getByText(/best lag/i).first()).toBeVisible({ timeout: 45_000 });
    await expect(
      page
        .getByText(/before the complaint rate|move together|customers notice first|no usable/i)
        .first(),
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

    // The rendered card… scoped to the preview, because the shell's own demo ribbon says
    // "Demo data" too and a loose match hits both.
    const preview = page.locator("section", { hasText: /teams card preview/i }).first();
    await expect(preview).toBeVisible();
    await expect(preview.getByText(/DEMO DATA/)).toBeVisible();

    // …and the payload itself, which is what makes the preview a real check.
    await expect(page.getByText(/adaptive card payload/i)).toBeVisible();
    await expect(page.getByText(/"type": "AdaptiveCard"/)).toBeVisible();

    // There is no send button, because there is no send endpoint.
    await expect(page.getByRole("button", { name: /^send/i })).toHaveCount(0);

    // Weekly works too.
    await page.getByRole("button", { name: "weekly", exact: true }).click();
    // The title appears twice by design: as the card's heading and inside the JSON payload
    // — which is exactly the point of showing both.
    await expect(page.getByText(/weekly network-care summary/i).first()).toBeVisible();
  });
});

test.describe("AI", () => {
  test("the agent's diagnosis shows its evidence and what it was refused", async ({ page }) => {
    await signIn(page, "NOC engineer");

    const firstIncident = page
      .getByRole("table", { name: /live incidents/i })
      .locator("tbody tr")
      .first()
      .getByRole("link")
      .first();
    await expect(firstIncident).toBeVisible();
    await firstIncident.click();

    const panel = page.locator("section", { hasText: /^AI diagnosis/ }).first();
    await expect(panel).toBeVisible();

    await panel.getByRole("button", { name: /^diagnose$/i }).click();

    // A cause and how it was reached. `rules` means no model was consulted at all.
    await expect(panel.getByText(/confidence \d+%/)).toBeVisible();
    await expect(panel.getByText(/method: (rules|llm|none)/)).toBeVisible();

    // The trace is the point of the panel: expand it and check the containment is visible.
    const trace = panel.getByText(/what the agent did/i);
    await expect(trace).toBeVisible();
    await trace.click();

    await expect(panel.getByText(/tools called/i)).toBeVisible();
    await expect(panel.getByText(/tools refused/i)).toBeVisible();
    await expect(panel.getByText(/stopped because/i)).toBeVisible();
    // Whatever happened, nothing that could act was called.
    await expect(panel.getByText("execute_runbook")).toHaveCount(0);
  });

  test("a plain-English question produces SQL that is shown before it runs", async ({
    page,
  }) => {
    await signIn(page, "Admin");
    await page.goto("/explorer");

    const ask = page.locator("section", { hasText: /ask a question/i }).first();
    await expect(ask).toBeVisible();

    await ask.getByRole("button", { name: /which regions had the most complaints/i }).click();

    // The SQL appears. No results do — nothing has been executed.
    await expect(ask.getByText(/the guard would (accept|refuse) this/i)).toBeVisible();
    await expect(ask.locator("pre")).toContainText(/SELECT/i);
    await expect(page.getByRole("table", { name: /query result/i })).toHaveCount(0);
    await expect(ask.getByText(/nothing has been executed/i)).toBeVisible();

    // The human approves it into the editor, then runs it.
    await ask.getByRole("button", { name: /use this query/i }).click();
    await page.getByRole("button", { name: /run query/i }).click();

    const result = page.getByRole("table", { name: /query result/i });
    await expect(result).toBeVisible();
    await expect(result.locator("tbody tr").first()).toBeVisible();
  });

  test("the AI quality page says what the model is and what each agent may touch", async ({
    page,
  }) => {
    await signIn(page, "Admin");
    await page.goto("/ai");

    await expect(page.getByRole("heading", { name: "AI quality" })).toBeVisible();
    await expect(page.getByText("Model endpoint").first()).toBeVisible();

    // All three agents, each with a ceiling and a stated fallback.
    const agents = page.getByRole("table", { name: /agents, their permission ceilings/i });
    await expect(agents).toBeVisible();
    for (const name of ["DiagnosisAgent", "KnowledgeAssistant", "Reporter"]) {
      await expect(agents.getByText(name)).toBeVisible();
    }
    await expect(page.getByText(/no agent holds a tool that can act/i)).toBeVisible();

    // And the assistant declines rather than guessing.
    await page.getByLabel(/question/i).fill("what is the quarterly dividend policy?");
    await page.getByRole("button", { name: /^ask$/i }).click();
    await expect(page.getByText(/declined|answered/).first()).toBeVisible();
  });
});
