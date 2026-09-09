from __future__ import annotations

import json
import logging
import time

import discord
import pytest
from conftest import (
    FakeInteraction,
    FakeSession,
    make_pdf,
    page,
    script_detail,
    version_row,
)
from discord import app_commands

from botcbot import discord_app
from botcbot.botcscripts import BotcScriptsClient
from botcbot.cache import ScriptCache
from botcbot.config import Config
from botcbot.discord_app import (
    MAX_CHOICE_NAME,
    MAX_CHOICES,
    ScriptBot,
    _api_auth,
    _deliver,
    _Delivery,
    _on_command_error,
    _Reply,
    alias_clear,
    alias_group,
    alias_set,
    alias_show,
    json_command,
    script_command,
    script_query_autocomplete,
)

BASE = "https://example.test"
PUBLIC = "https://www.botcscripts.com"
CREDENTIALS = ("discordbot", "botpass")

ADMIN = discord.Permissions(administrator=True)


def build_bot(
    tmp_path,
    routes,
    *,
    delay: float = 0.0,
    cache_entries: int = 100,
    base_url: str = BASE,
    credentials: tuple[str, str] | None = None,
    online_only: bool = False,
):
    user, password = credentials or (None, None)
    config = Config(
        discord_token="not-used-offline",
        base_url=base_url,
        guild_id=None,
        online_only=online_only,
        http_timeout=60.0,
        max_pdf_bytes=10 * 1024 * 1024,
        render_dpi=100,
        max_pages=10,
        cache_path=str(tmp_path / "suggestions.sqlite3"),
        cache_entries=cache_entries,
        log_level="INFO",
        api_user=user,
        api_password=password,
    )
    bot = ScriptBot(config)
    session = FakeSession(routes, delay=delay)
    # Through _api_auth rather than around it, so the tests exercise the same decision
    # setup_hook makes about whether this bot may write.
    bot.api = BotcScriptsClient(session, base_url=base_url, auth=_api_auth(config))
    return bot, session


def script_routes(
    *, pdf: bytes | None = None, with_content: bool = True, slug: str | None = None
) -> dict:
    row = version_row(pk=22755, script_id=13108, name="Sects and Violets", slug=slug)
    if not with_content:
        del row["content"]
    routes: dict = {"/api/scripts/": page([row])}
    if pdf is not None:
        routes["/script/13108/1.0.0/download_pdf"] = (200, pdf)
    return routes


@pytest.mark.asyncio
async def test_script_posts_the_rendered_pages_and_no_json_file(tmp_path):
    bot, _ = build_bot(tmp_path, script_routes(pdf=make_pdf(2)))
    interaction = FakeInteraction(bot, command_name="script")

    await script_command.callback(interaction, "Sects and Violets")

    assert [message.filenames for message in interaction.sent] == [["page_001.png", "page_002.png"]]
    assert "Sects and Violets" in (interaction.sent[0].content or "")
    bot.cache.close()


@pytest.mark.asyncio
async def test_json_delivers_the_file_and_never_downloads_the_pdf(tmp_path):
    bot, session = build_bot(tmp_path, script_routes(pdf=make_pdf(2)))
    interaction = FakeInteraction(bot, command_name="json")

    await json_command.callback(interaction, "Sects and Violets")

    (message,) = interaction.sent
    assert message.filenames == ["Sects_and_Violets_1_0_0.json"]
    assert json.loads(message.files[0][1]) == [{"id": "imp"}]
    assert not any("download_pdf" in request for request in session.requests)
    # The search row inlines the JSON, so the whole command is one upstream request.
    assert len(session.requests) == 1
    bot.cache.close()


