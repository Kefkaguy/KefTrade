"""Point-in-time Alpaca news ingestion and features for intraday research."""

from __future__ import annotations

import asyncio
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Any, Callable, Sequence

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.settings import settings

ALPACA_NEWS_ENDPOINT = "/v1beta1/news"
ALPACA_NEWS_PROVIDER = "alpaca_news"
NEWS_FEATURE_VERSION = "intraday_news_features_v1_point_in_time"
NEWS_PAGE_LIMIT = 50

POSITIVE_TERMS = (
    "beat",
    "beats",
    "raises",
    "raised",
    "upgrade",
    "upgraded",
    "outperform",
    "buy rating",
    "record",
    "approval",
    "approved",
    "surge",
    "jumps",
    "strong",
    "growth",
    "profit",
    "profits",
)
NEGATIVE_TERMS = (
    "miss",
    "misses",
    "cuts",
    "cut",
    "downgrade",
    "downgraded",
    "underperform",
    "sell rating",
    "probe",
    "investigation",
    "lawsuit",
    "recall",
    "falls",
    "drops",
    "weak",
    "loss",
    "losses",
)
CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "earnings_event": ("earnings", "eps", "revenue", "quarterly results", "profit"),
    "guidance_event": ("guidance", "forecast", "outlook", "sees fy", "raises forecast", "cuts forecast"),
    "analyst_event": ("analyst", "upgrade", "downgrade", "price target", "rating"),
    "ma_event": ("acquire", "acquisition", "merger", "takeover", "buyout"),
    "regulatory_event": ("fda", "sec", "doj", "ftc", "regulator", "approval", "probe"),
    "product_event": ("launch", "product", "unveils", "announces new", "release"),
    "management_event": ("ceo", "cfo", "resigns", "appoints", "chairman", "management"),
    "legal_event": ("lawsuit", "settlement", "court", "legal", "sues", "investigation"),
}
NEWS_FEATURE_NAMES = (
    "news_last_15m",
    "news_last_60m",
    "news_last_24h",
    "minutes_since_last_news",
    "first_news_today",
    "news_frequency_surprise",
    "positive_news_score",
    "negative_news_score",
    *CATEGORY_TERMS.keys(),
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("news timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("headline", "summary", "content")
    ).lower()


def _term_score(text: str, terms: Sequence[str]) -> int:
    return sum(1 for term in terms if term in text)


def normalize_alpaca_news_article(
    row: dict[str, Any],
    *,
    requested_symbols: Sequence[str] | None = None,
    provider: str = ALPACA_NEWS_PROVIDER,
) -> list[dict[str, Any]]:
    article_id = str(row.get("id") or "").strip()
    headline = str(row.get("headline") or "").strip()
    created_at = _parse_timestamp(row.get("created_at"))
    updated_at = _parse_timestamp(row.get("updated_at")) or created_at
    if not article_id or not headline or created_at is None or updated_at is None:
        return []
    symbols = [
        str(symbol).strip().upper()
        for symbol in (row.get("symbols") or [])
        if str(symbol or "").strip()
    ]
    if requested_symbols is not None:
        requested = {symbol.upper() for symbol in requested_symbols}
        symbols = [symbol for symbol in symbols if symbol in requested]
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        return []
    content_hash = sha256(
        dumps(
            {
                "headline": row.get("headline"),
                "summary": row.get("summary"),
                "content": row.get("content"),
                "url": row.get("url"),
                "updated_at": updated_at.isoformat(),
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    known_at = max(created_at, updated_at)
    common = {
        "provider": provider,
        "article_id": article_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "known_at": known_at,
        "headline": headline,
        "summary": row.get("summary"),
        "content": row.get("content"),
        "source": row.get("source"),
        "url": row.get("url"),
        "author": row.get("author"),
        "symbols": symbols,
        "images": row.get("images") or [],
        "raw_payload": dict(row),
        "content_hash": content_hash,
    }
    return [{**common, "symbol": symbol} for symbol in symbols]


async def iter_alpaca_news_pages(
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    include_content: bool = False,
    max_pages: int = 10_000,
    request_pause_seconds: float = 0.0,
    rate_limit_retries: int = 8,
    rate_limit_base_sleep: float = 10.0,
) -> list[tuple[list[dict[str, Any]], dict[str, Any]]]:
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_API_SECRET to fetch Alpaca news.")
    if end <= start:
        raise ValueError("news end must be after start")
    params: dict[str, Any] = {
        "symbols": ",".join(symbol.upper() for symbol in symbols),
        "start": _iso(start),
        "end": _iso(end),
        "sort": "asc",
        "limit": NEWS_PAGE_LIMIT,
        "include_content": str(bool(include_content)).lower(),
    }
    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }
    pages: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
    async with httpx.AsyncClient(
        base_url=settings.alpaca_data_base_url,
        timeout=60,
        headers=headers,
    ) as client:
        for page in range(max_pages):
            response = None
            for attempt in range(rate_limit_retries + 1):
                response = await client.get(ALPACA_NEWS_ENDPOINT, params=params)
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
            articles = payload.get("news") or []
            token = payload.get("next_page_token")
            pages.append(
                (
                    articles,
                    {
                        "page": page + 1,
                        "received": len(articles),
                        "request_id": response.headers.get("X-Request-ID"),
                        "next_page_token_present": bool(token),
                    },
                )
            )
            if not token or not articles:
                break
            params["page_token"] = token
            if request_pause_seconds > 0:
                await asyncio.sleep(request_pause_seconds)
    return pages


def upsert_news_articles(conn: psycopg.Connection, articles: Sequence[dict[str, Any]]) -> int:
    if not articles:
        return 0
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO intraday_news_articles(
                provider, article_id, symbol, created_at, updated_at, known_at,
                headline, summary, content, source, url, author, symbols, images,
                raw_payload, content_hash
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (provider, article_id, symbol, known_at)
            DO UPDATE SET
                updated_at = EXCLUDED.updated_at,
                headline = EXCLUDED.headline,
                summary = EXCLUDED.summary,
                content = EXCLUDED.content,
                source = EXCLUDED.source,
                url = EXCLUDED.url,
                author = EXCLUDED.author,
                symbols = EXCLUDED.symbols,
                images = EXCLUDED.images,
                raw_payload = EXCLUDED.raw_payload,
                content_hash = EXCLUDED.content_hash,
                received_at = NOW()
            """,
            [
                (
                    row["provider"],
                    row["article_id"],
                    row["symbol"],
                    row["created_at"],
                    row["updated_at"],
                    row["known_at"],
                    row["headline"],
                    row.get("summary"),
                    row.get("content"),
                    row.get("source"),
                    row.get("url"),
                    row.get("author"),
                    Jsonb(row["symbols"]),
                    Jsonb(row["images"]),
                    Jsonb(row["raw_payload"]),
                    row["content_hash"],
                )
                for row in articles
            ],
        )
    return len(articles)


async def ingest_alpaca_news(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    include_content: bool = False,
    max_pages: int = 10_000,
    request_pause_seconds: float = 0.0,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    selected = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    start = _utc(start)
    end = _utc(end)
    conn.execute(
        """
        INSERT INTO intraday_news_ingest_checkpoints(
            provider, symbol, start_at, end_at, status, updated_at
        )
        SELECT %s, symbol, %s, %s, 'running', NOW()
        FROM unnest(%s::text[]) AS symbol
        ON CONFLICT (provider, symbol, start_at, end_at)
        DO UPDATE SET status = 'running', error = NULL, updated_at = NOW()
        """,
        (ALPACA_NEWS_PROVIDER, start, end, selected),
    )
    conn.commit()
    try:
        pages = await iter_alpaca_news_pages(
            symbols=selected,
            start=start,
            end=end,
            include_content=include_content,
            max_pages=max_pages,
            request_pause_seconds=request_pause_seconds,
        )
    except Exception as exc:
        conn.execute(
            """
            UPDATE intraday_news_ingest_checkpoints
            SET status = 'failed', error = %s, updated_at = NOW()
            WHERE provider = %s
              AND symbol = ANY(%s::text[])
              AND start_at = %s
              AND end_at = %s
            """,
            (str(exc), ALPACA_NEWS_PROVIDER, selected, start, end),
        )
        conn.commit()
        raise
    articles_seen = 0
    articles_upserted = 0
    for page_articles, page_meta in pages:
        normalized: list[dict[str, Any]] = []
        for row in page_articles:
            if isinstance(row, dict):
                normalized.extend(
                    normalize_alpaca_news_article(row, requested_symbols=selected)
                )
        upserted = upsert_news_articles(conn, normalized)
        conn.commit()
        articles_seen += len(page_articles)
        articles_upserted += upserted
        if progress:
            progress(
                {
                    **page_meta,
                    "articles_seen": articles_seen,
                    "symbol_versions_upserted": articles_upserted,
                }
            )
    conn.execute(
        """
        UPDATE intraday_news_ingest_checkpoints
        SET status = 'completed',
            pages = %s,
            articles_seen = %s,
            articles_upserted = %s,
            error = NULL,
            updated_at = NOW()
        WHERE provider = %s
          AND symbol = ANY(%s::text[])
          AND start_at = %s
          AND end_at = %s
        """,
        (len(pages), articles_seen, articles_upserted, ALPACA_NEWS_PROVIDER, selected, start, end),
    )
    conn.commit()
    return {
        "provider": ALPACA_NEWS_PROVIDER,
        "symbols": len(selected),
        "start": start,
        "end": end,
        "pages": len(pages),
        "articles_seen": articles_seen,
        "symbol_article_versions_upserted": articles_upserted,
        "include_content": include_content,
    }


@dataclass
class NewsFeatureIndex:
    by_symbol: dict[str, list[dict[str, Any]]]
    _times_by_symbol: dict[str, list[datetime]] = field(default_factory=dict, init=False, repr=False)

    def features_at(self, symbol: str, decision_timestamp: datetime) -> dict[str, float]:
        normalized_symbol = symbol.upper()
        rows = self.by_symbol.get(normalized_symbol) or []
        if not rows:
            return empty_news_features()
        decision_timestamp = _utc(decision_timestamp)
        times = self._times_by_symbol.get(normalized_symbol)
        if times is None:
            times = [row["known_at"] for row in rows]
            self._times_by_symbol[normalized_symbol] = times
        right = bisect_right(times, decision_timestamp)
        day_start = decision_timestamp.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        windows = {
            "news_last_15m": decision_timestamp - timedelta(minutes=15),
            "news_last_60m": decision_timestamp - timedelta(minutes=60),
            "news_last_24h": decision_timestamp - timedelta(hours=24),
        }
        indices = {
            name: bisect_left(times, start, 0, right)
            for name, start in windows.items()
        }
        last_24h = rows[indices["news_last_24h"] : right]
        last_60m = rows[indices["news_last_60m"] : right]
        count_24h = len(last_24h)
        latest = rows[right - 1] if right else None
        minutes_since = (
            (decision_timestamp - latest["known_at"]).total_seconds() / 60.0
            if latest is not None
            else 100_000.0
        )
        text = " ".join(str(row.get("search_text") or "") for row in last_24h)
        day_left = bisect_left(times, day_start, 0, right)
        features = {
            "news_last_15m": float(right - indices["news_last_15m"]),
            "news_last_60m": float(len(last_60m)),
            "news_last_24h": float(count_24h),
            "minutes_since_last_news": float(minutes_since),
            "first_news_today": 1.0 if right > day_left and right - day_left == 1 else 0.0,
            "news_frequency_surprise": float(len(last_60m) - count_24h / 24.0),
            "positive_news_score": float(_term_score(text, POSITIVE_TERMS)),
            "negative_news_score": float(_term_score(text, NEGATIVE_TERMS)),
        }
        for name, terms in CATEGORY_TERMS.items():
            features[name] = 1.0 if _term_score(text, terms) else 0.0
        return features


def empty_news_features() -> dict[str, float]:
    return {
        name: (100_000.0 if name == "minutes_since_last_news" else 0.0)
        for name in NEWS_FEATURE_NAMES
    }


def load_news_feature_index(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    provider: str = ALPACA_NEWS_PROVIDER,
) -> NewsFeatureIndex:
    selected = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    rows = conn.execute(
        """
        SELECT symbol, known_at, created_at, updated_at, headline, summary, content,
               source, url, article_id
        FROM intraday_news_articles
        WHERE provider = %s
          AND symbol = ANY(%s::text[])
          AND known_at >= %s
          AND known_at <= %s
        ORDER BY symbol, known_at, article_id
        """,
        (provider, selected, _utc(start) - timedelta(hours=24), _utc(end)),
    ).fetchall()
    by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in selected}
    for row in rows:
        item = dict(row)
        item["known_at"] = _utc(item["known_at"])
        item["search_text"] = _text(item)
        by_symbol.setdefault(str(item["symbol"]).upper(), []).append(item)
    return NewsFeatureIndex(by_symbol=by_symbol)


def news_coverage(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    provider: str = ALPACA_NEWS_PROVIDER,
) -> dict[str, Any]:
    selected = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    row = conn.execute(
        """
        SELECT COUNT(*) AS article_symbol_versions,
               COUNT(DISTINCT article_id) AS distinct_articles,
               COUNT(DISTINCT symbol) AS symbols,
               MIN(known_at) AS first_known_at,
               MAX(known_at) AS last_known_at
        FROM intraday_news_articles
        WHERE provider = %s
          AND symbol = ANY(%s::text[])
          AND known_at >= %s
          AND known_at <= %s
        """,
        (provider, selected, _utc(start), _utc(end)),
    ).fetchone()
    checkpoints = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM intraday_news_ingest_checkpoints
        WHERE provider = %s
          AND symbol = ANY(%s::text[])
          AND start_at <= %s
          AND end_at >= %s
        GROUP BY status
        ORDER BY status
        """,
        (provider, selected, _utc(end), _utc(start)),
    ).fetchall()
    return {
        "provider": provider,
        "symbols_requested": len(selected),
        "article_symbol_versions": int(row["article_symbol_versions"] or 0),
        "distinct_articles": int(row["distinct_articles"] or 0),
        "symbols_with_news": int(row["symbols"] or 0),
        "first_known_at": row["first_known_at"],
        "last_known_at": row["last_known_at"],
        "checkpoint_status": {str(item["status"]): int(item["count"]) for item in checkpoints},
    }


def materialize_news_features_for_dataset(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
    symbols: Sequence[str] | None = None,
    provider: str = ALPACA_NEWS_PROVIDER,
    limit: int | None = None,
) -> dict[str, Any]:
    manifest = conn.execute(
        "SELECT assets, window_start, window_end FROM research_dataset_manifests WHERE id = %s",
        (dataset_id,),
    ).fetchone()
    if not manifest:
        raise ValueError(f"No dataset manifest id={dataset_id}.")
    selected = list(dict.fromkeys(str(symbol).upper() for symbol in (symbols or manifest["assets"] or [])))
    index = load_news_feature_index(
        conn,
        symbols=selected,
        start=manifest["window_start"],
        end=manifest["window_end"],
        provider=provider,
    )
    rows = conn.execute(
        """
        SELECT symbol, timestamp
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
                INSERT INTO intraday_news_feature_snapshots(
                    symbol, timeframe, timestamp, provider, feature_version, features
                ) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, timeframe, timestamp, provider, feature_version)
                DO UPDATE SET features = EXCLUDED.features, created_at = NOW()
                """,
                [
                    (
                        row["symbol"],
                        timeframe,
                        row["timestamp"],
                        provider,
                        NEWS_FEATURE_VERSION,
                        Jsonb(index.features_at(str(row["symbol"]), row["timestamp"])),
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
        "feature_version": NEWS_FEATURE_VERSION,
        "rows_materialized": inserted,
        "symbols": len(selected),
    }
