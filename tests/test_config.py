"""Environment parsing, and the two questions /alias asks of it: may we write, and where."""

from __future__ import annotations

import logging
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


def test_links_use_the_address_the_bot_calls_unless_a_public_one_is_given(monkeypatch):
    settings = config(monkeypatch, BOTC_BASE_URL="https://scripts.example.com")

    assert settings.public_url is None
    assert settings.link_url == "https://scripts.example.com"


def test_a_public_address_is_what_links_show_and_leaves_the_api_address_alone(monkeypatch):
    settings = config(
        monkeypatch,
        BOTC_BASE_URL="http://botc-scripts:8000",
        BOTC_PUBLIC_URL="https://scripts.example.com/",
    )

    # Trailing slash trimmed like BOTC_BASE_URL's, so a link never carries a double one.
    assert settings.link_url == "https://scripts.example.com"
    # Requests still go where they always went, and so does every check made on that
    # address: cleartext credentials are about the connection, not what people are shown.
    assert settings.base_url == "http://botc-scripts:8000"


def test_a_blank_public_address_counts_as_unset(monkeypatch):
    # What Compose passes when the variable it forwards from (SITE_URL) is empty, which is
    # the normal state of a stack that never set one.
    settings = config(
        monkeypatch, BOTC_BASE_URL="http://botc-scripts:8000", BOTC_PUBLIC_URL="  "
    )

    assert settings.public_url is None
    assert settings.link_url == "http://botc-scripts:8000"


def test_a_public_address_without_a_scheme_is_refused(monkeypatch):
    # Discord would post scripts.example.com/script/1/1.0.0 as plain text, not a link.
    with pytest.raises(ConfigError, match="BOTC_PUBLIC_URL must start with"):
        config(monkeypatch, BOTC_PUBLIC_URL="scripts.example.com")


OWN = "https://scripts.example.com"
PUBLIC_SITE = "https://www.botcscripts.com"


def test_auto_serves_only_what_is_on_the_server_on_your_own_instance(monkeypatch):
    settings = config(monkeypatch, BOTC_BASE_URL=OWN)

    assert (settings.selection_setting, settings.selection, settings.online_only) == (
        "auto",
        "online",
        True,
    )


@pytest.mark.parametrize("base_url", [PUBLIC_SITE, "https://botcscripts.com/", "http://WWW.BOTCSCRIPTS.COM"])
def test_auto_serves_the_newest_version_on_the_public_site(monkeypatch, base_url):
    # It has no Minecraft server, so there is nothing to filter by.
    settings = config(monkeypatch, BOTC_BASE_URL=base_url)

    assert (settings.selection_setting, settings.selection, settings.online_only) == (
        "auto",
        "latest",
        False,
    )


def test_the_default_instance_is_the_public_site_so_the_default_selection_is_latest(monkeypatch):
    assert config(monkeypatch).selection == "latest"


@pytest.mark.parametrize("base_url", [OWN, PUBLIC_SITE])
@pytest.mark.parametrize(
    ("value", "expected"), [("online", "online"), ("latest", "latest"), (" LATEST ", "latest")]
)
def test_an_explicit_selection_overrides_auto_on_either_host(
    monkeypatch, caplog, base_url, value, expected
):
    settings = config(monkeypatch, BOTC_BASE_URL=base_url, BOTC_SELECTION=value)

    assert (settings.selection, settings.selection_setting) == (expected, expected)


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [("true", "online"), ("yes", "online"), ("false", "latest"), ("0", "latest")],
)
def test_the_older_online_only_setting_still_means_what_it_did(monkeypatch, legacy, expected):
    settings = config(monkeypatch, BOTC_BASE_URL=OWN, BOTC_ONLINE_ONLY=legacy)

    assert settings.selection == expected
    # And the log can say that it was the older setting that decided.
    assert settings.selection_setting == expected


def test_selection_wins_over_the_older_setting_and_a_disagreement_is_reported(
    monkeypatch, caplog
):
    with caplog.at_level(logging.WARNING, logger="botcbot.config"):
        settings = config(
            monkeypatch, BOTC_BASE_URL=OWN, BOTC_SELECTION="latest", BOTC_ONLINE_ONLY="true"
        )

    assert settings.selection == "latest"
    assert "disagree" in caplog.text


def test_settings_that_agree_are_not_reported(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="botcbot.config"):
        config(monkeypatch, BOTC_BASE_URL=OWN, BOTC_SELECTION="online", BOTC_ONLINE_ONLY="true")

    assert caplog.records == []


def test_an_explicit_auto_is_not_overridden_by_the_older_setting(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="botcbot.config"):
        settings = config(
            monkeypatch, BOTC_BASE_URL=OWN, BOTC_SELECTION="auto", BOTC_ONLINE_ONLY="false"
        )

    assert settings.selection == "online"
    assert caplog.records == []


def test_online_on_the_public_site_is_allowed_but_warned_about(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="botcbot.config"):
        settings = config(monkeypatch, BOTC_SELECTION="online")

    assert settings.selection == "online"
    assert "no Minecraft server status" in caplog.text


def test_auto_and_latest_on_the_public_site_are_quiet(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="botcbot.config"):
        config(monkeypatch)
        config(monkeypatch, BOTC_SELECTION="latest")

    assert caplog.records == []


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"BOTC_SELECTION": "sometimes"}, "BOTC_SELECTION must be one of"),
        ({"BOTC_ONLINE_ONLY": "maybe"}, "BOTC_ONLINE_ONLY must be true or false"),
    ],
)
def test_an_unreadable_selection_setting_is_refused(monkeypatch, env, message):
    with pytest.raises(ConfigError, match=message):
        config(monkeypatch, **env)
