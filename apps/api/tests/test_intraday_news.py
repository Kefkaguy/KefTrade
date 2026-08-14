from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.intraday_news import (
    NEWS_FEATURE_VERSION,
    NewsFeatureIndex,
    empty_news_features,
    normalize_alpaca_news_article,
)


def test_normalize_alpaca_news_article_preserves_known_at_version_per_symbol() -> None:
    rows = normalize_alpaca_news_article(
        {
            "id": 123,
            "headline": "Apple raises guidance after earnings beat",
            "summary": "AAPL quarterly results beat estimates.",
            "content": "Management raises full-year outlook.",
            "created_at": "2026-01-05T14:00:00Z",
            "updated_at": "2026-01-05T14:17:00Z",
            "symbols": ["AAPL", "MSFT"],
            "source": "benzinga",
            "url": "https://example.test/news/123",
        },
        requested_symbols=["AAPL"],
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "alpaca_news"
    assert row["article_id"] == "123"
    assert row["symbol"] == "AAPL"
    assert row["created_at"] == datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
    assert row["updated_at"] == datetime(2026, 1, 5, 14, 17, tzinfo=UTC)
    assert row["known_at"] == datetime(2026, 1, 5, 14, 17, tzinfo=UTC)
    assert len(row["content_hash"]) == 64


def test_news_feature_index_is_point_in_time_and_scores_categories() -> None:
    known_at = datetime(2026, 1, 5, 14, 17, tzinfo=UTC)
    index = NewsFeatureIndex(
        by_symbol={
            "AAPL": [
                {
                    "known_at": known_at,
                    "headline": "Apple raises guidance after earnings beat",
                    "summary": "Analyst upgrade follows strong product launch.",
                    "content": "",
                    "search_text": "apple raises guidance earnings beat analyst upgrade strong product launch",
                }
            ]
        }
    )

    before = index.features_at("AAPL", known_at - timedelta(minutes=1))
    after = index.features_at("AAPL", known_at + timedelta(minutes=1))

    assert before == empty_news_features()
    assert after["news_last_15m"] == 1.0
    assert after["news_last_24h"] == 1.0
    assert after["minutes_since_last_news"] == 1.0
    assert after["positive_news_score"] > 0
    assert after["guidance_event"] == 1.0
    assert after["earnings_event"] == 1.0
    assert after["analyst_event"] == 1.0
    assert after["product_event"] == 1.0
    assert NEWS_FEATURE_VERSION == "intraday_news_features_v1_point_in_time"
