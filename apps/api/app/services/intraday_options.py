"""Point-in-time Alpaca options ingestion and features for intraday research."""

from __future__ import annotations

import asyncio
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from json import dumps
from statistics import median
from typing import Any, Callable, Sequence

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.settings import settings

ALPACA_OPTIONS_PROVIDER = "alpaca_options"
OPTION_FEATURE_VERSION = "intraday_option_surface_features_v1_point_in_time"
OPTION_CHAIN_ENDPOINT = "/v1beta1/options/snapshots/{underlying_symbol}"
OPTION_CHAIN_PAGE_LIMIT = 1000
OCC_SYMBOL_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")

OPTION_FEATURE_NAMES = (
    "option_contracts",
    "option_atm_iv",
    "option_put_call_iv_skew",
    "option_iv_term_slope",
    "option_call_volume",
    "option_put_volume",
    "option_put_call_volume_ratio",
    "option_gamma_proxy",
    "option_delta_abs_proxy",
    "option_near_atm_spread_bps",
    "option_minutes_since_snapshot",
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("option timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _pick(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def parse_occ_option_symbol(option_symbol: str) -> dict[str, Any]:
    """Parse a standard OCC option symbol.

    Example: AAPL240315C00225000 -> AAPL 2024-03-15 call 225.00.
    """
    match = OCC_SYMBOL_RE.match(option_symbol.upper())
    if not match:
        return {
            "underlying_symbol": None,
            "expiration_date": None,
            "option_type": None,
            "strike_price": None,
        }
    underlying, yymmdd, cp, strike = match.groups()
    year = 2000 + int(yymmdd[:2])
    expiration = date(year, int(yymmdd[2:4]), int(yymmdd[4:6]))
    return {
        "underlying_symbol": underlying,
        "expiration_date": expiration,
        "option_type": "call" if cp == "C" else "put",
        "strike_price": int(strike) / 1000.0,
    }


def _snapshot_items(payload: dict[str, Any] | list[Any]) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(payload, list):
        items = []
        for row in payload:
            if isinstance(row, dict):
                symbol = str(_pick(row, "symbol", "option_symbol", "contract_symbol") or "").strip().upper()
                if symbol:
                    items.append((symbol, row))
        return items
    snapshots = payload.get("snapshots") if isinstance(payload, dict) else None
    if isinstance(snapshots, dict):
        return [
            (str(symbol).upper(), row)
            for symbol, row in snapshots.items()
            if isinstance(row, dict)
        ]
    if isinstance(snapshots, list):
        return _snapshot_items(snapshots)
    if isinstance(payload, dict):
        return [
            (str(symbol).upper(), row)
            for symbol, row in payload.items()
            if isinstance(row, dict)
        ]
    return []


def normalize_option_chain_snapshot(
    *,
    underlying_symbol: str,
    option_symbol: str,
    row: dict[str, Any],
    observed_at: datetime,
    feed: str,
    provider: str = ALPACA_OPTIONS_PROVIDER,
) -> dict[str, Any] | None:
    option_symbol = option_symbol.upper()
    parsed = parse_occ_option_symbol(option_symbol)
    latest_quote = _pick(row, "latestQuote", "latest_quote") or {}
    latest_trade = _pick(row, "latestTrade", "latest_trade") or {}
    greeks = _pick(row, "greeks", "latestGreeks", "latest_greeks") or {}
    if not isinstance(latest_quote, dict):
        latest_quote = {}
    if not isinstance(latest_trade, dict):
        latest_trade = {}
    if not isinstance(greeks, dict):
        greeks = {}
    expiration = (
        _parse_date(_pick(row, "expirationDate", "expiration_date"))
        or parsed["expiration_date"]
    )
    option_type = str(_pick(row, "type", "option_type") or parsed["option_type"] or "").lower()
    if option_type in ("c", "call"):
        option_type = "call"
    elif option_type in ("p", "put"):
        option_type = "put"
    else:
        option_type = None
    strike = _number(_pick(row, "strikePrice", "strike_price")) or parsed["strike_price"]
    if option_symbol == "" or expiration is None:
        return None
    raw_payload = dict(row)
    content_hash = sha256(
        dumps(
            {
                "underlying_symbol": underlying_symbol.upper(),
                "option_symbol": option_symbol,
                "observed_at": _utc(observed_at).isoformat(),
                "payload": raw_payload,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    return {
        "provider": provider,
        "feed": feed,
        "underlying_symbol": underlying_symbol.upper(),
        "option_symbol": option_symbol,
        "observed_at": _utc(observed_at),
        "quote_timestamp": _parse_timestamp(_pick(latest_quote, "t", "timestamp")),
        "trade_timestamp": _parse_timestamp(_pick(latest_trade, "t", "timestamp")),
        "expiration_date": expiration,
        "option_type": option_type,
        "strike_price": strike,
        "bid_price": _number(_pick(latest_quote, "bp", "bid_price", "bidPrice")),
        "ask_price": _number(_pick(latest_quote, "ap", "ask_price", "askPrice")),
        "bid_size": _number(_pick(latest_quote, "bs", "bid_size", "bidSize")),
        "ask_size": _number(_pick(latest_quote, "as", "ask_size", "askSize")),
        "trade_price": _number(_pick(latest_trade, "p", "price")),
        "trade_size": _number(_pick(latest_trade, "s", "size")),
        "implied_volatility": _number(_pick(row, "impliedVolatility", "implied_volatility", "iv")),
        "delta": _number(_pick(greeks, "delta")),
        "gamma": _number(_pick(greeks, "gamma")),
        "theta": _number(_pick(greeks, "theta")),
        "vega": _number(_pick(greeks, "vega")),
        "rho": _number(_pick(greeks, "rho")),
        "open_interest": _number(_pick(row, "openInterest", "open_interest")),
        "raw_payload": raw_payload,
        "content_hash": content_hash,
    }


async def iter_option_chain_pages(
    *,
    underlying_symbol: str,
    feed: str = "opra",
    max_pages: int = 100,
    request_pause_seconds: float = 0.0,
    rate_limit_retries: int = 8,
    rate_limit_base_sleep: float = 10.0,
    expiration_date_gte: date | None = None,
    expiration_date_lte: date | None = None,
    strike_price_gte: float | None = None,
    strike_price_lte: float | None = None,
    option_type: str | None = None,
) -> list[tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]]:
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_API_SECRET to fetch Alpaca options.")
    params: dict[str, Any] = {
        "feed": feed,
        "limit": OPTION_CHAIN_PAGE_LIMIT,
    }
    if expiration_date_gte is not None:
        params["expiration_date_gte"] = expiration_date_gte.isoformat()
    if expiration_date_lte is not None:
        params["expiration_date_lte"] = expiration_date_lte.isoformat()
    if strike_price_gte is not None:
        params["strike_price_gte"] = strike_price_gte
    if strike_price_lte is not None:
        params["strike_price_lte"] = strike_price_lte
    if option_type is not None:
        params["type"] = option_type
    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }
    pages: list[tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]] = []
    endpoint = OPTION_CHAIN_ENDPOINT.format(underlying_symbol=underlying_symbol.upper())
    async with httpx.AsyncClient(
        base_url=settings.alpaca_data_base_url,
        timeout=60,
        headers=headers,
    ) as client:
        for page in range(max_pages):
            response = None
            for attempt in range(rate_limit_retries + 1):
                response = await client.get(endpoint, params=params)
                if response.status_code != 429:
                    break
                if attempt >= rate_limit_retries:
                    break
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.replace(".", "", 1).isdigit()
                    else min(rate_limit_base_sleep * (2 ** min(attempt, 4)), 300.0)
                )
                await asyncio.sleep(delay)
            assert response is not None
            response.raise_for_status()
            payload = response.json()
            items = _snapshot_items(payload)
            token = payload.get("next_page_token") if isinstance(payload, dict) else None
            pages.append(
                (
                    items,
                    {
                        "page": page + 1,
                        "received": len(items),
                        "request_id": response.headers.get("X-Request-ID"),
                        "next_page_token_present": bool(token),
                    },
                )
            )
            if not token or not items:
                break
            params["page_token"] = token
            if request_pause_seconds > 0:
                await asyncio.sleep(request_pause_seconds)
    return pages


def upsert_option_chain_snapshots(conn: psycopg.Connection, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO intraday_option_chain_snapshots(
                provider, feed, underlying_symbol, option_symbol, observed_at,
                quote_timestamp, trade_timestamp, expiration_date, option_type,
                strike_price, bid_price, ask_price, bid_size, ask_size,
                trade_price, trade_size, implied_volatility, delta, gamma,
                theta, vega, rho, open_interest, raw_payload, content_hash
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (provider, feed, underlying_symbol, option_symbol, observed_at)
            DO UPDATE SET
                quote_timestamp = EXCLUDED.quote_timestamp,
                trade_timestamp = EXCLUDED.trade_timestamp,
                expiration_date = EXCLUDED.expiration_date,
                option_type = EXCLUDED.option_type,
                strike_price = EXCLUDED.strike_price,
                bid_price = EXCLUDED.bid_price,
                ask_price = EXCLUDED.ask_price,
                bid_size = EXCLUDED.bid_size,
                ask_size = EXCLUDED.ask_size,
                trade_price = EXCLUDED.trade_price,
                trade_size = EXCLUDED.trade_size,
                implied_volatility = EXCLUDED.implied_volatility,
                delta = EXCLUDED.delta,
                gamma = EXCLUDED.gamma,
                theta = EXCLUDED.theta,
                vega = EXCLUDED.vega,
                rho = EXCLUDED.rho,
                open_interest = EXCLUDED.open_interest,
                raw_payload = EXCLUDED.raw_payload,
                content_hash = EXCLUDED.content_hash
            """,
            [
                (
                    row["provider"],
                    row["feed"],
                    row["underlying_symbol"],
                    row["option_symbol"],
                    row["observed_at"],
                    row.get("quote_timestamp"),
                    row.get("trade_timestamp"),
                    row.get("expiration_date"),
                    row.get("option_type"),
                    row.get("strike_price"),
                    row.get("bid_price"),
                    row.get("ask_price"),
                    row.get("bid_size"),
                    row.get("ask_size"),
                    row.get("trade_price"),
                    row.get("trade_size"),
                    row.get("implied_volatility"),
                    row.get("delta"),
                    row.get("gamma"),
                    row.get("theta"),
                    row.get("vega"),
                    row.get("rho"),
                    row.get("open_interest"),
                    Jsonb(row["raw_payload"]),
                    row["content_hash"],
                )
                for row in rows
            ],
        )
    return len(rows)


async def ingest_option_chains(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    feed: str = "opra",
    observed_at: datetime | None = None,
    max_pages: int = 100,
    request_pause_seconds: float = 0.0,
    progress: Callable[[dict[str, Any]], None] | None = None,
    expiration_date_gte: date | None = None,
    expiration_date_lte: date | None = None,
    strike_price_gte: float | None = None,
    strike_price_lte: float | None = None,
    option_type: str | None = None,
) -> dict[str, Any]:
    selected = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    observed_at = _utc(observed_at or datetime.now(tz=UTC))
    pages_total = 0
    contracts_seen = 0
    contracts_upserted = 0
    for symbol in selected:
        conn.execute(
            """
            INSERT INTO intraday_option_ingest_checkpoints(
                provider, feed, underlying_symbol, started_at, status, updated_at
            ) VALUES (%s,%s,%s,%s,'running',NOW())
            ON CONFLICT (provider, feed, underlying_symbol, started_at)
            DO UPDATE SET status = 'running', error = NULL, updated_at = NOW()
            """,
            (ALPACA_OPTIONS_PROVIDER, feed, symbol, observed_at),
        )
        conn.commit()
        try:
            pages = await iter_option_chain_pages(
                underlying_symbol=symbol,
                feed=feed,
                max_pages=max_pages,
                request_pause_seconds=request_pause_seconds,
                expiration_date_gte=expiration_date_gte,
                expiration_date_lte=expiration_date_lte,
                strike_price_gte=strike_price_gte,
                strike_price_lte=strike_price_lte,
                option_type=option_type,
            )
        except Exception as exc:
            conn.execute(
                """
                UPDATE intraday_option_ingest_checkpoints
                SET status = 'failed', error = %s, updated_at = NOW()
                WHERE provider = %s AND feed = %s AND underlying_symbol = %s AND started_at = %s
                """,
                (str(exc), ALPACA_OPTIONS_PROVIDER, feed, symbol, observed_at),
            )
            conn.commit()
            raise
        symbol_seen = 0
        symbol_upserted = 0
        for page_items, page_meta in pages:
            normalized = [
                normalized_row
                for option_symbol, row in page_items
                if (
                    normalized_row := normalize_option_chain_snapshot(
                        underlying_symbol=symbol,
                        option_symbol=option_symbol,
                        row=row,
                        observed_at=observed_at,
                        feed=feed,
                    )
                )
                is not None
            ]
            upserted = upsert_option_chain_snapshots(conn, normalized)
            conn.commit()
            symbol_seen += len(page_items)
            symbol_upserted += upserted
            contracts_seen += len(page_items)
            contracts_upserted += upserted
            pages_total += 1
            if progress:
                progress(
                    {
                        **page_meta,
                        "symbol": symbol,
                        "contracts_seen": contracts_seen,
                        "contracts_upserted": contracts_upserted,
                    }
                )
        conn.execute(
            """
            UPDATE intraday_option_ingest_checkpoints
            SET status = 'completed',
                pages = %s,
                contracts_seen = %s,
                contracts_upserted = %s,
                error = NULL,
                updated_at = NOW()
            WHERE provider = %s AND feed = %s AND underlying_symbol = %s AND started_at = %s
            """,
            (
                len(pages),
                symbol_seen,
                symbol_upserted,
                ALPACA_OPTIONS_PROVIDER,
                feed,
                symbol,
                observed_at,
            ),
        )
        conn.commit()
    return {
        "provider": ALPACA_OPTIONS_PROVIDER,
        "feed": feed,
        "symbols": len(selected),
        "observed_at": observed_at,
        "pages": pages_total,
        "contracts_seen": contracts_seen,
        "contracts_upserted": contracts_upserted,
    }


def empty_option_features() -> dict[str, float]:
    return {
        name: (100_000.0 if name == "option_minutes_since_snapshot" else 0.0)
        for name in OPTION_FEATURE_NAMES
    }


@dataclass
class OptionFeatureIndex:
    by_symbol: dict[str, dict[datetime, list[dict[str, Any]]]]
    _times_by_symbol: dict[str, list[datetime]] = field(default_factory=dict, init=False, repr=False)

    def features_at(
        self,
        symbol: str,
        decision_timestamp: datetime,
        *,
        underlying_price: float | None = None,
    ) -> dict[str, float]:
        surfaces = self.by_symbol.get(symbol.upper()) or {}
        if not surfaces:
            return empty_option_features()
        decision_timestamp = _utc(decision_timestamp)
        times = self._times_by_symbol.get(symbol.upper())
        if times is None:
            times = sorted(surfaces)
            self._times_by_symbol[symbol.upper()] = times
        index = bisect_right(times, decision_timestamp) - 1
        if index < 0:
            return empty_option_features()
        observed_at = times[index]
        rows = surfaces.get(observed_at) or []
        features = _surface_features(rows, underlying_price=underlying_price)
        features["option_minutes_since_snapshot"] = float(
            (decision_timestamp - observed_at).total_seconds() / 60.0
        )
        return features


def _median(values: Sequence[float]) -> float | None:
    clean = [value for value in values if value == value]
    return float(median(clean)) if clean else None


def _surface_features(
    rows: Sequence[dict[str, Any]],
    *,
    underlying_price: float | None = None,
) -> dict[str, float]:
    if not rows:
        return empty_option_features()
    strikes = [_number(row.get("strike_price")) for row in rows]
    strikes = [value for value in strikes if value is not None and value > 0]
    anchor = underlying_price if underlying_price and underlying_price > 0 else _median(strikes)
    if anchor is None:
        anchor = 0.0
    near_threshold = max(anchor * 0.03, 1.0) if anchor > 0 else 1.0
    near = [
        row
        for row in rows
        if (strike := _number(row.get("strike_price"))) is not None
        and abs(strike - anchor) <= near_threshold
    ]
    if not near:
        near = sorted(
            [row for row in rows if _number(row.get("strike_price")) is not None],
            key=lambda row: abs(float(row["strike_price"]) - anchor),
        )[:10]
    near_calls = [row for row in near if row.get("option_type") == "call"]
    near_puts = [row for row in near if row.get("option_type") == "put"]
    call_volume = sum(_number(row.get("trade_size")) or 0.0 for row in rows if row.get("option_type") == "call")
    put_volume = sum(_number(row.get("trade_size")) or 0.0 for row in rows if row.get("option_type") == "put")
    spreads = []
    for row in near:
        bid = _number(row.get("bid_price"))
        ask = _number(row.get("ask_price"))
        if bid is None or ask is None or bid < 0 or ask <= bid:
            continue
        mid = (bid + ask) / 2.0
        if mid > 0:
            spreads.append((ask - bid) / mid * 10_000)
    expirations = sorted({row.get("expiration_date") for row in rows if row.get("expiration_date") is not None})
    near_expiry_ivs = [
        _number(row.get("implied_volatility"))
        for row in rows
        if expirations and row.get("expiration_date") == expirations[0]
    ]
    far_expiry_ivs = [
        _number(row.get("implied_volatility"))
        for row in rows
        if expirations and row.get("expiration_date") == expirations[-1]
    ]
    near_iv = _median([_number(row.get("implied_volatility")) or float("nan") for row in near])
    call_iv = _median([_number(row.get("implied_volatility")) or float("nan") for row in near_calls])
    put_iv = _median([_number(row.get("implied_volatility")) or float("nan") for row in near_puts])
    near_expiry_iv = _median([value for value in near_expiry_ivs if value is not None])
    far_expiry_iv = _median([value for value in far_expiry_ivs if value is not None])
    return {
        "option_contracts": float(len(rows)),
        "option_atm_iv": float(near_iv or 0.0),
        "option_put_call_iv_skew": float((put_iv or 0.0) - (call_iv or 0.0)),
        "option_iv_term_slope": float(
            (far_expiry_iv - near_expiry_iv)
            if far_expiry_iv is not None and near_expiry_iv is not None
            else 0.0
        ),
        "option_call_volume": float(call_volume),
        "option_put_volume": float(put_volume),
        "option_put_call_volume_ratio": float(put_volume / max(call_volume, 1.0)),
        "option_gamma_proxy": float(
            sum(
                abs(_number(row.get("gamma")) or 0.0)
                * ((_number(row.get("bid_size")) or 0.0) + (_number(row.get("ask_size")) or 0.0) + (_number(row.get("trade_size")) or 0.0))
                for row in rows
            )
        ),
        "option_delta_abs_proxy": float(
            sum(
                abs(_number(row.get("delta")) or 0.0)
                * ((_number(row.get("bid_size")) or 0.0) + (_number(row.get("ask_size")) or 0.0) + (_number(row.get("trade_size")) or 0.0))
                for row in rows
            )
        ),
        "option_near_atm_spread_bps": float(_median(spreads) or 0.0),
        "option_minutes_since_snapshot": 100_000.0,
    }


def load_option_feature_index(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    provider: str = ALPACA_OPTIONS_PROVIDER,
    feed: str = "opra",
) -> OptionFeatureIndex:
    selected = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    rows = conn.execute(
        """
        WITH requested(symbol) AS (
            SELECT unnest(%s::text[])
        ),
        latest AS (
            SELECT
                requested.symbol AS underlying_symbol,
                MAX(snapshot.observed_at) AS observed_at
            FROM requested
            LEFT JOIN intraday_option_chain_snapshots AS snapshot
              ON snapshot.underlying_symbol = requested.symbol
             AND snapshot.provider = %s
             AND snapshot.feed = %s
             AND snapshot.observed_at >= %s
             AND snapshot.observed_at <= %s
            GROUP BY requested.symbol
        )
        SELECT underlying_symbol, option_symbol, observed_at, expiration_date,
               option_type, strike_price, bid_price, ask_price, bid_size, ask_size,
               trade_price, trade_size, implied_volatility, delta, gamma, theta,
               vega, rho, open_interest
        FROM intraday_option_chain_snapshots AS snapshot
        JOIN latest
          ON latest.underlying_symbol = snapshot.underlying_symbol
         AND latest.observed_at = snapshot.observed_at
        WHERE snapshot.provider = %s
          AND snapshot.feed = %s
        ORDER BY snapshot.underlying_symbol, snapshot.observed_at, snapshot.option_symbol
        """,
        (
            selected,
            provider,
            feed,
            _utc(start) - timedelta(days=7),
            _utc(end),
            provider,
            feed,
        ),
    ).fetchall()
    by_symbol: dict[str, dict[datetime, list[dict[str, Any]]]] = {symbol: {} for symbol in selected}
    for row in rows:
        item = dict(row)
        symbol = str(item["underlying_symbol"]).upper()
        observed_at = _utc(item["observed_at"])
        by_symbol.setdefault(symbol, {}).setdefault(observed_at, []).append(item)
    return OptionFeatureIndex(by_symbol=by_symbol)


def option_coverage(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    provider: str = ALPACA_OPTIONS_PROVIDER,
    feed: str = "opra",
) -> dict[str, Any]:
    selected = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    row = conn.execute(
        """
        SELECT COUNT(*) AS contract_snapshots,
               COUNT(DISTINCT underlying_symbol) AS underlyings,
               COUNT(DISTINCT option_symbol) AS option_contracts,
               COUNT(DISTINCT observed_at) AS snapshot_times,
               MIN(observed_at) AS first_observed_at,
               MAX(observed_at) AS last_observed_at
        FROM intraday_option_chain_snapshots
        WHERE provider = %s
          AND feed = %s
          AND underlying_symbol = ANY(%s::text[])
          AND observed_at >= %s
          AND observed_at <= %s
        """,
        (provider, feed, selected, _utc(start), _utc(end)),
    ).fetchone()
    checkpoints = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM intraday_option_ingest_checkpoints
        WHERE provider = %s
          AND feed = %s
          AND underlying_symbol = ANY(%s::text[])
          AND started_at >= %s
          AND started_at <= %s
        GROUP BY status
        ORDER BY status
        """,
        (provider, feed, selected, _utc(start), _utc(end)),
    ).fetchall()
    return {
        "provider": provider,
        "feed": feed,
        "symbols_requested": len(selected),
        "contract_snapshots": int(row["contract_snapshots"] or 0),
        "underlyings_with_options": int(row["underlyings"] or 0),
        "distinct_option_contracts": int(row["option_contracts"] or 0),
        "snapshot_times": int(row["snapshot_times"] or 0),
        "first_observed_at": row["first_observed_at"],
        "last_observed_at": row["last_observed_at"],
        "checkpoint_status": {str(item["status"]): int(item["count"]) for item in checkpoints},
    }


def option_live_status(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    provider: str = ALPACA_OPTIONS_PROVIDER,
    feed: str = "opra",
    fresh_minutes: int = 10,
    now: datetime | None = None,
) -> dict[str, Any]:
    selected = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    checked_at = _utc(now or datetime.now(tz=UTC))
    day_start = checked_at.replace(hour=0, minute=0, second=0, microsecond=0)
    latest = conn.execute(
        """
        WITH requested AS (
            SELECT unnest(%s::text[]) AS symbol
        ),
        latest_by_symbol AS (
            SELECT DISTINCT ON (underlying_symbol)
                   underlying_symbol,
                   observed_at
            FROM intraday_option_chain_snapshots
            WHERE provider = %s
              AND feed = %s
              AND underlying_symbol = ANY(%s::text[])
            ORDER BY underlying_symbol, observed_at DESC
        )
        SELECT
            COUNT(requested.symbol) AS requested_symbols,
            COUNT(latest_by_symbol.underlying_symbol) AS symbols_with_snapshots,
            COUNT(latest_by_symbol.underlying_symbol) FILTER (
                WHERE latest_by_symbol.observed_at >= %s
            ) AS fresh_symbols,
            MIN(latest_by_symbol.observed_at) AS oldest_latest_observed_at,
            MAX(latest_by_symbol.observed_at) AS newest_observed_at
        FROM requested
        LEFT JOIN latest_by_symbol
          ON latest_by_symbol.underlying_symbol = requested.symbol
        """,
        (
            selected,
            provider,
            feed,
            selected,
            checked_at - timedelta(minutes=fresh_minutes),
        ),
    ).fetchone()
    today = conn.execute(
        """
        SELECT COUNT(DISTINCT observed_at) AS snapshot_times_today,
               COUNT(*) AS contract_snapshots_today,
               MIN(observed_at) AS first_observed_at_today,
               MAX(observed_at) AS last_observed_at_today
        FROM intraday_option_chain_snapshots
        WHERE provider = %s
          AND feed = %s
          AND underlying_symbol = ANY(%s::text[])
          AND observed_at >= %s
          AND observed_at <= %s
        """,
        (provider, feed, selected, day_start, checked_at),
    ).fetchone()
    checkpoint = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM intraday_option_ingest_checkpoints
        WHERE provider = %s
          AND feed = %s
          AND underlying_symbol = ANY(%s::text[])
          AND started_at >= %s
          AND started_at <= %s
        GROUP BY status
        ORDER BY status
        """,
        (provider, feed, selected, day_start, checked_at),
    ).fetchall()
    newest = latest["newest_observed_at"]
    age_minutes = (
        (checked_at - _utc(newest)).total_seconds() / 60.0
        if newest is not None
        else None
    )
    fresh_symbols = int(latest["fresh_symbols"] or 0)
    requested_symbols = int(latest["requested_symbols"] or len(selected))
    return {
        "provider": provider,
        "feed": feed,
        "checked_at": checked_at,
        "freshness_threshold_minutes": fresh_minutes,
        "live": bool(requested_symbols and fresh_symbols == requested_symbols),
        "status": "live" if requested_symbols and fresh_symbols == requested_symbols else "stale_or_partial",
        "requested_symbols": requested_symbols,
        "symbols_with_snapshots": int(latest["symbols_with_snapshots"] or 0),
        "fresh_symbols": fresh_symbols,
        "oldest_latest_observed_at": latest["oldest_latest_observed_at"],
        "newest_observed_at": newest,
        "latest_snapshot_age_minutes": round(age_minutes, 2) if age_minutes is not None else None,
        "snapshot_times_today": int(today["snapshot_times_today"] or 0),
        "contract_snapshots_today": int(today["contract_snapshots_today"] or 0),
        "first_observed_at_today": today["first_observed_at_today"],
        "last_observed_at_today": today["last_observed_at_today"],
        "checkpoint_status_today": {str(item["status"]): int(item["count"]) for item in checkpoint},
    }


def materialize_option_features_for_dataset(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
    symbols: Sequence[str] | None = None,
    provider: str = ALPACA_OPTIONS_PROVIDER,
    feed: str = "opra",
    limit: int | None = None,
) -> dict[str, Any]:
    manifest = conn.execute(
        "SELECT assets, window_start, window_end FROM research_dataset_manifests WHERE id = %s",
        (dataset_id,),
    ).fetchone()
    if not manifest:
        raise ValueError(f"No dataset manifest id={dataset_id}.")
    selected = list(dict.fromkeys(str(symbol).upper() for symbol in (symbols or manifest["assets"] or [])))
    index = load_option_feature_index(
        conn,
        symbols=selected,
        start=manifest["window_start"],
        end=manifest["window_end"],
        provider=provider,
        feed=feed,
    )
    rows = conn.execute(
        """
        SELECT symbol, timestamp, close
        FROM research_dataset_candles
        WHERE dataset_id = %s
          AND timeframe = %s
          AND symbol = ANY(%s::text[])
        ORDER BY timestamp, symbol
        """ + (" LIMIT %s" if limit else ""),
        (dataset_id, timeframe, selected, limit) if limit else (dataset_id, timeframe, selected),
    ).fetchall()
    inserted = 0
    for batch_start in range(0, len(rows), 1000):
        batch = rows[batch_start : batch_start + 1000]
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO intraday_option_feature_snapshots(
                    symbol, timeframe, timestamp, provider, feed, feature_version, features
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, timeframe, timestamp, provider, feed, feature_version)
                DO UPDATE SET features = EXCLUDED.features, created_at = NOW()
                """,
                [
                    (
                        row["symbol"],
                        timeframe,
                        row["timestamp"],
                        provider,
                        feed,
                        OPTION_FEATURE_VERSION,
                        Jsonb(
                            index.features_at(
                                str(row["symbol"]),
                                row["timestamp"],
                                underlying_price=_number(row.get("close")),
                            )
                        ),
                    )
                    for row in batch
                ],
            )
        inserted += len(batch)
    conn.commit()
    return {
        "dataset_id": dataset_id,
        "timeframe": timeframe,
        "provider": provider,
        "feed": feed,
        "feature_version": OPTION_FEATURE_VERSION,
        "rows_materialized": inserted,
        "symbols": len(selected),
    }
