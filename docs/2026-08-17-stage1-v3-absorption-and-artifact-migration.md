# Stage 1 v3 — absorption semantics, and what must be rebuilt

Two pre-outcome corrections, triggered by the all-160 non-predictive diagnostic.
No feature/label relationship, correlation, IC, R², rank or P/L has been viewed.
Stage 2B has **not** been run.

---

## 1. What the diagnostic proved

| Measurement | Value |
|---|---|
| Feature rows | 19,484,064 |
| Rows with nonzero `modify_count` | 0 |
| Rows where `execution_count ≠ executions_without_price_move` | 0 |
| Rows where `execution_volume ≠ execution_volume_without_price_move` | 0 |
| Finite `absorption_ratio` | 8,315,861 |
| Finite `absorption_ratio` not equal to 1.0 | 0 |

An absorption ratio of exactly 1.0 across 8.3 M rows is not a market finding.
It is the signature of a comparison that cannot return anything else.

## 2. Correction 1 — absorption at the native-event boundary

`F` is book-neutral under the already-certified XNAS normalization: the
resting-book change arrives on the companion `C`/`M` record of the same native
event. v2 evaluated `price_moved` on the individual `F` record, so it compared
the midpoint against itself. Every execution came out "absorbed", and the
feature carried no information at all — it was a constant.

v3 tracks each normalized native event from immediately after the previous
`F_LAST` to the next `F_LAST`:

1. capture the coherent midpoint before the group's first record is applied;
2. accumulate every `F` execution count and volume inside the group;
3. apply every normalized record through `MboBook` as usual;
4. at `F_LAST`, capture the coherent post-event midpoint;
5. if the group contained executions, add them to `execution_count` /
   `execution_volume`, and classify them absorbed only if both midpoints exist
   and are equal.

A group whose pre- or post-midpoint is unavailable — a one-sided or empty book
at either end — is counted as executed and **never** as absorbed. Unknown is not
evidence of absorption. Those groups are counted internally so the exclusion is
not silent.

`absorption_ratio` now means *execution volume whose complete native event left
the midpoint unchanged*, which is the quantity the feature was always supposed
to express.

A side effect worth stating: because executions now settle at `F_LAST` rather
than at the `F` record, a time-grid snapshot can no longer contain a
half-processed native event. That is a second, smaller correctness gain.

### Tests

`apps/api/tests/test_mbo_feature_engine_v3.py`, 12 cases:

| Case | Test |
|---|---|
| `T → F → C`, midpoint unchanged ⇒ absorbed | `test_execution_leaving_the_midpoint_unchanged_is_absorbed` |
| `T → F → C`, midpoint moves ⇒ not absorbed | `test_execution_that_moves_the_midpoint_is_not_absorbed` |
| The v2 bug cannot reproduce | `test_the_v2_bug_is_actually_gone` |
| Several records share one native event | `test_multiple_records_share_one_native_event` |
| Adjacent groups in one window do not leak | `test_no_leakage_across_adjacent_flast_groups` |
| Groups split by a grid flush stay independent | `test_groups_separated_by_a_grid_flush_are_judged_independently` |
| A group with no executions settles cleanly | `test_a_group_with_no_executions_settles_without_counting_anything` |
| One-sided **final** book ⇒ not absorbed | `test_a_one_sided_final_book_is_not_counted_as_absorbed` |
| One-sided **opening** book ⇒ not absorbed | `test_a_one_sided_opening_book_is_not_counted_as_absorbed` |
| Provenance and vocabulary | 3 further tests |

## 3. Correction 2 — a dormant sensor must contribute zero, not annihilate

Stage-2 expanding standardization withheld forever when the prior standard
deviation was exactly zero. `modify_count` is zero on every one of 19,484,064
rows, so under that rule it would have withheld **every row of the entire
dataset**. One dormant-but-valid sensor would have destroyed the design.

Frozen before outcomes:

| Prior state | Standardized value |
|---|---|
| fewer than 30 prior finite observations | withheld |
| ≥30 priors, prior SD > 0 | ordinary prior-only z-score |
| ≥30 priors, prior SD = 0, current **equals** prior mean | exactly `0.0` |
| ≥30 priors, prior SD = 0, current **differs** from prior mean | withheld — no finite prior scale exists yet |

