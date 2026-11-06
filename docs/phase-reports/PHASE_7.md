# Phase 7 — Finalise the agents, natural-language query, presenter's material

Status: **gate green**. The three agents are reachable from the running application, the
Data Explorer takes questions in plain English, and the material needed to present this to
leadership exists.

The finding that justified the phase: **the agents were built, tested, passing 36 evals —
and nothing in the running application called them.** Diagnosis on an incident card came
from the deterministic rules path. Presenting the product as an AI platform while the AI
could not be clicked was a claim the demo could not back.

---

## 1. What was built

| Component | Where | Role |
| --- | --- | --- |
| `SqlAgentData` | `infrastructure/db/agent_repo.py` | The only database access the agents have. Read-only by construction |
| AI router | `api/v1/routers/ai.py` | `POST /ai/diagnose/{id}`, `POST /ai/ask`, `POST /ai/sql`, `GET /ai/status` |
| `SqlSuggestion` | `ai/llm/schemas.py` | A *proposed* query, shape-checked before it reaches a screen |
| `ai:diagnose` capability | `core/security.py` | noc_engineer, analyst, admin |
| AI diagnosis panel | `web/src/features/AiDiagnosis.tsx` | Cause, evidence, and the agent's trace |
| Ask-in-English box | `web/src/features/AskSql.tsx` | Question → SQL → human approval → the existing guard |
| AI Quality page | `web/src/app/ai/page.tsx` | Model status, cost, agent ceilings, fallbacks |
| ADR 0010 | `docs/adr/` | Why text-to-SQL proposes and never executes |

Documentation: `docs/DEMO_SCRIPT.md`, `docs/architecture/algorithm-choices.md`,
`docs/ROADMAP.md`, a rewritten `docs/architecture/overview.md`, and an updated `README.md`.

---

## 2. Gate results

```
tests/api_e2e/test_ai_api.py         22 passed
tests/security/test_text_to_sql.py   26 passed
tests/unit/test_agent_repo.py        16 passed
npm run lint / tsc --noEmit          clean
npm run build                        9 routes
import-linter                        5/5 contracts
```

The full `make ci` run was interrupted by the host running low on memory, not by a failure;
every suite above was run and passed individually, and `ruff`, `ruff format --check` and
`lint-imports` are clean. **`make ci` should be re-run before this is considered signed
off.**

Verified live against the seeded database, end to end:

- `/ai/diagnose` on the busiest incident: cause `planned_maintenance` at 0.95, method
  `rules`, 6 tools called, budget 2/4 steps and 6/10 calls.
- `/ai/sql` → *"which regions had the most complaints last week"* → a SELECT the guard
  accepts → run through `/discovery/query` → **Nairobi 1,550, Mombasa 915, Nakuru 888,
  Kisumu 767**.
- `/ai/ask` → *"what is the escalation tree"* → **declines**, because the only escalation
  document in the corpus is the deliberately-superseded SOP-00.

---

## 3. Four real defects found

### The gateway never told the model which task it was performing

The fake LLM routes its canned responses on a `TASK:` marker in the prompt. The gateway
never emitted one, so **every** request fell through to the server's generic answer, failed
schema validation twice, and took its deterministic fallback.

This had been invisible since Phase 4 because the Phase 4 evals scripted their responses
through `MockTransport` — the fake server's own routing was never exercised. The integration
looked like it worked precisely *because the fallbacks are good*. The task name is now the
first line of the user prompt, which also helps a real model answer a named task
consistently.

**Lesson applied:** `tests/api_e2e/test_ai_api.py` drives the real fake server through an
ASGI bridge rather than scripting responses, so this class of gap cannot hide again.

### `.env` was resolved against the working directory

`get_settings()` used a bare `".env"`, which resolves against the process's cwd. The API
runs from `backend/`, so it found **no env file at all** and every setting silently fell back
to its default — including `LLM_BASE_URL`, which made the whole AI surface report "not
configured" while a correctly-filled `.env` sat in the repo root.

