"""The Discord layer: three slash commands wired to the botc-scripts client.

``/script`` renders a script's PDF to page images; ``/json`` attaches its JSON. They
share query resolution, the interaction deadline, the error reporting and the
autocomplete callback, and differ only in what they build and who sees it by default.

``/alias`` is the odd one out: an administrators-only group that reads and writes a
script's custom id. It borrows the same discipline — defer first, fix visibility, bound
the work — but not ``_serve`` itself, which is built to deliver files.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Final, Literal

import aiohttp
import discord
from discord import app_commands

from .botcscripts import (
    AmbiguousScript,
    BotcScriptsClient,
    BotcScriptsError,
    InvalidVersion,
    NotPermitted,
    PdfUnavailable,
    ScriptNotFound,
    ScriptVersion,
    SlugRejected,
    UpstreamError,
    WriteNotConfigured,
    basic_auth,
    human_bytes,
)
from .cache import CachedScript, ScriptCache
from .config import Config
from .rendering import RenderError, RenderResult, rasterise
from .slugs import normalise_slug, slug_problem

_LOGGER = logging.getLogger(__name__)

# Discord refuses more than 10 attachments on a single message.
MAX_ATTACHMENTS_PER_MESSAGE: Final = 10

# Documented maximum size of a whole create-message request.
MAX_REQUEST_BYTES: Final = 25 * 1024 * 1024

# Headroom for multipart overhead on top of whatever limit Discord reports.
_SIZE_SAFETY: Final = 0.95

# Discord accepts at most 25 autocomplete choices, each with a 1-100 character name.
# discord.py checks neither: an over-long payload comes back as a 400 that the library
# swallows, so the only symptom is that suggestions silently stop appearing.
MAX_CHOICES: Final = 25
MAX_CHOICE_NAME: Final = 100

# Three separate buckets, deliberately. /script downloads and rasterises megabytes,
# which is the whole reason the limit exists; /json is a single cheap request, sometimes
# none at all, so throttling it as hard would only be annoying. /alias sits between
# them: a couple of small requests, but administrators-only and rarely run in bursts.
_SCRIPT_COOLDOWN_RATE: Final = 2
_SCRIPT_COOLDOWN_PER: Final = 15.0
_JSON_COOLDOWN_RATE: Final = 6
_JSON_COOLDOWN_PER: Final = 15.0
_ALIAS_COOLDOWN_RATE: Final = 4
_ALIAS_COOLDOWN_PER: Final = 30.0

# Discord invalidates a deferred interaction's token 15 minutes after it was created.
# Stop the slow work this far short of that so there is still a live token to upload the
# attachments with — or, failing that, to tell the user what went wrong.
_UPLOAD_HEADROOM: Final = 90.0

# Autocomplete cannot be deferred and Discord hard-fails it after 3 seconds, so both
# lookups get a small fraction of that and the answer degrades instead of arriving late.
_LIVE_SEARCH_BUDGET: Final = 1.2
_CACHE_READ_BUDGET: Final = 0.5

# Rasterising is memory-hungry and interactions are dispatched as independent tasks, so
# without this several large renders would allocate simultaneously across the thread pool.
_RENDER_CONCURRENCY: Final = 2


class ScriptBot(discord.Client):
    def __init__(self, config: Config) -> None:
        # A slash-command-only bot needs no gateway intents at all; interactions
        # arrive regardless and message_content is a privileged intent we never use.
        super().__init__(intents=discord.Intents.none())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None
        self.api: BotcScriptsClient | None = None
        self.cache = ScriptCache(config.cache_path, max_entries=config.cache_entries)
        # Per-instance rather than module-level: an asyncio primitive binds to whichever
        # loop first contends it, and a module-level one would outlive that loop.
        self.render_limit = asyncio.Semaphore(_RENDER_CONCURRENCY)

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.http_timeout)
        )
        self.api = BotcScriptsClient(
            self.session,
            base_url=self.config.base_url,
            max_pdf_bytes=self.config.max_pdf_bytes,
            auth=_api_auth(self.config),
            online_only=self.config.online_only,
        )

        self.tree.add_command(script_command)
        self.tree.add_command(json_command)
        # Registered whatever the instance is. /alias show reads custom ids from any
        # instance that has them, and an administrator whose credentials are missing or
        # wrong needs to be told which it is — a command absent from the picker explains
        # nothing, and looks like a broken bot rather than a setting.
        self.tree.add_command(alias_group)
        self.tree.error(_on_command_error)
        self._log_alias_mode()

        # Sync here rather than in on_ready, which fires again on every reconnect.
        if self.config.guild_id is not None:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            # A previous run without DISCORD_GUILD_ID registers the same commands
            # globally, and Discord keeps them until told otherwise — so every command
            # then appears twice in the picker. Clearing the global set after the guild
            # copy removes the duplicates and keeps them from coming back.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            _LOGGER.info(
                "Commands synced to guild %s (available immediately); "
                "any global registration from an earlier run has been cleared.",
                self.config.guild_id,
            )
        else:
            await self.tree.sync()
            _LOGGER.info("Commands synced globally (propagation can take up to an hour).")

    def _log_alias_mode(self) -> None:
        """Say at startup which half of /alias will work here, not at first use."""
        if self.config.is_public_site:
            _LOGGER.info(
                "%s is the public site, which has no custom ids: /alias will say so.",
                self.config.base_url,
            )
        elif not self.config.can_write:
            _LOGGER.info(
                "BOTC_API_USER/BOTC_API_PASSWORD are unset: /alias can show custom ids "
                "on %s but not change them.",
                self.config.base_url,
            )
        else:
            _LOGGER.info(
                "/alias will set custom ids on %s as %s.",
                self.config.base_url,
                self.config.api_user,
            )
            if self.config.credentials_are_cleartext:
                # Not fatal: http://botc-scripts:8000 over a Compose bridge is the
                # intended deployment. Worth saying once, in case it is not that.
                _LOGGER.warning(
                    "BOTC_BASE_URL is http://, so the API password crosses the network "
                    "unencrypted. Fine on a private network; use https:// off one."
                )

    async def on_ready(self) -> None:
        _LOGGER.info("Connected as %s, serving %s.", self.user, self.config.base_url)

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()
        # Closing a sqlite connection is a local, sub-millisecond call; the to_thread
        # discipline elsewhere is for the query path, not for shutdown.
        self.cache.close()
        await super().close()


def _api_auth(config: Config) -> str | None:
    """HTTP Basic credentials for the instance, or ``None`` to stay read-only.

    The fork gates writes on a Django user holding ``scripts.api_write_permission`` and
    accepts Basic auth; there is no token model to use instead.
    """
    if config.api_user is None or config.api_password is None:
        return None
    return basic_auth(config.api_user, config.api_password)


@dataclass(frozen=True, slots=True)
class _Delivery:
    """What one command produced: a message, and the files to hang off it."""

    header: str
    attachments: tuple[tuple[str, bytes], ...]


async def script_query_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest scripts for either command's ``query``.

    Nothing in here may raise. ``CommandTree`` catches and logs an autocomplete
    exception without responding at all, which leaves the client spinning until
    Discord's 3-second deadline lapses; an empty list is the same outcome, at once.
    """
    try:
        return await _suggestions(interaction, current)
    except Exception:
        _LOGGER.warning("Autocomplete failed for %r.", current, exc_info=True)
        return []


