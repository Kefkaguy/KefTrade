from datetime import date

from app.services.intraday_universe import (
    UniverseRule,
    build_membership_intervals,
    rank_universe_at,
    rebalance_dates,
)


def liquidity(symbol_levels: dict[str, float], *, sessions: int = 80, start=date(2024, 1, 1)):
    from datetime import timedelta

    return {
        symbol: {
            start + timedelta(days=offset): level
            for offset in range(sessions)
        }
        for symbol, level in symbol_levels.items()
    }


def rule(**overrides) -> UniverseRule:
    defaults = {
        "universe_key": "test_liquid_100",
        "target_size": 2,
        "rebalance_months": 3,
        "rank_lookback_sessions": 60,
        "minimum_sessions": 10,
        "minimum_median_dollar_volume": 5_000_000.0,
    }
    defaults.update(overrides)
    return UniverseRule(**defaults)


def test_rebalance_dates_land_on_month_starts():
    dates = rebalance_dates(date(2024, 2, 15), date(2024, 12, 1), months=3)

    assert dates[0] == date(2024, 2, 1)
    assert all(item.day == 1 for item in dates)
    assert dates == [date(2024, 2, 1), date(2024, 5, 1), date(2024, 8, 1), date(2024, 11, 1)]


def test_ranking_uses_only_sessions_before_the_effective_date():
    data = liquidity({"AAA": 10_000_000.0}, sessions=40)
    # Everything after the cut is enormous; it must not influence the rank.
    for offset, session_date in enumerate(sorted(data["AAA"])):
        if session_date >= date(2024, 2, 1):
            data["AAA"][session_date] = 10_000_000_000.0

    ranked = rank_universe_at(data, as_of=date(2024, 2, 1), rule=rule())

    assert ranked[0]["median_dollar_volume"] == 10_000_000.0


def test_ranking_drops_symbols_with_too_little_history():
    data = liquidity({"AAA": 50_000_000.0}, sessions=5)

    ranked = rank_universe_at(data, as_of=date(2024, 3, 1), rule=rule(minimum_sessions=10))

    assert ranked == []


def test_ranking_drops_symbols_below_the_liquidity_floor():
    data = liquidity({"THIN": 1_000_000.0, "DEEP": 90_000_000.0}, sessions=40)

    ranked = rank_universe_at(data, as_of=date(2024, 3, 1), rule=rule())

    assert [row["symbol"] for row in ranked] == ["DEEP"]


def test_ranking_keeps_only_the_target_size_most_liquid():
    data = liquidity(
        {"AAA": 90_000_000.0, "BBB": 80_000_000.0, "CCC": 70_000_000.0}, sessions=40
    )

    ranked = rank_universe_at(data, as_of=date(2024, 3, 1), rule=rule(target_size=2))

    assert [row["symbol"] for row in ranked] == ["AAA", "BBB"]
    assert [row["rank"] for row in ranked] == [1, 2]


def test_membership_intervals_merge_consecutive_rebalances():
    data = liquidity({"AAA": 90_000_000.0}, sessions=400)

    intervals = build_membership_intervals(
        data, rule=rule(), start=date(2024, 3, 1), end=date(2024, 12, 31)
    )

    assert len([row for row in intervals if row["symbol"] == "AAA"]) == 1
    only = intervals[0]
    assert only["effective_from"] == date(2024, 3, 1)
    assert only["effective_to"] == date(2024, 12, 31)


def test_a_symbol_that_drops_out_and_returns_gets_separate_intervals():
    from datetime import timedelta

    sessions = {
        date(2024, 1, 1) + timedelta(days=offset): (
            90_000_000.0 if offset < 90 or offset > 200 else 1_000.0
        )
        for offset in range(400)
    }

    intervals = build_membership_intervals(
        {"AAA": sessions}, rule=rule(), start=date(2024, 4, 1), end=date(2024, 12, 31)
    )

    # Membership is not backfilled over the stretch where it did not qualify.
    assert len(intervals) >= 2
    assert intervals[0]["effective_to"] < intervals[1]["effective_from"]


def test_universe_rule_hash_changes_when_the_rule_changes():
    base = rule()

    assert base.rule_hash() == rule().rule_hash()
    assert base.rule_hash() != rule(target_size=50).rule_hash()
    assert base.rule_hash() != rule(minimum_median_dollar_volume=1.0).rule_hash()
