

from __future__ import annotations

from statistics import fmean, median
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.types.json import Jsonb

DIAGNOSTICS_VERSION = "research_diagnostics_v1"

# A single symbol contributing more than this share of gross profit means the
# family is that symbol's idiosyncrasy, not a general effect.
ONE_SYMBOL_PROFIT_SHARE = 0.60
# Costs above this share of gross edge mean the signal is real but uneconomic.
COST_SHARE_DESTROYS_EDGE = 1.0
# Realized capture below this share of favorable excursion means the exit is
# giving back most of what the entry earned.
POOR_EXIT_CAPTURE_RATIO = 0.35
# Adverse excursion this much larger than favorable means price systematically
# moves against the fill -- an entry-timing problem, not a signal problem.
ADVERSE_SELECTION_RATIO = 1.5


def _safe_mean(values: Sequence[float]) -> float | None:
    return round(fmean(values), 8) if values else None


def _group_metrics(pnls: Sequence[float]) -> dict[str, Any]:
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(pnls),
        "net_pnl": round(sum(pnls), 6),
        "win_rate": round(len(wins) / len(pnls), 6) if pnls else 0.0,
        "average_win": round(fmean(wins), 6) if wins else 0.0,
        "average_loss": round(fmean(losses), 6) if losses else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > 0 else None,
    }


