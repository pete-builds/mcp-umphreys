"""Tests for the token-bucket throttle."""

from __future__ import annotations

import asyncio
import time

import pytest

from mcp_umphreys.throttle import TokenBucket


def test_invalid_rps_raises() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rps=0)
    with pytest.raises(ValueError):
        TokenBucket(rps=-1)


@pytest.mark.asyncio
async def test_acquire_consumes_token() -> None:
    bucket = TokenBucket(rps=100, burst=2)
    snap0 = bucket.snapshot()
    assert snap0.tokens_available == pytest.approx(2.0, abs=0.1)
    await bucket.acquire()
    snap1 = bucket.snapshot()
    assert snap1.tokens_available <= 1.5
    assert snap1.last_call_ts is not None


@pytest.mark.asyncio
async def test_acquire_blocks_until_refill() -> None:
    bucket = TokenBucket(rps=5, burst=1)
    await bucket.acquire()
    start = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15


@pytest.mark.asyncio
async def test_snapshot_does_not_consume() -> None:
    bucket = TokenBucket(rps=10, burst=3)
    s1 = bucket.snapshot()
    s2 = bucket.snapshot()
    assert s1.tokens_available >= 2.5
    assert s2.tokens_available >= 2.5


@pytest.mark.asyncio
async def test_burst_capacity_caps_refill() -> None:
    bucket = TokenBucket(rps=10, burst=2)
    await asyncio.sleep(0.5)
    snap = bucket.snapshot()
    assert snap.tokens_available <= 2.0
