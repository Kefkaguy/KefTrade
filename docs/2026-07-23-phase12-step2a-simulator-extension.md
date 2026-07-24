# Phase 12, Step 2A — generic simulator extension for intraday strategies

Scope actually implemented (nothing beyond this, per the approved architecture
review): `ExecutionConstraints` metadata, the single stable strategy calling
convention (`StrategyProtocol`), strategy-owned state lifecycle (reset before
every run), the structural session-close exit cap and its `session_close`
exit reason, the intraday dataset loader, and this document. **No ORB entry
rules, no campaign/discovery wiring, no elite-lifecycle change.**

## What changed

| File | Change |
|---|---|
| `apps/api/app/services/strategy.py` | Added `ExecutionConstraints` (frozen dataclass), `DEFAULT_EXECUTION_CONSTRAINTS`, `StrategyProtocol`, `get_execution_constraints()`, `reset_strategy_state()`. No existing symbol was modified. |
| `apps/api/app/services/backtester.py` | `run_backtest()` gained an optional `session_end_index` parameter and now calls `reset_strategy_state()` first and reads `get_execution_constraints()`; `count_setup_opportunities()` gained the same reset call; `find_exit_index()` gained an optional `session_end_index` parameter and the `session_close` exit reason. |
| `apps/api/app/services/labs/intraday/dataset.py` *(new)* | `build_intraday_backtest_dataset()`, `load_intraday_backtest_dataset()`, `load_intraday_features()`, `build_session_end_index()`, `minimum_entry_lookahead_minutes()`, `entry_is_within_session_cutoff()`, `IntradayDatasetError`. |
| `apps/api/tests/test_backtester_session_close.py` *(new)* | 20 tests covering every acceptance criterion below. |
| `apps/api/tests/test_intraday_dataset.py` *(new)* | 11 tests covering the loader's honesty checks and the entry-cutoff helper. |

**Untouched**: `strategy_discovery.py`, `strategy_families.py`, `family_registry.py`, `elite_portfolio_builder.py`, `research_campaigns.py`, every existing strategy function, `strategy_diagnostics.py`. No new strategy (ORB or otherwise) was written.

## One calling convention, not two

`StrategyFn` still means exactly what it always has:
`Callable[[candle, feature, recent_candles, params], StrategyDecision]`.
There is no second, state-carrying call shape. A stateful intraday strategy
is a callable **instance** that happens to also expose `execution_constraints`
and `reset()`:

```python
class StrategyProtocol(Protocol):
    execution_constraints: ExecutionConstraints
    def reset(self) -> None: ...
    def __call__(self, candle, feature, recent_candles, params) -> StrategyDecision: ...
```

The simulator never inspects *which kind* of strategy it has — it reads two
attributes via `getattr(..., default)` and calls the object exactly the same
way regardless:

```python
constraints = getattr(strategy_decide, "execution_constraints", DEFAULT_EXECUTION_CONSTRAINTS)
reset = getattr(strategy_decide, "reset", None)
if callable(reset):
    reset()
...
decision = strategy_decide(candle, feature, recent_candles, params)  # unchanged, single shape
```

Every existing swing strategy (a plain function) has neither attribute, so
both `getattr` calls silently fall through to defaults — zero behavior
change, zero new branch on strategy identity.

## Forced-flat is a strategy-declared constraint, never a campaign parameter

`ExecutionConstraints.flat_by_session_close` lives on the strategy object,
not in `params`. Campaign generation can tune `params` freely; it has no way
to reach into a strategy's `execution_constraints` and disable this. Every
future Phase 12 intraday strategy sets `flat_by_session_close = True` in its
own class definition — a fixed fact about the strategy, not a tunable knob.

## Structural session-close exit cap

`find_exit_index()` now takes an optional `session_end_index: list[int]`
(one entry per combined candle/feature row — see `build_session_end_index()`
in the new dataset module). When the strategy's constraint requires it,
the exit search window is capped at `min(time_bound, session_bound)`:

```python
time_bound = min(final_index, start_index + max_holding_bars) if max_holding_bars > 0 else final_index
session_bound = session_end_index[start_index] if session_end_index is not None else final_index
search_end = min(time_bound, session_bound)
```

Because the numpy slice used for the stop/target scan is `arrays["low"][start_index:search_end+1]`,
a touch in the *next* session's bars is structurally unreachable, not merely
avoided by convention — the array slice never contains those rows. If no
stop/target fires before `search_end`, and the session boundary is what
capped the window, the exit is `("session_close", search_end)` and the
existing (already-generic) exit-price branch handles the price/slippage/fee
computation exactly as it already did for `time_exit`/`end_of_data` — no new
pricing logic was needed, since `session_close` is just another value that
branch was already prepared to see.

