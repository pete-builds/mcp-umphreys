"""Tests for the opaque KV cache."""

from __future__ import annotations

import asyncio

import pytest

from mcp_umphreys.cache import ResponseCache, _hash_params


def test_hash_params_is_order_invariant() -> None:
    a = _hash_params({"year": 2023, "venue": "Tabernacle"})
    b = _hash_params({"venue": "Tabernacle", "year": 2023})
    assert a == b


def test_hash_params_distinguishes_values() -> None:
    a = _hash_params({"year": 2022})
    b = _hash_params({"year": 2023})
    assert a != b


@pytest.mark.asyncio
async def test_get_returns_none_on_miss(empty_cache: ResponseCache) -> None:
    await empty_cache.init()
    result = await empty_cache.get("atu:setlists_by_date", {"date": "2023-02-26"})
    assert result is None
    assert empty_cache.last_miss_ts is not None
    assert empty_cache.last_hit_ts is None


@pytest.mark.asyncio
async def test_put_then_get_returns_payload(empty_cache: ResponseCache) -> None:
    payload = [{"show_id": 5551001, "venuename": "The Tabernacle"}]
    await empty_cache.init()
    await empty_cache.put("atu:setlists_by_date", {"date": "2023-02-26"}, payload)
    hit = await empty_cache.get("atu:setlists_by_date", {"date": "2023-02-26"})
    assert hit == payload
    assert empty_cache.last_hit_ts is not None


@pytest.mark.asyncio
async def test_ttl_expiry(temp_cache_path: str) -> None:
    cache = ResponseCache(db_path=temp_cache_path, ttl_seconds=1)
    await cache.init()
    await cache.put("ep", {"k": "v"}, {"hello": "world"})
    assert await cache.get("ep", {"k": "v"}) == {"hello": "world"}
    await asyncio.sleep(2.5)
    assert await cache.get("ep", {"k": "v"}) is None


@pytest.mark.asyncio
async def test_ttl_override_expires_before_instance_ttl(temp_cache_path: str) -> None:
    cache = ResponseCache(db_path=temp_cache_path, ttl_seconds=86400)
    await cache.init()
    await cache.put("ep", {"k": "v"}, {"hello": "world"})
    assert await cache.get("ep", {"k": "v"}) == {"hello": "world"}
    await asyncio.sleep(2.5)
    assert await cache.get("ep", {"k": "v"}, ttl_override=1) is None
    assert await cache.get("ep", {"k": "v"}) == {"hello": "world"}


@pytest.mark.asyncio
async def test_size_bytes_grows_after_put(empty_cache: ResponseCache) -> None:
    await empty_cache.init()
    initial = empty_cache.size_bytes()
    await empty_cache.put("ep", {"k": "v"}, {"data": list(range(10_000))})
    assert empty_cache.size_bytes() > initial


@pytest.mark.asyncio
async def test_replace_on_duplicate_key(empty_cache: ResponseCache) -> None:
    await empty_cache.init()
    await empty_cache.put("ep", {"k": "v"}, {"v": 1})
    await empty_cache.put("ep", {"k": "v"}, {"v": 2})
    assert await empty_cache.get("ep", {"k": "v"}) == {"v": 2}
