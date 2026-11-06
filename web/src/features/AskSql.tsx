/**
 * Ask a question, get SQL, approve it yourself.
 *
 * The flow is the safety design made visible:
 *
 *   question → the model writes SQL → **you read it** → you press Use → you press Run
 *
 * The model never executes anything. There is no endpoint that would let it. What comes
 * back from `/ai/sql` is a string and the guard's opinion of that string, and the only
 * thing that ever runs a query is the Run button next to the editor — attributed in the
 * audit log to the person who pressed it, which is the correct actor for a query a human
 * approved.
 *
 * So the UI is built to make reading the SQL unavoidable rather than optional: the
 * suggestion appears as code with the guard's verdict beside it, and "Use this query" only
 * loads it into the editor.
 */
"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { Badge, Button, ErrorNote, Loading } from "@/components/ui";
import { suggestSql, type AiSqlSuggestion } from "@/lib/api";

const EXAMPLES = [
  "which regions had the most complaints last week",
  "which cells had the worst availability",
  "how many complaints never resolved to a cell",
];

export function AskSql({ onUse }: { onUse: (sql: string) => void }) {
  const [question, setQuestion] = useState("");

  const ask = useMutation<AiSqlSuggestion, unknown, string>({
    mutationFn: (text) => suggestSql(text),
  });

  function submit(text: string) {
    const trimmed = text.trim();
    if (!trimmed) return;
    setQuestion(trimmed);
    ask.mutate(trimmed);
  }

  return (
    <div className="grid gap-3">
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          submit(question);
        }}
      >
        <label className="grid flex-1 gap-1 text-xs font-medium uppercase tracking-wide text-ink-muted">
          Ask in plain English
          <input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="which regions had the most complaints last week"
            className="w-full rounded border border-line bg-surface px-2 py-1.5 text-sm font-normal normal-case text-ink"
          />
        </label>
        <Button variant="primary" type="submit" disabled={ask.isPending}>
          {ask.isPending ? "Writing SQL…" : "Suggest a query"}
        </Button>
      </form>

      <div className="flex flex-wrap gap-2">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            onClick={() => submit(example)}
            className="rounded border border-line bg-surface px-2 py-1 text-xs text-ink-muted hover:bg-surface-raised hover:text-ink"
          >
            {example}
          </button>
        ))}
      </div>

      {ask.isPending && <Loading what="a suggested query" />}
      {ask.error ? <ErrorNote error={ask.error} /> : null}
      {ask.data && <Suggestion suggestion={ask.data} onUse={onUse} />}
    </div>
  );
}

function Suggestion({
  suggestion,
  onUse,
}: {
  suggestion: AiSqlSuggestion;
  onUse: (sql: string) => void;
}) {
  const accepted = suggestion.guard_verdict === "would-be-accepted";

  if (!suggestion.sql) {
    return (
      <div className="rounded border border-line bg-surface-raised p-3 text-sm" role="status">
        <Badge tone="neutral">no suggestion</Badge>
        <p className="mt-2">{suggestion.explanation}</p>
        <p className="mt-1 text-xs text-ink-muted">{suggestion.note}</p>
      </div>
    );
  }

  return (
    <div className="grid gap-2 rounded border border-line bg-surface-raised p-3" role="status">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={accepted ? "ok" : "warn"}>
          {accepted ? "the guard would accept this" : "the guard would refuse this"}
        </Badge>
        <Badge tone="neutral">confidence {(suggestion.confidence * 100).toFixed(0)}%</Badge>
        <Badge tone="info">reads: {suggestion.tables_used.join(", ") || "—"}</Badge>
        {suggestion.injection_flags.length > 0 && (
          <Badge tone="warn">
            question flagged: {suggestion.injection_flags.join(", ")}
          </Badge>
        )}
      </div>

      <p className="text-sm">{suggestion.explanation}</p>

      <pre className="overflow-x-auto rounded border border-line bg-surface-sunken p-2 font-mono text-xs">
        {suggestion.sql}
      </pre>

      {suggestion.guard_reason && (
        <p className="text-xs text-warn">Guard: {suggestion.guard_reason}</p>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" onClick={() => onUse(suggestion.sql ?? "")}>
          Use this query
        </Button>
        <span className="text-xs text-ink-muted">
          {suggestion.note} Read it first — then press Run.
        </span>
      </div>
    </div>
  );
}