When `session_end_index` is `None` (every existing swing call site,
unchanged), `session_bound` defaults to `final_index`, `search_end` reduces
to exactly today's `time_bound`-only computation, and the reason space
collapses back to the original `{stop_loss, take_profit, time_exit,
end_of_data}` — bit-for-bit identical to pre-Step-2A behavior.

## State lifecycle: reset-before-run, not reconstruct-before-run

`reset_strategy_state()` runs at the very top of `run_backtest()` and
`count_setup_opportunities()`, before the loop touches the strategy at all.
This guarantees a fresh state for every backtest run, symbol, parameter
combination, campaign job, and deterministic rerun **even if the same
strategy object instance is reused** across those calls — the guarantee
comes from "always reset before use," not from "always construct a new
object," which is simpler and doesn't require every caller (campaign job
runner, discovery evaluator, test) to know it must build a new instance per
symbol.

## Intraday dataset loader

`build_intraday_backtest_dataset()` (pure) / `load_intraday_backtest_dataset()`
(DB wrapper) join `candles` to `intraday_features` — never the swing
`features` table — and refuse to produce a dataset that looks complete but
isn't:

- Unsupported timeframe (anything but `15m`/`30m`) → `IntradayDatasetError`.
- Zero candles, zero feature rows, or a zero-overlap join → `IntradayDatasetError`.
- Candle/feature join coverage below 50% (a real backfill gap, not the
  normal ~10-15% premarket-orphan exclusion rate) → `IntradayDatasetError`.
- Any joined row missing `session_date` → `IntradayDatasetError` (should be
  structurally impossible given the `intraday_features` schema, but asserted
  rather than assumed).
- Fewer distinct sessions than `settings.intraday_minimum_distinct_sessions`
  (the Step 1 config field that was "defined now, consumed by a future
  validation step" — this is that step) → `IntradayDatasetError`.
- Opening-range coverage below 95% (real gaps, since the first in-window bar
  of every session should already have a non-null value) → `IntradayDatasetError`.

A successful load returns `rows`, `market_arrays`, `session_end_index`
(ready to pass straight into `run_backtest`), and a `coverage` report.

`entry_is_within_session_cutoff()` / `minimum_entry_lookahead_minutes()` are
generic, timeframe-aware helpers (correct for both 15m and 30m) that any
future intraday strategy's own `decide()` can call to avoid signaling too
late in a session to get next-bar-open execution plus at least one holding
bar before the structural close cap — infrastructure, not ORB's entry logic.

## Acceptance tests (all pass)

1. **Existing swing results remain bit-for-bit unchanged** — `test_frozen_long_backtest_baseline_v1` and the full 14-test `test_backtester.py` suite pass unmodified.
2. **Plain stateless strategies preserve existing behavior** — `test_plain_function_has_default_execution_constraints`, `test_reset_strategy_state_is_a_no_op_for_plain_functions`.
3. **State isolated between repeated runs** — `test_state_resets_between_repeated_runs_with_the_same_instance`.
4. **State isolated across symbols/parameter combinations** — `test_state_does_not_leak_across_symbols_or_parameter_combinations`.
5. **Normal-session forced exits** — `test_forced_exit_at_normal_session_close_when_no_stop_or_target_hit`.
6. **Early-close-style forced exits** (short session) — `test_forced_exit_on_a_short_session_mimics_early_close`.
7. **Stops/targets before close take precedence** — `test_stop_loss_before_session_close_wins`, `test_take_profit_before_session_close_wins`.
8. **Exit scans never cross session boundaries** — `test_exit_scan_never_reads_into_the_next_session`, `test_find_exit_index_caps_the_array_slice_at_the_session_boundary`.
9. **Slippage and fees apply to session_close exits** — `test_session_close_exit_applies_slippage_and_fees`.
10. **Missing session metadata fails honestly** — `test_flat_by_session_close_strategy_requires_session_end_index`, `test_session_end_index_length_mismatch_raises`, plus 7 loader-honesty tests in `test_intraday_dataset.py`.
11. **Identical inputs produce identical outputs** — `test_identical_inputs_produce_identical_outputs`.
12. **No strategy-name branching in the simulator** — `test_backtester_module_contains_no_strategy_family_identity_branching` (static source-text guard against `opening_range`, `vwap_reversion`, `gap_fill`, `session_momentum`, `strategy_family`, `strategy ==`, `family ==`).

Full regression: 428 passed (1 pre-existing, unrelated `PermissionError` in
`test_production_validation.py` from a Windows temp-directory ACL issue,
confirmed present identically on `main` before this change via `git stash`).

## Result

The simulator can now structurally guarantee flat-by-session-close for any
strategy that declares it, using one unchanged calling convention, with zero
identity branching and zero effect on existing swing behavior. ORB v1's own
entry/exit logic is the only thing left to build.
