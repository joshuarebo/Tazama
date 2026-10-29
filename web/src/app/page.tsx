/**
 * Command Center.
 *
 * What a NOC engineer needs in one glance: how much is broken, where, what is driving it,
 * and which incident to open first. Incidents are ordered by priority then recency, because
 * that is the order they should be worked in.
 */
"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { IncidentMap } from "@/features/IncidentMap";
import {
  Badge,
  Card,
  Empty,
  ErrorNote,
  Loading,
  PriorityBadge,
  Stat,
  StatusPill,
  Table,
  num,
  timeIn,
} from "@/components/ui";
import {
  getCoverage,
  getIncidentMap,
  getIncidents,
  getMeta,
  getOverview,
  type IncidentSummary,
} from "@/lib/api";

export default function CommandCenter() {
  const meta = useQuery({ queryKey: ["meta"], queryFn: getMeta });
  const overview = useQuery({ queryKey: ["overview"], queryFn: getOverview });
  const incidents = useQuery({
    queryKey: ["incidents", "live"],
    queryFn: () => getIncidents("limit=50"),
  });
  const points = useQuery({ queryKey: ["incident-map"], queryFn: getIncidentMap });
  const coverage = useQuery({ queryKey: ["coverage"], queryFn: getCoverage });

  const tz = meta.data?.display_tz ?? "Africa/Nairobi";
  const byPriority = overview.data?.by_priority ?? {};

  return (
    <div className="grid gap-4">
      <header className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Command Center</h1>
          <p className="text-sm text-ink-muted">
            {overview.data?.data_window_end
              ? `Network data through ${timeIn(tz, overview.data.data_window_end)}`
              : "Live network and care picture"}
          </p>
        </div>
        {coverage.data && (
          <div className="text-right text-xs text-ink-muted">
            <div>
              complaint → cell coverage{" "}
              <strong className="text-ink">{coverage.data.complaint_to_cell_pct.toFixed(1)}%</strong>
            </div>
            <div>the hardest join in the product, measured on every run</div>
          </div>
        )}
      </header>

      {/* --- headline numbers --- */}
      {overview.isLoading ? (
        <Loading what="the network picture" />
      ) : overview.error ? (
        <ErrorNote error={overview.error} retry={() => overview.refetch()} />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Stat
            label="Live incidents"
            value={num(overview.data?.live_incidents)}
            hint={`${num(overview.data?.suppressed)} suppressed`}
            tone={overview.data && overview.data.live_incidents > 0 ? "danger" : "ok"}
          />
          <Stat
            label="P1 / P2"
            value={`${byPriority.P1 ?? 0} / ${byPriority.P2 ?? 0}`}
            hint="need attention now"
            tone={(byPriority.P1 ?? 0) > 0 ? "danger" : "default"}
          />
          <Stat
            label="Subscribers affected"
            value={num(overview.data?.affected_subs)}
            hint="estimated from cell exposure"
            tone="warn"
          />
          <Stat
            label="Complaints (24h)"
            value={num(overview.data?.complaints_24h)}
            hint={`${num(overview.data?.complaints_correlated)} correlated to an incident`}
          />
          <Stat
            label="Notifications waiting"
            value={num(overview.data?.open_notifications)}
            hint="proposed, awaiting a human"
            tone={(overview.data?.open_notifications ?? 0) > 0 ? "info" : "default"}
          />
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
        {/* --- map --- */}
        <Card
          title="Affected cells"
          action={
            points.data ? (
              <span className="text-xs text-ink-muted">{points.data.length} cells</span>
            ) : null
          }
        >
          {points.isLoading ? (
            <Loading what="the map" />
          ) : points.error ? (
            <ErrorNote error={points.error} retry={() => points.refetch()} />
          ) : !points.data?.length ? (
            <Empty
              message="No affected cells"
              hint="Nothing is currently correlated to an incident."
            />
          ) : (
            <IncidentMap points={points.data} tileUrl={meta.data?.map_tile_url ?? ""} />
          )}
        </Card>

        {/* --- drivers --- */}
        <div className="grid gap-4 content-start">
          <Card title="Probable cause">
            {!overview.data ? (
              <Loading what="causes" />
            ) : !Object.keys(overview.data.by_cause).length ? (
              <Empty message="No live incidents" />
            ) : (
              <ul className="grid gap-2">
                {Object.entries(overview.data.by_cause)
                  .sort((a, b) => b[1] - a[1])
                  .map(([cause, count]) => (
                    <li key={cause} className="flex items-center gap-2">
                      <span className="flex-1 text-sm">{cause.replace(/_/g, " ")}</span>
                      <div
                        className="h-2 rounded-sm"
                        style={{
                          width: `${Math.max(8, (count / overview.data.live_incidents) * 100)}%`,
                          background: "var(--brand-green)",
                        }}
                        aria-hidden="true"
                      />
                      <span className="stat w-6 text-right text-sm font-semibold">{count}</span>
                    </li>
                  ))}
              </ul>
            )}
          </Card>

          <Card title="By region">
            {!overview.data ? (
              <Loading what="regions" />
            ) : !overview.data.by_region.length ? (
              <Empty message="No live incidents" />
            ) : (
              <Table head={["Region", "Incidents", "Subscribers"]} caption="Incidents by region">
                {overview.data.by_region.map((row) => (
                  <tr key={row.region} className="border-b border-line last:border-0">
                    <td className="px-2 py-1.5">{row.region}</td>
                    <td className="stat px-2 py-1.5">{row.incidents}</td>
                    <td className="stat px-2 py-1.5">{num(row.affected_subs)}</td>
                  </tr>
                ))}
              </Table>
            )}
          </Card>
        </div>
      </div>

      {/* --- the queue --- */}
      <Card
        title="Incidents"
        action={
          incidents.data ? (
            <span className="text-xs text-ink-muted">
              {incidents.data.total} total · worst first
            </span>
          ) : null
        }
      >
        {incidents.isLoading ? (
          <Loading what="incidents" />
        ) : incidents.error ? (
          <ErrorNote error={incidents.error} retry={() => incidents.refetch()} />
        ) : !incidents.data?.items.length ? (
          <Empty
            message="No live incidents"
            hint="Run `make pipeline` to detect incidents in the seeded data."
          />
        ) : (
          <Table
            head={[
              "Priority",
              "Incident",
              "Cause",
              "Region",
              "Cells",
              "Subscribers",
              "Complaints",
              "Detected",
              "Status",
            ]}
            caption="Live incidents, highest priority first"
          >
            {incidents.data.items.map((incident: IncidentSummary) => (
              <tr
                key={incident.id}
                className="border-b border-line last:border-0 hover:bg-surface-raised"
              >
                <td className="px-2 py-2">
                  <PriorityBadge priority={incident.priority} />
                </td>
                <td className="px-2 py-2">
                  <Link
                    href={`/incidents/${incident.id}`}
                    className="font-medium text-brand-strong underline-offset-2 hover:underline"
                  >
                    {incident.title}
                  </Link>
                  <div className="font-mono text-xs text-ink-muted">{incident.id}</div>
                </td>
                <td className="px-2 py-2">
                  {incident.cause ? (
                    <Badge tone="info">{incident.cause.replace(/_/g, " ")}</Badge>
                  ) : (
                    <span className="text-ink-muted">unknown</span>
                  )}
                </td>
                <td className="px-2 py-2">{incident.region}</td>
                <td className="stat px-2 py-2">{incident.affected_cells}</td>
                <td className="stat px-2 py-2">{num(incident.affected_subs)}</td>
                <td className="stat px-2 py-2">{incident.complaint_count}</td>
                <td className="whitespace-nowrap px-2 py-2 text-ink-muted">
                  {timeIn(tz, incident.detected_at)}
                </td>
                <td className="px-2 py-2">
                  <StatusPill status={incident.status} />
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </div>
  );
}
