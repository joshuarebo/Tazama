/**
 * One cell's KPI curves across the incident window.
 *
 * Four series, chosen because together they separate the three faults the engine can
 * detect: availability collapsing is an outage, PRB utilisation pinned high with
 * throughput falling is congestion, and a drop rate climbing while availability holds is
 * a quality fault. The dashed marker is the moment the incident started, so the eye can
 * check that the curve actually turned there rather than taking the detection on trust.
 *
 * ECharts is loaded client-side only: it touches `window` on import, and the chart is
 * below the fold anyway.
 */
"use client";

import dynamic from "next/dynamic";
import { useMemo } from "react";

import { Loading } from "@/components/ui";

const ReactECharts = dynamic(() => import("echarts-for-react"), {
  ssr: false,
  loading: () => <Loading what="the chart" />,
});

type Point = Record<string, number | string | null>;

const PERCENT_SERIES = [
  { key: "availability_pct", label: "Availability %", token: "--ok" },
  { key: "prb_utilisation_pct", label: "PRB utilisation %", token: "--warn" },
  { key: "drop_rate_pct", label: "Drop rate %", token: "--danger" },
] as const;

export function KpiChart({
  cellId,
  points,
  tz,
  markAt,
}: {
  cellId: string;
  points: Point[];
  tz: string;
  markAt: string | null;
}) {
  const option = useMemo(() => {
    const labels = points.map((point) => {
      const at = String(point.at ?? "");
      try {
        return new Intl.DateTimeFormat("en-GB", {
          timeZone: tz,
          day: "2-digit",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
        }).format(new Date(at));
      } catch {
        return at;
      }
    });

    const markIndex = markAt
      ? points.findIndex((point) => String(point.at ?? "") >= markAt)
      : -1;

    return {
      // `color` is not read from tokens because ECharts needs resolved values; each series
      // sets its own colour from a CSS variable below instead.
      grid: { left: 44, right: 52, top: 28, bottom: 52 },
      tooltip: { trigger: "axis" },
      legend: { bottom: 0, textStyle: { fontSize: 10 } },
      xAxis: {
        type: "category",
        data: labels,
        axisLabel: { fontSize: 9, interval: Math.max(1, Math.floor(labels.length / 6)) },
      },
      yAxis: [
        { type: "value", name: "%", min: 0, max: 100, axisLabel: { fontSize: 9 } },
        { type: "value", name: "Mbps", axisLabel: { fontSize: 9 } },
      ],
      series: [
        ...PERCENT_SERIES.map((series, index) => ({
          name: series.label,
          type: "line",
          yAxisIndex: 0,
          showSymbol: false,
          smooth: false,
          lineStyle: { width: 1.6 },
          itemStyle: { color: cssVar(series.token) },
          data: points.map((point) => point[series.key] ?? null),
          // Only the first series carries the marker, or it is drawn three times.
          ...(index === 0 && markIndex >= 0
            ? {
                markLine: {
                  silent: true,
                  symbol: "none",
                  label: { formatter: "incident start", fontSize: 9 },
                  lineStyle: { type: "dashed", color: cssVar("--text-muted") },
                  data: [{ xAxis: markIndex }],
                },
              }
            : {}),
        })),
        {
          name: "DL throughput (Mbps)",
          type: "line",
          yAxisIndex: 1,
          showSymbol: false,
          lineStyle: { width: 1.6, type: "dotted" },
          itemStyle: { color: cssVar("--info") },
          data: points.map((point) => point.dl_throughput_mbps ?? null),
        },
      ],
    };
  }, [points, tz, markAt]);

  return (
    <figure className="m-0">
      <figcaption className="mb-1 font-mono text-xs text-ink-muted">{cellId}</figcaption>
      <ReactECharts option={option} style={{ height: 260, width: "100%" }} notMerge />
    </figure>
  );
}

/** Resolve a design token to the value ECharts needs, with a readable fallback. */
function cssVar(name: string): string {
  if (typeof window === "undefined") return "#888888";
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || "#888888";
}
