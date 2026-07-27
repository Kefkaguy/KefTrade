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
| `out_of_sample` | Does it work on bars the promoted job never traded? | Profit factor below the floor, negative expectancy, or decay past the retention floor relative to the selection window |
| `minimum_trades` | Is the unseen-window result built on enough trades? | Below the trade floor over a window that was long enough to produce them |
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

* `walk_forward_train_ratio` is pinned to `0` so the two windows execute
  identically and their profit factors are directly comparable. The split is done
  by slicing rows, not by the simulator's internal split.

  **Which way the split runs matters.** `run_backtest` does not merely score the
  walk-forward split — it refuses to open a position before it, trading only from
  `len(rows) * walk_forward_train_ratio` onward. Every promoted job's headline
  metrics therefore come from the *tail* of its dataset, which makes that tail the
  window its promotion was selected on, and the *head* the only part no candidate
  was ever ranked on. `_selection_split` reads the candidate's own ratio to find
  the boundary; `unseen_period` is the head, `selection_period` is the tail.

  The first implementation of this gate chose its own 30% holdout from the tail,
  which meant it measured the selection window and compared it against fresh data
  in the wrong direction. It passed 82 of 82 champions before the bug was found.
  A gate that only ever passes is not a gate.
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

## What counts as one strategy

`research_champion_import._cluster_key` decides whether two promoted jobs are the
same strategy. It keys on symbol, timeframe, family, direction, rule blocks and
the executable parameters (via `candidate_execution_key`, so research-provenance
parameters such as hypothesis ids do not split a cluster). It deliberately
excludes `candidate_id`, `campaign_id` and lineage.

The original key fell back to `candidate_id` whenever lineage was absent — which
is always, since `research_campaign_jobs.parent_candidate_id` is never populated
on insert and generated intraday candidates carry `parent_candidate_id=None`.
Every row was therefore its own cluster and the dedup was a no-op: the champion
queue reached 1,789 rows, roughly 1,700 of them the same AMD 30m Momentum
strategy, each one costing a full validation battery to reach an identical
verdict. Import now also seeds its dedup set from clusters that live champions
and elites already cover, so a later campaign run cannot reintroduce one.

`POST /research-champions/dedupe` collapses champions imported before that fix:
one representative per cluster kept, the rest set to `promotion_state='demoted'`
— demoted, never deleted, and never applied to an already-graduated elite.

## Running it

One call is bounded by `max_runtime_seconds` (default
`DEFAULT_RUN_BUDGET_SECONDS`) as well as by `limit`, checked between champions so
a partially measured champion is never abandoned. A large queue is drained by
calling repeatedly: the response carries `budget_exhausted` and `remaining`, and
the Elite Builder page loops on those, reporting progress as it goes. Every
champion commits its own verdict, so stopping between batches never loses work.

