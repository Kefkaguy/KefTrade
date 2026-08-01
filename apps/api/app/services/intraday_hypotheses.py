"""Economic hypothesis predeclaration and factor-version retirement.

A factor key is not a hypothesis.  A hypothesis names who is forced to trade,
why they cannot wait, how their urgency reaches the data this system holds,
when the signal is knowable, when it can actually be entered, how long it is
held, which way it should go, and what result would prove it wrong.  Anything
less cannot be falsified -- it can only be re-fitted.

Every field below is required for exactly that reason, and the whole set is
hashed.  Changing any of it produces a new version and a new trial, so a
hypothesis cannot be quietly edited into agreement with a result that has
already been seen.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from json import dumps
from typing import Any, Sequence

import psycopg
from psycopg.types.json import Jsonb

HYPOTHESIS_VERSION = "intraday_hypotheses_v1"

REQUIRED_FIELDS = (
    "forced_participant",
    "why_they_cannot_wait",
    "how_the_flow_appears_in_data",
    "signal_timestamp",
    "decision_timestamp",
    "executable_entry_timestamp",
    "exit_horizon",
    "expected_direction",
    "universe",
    "cost_model",
    "required_event_count",
    "invalidation_conditions",
    "success_criteria",
)
DIRECTIONS = ("long", "short", "both")


@dataclass(frozen=True)
class Hypothesis:
    """One falsifiable economic claim, frozen and hashed."""

    key: str
    factor_key: str
    title: str
    forced_participant: str
    why_they_cannot_wait: str
    how_the_flow_appears_in_data: str
    signal_timestamp: str
    decision_timestamp: str
    executable_entry_timestamp: str
    exit_horizon: str
    expected_direction: str
    universe: str
    cost_model: str
    required_event_count: int
    invalidation_conditions: tuple[str, ...]
    success_criteria: tuple[str, ...]
    horizon_bars: int = 1
    parameters: tuple[tuple[str, Any], ...] = field(default_factory=tuple)
    version: int = 1

    def __post_init__(self) -> None:
        for name in REQUIRED_FIELDS:
            value = getattr(self, name)
            if value is None or (isinstance(value, (str, tuple, list)) and not value):
                raise ValueError(f"{self.key}: hypothesis field {name!r} is required")
        if self.expected_direction not in DIRECTIONS:
            raise ValueError(
                f"{self.key}: expected_direction must be one of {DIRECTIONS}"
            )
        if int(self.required_event_count) < 1:
            raise ValueError(f"{self.key}: required_event_count must be at least 1")

    def frozen(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["invalidation_conditions"] = list(self.invalidation_conditions)
        payload["success_criteria"] = list(self.success_criteria)
        payload["parameters"] = {key: value for key, value in self.parameters}
        payload["hypothesis_version"] = HYPOTHESIS_VERSION
        return payload

    def hypothesis_hash(self) -> str:
        """Identity of this exact claim, parameters included.

        Any parameter change lands on a different hash, which is what forces a
        new version and another trial rather than a silent re-fit.
        """
        return sha256(dumps(self.frozen(), sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------
# The first bounded experiment: exactly six predeclared tests
# ---------------------------------------------------------------------------

GAP_EXPERIMENT_KEY = "bounded_gap_down_experiment_v1"
GAP_FIXED_PARAMETERS: tuple[tuple[str, Any], ...] = (
    ("minimum_gap_fraction", 0.003),
    ("minimum_relative_volume", 1.5),
    ("maximum_acceptance_fill_fraction", 0.25),
    ("minimum_absorption_fill_fraction", 0.50),
    ("decision_bar_slot", "10:00"),
    ("entry_bar_slot", "10:30"),
    ("entry_price", "entry_bar_open"),
    ("exit_price", "horizon_bar_close"),
    ("forced_session_close_exit", True),
    ("cost_scenario", "stressed_p90"),
)

_GAP_INVALIDATION = (
    "net stressed edge is not positive",
    "day-clustered t-statistic below 3.0",
    "block-bootstrap lower bound on net return is not positive",
    "effect is confined to one quarter, one symbol or one volatility regime",
    "qualifying event count below the predeclared requirement",
)
_GAP_SUCCESS = (
    "positive event-conditioned net return after stressed costs",
    "day-clustered t-statistic of at least 3.0",
    "false-discovery q-value at most 0.10 against the cumulative trial ledger",
    "positive block-bootstrap lower confidence bound",
    "stable direction across quarters and volatility regimes",
)


def _gap_hypothesis(
    *,
    flow_state: str,
    horizon_bars: int,
    required_event_count: int,
) -> Hypothesis:
    continuation = flow_state == "acceptance"
    factor_key = (
        "gap_down_acceptance_continuation" if continuation else "gap_down_absorption_reversal"
    )
    if horizon_bars > 1:
        factor_key = f"{factor_key}_{horizon_bars}bar"
    return Hypothesis(
        key=f"{GAP_EXPERIMENT_KEY}:{flow_state}:{horizon_bars}bar",
        factor_key=factor_key,
        title=(
            f"Gap-down {flow_state} "
            f"{'continuation' if continuation else 'reversal'}, {horizon_bars}-bar hold"
        ),
        forced_participant=(
            "Overnight holders facing a gap down against them: risk-limit and "
            "margin-driven sellers, index and ETF replication flow that must "
            "reweight, and stop-driven retail exits."
            if continuation
            else "Liquidity providers and value buyers who committed capital "
            "against the overnight sellers and must now defend or unwind it."
        ),
        why_they_cannot_wait=(
            "Risk limits and margin calls are evaluated against the opening "
            "print, not against a price the holder would prefer, so the "
            "resulting sales are scheduled by the mandate rather than by the "
            "seller's view of value."
            if continuation
            else "Having absorbed the opening supply, the buyer holds inventory "
            "acquired in a falling market and cannot wait indefinitely to "
            "recycle it, so the reversal is the price of that inventory being "
            "worked back out."
        ),
        how_the_flow_appears_in_data=(
            "A gap down of at least 30 bps from the previous regular close to "
            "the 09:30 open, with 10:00 participation at least 1.5x its "
            "same-slot baseline, and at most a quarter of the gap refilled by "
            "the 10:00 close."
            if continuation
            else "The same gap and elevated participation, but with at least "
            "half the gap refilled by the 10:00 close, which is the observable "
            "signature of the opening supply having been absorbed."
        ),
        signal_timestamp="close of the 10:00 ET bar",
        decision_timestamp="10:30 ET, when the 10:00 bar is complete",
        executable_entry_timestamp="open of the 10:30 ET bar",
        exit_horizon=(
            f"{horizon_bars} bar(s), exited at that bar's close; never carried "
            "past the regular session close"
        ),
        expected_direction="short" if continuation else "long",
        universe="point-in-time liquid US equity membership at the observation timestamp",
        cost_model="stressed p90 round-trip spread from the SIP execution-cost calibration",
        required_event_count=required_event_count,
        invalidation_conditions=_GAP_INVALIDATION,
        success_criteria=_GAP_SUCCESS,
        horizon_bars=horizon_bars,
        parameters=GAP_FIXED_PARAMETERS,
    )


def gap_experiment_hypotheses(*, required_event_count: int = 850) -> list[Hypothesis]:
    """The six tests, and only these six.

    No threshold search, no symbol exclusion after results, and every one of
    the six counts toward the multiple-testing correction whether or not it is
    reported.
    """
    return [
        _gap_hypothesis(
            flow_state=flow_state,
            horizon_bars=horizon_bars,
            required_event_count=required_event_count,
        )
        for flow_state in ("acceptance", "absorption")
        for horizon_bars in (1, 2, 4)
    ]


# ---------------------------------------------------------------------------
# The second bounded experiment: order flow, again exactly six tests
# ---------------------------------------------------------------------------

ORDER_FLOW_EXPERIMENT_KEY = "bounded_order_flow_experiment_v1"

# The required event count is derived from a minimum effect declared in
# advance, not from an effect that was observed.  The first experiment's
# power gate was briefly circular -- it asked how many events would be needed
# to detect the effect the data happened to show, so a null effect demanded an
# impossible sample and every genuine null read as "underpowered".  These
# three constants are the fix: they are the smallest effect worth trading,
# the dispersion it must be detected against, and the hurdle it must clear.
MINIMUM_TRADEABLE_NET_BPS = 5.0
DECLARED_OBSERVATION_DISPERSION_BPS = 60.0
REQUIRED_T_STATISTIC = 3.0


def order_flow_required_event_count(
    *,
    minimum_net_bps: float = MINIMUM_TRADEABLE_NET_BPS,
    dispersion_bps: float = DECLARED_OBSERVATION_DISPERSION_BPS,
    hurdle_t: float = REQUIRED_T_STATISTIC,
    power_z: float = 0.841621,
) -> int:
    """Events needed to reach the t-hurdle on the smallest effect worth trading.

    Both inputs are declared, so the answer is fixed before a single
    observation is measured and a null result stays interpretable.
    """
    return int(round(((hurdle_t + power_z) * dispersion_bps / minimum_net_bps) ** 2 + 0.5))


_ORDER_FLOW_INVALIDATION = (
    "net stressed edge is not positive",
    "day-clustered t-statistic below 3.0",
    "block-bootstrap lower bound on net return is not positive",
    "effect is confined to one quarter, one symbol or one volatility regime",
    "qualifying event count below the predeclared requirement",
    "the side channel the factor depends on is absent or incomplete",
)
_ORDER_FLOW_SUCCESS = (
    "positive event-conditioned net return after stressed costs",
    "day-clustered t-statistic of at least 3.0",
    "false-discovery q-value at most 0.10 against the cumulative trial ledger",
    "positive block-bootstrap lower confidence bound",
    "stable direction across quarters and volatility regimes",
)

_ORDER_FLOW_PARAMETERS: dict[str, tuple[tuple[str, Any], ...]] = {
    "premarket_undiscovered_gap_reversal": (
        ("minimum_opening_gap", 0.003),
        ("maximum_premarket_discovery_share", 0.35),
        ("maximum_premarket_relative_volume", 1.0),
        ("decision_bar_slot", "10:00"),
        ("entry_bar_slot", "10:30"),
    ),
    "signed_trade_imbalance_continuation": (
        ("minimum_signed_imbalance", 0.30),
        ("minimum_trade_count", 200),
        ("maximum_unclassified_share", 0.25),
        ("classifier", "tick_rule_with_lee_ready_agreement_evidence"),
    ),
    "sector_relative_forced_flow_reversal": (
        ("minimum_standardized_residual", 2.0),
        ("minimum_excess_participation", 2.0),
        ("minimum_sector_peers", 4),
        ("peer_aggregate_excludes_the_symbol", True),
    ),
}

_ORDER_FLOW_COMMON: tuple[tuple[str, Any], ...] = (
    ("entry_price", "entry_bar_open"),
    ("exit_price", "horizon_bar_close"),
    ("forced_session_close_exit", True),
    ("cost_scenario", "stressed_p90"),
)

_ORDER_FLOW_CLAIMS: dict[str, dict[str, str]] = {
    "premarket_undiscovered_gap_reversal": {
        "title": "Premarket-undiscovered gap reversal",
        "forced_participant": (
            "Opening-auction participants who must clear an overnight order "
            "imbalance against whatever liquidity is present at 09:30, and the "
            "index, ETF and stop-driven flow that is scheduled into that "
            "auction regardless of price."
        ),
        "why_they_cannot_wait": (
            "Auction orders are executed at the single clearing price the "
            "auction produces. A participant mandated into the open has no "
            "ability to wait for a better price, so when the premarket left the "
            "gap unnegotiated the clearing price is set by who was present "
            "rather than by what was learned."
        ),
        "how_the_flow_appears_in_data": (
            "An opening gap of at least 30 bps where the premarket session "
            "discovered at most 35 percent of it, on premarket volume at or "
            "below the symbol's own trailing premarket baseline."
        ),
        "signal_timestamp": "09:30 ET open, with premarket features complete at 09:30",
        "decision_timestamp": "10:00 ET, when the opening bar is complete",
        "executable_entry_timestamp": "open of the 10:00 ET bar",
        "expected_direction": "both",
    },
    "signed_trade_imbalance_continuation": {
        "title": "Signed trade-imbalance continuation",
        "forced_participant": (
            "An institution working a large parent order to a same-day "
            "completion target through a VWAP or percentage-of-volume "
            "schedule."
        ),
        "why_they_cannot_wait": (
            "The schedule is set by the mandate and the completion deadline, "
            "not by the trader's read of price. While the parent is unfilled "
            "its child orders keep crossing the spread in one direction, and "
            "that demand for immediacy has to be paid for by the price."
        ),
        "how_the_flow_appears_in_data": (
            "Signed trade imbalance of at least 0.30 in a 30-minute bar with "
            "at least 200 prints and at most a quarter of volume unsignable. "
            "The imbalance is signed by which side crossed the spread, which "
            "no candle records at any resolution."
        ),
        "signal_timestamp": "close of the signal bar",
        "decision_timestamp": "close of the signal bar, when its prints are complete",
        "executable_entry_timestamp": "open of the following bar",
        "expected_direction": "both",
    },
    "sector_relative_forced_flow_reversal": {
        "title": "Sector-relative forced-flow reversal",
        "forced_participant": (
            "A single holder liquidating one position under a mandate -- fund "
            "redemption, index deletion, or a risk limit breached on that name "
            "alone -- while nothing has changed for the sector."
        ),
        "why_they_cannot_wait": (
            "The liquidation is scheduled by the redemption or the limit, not "
            "by price. Because the seller must complete regardless, they pay a "
            "concession to whoever will take the other side, and that "
            "concession is not information about value."
        ),
        "how_the_flow_appears_in_data": (
            "A move at least two peer-dispersion units away from the sector "
            "median at the same instant, with participation at least twice the "
            "sector's, measured against a peer group of at least five names "
            "that excludes the symbol itself."
        ),
        "signal_timestamp": "close of the signal bar",
        "decision_timestamp": "close of the signal bar, when the peer cross-section is complete",
        "executable_entry_timestamp": "open of the following bar",
        "expected_direction": "both",
    },
}


def _order_flow_hypothesis(
    *,
    family: str,
    horizon_bars: int,
    required_event_count: int,
) -> Hypothesis:
    claim = _ORDER_FLOW_CLAIMS[family]
    return Hypothesis(
        key=f"{ORDER_FLOW_EXPERIMENT_KEY}:{family}:{horizon_bars}bar",
        factor_key=f"{family}_{horizon_bars}bar",
        title=f"{claim['title']}, {horizon_bars}-bar hold",
        forced_participant=claim["forced_participant"],
        why_they_cannot_wait=claim["why_they_cannot_wait"],
        how_the_flow_appears_in_data=claim["how_the_flow_appears_in_data"],
        signal_timestamp=claim["signal_timestamp"],
        decision_timestamp=claim["decision_timestamp"],
        executable_entry_timestamp=claim["executable_entry_timestamp"],
        exit_horizon=(
            f"{horizon_bars} bar(s), exited at that bar's close; never carried "
            "past the regular session close"
        ),
        # Both families are signed by the event, not by a fixed side: the score
        # already carries the direction, so a declared "long" would be a claim
        # about which events happen to occur rather than about the mechanism.
        expected_direction=claim["expected_direction"],
        universe="point-in-time liquid US equity membership at the observation timestamp",
        cost_model="stressed p90 round-trip spread from the SIP execution-cost calibration",
        required_event_count=required_event_count,
        invalidation_conditions=_ORDER_FLOW_INVALIDATION,
        success_criteria=_ORDER_FLOW_SUCCESS,
        horizon_bars=horizon_bars,
        parameters=_ORDER_FLOW_PARAMETERS[family] + _ORDER_FLOW_COMMON,
    )


def order_flow_experiment_hypotheses(
    *, required_event_count: int | None = None
) -> list[Hypothesis]:
    """The six order-flow tests, and only these six.

    Three families at two horizons.  Every one counts toward the cumulative
    multiple-testing ledger whether or not it is reported, and the families
    are declared together so a disappointing one cannot be dropped afterwards.
    """
    count = required_event_count or order_flow_required_event_count()
    return [
        _order_flow_hypothesis(
            family=family,
            horizon_bars=horizon_bars,
            required_event_count=count,
        )
        for family in (
            "premarket_undiscovered_gap_reversal",
            "signed_trade_imbalance_continuation",
            "sector_relative_forced_flow_reversal",
        )
        for horizon_bars in (1, 2)
    ]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_hypotheses(
    conn: psycopg.Connection,
    hypotheses: Sequence[Hypothesis],
    *,
    experiment_key: str,
    timeframe: str,
    dataset_id: int | None = None,
) -> list[dict[str, Any]]:
    """Store predeclared hypotheses. Immutable and idempotent by hash."""
    stored: list[dict[str, Any]] = []
    for hypothesis in hypotheses:
        payload = hypothesis.frozen()
        existing = conn.execute(
            "SELECT id, version FROM intraday_research_hypotheses WHERE hypothesis_hash = %s",
            (hypothesis.hypothesis_hash(),),
        ).fetchone()
        if existing:
            stored.append(
                {
                    "hypothesis_id": int(existing["id"]),
                    "key": hypothesis.key,
                    "factor_key": hypothesis.factor_key,
                    "version": int(existing["version"]),
                    "already_declared": True,
                }
            )
            continue
        # A new hash for an existing key is a new version, and the version
        # number is derived rather than supplied so it cannot be reused.
        prior = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM intraday_research_hypotheses WHERE hypothesis_key = %s",
            (hypothesis.key,),
        ).fetchone()
        version = int((prior or {}).get("version") or 0) + 1
        row = conn.execute(
            """
            INSERT INTO intraday_research_hypotheses(
                hypothesis_key, hypothesis_hash, experiment_key, factor_key,
                timeframe, dataset_id, version, horizon_bars,
                expected_direction, required_event_count, hypothesis,
                hypothesis_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                hypothesis.key,
                hypothesis.hypothesis_hash(),
                experiment_key,
                hypothesis.factor_key,
                timeframe,
                dataset_id,
                version,
                hypothesis.horizon_bars,
                hypothesis.expected_direction,
                int(hypothesis.required_event_count),
                Jsonb(payload),
                HYPOTHESIS_VERSION,
            ),
        ).fetchone()
        stored.append(
            {
                "hypothesis_id": int(row["id"]),
                "key": hypothesis.key,
                "factor_key": hypothesis.factor_key,
                "version": version,
                "already_declared": False,
            }
        )
    conn.commit()
    return stored


