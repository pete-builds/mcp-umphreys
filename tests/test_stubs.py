"""Tests for the stub ATU client (hot-window dev/test fixture)."""

from __future__ import annotations

import pytest

from mcp_umphreys.clients.stubs import StubATUClient


@pytest.mark.asyncio
async def test_setlists_by_date_known() -> None:
    client = StubATUClient()
    rows = await client.setlists_by_date("2023-02-26")
    assert rows
    # Raw ATU field names are present (the projection layer depends on them).
    head = rows[0]
    for key in ("setnumber", "settype", "position", "slug", "songname", "showdate"):
        assert key in head
    # The fixture carries an encore row (setnumber == "e").
    assert any(r["setnumber"] == "e" for r in rows)


@pytest.mark.asyncio
async def test_setlists_by_date_unknown_is_empty() -> None:
    client = StubATUClient()
    rows = await client.setlists_by_date("1900-01-01")
    assert rows == []


@pytest.mark.asyncio
async def test_latest_returns_most_recent_show() -> None:
    client = StubATUClient()
    rows = await client.latest()
    assert rows
    assert all(r["showdate"] == "2023-02-26" for r in rows)


@pytest.mark.asyncio
async def test_one_set_fixture_present() -> None:
    client = StubATUClient()
    rows = await client.setlists_by_date("2021-08-13")
    assert rows
    assert rows[0]["settype"] == "One Set"
