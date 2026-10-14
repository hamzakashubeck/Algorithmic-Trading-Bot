"""Settings loaded from environment variables (and a local .env file, if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv

PAPER_URL = "https://paper-api.alpaca.markets"


class ConfigError(Exception):
    """Raised when required settings are missing or unsafe."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    secret_key: str
    base_url: str = PAPER_URL
    data_feed: str = "iex"
    allow_live: bool = False

    @property
    def paper(self) -> bool:
        return self.base_url.rstrip("/") == PAPER_URL

    def __repr__(self) -> str:
        # Keep secrets out of logs and tracebacks.
        return (
            f"Settings(base_url={self.base_url!r}, data_feed={self.data_feed!r}, "
            f"paper={self.paper}, allow_live={self.allow_live})"
        )


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


def load_settings() -> Settings:
    """Read settings from the environment. Values already set in the shell win over .env."""
    # Search from the working directory, not from wherever this module is installed.
    load_dotenv(find_dotenv(usecwd=True))

    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        raise ConfigError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set (copy .env.example to .env)."
        )

    settings = Settings(
        api_key=api_key,
        secret_key=secret_key,
        base_url=os.getenv("ALPACA_BASE_URL", PAPER_URL).strip() or PAPER_URL,
        data_feed=os.getenv("ALPACA_DATA_FEED", "iex").strip().lower(),
        allow_live=_env_bool("ALLOW_LIVE_TRADING"),
    )

    if not settings.paper and not settings.allow_live:
        raise ConfigError(
            f"ALPACA_BASE_URL is {settings.base_url!r}, which is not the paper endpoint. "
            "Set ALLOW_LIVE_TRADING=true if you really mean to trade real money."
        )
    return settings
