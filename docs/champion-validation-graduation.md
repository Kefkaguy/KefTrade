# Champion validation and graduation (Phase 13.9)

The Elite Builder page previously ended at champion import. Importing produced
deduped research champions and then said, in effect, "a validation runner still
needs to be connected." This is that runner.

## The problem it solves

A promoted research job is a strategy that scored well on the dataset the search
used to find it. Selecting the best of thousands of such jobs selects partly for
edge and partly for luck, and the backtesting literature is unambiguous that the
second effect dominates once the search is wide enough. A champion is therefore
not evidence of an edge; it is a hypothesis. Validation is what makes it evidence.

## Pipeline

```
promoted jobs → research champions → validated elites → portfolio → paper trading
                 ^ import                ^ this document
```

`promotion_state` is unchanged and still governs the portfolio solver:
`load_elite_candidate_variants` reads `promotion_state = 'elite'` only. Validation
adds an independent `validation_state` axis and, on a full pass, flips
`promotion_state` from `research_champion` to `elite`. Nothing else promotes.

## The nine gates

Every gate is re-measured from the candidate's stored payload, not read back from
the original result.

| Gate | Question | Failure means |
| --- | --- | --- |
| `out_of_sample` | Does it work on a held-out later period? | Profit factor below the floor, negative expectancy, or decay past the retention floor relative to the in-sample half |
| `minimum_trades` | Is the holdout result built on enough trades? | Below the trade floor over a window that was long enough to produce them |
| `cross_symbol` | Is it an edge, or a fact about one ticker? | Fewer than the required number of alternate symbols reproduced it |
| `regime_robustness` | Did it work in more than one kind of market? | Profitable in fewer regimes than required |
| `cost_stress` | Does it survive paying more than assumed? | Edge disappears at the cost multiplier |
| `drawdown_stress` | What is the worst drawdown anywhere in the battery? | Worst observed drawdown exceeds the stress limit |
| `timeframe_stability` | Is it a property of the strategy or of one bar size? | Collapses on the sibling timeframe (15m ↔ 30m, 1h ↔ 4h) |
| `correlation_duplication` | Would it add anything to the existing elites? | Daily returns track an existing elite above the correlation limit |
| `parameter_similarity` | Is it a re-parameterisation of the same slot? | Above the similarity limit against an elite in the same symbol/timeframe/family |

Thresholds live in `DEFAULT_VALIDATION_THRESHOLDS`. The API accepts overrides but
`run_champion_validation` rejects any override looser than the shipped default, so
a gate can be tightened for an experiment and never quietly relaxed to force a
graduation.

## States

| State | Meaning |
| --- | --- |
| `pending_validation` | Imported, never validated |
| `validating` | A run is in progress |
| `validated` | Passed every gate; `promotion_state` is now `elite` |
| `failed_validation` | At least one gate failed on real evidence |
| `needs_more_data` | At least one gate could not be measured, and no gate failed |

The distinction between the last two is the point of the design. A gate that could
not be evaluated never counts as a pass, so a missing sibling-timeframe dataset
parks a champion in `needs_more_data` rather than graduating it on eight of nine
gates. An execution error (a missing snapshot, a loader raising) also lands in
`needs_more_data`, never `failed_validation`: a broken loader is not evidence that
a strategy is bad.

## How measurements are taken

`measure_champion` re-runs the stored candidate through the same
`strategy_discovery.evaluate_candidate` path the campaign workers use, with three
deliberate differences, all recorded in the stored measurement:

* `walk_forward_train_ratio` is pinned to `0` so the in-sample and out-of-sample
  slices execute identically and their profit factors are directly comparable. The
  holdout split is done by slicing rows, not by the simulator's internal split.
* `frequency_screen_min_opportunities` is disabled, because it is calibrated for a
  full dataset and would short-circuit a deliberately shortened window before any
  trade is simulated. The trade floor is enforced directly on the measured result
  by `minimum_trades` instead.
* Costs are multiplied for the stress run only. A candidate backtested with zero
  fees and zero slippage produces an *unavailable* cost-stress measurement rather
  than a re-run of the identical backtest reported as "survived".

The frozen dataset snapshot is preferred everywhere. Live tables are a fallback for
the robustness probes only (alternate symbols, sibling timeframe), and every
measurement stamps `data_source` so a snapshot result and a live result are never
confused. `require_frozen_datasets: true` disables the fallback entirely.

## Schema

`database/migrations/054_champion_validation.sql`, additive and re-appliable:

* `elite_research_candidates`: `validation_state`, `validation_state_reason`,
  `validation_protocol_version`, `last_validation_run_id`, `validation_started_at`,
  `validated_at`
* `elite_champion_validation_runs`: one immutable row per attempt, including the
  thresholds used, every measurement, and an evidence hash
* `elite_champion_validation_gates`: one row per gate per run, so failures can be
  grouped by gate, family, symbol and timeframe without unpacking JSONB

Existing elite rows start at `pending_validation`. That is the honest value — this
battery never ran on them — and it deliberately does not demote them.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /research/elite-portfolios/champion-validation/queue` | Counts by state plus the next champions in line |
| `POST /research/elite-portfolios/champion-validation/run` | Validate the next N champions (default 5) and graduate the survivors |
| `GET /research/elite-portfolios/champion-validation/diagnostics` | Failures grouped by gate and by family/symbol/timeframe |
| `GET /research/elite-portfolios/champion-validation/runs/{id}` | One run's full evidence, gate by gate |

## When validation fails

That is the system working. The diagnostics endpoint groups failures by gate and
by family/symbol/timeframe precisely so the next research decision is a reading of
evidence rather than a guess: stop expanding the families that die on
`cross_symbol`, keep expanding the ones that only miss `minimum_trades`, and
snapshot more timeframes when `needs_more_data` dominates. The response to a wave
of failures is more research breadth, not a weaker gate.
