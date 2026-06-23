"""Validated, env-driven configuration for mcp-umphreys.

Loads values from environment variables (and a ``.env`` file when present),
validates types/ranges, and exposes a Postgres DSN for the vault read path.

Unlike the Phish lineage this was templated from, the ATU upstream is keyless,
so there are no API credentials to require even in real mode. ``STUB_MODE``
still exists for dev/tests so the server boots with no network and no Postgres.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the MCP Umphreys server.

    All fields can be overridden via environment variables. Names map 1:1 with
    the env var names (case-insensitive). Pydantic validates them at startup.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Mode toggle
    # ------------------------------------------------------------------
    stub_mode: bool = Field(
        default=False,
        description=(
            "If True, the live ATU hot-window path returns realistic mock data "
            "with no network calls. Used by dev/tests. In production the vault is "
            "the source of truth and the live path hits the real ATU v2 API."
        ),
    )

    # ------------------------------------------------------------------
    # ATU upstream (keyless public API)
    # ------------------------------------------------------------------
    atu_base_url: str = Field(default="https://allthings.umphreys.com/api/v2")
    atu_artist_id: int = Field(default=1, ge=1)

    # ------------------------------------------------------------------
    # Cache (aiosqlite opaque KV)
    # ------------------------------------------------------------------
    cache_db_path: str = Field(default="/data/umphreys-cache.db")
    cache_ttl_seconds: int = Field(default=86400, ge=60, le=7 * 86400)
    hot_window_cache_ttl_seconds: int = Field(
        default=90,
        ge=1,
        le=86400,
        description=(
            "Short cache TTL applied to live ATU reads of shows inside the hot "
            "window (see VAULT_HOT_WINDOW_HOURS). On show night the setlist grows "
            "live on ATU, so a long TTL would freeze a partial snapshot for hours. "
            "This keeps frequent polls fresh while still absorbing burst traffic."
        ),
    )

    # ------------------------------------------------------------------
    # Per-instance throttle (requests/second)
    # ------------------------------------------------------------------
    throttle_atu_rps: float = Field(default=3.0, gt=0, le=50)

    # ------------------------------------------------------------------
    # MCP server settings
    # ------------------------------------------------------------------
    mcp_host: str = Field(default="0.0.0.0")
    mcp_port: int = Field(default=3717, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO")
    log_format: Literal["json", "text"] = Field(
        default="json",
        description="Structured JSON logs (production) or human-readable text.",
    )

    # ------------------------------------------------------------------
    # Vault read path (umphreys-vault Postgres — the source of truth)
    # ------------------------------------------------------------------
    vault_enabled: bool = Field(
        default=True,
        description=(
            "If True, read tools serve from the umphreys-vault Postgres database "
            "(the source of truth). Hot-window shows always read live from ATU "
            "regardless of this flag so an in-progress setlist isn't frozen."
        ),
    )
    vault_hot_window_hours: int = Field(
        default=24,
        ge=1,
        description="Shows newer than this many hours are always read from the live ATU API.",
    )
    vault_max_stale_hours: int = Field(
        default=36,
        ge=1,
        description=(
            "Health reports 'degraded' if the last successful ETL run is older "
            "than this many hours, so callers can detect a stuck pipeline."
        ),
    )
    # ------------------------------------------------------------------
    # Advisory X/Twitter staging merge (hot-window only)
    # ------------------------------------------------------------------
    x_merge_enabled: bool = Field(
        default=True,
        description=(
            "If True, hot-window get_show reads merge advisory X/Twitter-sourced "
            "rows from the x_setlist_staging vault table on top of (or in the "
            "absence of) authoritative ATU rows. Staging is IGNORED entirely "
            "outside the hot window, so a bad X read is never permanent."
        ),
    )
    x_min_confidence: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum confidence (0-1) an x_setlist_staging row must carry to be "
            "merged into a hot-window setlist. Lower-confidence advisory rows are "
            "dropped."
        ),
    )

    pg_host: str = Field(default="postgres")
    pg_port: int = Field(default=5432, ge=1, le=65535)
    pg_db: str = Field(default="umphreys")
    pg_user: str = Field(default="umphreys")
    pg_password: SecretStr = Field(default=SecretStr(""))

    @property
    def pg_dsn(self) -> str:
        """Build a PostgreSQL DSN from vault connection settings."""
        pw = self.pg_password.get_secret_value()
        return f"postgresql://{self.pg_user}:{pw}@{self.pg_host}:{self.pg_port}/{self.pg_db}"

    def safe_repr(self) -> dict[str, object]:
        """Return a redacted dict suitable for logging at startup."""
        return {
            "stub_mode": self.stub_mode,
            "atu_base_url": self.atu_base_url,
            "atu_artist_id": self.atu_artist_id,
            "cache_db_path": self.cache_db_path,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "hot_window_cache_ttl_seconds": self.hot_window_cache_ttl_seconds,
            "throttle_atu_rps": self.throttle_atu_rps,
            "mcp_host": self.mcp_host,
            "mcp_port": self.mcp_port,
            "log_level": self.log_level,
            "log_format": self.log_format,
            "vault_enabled": self.vault_enabled,
            "vault_hot_window_hours": self.vault_hot_window_hours,
            "vault_max_stale_hours": self.vault_max_stale_hours,
            "x_merge_enabled": self.x_merge_enabled,
            "x_min_confidence": self.x_min_confidence,
            "pg_host": self.pg_host,
            "pg_port": self.pg_port,
            "pg_db": self.pg_db,
            "pg_user": self.pg_user,
            # pg_password intentionally omitted
        }


def load_settings() -> Settings:
    """Build a Settings instance from the environment. Raises on invalid config."""
    return Settings()
