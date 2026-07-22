# Changelog

## 1.0.0 — 2026-07-22

Initial release.

### Features

- **Interactive zone map** — D3 choropleth of all US 3-digit ZIP prefixes
  (Albers USA projection with Alaska/Hawaii insets plus a Puerto Rico inset),
  colored by UPS zone on a single blue ramp: light = close to your origin,
  dark = far. Extended zones (AK/HI/PR) in orange. Hover tooltips,
  click-to-pin, scroll zoom/pan with reset.
- **Six UPS services** — Ground, 3 Day Select, 2nd Day Air, 2nd Day Air A.M.,
  Next Day Air Saver, Next Day Air — with typical business-day transit
  estimates per zone in the legend.
- **Waybill panel** — a live shipping-label-style card showing every
  service's zone and transit estimate for the hovered/pinned/searched
  destination. 5-digit Hawaii/Alaska ZIPs resolve their exact extended zones
  from the chart's exception tables.
- **Any origin** — switch the origin to any US ZIP; the whole map re-renders
  from that origin's official UPS chart.
- **Admin dashboard** (`/admin`, password-protected) — download or refresh
  the complete UPS chart dataset (~894 origin charts) from inside the
  container, with a live progress bar and per-prefix status grid. Resumable
  and cancellable; new charts go live without a restart.
- **Accessible & responsive** — full data table view, light/dark themes,
  keyboard focus states, mobile layout.
- Ships with one seed chart (origin 439) so a fresh install renders
  immediately; the full dataset is one admin refresh away.

### Notes

- UPS prefixes 008 and 969 (Guam) have no published charts; territories
  outside PR follow UPS's worldwide tables.
- Puerto Rico origin charts (006/007/009) are published by UPS with partial
  destination coverage; unlisted destinations render as "no data".
- amd64 image only in this release.
