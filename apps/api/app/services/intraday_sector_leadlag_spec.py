"""Frozen specification helpers for the governed 5m sector lead/lag study."""

from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
from json import dumps
from statistics import NormalDist
from typing import Any

SECTOR_LEADLAG_VERSION = "intraday_sector_leadlag_5m_v1_peer_excess_spy"
PREDICTOR_MINUTES = 5
HORIZONS_MINUTES = (5, 10, 15)
Z_THRESHOLD = 1.5
MIN_HISTORY_SESSIONS = 20
MIN_SECTOR_MEMBERS = 6
TARGET_GROSS_LOWER_BOUND_BPS = 5.0
FRESH_TESTS = 6
EXCLUDED_TARGETS = ("SPY", "QQQ")

STATE_POSITIVE_PEER_IMPULSE = "positive_peer_impulse"
STATE_NEGATIVE_PEER_IMPULSE = "negative_peer_impulse"
STATE_DIRECTIONS: dict[str, int] = {
    STATE_POSITIVE_PEER_IMPULSE: 1,
    STATE_NEGATIVE_PEER_IMPULSE: -1,
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _stable_hash(payload: Any) -> str:
    return sha256(dumps(_jsonable(payload), sort_keys=True, default=str).encode()).hexdigest()


def selection_t_threshold(total_trials: int, familywise_alpha: float = 0.05) -> float:
    if total_trials <= 0:
        raise ValueError("total_trials must be positive")
    tail = familywise_alpha / (2.0 * total_trials)
    return NormalDist().inv_cdf(1.0 - tail)


def classify_peer_impulse(z_score: float) -> str | None:
    if z_score >= Z_THRESHOLD:
        return STATE_POSITIVE_PEER_IMPULSE
    if z_score <= -Z_THRESHOLD:
        return STATE_NEGATIVE_PEER_IMPULSE
    return None