@pytest.mark.asyncio
async def test_script_is_public_and_json_is_private_when_output_is_omitted(tmp_path):
    bot, _ = build_bot(tmp_path, script_routes(pdf=make_pdf(1)))

    public = FakeInteraction(bot, command_name="script")
    await script_command.callback(public, "Sects and Violets")
    assert public.response.deferred == {"thinking": True, "ephemeral": False}
    assert not any(message.ephemeral for message in public.sent)

    private = FakeInteraction(bot, command_name="json")
    await json_command.callback(private, "Sects and Violets")
    assert private.response.deferred == {"thinking": True, "ephemeral": True}
    assert all(message.ephemeral for message in private.sent)
    bot.cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "name", "output", "expected_ephemeral"),
    [
        (script_command, "script", "private", True),
        (script_command, "script", "public", False),
        (json_command, "json", "public", False),
        (json_command, "json", "private", True),
    ],
)
async def test_the_output_parameter_overrides_each_command_default(
    tmp_path, command, name, output, expected_ephemeral
):
    bot, _ = build_bot(tmp_path, script_routes(pdf=make_pdf(1)))
    interaction = FakeInteraction(bot, command_name=name)

    await command.callback(interaction, "Sects and Violets", None, output)

    assert interaction.response.deferred == {"thinking": True, "ephemeral": expected_ephemeral}
    assert interaction.extras["ephemeral"] is expected_ephemeral
    assert all(message.ephemeral is expected_ephemeral for message in interaction.sent)
    bot.cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ephemeral", [True, False])
async def test_every_message_of_a_multi_part_reply_keeps_the_chosen_visibility(tmp_path, ephemeral):
    # A followup never inherits ephemerality: pages 11+ would go public by default.
    bot, _ = build_bot(tmp_path, {})
    interaction = FakeInteraction(bot)
    await interaction.response.defer(thinking=True, ephemeral=ephemeral)
    delivery = _Delivery("header", tuple((f"page_{i:03d}.png", b"x") for i in range(12)))

    assert await _deliver(interaction, _Reply(interaction, ephemeral=ephemeral), delivery)

    assert [message.kind for message in interaction.sent] == ["edit", "followup"]
    assert [len(message.files) for message in interaction.sent] == [10, 2]
    assert all(message.ephemeral is ephemeral for message in interaction.sent)
    bot.cache.close()


@pytest.mark.asyncio
async def test_an_expired_token_stops_the_delivery_rather_than_404ing_through_it(tmp_path):
    bot, _ = build_bot(tmp_path, {})
    interaction = FakeInteraction(bot)
    interaction.expired = True

    assert not await _deliver(
        interaction, _Reply(interaction, ephemeral=False), _Delivery("header", ())
    )
    assert interaction.sent == []
    bot.cache.close()


@pytest.mark.asyncio
async def test_a_script_with_no_pdf_says_so_and_points_at_the_json_command(tmp_path):
    bot, _ = build_bot(tmp_path, script_routes(pdf=None))
    interaction = FakeInteraction(bot, command_name="script")

    await script_command.callback(interaction, "Sects and Violets")

    (message,) = interaction.sent
    assert message.files == []
    assert "no PDF has been uploaded" in (message.content or "")
    assert "/json query:13108" in (message.content or "")
    bot.cache.close()


@pytest.mark.asyncio
async def test_json_reports_a_failed_fetch_instead_of_delivering_an_empty_message(tmp_path):
    bot, _ = build_bot(tmp_path, script_routes(with_content=False))
    interaction = FakeInteraction(bot, command_name="json")

    await json_command.callback(interaction, "Sects and Violets")

    (message,) = interaction.sent
    assert message.files == []
    assert "Could not" in (message.content or "")
    bot.cache.close()


@pytest.mark.asyncio
async def test_an_ambiguous_name_names_the_command_the_user_actually_ran(tmp_path):
    rows = [
        version_row(pk=1, script_id=135, name="Bad Moon Rising"),
        version_row(pk=2, script_id=4665, name="Bad Moon Boffins"),
    ]
    bot, _ = build_bot(tmp_path, {"/api/scripts/": page(rows)})
    interaction = FakeInteraction(bot, command_name="json")

    await json_command.callback(interaction, "Bad Moon")

    assert "`/json`" in (interaction.sent[0].content or "")
    bot.cache.close()


