"""VaultReader — async read layer over the umphreys-vault Postgres database.

The vault is Umphrey's source of truth. All methods return asyncpg.Record
objects (or tuples/sets of them) so the projection layer in server.py can map
them to the frozen Pydantic models.

The vault schema lives in the umphreys-vault repo (``migrations/001_initial.sql``).
Tables used here: shows, venues, tours, songs, setlist_entries,
jam_chart_entries, appearances, etl_runs. There are no audio or reviews tables
(Umphrey's has no upstream analog for either).

Schema differences from the phish-vault lineage this was templated from:

* ``shows`` has a single ``show_id`` (ATU id, BIGINT) keyed by ``date`` — there
  is no phishin/phishnet id split.
* ``songs`` exposes computed ``times_played`` / ``gap_current`` /
  ``last_play_date`` columns directly (no ``tracks_count`` alias), and uses
  ``original`` + ``original_artist`` rather than a single ``artist`` column.
* setlist rows live in ``setlist_entries`` with raw ATU ``set_number`` /
  ``set_type`` (normalized to a set label at the projection boundary), not a
  pre-labelled ``setlist_notes`` table.

Connection is an asyncpg.Pool injected at construction time. The pool
lifecycle (create / close) is the caller's responsibility.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import asyncpg

logger = logging.getLogger("mcp_umphreys.vault")


class VaultReader:
    """Async read facade over the umphreys-vault Postgres database."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # Show queries
    # ------------------------------------------------------------------

    async def get_show(self, date_or_id: str) -> tuple[asyncpg.Record | None, list[asyncpg.Record]]:
        """Return (show_row, setlist_rows) for a date (YYYY-MM-DD) or ATU show id.

        show_row is None when not found. setlist_rows may be empty if the
        vault has the show but lacks a setlist.
        """
        async with self._pool.acquire() as conn:
            if _is_date(date_or_id):
                show_row: asyncpg.Record | None = await conn.fetchrow(
                    """
                    SELECT s.date, s.show_id,
                           s.venue_slug, s.tour_slug,
                           v.name  AS venue_name,
                           v.city, v.state, v.country, v.location,
                           v.latitude, v.longitude,
                           t.name  AS tour_name
                    FROM   shows s
                    LEFT JOIN venues v ON v.slug = s.venue_slug
                    LEFT JOIN tours  t ON t.slug = s.tour_slug
                    WHERE  s.date = $1
                    """,
                    dt.date.fromisoformat(date_or_id),
                )
            else:
                try:
                    show_id_int = int(date_or_id)
                except ValueError:
                    return None, []
                show_row = await conn.fetchrow(
                    """
                    SELECT s.date, s.show_id,
                           s.venue_slug, s.tour_slug,
                           v.name  AS venue_name,
                           v.city, v.state, v.country, v.location,
                           v.latitude, v.longitude,
                           t.name  AS tour_name
                    FROM   shows s
                    LEFT JOIN venues v ON v.slug = s.venue_slug
                    LEFT JOIN tours  t ON t.slug = s.tour_slug
                    WHERE  s.show_id = $1
                    """,
                    show_id_int,
                )

            if show_row is None:
                return None, []

            # show_row["date"] is already a datetime.date from asyncpg; bind it
            # directly (binding a str against a date column raises a DataError).
            show_date = show_row["date"]
            setlist_rows: list[asyncpg.Record] = await conn.fetch(
                """
                SELECT se.set_number, se.set_type, se.position, se.song_slug,
                       se.song_name, se.transition, se.footnote
                FROM   setlist_entries se
                WHERE  se.show_date = $1
                ORDER  BY se.position
                """,
                show_date,
            )

        return show_row, setlist_rows

    async def search_shows(
        self,
        year: int | None = None,
        venue: str = "",
        city: str = "",
        state: str = "",
        country: str = "",
        limit: int = 25,
    ) -> list[asyncpg.Record]:
        """Search shows with optional year + venue/geo filters."""
        clauses: list[str] = []
        args: list[Any] = []
        idx = 1

        if year is not None:
            clauses.append(f"EXTRACT(YEAR FROM s.date) = ${idx}")
            args.append(year)
            idx += 1
        if venue:
            clauses.append(f"v.name ILIKE ${idx}")
            args.append(f"%{venue}%")
            idx += 1
        if city:
            clauses.append(f"v.city ILIKE ${idx}")
            args.append(f"%{city}%")
            idx += 1
        if state:
            clauses.append(f"v.state ILIKE ${idx}")
            args.append(f"%{state}%")
            idx += 1
        if country:
            clauses.append(f"v.country ILIKE ${idx}")
            args.append(f"%{country}%")
            idx += 1

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(limit)

        # SQL fragments interpolated below (`where`, `idx`) are constructed from
        # internal counters and column-name whitelist clauses, never from user
        # input. All user values flow through asyncpg parameter substitution.
        sql = f"""
            SELECT s.date, s.show_id,
                   v.name AS venue_name, v.location,
                   t.name AS tour_name
            FROM   shows s
            LEFT JOIN venues v ON v.slug = s.venue_slug
            LEFT JOIN tours  t ON t.slug = s.tour_slug
            {where}
            ORDER  BY s.date DESC
            LIMIT  ${idx}
        """  # noqa: S608 — values pass through asyncpg params, fragments are internal
        async with self._pool.acquire() as conn:
            return list(await conn.fetch(sql, *args))

    async def recent_shows(self, limit: int = 10) -> list[asyncpg.Record]:
        """Return the most recent shows, newest first."""
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT s.date, s.show_id,
                           v.name AS venue_name, v.location,
                           t.name AS tour_name
                    FROM   shows s
                    LEFT JOIN venues v ON v.slug = s.venue_slug
                    LEFT JOIN tours  t ON t.slug = s.tour_slug
                    ORDER  BY s.date DESC
                    LIMIT  $1
                    """,
                    limit,
                )
            )

    # ------------------------------------------------------------------
    # Song queries
    # ------------------------------------------------------------------

    async def get_song(self, slug: str) -> asyncpg.Record | None:
        """Return a single song row by slug, or None."""
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT slug, title, alias, original, original_artist,
                       times_played, debut_date, last_play_date, gap_current
                FROM   songs
                WHERE  slug = $1
                """,
                slug,
            )

    async def search_songs(self, query: str, limit: int = 25) -> list[asyncpg.Record]:
        """ILIKE search against song title and upstream alias.

        Umphrey's catalog has no community-alias table (unlike the Phish
        lineage), so this is a straight title/alias match ordered by play count.
        """
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT slug, title, alias, original, original_artist,
                           times_played, gap_current
                    FROM   songs
                    WHERE  title ILIKE $1
                       OR  alias ILIKE $1
                    ORDER  BY times_played DESC NULLS LAST, title ASC
                    LIMIT  $2
                    """,
                    f"%{query}%",
                    limit,
                )
            )

    async def validate_slugs(self, slugs: list[str]) -> set[str]:
        """Return the subset of ``slugs`` that exist in ``songs.slug``.

        Single SELECT, single round-trip. Order is not preserved here;
        the caller is responsible for ordering the result against the request.
        """
        if not slugs:
            return set()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT slug FROM songs WHERE slug = ANY($1::text[])",
                slugs,
            )
        return {str(row["slug"]) for row in rows}

    async def song_history(self, slug: str, limit: int = 50) -> list[asyncpg.Record]:
        """Return performances of a song, most-recent first.

        Derived from ``setlist_entries`` joined to shows + venues. The vault
        does not pre-compute a per-performance gap, so ``gap`` is always NULL
        here (the per-song current gap lives on ``songs.gap_current`` and is
        surfaced by :meth:`get_song` / :meth:`songs_by_gap`).
        """
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT se.show_date AS date,
                           s.show_id,
                           se.set_number,
                           se.set_type,
                           se.transition,
                           v.name     AS venue_name,
                           v.location AS venue_location
                    FROM   setlist_entries se
                    JOIN   shows  s  ON s.date = se.show_date
                    LEFT JOIN venues v ON v.slug = s.venue_slug
                    WHERE  se.song_slug = $1
                    ORDER  BY se.show_date DESC, se.position ASC
                    LIMIT  $2
                    """,
                    slug,
                    limit,
                )
            )

    # ------------------------------------------------------------------
    # Jam chart (native ATU `jamcharts` method)
    # ------------------------------------------------------------------

    async def jam_chart(self, year: int | None = None, limit: int = 50) -> list[asyncpg.Record]:
        """Return jam-chart entries, optionally filtered by year."""
        args: list[Any] = []
        year_clause = ""
        if year is not None:
            year_clause = "AND EXTRACT(YEAR FROM jc.show_date) = $1"
            args.append(year)
        args.append(limit)
        limit_idx = len(args)

        # `year_clause` and `limit_idx` are derived from internal counters/
        # constants, never from user input. All values use asyncpg params.
        sql = f"""
            SELECT jc.show_date AS date,
                   jc.song_slug,
                   jc.song_name,
                   jc.notes,
                   s.show_id,
                   v.name AS venue_name
            FROM   jam_chart_entries jc
            JOIN   shows  s ON s.date = jc.show_date
            LEFT JOIN venues v ON v.slug = s.venue_slug
            WHERE  1=1
            {year_clause}
            ORDER  BY jc.show_date DESC
            LIMIT  ${limit_idx}
        """  # noqa: S608 — values pass through asyncpg params, fragments are internal
        async with self._pool.acquire() as conn:
            return list(await conn.fetch(sql, *args))

    # ------------------------------------------------------------------
    # Appearances (native ATU `appearances` method) — UM-native capability
    # ------------------------------------------------------------------

    async def appearances(
        self,
        person_slug: str | None = None,
        show_date: str | None = None,
        limit: int = 50,
    ) -> list[asyncpg.Record]:
        """Return guest sit-ins, optionally filtered by person or show date."""
        clauses: list[str] = []
        args: list[Any] = []
        idx = 1
        if person_slug:
            clauses.append(f"a.person_slug = ${idx}")
            args.append(person_slug)
            idx += 1
        if show_date:
            clauses.append(f"a.show_date = ${idx}")
            args.append(dt.date.fromisoformat(show_date))
            idx += 1
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(limit)

        # Fragments (`where`, `idx`) come from internal counters; values are
        # asyncpg params.
        sql = f"""
            SELECT a.show_date AS date,
                   a.person_name,
                   a.person_slug,
                   a.appearance_type,
                   a.notes
            FROM   appearances a
            {where}
            ORDER  BY a.show_date DESC, a.person_name ASC
            LIMIT  ${idx}
        """  # noqa: S608 — values pass through asyncpg params, fragments are internal
        async with self._pool.acquire() as conn:
            return list(await conn.fetch(sql, *args))

    # ------------------------------------------------------------------
    # Analytical tools (vault-only)
    # ------------------------------------------------------------------

    async def venue_history(self, venue_slug: str, limit: int = 25) -> list[asyncpg.Record]:
        """Return shows at a given venue, newest first."""
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT s.date, s.show_id,
                           v.name AS venue_name, v.location,
                           t.name AS tour_name
                    FROM   shows s
                    LEFT JOIN venues v ON v.slug = s.venue_slug
                    LEFT JOIN tours  t ON t.slug = s.tour_slug
                    WHERE  s.venue_slug = $1
                    ORDER  BY s.date DESC
                    LIMIT  $2
                    """,
                    venue_slug,
                    limit,
                )
            )

    async def songs_by_gap(self, limit: int = 25) -> list[asyncpg.Record]:
        """Return songs ordered by current gap (shows since last play), descending."""
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT slug, title,
                           times_played,
                           gap_current,
                           last_play_date
                    FROM   songs
                    WHERE  gap_current IS NOT NULL
                    ORDER  BY gap_current DESC
                    LIMIT  $1
                    """,
                    limit,
                )
            )

    # ------------------------------------------------------------------
    # Catalog-wide statistics (powers the downstream public Stats page)
    # ------------------------------------------------------------------

    async def stats_overview(self, top_n: int = 10) -> dict[str, Any]:
        """Compute catalog-wide aggregate statistics in one batch.

        Read-only. Returns a plain dict the server layer projects onto the
        ``StatsOverview`` model. The per-list slices (most-played, biggest
        gaps, rarest, recent debuts, longest shows) are each capped at
        ``top_n``. Only shows that have at least one setlist row count toward
        the show totals and the average, so future-dated upcoming shows (no
        setlist yet) don't drag the average down.
        """
        capped = max(1, min(int(top_n), 50))
        async with self._pool.acquire() as conn:
            # Headline aggregates. ``played_shows`` counts distinct show dates
            # that actually have setlist rows; the average uses that basis.
            headline = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM songs) AS total_songs_tracked,
                    (SELECT COUNT(DISTINCT song_slug)
                       FROM setlist_entries
                      WHERE song_slug IS NOT NULL) AS distinct_songs_played,
                    (SELECT COUNT(*) FROM setlist_entries) AS total_performances,
                    (SELECT COUNT(DISTINCT show_date)
                       FROM setlist_entries) AS played_shows,
                    (SELECT MIN(show_date) FROM setlist_entries) AS first_show_date,
                    (SELECT MAX(show_date) FROM setlist_entries) AS last_show_date
                """
            )
            most_played = await conn.fetch(
                """
                SELECT slug, title, times_played
                  FROM songs
                 WHERE times_played IS NOT NULL AND times_played > 0
                 ORDER BY times_played DESC, title ASC
                 LIMIT $1
                """,
                capped,
            )
            biggest_gaps = await conn.fetch(
                """
                SELECT slug, title, gap_current, times_played, last_play_date
                  FROM songs
                 WHERE gap_current IS NOT NULL
                   AND times_played IS NOT NULL AND times_played > 0
                 ORDER BY gap_current DESC, title ASC
                 LIMIT $1
                """,
                capped,
            )
            rarest = await conn.fetch(
                """
                SELECT slug, title, times_played
                  FROM songs
                 WHERE times_played IS NOT NULL AND times_played > 0
                 ORDER BY times_played ASC, title ASC
                 LIMIT $1
                """,
                capped,
            )
            recent_debuts = await conn.fetch(
                """
                SELECT slug, title, debut_date, times_played
                  FROM songs
                 WHERE debut_date IS NOT NULL
                 ORDER BY debut_date DESC, title ASC
                 LIMIT $1
                """,
                capped,
            )
            longest_shows = await conn.fetch(
                """
                SELECT se.show_date AS date,
                       s.show_id,
                       v.name     AS venue_name,
                       v.location AS location,
                       COUNT(*)   AS song_count
                  FROM setlist_entries se
                  JOIN shows  s ON s.date = se.show_date
                  LEFT JOIN venues v ON v.slug = s.venue_slug
                 GROUP BY se.show_date, s.show_id, v.name, v.location
                 ORDER BY song_count DESC, se.show_date DESC
                 LIMIT $1
                """,
                capped,
            )

        played_shows = int(headline["played_shows"] or 0)
        total_perf = int(headline["total_performances"] or 0)
        avg = round(total_perf / played_shows, 1) if played_shows else 0.0
        return {
            "total_shows": played_shows,
            "total_songs_tracked": int(headline["total_songs_tracked"] or 0),
            "distinct_songs_played": int(headline["distinct_songs_played"] or 0),
            "total_performances": total_perf,
            "avg_songs_per_show": avg,
            "first_show_date": headline["first_show_date"],
            "last_show_date": headline["last_show_date"],
            "most_played": [dict(r) for r in most_played],
            "biggest_gaps": [dict(r) for r in biggest_gaps],
            "rarest_songs": [dict(r) for r in rarest],
            "recent_debuts": [dict(r) for r in recent_debuts],
            "longest_shows": [dict(r) for r in longest_shows],
        }

    # ------------------------------------------------------------------
    # ETL health
    # ------------------------------------------------------------------

    async def last_etl_run(self) -> dict[str, object] | None:
        """Return the most recent etl_runs row as a plain dict, or None."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, started_at, finished_at, mode, status,
                       rows_added, rows_updated
                FROM   etl_runs
                ORDER  BY id DESC
                LIMIT  1
                """
            )
        if row is None:
            return None
        return dict(row)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _is_date(value: str) -> bool:
    """Return True if value looks like YYYY-MM-DD."""
    return len(value) == 10 and value.count("-") == 2
