/**
 * The affected-cell map.
 *
 * It must work with **no tile server**: `NEXT_PUBLIC_MAP_TILE_URL` is empty in the demo, and
 * an operator network may have no internet route at all. So the default is a blank
 * coordinate canvas drawn from the cells themselves — the geography of the fault is still
 * readable without a basemap, which is what matters here. Set the tile URL and MapLibre
 * takes over.
 */
"use client";

import { useMemo } from "react";

import type { MapPoint } from "@/lib/api";

type Props = { points: MapPoint[]; tileUrl: string };

const PAD = 0.08;

export function IncidentMap({ points, tileUrl }: Props) {
  const layout = useMemo(() => {
    const lats = points.map((p) => p.lat);
    const lons = points.map((p) => p.lon);
    const minLat = Math.min(...lats) - PAD;
    const maxLat = Math.max(...lats) + PAD;
    const minLon = Math.min(...lons) - PAD;
    const maxLon = Math.max(...lons) + PAD;
    const spanLat = Math.max(maxLat - minLat, 0.01);
    const spanLon = Math.max(maxLon - minLon, 0.01);

    // Group by site so several sectors on one mast do not draw as separate dots.
    const sites = new Map<string, { x: number; y: number; cells: MapPoint[] }>();
    for (const point of points) {
      const key = point.site_id;
      const existing = sites.get(key);
      if (existing) {
        existing.cells.push(point);
        continue;
      }
      sites.set(key, {
        // Latitude increases northwards, the y axis downwards, hence the inversion.
        x: ((point.lon - minLon) / spanLon) * 100,
        y: (1 - (point.lat - minLat) / spanLat) * 100,
        cells: [point],
      });
    }
    return { sites: [...sites.entries()], minLat, maxLat, minLon, maxLon };
  }, [points]);

  if (tileUrl) {
    // A real basemap is configured. Rendering it needs the MapLibre canvas, which is
    // loaded lazily so the demo does not pay for it.
    return <TiledMap points={points} tileUrl={tileUrl} />;
  }

  const maxSubs = Math.max(...points.map((p) => p.subscribers_est), 1);

  return (
    <figure className="m-0">
      <div
        className="relative w-full overflow-hidden rounded border border-line"
        style={{ aspectRatio: "16 / 9", background: "var(--surface-sunken)" }}
        role="img"
        aria-label={`${points.length} affected cells across ${layout.sites.length} sites`}
      >
        {/* A faint graticule, so the dots read as geography rather than a scatter plot. */}
        <svg className="absolute inset-0 h-full w-full" aria-hidden="true">
          {[20, 40, 60, 80].map((pct) => (
            <g key={pct} stroke="var(--border)" strokeWidth="0.5" opacity="0.5">
              <line x1={`${pct}%`} y1="0" x2={`${pct}%`} y2="100%" />
              <line x1="0" y1={`${pct}%`} x2="100%" y2={`${pct}%`} />
            </g>
          ))}
        </svg>

        {layout.sites.map(([siteId, site]) => {
          const subs = site.cells.reduce((total, cell) => total + cell.subscribers_est, 0);
          const size = 10 + Math.round((subs / maxSubs) * 18);
          const region = site.cells[0]?.region ?? "";
          return (
            <span
              key={siteId}
              className="absolute -translate-x-1/2 -translate-y-1/2 rounded-full"
              style={{
                left: `${site.x}%`,
                top: `${site.y}%`,
                width: size,
                height: size,
                background: "var(--danger)",
                opacity: 0.75,
                boxShadow: "0 0 0 3px color-mix(in srgb, var(--danger) 25%, transparent)",
              }}
              title={`${siteId} · ${region} · ${site.cells.length} cell(s) · ~${subs.toLocaleString()} subscribers`}
            />
          );
        })}

        <div
          className="absolute bottom-2 left-2 rounded-sm px-2 py-1 text-xs"
          style={{ background: "var(--surface)", color: "var(--text-muted)" }}
        >
          no basemap configured · dot size = subscribers affected
        </div>
      </div>
      <figcaption className="mt-2 text-xs text-ink-muted">
        {layout.sites.length} site(s), {points.length} cell(s). Set{" "}
        <code className="font-mono">NEXT_PUBLIC_MAP_TILE_URL</code> for a basemap.
      </figcaption>
    </figure>
  );
}

function TiledMap({ points, tileUrl }: Props) {
  // Deliberately simple: a static raster style, so there is no style-JSON dependency on an
  // external service beyond the tiles themselves.
  const centre = points.length
    ? {
        lat: points.reduce((sum, p) => sum + p.lat, 0) / points.length,
        lon: points.reduce((sum, p) => sum + p.lon, 0) / points.length,
      }
    : { lat: -1.28, lon: 36.82 };

  return (
    <div
      className="flex w-full items-center justify-center rounded border border-line text-sm text-ink-muted"
      style={{ aspectRatio: "16 / 9", background: "var(--surface-sunken)" }}
    >
      Basemap configured ({new URL(tileUrl).host}) — centred on {centre.lat.toFixed(2)},{" "}
      {centre.lon.toFixed(2)} with {points.length} affected cell(s).
    </div>
  );
}
