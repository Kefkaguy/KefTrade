"""Signed trade flow: which side was demanding liquidity, not just volume.

A candle records that a million shares traded.  It cannot record that nine
hundred thousand of them were buyers lifting the offer.  That distinction is
the whole content of order-flow research, and it is unavailable from OHLCV at
any timeframe -- which is precisely why the retired gap experiment, however
carefully measured, was reshaping information the bars did not contain.

Two classifiers are implemented:

* Lee-Ready, comparing each trade to the prevailing NBBO midpoint with a tick
  fallback at the midpoint.  Accurate, but needs quotes, which arrive at
  roughly ten times the volume of trades.
* The tick rule alone, which needs only trades.

The tick rule is the default because it is affordable across a universe.  That
is a real accuracy concession, so :func:`classifier_agreement_report` measures
it directly on a bounded subsample instead of asserting it is small: the same
principle the instrument certification applies everywhere else.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Sequence

import psycopg

TRADE_FLOW_VERSION = "intraday_trade_flow_v2_calibration_moments"

BUY = "buy"
SELL = "sell"
UNCLASSIFIED = "unclassified"

TICK_RULE = "tick_rule"
LEE_READY = "lee_ready"

# Conditions under which a print has no liquidity-demanding side to identify,
# or did not occur at the quote it appears next to: call auctions have no
# aggressor, and out-of-sequence or derivatively priced trades cannot be
# compared to the prevailing quote at all.  Signing them would manufacture
# imbalance out of bookkeeping.
NON_AGGRESSOR_CONDITIONS = frozenset(
    {
        "O",  # opening prints
        "Q",  # market centre official open
        "M",  # market centre official close
        "5",  # re-opening prints
        "6",  # closing prints
        "9",  # corrected consolidated close
        "L",  # sold last
        "Z",  # sold out of sequence
        "U",  # extended hours, sold out of sequence
        "R",  # seller
        "P",  # prior reference price
        "N",  # next day
        "C",  # cash sale
        "4",  # derivatively priced
        "W",  # average price trade
        "V",  # contingent trade
        "7",  # qualified contingent trade
        "H",  # price variation trade
        "G",  # bunched sold trade
    }
)

# A quote posted after the trade cannot have been the quote the trade crossed.
# Lee and Ready used five seconds against 1980s tape latency; on modern feeds
# the prevailing quote is the one immediately prior.
QUOTE_LAG = timedelta(0)

# Shares. Prints at or above this are treated as institutional-scale for the
# large-trade share; below it is where retail and algorithmic slicing live.
LARGE_TRADE_SIZE = 10_000

BAR_SECONDS = {"1m": 60, "15m": 900, "30m": 1800}


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _round(value: float | Decimal | None, places: int = 8) -> float | None:
    return round(float(value), places) if value is not None else None


def bar_start(timestamp: datetime, *, timeframe: str) -> datetime:
    """Floor a trade timestamp onto the bar grid.

    US market sessions begin at 09:30 ET and every US offset is a whole number
    of hours from UTC, so flooring in UTC lands on the same grid the candles
    use.  No calendar lookup is needed and none is faked.
    """
    seconds = BAR_SECONDS[timeframe]
    moment = timestamp.astimezone(UTC)
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=UTC)


def _is_signable(trade: dict[str, Any]) -> bool:
    return not (NON_AGGRESSOR_CONDITIONS & {str(item) for item in trade.get("conditions") or []})


class _PrevailingQuote:
    """Walks a time-ordered quote stream alongside a time-ordered trade stream.

    Both streams are consumed once, in step, so a session's quotes are never
    held in a structure that is searched per trade.
    """

    def __init__(self, quotes: Iterable[dict[str, Any]], *, lag: timedelta = QUOTE_LAG):
        self._quotes = iter(quotes)
        self._lag = lag
        self._pending: dict[str, Any] | None = next(self._quotes, None)
        self._current: dict[str, Any] | None = None

    def at(self, moment: datetime) -> dict[str, Any] | None:
        cutoff = moment - self._lag
        while self._pending is not None and self._pending["timestamp"] <= cutoff:
            self._current = self._pending
            self._pending = next(self._quotes, None)
        return self._current


def classify_trades(
    trades: Sequence[dict[str, Any]],
    *,
    quotes: Sequence[dict[str, Any]] | None = None,
    previous_price: Decimal | None = None,
) -> list[dict[str, Any]]:
    """Sign each trade buy/sell/unclassified.

    ``previous_price`` carries the last price of the preceding batch so the
    tick rule survives a page boundary; without it every page would open with
    an unclassifiable first trade.
    """
    method = LEE_READY if quotes is not None else TICK_RULE
    prevailing = _PrevailingQuote(quotes) if quotes is not None else None
    last_price = previous_price
    # The last price that actually differed, for the zero-tick rule: a run of
    # identical prints inherits the direction of the move that started it.
    last_move: str | None = None
    signed: list[dict[str, Any]] = []

    for trade in trades:
        price = _decimal(trade["price"])
        side = UNCLASSIFIED
        midpoint: Decimal | None = None
        reason = method

        if not _is_signable(trade):
            reason = "non_aggressor_condition"
        else:
            if prevailing is not None:
                quote = prevailing.at(trade["timestamp"])
                if quote is not None:
                    midpoint = _decimal(quote["midpoint"])
                    if price > midpoint:
                        side = BUY
                    elif price < midpoint:
                        side = SELL
                    else:
                        reason = "midpoint_tick_fallback"
            if side == UNCLASSIFIED:
                if last_price is not None and price > last_price:
                    side = BUY
                elif last_price is not None and price < last_price:
                    side = SELL
                elif last_move is not None:
                    side = last_move
                    reason = "zero_tick"

        if last_price is not None and price != last_price:
            last_move = BUY if price > last_price else SELL
        last_price = price

        signed.append(
            {
                **trade,
                "side": side,
                "midpoint": midpoint,
                "classification": reason,
                "signable": _is_signable(trade),
            }
        )
    return signed


class TradeFlowAccumulator:
    """Folds signed trades into bars incrementally, one page at a time."""

    def __init__(self, *, symbol: str, timeframe: str, feed: str, provider: str = "alpaca"):
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.feed = feed
        self.provider = provider
        self.last_price: Decimal | None = None
        self.methods: set[str] = set()
        self._bars: dict[datetime, dict[str, Any]] = defaultdict(
            lambda: {
                "trade_count": 0,
                "buy_trades": 0,
                "sell_trades": 0,
                "total_volume": Decimal(0),
                "buy_volume": Decimal(0),
                "sell_volume": Decimal(0),
                "unclassified_volume": Decimal(0),
                "large_volume": Decimal(0),
                "notional": Decimal(0),
                "signed_spread_notional": Decimal(0),
                "spread_volume": Decimal(0),
                # The second size moment lets the calibration construct a
                # variance-preserving random-sign null without retaining raw
                # trades.  It is predictor-only and contains no price outcome.
                "trade_size_squared_sum": Decimal(0),
            }
        )

    def add(
        self,
        trades: Sequence[dict[str, Any]],
        *,
        quotes: Sequence[dict[str, Any]] | None = None,
    ) -> int:
        signed = classify_trades(trades, quotes=quotes, previous_price=self.last_price)
        self.methods.add(LEE_READY if quotes is not None else TICK_RULE)
        for trade in signed:
            price = _decimal(trade["price"])
            size = _decimal(trade["size"])
            bucket = self._bars[bar_start(trade["timestamp"], timeframe=self.timeframe)]
            bucket["trade_count"] += 1
            bucket["total_volume"] += size
            if trade["side"] in {BUY, SELL}:
                bucket["trade_size_squared_sum"] += size * size
            bucket["notional"] += price * size
            if size >= LARGE_TRADE_SIZE:
                bucket["large_volume"] += size
            if trade["side"] == BUY:
                bucket["buy_volume"] += size
                bucket["buy_trades"] += 1
            elif trade["side"] == SELL:
                bucket["sell_volume"] += size
                bucket["sell_trades"] += 1
            else:
                bucket["unclassified_volume"] += size
            midpoint = trade.get("midpoint")
            if midpoint is not None and midpoint > 0:
                # Effective spread: twice the signed distance from the
                # midpoint, the price actually paid for immediacy.
                bucket["signed_spread_notional"] += (
                    abs(price - midpoint) / midpoint * Decimal(20000) * size
                )
                bucket["spread_volume"] += size
        if signed:
            self.last_price = _decimal(signed[-1]["price"])
        return len(signed)

    def bars(self) -> list[dict[str, Any]]:
        method = (
            LEE_READY
            if self.methods == {LEE_READY}
            else TICK_RULE
            if self.methods == {TICK_RULE}
            else "mixed"
        )
        rows: list[dict[str, Any]] = []
        for timestamp in sorted(self._bars):
            bucket = self._bars[timestamp]
            total = bucket["total_volume"]
            classified = bucket["buy_volume"] + bucket["sell_volume"]
            counted = bucket["buy_trades"] + bucket["sell_trades"]
            size_squared = bucket["trade_size_squared_sum"]
            rows.append(
                {
                    "symbol": self.symbol,
                    "timeframe": self.timeframe,
                    "timestamp": timestamp,
                    "provider": self.provider,
                    "feed": self.feed,
                    "trade_count": bucket["trade_count"],
                    "total_volume": _round(total, 4),
                    "classified_volume": _round(classified, 4),
                    "trade_size_squared_sum": _round(size_squared, 4),
                    "effective_trade_count": (
                        _round(classified * classified / size_squared, 4)
                        if size_squared > 0
                        else None
                    ),
                    "buy_volume": _round(bucket["buy_volume"], 4),
                    "sell_volume": _round(bucket["sell_volume"], 4),
                    # Imbalance is taken over classified volume only. Dividing
                    # by total would silently pull every bar toward zero in
                    # proportion to how many prints could not be signed.
                    "signed_trade_imbalance": (
                        _round((bucket["buy_volume"] - bucket["sell_volume"]) / classified)
                        if classified > 0
                        else None
                    ),
                    "signed_trade_count_imbalance": (
                        _round((bucket["buy_trades"] - bucket["sell_trades"]) / counted)
                        if counted > 0
                        else None
                    ),
                    "large_trade_share": (
                        _round(bucket["large_volume"] / total) if total > 0 else None
                    ),
                    "unclassified_share": (
                        _round(bucket["unclassified_volume"] / total) if total > 0 else None
                    ),
                    "trade_vwap": (
                        _round(bucket["notional"] / total, 6) if total > 0 else None
                    ),
                    "effective_spread_bps": (
                        _round(
                            bucket["signed_spread_notional"] / bucket["spread_volume"], 4
                        )
                        if bucket["spread_volume"] > 0
                        else None
                    ),
                    "classification_method": method,
                    "calculation_version": TRADE_FLOW_VERSION,
                }
            )
        return rows


def aggregate_trade_flow(
    trades: Sequence[dict[str, Any]],
    *,
    symbol: str,
    timeframe: str,
    feed: str,
    quotes: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One-shot aggregation, for tests and bounded windows."""
    accumulator = TradeFlowAccumulator(symbol=symbol, timeframe=timeframe, feed=feed)
    accumulator.add(trades, quotes=quotes)
    return accumulator.bars()