def _breakdown(trades: Sequence[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[Any, list[float]] = {}
    for trade in trades:
        value = trade.get(key)
        grouped.setdefault("unknown" if value is None else value, []).append(float(trade["net_pnl"]))
    return [
        {key: bucket, **_group_metrics(pnls)}
        for bucket, pnls in sorted(grouped.items(), key=lambda item: str(item[0]))
    ]


def _time_of_day_bucket(minutes_from_open: Any) -> str:
    if minutes_from_open is None:
        return "unknown"
    minutes = float(minutes_from_open)
    if minutes < 30:
        return "open_0_30m"
    if minutes < 120:
        return "morning_30m_2h"
    if minutes < 300:
        return "midday_2h_5h"
    return "close_5h_plus"


def decompose_family_performance(
    trades: Iterable[dict[str, Any]], *, architecture: str
) -> dict[str, Any]:
    """Full loss decomposition for one family from its stored trades."""
    rows = list(trades)
    if not rows:
        return {
            "architecture": architecture,
            "diagnostics_version": DIAGNOSTICS_VERSION,
            "trades": 0,
            "evaluable": False,
            "reason": "no stored trades",
        }

    gross = sum(float(row["gross_pnl"]) for row in rows)
    fees = sum(float(row["fees"]) for row in rows)
    slippage = sum(float(row["slippage_cost"]) for row in rows)
    net = sum(float(row["net_pnl"]) for row in rows)
    holding = [float(row["holding_period_hours"]) for row in rows if row.get("holding_period_hours") is not None]
    mfe = [float(row["mfe_r"]) for row in rows if row.get("mfe_r") is not None]
    mae = [float(row["mae_r"]) for row in rows if row.get("mae_r") is not None]

    for row in rows:
        row["_time_of_day"] = _time_of_day_bucket(row.get("entry_minutes_from_open"))

    by_symbol = _breakdown(rows, "symbol")
    profitable_symbols = {row["symbol"]: row["net_pnl"] for row in by_symbol if row["net_pnl"] > 0}
    total_symbol_profit = sum(profitable_symbols.values())
    largest_symbol_share = (
        max(profitable_symbols.values()) / total_symbol_profit if total_symbol_profit > 0 else None
    )

    regimes = {str(row.get("market_regime") or "unknown") for row in rows}
    volatility_regimes = {str(row.get("volatility_regime") or "unknown") for row in rows}
    regimes_are_measured = regimes != {"unknown"}
    volatility_is_measured = volatility_regimes != {"unknown"}

    # Realized capture: how much of the favorable excursion the exit actually
    # kept. A family whose winners repeatedly ran far past the exit before
    # coming back has an exit problem, not an entry problem.
    realized_r = [
        float(row["net_pnl"]) / float(row["risk_per_unit"]) / float(row["quantity"])
        for row in rows
        if row.get("risk_per_unit") and row.get("quantity") and float(row["risk_per_unit"]) > 0
    ]
    mean_mfe = _safe_mean(mfe)
    mean_mae = _safe_mean(mae)
    mean_realized = _safe_mean(realized_r)
    capture_ratio = (
        mean_realized / mean_mfe if mean_mfe and mean_mfe > 0 and mean_realized is not None else None
    )

    return {
        "architecture": architecture,
        "diagnostics_version": DIAGNOSTICS_VERSION,
        "evaluable": True,
        "trades": len(rows),
        "cost_decomposition": {
            "gross_edge_before_costs": round(gross, 6),
            "fees": round(fees, 6),
            "slippage": round(slippage, 6),
            "total_costs": round(fees + slippage, 6),
            "net_pnl": round(net, 6),
            "cost_share_of_gross_edge": (
                round((fees + slippage) / gross, 6) if gross > 0 else None
            ),
            "cost_per_trade": round((fees + slippage) / len(rows), 6),
        },
        "outcome_profile": _group_metrics([float(row["net_pnl"]) for row in rows]),
        "holding_time_hours": {
            "median": round(median(holding), 4) if holding else None,
            "mean": _safe_mean(holding),
            "shortest": round(min(holding), 4) if holding else None,
            "longest": round(max(holding), 4) if holding else None,
        },
        "excursions": {
            "mean_favorable_r": mean_mfe,
            "mean_adverse_r": mean_mae,
            "mean_realized_r": mean_realized,
            "realized_capture_of_favorable": round(capture_ratio, 6) if capture_ratio is not None else None,
            "adverse_selection_ratio": (
                round(mean_mae / mean_mfe, 6) if mean_mfe and mean_mfe > 0 and mean_mae is not None else None
            ),
        },
        "by_exit_reason": _breakdown(rows, "exit_reason"),
        "by_direction": _breakdown(rows, "direction"),
        "by_symbol": by_symbol,
        "by_time_of_day": _breakdown(rows, "_time_of_day"),
        "by_market_regime": _breakdown(rows, "market_regime") if regimes_are_measured else None,
        "by_volatility_regime": _breakdown(rows, "volatility_regime") if volatility_is_measured else None,
        "regime_note": (
            None
            if regimes_are_measured
            else "unavailable: intraday jobs pass an empty context_by_time, so every trade's regime tag "
            "reads 'unknown'. Not estimated."
        ),
        "largest_symbol_profit_share": round(largest_symbol_share, 6) if largest_symbol_share else None,
    }


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------

FAILURE_REASONS = (
    "NO_RAW_SIGNAL",
    "COST_DESTROYED_SIGNAL",
    "WRONG_EXIT_LOGIC",
    "WRONG_DIRECTION",
    "POOR_REGIME_TARGETING",
    "ONE_SYMBOL_DEPENDENCE",
    "EXCESSIVE_TURNOVER",
    "ENTRY_LATENCY_PROBLEM",
    "PASSED_NO_FAILURE",
)


def diagnose_failure(decomposition: dict[str, Any]) -> dict[str, Any]:
    """Name the failure, in priority order.

    Order matters and is not arbitrary. `NO_RAW_SIGNAL` is checked first
    because when there is no edge before costs, every other observation is a
    description of noise -- reporting "wrong exit logic" for a family with no
    signal would send someone to redesign an exit that was never the problem.
    """
    if not decomposition.get("evaluable"):
        return {"failure_reason": None, "confidence": "none", "evidence": {}, "detail": "not evaluable"}

    costs = decomposition["cost_decomposition"]
    excursions = decomposition["excursions"]
    gross = costs["gross_edge_before_costs"]
    net = costs["net_pnl"]

    if gross <= 0:
        return {
            "failure_reason": "NO_RAW_SIGNAL",
            "confidence": "high",
            "detail": "Negative edge before any costs were charged. There is nothing here for costs, exits, or filters to have destroyed.",
            "evidence": {"gross_edge_before_costs": gross},
        }

    if net <= 0:
        return {
            "failure_reason": "COST_DESTROYED_SIGNAL",
            "confidence": "high",
            "detail": (
                f"Positive gross edge of {gross:.2f} turned negative by {costs['total_costs']:.2f} of costs. "
                "The signal is real; the configuration is uneconomic."
            ),
            "evidence": {
                "gross_edge_before_costs": gross,
                "total_costs": costs["total_costs"],
                "cost_share_of_gross_edge": costs["cost_share_of_gross_edge"],
            },
        }

    directions = {row["direction"]: row for row in decomposition["by_direction"]}
    if len(directions) > 1:
        losing = [row for row in directions.values() if row["net_pnl"] < 0]
        winning = [row for row in directions.values() if row["net_pnl"] > 0]
        if losing and winning and abs(sum(row["net_pnl"] for row in losing)) > 0.5 * sum(
            row["net_pnl"] for row in winning
        ):
            return {
                "failure_reason": "WRONG_DIRECTION",
                "confidence": "medium",
                "detail": "One direction is profitable while the other gives most of it back. The hypothesis may only hold on one side.",
                "evidence": {row["direction"]: row["net_pnl"] for row in directions.values()},
            }

    capture = excursions.get("realized_capture_of_favorable")
    if capture is not None and capture < POOR_EXIT_CAPTURE_RATIO:
        return {
            "failure_reason": "WRONG_EXIT_LOGIC",
            "confidence": "medium",
            "detail": (
                f"Trades realize only {capture:.0%} of their favorable excursion. The entry is finding moves "
                "the exit is not keeping."
            ),
            "evidence": {
                "mean_favorable_r": excursions["mean_favorable_r"],
                "mean_realized_r": excursions["mean_realized_r"],
            },
        }

    adverse = excursions.get("adverse_selection_ratio")
    if adverse is not None and adverse > ADVERSE_SELECTION_RATIO:
        return {
            "failure_reason": "ENTRY_LATENCY_PROBLEM",
            "confidence": "medium",
            "detail": (
                f"Adverse excursion is {adverse:.2f}x favorable: price systematically moves against the fill "
                "immediately after entry. The signal may already be spent by the time it is tradeable."
            ),
            "evidence": {"adverse_selection_ratio": adverse},
        }

    share = decomposition.get("largest_symbol_profit_share")
    if share is not None and share > ONE_SYMBOL_PROFIT_SHARE:
        return {
            "failure_reason": "ONE_SYMBOL_DEPENDENCE",
            "confidence": "high",
            "detail": f"A single symbol supplies {share:.0%} of gross profit. This is that instrument's behavior, not a general effect.",
            "evidence": {"largest_symbol_profit_share": share},
        }

    by_regime = decomposition.get("by_market_regime")
    if by_regime:
        losing_regimes = [row for row in by_regime if row["net_pnl"] < 0]
        if losing_regimes and len(losing_regimes) < len(by_regime):
            return {
                "failure_reason": "POOR_REGIME_TARGETING",
                "confidence": "medium",
                "detail": "Profitable in some regimes and loss-making in others; the family is being run in conditions its hypothesis does not claim.",
                "evidence": {row["market_regime"]: row["net_pnl"] for row in by_regime},
            }

    holding = decomposition["holding_time_hours"]["median"]
    cost_share = costs.get("cost_share_of_gross_edge")
    if cost_share is not None and cost_share > 0.5 and holding is not None and holding < 2.0:
        return {
            "failure_reason": "EXCESSIVE_TURNOVER",
            "confidence": "medium",
            "detail": (
                f"Costs consume {cost_share:.0%} of gross edge at a median hold of {holding:.1f}h. The edge per "
                "trade is too small for the trading frequency."
            ),
            "evidence": {"cost_share_of_gross_edge": cost_share, "median_holding_hours": holding},
        }

    return {
        "failure_reason": "PASSED_NO_FAILURE",
        "confidence": "high",
        "detail": "Net positive with no dominant structural weakness detected.",
        "evidence": {"net_pnl": net},
    }


# ---------------------------------------------------------------------------
# Hypothesis-driven mutation
# ---------------------------------------------------------------------------

# One causal change per failure. Each entry states what is PRESERVED, because
# preserving the part that worked is what makes the next run informative.
MUTATION_RULES: dict[str, dict[str, Any]] = {
    "COST_DESTROYED_SIGNAL": {
        "strategy": "preserve_signal_reduce_cost_per_unit_risk",
        "preserves": "entry signal and direction",
        "changes": "stop_atr_multiple",
        "direction": "increase",
        "rationale": (
            "Risk-based sizing makes position size inversely proportional to stop distance, so costs in R "
            "scale as 1/stop. Widening the stop lowers notional per unit of risk and is the only change that "
            "reduces cost without touching the signal."
        ),
    },
    "EXCESSIVE_TURNOVER": {
        "strategy": "preserve_signal_reduce_turnover",
        "preserves": "entry signal",
        "changes": "max_holding_bars",
        "direction": "increase",
        "rationale": "Fewer, longer trades spread the same per-trade cost over a larger expected move.",
    },
    "WRONG_EXIT_LOGIC": {
        "strategy": "preserve_entry_replace_exit",
        "preserves": "entry signal and universe",
        "changes": "reward_risk_multiple",
        "direction": "increase",
        "rationale": "The entry locates moves the exit closes too early; let winners reach more of their observed favorable excursion.",
    },
    "WRONG_DIRECTION": {
        "strategy": "preserve_setup_restrict_direction",
        "preserves": "entry signal and parameters",
        "changes": "direction",
        "direction": "restrict_to_profitable_side",
        "rationale": "Test whether the hypothesis is genuinely one-sided rather than averaging a working side with a broken one.",
    },
    "POOR_REGIME_TARGETING": {
        "strategy": "preserve_family_add_regime_filter",
        "preserves": "entry signal and exits",
        "changes": "regime_filter",
        "direction": "enable",
        "rationale": "Restrict the family to the conditions its own hypothesis claims, rather than running it everywhere.",
    },
    "ONE_SYMBOL_DEPENDENCE": {
        "strategy": "preserve_hypothesis_change_universe",
        "preserves": "entry signal and parameters",
        "changes": "intended_asset_universe",
        "direction": "broaden_or_specialize",
        "rationale": (
            "Either the effect is instrument-specific -- in which case say so and test it as a specialist -- "
            "or it should reappear on comparable names."
        ),
    },
    "ENTRY_LATENCY_PROBLEM": {
        "strategy": "preserve_signal_change_entry_timing",
        "preserves": "entry condition",
        "changes": "entry_delay_bars",
        "direction": "decrease_or_use_limit_entry",
        "rationale": "If price has already moved by the next bar's open, the signal is being measured too late to be tradeable as specified.",
    },
}


def propose_next_experiment(
    diagnosis: dict[str, Any],
    *,
    architecture: str,
    hypothesis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One causal change to test next, or an explicit refusal to mutate.

    A refusal is a real answer. Mutating a family with no signal produces
    variants of noise, and that is exactly how a broad grid ends up with 120
    configurations and no hypothesis behind any of them.
    """
    reason = diagnosis.get("failure_reason")
    if reason == "NO_RAW_SIGNAL":
        return {
            "architecture": architecture,
            "mutate": False,
            "recommendation": "retire",
            "detail": (
                "No edge exists before costs, so there is nothing to preserve. Retire the family to the "
                "graveyard and spend the compute on a different hypothesis."
            ),
        }
    if reason == "PASSED_NO_FAILURE":
        return {
            "architecture": architecture,
            "mutate": False,
            "recommendation": "advance_to_confirmation",
            "detail": "No structural weakness found. Freeze the candidate and spend its single confirmation run.",
        }

    rule = MUTATION_RULES.get(str(reason))
    if rule is None:
        return {
            "architecture": architecture,
            "mutate": False,
            "recommendation": "investigate",
            "detail": f"No mutation rule is defined for {reason!r}; diagnose manually before spending compute.",
        }

    return {
        "architecture": architecture,
        "mutate": True,
        "recommendation": "single_change_experiment",
        "failure_reason": reason,
        "mutation_strategy": rule["strategy"],
        "preserves": rule["preserves"],
        "changes": rule["changes"],
        "change_direction": rule["direction"],
        "rationale": rule["rationale"],
        "hypothesis_under_test": (hypothesis or {}).get("hypothesis"),
        "invalidation_conditions": (hypothesis or {}).get("invalidation_conditions"),
        "policy": (
            "Exactly one parameter changes. Changing several at once and observing an improvement does not "
            "identify which one worked."
        ),
    }


# ---------------------------------------------------------------------------
# The graveyard
# ---------------------------------------------------------------------------

def ensure_graveyard_table(conn: psycopg.Connection) -> None:
    """Idempotent creation for fresh environments. The authoritative
    definition is migration 056; this mirrors the `ensure_*` convention the
    surrounding research modules already use."""
    statements = (
        """
        CREATE TABLE IF NOT EXISTS research_family_graveyard (
            id BIGSERIAL PRIMARY KEY,
            architecture TEXT NOT NULL,
            campaign_id BIGINT,
            failure_reason TEXT NOT NULL,
            confidence TEXT NOT NULL,
            detail TEXT NOT NULL,
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            decomposition JSONB NOT NULL DEFAULT '{}'::jsonb,
            next_experiment JSONB NOT NULL DEFAULT '{}'::jsonb,
            diagnostics_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT research_family_graveyard_unique UNIQUE (architecture, campaign_id)
        )
        """,
    )
    for statement in statements:
        conn.execute(statement)


def bury_family(
    conn: psycopg.Connection,
    *,
    architecture: str,
    campaign_id: int | None,
    diagnosis: dict[str, Any],
    decomposition: dict[str, Any],
    next_experiment: dict[str, Any],
) -> dict[str, Any]:
    """Record exactly why a family failed, so the same dead end is not
    rediscovered by a later campaign that has forgotten it."""
    ensure_graveyard_table(conn)
    row = conn.execute(
        """
        INSERT INTO research_family_graveyard(
            architecture, campaign_id, failure_reason, confidence, detail,
            evidence, decomposition, next_experiment, diagnostics_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (architecture, campaign_id) DO UPDATE SET
            failure_reason = EXCLUDED.failure_reason,
            confidence = EXCLUDED.confidence,
            detail = EXCLUDED.detail,
            evidence = EXCLUDED.evidence,
            decomposition = EXCLUDED.decomposition,
            next_experiment = EXCLUDED.next_experiment
        RETURNING *
        """,
        (
            architecture,
            campaign_id,
            str(diagnosis.get("failure_reason")),
            str(diagnosis.get("confidence")),
            str(diagnosis.get("detail")),
            Jsonb(diagnosis.get("evidence") or {}),
            Jsonb({key: value for key, value in decomposition.items() if key != "by_symbol"}),
            Jsonb(next_experiment),
            DIAGNOSTICS_VERSION,
        ),
    ).fetchone()
    return dict(row)


def list_graveyard(conn: psycopg.Connection) -> list[dict[str, Any]]:
    ensure_graveyard_table(conn)
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM research_family_graveyard ORDER BY created_at DESC"
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# Campaign-level report
# ---------------------------------------------------------------------------

def campaign_diagnostics_report(
    conn: psycopg.Connection, campaign_id: int, *, persist: bool = False
) -> dict[str, Any]:
    """Decompose, diagnose, and propose a next experiment for every family."""
    from app.services.labs.intraday.response_surface import load_campaign_family_trades

    trades_by_family, _ = load_campaign_family_trades(conn, campaign_id)
    full_trades = conn.execute(
        """
        SELECT strategy_architecture, symbol, direction, exit_reason, gross_pnl, fees,
               slippage_cost, net_pnl, holding_period_hours, mfe_r, mae_r, risk_per_unit,
               quantity, entry_minutes_from_open, market_regime, volatility_regime
        FROM research_campaign_trades
        WHERE campaign_id = %s
        """,
        (campaign_id,),
    ).fetchall()
    by_family: dict[str, list[dict[str, Any]]] = {}
    for trade in full_trades:
        by_family.setdefault(str(trade["strategy_architecture"]), []).append(dict(trade))

    try:
        from app.services.labs.intraday.families.v2.base import V2_HYPOTHESES
    except Exception:  # noqa: BLE001 - hypotheses are documentation, not a hard dependency
        V2_HYPOTHESES = {}

    families = []
    for architecture in sorted(by_family or trades_by_family):
        decomposition = decompose_family_performance(
            by_family.get(architecture, []), architecture=architecture
        )
        diagnosis = diagnose_failure(decomposition)
        spec = V2_HYPOTHESES.get(architecture)
        hypothesis = spec.as_dict() if spec is not None and hasattr(spec, "as_dict") else None
        experiment = propose_next_experiment(
            diagnosis, architecture=architecture, hypothesis=hypothesis
        )
        if persist and diagnosis.get("failure_reason") not in (None, "PASSED_NO_FAILURE"):
            bury_family(
                conn,
                architecture=architecture,
                campaign_id=campaign_id,
                diagnosis=diagnosis,
                decomposition=decomposition,
                next_experiment=experiment,
            )
        families.append(
            {
                "architecture": architecture,
                "decomposition": decomposition,
                "diagnosis": diagnosis,
                "next_experiment": experiment,
                "hypothesis": hypothesis,
            }
        )

    counts: dict[str, int] = {}
    for family in families:
        reason = str(family["diagnosis"].get("failure_reason"))
        counts[reason] = counts.get(reason, 0) + 1
    return {
        "campaign_id": campaign_id,
        "diagnostics_version": DIAGNOSTICS_VERSION,
        "families_diagnosed": len(families),
        "failure_reason_counts": dict(sorted(counts.items())),
        "families": families,
    }
