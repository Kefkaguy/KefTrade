from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from app.db import connect


OUT = Path("/reports")
OUT.mkdir(parents=True, exist_ok=True)

SOURCE = "alpaca_sip"
START = "2024-04-01"
END_EXCLUSIVE = "2026-08-31"
DISCOVERY_END = date(2025, 3, 31)
VALIDATION_END = date(2025, 12, 31)
COSTS = (5.0, 10.0, 25.0)
MIN_TEST_TRADES = 40
SEED = 20260830

ETFS = {"SPY", "QQQ"}
EQUITIES = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AMD",
    "NFLX", "AVGO", "ORCL", "CRM", "INTC", "MU", "COIN", "BA",
    "JPM", "BAC", "DIS", "WMT", "TGT", "F", "GM",
]
SYMBOLS = EQUITIES + sorted(ETFS)
PAIRS = [
    ("AAPL", "MSFT"),
    ("AMD", "NVDA"),
    ("JPM", "BAC"),
    ("F", "GM"),
    ("ORCL", "CRM"),
    ("AMZN", "GOOGL"),
]

POSITIVE_WORDS = (
    "beats estimates", "beat estimates", "raises guidance", "raised guidance",
    "guidance raised", "wins contract", "awarded contract", "approval granted",
    "fda approval", "record revenue", "record earnings", "upgrade to buy",
    "upgraded to buy", "positive trial", "special dividend",
)
NEGATIVE_WORDS = (
    "misses estimates", "missed estimates", "cuts guidance", "cut guidance",
    "guidance cut", "downgrade to sell", "downgraded to sell", "investigation",
    "accounting probe", "sec probe", "fraud", "bankruptcy", "chapter 11",
    "recall", "data breach", "share offering", "public offering", "failed trial",
)


def phase_for(d: date) -> str:
    if d <= DISCOVERY_END:
        return "discovery"
    if d <= VALIDATION_END:
        return "validation"
    return "test"


def getrow(g: pd.DataFrame, minute: int):
    x = g.loc[g["minute"].eq(minute)]
    return None if x.empty else x.iloc[0]


def headline_score(text: str) -> int:
    t = (text or "").lower()
    return int(any(w in t for w in POSITIVE_WORDS)) - int(any(w in t for w in NEGATIVE_WORDS))


def side_return(side: str, entry: float, exit_price: float) -> float:
    raw = exit_price / entry - 1.0
    return raw if side == "LONG" else -raw


def simulate_exit(
    g: pd.DataFrame,
    entry_i: int,
    side: str,
    entry: float,
    stop_bps: float,
    target_bps: float,
    max_bars: int,
):
    if side == "LONG":
        stop = entry * (1.0 - stop_bps / 10000.0)
        target = entry * (1.0 + target_bps / 10000.0)
    else:
        stop = entry * (1.0 + stop_bps / 10000.0)
        target = entry * (1.0 - target_bps / 10000.0)
    last_i = min(len(g) - 1, entry_i + max_bars - 1)
    for j in range(entry_i, last_i + 1):
        r = g.iloc[j]
        hit_stop = r.low <= stop if side == "LONG" else r.high >= stop
        hit_target = r.high >= target if side == "LONG" else r.low <= target
        if hit_stop and hit_target:
            return stop, int(r.minute), "stop_same_bar_conservative"
        if hit_stop:
            return stop, int(r.minute), "stop"
        if hit_target:
            return target, int(r.minute), "target"
    r = g.iloc[last_i]
    return float(r.close), int(r.minute), "time"


trades: list[dict] = []


def add_trade(
    strategy: str,
    d: date,
    symbol: str,
    side: str,
    entry: float,
    exit_price: float,
    entry_minute: int,
    exit_minute: int,
    reason: str,
    gross_override: float | None = None,
):
    gross = float(gross_override if gross_override is not None else side_return(side, entry, exit_price) * 10000.0)
    short_surcharge = 2.0 if side == "SHORT" else 1.0 if side == "LONG_SHORT" else 0.0
    row = {
        "strategy": strategy,
        "date": d,
        "phase": phase_for(d),
        "symbol": symbol,
        "side": side,
        "entry": float(entry),
        "exit": float(exit_price),
        "entry_minute_et": int(entry_minute),
        "exit_minute_et": int(exit_minute),
        "exit_reason": reason,
        "gross_bps": gross,
        "short_surcharge_bps": short_surcharge,
    }
    for c in COSTS:
        row[f"net{int(c)}_bps"] = gross - c - short_surcharge
    trades.append(row)