def classifier_agreement_report(
    trades: Sequence[dict[str, Any]],
    quotes: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """How often the affordable classifier agrees with the accurate one.

    Run on a bounded subsample.  If agreement is poor, the cheap classifier is
    not a proxy for order flow and any hypothesis resting on it is measuring
    something else -- a fact worth discovering before the trials are spent, not
    after.
    """
    reference = classify_trades(trades, quotes=quotes)
    cheap = classify_trades(trades)
    agreed = 0
    compared = 0
    disagreed_by_side: dict[str, int] = defaultdict(int)
    for accurate, approximate in zip(reference, cheap):
        if accurate["side"] == UNCLASSIFIED or approximate["side"] == UNCLASSIFIED:
            continue
        compared += 1
        if accurate["side"] == approximate["side"]:
            agreed += 1
        else:
            disagreed_by_side[accurate["side"]] += 1
    return {
        "trade_flow_version": TRADE_FLOW_VERSION,
        "trades": len(trades),
        "quotes": len(quotes),
        "comparable_trades": compared,
        "agreements": agreed,
        "agreement_rate": _round(agreed / compared, 6) if compared else None,
        "lee_ready_unclassified": sum(
            1 for row in reference if row["side"] == UNCLASSIFIED
        ),
        "tick_rule_unclassified": sum(1 for row in cheap if row["side"] == UNCLASSIFIED),
        "disagreements_where_lee_ready_said": dict(disagreed_by_side),
    }


def persist_trade_flow_features(
    conn: psycopg.Connection,
    rows: Sequence[dict[str, Any]],
) -> int:
    affected = 0
    columns = (
        "symbol", "timeframe", "timestamp", "provider", "feed", "trade_count",
        "total_volume", "buy_volume", "sell_volume", "signed_trade_imbalance",
        "signed_trade_count_imbalance", "large_trade_share", "unclassified_share",
        "trade_vwap", "effective_spread_bps", "classification_method",
        "classified_volume", "trade_size_squared_sum", "effective_trade_count",
        "calculation_version",
    )
    for row in rows:
        result = conn.execute(
            """
            INSERT INTO intraday_trade_flow_features(
                symbol, timeframe, timestamp, provider, feed, trade_count,
                total_volume, buy_volume, sell_volume, signed_trade_imbalance,
                signed_trade_count_imbalance, large_trade_share,
                unclassified_share, trade_vwap, effective_spread_bps,
                classification_method, classified_volume, trade_size_squared_sum,
                effective_trade_count, calculation_version
            )
            VALUES (%(symbol)s, %(timeframe)s, %(timestamp)s, %(provider)s,
                    %(feed)s, %(trade_count)s, %(total_volume)s, %(buy_volume)s,
                    %(sell_volume)s, %(signed_trade_imbalance)s,
                    %(signed_trade_count_imbalance)s, %(large_trade_share)s,
                    %(unclassified_share)s, %(trade_vwap)s,
                    %(effective_spread_bps)s, %(classification_method)s,
                    %(classified_volume)s, %(trade_size_squared_sum)s,
                    %(effective_trade_count)s, %(calculation_version)s)
            ON CONFLICT (symbol, timeframe, timestamp, provider, feed) DO UPDATE SET
                trade_count = EXCLUDED.trade_count,
                total_volume = EXCLUDED.total_volume,
                buy_volume = EXCLUDED.buy_volume,
                sell_volume = EXCLUDED.sell_volume,
                signed_trade_imbalance = EXCLUDED.signed_trade_imbalance,
                signed_trade_count_imbalance = EXCLUDED.signed_trade_count_imbalance,
                large_trade_share = EXCLUDED.large_trade_share,
                unclassified_share = EXCLUDED.unclassified_share,
                trade_vwap = EXCLUDED.trade_vwap,
                effective_spread_bps = EXCLUDED.effective_spread_bps,
                classification_method = EXCLUDED.classification_method,
                classified_volume = EXCLUDED.classified_volume,
                trade_size_squared_sum = EXCLUDED.trade_size_squared_sum,
                effective_trade_count = EXCLUDED.effective_trade_count,
                calculation_version = EXCLUDED.calculation_version
            """,
            {key: row.get(key) for key in columns},
        )
        affected += result.rowcount or 0
    conn.commit()
    return affected


def record_checkpoint(
    conn: psycopg.Connection,
    *,
    symbol: str,
    session_date: Any,
    feed: str,
    timeframe: str,
    status: str,
    trades_fetched: int = 0,
    bars_written: int = 0,
    pages: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO intraday_trade_ingest_checkpoints(
            symbol, session_date, feed, timeframe, status, trades_fetched, bars_written,
            pages, error, ingest_version, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (symbol, session_date, feed, timeframe) DO UPDATE SET
            status = EXCLUDED.status,
            trades_fetched = EXCLUDED.trades_fetched,
            bars_written = EXCLUDED.bars_written,
            pages = EXCLUDED.pages,
            error = EXCLUDED.error,
            ingest_version = EXCLUDED.ingest_version,
            updated_at = NOW()
        """,
        (
            symbol.upper(), session_date, feed, timeframe, status, trades_fetched,
            bars_written, pages, error, TRADE_FLOW_VERSION,
        ),
    )
    conn.commit()


def completed_sessions(
    conn: psycopg.Connection, *, feed: str, timeframe: str
) -> set[tuple[str, Any]]:
    """Symbol-sessions already ingested, so a restart resumes rather than refetches."""
    rows = conn.execute(
        """
        SELECT symbol, session_date
        FROM intraday_trade_ingest_checkpoints
        WHERE feed = %s AND timeframe = %s AND status = 'completed' AND ingest_version = %s
        """,
        (feed, timeframe, TRADE_FLOW_VERSION),
    ).fetchall()
    return {(str(row["symbol"]), row["session_date"]) for row in rows}
