# Stage 1 — Tier-1 order-book state and feature engine

**Scope:** causal state variables only.
**Not built:** forward-return labels, correlation with future price, Alpha Map
cells, strategy logic, P/L, parameter optimization, threshold selection,
MBP/TBBO downloads. **Stage 1 stops before prediction.**

**Feature vocabulary frozen** 2026-08-16, before any predictive outcome was
inspected.

```
feature_engine_version   tier1_mbo_feature_engine_v2
validator_version        tier1_mbo_book_validator_v1
feature_vocabulary_hash  25e685913e3a3d05248ef6f09ad44e4b0cab91276bf7bd66d2f0d650f06b82a7
snapshot_schema_hash     7e19d06b91a2faa6178a767462fe6e1c2b3ad5865c2db2055e82c02dd47185e9
feature_semantics_hash   4aaeb9cb6d6700524d7fb065036612376d482a5cdff47d555d42c8a895c62551
features                 59
```

### v1 superseded before outcome

`tier1_mbo_feature_engine_v1` was corrected by audit **before any predictive
result was inspected**, so this is a fix to a measurement that was wrong, not
tuning toward a result that was wanted. The record is kept in
`SUPERSEDED_ENGINE_VERSIONS` and in every manifest.

| | |
|---|---|
| Superseded | `tier1_mbo_feature_engine_v1` |
| Vocabulary hash | `25e685913e3a...` — **unchanged**, because no column was renamed |
| Reason | aggressive-flow double counting; per-file time-grid anchoring; missing availability timestamps |

**Three hashes, because one was not enough.** v2 changed what `T` and `F`
*mean* without renaming a single column, so a hash over feature names alone
still matches v1 — it would have called the corrected engine identical to the
one it replaced. `feature_semantics_hash` binds the engine version to the
schema so any semantic correction necessarily changes it, and
`snapshot_schema_hash` covers the new provenance columns. A test asserts all
three relationships.

## The three corrections

### 1. Aggressive flow was double counted

XNAS normalizes one displayed execution as `T` → `F` → `C`, all sharing a
sequence and a quantity. v1 added the size to buy/sell aggressor volume on
**both** the `T` and the `F`, so a 100-share execution produced **200 shares**
of signed flow. The two records describe the same trade from opposite sides.

Attribution is now strict:

| Record | Contributes to |
|---|---|
| `T` Trade | `trade_count`, `trade_volume`, buy/sell aggressor volume, `signed_trade_volume`, `aggressor_imbalance`, `unclassified_trade_volume` |
| `F` Fill | `execution_count`, `execution_volume`, `executions_without_price_move`, absorption, refill/lifecycle |

A fill's `side` is still meaningful — it names the *resting* side, the opposite
of the aggressor — but the `T` already carries the trade, so signing the `F` as
well counts it twice. Fills no longer touch aggressor volume at all.

Tested with the real `T/F/C` shape for buy-aggressor, sell-aggressor and
`side=N`: 100 shares yields 100 of signed/classified flow, **not 200**, while
still yielding 100 of execution volume. A further test asserts the invariant
the bug broke — classified volume, execution volume and trade volume agree on a
single execution.

### 2. Time grids are absolute UTC, not per-file

v1 anchored the 1s/5s grids to each file's first `F_LAST`, so two symbols were
sampled on grids offset from one another by an arbitrary sub-second amount and
were **not comparable at the same instant**. Boundaries are now absolute
multiples of the interval in UTC nanoseconds, identical for every symbol.

The rule: a snapshot at boundary `t` uses **only the last completed `F_LAST`
with `ts_event <= t`**. An event at `t + 1 ns` belongs strictly to the next
interval. An interval with no events emits the last known book state with zero
new window-flow primitives — a quiet second is an observation, not a gap.

This required capturing book state *at* each completed `F_LAST` rather than
reading it live at emission, because the emission is triggered by a later
event; reading the book then would have included events after the boundary.

Tested: two symbols with different sub-second event placement over the same
window emit **identical** grid timestamps at both 1s and 5s; an event one
nanosecond after a boundary cannot affect it; an event exactly *on* a boundary
is inside it; a four-second silence emits four rows carrying the last state
with zero flow. 50ev/200ev are unchanged and carry `grid_ts_event = None`.

