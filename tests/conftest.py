"""Shared fixtures for the mcp-umphreys test suite."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator

import pytest

from mcp_umphreys.cache import ResponseCache
from mcp_umphreys.config import Settings

_ENV_VARS = (
    "STUB_MODE",
    "ATU_BASE_URL",
    "ATU_ARTIST_ID",
    "CACHE_DB_PATH",
    "CACHE_TTL_SECONDS",
    "HOT_WINDOW_CACHE_TTL_SECONDS",
    "THROTTLE_ATU_RPS",
    "MCP_HOST",
    "MCP_PORT",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "VAULT_ENABLED",
    "VAULT_HOT_WINDOW_HOURS",
    "VAULT_MAX_STALE_HOURS",
    "PG_HOST",
    "PG_PORT",
    "PG_DB",
    "PG_USER",
    "PG_PASSWORD",
)


@pytest.fixture
def temp_cache_path() -> Iterator[str]:
    """Disposable file path for an aiosqlite cache."""
    fd, path = tempfile.mkstemp(prefix="umphreys-cache-test-", suffix=".db")
    os.close(fd)
    os.remove(path)  # let aiosqlite create it cleanly
    try:
        yield path
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(path + suffix)


@pytest.fixture
def stub_settings(monkeypatch: pytest.MonkeyPatch, temp_cache_path: str) -> Settings:
    """Stub-mode settings with vault DISABLED by default.

    Tests that want vault routing clone this with ``vault_enabled=True`` and
    inject a fake ``VaultReader``. Stub mode here only governs the live ATU
    hot-window client; it never touches Postgres.
    """
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return Settings(
        stub_mode=True,
        vault_enabled=False,
        log_format="text",
        cache_db_path=temp_cache_path,
        cache_ttl_seconds=86400,
    )


@pytest.fixture
def empty_cache(temp_cache_path: str) -> ResponseCache:
    """Fresh ResponseCache pointing at a brand-new file."""
    return ResponseCache(db_path=temp_cache_path, ttl_seconds=60)


def parse_tool_response(raw: str) -> dict[str, object]:
    """Helper for tests: parse a tool's JSON-string return value."""
    return json.loads(raw)
