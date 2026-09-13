from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/app/apps/api")

from app.db import connect
from app.services.labs.intraday.session import trading_schedule


SRC = "alpaca_sip"
TF = "30m"
START = date(2016, 1, 1)
END = date(2026, 7, 30)

DISCOVERY_FRAC = 0.50
VALIDATION_FRAC = 0.30

MIN_PRIOR20_DOLLAR_VOL = 20_000_000
PRIMARY_COST_BPS = 25.0
STRESS_COSTS_BPS = (10.0, 25.0, 50.0, 75.0)
BORROW_ANNUAL_BPS = 500.0
LOCATE_FEE_BPS = 3.0

GAP_GRID_BPS = (150.0, 250.0, 350.0, 500.0)
FIRST30_MAX_GRID_BPS = (-10.0, -25.0, -50.0, -75.0)
CLOSE_LOCATION_MAX_GRID = (0.35, 0.50)
EXIT_BARS = (4, 5, 7, 13)

MIN_DISCOVERY_TRADES = 80
MIN_VALIDATION_TRADES = 40
SEED = 20260823
NBOOT = 3000


def finite(value: float) -> bool:
    return bool(np.isfinite(value))


def clustered_t(df: pd.DataFrame, column: str) -> float:
    x = df[column].to_numpy(dtype=float)
    clusters = df["entry_date"].to_numpy()
    unique = np.unique(clusters)
    if len(x) < 2 or len(unique) < 2:
        return float("nan")
    mean = x.mean()
    meat = 0.0
    for cluster in unique:
        u = x[clusters == cluster] - mean
        meat += float(u.sum() ** 2)
    variance = len(unique) / (len(unique) - 1) * meat / (len(x) ** 2)
    if variance <= 0:
        return float("nan")
    return float(mean / math.sqrt(variance))


def cluster_bootstrap_ci(df: pd.DataFrame, column: str) -> tuple[float, float]:
    grouped = df.groupby("entry_date")[column].agg(["sum", "count"])
    if len(grouped) < 2:
        return (float("nan"), float("nan"))
    sums = grouped["sum"].to_numpy(dtype=float)
    counts = grouped["count"].to_numpy(dtype=float)
    rng = np.random.default_rng(SEED)
    boot = np.empty(NBOOT, dtype=float)
    for i in range(NBOOT):
        idx = rng.integers(0, len(grouped), size=len(grouped))
        boot[i] = sums[idx].sum() / counts[idx].sum()
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return (float(lo), float(hi))


def summarize(df: pd.DataFrame, phase: str, cost_col: str = "net25_bps") -> dict:
    if df.empty:
        return {"phase": phase, "trades": 0}
    x = df[cost_col].to_numpy(dtype=float)
    winners = x[x > 0]
    losers = x[x <= 0]
    yearly = df.assign(year=df["entry_date"].str[:4]).groupby("year")[cost_col].mean()
    ci_lo, ci_hi = cluster_bootstrap_ci(df, cost_col)
    positive_sum = df.loc[df[cost_col] > 0, cost_col].sum()
    top5_winner_share = (
        df.loc[df[cost_col] > 0, cost_col].sort_values(ascending=False).head(5).sum()
        / positive_sum
        if positive_sum > 0
        else float("nan")
    )
    return {
        "phase": phase,
        "trades": int(len(df)),
        "entry_dates": int(df["entry_date"].nunique()),
        "symbols": int(df["symbol"].nunique()),
        "mean_gross_bps": float(df["gross_bps"].mean()),
        "median_gross_bps": float(df["gross_bps"].median()),
        "mean_net25_bps": float(df["net25_bps"].mean()),
        "mean_net50_bps": float(df["net50_bps"].mean()),
        "mean_net75_bps": float(df["net75_bps"].mean()),
        "median_net25_bps": float(df["net25_bps"].median()),
        "win_rate_net25": float((df["net25_bps"] > 0).mean()),
        "profit_factor_net25": float(winners.sum() / abs(losers.sum())) if len(losers) and abs(losers.sum()) > 0 else None,
        "entry_date_cluster_t_net25": clustered_t(df, "net25_bps"),
        "cluster_bootstrap_95_lo_net25": ci_lo,
        "cluster_bootstrap_95_hi_net25": ci_hi,
        "positive_year_share_net25": float((yearly > 0).mean()),
        "top5_winner_share": float(top5_winner_share),
    }


