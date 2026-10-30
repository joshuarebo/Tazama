/**
 * Data Explorer.
 *
 * The only component in the product allowed to take SQL from a person — and it is the
 * component most likely to be attacked, so the UI is built to make the guard visible
 * rather than to hide it: the allowlisted tables are listed, the refusal is shown verbatim
 * with the reason, masked columns are labelled in the result header, and the SQL the
 * server actually ran (after the row cap was applied) is printed back.
 *
 * A refusal is a successful outcome here, not an error, which is why the deliberately
 * dangerous query is a one-click button: a reviewer should be able to see the guard work
 * without having to think of an attack themselves.
 */
"use client";

import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { Badge, Button, Card, Empty, ErrorNote, Loading, Table } from "@/components/ui";
import { ApiError, getTables, runQuery, type QueryResult } from "@/lib/api";

const EXAMPLES: { label: string; sql: string; note: string }[] = [
  {
    label: "Complaints per region, last 7 days of data",
    note: "safe — allowlisted tables, bounded window anchored on the data",
    // Anchored on the newest complaint, not on `now`. The seeded database sits still, so a
    // `DATETIME('now', '-7 days')` window returns zero rows and the flagship example looks
    // broken while being perfectly correct. The same trap as the insights window.
    sql: `SELECT region, COUNT(*) AS complaints
FROM complaint
WHERE received_at >= (SELECT DATETIME(MAX(received_at), '-7 days') FROM complaint)
GROUP BY region
ORDER BY complaints DESC`,
  },
  {
    label: "Worst availability cells",
    note: "safe — aggregate over the KPI table",
    sql: `SELECT cell_id, ROUND(AVG(availability_pct), 2) AS avg_availability
FROM kpi_cell
WHERE granularity = '1h'
GROUP BY cell_id
ORDER BY avg_availability ASC
LIMIT 20`,
  },
  {
    label: "Unlocated complaints by channel",
    note: "safe — the join-failure view",
    sql: `SELECT channel, COUNT(*) AS unlocated
FROM complaint
WHERE cell_id IS NULL
GROUP BY channel
ORDER BY unlocated DESC`,
  },
  {
    label: "Try to delete data",
    note: "must be refused — the guard is SELECT-only",
    sql: "DELETE FROM complaint WHERE 1=1",
  },
  {
    label: "Try a stacked statement",
    note: "must be refused — one statement per query",
    sql: "SELECT 1; DROP TABLE complaint",
  },
  {
    label: "Try a table outside the allowlist",
    note: "must be refused — allowlist, not denylist",
    sql: "SELECT * FROM sqlite_master",
  },
];

export default function ExplorerPage() {
  const [sql, setSql] = useState(EXAMPLES[0]!.sql);
  const tables = useQuery({ queryKey: ["tables"], queryFn: getTables });

  const run = useMutation<QueryResult, unknown, string>({ mutationFn: (text) => runQuery(text) });

  const refusal = run.error instanceof ApiError ? run.error : null;

  return (
    <div className="grid gap-4">
      <header>
        <h1 className="text-xl font-semibold">Data Explorer</h1>
        <p className="text-sm text-ink-muted">
          SELECT only. Parsed before execution, restricted to allowlisted tables, row-capped,
          timed out, PII-masked and audited — every query, accepted or refused.
        </p>
      </header>

      <div className="grid gap-4 lg:grid-cols-[1fr_18rem]">
        <div className="grid gap-4">
          <Card
            title="Query"
            action={
              <Button
                variant="primary"
                disabled={run.isPending || !sql.trim()}
                onClick={() => run.mutate(sql)}
              >
                {run.isPending ? "Running…" : "Run query"}
              </Button>
            }
          >
            <label className="sr-only" htmlFor="sql">
              SQL query
            </label>
            <textarea
              id="sql"
              value={sql}
              onChange={(event) => setSql(event.target.value)}
              spellCheck={false}
              rows={8}
              className="w-full rounded border border-line bg-surface-sunken p-3 font-mono text-sm text-ink"
            />
            <div className="mt-3 flex flex-wrap gap-2">
              {EXAMPLES.map((example) => (
                <button
                  key={example.label}
                  type="button"
                  onClick={() => {
                    setSql(example.sql);
                    run.reset();
                  }}
                  title={example.note}
                  className="rounded border border-line bg-surface px-2 py-1 text-xs text-ink-muted hover:bg-surface-raised hover:text-ink"
                >
                  {example.label}
                </button>
              ))}
            </div>
          </Card>

          {refusal && (
            <div className="rounded border border-warn bg-warn-soft p-4" role="status">
              <div className="flex items-center gap-2">
                <Badge tone="warn">refused by the SQL guard</Badge>
                <span className="font-mono text-xs text-ink-muted">
                  HTTP {refusal.status} · {refusal.type.split("/").pop()}
                </span>
              </div>
              <p className="mt-2 text-sm text-ink">{refusal.message}</p>
              <p className="mt-1 text-xs text-ink-muted">
                The query was never sent to the database. The attempt is in the audit log.
              </p>
            </div>
          )}

          {run.error && !refusal ? <ErrorNote error={run.error} /> : null}

          {run.data && <Result result={run.data} />}

          {!run.data && !run.error && !run.isPending && (
            <Card title="Result">
              <Empty message="Nothing run yet" hint="Pick an example or write a SELECT." />
            </Card>
          )}
        </div>

        <Card title="Allowlisted tables">
          {tables.isLoading ? (
            <Loading what="the allowlist" />
          ) : tables.error ? (
            <ErrorNote error={tables.error} retry={() => tables.refetch()} />
          ) : (
            <ul className="grid gap-1 font-mono text-xs">
              {(tables.data ?? []).map((table) => (
                <li key={table}>
                  <button
                    type="button"
                    className="text-left text-brand-strong hover:underline"
                    onClick={() => setSql(`SELECT *\nFROM ${table}\nLIMIT 50`)}
                  >
                    {table}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}

function Result({ result }: { result: QueryResult }) {
  return (
    <Card
      title="Result"
      action={
        <span className="text-xs text-ink-muted">
          {result.row_count} row(s) · {result.duration_ms} ms
          {result.truncated ? " · truncated at the row cap" : ""}
        </span>
      }
    >
      {result.warnings.length > 0 && (
        <ul className="mb-3 grid gap-1">
          {result.warnings.map((warning, index) => (
            <li key={index} className="text-xs text-warn">
              {warning}
            </li>
          ))}
        </ul>
      )}

      {!result.rows.length ? (
        <Empty message="No rows" />
      ) : (
        <Table
          head={result.columns.map((column) => (
            <span key={column.name} className="inline-flex items-center gap-1">
              {column.name}
              {column.masked && <Badge tone="info">masked</Badge>}
            </span>
          ))}
          caption="Query result"
        >
          {result.rows.map((row, rowIndex) => (
            <tr key={rowIndex} className="border-b border-line last:border-0">
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} className="px-2 py-1 font-mono text-xs">
                  {cell === null ? <span className="text-ink-muted">null</span> : String(cell)}
                </td>
              ))}
            </tr>
          ))}
        </Table>
      )}

      <details className="mt-3">
        <summary className="cursor-pointer text-xs text-ink-muted">SQL as executed</summary>
        <pre className="mt-2 overflow-x-auto rounded border border-line bg-surface-sunken p-2 font-mono text-xs">
          {result.executed_sql}
        </pre>
      </details>
    </Card>
  );
}
