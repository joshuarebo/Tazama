/**
 * AI Quality — the console's honesty page.
 *
 * Most AI products show you what the AI said. This page shows you what the AI *is*: whether
 * a model is reachable at all, what it has cost, what each agent is allowed to touch, and
 * what happens when the model is absent.
 *
 * It exists because "AI degraded" is a first-class state in this product rather than an
 * error, and a reviewer should be able to see that state and its consequences without
 * reading a log. The fallback column is the point: every agent keeps working without a
 * model, and this is where you can read how.
 */
"use client";

import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  Loading,
  Stat,
  Table,
  num,
} from "@/components/ui";
import { askKnowledge, getAiStatus, type AiAnswer } from "@/lib/api";

export default function AiQualityPage() {
  const status = useQuery({ queryKey: ["ai-status"], queryFn: getAiStatus, staleTime: 15_000 });

  return (
    <div className="grid gap-4">
      <header>
        <h1 className="text-xl font-semibold">AI quality</h1>
        <p className="text-sm text-ink-muted">
          What the model endpoint is doing, what the AI has cost, and what each agent is
          allowed to touch. Every agent works without a model — less fluently, and it says
          so.
        </p>
      </header>

      {status.isLoading ? (
        <Loading what="the model status" />
      ) : status.error ? (
        <ErrorNote error={status.error} retry={() => status.refetch()} />
      ) : !status.data ? (
        <Empty message="No status available" />
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat
              label="Model endpoint"
              value={status.data.degraded ? "degraded" : "live"}
              hint={
                status.data.configured
                  ? status.data.reachable
                    ? status.data.models.join(", ") || "reachable"
                    : "configured but unreachable"
                  : "no endpoint configured"
              }
              tone={status.data.degraded ? "warn" : "ok"}
            />
            <Stat
              label="Calls"
              value={num(status.data.accounting.calls)}
              hint={`${num(status.data.accounting.cached)} from cache · ${num(
                status.data.accounting.degraded,
              )} degraded`}
            />
            <Stat
              label="Tokens"
              value={num(status.data.accounting.tokens)}
              hint={`${num(status.data.budget_remaining.tokens)} left in this process`}
            />
            <Stat
              label="Tool calling"
              value={status.data.tool_calling ? "supported" : "unavailable"}
              hint={status.data.probe_note}
              tone={status.data.tool_calling ? "default" : "warn"}
            />
          </div>

          {status.data.degraded && (
            <div
              className="rounded border border-warn bg-warn-soft p-3 text-sm"
              role="status"
            >
              <strong>Running without a model.</strong> This is a supported configuration,
              not a fault: every AI feature falls back to a deterministic path, and the
              console says so wherever it matters. Diagnosis still scores 7 of 7 on the
              labelled scenarios using rules alone.
            </div>
          )}

          <Card title="The three agents">
            <Table
              head={["Agent", "What it does", "Highest tier it may use", "Without a model"]}
              caption="The agents, their permission ceilings and their fallbacks"
            >
              {status.data.agents.map((agent) => (
                <tr key={agent.name} className="border-b border-line align-top last:border-0">
                  <td className="px-2 py-2 font-medium">{agent.name}</td>
                  <td className="px-2 py-2">{agent.purpose}</td>
                  <td className="px-2 py-2">
                    <Badge tone={agent.ceiling === "propose" ? "warn" : "neutral"}>
                      {agent.ceiling}
                    </Badge>
                  </td>
                  <td className="px-2 py-2 text-ink-muted">{agent.fallback}</td>
                </tr>
              ))}
            </Table>
            <p className="mt-3 text-xs text-ink-muted">
              No agent holds a tool that can act. <code className="font-mono">propose</code>{" "}
              returns a proposal for a human to approve and writes nothing;{" "}
              <code className="font-mono">execute_runbook</code> is registered only so that
              an agent reaching for it is recorded, and it is refused before any handler
              runs.
            </p>
          </Card>

          {Object.keys(status.data.accounting.by_task).length > 0 && (
            <Card title="Calls by task">
              <ul className="grid gap-1">
                {Object.entries(status.data.accounting.by_task)
                  .sort((a, b) => b[1] - a[1])
                  .map(([task, count]) => (
                    <li key={task} className="flex items-center gap-2 text-sm">
                      <span className="w-48 font-mono text-xs">{task}</span>
                      <span className="stat">{count}</span>
                    </li>
                  ))}
              </ul>
            </Card>
          )}

          <AskPanel />
        </>
      )}
    </div>
  );
}

function AskPanel() {
  const [question, setQuestion] = useState("");
  const ask = useMutation<AiAnswer, unknown, string>({
    mutationFn: (text) => askKnowledge(text),
  });

  return (
    <Card title="Ask the operations knowledge base">
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (question.trim()) ask.mutate(question.trim());
        }}
      >
        <label className="grid flex-1 gap-1 text-xs font-medium uppercase tracking-wide text-ink-muted">
          Question
          <input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="how are alarm storms handled?"
            className="w-full rounded border border-line bg-surface px-2 py-1.5 text-sm font-normal normal-case text-ink"
          />
        </label>
        <Button variant="primary" type="submit" disabled={ask.isPending}>
          {ask.isPending ? "Looking…" : "Ask"}
        </Button>
      </form>

      <p className="mt-2 text-xs text-ink-muted">
        It answers only from the SOP corpus, and it declines rather than guessing. Expired
        procedures are never retrieved, and an answer citing a document that was not
        retrieved is rejected outright.
      </p>

      {ask.isPending && <Loading what="an answer" />}
      {ask.error ? (
        <div className="mt-3">
          <ErrorNote error={ask.error} />
        </div>
      ) : null}

      {ask.data && (
        <div className="mt-3 grid gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={ask.data.declined ? "warn" : "ok"}>
              {ask.data.declined ? "declined" : "answered"}
            </Badge>
            <Badge tone="neutral">method: {ask.data.method}</Badge>
          </div>
          <p className="text-sm">{ask.data.answer}</p>
          {ask.data.citations.length > 0 && (
            <ul className="flex flex-wrap gap-1">
              {ask.data.citations.map((citation, index) => (
                <li key={`${citation.doc_id}-${index}`}>
                  <Badge tone="brand">{citation.doc_id}</Badge>
                </li>
              ))}
            </ul>
          )}
          {ask.data.declined && (
            <p className="text-xs text-ink-muted">
              A refusal is the correct answer when the documentation does not cover the
              question. It is the behaviour that makes the answers it *does* give worth
              acting on.
            </p>
          )}
        </div>
      )}
    </Card>
  );
}
