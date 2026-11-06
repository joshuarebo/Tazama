/**
 * The AI diagnosis panel.
 *
 * It shows three things, and the order is deliberate: **the conclusion**, **the evidence it
 * rests on**, and **what the agent was and was not allowed to do**.
 *
 * That last part is why the panel exists at all. A cause and a confidence score are easy to
 * show and impossible to trust. A trace showing which tools ran, which were *refused*, and
 * what budget was spent is the thing an engineer can audit — and the thing that makes
 * "the AI cannot act" a statement someone can check rather than take on faith.
 *
 * It is opt-in (a button) rather than automatic: running it spends model budget and writes
 * a decision record, and an incident card should not do either just because someone opened
 * it.
 */
"use client";

import { useMutation } from "@tanstack/react-query";

import { Badge, Button, Card, ErrorNote, Loading } from "@/components/ui";
import { diagnoseIncident, type AgentTrace, type AiDiagnosis } from "@/lib/api";

/** How the answer was reached. The honest field, and the one worth explaining. */
const METHOD_NOTE: Record<string, string> = {
  rules: "Deterministic rules were conclusive, so no model was consulted.",
  llm: "The model answered and every piece of evidence it cited was verified.",
  none: "Nothing could be concluded from the available evidence.",
};

export function AiDiagnosis({ incidentId }: { incidentId: string }) {
  const run = useMutation<AiDiagnosis, unknown, void>({
    mutationFn: () => diagnoseIncident(incidentId),
  });

  return (
    <Card
      title="AI diagnosis"
      action={
        <Button
          variant="primary"
          disabled={run.isPending}
          onClick={() => run.mutate()}
        >
          {run.isPending ? "Thinking…" : run.data ? "Run again" : "Diagnose"}
        </Button>
      }
    >
      {run.isPending ? (
        <Loading what="the diagnosis" />
      ) : run.error ? (
        <ErrorNote error={run.error} retry={() => run.mutate()} />
      ) : !run.data ? (
        <p className="text-sm text-ink-muted">
          Runs the DiagnosisAgent over this incident&apos;s alarms, topology, complaints and
          history. It can recommend an approved runbook; it cannot execute one.
        </p>
      ) : (
        <Result result={run.data} />
      )}
    </Card>
  );
}

function Result({ result }: { result: AiDiagnosis }) {
  const confident = result.confidence >= result.confidence_floor;
  return (
    <div className="grid gap-4">
      {/* --- the conclusion --- */}
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-lg font-semibold">
          {result.cause ? result.cause.replace(/_/g, " ") : "no cause established"}
        </span>
        <Badge tone={confident ? "ok" : "warn"}>
          confidence {(result.confidence * 100).toFixed(0)}%
        </Badge>
        <Badge tone={result.method === "llm" ? "brand" : "neutral"}>
          method: {result.method}
        </Badge>
        {result.needs_human && <Badge tone="warn">needs a human</Badge>}
      </div>

      <p className="text-sm text-ink-muted">
        {METHOD_NOTE[result.method] ?? ""}
        {!confident &&
          ` Below the ${(result.confidence_floor * 100).toFixed(0)}% floor, so this is routed to a human rather than acted on.`}
      </p>

      {result.summary && <p className="text-sm">{result.summary}</p>}

      {/* --- the evidence --- */}
      {result.evidence.length > 0 && (
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
            Evidence
          </h3>
          <ul className="mt-2 grid gap-2">
            {result.evidence.map((item, index) => (
              <li
                key={`${item.ref}-${index}`}
                className="flex flex-wrap items-baseline gap-2 rounded border border-line bg-surface-sunken p-2 text-sm"
              >
                <Badge tone="brand">{item.kind.replace(/_/g, " ")}</Badge>
                <span className="font-mono text-xs text-ink-muted">{item.ref}</span>
                <span className="flex-1">{item.note}</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-ink-muted">
            Every reference above was checked against the evidence the agent was actually
            given. A diagnosis citing something it was never shown is discarded.
          </p>
        </div>
      )}

      {/* --- the recommendation --- */}
      {result.runbook_id && (
        <div className="rounded border border-line bg-surface-raised p-3 text-sm">
          <strong>Recommended runbook:</strong>{" "}
          <span className="font-mono">{result.runbook_id}</span>
          <p className="mt-1 text-xs text-ink-muted">
            A recommendation awaiting approval. Nothing has been executed, and there is no
            path from here that could execute it.
          </p>
        </div>
      )}

      <Trace trace={result.trace} />
    </div>
  );
}

function Trace({ trace }: { trace: AgentTrace }) {
  const budget = trace.budget;
  return (
    <details className="rounded border border-line bg-surface-sunken p-3">
      <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-ink-muted">
        What the agent did ({trace.called.length} tool call
        {trace.called.length === 1 ? "" : "s"}
        {trace.refused.length > 0 ? `, ${trace.refused.length} refused` : ""})
      </summary>

      <div className="mt-3 grid gap-3 text-sm">
        <div>
          <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
            Steps
          </span>
          <ol className="mt-1 grid gap-0.5">
            {trace.steps.map((step, index) => (
              <li key={index} className="text-sm">
                {index + 1}. {step}
              </li>
            ))}
          </ol>
        </div>

        <div className="flex flex-wrap gap-4">
          <div>
            <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
              Tools called
            </span>
            <ul className="mt-1 flex flex-wrap gap-1">
              {trace.called.map((name) => (
                <li key={name}>
                  <Badge tone="ok">{name}</Badge>
                </li>
              ))}
            </ul>
          </div>

          {/* The refusals are the most important thing on this panel. */}
          <div>
            <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
              Tools refused
            </span>
            <ul className="mt-1 flex flex-wrap gap-1">
              {trace.refused.length === 0 ? (
                <li className="text-xs text-ink-muted">
                  none requested — nothing above the agent&apos;s ceiling was reachable
                </li>
              ) : (
                trace.refused.map((name) => (
                  <li key={name}>
                    <Badge tone="danger">{name}</Badge>
                  </li>
                ))
              )}
            </ul>
          </div>
        </div>

        <dl className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-ink-muted">
          <div>
            <dt className="inline font-medium">Steps: </dt>
            <dd className="inline">
              {budget.steps} of {budget.max_steps}
            </dd>
          </div>
          <div>
            <dt className="inline font-medium">Tool calls: </dt>
            <dd className="inline">
              {budget.tool_calls} of {budget.max_tool_calls}
            </dd>
          </div>
          <div>
            <dt className="inline font-medium">Elapsed: </dt>
            <dd className="inline">
              {budget.elapsed_s}s of {budget.max_seconds}s
            </dd>
          </div>
          <div>
            <dt className="inline font-medium">Stopped because: </dt>
            <dd className="inline">{trace.stopped_because}</dd>
          </div>
        </dl>

        {trace.degraded && (
          <p className="text-xs text-warn">
            The model was unavailable or its answer could not be verified, so the
            deterministic path produced this result.
          </p>
        )}
      </div>
    </details>
  );
}
