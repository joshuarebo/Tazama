/**
 * Reports — the Teams digest, previewed.
 *
 * The card shown here is rendered from the same Adaptive Card JSON the scheduled sender
 * would post, so the preview is a real check rather than a mock-up: if the renderer below
 * shows it, Teams has the same payload. The raw JSON is one click away for exactly that
 * reason.
 *
 * There is no send button. The API has no send endpoint, and the panel says so in plain
 * words, because "I thought it was a preview" is how an unapproved report reaches a
 * channel.
 */
"use client";

import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Badge, Card, Empty, ErrorNote, Loading, PriorityBadge, num, timeIn } from "@/components/ui";
import { getDigest, getMeta } from "@/lib/api";

type Kind = "daily" | "weekly";

export default function ReportsPage() {
  const [kind, setKind] = useState<Kind>("daily");
  const meta = useQuery({ queryKey: ["meta"], queryFn: getMeta });
  const tz = meta.data?.display_tz ?? "Africa/Nairobi";
  const digest = useQuery({ queryKey: ["digest", kind], queryFn: () => getDigest(kind) });

  return (
    <div className="grid gap-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Reports</h1>
          <p className="text-sm text-ink-muted">
            The Microsoft Teams digest, built and validated but never sent from this screen.
          </p>
        </div>
        <div className="flex gap-1">
          {(["daily", "weekly"] as Kind[]).map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setKind(option)}
              aria-current={kind === option ? "true" : undefined}
              className={`rounded px-3 py-1.5 text-sm font-medium ${
                kind === option
                  ? "bg-brand-soft text-brand-strong"
                  : "text-ink-muted hover:bg-surface-raised hover:text-ink"
              }`}
            >
              {option}
            </button>
          ))}
        </div>
      </header>

      {digest.isLoading ? (
        <Loading what="the digest" />
      ) : digest.error ? (
        <ErrorNote error={digest.error} retry={() => digest.refetch()} />
      ) : !digest.data ? (
        <Empty message="No digest" />
      ) : (
        <>
          <div className="rounded border border-line bg-surface-raised p-3 text-sm" role="note">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={digest.data.dry_run ? "ok" : "warn"}>
                {digest.data.dry_run ? "TEAMS_DRY_RUN = true" : "TEAMS_DRY_RUN = false"}
              </Badge>
              <Badge tone={digest.data.webhook_configured ? "info" : "neutral"}>
                {digest.data.webhook_configured ? "webhook configured" : "no webhook configured"}
              </Badge>
              <span className="font-mono text-xs text-ink-muted">{digest.data.report_id}</span>
              <span className="text-xs text-ink-muted">{digest.data.card_bytes} bytes</span>
            </div>
            <p className="mt-2 text-ink-muted">{digest.data.delivery_note}</p>
          </div>

          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            {/* --- the card as Teams would render it --- */}
            <Card title="Teams card preview">
              <div
                className="rounded border border-line p-4"
                style={{ background: "var(--surface-sunken)" }}
              >
                <h3 className="text-base font-semibold">{digest.data.title}</h3>
                <p className="text-xs text-ink-muted">
                  {timeIn(tz, digest.data.window_start)} – {timeIn(tz, digest.data.window_end)} (
                  {digest.data.display_tz})
                </p>

                {digest.data.is_demo && (
                  <p className="mt-2 text-sm font-semibold text-warn">
                    DEMO DATA — synthetic network, synthetic customers.
                  </p>
                )}

                <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                  {digest.data.headline.map(([label, value]) => (
                    <div key={label} className="col-span-2 grid grid-cols-subgrid">
                      <dt className="font-semibold">{label}</dt>
                      <dd>{value}</dd>
                    </div>
                  ))}
                </dl>

                <h4 className="mt-4 text-sm font-semibold">Incidents</h4>
                {!digest.data.incidents.length ? (
                  <p className="text-sm text-ink-muted">No live incidents in this window.</p>
                ) : (
                  <ul className="mt-1 grid gap-2">
                    {digest.data.incidents.slice(0, 12).map((incident) => (
                      <li key={incident.incident_id} className="flex gap-2">
                        <PriorityBadge priority={incident.priority} />
                        <div className="min-w-0">
                          <Link
                            href={`/incidents/${incident.incident_id}`}
                            className="text-sm font-medium text-brand-strong hover:underline"
                          >
                            {incident.title}
                          </Link>
                          <div className="text-xs text-ink-muted">
                            {incident.region} ·{" "}
                            {(incident.cause ?? "cause not yet established").replace(/_/g, " ")} ·{" "}
                            {incident.affected_cells} cell(s) · ~{num(incident.affected_subs)}{" "}
                            subscribers · {incident.complaint_count} complaint(s)
                            {incident.suppressed_reason
                              ? ` · suppressed: ${incident.suppressed_reason.replace(/_/g, " ")}`
                              : ""}
                          </div>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
                {digest.data.incidents.length > 12 && (
                  <p className="mt-2 text-xs text-ink-muted">
                    {digest.data.incidents.length - 12} further incident(s) in the payload.
                  </p>
                )}

                {digest.data.sections.map((section) => (
                  <div key={section.heading} className="mt-4">
                    <h4 className="text-sm font-semibold">{section.heading}</h4>
                    <ul className="mt-1 grid gap-0.5">
                      {section.lines.map((line, index) => (
                        <li key={index} className="text-sm text-ink-muted">
                          • {line}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}

                {digest.data.caveats.length > 0 && (
                  <p className="mt-4 border-t border-line pt-2 text-xs text-ink-muted">
                    Caveats: {digest.data.caveats.join(" ")}
                  </p>
                )}
              </div>

              <p className="mt-3 text-xs text-ink-muted">
                No subscriber identifier of any kind reaches a card: the payload is scanned for
                MSISDN, pseudonym-token and e-mail shapes before it is returned, and a hit blocks
                the report rather than redacting it.
              </p>
            </Card>

            {/* --- the payload itself --- */}
            <Card
              title="Adaptive Card payload"
              action={
                <span className="text-xs text-ink-muted">
                  schema {String((digest.data.card as { version?: string }).version ?? "1.5")}
                </span>
              }
            >
              {/* A scrollable region needs keyboard access, or a keyboard user cannot
                  reach the rest of the payload (axe: scrollable-region-focusable). */}
              <pre
                tabIndex={0}
                role="region"
                aria-label="Adaptive Card payload as JSON"
                className="max-h-[32rem] overflow-auto rounded border border-line bg-surface-sunken p-3 font-mono text-xs"
              >
                {JSON.stringify(digest.data.card, null, 2)}
              </pre>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