@pytest.mark.asyncio
async def test_a_numeric_query_resolves_by_id_without_a_fuzzy_search(tmp_path):
    # This is the shape a picked autocomplete suggestion submits.
    routes = {
        "/api/script_ids/13108/": {
            "pk": 13108,
            "name": "Sects and Violets",
            "versions": {"1.0.0": f"{BASE}/api/scripts/22755/"},
            "latest_version": f"{BASE}/api/scripts/22755/",
        },
        "/api/scripts/22755/": version_row(pk=22755, script_id=13108, name="Sects and Violets"),
    }
    bot, session = build_bot(tmp_path, routes)
    interaction = FakeInteraction(bot, command_name="json")

    await json_command.callback(interaction, "13108")

    assert interaction.sent[0].filenames == ["Sects_and_Violets_1_0_0.json"]
    assert not any("search=" in request for request in session.requests)
    bot.cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("pdf", [make_pdf(1), None])
async def test_a_delivered_script_is_recorded_for_autocomplete_under_its_guild(tmp_path, pdf):
    # Recorded even with no PDF: /json can still serve it, and both commands share
    # the one suggestion cache.
    bot, _ = build_bot(tmp_path, script_routes(pdf=pdf))
    interaction = FakeInteraction(bot, command_name="script", guild_id=42)

    await script_command.callback(interaction, "Sects and Violets")

    entries = bot.cache.suggest("sects", guild_id=42)
    assert [(e.script_id, e.name, e.guild_id) for e in entries] == [
        (13108, "Sects and Violets", 42)
    ]
    bot.cache.close()


@pytest.mark.asyncio
async def test_a_script_that_could_not_be_resolved_is_not_recorded(tmp_path):
    bot, _ = build_bot(tmp_path, {"/api/scripts/": page([])})
    interaction = FakeInteraction(bot, command_name="script")

    await script_command.callback(interaction, "zzzznotascriptzzzz")

    assert bot.cache.count() == 0
    bot.cache.close()


@pytest.mark.asyncio
async def test_autocomplete_offers_cached_scripts_before_live_ones(tmp_path):
    live = page([version_row(pk=9, script_id=900, name="Trouble Abroad", author=None)])
    bot, _ = build_bot(tmp_path, {"/api/scripts/": live})
    bot.cache.record(script_id=13108, name="Trouble Brewing", author="TPI", guild_id=42)
    interaction = FakeInteraction(
        bot, guild_id=42, interaction_type=discord.InteractionType.autocomplete
    )

    choices = await script_query_autocomplete(interaction, "trouble")

    assert [choice.value for choice in choices] == ["13108", "900"]
    assert choices[0].name == "Trouble Brewing — TPI"
    bot.cache.close()


