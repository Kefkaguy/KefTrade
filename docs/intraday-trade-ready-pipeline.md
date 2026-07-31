# Trade-ready 30m pipeline

An elite cannot be guaranteed. What can be guaranteed is that nothing reaches
that label without repeatable predictive evidence, survival of realistic
costs, an untouched confirmation, and demonstrated executable fills.

```
certified data
→ predeclared hypothesis
→ powered discovery
→ chronological validation
→ locked confirmation
→ deterministic strategy family
→ executable simulation
→ paper-fill calibration
→ elite qualification
→ limited live deployment
```

Each stage is a gate that no later stage can satisfy on its own behalf, and no
earlier stage can be re-run to rescue.

## Immediate implementation order

```bash
python -m app.cli.intraday_dataset_pipeline ingest --symbols "$SYMBOLS" --start 2016-01-01 --end 2026-07-30 --feed sip
```

```bash
python -m app.cli.intraday_dataset_pipeline universe --universe-key liquid_100_v1 --symbols "$SYMBOLS" --start 2016-01-01 --end 2026-07-30 --target-size 100
```

```bash
python -m app.cli.intraday_dataset_pipeline snapshot --symbols "$SYMBOLS" --universe-key liquid_100_v1 --feed sip --as-of 2026-07-30T20:00:00Z
```

```bash
python -m app.cli.intraday_dataset_pipeline quality --dataset-id <id> --universe-key liquid_100_v1
```

```bash
python -m app.cli.intraday_factor_audit certify --dataset-id <id>
```

```bash
python -m app.cli.intraday_factor_audit declare-experiment --dataset-id <id>
```

```bash
python -m app.cli.intraday_factor_audit discover --dataset-id <id> --certification-id <cid> --declaration-id <did> --cost-calibration-id latest
```

Then stop unless a real evidence survivor appears.

## Phase 1 — the dataset

**Feed.** The account has SIP access reaching back to 2016. IEX reaches 2021
and returns roughly half the bars, because IEX is one venue's share of the
tape rather than the consolidated tape. Research ingestion therefore defaults
to SIP, and the feed is recorded in the candle `source` (`alpaca_sip` /
`alpaca_iex`).

The two feeds must never share a source label, and the feed is pinned through
feature computation and snapshot materialization. Both previously read
`candles` without a source filter, so a symbol holding both histories would
have computed every feature over duplicated timestamps and doubled every
snapshot row. The feed is part of the dataset content hash: the same symbols
over the same window on a different feed are a different dataset.

**Ingestion.** `intraday_candle_ingest` takes an explicit `[start, end)` range
rather than "the last N bars". It walks every page to exhaustion — there is
deliberately no result cap, because a cap makes a truncated history look
identical to a market that had no data. Work is chunked by month and
checkpointed, so an interrupted ten-year backfill resumes at the month it
stopped. Rate limits and transport failures retry with bounded exponential
backoff honouring `Retry-After`. What arrived is reconciled against the XNYS
calendar, so missing sessions are reported rather than assumed absent.

**Universe.** `intraday_universe` builds point-in-time liquid membership: each
quarterly rebalance ranks symbols on trailing median dollar volume using only
sessions strictly before its effective date, and membership intervals are
stored so a symbol that dropped out later is still a member for the sessions
it qualified for. The construction rule is frozen and hashed.

The honest limitation is the candidate pool. Index membership is licensed data
this system does not have, and ranking today's tradable names over ten years
is survivorship bias however careful the ranking is. `survivorship_audit`
therefore checks whether the pool contains any symbol that stopped trading in
the window and reports `survivorship_bias_present` when it does not. That
limitation travels with the dataset instead of being quietly absorbed.

**Quality and power.** `intraday_dataset_quality` runs before any factor is
calculated, never after — once numbers exist, the judgement about whether the
sample was adequate is no longer independent of what they said.

| check | fails when |
| --- | --- |
| `no_duplicate_rows` | two rows share a symbol/timestamp, or two feeds are present |
| `session_shapes` | fewer than 95% of sessions match the full or early-close complement |
| `price_integrity` | OHLC incoherence, non-positive prices, or >2% stale volume |
| `corporate_actions` | large overnight jumps with no recorded split or dividend |
| `feature_alignment` | orphan feature rows, or under 98% feature coverage |
| `point_in_time_membership` | any observation falls outside its symbol's membership |

