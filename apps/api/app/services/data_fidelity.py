"""Data-fidelity retirements: what the Stage 0 probe took off the table.

The Stage 0 microstructure probe (2026-08-16) measured Alpaca's SIP NBBO feed
against a threshold declared before the number existed, and the feed failed.

Alpaca returns the size at the **single** venue currently posting the best
price, and re-picks that venue whenever the NBBO ties between exchanges.  A
handoff between two venues quoting the same price therefore changes the
reported size with no liquidity event behind it, and the Cont-Kukanov-Stoikov
event variable reads that change as order flow.

Across 23,016,760 quote updates (INTC and NVDA, five sessions each), venue
rotation accounted for **45.224%** of gross ``|e_n|`` against a predeclared
ceiling of **30%**.  Every one of the ten symbol-sessions exceeded the ceiling,
and removing rotation events flipped the sign of the session's net order-flow
imbalance in four of them.

This module is the single place that fact is recorded, so a family, a factor
and a dataset snapshot cannot disagree about it.

**Scope discipline.** The finding concerns *quoted sizes*, which are a
single-venue fragment.  It does **not** touch measurements derived from prices
and trades, which the same probe found healthy: quoted and effective spread are
computed from NBBO prices, and Lee-Ready aggressor classification agreed with
the tick rule on 90.2% (INTC) and 88.0% (NVDA) of comparable trades.  Retiring
those too would be overreach, and they are deliberately left alone.

Nothing here deletes code or evidence.  Retired work stays readable and stays
registered; it simply refuses to run, and says why.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

DATA_FIDELITY_VERSION = "stage0_venue_rotation_v1"

# The measurement, and the rule it was measured against.
STAGE0_ROTATION_MEASURED = 0.45224
STAGE0_ROTATION_THRESHOLD = 0.30
STAGE0_QUOTES_MEASURED = 23_016_760
STAGE0_PROBE_DATE = "2026-08-16"
STAGE0_REPORT = "docs/2026-08-16-stage0-microstructure-probe-results.md"

RETIREMENT_CODE = "retired_data_fidelity"

RETIREMENT_REASON = (
    f"{RETIREMENT_CODE}: Alpaca SIP venue rotation measured at "
    f"{STAGE0_ROTATION_MEASURED * 100:.3f}% of gross OFI vs "
    f"{STAGE0_ROTATION_THRESHOLD * 100:.0f}% allowed"
)

# Columns on ``intraday_microstructure_features`` (and its snapshot copies)
# whose economic interpretation rests on the single-venue quoted size.
RETIRED_QUOTE_QUEUE_FIELDS: frozenset[str] = frozenset(
    {
        "order_flow_imbalance",
        "normalized_order_flow_imbalance",
        # Depth is the mean of the two quoted sizes, so it inherits the same
        # defect: it is one venue's queue, not the book's depth at that price.
        "mean_depth",
    }
)

# Measurements from the same table that the probe did **not** invalidate,
# listed explicitly so a future reader does not retire them by association.
PRESERVED_QUOTE_PRICE_FIELDS: frozenset[str] = frozenset(
    {"median_spread_bps", "p90_spread_bps", "quote_count"}
)

RETIRED_FACTOR_KEYS: frozenset[str] = frozenset({"liquidity_shock_reversal"})

RETIRED_V2_ARCHITECTURES: frozenset[str] = frozenset({"liquidity_shock_reversal_v1"})


def retirement_record(subject: str, *, kind: str) -> dict[str, Any]:
    """The structured form of the refusal, for storage and for reports."""
    return {
        "status": RETIREMENT_CODE,
        "kind": kind,
        "subject": subject,
        "reason": RETIREMENT_REASON,
        "measured_rotation_share": STAGE0_ROTATION_MEASURED,
        "allowed_rotation_share": STAGE0_ROTATION_THRESHOLD,
        "quotes_measured": STAGE0_QUOTES_MEASURED,
        "probe_date": STAGE0_PROBE_DATE,
        "report": STAGE0_REPORT,
        "data_fidelity_version": DATA_FIDELITY_VERSION,
    }


def factor_retirement(factor_key: str) -> dict[str, Any] | None:
    """A retirement record for a factor key, or ``None`` if it still runs."""
    if factor_key in RETIRED_FACTOR_KEYS:
        return retirement_record(factor_key, kind="factor")
    return None


def architecture_retirement(architecture: str) -> dict[str, Any] | None:
    """A retirement record for a V2 architecture, or ``None``."""
    if architecture in RETIRED_V2_ARCHITECTURES:
        return retirement_record(architecture, kind="v2_family")
    return None


def assert_architecture_runnable(architecture: str) -> None:
    """Refuse to construct or expand a retired family.

    Raised as ``ValueError`` so the CLI refusal handler reports it as a
    governance decision rather than a crash.
    """
    if architecture in RETIRED_V2_ARCHITECTURES:
        raise ValueError(
            f"{RETIREMENT_REASON}. Family {architecture!r} reads a queue size that "
            f"the feed does not coherently represent; see {STAGE0_REPORT}."
        )


def retired_fields_present(fields: Iterable[str]) -> list[str]:
    """Which of ``fields`` are no longer approved for queue interpretation."""
    return sorted(set(fields) & RETIRED_QUOTE_QUEUE_FIELDS)


CERTIFICATION_NOT_APPROVED = "not_approved_for_queue_interpretation"

CERTIFIED_TABLES: tuple[str, ...] = (
    "intraday_microstructure_features",
    "research_dataset_intraday_features",
)


def snapshot_field_certifications(
    conn: Any, *, dataset_id: int | None = None
) -> dict[str, Any]:
    """Report which frozen fields are no longer approved, without touching them.

    Historical snapshots stay byte-identical and keep their immutability
    triggers; migration 079 records a separate, additive statement *about*
    them.  This reads that statement back so a dataset can be inspected before
    anything is built on it.
    """
    rows = conn.execute(
        """
        SELECT table_name, field_name, certification, reason, evidence_report,
               measured_value, allowed_value, observations, data_fidelity_version
          FROM research_dataset_field_certifications
         WHERE dataset_id IS NULL OR dataset_id = %s
         ORDER BY table_name, field_name
        """,
        (dataset_id,),
    ).fetchall()
    certifications = [dict(row) for row in rows]
    not_approved = sorted(
        {
            str(row["field_name"])
            for row in certifications
            if row["certification"] == CERTIFICATION_NOT_APPROVED
        }
    )
    return {
        "dataset_id": dataset_id,
        "data_fidelity_version": DATA_FIDELITY_VERSION,
        "certifications": certifications,
        "fields_not_approved_for_queue_interpretation": not_approved,
        "fields_preserved": sorted(PRESERVED_QUOTE_PRICE_FIELDS),
        "historical_rows_mutated": False,
    }
