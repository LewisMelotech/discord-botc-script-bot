"""Environment parsing, and the two questions /alias asks of it: may we write, and where."""

from __future__ import annotations

import os

import pytest

from botcbot.config import Config, ConfigError


def _isolate(monkeypatch) -> None:
    """Drop every setting this bot reads, so a real .env cannot change a result."""
    for name in list(os.environ):
        if name.startswith(("BOTC_", "DISCORD_")) or name == "LOG_LEVEL":
            monkeypatch.delenv(name, raising=False)


def config(monkeypatch, **env: str) -> Config:
    _isolate(monkeypatch)
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    # Never the .env file: these tests describe the environment they set, and nothing else.
    return Config.from_env(load_dotenv_file=False)


def test_api_credentials_are_absent_by_default_and_the_bot_stays_read_only(monkeypatch):
    settings = config(monkeypatch)

    assert (settings.api_user, settings.api_password) == (None, None)
    assert settings.can_write is False
    # And nothing else about the bot changes: it still serves the public site.
    assert settings.base_url == "https://www.botcscripts.com"


def test_both_credentials_together_are_what_allows_a_write(monkeypatch):
    settings = config(
        monkeypatch,
        BOTC_BASE_URL="https://scripts.example.com",
        BOTC_API_USER="discordbot",
        BOTC_API_PASSWORD="botpass",
    )

    assert (settings.api_user, settings.api_password) == ("discordbot", "botpass")
    assert settings.can_write is True


@pytest.mark.parametrize(
    "half", [{"BOTC_API_USER": "discordbot"}, {"BOTC_API_PASSWORD": "botpass"}]
)
def test_half_a_credential_is_refused_rather_than_quietly_read_only(monkeypatch, half):
    # Silently running read-only would be discovered only when /alias refuses to write,
    # a long way from the typo that caused it.
    with pytest.raises(ConfigError, match="must be set together"):
        config(monkeypatch, **half)


def test_a_username_is_trimmed_but_a_password_is_taken_exactly_as_written(monkeypatch):
    # Trimming a password that legitimately ends in a space turns a correct credential
    # into a rejected one, and the failure then looks like a typo in the username.
    settings = config(
        monkeypatch, BOTC_API_USER="  discordbot  ", BOTC_API_PASSWORD="  pass word  "
    )

    assert settings.api_user == "discordbot"
    assert settings.api_password == "  pass word  "


def test_a_username_containing_a_colon_is_refused_at_startup(monkeypatch):
    # HTTP Basic joins the pair with a colon, so this cannot be encoded at all.
    with pytest.raises(ConfigError, match="colon"):
        config(monkeypatch, BOTC_API_USER="bot:user", BOTC_API_PASSWORD="botpass")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://www.botcscripts.com",
        "https://botcscripts.com",
        "https://www.botcscripts.com/",
        "https://WWW.BotcScripts.com",
    ],
)
def test_the_public_site_is_recognised_however_it_is_written(monkeypatch, base_url):
    # Custom ids are a fork's feature, so /alias has to know it is pointed at the site
    # that does not have them before it spends a request finding out.
    assert config(monkeypatch, BOTC_BASE_URL=base_url).is_public_site is True


@pytest.mark.parametrize(
    "base_url",
    ["https://scripts.example.com", "http://botc-scripts:8000", "https://example.test"],
)
def test_a_self_hosted_instance_is_not_mistaken_for_the_public_site(monkeypatch, base_url):
    assert config(monkeypatch, BOTC_BASE_URL=base_url).is_public_site is False


def test_credentials_over_plain_http_are_flagged_as_cleartext(monkeypatch):
    # Not fatal: http://botc-scripts:8000 across a Docker Compose bridge is the intended
    # deployment. It is worth one warning at startup in case it is not that.
    over_http = config(
        monkeypatch,
        BOTC_BASE_URL="http://botc-scripts:8000",
        BOTC_API_USER="discordbot",
        BOTC_API_PASSWORD="botpass",
    )
    over_https = config(
        monkeypatch,
        BOTC_BASE_URL="https://scripts.example.com",
        BOTC_API_USER="discordbot",
        BOTC_API_PASSWORD="botpass",
    )

    assert over_http.credentials_are_cleartext is True
    assert over_https.credentials_are_cleartext is False
    # Nothing to expose without credentials, so plain http alone is not flagged.
    anonymous = config(monkeypatch, BOTC_BASE_URL="http://botc-scripts:8000")
    assert anonymous.credentials_are_cleartext is False