def load_experiment(
    conn: psycopg.Connection,
    *,
    experiment_key: str,
    timeframe: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM intraday_research_hypotheses
        WHERE experiment_key = %s AND timeframe = %s
        ORDER BY hypothesis_key, version
        """,
        (experiment_key, timeframe),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------


def retire_factor_version(
    conn: psycopg.Connection,
    *,
    factor_key: str,
    timeframe: str,
    spec_hash: str,
    reason: str,
    evidence: dict[str, Any],
    hypothesis_hash: str | None = None,
) -> dict[str, Any]:
    """Permanently retire a factor version. Append-only and irreversible."""
    row = conn.execute(
        """
        INSERT INTO intraday_retired_factor_versions(
            factor_key, timeframe, spec_hash, hypothesis_hash, reason, evidence
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (factor_key, spec_hash) DO NOTHING
        RETURNING id, created_at
        """,
        (factor_key, timeframe, spec_hash, hypothesis_hash, reason, Jsonb(evidence)),
    ).fetchone()
    conn.commit()
    if row:
        return {"retired": True, "retirement_id": int(row["id"]), "already_retired": False}
    return {"retired": True, "already_retired": True}


def retired_factor_versions(
    conn: psycopg.Connection,
    *,
    timeframe: str,
) -> dict[str, list[str]]:
    rows = conn.execute(
        "SELECT factor_key, spec_hash FROM intraday_retired_factor_versions WHERE timeframe = %s",
        (timeframe,),
    ).fetchall()
    output: dict[str, list[str]] = {}
    for row in rows:
        output.setdefault(str(row["factor_key"]), []).append(str(row["spec_hash"]))
    return output


def assert_not_retired(
    conn: psycopg.Connection,
    *,
    timeframe: str,
    factor_keys: Sequence[str],
    spec_hash: str,
) -> None:
    """A retired factor version never runs again under the same specification."""
    retired = retired_factor_versions(conn, timeframe=timeframe)
    blocked = [key for key in factor_keys if spec_hash in retired.get(key, [])]
    if blocked:
        raise ValueError(
            f"These factor versions were permanently retired and cannot be re-run "
            f"under specification {spec_hash[:12]}: {sorted(blocked)}. "
            "A retired version is retired; a genuinely different hypothesis needs "
            "a new declaration and counts as a new trial."
        )