async def _suggestions(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    bot = interaction.client
    if not isinstance(bot, ScriptBot):
        return []

    text = current.strip()
    # Keyed by script id so a live result never repeats one already offered from cache.
    choices: dict[int, app_commands.Choice[str]] = {}

    for entry in await _cached_suggestions(bot.cache, text, interaction.guild_id):
        choices[entry.script_id] = _choice(entry.script_id, entry.name, entry.author, entry.slug)

    if text and bot.api is not None and len(choices) < MAX_CHOICES:
        for found in await _live_suggestions(bot.api, text):
            if found.script_id in choices:
                continue
            choices[found.script_id] = _choice(
                found.script_id, found.name, found.author, found.slug
            )
            if len(choices) >= MAX_CHOICES:
                break

    return list(choices.values())[:MAX_CHOICES]


async def _cached_suggestions(
    cache: ScriptCache, text: str, guild_id: int | None
) -> list[CachedScript]:
    if not cache.enabled:
        return []
    try:
        # The timeout abandons the thread rather than stopping it, which is the right
        # trade here: a slow disk must not cost the user their suggestions.
        async with asyncio.timeout(_CACHE_READ_BUDGET):
            return await asyncio.to_thread(
                cache.suggest, text, guild_id=guild_id, limit=MAX_CHOICES
            )
    except Exception as exc:
        # Broad on purpose: a corrupt or unreadable database must still leave the live
        # search below able to answer.
        _LOGGER.warning("Suggestion cache read failed: %s", exc)
        return []


async def _live_suggestions(api: BotcScriptsClient, text: str) -> list[ScriptVersion]:
    try:
        async with asyncio.timeout(_LIVE_SEARCH_BUDGET):
            return await api.search(text, limit=MAX_CHOICES)
    except (TimeoutError, BotcScriptsError) as exc:
        _LOGGER.debug("Live suggestions for %r fell back to the cache: %s", text, exc)
        return []


def _choice(
    script_id: int, name: str, author: str | None, slug: str | None = None
) -> app_commands.Choice[str]:
    """Label the script for a human; carry its canonical id as the value.

    Picking a suggestion submits the value, so sending an id means the command resolves
    it exactly rather than re-running the fuzzy name search. The custom id is that id
    where the script has one, and the numeric id everywhere else — both resolve exactly,
    so a mixed list needs no flag.

    The custom id is also shown in the label, ahead of the author, because autocomplete
    is the cheapest place for people to learn that a script has one. Each extra is
    appended only if the whole label still fits: over-long choices come back as a 400
    that discord.py swallows, which would silently stop all suggestions.
    """
    label = name.strip() or f"Script {script_id}"
    for extra in (f" ({slug})" if slug else "", f" — {author}" if author else ""):
        if extra and len(label) + len(extra) <= MAX_CHOICE_NAME:
            label += extra
    return app_commands.Choice(name=label[:MAX_CHOICE_NAME], value=slug or str(script_id))


@app_commands.command(
    name="script",
    description="Post a Blood on the Clocktower script's pages as images",
)
@app_commands.describe(
    query="Script name, custom id, or numeric script id",
    version="Optional version such as 1.0.0 (defaults to the latest)",
    output="Who sees the reply. Defaults to public.",
)
@app_commands.autocomplete(query=script_query_autocomplete)
@app_commands.checks.cooldown(_SCRIPT_COOLDOWN_RATE, _SCRIPT_COOLDOWN_PER, key=lambda i: i.user.id)
async def script_command(
    interaction: discord.Interaction,
    query: str,
    version: str | None = None,
    output: Literal["public", "private"] = "public",
) -> None:
    await _serve(
        interaction,
        query=query,
        version=version,
        ephemeral=output == "private",
        build=_pdf_delivery,
    )


# A fresh @autocomplete call per command, never a hoisted decorator object: discord.py
# pops the parameters out of the dict it is handed, so reusing one would silently attach
# the callback to the first command only.
@app_commands.command(
    name="json",
    description="Post a Blood on the Clocktower script's JSON file",
)
@app_commands.describe(
    query="Script name, custom id, or numeric script id",
    version="Optional version such as 1.0.0 (defaults to the latest)",
    output="Who sees the reply. Defaults to private.",
)
@app_commands.autocomplete(query=script_query_autocomplete)
@app_commands.checks.cooldown(_JSON_COOLDOWN_RATE, _JSON_COOLDOWN_PER, key=lambda i: i.user.id)
async def json_command(
    interaction: discord.Interaction,
    query: str,
    version: str | None = None,
    output: Literal["public", "private"] = "private",
) -> None:
    await _serve(
        interaction,
        query=query,
        version=version,
        ephemeral=output == "private",
        build=_json_delivery,
    )


# Two decorators doing two different jobs, and both are needed. default_permissions()
# with no arguments registers the command with default_member_permissions=0, so Discord
# hides it from everyone but administrators — that is a server-side hint, not
# enforcement: a server admin can re-grant it to any role under Integrations, and no
# error handler is called for it. checks.has_permissions on each subcommand is the real
# gate, evaluated locally on every invocation. Both of these must sit on the Group
# object: Discord ignores guild_only and default_permissions on subcommands.
alias_group = app_commands.Group(
    name="alias",
    description="Manage a script's custom id (needs a self-hosted botc-scripts)",
)
app_commands.guild_only()(alias_group)
app_commands.default_permissions()(alias_group)


@alias_group.command(name="set", description="Give a script a custom id")
@app_commands.describe(
    query="Script name, current custom id, or numeric script id",
    custom_id="Lowercase letters, digits, single hyphens — never a number. e.g. sects",
)
@app_commands.autocomplete(query=script_query_autocomplete)
@app_commands.checks.cooldown(_ALIAS_COOLDOWN_RATE, _ALIAS_COOLDOWN_PER, key=lambda i: i.user.id)
# Listed last so it is checked first — the checks run in the order they were applied, and
# a refused non-administrator should not also spend a token from the bucket above.
@app_commands.checks.has_permissions(administrator=True)
async def alias_set(interaction: discord.Interaction, query: str, custom_id: str) -> None:
    wanted = normalise_slug(custom_id)
    await _serve_alias(
        interaction,
        query=query,
        writing=True,
        # Shape is checked here, before a single request is spent, so a typo is answered
        # precisely and for free. Everything the server alone knows — reserved words and
        # whether the id is taken — comes back from it as an HTTP 400 instead.
        refusal=slug_problem(wanted),
        act=partial(_apply_custom_id, custom_id=wanted),
    )


# A fresh @autocomplete call per subcommand, for the reason given above json_command.
@alias_group.command(name="clear", description="Remove a script's custom id")
@app_commands.describe(query="Script name, custom id, or numeric script id")
@app_commands.autocomplete(query=script_query_autocomplete)
@app_commands.checks.cooldown(_ALIAS_COOLDOWN_RATE, _ALIAS_COOLDOWN_PER, key=lambda i: i.user.id)
@app_commands.checks.has_permissions(administrator=True)
async def alias_clear(interaction: discord.Interaction, query: str) -> None:
    await _serve_alias(
        interaction,
        query=query,
        writing=True,
        act=partial(_apply_custom_id, custom_id=None),
    )


@alias_group.command(name="show", description="Show a script's custom id")
@app_commands.describe(query="Script name, custom id, or numeric script id")
@app_commands.autocomplete(query=script_query_autocomplete)
@app_commands.checks.cooldown(_ALIAS_COOLDOWN_RATE, _ALIAS_COOLDOWN_PER, key=lambda i: i.user.id)
@app_commands.checks.has_permissions(administrator=True)
async def alias_show(interaction: discord.Interaction, query: str) -> None:
    # Reading needs no credentials, so this one works on any instance that has the
    # feature — including a self-hosted one this bot may only read.
    await _serve_alias(interaction, query=query, writing=False, act=_report_custom_id)


async def _apply_custom_id(bot: ScriptBot, script: ScriptVersion, *, custom_id: str | None) -> str:
    """Set or clear the custom id, and say what changed.

    The clear is sent even when the resolved script appears to have no custom id: the
    instance is the authority on that, and the write is idempotent either way.
    """
    info = await bot.api.set_slug(script.script_id, custom_id)
    name = discord.utils.escape_markdown(info.name)

    if info.slug is None:
        had = f" It was `{script.slug}`." if script.slug else " It had none."
        return f"**{name}** (id `{info.script_id}`) now has no custom id.{had}"

    replaced = f", replacing `{script.slug}`" if script.slug and script.slug != info.slug else ""
    return (
        f"**{name}** (id `{info.script_id}`) is now `{info.slug}`{replaced}.\n"
        f"Anyone can use it wherever a script id works: `/script query:{info.slug}`"
    )


async def _report_custom_id(bot: ScriptBot, script: ScriptVersion) -> str:
    """Read-only: report the custom id the resolved script already carries."""
    name = discord.utils.escape_markdown(script.name)
    if script.slug:
        return (
            f"**{name}** (id `{script.script_id}`) has the custom id `{script.slug}`.\n"
            f"Use it wherever a script id works: `/script query:{script.slug}`"
        )
    return (
        f"**{name}** has no custom id, so it is only addressable as `{script.script_id}`.\n"
        f"Give it one with `/alias set query:{script.script_id} custom_id:…`"
    )


async def _serve_alias(
    interaction: discord.Interaction,
    *,
    query: str,
    writing: bool,
    act: Callable[[ScriptBot, ScriptVersion], Awaitable[str]],
    refusal: str | None = None,
) -> None:
    """``_serve``'s discipline for a command that produces one line and no files.

    Deliberately not ``_serve`` itself, which exists to build attachments, chunk them
    across messages and record the delivery — none of which applies to a custom id.
    """
    # Always private, with no output parameter to choose otherwise. These replies name
    # the instance's host, the bot's API user and other scripts' ids; and a permission
    # refusal fires before this runs, where _on_command_error has no recorded choice and
    # defaults to private — a public /alias would answer non-administrators privately and
    # administrators publicly, the one asymmetry extras["ephemeral"] exists to prevent.
    interaction.extras["ephemeral"] = True
    await interaction.response.defer(thinking=True, ephemeral=True)
    reply = _Reply(interaction, ephemeral=True)

    bot = interaction.client
    if not isinstance(bot, ScriptBot) or bot.api is None:
        await reply.send(content="The bot is still starting up. Try again shortly.")
        return

    # Both of these are answered before anything is resolved, so a misconfigured bot and
    # a mistyped custom id each cost zero requests.
    stopped = _alias_unavailable(bot.config, writing=writing) or refusal
    if stopped is not None:
        await reply.send(content=stopped)
        return

    command = _command_name(interaction)
    try:
        async with asyncio.timeout(_work_budget(interaction)):
            script = await bot.api.resolve(query)
            message = await act(bot, script)
    except ScriptNotFound as exc:
        message = _not_found_message(exc)
    except AmbiguousScript as exc:
        message = _ambiguous_message(exc, command)
    except SlugRejected as exc:
        # The instance owns the rules this bot cannot check — reserved words, and whether
        # the id is already taken — and words the refusal itself. Pass its sentence on.
        message = f"{bot.config.base_url} would not accept that custom id: {exc}"
    except (NotPermitted, WriteNotConfigured) as exc:
        _LOGGER.warning("Custom id write refused by %s: %s", bot.config.base_url, exc)
        message = _credentials_message(bot.config, exc)
    except UpstreamError as exc:
        _LOGGER.warning("Upstream failure in /%s for %r: %s", command, query, exc)
        message = f"Could not reach the script library: {exc}"
    except BotcScriptsError as exc:
        _LOGGER.warning("Could not complete /%s for %r: %s", command, query, exc)
        message = f"Could not do that: {exc}"
    except TimeoutError:
        _LOGGER.warning("Gave up on /%s for %r before the token expired.", command, query)
        message = (
            f"Gave up waiting on {bot.config.base_url} — it did not answer in time. "
            "Try again in a moment."
        )

    await reply.send(content=message)


def _alias_unavailable(config: Config, *, writing: bool) -> str | None:
    """Why /alias cannot work against this instance, or ``None`` if it can."""
    if config.is_public_site:
        return (
            "Custom ids are a self-hosted botc-scripts feature, and this bot is pointed "
            f"at <{config.base_url}>, which does not have them.\n"
            "Point `BOTC_BASE_URL` at your own instance to use `/alias`. `/script` and "
            "`/json` are unaffected."
        )
    if writing and not config.can_write:
        return _no_credentials_message(config)
    return None


def _no_credentials_message(config: Config) -> str:
    return (
        f"This bot has no API credentials for <{config.base_url}>, so it can show custom "
        "ids but not change them.\n"
        "Set `BOTC_API_USER` and `BOTC_API_PASSWORD` to a user on that instance holding "
        "the `scripts.api_write_permission` permission, then restart the bot."
    )


def _credentials_message(config: Config, exc: BotcScriptsError) -> str:
    if isinstance(exc, WriteNotConfigured) or not config.can_write:
        return _no_credentials_message(config)
    return (
        f"<{config.base_url}> refused this bot's API credentials: {exc}\n"
        f"Check that `BOTC_API_USER` (currently `{config.api_user}`) and "
        "`BOTC_API_PASSWORD` name a user on that instance holding the "
        "`scripts.api_write_permission` permission, then restart the bot."
    )


async def _serve(
    interaction: discord.Interaction,
    *,
    query: str,
    version: str | None,
    ephemeral: bool,
    build: Callable[[ScriptBot, discord.Interaction, ScriptVersion], Awaitable[_Delivery]],
) -> None:
    """Everything the two commands share: visibility, deadline, errors and delivery."""
    # Read back by _on_command_error, which otherwise cannot tell whether this
    # invocation was deferred publicly or privately.
    interaction.extras["ephemeral"] = ephemeral

    # Must be the first await: Discord invalidates the token after 3 seconds without an
    # initial response. It is also the only moment at which visibility can be chosen —
    # an existing message's ephemeral state can never be changed afterwards.
    await interaction.response.defer(thinking=True, ephemeral=ephemeral)
    reply = _Reply(interaction, ephemeral=ephemeral)

    bot = interaction.client
    if not isinstance(bot, ScriptBot) or bot.api is None:
        await reply.send(content="The bot is still starting up. Try again shortly.")
        return

    try:
        # Every upstream request has its own timeout, but half a dozen of them plus a
        # render can still outlive the interaction token. Bound the lot of it.
        async with asyncio.timeout(_work_budget(interaction)):
            script = await bot.api.resolve(query, version)
            delivery = await build(bot, interaction, script)
    except ScriptNotFound as exc:
        await reply.send(content=_not_found_message(exc))
        return
    except AmbiguousScript as exc:
        await reply.send(content=_ambiguous_message(exc, _command_name(interaction)))
        return
    except InvalidVersion as exc:
        await reply.send(content=str(exc))
        return
    except UpstreamError as exc:
        _LOGGER.warning("Upstream failure serving %r: %s", query, exc)
        await reply.send(content=f"Could not reach the script library: {exc}")
        return
    except BotcScriptsError as exc:
        # /json lets a failed JSON fetch propagate rather than delivering nothing
        # silently, so the base class needs an arm of its own after the specific ones.
        _LOGGER.warning("Could not serve %r: %s", query, exc)
        await reply.send(content=f"Could not fetch that script: {exc}")
        return
    except TimeoutError:
        _LOGGER.warning("Gave up on %r before the interaction token expired.", query)
        await reply.send(
            content=(
                f"Gave up waiting on {bot.config.base_url} — it did not answer in time. "
                "Try again in a moment."
            )
        )
        return

    if not await _deliver(interaction, reply, delivery):
        return
    await _remember(bot, interaction, script)


async def _deliver(interaction: discord.Interaction, reply: _Reply, delivery: _Delivery) -> bool:
    """Send the header and every attachment, ten files at a time."""
    chunks = [
        delivery.attachments[start : start + MAX_ATTACHMENTS_PER_MESSAGE]
        for start in range(0, len(delivery.attachments), MAX_ATTACHMENTS_PER_MESSAGE)
    ] or [()]

    content: str | None = delivery.header
    for chunk in chunks:
        if interaction.is_expired():
            _LOGGER.warning("Interaction expired part-way through delivering the reply.")
            return False
        await reply.send(content=content, files=chunk)
        content = None
    return True


class _Reply:
    """Sends every message of one interaction at a single, fixed visibility.

    Two Discord rules make this worth centralising. The first followup after a deferred
    response is not a new message at all — Discord redirects it into an edit of the
    "thinking" placeholder and ignores whatever ephemeral flag it carried — so it is
    done here as the edit it really is, which is also the call Discord has not
    deprecated. Every later followup, by contrast, starts from ``ephemeral=False`` and
    inherits nothing, so the flag has to be passed again each time or pages 2+ of a
    private ``/script`` would land in the channel for everyone.
    """

    def __init__(self, interaction: discord.Interaction, *, ephemeral: bool) -> None:
        self._interaction = interaction
        self._ephemeral = ephemeral
        self._placeholder_used = False

    async def send(
        self, *, content: str | None = None, files: Sequence[tuple[str, bytes]] = ()
    ) -> None:
        body = discord.utils.MISSING if content is None else content
        if not self._placeholder_used:
            self._placeholder_used = True
            await self._interaction.edit_original_response(
                content=body, attachments=_as_files(files)
            )
            return
        await self._interaction.followup.send(
            content=body, files=_as_files(files), ephemeral=self._ephemeral
        )


async def _pdf_delivery(
    bot: ScriptBot, interaction: discord.Interaction, script: ScriptVersion
) -> _Delivery:
    """Download the PDF and rasterise it. A missing PDF is reported, not raised."""
    per_file_budget, request_budget = _upload_budgets(interaction)

    render: RenderResult | None = None
    problem: str | None = None
    try:
        pdf_bytes = await bot.api.fetch_pdf(script)
        async with bot.render_limit:
            render = await asyncio.to_thread(
                rasterise,
                pdf_bytes,
                dpi=bot.config.render_dpi,
                max_pages=bot.config.max_pages,
                per_file_budget=min(per_file_budget, request_budget),
                total_budget=request_budget,
            )
    except (PdfUnavailable, RenderError) as exc:
        problem = str(exc)
    except UpstreamError as exc:
        _LOGGER.warning("Upstream failure fetching the PDF for %s: %s", script.label, exc)
        problem = str(exc)

    if render is None:
        return _Delivery(_no_pages_message(script, bot.config.base_url, problem), ())

    return _Delivery(
        _pdf_header(script, bot.config.base_url, render, bot.config.max_pages),
        tuple((page.filename, page.data) for page in render.pages),
    )


async def _json_delivery(
    bot: ScriptBot, interaction: discord.Interaction, script: ScriptVersion
) -> _Delivery:
    """One request at most — usually none, since the search row inlines the JSON."""
    json_bytes = await bot.api.fetch_script_json(script)

    lines = _title_lines(script, bot.config.base_url)
    per_file_budget, _ = _upload_budgets(interaction)
    if len(json_bytes) > per_file_budget:
        lines.append(
            f"-# The JSON is {human_bytes(len(json_bytes))}, over the "
            f"{human_bytes(per_file_budget)} this bot may upload here. "
            "Download it from the page above instead."
        )
        return _Delivery("\n".join(lines), ())

    return _Delivery("\n".join(lines), ((script.json_filename, json_bytes),))


async def _remember(
    bot: ScriptBot, interaction: discord.Interaction, script: ScriptVersion
) -> None:
    """Record a delivered script for autocomplete. Never fatal: the user already has it."""
    if not bot.cache.enabled:
        return
    try:
        await asyncio.to_thread(
            bot.cache.record,
            script_id=script.script_id,
            name=script.name,
            author=script.author,
            guild_id=interaction.guild_id,
            # Stored, not just used live: the cache is what answers when the instance is
            # slow, and a cached row with no custom id would offer the numeric one while
            # live rows offered custom ids — inconsistent exactly when it matters most.
            slug=script.slug,
        )
    except Exception:
        _LOGGER.warning("Could not record %s in the suggestion cache.", script.label, exc_info=True)


def _work_budget(interaction: discord.Interaction) -> float:
    """Seconds of the interaction token's remaining life to spend on the slow work."""
    remaining = (interaction.expires_at - discord.utils.utcnow()).total_seconds()
    return max(1.0, remaining - _UPLOAD_HEADROOM)


def _as_files(items: Sequence[tuple[str, bytes]]) -> list[discord.File]:
    """Build fresh File objects per send: discord.py closes them after each request."""
    return [discord.File(io.BytesIO(data), filename=name) for name, data in items]


def _upload_budgets(interaction: discord.Interaction) -> tuple[int, int]:
    """Per-file and per-request byte budgets.

    ``Interaction.filesize_limit`` comes off the wire already resolved for the
    guild's boost tier and the caller's Nitro status. ``Guild.filesize_limit`` is
    computed from a table in discord.py that is stale, so it is not used here.
    """
    reported = int(getattr(interaction, "filesize_limit", 0) or 0) or 10 * 1024 * 1024
    per_file = int(reported * _SIZE_SAFETY)
    per_request = int(min(MAX_REQUEST_BYTES, reported * MAX_ATTACHMENTS_PER_MESSAGE) * _SIZE_SAFETY)
    return per_file, per_request


def _title_lines(script: ScriptVersion, base_url: str) -> list[str]:
    details = [f"v{script.version}"]
    if script.script_type:
        details.append(script.script_type)
    if script.author:
        details.append(f"by {script.author}")
    return [
        f"**{discord.utils.escape_markdown(script.name)}** — {' · '.join(details)}",
        f"<{script.web_url(base_url)}>",
    ]


def _pdf_header(script: ScriptVersion, base_url: str, render: RenderResult, max_pages: int) -> str:
    lines = _title_lines(script, base_url)
    if render.omitted_pages:
        # Pages are only ever dropped for one of two reasons: the upload budget ran out
        # mid-render, or the page cap stopped the loop. Discord's attachment limit is not
        # one of them — over ten attachments are split across a second message instead.
        why = (
            "the rest would exceed Discord's upload limit"
            if render.size_limited
            else f"this bot is set to render at most {max_pages} pages"
        )
        pdf_url = f"{base_url}/script/{script.script_id}/{script.version}/download_pdf"
        lines.append(
            f"-# Showing the first {render.rendered_pages} of {render.total_pages} pages — {why}. "
            f"The full PDF is at <{pdf_url}>"
        )
    return "\n".join(lines)


def _no_pages_message(script: ScriptVersion, base_url: str, problem: str | None) -> str:
    lines = _title_lines(script, base_url)
    lines.append(f"-# No pages to show: {problem or 'the PDF could not be rendered'}.")
    lines.append(
        f"-# The script's JSON is unaffected — run `/json query:{script.script_id}` for it."
    )
    return "\n".join(lines)


def _not_found_message(exc: ScriptNotFound) -> str:
    lines = [str(exc)]
    if exc.suggestions:
        lines.append("Did you mean one of these?")
        lines.extend(_candidate_lines(exc.suggestions))
    else:
        lines.append("Check the spelling, or pass the numeric script id instead.")
    return "\n".join(lines)


def _ambiguous_message(exc: AmbiguousScript, command_name: str) -> str:
    lines = [
        (
            f"Several scripts match **{discord.utils.escape_markdown(exc.query)}**. "
            f"Re-run `/{command_name}` with one of these ids:"
        ),
    ]
    lines.extend(_candidate_lines(exc.candidates))
    return "\n".join(lines)


def _candidate_lines(candidates: list[ScriptVersion]) -> list[str]:
    lines = []
    for candidate in candidates:
        details = [f"v{candidate.version}"]
        if candidate.author:
            details.append(f"by {candidate.author}")
        name = discord.utils.escape_markdown(candidate.name)
        lines.append(f"• `{candidate.script_id}` — **{name}** ({', '.join(details)})")
    return lines


def _command_name(interaction: discord.Interaction) -> str:
    """``script``, ``json`` or ``alias set`` — the qualified name a user would retype."""
    command = interaction.command
    return getattr(command, "qualified_name", None) or getattr(command, "name", "script")


async def _on_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if interaction.type is discord.InteractionType.autocomplete:
        # An autocomplete interaction can only be answered with choices, and the two
        # paths that reach here have already failed to produce any. Anything sent below
        # would raise instead.
        _LOGGER.warning("Autocomplete error on /%s: %s", _command_name(interaction), error)
        return

    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"Slow down a moment — try again in {error.retry_after:.0f}s."
    elif isinstance(error, app_commands.MissingPermissions):
        # An ordinary, expected refusal — not a fault. Logging a traceback for it would
        # fill the log every time a non-administrator pokes /alias, and telling them to
        # "try again" would be advice for something that can never succeed.
        # This arm must stay below the cooldown one: CommandOnCooldown is itself a
        # CheckFailure, so a broader arm placed first would swallow its message.
        _LOGGER.info("Refused /%s: %s", _command_name(interaction), error)
        message = "`/alias` is for server administrators only."
    else:
        _LOGGER.exception("Unhandled error in /%s", _command_name(interaction), exc_info=error)
        message = "Something went wrong handling that command. Please try again."

    if interaction.is_expired():
        # Every send below would 404 on a dead token; say so rather than logging it as a
        # generic delivery failure.
        _LOGGER.warning(
            "Interaction for /%s expired before its error could be delivered.",
            _command_name(interaction),
        )
        return

    # Match the visibility the invocation chose. A private error on a public /script
    # would leave the public "thinking..." placeholder in the channel forever, and a
    # public one on a private /json would expose what the user asked for. A failure
    # before the defer — a cooldown, for one — has no choice recorded and stays private.
    ephemeral = bool(interaction.extras.get("ephemeral", True))

    # Never leave the user staring at a permanent "thinking..." state.
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(message, ephemeral=ephemeral)
    except discord.HTTPException:
        _LOGGER.warning("Could not deliver the error message to the user.")


__all__ = [
    "ScriptBot",
    "alias_group",
    "json_command",
    "script_command",
    "script_query_autocomplete",
]
