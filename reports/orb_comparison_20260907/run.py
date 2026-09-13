"""Standalone, read-only Alpaca data download and bounded ORB comparison.

No broker client, orders, database mutations, parameter search, or promotion.
Run from repository root with .venv/Scripts/python reports/orb_comparison_20260907/run.py.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

import exchange_calendars as xcals
import httpx
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/api"))
from app.settings import settings

OUT = Path(__file__).resolve().parent
SYMBOLS = sorted("AAPL MSFT NVDA TSLA AMZN META GOOGL AMD NFLX AVGO ORCL CRM INTC MU COIN BA JPM BAC DIS WMT TGT F GM XOM CVX COP SLB CAT DE GE HON UPS UNP LMT RTX COST HD LOW NKE SBUX MCD PEP KO PG JNJ MRK ABBV UNH CVS WFC".split())
START = "2024-11-01"
TEST_START = "2025-01-02"
END = "2026-09-01"
PAPER = "https://concretumgroup.com/wp-content/uploads/2026/02/A-Profitable-Day-Trading-Strategy-For-The-U.S.-Equity-Market.pdf"
_lock = threading.Lock()
_last_request = 0.0
OFFLINE = False
BAR_MINUTES = 5


def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, allow_nan=False, default=str), encoding="utf-8")


def fetch(symbol, timeframe, adjustment):
    cache = OUT / "data" / f"{symbol}_{timeframe}_{adjustment}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    if OFFLINE:
        raise FileNotFoundError(f"Offline cache missing: {cache.name}")
    global _last_request
    params = dict(symbols=symbol, timeframe=timeframe, start=START + "T00:00:00Z",
                  end=END + "T00:00:00Z", feed="sip", adjustment=adjustment,
                  asof="-", limit=10000, sort="asc")
    headers = {"APCA-API-KEY-ID": settings.alpaca_api_key,
               "APCA-API-SECRET-KEY": settings.alpaca_api_secret}
    bars = []
    with httpx.Client(base_url=settings.alpaca_data_base_url, headers=headers, timeout=45) as client:
        while True:
            for attempt in range(4):
                with _lock:
                    time.sleep(max(0, .36 - (time.monotonic() - _last_request)))
                    _last_request = time.monotonic()
                response = client.get("/v2/stocks/bars", params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    time.sleep(2 ** (attempt + 1))
                    continue
                response.raise_for_status()
                break
            else:
                raise RuntimeError(f"Data request failed for {symbol}: HTTP {response.status_code}")
            body = response.json()
            bars.extend(body.get("bars", {}).get(symbol, []))
            token = body.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
    if not bars:
        raise ValueError(f"No {timeframe} bars: {symbol}")
    frame = pd.DataFrame(bars).rename(columns=dict(t="timestamp", o="open", h="high", l="low", c="close", v="volume"))
    frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True)
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    if frame.timestamp.duplicated().any():
        raise ValueError(f"Duplicate timestamps: {symbol}")
    vals = frame[["open", "high", "low", "close", "volume"]]
    if not np.isfinite(vals.to_numpy()).all() or (vals.iloc[:, :4] <= 0).any().any() or (frame.volume < 0).any():
        raise ValueError(f"Invalid values: {symbol}")
    if ((frame.high < frame[["open", "close", "low"]].max(axis=1)) | (frame.low > frame[["open", "close"]].min(axis=1))).any():
        raise ValueError(f"Invalid OHLC geometry: {symbol}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(cache, index=False)
    return frame


def download_symbol(symbol):
    for tf, adj in [(f"{BAR_MINUTES}Min", "raw"), ("1Day", "raw"), ("1Day", "split")]:
        fetch(symbol, tf, adj)
    print(json.dumps({"downloaded": symbol}), flush=True)


def calendar():
    cal = xcals.get_calendar("XNYS", start=START, end=END)
    return {d.strftime("%Y-%m-%d"): (cal.session_open(d), cal.session_close(d))
            for d in cal.sessions_in_range(START, "2026-08-31")}


def prepare_symbol(symbol, sessions):
    intra = fetch(symbol, f"{BAR_MINUTES}Min", "raw")
    daily = fetch(symbol, "1Day", "raw").set_index("timestamp")
    split = fetch(symbol, "1Day", "split").set_index("timestamp")
    if not daily.index.equals(split.index):
        raise ValueError(f"Raw/split daily key mismatch: {symbol}")
    daily["factor"] = daily.close / split.close
    daily["date"] = daily.index.tz_convert("America/New_York").strftime("%Y-%m-%d")
    daily = daily.set_index("date").reindex(list(sessions))
    intra["date"] = intra.timestamp.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    groups = dict(tuple(intra.groupby("date", sort=True)))
    reference_groups = {}
    if BAR_MINUTES == 1:
        reference = pd.read_parquet(Path(__file__).resolve().parent / 'data' / f'{symbol}_5Min_raw.parquet')
        reference['date'] = reference.timestamp.dt.tz_convert('America/New_York').dt.strftime('%Y-%m-%d')
        reference_groups = dict(tuple(reference.groupby('date', sort=True)))
    opening = {}
    complete = {}
    issues = []
    for date, (op, cl) in sessions.items():
        g = groups.get(date)
        if g is None:
            issues.append({"symbol": symbol, "date": date, "reason": "missing_session"})
            continue
        g = g.loc[(g.timestamp >= op) & (g.timestamp < cl)].copy()
        expected = pd.date_range(op, cl, freq=f"{BAR_MINUTES}min", inclusive="left")
        if BAR_MINUTES == 1:
            ref = reference_groups.get(date)
            if ref is None:
                issues.append(dict(symbol=symbol,date=date,reason='missing_5min_reference'))
                continue
            ref = ref.loc[(ref.timestamp>=op)&(ref.timestamp<cl)].set_index('timestamp')
            try:
                g, empty_count = reconcile_minutes(g, ref, expected)
            except ValueError:
                issues.append(dict(symbol=symbol,date=date,reason='minute_aggregate_mismatch'))
                continue
            if empty_count:
                issues.append(dict(symbol=symbol,date=date,reason='verified_no_trade_minutes',count=empty_count))
        opening_bars = 5 // BAR_MINUTES
        if len(g) >= opening_bars and pd.DatetimeIndex(g.timestamp.iloc[:opening_bars]).equals(expected[:opening_bars]):
            opening[date] = float(g.volume.iloc[:opening_bars].sum())
        if not pd.DatetimeIndex(g.timestamp).equals(expected):
            issues.append({"symbol": symbol, "date": date, "reason": "incomplete_session", "bars": len(g), "expected": len(expected)})
            continue
        complete[date] = g
    dates = list(sessions)
    candidates = []
    for i, date in enumerate(dates):
        if date < TEST_START or i < 15 or date not in complete:
            continue
        current = daily.iloc[i]
        hist = daily.iloc[i-15:i].copy()
        prior_dates = dates[i-14:i]
        if hist[["open", "high", "low", "close", "volume", "factor"]].isna().any().any() or not np.isfinite(current.factor):
            issues.append({"symbol": symbol, "date": date, "reason": "missing_daily_history"})
            continue
        if any(d not in opening for d in prior_dates):
            issues.append({"symbol": symbol, "date": date, "reason": "missing_opening_history"})
            continue
        # Put historical bars in the current session's share units. Future split
        # factors cancel in these ratios; current-day returns/volume are not features.
        scale = current.factor / hist.factor.to_numpy()
        h = hist.high.to_numpy() * scale
        l = hist.low.to_numpy() * scale
        c = hist.close.to_numpy() * scale
        tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
        atr = float(tr.mean())
        adv = float((hist.volume.to_numpy()[1:] / scale[1:]).mean())
        old_or_vol = np.array([opening[d] for d in prior_dates]) / scale[1:]
        if old_or_vol.mean() <= 0:
            continue
        g = complete[date]
        opening_frame = g.iloc[:5 // BAR_MINUTES]
        opening_price = float(opening_frame.open.iloc[0])
        rvol = float(opening_frame.volume.sum() / old_or_vol.mean())
        side = int(np.sign(opening_frame.close.iloc[-1] - opening_price))
        if opening_price <= 5 or adv < 1_000_000 or atr <= .50 or side == 0:
            continue
        candidates.append(dict(symbol=symbol, date=date, side=side, atr=atr,
                               rvol=rvol, trigger=float(opening_frame.high.max() if side == 1 else opening_frame.low.min())))
    return candidates, complete, issues


def reconcile_minutes(g, ref, expected):
    """Accept absent one-minute bars only when aggregate OHLCV matches 5Min.

    Explicit no-trade slots carry the last price and zero volume; they cannot
    trigger an entry/stop. This is not a silent repair of unmatched market data.
    """
    cols=['open','high','low','close','volume']
    indexed=g.set_index('timestamp')
    agg=indexed.resample('5min').agg(dict(open='first',high='max',low='min',close='last',volume='sum'))
    if not agg.index.equals(ref.index) or not np.allclose(agg[cols].to_numpy(float),ref[cols].to_numpy(float),rtol=0,atol=1e-6):
        raise ValueError('One-minute OHLCV does not reconcile to five-minute source')
    dense=indexed.reindex(expected)
    missing=dense.open.isna()
    if missing.any():
        prices=dense.close.ffill().fillna(float(indexed.open.iloc[0]))
        for c in cols[:4]:
            dense.loc[missing,c]=prices[missing]
        dense.loc[missing,'volume']=0.
    dense.index.name='timestamp'
    return dense.reset_index(),int(missing.sum())


def simulate_trade(bars, side, trigger, distance, sleeve, leverage, bps, optimistic_intrabar=False):
    """Preplaced stop entry after opening bar; adverse entry-bar stop ordering.

    One entry/day. Strict trade-through. Fill gaps at the open. Slippage/spread
    modeled as adverse bps each side; no limit queue assumption.
    """
    a = bars if isinstance(bars, np.ndarray) else bars[["open", "high", "low", "close", "volume"]].to_numpy(float)
    for k in range(5 // BAR_MINUTES, len(a)):
        op, hi, lo, close, vol = a[k]
        if vol <= 0:
            continue
        if not (hi > trigger if side == 1 else lo < trigger):
            continue
        mid = max(op, trigger) if side == 1 else min(op, trigger)
        entry = mid * (1 + side * bps / 10000)
        qty = int(min(sleeve * .01 / distance, sleeve * leverage / entry))
        if qty < 1:
            return None
        stop = entry - side * distance
        reason = "session_close"
        exit_mid = a[-1, 3]
        end = len(a) - 1
        for j in range(k, len(a)):
            o, h, l, c, v = a[j]
            if v <= 0:
                continue
            if optimistic_intrabar and j == k and side * (op - trigger) < 0:
                # Diagnostic only: pretend adverse entry-bar excursion preceded
                # the intrabar breakout. Never ignore stops after gap/open entries.
                continue
            if l <= stop if side == 1 else h >= stop:
                # Entry-bar open occurred before intrabar entry; cannot reuse it
                # as a later gap stop. Later-bar gap stops use adverse open.
                exit_mid = stop if j == k else (min(o, stop) if side == 1 else max(o, stop))
                end = j
                reason = "ambiguous_entry_bar_stop" if j == k else "stop"
                break
        exit_price = exit_mid * (1 - side * bps / 10000)
        commission = 2 * max(.35, .0035 * qty)
        net = side * qty * (exit_price - entry) - commission
        # Intraday mark-to-market at bar closes; not tick-level worst drawdown.
        path = np.zeros(len(a))
        path[k:end] = side * qty * (a[k:end, 3] - entry) - commission / 2
        path[end:] = net
        return dict(entry_bar=k, exit_bar=end, entry_price=entry, exit_price=exit_price,
                    shares=qty, net_pnl=net, commissions=commission, exit_reason=reason,
                    entry_bar_volume=float(a[k, 4]), participation=float(qty / max(a[k, 4], 1)),
                    path=path)
    return None


def evaluate(candidates, groups, valid_dates, sessions, capital, leverage, bps, direction, variant):
    equity = capital
    peak = capital
    mdd = 0.
    trades = []
    days = []
    for date in valid_dates:
        pool = candidates.get(date, [])
        if variant == "top20_rvol":
            pool = sorted([p for p in pool if p["rvol"] >= 1], key=lambda p: (-p["rvol"], p["symbol"]))[:20]
        # Rank both directions first. Long-only sensitivity retains cash in
        # short sleeves, so it does not silently re-rank/lever the long subset.
        sleeve = equity / max(len(pool), 1)
        n = int((sessions[date][1] - sessions[date][0]).total_seconds() / (60 * BAR_MINUTES))
        daypath = np.zeros(n)
        opened = 0
        start_equity = equity
        for p in pool:
            if direction == "long_only" and p["side"] < 0:
                continue
            trade = simulate_trade(groups[p["symbol"]][date], p["side"], p["trigger"],
                                   .1 * p["atr"], sleeve, leverage, bps)
            if trade is None:
                continue
            daypath += trade.pop("path")
            opened += 1
            trades.append({**p, **trade})
        curve = start_equity + daypath
        for value in curve:
            peak = max(peak, value)
            mdd = max(mdd, 1 - value / peak)
        equity = float(curve[-1])
        days.append(dict(date=date, eligible_or_selected=len(pool), trades=opened,
                         start_equity=start_equity, net_pnl=equity-start_equity, equity=equity))
        if equity <= 0:
            break
    pnl = np.array([t["net_pnl"] for t in trades])
    frame = pd.DataFrame(days)
    monthly = frame.groupby(frame.date.str[:7]).net_pnl.sum()
    losses = float(-pnl[pnl < 0].sum())
    years = (pd.Timestamp(valid_dates[-1]) - pd.Timestamp(valid_dates[0])).days / 365.25
    return dict(capital=capital, leverage=leverage, adverse_bps_per_side=bps,
                direction=direction, variant=variant, ending_equity=equity,
                net_profit=equity-capital, return_pct=100*(equity/capital-1),
                annualized_pct=100*((equity/capital)**(1/years)-1) if equity > 0 else -100,
                max_5min_close_drawdown_pct=100*mdd, trades=len(trades),
                win_rate_pct=float(100*(pnl>0).mean()) if len(pnl) else 0,
                profit_factor=float(pnl[pnl>0].sum()/losses) if losses else None,
                expectancy_dollars=float(pnl.mean()) if len(pnl) else 0,
                months=len(monthly), positive_months=int((monthly>0).sum()),
                months_at_least_1000=int((monthly>=1000).sum()),
                months_at_least_2000=int((monthly>=2000).sum()),
                worst_month=float(monthly.min()), best_month=float(monthly.max()),
                average_month=float(monthly.mean()), monthly=monthly.to_dict(),
                large_participation_trades=sum(t["participation"]>.01 for t in trades),
                ambiguous_entry_bar_stops=sum(t["exit_reason"]=="ambiguous_entry_bar_stop" for t in trades),
                daily=days, trade_log=trades)


def main():
    global OFFLINE, BAR_MINUTES, START, TEST_START, OUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--minute-check", action="store_true", help="Separate June-August 2026 pilot with one-minute execution bars")
    args = parser.parse_args()
    OFFLINE = args.offline
    if args.minute_check:
        BAR_MINUTES = 1
        START = "2026-05-01"
        TEST_START = "2026-06-01"
        OUT = OUT / "minute_check"
    OUT.mkdir(exist_ok=True)
    manifest = dict(created_at=datetime.now(timezone.utc).isoformat(), symbols=SYMBOLS,
                    warmup_start=START, test_start=TEST_START, end_exclusive=END,
                    feed="sip", timeframe=f"{BAR_MINUTES}Min", paper=PAPER, universe="fixed_50_current_names_not_point_in_time",
                    status="running", real_money=False, rule_search=False)
    save_json(OUT / "manifest.json", manifest)
    if not args.offline:
        with ThreadPoolExecutor(max_workers=4) as executor:
            for future in as_completed([executor.submit(download_symbol, s) for s in SYMBOLS]):
                future.result()
    if args.download_only:
        return
    sessions = calendar()
    candidates = {}
    groups = {}
    issues = []
    valid_dates = {d for d in sessions if d >= TEST_START}
    for symbol in SYMBOLS:
        rows, complete, failures = prepare_symbol(symbol, sessions)
        groups[symbol] = {d:g[["open", "high", "low", "close", "volume"]].to_numpy(float) for d,g in complete.items()}
        issues.extend(failures)
        valid_dates &= set(complete)
        for row in rows:
            candidates.setdefault(row["date"], []).append(row)
        print(json.dumps({"prepared": symbol, "candidates": len(rows), "quality_issues": len(failures)}), flush=True)
    # Do not remove individual tickers based on future session completeness,
    # which would change the morning ranking. Exclude whole dates for all arms.
    dates_with_feature_failures = {x["date"] for x in issues if x["reason"] in {"missing_daily_history", "missing_opening_history"}}
    valid_dates = sorted(valid_dates - dates_with_feature_failures)
    if len(valid_dates) < (40 if args.minute_check else 60):
        raise ValueError(f"Insufficient common complete dates: {len(valid_dates)}")
    quality = dict(expected_test_sessions=sum(d>=TEST_START for d in sessions),
                   common_complete_test_sessions=len(valid_dates),
                   excluded_test_dates=sorted({d for d in sessions if d>=TEST_START}-set(valid_dates)),
                   issues=issues, data_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (OUT/"data").glob("*.parquet")})
    if BAR_MINUTES == 1:
        quality['five_minute_reference_hashes']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path(__file__).resolve().parent/'data').glob('*5Min_raw.parquet')}
    save_json(OUT/"quality.json", quality)
    results=[]
    for capital in (15000, 20000):
        for leverage in (1, 4):
            for bps in (0, 2.5, 5):
                for direction in ("both_hypothetical_borrow", "long_only"):
                    for variant in ("base", "top20_rvol"):
                        result=evaluate(candidates,groups,valid_dates,sessions,capital,leverage,bps,direction,variant)
                        key=f"{capital}_{leverage}x_{bps}bps_{direction}_{variant}"
                        pd.DataFrame(result.pop("trade_log")).to_csv(OUT/f"trades_{key}.csv",index=False)
                        pd.DataFrame(result.pop("daily")).to_csv(OUT/f"daily_{key}.csv",index=False)
                        results.append(result)
                        print(json.dumps(result,allow_nan=False),flush=True)
    save_json(OUT/"results.json",results)
    pd.DataFrame([{k:v for k,v in r.items() if k!="monthly"} for r in results]).to_csv(OUT/"summary.csv",index=False)
    manifest.update(status="complete",common_sessions=len(valid_dates),first_session=valid_dates[0],last_session=valid_dates[-1],scenarios=len(results),code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    save_json(OUT/"manifest.json",manifest)


if __name__ == "__main__":
    main()
