from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.services.intraday_options import (
    OPTION_FEATURE_VERSION,
    OptionFeatureIndex,
    empty_option_features,
    normalize_option_chain_snapshot,
    parse_occ_option_symbol,
)
from app.services.intraday_paper_lab import _build_options_helper


def test_parse_occ_option_symbol_extracts_expiration_type_and_strike() -> None:
    parsed = parse_occ_option_symbol("AAPL240315C00225000")

    assert parsed["underlying_symbol"] == "AAPL"
    assert parsed["expiration_date"] == date(2024, 3, 15)
    assert parsed["option_type"] == "call"
    assert parsed["strike_price"] == 225.0


def test_normalize_option_chain_snapshot_handles_alpaca_chain_shape() -> None:
    observed_at = datetime(2026, 8, 14, 14, 31, tzinfo=UTC)
    normalized = normalize_option_chain_snapshot(
        underlying_symbol="AAPL",
        option_symbol="AAPL260821P00230000",
        observed_at=observed_at,
        feed="opra",
        row={
            "latestQuote": {
                "t": "2026-08-14T14:30:58Z",
                "bp": 3.1,
                "ap": 3.25,
                "bs": 20,
                "as": 24,
            },
            "latestTrade": {"t": "2026-08-14T14:30:59Z", "p": 3.18, "s": 7},
            "impliedVolatility": 0.33,
            "greeks": {"delta": -0.48, "gamma": 0.04, "theta": -0.05, "vega": 0.12, "rho": -0.02},
            "openInterest": 1234,
        },
    )

    assert normalized is not None
    assert normalized["underlying_symbol"] == "AAPL"
    assert normalized["option_symbol"] == "AAPL260821P00230000"
    assert normalized["expiration_date"] == date(2026, 8, 21)
    assert normalized["option_type"] == "put"
    assert normalized["strike_price"] == 230.0
    assert normalized["bid_price"] == 3.1
    assert normalized["ask_price"] == 3.25
    assert normalized["implied_volatility"] == 0.33
    assert normalized["delta"] == -0.48
    assert len(normalized["content_hash"]) == 64


def test_option_feature_index_is_point_in_time_and_summarizes_surface() -> None:
    observed_at = datetime(2026, 8, 14, 14, 31, tzinfo=UTC)
    index = OptionFeatureIndex(
        by_symbol={
            "AAPL": {
                observed_at: [
                    {
                        "option_symbol": "AAPL260821C00230000",
                        "observed_at": observed_at,
                        "expiration_date": date(2026, 8, 21),
                        "option_type": "call",
                        "strike_price": 230.0,
                        "bid_price": 2.0,
                        "ask_price": 2.2,
                        "bid_size": 10,
                        "ask_size": 12,
                        "trade_size": 5,
                        "implied_volatility": 0.30,
                        "delta": 0.52,
                        "gamma": 0.03,
                    },
                    {
                        "option_symbol": "AAPL260821P00230000",
                        "observed_at": observed_at,
                        "expiration_date": date(2026, 8, 21),
                        "option_type": "put",
                        "strike_price": 230.0,
                        "bid_price": 2.5,
                        "ask_price": 2.7,
                        "bid_size": 9,
                        "ask_size": 11,
                        "trade_size": 15,
                        "implied_volatility": 0.40,
                        "delta": -0.48,
                        "gamma": 0.04,
                    },
                    {
                        "option_symbol": "AAPL260918C00230000",
                        "observed_at": observed_at,
                        "expiration_date": date(2026, 9, 18),
                        "option_type": "call",
                        "strike_price": 230.0,
                        "bid_price": 5.0,
                        "ask_price": 5.4,
                        "bid_size": 4,
                        "ask_size": 4,
                        "trade_size": 2,
                        "implied_volatility": 0.42,
                        "delta": 0.55,
                        "gamma": 0.02,
                    },
                ]
            }
        }
    )

    before = index.features_at("AAPL", observed_at - timedelta(seconds=1), underlying_price=231)
    after = index.features_at("AAPL", observed_at + timedelta(minutes=4), underlying_price=231)

    assert before == empty_option_features()
    assert after["option_contracts"] == 3.0
    assert after["option_atm_iv"] == 0.40
    assert after["option_put_call_iv_skew"] > 0
    assert after["option_put_call_volume_ratio"] == 15 / 7
    assert after["option_gamma_proxy"] > 0
    assert after["option_minutes_since_snapshot"] == 4.0
    assert OPTION_FEATURE_VERSION == "intraday_option_surface_features_v1_point_in_time"


def test_paper_lab_options_helper_is_record_only_and_classifies_stale_snapshot() -> None:
    class FakeIndex:
        def features_at(self, *_args, **_kwargs):
            return {
                **empty_option_features(),
                "option_contracts": 1200.0,
                "option_atm_iv": 0.31,
                "option_put_call_iv_skew": 0.09,
                "option_near_atm_spread_bps": 120.0,
                "option_minutes_since_snapshot": 60.0,
            }

    helper = _build_options_helper(
        FakeIndex(),
        symbol="AAPL",
        decision_timestamp=datetime(2026, 8, 14, 14, 0, tzinfo=UTC),
        underlying_price=230.0,
    )

    assert helper["enabled"] is True
    assert helper["mode"] == "record_only"
    assert helper["status"] == "stale"
    assert "option_snapshot_older_than_45m" in helper["reasons"]
    assert "large_put_call_iv_skew" in helper["reasons"]
    assert helper["features"]["option_atm_iv"] == 0.31
