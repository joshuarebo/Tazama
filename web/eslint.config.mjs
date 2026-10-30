/**
 * ESLint flat config.
 *
 * Two changes arrived together: Next 16 removed `next lint`, and ESLint 9 reads flat
 * config rather than `.eslintrc`. `npm run lint` now calls the ESLint CLI directly.
 *
 * eslint-config-next 16 publishes native flat configs, so they are imported rather than
 * bridged through FlatCompat — the bridge fails outright on this version (a circular
 * `react` property while validating the old schema), and importing the real thing avoids
 * silently dropping rules on the next upgrade anyway.
 */
import coreWebVitals from "eslint-config-next/core-web-vitals";
import typescript from "eslint-config-next/typescript";

const config = [
  {
    ignores: [
      ".next/**",
      "node_modules/**",
      "next-env.d.ts",
      "playwright-report/**",
      "test-results/**",
    ],
  },
  ...coreWebVitals,
  ...typescript,
  {
    rules: {
      // The console is an operator tool: an unlabelled image is a real accessibility bug
      // here, not a style preference.
      "jsx-a11y/alt-text": "error",
    },
  },
];

export default config;
