# Phase 4 Multi-Generation Evolution

Phase 4 adds controlled descendant generation for promoted asset specialists.

It does not create a parallel evolutionary platform. Descendants remain `DiscoveryCandidate` payloads, are queued through the existing campaign/job tables, validated through the existing gates, learned through existing learning tables, and archived through the existing research archive flow.

## Parent Eligibility

A parent is eligible only when it is a promoted asset specialist with:

- dataset lineage;
- hypothesis lineage;
- candidate payload;
- at least 30 trades;
- profit factor gate pass;
- positive expectancy;
- drawdown gate pass;
- walk-forward evidence;
- paper-readiness evidence;
- no operational failure.

Near-pass candidates are not used as parents.

## Child Lineage

Each child stores:

- parent candidate ID;
- root ancestor ID;
- generation number;
- mutation channel;
- mutated executable parameter;
- old value;
- new value;
- hypothesis ID;
- dataset ID;
- strategy family;
- expected improvement;
- falsification criterion.

Metadata-only fields are not used as mutation targets.

## Diversity Controls

Phase 4 enforces:

- maximum children per parent;
- parent concentration audit;
- executable-key deduplication;
- mutation-parameter entropy;
- lineage entropy;
- family mix reporting;
- duplicate execution-key reporting.

If diversity collapses, the failed run is preserved as contradictory evidence and excluded from improvement claims.

## Independent Validation

The local database currently contains one frozen dataset, dataset `1`.

Because dataset `1` supplied the parent-selection evidence, same-dataset child results are development evidence only. Without a separate frozen validation dataset, every child remains:

```text
Promising descendant - unconfirmed
```

No child may be called an independently confirmed improvement until it passes unchanged gates on a future independent frozen dataset.

## Safety

Phase 4 is simulation-only. It does not touch VPS, broker, paper-routing, live-routing, deployment, or UI page behavior.

Phase 5 must not begin without explicit approval.
