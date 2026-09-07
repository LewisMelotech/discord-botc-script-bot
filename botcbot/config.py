"""Environment-driven configuration. Nothing is read from anywhere but the process env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://www.botcscripts.com"
DEFAULT_CACHE_PATH = "botc-suggestions.sqlite3"

_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})

# The public site. Custom ids are a self-hosted fork's feature, so pointing at either
# of these means /alias has nothing to work with, credentials or not.
_PUBLIC_HOSTS = frozenset({"botcscripts.com", "www.botcscripts.com"})


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    base_url: str
    guild_id: int | None
    http_timeout: float
    max_pdf_bytes: int
    render_dpi: int
    max_pages: int
    cache_path: str
    cache_entries: int
    log_level: str
    api_user: str | None = None
    api_password: str | None = None

    @property
    def can_write(self) -> bool:
        """Whether the bot may change anything on the instance, i.e. set custom ids."""
        return self.api_user is not None and self.api_password is not None

    @property
    def is_public_site(self) -> bool:
        """Whether ``base_url`` is botcscripts.com, which has no custom ids at all."""
        return (urlsplit(self.base_url).hostname or "").casefold() in _PUBLIC_HOSTS

    @property
    def credentials_are_cleartext(self) -> bool:
        """Whether API credentials would cross the network unencrypted."""
        return self.can_write and self.base_url.startswith("http://")

    @classmethod
    def from_env(cls, *, load_dotenv_file: bool = True) -> Config:
        if load_dotenv_file:
            load_dotenv()

        token = (os.environ.get("DISCORD_TOKEN") or "").strip()
        if not token:
            raise ConfigError(
                "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
            )

        base_url = (os.environ.get("BOTC_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError(
                f"BOTC_BASE_URL must start with http:// or https:// (got {base_url!r})."
            )

        raw_guild = (os.environ.get("DISCORD_GUILD_ID") or "").strip()
        guild_id: int | None = None
        if raw_guild:
            try:
                guild_id = int(raw_guild)
            except ValueError as exc:
                raise ConfigError(
                    f"DISCORD_GUILD_ID must be a numeric snowflake (got {raw_guild!r})."
                ) from exc

        cache_path = (os.environ.get("BOTC_CACHE_PATH") or DEFAULT_CACHE_PATH).strip()
        if not cache_path:
            raise ConfigError("BOTC_CACHE_PATH must be a file path, or left unset.")

        log_level = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
        if log_level not in _LOG_LEVELS:
            raise ConfigError(
                f"LOG_LEVEL must be one of {sorted(_LOG_LEVELS)} (got {log_level!r})."
            )

        api_user = (os.environ.get("BOTC_API_USER") or "").strip() or None
        # Deliberately not stripped, unlike every other setting here: trimming a
        # password that legitimately ends in a space turns a correct credential into a
        # rejected one, and the failure looks like a typo in the username instead.
        api_password = os.environ.get("BOTC_API_PASSWORD") or None
        if (api_user is None) != (api_password is None):
            # Half a credential is always a mistake, and running read-only anyway would
            # be discovered only when /alias refuses to write, far from the cause.
            raise ConfigError(
                "BOTC_API_USER and BOTC_API_PASSWORD must be set together, or both left "
                "unset. They are only needed to let /alias set custom ids on a "
                "self-hosted botc-scripts instance."
            )
        if api_user is not None and ":" in api_user:
            # HTTP Basic joins the pair with a colon, so a username containing one cannot
            # be encoded at all. Said here rather than as a ValueError at connect time.
            raise ConfigError(
                f"BOTC_API_USER must not contain a colon (got {api_user!r}); "
                "HTTP Basic authentication cannot encode one."
            )

        return cls(
            discord_token=token,
            base_url=base_url,
            guild_id=guild_id,
            http_timeout=_float_env("BOTC_HTTP_TIMEOUT", 60.0, minimum=5.0, maximum=600.0),
            max_pdf_bytes=_int_env(
                "BOTC_MAX_PDF_BYTES", 60 * 1024 * 1024, minimum=1024, maximum=512 * 1024 * 1024
            ),
            render_dpi=_int_env("BOTC_RENDER_DPI", 150, minimum=50, maximum=400),
            max_pages=_int_env("BOTC_MAX_PAGES", 10, minimum=1, maximum=10),
            cache_path=cache_path,
            # Zero disables the suggestion cache and leaves the database file uncreated.
            cache_entries=_int_env("BOTC_CACHE_ENTRIES", 500, minimum=0, maximum=100_000),
            log_level=log_level,
            api_user=api_user,
            api_password=api_password,
        )


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer (got {raw!r}).") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum} (got {value}).")
    return value


def _float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number (got {raw!r}).") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum} (got {value}).")
    return value