@pytest.mark.asyncio
async def test_autocomplete_falls_back_to_the_cache_when_the_live_search_is_slow(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(discord_app, "_LIVE_SEARCH_BUDGET", 0.05)
    live = page([version_row(pk=9, script_id=900, name="Trouble Abroad")])
    bot, _ = build_bot(tmp_path, {"/api/scripts/": live}, delay=1.0)
    bot.cache.record(script_id=13108, name="Trouble Brewing", guild_id=42)
    interaction = FakeInteraction(
        bot, guild_id=42, interaction_type=discord.InteractionType.autocomplete
    )

    started = time.perf_counter()
    choices = await script_query_autocomplete(interaction, "trouble")
    elapsed = time.perf_counter() - started

    assert [choice.value for choice in choices] == ["13108"]
    assert elapsed < 0.5
    bot.cache.close()


@pytest.mark.asyncio
async def test_autocomplete_returns_nothing_when_both_sources_fail(tmp_path):
    bot, _ = build_bot(tmp_path, {"/api/scripts/": (200, b"<html>maintenance</html>")})
    # A directory is not a database: every cache call raises.
    bot.cache = ScriptCache(tmp_path)
    interaction = FakeInteraction(bot, interaction_type=discord.InteractionType.autocomplete)

    assert await script_query_autocomplete(interaction, "trouble") == []


@pytest.mark.asyncio
async def test_autocomplete_stays_inside_discords_choice_and_name_limits(tmp_path):
    rows = [
        version_row(pk=i, script_id=i, name="Sects and Violets " * 20, author="A" * 200)
        for i in range(1, 41)
    ]
    bot, _ = build_bot(tmp_path, {"/api/scripts/": page(rows)})
    interaction = FakeInteraction(bot, interaction_type=discord.InteractionType.autocomplete)

    choices = await script_query_autocomplete(interaction, "sects")

    assert len(choices) == MAX_CHOICES
    assert all(1 <= len(choice.name) <= MAX_CHOICE_NAME for choice in choices)
    # The author is only appended when the whole label still fits.
    assert "A" not in choices[0].name
    bot.cache.close()


@pytest.mark.asyncio
async def test_autocomplete_with_no_text_offers_the_most_recent_scripts(tmp_path):
    bot, session = build_bot(tmp_path, {"/api/scripts/": page([])})
    bot.cache.record(script_id=1, name="Older", used_at=1.0)
    bot.cache.record(script_id=2, name="Newer", used_at=2.0)
    interaction = FakeInteraction(bot, interaction_type=discord.InteractionType.autocomplete)

    choices = await script_query_autocomplete(interaction, "")

    assert [choice.value for choice in choices] == ["2", "1"]
    # No live search on an empty query: there is nothing to search for.
    assert session.requests == []
    bot.cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ephemeral", [True, False])
async def test_the_error_handler_matches_the_visibility_the_invocation_chose(tmp_path, ephemeral):
    bot, _ = build_bot(tmp_path, {})
    interaction = FakeInteraction(bot, command_name="script")
    interaction.extras["ephemeral"] = ephemeral
    await interaction.response.defer(thinking=True, ephemeral=ephemeral)

    await _on_command_error(interaction, app_commands.AppCommandError("boom"))

    assert interaction.sent[-1].ephemeral is ephemeral
    bot.cache.close()


@pytest.mark.asyncio
async def test_a_failure_before_the_defer_is_reported_privately(tmp_path):
    bot, _ = build_bot(tmp_path, {})
    interaction = FakeInteraction(bot, command_name="script")

    await _on_command_error(
        interaction, app_commands.CommandOnCooldown(app_commands.Cooldown(2, 15.0), 4.0)
    )

    assert interaction.sent[-1].ephemeral is True
    assert "Slow down" in (interaction.sent[-1].content or "")
    bot.cache.close()


@pytest.mark.asyncio
async def test_the_error_handler_never_tries_to_reply_to_an_autocomplete_interaction(tmp_path):
    bot, _ = build_bot(tmp_path, {})
    interaction = FakeInteraction(bot, interaction_type=discord.InteractionType.autocomplete)

    await _on_command_error(interaction, app_commands.AppCommandError("boom"))

    assert interaction.sent == []
    bot.cache.close()


@pytest.mark.asyncio
async def test_an_expired_interaction_is_not_replied_to(tmp_path):
    bot, _ = build_bot(tmp_path, {})
    interaction = FakeInteraction(bot, command_name="script")
    interaction.expired = True

    await _on_command_error(interaction, app_commands.AppCommandError("boom"))

    assert interaction.sent == []
    bot.cache.close()


def custom_id_routes(*, pdf: bytes | None = None) -> dict:
    """What a fork serves for a script whose custom id is ``snv``."""
    routes: dict = {
        "/api/script_ids/slug/snv/": script_detail(
            pk=13108, name="Sects and Violets", version_pk=22755, slug="snv"
        ),
        "/api/scripts/22755/": version_row(
            pk=22755, script_id=13108, name="Sects and Violets", slug="snv"
        ),
    }
    if pdf is not None:
        routes["/script/13108/1.0.0/download_pdf"] = (200, pdf)
    return routes


@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "name"), [(script_command, "script"), (json_command, "json")])
async def test_both_commands_take_a_custom_id_exactly_as_they_take_a_numeric_id(
    tmp_path, command, name
):
    # One resolution path serves both, so neither command needs to know about custom ids.
    bot, session = build_bot(tmp_path, custom_id_routes(pdf=make_pdf(1)))
    interaction = FakeInteraction(bot, command_name=name)

    await command.callback(interaction, "snv")

    assert "Sects and Violets" in (interaction.sent[0].content or "")
    assert not any("search=" in request for request in session.requests)
    bot.cache.close()


