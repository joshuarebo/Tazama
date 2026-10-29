import type { Config } from "tailwindcss";

/**
 * Tailwind reads the design tokens rather than defining colours of its own, so a brand
 * update is a change to `tokens.css` alone. Any `bg-surface` / `text-muted` class in the
 * app therefore resolves to a token, and nothing hard-codes a hex value.
 */
const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: "var(--brand-green)",
          strong: "var(--brand-green-strong)",
          soft: "var(--brand-green-soft)",
        },
        surface: {
          DEFAULT: "var(--surface)",
          raised: "var(--surface-raised)",
          sunken: "var(--surface-sunken)",
        },
        line: { DEFAULT: "var(--border)", strong: "var(--border-strong)" },
        ink: { DEFAULT: "var(--text)", muted: "var(--text-muted)", inverse: "var(--text-inverse)" },
        danger: { DEFAULT: "var(--danger)", soft: "var(--danger-soft)" },
        warn: { DEFAULT: "var(--warn)", soft: "var(--warn-soft)" },
        info: { DEFAULT: "var(--info)", soft: "var(--info-soft)" },
        ok: { DEFAULT: "var(--ok)", soft: "var(--ok-soft)" },
        p1: "var(--p1)",
        p2: "var(--p2)",
        p3: "var(--p3)",
        p4: "var(--p4)",
      },
      borderRadius: {
        sm: "var(--radius-sm)",
        DEFAULT: "var(--radius)",
        lg: "var(--radius-lg)",
      },
      boxShadow: {
        sm: "var(--shadow-sm)",
        DEFAULT: "var(--shadow)",
        lg: "var(--shadow-lg)",
      },
      fontFamily: {
        sans: "var(--font-sans)",
        mono: "var(--font-mono)",
      },
    },
  },
  plugins: [],
};

export default config;