The current observation enters the history only after its own value is scored.
A sensor reading zero is information that nothing happened; it is not missing
data.

`modify_count` stays in the frozen 59-feature vocabulary. It was not removed and
no feature was selected or dropped on the strength of this diagnostic. It simply
contributes zero while dormant.

## 4. New hashes

| Artefact | Before | After |
|---|---|---|
| `FEATURE_ENGINE_VERSION` | `tier1_mbo_feature_engine_v2` | **`tier1_mbo_feature_engine_v3`** |
| `FEATURE_SEMANTICS_HASH` | `4aaeb9cb…c62551` | **`7f613b06e8ba25bc45947c1ea6d3558e4508f73e37d6ef09736ba91d2d3933eb`** |
| `FEATURE_VOCABULARY_HASH` | `25e68591…6b82a7` | *unchanged* — no column was renamed |
| `SNAPSHOT_SCHEMA_HASH` | `7e19d06b…7185e9` | *unchanged* — no column was added |
| `LABEL_LOGIC_HASH` | *(new)* | `36cb54fd69b580bfdb521e940d85344cf0fc06fcf89bccea1fa2cc863fcfa7b4` |
| `LABEL_DEFINITION_HASH` | `2e8ada7e…677ac` | **`75239cc325d7aaa12caf2a24dd4c6f378788fb2e360ff76281731204410e9d73`** |
| `LABEL_SCHEMA_HASH` | `f0d55b8d…933354` | *unchanged* |
| `PLAN_DESIGN_HASH` | *(new)* | `44ac79d1c8fcb6ba452fed9820788c00033ed90fd71ce1996ceec9e3a2443b93` |
| `PLAN_HASH` | `ba51ccba…31ca6e` | **`e575428229bc5324fe74ca1593213a7acc39c879bf46eaac77bb1921d8430a25`** |

That the vocabulary and schema hashes did **not** move is the point of having
three separate feature hashes: this correction renamed nothing, so a name hash
alone would have declared v2 and v3 identical.

### The plan hash moved, and the plan did not

`PLAN_HASH` binds the design to the artefacts it is declared over, so a Stage-1
semantic correction necessarily moves it. To keep that distinguishable from an
actual design change, the design is now hashed separately as
`PLAN_DESIGN_HASH`, and the proof is executable:

> recomputing `PLAN_HASH` with the superseded v2 feature-semantics and
> label-definition hashes reproduces `ba51ccba12caf6969bbb0da84ff4cffa956361c56d5ea7bf77b453893331ca6e`
> **exactly**.

Asserted in `test_the_design_survived_the_v3_rebinding_untouched`. Not one
design element moved: 14 cells, the 10/6/4 split, the raw `return_bps` target,
ridge, the five alphas, `delta_R2`, nested CSCV, the 0.50 PBO ceiling, the BH
family of 14 and the 508 prior effective trials are all as frozen.
`assert_frozen_plan` now refuses on a design-hash change with a distinct message
from a rebinding.

## 5. Migration — what must be rebuilt, what can be certified for reuse

### Must be rebuilt: the Stage-1 feature Parquet (all 160 symbol-days)

Feature *values* changed. `execution_count`, `execution_volume`,
`executions_without_price_move`, `execution_volume_without_price_move` and
`absorption_ratio` all differ, and the first two also change attribution timing.
Existing files claim `tier1_mbo_feature_engine_v2` provenance and must not be
reused under a v3 claim.

```bash
cd apps/api && python -m app.cli.mbo_features extract --raw-dir <dbn-dir> --output-dir ../../reports/tier1_features
```

### Can be reused, once spine-certified: the Stage-2A labels

**No feature value enters a label.** Labels are forward midpoint returns
resolved from the raw certified MBO stream against the *snapshot spine* —
`symbol`, `session_date`, `cadence`, `sequence_index`, `source_ts_event`,
`source_midpoint`. The v3 correction changes none of those: it does not change
when a snapshot is emitted, how many are emitted, or their timestamps and
midpoints. `LABEL_LOGIC_HASH` is unchanged, which states the label definition
itself did not move.

