/**
 * Incident card — the one screen an engineer defends a decision from.
 *
 * Order matters: what we think is wrong and why (with the evidence that says so), then the
 * geography, then the raw signals the conclusion rests on (alarms, KPI curves, the
 * complaints themselves), then what was decided and what happened before. Nothing here is
 * an assertion without a trail: every claim on this page is backed by a row in the
 * database, and the decision list shows who or what made each call.
 */
"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { IncidentMap } from "@/features/IncidentMap";
import { KpiChart } from "@/features/KpiChart";
import {
  Badge,
  Card,
  Empty,
  ErrorNote,
  Loading,
  PriorityBadge,
  StatusPill,
  Table,
  num,
  timeIn,
} from "@/components/ui";
import { getIncident, getMeta, type Decision } from "@/lib/api";

export default function IncidentCard() {
  const params = useParams<{ id: string }>();
  const id = decodeURIComponent(String(params.id ?? ""));
  const meta = useQuery({ queryKey: ["meta"], queryFn: getMeta });
  const tz = meta.data?.display_tz ?? "Africa/Nairobi";

  const detail = useQuery({
    queryKey: ["incident", id],
    queryFn: () => getIncident(id),
    enabled: Boolean(id),
  });

  if (detail.isLoading) return <Loading what="the incident" />;
  if (detail.error) return <ErrorNote error={detail.error} retry={() => detail.refetch()} />;
  if (!detail.data) return <Empty message="Incident not found" />;

  const { incident, cells, alarms, complaints, kpi_series, decisions, similar, maintenance } =
    detail.data;

  // The diagnosis is whatever the pipeline last concluded; its evidence is attached to the
  // decision row, not re-derived here, so the page cannot disagree with the audit log.
  const diagnosis =
    [...decisions].reverse().find((d) => d.action.includes("diagnos") || d.action.includes("cause")) ??
    [...decisions].reverse().find((d) => d.evidence.length > 0) ??
    null;

  return (
    <div className="grid gap-4">
      <header className="grid gap-2">
        <div className="flex items-center gap-2 text-sm text-ink-muted">
          <Link href="/" className="text-brand-strong hover:underline">
            Command Center
          </Link>
          <span aria-hidden="true">/</span>
          <span className="font-mono">{incident.id}</span>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <PriorityBadge priority={incident.priority} />
          <h1 className="text-xl font-semibold">{incident.title}</h1>
          <StatusPill status={incident.status} />
          {incident.cause && <Badge tone="info">{incident.cause.replace(/_/g, " ")}</Badge>}
        </div>
        <dl className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-ink-muted">
          <div>
            <dt className="inline font-medium">Region: </dt>
            <dd className="inline text-ink">{incident.region}</dd>
          </div>
          <div>
            <dt className="inline font-medium">Detected: </dt>
            <dd className="inline text-ink">{timeIn(tz, incident.detected_at)}</dd>
          </div>
          <div>
            <dt className="inline font-medium">Started: </dt>
            <dd className="inline text-ink">{timeIn(tz, incident.started_at)}</dd>
          </div>
          <div>
            <dt className="inline font-medium">Duration: </dt>
            <dd className="inline text-ink">
              {incident.duration_minutes === null ? "ongoing" : `${incident.duration_minutes} min`}
            </dd>
          </div>
          {incident.primary_node_id && (
            <div>
              <dt className="inline font-medium">Primary node: </dt>
              <dd className="inline font-mono text-ink">
                {incident.primary_node_id} ({incident.primary_node_kind})
              </dd>
            </div>
          )}
        </dl>
      </header>

      {incident.suppressed_reason && (
        <div className="rounded border border-line bg-surface-raised p-3 text-sm" role="note">
          <strong>Suppressed:</strong> {incident.suppressed_reason.replace(/_/g, " ")}. No customer
          notification is proposed for a suppressed incident.
        </div>
      )}

      {/* --- impact, stated in the units a manager asks about --- */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Figure label="Subscribers affected" value={num(incident.affected_subs)} tone="danger" />
        <Figure label="Cells" value={num(incident.affected_cells)} hint={`${incident.affected_sites} site(s)`} />
        <Figure label="Complaints" value={num(incident.complaint_count)} hint="correlated to this incident" />
        <Figure label="Enterprise accounts" value={num(incident.enterprise_accounts)} />
        <Figure
          label="SLA risk"
          value={incident.sla_risk === null ? "—" : `${(incident.sla_risk * 100).toFixed(0)}%`}
          tone={(incident.sla_risk ?? 0) > 0.5 ? "warn" : "default"}
        />
      </div>

      {/* --- the conclusion and its evidence --- */}
      <Card title="Probable cause and evidence">
        {!diagnosis ? (
          <Empty
            message="No diagnosis recorded"
            hint="The correlation engine detected this incident but nothing has written a cause decision yet."
          />
        ) : (
          <div className="grid gap-3">
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="text-lg font-semibold">
                {(incident.cause ?? diagnosis.action).replace(/_/g, " ")}
              </span>
              {diagnosis.confidence !== null && (
                <Badge tone={diagnosis.confidence >= 0.7 ? "ok" : "warn"}>
                  confidence {(diagnosis.confidence * 100).toFixed(0)}%
                </Badge>
              )}
              <span className="text-xs text-ink-muted">
                by {diagnosis.actor}
                {diagnosis.rule_version ? ` · rules ${diagnosis.rule_version}` : ""} ·{" "}
                {timeIn(tz, diagnosis.occurred_at)}
              </span>
            </div>
            {diagnosis.evidence.length === 0 ? (
              <p className="text-sm text-ink-muted">This decision carries no attached evidence.</p>
            ) : (
              <ul className="grid gap-2">
                {diagnosis.evidence.map((item, index) => (
                  <li
                    key={`${item.ref}-${index}`}
                    className="flex flex-wrap items-baseline gap-2 rounded border border-line bg-surface-sunken p-2 text-sm"
                  >
                    <Badge tone="brand">{item.kind.replace(/_/g, " ")}</Badge>
                    <span className="font-mono text-xs text-ink-muted">{item.ref}</span>
                    <span className="flex-1">{item.note}</span>
                    <span className="stat text-xs text-ink-muted">
                      weight {item.weight.toFixed(2)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-[3fr_2fr]">
        <Card title="Affected cells" action={<span className="text-xs text-ink-muted">{cells.length} cells</span>}>
          {cells.length ? (
            <IncidentMap points={cells} tileUrl={meta.data?.map_tile_url ?? ""} />
          ) : (
            <Empty message="No cells attached" />
          )}
        </Card>

        <Card title="Alarms" action={<span className="text-xs text-ink-muted">{alarms.length}</span>}>
          {!alarms.length ? (
            <Empty message="No alarms in the window" hint="A fault can be KPI-only — silent to the alarm system." />
          ) : (
            <Table head={["Node", "Alarm", "Severity", "Raised", "Cleared"]} caption="Alarms in the incident window">
              {alarms.map((alarm) => (
                <tr key={alarm.id} className="border-b border-line last:border-0">
                  <td className="px-2 py-1.5 font-mono text-xs">{alarm.node_id}</td>
                  <td className="px-2 py-1.5">
                    {alarm.alarm_type.replace(/_/g, " ")}
                    {alarm.is_flapping && (
                      <span className="ml-1">
                        <Badge tone="warn">flapping</Badge>
                      </span>
                    )}
                  </td>
                  <td className="px-2 py-1.5">
                    <Badge tone={alarm.severity === "critical" ? "danger" : "warn"}>
                      {alarm.severity}
                    </Badge>
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-ink-muted">
                    {timeIn(tz, alarm.raised_at)}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-ink-muted">
                    {timeIn(tz, alarm.cleared_at)}
                  </td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      </div>

      {/* --- the curves the detection actually fired on --- */}
      <Card
        title="KPI behaviour"
        action={<span className="text-xs text-ink-muted">hourly, ±padding around the window</span>}
      >
        {!kpi_series.length ? (
          <Empty message="No KPI samples in the window" />
        ) : (
          <div className="grid gap-4 xl:grid-cols-2">
            {kpi_series.map((series) => (
              <KpiChart
                key={series.cell_id}
                cellId={series.cell_id}
                points={series.points}
                tz={tz}
                markAt={incident.started_at ?? incident.detected_at}
              />
            ))}
          </div>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card
          title="Complaints"
          action={
            <Link
              href={`/complaints?incident_id=${encodeURIComponent(incident.id)}`}
              className="text-xs text-brand-strong hover:underline"
            >
              open in Complaints
            </Link>
          }
        >
          {!complaints.length ? (
            <Empty
              message="No complaints correlated"
              hint="A network-first detection can precede the first call — that is the point of it."
            />
          ) : (
            <Table head={["Received", "Channel", "Category", "Cell", "Text"]} caption="Correlated complaints">
              {complaints.map((complaint) => (
                <tr key={complaint.id} className="border-b border-line last:border-0 align-top">
                  <td className="whitespace-nowrap px-2 py-1.5 text-ink-muted">
                    {timeIn(tz, complaint.received_at)}
                  </td>
                  <td className="px-2 py-1.5">{complaint.channel}</td>
                  <td className="px-2 py-1.5">
                    {complaint.category ? complaint.category.replace(/_/g, " ") : "—"}
                  </td>
                  <td className="px-2 py-1.5 font-mono text-xs">{complaint.cell_id ?? "—"}</td>
                  <td className="px-2 py-1.5">{complaint.text_preview}</td>
                </tr>
              ))}
            </Table>
          )}
        </Card>

        <div className="grid gap-4 content-start">
          <Card title="Decisions">
            {!decisions.length ? (
              <Empty message="No decisions recorded" />
            ) : (
              <ol className="grid gap-2">
                {decisions.map((decision: Decision, index) => (
                  <li
                    key={`${decision.action}-${index}`}
                    className="grid gap-0.5 border-l-2 border-brand pl-3"
                  >
                    <div className="flex flex-wrap items-baseline gap-2 text-sm">
                      <strong>{decision.action.replace(/_/g, " ")}</strong>
                      <Badge tone={decision.outcome === "rejected" ? "danger" : "ok"}>
                        {decision.outcome}
                      </Badge>
                    </div>
                    <div className="text-xs text-ink-muted">
                      {decision.actor} · {timeIn(tz, decision.occurred_at)}
                      {decision.confidence !== null
                        ? ` · confidence ${(decision.confidence * 100).toFixed(0)}%`
                        : ""}
                      {decision.evidence.length ? ` · ${decision.evidence.length} evidence item(s)` : ""}
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </Card>

          {maintenance.length > 0 && (
            <Card title="Planned maintenance overlapping this window">
              <ul className="grid gap-1 text-sm">
                {maintenance.map((window, index) => (
                  <li key={index} className="font-mono text-xs">
                    {JSON.stringify(window)}
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <Card title="Similar past incidents">
            {!similar.length ? (
              <Empty message="Nothing comparable in history" />
            ) : (
              <Table head={["Incident", "Cause", "Region", "Detected", "Subs"]} caption="Similar incidents">
                {similar.map((other) => (
                  <tr key={other.id} className="border-b border-line last:border-0">
                    <td className="px-2 py-1.5">
                      <Link
                        href={`/incidents/${other.id}`}
                        className="text-brand-strong hover:underline"
                      >
                        {other.title}
                      </Link>
                    </td>
                    <td className="px-2 py-1.5">{other.cause?.replace(/_/g, " ") ?? "—"}</td>
                    <td className="px-2 py-1.5">{other.region}</td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-ink-muted">
                      {timeIn(tz, other.detected_at)}
                    </td>
                    <td className="stat px-2 py-1.5">{num(other.affected_subs)}</td>
                  </tr>
                ))}
              </Table>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

function Figure({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "default" | "danger" | "warn";
}) {
  const toneClass = { default: "text-ink", danger: "text-danger", warn: "text-warn" }[tone];
  return (
    <div className="card p-3">
      <div className="text-xs font-medium uppercase tracking-wide text-ink-muted">{label}</div>
      <div className={`stat mt-0.5 text-2xl font-semibold ${toneClass}`}>{value}</div>
      {hint && <div className="text-xs text-ink-muted">{hint}</div>}
    </div>
  );
}
