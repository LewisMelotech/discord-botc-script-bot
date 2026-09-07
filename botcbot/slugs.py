"""What a custom script id may look like, kept in step with the fork's ``scripts/slugs.py``.

A *slug* is an optional custom string id for a script — ``sects`` rather than ``13108``.
It is canonical in the same sense the numeric id is: both address exactly one script.
Upstream botc-scripts has no such field, so this only ever does anything against a
self-hosted fork that added one.

Only the **shape** rules live here, and they are the half that has to agree with the
server for resolution to be safe. The server keeps the other half — reserved words and
uniqueness — because both depend on state this bot does not have; those come back as an
HTTP 400 whose message is shown to the user as it was written.
"""

from __future__ import annotations

import re
from typing import Final

# ``Script.slug`` is a ``SlugField(max_length=50)`` on the fork, and two characters is
# the shortest id worth typing instead of a number.
MIN_SLUG_LENGTH: Final = 2
MAX_SLUG_LENGTH: Final = 50

# Lowercase ASCII letters and digits, separated by single hyphens: no leading or
# trailing hyphen, no ``--``, no underscores, nothing outside ASCII. One spelling per
# slug, and safe in both a URL path and a filename.
SLUG_PATTERN: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def normalise_slug(text: str) -> str:
    """Fold a person's typing into the single spelling the server stores.

    Mixed case is folded rather than refused, on both sides: ``SECTS`` and ``sects``
    are the same slug, so ``/alias set custom_id:SECTS`` is a success, not an error.
    """
    return text.strip().casefold()


def is_slug(text: str) -> bool:
    """Whether ``text`` is shaped like a slug, and so worth an exact lookup."""
    return slug_problem(text) is None


def slug_problem(text: str) -> str | None:
    """Why ``text`` cannot be a slug, or ``None`` if it can be.

    Called before ``/alias`` spends a request, so a typo is answered precisely and for
    free rather than as a bare HTTP 400 — and called again on the resolution path,
    where the integer clause below is what keeps slugs and script ids apart.
    """
    if not text:
        return "A custom id cannot be empty."

    # This clause is load-bearing, not cosmetic. Resolution checks for a numeric script
    # id first, so a slug that reads as a number would silently resolve to whichever
    # script happens to hold that id — a wrong answer rather than an error, and only
    # once the instance has enough scripts for the id to exist. ``int()`` rather than
    # ``str.isdigit()`` because ``int()`` is the wider net: it also accepts a sign,
    # surrounding whitespace, and underscores (``int("1_0")`` is 10).
    if _is_integer(text):
        return (
            f"`{text}` is a number, and numbers are reserved for script ids. "
            "Add a letter so it cannot be mistaken for one."
        )

    if len(text) < MIN_SLUG_LENGTH:
        return f"`{text}` is too short — a custom id needs at least {MIN_SLUG_LENGTH} characters."
    if len(text) > MAX_SLUG_LENGTH:
        return (
            f"That custom id is {len(text)} characters; "
            f"the limit is {MAX_SLUG_LENGTH}."
        )

    if not SLUG_PATTERN.match(text):
        return (
            f"`{text}` is not a valid custom id. Use lowercase letters and digits "
            "separated by single hyphens, like `sects-and-violets`."
        )
    return None


def _is_integer(text: str) -> bool:
    try:
        int(text)
    except ValueError:
        return False
    return True


__all__ = [
    "MAX_SLUG_LENGTH",
    "MIN_SLUG_LENGTH",
    "SLUG_PATTERN",
    "is_slug",
    "normalise_slug",
    "slug_problem",
]
