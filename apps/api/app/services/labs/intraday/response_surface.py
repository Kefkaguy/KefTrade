"""Phase C: judge a family by the shape of its response surface, not its mean.

Averaging every variant in a broad grid into one profit factor is the wrong
summary statistic for a discovery screen. A grid is deliberately built to
include configurations that should lose -- that is what makes it a search --
so the mean mostly measures how wide the grid was. It buries a small stable
region under many intentionally bad variants, and it equally rewards a family
whose entire surface is mildly positive and one that has a single lucky spike.

What actually distinguishes a real edge is *structure*:

  * a **stable region** -- several variants that are parameter-neighbors of
    one another, all positive. One lucky variant surrounded by losses is
    noise; a connected plateau is a hypothesis worth testing.
  * **breadth** -- more than one symbol contributing, so the result is not
    one instrument's idiosyncrasy.
  * **absence of concentration** -- no single symbol, no handful of trades,
    and no single month carrying the whole result.

This module computes those, and applies discovery rules built from them.
These are DISCOVERY rules: they decide what deserves more compute. The elite
gate is untouched and remains the only thing that decides what may be traded.

Every metric is computed from stored trade rows, and because those rows keep
`gross_pnl`, `fees`, and `slippage_cost` separately, the same evidence can be
re-scored under a different cost assumption without re-running a single
simulation -- see `recost_net_pnl`.
"""

from __future__ import annotations

from statistics import fmean, median
from typing import Any, Iterable, Sequence

import psycopg

RESPONSE_SURFACE_VERSION = "family_response_surface_v1"

# --- Discovery rules (NOT the elite gate) ----------------------------------
# A stable region needs at least this many mutually-neighboring positive
# variants. Two is a coincidence; three connected points is a plateau.
MINIMUM_STABLE_REGION_VARIANTS = 3
MINIMUM_POSITIVE_SYMBOLS = 2
# Applied to the best decile, not the mean -- a discovery floor asks "is the
# good part of this surface good enough to investigate", not "is all of it".
DISCOVERY_BEST_DECILE_PROFIT_FACTOR = 1.2
MAXIMUM_SINGLE_SYMBOL_PROFIT_SHARE = 0.60
MAXIMUM_TOP_TRADE_PROFIT_SHARE = 0.50
TOP_TRADE_FRACTION = 0.05
MAXIMUM_SINGLE_MONTH_PROFIT_SHARE = 0.60
MINIMUM_TRADES_FOR_A_VERDICT = 30