def make_panel(db, schedule: pd.DataFrame, visible_end_idx: int) -> pd.DataFrame:
    visible = schedule.iloc[:visible_end_idx]
    db.execute("SET LOCAL statement_timeout=0")
    db.execute(
        """
        CREATE TEMP TABLE sgf_sched (
            session_date DATE PRIMARY KEY,
            session_ord INTEGER NOT NULL,
            market_open TIMESTAMPTZ NOT NULL,
            market_close TIMESTAMPTZ NOT NULL,
            expected INTEGER NOT NULL
        ) ON COMMIT DROP
        """
    )
    values = []
    for session_ord, (idx, row) in enumerate(visible.iterrows()):
        market_open = row["market_open"].to_pydatetime()
        market_close = row["market_close"].to_pydatetime()
        expected = int((market_close - market_open).total_seconds() // 1800)
        values.append((idx.date(), session_ord, market_open, market_close, expected))
    with db.cursor() as cursor:
        cursor.executemany("INSERT INTO sgf_sched VALUES (%s,%s,%s,%s,%s)", values)

    rows = db.execute(
        """
        WITH per_session AS (
            SELECT
                c.symbol,
                s.session_date,
                s.session_ord,
                s.market_open,
                s.market_close,
                s.expected,
                COUNT(*) AS n,
                MIN(c.timestamp) AS first_bar,
                MAX(c.timestamp) AS last_bar,
                (ARRAY_AGG(c.open ORDER BY c.timestamp))[1]::float8 AS first_open,
                (ARRAY_AGG(c.high ORDER BY c.timestamp))[1]::float8 AS first_high,
                (ARRAY_AGG(c.low ORDER BY c.timestamp))[1]::float8 AS first_low,
                (ARRAY_AGG(c.close ORDER BY c.timestamp))[1]::float8 AS first_close,
                (ARRAY_AGG(c.open ORDER BY c.timestamp))[2]::float8 AS entry_open,
                (ARRAY_AGG(c.open ORDER BY c.timestamp))[4]::float8 AS exit4_open,
                (ARRAY_AGG(c.open ORDER BY c.timestamp))[5]::float8 AS exit5_open,
                (ARRAY_AGG(c.open ORDER BY c.timestamp))[7]::float8 AS exit7_open,
                (ARRAY_AGG(c.open ORDER BY c.timestamp))[13]::float8 AS exit13_open,
                (ARRAY_AGG(c.close ORDER BY c.timestamp DESC))[1]::float8 AS day_close,
                SUM((c.close::float8) * (c.volume::float8))::float8 AS dollar_volume
            FROM candles c
            JOIN sgf_sched s
              ON c.timestamp >= s.market_open
             AND c.timestamp < s.market_close
            WHERE c.source = %s
              AND c.timeframe = %s
            GROUP BY c.symbol, s.session_date, s.session_ord, s.market_open, s.market_close, s.expected
        )
        SELECT *
        FROM per_session
        WHERE n = expected
          AND expected >= 13
          AND first_bar = market_open
          AND last_bar = market_close - INTERVAL '30 minutes'
        ORDER BY symbol, session_ord
        """,
        (SRC, TF),
    ).fetchall()
    panel = pd.DataFrame(rows)
    if panel.empty:
        raise RuntimeError("No complete 30m sessions found.")
    return panel


def add_features(panel: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for symbol, group in panel.groupby("symbol", sort=False):
        g = group.sort_values("session_ord").copy()
        g["prev_close"] = g["day_close"].shift(1)
        g["prior20_dollar_volume"] = g["dollar_volume"].shift(1).rolling(20, min_periods=20).median()
        g["gap_bps"] = (g["first_open"] / g["prev_close"] - 1.0) * 10000.0
        g["first30_bps"] = (g["first_close"] / g["first_open"] - 1.0) * 10000.0
        denom = g["first_high"] - g["first_low"]
        g["close_location"] = np.where(denom > 0, (g["first_close"] - g["first_low"]) / denom, np.nan)
        frames.append(g)
    return pd.concat(frames, ignore_index=True)


def simulate_variant(panel: pd.DataFrame, spec: dict, phase: str, lo: int, hi: int) -> pd.DataFrame:
    exit_col = f"exit{spec['exit_bar']}_open"
    df = panel[
        (panel["session_ord"] >= lo)
        & (panel["session_ord"] < hi)
        & (panel["gap_bps"] >= spec["min_gap_bps"])
        & (panel["first30_bps"] <= spec["first30_max_bps"])
        & (panel["close_location"] <= spec["close_location_max"])
        & (panel["prior20_dollar_volume"] >= MIN_PRIOR20_DOLLAR_VOL)
        & (panel["entry_open"] > 0)
        & (panel[exit_col] > 0)
    ].copy()
    if df.empty:
        return df
    df["phase"] = phase
    df["variant"] = spec["variant"]
    df["exit_bar"] = spec["exit_bar"]
    df["entry_date"] = df["session_date"].astype(str)
    df["entry"] = df["entry_open"].astype(float)
    df["exit"] = df[exit_col].astype(float)
    df["gross_bps"] = (df["entry"] - df["exit"]) / df["entry"] * 10000.0
    borrow_bps = BORROW_ANNUAL_BPS / 252.0
    for cost in STRESS_COSTS_BPS:
        df[f"net{int(cost)}_bps"] = df["gross_bps"] - cost - borrow_bps - LOCATE_FEE_BPS
    keep = [
        "phase",
        "variant",
        "symbol",
        "entry_date",
        "exit_bar",
        "gap_bps",
        "first30_bps",
        "close_location",
        "prior20_dollar_volume",
        "entry",
        "exit",
        "gross_bps",
        "net10_bps",
        "net25_bps",
        "net50_bps",
        "net75_bps",
    ]
    return df[keep].copy()


def print_metrics(title: str, metrics: dict) -> None:
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)
    for key, value in metrics.items():
        if isinstance(value, float):
            print(f"{key:42s}{value:12.4f}")
        else:
            print(f"{key:42s}{value}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Short-only gap failure deep research. Read-only; no broker orders.")
    parser.add_argument("--out", default="/reports")
    parser.add_argument("--read-confirmation", action="store_true")
    args = parser.parse_args()
    if args.read_confirmation:
        raise SystemExit("Confirmation window is locked for this research script.")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    schedule = trading_schedule(START, END, padding_days=0)
    total_sessions = len(schedule)
    discovery_cut = int(total_sessions * DISCOVERY_FRAC)
    validation_cut = int(total_sessions * (DISCOVERY_FRAC + VALIDATION_FRAC))

    with connect() as db:
        panel = add_features(make_panel(db, schedule, validation_cut))

    print("FROZEN SPECIFICATION")
    print(f"family=short_gap_failure_deep_v1 source={SRC} timeframe={TF}")
    print("side=SHORT_ONLY entry=second_30m_open exit=grid(4,5,7,13)_30m_open")
    print(f"costs_bps={STRESS_COSTS_BPS} borrow_annual_bps={BORROW_ANNUAL_BPS} locate_fee_bps={LOCATE_FEE_BPS}")
    print(f"discovery={schedule.index[0].date()}..{schedule.index[discovery_cut - 1].date()}")
    print(f"validation={schedule.index[discovery_cut].date()}..{schedule.index[validation_cut - 1].date()}")
    print(f"confirmation_LOCKED={schedule.index[validation_cut].date()}..{schedule.index[-1].date()}")
    print("confirmation_read=False")
    print(f"panel_rows={len(panel):,} symbols={panel['symbol'].nunique():,}")

    variants = []
    for min_gap in GAP_GRID_BPS:
        for first30_max in FIRST30_MAX_GRID_BPS:
            for close_location_max in CLOSE_LOCATION_MAX_GRID:
                for exit_bar in EXIT_BARS:
                    variant = f"gap{int(min_gap)}_f30{int(abs(first30_max))}_cl{int(close_location_max*100)}_x{exit_bar}"
                    variants.append(
                        {
                            "variant": variant,
                            "min_gap_bps": min_gap,
                            "first30_max_bps": first30_max,
                            "close_location_max": close_location_max,
                            "exit_bar": exit_bar,
                        }
                    )

    discovery_rows = []
    discovery_metrics = []
    for spec in variants:
        trades = simulate_variant(panel, spec, "discovery", 0, discovery_cut)
        if not trades.empty:
            discovery_rows.append(trades)
        metrics = summarize(trades, "discovery")
        metrics.update(spec)
        discovery_metrics.append(metrics)

    discovery_table = pd.DataFrame(discovery_metrics).sort_values(
        ["mean_net25_bps", "entry_date_cluster_t_net25", "trades"],
        ascending=False,
    )
    discovery_table.to_csv(out / "discovery_variants.csv", index=False)

    selected = discovery_table[
        (discovery_table["trades"] >= MIN_DISCOVERY_TRADES)
        & (discovery_table["mean_net25_bps"] > 0)
        & (discovery_table["mean_net50_bps"] > 0)
        & (discovery_table["entry_date_cluster_t_net25"] >= 1.5)
        & (discovery_table["top5_winner_share"] <= 0.30)
    ].head(10)

    validation_rows = []
    validation_metrics = []
    for _, row in selected.iterrows():
        spec = {
            "variant": row["variant"],
            "min_gap_bps": float(row["min_gap_bps"]),
            "first30_max_bps": float(row["first30_max_bps"]),
            "close_location_max": float(row["close_location_max"]),
            "exit_bar": int(row["exit_bar"]),
        }
        trades = simulate_variant(panel, spec, "validation", discovery_cut, validation_cut)
        if not trades.empty:
            validation_rows.append(trades)
        metrics = summarize(trades, "validation")
        metrics.update(spec)
        validation_metrics.append(metrics)

    validation_table = pd.DataFrame(validation_metrics)
    if not validation_table.empty:
        validation_table = validation_table.sort_values(
            ["mean_net25_bps", "entry_date_cluster_t_net25", "trades"],
            ascending=False,
        )
    validation_table.to_csv(out / "validation_variants.csv", index=False)

    all_discovery = pd.concat(discovery_rows, ignore_index=True) if discovery_rows else pd.DataFrame()
    all_validation = pd.concat(validation_rows, ignore_index=True) if validation_rows else pd.DataFrame()
    all_trades = pd.concat([all_discovery, all_validation], ignore_index=True) if not all_discovery.empty or not all_validation.empty else pd.DataFrame()
    all_trades.to_csv(out / "trades.csv", index=False)

    best_validation = validation_table.iloc[0].to_dict() if not validation_table.empty else {}
    verdict = "REJECT"
    if best_validation:
        if (
            best_validation.get("trades", 0) >= MIN_VALIDATION_TRADES
            and best_validation.get("mean_net25_bps", -999) > 25
            and best_validation.get("mean_net50_bps", -999) > 0
            and best_validation.get("median_net25_bps", -999) > 0
            and best_validation.get("entry_date_cluster_t_net25", -999) >= 2.0
            and best_validation.get("cluster_bootstrap_95_lo_net25", -999) > 0
            and best_validation.get("positive_year_share_net25", 0) >= 0.70
        ):
            verdict = "STRONG_ENOUGH_FOR_CONFIRMATION"
        elif (
            best_validation.get("trades", 0) >= MIN_VALIDATION_TRADES
            and best_validation.get("mean_net25_bps", -999) > 0
            and best_validation.get("mean_net50_bps", -999) > -10
            and best_validation.get("entry_date_cluster_t_net25", -999) >= 1.5
        ):
            verdict = "PROMISING_INVESTIGATE"

    result = {
        "strategy": "short_gap_failure_deep_v1",
        "verdict": verdict,
        "confirmation_read": False,
        "variants_tested": len(variants),
        "variants_selected_from_discovery": int(len(selected)),
        "best_validation": best_validation,
        "windows": {
            "discovery": [str(schedule.index[0].date()), str(schedule.index[discovery_cut - 1].date())],
            "validation": [str(schedule.index[discovery_cut].date()), str(schedule.index[validation_cut - 1].date())],
            "confirmation_locked": [str(schedule.index[validation_cut].date()), str(schedule.index[-1].date())],
        },
        "cost_model": {
            "stress_costs_bps": STRESS_COSTS_BPS,
            "borrow_annual_bps": BORROW_ANNUAL_BPS,
            "locate_fee_bps": LOCATE_FEE_BPS,
        },
    }
    (out / "result.json").write_text(json.dumps(result, indent=2, default=str))
    (out / "frozen_spec.json").write_text(
        json.dumps(
            {
                "strategy": "short_gap_failure_deep_v1",
                "side": "short_only",
                "source": SRC,
                "timeframe": TF,
                "min_prior20_dollar_volume": MIN_PRIOR20_DOLLAR_VOL,
                "gap_grid_bps": GAP_GRID_BPS,
                "first30_max_grid_bps": FIRST30_MAX_GRID_BPS,
                "close_location_max_grid": CLOSE_LOCATION_MAX_GRID,
                "exit_bars": EXIT_BARS,
                "primary_cost_bps": PRIMARY_COST_BPS,
                "borrow_annual_bps": BORROW_ANNUAL_BPS,
                "locate_fee_bps": LOCATE_FEE_BPS,
                "confirmation_read": False,
            },
            indent=2,
            sort_keys=True,
        )
    )

    print_metrics("TOP DISCOVERY VARIANTS", discovery_table.head(10).to_dict("records")[0] if not discovery_table.empty else {})
    if best_validation:
        print_metrics("BEST VALIDATION VARIANT", best_validation)
    print("\n" + "=" * 100)
    print("VERDICT:", verdict)
    print("artifacts=/reports/discovery_variants.csv,/reports/validation_variants.csv,/reports/trades.csv,/reports/result.json")
    print("RESULT_JSON=" + json.dumps(result, default=str, separators=(",", ":")))
    print("=" * 100)


if __name__ == "__main__":
    main()
