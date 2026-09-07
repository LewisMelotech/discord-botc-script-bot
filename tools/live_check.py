"""End-to-end check of the botc-scripts client against a real instance.

Needs no Discord token: it exercises everything except the Discord API call. The two
commands' paths are checked separately, the way the bot runs them — ``/json`` stops
after the JSON and never touches the PDF — plus the search that backs autocomplete.

    python tools/live_check.py                     # a few well-known scripts
    python tools/live_check.py "Sects and Violets"
    BOTC_BASE_URL=https://scripts.example.com python tools/live_check.py 13108

Pass a custom id as a query to prove one resolves through the same path the bot uses:
``resolve`` → the slug route → the version rebuilt against the configured base. Every
query's ``custom_id=`` line is that assertion — a serializer that sends the field on the
list route but not the detail one shows up here as ``custom_id=None`` and nowhere else.

Read-only throughout, deliberately: it never writes, so it cannot change your instance.
``/alias`` is the way to exercise the write half.

    docker compose run --rm -e BOTC_BASE_URL=http://botc-scripts:8000 bot \\
        python tools/live_check.py sects 13108
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botcbot.tls import install_os_trust_store

# Must precede the aiohttp import; see botcbot/tls.py.
_OS_TRUST = install_os_trust_store()

import aiohttp  # noqa: E402

from botcbot.botcscripts import (  # noqa: E402
    USER_AGENT,
    AmbiguousScript,
    BotcScriptsClient,
    PdfUnavailable,
    ScriptNotFound,
    UpstreamError,
)
from botcbot.rendering import RenderError, rasterise  # noqa: E402

BASE_URL = os.environ.get("BOTC_BASE_URL", "https://www.botcscripts.com").rstrip("/")
# A name, a name, a real numeric id, and a miss. "13108" is the one that shows custom ids
# and script ids cannot collide: it can only ever be looked up as an id, because a custom
# id that reads as a number is refused when it is set.
DEFAULT_QUERIES = ["No Greater Joy", "Sects and Violets", "13108", "zzzznotascriptzzzz"]

# What a half-typed name looks like when Discord asks for autocomplete suggestions.
AUTOCOMPLETE_PREFIXES = ["sect", "no great", "zzzqqq"]


async def check(client: BotcScriptsClient, query: str) -> None:
    print(f"\n=== {query!r} " + "=" * (60 - len(repr(query))))
    try:
        script = await client.resolve(query)
    except UpstreamError as exc:
        print(f"  UPSTREAM ERROR: {exc}")
        return
    except AmbiguousScript as exc:
        print(f"  AMBIGUOUS ({len(exc.candidates)} candidates):")
        for candidate in exc.candidates[:5]:
            print(f"    id={candidate.script_id} {candidate.name!r} v{candidate.version}")
        return
    except ScriptNotFound as exc:
        print(f"  NOT FOUND: {exc}")
        for candidate in exc.suggestions[:5]:
            print(f"    suggestion: id={candidate.script_id} {candidate.name!r}")
        return

    print(f"  resolved: {script.name!r} v{script.version} "
          f"script_id={script.script_id} version_pk={script.version_pk} "
          f"type={script.script_type} author={script.author}")
    # None everywhere against the public site, which has no such field.
    print(f"  custom id: {script.slug!r} (the bot links and labels it as "
          f"{script.reference!r})")
    print(f"  page:     {script.web_url(BASE_URL)}")

    # The /json path. It stops here: no PDF is fetched and nothing is rasterised.
    payload = await client.fetch_script_json(script)
    print(f"  /json:    {len(payload):,} bytes -> {script.json_filename}")
    print(f"            starts {payload[:60].decode('utf-8', 'replace')!r}")

    # The /script path, from here down.
    try:
        pdf = await client.fetch_pdf(script)
    except PdfUnavailable as exc:
        print(f"  /script:  no pages ({exc}) — the bot points the user at /json instead")
        return

    print(f"  PDF:      {len(pdf):,} bytes, %PDF magic={pdf[:5] == b'%PDF-'}")

    try:
        result = await asyncio.to_thread(
            rasterise,
            pdf,
            dpi=150,
            max_pages=10,
            per_file_budget=int(10 * 1024 * 1024 * 0.95),
            total_budget=int(25 * 1024 * 1024 * 0.95),
        )
    except RenderError as exc:
        print(f"  RENDER:   failed ({exc})")
        return

    print(f"  RENDER:   {result.rendered_pages}/{result.total_pages} pages, "
          f"{result.total_bytes:,} bytes total, size_limited={result.size_limited}")
    for page in result.pages:
        magic = "PNG" if page.data[:8] == b"\x89PNG\r\n\x1a\n" else (
            "JPEG" if page.data[:2] == b"\xff\xd8" else "???"
        )
        print(f"            {page.filename}: {len(page.data):,} bytes, magic={magic}")


async def check_autocomplete(client: BotcScriptsClient, prefix: str) -> None:
    """The live half of the autocomplete callback, which has a 3-second hard deadline."""
    started = time.perf_counter()
    try:
        found = await client.search(prefix, limit=25)
    except UpstreamError as exc:
        print(f"  {prefix!r:<12} UPSTREAM ERROR: {exc}")
        return
    elapsed = (time.perf_counter() - started) * 1000
    # #id or #custom-id: what picking that suggestion would actually submit.
    head = ", ".join(f"{s.name!r}#{s.reference}" for s in found[:3])
    print(f"  {prefix!r:<12} {len(found):>2} suggestions in {elapsed:7.1f} ms   {head}")


async def main() -> int:
    queries = sys.argv[1:] or DEFAULT_QUERIES
    print(f"base_url:   {BASE_URL}")
    print(f"user-agent: {USER_AGENT}")
    print(f"os trust:   {_OS_TRUST}")
    # Reported, never used: this check makes no writes, so it needs no credentials.
    configured = bool(os.environ.get("BOTC_API_USER") and os.environ.get("BOTC_API_PASSWORD"))
    print(f"api creds:  {'configured' if configured else 'unset'} "
          f"(only /alias uses them; this check is read-only)")
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = BotcScriptsClient(session, base_url=BASE_URL)
        for query in queries:
            await check(client, query)

        print("\n=== autocomplete search " + "=" * 44)
        for prefix in AUTOCOMPLETE_PREFIXES:
            await check_autocomplete(client, prefix)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
