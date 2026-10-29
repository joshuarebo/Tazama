/**
 * Complaints explorer.
 *
 * The filter that matters most is "unlocated": complaints with no cell are the ones the
 * site-identification join failed on, and they are invisible to correlation. Surfacing
 * that count on the same screen as the list keeps the product's hardest problem in view
 * instead of buried in a report.
 */
"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  Loading,
  Table,
  num,
  timeIn,
} from "@/components/ui";
import { getComplaints, getMeta } from "@/lib/api";

const CHANNELS = ["", "call_centre", "ussd", "sms", "app", "social", "retail_shop"];
const CATEGORIES = [
  "",
  "no_service",
  "slow_data",
  "call_drops",
  "voice_quality",
  "network_congestion",
  "billing",
  "bundle_confusion",
  "sim_swap",
  "other",
];
const PAGE = 50;

export default function ComplaintsPage() {
  return (
    <Suspense fallback={<Loading what="complaints" />}>
      <Complaints />
    </Suspense>
  );
}

function Complaints() {
  const search = useSearchParams();
  const incidentId = search.get("incident_id") ?? "";

  const [channel, setChannel] = useState("");
  const [category, setCategory] = useState("");
  const [region, setRegion] = useState("");
  const [unlocatedOnly, setUnlocatedOnly] = useState(false);
  const [offset, setOffset] = useState(0);

  const meta = useQuery({ queryKey: ["meta"], queryFn: getMeta });
  const tz = meta.data?.display_tz ?? "Africa/Nairobi";

  const params = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
  if (incidentId) params.set("incident_id", incidentId);
  if (channel) params.set("channel", channel);
  if (category) params.set("category", category);
  if (region) params.set("region", region);
  if (unlocatedOnly) params.set("unresolved_only", "true");
  const query = params.toString();

  const complaints = useQuery({
    queryKey: ["complaints", query],
    queryFn: () => getComplaints(query),
  });

  // Measured, not assumed: the same filter set with `unresolved_only` gives the unlocated
  // share directly from the API rather than from the current page of rows.
  const unlocated = useQuery({
    queryKey: ["complaints-unlocated", channel, category, region, incidentId],
    queryFn: () => {
      const probe = new URLSearchParams({ limit: "1", unresolved_only: "true" });
      if (incidentId) probe.set("incident_id", incidentId);
      if (channel) probe.set("channel", channel);
      if (category) probe.set("category", category);
      if (region) probe.set("region", region);
      return getComplaints(probe.toString());
    },
  });

  const total = complaints.data?.total ?? 0;

  return (
    <div className="grid gap-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Complaints</h1>
          <p className="text-sm text-ink-muted">
            {incidentId ? (
              <>
                Filtered to incident <span className="font-mono">{incidentId}</span>
              </>
            ) : (
              "Every complaint and care contact, as received."
            )}
          </p>
        </div>
        {unlocated.data && total > 0 && (
          <div className="text-right text-xs text-ink-muted">
            <div>
              unlocated:{" "}
              <strong className="text-warn">
                {num(unlocated.data.total)} of {num(total)}
              </strong>
            </div>
            <div>no serving cell resolved — invisible to correlation</div>
          </div>
        )}
      </header>

      <Card title="Filters">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Channel">
            <Select
              value={channel}
              onChange={(value) => {
                setChannel(value);
                setOffset(0);
              }}
              options={CHANNELS}
            />
          </Field>
          <Field label="Category">
            <Select
              value={category}
              onChange={(value) => {
                setCategory(value);
                setOffset(0);
              }}
              options={CATEGORIES}
            />
          </Field>
          <Field label="Region">
            <input
              className="rounded border border-line bg-surface px-2 py-1.5 text-sm"
              value={region}
              placeholder="e.g. Nairobi"
              onChange={(event) => {
                setRegion(event.target.value);
                setOffset(0);
              }}
            />
          </Field>
          <label className="flex items-center gap-2 pb-1.5 text-sm">
            <input
              type="checkbox"
              checked={unlocatedOnly}
              onChange={(event) => {
                setUnlocatedOnly(event.target.checked);
                setOffset(0);
              }}
            />
            unlocated only
          </label>
          {incidentId && (
            <Link href="/complaints" className="pb-2 text-sm text-brand-strong hover:underline">
              clear incident filter
            </Link>
          )}
        </div>
      </Card>

      <Card
        title="Results"
        action={
          <span className="text-xs text-ink-muted">
            {num(total)} match · showing {offset + 1}–{Math.min(offset + PAGE, total)}
          </span>
        }
      >
        {complaints.isLoading ? (
          <Loading what="complaints" />
        ) : complaints.error ? (
          <ErrorNote error={complaints.error} retry={() => complaints.refetch()} />
        ) : !complaints.data?.items.length ? (
          <Empty message="No complaints match" hint="Loosen a filter." />
        ) : (
          <>
            <Table
              head={[
                "Received",
                "Channel",
                "Category",
                "Lang",
                "Region",
                "Cell",
                "Conf.",
                "Incident",
                "Text",
              ]}
              caption="Complaints"
            >
              {complaints.data.items.map((row) => (
                <tr key={row.id} className="border-b border-line align-top last:border-0">
                  <td className="whitespace-nowrap px-2 py-1.5 text-ink-muted">
                    {timeIn(tz, row.received_at)}
                  </td>
                  <td className="px-2 py-1.5">{row.channel.replace(/_/g, " ")}</td>
                  <td className="px-2 py-1.5">
                    {row.category ? row.category.replace(/_/g, " ") : <Badge tone="warn">unclassified</Badge>}
                  </td>
                  <td className="px-2 py-1.5">{row.language ?? "—"}</td>
                  <td className="px-2 py-1.5">{row.region ?? "—"}</td>
                  <td className="px-2 py-1.5 font-mono text-xs">
                    {row.cell_id ?? <Badge tone="warn">unlocated</Badge>}
                  </td>
                  <td className="stat px-2 py-1.5">
                    {row.site_confidence === null ? "—" : row.site_confidence.toFixed(2)}
                  </td>
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
                  <td className="max-w-[28rem] px-2 py-1.5">{row.text_preview}</td>
                </tr>
              ))}
            </Table>
            <div className="mt-3 flex items-center gap-2">
              <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                Previous
              </Button>
              <Button
                disabled={offset + PAGE >= total}
                onClick={() => setOffset(offset + PAGE)}
              >
                Next
              </Button>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="grid gap-1 text-xs font-medium uppercase tracking-wide text-ink-muted">
      {label}
      {children}
    </label>
  );
}

function Select({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (value: string) => void;
  options: string[];
}) {
  return (
    <select
      className="rounded border border-line bg-surface px-2 py-1.5 text-sm font-normal normal-case text-ink"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    >
      {options.map((option) => (
        <option key={option} value={option}>
          {option ? option.replace(/_/g, " ") : "any"}
        </option>
      ))}
    </select>
  );
}
