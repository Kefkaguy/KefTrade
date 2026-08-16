"""Stage 0 feasibility probe for order-book microstructure research.

This module answers four questions and nothing else:

1. Is the experiment powered, given an effect size taken from the literature
   rather than from our own data?
2. What share of Cont-Kukanov-Stoikov order-flow imbalance computed from
   Alpaca's NBBO is an artefact of the quoting venue rotating at an unchanged
   price?
3. How many quote updates does the current schema silently discard by
   truncating nanosecond timestamps to microseconds?
4. Can trading halts be identified at all from the data we can retrieve?

It deliberately contains **no** forward returns, no strategy, no threshold
fitted to an outcome, no trial declaration and no database write.  Stage 0 is a
data-fitness measurement; spending statistical budget on it would defeat the
purpose of running it first.

Every constant that a result could be compared against is declared at the top
of this file and is not to be edited once the probe has been run.  The point of
a predeclared kill threshold is that it was chosen before the number existed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import fmean, median
from typing import Any, Iterable, Sequence

STAGE0_VERSION = "microstructure_stage0_probe_v1"

# ---------------------------------------------------------------------------
# Predeclared inputs.  Fixed 2026-08-16, before any observation was made.
# ---------------------------------------------------------------------------

# The kill rule from the design document, §5 Stage 0.  If venue rotation
# accounts for more than this share of gross |e_n|, Alpaca L1 OFI is measuring
# venue routing rather than order flow.
VENUE_ROTATION_KILL_THRESHOLD = 0.30

# Two symbols chosen on relative tick size -- the structural variable Cartea,
# Jaimungal & Penalva §3.4 identify as decisive -- and not on any result.
# INTC is the large-tick case (low nominal price, so the one-cent minimum binds
# often); NVDA is the small-tick case.  Both are liquid enough that a thin
# quote stream cannot be blamed for a null.
PREDECLARED_PROBE_SYMBOLS = ("INTC", "NVDA")

# Five consecutive regular sessions in June 2026, outside the 2025 research
# window the real experiment would use, so the probe consumes no part of it.
PREDECLARED_PROBE_SESSIONS = (
    date(2026, 6, 1),
    date(2026, 6, 2),
    date(2026, 6, 3),
    date(2026, 6, 4),
    date(2026, 6, 5),
)

# Cartea, Jaimungal & Penalva, Table 12.1 (ORCL, 2013-11-01, opening price
# $33.72, one tick = 2.966 bps).  Mid-price change one second after a buy
# market order, conditional on the imbalance regime immediately prior.
CJP_TICK_BPS = 2.966
CJP_BUY_MO_PRICE_CHANGE_DISTRIBUTION = {
    # regime -> {tick move: probability}
    "neutral": {0: 0.77, 1: 0.21, -1: 0.01},
    "bid_heavy": {0: 0.70, 1: 0.28, 2: 0.02},
    "ask_heavy": {0: 1.00},
}

# The literature horizon the effect above is measured at.
CJP_EFFECT_HORIZON_SECONDS = 1

# A halt proxy: the regular session should not contain a gap this long between
# consecutive NBBO updates in a name this liquid.
HALT_PROXY_QUOTE_GAP_SECONDS = 60.0

REGULAR_SESSION_OPEN = (13, 30)  # 09:30 ET in UTC during daylight saving
REGULAR_SESSION_CLOSE = (20, 0)  # 16:00 ET in UTC during daylight saving


# ---------------------------------------------------------------------------
# 1. Power
# ---------------------------------------------------------------------------


def cjp_regime_expected_move_bps(regime: str) -> float:
    """Expected one-second mid-price move, in bps, for one imbalance regime."""
    distribution = CJP_BUY_MO_PRICE_CHANGE_DISTRIBUTION[regime]
    return sum(ticks * probability for ticks, probability in distribution.items()) * CJP_TICK_BPS


def cjp_regime_dispersion_bps(regime: str) -> float:
    """Standard deviation of the same conditional distribution, in bps.

    Declared from the same table as the effect, so the two cannot be mixed and
    matched from different sources to flatter the power calculation.
    """
    distribution = CJP_BUY_MO_PRICE_CHANGE_DISTRIBUTION[regime]
    mean_ticks = sum(ticks * probability for ticks, probability in distribution.items())
    second_moment = sum(
        (ticks**2) * probability for ticks, probability in distribution.items()
    )
    variance = max(second_moment - mean_ticks**2, 0.0)
    return (variance**0.5) * CJP_TICK_BPS


def stage0_power_report(
    *,
    minimum_tradeable_net_bps: float,
    declared_dispersion_bps: float,
    hurdle_t: float,
    power_z: float,
    round_trip_cost_bps: float,
    cost_safety_multiple: float,
) -> dict[str, Any]:
    """Compare the literature's effect against KefTrade's declared bars.

    Two comparisons, both fixed before measurement:

    * *Detectability* -- how many independent observations are needed to
      resolve the effect the literature reports, at the project's t-hurdle.
    * *Materiality* -- whether that effect clears the smallest effect the
      project has declared worth trading, and the cost hurdle it would have to
      beat to be harvestable.

    A direction can fail on materiality while passing on detectability, and
    that is the most likely outcome here.  Reporting only the first would be
    the circular power gate this project already fixed once.
    """

    def required_events(effect_bps: float, dispersion_bps: float) -> int | None:
        if effect_bps <= 0 or dispersion_bps <= 0:
            return None
        return int(round(((hurdle_t + power_z) * dispersion_bps / effect_bps) ** 2 + 0.5))

    neutral = cjp_regime_expected_move_bps("neutral")
    bid_heavy = cjp_regime_expected_move_bps("bid_heavy")
    ask_heavy = cjp_regime_expected_move_bps("ask_heavy")

    incremental_bps = bid_heavy - neutral
    full_span_bps = bid_heavy - ask_heavy
    dispersion_bps = cjp_regime_dispersion_bps("bid_heavy")
    required_gross_bps = round_trip_cost_bps * cost_safety_multiple

    measures = {
        "regime_incremental": incremental_bps,
        "regime_full_span": full_span_bps,
    }
    detectability = {
        name: {
            "effect_bps": round(effect, 4),
            "required_independent_events": required_events(effect, dispersion_bps),
        }
        for name, effect in measures.items()
    }
    materiality = {
        name: {
            "effect_bps": round(effect, 4),
            "minimum_tradeable_net_bps": minimum_tradeable_net_bps,
            "shortfall_multiple_vs_minimum": (
                round(minimum_tradeable_net_bps / effect, 2) if effect > 0 else None
            ),
            "clears_minimum_tradeable": effect >= minimum_tradeable_net_bps,
            "required_gross_bps": round(required_gross_bps, 4),
            "shortfall_multiple_vs_cost_hurdle": (
                round(required_gross_bps / effect, 2) if effect > 0 else None
            ),
            "clears_cost_hurdle": effect >= required_gross_bps,
        }
        for name, effect in measures.items()
    }

    return {
        "stage0_version": STAGE0_VERSION,
        "source": (
            "Cartea, Jaimungal & Penalva (2015), Algorithmic and High-Frequency "
            "Trading, Table 12.1 -- ORCL 2013-11-01, mid-price change 1s after a "
            "buy market order conditional on prior imbalance regime."
        ),
        "horizon_seconds": CJP_EFFECT_HORIZON_SECONDS,
        "tick_bps": CJP_TICK_BPS,
        "conditional_expected_move_bps": {
            "ask_heavy": round(ask_heavy, 4),
            "neutral": round(neutral, 4),
            "bid_heavy": round(bid_heavy, 4),
        },
        "conditional_dispersion_bps": round(dispersion_bps, 4),
        "hurdle_t": hurdle_t,
        "power_z": power_z,
        "detectability": detectability,
        "materiality": materiality,
        # The project's own declared dispersion, carried through so a reader can
        # see that the sub-minute number is a different quantity from the
        # 30-minute one and was not silently substituted for it.
        "project_declared_dispersion_bps_30m": declared_dispersion_bps,
    }


# ---------------------------------------------------------------------------
# 2. Nanosecond timestamps
# ---------------------------------------------------------------------------


def parse_rfc3339_nanoseconds(value: str) -> int:
    """Parse an RFC-3339 timestamp to integer nanoseconds since the epoch.

    ``datetime.fromisoformat`` floors to microseconds, which is the precision
    the current quote schema stores and therefore the precision at which
    distinct NBBO updates start colliding.  Keeping the integer lets the probe
    measure how much is being lost instead of assuming it is negligible.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, tail = text.split(".", 1)
        digits = ""
        for character in tail:
            if character.isdigit():
                digits += character
            else:
                tail = tail[len(digits) :]
                break
        else:
            tail = ""
        fraction_ns = int((digits + "0" * 9)[:9])
        base = datetime.fromisoformat(head + (tail or "+00:00"))
    else:
        fraction_ns = 0
        base = datetime.fromisoformat(text)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    whole_seconds = int(base.replace(microsecond=0).timestamp())
    return whole_seconds * 1_000_000_000 + fraction_ns


