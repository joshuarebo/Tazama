# Brand assets

The official logo is **not** in this repository and must not be committed here without
clearance. The console renders a text wordmark (`src/components/Wordmark.tsx`) instead.

To use the real identity:

1. Drop the approved SVG here as `logo.svg` (and `logo-dark.svg` if there are two).
2. Replace the wordmark component with an `<Image>` pointing at it.
3. Replace every `--brand-*` value in `src/styles/tokens.css` with the official palette.

Until then the greens in `tokens.css` are approximations and are labelled UNVERIFIED.