This is deliberately not a background job system. It is the smallest thing that
makes a long queue observable and resumable; if validation ever needs to survive
a page close, that is the point to introduce one.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /research/elite-portfolios/champion-validation/queue` | Counts by state plus the next champions in line |
| `POST /research/elite-portfolios/champion-validation/run` | Validate champions within one time budget and graduate the survivors |
| `GET /research/elite-portfolios/champion-validation/diagnostics` | Failures grouped by gate and by family/symbol/timeframe |
| `GET /research/elite-portfolios/champion-validation/runs/{id}` | One run's full evidence, gate by gate |
| `POST /research/elite-portfolios/research-champions/dedupe` | Collapse duplicate champions to one per cluster |

## When validation fails

That is the system working. The diagnostics endpoint groups failures by gate and
by family/symbol/timeframe precisely so the next research decision is a reading of
evidence rather than a guess: stop expanding the families that die on
`cross_symbol`, keep expanding the ones that only miss `minimum_trades`, and
snapshot more timeframes when `needs_more_data` dominates. The response to a wave
of failures is more research breadth, not a weaker gate.


## Reaching the portfolio solver (Phase 13.10)

Graduating a champion is not enough on its own: the solver reads
`promotion_state = 'elite'` but *also* applies `DEFAULT_THRESHOLDS`, including
`minimum_assets_passed = 2`. The import writes a placeholder `assets_passed = 1`
because at import time nothing had been measured outside the originating symbol,
and nothing used to overwrite it -- so every graduated champion was structurally
ineligible for the solver no matter how many symbols its cross-symbol gate had
actually proved it on.

`measured_breadth` fixes that at the source: on graduation the candidate's
`assets_passed`, `timeframes_passed` and `regimes_passed` are set from the gates
that actually passed (own symbol plus each alternate that cleared the
cross-symbol gate; 2 timeframes when the stability gate passed; the count of
profitable regime buckets). Applied with `GREATEST`, so a candidate carrying
richer breadth from an older promotion path is never downgraded, and counted
only from *passing* gates, so a failed or unmeasured probe contributes nothing.

No threshold was lowered to achieve this. The evidence already existed; it was
simply never written down.

## Portfolio profiles

`elite_portfolio_builder.PORTFOLIO_PROFILES` presets portfolio *shape* only:

| Profile | Size | Assets | Families | Timeframe cap |
| --- | --- | --- | --- | --- |
| Strict Diversified | 5-20 | >= 5 | >= 4 | 1/2 (exact half) |
| Small Paper Launch | 2-4 | >= 2 | >= 2 | 2/3 |
| Single Elite Test | 1 | >= 1 | >= 1 | 1/1 |

Quality thresholds, the validated-elite requirement, parameter similarity,
signal correlation, strategy-return correlation, the minimum correlation
evidence and the one-per-symbol-family rule are identical in all three.
`protected_constraint_violations` rejects any configuration that tries to widen
one, so the API returns 422 rather than quietly building a portfolio on weaker
protections.

The former 50% timeframe cap was hardcoded as `2 * count <= total` in three
places. It is now `count * denominator <= total * numerator`, which is
byte-for-byte the old behaviour at 1/2 and makes odd-sized portfolios reachable
at 2/3 -- without ever permitting a single-timeframe portfolio above size 2.

`recommend_profile` walks the profiles strictest-first and stops at the first
one that actually yields a portfolio, reporting exactly which shape constraints
differ from strict. When none works it says so and calls it an evidence problem
rather than proposing a looser gate.

## All Validated Elites Paper Lab (execution-testing mode)

A fourth mode, deliberately not a fourth profile: `preview()`/`create_run()`
build a diversity-constrained *portfolio*. `paper_lab_preview()`/
`create_paper_lab_run()` build an execution-testing *set* -- every validated
elite that can reach Alpaca Paper, correlated or duplicated or not. Sharing
code with the diversified solver would risk one day sharing its exclusion
logic too, so it is a separate pure function (`elite_portfolio_builder.
paper_lab_preview`) that happens to return the same response shape, which is
what lets it reuse the diversified path's persistence, approval, and Step 04
activation machinery unmodified.

### Eligibility

Narrower and different from the solver's `evaluate_eligibility`: nothing here
is excluded on profit factor, drawdown, or any other quality threshold --
`load_elite_candidate_variants` already filtered to `promotion_state='elite'`,
and quality was the champion validation battery's job. `paper_lab_eligibility`
checks only whether a member can actually reach a deployment at all:

| Reason | Meaning |
| --- | --- |
| `NOT_PROMOTED_ELITE` | Not `promotion_state='elite'` (defensive; the input pool already filters this) |
| `NOT_VALIDATED` | `validation_state != 'validated'` -- catches a legacy elite that reached `elite` through the older pooled-consistency gate and never actually ran the champion validation battery |
| `SHORT_DIRECTION_EXCLUDED` | Short strategies have no Alpaca external execution path |
| `INTERNAL_ONLY_EXCLUDED` | `execution_capability='internal_only'` |
| `MISSING_AUTHORITATIVE_LINEAGE` | No campaign, research job, or candidate id |
| `DUPLICATE_CANDIDATE_SYMBOL_TIMEFRAME` | Same (candidate_id, symbol, timeframe) as an already-included, higher-scoring row |

### What is never a gate here

Correlation, shared symbols, shared families, and parameter similarity are
computed (`paper_lab_advisory_conflicts`, reusing the diversified solver's own
0.90/0.75 thresholds so the labeling is not invented looser) but every
conflict is stored `hard_conflict: False, advisory_only: True`. Nothing is
excluded for it. The response and the Step 04 activation view both carry
`diversified: False` and a warning string
(`PAPER_LAB_WARNING`) that the frontend renders as a persistent banner --
`configuration["warning"]` round-trips through `elite_portfolio_runs.
source_configuration`, so it survives a page refresh along with everything
else.

### Reuse, not a fork

`_create_run_from_preview` is the one insert pipeline both `create_run` and
`create_paper_lab_run` call; `_recompute_for_run` is the one place that
dispatches an approval/staleness check to either `preview_from_database` or
`paper_lab_preview_from_database`, keyed on
`source_configuration["mode"] == PAPER_LAB_MODE`. Everything downstream --
`get_run`, `list_runs`, `activate_internal`, the whole of
`elite_portfolio_operations.py` (per-member approval, preflight, execution
enable) -- operates on `elite_portfolio_runs`/`elite_portfolio_members` rows
and has no idea which path created them.

### Bulk actions

`approve_all_members_for_alpaca_paper` and
`enable_all_ready_members_paper_execution` are loops over the exact
per-member functions a single click already used
(`approve_member_external_paper` -> `enable_observe_only`;
`enable_member_paper_execution` -> `enable_paper_execution`), not a
lower-privilege shortcut. One member's failure never blocks the rest, and a
member already approved is reported as skipped rather than as an error, so a
retried bulk call reads as progress. A member whose preflight has any
outstanding check is left completely unchanged by the bulk execution-enable
call -- it is either enabled in full or not touched, never partially.

### Account-level safety is unchanged

`default_risk_policy()`, `evaluate_portfolio_risk`, and the preflight's halt
and reconciliation checks are account-scoped, not portfolio-run-scoped, so
having thirteen members active from one paper lab run does not create
thirteen independent risk budgets -- it is still the one account's allocated
capital, risk-per-trade, exposure, and loss limits. Duplicate-deployment
arbitration already existed structurally: `_activate_member`'s existing-row
lookup is keyed on `(campaign_id, candidate_id, symbol, timeframe)` globally,
not per portfolio run, so the same elite referenced by both a diversified run
and the paper lab resolves to the same internal and external deployment
rows -- never two.

### Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /paper-lab/preview` | Read-only: every deployable validated elite plus exclusion reasons |
| `POST /paper-lab` | Save the current set as an immutable run |
| `POST /{portfolio_id}/members/approve-all-external-paper` | Bulk Alpaca Paper approval (`confirm_portfolio_run_id` required) |
| `POST /{portfolio_id}/members/enable-all-paper-execution` | Bulk execution enable for members whose preflight passes (`confirm_portfolio_run_id` required) |

Approval, internal activation, and single-member Step 04 actions reuse the
existing generic endpoints (`/{portfolio_id}/approve`,
`/{portfolio_id}/activate-internal`, `/{portfolio_id}/members/{member_id}/...`)
unchanged.