# ---------------------------------------------------------------------------
# 3. Venue rotation, collapse and stream health
# ---------------------------------------------------------------------------

PRICE_CHANGE = "price_change"
VENUE_ROTATION = "venue_rotation"
SIZE_SAME_VENUE = "size_change_same_venue"


def classify_side_event(
    *,
    previous_price: float,
    current_price: float,
    previous_venue: str | None,
    current_venue: str | None,
) -> str:
    """Why one side of the book changed between two consecutive NBBO updates.

    ``venue_rotation`` is the case this whole probe exists to size: the price
    did not move, but the exchange posting it did, so the size we observe is a
    different venue's queue.  Under the Cont-Kukanov-Stoikov formula that is
    indistinguishable from real liquidity arriving or leaving.
    """
    if current_price != previous_price:
        return PRICE_CHANGE
    if (current_venue or "") != (previous_venue or ""):
        return VENUE_ROTATION
    return SIZE_SAME_VENUE


def cks_side_contributions(
    previous: dict[str, Any], current: dict[str, Any]
) -> tuple[float, float]:
    """The bid and ask halves of the CKS event variable ``e_n``.

    ``e_n = 1{P_b_n >= P_b_n-1} q_b_n - 1{P_b_n <= P_b_n-1} q_b_n-1
           - 1{P_a_n <= P_a_n-1} q_a_n + 1{P_a_n >= P_a_n-1} q_a_n-1``

    This is the same kernel already implemented in
    ``intraday_execution_costs.aggregate_microstructure_bars``, split by side so
    each half can be attributed to the reason that side changed.
    """
    pb0, pb1 = float(previous["bid_price"]), float(current["bid_price"])
    pa0, pa1 = float(previous["ask_price"]), float(current["ask_price"])
    qb0, qb1 = float(previous["bid_size"]), float(current["bid_size"])
    qa0, qa1 = float(previous["ask_size"]), float(current["ask_size"])
    bid = (qb1 if pb1 >= pb0 else 0.0) - (qb0 if pb1 <= pb0 else 0.0)
    ask = -((qa1 if pa1 <= pa0 else 0.0) - (qa0 if pa1 >= pa0 else 0.0))
    return bid, ask