### 3. Availability timestamps preserved

Four columns carried so a later stage can simulate latency without re-deriving
when a row could first have been known:

| Column | Meaning |
|---|---|
| `grid_ts_event` | absolute UTC boundary; `None` for event cadences |
| `source_ts_event` | the last completed `F_LAST` at or before the instant |
| `source_ts_recv` | that record's receive time |
| `feature_available_ts_recv` | max `ts_recv` over every record the snapshot depended on |

`feature_available_ts_recv` never precedes an input. Tested by perturbing every
future `ts_recv` by nine seconds and asserting earlier rows are byte-identical.

All three hashes are recorded in every per-symbol-day manifest and in the batch
manifest, so a vocabulary or a semantics that moved after results were seen is
visible in the data rather than a matter of recollection.
`batch_manifest.feature_vocabulary_consistent` and
`feature_semantics_consistent` both fail if the corresponding hash is not
identical across every file in an extraction.

## What this is built on

The Tier-1 gate passed 160/160 symbol-days: 562,335,305 records, 337,561,148
`F_LAST` book states, zero `F_MAYBE_BAD_BOOK`, zero fatal violations. The
feature engine drives **the same `MboBook`** that gate certified, rather than a
second reconstruction that could quietly disagree with it.

## Causality

Every snapshot is emitted at a completed `F_LAST` boundary and uses only events
at or before it. Three disciplines, each tested:

1. **Windowed counters cover `(previous snapshot of this cadence, now]`** and
   are reset on emission, so a window cannot reach forward by construction.
2. **Normalization is prior-only and session-local.** Welford mean/variance over
   *strictly prior* snapshots within the same symbol-day; the current value is
   normalized first and folded in afterwards. Withheld below 30 prior
   observations rather than computed from a handful.
3. **Nothing crosses symbol-days.** Each file starts cold.

### Signing aggressive flow

Only `T` records are signed, and they name their aggressor directly:
`side=B` is a buy aggressor, `side=A` a sell aggressor.

Fills are *not* signed — see correction 1 above. Their `side` names the resting
side, which is the opposite of the aggressor, and it was that inversion plus the
double count that v1 got wrong. Reading a fill's side as an aggressor side would
be wrong twice over: wrong sign, and already carried by the `T`.

`side=N` is never signed. Databento enumerate the cases: auctions, trades
against non-displayed orders, implied orders, off-exchange prints, and sources
that do not disseminate a side. Those count toward volume and toward
`unclassified_trade_share`, and `aggressor_imbalance` divides by **classified**
volume only — dividing by total would drag every window toward zero in
proportion to how many prints happened to be unsignable. That is the same rule
the Stage 0 trade-flow work used.

## Feature definitions

59 features in five declared groups, plus 14 context columns. Every ratio is
stored beside the primitives it came from, so any derived value can be
recomputed and audited — `cancel_add_ratio` without `cancel_count` and
`add_count` is an assertion; with them it is a measurement.

### Book state (18)

`best_bid_price`, `best_ask_price`, `spread`, `spread_bps`, `midpoint`,
`bid_size_l1`, `ask_size_l1`, `bid_order_count_l1`, `ask_order_count_l1`,
`bid_depth_5`, `ask_depth_5`, `bid_depth_10`, `ask_depth_10`,
`bid_order_count_5`, `ask_order_count_5`, `bid_levels`, `ask_levels`,
`resting_orders`.

Depth at fixed levels is cumulative size over the top *n* price levels. Order
counts exclude synthetic top-of-book entries, matching the validator.

### Pressure (8)

| Feature | Definition |
|---|---|
| `queue_imbalance` | `q_b / (q_b + q_a)` ∈ [0,1] |
| `normalized_queue_imbalance` | `(q_b − q_a) / (q_b + q_a)` ∈ [−1,1] |
| `microprice` | `(P_b·q_a + P_a·q_b) / (q_b + q_a)` |
| `microprice_minus_mid` | `microprice − midpoint` |
| `microprice_minus_mid_bps` | the same, relative to mid |
| `order_flow_imbalance` | Cont-Kukanov-Stoikov `Σ e_n` over the window |
| `order_flow_imbalance_normalized` | OFI ÷ mean touch depth over the window |
| `mean_touch_depth` | `(q_b + q_a)/2` averaged over the window's `F_LAST` states |