@pytest.mark.asyncio
async def test_a_custom_id_becomes_the_page_link_the_reply_carries(tmp_path):
    bot, _ = build_bot(tmp_path, custom_id_routes())
    interaction = FakeInteraction(bot, command_name="json")

    await json_command.callback(interaction, "snv")

    assert f"<{BASE}/script/snv/1.0.0>" in (interaction.sent[0].content or "")
    bot.cache.close()


@pytest.mark.asyncio
async def test_autocomplete_offers_a_custom_id_as_the_value_and_shows_it_in_the_label(
    tmp_path,
):
    # A cached script that has a custom id, and a live one that does not: picking either
    # submits its canonical id, and both resolve exactly.
    live = page([version_row(pk=9, script_id=900, name="Trouble Abroad", author=None)])
    bot, _ = build_bot(tmp_path, {"/api/scripts/": live})
    bot.cache.record(script_id=13108, name="Trouble Brewing", author="TPI", guild_id=42, slug="tb")
    interaction = FakeInteraction(
        bot, guild_id=42, interaction_type=discord.InteractionType.autocomplete
    )

    choices = await script_query_autocomplete(interaction, "trouble")

    assert [choice.value for choice in choices] == ["tb", "900"]
    assert choices[0].name == "Trouble Brewing (tb) — TPI"
    bot.cache.close()


@pytest.mark.asyncio
async def test_a_delivered_script_records_its_custom_id_for_later_suggestions(tmp_path):
    # The cache is what answers when the instance is slow, so a custom id has to survive
    # in it or suggestions would lose them exactly when the live search cannot help.
    bot, _ = build_bot(tmp_path, script_routes(pdf=make_pdf(1), slug="snv"))
    interaction = FakeInteraction(bot, command_name="script", guild_id=42)

    await script_command.callback(interaction, "Sects and Violets")

    entries = bot.cache.suggest("snv", guild_id=42)
    assert [(entry.script_id, entry.slug) for entry in entries] == [(13108, "snv")]
    bot.cache.close()


def test_the_alias_group_is_registered_as_administrators_only_and_guild_only():
    # Discord ignores both of these on a subcommand, so they have to sit on the Group.
    # default_member_permissions=0 hides the command from non-administrators; it is a
    # server-side hint, which is why each subcommand also carries a real check.
    assert alias_group.default_permissions == discord.Permissions(0)
    assert alias_group.guild_only is True
    assert [sub.default_permissions for sub in alias_group.commands] == [None, None, None]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [alias_set, alias_clear, alias_show])
async def test_alias_refuses_anyone_who_is_not_a_server_administrator(tmp_path, command):
    # The callback is invoked directly everywhere else in this file, which bypasses
    # checks entirely — the gate is only exercised by driving _check_can_run.
    bot, _ = build_bot(tmp_path, {})
    outsider = FakeInteraction(bot, command_name=command.qualified_name)

    with pytest.raises(app_commands.MissingPermissions):
        await command._check_can_run(outsider)

    administrator = FakeInteraction(bot, command_name=command.qualified_name, permissions=ADMIN)
    assert await command._check_can_run(administrator)
    bot.cache.close()


@pytest.mark.asyncio
async def test_a_permission_refusal_is_reported_privately_and_not_logged_as_a_fault(
    tmp_path, caplog
):
    bot, _ = build_bot(tmp_path, {})
    interaction = FakeInteraction(bot, command_name="alias set")

    with caplog.at_level(logging.WARNING, logger="botcbot.discord_app"):
        await _on_command_error(interaction, app_commands.MissingPermissions(["administrator"]))

    (message,) = interaction.sent
    assert message.ephemeral is True
    assert "administrators only" in (message.content or "")
    # An expected refusal, not a fault: nothing from this module at WARNING or above, so
    # a non-administrator poking /alias cannot fill the log with tracebacks.
    assert [record for record in caplog.records if record.name == discord_app.__name__] == []
    bot.cache.close()


