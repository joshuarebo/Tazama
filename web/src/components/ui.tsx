/**
 * The small in-house design system. Every colour comes from a token, so a brand change is
 * `tokens.css` and nothing else.
 */
"use client";

import type { ReactNode } from "react";

export function Card({
  title,
  action,
  children,
  className = "",
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card p-4 ${className}`}>
      {(title || action) && (
        <header className="mb-3 flex items-baseline justify-between gap-3">
          {typeof title === "string" ? (
            <h2 className="text-sm font-semibold uppercase tracking-wide text-ink-muted">
              {title}
            </h2>
          ) : (
            title
          )}
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  hint?: string;
  tone?: "default" | "danger" | "warn" | "ok" | "info";
}) {
  const toneClass = {
    default: "text-ink",
    danger: "text-danger",
    warn: "text-warn",
    ok: "text-ok",
    info: "text-info",
  }[tone];
  return (
    <div className="card p-4">
      <div className="text-xs font-medium uppercase tracking-wide text-ink-muted">{label}</div>
      <div className={`stat mt-1 text-3xl font-semibold ${toneClass}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-ink-muted">{hint}</div>}
    </div>
  );
}

const PRIORITY_CLASS: Record<string, string> = {
  P1: "bg-danger-soft text-danger border-danger",
  P2: "bg-warn-soft text-warn border-warn",
  P3: "bg-info-soft text-info border-info",
  P4: "bg-surface-raised text-ink-muted border-line",
};

export function PriorityBadge({ priority }: { priority: string }) {
  const cls = PRIORITY_CLASS[priority] ?? PRIORITY_CLASS.P4;
  return (
    <span
      className={`inline-flex items-center rounded-sm border px-1.5 py-0.5 text-xs font-bold ${cls}`}
      // The colour alone must not carry the meaning (WCAG 1.4.1).
      aria-label={`priority ${priority}`}
    >
      {priority}
    </span>
  );
}

export function StatusPill({ status }: { status: string }) {
  const tone =
    status === "closed" || status === "resolved"
      ? "bg-ok-soft text-ok border-ok"
      : status === "suppressed"
        ? "bg-surface-raised text-ink-muted border-line"
        : "bg-brand-soft text-brand-strong border-brand";
  return (
    <span className={`inline-flex rounded-sm border px-1.5 py-0.5 text-xs font-medium ${tone}`}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "brand" | "danger" | "warn" | "info" | "ok";
}) {
  const cls = {
    neutral: "bg-surface-raised text-ink-muted border-line",
    brand: "bg-brand-soft text-brand-strong border-brand",
    danger: "bg-danger-soft text-danger border-danger",
    warn: "bg-warn-soft text-warn border-warn",
    info: "bg-info-soft text-info border-info",
    ok: "bg-ok-soft text-ok border-ok",
  }[tone];
  return (
    <span className={`inline-flex rounded-sm border px-1.5 py-0.5 text-xs ${cls}`}>{children}</span>
  );
}

export function Button({
  children,
  onClick,
  variant = "secondary",
  type = "button",
  disabled = false,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "danger";
  type?: "button" | "submit";
  disabled?: boolean;
}) {
  const base =
    "inline-flex items-center justify-center gap-2 rounded px-3 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50";
  const styles = {
    primary: "bg-brand text-ink-inverse hover:bg-brand-strong",
    secondary: "border border-line bg-surface text-ink hover:bg-surface-raised",
    danger: "border border-danger bg-danger-soft text-danger hover:bg-danger hover:text-ink-inverse",
  }[variant];
  return (
    <button type={type} onClick={onClick} disabled={disabled} className={`${base} ${styles}`}>
      {children}
    </button>
  );
}

/** Loading, empty and error states exist for every panel — PRD section 10. */
export function Loading({ what = "data" }: { what?: string }) {
  return (
    <div className="flex items-center gap-2 p-6 text-sm text-ink-muted" role="status">
      <span
        className="h-3 w-3 animate-pulse rounded-full"
        style={{ background: "var(--brand-green)" }}
      />
      Loading {what}…
    </div>
  );
}

export function Empty({ message, hint }: { message: string; hint?: string }) {
  return (
    <div className="p-6 text-center">
      <p className="text-sm font-medium text-ink">{message}</p>
      {hint && <p className="mt-1 text-xs text-ink-muted">{hint}</p>}
    </div>
  );
}

export function ErrorNote({ error, retry }: { error: unknown; retry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong.";
  const correlation =
    error && typeof error === "object" && "correlationId" in error
      ? (error as { correlationId?: string }).correlationId
      : undefined;
  return (
    <div className="rounded border border-danger bg-danger-soft p-4" role="alert">
      <p className="text-sm font-medium text-danger">{message}</p>
      {correlation && (
        <p className="mt-1 font-mono text-xs text-ink-muted">correlation id: {correlation}</p>
      )}
      {retry && (
        <div className="mt-3">
          <Button onClick={retry}>Try again</Button>
        </div>
      )}
    </div>
  );
}

export function Table({
  head,
  children,
  caption,
}: {
  head: ReactNode[];
  children: ReactNode;
  caption?: string;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr className="border-b border-line text-left">
            {head.map((cell, index) => (
              <th
                key={index}
                scope="col"
                className="whitespace-nowrap px-2 py-2 text-xs font-semibold uppercase tracking-wide text-ink-muted"
              >
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function timeIn(tz: string, iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: tz,
      dateStyle: "short",
      timeStyle: "short",
    }).format(new Date(iso));
  } catch {
    return iso;
  }
}

export function num(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("en-GB").format(value);
}
