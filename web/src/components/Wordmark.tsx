/**
 * A text wordmark, deliberately.
 *
 * The real logo is a protected asset and is not in this repository. This renders the
 * product name in the brand colour so the console looks finished without passing off a
 * fabricated mark as the real one. `public/brand/README.md` says where the official asset
 * goes and what to change when it arrives.
 */
export function Wordmark() {
  return (
    <span className="flex items-baseline gap-1.5">
      <span
        aria-hidden="true"
        className="inline-block h-4 w-4 rounded-sm"
        style={{ background: "var(--brand-green)" }}
      />
      <span className="text-lg font-bold tracking-tight" style={{ color: "var(--brand-green)" }}>
        TAZAMA
      </span>
      <span className="hidden text-xs text-ink-muted sm:inline">
        network care intelligence
      </span>
      <span className="sr-only">(placeholder wordmark, not the official logo)</span>
    </span>
  );
}