The OFI kernel is the same one Stage 0 measured on Alpaca's NBBO. There, 45.224 %
of it was venue rotation, because the sizes were one venue's slice of a tied
best price. Here both sides come from a single real book, so a size change is a
liquidity event. **That is the entire reason Tier 1 exists.**

### Order lifecycle (15)

`add_count`, `add_volume`, `cancel_count`, `cancel_volume`, `modify_count`,
`execution_count`, `execution_volume`, `cancel_add_ratio`,
`cancel_volume_ratio`, `touch_replenishment_volume`,
`touch_replenishment_events`, `queue_depletion_events`, `queue_persistence`,
`best_bid_changes`, `best_ask_changes`.

- **Replenishment** — an add at or inside the prevailing touch.
- **Depletion** — the touch size going from positive to zero.
- **Persistence** — the share of the window's `F_LAST` states where neither
  touch price moved.

### Aggressive flow (9)

`trade_count`, `trade_volume`, `buy_aggressor_volume`, `sell_aggressor_volume`,
`unclassified_trade_volume`, `unclassified_trade_share`, `signed_trade_volume`,
`aggressor_imbalance`, `execution_intensity`.

`execution_intensity` is executions per second of window wall-clock, `None`
when the window has no elapsed time rather than dividing by zero.

### Absorption / resilience (5)

`executions_without_price_move`, `execution_volume_without_price_move`,
`absorption_ratio`, `refill_after_execution_volume`,
`depletion_followed_by_quote_move`.

`depletion_followed_by_quote_move` is a **sequence**, not a coincidence of two
counters: a depletion arms a pending flag on that side, and the flag fires only
when the touch price subsequently moves.

### Prior-only normalized (4)

`spread_bps_z`, `normalized_queue_imbalance_z`,
`order_flow_imbalance_normalized_z`, `signed_trade_volume_z`.

`None` below 30 prior observations, and `None` when prior variance is zero.

### Context (14)

`symbol`, `session_date`, `cadence`, `sequence_index`, `ts_event`,
`grid_ts_event`, `source_ts_event`, `source_ts_recv`,
`feature_available_ts_recv`, `sequence`, `flast_index`, `window_ns`,
`window_flast_events`, `window_records`.

`ts_event` is the snapshot's nominal time: the grid boundary for time cadences,
the source event for event cadences.

`WINDOWED_FEATURES` is exported so a consumer cannot mistake a window aggregate
for an instantaneous book reading.

## Cadences

| Name | Kind | Interval |
|---|---|---|
| `1s` | time | 1 s |
| `5s` | time | 5 s |
| `50ev` | events | 50 `F_LAST` events |
| `200ev` | events | 200 `F_LAST` events |

Time grids are **anchored to the first `F_LAST` in the file**, not to a wall
clock, so a session opening at an arbitrary nanosecond is not sampled on a
fictional grid. A gap in the stream advances past missed boundaries rather than
emitting a burst of empty windows.

## Leakage tests

The load-bearing deliverable, stated two ways because a bug that survives one
framing rarely survives both.

**Truncation invariance.** Every snapshot from a full session must be identical
to the same snapshot produced from a stream cut off at its own event. If any
feature saw the future, removing the future would change it. Run over all 28
snapshots of a mixed-action fixture spanning all four cadences.

**Future perturbation.** Everything after a chosen snapshot is replaced with
perturbed prices, sizes and timestamps; the snapshot must not move. This is the
Stage-1 analogue of `perturb_future_candles`.

**A gap this caught.** The first fixture pinned the spread at exactly one tick,
so prior variance was zero and *every* `spread_bps_z` was `None` — meaning the
expanding normalizer, the single most leak-prone component, was never exercised
by the invariance check at all. The fixture now drifts its reference price and
varies the inside spread; all four normalizers populate (183 values each across
297 snapshots, 21 distinct spreads), and
`test_truncation_invariance_holds_once_z_scores_are_populated` truncates
specifically at snapshots that carry them.

Also tested: the normalizer excludes the current observation; it withholds
below the minimum history; normalization is session-local; and no feature name
contains `forward`, `future`, `return`, `label`, `target`, `pnl`, `profit`,
`alpha` or `signal`.

