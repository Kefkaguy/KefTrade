from __future__ import annotations

import time
from typing import Any


COMMAND_CENTER_CACHE_TTL_SECONDS = 30.0
_COMMAND_CENTER_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def cached_command_center_payload(cache_key: str) -> dict[str, Any] | None:
    cached = _COMMAND_CENTER_CACHE.get(cache_key)
    if not cached:
        return None
    created_at, payload = cached
    if time.monotonic() - created_at > COMMAND_CENTER_CACHE_TTL_SECONDS:
        _COMMAND_CENTER_CACHE.pop(cache_key, None)
        return None
    return payload


def remember_command_center_payload(cache_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    _COMMAND_CENTER_CACHE[cache_key] = (time.monotonic(), payload)
    if len(_COMMAND_CENTER_CACHE) > 64:
        oldest_key = min(_COMMAND_CENTER_CACHE, key=lambda key: _COMMAND_CENTER_CACHE[key][0])
        _COMMAND_CENTER_CACHE.pop(oldest_key, None)
    return payload


def clear_command_center_cache() -> None:
    _COMMAND_CENTER_CACHE.clear()
