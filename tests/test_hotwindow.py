"""Hot-window anchoring tests.

The regression these guard: a US-evening show plays in next-day UTC, so a
window anchored to midnight UTC of the show date closed ~25h too early and a
live in-progress show was served as a cold vault read (empty setlist). Measured
on this server before the fix: 0 of 11 shows from 2026-06-21 onward scored live.

Every case pins ``now`` explicitly — none of these may depend on wall clock.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mcp_umphreys.hotwindow import is_hot

WINDOW = 24.0


def _utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_evening_show_is_hot_during_showtime() -> None:
    """2026-07-19, a US-evening show. Second set around 01:30 UTC on 07-20 is
    25.5h past midnight-UTC of the show date, so the old anchor called it cold
    while the band was still playing. It must be hot."""
    now = _utc(2026, 7, 20, 1, 30)
    assert is_hot("2026-07-19", WINDOW, now) is True


def test_next_morning_finalization_is_hot() -> None:
    """ATU finalizes the setlist the morning after; still hot for the cron."""
    now = _utc(2026, 7, 20, 13, 0)  # ~9am ET the day after the show
    assert is_hot("2026-07-19", WINDOW, now) is True


def test_stale_show_is_cold() -> None:
    now = _utc(2026, 7, 23, 12, 0)
    assert is_hot("2026-07-19", WINDOW, now) is False


def test_future_show_is_hot() -> None:
    now = _utc(2026, 7, 19, 12, 0)
    assert is_hot("2026-07-21", WINDOW, now) is True


def test_late_pacific_show_still_hot_while_playing() -> None:
    """A Pacific show plays into the early UTC hours of the next day; a read
    during the encore must still be hot."""
    now = _utc(2026, 7, 20, 6, 0)
    assert is_hot("2026-07-19", WINDOW, now) is True


def test_window_edge_just_inside_and_outside() -> None:
    # End of show day (ET) for 2026-07-19 = 2026-07-20 00:00 EDT = 04:00 UTC.
    # Hot until 04:00 UTC on 07-20 + 24h = 04:00 UTC on 07-21.
    assert is_hot("2026-07-19", WINDOW, _utc(2026, 7, 21, 3, 59)) is True
    assert is_hot("2026-07-19", WINDOW, _utc(2026, 7, 21, 4, 1)) is False


def test_dst_boundary_est_show_uses_standard_time_offset() -> None:
    """A January show sits in EST (UTC-5), so end-of-show-day is 05:00 UTC the
    next day and the window closes 24h later at 05:00 UTC. A fixed -4 offset
    would close it an hour early."""
    # 2026-01-15 show: end of day ET = 2026-01-16 00:00 EST = 05:00 UTC.
    assert is_hot("2026-01-15", WINDOW, _utc(2026, 1, 17, 4, 59)) is True
    assert is_hot("2026-01-15", WINDOW, _utc(2026, 1, 17, 5, 1)) is False


def test_dst_spring_forward_night_show_is_hot() -> None:
    """Show on the 2026 spring-forward date (2026-03-08). The clock jumps
    02:00 EST -> 03:00 EDT that morning, so end-of-show-day is 2026-03-09
    00:00 EDT = 04:00 UTC. A show read at 03:00 UTC on 03-09 (10pm ET, mid-set)
    must be hot."""
    assert is_hot("2026-03-08", WINDOW, _utc(2026, 3, 9, 3, 0)) is True
    assert is_hot("2026-03-08", WINDOW, _utc(2026, 3, 10, 3, 59)) is True
    assert is_hot("2026-03-08", WINDOW, _utc(2026, 3, 10, 4, 1)) is False


def test_malformed_date_is_not_hot() -> None:
    now = _utc(2026, 7, 20, 1, 30)
    assert is_hot("not-a-date", WINDOW, now) is False
    assert is_hot("", WINDOW, now) is False


def test_accepts_iso_datetime_uses_date_part() -> None:
    now = _utc(2026, 7, 20, 1, 30)
    assert is_hot("2026-07-19T20:00:00", WINDOW, now) is True