@pytest.mark.asyncio
async def test_alias_set_writes_the_custom_id_and_answers_privately(tmp_path):
    routes = {
        **script_routes(),
        "PATCH /api/script_ids/13108/slug/": {
            "pk": 13108,
            "name": "Sects and Violets",
            "slug": "snv",
        },
    }
    bot, session = build_bot(tmp_path, routes, credentials=CREDENTIALS)
    interaction = FakeInteraction(bot, command_name="alias set", permissions=ADMIN)

    await alias_set.callback(interaction, "Sects and Violets", "SNV")

    (message,) = interaction.sent
    # Private with no output parameter to choose otherwise: an admin action, not content.
    assert interaction.response.deferred == {"thinking": True, "ephemeral": True}
    assert message.ephemeral is True
    assert "`snv`" in (message.content or "")
    assert "/script query:snv" in (message.content or "")
    # Folded on the way out, so SNV and snv are the same custom id rather than two.
    assert session.payloads == [{"slug": "snv"}]
    bot.cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("custom_id", "expected"),
    [
        ("13108", "reserved for script ids"),
        ("-12", "reserved for script ids"),
        ("Sects and Violets", "not a valid custom id"),
        ("double--hyphen", "not a valid custom id"),
        ("a", "at least 2 characters"),
    ],
)
async def test_alias_set_answers_a_malformed_custom_id_without_spending_a_request(
    tmp_path, custom_id, expected
):
    bot, session = build_bot(tmp_path, script_routes(), credentials=CREDENTIALS)
    interaction = FakeInteraction(bot, command_name="alias set", permissions=ADMIN)

    await alias_set.callback(interaction, "Sects and Violets", custom_id)

    assert expected in (interaction.sent[0].content or "")
    assert session.requests == []
    bot.cache.close()


@pytest.mark.asyncio
async def test_alias_set_explains_missing_credentials_instead_of_failing_at_the_api(
    tmp_path,
):
    bot, session = build_bot(tmp_path, script_routes())  # No BOTC_API_USER/PASSWORD.
    interaction = FakeInteraction(bot, command_name="alias set", permissions=ADMIN)

    await alias_set.callback(interaction, "Sects and Violets", "snv")

    content = interaction.sent[0].content or ""
    assert "BOTC_API_USER" in content and "BOTC_API_PASSWORD" in content
    assert "scripts.api_write_permission" in content
    assert session.requests == []
    bot.cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "args"),
    [(alias_set, ("snv",)), (alias_clear, ()), (alias_show, ())],
)
async def test_alias_says_custom_ids_need_a_self_hosted_instance_on_the_public_site(
    tmp_path, command, args
):
    bot, session = build_bot(tmp_path, {}, base_url=PUBLIC, credentials=CREDENTIALS)
    interaction = FakeInteraction(bot, command_name=command.qualified_name, permissions=ADMIN)

    await command.callback(interaction, "Sects and Violets", *args)

    content = interaction.sent[0].content or ""
    assert "self-hosted" in content
    assert "BOTC_BASE_URL" in content
    assert session.requests == []
    bot.cache.close()


@pytest.mark.asyncio
async def test_alias_set_surfaces_the_instances_own_refusal_rather_than_a_traceback(
    tmp_path,
):
    refusal = "That slug is already used by another script."
    routes = {
        **script_routes(),
        "PATCH /api/script_ids/13108/slug/": (400, json.dumps({"slug": [refusal]}).encode()),
    }
    bot, _ = build_bot(tmp_path, routes, credentials=CREDENTIALS)
    interaction = FakeInteraction(bot, command_name="alias set", permissions=ADMIN)

    await alias_set.callback(interaction, "Sects and Violets", "snv")

    content = interaction.sent[0].content or ""
    assert refusal in content
    assert BASE in content
    bot.cache.close()


@pytest.mark.asyncio
async def test_alias_set_reports_credentials_the_instance_rejected(tmp_path):
    detail = "Invalid username/password."
    routes = {
        **script_routes(),
        "PATCH /api/script_ids/13108/slug/": (403, json.dumps({"detail": detail}).encode()),
    }
    bot, _ = build_bot(tmp_path, routes, credentials=CREDENTIALS)
    interaction = FakeInteraction(bot, command_name="alias set", permissions=ADMIN)

    await alias_set.callback(interaction, "Sects and Violets", "snv")

    content = interaction.sent[0].content or ""
    assert detail in content
    assert "discordbot" in content  # Which user was refused, for the admin who ran it.
    bot.cache.close()


