"""Config tests."""

from __future__ import annotations

import pytest

from mcp_umphreys.config import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "STUB_MODE",
        "VAULT_ENABLED",
        "MCP_PORT",
        "PG_DB",
        "PG_USER",
        "ATU_BASE_URL",
        "HOT_WINDOW_CACHE_TTL_SECONDS",
        "THROTTLE_ATU_RPS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_defaults() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.vault_enabled is True  # vault is UM's source of truth
    assert s.mcp_port == 3717
    assert s.pg_db == "umphreys"
    assert s.pg_user == "umphreys"
    assert s.atu_base_url == "https://allthings.umphreys.com/api/v2"
    assert s.atu_artist_id == 1
    assert s.hot_window_cache_ttl_seconds == 90
    assert s.throttle_atu_rps == 3.0
    assert s.cache_db_path == "/data/umphreys-cache.db"


def test_pg_dsn_redacts_nothing_but_builds_url() -> None:
    s = Settings(_env_file=None, pg_password="hunter2")  # type: ignore[call-arg]
    assert s.pg_dsn == "postgresql://umphreys:hunter2@postgres:5432/umphreys"


def test_safe_repr_omits_password() -> None:
    s = Settings(_env_file=None, pg_password="hunter2")  # type: ignore[call-arg]
    rep = s.safe_repr()
    assert "pg_password" not in rep
    assert rep["hot_window_cache_ttl_seconds"] == 90
    assert rep["vault_enabled"] is True
    # No secret leaks into the repr values.
    assert "hunter2" not in str(rep)


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PORT", "4000")
    monkeypatch.setenv("VAULT_ENABLED", "false")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.mcp_port == 4000
    assert s.vault_enabled is False