Reuse is nevertheless **not taken on trust**. `mbo_stage2 grams` now certifies
the spine file by file: for every symbol-day and cadence it compares the labels'
`source_ts_event` and `source_midpoint` against the regenerated feature
snapshots and **refuses on any mismatch**, naming the file. Labels carrying the
superseded `2e8ada7e…` definition hash are admitted only through that path, and
only because the supersession record declares `label_content_changed: "false"`.

So: no 160-file raw MBO re-replay for labels — but only if every spine row
matches. If any file fails, that symbol-day's labels must be rebuilt:

```bash
cd apps/api && python -m app.cli.mbo_labels build --features-dir ../../reports/tier1_features --raw-dir <dbn-dir>
```

### Unaffected

- The MBO book validator and the 160-file certification: v3 changes feature
  derivation, not book reconstruction. No re-validation needed.
- The raw `.dbn.zst` files.
- Stage-2 statistics, gates, and the multiplicity ledger.

### Order of operations

1. Rebuild features (160 symbol-days) → new manifest claims v3 / `7f613b06…`.
2. Run `mbo_stage2 grams`. It verifies the feature semantics hash, admits the
   existing labels under the recorded supersession, and certifies every spine
   row. Any mismatch is a hard refusal.
3. Only if step 2 reports `spine_verified_every_file: true` are the labels
   reused. Otherwise rebuild labels and repeat.
4. `mbo_stage2 run` — **not yet authorized.**

## 6. Governance

- No predictive outcome has been computed or inspected. This is an
  implementation correction, not a new alpha trial, and the multiplicity ledger
  does not advance.
- v2 is preserved in `SUPERSEDED_ENGINE_VERSIONS` with the exact reason and the
  diagnostic counts that produced it. The superseded plan hash and label
  definition hash are preserved likewise, each `superseded_before_outcome:
  "true"`.
- No feature was added, removed, renamed or selected. The vocabulary is still
  the frozen 59.
- **347 tests pass**, 3 skipped, across the MBO suite.

---

# Addendum — v4: `queue_persistence` at the coherent boundary

A second instance of the same mistake, found and corrected **before** the
all-160 rebuild. No dataset was ever extracted under v3, so nothing needs
migrating from it; the rebuild goes straight from v2 artefacts to v4.

## The defect

`queue_persistence` is declared as *the share of the window's `F_LAST` states
where neither touch price moved*. It was computed as:

```python
touch_unchanged = (
    before.bid_price == after.bid_price and before.ask_price == after.ask_price
)
```

`before` and `after` bracket **only the final normalized record** of the native
event — the one that happened to carry the `F_LAST` flag. Two distinct errors
follow.

**1. Transient state instead of coherent state.** Inside a multi-record native
event the touch can move on an early record and be left alone by the last one.
The event then reads as persistent although the touch plainly moved from one
completed state to the next. The converse also held: a touch that moved and was
rebuilt at the same price within one event counted as two changes.

**2. Absent touches compared equal.** `bid_price` is `None` when a side is
empty, and `None == None` is `True`. A one-sided or empty book at both ends
therefore counted as *evidence of persistence*, which is exactly backwards — it
is absence of evidence.

## The correction

`_touch_persisted(previous_completed, current)`:

- previous bid/ask = the prior completed `F_LAST` state (`self._completed`,
  which is still the previous one at the point of comparison and is replaced
  immediately after);