@dataclass
class QuoteStreamProbe:
    """Folds quote pages into Stage 0 statistics without holding the stream."""

    symbol: str
    session_date: date
    feed: str

    quotes_received: int = 0
    quotes_parsed: int = 0
    normalizer_rejections: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    distinct_nanosecond_keys: int = 0
    distinct_microsecond_keys: int = 0
    out_of_order: int = 0

    gross_abs_e: float = 0.0
    signed_e: float = 0.0
    gross_by_reason: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    signed_by_reason: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    side_events_by_reason: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    updates_with_any_rotation: int = 0
    updates_compared: int = 0

    crossed_quotes: int = 0
    locked_quotes: int = 0
    zero_bid_size: int = 0
    zero_ask_size: int = 0

    spread_bps_samples: list[float] = field(default_factory=list)
    quote_gaps_over_threshold: int = 0
    max_gap_seconds: float = 0.0

    first_ns: int | None = None
    last_ns: int | None = None
    pages: int = 0
    exhausted: bool = False

    _previous: dict[str, Any] | None = None
    _previous_ns: int | None = None
    _last_us_key: int | None = None
    _last_ns_key: int | None = None
    _spread_sample_stride: int = 97
    _seen_for_stride: int = 0

    def add_page(self, rows: Sequence[dict[str, Any]], meta: dict[str, Any]) -> None:
        self.pages += 1
        self.quotes_received += len(rows)
        self.exhausted = bool(meta.get("exhausted"))
        for raw in rows:
            self._add_quote(raw)

    def _add_quote(self, raw: dict[str, Any]) -> None:
        try:
            timestamp_ns = parse_rfc3339_nanoseconds(str(raw["t"]))
            bid_price = float(raw["bp"])
            ask_price = float(raw["ap"])
            bid_size = float(raw.get("bs") or 0)
            ask_size = float(raw.get("as") or 0)
        except (KeyError, TypeError, ValueError):
            self.normalizer_rejections["unparseable"] += 1
            return

        # Timestamp collision accounting.  Rows are already sorted ascending by
        # the API, so a repeated key is a genuine same-instant update rather
        # than an ordering artefact.
        if self._previous_ns is not None and timestamp_ns < self._previous_ns:
            self.out_of_order += 1
        microsecond_key = timestamp_ns // 1_000
        if microsecond_key != self._last_us_key:
            self.distinct_microsecond_keys += 1
            self._last_us_key = microsecond_key
        if timestamp_ns != self._last_ns_key:
            self.distinct_nanosecond_keys += 1
            self._last_ns_key = timestamp_ns

        if self.first_ns is None:
            self.first_ns = timestamp_ns
        if self._previous_ns is not None:
            gap_seconds = (timestamp_ns - self._previous_ns) / 1_000_000_000
            if gap_seconds > self.max_gap_seconds:
                self.max_gap_seconds = gap_seconds
            if gap_seconds > HALT_PROXY_QUOTE_GAP_SECONDS:
                self.quote_gaps_over_threshold += 1
        self.last_ns = timestamp_ns
        self._previous_ns = timestamp_ns

        # Stream-health counters.  These are recorded, not filtered, because a
        # silent row-by-row rejection is how a halt hides.
        if ask_price < bid_price:
            self.crossed_quotes += 1
        elif ask_price == bid_price:
            self.locked_quotes += 1
        if bid_size == 0:
            self.zero_bid_size += 1
        if ask_size == 0:
            self.zero_ask_size += 1
        if bid_price <= 0 or ask_price <= 0:
            self.normalizer_rejections["nonpositive_price"] += 1
        if ask_price < bid_price:
            self.normalizer_rejections["crossed"] += 1

        current = {
            "bid_price": bid_price,
            "ask_price": ask_price,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "bid_venue": raw.get("bx"),
            "ask_venue": raw.get("ax"),
        }
        self.quotes_parsed += 1

        self._seen_for_stride += 1
        if bid_price > 0 and ask_price >= bid_price and self._seen_for_stride % self._spread_sample_stride == 0:
            midpoint = (bid_price + ask_price) / 2
            if midpoint > 0:
                self.spread_bps_samples.append((ask_price - bid_price) / midpoint * 10_000)

        previous = self._previous
        if previous is not None:
            self._accumulate_event(previous, current)
        self._previous = current

    def _accumulate_event(self, previous: dict[str, Any], current: dict[str, Any]) -> None:
        bid_e, ask_e = cks_side_contributions(previous, current)
        bid_reason = classify_side_event(
            previous_price=previous["bid_price"],
            current_price=current["bid_price"],
            previous_venue=previous["bid_venue"],
            current_venue=current["bid_venue"],
        )
        ask_reason = classify_side_event(
            previous_price=previous["ask_price"],
            current_price=current["ask_price"],
            previous_venue=previous["ask_venue"],
            current_venue=current["ask_venue"],
        )
        self.updates_compared += 1
        if VENUE_ROTATION in (bid_reason, ask_reason):
            self.updates_with_any_rotation += 1
        for contribution, reason in ((bid_e, bid_reason), (ask_e, ask_reason)):
            self.side_events_by_reason[reason] += 1
            self.gross_by_reason[reason] += abs(contribution)
            self.signed_by_reason[reason] += contribution
        self.gross_abs_e += abs(bid_e) + abs(ask_e)
        self.signed_e += bid_e + ask_e

    def report(self) -> dict[str, Any]:
        rotation_gross = self.gross_by_reason.get(VENUE_ROTATION, 0.0)
        rotation_share = rotation_gross / self.gross_abs_e if self.gross_abs_e > 0 else None
        span_seconds = (
            (self.last_ns - self.first_ns) / 1_000_000_000
            if self.first_ns is not None and self.last_ns is not None
            else 0.0
        )
        collapsed = self.quotes_parsed - self.distinct_microsecond_keys
        ns_collapsed = self.quotes_parsed - self.distinct_nanosecond_keys
        rotation_free_gross = self.gross_abs_e - rotation_gross
        return {
            "symbol": self.symbol,
            "session_date": self.session_date.isoformat(),
            "feed": self.feed,
            "pages": self.pages,
            "window_exhausted": self.exhausted,
            "quotes_received": self.quotes_received,
            "quotes_parsed": self.quotes_parsed,
            "quote_span_seconds": round(span_seconds, 3),
            "quotes_per_second": (
                round(self.quotes_parsed / span_seconds, 3) if span_seconds > 0 else None
            ),
            "timestamp_fidelity": {
                "distinct_nanosecond_instants": self.distinct_nanosecond_keys,
                "distinct_microsecond_instants": self.distinct_microsecond_keys,
                "rows_lost_to_microsecond_truncation": collapsed,
                "microsecond_collapse_rate": (
                    round(collapsed / self.quotes_parsed, 6) if self.quotes_parsed else None
                ),
                "rows_at_identical_nanosecond": ns_collapsed,
                "nanosecond_collapse_rate": (
                    round(ns_collapsed / self.quotes_parsed, 6) if self.quotes_parsed else None
                ),
                "out_of_order_rows": self.out_of_order,
            },
            "venue_rotation": {
                "gross_abs_e": round(self.gross_abs_e, 3),
                "gross_abs_e_by_reason": {
                    key: round(value, 3) for key, value in sorted(self.gross_by_reason.items())
                },
                "signed_e_by_reason": {
                    key: round(value, 3) for key, value in sorted(self.signed_by_reason.items())
                },
                "side_events_by_reason": dict(sorted(self.side_events_by_reason.items())),
                "rotation_share_of_gross_abs_e": (
                    round(rotation_share, 6) if rotation_share is not None else None
                ),
                "rotation_free_gross_abs_e": round(rotation_free_gross, 3),
                "updates_compared": self.updates_compared,
                "updates_with_any_rotation": self.updates_with_any_rotation,
                "update_rotation_rate": (
                    round(self.updates_with_any_rotation / self.updates_compared, 6)
                    if self.updates_compared
                    else None
                ),
            },
            "stream_health": {
                "crossed_quotes": self.crossed_quotes,
                "locked_quotes": self.locked_quotes,
                "zero_bid_size": self.zero_bid_size,
                "zero_ask_size": self.zero_ask_size,
                "silent_normalizer_rejections": dict(sorted(self.normalizer_rejections.items())),
                "median_spread_bps": (
                    round(median(self.spread_bps_samples), 4) if self.spread_bps_samples else None
                ),
                "spread_samples": len(self.spread_bps_samples),
                "max_quote_gap_seconds": round(self.max_gap_seconds, 3),
                "quote_gaps_over_halt_proxy": self.quote_gaps_over_threshold,
                "halt_proxy_gap_seconds": HALT_PROXY_QUOTE_GAP_SECONDS,
            },
        }


