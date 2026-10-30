/**
 * The application shell: wordmark, navigation, the DEMO DATA ribbon, the internal-use
 * footer, and the sign-in gate.
 *
 * Sign-in lives here rather than on a separate route because the console is useless
 * without a session: a NOC engineer should land on the Command Center, not on a login
 * page they have to navigate away from.
 */
"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactNode, useState, useSyncExternalStore } from "react";

import { Button, ErrorNote } from "@/components/ui";
import { Wordmark } from "@/components/Wordmark";
import {
  login,
  sessionServerSnapshot,
  sessionSnapshot,
  signOut,
  subscribeToSession,
  type Meta,
} from "@/lib/api";

const NAV = [
  { href: "/", label: "Command Center", capability: "incident:read" },
  { href: "/complaints", label: "Complaints", capability: "complaint:read" },
  { href: "/care", label: "Care lookup", capability: "subscriber:lookup" },
  { href: "/explorer", label: "Data Explorer", capability: "explorer:query" },
  { href: "/insights", label: "Insights", capability: "insights:read" },
  { href: "/reports", label: "Reports", capability: "report:read" },
];

/** The dev-mode accounts, so a reviewer can switch roles and see the RBAC working. */
const DEV_ACCOUNTS = [
  { user: "noc", label: "NOC engineer" },
  { user: "care", label: "Care agent" },
  { user: "analyst", label: "Analyst" },
  { user: "manager", label: "Manager" },
  { user: "admin", label: "Admin" },
];

export function Shell({ meta, children }: { meta: Meta | null; children: ReactNode }) {
  const pathname = usePathname();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  // The session lives in `sessionStorage`, which does not exist on the server. Reading it
  // in an effect and calling `setState` cost a cascading render on every page load;
  // `useSyncExternalStore` with an explicit server snapshot is the purpose-built answer and
  // removes the "ready" flag entirely — the server snapshot *is* "not known yet".
  const role = useSyncExternalStore(
    subscribeToSession,
    sessionSnapshot,
    sessionServerSnapshot,
  );

  async function signIn(user: string) {
    setBusy(true);
    setError(null);
    try {
      // Dev mode: the password equals the username. The settings validator refuses this
      // auth mode outside local/dev, so it cannot reach production.
      await login(user, user);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  const allowed = (capability: string) =>
    !role || !meta ? true : (meta.capabilities[capability] ?? []).includes(role);

  return (
    <div className="flex min-h-screen flex-col">
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      {meta?.demo_banner && (
        <div
          className="flex items-center justify-center gap-2 px-4 py-1 text-xs font-semibold uppercase tracking-widest"
          style={{ background: "var(--warn-soft)", color: "var(--warn)" }}
          role="note"
        >
          Demo data — every subscriber, site and complaint here is synthetic
        </div>
      )}

      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-4 px-4 py-3">
          <Link href="/" className="flex items-center gap-2">
            <Wordmark />
          </Link>

          <nav aria-label="Main" className="flex flex-1 flex-wrap gap-1">
            {NAV.filter((item) => allowed(item.capability)).map((item) => {
              const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={`rounded px-3 py-1.5 text-sm font-medium ${
                    active
                      ? "bg-brand-soft text-brand-strong"
                      : "text-ink-muted hover:bg-surface-raised hover:text-ink"
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>

          <div className="flex items-center gap-2 text-xs">
            {meta && !meta.ai_available && (
              <span
                className="rounded-sm border px-1.5 py-0.5"
                style={{
                  background: "var(--surface-raised)",
                  color: "var(--text-muted)",
                  borderColor: "var(--border)",
                }}
                title="No model endpoint is configured, so AI panels fall back to deterministic output."
              >
                AI degraded
              </span>
            )}
            {role ? (
              <>
                <span className="text-ink-muted">
                  signed in as <strong className="text-ink">{role.replace(/_/g, " ")}</strong>
                </span>
                <Button onClick={signOut}>
                  Sign out
                </Button>
              </>
            ) : null}
          </div>
        </div>
      </header>

      {/* `tabIndex={-1}` is what makes the skip link work. Without it the browser scrolls
          to `#main` but leaves focus where it was, so a keyboard user presses the skip link
          and then still tabs through the whole nav — the link appears to do nothing. */}
      <main
        id="main"
        tabIndex={-1}
        className="mx-auto w-full max-w-[1600px] flex-1 px-4 py-5 outline-none"
      >
        {role ? (
          children
        ) : (
          <div className="mx-auto max-w-lg">
            <div className="card p-6">
              <h1 className="text-lg font-semibold">Sign in</h1>
              <p className="mt-1 text-sm text-ink-muted">
                This deployment runs in dev auth mode, so pick a role to see the console as that
                person. Every endpoint checks the role server-side.
              </p>
              {error ? (
                <div className="mt-4">
                  <ErrorNote error={error} />
                </div>
              ) : null}
              <div className="mt-4 grid gap-2">
                {DEV_ACCOUNTS.map((account) => (
                  <Button
                    key={account.user}
                    variant={account.user === "noc" ? "primary" : "secondary"}
                    disabled={busy}
                    onClick={() => signIn(account.user)}
                  >
                    {account.label}
                  </Button>
                ))}
              </div>
            </div>
          </div>
        )}
      </main>

      <footer className="border-t border-line bg-surface px-4 py-3">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-2 text-xs text-ink-muted">
          <span>Internal — Confidential</span>
          <span>
            {meta
              ? `v${meta.version} · ${meta.app_mode} · times in ${meta.display_tz}`
              : "connecting…"}
          </span>
        </div>
      </footer>
    </div>
  );
}