print("=" * 100)
print("KEFKA STRATEGY TOURNAMENT V1 - 8 DISTINCT DAY-TRADING HYPOTHESES")
print("broker_mutation=False paper_orders_submitted=False")
print("source=Alpaca SIP 1m; research bars=5m aggregated in PostgreSQL")
print("fills=next 5m open; exits=conservative stop-first when stop and target share a bar")
print("cost_stress_round_trip_bps=5,10,25 plus short surcharge")
print("=" * 100)

bars_sql = """
WITH b AS (
    SELECT
        symbol,
        date_bin('5 minutes', timestamp, TIMESTAMPTZ '2000-01-01 00:00:00+00') AS ts,
        (array_agg(open ORDER BY timestamp))[1]::float8 AS open,
        max(high)::float8 AS high,
        min(low)::float8 AS low,
        (array_agg(close ORDER BY timestamp DESC))[1]::float8 AS close,
        sum(volume)::float8 AS volume,
        count(*) AS minute_rows
    FROM candles
    WHERE source = %s
      AND timeframe = '1m'
      AND symbol = ANY(%s)
      AND timestamp >= %s::timestamptz
      AND timestamp < %s::timestamptz
      AND (timestamp AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
      AND (timestamp AT TIME ZONE 'America/New_York')::time < TIME '16:00'
    GROUP BY symbol, ts
)
SELECT * FROM b ORDER BY symbol, ts
"""

news_sql = """
SELECT DISTINCT ON (provider, article_id, symbol)
       provider, article_id, symbol, known_at, headline
FROM intraday_news_articles
WHERE symbol = ANY(%s)
  AND known_at >= %s::timestamptz
  AND known_at < %s::timestamptz
ORDER BY provider, article_id, symbol, known_at
"""

