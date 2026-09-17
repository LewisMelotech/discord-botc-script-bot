"""The Minecraft commands that load a script on the server.

The server's datapack names its functions after each script's custom id, so the id is
the whole of what varies — and a script without one has no command to give. Every custom
id is already a valid function path: the fork only accepts lowercase letters, digits and
single hyphens, and Minecraft allows all of those.

The website builds the same two commands in the fork's ``scripts/minecraft.py``. Change
one and change the other, or the site and the bot will hand out different commands.
"""

from __future__ import annotations

from typing import Final

COMMAND_TEMPLATES: Final = (
    "/function botc_nw_lite:roles/{custom_id}",
    "/function botc_nw_lite:scripts/{custom_id}",
)


def commands_for(custom_id: str | None) -> tuple[str, ...]:
    """Both commands for a script, in order, or none when it has no custom id."""
    if not custom_id:
        return ()
    return tuple(template.format(custom_id=custom_id) for template in COMMAND_TEMPLATES)