The power gate requires **475 qualifying gap-down sessions and 850
observations**, above the 396/707 minimum the power report derived from the
current sample. Discovery refuses to run on a dataset that has not cleared
both quality and power.

## Phase 2 — predeclared hypotheses

A factor key is not a hypothesis. `intraday_hypotheses.Hypothesis` requires
who is forced to trade, why they cannot wait, how that urgency reaches the
data, all four timestamps, the exit horizon, the expected direction, the
universe, the cost model, the required event count, the invalidation
conditions and the success criteria. Every field is enforced, and the whole
set is hashed — so a parameter cannot be edited into agreement with a result
already seen. A changed hash is a new version and another trial.

The first bounded experiment is exactly six tests: gap-down acceptance and
gap-down absorption, each at 1, 2 and 4 bars, on one fixed threshold set.
Holding horizons are separately registered factors
(`gap_down_acceptance_continuation_2bar`, …), not knobs — each lands in the
ledger as its own trial and cannot be swapped in after a one-bar result
disappoints. A horizon that would run past the session close drops the event
rather than silently shortening, because a shortened horizon is a different
hypothesis.

## Phase 3 — discovery and validation

Splits are 50/30/20 chronological with an **embargo** between them. Without
it, a position opened near the end of discovery is still open into the first
validation session, so the two samples share an outcome and validation is no
longer out of sample. One session covers every horizon closed at the session
close.

Failure is interpreted, not just recorded:

| verdict | action | retires? |
| --- | --- | --- |
| `survivor` | proceed to locked confirmation | no |
| `interpretable_null` | retire the hypothesis | yes |
| `underpowered_null` | gather more data | **no** |
| `fails_on_cost` | retire or redesign the horizon | yes |
| `unstable` | retire or declare a new regime hypothesis | yes |
| `data_not_ready` | repair data before confirmation | no |

The distinction that carries the weight is the second versus the third. A null
from a sample that could have detected the effect is a rejection. A null from
a sample that could not is not a result at all.

## Phase 4 — locked confirmation

One execution against the exact frozen specification. The specification hash
is recomputed and must reproduce the discovery hash; a discovery run that has
already been confirmed is refused. Failing confirmation permanently retires
that factor version through `intraday_retired_factor_versions`, and
`assert_not_retired` blocks any attempt to re-run it.

## Phases 5–9 — from factor to deployment

`intraday_elite_gates` covers the rest. A confirmed factor gets exactly one
deterministic `FamilyRecipe`, frozen and hashed — entry, direction, holding
period, stops, forced session-close exit, concurrency, sizing, eligible
symbols and slots, cost calibration id. Raw factor diagnostics carry no stops,
targets or sizing on purpose: adding them before the entry edge is confirmed
turns a failed entry into a tuning exercise.

Execution semantics reject a fill at the signal's own closing price, a
decision taken after entry, the wrong side of the spread, an uncharged trade,
an undeclared overnight position, and any fill without execution evidence.
Robustness checks concentration by symbol and quarter, participation limits,
cost stress above the calibrated p90, and whether the edge survives removing
its best symbol.

Paper-fill calibration measures what execution actually charged rather than
what the quote advertised, and requires the p90 round trip to stay below the
confirmed gross edge. Elite qualification requires every gate plus a complete
evidence chain by id — certification, declaration, hypothesis, dataset and
hash, quality report, discovery run, confirmation run, cost calibration,
family, simulation, fill calibration, and the cumulative trial count. Live
safeguards pause on any anomaly rather than retuning, because retuning against
live results makes the live account another validation sample.

## Hard stops

- If the powered six-test gap experiment produces no survivor, candle-only gap
  research is retired. Further work needs genuinely new data: signed trade
  imbalance, depth, auction imbalance messages, premarket price discovery, or
  sector-relative forced-flow context.
- Statistical, cost and confirmation gates are never lowered to manufacture an
  elite.
- Confirmation data is never consumed to decide what should have been tested.
