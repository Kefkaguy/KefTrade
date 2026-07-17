# Phase 6 Automated Scientific Reporting

Phase 6 upgrades the existing `research_campaign_reports` path into a deterministic scientific reporting layer.

It does not add infrastructure, services, UI pages, broker behavior, paper routing, live routing, or validation-threshold changes.

## Report trigger

`finalize_research_campaign` now generates an authoritative scientific report after:

- campaign jobs are finalized;
- learning has run;
- architecture candidate-stage evidence has been persisted;
- hypothesis lifecycle interpretation has been appended where applicable.

For dataset-backed campaigns, the campaign archive is refreshed after the scientific report so the final report is preserved with the rest of the reproducible evidence.

Manual report regeneration through `POST /research/campaigns/{campaign_id}/reports` uses the same Phase 6 reporting path.

## Report contents

Each report includes:

- executive summary;
- what was learned;
- comparison against previous completed campaigns;
- failures and explicit failed gates;
- hypothesis lifecycle interpretation;
- Phase 5 observation contribution analysis when observation fields are available;
- strategy-family performance;
- structural similarity and transferability analysis;
- candidate-level validation failures;
- evolution outcomes;
- contradictory evidence;
- unresolved questions;
- prioritized next-campaign recommendations;
- reproducibility hash and compute-budget comparison.

## Evidence standard

Report statements reference campaign IDs, candidate/job IDs, dataset IDs and hashes, hypothesis IDs, archive keys, or candidate-stage evidence keys where available.

When evidence is missing or insufficient, the report explicitly uses:

```text
Inconclusive — insufficient evidence.
```

Post-hoc hypotheses remain unconfirmed unless they pass an independent future frozen dataset. Same-dataset development evidence is not reported as confirmed improvement.

## Backfill

Phase 6 was backfilled over the currently completed campaign history:

- completed campaigns processed: `25`
- backfill errors: `0`
- latest corrected evolution campaign: `73`
- diversity-collapse pilot preserved: `72`

No Phase 7 or additional research phase was started.
