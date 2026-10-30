# Phase 5 — Frontend

Status: **gate green**. Six pages against the real API and the seeded database, with
Playwright journeys, axe on every page, and contrast assertions on the tokens themselves.

The two findings worth reading are both things a human would not have noticed: the
placeholder brand green failed WCAG on every page, and the shell re-rendered twice on
every load because it read the session in an effect.

---

## 1. What was built

| Page | Route | What it answers |
| --- | --- | --- |
| Command Center | `/` | How much is broken, where, what is driving it, which incident to open first |
| Incident card | `/incidents/[id]` | What is wrong, the evidence for it, and what was decided |
| Complaints | `/complaints` | Every complaint as received, with the unlocated count in view |
| Care lookup | `/care` | Is there a known problem for this subscriber, and what may I say |
| Data Explorer | `/explorer` | Ad-hoc SQL, with the guard visible rather than hidden |
| Insights | `/insights` | Does a KPI move before the complaints do |
| Reports | `/reports` | What would be posted to Teams, and that nothing was |

Supporting: `components/ui.tsx` (the small design system), `components/Shell.tsx` (nav,
DEMO DATA ribbon, role sign-in, AI-degraded badge, Internal — Confidential footer),
`features/IncidentMap.tsx` (works with no tile server), `features/KpiChart.tsx` and
`features/RateChart.tsx` (ECharts, client-only), `lib/api.ts` (the only place the console
talks to the API), `styles/tokens.css` (the only place a colour is defined).

---

## 2. Gate results

```
npm run lint          0 errors, 0 warnings
npx tsc --noEmit      clean (strict, noUncheckedIndexedAccess)
npm run build         8 routes, prerendered
npm audit             found 0 vulnerabilities
playwright test       22 tests — journeys, a11y, token contrast
```

Journeys cover the five the plan named: care lookup shows a known-issue banner, the NOC
opens an incident and reads its evidence, the Data Explorer runs a safe query **and is
refused a dangerous one**, Insights renders lead-lag with its caveats, and Reports shows
the Teams digest preview.

Role separation is asserted end to end, not just in the nav: a care agent is not offered
the Data Explorer *and* is refused it when navigating there directly, because hiding a
link is not authorisation.

---

## 3. Findings

### The placeholder brand green failed WCAG AA on every page

axe reported `color-contrast` as *serious* on all six pages. The cause was
`--brand-green: #3fa72f` with white button text: **3.10:1, against the 4.5:1 AA requires.**
Measured rather than eyeballed, and the replacement was chosen by computation —
`#2f7d23` gives 5.15:1 on white, and the strong variant gives 6.78:1 on the soft
background the active nav item uses.

The interesting part is what it says about the handover. These values are placeholders
(`tokens.css` says so in a banner), so the real brand green may be lighter and fail the
same check. `tests/e2e/tokens.spec.ts` therefore asserts the ratios on the tokens
themselves and includes a deliberate tripwire: a test that fails when `--brand-green`
changes, whose message says to re-run the contrast pairs and clear the UNVERIFIED note.
The guidance is recorded in the token file: if the official green is lighter, use it for
fills and borders and pair button text with a darker shade rather than with white.

### The shell re-rendered twice on every page load

ESLint (`react-hooks/set-state-in-effect`) flagged `setRole(currentRole())` inside a
`useEffect`. The pattern is the standard way to read `sessionStorage` — which does not
exist during server rendering — and it costs a cascading render on every load, plus a
`ready` flag to suppress the flash of the sign-in panel.

`useSyncExternalStore` is the purpose-built answer: `lib/api.ts` now exposes the session as
a three-function store with an explicit server snapshot of `null`, and the `ready` flag is
gone because "the server has no session" *is* the not-known-yet state. The subscription is
a plain listener set rather than a `storage` event listener, because the `storage` event
does not fire in the tab that made the change.

### The Playwright sign-in helper was wrong in a way that hid everything else

The first run failed 11 tests at exactly the expect timeout. The helper did `goto`, then
`isVisible()`, then clicked — but the shell rendered `null` until it had read the session,
so neither the sign-in panel nor the signed-in header existed yet, `isVisible()` answered
`false` immediately, the helper concluded it was already signed in, and every assertion
afterwards timed out against a blank page. `tests/e2e/helpers.ts` now waits for the shell
to have decided (`Sign in` heading **or** `signed in as`) before acting on what it decided.

### Four more things the journeys caught

Worth listing, because each one was invisible without a test driving the real page.

