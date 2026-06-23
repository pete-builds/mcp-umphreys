"""Model contract tests: field sets are the frozen public API.

The downstream game parses these shapes field-for-field. If a field name or
the model roster drifts, these tests fail loudly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_umphreys.models import (
    Appearance,
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
    Venue,
    VenueShow,
)


def test_song_has_gap_field_not_gap_current() -> None:
    """get_song's model uses `gap` (the game normalizes it to gap_current)."""
    fields = set(Song.model_fields)
    assert "gap" in fields
    assert "gap_current" not in fields


def test_song_gap_field_set_exact() -> None:
    assert set(Song.model_fields) == {
        "slug",
        "title",
        "artist",
        "original",
        "times_played",
        "debut_date",
        "last_played_date",
        "gap",
    }


def test_song_gap_model_uses_gap_current() -> None:
    """songs_by_gap's model uses `gap_current` (read directly by the game)."""
    assert "gap_current" in set(SongGap.model_fields)
    assert "gap" not in set(SongGap.model_fields)
    assert set(SongGap.model_fields) == {
        "slug",
        "title",
        "times_played",
        "gap_current",
        "last_played_date",
    }


def test_setlist_entry_field_set() -> None:
    assert set(SetlistEntry.model_fields) == {
        "position",
        "set_name",
        "song_slug",
        "song_title",
        "transition",
        "footnote",
        "provenance",
        "advisory",
    }


def test_setlist_entry_provenance_defaults_keep_contract() -> None:
    """New advisory fields default so existing ATU/vault rows serialize unchanged."""
    entry = SetlistEntry(
        position=1,
        set_name="Set 1",
        song_slug="all-in-time",
        song_title="All In Time",
    )
    assert entry.provenance == "atu"
    assert entry.advisory is False


def test_show_field_set() -> None:
    assert set(Show.model_fields) == {
        "show_id",
        "date",
        "venue",
        "tour_name",
        "setlist",
        "rating",
        "rating_count",
        "review_count",
        "setlist_notes",
    }


def test_show_summary_field_set() -> None:
    assert set(ShowSummary.model_fields) == {
        "show_id",
        "date",
        "venue_name",
        "location",
        "tour_name",
    }


def test_song_summary_field_set() -> None:
    assert set(SongSummary.model_fields) == {
        "slug",
        "title",
        "artist",
        "original",
        "times_played",
        "gap",
    }


def test_venue_field_set() -> None:
    assert set(Venue.model_fields) == {
        "slug",
        "name",
        "city",
        "state",
        "country",
        "location",
        "latitude",
        "longitude",
    }


def test_venue_show_field_set() -> None:
    assert set(VenueShow.model_fields) == {
        "show_id",
        "date",
        "venue_name",
        "location",
        "tour_name",
    }


def test_performance_field_set() -> None:
    assert set(Performance.model_fields) == {
        "show_id",
        "date",
        "venue_name",
        "location",
        "set_name",
        "transition",
        "gap",
    }


def test_notable_jam_field_set() -> None:
    assert set(NotableJam.model_fields) == {
        "show_id",
        "date",
        "song_slug",
        "song_title",
        "venue_name",
        "notes",
    }


def test_appearance_field_set() -> None:
    """The added Umphrey's-native model."""
    assert set(Appearance.model_fields) == {
        "date",
        "person_name",
        "person_slug",
        "appearance_type",
        "notes",
    }


def test_slug_validation_field_set() -> None:
    assert set(SlugValidation.model_fields) == {"valid", "unknown"}


def test_health_uses_single_atu_upstream() -> None:
    """Health drops the phishnet/phishin pair for a single `atu` upstream."""
    fields = set(Health.model_fields)
    assert "atu" in fields
    assert "phishnet" not in fields
    assert "phishin" not in fields
    assert fields == {"status", "stub_mode", "version", "atu", "cache", "vault"}


def test_models_are_frozen() -> None:
    song = Song(slug="x", title="X")
    with pytest.raises(ValidationError):
        song.title = "Y"  # type: ignore[misc]


def test_models_forbid_extra() -> None:
    with pytest.raises(ValidationError):
        Song(slug="x", title="X", bogus="nope")  # type: ignore[call-arg]
