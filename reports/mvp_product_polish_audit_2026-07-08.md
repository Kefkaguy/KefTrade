# KefTrade MVP Product Polish Audit

Date: 2026-07-08

## Scope

This pass reviewed the web app pages, API-backed research views, drilldown coverage, visualizations, and backend API surface. It intentionally did not add live trading, paper trading, broker integration, leverage, or weaker validation thresholds.

## Product Polish Completed

- Replaced the legacy hardcoded `/symbol/BTCUSDT` page with a redirect to the live asset drilldown.
- Added live API-backed asset candle charts to asset drilldown pages.
- Added research candidate drilldown pages at `/candidates/[id]`.
- Added saved validation-run drilldown pages at `/validation/[id]`.
- Added experiment-definition drilldown pages at `/experiments/[id]`.
- Added validation-run links to the validation page.
- Added experiment-definition links to the experiments page.
- Added candidate links to the portfolio and promising-candidates pages.
- Added reusable visualizations:
  - cross-asset heatmaps
  - drawdown charts
  - expectancy/trade-style distributions
  - research score history
- Added global loading and error states for API-backed research pages.
- Relabeled visible paper-trading recommendation text to alpha-validation candidate language in the frontend while preserving underlying research results.

## Remaining MVP Issues

- The home analysis flow is useful but still opinionated around a short predefined asset list. It should eventually read default asset choices from `/symbols`.
- `getSignal()` and `generateSignal()` remain BTCUSDT/4h defaults in the frontend API helper. The app should make these symbol/timeframe aware before broader asset workflows rely on them.
- The settings/risk surface exists even though the MVP is research-only. It currently communicates locked guardrails, but v2 should decide whether to keep it as compliance context or move it out of primary navigation.
- Some charts are compact SVG summaries, not full analytical charting with axes, brushing, legends, or export. They are sufficient for MVP orientation but not institutional-grade analytics.
- Error handling is improved at the page level, but API errors are still mostly generic in client components.

## Technical Debt

- `apps/web/lib/api.ts` has become a broad API client with many unrelated types and functions. Split by domain: market data, strategy research, validation, portfolio, copilot.
- Several backend routes return raw dictionaries instead of typed response models. Pydantic response models would make frontend contracts safer.
- Research archive, validation runs, and portfolio endpoints need consistent pagination, sorting, and limit metadata.
- `build_research_portfolio()` computes expensive candidate evidence during a GET request and records lifecycle state as a side effect. A v2 architecture should separate refresh/materialization from read APIs.
- Experiment, validation, and portfolio services repeat metric formatting and finite-number normalization in multiple places.
- The frontend has several one-off table/chart compositions. More reusable research table and metric-explainability components would reduce drift.

## Performance Bottlenecks

- `/research/portfolio` can be slow because it evaluates promising candidates across assets/timeframes before rendering.
- Candidate drilldowns currently fetch the full portfolio and then filter by candidate id. A backend `/research/candidates/{candidate_id}` endpoint would be more efficient.
- Experiment drilldowns fetch portfolio, experiment metadata, and live research snapshot together. This is acceptable for MVP, but v2 should use narrower endpoints.
- Validation run detail loads the full persisted validation report. Large leaderboards may need pagination or server-side slicing.
- Research intelligence rebuilds archive summaries on demand from historical rows. Materialized summaries would improve dashboard latency.

## API Consistency Recommendations

- Introduce shared envelope metadata for list endpoints: `items`, `total`, `limit`, `offset`, `sort`.
- Add Pydantic response models for validation runs, research archive rows, portfolio candidates, and experiment definitions.
- Standardize error shape: `code`, `message`, `details`, `request_id`.
- Add filtering/sorting to `/alpha/validation-runs` and `/research/portfolio`.
- Add dedicated detail endpoints for candidates and archive evidence refs.

## UI/UX Recommendations

- Keep the current dense research-workspace design. Avoid marketing sections.
- Promote `/portfolio`, `/promising`, `/validation`, and `/experiments` as the primary research loop.
- Add consistent breadcrumbs to all drilldowns.
- Add table-level search/filter controls once datasets become large.
- Add chart legends and explicit axis labels for heatmaps and distributions.
- Add saved view presets for common research workflows: candidate review, validation review, and asset coverage review.

## v2 Roadmap

1. Materialize research portfolio snapshots asynchronously.
2. Add candidate detail and archive-evidence detail backend endpoints.
3. Add typed response models and paginated list envelopes.
4. Upgrade charts to a shared analytical visualization layer.
5. Add cross-page breadcrumbs and table filters.
6. Split frontend API client by domain.
7. Add performance telemetry around portfolio, validation, and intelligence endpoints.
8. Keep KefTrade research-only unless a separate, explicitly approved product track is created.
