"""MCP Umphreys — wraps the umphreys-vault Postgres behind a typed tool surface.

Eleven tools across four domains:

* shows  — recent_shows, get_show, venue_history
* songs  — search_songs, get_song, songs_by_gap, song_history, validate_song_slugs
* native — jam_chart, appearances
* meta   — health

The vault (umphreys-vault Postgres) is Umphrey's source of truth. The live ATU
v2 API is consulted ONLY for hot-window shows (within VAULT_HOT_WINDOW_HOURS of
now) so the downstream game's resolver sees an in-progress setlist grow on show
night instead of a frozen vault snapshot.

Returns are projected through the public Pydantic models in ``models.py`` so
the wire format stays identical across stub mode, live mode, and the vault. The
shapes are byte-for-byte compatible with the mcp-phish contract the downstream
game already parses.

Transport: Streamable HTTP via FastMCP (current MCP spec).
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from fastmcp import FastMCP
from pydantic import BaseModel

from mcp_umphreys import __version__
from mcp_umphreys.cache import ResponseCache
from mcp_umphreys.clients.atu import ATUError
from mcp_umphreys.clients.stubs import StubATUClient
from mcp_umphreys.config import Settings, load_settings
from mcp_umphreys.logging_setup import configure_logging
from mcp_umphreys.models import (
    Appearance,
    CacheHealth,
    Health,
    NotableJam,
    Performance,
    SetlistEntry,
    Show,
    ShowSummary,
    SlugValidation,
    Song,
    SongGap,
    SongSummary,
    UpstreamHealth,
    VaultHealth,
    Venue,
    VenueShow,
)
from mcp_umphreys.throttle import TokenBucket
from mcp_umphreys.vault import VaultReader

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger("mcp_umphreys.server")


# ---------------------------------------------------------------------------
# Client protocol (so the stub and real ATU client are duck-type compatible)
# ---------------------------------------------------------------------------


class _ATULike(Protocol):
    async def setlists_by_date(self, date: str) -> list[dict[str, Any]]: ...
    async def latest(self) -> list[dict[str, Any]]: ...
    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# Set-label normalization (shared by the vault AND live ATU paths)
#
# The downstream resolver detects encores by ``set_name == "Encore"``, so the
# ATU ``set_number == "e"`` MUST become "Encore" on BOTH read paths. settype
# "One Set" collapses to "Set 1". Other digits get "Set {n}"; anything else
# passes through unchanged.
# ---------------------------------------------------------------------------

_SET_LABEL_MAP: dict[str, str] = {
    "1": "Set 1",
    "2": "Set 2",
    "3": "Set 3",
    "4": "Set 4",
    "e": "Encore",
}


def _set_label(set_number: Any, set_type: Any = None) -> str:
    """Map a raw ATU (set_number, set_type) pair to a public set label.

    * ``set_number == "e"`` → ``"Encore"`` (encore detection — load-bearing).
    * settype ``"One Set"`` → ``"Set 1"`` regardless of set_number.
    * known digits 1-4 → ``"Set N"``.
    * any other digit → ``"Set {n}"``.
    * anything else → the raw value (already a label, or empty).
    """
    raw_type = _safe_str(set_type).strip()
    if raw_type.lower() == "one set":
        return "Set 1"
    raw = _safe_str(set_number).strip()
    if raw in _SET_LABEL_MAP:
        return _SET_LABEL_MAP[raw]
    if raw.isdigit():
        return f"Set {raw}"
    return raw


# ---------------------------------------------------------------------------
# Response envelope helpers (Standard Error Contract — verbatim from mcp-phish)
# ---------------------------------------------------------------------------


def _ok(data: Any) -> str:
    """Serialize a ``data`` payload. Pydantic models flatten via ``model_dump``."""
    return json.dumps({"data": _to_jsonable(data)}, indent=2, default=str)


def _err(message: str, code: str, **details: Any) -> str:
    """Serialize the standard failure shape."""
    payload: dict[str, Any] = {"error": message, "code": code}
    if details:
        payload["details"] = details
    return json.dumps(payload, indent=2, default=str)


def _to_jsonable(obj: Any) -> Any:
    """Recursively convert pydantic models / sequences into JSON-friendly types."""
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_to_jsonable(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Small coercion helpers
# ---------------------------------------------------------------------------


def _safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _location(city: Any, state: Any) -> str:
    c = _safe_str(city)
    s = _safe_str(state)
    if c and s:
        return f"{c}, {s}"
    return c or s


# ---------------------------------------------------------------------------
# Live ATU projections (raw ATU setlist rows → public models)
#
# Used only on the hot-window path. ATU setlist rows are denormalized: show,
# venue, and tour are repeated on every row. Field names match the live
# `setlists` rows: setnumber, settype, position, slug, songname, transition,
# footnote, venuename, city, state, country, showtitle, show_id, showdate,
# tourname.
# ---------------------------------------------------------------------------


def _atu_show_full(rows: list[dict[str, Any]]) -> Show | None:
    """Build a Show from raw ATU setlist rows for one date. None if no rows."""
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: _safe_int(r.get("position")))
    head = rows[0]
    setlist = [
        SetlistEntry(
            position=_safe_int(row.get("position")),
            set_name=_set_label(row.get("setnumber"), row.get("settype")),
            song_slug=_safe_str(row.get("slug")),
            song_title=_safe_str(row.get("songname")),
            transition=_safe_str(row.get("transition")).strip(),
            footnote=_safe_str(row.get("footnote")),
        )
        for row in rows
    ]
    venue = Venue(
        slug="",
        name=_safe_str(head.get("venuename")),
        city=_safe_str(head.get("city")),
        state=_safe_str(head.get("state")),
        country=_safe_str(head.get("country")),
        location=_location(head.get("city"), head.get("state")),
    )
    return Show(
        show_id=_safe_str(head.get("show_id")),
        date=_safe_str(head.get("showdate")),
        venue=venue,
        tour_name=_safe_str(head.get("tourname")),
        setlist=setlist,
    )


def _atu_show_summary(rows: list[dict[str, Any]]) -> ShowSummary | None:
    """Build a ShowSummary from raw ATU setlist rows for one date."""
    if not rows:
        return None
    head = sorted(rows, key=lambda r: _safe_int(r.get("position")))[0]
    return ShowSummary(
        show_id=_safe_str(head.get("show_id")),
        date=_safe_str(head.get("showdate")),
        venue_name=_safe_str(head.get("venuename")),
        location=_location(head.get("city"), head.get("state")),
        tour_name=_safe_str(head.get("tourname")),
    )


# ---------------------------------------------------------------------------
# Vault projections (asyncpg.Record → frozen Pydantic models)
# ---------------------------------------------------------------------------


def _vault_show_summary(row: Any) -> ShowSummary:
    return ShowSummary(
        show_id=_safe_str(row.get("show_id")),
        date=str(row["date"]),
        venue_name=_safe_str(row.get("venue_name")),
        location=_safe_str(row.get("location")),
        tour_name=_safe_str(row.get("tour_name")),
    )


def _vault_show_full(show_row: Any, setlist_rows: list[Any]) -> Show:
    setlist = [
        SetlistEntry(
            position=_safe_int(row.get("position")),
            set_name=_set_label(row.get("set_number"), row.get("set_type")),
            song_slug=_safe_str(row.get("song_slug")),
            song_title=_safe_str(row.get("song_name")),
            transition=_safe_str(row.get("transition")).strip(),
            footnote=_safe_str(row.get("footnote")),
        )
        for row in setlist_rows
    ]
    venue = Venue(
        slug=_safe_str(show_row.get("venue_slug")),
        name=_safe_str(show_row.get("venue_name")),
        city=_safe_str(show_row.get("city")),
        state=_safe_str(show_row.get("state")),
        country=_safe_str(show_row.get("country")),
        location=_safe_str(show_row.get("location")),
        latitude=_safe_float(show_row.get("latitude")),
        longitude=_safe_float(show_row.get("longitude")),
    )
    return Show(
        show_id=_safe_str(show_row.get("show_id")),
        date=str(show_row["date"]),
        venue=venue,
        tour_name=_safe_str(show_row.get("tour_name")),
        setlist=setlist,
    )


def _vault_song_summary(row: Any) -> SongSummary:
    # Umphrey's catalog has no single `artist` column. Surface the cover
    # source (`original_artist`) when the song is a cover, else None.
    is_original = bool(row.get("original", True))
    artist = row.get("original_artist") or None
    return SongSummary(
        slug=_safe_str(row.get("slug")),
        title=_safe_str(row.get("title")),
        artist=artist,
        original=is_original,
        times_played=_safe_int(row.get("times_played")),
    )


def _vault_song_full(row: Any) -> Song:
    # CRITICAL: the vault column is `gap_current`; project it onto the model
    # field named `gap` (the downstream game normalizes gap → gap_current).
    debut = row.get("debut_date")
    last_played = row.get("last_play_date")
    gap = row.get("gap_current")
    is_original = bool(row.get("original", True))
    artist = row.get("original_artist") or None
    return Song(
        slug=_safe_str(row.get("slug")),
        title=_safe_str(row.get("title")),
        artist=artist,
        original=is_original,
        times_played=_safe_int(row.get("times_played")),
        debut_date=str(debut) if debut is not None else None,
        last_played_date=str(last_played) if last_played is not None else None,
        gap=_safe_int(gap) if gap is not None else None,
    )


def _vault_performance(row: Any) -> Performance:
    gap = row.get("gap")
    return Performance(
        show_id=_safe_str(row.get("show_id")),
        date=str(row["date"]),
        venue_name=_safe_str(row.get("venue_name")),
        location=_safe_str(row.get("venue_location")),
        set_name=_set_label(row.get("set_number"), row.get("set_type")),
        transition=_safe_str(row.get("transition")).strip(),
        gap=_safe_int(gap) if gap is not None else None,
    )


def _vault_jam(row: Any) -> NotableJam:
    return NotableJam(
        show_id=_safe_str(row.get("show_id")),
        date=str(row["date"]),
        song_slug=_safe_str(row.get("song_slug")),
        song_title=_safe_str(row.get("song_name")),
        venue_name=_safe_str(row.get("venue_name")),
        notes=_safe_str(row.get("notes")),
    )


def _vault_appearance(row: Any) -> Appearance:
    return Appearance(
        date=str(row["date"]),
        person_name=_safe_str(row.get("person_name")),
        person_slug=_safe_str(row.get("person_slug")),
        appearance_type=_safe_str(row.get("appearance_type")),
        notes=_safe_str(row.get("notes")),
    )


def _vault_venue_show(row: Any) -> VenueShow:
    return VenueShow(
        show_id=_safe_str(row.get("show_id")),
        date=str(row["date"]),
        venue_name=_safe_str(row.get("venue_name")),
        location=_safe_str(row.get("location")),
        tour_name=_safe_str(row.get("tour_name")),
    )


def _vault_song_gap(row: Any) -> SongGap:
    last_played = row.get("last_play_date")
    return SongGap(
        slug=_safe_str(row.get("slug")),
        title=_safe_str(row.get("title")),
        times_played=_safe_int(row.get("times_played")),
        gap_current=_safe_int(row.get("gap_current")),
        last_played_date=str(last_played) if last_played is not None else None,
    )


# ---------------------------------------------------------------------------
# Cache key helper
# ---------------------------------------------------------------------------


def _ckey_atu(method: str, **params: Any) -> tuple[str, dict[str, Any]]:
    return (f"atu:{method}", params)


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def build_server(
    settings: Settings,
    *,
    atu_client: _ATULike | None = None,
    cache: ResponseCache | None = None,
    atu_throttle: TokenBucket | None = None,
    vault_reader: VaultReader | None = None,
    vault_pool: asyncpg.Pool | None = None,
) -> FastMCP:
    """Build a fully-wired FastMCP instance.

    Tests can pass in their own stubs/throttle/cache/vault_reader to keep
    behavior isolated. Production calls ``main()`` which relies on lazy-init
    for the vault pool.

    ``vault_reader`` takes precedence over ``vault_pool`` when both are given.
    When ``vault_pool`` is given, a ``VaultReader`` is constructed from it.
    When neither is given and ``settings.vault_enabled`` is True, the pool is
    created lazily on the first vault read.
    """
    atu_tb = atu_throttle or TokenBucket(rps=settings.throttle_atu_rps)

    atu: _ATULike
    if atu_client is not None:
        atu = atu_client
    elif settings.stub_mode:
        atu = StubATUClient()
    else:
        # Lazy import keeps module-level surface clean for tests.
        from mcp_umphreys.clients.atu import ATUClient

        atu = ATUClient(
            throttle=atu_tb,
            base_url=settings.atu_base_url,
            artist_id=settings.atu_artist_id,
        )

    response_cache = cache or ResponseCache(
        db_path=settings.cache_db_path,
        ttl_seconds=settings.cache_ttl_seconds,
    )

    # --- vault reader setup ------------------------------------------------
    # Priority: explicit vault_reader > vault_pool > lazy-init on first use
    _vault_reader: VaultReader | None
    if vault_reader is not None:
        _vault_reader = vault_reader
    elif vault_pool is not None:
        _vault_reader = VaultReader(vault_pool)
    else:
        _vault_reader = None  # will be created lazily if vault_enabled

    _lazy_pool_holder: list[Any] = [None]  # mutable cell for lazy pool

    async def _get_vault_reader() -> VaultReader | None:
        """Return the VaultReader, lazily initialising the pool when needed."""
        if _vault_reader is not None:
            return _vault_reader
        if not settings.vault_enabled:
            return None
        if _lazy_pool_holder[0] is None:
            try:
                import asyncpg as _asyncpg

                _lazy_pool_holder[0] = await _asyncpg.create_pool(
                    settings.pg_dsn,
                    min_size=1,
                    max_size=5,
                )
                logger.info("vault pool created", extra={"dsn_host": settings.pg_host})
            except Exception:
                logger.exception("failed to create vault pool")
                return None
        return VaultReader(_lazy_pool_holder[0])

    def _is_hot_window(date_str: str) -> bool:
        """Return True if show date is within vault_hot_window_hours of now."""
        try:
            show_dt = datetime.fromisoformat(date_str)
            if show_dt.tzinfo is None:
                show_dt = show_dt.replace(tzinfo=UTC)
            age_hours = (datetime.now(tz=UTC) - show_dt).total_seconds() / 3600
            return age_hours < settings.vault_hot_window_hours
        except (ValueError, OverflowError):
            return False

    mcp = FastMCP("Umphreys")
    started_at = time.time()

    async def _cached_atu(
        endpoint: str,
        params: dict[str, Any],
        call: Any,
        *,
        ttl_override: int | None = None,
    ) -> Any:
        await response_cache.init()
        cache_key, cache_params = _ckey_atu(endpoint, **params)
        hit = await response_cache.get(cache_key, cache_params, ttl_override=ttl_override)
        if hit is not None:
            return hit
        payload = await call()
        await response_cache.put(cache_key, cache_params, payload)
        return payload

    # ------------------------------------------------------------------
    # Show tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def recent_shows(limit: int = 10) -> str:
        """List the most recent Umphrey's McGee shows, newest first.

        Args:
            limit: Max rows to return. Default 10. Capped at 100.

        Returns:
            JSON ``{"data": [ShowSummary, ...]}``, ordered most-recent-first.
            Each ShowSummary has ``show_id, date, venue_name, location,
            tour_name``.

        On show night the newest show reads LIVE from ATU (hot window) so an
        in-progress show surfaces immediately; older shows read from the vault.

        Idempotent. Example: ``recent_shows(limit=5)``.
        """
        capped = max(1, min(int(limit), 100))

        # Hot-window: the newest show may be in progress tonight. Pull the live
        # ATU `latest` row and splice it in front of the vault list so the
        # resolver sees a growing setlist.
        live_summary: ShowSummary | None = None
        with contextlib.suppress(Exception):
            live_rows = await _cached_atu(
                "latest",
                {},
                atu.latest,
                ttl_override=settings.hot_window_cache_ttl_seconds,
            )
            rows = live_rows if isinstance(live_rows, list) else []
            if rows:
                head_date = _safe_str(rows[0].get("showdate"))
                if head_date and _is_hot_window(head_date):
                    live_summary = _atu_show_summary(rows)

        vr = await _get_vault_reader()
        if vr is not None:
            try:
                vault_rows = await vr.recent_shows(limit=capped)
                summaries = [_vault_show_summary(row) for row in vault_rows]
                if live_summary is not None:
                    # Replace any vault row for the same date, then prepend live.
                    summaries = [s for s in summaries if s.date != live_summary.date]
                    summaries = [live_summary, *summaries][:capped]
                return _ok(summaries)
            except Exception:
                logger.exception("vault recent_shows failed; serving live-only")

        # Vault unavailable: serve whatever the live hot-window read produced.
        return _ok([live_summary] if live_summary is not None else [])

    @mcp.tool()
    async def get_show(date_or_id: str) -> str:
        """Get a single Umphrey's show with full setlist and venue.

        ``date_or_id`` may be a YYYY-MM-DD date or an ATU show id.

        Args:
            date_or_id: ``"2023-02-26"`` or a numeric ATU show id string.

        Returns:
            JSON ``{"data": Show}``. ``Show`` has ``show_id, date, venue,
            tour_name, setlist[]``. Each SetlistEntry has ``position, set_name,
            song_slug, song_title, transition, footnote``. ``set_number == "e"``
            projects to ``set_name == "Encore"`` (encore detection).

        A show inside the hot window reads LIVE from ATU so an in-progress
        setlist grows on show night; historical shows read from the vault.

        Idempotent. Example: ``get_show("2023-02-26")``.
        """
        if not date_or_id:
            return _err("date_or_id is required", "INVALID_INPUT")
        is_date = len(date_or_id) == 10 and date_or_id.count("-") == 2

        # Hot-window live read (date keys only — ATU is keyed by show date).
        if is_date and _is_hot_window(date_or_id):
            try:
                live_rows = await _cached_atu(
                    "setlists_by_date",
                    {"date": date_or_id},
                    lambda: atu.setlists_by_date(date_or_id),
                    ttl_override=settings.hot_window_cache_ttl_seconds,
                )
                rows = live_rows if isinstance(live_rows, list) else []
                show = _atu_show_full(rows)
                if show is not None:
                    return _ok(show)
                # No live rows yet (show not started): fall through to vault.
            except ATUError:
                logger.exception("live get_show failed; falling back to vault")

        vr = await _get_vault_reader()
        if vr is not None:
            try:
                show_row, setlist_rows = await vr.get_show(date_or_id)
                if show_row is None:
                    return _err(f"show not found: {date_or_id}", "NOT_FOUND")
                return _ok(_vault_show_full(show_row, setlist_rows))
            except Exception:
                logger.exception("vault get_show failed", extra={"date_or_id": date_or_id})
                return _err("vault read failed", "VAULT_ERROR")
        return _err(f"show not found: {date_or_id}", "NOT_FOUND")

    @mcp.tool()
    async def venue_history(venue_slug: str, limit: int = 25) -> str:
        """List all shows at a venue, most recent first. Requires vault.

        Args:
            venue_slug: ATU venue slug (e.g. ``"the-tabernacle-atlanta-ga-usa"``).
            limit: Max rows to return. Default 25, capped at 200.

        Returns:
            JSON ``{"data": [VenueShow, ...]}``. Each VenueShow has
            ``show_id, date, venue_name, location, tour_name``.

        Vault-only. Returns ``VAULT_DISABLED`` error if vault is not enabled.
        Idempotent. Example: ``venue_history("red-rocks-amphitheatre-morrison-co-usa")``.
        """
        vr = await _get_vault_reader()
        if vr is None:
            return _err("venue_history requires vault (VAULT_ENABLED=true)", "VAULT_DISABLED")
        if not venue_slug:
            return _err("venue_slug is required", "INVALID_INPUT")
        capped = max(1, min(int(limit), 200))
        try:
            rows = await vr.venue_history(venue_slug=venue_slug, limit=capped)
            return _ok([_vault_venue_show(row) for row in rows])
        except Exception as exc:
            logger.exception("venue_history failed", extra={"venue_slug": venue_slug})
            return _err(str(exc), "VAULT_ERROR")

    # ------------------------------------------------------------------
    # Song tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def search_songs(query: str, limit: int = 25) -> str:
        """Search the Umphrey's song catalog by title fragment.

        Args:
            query: Substring matched against song title (case-insensitive).
            limit: Max rows to return. Default 25.

        Returns:
            JSON ``{"data": [SongSummary, ...]}``. Each SongSummary has
            ``slug, title, artist, original, times_played``.

        Idempotent. Example: ``search_songs("bridgeless", limit=5)``.
        """
        if not query:
            return _err("query is required", "INVALID_INPUT")
        capped = max(1, min(int(limit), 200))
        vr = await _get_vault_reader()
        if vr is None:
            return _err("search_songs requires vault (VAULT_ENABLED=true)", "VAULT_DISABLED")
        try:
            rows = await vr.search_songs(query=query, limit=capped)
            return _ok([_vault_song_summary(row) for row in rows])
        except Exception as exc:
            logger.exception("search_songs failed", extra={"query": query})
            return _err(str(exc), "VAULT_ERROR")

    @mcp.tool()
    async def get_song(slug: str) -> str:
        """Get a single song's catalog record (debut, last play, gap, total).

        Args:
            slug: ATU song slug (e.g. ``"all-in-time"``, ``"bridgeless"``).

        Returns:
            JSON ``{"data": Song}``. Song fields: slug, title, artist,
            original, times_played, debut_date, last_played_date, gap.
            NOTE: the field is ``gap`` (the vault column ``gap_current`` is
            projected onto it); the downstream game normalizes it back.

        Idempotent. Example: ``get_song("all-in-time")``.
        """
        if not slug:
            return _err("slug is required", "INVALID_INPUT")
        vr = await _get_vault_reader()
        if vr is None:
            return _err("get_song requires vault (VAULT_ENABLED=true)", "VAULT_DISABLED")
        try:
            row = await vr.get_song(slug)
            if row is None:
                return _err(f"song not found: {slug}", "NOT_FOUND")
            return _ok(_vault_song_full(row))
        except Exception as exc:
            logger.exception("get_song failed", extra={"slug": slug})
            return _err(str(exc), "VAULT_ERROR")

    @mcp.tool()
    async def songs_by_gap(limit: int = 25) -> str:
        """List songs ordered by current gap (shows since last play), descending.

        "Gap" means the number of shows since the song was last performed.
        High-gap songs are overdue; lower-gap songs were recently played.
        Only songs with a known gap are included.

        Args:
            limit: Max rows to return. Default 25, capped at 200.

        Returns:
            JSON ``{"data": [SongGap, ...]}``. Each SongGap has
            ``slug, title, times_played, gap_current, last_played_date``.
            Here the field IS ``gap_current`` (contrast get_song's ``gap``).

        Vault-only. Returns ``VAULT_DISABLED`` error if vault is not enabled.
        Idempotent. Example: ``songs_by_gap(limit=10)``.
        """
        vr = await _get_vault_reader()
        if vr is None:
            return _err("songs_by_gap requires vault (VAULT_ENABLED=true)", "VAULT_DISABLED")
        capped = max(1, min(int(limit), 200))
        try:
            rows = await vr.songs_by_gap(limit=capped)
            return _ok([_vault_song_gap(row) for row in rows])
        except Exception as exc:
            logger.exception("songs_by_gap failed")
            return _err(str(exc), "VAULT_ERROR")

    @mcp.tool()
    async def song_history(slug: str, limit: int = 50) -> str:
        """List every performance of a song, most-recent first.

        Args:
            slug: ATU song slug.
            limit: Max rows. Default 50, capped at 500.

        Returns:
            JSON ``{"data": [Performance, ...]}``. Each Performance has
            ``show_id, date, venue_name, location, set_name, transition, gap``.
            ``gap`` is null (Umphrey's vault stores no per-performance gap).

        Vault-only. Idempotent. Example: ``song_history("bridgeless", limit=20)``.
        """
        if not slug:
            return _err("slug is required", "INVALID_INPUT")
        capped = max(1, min(int(limit), 500))
        vr = await _get_vault_reader()
        if vr is None:
            return _err("song_history requires vault (VAULT_ENABLED=true)", "VAULT_DISABLED")
        try:
            rows = await vr.song_history(slug=slug, limit=capped)
            return _ok([_vault_performance(row) for row in rows])
        except Exception as exc:
            logger.exception("song_history failed", extra={"slug": slug})
            return _err(str(exc), "VAULT_ERROR")

    @mcp.tool()
    async def validate_song_slugs(slugs: list[str]) -> str:
        """Partition a list of song slugs into ``valid`` and ``unknown``.

        Useful for form validation in a downstream client (e.g. the setlist
        game's date-pick screen). One round-trip against the vault.

        Args:
            slugs: 1 to 50 candidate slugs (e.g. ``["all-in-time","bridgeless"]``).
                Empty or oversized lists return ``INVALID_INPUT``.

        Returns:
            JSON ``{"data": {"valid": [...], "unknown": [...]}}``.
            ``valid`` lists resolved slugs sorted for determinism; ``unknown``
            lists unresolved slugs in request order.

        Idempotent. Read-only. Example:
        ``validate_song_slugs(["all-in-time","blarghhh"])`` →
        ``{"valid": ["all-in-time"], "unknown": ["blarghhh"]}``.
        """
        if not isinstance(slugs, list) or len(slugs) == 0:
            return _err("slugs must be a non-empty list", "INVALID_INPUT")
        if len(slugs) > 50:
            return _err(
                f"too many slugs ({len(slugs)}); cap is 50",
                "INVALID_INPUT",
                count=len(slugs),
            )
        requested: list[str] = [str(s).strip() for s in slugs]
        if any(not s for s in requested):
            return _err("slugs must not contain empty strings", "INVALID_INPUT")

        vr = await _get_vault_reader()
        if vr is None:
            return _err("validate_song_slugs requires vault (VAULT_ENABLED=true)", "VAULT_DISABLED")
        try:
            found_set = await vr.validate_slugs(requested)
            valid_sorted = sorted(found_set)
            unknown = [s for s in requested if s not in found_set]
            return _ok(SlugValidation(valid=valid_sorted, unknown=unknown))
        except Exception as exc:
            logger.exception("validate_song_slugs failed")
            return _err(str(exc), "VAULT_ERROR")

    # ------------------------------------------------------------------
    # Umphrey's-native tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def jam_chart(year: int | None = None, limit: int = 50) -> str:
        """Return ATU's jam-chart entries — editorially flagged notable jams.

        Args:
            year: Optional four-digit year filter.
            limit: Max rows. Default 50, capped at 500.

        Returns:
            JSON ``{"data": [NotableJam, ...]}``. Each NotableJam has
            ``show_id, date, song_slug, song_title, venue_name, notes``.

        Vault-only. Idempotent. Example: ``jam_chart(year=2023, limit=10)``.
        """
        capped = max(1, min(int(limit), 500))
        vr = await _get_vault_reader()
        if vr is None:
            return _err("jam_chart requires vault (VAULT_ENABLED=true)", "VAULT_DISABLED")
        try:
            rows = await vr.jam_chart(year=year, limit=capped)
            return _ok([_vault_jam(row) for row in rows])
        except Exception as exc:
            logger.exception("jam_chart failed")
            return _err(str(exc), "VAULT_ERROR")

    @mcp.tool()
    async def appearances(
        person_slug: str | None = None,
        show_date: str | None = None,
        limit: int = 50,
    ) -> str:
        """List guest sit-ins / appearances (an Umphrey's-native capability).

        Filter by a guest (``person_slug``) or by a single show (``show_date``),
        or pass neither for the most recent sit-ins across the catalog.

        Args:
            person_slug: Optional guest slug to filter by.
            show_date: Optional ``YYYY-MM-DD`` to filter to one show.
            limit: Max rows. Default 50, capped at 500.

        Returns:
            JSON ``{"data": [Appearance, ...]}``. Each Appearance has
            ``date, person_name, person_slug, appearance_type, notes``.

        Vault-only. Idempotent. Example: ``appearances(show_date="2023-02-26")``.
        """
        capped = max(1, min(int(limit), 500))
        vr = await _get_vault_reader()
        if vr is None:
            return _err("appearances requires vault (VAULT_ENABLED=true)", "VAULT_DISABLED")
        slug = person_slug.strip() if person_slug else None
        date = show_date.strip() if show_date else None
        try:
            rows = await vr.appearances(person_slug=slug, show_date=date, limit=capped)
            return _ok([_vault_appearance(row) for row in rows])
        except Exception as exc:
            logger.exception("appearances failed")
            return _err(str(exc), "VAULT_ERROR")

    # ------------------------------------------------------------------
    # Meta tool
    # ------------------------------------------------------------------

    @mcp.tool()
    async def health() -> str:
        """Report server status: stub mode, ATU throttle state, cache + vault.

        Calling this tool never touches an upstream. It only reads in-process
        state and the local cache file (plus a quick vault ETL freshness read).

        Returns:
            JSON ``{"data": Health}``. Health.atu contains ``reachable,
            rps_limit, tokens_available, last_call_ts``. Health also surfaces
            the cache path/size/TTL, last hit/miss timestamps, and vault ETL
            freshness.

        Idempotent. Example: ``health()``.
        """

        def _iso(ts: float | None) -> str | None:
            if ts is None:
                return None
            return datetime.fromtimestamp(ts, tz=UTC).isoformat()

        with contextlib.suppress(Exception):  # pragma: no cover — surfaced as "degraded"
            await response_cache.init()

        atu_snap = atu_tb.snapshot()

        vault_health_status = "ok"
        vault_h: VaultHealth
        vr = await _get_vault_reader()
        if settings.vault_enabled:
            last_etl_iso: str | None = None
            staleness_hours: float | None = None
            is_stale = False
            if vr is not None:
                with contextlib.suppress(Exception):
                    etl_row = await vr.last_etl_run()
                    if etl_row is not None:
                        finished = etl_row.get("finished_at")
                        if finished is not None:
                            if isinstance(finished, datetime):
                                last_etl_iso = finished.isoformat()
                                staleness_hours = (
                                    datetime.now(tz=UTC) - finished
                                ).total_seconds() / 3600
                            else:
                                last_etl_iso = str(finished)
                            if staleness_hours is not None and (
                                staleness_hours > settings.vault_max_stale_hours
                            ):
                                is_stale = True
                                vault_health_status = "degraded"
            vault_h = VaultHealth(
                enabled=True,
                last_etl_run=last_etl_iso,
                staleness_hours=staleness_hours,
                stale=is_stale,
            )
        else:
            vault_h = VaultHealth(enabled=False)

        report = Health(
            status=vault_health_status,
            stub_mode=settings.stub_mode,
            version=__version__,
            atu=UpstreamHealth(
                reachable=True,
                rps_limit=atu_snap.rps,
                tokens_available=atu_snap.tokens_available,
                last_call_ts=_iso(atu_snap.last_call_ts),
            ),
            cache=CacheHealth(
                path=settings.cache_db_path,
                size_bytes=response_cache.size_bytes(),
                ttl_seconds=settings.cache_ttl_seconds,
                last_hit_ts=_iso(response_cache.last_hit_ts),
                last_miss_ts=_iso(response_cache.last_miss_ts),
            ),
            vault=vault_h,
        )
        logger.debug("health snapshot", extra={"uptime_s": int(time.time() - started_at)})
        return _ok(report)

    return mcp


# ---------------------------------------------------------------------------
# Module-level entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entrypoint used by the Docker image."""
    settings = load_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    logger.info("MCP Umphreys starting", extra={"config": settings.safe_repr()})
    server = build_server(settings)
    server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
    )


if __name__ == "__main__":
    main()
