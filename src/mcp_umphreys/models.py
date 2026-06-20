"""Public Pydantic models for mcp-umphreys — THE FROZEN CONTRACT.

Every tool returns one of these types. These shapes are the public API of the
MCP server and the downstream game (``open-setlist-stash``) parses them
field-for-field. The source can change (live ATU API ↔ Postgres vault); the
shape exposed to MCP clients never can.

This module is templated from mcp-phish's ``models.py``. The game-critical
public models keep identical field names/types so the resolver works
unchanged. Phish-only models are dropped:

* ``Track`` / ``ShowAudio`` — Umphrey's has no audio source (no phish.in
  analog).
* ``Review`` — the ATU API exposes no reviews method.

One model is added for an Umphrey's-native capability the Phish lineage
lacked:

* ``Appearance`` — guest sit-ins from the ATU ``appearances`` method.

Design notes:

* All models use ``model_config = ConfigDict(frozen=True, extra="forbid")``
  so any drift in the upstream API surfaces as a validation failure rather
  than silently leaking new fields into the contract.
* Fields are projections of the upstream response, NOT raw passthroughs. A
  client sees a stable shape regardless of which source produced it.
* Optional fields use ``None`` defaults; lists default to ``[]``. Empty is
  always an explicit value, never an absent key.
* All datetimes are returned as ISO 8601 UTC strings via ``str``. Pydantic
  will coerce; we don't need ``datetime`` typing for the wire format.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Helper config
# ---------------------------------------------------------------------------

_FROZEN: ConfigDict = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Show models
# ---------------------------------------------------------------------------


class Venue(BaseModel):
    """A venue, normalized from the ATU venue catalog / setlist rows."""

    model_config = _FROZEN

    slug: str = ""
    name: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    location: str = ""  # human-readable "City, ST"
    latitude: float | None = None
    longitude: float | None = None


class ShowSummary(BaseModel):
    """Lightweight show record for list endpoints."""

    model_config = _FROZEN

    show_id: str  # ATU show_id, stringified
    date: str  # YYYY-MM-DD
    venue_name: str = ""
    location: str = ""
    tour_name: str = ""


class SetlistEntry(BaseModel):
    """One song in a setlist, with its position and any segue/note metadata."""

    model_config = _FROZEN

    position: int
    set_name: str  # "Set 1", "Set 2", "Encore", etc.
    song_slug: str
    song_title: str
    transition: str = ""  # ">", "->", "" (no segue)
    footnote: str = ""


class Show(BaseModel):
    """Full show: setlist + venue.

    ``rating``/``rating_count``/``review_count``/``setlist_notes`` are retained
    from the Phish contract for byte-for-byte compatibility with the downstream
    resolver, but Umphrey's has no ratings or reviews source, so they always
    take their model defaults.
    """

    model_config = _FROZEN

    show_id: str
    date: str
    venue: Venue
    tour_name: str = ""
    setlist: list[SetlistEntry] = Field(default_factory=list)
    rating: float | None = None  # UM has no ratings — always None
    rating_count: int = 0
    review_count: int = 0
    setlist_notes: str = ""


# ---------------------------------------------------------------------------
# Song models
# ---------------------------------------------------------------------------


class SongSummary(BaseModel):
    """Lightweight song record for search results.

    Carries ``gap`` (shows since last play) so a caller can show a "last
    played N shows ago" hint in a picker without a second ``get_song`` round
    trip. ``None`` when the song has never been played (no gap to report).
    """

    model_config = _FROZEN

    slug: str
    title: str
    artist: str | None = None
    original: bool = True
    times_played: int = 0
    gap: int | None = None


class Song(BaseModel):
    """Detailed song record: debut, last play, gap, total.

    NOTE: the field is named ``gap`` (not ``gap_current``) — the downstream
    game normalizes ``gap``→``gap_current`` itself. The vault column is
    ``gap_current``; the projection maps it onto this ``gap`` field.
    """

    model_config = _FROZEN

    slug: str
    title: str
    artist: str | None = None
    original: bool = True
    times_played: int = 0
    debut_date: str | None = None
    last_played_date: str | None = None
    gap: int | None = None  # shows since last play


class Performance(BaseModel):
    """One performance of a song — used by song_history()."""

    model_config = _FROZEN

    show_id: str
    date: str
    venue_name: str = ""
    location: str = ""
    set_name: str = ""
    transition: str = ""
    gap: int | None = None  # gap from prior performance (may be null)


class NotableJam(BaseModel):
    """A jam-chart entry: a notable performance flagged by ATU editors."""

    model_config = _FROZEN

    show_id: str
    date: str
    song_slug: str
    song_title: str
    venue_name: str = ""
    notes: str = ""


class Appearance(BaseModel):
    """A guest sit-in / appearance — used by appearances().

    Umphrey's-native capability (the ATU ``appearances`` method). Powers
    sit-in tooling, e.g. tracking a guest guitarist covering for an absent
    member.
    """

    model_config = _FROZEN

    date: str
    person_name: str = ""
    person_slug: str = ""
    appearance_type: str = ""
    notes: str = ""


# ---------------------------------------------------------------------------
# Health model (meta)
# ---------------------------------------------------------------------------


class UpstreamHealth(BaseModel):
    """Per-upstream health snapshot, surfaced in health()."""

    model_config = _FROZEN

    reachable: bool
    rps_limit: float
    tokens_available: float
    last_call_ts: str | None = None  # ISO 8601


class CacheHealth(BaseModel):
    """Cache snapshot, surfaced in health()."""

    model_config = _FROZEN

    path: str
    size_bytes: int
    ttl_seconds: int
    last_hit_ts: str | None = None
    last_miss_ts: str | None = None


class VaultHealth(BaseModel):
    """Vault read-path health snapshot, surfaced in health()."""

    model_config = _FROZEN

    enabled: bool
    last_etl_run: str | None = None  # ISO 8601
    staleness_hours: float | None = None
    stale: bool = False


class Health(BaseModel):
    """Top-level health summary.

    Single ``atu`` upstream replaces the Phish lineage's phishnet/phishin pair
    (Umphrey's has exactly one upstream source).
    """

    model_config = _FROZEN

    status: str  # "ok" | "degraded"
    stub_mode: bool
    version: str
    atu: UpstreamHealth
    cache: CacheHealth
    vault: VaultHealth


# ---------------------------------------------------------------------------
# Vault-only analytical models
# ---------------------------------------------------------------------------


class VenueShow(BaseModel):
    """One show at a venue — used by venue_history()."""

    model_config = _FROZEN

    show_id: str
    date: str
    venue_name: str = ""
    location: str = ""
    tour_name: str = ""


class SongGap(BaseModel):
    """Song with current gap — used by songs_by_gap().

    Here the field IS ``gap_current`` (not ``gap``): the game reads this list
    directly. Contrast with :class:`Song`, whose detail view uses ``gap``.
    """

    model_config = _FROZEN

    slug: str
    title: str
    times_played: int = 0
    gap_current: int
    last_played_date: str | None = None


# ---------------------------------------------------------------------------
# Batch validation model (form-validation in downstream clients)
# ---------------------------------------------------------------------------


class SlugValidation(BaseModel):
    """Result of validating a batch of song slugs against the catalog.

    Used by ``validate_song_slugs()``. ``valid`` is the subset of
    requested slugs that resolved to a real song; ``unknown`` is the
    subset that did not. Both lists preserve a deterministic order
    (see the tool docstring for details).
    """

    model_config = _FROZEN

    valid: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Catalog-wide statistics (powers a downstream public "Stats" page)
# ---------------------------------------------------------------------------


class TopSong(BaseModel):
    """A song with its play count — used in the most-played leaderboard."""

    model_config = _FROZEN

    slug: str
    title: str
    times_played: int = 0


class GapSong(BaseModel):
    """A song that's been cold the longest — a bust-out candidate."""

    model_config = _FROZEN

    slug: str
    title: str
    gap_current: int
    times_played: int = 0
    last_played_date: str | None = None


class DebutSong(BaseModel):
    """A recently debuted song."""

    model_config = _FROZEN

    slug: str
    title: str
    debut_date: str | None = None
    times_played: int = 0


class LongShow(BaseModel):
    """A show with an unusually high song count."""

    model_config = _FROZEN

    show_id: str
    date: str
    venue_name: str = ""
    location: str = ""
    song_count: int = 0


class StatsOverview(BaseModel):
    """Catalog-wide aggregate statistics across the whole Umphrey's corpus.

    A single read-only roll-up the downstream game's public Stats page renders
    as cards/tables. Aggregates that the per-song / per-show tools don't cover
    (total shows, average songs per show, distinct-songs-played) are computed
    here from the full setlist corpus.
    """

    model_config = _FROZEN

    total_shows: int = 0
    total_songs_tracked: int = 0
    distinct_songs_played: int = 0
    total_performances: int = 0
    avg_songs_per_show: float = 0.0
    first_show_date: str | None = None
    last_show_date: str | None = None
    most_played: list[TopSong] = Field(default_factory=list)
    biggest_gaps: list[GapSong] = Field(default_factory=list)
    rarest_songs: list[TopSong] = Field(default_factory=list)
    recent_debuts: list[DebutSong] = Field(default_factory=list)
    longest_shows: list[LongShow] = Field(default_factory=list)