def cost_scenarios() -> dict[str, dict[str, Any]]:
    """The cost assumptions a family is scored under, side by side.

    `as_simulated` reproduces the stored result exactly. `realistic_retail`
    re-scores the identical trades at costs typical of liquid US large-caps.
    Multipliers are derived from the live parameters rather than hardcoded,
    so changing the base config cannot silently desynchronize the comparison.
    """
    from app.services.labs.intraday.families.v2.base import BASE_V2_PARAMETERS

    simulated_fee = float(BASE_V2_PARAMETERS["fee_rate"])
    simulated_slippage = float(BASE_V2_PARAMETERS["slippage_rate"])
    realistic_fee = 0.0001
    realistic_slippage = 0.0002
    return {
        "as_simulated": {
            "fee_rate": simulated_fee,
            "slippage_rate": simulated_slippage,
            "fee_multiplier": 1.0,
            "slippage_multiplier": 1.0,
            "description": "The costs the campaign actually ran under.",
        },
        "realistic_retail": {
            "fee_rate": realistic_fee,
            "slippage_rate": realistic_slippage,
            "fee_multiplier": realistic_fee / simulated_fee if simulated_fee else 0.0,
            "slippage_multiplier": realistic_slippage / simulated_slippage if simulated_slippage else 0.0,
            "description": (
                "Same trades re-scored at ~1bp fee + 2bps slippage per leg. Approximation: entry and "
                "exit decisions are held fixed, so it does not model the fills a lower slippage "
                "assumption would itself have changed."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Re-costing
# ---------------------------------------------------------------------------

def recost_net_pnl(trade: dict[str, Any], *, fee_multiplier: float, slippage_multiplier: float) -> float:
    """Net P&L of a stored trade under different cost rates.

    Both cost components are linear in their rate (fees are notional x rate
    per leg, slippage is price x rate per leg), so scaling the stored amounts
    is exact for the cost term. What it does NOT capture is that a different
    slippage assumption would have shifted the fill prices themselves, and
    therefore `gross_pnl` and possibly which bar a stop or target was hit on.
    That second-order effect is small for small rate changes and is stated
    here rather than buried.
    """
    return (
        float(trade["gross_pnl"])
        - float(trade["fees"]) * fee_multiplier
        - float(trade["slippage_cost"]) * slippage_multiplier
    )


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def _profit_metrics(pnls: Sequence[float]) -> dict[str, Any]:
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trade_count": len(pnls),
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "net_pnl": round(gross_profit - gross_loss, 6),
        "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > 0 else None,
        "expectancy": round(fmean(pnls), 6) if pnls else 0.0,
        "win_rate": round(len(wins) / len(pnls), 6) if pnls else 0.0,
    }


def _sort_key(value: Any) -> tuple[int, Any]:
    """Order values within one parameter, tolerating mixed types.

    Numbers sort numerically ahead of everything else; anything else sorts by
    its string form. A grid mixing types is unusual but must not crash the
    screen.
    """
    if isinstance(value, bool):
        return (1, str(value))
    if isinstance(value, (int, float)):
        return (0, float(value))
    try:
        return (0, float(str(value)))
    except (TypeError, ValueError):
        return (1, str(value))


def build_parameter_value_order(variants: Iterable[dict[str, Any]]) -> dict[str, list[Any]]:
    """Per parameter, the sorted distinct values the family actually explored.

    Built from every variant including the losing ones, because adjacency has
    to be defined over the whole grid: a losing configuration sitting between
    two winners genuinely separates them, and must not be stepped over.
    """
    values_by_key: dict[str, list[Any]] = {}
    for variant in variants:
        for key, value in (variant.get("parameters") or {}).items():
            bucket = values_by_key.setdefault(key, [])
            if value not in bucket:
                bucket.append(value)
    return {key: sorted(values, key=_sort_key) for key, values in values_by_key.items()}


def are_parameter_neighbors(
    left: dict[str, Any],
    right: dict[str, Any],
    value_order: dict[str, list[Any]] | None = None,
) -> bool:
    """True when two variants differ in exactly one parameter, by one step.

    Neighborhood is what makes a region a region: it distinguishes three
    variants sitting next to each other on the surface from three unrelated
    points that happen to be positive. Requiring the differing value to be
    *adjacent in the explored grid* -- not merely different -- is what stops
    two winners on opposite ends of a parameter's range from being counted as
    a plateau when everything between them loses.

    With no `value_order` the check falls back to plain single-key difference,
    which is the right behavior when the surrounding grid is unknown.
    """
    keys = set(left) | set(right)
    differing = [key for key in keys if left.get(key) != right.get(key)]
    if len(differing) != 1:
        return False
    if value_order is None:
        return True
    key = differing[0]
    ordered = value_order.get(key)
    if not ordered:
        return True
    try:
        return abs(ordered.index(left.get(key)) - ordered.index(right.get(key))) == 1
    except ValueError:
        return False


def largest_stable_region(variants: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Size of the biggest connected cluster of positive-expectancy variants.

    Connectivity is single-parameter adjacency, so the returned cluster is a
    genuine plateau on the response surface rather than a scattered set of
    lucky points.
    """
    value_order = build_parameter_value_order(variants)
    positive = [variant for variant in variants if (variant.get("expectancy") or 0) > 0]
    if not positive:
        return {"size": 0, "candidate_ids": []}

    adjacency: dict[int, list[int]] = {index: [] for index in range(len(positive))}
    for i in range(len(positive)):
        for j in range(i + 1, len(positive)):
            if are_parameter_neighbors(positive[i]["parameters"], positive[j]["parameters"], value_order):
                adjacency[i].append(j)
                adjacency[j].append(i)

    seen: set[int] = set()
    best: list[int] = []
    for start in range(len(positive)):
        if start in seen:
            continue
        stack = [start]
        component: list[int] = []
        seen.add(start)
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbor in adjacency[node]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        if len(component) > len(best):
            best = component
    return {
        "size": len(best),
        "candidate_ids": sorted(positive[index]["candidate_id"] for index in best),
    }


def concentration(amounts_by_key: dict[str, float]) -> dict[str, Any]:
    """How much of the total profit sits in its single largest contributor."""
    positive = {key: value for key, value in amounts_by_key.items() if value > 0}
    total = sum(positive.values())
    if total <= 0:
        return {"total_profit": round(total, 6), "largest_share": None, "largest_key": None, "contributors": 0}
    largest_key = max(positive, key=lambda key: positive[key])
    return {
        "total_profit": round(total, 6),
        "largest_key": largest_key,
        "largest_share": round(positive[largest_key] / total, 6),
        "contributors": len(positive),
    }


def trade_concentration(pnls: Sequence[float], *, top_fraction: float = TOP_TRADE_FRACTION) -> dict[str, Any]:
    """Share of gross profit produced by the best few trades.

    A family whose entire result is two lucky trades has not demonstrated a
    repeatable process, however good the aggregate looks.
    """
    wins = sorted((value for value in pnls if value > 0), reverse=True)
    gross_profit = sum(wins)
    if not wins or gross_profit <= 0:
        return {"top_trade_share": None, "top_trade_count": 0, "gross_profit": 0.0}
    count = max(1, int(round(len(pnls) * top_fraction)))
    return {
        "top_trade_share": round(sum(wins[:count]) / gross_profit, 6),
        "top_trade_count": count,
        "gross_profit": round(gross_profit, 6),
    }


# ---------------------------------------------------------------------------
# Family analysis
# ---------------------------------------------------------------------------

def analyze_family_response_surface(
    trades: Iterable[dict[str, Any]],
    parameters_by_candidate: dict[str, dict[str, Any]],
    *,
    architecture: str,
    fee_multiplier: float = 1.0,
    slippage_multiplier: float = 1.0,
) -> dict[str, Any]:
    """Full response-surface description and discovery verdict for one family.

    `trades` are stored trade rows; each must carry `candidate_id`, `symbol`,
    `gross_pnl`, `fees`, `slippage_cost`, and optionally `month_key`.
    """
    rows = list(trades)
    by_candidate: dict[str, list[float]] = {}
    by_symbol: dict[str, list[float]] = {}
    by_month: dict[str, list[float]] = {}
    all_pnls: list[float] = []
    for trade in rows:
        net = recost_net_pnl(trade, fee_multiplier=fee_multiplier, slippage_multiplier=slippage_multiplier)
        all_pnls.append(net)
        by_candidate.setdefault(str(trade["candidate_id"]), []).append(net)
        by_symbol.setdefault(str(trade["symbol"]), []).append(net)
        month = trade.get("month_key")
        if month:
            by_month.setdefault(str(month), []).append(net)

    variants = [
        {
            "candidate_id": candidate_id,
            "parameters": parameters_by_candidate.get(candidate_id, {}),
            **_profit_metrics(pnls),
        }
        for candidate_id, pnls in sorted(by_candidate.items())
    ]
    symbols = [{"symbol": symbol, **_profit_metrics(pnls)} for symbol, pnls in sorted(by_symbol.items())]

    variant_profit_factors = [
        variant["profit_factor"] for variant in variants if variant["profit_factor"] is not None
    ]
    ranked = sorted(variants, key=lambda variant: variant["expectancy"], reverse=True)
    decile_size = max(1, len(ranked) // 10)
    best_decile = ranked[:decile_size]
    best_decile_profit_factors = [
        variant["profit_factor"] for variant in best_decile if variant["profit_factor"] is not None
    ]

    region = largest_stable_region(variants)
    symbol_concentration = concentration({row["symbol"]: row["net_pnl"] for row in symbols})
    month_concentration = concentration(
        {month: sum(pnls) for month, pnls in by_month.items()}
    )
    positive_symbols = [row for row in symbols if row["expectancy"] > 0]
    positive_variants = [variant for variant in variants if variant["expectancy"] > 0]
    positive_months = [month for month, pnls in by_month.items() if sum(pnls) > 0]

    surface = {
        "architecture": architecture,
        "response_surface_version": RESPONSE_SURFACE_VERSION,
        "trade_count": len(rows),
        "variant_count": len(variants),
        "symbol_count": len(symbols),
        "aggregate": _profit_metrics(all_pnls),
        "median_variant_profit_factor": round(median(variant_profit_factors), 6) if variant_profit_factors else None,
        "median_variant_expectancy": round(median([v["expectancy"] for v in variants]), 6) if variants else None,
        "best_decile_median_profit_factor": (
            round(median(best_decile_profit_factors), 6) if best_decile_profit_factors else None
        ),
        "best_decile_variant_count": len(best_decile),
        "positive_variant_share": round(len(positive_variants) / len(variants), 6) if variants else 0.0,
        "positive_symbol_share": round(len(positive_symbols) / len(symbols), 6) if symbols else 0.0,
        "positive_symbol_count": len(positive_symbols),
        "positive_month_share": round(len(positive_months) / len(by_month), 6) if by_month else None,
        "stable_region": region,
        "symbol_profit_concentration": symbol_concentration,
        "month_profit_concentration": month_concentration,
        "trade_profit_concentration": trade_concentration(all_pnls),
        "variants": variants,
        "symbols": symbols,
    }
    surface["exclusion_reasons"] = _discovery_exclusions(surface)
    surface["promising"] = not surface["exclusion_reasons"]
    return surface


def _discovery_exclusions(surface: dict[str, Any]) -> list[str]:
    """Every reason a family does not qualify for focused research.

    Deliberately verbose: a family that fails should say which structural
    property it lacked, because that determines the next experiment.
    """
    reasons: list[str] = []
    if surface["trade_count"] < MINIMUM_TRADES_FOR_A_VERDICT:
        reasons.append("INSUFFICIENT_TRADES_FOR_A_VERDICT")
    if surface["aggregate"]["expectancy"] <= 0:
        reasons.append("NON_POSITIVE_AGGREGATE_EXPECTANCY")
    if surface["stable_region"]["size"] < MINIMUM_STABLE_REGION_VARIANTS:
        reasons.append("NO_STABLE_PARAMETER_REGION")
    if surface["positive_symbol_count"] < MINIMUM_POSITIVE_SYMBOLS:
        reasons.append("TOO_FEW_POSITIVE_SYMBOLS")
    best_decile = surface["best_decile_median_profit_factor"]
    if best_decile is None or best_decile < DISCOVERY_BEST_DECILE_PROFIT_FACTOR:
        reasons.append("BEST_DECILE_BELOW_DISCOVERY_FLOOR")
    symbol_share = surface["symbol_profit_concentration"]["largest_share"]
    if symbol_share is not None and symbol_share > MAXIMUM_SINGLE_SYMBOL_PROFIT_SHARE:
        reasons.append("PROFIT_CONCENTRATED_IN_ONE_SYMBOL")
    trade_share = surface["trade_profit_concentration"]["top_trade_share"]
    if trade_share is not None and trade_share > MAXIMUM_TOP_TRADE_PROFIT_SHARE:
        reasons.append("PROFIT_CONCENTRATED_IN_FEW_TRADES")
    month_share = surface["month_profit_concentration"]["largest_share"]
    if month_share is not None and month_share > MAXIMUM_SINGLE_MONTH_PROFIT_SHARE:
        reasons.append("PROFIT_CONCENTRATED_IN_ONE_PERIOD")
    return reasons


# ---------------------------------------------------------------------------
# Campaign-level report
# ---------------------------------------------------------------------------

def load_campaign_family_trades(
    conn: psycopg.Connection, campaign_id: int
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Stored trades grouped by family, plus each candidate's parameter set."""
    trades = conn.execute(
        """
        SELECT strategy_architecture, candidate_id, symbol, month_key,
               gross_pnl, fees, slippage_cost, net_pnl
        FROM research_campaign_trades
        WHERE campaign_id = %s
        """,
        (campaign_id,),
    ).fetchall()
    by_family: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        by_family.setdefault(str(trade["strategy_architecture"]), []).append(dict(trade))

    parameter_rows = conn.execute(
        """
        SELECT DISTINCT ON (candidate_id)
               candidate_id, candidate->'parameters' AS parameters
        FROM research_campaign_jobs
        WHERE campaign_id = %s AND candidate IS NOT NULL
        ORDER BY candidate_id
        """,
        (campaign_id,),
    ).fetchall()
    parameters_by_candidate = {
        str(row["candidate_id"]): dict(row["parameters"] or {}) for row in parameter_rows
    }
    return by_family, parameters_by_candidate


def family_response_surface_report(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    """Score every family's response surface under each cost scenario.

    Reporting both scenarios side by side keeps the cost assumption visible
    instead of embedded: a family that only becomes promising once costs are
    corrected is a different finding from one that is promising either way,
    and the difference should never be silently resolved by whichever
    assumption happened to be configured.
    """
    by_family, parameters_by_candidate = load_campaign_family_trades(conn, campaign_id)
    scenarios = cost_scenarios()

    families: list[dict[str, Any]] = []
    for architecture, trades in sorted(by_family.items()):
        scored = {
            name: analyze_family_response_surface(
                trades,
                parameters_by_candidate,
                architecture=architecture,
                fee_multiplier=scenario["fee_multiplier"],
                slippage_multiplier=scenario["slippage_multiplier"],
            )
            for name, scenario in scenarios.items()
        }
        simulated = scored["as_simulated"]
        realistic = scored["realistic_retail"]
        families.append(
            {
                "architecture": architecture,
                "trade_count": simulated["trade_count"],
                "promising_as_simulated": simulated["promising"],
                "promising_at_realistic_costs": realistic["promising"],
                "cost_sensitive": realistic["promising"] and not simulated["promising"],
                "expectancy_as_simulated": simulated["aggregate"]["expectancy"],
                "expectancy_at_realistic_costs": realistic["aggregate"]["expectancy"],
                "scenarios": scored,
            }
        )

    families.sort(
        key=lambda row: (
            row["promising_at_realistic_costs"],
            row["expectancy_at_realistic_costs"],
        ),
        reverse=True,
    )
    cost_sensitive = [row["architecture"] for row in families if row["cost_sensitive"]]
    return {
        "campaign_id": campaign_id,
        "response_surface_version": RESPONSE_SURFACE_VERSION,
        "cost_scenarios": scenarios,
        "families_analyzed": len(families),
        "promising_as_simulated": sum(1 for row in families if row["promising_as_simulated"]),
        "promising_at_realistic_costs": sum(1 for row in families if row["promising_at_realistic_costs"]),
        "cost_sensitive_families": cost_sensitive,
        "interpretation": (
            "Families listed as cost_sensitive have a structurally sound response surface that the "
            "configured cost model alone rejects. That is a cost-calibration finding, not evidence of "
            "edge -- confirm the real execution cost before acting on it."
            if cost_sensitive
            else "No family changes verdict between the two cost assumptions."
        ),
        "families": families,
    }