- current bid/ask = the book after the current `F_LAST`;
- unchanged only if **both** touch prices are present at both ends and equal;
- a missing previous state (the session's first `F_LAST`) or a one-sided book at
  either end counts as changed, never as persistent.

## Targeted audit of coherent/`F_LAST`-declared features

Scope: every feature whose *documented* semantics explicitly reference completed
`F_LAST` states or coherent transitions.

| Feature | Declared over | Computed from | Verdict |
|---|---|---|---|
| `queue_persistence` | `F_LAST` → `F_LAST` transition | final record's `before`/`after` | **defective — corrected in v4** |
| `mean_touch_depth` | average over the window's `F_LAST` states | post-`F_LAST` touch, once per completed event | correct, no change |
| `executions_without_price_move`, `execution_volume_without_price_move`, `absorption_ratio` | complete native event | pre-group vs post-`F_LAST` midpoint | correct as of v3 |
| all snapshot book-state columns (`best_bid_price`, `spread`, `midpoint`, `queue_imbalance`, `microprice`, depths, levels, `resting_orders`) | last completed `F_LAST` at or before the instant | `CompletedState` captured at `F_LAST` | correct, no change |

`mean_touch_depth` was the only other `F_LAST`-declared feature, and it is a
*level* rather than a transition, sampled once per completed event from the
post-`F_LAST` book. It needed no correction, and
`test_mean_touch_depth_samples_the_coherent_state_once_per_flast` now pins that
so a transient interior size cannot enter the average.

### Flagged, deliberately not changed

These are computed per normalized record. Their documented semantics do **not**
reference `F_LAST` or coherent states, so changing them would be an unmandated
semantic change rather than a correction — but they carry the same structural
risk and are recorded here for a decision:

| Feature | Risk if judged coherently instead |
|---|---|
| `best_bid_changes`, `best_ask_changes` | a touch that moves and reverts inside one native event counts 2 changes; the coherent view counts 0 |
| `queue_depletion_events` | a transient interior zero (cancel then re-add at the same price in one event) counts as a depletion the coherent view never saw |
| `depletion_followed_by_quote_move` | inherits the above, since a depletion arms its pending flag |
| `order_flow_imbalance` (CKS `Σ e_n`) | the kernel is conditional on price direction and so is **not** additive across intra-event transients; a per-record sum can differ from one `e_n` across the coherent group |
| `touch_replenishment_*`, `refill_after_execution_volume` | "an add at or inside the prevailing touch" is per-record by definition; lowest risk of the set |

I have not touched these. If you want any of them moved to coherent-state
semantics, that is a further declared correction with its own version bump.

## Final hashes

| Artefact | Value |
|---|---|
| `FEATURE_ENGINE_VERSION` | `tier1_mbo_feature_engine_v4` |
| `FEATURE_SEMANTICS_HASH` | `fbe8add54376592e4c1a7196124086f6c5a69bf3bd0748dc1f08fa7db0d7563c` |
| `FEATURE_VOCABULARY_HASH` | `25e685913e3a3d05248ef6f09ad44e4b0cab91276bf7bd66d2f0d650f06b82a7` *(unchanged)* |
| `SNAPSHOT_SCHEMA_HASH` | `7e19d06b91a2faa6178a767462fe6e1c2b3ad5865c2db2055e82c02dd47185e9` *(unchanged)* |
| `LABEL_LOGIC_HASH` | `36cb54fd69b580bfdb521e940d85344cf0fc06fcf89bccea1fa2cc863fcfa7b4` *(unchanged)* |
| `LABEL_DEFINITION_HASH` | `ba4f1a38562a13603f9766aec828f8c4505ede505b30443db57e207b51fb510b` |
| `LABEL_SCHEMA_HASH` | `f0d55b8db8755e9638155170196c2dadd2e02c19856d8a7edfe47f9b5b933354` *(unchanged)* |
| `PLAN_DESIGN_HASH` | `44ac79d1c8fcb6ba452fed9820788c00033ed90fd71ce1996ceec9e3a2443b93` *(unchanged)* |
| `PLAN_HASH` | `19671245f7a83defff54902118afd2491cfcfdbf5a8e7dc6e648d9c4785e9ca3` |

`PLAN_DESIGN_HASH` is unchanged across **both** rebindings, and every superseded
plan hash is reproducible from the same design elements with only the bindings
swapped — asserted in `test_the_design_survived_every_rebinding_untouched`. The
Stage-2 statistical design is untouched: 14 cells, 10/6/4, raw `return_bps`,
ridge, five alphas, `delta_R2`, nested CSCV, 0.50 ceiling, BH family 14, 508
prior effective trials.

## Migration, restated

Unchanged from the v3 plan above, with one simplification: because no artefact
was ever produced under v3, the rebuild is a single hop from the existing v2
feature files to v4. Labels remain reusable under spine certification — the
`grams` command now recognises both superseded label-definition hashes and still
refuses on any spine mismatch, row by row.

**No predictive outcome has been computed or inspected. Stage 2B has not been
run, and the feature rebuild has not been started.**
