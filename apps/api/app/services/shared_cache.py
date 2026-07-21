from __future__ import annotations

import json
import time
from typing import Any, Callable, TypeVar
from uuid import uuid4

from app.settings import settings

_client: Any | None = None
_initialized = False
T = TypeVar("T")


def client() -> Any | None:
    global _client, _initialized
    if _initialized:
        return _client
    _initialized = True
    if not settings.redis_url:
        return None
    try:
        from redis import Redis

        _client = Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
        _client.ping()
    except Exception:
        _client = None
    return _client


def get_json(key: str) -> Any | None:
    current = client()
    if current is None:
        return None
    try:
        payload = current.get(full_key(key))
        return json.loads(payload) if payload else None
    except Exception:
        return None


def set_json(key: str, value: Any, ttl_seconds: int) -> None:
    current = client()
    if current is None:
        return
    try:
        current.setex(full_key(key), ttl_seconds, json.dumps(value, default=str, separators=(",", ":")))
    except Exception:
        return


def get_or_load_json(key: str, ttl_seconds: int, loader: Callable[[], T]) -> T:
    """Cache-aside with a short Redis lease to coalesce summary stampedes."""
    cached = get_json(key)
    if cached is not None:
        return cached
    current = client()
    if current is None:
        return loader()
    lock_key = full_key(f"lock:{key}")
    token = str(uuid4())
    owns_lock = False
    try:
        owns_lock = bool(current.set(lock_key, token, nx=True, ex=5))
        if not owns_lock:
            for _ in range(20):
                time.sleep(0.05)
                cached = get_json(key)
                if cached is not None:
                    return cached
        value = loader()
        set_json(key, value, ttl_seconds)
        return value
    finally:
        if owns_lock:
            try:
                current.eval("if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end", 1, lock_key, token)
            except Exception:
                pass
def invalidate_summary_cache() -> None:
    current = client()
    if current is None:
        return
    try:
        keys = list(current.scan_iter(match=full_key("summary:*") , count=100))
        if keys:
            current.delete(*keys)
    except Exception:
        return


def full_key(key: str) -> str:
    return f"{settings.cache_key_prefix}:{settings.environment}:v1:{key}"