Same class as the two working-directory bugs fixed in Phase 1 (`config/` and `SQLITE_PATH`)
and the same fix: anchor to `repo_root()`. `TAZAMA_ENV_FILE` still overrides, so CI and the
test suite remain environment-only.

This one is worth dwelling on: the symptom was *the AI does not work*, and the cause was a
relative path. Nothing about the gateway, the agents or the model was wrong.

### The fake model server always fabricated its citations

Its `knowledge_answer` response hardcoded `doc_id: "KB-TRANSMISSION"`, which exists in no
real corpus. The KnowledgeAssistant's grounding check correctly rejected **every** answer as
a fabricated citation — so the knowledge path could never succeed, and the rejection looked
like the only behaviour it had.

The fake now cites documents it was actually supplied, by reading the `doc_id` values out of
the prompt. A fake should model a *competent* model by default; adversarial behaviour is what
the injectable `::fake:` switches are for.

### `ASGITransport` cannot be driven by a synchronous client

A test fixture wired the in-process fake server into the gateway with
`httpx.ASGITransport`, which is async-only, so the gateway's capability probe could not
reach it at all. `TestClient` subclasses `httpx.Client` and carries a synchronous bridge —
noted here because the failure mode (`reachable=False` with no error) is indistinguishable
from a down server.

---

## 4. Design decisions worth defending

**The AI panel is opt-in, not automatic.** Running a diagnosis spends model budget and
writes a decision record. An incident card should not do either just because somebody opened
it.

**The trace is the panel's most important half.** A cause and a confidence score are easy to
show and impossible to trust. Tools called, tools *refused*, and budget spent is what an
engineer can audit — and it turns "the AI cannot act" from an assurance into something a
reviewer checks in ten seconds.

**Text-to-SQL proposes; the guard executes; a human decides.** ADR 0010 has the full
reasoning. The short version: the dangerous failure is not a destructive statement — the
schema and the guard stop those, and a test proves it against a *fully compromised* model
returning `DROP TABLE complaint`. The dangerous failure is a query that is valid, permitted
and subtly wrong. Showing the SQL makes that reviewable; showing only the answer makes it
invisible.

**`make demo` now starts the model server.** Otherwise a fresh demo shows "AI degraded" on
every panel and the AI cannot be demonstrated at all. `.env.example` points at the bundled
deterministic server by default — local, offline, and nothing leaves the machine — with the
comment that it must be empty or a real endpoint anywhere else.

---

## 5. Deviations from the plan

| Planned | Built | Why |
| --- | --- | --- |
| `/ai/sql` gated on a new capability | Gated on the existing `explorer:query` | Suggesting a query to someone who may not run one is theatre, and this keeps the two surfaces' audiences identical by construction |
| Reporter exposed through the API | Status only | The digest already renders on `/reports` through `modules/reporting`; a second path to the same prose would be two things to keep consistent |
| Hybrid retrieval for the assistant | Keyword search with expiry filtering | The corpus is eight documents. Citation integrity is what makes an answer safe, and that is built and tested. Tech-debt #32 |

---

## 6. Known gaps

1. **Text-to-SQL accuracy is unmeasured**, and cannot honestly be measured on a synthetic
   schema. The *mechanism* is demonstrated — schema, guard, approval, audit — not the
   *capability*. `docs/ROADMAP.md` §8.
2. **The fake server returns one canned query**, so the tests prove the chain rather than
   the model's SQL quality.
3. **Retrieval is keyword-based over eight documents.** Ranking by matched-term count is
   exact at this size and will not scale.
4. **`make ci` has not completed since these changes** — interrupted by host memory
   pressure. Re-run before sign-off.
5. **The agents are still not in the pipeline.** They are reachable on demand from the
   console; the automated pipeline continues to use the deterministic path. That is
   deliberate — an agent in the pipeline spends budget on every incident — but it means the
   per-incident `method` on a card reflects an *operator's* request, not the pipeline's.
6. **One runbook still missing**, and the three privacy procedures in `ROADMAP.md` still
   block real data.
