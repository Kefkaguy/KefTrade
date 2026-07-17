# Phase 3 Edge Discovery Engine

Phase 3 adds an evidence-to-hypothesis layer inside the existing KefTrade research pipeline.

It does not generate more candidates by itself. It reads preserved campaign, job, candidate-stage, profile, cluster, and hypothesis evidence, then appends new rows to the existing `research_hypothesis_versions` table.

## Contract

The primary output is a standard KefTrade hypothesis version:

- `scope_type` and `scope_ref` identify the target asset, cluster, or universal lifecycle scope.
- `strategy_family` remains one of the existing generator-consumable families whenever the hypothesis is meant to drive candidate generation.
- `observation`, `hypothesis`, and `expected_behavior` are falsifiable and measurable.
- `supporting_evidence` and `contradictory_evidence` retain evidence references.
- `test_summary.post_hoc` is `true` for discoveries derived from known outcomes.
- `test_summary.confirmation_status` remains `unconfirmed`.
- `evidence_window.independent_confirmation_required` is set for market-edge hypotheses.

The existing `generate_targeted_candidates` function can consume the generated market-edge hypotheses without custom glue code.

## Controls

The engine applies these controls before creating a hypothesis:

- minimum family job count;
- minimum computed-result count;
- minimum unique executable-key count;
- family-separated analysis;
- executable-strategy deduplication;
- winner and loser comparison when both exist;
- source/target asset distinction through evidence refs and scope;
- contradictory-evidence retention;
- multiple-comparison awareness through explicit post-hoc labeling;
- no validation-threshold changes;
- no candidate-volume increase.

When evidence does not pass the controls, the finding remains:

```text
Inconclusive - insufficient evidence.
```

## Lifecycle Integrity

Historical records are immutable. Phase 3 does not edit older hypotheses whose text contains optimistic or confirmed wording.

Instead, it derives an authoritative interpretation:

```text
stored status + independent-confirmation metadata override historical wording
```

If a historical record says "Confirmed" in text but remains `testing`, the derived interpretation preserves the old text and marks the authoritative confirmation state as `unconfirmed`.

## Safety

Phase 3 is simulation-only. It does not touch VPS, broker, paper-routing, live-routing, deployment, or UI page behavior.

Phase 4 must not begin until explicitly approved.