def read_frame(conn, sql: str, params: tuple) -> pd.DataFrame:
    """Read psycopg rows without pandas misreading dict-row keys as data."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        columns = [col.name for col in cur.description]
    return pd.DataFrame(rows, columns=columns)


with connect() as conn:
    bars = read_frame(conn, bars_sql, (SOURCE, SYMBOLS, START, END_EXCLUSIVE))
    news = read_frame(conn, news_sql, (EQUITIES, START, END_EXCLUSIVE))

if bars.empty:
    raise SystemExit("No Alpaca SIP 1m bars found")

bars["ts"] = pd.to_datetime(bars["ts"], utc=True).dt.tz_convert("America/New_York")
bars["date"] = bars["ts"].dt.date
bars["minute"] = bars["ts"].dt.hour * 60 + bars["ts"].dt.minute
for c in ("open", "high", "low", "close", "volume"):
    bars[c] = pd.to_numeric(bars[c], errors="coerce")
bars = bars.dropna(subset=["open", "high", "low", "close", "volume"])
bars = bars.loc[(bars.open > 0) & (bars.high >= bars.low) & (bars.volume >= 0)].copy()

# Keep full regular sessions only; early closes and badly incomplete sessions are excluded.
session_counts = bars.groupby(["symbol", "date"]).size()
valid_sessions = set(session_counts.loc[session_counts >= 72].index)
bars = bars.loc[[x in valid_sessions for x in zip(bars.symbol, bars.date)]].copy()

news_events: dict[tuple[str, date], list[tuple[int, int, str]]] = defaultdict(list)
if not news.empty:
    news["known_at"] = pd.to_datetime(news["known_at"], utc=True).dt.tz_convert("America/New_York")
    for r in news.itertuples(index=False):
        score = headline_score(r.headline)
        if score:
            m = int(r.known_at.hour * 60 + r.known_at.minute)
            news_events[(r.symbol, r.known_at.date())].append((m, score, r.headline))

print(f"five_minute_rows={len(bars):,} full_sessions={len(valid_sessions):,} symbols={bars.symbol.nunique()}")
print(f"range={bars.date.min()}..{bars.date.max()} scored_news_events={sum(map(len, news_events.values())):,}")

# SPY reference values used by market-adjusted reversal.
spy_mid: dict[date, float] = {}
for d, g in bars.loc[bars.symbol.eq("SPY")].groupby("date", sort=True):
    g = g.sort_values("minute")
    a, b = getrow(g, 630), getrow(g, 655)
    if a is not None and b is not None:
        spy_mid[d] = float(b.close / a.open - 1.0)

xsmom_candidates: list[dict] = []
snapshots: dict[tuple[str, date], dict] = {}

for symbol, sg in bars.groupby("symbol", sort=True):
    slot20 = deque(maxlen=20)
    open_ranges = deque(maxlen=20)
    first_hour_volumes = deque(maxlen=20)
    residual_volumes = deque(maxlen=20)
    prev_close = None

    for d, g in sg.groupby("date", sort=True):
        g = g.sort_values("minute").reset_index(drop=True)
        r930, r955 = getrow(g, 570), getrow(g, 595)
        r1000, r1025 = getrow(g, 600), getrow(g, 625)
        r1030, r1055 = getrow(g, 630), getrow(g, 655)
        r1100, r1155 = getrow(g, 660), getrow(g, 715)
        r1200, r1555 = getrow(g, 720), getrow(g, 955)
        if any(x is None for x in (r930, r955, r1000, r1025, r1030, r1055, r1100, r1155, r1200, r1555)):
            prev_close = float(g.iloc[-1].close)
            continue

        typical = (g.high + g.low + g.close) / 3.0
        cumv = g.volume.cumsum().replace(0, np.nan)
        g["vwap"] = (typical * g.volume).cumsum() / cumv
        first_hour = g.loc[g.minute.between(570, 625)]
        mid_window = g.loc[g.minute.between(630, 655)]
        opening_range = float(first_hour.high.max() / first_hour.low.min() - 1.0)
        first_hour_volume = float(first_hour.volume.sum())
        residual_volume = float(mid_window.volume.sum())
        slot_return = float(r1025.close / r1000.open - 1.0)

        snapshots[(symbol, d)] = {
            "p0955": float(r955.close),
            "p1000": float(r1000.open),
            "p1555": float(r1555.close),
            "close": float(r1555.close),
        }

        # 1. LAST-HALF-HOUR ECHO: prior close to first half-hour predicts last half-hour.
        if symbol in ETFS and prev_close and prev_close > 0:
            early_bps = (float(r955.close) / prev_close - 1.0) * 10000.0
            if abs(early_bps) >= 35.0:
                side = "LONG" if early_bps > 0 else "SHORT"
                r1530 = getrow(g, 930)
                if r1530 is not None:
                    add_trade("MIM_ECHO30", d, symbol, side, r1530.open,
                              r1555.close, 930, 955, "last_half_hour")

        # 2. SAME-CLOCK SEASONALITY: trailing 20 same half-hour returns, no current-day input.
        if len(slot20) == 20:
            arr = np.asarray(slot20, dtype=float)
            mu = float(arr.mean())
            sd = float(arr.std(ddof=1))
            score_t = mu / (sd / math.sqrt(len(arr))) if sd > 0 else 0.0
            if abs(mu) * 10000.0 >= 5.0 and abs(score_t) >= 1.5:
                side = "LONG" if mu > 0 else "SHORT"
                add_trade("SLOT_ECHO_20D", d, symbol, side, r1000.open, r1025.close, 600, 625, "same_clock_20d")

        # 3. Cross-sectional noon momentum candidates, ranked after all symbols are processed.
        if symbol not in ETFS:
            xsmom_candidates.append({
                "date": d,
                "symbol": symbol,
                "signal": float(r1155.close / r930.open - 1.0),
                "entry": float(r1200.open),
                "exit": float(r1555.close),
            })

        # 4. Market-adjusted liquidity-shock reversal, excluding known-news shocks.
        if symbol not in ETFS and d in spy_mid and len(residual_volumes) == 20:
            stock_mid = float(r1055.close / r1030.open - 1.0)
            residual_bps = (stock_mid - spy_mid[d]) * 10000.0
            vol_ratio = residual_volume / max(float(np.median(residual_volumes)), 1.0)
            known_news = any(m <= 660 for m, _, _ in news_events.get((symbol, d), []))
            if abs(residual_bps) >= 80.0 and vol_ratio >= 1.5 and not known_news:
                side = "SHORT" if residual_bps > 0 else "LONG"
                add_trade("LIQUIDITY_SHOCK_REVERSAL", d, symbol, side, r1100.open, r1155.close,
                          660, 715, "one_hour_residual_snapback")

        # 5. VWAP pullback continuation after a strong, high-volume first hour.
        if symbol not in ETFS and len(first_hour_volumes) == 20:
            trend_bps = (float(r1025.close) / float(r930.open) - 1.0) * 10000.0
            high_rvol = first_hour_volume >= 1.2 * float(np.median(first_hour_volumes))
            direction = 1 if trend_bps >= 60.0 else -1 if trend_bps <= -60.0 else 0
            if direction and high_rvol:
                search = g.index[g.minute.between(630, 780)].tolist()
                signal_i = None
                for i in search:
                    r = g.iloc[i]
                    if not np.isfinite(r.vwap):
                        continue
                    if direction > 0 and r.low <= r.vwap * 1.001 and r.low >= r.vwap * 0.998 and r.close > r.vwap:
                        signal_i = i
                        break
                    if direction < 0 and r.high >= r.vwap * 0.999 and r.high <= r.vwap * 1.002 and r.close < r.vwap:
                        signal_i = i
                        break
                if signal_i is not None and signal_i + 1 < len(g):
                    entry_i = signal_i + 1
                    entry_row = g.iloc[entry_i]
                    side = "LONG" if direction > 0 else "SHORT"
                    px, xm, why = simulate_exit(g, entry_i, side, float(entry_row.open), 60.0, 120.0, 24)
                    add_trade("VWAP_TREND_PULLBACK", d, symbol, side, entry_row.open, px,
                              int(entry_row.minute), xm, why)

        # 6. Volatility-compression breakout; current range is judged only against prior days.
        if symbol not in ETFS and len(open_ranges) == 20:
            hist_med = float(np.median(open_ranges))
            compressed = 0.0002 <= opening_range <= 0.65 * hist_med
            if compressed:
                or_high = float(first_hour.high.max())
                or_low = float(first_hour.low.min())
                base_bar_volume = max(first_hour_volume / max(len(first_hour), 1), 1.0)
                signal_i = None
                direction = 0
                for i in g.index[g.minute.between(630, 840)].tolist():
                    r = g.iloc[i]
                    if r.volume < 1.5 * base_bar_volume:
                        continue
                    if r.close >= or_high * 1.0005:
                        signal_i, direction = i, 1
                        break
                    if r.close <= or_low * 0.9995:
                        signal_i, direction = i, -1
                        break
                if signal_i is not None and signal_i + 1 < len(g):
                    entry_i = signal_i + 1
                    entry_row = g.iloc[entry_i]
                    side = "LONG" if direction > 0 else "SHORT"
                    stop_bps = max(40.0, opening_range * 10000.0 * 0.75)
                    target_bps = max(80.0, opening_range * 10000.0 * 1.50)
                    px, xm, why = simulate_exit(g, entry_i, side, float(entry_row.open), stop_bps, target_bps, 30)
                    add_trade("COMPRESSION_BREAKOUT", d, symbol, side, entry_row.open, px,
                              int(entry_row.minute), xm, why)

        # 7. Point-in-time catalyst drift: lexicon supplies direction; price must confirm for 10m.
        if symbol not in ETFS:
            usable = [(m, s, h) for m, s, h in news_events.get((symbol, d), []) if 575 <= m <= 870]
            if usable:
                m, score, _ = sorted(usable, key=lambda x: x[0])[0]
                start_i = next((i for i, bm in enumerate(g.minute) if bm >= int(math.ceil(m / 5.0) * 5)), None)
                if start_i is not None and start_i + 2 < len(g):
                    start_row = g.iloc[start_i]
                    confirm_row = g.iloc[start_i + 1]
                    confirm_bps = (float(confirm_row.close) / float(start_row.open) - 1.0) * 10000.0
                    if score * confirm_bps >= 25.0:
                        entry_i = start_i + 2
                        entry_row = g.iloc[entry_i]
                        side = "LONG" if score > 0 else "SHORT"
                        px, xm, why = simulate_exit(g, entry_i, side, float(entry_row.open), 80.0, 140.0, 12)
                        add_trade("CATALYST_NEWS_DRIFT", d, symbol, side, entry_row.open, px,
                                  int(entry_row.minute), xm, why)

        slot20.append(slot_return)
        open_ranges.append(opening_range)
        first_hour_volumes.append(first_hour_volume)
        residual_volumes.append(residual_volume)
        prev_close = float(r1555.close)

# 8. Cross-sectional noon momentum: equal-weight top 2 / bottom 2.
xsc = pd.DataFrame(xsmom_candidates)
if not xsc.empty:
    for d, g in xsc.groupby("date", sort=True):
        if len(g) < 8:
            continue
        ranked = g.sort_values("signal")
        for r in ranked.head(2).itertuples(index=False):
            add_trade("XSECTION_NOON_MOMENTUM", d, r.symbol, "SHORT", r.entry, r.exit, 720, 955, "bottom_two")
        for r in ranked.tail(2).itertuples(index=False):
            add_trade("XSECTION_NOON_MOMENTUM", d, r.symbol, "LONG", r.entry, r.exit, 720, 955, "top_two")

# 8. Sector-pairs convergence using a rolling 60-session hedge ratio.
for ysym, xsym in PAIRS:
    common = sorted({d for s, d in snapshots if s == ysym} & {d for s, d in snapshots if s == xsym})
    for i in range(60, len(common)):
        d = common[i]
        hist = common[i - 60:i]
        ly = np.log([snapshots[(ysym, hd)]["close"] for hd in hist])
        lx = np.log([snapshots[(xsym, hd)]["close"] for hd in hist])
        beta, alpha = np.polyfit(lx, ly, 1)
        if not (0.3 <= beta <= 3.0):
            continue
        residuals = ly - (alpha + beta * lx)
        sd = float(residuals.std(ddof=1))
        if sd <= 0:
            continue
        cy = math.log(snapshots[(ysym, d)]["p0955"])
        cx = math.log(snapshots[(xsym, d)]["p0955"])
        z = (cy - (alpha + beta * cx) - float(residuals.mean())) / sd
        if abs(z) < 2.0:
            continue
        sy = snapshots[(ysym, d)]
        sx = snapshots[(xsym, d)]
        ry = sy["p1555"] / sy["p1000"] - 1.0
        rx = sx["p1555"] / sx["p1000"] - 1.0
        gross = ((-ry + beta * rx) if z > 0 else (ry - beta * rx)) / (1.0 + beta) * 10000.0
        add_trade("SECTOR_PAIRS_CONVERGENCE", d, f"{ysym}/{xsym}", "LONG_SHORT", 1.0, 1.0,
                  600, 955, "zscore_convergence", gross_override=float(gross))

tdf = pd.DataFrame(trades)
if tdf.empty:
    raise SystemExit("No strategy generated any trades")
tdf = tdf.sort_values(["strategy", "date", "symbol"]).reset_index(drop=True)
tdf.to_csv(OUT / "all_trades.csv", index=False)

all_dates = sorted(bars.loc[bars.symbol.eq("SPY"), "date"].unique())


def hac_t(x: np.ndarray, lag: int = 5) -> float:
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 3:
        return float("nan")
    u = x - x.mean()
    gamma0 = float(u @ u / n)
    lrv = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        gamma = float(u[k:] @ u[:-k] / n)
        lrv += 2.0 * (1.0 - k / (lag + 1.0)) * gamma
    se = math.sqrt(max(lrv, 0.0) / n)
    return float(x.mean() / se) if se > 0 else float("nan")


def block_ci(x: np.ndarray, block: int = 5, reps: int = 1500):
    x = np.asarray(x, dtype=float)
    if len(x) < block:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(SEED + len(x))
    vals = np.empty(reps)
    starts = np.arange(0, len(x) - block + 1)
    need = int(math.ceil(len(x) / block))
    for j in range(reps):
        chunks = [x[s:s + block] for s in rng.choice(starts, size=need, replace=True)]
        vals[j] = np.concatenate(chunks)[:len(x)].mean()
    return tuple(map(float, np.quantile(vals, [0.025, 0.975])))


def summarize(strategy: str, phase: str, g: pd.DataFrame) -> dict:
    phase_dates = [d for d in all_dates if phase_for(d) == phase]
    daily = g.groupby("date")["net10_bps"].mean().reindex(phase_dates, fill_value=0.0)
    x = g.net10_bps.to_numpy(dtype=float)
    winners = x[x > 0]
    losers = x[x <= 0]
    lo, hi = block_ci(daily.to_numpy(dtype=float))
    p_normal = math.erfc(abs(hac_t(daily.to_numpy(dtype=float))) / math.sqrt(2.0)) if len(daily) else float("nan")
    equity = (1.0 + daily / 10000.0).cumprod()
    dd = equity / equity.cummax() - 1.0
    return {
        "strategy": strategy,
        "phase": phase,
        "trades": int(len(g)),
        "active_days": int(g.date.nunique()),
        "calendar_days": int(len(phase_dates)),
        "gross_bps": float(g.gross_bps.mean()) if len(g) else None,
        "net5_bps": float(g.net5_bps.mean()) if len(g) else None,
        "net10_bps": float(g.net10_bps.mean()) if len(g) else None,
        "net25_bps": float(g.net25_bps.mean()) if len(g) else None,
        "win_rate_net10": float((x > 0).mean()) if len(x) else None,
        "profit_factor_net10": float(winners.sum() / abs(losers.sum())) if len(winners) and len(losers) and abs(losers.sum()) > 0 else None,
        "hac_t_net10": hac_t(daily.to_numpy(dtype=float)),
        "normal_p_net10": p_normal,
        "bonferroni_p_8_families": min(1.0, p_normal * 8.0) if np.isfinite(p_normal) else None,
        "block_bootstrap_95_lo_daily_net10": lo,
        "block_bootstrap_95_hi_daily_net10": hi,
        "annualized_return_net10": float(equity.iloc[-1] ** (252.0 / len(equity)) - 1.0) if len(equity) else None,
        "max_drawdown_net10": float(dd.min()) if len(dd) else None,
    }


summary_rows = []
for strategy in sorted(tdf.strategy.unique()):
    for phase in ("discovery", "validation", "test"):
        summary_rows.append(summarize(strategy, phase, tdf.loc[(tdf.strategy == strategy) & (tdf.phase == phase)]))
summary = pd.DataFrame(summary_rows)

verdicts = {}
for strategy in sorted(tdf.strategy.unique()):
    v = summary.loc[(summary.strategy == strategy) & (summary.phase == "validation")].iloc[0]
    t = summary.loc[(summary.strategy == strategy) & (summary.phase == "test")].iloc[0]
    enough = int(t.trades) >= MIN_TEST_TRADES and int(t.active_days) >= 25
    survives = enough and (t.net10_bps or -1e9) > 0 and (t.net25_bps or -1e9) > 0
    stable = (v.net10_bps or -1e9) > 0 and (t.profit_factor_net10 or 0) >= 1.20
    strong = survives and stable and (t.hac_t_net10 or -1e9) >= 3.0 and (t.bonferroni_p_8_families or 1.0) <= 0.05
    promising = survives and stable and (t.hac_t_net10 or -1e9) >= 1.5
    verdicts[strategy] = "RESEARCH_PASS_NOT_DEPLOY" if strong else "PROMISING_MORE_CONFIRMATION" if promising else "REJECT_OR_INCONCLUSIVE"

summary["verdict"] = summary.strategy.map(verdicts)
summary.to_csv(OUT / "strategy_summary.csv", index=False)

result = {
    "name": "kefka_strategy_tournament_v1",
    "broker_mutation": False,
    "paper_orders_submitted": False,
    "source": "alpaca_sip_1m_aggregated_to_5m",
    "splits": {
        "discovery": f"{START}..{DISCOVERY_END}",
        "validation": f"2025-04-01..{VALIDATION_END}",
        "test": "2026-01-01..2026-08-30",
    },
    "costs_round_trip_bps": list(COSTS),
    "family_count": int(tdf.strategy.nunique()),
    "verdicts": verdicts,
}
(OUT / "result.json").write_text(json.dumps(result, indent=2, default=str))

cols = [
    "strategy", "phase", "trades", "active_days", "gross_bps", "net10_bps", "net25_bps",
    "win_rate_net10", "profit_factor_net10", "hac_t_net10", "bonferroni_p_8_families",
    "annualized_return_net10", "max_drawdown_net10", "verdict",
]
print("\nRESULTS")
print(summary[cols].to_string(index=False))
print("\nVERDICTS")
print(json.dumps(verdicts, indent=2))
print("\nNo Alpaca orders were submitted.")
print("RESULT_JSON=" + json.dumps(result, default=str))