## Storage

Derived state goes to Parquet (zstd), one file per `(symbol-day, cadence)`,
written in row groups so a session is never fully resident. **Nothing goes into
Postgres** — not the 562 M raw events, and not a row-per-event derivative.

Measured on a 200,000-event synthetic session: **128.55 bytes/row**.

Projected from the real gate counts (337,561,148 `F_LAST` ÷ 160 = 2,109,757 per
symbol-day):

| Session span assumption | Rows/symbol-day | Total rows | Size |
|---|---|---|---|
| Full day, 04:00–20:00 ET | 121,864 | 19,498,229 | **2.33 GiB** |
| RTH only, 09:30–16:00 | 80,824 | 12,931,829 | **1.55 GiB** |

So roughly **1.5–2.4 GiB against 8.14 GiB of compressed source** — the reduction
Stage 1 exists to produce. The span is the only unknown; the event-cadence rows
are computed from measured `F_LAST` counts, not assumed.

`estimate_storage` extrapolates from measured rows and bytes rather than a
hard-coded bytes-per-row, because Parquet + zstd on these columns compresses far
better than a guessed constant would suggest in either direction.

## Provenance

Per symbol-day manifest:

- source DBN filename, byte size, **SHA-256**
- `validator_version`, `feature_engine_version`, `feature_store_version`
- `feature_vocabulary_hash` and feature count
- records consumed, `F_LAST` events, per-cadence rows/bytes/time-span
- `contains_forward_information: false`

Batch manifest adds the cross-file vocabulary-consistency check and the full
feature definitions.

## Usage

```bash
python -m app.cli.mbo_features definitions
```
```bash
python -m app.cli.mbo_features --output-dir reports/tier1_mbo_features file --path /path/to/xnas-itch-20250626.mbo.CMCSA.0000.dbn.zst
```
```bash
python -m app.cli.mbo_features --output-dir reports/tier1_mbo_features batch --directory /path/to/dbn
```

The real-file integration test is opt-in:

```bash
KEFTRADE_MBO_TEST_FILE=/path/to/xnas-itch-20250626.mbo.CMCSA.0000.dbn.zst python -m pytest tests/test_mbo_feature_engine.py -q
```

## Test results

```
59 Stage-1 tests pass (24 semantics/storage, 17 v2 corrections, 18 leakage), 1 skipped
```

The skip is the real-file integration test. Full suite: **1887 passed**, one
pre-existing unrelated failure (below).

## Limitations, stated plainly

- **No real symbol-day has been extracted.** Everything above is synthetic plus
  a measured Parquet cost. The integration test is written and unexecuted, and
  the storage projection rests on synthetic compression — real order books have
  more distinct prices and will compress somewhat worse.
- **The 1 s and 5 s row counts are span-dependent.** I do not know from here
  whether the frozen files cover the full day or only regular hours; both bounds
  are given rather than one guessed number.
- **`queue_persistence` is event-weighted, not time-weighted.** A window with
  one long quiet stretch and one busy stretch is not distinguished from an even
  one. Time-weighting is a defensible alternative; the choice is declared, not
  discovered.
- **Depth beyond 10 levels is not captured.** Fixed at 1/5/10 by declaration.
- **No cross-venue view.** XNAS only, by design — that is what made the book
  coherent in the first place.

## Pre-existing unrelated failure

`test_phase10_modules_have_no_runtime_ddl` fails on six temp-table
`CREATE INDEX` calls in `intraday_sector_leadlag_predictor.py`, introduced by
the sector-leadlag merge (commit 69015a1). Verified against a clean worktree at
HEAD before this work. Unrelated to Stage 1 and still open.

Separately, `pytest`'s default temp directory on this workstation
(`%LOCALAPPDATA%\Temp\pytest-of-erosi`) has an unreadable ACL, which makes any
`tmp_path` test error out locally. Run with `--basetemp=<writable path>`; it is
a machine-local lockout, not a project defect, and Linux is unaffected.

## Stopping here

Per instruction: state and features only. No forward returns, no labels, no
correlation with future price, no Alpha Map, no strategy, no P/L, no parameter
optimization, no threshold selection, no new downloads.
