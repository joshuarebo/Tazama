/**
 * Insights.
 *
 * Exploratory analytics, shown with their qualifications attached. Every panel prints the
 * number of points it ran on and the caveats the service returned, because the whole
 * failure mode of a page like this is a confident-looking coefficient computed over a
 * series with holes in it.
 *
 * The lead-lag panel is the one that earns the page: if a KPI reliably turns hours before
 * the complaints do, that is the warning time the product is built to exploit.
 */
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Badge, Card, Empty, ErrorNote, Loading, Table, num } from "@/components/ui";
import { RateChart } from "@/features/RateChart";
import {
  getBusiestCells,
  getComplaintRate,
  getCorrelations,
  getDrivers,
  getLeadLag,
  getMeta,
  getPca,
} from "@/lib/api";

const KPIS = [
  "availability_pct",
  "rrc_setup_success_pct",
  "erab_setup_success_pct",
  "drop_rate_pct",
  "prb_utilisation_pct",
  "dl_throughput_mbps",
  "ul_throughput_mbps",
  "latency_ms",
  "active_users",
  "sinr_db",
];

export default function InsightsPage() {
  const [days, setDays] = useState(14);
  const [kpi, setKpi] = useState(KPIS[0]!);
  const [cellId, setCellId] = useState<string>("");

  const meta = useQuery({ queryKey: ["meta"], queryFn: getMeta });
  const tz = meta.data?.display_tz ?? "Africa/Nairobi";

  const cells = useQuery({ queryKey: ["busiest-cells"], queryFn: () => getBusiestCells(12) });
  const effectiveCell = cellId || cells.data?.[0]?.cell_id || "";

  const rate = useQuery({
    queryKey: ["complaint-rate", days],
    queryFn: () => getComplaintRate(`days=${days}`),
  });
  const correlations = useQuery({
    queryKey: ["correlations", days],
    queryFn: () => getCorrelations(`days=${days}`),
  });
  const drivers = useQuery({
    queryKey: ["drivers", days],
    queryFn: () => getDrivers(`days=${days}`),
  });
  const pca = useQuery({ queryKey: ["pca", days], queryFn: () => getPca(`days=${days}`) });
  const leadLag = useQuery({
    queryKey: ["lead-lag", effectiveCell, kpi, days],
    queryFn: () => getLeadLag(`cell_id=${encodeURIComponent(effectiveCell)}&kpi=${kpi}&days=${days}`),
    enabled: Boolean(effectiveCell),
  });

  return (
    <div className="grid gap-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Insights</h1>
          <p className="text-sm text-ink-muted">
            Exploratory analysis over hourly buckets, anchored on the latest data rather
            than on the wall clock, so the same window reproduces.
          </p>
        </div>
        <label className="grid gap-1 text-xs font-medium uppercase tracking-wide text-ink-muted">
          Window
          <select
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
            className="rounded border border-line bg-surface px-2 py-1.5 text-sm font-normal normal-case text-ink"
          >
            {[7, 14, 21, 30].map((value) => (
              <option key={value} value={value}>
                last {value} days of data
              </option>
            ))}
          </select>
        </label>
      </header>

      {/* --- the normalised rate, the basis of everything else here --- */}
      <Card
        title="Complaint rate"
        action={
          rate.data ? (
            <span className="text-xs text-ink-muted">
              {rate.data.n_points} hourly points · {rate.data.gaps} gap(s) · denominator{" "}
              {num(rate.data.subscribers_est)} subscribers
            </span>
          ) : null
        }
      >
        {rate.isLoading ? (
          <Loading what="the complaint rate" />
        ) : rate.error ? (
          <ErrorNote error={rate.error} retry={() => rate.refetch()} />
        ) : !rate.data?.n_points ? (
          <Empty message="No complaints in this window" />
        ) : (
          <>
            <RateChart
              labels={rate.data.bucket_starts}
              raw={rate.data.rate_per_1k}
              deseasonalised={rate.data.deseasonalised}
              tz={tz}
            />
            <Caveats items={rate.data.caveats} />
          </>
        )}
      </Card>

      {/* --- lead-lag: the finding that justifies acting early --- */}
      <Card
        title="Lead and lag"
        action={
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={effectiveCell}
              onChange={(event) => setCellId(event.target.value)}
              className="rounded border border-line bg-surface px-2 py-1 font-mono text-xs text-ink"
              aria-label="Cell"
            >
              {(cells.data ?? []).map((cell) => (
                <option key={cell.cell_id} value={cell.cell_id}>
                  {cell.cell_id} · {cell.region} · {cell.complaints} complaints
                </option>
              ))}
            </select>
            <select
              value={kpi}
              onChange={(event) => setKpi(event.target.value)}
              className="rounded border border-line bg-surface px-2 py-1 text-xs text-ink"
              aria-label="KPI"
            >
              {KPIS.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {!effectiveCell ? (
          <Empty message="No cell has enough complaints to analyse" />
        ) : leadLag.isLoading ? (
          <Loading what="the cross-correlation" />
        ) : leadLag.error ? (
          <ErrorNote error={leadLag.error} retry={() => leadLag.refetch()} />
        ) : !leadLag.data ? null : (
          <div className="grid gap-3">
            <p className="text-sm">
              <strong>{leadLag.data.reading}</strong>
            </p>
            <div className="flex flex-wrap gap-2 text-xs">
              <Badge tone={leadLag.data.kpi_leads ? "ok" : "neutral"}>
                {leadLag.data.kpi_leads ? "KPI leads" : "no KPI lead"}
              </Badge>
              <Badge tone="neutral">
                best lag{" "}
                {leadLag.data.best_lag_hours === null ? "—" : `${leadLag.data.best_lag_hours}h`}
              </Badge>
              <Badge tone="neutral">
                r at zero lag{" "}
                {leadLag.data.zero_lag_correlation === null
                  ? "—"
                  : leadLag.data.zero_lag_correlation.toFixed(3)}
              </Badge>
              <Badge tone="neutral">{leadLag.data.n_points} points</Badge>
            </div>
            <LagBars lags={leadLag.data.lags} correlations={leadLag.data.correlations} />
            <Caveats items={leadLag.data.caveats} />
          </div>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* --- candidate drivers --- */}
        <Card
          title="Candidate drivers"
          action={<span className="text-xs text-ink-muted">mutual information, unsigned</span>}
        >
          {drivers.isLoading ? (
            <Loading what="driver candidates" />
          ) : drivers.error ? (
            <ErrorNote error={drivers.error} retry={() => drivers.refetch()} />
          ) : !drivers.data?.drivers.length ? (
            <Empty message="Not enough points to estimate mutual information" />
          ) : (
            <>
              <ul className="grid gap-1.5">
                {drivers.data.drivers.map((driver) => {
                  const max = drivers.data!.drivers[0]!.score || 1;
                  return (
                    <li key={driver.kpi} className="flex items-center gap-2 text-sm">
                      <span className="w-6 text-right text-xs text-ink-muted">{driver.rank}</span>
                      <span className="w-44 truncate">{driver.kpi}</span>
                      <div
                        className="h-2 rounded-sm"
                        style={{
                          width: `${Math.max(2, (driver.score / max) * 100)}%`,
                          background: "var(--info)",
                        }}
                        aria-hidden="true"
                      />
                      <span className="stat text-xs">{driver.score.toFixed(3)}</span>
                    </li>
                  );
                })}
              </ul>
              <p className="mt-3 text-xs text-ink-muted">
                Candidates, not drivers: mutual information is unconditional, so a high score
                can come from a shared third cause. A supervised model ranks them.
              </p>
              <Caveats items={drivers.data.drivers[0]?.caveats ?? []} />
            </>
          )}
        </Card>

        {/* --- correlation pairs --- */}
        <Card
          title="Strongest relationships"
          action={
            correlations.data ? (
              <span className="text-xs text-ink-muted">{correlations.data.n_points} points</span>
            ) : null
          }
        >
          {correlations.isLoading ? (
            <Loading what="correlations" />
          ) : correlations.error ? (
            <ErrorNote error={correlations.error} retry={() => correlations.refetch()} />
          ) : !correlations.data?.top.length ? (
            <Empty message="No usable pairs in this window" />
          ) : (
            <>
              <Table head={["Pair", "Pearson", "Spearman"]} caption="Ranked correlation pairs">
                {correlations.data.top.map((pair) => (
                  <tr key={`${pair.left}-${pair.right}`} className="border-b border-line last:border-0">
                    <td className="px-2 py-1.5 text-sm">
                      {pair.left} <span className="text-ink-muted">×</span> {pair.right}
                    </td>
                    <td className="stat px-2 py-1.5">{pair.pearson?.toFixed(3) ?? "—"}</td>
                    <td className="stat px-2 py-1.5">{pair.spearman?.toFixed(3) ?? "—"}</td>
                  </tr>
                ))}
              </Table>
              <Caveats items={correlations.data.caveats} />
            </>
          )}
        </Card>
      </div>

      {/* --- structure --- */}
      <Card
        title="KPI structure (PCA)"
        action={
          pca.data ? (
            <span className="text-xs text-ink-muted">
              {pca.data.labels.length} series · {pca.data.n_points} points
            </span>
          ) : null
        }
      >
        {pca.isLoading ? (
          <Loading what="the decomposition" />
        ) : pca.error ? (
          <ErrorNote error={pca.error} retry={() => pca.refetch()} />
        ) : !pca.data?.explained_variance_ratio.length ? (
          <Empty message="Not enough data to decompose" />
        ) : (
          <div className="grid gap-3 lg:grid-cols-[1fr_2fr]">
            <div>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
                Scree
              </h3>
              <ul className="mt-2 grid gap-1">
                {pca.data.explained_variance_ratio.map((ratio, index) => (
                  <li key={index} className="flex items-center gap-2 text-sm">
                    <span className="w-10 text-xs text-ink-muted">PC{index + 1}</span>
                    <div
                      className="h-2 rounded-sm"
                      style={{
                        width: `${Math.max(2, ratio * 100)}%`,
                        background: "var(--brand-green)",
                      }}
                      aria-hidden="true"
                    />
                    <span className="stat text-xs">{(ratio * 100).toFixed(1)}%</span>
                    <span className="text-xs text-ink-muted">
                      cum {(pca.data!.cumulative_variance[index]! * 100).toFixed(1)}%
                    </span>
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
                Loadings on PC1 and PC2
              </h3>
              <Table head={["Series", "PC1", "PC2"]} caption="PCA loadings">
                {pca.data.labels.map((label, index) => (
                  <tr key={label} className="border-b border-line last:border-0">
                    <td className="px-2 py-1 text-sm">{label}</td>
                    <td className="stat px-2 py-1">
                      {pca.data!.loadings[0]?.[index]?.toFixed(3) ?? "—"}
                    </td>
                    <td className="stat px-2 py-1">
                      {pca.data!.loadings[1]?.[index]?.toFixed(3) ?? "—"}
                    </td>
                  </tr>
                ))}
              </Table>
            </div>
            <div className="lg:col-span-2">
              <p className="text-xs text-ink-muted">
                Variance is not relevance: PCA knows nothing about complaints. Use it to see
                collinearity, and rank drivers with supervised importance instead.
              </p>
              <Caveats items={pca.data.caveats} />
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}

function Caveats({ items }: { items: string[] }) {
  if (!items.length) return null;
  return (
    <ul className="mt-3 grid gap-1 border-t border-line pt-2">
      {items.map((item, index) => (
        <li key={index} className="text-xs text-ink-muted">
          ⚠ {item}
        </li>
      ))}
    </ul>
  );
}

/** A horizontal bar per lag. Positive lags are to the right: the KPI moved first. */
function LagBars({ lags, correlations }: { lags: number[]; correlations: (number | null)[] }) {
  const max = Math.max(...correlations.map((value) => Math.abs(value ?? 0)), 0.01);
  return (
    <div className="grid gap-1">
      {lags.map((lag, index) => {
        const value = correlations[index] ?? null;
        const width = value === null ? 0 : (Math.abs(value) / max) * 50;
        return (
          <div key={lag} className="flex items-center gap-2 text-xs">
            <span className="w-12 text-right text-ink-muted">
              {lag > 0 ? `+${lag}h` : `${lag}h`}
            </span>
            <div className="relative h-3 flex-1 rounded-sm" style={{ background: "var(--surface-sunken)" }}>
              <span className="absolute left-1/2 top-0 h-full w-px" style={{ background: "var(--border)" }} />
              {value !== null && (
                <span
                  className="absolute top-0 h-full rounded-sm"
                  style={{
                    width: `${width}%`,
                    left: value >= 0 ? "50%" : `${50 - width}%`,
                    background: value >= 0 ? "var(--ok)" : "var(--danger)",
                  }}
                />
              )}
            </div>
            <span className="stat w-14 text-right">{value === null ? "—" : value.toFixed(3)}</span>
          </div>
        );
      })}
      <p className="mt-1 text-xs text-ink-muted">
        A positive lag means the KPI moved first and complaints followed.
      </p>
    </div>
  );
}
