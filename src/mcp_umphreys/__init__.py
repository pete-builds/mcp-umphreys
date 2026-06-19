"""mcp-umphreys — MCP server for Umphrey's McGee setlist data.

Source of truth is the umphreys-vault Postgres database, with a live All
Things Umphreys (ATU) v2 API fallthrough for in-progress (hot-window) shows.
No audio, no reviews — Umphrey's has no upstream analog for either.
"""

__version__ = "0.1.0"
