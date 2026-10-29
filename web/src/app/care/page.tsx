/**
 * Care agent lookup.
 *
 * Built for someone with a customer on the line, so the answer is top-left and the
 * evidence is underneath: is there a known problem on this subscriber's cell, what am I
 * allowed to say, and has anything already been sent to them.
 *
 * The approved wording is shown as a quote with a copy button and no edit field. That is
 * the product rule made physical: an agent may read the approved line, not improvise one.
 */
"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  Loading,
  PriorityBadge,
  Table,
  num,
  timeIn,
} from "@/components/ui";
import { careLookup, getCareSamples, getMeta, type CareLookup } from "@/lib/api";

export default function CarePage() {
  const [identifier, setIdentifier] = useState("");
  const meta = useQuery({ queryKey: ["meta"], queryFn: getMeta });
  const tz = meta.data?.display_tz ?? "Africa/Nairobi";
  const samples = useQuery({ queryKey: ["care-samples"], queryFn: () => getCareSamples(6) });

  const lookup = useMutation<CareLookup, unknown, string>({
    mutationFn: (value) => careLookup(value),
  });

  function run(value: string) {
    const trimmed = value.trim();
    if (!trimmed) return;
    setIdentifier(trimmed);
    lookup.mutate(trimmed);
  }

  const result = lookup.data;

  return (
    <div className="grid gap-4">
      <header>
        <h1 className="text-xl font-semibold">Care lookup</h1>
        <p className="text-sm text-ink-muted">
          One subscriber, one screen. The number is pseudonymised the moment it is
          submitted — it is never stored, logged or shown back.
        </p>
      </header>

      <Card title="Find a subscriber">
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            run(identifier);
          }}
        >
          <label className="grid gap-1 text-xs font-medium uppercase tracking-wide text-ink-muted">
            MSISDN or pseudonym token
            <input
              value={identifier}
              onChange={(event) => setIdentifier(event.target.value)}
              placeholder="07XXXXXXXX or SUB-…"
              autoComplete="off"
              className="w-80 rounded border border-line bg-surface px-2 py-1.5 font-mono text-sm"
            />
          </label>
          <Button variant="primary" type="submit" disabled={lookup.isPending}>
            {lookup.isPending ? "Looking up…" : "Look up"}
          </Button>
        </form>

        <div className="mt-3 text-xs text-ink-muted">
          The demo seed keeps no raw numbers — only the shape each one arrived in — so a
          real number cannot match. These seeded subscribers have complaint history:
          <div className="mt-2 flex flex-wrap gap-2">
            {samples.isLoading ? (
              <span>loading…</span>
            ) : (
              (samples.data ?? []).map((token) => (
                <button
                  key={token}
                  type="button"
                  onClick={() => run(token)}
                  className="rounded border border-line bg-surface px-2 py-1 font-mono text-xs text-brand-strong hover:bg-surface-raised"
                >
                  {token}
                </button>
              ))
            )}
          </div>
        </div>
      </Card>

      {lookup.error ? <ErrorNote error={lookup.error} /> : null}

      {result && !result.found && (
        <Card title="Result">
          <Empty
            message="No subscriber matches that identifier"
            hint={result.notes.join(" ")}
          />
        </Card>
      )}

      {result?.found && result.subscriber && (
        <>
          <div className="grid gap-4 lg:grid-cols-[2fr_3fr]">
            <Card title="Subscriber">
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
                <Row label="Token" value={<span className="font-mono text-xs">{result.subscriber.token}</span>} />
                <Row label="Region" value={result.subscriber.region} />
                <Row label="Segment" value={result.subscriber.segment.replace(/_/g, " ")} />
                <Row
                  label="Account"
                  value={
                    result.subscriber.is_enterprise ? (
                      <Badge tone="brand">
                        enterprise{result.subscriber.sla_tier ? ` · ${result.subscriber.sla_tier}` : ""}
                      </Badge>
                    ) : (
                      "consumer"
                    )
                  }
                />
                <Row
                  label="Serving cell"
                  value={
                    result.serving_cell ? (
                      <span className="font-mono text-xs">
                        {result.serving_cell.cell_id} · {result.serving_cell.tech}
                      </span>
                    ) : (
                      "unknown"
                    )
                  }
                />
                <Row label="Last seen" value={timeIn(tz, result.subscriber.last_seen_at)} />
                <Row
                  label="Notifications"
                  value={
                    result.subscriber.opt_out ? (
                      <Badge tone="warn">opted out</Badge>
                    ) : (
                      "opted in"
                    )
                  }
                />
                {result.subscriber.is_roaming && (
                  <Row label="Roaming" value={<Badge tone="info">yes</Badge>} />
                )}
                <Row
                  label="Number format"
                  value={
                    <span className="font-mono text-xs">
                      {result.subscriber.msisdn_format ?? "—"}
                    </span>
                  }
                />
              </dl>
            </Card>

            <Card title="Known issue">
              {!result.known_issue ? (
                <Empty
                  message="No live incident affects this subscriber"
                  hint="Treat the complaint as individual: there is no network story to tell."
                />
              ) : (
                <div className="grid gap-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <PriorityBadge priority={result.known_issue.priority} />
                    <Link
                      href={`/incidents/${result.known_issue.incident_id}`}
                      className="font-medium text-brand-strong hover:underline"
                    >
                      {result.known_issue.title}
                    </Link>
                    <Badge tone="info">{result.known_issue.status.replace(/_/g, " ")}</Badge>
                  </div>
                  <blockquote
                    className="rounded border-l-4 border-brand bg-brand-soft p-3 text-sm"
                    aria-label="Approved wording to read to the customer"
                  >
                    “{result.known_issue.say_to_customer}”
                    <div className="mt-2 text-xs text-ink-muted">{result.known_issue.eta_note}</div>
                  </blockquote>
                  <div className="flex flex-wrap items-center gap-3 text-xs text-ink-muted">
                    <Button
                      onClick={() => {
                        void navigator.clipboard?.writeText(
                          `${result.known_issue!.say_to_customer} ${result.known_issue!.eta_note}`,
                        );
                      }}
                    >
                      Copy wording
                    </Button>
                    <span>
                      detected {timeIn(tz, result.known_issue.detected_at)} · ~
                      {num(result.known_issue.affected_subs)} subscribers affected
                    </span>
                    <span>
                      {result.known_issue.notification_sent
                        ? "a proactive message has already gone out"
                        : "no proactive message has been sent"}
                    </span>
                  </div>
                  <p className="text-xs text-ink-muted">
                    Approved template wording only. Do not promise a restoration time that is
                    not written above.
                  </p>
                </div>
              )}
            </Card>
          </div>

          {result.notes.length > 0 && (
            <Card title="What the agent should know">
              <ul className="grid gap-1 text-sm">
                {result.notes.map((note, index) => (
                  <li key={index} className="flex gap-2">
                    <span aria-hidden="true">•</span>
                    <span>{note}</span>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Recent complaints">
              {!result.complaints.length ? (
                <Empty message="No complaints on record" />
              ) : (
                <Table head={["Received", "Channel", "Category", "Incident", "Text"]} caption="Complaints">
                  {result.complaints.map((row) => (
                    <tr key={row.id} className="border-b border-line align-top last:border-0">
                      <td className="whitespace-nowrap px-2 py-1.5 text-ink-muted">
                        {timeIn(tz, row.received_at)}
                      </td>
                      <td className="px-2 py-1.5">{row.channel.replace(/_/g, " ")}</td>
                      <td className="px-2 py-1.5">{row.category?.replace(/_/g, " ") ?? "—"}</td>
                      <td className="px-2 py-1.5">
                        {row.incident_id ? (
                          <Link
                            href={`/incidents/${row.incident_id}`}
                            className="font-mono text-xs text-brand-strong hover:underline"
                          >
                            {row.incident_id}
                          </Link>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="px-2 py-1.5">{row.text_preview}</td>
                    </tr>
                  ))}
                </Table>
              )}
            </Card>

            <Card title="Recent care calls">
              {!result.care_calls.length ? (
                <Empty message="No care calls on record" />
              ) : (
                <Table head={["Started", "Duration", "Reason"]} caption="Care calls">
                  {result.care_calls.map((row) => (
                    <tr key={row.id} className="border-b border-line last:border-0">
                      <td className="whitespace-nowrap px-2 py-1.5 text-ink-muted">
                        {timeIn(tz, row.started_at)}
                      </td>
                      <td className="stat px-2 py-1.5">
                        {Math.floor(row.duration_s / 60)}m {row.duration_s % 60}s
                      </td>
                      <td className="px-2 py-1.5">
                        {row.reason_text ?? row.reason_code.replace(/_/g, " ")}
                      </td>
                    </tr>
                  ))}
                </Table>
              )}
            </Card>
          </div>
        </>
      )}

      {!result && !lookup.isPending && !lookup.error && (
        <Card title="Result">
          <Empty message="Nothing looked up yet" hint="Pick a seeded subscriber above." />
        </Card>
      )}

      {lookup.isPending && <Loading what="the subscriber" />}
    </div>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <>
      <dt className="text-ink-muted">{label}</dt>
      <dd>{value}</dd>
    </>
  );
}