1. **The Explorer's flagship example query returned zero rows.** It used
   `DATETIME('now', '-7 days')` against a seeded database whose data ends in March, so the
   first thing a reviewer clicks produced an empty result — correct, and looking broken.
   The example is now anchored on `MAX(received_at)`, the same lesson as the insights
   window, and the test demands *rows* rather than a result panel.
2. **The skip link did nothing** (see above) — found only by pressing Tab.
3. **Several assertions raced the data.** The pages fetch asynchronously, so counting rows
   immediately counts a loading page. Fixed with explicit content waits, not timeouts.
4. **Next's dev overlay contributes an empty `role="alert"`.** The assertion that a SQL
   refusal is "not an error state" had to be scoped to `main` — the refusal panel is
   correctly a `status`, and the stray alert is not ours.

### Next 15 → 16, and the dependency audit

`npm audit` reported 11 vulnerabilities, 3 critical. Resolved to **zero**:

| Package | Was | Now | Note |
| --- | --- | --- | --- |
| `maplibre-gl` | 4.7.1 | 6.16.0 | critical |
| `vitest` | 2.1.8 | 4.1.12 | critical via `vite`/`tinypool`; 4.1.12 is past the advisory range, so no major jump to 5 was needed |
| `echarts` / `echarts-for-react` | 5.5.1 / 3.0.2 | 6.1.0 / 3.0.6 | moderate |
| `postcss` | 8.4.49 | 8.5.32 | high |
| `next` | 15.5.28 | 16.4.3 | the bundled postcss was still 8.4.31 |
| `eslint` | 8.57.1 | 9.39.1 | required by `eslint-config-next@16` |

Three consequences of the Next 16 upgrade, each handled:

1. **`next lint` was removed.** `npm run lint` calls the ESLint CLI directly.
2. **ESLint 9 reads flat config.** `.eslintrc.json` became `eslint.config.mjs`.
   `FlatCompat` fails outright on `eslint-config-next@16` (a circular `react` property
   while validating the old schema), so the native flat configs are imported instead —
   which also avoids silently dropping rules on the next upgrade.
3. **Next 16 writes an `AGENTS.md` into the project on every dev start.** Disabled via
   `agentRules: false`; the repo's conventions live in `CLAUDE.md`.

A stale `.next` directory from the Next 15 build produced
`ensureInstrumentationRegisteredWithDeferredSpans is not a function` on every route — worth
noting because the error names nothing relevant. Clearing `.next` fixes it.

---

## 4. Deviations from the plan

| Planned | Built | Why |
| --- | --- | --- |
| Component unit tests (vitest) | None | vitest is installed and configured but has **zero tests**. The journeys cover the behaviour and the a11y suite covers the markup contracts. Recorded as a gap, not quietly dropped |
| MapLibre basemap | Blank coordinate canvas by default | `NEXT_PUBLIC_MAP_TILE_URL` is empty in the demo and an operator network may have no internet route. The geography of a fault is readable without a basemap, and a test asserts the fallback renders |
| i18n en/sw | Not built | Swahili exists where it is customer-facing (the notification templates, hand-written). The operator console is an internal English-language tool; translating it was judged lower value than the pages themselves. Recorded as a gap |
| AI Quality page | Not built | The gateway's accounting and the agent traces are exposed and tested; no page renders them yet |
| Admin page | Not built | No admin action exists that a human needs a page for yet — reporting has no send button by design |
| SSE live updates on the pages | Stream endpoint built and tested; pages poll on a 30s stale time | The console is correct either way, and polling is simpler to reason about on a wall display. The ticket-based SSE path is tested and ready |

---

## 5. Assumptions

1. **Brand values are placeholders and are labelled as such** in `tokens.css`, in
   `public/brand/README.md`, and now in a test. No logo is embedded; the wordmark is text.
2. **Dev auth mode** (password equals username) is how the role switcher works. The
   settings validator refuses that auth mode outside local/dev, so it cannot reach
   production.
3. **The API is not started by Playwright.** The journeys need the seeded database behind
   the real API, and starting it from the test runner would hide whether the stack comes
   up. A dead API fails the run loudly, which is the right outcome.

---

## 6. Known gaps

1. **No component unit tests.** vitest is wired with nothing in it.
2. **No i18n in the console.** Customer-facing templates are bilingual; the console is not.
3. **No AI Quality or Admin page.**
4. **React 18 is deprecated in Next 16** and will be unsupported in Next 17. The upgrade to
   React 19 is a tech-debt item, not a blocker today.
5. **Playwright runs chromium only.** Firefox and WebKit are configured out, not tested.
6. **No visual regression testing.** Contrast and structure are asserted; layout is not.
7. **Load testing is Phase 6** and is not covered here.
