"""Gates between a confirmed factor and a tradable elite.

A confirmed factor is an entry edge measured on frozen bars.  Everything
between that and money is a separate question: whether the edge survives being
turned into a deterministic strategy, whether it survives being executed at
prices someone will actually give you, and whether the fills observed in paper
match the fills the research assumed.  Each of those can kill a real edge, and
none of them can rescue one that was never there.

The gates here are deliberately structured so that no stage can be satisfied
by re-running an earlier one.  Elite status requires the whole chain of
evidence to exist and to reference each other by id.

The module contains no UI code and submits no orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from json import dumps
from random import Random
from statistics import fmean, median, pstdev
from typing import Any, Sequence

import psycopg
from psycopg.types.json import Jsonb

ELITE_GATES_VERSION = "intraday_elite_gates_v1"

MINIMUM_MATCHED_FILLS = 30
MAXIMUM_SINGLE_SYMBOL_PROFIT_SHARE = 0.50
MAXIMUM_SINGLE_QUARTER_PROFIT_SHARE = 0.50
MAXIMUM_PARTICIPATION_RATE = 0.01
COST_STRESS_MULTIPLIER = 1.5


def _round(value: float | None) -> float | None:
    return round(float(value), 6) if value is not None else None


# ---------------------------------------------------------------------------
# Phase 5 - deterministic family freeze
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FamilyRecipe:
    """The single deterministic translation a confirmed factor is allowed.

    Raw factor diagnostics carry no stops, targets or sizing on purpose: those
    are risk decisions, and adding them before the entry edge is confirmed
    turns a failed entry into a tuning exercise.
    """

    factor_key: str
    entry_condition: str
    direction: str
    holding_bars: int
    stop_loss: str
    forced_session_close_exit: bool
    max_concurrent_positions: int
    position_size_fraction: float
    max_gross_exposure: float
    eligible_symbols: tuple[str, ...]
    eligible_session_slots: tuple[str, ...]
    cost_calibration_id: int

    def frozen(self) -> dict[str, Any]:
        return {
            "gates_version": ELITE_GATES_VERSION,
            "factor_key": self.factor_key,
            "entry_condition": self.entry_condition,
            "direction": self.direction,
            "holding_bars": self.holding_bars,
            "stop_loss": self.stop_loss,
            "forced_session_close_exit": self.forced_session_close_exit,
            "max_concurrent_positions": self.max_concurrent_positions,
            "position_size_fraction": self.position_size_fraction,
            "max_gross_exposure": self.max_gross_exposure,
            "eligible_symbols": list(self.eligible_symbols),
            "eligible_session_slots": list(self.eligible_session_slots),
            "cost_calibration_id": self.cost_calibration_id,
        }

    def recipe_hash(self) -> str:
        return sha256(dumps(self.frozen(), sort_keys=True).encode()).hexdigest()


def freeze_family(
    conn: psycopg.Connection,
    *,
    recipe: FamilyRecipe,
    confirmation_run_id: int,
) -> dict[str, Any]:
    """One confirmed factor, one frozen family. A campaign cannot rescue it."""
    confirmation = conn.execute(
        "SELECT id, mode, results FROM intraday_factor_diagnostic_runs WHERE id = %s",
        (confirmation_run_id,),
    ).fetchone()
    if not confirmation or str(confirmation["mode"]) != "confirmation":
        raise ValueError("A frozen family requires a completed locked-confirmation run.")
    passed = list((confirmation["results"] or {}).get("passed_locked_confirmation") or [])
    if recipe.factor_key not in passed:
        raise ValueError(
            f"{recipe.factor_key} did not pass locked confirmation run "
            f"{confirmation_run_id}; a family cannot be built on it."
        )
    existing = conn.execute(
        "SELECT id FROM intraday_strategy_families WHERE recipe_hash = %s",
        (recipe.recipe_hash(),),
    ).fetchone()
    if existing:
        return {"family_id": int(existing["id"]), "created": False}
    row = conn.execute(
        """
        INSERT INTO intraday_strategy_families(
            factor_key, recipe_hash, confirmation_run_id, cost_calibration_id,
            recipe, gates_version
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            recipe.factor_key,
            recipe.recipe_hash(),
            confirmation_run_id,
            recipe.cost_calibration_id,
            Jsonb(recipe.frozen()),
            ELITE_GATES_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return {"family_id": int(row["id"]), "created": True, "recipe_hash": recipe.recipe_hash()}


# ---------------------------------------------------------------------------
# Phase 6 - executable simulation and robustness
# ---------------------------------------------------------------------------


def execution_semantics_report(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Did every simulated trade obey executable semantics?

    A fill is only legitimate if the decision preceded it, the price came from
    a bar that had not yet happened at decision time, the correct side of the
    spread was crossed, and costs were actually charged.
    """
    total = len(trades)
    decision_after_entry = 0
    entry_at_signal_close = 0
    wrong_side = 0
    uncharged = 0
    overnight = 0
    manufactured = 0
    for trade in trades:
        decision = trade.get("decision_timestamp")
        entry = trade.get("entry_timestamp")
        if decision is None or entry is None:
            manufactured += 1
            continue
        if decision > entry:
            decision_after_entry += 1
        if trade.get("entry_price") is not None and trade.get("signal_close") is not None:
            if float(trade["entry_price"]) == float(trade["signal_close"]):
                entry_at_signal_close += 1
        side = str(trade.get("side") or "").lower()
        bid, ask = trade.get("bid"), trade.get("ask")
        if bid is not None and ask is not None and trade.get("entry_price") is not None:
            price = float(trade["entry_price"])
            if side == "long" and price < float(ask):
                wrong_side += 1
            if side == "short" and price > float(bid):
                wrong_side += 1
        if not float(trade.get("cost_bps") or 0) > 0:
            uncharged += 1
        if trade.get("exit_session_date") and trade.get("entry_session_date"):
            if trade["exit_session_date"] != trade["entry_session_date"] and not trade.get(
                "overnight_declared"
            ):
                overnight += 1
        if trade.get("execution_evidence_present") is False:
            manufactured += 1

    checks = {
        "decision_precedes_entry": decision_after_entry == 0,
        "entry_is_not_the_signal_close": entry_at_signal_close == 0,
        "spread_side_respected": wrong_side == 0,
        "costs_charged_on_every_trade": uncharged == 0,
        "no_undeclared_overnight_positions": overnight == 0,
        "no_fill_without_execution_evidence": manufactured == 0,
    }
    return {
        "trades": total,
        "decision_after_entry": decision_after_entry,
        "entry_at_signal_close": entry_at_signal_close,
        "wrong_side_fills": wrong_side,
        "trades_without_costs": uncharged,
        "undeclared_overnight": overnight,
        "fills_without_evidence": manufactured,
        "checks": checks,
        "passed": total > 0 and all(checks.values()),
    }


def _profit_share(values: dict[Any, float]) -> float | None:
    total = sum(value for value in values.values() if value > 0)
    if total <= 0:
        return None
    return max((value for value in values.values() if value > 0), default=0.0) / total


def robustness_report(
    trades: Sequence[dict[str, Any]],
    *,
    cost_stress_multiplier: float = COST_STRESS_MULTIPLIER,
    bootstrap_samples: int = 1_000,
    max_drawdown_bps: float | None = None,
    seed: int = 20260731,
) -> dict[str, Any]:
    """Every robustness question the protocol asks of realized trades.

    Concentration by symbol and quarter, participation limits, cost stress,
    walk-forward stability, block and trade-order bootstraps, and drawdown
    and tail loss.  The two bootstraps are deliberately different: blocks
    preserve serial dependence, an iid resample destroys it, and an edge that
    survives only one of the two is an artefact of trade ordering.
    """
    if not trades:
        return {"trades": 0, "passed": False, "detail": "no simulated trades"}
    net = [float(trade["net_return"]) for trade in trades]
    by_symbol: dict[str, float] = {}
    by_quarter: dict[str, float] = {}
    by_session: dict[Any, float] = {}
    for trade, value in zip(trades, net):
        by_symbol[str(trade.get("symbol"))] = by_symbol.get(str(trade.get("symbol")), 0.0) + value
        session = trade.get("entry_session_date")
        quarter = (
            f"{session.year}Q{(session.month - 1) // 3 + 1}" if session is not None else "unknown"
        )
        by_quarter[quarter] = by_quarter.get(quarter, 0.0) + value
        by_session[session] = by_session.get(session, 0.0) + value

    stressed = [
        value - (float(trade.get("cost_bps") or 0) * (cost_stress_multiplier - 1)) / 10_000
        for trade, value in zip(trades, net)
    ]
    participation = [
        float(trade["participation_rate"])
        for trade in trades
        if trade.get("participation_rate") is not None
    ]
    symbol_share = _profit_share(by_symbol)
    quarter_share = _profit_share(by_quarter)
    deviation = pstdev(net) if len(net) > 1 else 0.0

    walk_forward = walk_forward_report(trades, net)
    block_lower = _bootstrap_lower_bound(
        net, samples=bootstrap_samples, block_length=5, seed=seed
    )
    # Trade-order resampling: blocks preserve any serial dependence, an iid
    # resample deliberately destroys it. An edge that survives only one of the
    # two is an artefact of how the trades happened to be ordered.
    order_lower = _bootstrap_lower_bound(
        net, samples=bootstrap_samples, block_length=1, seed=seed + 1
    )
    risk = _drawdown_and_tail(net)
    quarter_removed = _survives_quarter_removal(trades, net)

    checks = {
        "positive_mean_net_return": fmean(net) > 0,
        "survives_cost_stress": fmean(stressed) > 0,
        "no_single_symbol_majority": symbol_share is None or symbol_share <= MAXIMUM_SINGLE_SYMBOL_PROFIT_SHARE,
        "no_single_quarter_majority": quarter_share is None or quarter_share <= MAXIMUM_SINGLE_QUARTER_PROFIT_SHARE,
        "within_participation_limit": (
            not participation or max(participation) <= MAXIMUM_PARTICIPATION_RATE
        ),
        # Removing the best symbol must not remove the edge.
        "survives_best_symbol_removal": _survives_removal(trades, net, key="symbol"),
        "survives_best_regime_removal": quarter_removed,
        "walk_forward_stable": bool(walk_forward["passed"]),
        "positive_block_bootstrap_lower_bound": block_lower is not None and block_lower > 0,
        "positive_trade_order_bootstrap_lower_bound": (
            order_lower is not None and order_lower > 0
        ),
        "within_drawdown_limit": (
            max_drawdown_bps is None
            or abs(risk["max_drawdown_bps"] or 0) <= max_drawdown_bps
        ),
    }
    return {
        "trades": len(trades),
        "mean_net_return_bps": _round(fmean(net) * 10_000),
        "stressed_mean_net_return_bps": _round(fmean(stressed) * 10_000),
        "cost_stress_multiplier": cost_stress_multiplier,
        "return_dispersion_bps": _round(deviation * 10_000),
        "max_symbol_profit_share": _round(symbol_share),
        "max_quarter_profit_share": _round(quarter_share),
        "max_participation_rate": _round(max(participation)) if participation else None,
        "walk_forward": walk_forward,
        "block_bootstrap_lower_bound_bps": _round(block_lower),
        "trade_order_bootstrap_lower_bound_bps": _round(order_lower),
        "risk": risk,
        "drawdown_limit_bps": max_drawdown_bps,
        "checks": checks,
        "passed": all(checks.values()),
    }


def _survives_removal(
    trades: Sequence[dict[str, Any]],
    net: Sequence[float],
    *,
    key: str,
) -> bool:
    totals: dict[str, float] = {}
    for trade, value in zip(trades, net):
        totals[str(trade.get(key))] = totals.get(str(trade.get(key)), 0.0) + value
    if not totals:
        return False
    best = max(totals, key=lambda item: totals[item])
    remaining = [value for trade, value in zip(trades, net) if str(trade.get(key)) != best]
    return bool(remaining) and fmean(remaining) > 0


def _survives_quarter_removal(
    trades: Sequence[dict[str, Any]],
    net: Sequence[float],
) -> bool:
    """Dropping the single best quarter must not remove the edge."""
    totals: dict[str, float] = {}
    for trade, value in zip(trades, net):
        label = _quarter(trade)
        totals[label] = totals.get(label, 0.0) + value
    if len(totals) < 2:
        return False
    best = max(totals, key=lambda item: totals[item])
    remaining = [value for trade, value in zip(trades, net) if _quarter(trade) != best]
    return bool(remaining) and fmean(remaining) > 0


def _quarter(trade: dict[str, Any]) -> str:
    session = trade.get("entry_session_date")
    if session is None:
        return "unknown"
    return f"{session.year}Q{(session.month - 1) // 3 + 1}"


def walk_forward_report(
    trades: Sequence[dict[str, Any]],
    net: Sequence[float],
    *,
    folds: int = 4,
) -> dict[str, Any]:
    """Chronological folds: an edge should not live in one stretch of time."""
    ordered = sorted(
        zip(trades, net), key=lambda pair: (pair[0].get("entry_session_date") or date.min)
    )
    if len(ordered) < folds * 5:
        return {"folds": 0, "passed": False, "detail": "too few trades to split"}
    size = len(ordered) // folds
    means: list[float] = []
    for index in range(folds):
        window = ordered[index * size : (index + 1) * size if index < folds - 1 else None]
        means.append(fmean(value for _, value in window))
    positive = sum(1 for value in means if value > 0)
    return {
        "folds": folds,
        "fold_mean_return_bps": [_round(value * 10_000) for value in means],
        "positive_folds": positive,
        # Three quarters of the folds pointing the same way is the bar; an
        # edge carried by a single fold is one regime, not an edge.
        "passed": positive / folds >= 0.75,
    }


def _bootstrap_lower_bound(
    values: Sequence[float],
    *,
    samples: int,
    block_length: int,
    seed: int,
) -> float | None:
    """Moving-block bootstrap lower bound on the mean, in bps."""
    if len(values) < max(8, block_length * 2):
        return None
    rng = Random(seed)
    blocks = [
        [values[(start + offset) % len(values)] for offset in range(block_length)]
        for start in range(len(values))
    ]
    means: list[float] = []
    for _ in range(samples):
        sample: list[float] = []
        while len(sample) < len(values):
            sample.extend(blocks[rng.randrange(len(blocks))])
        means.append(fmean(sample[: len(values)]))
    means.sort()
    return means[int(len(means) * 0.05)] * 10_000


def _drawdown_and_tail(values: Sequence[float]) -> dict[str, Any]:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    ordered = sorted(values)
    tail = ordered[: max(1, len(ordered) // 20)]
    return {
        "max_drawdown_bps": _round(worst * 10_000),
        "worst_trade_bps": _round(ordered[0] * 10_000),
        "expected_shortfall_5pct_bps": _round(fmean(tail) * 10_000),
    }


# ---------------------------------------------------------------------------
# Phase 7 - paper fill calibration
# ---------------------------------------------------------------------------


def fill_calibration_report(
    fills: Sequence[dict[str, Any]],
    *,
    confirmed_gross_edge_bps: float,
    research_signals_per_session: float | None = None,
    minimum_matched_fills: int = MINIMUM_MATCHED_FILLS,
) -> dict[str, Any]:
    """Turn observed paper fills into a production cost verdict.

    Quoted spread is what the market advertised; slippage is what it charged.
    Only the second can say whether the confirmed edge survives execution.
    """
    matched = [
        row
        for row in fills
        if row.get("filled_price") is not None and row.get("midpoint_at_decision") is not None
    ]
    if not matched:
        return {
            "matched_fills": 0,
            "passed": False,
            "detail": "no matched fills; execution quality is unmeasured",
        }
    slippage = []
    for row in matched:
        midpoint = float(row["midpoint_at_decision"])
        filled = float(row["filled_price"])
        sign = 1.0 if str(row.get("side") or "long").lower() == "long" else -1.0
        slippage.append(sign * (filled - midpoint) / midpoint * 10_000)
    ordered = sorted(slippage)
    p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    spreads = [
        (float(row["ask"]) - float(row["bid"])) / float(row["midpoint_at_decision"]) * 10_000
        for row in matched
        if row.get("ask") is not None and row.get("bid") is not None
    ]
    partial = sum(1 for row in matched if row.get("partial_fill"))
    rejected = sum(1 for row in fills if str(row.get("status") or "").lower() == "rejected")
    live_frequency = (
        len({row.get("session_date") for row in fills}) and len(fills) / max(1, len({row.get("session_date") for row in fills}))
    )
    frequency_ratio = (
        live_frequency / research_signals_per_session
        if research_signals_per_session
        else None
    )
    # Round trip: the p90 charge is paid on entry and exit.
    p90_total_cost = p90 * 2
    checks = {
        "sufficient_matched_fills": len(matched) >= minimum_matched_fills,
        "p90_cost_below_confirmed_gross_edge": p90_total_cost < confirmed_gross_edge_bps,
        "no_persistent_partial_fills": partial / len(matched) <= 0.20,
        "no_persistent_rejections": (rejected / len(fills) if fills else 0) <= 0.05,
        "signal_frequency_matches_research": (
            frequency_ratio is None or 0.5 <= frequency_ratio <= 2.0
        ),
        "execution_does_not_reverse_expectancy": (
            confirmed_gross_edge_bps - p90_total_cost > 0
        ),
    }
    return {
        "matched_fills": len(matched),
        "median_slippage_bps": _round(median(slippage)),
        "p90_slippage_bps": _round(p90),
        "p90_round_trip_cost_bps": _round(p90_total_cost),
        "median_quoted_spread_bps": _round(median(spreads)) if spreads else None,
        "partial_fill_share": _round(partial / len(matched)),
        "rejection_share": _round(rejected / len(fills)) if fills else None,
        "live_signals_per_session": _round(live_frequency),
        "research_signals_per_session": research_signals_per_session,
        "signal_frequency_ratio": _round(frequency_ratio),
        "confirmed_gross_edge_bps": confirmed_gross_edge_bps,
        "net_edge_after_observed_cost_bps": _round(
            confirmed_gross_edge_bps - p90_total_cost
        ),
        "checks": checks,
        "passed": all(checks.values()),
    }


# ---------------------------------------------------------------------------
# Phase 8 - elite qualification
# ---------------------------------------------------------------------------

REQUIRED_EVIDENCE_REFERENCES = (
    "certification_id",
    "declaration_id",
    "hypothesis_id",
    "dataset_id",
    "dataset_hash",
    "quality_report_id",
    "discovery_run_id",
    "confirmation_run_id",
    "cost_calibration_id",
    "family_id",
    "simulation_run_id",
    "fill_calibration_id",
    "cumulative_trial_count",
)


def qualify_elite(
    *,
    evidence: dict[str, Any],
    discovery_passed: bool,
    confirmation_passed: bool,
    quality_report: dict[str, Any],
    execution_report: dict[str, Any],
    robustness: dict[str, Any],
    fill_calibration: dict[str, Any],
    risk_approved: bool,
) -> dict[str, Any]:
    """Every stage must hold, and every stage must be referenceable."""
    missing = [
        name for name in REQUIRED_EVIDENCE_REFERENCES if evidence.get(name) in (None, "")
    ]
    gates = {
        "certified_dataset": bool(quality_report.get("ready_for_discovery")),
        "powered_discovery": bool(quality_report.get("power_passed")),
        "chronological_validation": bool(discovery_passed),
        "locked_confirmation": bool(confirmation_passed),
        "deterministic_executable_family": bool(execution_report.get("passed")),
        "stressed_cost_profitability": bool(
            (robustness.get("checks") or {}).get("survives_cost_stress")
        ),
        "paper_fill_calibration": bool(fill_calibration.get("passed")),
        "robustness_and_stability": bool(robustness.get("passed")),
        "risk_approval": bool(risk_approved),
        "complete_evidence_chain": not missing,
    }
    return {
        "gates_version": ELITE_GATES_VERSION,
        "gates": gates,
        "missing_evidence_references": missing,
        "qualified": all(gates.values()),
        "failed_gates": [name for name, ok in gates.items() if not ok],
        "evidence": {name: evidence.get(name) for name in REQUIRED_EVIDENCE_REFERENCES},
    }


def persist_elite_qualification(
    conn: psycopg.Connection,
    *,
    factor_key: str,
    timeframe: str,
    verdict: dict[str, Any],
) -> int:
    row = conn.execute(
        """
        INSERT INTO intraday_elite_qualifications(
            factor_key, timeframe, qualified, verdict, gates_version
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            factor_key,
            timeframe,
            bool(verdict["qualified"]),
            Jsonb(verdict),
            ELITE_GATES_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return int(row["id"])


# ---------------------------------------------------------------------------
# Phase 9 - live safeguards
# ---------------------------------------------------------------------------

PAUSE_CONDITIONS = (
    "stale_market_data",
    "calendar_session_mismatch",
    "spread_above_limit",
    "slippage_above_limit",
    "signal_frequency_out_of_bounds",
    "realized_edge_below_cost",
    "drawdown_limit_breached",
    "order_rejection_anomaly",
    "fill_anomaly",
    "version_mismatch",
)


def evaluate_live_safeguards(state: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any]:
    """Which safeguard conditions currently demand a pause.

    Every trigger pauses rather than adjusts.  Retuning against live results
    turns the live account into another validation sample, which is exactly
    the reuse the whole protocol exists to prevent.
    """
    max_age = float(limits.get("max_market_data_age_seconds", 300))
    triggered: dict[str, Any] = {}

    age = state.get("market_data_age_seconds")
    if age is None or float(age) > max_age:
        triggered["stale_market_data"] = {"age_seconds": age, "limit": max_age}
    if state.get("expected_session_date") != state.get("observed_session_date"):
        triggered["calendar_session_mismatch"] = {
            "expected": str(state.get("expected_session_date")),
            "observed": str(state.get("observed_session_date")),
        }
    for key, limit_key in (
        ("observed_spread_bps", "max_spread_bps"),
        ("observed_slippage_bps", "max_slippage_bps"),
    ):
        value, limit = state.get(key), limits.get(limit_key)
        if value is not None and limit is not None and float(value) > float(limit):
            triggered[
                "spread_above_limit" if "spread" in key else "slippage_above_limit"
            ] = {"observed": value, "limit": limit}
    frequency = state.get("signals_per_session")
    expected = limits.get("expected_signals_per_session")
    if frequency is not None and expected:
        ratio = float(frequency) / float(expected)
        if not 0.5 <= ratio <= 2.0:
            triggered["signal_frequency_out_of_bounds"] = {"ratio": _round(ratio)}
    edge, cost = state.get("realized_edge_bps"), state.get("realized_cost_bps")
    if edge is not None and cost is not None and float(edge) <= float(cost):
        triggered["realized_edge_below_cost"] = {"edge_bps": edge, "cost_bps": cost}
    drawdown, limit = state.get("drawdown"), limits.get("max_drawdown")
    if drawdown is not None and limit is not None and abs(float(drawdown)) > float(limit):
        triggered["drawdown_limit_breached"] = {"drawdown": drawdown, "limit": limit}
    rejection = state.get("rejection_rate")
    if rejection is not None and float(rejection) > float(limits.get("max_rejection_rate", 0.05)):
        triggered["order_rejection_anomaly"] = {"rejection_rate": rejection}
    if state.get("fill_anomaly"):
        triggered["fill_anomaly"] = {"detail": state.get("fill_anomaly")}
    for field in ("feature_version", "model_version", "recipe_hash"):
        expected_value = limits.get(f"expected_{field}")
        if expected_value is not None and state.get(field) != expected_value:
            triggered["version_mismatch"] = {
                "field": field,
                "expected": expected_value,
                "observed": state.get(field),
            }
            break
    return {
        "gates_version": ELITE_GATES_VERSION,
        "pause_required": bool(triggered),
        "triggered": triggered,
        "conditions_checked": list(PAUSE_CONDITIONS),
    }
