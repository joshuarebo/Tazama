/**
 * The complaint rate, raw against de-seasonalised.
 *
 * Both lines on one axis on purpose: the gap between them *is* the seasonality, and seeing
 * it is what stops someone reading the evening busy hour as an incident.
 */
"use client";

import dynamic from "next/dynamic";
import { useMemo } from "react";

import { Loading } from "@/components/ui";

const ReactECharts = dynamic(() => import("echarts-for-react"), {
  ssr: false,
  loading: () => <Loading what="the chart" />,
});

export function RateChart({
  labels,
  raw,
  deseasonalised,
  tz,
}: {
  labels: string[];
  raw: number[];
  deseasonalised: number[];
  tz: string;
}) {
  const option = useMemo(() => {
    const formatted = labels.map((iso) => {
      try {
        return new Intl.DateTimeFormat("en-GB", {
          timeZone: tz,
          day: "2-digit",
          month: "short",
          hour: "2-digit",
        }).format(new Date(iso));
      } catch {
        return iso;
      }
    });
    return {
      grid: { left: 48, right: 16, top: 24, bottom: 48 },
      tooltip: { trigger: "axis" },
      legend: { bottom: 0, textStyle: { fontSize: 10 } },
      xAxis: {
        type: "category",
        data: formatted,
        axisLabel: { fontSize: 9, interval: Math.max(1, Math.floor(formatted.length / 8)) },
      },
      yAxis: { type: "value", name: "per 1k subs/h", axisLabel: { fontSize: 9 } },
      series: [
        {
          name: "rate per 1,000 subscribers",
          type: "line",
          showSymbol: false,
          lineStyle: { width: 1.4 },
          itemStyle: { color: cssVar("--info") },
          data: raw,
        },
        {
          name: "de-seasonalised",
          type: "line",
          showSymbol: false,
          lineStyle: { width: 1.8 },
          itemStyle: { color: cssVar("--brand-green") },
          data: deseasonalised,
        },
      ],
    };
  }, [labels, raw, deseasonalised, tz]);

  return <ReactECharts option={option} style={{ height: 300, width: "100%" }} notMerge />;
}

function cssVar(name: string): string {
  if (typeof window === "undefined") return "#888888";
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || "#888888";
}
