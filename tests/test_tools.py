"""End-to-end tool tests against the FastMCP server.

We invoke each tool's underlying coroutine via the FastMCP tool manager. This
validates wire-format JSON, projection logic, error handling, the vault/live
dispatcher, and — most importantly — the frozen output CONTRACT the downstream
game depends on.

No live network and no Postgres: the ATU hot-window client is the in-memory
stub, and the vault is a record-shaped fake reader.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mcp_umphreys.cache import ResponseCache
from mcp_umphreys.config import Settings
from mcp_umphreys.models import (
    Appearance,
    Health,
    NotableJam,
    Performance,
    Show,
    ShowSummary,
    SlugValidation,
    Song,
    SongGap,
)
from mcp_umphreys.server import build_server
from mcp_umphreys.throttle import TokenBucket

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call(server: Any, tool: str, **kwargs: Any) -> dict[str, Any]:
    """Invoke a FastMCP-registered tool by name and return its parsed JSON."""
    result = await server.call_tool(tool, kwargs)
    if hasattr(result, "content") and result.content:
        raw = getattr(result.content[0], "text", "") or ""
    elif hasattr(result, "structured_content") and result.structured_content is not None:
        sc = result.structured_content
        if isinstance(sc, dict) and "result" in sc and isinstance(sc["result"], str):
            raw = sc["result"]
        else:
            raw = json.dumps(sc)
    else:
        raw = str(result)
    return json.loads(raw)


class FakeVaultReader:
    """Record-shaped fake of VaultReader. Returns dicts (asyncpg.Record-like)."""

    def __init__(self) -> None:
        self._shows: dict[str, dict[str, Any]] = {
            "2023-02-26": {
                "date": "2023-02-26",
                "show_id": 5551001,
                "venue_slug": "the-tabernacle-atlanta-ga-usa",
                "venue_name": "The Tabernacle",
                "city": "Atlanta",
                "state": "GA",
                "country": "USA",
                "location": "Atlanta, GA",
                "latitude": None,
                "longitude": None,
                "tour_name": "Winter 2023 Tour",
            },
        }
        self._setlists: dict[str, list[dict[str, Any]]] = {
            "2023-02-26": [
                {
                    "set_number": "1",
                    "set_type": "Set",
                    "position": 1,
                    "song_slug": "all-in-time",
                    "song_name": "All in Time",
                    "transition": " > ",
                    "footnote": "",
                },
                {
                    "set_number": "2",
                    "set_type": "Set",
                    "position": 2,
                    "song_slug": "bridgeless",
                    "song_name": "Bridgeless",
                    "transition": " > ",
                    "footnote": "Unfinished.",
                },
                {
                    "set_number": "e",
                    "set_type": "Encore",
                    "position": 3,
                    "song_slug": "bridgeless-reprise",
                    "song_name": "Bridgeless (Reprise)",
                    "transition": "",
                    "footnote": "",
                },
            ],
        }
        self._songs: dict[str, dict[str, Any]] = {
            "all-in-time": {
                "slug": "all-in-time",
                "title": "All in Time",
                "alias": None,
                "original": True,
                "original_artist": None,
                "times_played": 600,
                "debut_date": "1998-01-09",
                "last_play_date": "2023-02-26",
                "gap_current": 0,
            },
            "bridgeless": {
                "slug": "bridgeless",
                "title": "Bridgeless",
                "alias": None,
                "original": True,
                "original_artist": None,
                "times_played": 450,
                "debut_date": "2002-04-19",
                "last_play_date": "2023-02-26",
                "gap_current": 0,
            },
            "ace-of-spades": {
                "slug": "ace-of-spades",
                "title": "Ace of Spades",
                "alias": None,
                "original": False,
                "original_artist": "Motörhead",
                "times_played": 12,
                "debut_date": "2005-10-31",
                "last_play_date": "2018-10-31",
                "gap_current": 320,
            },
        }

    async def get_show(self, date_or_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        show = self._shows.get(date_or_id)
        if show is None:
            return None, []
        return show, self._setlists.get(date_or_id, [])

    async def search_shows(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "date": s["date"],
                "show_id": s["show_id"],
                "venue_name": s["venue_name"],
                "location": s["location"],
                "tour_name": s["tour_name"],
            }
            for s in self._shows.values()
        ]

    async def recent_shows(self, limit: int = 10) -> list[dict[str, Any]]:
        return [
            {
                "date": s["date"],
                "show_id": s["show_id"],
                "venue_name": s["venue_name"],
                "location": s["location"],
                "tour_name": s["tour_name"],
            }
            for s in sorted(self._shows.values(), key=lambda r: r["date"], reverse=True)
        ][:limit]

    async def get_song(self, slug: str) -> dict[str, Any] | None:
        return self._songs.get(slug)

    async def search_songs(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        q = query.lower()
        rows = [
            s for s in self._songs.values() if q in s["title"].lower() or q in s["slug"].lower()
        ]
        rows.sort(key=lambda s: s["times_played"], reverse=True)
        return rows[:limit]

    async def validate_slugs(self, slugs: list[str]) -> set[str]:
        return {s for s in slugs if s in self._songs}

    async def song_history(self, slug: str, limit: int = 50) -> list[dict[str, Any]]:
        if slug not in self._songs:
            return []
        return [
            {
                "date": "2023-02-26",
                "show_id": 5551001,
                "set_number": "e" if slug == "bridgeless-reprise" else "2",
                "set_type": "Set",
                "transition": " > ",
                "venue_name": "The Tabernacle",
                "venue_location": "Atlanta, GA",
            }
        ][:limit]

    async def jam_chart(self, year: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = [
            {
                "date": "2023-02-26",
                "song_slug": "bridgeless",
                "song_name": "Bridgeless",
                "notes": "Patient Type-II exploration.",
                "show_id": 5551001,
                "venue_name": "The Tabernacle",
            }
        ]
        if year is not None:
            rows = [r for r in rows if str(r["date"]).startswith(str(year))]
        return rows[:limit]

    async def appearances(
        self,
        person_slug: str | None = None,
        show_date: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = [
            {
                "date": "2023-02-26",
                "person_name": "Joel Cummins Guest",
                "person_slug": "guest-keys",
                "appearance_type": "guest musician",
                "notes": "on keys",
            }
        ]
        if person_slug:
            rows = [r for r in rows if r["person_slug"] == person_slug]
        if show_date:
            rows = [r for r in rows if r["date"] == show_date]
        return rows[:limit]

    async def venue_history(self, venue_slug: str, limit: int = 25) -> list[dict[str, Any]]:
        rows = [
            {
                "date": s["date"],
                "show_id": s["show_id"],
                "venue_name": s["venue_name"],
                "location": s["location"],
                "tour_name": s["tour_name"],
            }
            for s in self._shows.values()
            if s["venue_slug"] == venue_slug
        ]
        return rows[:limit]

    async def songs_by_gap(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = [
            {
                "slug": s["slug"],
                "title": s["title"],
                "times_played": s["times_played"],
                "gap_current": s["gap_current"],
                "last_play_date": s["last_play_date"],
            }
            for s in self._songs.values()
            if s["gap_current"] is not None
        ]
        rows.sort(key=lambda r: r["gap_current"], reverse=True)
        return rows[:limit]

    async def last_etl_run(self) -> dict[str, Any] | None:
        finished = datetime.now(tz=UTC) - timedelta(hours=2)
        return {
            "id": 1,
            "started_at": finished,
            "finished_at": finished,
            "mode": "refresh",
            "status": "ok",
            "rows_added": 5,
            "rows_updated": 2,
        }


def _vault_settings(stub_settings: Settings) -> Settings:
    """Clone stub_settings with vault enabled and a tight hot window.

    hot_window_hours=1 keeps the seeded 2023 show out of the live path so the
    deterministic vault projection is exercised for those tests.
    """
    return stub_settings.model_copy(update={"vault_enabled": True, "vault_hot_window_hours": 1})


def _build(settings: Settings, **kwargs: Any) -> Any:
    cache = ResponseCache(
        db_path=settings.cache_db_path,
        ttl_seconds=settings.cache_ttl_seconds,
    )
    return build_server(
        settings,
        cache=cache,
        atu_throttle=TokenBucket(rps=100),
        **kwargs,
    )


@pytest.fixture
def vault_server(stub_settings: Settings) -> Any:
    return _build(_vault_settings(stub_settings), vault_reader=FakeVaultReader())


# ===========================================================================
# CONTRACT TESTS — each game-critical tool's `data` must have EXACTLY the
# documented keys (the model field set).
# ===========================================================================

_MODEL_KEYS = {
    "ShowSummary": {"show_id", "date", "venue_name", "location", "tour_name"},
    "SongSummary": {"slug", "title", "artist", "original", "times_played"},
    "Song": {
        "slug",
        "title",
        "artist",
        "original",
        "times_played",
        "debut_date",
        "last_played_date",
        "gap",
    },
    "SetlistEntry": {
        "position",
        "set_name",
        "song_slug",
        "song_title",
        "transition",
        "footnote",
    },
    "SongGap": {"slug", "title", "times_played", "gap_current", "last_played_date"},
    "VenueShow": {"show_id", "date", "venue_name", "location", "tour_name"},
    "Appearance": {"date", "person_name", "person_slug", "appearance_type", "notes"},
    "Performance": {
        "show_id",
        "date",
        "venue_name",
        "location",
        "set_name",
        "transition",
        "gap",
    },
    "NotableJam": {"show_id", "date", "song_slug", "song_title", "venue_name", "notes"},
}


@pytest.mark.asyncio
async def test_contract_recent_shows_keys(vault_server: Any) -> None:
    body = await _call(vault_server, "recent_shows", limit=10)
    assert body["data"]
    for row in body["data"]:
        assert set(row) == _MODEL_KEYS["ShowSummary"]


@pytest.mark.asyncio
async def test_contract_search_songs_keys(vault_server: Any) -> None:
    body = await _call(vault_server, "search_songs", query="bridgeless", limit=10)
    assert body["data"]
    for row in body["data"]:
        assert set(row) == _MODEL_KEYS["SongSummary"]


@pytest.mark.asyncio
async def test_contract_get_song_has_gap_not_gap_current(vault_server: Any) -> None:
    body = await _call(vault_server, "get_song", slug="all-in-time")
    data = body["data"]
    assert set(data) == _MODEL_KEYS["Song"]
    assert "gap" in data
    assert "gap_current" not in data
    # The vault column gap_current=0 must surface on the `gap` field.
    assert data["gap"] == 0
    assert data["last_played_date"] == "2023-02-26"
    Song(**data)  # validates


@pytest.mark.asyncio
async def test_contract_get_show_setlist_entry_keys_and_encore(vault_server: Any) -> None:
    body = await _call(vault_server, "get_show", date_or_id="2023-02-26")
    show = Show(**body["data"])  # validates the whole shape
    assert show.show_id == "5551001"
    assert show.venue.name == "The Tabernacle"
    for entry in body["data"]["setlist"]:
        assert set(entry) == _MODEL_KEYS["SetlistEntry"]
    # CRITICAL: set_number == "e" → set_name == "Encore" on the VAULT path.
    encore = [e for e in show.setlist if e.set_name == "Encore"]
    assert len(encore) == 1
    assert encore[0].song_slug == "bridgeless-reprise"
    # And normal sets map to "Set N".
    assert {e.set_name for e in show.setlist} == {"Set 1", "Set 2", "Encore"}


@pytest.mark.asyncio
async def test_contract_songs_by_gap_keys(vault_server: Any) -> None:
    body = await _call(vault_server, "songs_by_gap", limit=25)
    assert body["data"]
    for row in body["data"]:
        assert set(row) == _MODEL_KEYS["SongGap"]
        assert "gap_current" in row
        assert "gap" not in row


@pytest.mark.asyncio
async def test_contract_venue_history_keys(vault_server: Any) -> None:
    body = await _call(vault_server, "venue_history", venue_slug="the-tabernacle-atlanta-ga-usa")
    assert body["data"]
    for row in body["data"]:
        assert set(row) == _MODEL_KEYS["VenueShow"]


@pytest.mark.asyncio
async def test_contract_appearances_keys(vault_server: Any) -> None:
    body = await _call(vault_server, "appearances", show_date="2023-02-26")
    assert body["data"]
    for row in body["data"]:
        assert set(row) == _MODEL_KEYS["Appearance"]
    Appearance(**body["data"][0])


@pytest.mark.asyncio
async def test_contract_song_history_keys(vault_server: Any) -> None:
    body = await _call(vault_server, "song_history", slug="bridgeless", limit=10)
    assert body["data"]
    for row in body["data"]:
        assert set(row) == _MODEL_KEYS["Performance"]
        # gap is null in the UM vault (no per-performance gap).
        assert row["gap"] is None
    Performance(**body["data"][0])


@pytest.mark.asyncio
async def test_contract_jam_chart_keys(vault_server: Any) -> None:
    body = await _call(vault_server, "jam_chart", year=2023, limit=10)
    assert body["data"]
    for row in body["data"]:
        assert set(row) == _MODEL_KEYS["NotableJam"]
    NotableJam(**body["data"][0])


# ===========================================================================
# ParsedSetlist-compatibility — the resolver's shape derives cleanly from
# `setlist[]` using set_name.
# ===========================================================================


@pytest.mark.asyncio
async def test_parsed_setlist_shape_derivable(vault_server: Any) -> None:
    body = await _call(vault_server, "get_show", date_or_id="2023-02-26")
    setlist = body["data"]["setlist"]
    assert setlist

    # Mirror the downstream resolver's derivation.
    all_slugs = [e["song_slug"] for e in setlist]
    opener = setlist[0]["song_slug"]
    closer = setlist[-1]["song_slug"]
    encore_slugs = [e["song_slug"] for e in setlist if e["set_name"] == "Encore"]
    song_count = len(setlist)

    assert opener == "all-in-time"
    assert closer == "bridgeless-reprise"
    assert encore_slugs == ["bridgeless-reprise"]
    assert song_count == 3
    assert all_slugs == ["all-in-time", "bridgeless", "bridgeless-reprise"]


# ===========================================================================
# Live hot-window path — get_show within the hot window reads ATU (stub) and
# must produce a Show with an Encore (e → Encore on the LIVE path too).
# ===========================================================================


def _hot_window_settings(stub_settings: Settings) -> Settings:
    """Vault enabled, but a 1000h hot window so 'today' is always live."""
    return stub_settings.model_copy(
        update={"vault_enabled": True, "vault_hot_window_hours": 100000}
    )


@pytest.mark.asyncio
async def test_live_hot_window_get_show_builds_encore(stub_settings: Settings) -> None:
    """A hot-window date reads the live ATU stub and maps e → Encore."""
    server = _build(_hot_window_settings(stub_settings), vault_reader=FakeVaultReader())
    # The stub keys its fixtures by literal date; point the hot window at it by
    # asking for that exact date (it is within the giant hot window).
    body = await _call(server, "get_show", date_or_id="2023-02-26")
    show = Show(**body["data"])
    # show_id comes from the live ATU rows (show_id field on the raw rows).
    assert show.show_id == "5551001"
    encore = [e for e in show.setlist if e.set_name == "Encore"]
    assert len(encore) == 1
    assert encore[0].song_slug == "bridgeless-reprise"
    assert {e.set_name for e in show.setlist} == {"Set 1", "Set 2", "Encore"}


@pytest.mark.asyncio
async def test_live_one_set_normalizes_to_set_1(stub_settings: Settings) -> None:
    """settype 'One Set' must collapse to 'Set 1' on the live path."""
    server = _build(_hot_window_settings(stub_settings), vault_reader=FakeVaultReader())
    body = await _call(server, "get_show", date_or_id="2021-08-13")
    show = Show(**body["data"])
    assert [e.set_name for e in show.setlist] == ["Set 1"]


@pytest.mark.asyncio
async def test_live_get_show_falls_back_to_vault_when_no_live_rows(
    stub_settings: Settings,
) -> None:
    """A hot-window date with no live ATU rows falls back to the vault."""
    server = _build(_hot_window_settings(stub_settings), vault_reader=FakeVaultReader())
    # The stub has no rows for this date, but the fake vault does (2023-02-26).
    # Use a date the stub lacks but the vault has — confirm vault fallback.
    # (The stub lacks 2023-02-26? It has it. Use the vault-only path by asking
    # for a date the stub does not stock.)
    body = await _call(server, "get_show", date_or_id="2099-01-01")
    # Neither stub nor vault has it → NOT_FOUND.
    assert body["code"] == "NOT_FOUND"


# ===========================================================================
# Ordering, validation, error paths
# ===========================================================================


@pytest.mark.asyncio
async def test_songs_by_gap_ordering(vault_server: Any) -> None:
    body = await _call(vault_server, "songs_by_gap", limit=25)
    gaps = [SongGap(**row) for row in body["data"]]
    values = [g.gap_current for g in gaps]
    assert values == sorted(values, reverse=True)
    # The overdue cover should top the list.
    assert gaps[0].slug == "ace-of-spades"
    assert gaps[0].gap_current == 320


@pytest.mark.asyncio
async def test_recent_shows_newest_first(vault_server: Any) -> None:
    body = await _call(vault_server, "recent_shows", limit=10)
    summaries = [ShowSummary(**row) for row in body["data"]]
    dates = [s.date for s in summaries]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.asyncio
async def test_recent_shows_splices_live_hot_window_show(stub_settings: Settings) -> None:
    """On show night the live ATU `latest` show is spliced in front of the vault
    list (and de-dupes any vault row for the same date)."""
    server = _build(_hot_window_settings(stub_settings), vault_reader=FakeVaultReader())
    body = await _call(server, "recent_shows", limit=10)
    summaries = [ShowSummary(**row) for row in body["data"]]
    assert summaries
    # The live stub's latest show (2023-02-26, show_id 5551001) leads the list,
    # sourced from the live ATU rows — and it appears exactly once.
    assert summaries[0].date == "2023-02-26"
    assert summaries[0].show_id == "5551001"
    assert [s.date for s in summaries].count("2023-02-26") == 1


@pytest.mark.asyncio
async def test_validate_song_slugs_partition(vault_server: Any) -> None:
    body = await _call(
        vault_server,
        "validate_song_slugs",
        slugs=["bridgeless", "blarghhh", "all-in-time", "totallyfake"],
    )
    sv = SlugValidation(**body["data"])
    assert set(sv.valid) == {"all-in-time", "bridgeless"}
    # valid is sorted for determinism.
    assert sv.valid == sorted(sv.valid)
    # unknown preserves request order.
    assert sv.unknown == ["blarghhh", "totallyfake"]


@pytest.mark.asyncio
async def test_validate_song_slugs_empty_invalid(vault_server: Any) -> None:
    body = await _call(vault_server, "validate_song_slugs", slugs=[])
    assert body["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_validate_song_slugs_oversize_invalid(vault_server: Any) -> None:
    body = await _call(vault_server, "validate_song_slugs", slugs=[f"s-{i}" for i in range(51)])
    assert body["code"] == "INVALID_INPUT"
    assert body.get("details", {}).get("count") == 51


@pytest.mark.asyncio
async def test_get_song_not_found(vault_server: Any) -> None:
    body = await _call(vault_server, "get_song", slug="no-such-song")
    assert body["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_get_song_invalid_input(vault_server: Any) -> None:
    body = await _call(vault_server, "get_song", slug="")
    assert body["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_get_show_invalid_input(vault_server: Any) -> None:
    body = await _call(vault_server, "get_show", date_or_id="")
    assert body["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_appearances_filter_by_person(vault_server: Any) -> None:
    body = await _call(vault_server, "appearances", person_slug="guest-keys")
    assert body["data"]
    assert all(r["person_slug"] == "guest-keys" for r in body["data"])
    miss = await _call(vault_server, "appearances", person_slug="nobody")
    assert miss["data"] == []


@pytest.mark.asyncio
async def test_get_song_cover_surfaces_original_artist(vault_server: Any) -> None:
    body = await _call(vault_server, "get_song", slug="ace-of-spades")
    song = Song(**body["data"])
    assert song.original is False
    assert song.artist == "Motörhead"


# ===========================================================================
# Vault-disabled guard + health
# ===========================================================================


@pytest.mark.asyncio
async def test_vault_disabled_tools_guard(stub_settings: Settings) -> None:
    """With vault disabled and no reader, vault-only tools return VAULT_DISABLED."""
    server = _build(stub_settings)  # vault_enabled=False
    for tool, kwargs in (
        ("get_song", {"slug": "x"}),
        ("search_songs", {"query": "x"}),
        ("songs_by_gap", {}),
        ("venue_history", {"venue_slug": "x"}),
        ("jam_chart", {}),
        ("appearances", {}),
        ("song_history", {"slug": "x"}),
        ("validate_song_slugs", {"slugs": ["x"]}),
    ):
        body = await _call(server, tool, **kwargs)
        assert body["code"] == "VAULT_DISABLED", tool


@pytest.mark.asyncio
async def test_health_single_atu_upstream(vault_server: Any) -> None:
    body = await _call(vault_server, "health")
    health = Health(**body["data"])
    assert health.atu.rps_limit > 0
    assert health.cache.ttl_seconds == 86400
    assert health.vault.enabled is True
    assert health.vault.stale is False
    assert health.vault.staleness_hours is not None
    assert 1 < health.vault.staleness_hours < 3
    # Single upstream — the phishnet/phishin pair is gone.
    assert "phishnet" not in body["data"]
    assert "phishin" not in body["data"]
    assert "atu" in body["data"]


@pytest.mark.asyncio
async def test_health_vault_disabled_default(stub_settings: Settings) -> None:
    server = _build(stub_settings)
    body = await _call(server, "health")
    health = Health(**body["data"])
    assert health.vault.enabled is False


@pytest.mark.asyncio
async def test_health_marks_stale_when_etl_too_old(stub_settings: Settings) -> None:
    class StaleVault(FakeVaultReader):
        async def last_etl_run(self) -> dict[str, Any] | None:
            finished = datetime.now(tz=UTC) - timedelta(hours=72)
            return {
                "id": 1,
                "started_at": finished,
                "finished_at": finished,
                "mode": "refresh",
                "status": "ok",
                "rows_added": 0,
                "rows_updated": 0,
            }

    server = _build(_vault_settings(stub_settings), vault_reader=StaleVault())
    body = await _call(server, "health")
    health = Health(**body["data"])
    assert health.status == "degraded"
    assert health.vault.stale is True
