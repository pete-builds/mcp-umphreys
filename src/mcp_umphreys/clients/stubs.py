"""Realistic stub responses for the ATU v2 API hot-window path.

Used when ``stub_mode=True`` so the live read path boots and returns sensible
data without a network call. Payloads mirror the raw ATU ``setlists`` row shape
closely enough that the live projection in ``server.py`` cannot tell the
difference, which keeps the public Pydantic contract identical between modes.

State is in-memory and read-only; this is a read-only server.

The reference show (2023-02-26, The Tabernacle) carries a four-set-plus-encore
shape so the encore-detection path (``setnumber == "e"`` → ``"Encore"``) and the
"One Set" settype normalization both get exercised end-to-end.
"""

from __future__ import annotations

from typing import Any

from mcp_umphreys.clients.atu import ATUError

# ---------------------------------------------------------------------------
# Seed data — raw ATU setlist rows, keyed by show date.
#
# Field names match the live ATU `setlists` rows: setnumber, settype, position,
# slug, songname, transition, footnote, venuename, city, state, country,
# showtitle, show_id, showdate, tourname.
# ---------------------------------------------------------------------------

_ATU_SETLISTS: dict[str, list[dict[str, Any]]] = {
    "2023-02-26": [
        {
            "show_id": 5551001,
            "showdate": "2023-02-26",
            "showtitle": "",
            "venuename": "The Tabernacle",
            "city": "Atlanta",
            "state": "GA",
            "country": "USA",
            "tourname": "Winter 2023 Tour",
            "setnumber": "1",
            "settype": "Set",
            "position": 1,
            "slug": "all-in-time",
            "songname": "All in Time",
            "transition": " > ",
            "footnote": "",
        },
        {
            "show_id": 5551001,
            "showdate": "2023-02-26",
            "showtitle": "",
            "venuename": "The Tabernacle",
            "city": "Atlanta",
            "state": "GA",
            "country": "USA",
            "tourname": "Winter 2023 Tour",
            "setnumber": "1",
            "settype": "Set",
            "position": 2,
            "slug": "the-bottom-half",
            "songname": "The Bottom Half",
            "transition": ", ",
            "footnote": "",
        },
        {
            "show_id": 5551001,
            "showdate": "2023-02-26",
            "showtitle": "",
            "venuename": "The Tabernacle",
            "city": "Atlanta",
            "state": "GA",
            "country": "USA",
            "tourname": "Winter 2023 Tour",
            "setnumber": "2",
            "settype": "Set",
            "position": 3,
            "slug": "bridgeless",
            "songname": "Bridgeless",
            "transition": " > ",
            "footnote": "Unfinished; resolved at the encore.",
        },
        {
            "show_id": 5551001,
            "showdate": "2023-02-26",
            "showtitle": "",
            "venuename": "The Tabernacle",
            "city": "Atlanta",
            "state": "GA",
            "country": "USA",
            "tourname": "Winter 2023 Tour",
            "setnumber": "e",
            "settype": "Encore",
            "position": 4,
            "slug": "bridgeless-reprise",
            "songname": "Bridgeless (Reprise)",
            "transition": "",
            "footnote": "",
        },
    ],
    # A "One Set" settype show — must normalize to "Set 1".
    "2021-08-13": [
        {
            "show_id": 5552002,
            "showdate": "2021-08-13",
            "showtitle": "",
            "venuename": "Red Rocks Amphitheatre",
            "city": "Morrison",
            "state": "CO",
            "country": "USA",
            "tourname": "Summer 2021 Tour",
            "setnumber": "1",
            "settype": "One Set",
            "position": 1,
            "slug": "1348",
            "songname": "1348",
            "transition": "",
            "footnote": "",
        },
    ],
}

# `latest` returns the rows for the most recent stub show.
_ATU_LATEST_DATE = "2023-02-26"


class StubATUClient:
    """In-memory ATU stub. Same async surface as the slim :class:`ATUClient`."""

    def __init__(self) -> None:
        self.base_url = "stub://atu"
        self.artist_id = 1
        self._calls: int = 0

    async def aclose(self) -> None:
        return None

    async def setlists_by_date(self, date: str) -> list[dict[str, Any]]:
        self._calls += 1
        rows = _ATU_SETLISTS.get(date)
        if rows is None:
            # ATU returns an empty data list for an unknown / future date.
            return []
        return [dict(r) for r in rows]

    async def latest(self) -> list[dict[str, Any]]:
        self._calls += 1
        return [dict(r) for r in _ATU_SETLISTS[_ATU_LATEST_DATE]]


__all__ = ["ATUError", "StubATUClient"]
