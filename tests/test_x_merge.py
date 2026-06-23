"""Unit tests for the pure advisory-X merge helper ``_merge_x_setlist``.

These need no DB and no network: the merge is a pure function over a ``Show``
(or None) plus a list of staging-row dicts. They lock in:

* ATU precedence on slug conflict.
* The confidence gate.
* The ATU-empty gap-fill case (advisory-only Show).
* Encore mapping (``set_number_hint == "e"`` → ``"Encore"``) and the advisory
  flag/provenance the downstream game keys on.

The merge does NOT do scoring or encore-bonus eligibility; it only surfaces the
rows. That is intentionally not tested here because it is not the merge's job.
"""

from __future__ import annotations

from mcp_umphreys.models import SetlistEntry, Show, Venue
from mcp_umphreys.server import _merge_x_setlist

MIN_CONF = 0.7


def _atu_entry(position: int, slug: str, set_name: str = "Set 1") -> SetlistEntry:
    return SetlistEntry(
        position=position,
        set_name=set_name,
        song_slug=slug,
        song_title=slug.replace("-", " ").title(),
    )


def _show(setlist: list[SetlistEntry]) -> Show:
    return Show(show_id="1", date="2026-06-20", venue=Venue(name="The Tabernacle"), setlist=setlist)


def _x_row(
    slug: str,
    conf: float,
    *,
    position_hint: int | None = None,
    set_number_hint: str = "1",
    show_date: str = "2026-06-20",
) -> dict[str, object]:
    return {
        "song_slug": slug,
        "song_name": slug.replace("-", " ").title(),
        "set_number_hint": set_number_hint,
        "position_hint": position_hint,
        "confidence": conf,
        "show_date": show_date,
    }


def test_atu_precedence_and_append() -> None:
    """ATU [A,B]; X [B(.9), C(.9)] -> [A,B (atu)] + [C (x, advisory)]."""
    show = _show([_atu_entry(1, "song-a"), _atu_entry(2, "song-b")])
    x_rows = [
        _x_row("song-b", 0.9, position_hint=1),  # conflicts with ATU -> dropped
        _x_row("song-c", 0.9, position_hint=2),
    ]
    merged = _merge_x_setlist(show, x_rows, MIN_CONF)
    assert merged is not None
    slugs = [e.song_slug for e in merged.setlist]
    assert slugs == ["song-a", "song-b", "song-c"]

    by_slug = {e.song_slug: e for e in merged.setlist}
    # ATU rows untouched.
    assert by_slug["song-a"].provenance == "atu"
    assert by_slug["song-a"].advisory is False
    assert by_slug["song-b"].provenance == "atu"  # NOT overwritten by the X dup
    assert by_slug["song-b"].advisory is False
    # Appended X row flagged advisory and positioned after the ATU max.
    assert by_slug["song-c"].provenance == "x"
    assert by_slug["song-c"].advisory is True
    assert by_slug["song-c"].position == 3


def test_below_min_confidence_dropped() -> None:
    show = _show([_atu_entry(1, "song-a")])
    x_rows = [_x_row("song-low", 0.5, position_hint=1)]  # below 0.7
    merged = _merge_x_setlist(show, x_rows, MIN_CONF)
    assert merged is not None
    assert [e.song_slug for e in merged.setlist] == ["song-a"]


def test_atu_empty_builds_advisory_only_show() -> None:
    """ATU empty + X [C,D] -> a Show whose setlist is all advisory."""
    x_rows = [
        _x_row("song-c", 0.9, position_hint=1),
        _x_row("song-d", 0.8, position_hint=2),
    ]
    merged = _merge_x_setlist(None, x_rows, MIN_CONF)
    assert merged is not None
    assert merged.date == "2026-06-20"
    assert [e.song_slug for e in merged.setlist] == ["song-c", "song-d"]
    assert all(e.provenance == "x" for e in merged.setlist)
    assert all(e.advisory for e in merged.setlist)
    # Positions continue from 0 (no ATU rows).
    assert [e.position for e in merged.setlist] == [1, 2]


def test_x_only_encore_row_maps_to_encore() -> None:
    """set_number_hint == 'e' -> set_name 'Encore', advisory True."""
    x_rows = [_x_row("the-bottom-half", 0.95, position_hint=1, set_number_hint="e")]
    merged = _merge_x_setlist(None, x_rows, MIN_CONF)
    assert merged is not None
    entry = merged.setlist[0]
    assert entry.set_name == "Encore"
    assert entry.advisory is True
    assert entry.provenance == "x"


def test_position_hint_nulls_sort_last() -> None:
    show = _show([_atu_entry(1, "song-a")])
    x_rows = [
        _x_row("song-null", 0.9, position_hint=None),
        _x_row("song-first", 0.9, position_hint=1),
    ]
    merged = _merge_x_setlist(show, x_rows, MIN_CONF)
    assert merged is not None
    appended = [e.song_slug for e in merged.setlist if e.advisory]
    assert appended == ["song-first", "song-null"]


def test_no_surviving_rows_returns_original_show() -> None:
    show = _show([_atu_entry(1, "song-a")])
    merged = _merge_x_setlist(show, [], MIN_CONF)
    assert merged is show  # unchanged identity when nothing to merge


def test_none_show_and_no_rows_returns_none() -> None:
    assert _merge_x_setlist(None, [], MIN_CONF) is None
