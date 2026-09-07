"""Entry point: python bot.py"""

from __future__ import annotations

import logging
import sys

from botcbot.tls import install_os_trust_store

# aiohttp builds and caches its default SSL context at import time, so the trust
# store has to be swapped in before discord.py (and therefore aiohttp) is imported.
install_os_trust_store()

import discord  # noqa: E402

from botcbot.config import Config, ConfigError  # noqa: E402
from botcbot.discord_app import ScriptBot  # noqa: E402


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    discord.utils.setup_logging(level=getattr(logging, config.log_level))

    bot = ScriptBot(config)
    try:
        bot.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        print("Discord rejected DISCORD_TOKEN. Check the value in your .env.", file=sys.stderr)
        return 1
    except discord.PrivilegedIntentsRequired:
        print(
            "Discord demanded privileged intents, which this bot never requests.",
            file=sys.stderr,
        )
        return 1
    except discord.Forbidden as exc:
        # Almost always the command sync in setup_hook: a guild id the bot is not in, or
        # an invite that omitted applications.commands.
        print(
            f"Discord refused the request: {exc}\n"
            "Check that DISCORD_GUILD_ID names a server this bot has been added to, and "
            "that it was invited with both the bot and applications.commands scopes.",
            file=sys.stderr,
        )
        return 1
    except discord.HTTPException as exc:
        print(f"Discord returned an error during startup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
