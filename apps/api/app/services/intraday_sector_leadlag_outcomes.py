"""Forward-outcome loaders for the governed 5m sector lead/lag study."""

from __future__ import annotations

from typing import Any

import psycopg

from app.services.intraday_factor_diagnostics import load_cost_model
from app.services.intraday_research_integrity import estimated_round_trip_cost_bps
from app.services.intraday_sector_leadlag_predictor import _build_predictor_states
from app.services.intraday_sector_leadlag_spec import (
    HORIZONS_MINUTES,
    STATE_DIRECTIONS,
    _stable_hash,
)
from app.services.research_splits import get_dataset_splits


def _load_outcome_events(
    conn: psycopg.Connection,
    declaration: dict[str, Any],
    *,
    allowed_phases: set[str],
) -> list[dict[str, Any]]:
    splits = get_dataset_splits(conn, int(declaration["dataset_id"]))
    if splits is None:
        raise ValueError("Frozen split disappeared")
    cost_model = load_cost_model(conn, int(declaration["cost_calibration_id"]))
    if _stable_hash(cost_model) != str(declaration["specification"]["cost_model_hash"]):
        raise ValueError("Cost calibration changed after declaration")

    include_confirmation = "confirmation" in allowed_phases
    _build_predictor_states(
        conn,
        dataset_id=int(declaration["dataset_id"]),
        include_confirmation=include_confirmation,
    )
    phase_list = sorted(allowed_phases)
    rows = conn.execute(
        """
        SELECT
            s.symbol,
            s.sector,
            s.session_date,
            s.decision_at,
            s.peer_count,
            s.peer_return_5m,
            s.spy_return_5m,
            s.peer_excess_5m,
            s.peer_impulse_z,
            s.state,
            s.phase,
            te.open AS target_entry_open,
            t5.close AS target_exit_5m_close,
            t10.close AS target_exit_10m_close,
            t15.close AS target_exit_15m_close,
            se.open AS spy_entry_open,
            s5.close AS spy_exit_5m_close,
            s10.close AS spy_exit_10m_close,
            s15.close AS spy_exit_15m_close
        FROM tmp_sector_leadlag_final_states s
        JOIN research_dataset_candles te
          ON te.dataset_id = %(dataset_id)s
         AND te.timeframe = '1m'
         AND te.symbol = s.symbol
         AND te.timestamp = s.decision_at
        JOIN research_dataset_candles t5
          ON t5.dataset_id = %(dataset_id)s
         AND t5.timeframe = '1m'
         AND t5.symbol = s.symbol
         AND t5.timestamp = s.decision_at + INTERVAL '4 minutes'
        JOIN research_dataset_candles t10
          ON t10.dataset_id = %(dataset_id)s
         AND t10.timeframe = '1m'
         AND t10.symbol = s.symbol
         AND t10.timestamp = s.decision_at + INTERVAL '9 minutes'
        JOIN research_dataset_candles t15
          ON t15.dataset_id = %(dataset_id)s
         AND t15.timeframe = '1m'
         AND t15.symbol = s.symbol
         AND t15.timestamp = s.decision_at + INTERVAL '14 minutes'
        JOIN research_dataset_candles se
          ON se.dataset_id = %(dataset_id)s
         AND se.timeframe = '1m'
         AND se.symbol = 'SPY'
         AND se.timestamp = s.decision_at
        JOIN research_dataset_candles s5
          ON s5.dataset_id = %(dataset_id)s
         AND s5.timeframe = '1m'
         AND s5.symbol = 'SPY'
         AND s5.timestamp = s.decision_at + INTERVAL '4 minutes'
        JOIN research_dataset_candles s10
          ON s10.dataset_id = %(dataset_id)s
         AND s10.timeframe = '1m'
         AND s10.symbol = 'SPY'
         AND s10.timestamp = s.decision_at + INTERVAL '9 minutes'
        JOIN research_dataset_candles s15
          ON s15.dataset_id = %(dataset_id)s
         AND s15.timeframe = '1m'
         AND s15.symbol = 'SPY'
         AND s15.timestamp = s.decision_at + INTERVAL '14 minutes'
        WHERE s.phase = ANY(%(phases)s)
        ORDER BY s.decision_at, s.symbol
        """,
        {
            "dataset_id": int(declaration["dataset_id"]),
            "phases": phase_list,
        },
    ).fetchall()

    prepared: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        target_entry = float(row["target_entry_open"])
        spy_entry = float(row["spy_entry_open"])
        if target_entry <= 0 or spy_entry <= 0:
            continue
        state = str(row["state"])
        direction = STATE_DIRECTIONS[state]
        target_cost_bps = estimated_round_trip_cost_bps(
            cost_model,
            symbol=str(row["symbol"]),
            timestamp=row["decision_at"],
            stressed=True,
        )
        spy_cost_bps = estimated_round_trip_cost_bps(
            cost_model,
            symbol="SPY",
            timestamp=row["decision_at"],
            stressed=True,
        )
        total_cost_bps = float(target_cost_bps) + float(spy_cost_bps)
        outcomes: dict[int, dict[str, float]] = {}
        for horizon in HORIZONS_MINUTES:
            target_exit = float(row[f"target_exit_{horizon}m_close"])
            spy_exit = float(row[f"spy_exit_{horizon}m_close"])
            target_return = target_exit / target_entry - 1.0
            spy_return = spy_exit / spy_entry - 1.0
            residual_return = target_return - spy_return
            gross = direction * residual_return
            outcomes[horizon] = {
                "target_return": target_return,
                "spy_return": spy_return,
                "residual_return": residual_return,
                "gross_return": gross,
                "net_return": gross - total_cost_bps / 10_000.0,
            }
        prepared.append(
            {
                "symbol": str(row["symbol"]).upper(),
                "sector": str(row["sector"]),
                "session_date": row["session_date"],
                "decision_at": row["decision_at"],
                "state": state,
                "phase": str(row["phase"]),
                "direction": direction,
                "peer_count": int(row["peer_count"]),
                "peer_return_bps": float(row["peer_return_5m"]) * 10_000.0,
                "spy_predictor_return_bps": float(row["spy_return_5m"]) * 10_000.0,
                "peer_excess_bps": float(row["peer_excess_5m"]) * 10_000.0,
                "peer_impulse_z": float(row["peer_impulse_z"]),
                "target_cost_bps": float(target_cost_bps),
                "spy_cost_bps": float(spy_cost_bps),
                "total_cost_bps": total_cost_bps,
                "outcomes": outcomes,
            }
        )
    return prepared