def aggregate_probe_reports(reports: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Pool per-symbol-session reports into the numbers the kill rule reads."""
    if not reports:
        return {"symbol_sessions": 0}
    gross = sum(row["venue_rotation"]["gross_abs_e"] for row in reports)
    rotation = sum(
        row["venue_rotation"]["gross_abs_e_by_reason"].get(VENUE_ROTATION, 0.0)
        for row in reports
    )
    parsed = sum(row["quotes_parsed"] for row in reports)
    collapsed = sum(row["timestamp_fidelity"]["rows_lost_to_microsecond_truncation"] for row in reports)
    updates = sum(row["venue_rotation"]["updates_compared"] for row in reports)
    rotating_updates = sum(row["venue_rotation"]["updates_with_any_rotation"] for row in reports)
    rates = [row["quotes_per_second"] for row in reports if row["quotes_per_second"]]
    spreads = [
        row["stream_health"]["median_spread_bps"]
        for row in reports
        if row["stream_health"]["median_spread_bps"] is not None
    ]
    rotation_share = rotation / gross if gross > 0 else None
    return {
        "symbol_sessions": len(reports),
        "quotes_parsed": parsed,
        "gross_abs_e": round(gross, 3),
        "rotation_gross_abs_e": round(rotation, 3),
        "rotation_share_of_gross_abs_e": (
            round(rotation_share, 6) if rotation_share is not None else None
        ),
        "update_rotation_rate": round(rotating_updates / updates, 6) if updates else None,
        "microsecond_collapse_rate": round(collapsed / parsed, 6) if parsed else None,
        "rows_lost_to_microsecond_truncation": collapsed,
        "mean_quotes_per_second": round(fmean(rates), 3) if rates else None,
        "median_spread_bps": round(median(spreads), 4) if spreads else None,
        "all_windows_exhausted": all(row["window_exhausted"] for row in reports),
    }


def rotation_verdict(rotation_share: float | None) -> dict[str, Any]:
    """Apply the predeclared kill rule.  No threshold is chosen here."""
    if rotation_share is None:
        return {
            "threshold": VENUE_ROTATION_KILL_THRESHOLD,
            "measured": None,
            "verdict": "not_measurable",
            "meaning": "No order-flow events were observed, so the rule cannot be applied.",
        }
    passed = rotation_share <= VENUE_ROTATION_KILL_THRESHOLD
    return {
        "threshold": VENUE_ROTATION_KILL_THRESHOLD,
        "measured": round(rotation_share, 6),
        "verdict": "within_threshold" if passed else "exceeds_threshold",
        "meaning": (
            "Venue rotation accounts for at most the predeclared share of gross "
            "order-flow imbalance, so OFI computed from this feed is measuring "
            "order flow rather than routing."
            if passed
            else "Venue rotation accounts for more gross order-flow imbalance than "
            "the predeclared limit, so OFI computed from this feed is dominated "
            "by which exchange happens to be posting the NBBO."
        ),
    }


def session_window(session: date) -> tuple[datetime, datetime]:
    """The regular-session UTC window for one probe date."""
    open_hour, open_minute = REGULAR_SESSION_OPEN
    close_hour, close_minute = REGULAR_SESSION_CLOSE
    start = datetime(
        session.year, session.month, session.day, open_hour, open_minute, tzinfo=timezone.utc
    )
    end = datetime(
        session.year, session.month, session.day, close_hour, close_minute, tzinfo=timezone.utc
    )
    return start, end