@pytest.mark.asyncio
async def test_alias_clear_removes_the_custom_id_and_says_what_it_was(tmp_path):
    routes = {
        **script_routes(slug="snv"),
        "PATCH /api/script_ids/13108/slug/": {
            "pk": 13108,
            "name": "Sects and Violets",
            "slug": None,
        },
    }
    bot, session = build_bot(tmp_path, routes, credentials=CREDENTIALS)
    interaction = FakeInteraction(bot, command_name="alias clear", permissions=ADMIN)

    await alias_clear.callback(interaction, "Sects and Violets")

    content = interaction.sent[0].content or ""
    assert "no custom id" in content
    assert "`snv`" in content
    # An explicit null, which the fork requires: an empty body is a 400, not a no-op.
    assert session.payloads == [{"slug": None}]
    bot.cache.close()


@pytest.mark.asyncio
async def test_alias_show_reads_the_custom_id_without_credentials_or_a_write(tmp_path):
    bot, session = build_bot(tmp_path, script_routes(slug="snv"))
    interaction = FakeInteraction(bot, command_name="alias show", permissions=ADMIN)

    await alias_show.callback(interaction, "Sects and Violets")

    assert "`snv`" in (interaction.sent[0].content or "")
    assert not any(request.startswith("PATCH") for request in session.requests)
    bot.cache.close()


@pytest.mark.asyncio
async def test_alias_show_says_how_to_give_a_script_that_has_none_one(tmp_path):
    bot, _ = build_bot(tmp_path, script_routes())
    interaction = FakeInteraction(bot, command_name="alias show", permissions=ADMIN)

    await alias_show.callback(interaction, "Sects and Violets")

    content = interaction.sent[0].content or ""
    assert "no custom id" in content
    assert "/alias set query:13108" in content
    bot.cache.close()


@pytest.mark.asyncio
async def test_alias_reuses_the_ambiguous_and_not_found_messages_the_others_use(tmp_path):
    rows = [
        version_row(pk=1, script_id=135, name="Bad Moon Rising"),
        version_row(pk=2, script_id=4665, name="Bad Moon Boffins"),
    ]
    bot, _ = build_bot(tmp_path, {"/api/scripts/": page(rows)}, credentials=CREDENTIALS)
    interaction = FakeInteraction(bot, command_name="alias set", permissions=ADMIN)

    await alias_set.callback(interaction, "Bad Moon", "snv")

    content = interaction.sent[0].content or ""
    # Named as the subcommand the user actually ran, not as /script.
    assert "`/alias set`" in content
    assert "`135`" in content and "`4665`" in content
    bot.cache.close()


def test_a_guild_sync_also_clears_the_global_commands(tmp_path):
    """Regression: commands appeared twice in Discord's picker.

    A run without DISCORD_GUILD_ID registers globally, and Discord keeps that set until
    told otherwise, so adding a guild id later left both registrations live.
    """
    import asyncio

    calls: list[str] = []

    class StubTree:
        def add_command(self, *_args, **_kwargs):
            pass

        def error(self, handler):
            return handler

        def copy_global_to(self, *, guild):
            calls.append(f"copy_global_to({guild.id})")

        def clear_commands(self, *, guild):
            calls.append(f"clear_commands(guild={guild})")

        async def sync(self, *, guild=None):
            calls.append(f"sync(guild={guild.id if guild else None})")

    from dataclasses import replace

    bot, _session = build_bot(tmp_path, {})
    bot.config = replace(bot.config, guild_id=42)
    bot.tree = StubTree()

    asyncio.run(bot.setup_hook())

    assert "copy_global_to(42)" in calls
    assert "sync(guild=42)" in calls
    # The global set is emptied and pushed, in that order and after the guild copy.
    assert calls.index("clear_commands(guild=None)") > calls.index("copy_global_to(42)")
    assert calls.index("sync(guild=None)") > calls.index("clear_commands(guild=None)")