def _load_confirmation_cell_events(
    conn: psycopg.Connection,
    declaration: dict[str, Any],
    *,
    state: str,
    horizon: int,
) -> list[dict[str, Any]]:
    """Read exactly one promoted confirmation cell and no other forward horizon."""
    if state not in STATE_DIRECTIONS:
        raise ValueError(f"Unknown frozen state {state}")
    if horizon not in HORIZONS_MINUTES:
        raise ValueError(f"Unknown frozen horizon {horizon}")

    cost_model = load_cost_model(conn, int(declaration["cost_calibration_id"]))
    if _stable_hash(cost_model) != str(declaration["specification"]["cost_model_hash"]):
        raise ValueError("Cost calibration changed after declaration")

    exit_offset = horizon - 1
    rows = conn.execute(
        f"""
        SELECT
            s.symbol,
            s.sector,
            s.session_date,
            s.decision_at,
            s.peer_count,
            s.peer_return_5m,
            s.spy_return_5m,
            s.peer_excess_5m,
            s.peer_impulse_z,
            s.state,
            te.open AS target_entry_open,
            tx.close AS target_exit_close,
            se.open AS spy_entry_open,
            sx.close AS spy_exit_close
        FROM tmp_sector_leadlag_final_states s
        JOIN research_dataset_candles te
          ON te.dataset_id = %(dataset_id)s
         AND te.timeframe = '1m'
         AND te.symbol = s.symbol
         AND te.timestamp = s.decision_at
        JOIN research_dataset_candles tx
          ON tx.dataset_id = %(dataset_id)s
         AND tx.timeframe = '1m'
         AND tx.symbol = s.symbol
         AND tx.timestamp = s.decision_at + INTERVAL '{exit_offset} minutes'
        JOIN research_dataset_candles se
          ON se.dataset_id = %(dataset_id)s
         AND se.timeframe = '1m'
         AND se.symbol = 'SPY'
         AND se.timestamp = s.decision_at
        JOIN research_dataset_candles sx
          ON sx.dataset_id = %(dataset_id)s
         AND sx.timeframe = '1m'
         AND sx.symbol = 'SPY'
         AND sx.timestamp = s.decision_at + INTERVAL '{exit_offset} minutes'
        WHERE s.phase = 'confirmation'
          AND s.state = %(state)s
        ORDER BY s.decision_at, s.symbol
        """,
        {
            "dataset_id": int(declaration["dataset_id"]),
            "state": state,
        },
    ).fetchall()

    direction = STATE_DIRECTIONS[state]
    prepared: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        target_entry = float(row["target_entry_open"])
        spy_entry = float(row["spy_entry_open"])
        if target_entry <= 0 or spy_entry <= 0:
            continue
        target_return = float(row["target_exit_close"]) / target_entry - 1.0
        spy_return = float(row["spy_exit_close"]) / spy_entry - 1.0
        residual_return = target_return - spy_return
        gross = direction * residual_return
        target_cost_bps = float(
            estimated_round_trip_cost_bps(
                cost_model,
                symbol=str(row["symbol"]),
                timestamp=row["decision_at"],
                stressed=True,
            )
        )
        spy_cost_bps = float(
            estimated_round_trip_cost_bps(
                cost_model,
                symbol="SPY",
                timestamp=row["decision_at"],
                stressed=True,
            )
        )
        total_cost_bps = target_cost_bps + spy_cost_bps
        prepared.append(
            {
                "symbol": str(row["symbol"]).upper(),
                "sector": str(row["sector"]),
                "session_date": row["session_date"],
                "decision_at": row["decision_at"],
                "state": state,
                "phase": "confirmation",
                "direction": direction,
                "peer_count": int(row["peer_count"]),
                "peer_return_bps": float(row["peer_return_5m"]) * 10_000.0,
                "spy_predictor_return_bps": float(row["spy_return_5m"]) * 10_000.0,
                "peer_excess_bps": float(row["peer_excess_5m"]) * 10_000.0,
                "peer_impulse_z": float(row["peer_impulse_z"]),
                "target_cost_bps": target_cost_bps,
                "spy_cost_bps": spy_cost_bps,
                "total_cost_bps": total_cost_bps,
                "outcomes": {
                    horizon: {
                        "target_return": target_return,
                        "spy_return": spy_return,
                        "residual_return": residual_return,
                        "gross_return": gross,
                        "net_return": gross - total_cost_bps / 10_000.0,
                    }
                },
            }
        )
    return prepared
