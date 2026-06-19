# mcp-umphreys

A [FastMCP](https://github.com/jlowin/fastmcp) (Streamable HTTP) MCP server for
Umphrey's McGee setlist data. It reads from the **umphreys-vault** Postgres
database (the source of truth) with a live **All Things Umphreys (ATU) v2** API
fallthrough for in-progress shows on show night.

No audio, no reviews: Umphrey's has no upstream analog for either.

Templated from `mcp-phish`; the public tool output shapes are byte-for-byte
compatible with that contract so the downstream setlist game
(`open-setlist-stash`) parses them unchanged.

## Tools

Game-critical (shapes match the mcp-phish contract):

| Tool | Returns | Notes |
|------|---------|-------|
| `health()` | `Health` | Single `atu` upstream; cache + vault freshness. |
| `recent_shows(limit=10)` | `[ShowSummary]` | Newest first. Hot-window newest show reads live. |
| `search_songs(query, limit=25)` | `[SongSummary]` | Title/alias ILIKE. |
| `get_song(slug)` | `Song` | Field is **`gap`** (vault `gap_current` projected). |
| `get_show(date_or_id)` | `Show` | `set_number=="e"` → `set_name=="Encore"`. Hot-window reads live ATU. |
| `songs_by_gap(limit=25)` | `[SongGap]` | Field is **`gap_current`**, gap desc. |
| `validate_song_slugs(slugs)` | `SlugValidation` | `valid` sorted, `unknown` in request order. |
| `venue_history(venue_slug, limit=25)` | `[VenueShow]` | Newest first. |

Umphrey's-native (no game dependency):

| Tool | Returns | Notes |
|------|---------|-------|
| `jam_chart(year=None, limit=50)` | `[NotableJam]` | From `jam_chart_entries`. |
| `appearances(person_slug=None, show_date=None, limit=50)` | `[Appearance]` | Guest sit-ins. |
| `song_history(slug, limit=50)` | `[Performance]` | Most-recent first; `gap` is null. |

Every tool returns `{"data": <model>}` (or the standard `{"error", "code"}`
failure shape) as a JSON string in the FastMCP `content[0].text`.

## The hot window

`get_show` / `recent_shows` for a show within `VAULT_HOT_WINDOW_HOURS` (default
24) of now read **live** from ATU instead of the vault, with a short cache TTL
(`HOT_WINDOW_CACHE_TTL_SECONDS`, default 90s). This is required so the game's
resolver sees an in-progress setlist grow on show night instead of a frozen
vault snapshot. The set-label normalization (`e` → `Encore`, `One Set` →
`Set 1`) is applied identically on the live and vault paths so encore detection
works in both.

## Running

```bash
cp .env.example .env   # set PG_PASSWORD; STUB_MODE=true skips the live network
pip install -e ".[dev]"
python -m mcp_umphreys.server   # Streamable HTTP on :3717
```

Tests run with no network and no Postgres (stub ATU client + a fake vault
reader):

```bash
ruff check . && mypy && pytest
```

## Deployment

`docker compose up -d` builds the image and joins the external
`umphreys-vault_default` network so the server reaches the vault's `postgres`
container by name. The opaque response cache persists in the
`mcp-umphreys-cache` volume. Port **3717**.
