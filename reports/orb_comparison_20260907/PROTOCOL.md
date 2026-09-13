# Opening-range breakout comparison — frozen pilot protocol

Specified September 7, 2026, before inspecting portfolio outcomes.

## Question

On one fixed, limited universe, does adding opening relative-volume selection improve the same opening-range breakout strategy after modeled costs?

This is a bounded implementation/selection pilot, not a replication of the published 2016–2023 full-market result. It is historical exploratory evidence, not certified untouched out-of-sample evidence or a live recommendation.

## Source and data

- Strategy reference: [Zarattini, Barbon and Aziz](https://concretumgroup.com/wp-content/uploads/2026/02/A-Profitable-Day-Trading-Strategy-For-The-U.S.-Equity-Market.pdf).
- [Alpaca historical stock bars](https://docs.alpaca.markets/us/reference/stockbars): SIP, raw five-minute bars; raw and split-adjusted daily bars.
- Universe: 50 current, manually specified liquid US stock names, frozen in `run.py`. No claim of historical universe completeness, delisted-stock coverage or absence of survivorship/selection bias.
- Evaluation: January 2, 2025–August 31, 2026. Warmup starts November 1, 2024. This is a different period from the paper.
- Regular sessions follow the XNYS exchange calendar, including DST, holidays and early closes.
- All pagination tokens are consumed. Data are cached, keyed by symbol/timeframe/adjustment, and SHA-256 hashes recorded. Duplicate timestamps, invalid OHLC, nonfinite values and negative volume fail ingestion.
- A missing intraday bar excludes the entire date from both arms across all symbols, rather than retrospectively removing a stock from a morning ranking. Exclusions are reported. This creates a completeness-conditioned sample; excluded dates have unknown strategy outcomes.

## Rules and sizing

- Price above $5; previous 14-session average daily volume at least one million shares; previous 14-session mean true range above $0.50. Daily features use vendor daily OHLCV, not inferred intraday partial days.
- True range is the maximum of daily high-low, absolute high minus previous close, and absolute low minus previous close. ATR is its simple 14-session average.
- Historical price and volume units are normalized to the current session's share basis using ratios of raw/split daily close factors. Absolute future adjustment factors cancel. Current-session closing price, high, low and volume are not signal inputs.
- Set opening levels and direction only after the 09:30–09:35 ET bar ends. Bullish opening bar permits a long breakout, bearish permits a short, doji is skipped. Maximum one entry per stock per day.
- Basic arm: all stocks satisfying these rules. Filtered arm: relative opening volume at least 1, retaining up to 20 with the highest ratio. Denominator is the previous 14 opening intervals, normalized for splits. Ties use symbol order.
- Stop distance: 10% of prior daily ATR. No profit target. Exit remaining positions at session end.
- Equal capital sleeves across that morning's selected stocks. Risk per trade is 1% of its sleeve, capped by sleeve notional capacity. This is an explicit interpretation of the paper's capital allocation language, not a claim of independently verified exact portfolio replication.
- Test $15,000 and $20,000 initial equity, and 1x versus 4x entry-notional caps. Capital compounds; no deposits or withdrawals. Positions do not share/recycle unused sleeves intraday. No additional borrowing is permitted beyond the scenario's entry cap; broker margin/forced liquidation is not simulated.
- Long/short scenarios assume borrow availability and omit borrow/locate costs. They are hypothetical. Long-only sensitivity preserves unused short sleeves as cash, so it does not re-rank or increase the remaining long exposure.

## Execution and costs

- Stop entries require strict trade-through after the opening bar; touching a level alone is insufficient. A gap through entry fills at the adverse open.
- Entry and exit costs: 0, 2.5 and 5 basis points of adverse price movement per side, plus $0.0035/share commission with $0.35/order minimum. These are declared sensitivity assumptions, not measured execution calibration; exchange/regulatory fees, borrow and nonlinear market impact remain omitted.
- Stop gaps after entry fill at the adverse open. If a five-minute entry bar also crosses the stop, assume entry then stop, an adverse but potentially overly pessimistic ordering. Count these cases explicitly.
- Closing price of the final regular-session bar proxies a session-end fill, with adverse cost. It is not a verified closing-auction execution.
- Position sizes are integer shares. Entry-bar participation above 1% is flagged, not silently assumed capacity-validated.
- Drawdown is marked at five-minute bar closes with simulated exits, and includes opening capital. It is not a tick-level maximum drawdown or guaranteed loss bound. No 30% account stop is imposed in this diagnostic run.

## Decision outputs

Compare both arms under each identical assumption set. Retain trade and daily equity ledgers, monthly P&L, net profit, annualized return, drawdown, expectancy, trade count, profit factor, ambiguity counts and data exclusions. Display long/short and long-only separately. No tuning after observing results; any repaired execution defect is documented and the affected results rerun.

Even a positive result requires a broader point-in-time universe, finer execution evidence, actual account constraints, and prospective validation before funding. A negative result rejects this pilot implementation on its sample; it does not disprove the full-market paper.

## Execution-resolution follow-up

The initial five-minute runs showed a large number of same-entry-bar stops. Added a separate June–August 2026 one-minute execution replay, retaining five-minute opening signals and the same 50 symbols, rules and scenario grid. This is a diagnostics-driven follow-up, not part of the originally frozen period specification. One-minute data are reconciled to the independently downloaded five-minute OHLCV. Absent minute bars are accepted only after that match, marked as zero-volume no-trade intervals, and cannot trigger entry/exit; unmatched dates are excluded. Minute-close drawdown is saved under the legacy `max_5min_close_drawdown_pct` field and labeled correctly in the report.

Added an explicit optimistic entry-bar ordering sensitivity for the longer run: ignore entry-bar adverse excursions only when entry occurs intrabar, never when the bar opens through the trigger. This is a diagnostic assumption, not a claim about observed event order.

Because Alpaca advertises commission-free execution for eligible retail accounts, also test a zero-fee sensitivity for the minute replay at 0/2.5/5 bps per side. This omits regulatory, borrowing and financing charges and is explicitly favorable, not an account-specific calibrated cost model. Rules, symbols and sizing remain unchanged.
