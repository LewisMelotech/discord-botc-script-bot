from __future__ import annotations

import json
import re

import aiohttp
import pytest
from conftest import FakeSession, page, script_detail, version_row

from botcbot.botcscripts import (
    AmbiguousScript,
    BotcScriptsClient,
    InvalidVersion,
    NotPermitted,
    PdfUnavailable,
    ScriptNotFound,
    ScriptVersion,
    SlugRejected,
    UpstreamError,
    WriteNotConfigured,
    safe_filename,
)

BASE = "https://example.test"

PDF_BYTES = b"%PDF-1.7\n% fake but correctly magic-numbered\n"

AUTH = aiohttp.encode_basic_auth("discordbot", "botpass")

SECTS_ROUTES = {
    "/api/script_ids/slug/sects/": script_detail(
        pk=13108, name="Sects and Violets", version_pk=22755, slug="sects"
    ),
    "/api/scripts/22755/": version_row(
        pk=22755, script_id=13108, name="Sects and Violets", slug="sects"
    ),
}


def client(routes: dict) -> tuple[BotcScriptsClient, FakeSession]:
    session = FakeSession(routes)
    return BotcScriptsClient(session, base_url=BASE), session


def writing_client(routes: dict) -> tuple[BotcScriptsClient, FakeSession]:
    """A client holding API credentials, i.e. one pointed at a self-hosted fork."""
    session = FakeSession(routes)
    return BotcScriptsClient(session, base_url=BASE, auth=AUTH), session


def test_from_api_reads_the_documented_field_names():
    script = ScriptVersion.from_api(
        version_row(pk=22755, script_id=13108, name="Let the Dead Rest in Peace")
    )
    assert (script.version_pk, script.script_id) == (22755, 13108)
    assert script.json_filename == "Let_the_Dead_Rest_in_Peace_1_0_0.json"
    assert script.web_url(BASE) == f"{BASE}/script/13108/1.0.0"


def test_from_api_rejects_a_payload_missing_required_keys():
    with pytest.raises(UpstreamError):
        ScriptVersion.from_api({"pk": 1, "name": "x"})


def test_from_api_reads_the_custom_id_and_tolerates_its_absence():
    # The fork sends null when a script has no custom id; the public site, which has no
    # such field, omits the key entirely. Both must read back as None.
    row = version_row(pk=1, script_id=2, name="Two Words")
    named = ScriptVersion.from_api(version_row(pk=1, script_id=2, name="Two Words", slug="sects"))
    absent = ScriptVersion.from_api({k: v for k, v in row.items() if k != "slug"})

    assert (named.slug, ScriptVersion.from_api(row).slug, absent.slug) == ("sects", None, None)


def test_the_reference_and_the_page_link_prefer_the_custom_id_over_the_number():
    plain = ScriptVersion.from_api(version_row(pk=1, script_id=13108, name="Two Words"))
    named = ScriptVersion.from_api(
        version_row(pk=1, script_id=13108, name="Two Words", slug="sects")
    )

    assert (plain.reference, named.reference) == ("13108", "sects")
    assert named.web_url(BASE) == f"{BASE}/script/sects/1.0.0"
    assert plain.web_url(BASE) == f"{BASE}/script/13108/1.0.0"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sects and Violets", "Sects_and_Violets"),
        ("../../etc/passwd", "etcpasswd"),
        ("Ω script: v2!", "script_v2"),
        ("!!!", ""),
    ],
)
def test_safe_filename_strips_path_and_shell_characters(raw, expected):
    assert safe_filename(raw) == expected


@pytest.mark.asyncio
async def test_exact_name_wins_over_higher_scoring_fuzzy_matches():
    rows = [
        version_row(pk=1, script_id=10, name="yes greater joy ?.0"),
        version_row(pk=2, script_id=77, name="No Greater Joy"),
        version_row(pk=3, script_id=12, name="No Greater Joy Remix"),
    ]
    api, _ = client({"/api/scripts/": page(rows)})
    assert (await api.resolve("no greater JOY")).script_id == 77


@pytest.mark.asyncio
async def test_several_fuzzy_matches_and_no_exact_one_is_ambiguous():
    rows = [
        version_row(pk=1, script_id=135, name="Bad Moon Rising"),
        version_row(pk=2, script_id=4665, name="Bad Moon Boffins"),
    ]
    api, _ = client({"/api/scripts/": page(rows)})
    with pytest.raises(AmbiguousScript) as excinfo:
        await api.resolve("Bad Moon")
    assert [c.script_id for c in excinfo.value.candidates] == [135, 4665]


@pytest.mark.asyncio
async def test_a_single_fuzzy_match_resolves_without_an_exact_name():
    api, _ = client({"/api/scripts/": page([version_row(pk=1, script_id=9, name="Catfishing")])})
    assert (await api.resolve("catfshing")).script_id == 9


@pytest.mark.asyncio
async def test_no_match_falls_back_to_the_unbounded_search_for_suggestions():
    api, session = client({"/api/scripts/": page([])})
    with pytest.raises(ScriptNotFound):
        await api.resolve("zzzz")
    # "zzzz" is shaped like a custom id, so the exact lookup is spent (and 404s) first.
    searches = [request for request in session.requests if request.startswith("/api/scripts/")]
    # Tight (ordered) search first, then the loose one that supplies suggestions.
    assert "ordering=-score" in searches[0]
    assert "ordering" not in searches[1]


@pytest.mark.asyncio
async def test_search_asks_for_homebrew_and_hybrid_which_are_excluded_by_default():
    api, session = client(
        {"/api/scripts/": page([version_row(pk=1, script_id=9, name="Two Words")])}
    )
    await api.resolve("Two Words")
    assert "include_homebrew=true" in session.requests[0]
    assert "include_hybrid=true" in session.requests[0]


@pytest.mark.asyncio
async def test_a_numeric_query_is_looked_up_as_a_script_id():
    api, _ = client(
        {
            "/api/script_ids/13108/": {
                "pk": 13108,
                "name": "Let the Dead Rest in Peace",
                "versions": {"1.0.0": f"{BASE}/api/scripts/22755/"},
                "latest_version": f"{BASE}/api/scripts/22755/",
            },
            "/api/scripts/22755/": version_row(
                pk=22755, script_id=13108, name="Let the Dead Rest in Peace"
            ),
        }
    )
    assert (await api.resolve("13108")).version_pk == 22755


@pytest.mark.asyncio
async def test_a_numeric_query_falls_back_to_name_search_when_no_such_id_exists():
    api, _ = client({"/api/scripts/": page([version_row(pk=5, script_id=42, name="1984")])})
    assert (await api.resolve("1984")).script_id == 42


@pytest.mark.asyncio
async def test_a_custom_id_resolves_exactly_and_never_reaches_the_name_search():
    api, session = client(SECTS_ROUTES)

    script = await api.resolve("sects")

    assert (script.script_id, script.slug) == (13108, "sects")
    assert not any("search=" in request for request in session.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["sects", "SECTS", "  Sects  "])
async def test_a_custom_id_is_matched_however_it_was_typed(query):
    api, _ = client(SECTS_ROUTES)
    assert (await api.resolve(query)).script_id == 13108


@pytest.mark.asyncio
async def test_an_unknown_custom_id_falls_through_to_the_name_search():
    # An instance without the feature 404s every custom id route, which has to read as
    # "no such id" rather than as an outage — and a script may be *named* like one.
    rows = page([version_row(pk=5, script_id=42, name="Catfishing")])
    api, session = client({"/api/scripts/": rows})

    assert (await api.resolve("catfishing")).script_id == 42

    assert session.requests[0].startswith("/api/script_ids/slug/catfishing/")


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["13108", "007", "-12", "1_0", " 42 "])
async def test_an_integer_like_query_is_never_looked_up_as_a_custom_id(query):
    # The numeric-id branch is checked first, so a custom id that reads as a number would
    # be shadowed by whichever script happens to hold that id — a wrong answer rather
    # than an error, and only once the instance has grown that far. int() rather than
    # str.isdigit() because int() is the wider net: it also takes a sign, surrounding
    # whitespace and underscores.
    api, session = client(
        {"/api/scripts/": page([version_row(pk=1, script_id=9, name="Two Words")])}
    )

    assert (await api.resolve(query)).script_id == 9

    assert not any("/slug/" in request for request in session.requests)


@pytest.mark.asyncio
async def test_version_lookup_normalises_trailing_zeros_like_the_server_does():
    api, _ = client(
        {
            "/api/scripts/": page([version_row(pk=22338, script_id=77, name="No Greater Joy")]),
            "/api/script_ids/77/": {
                "pk": 77,
                "name": "No Greater Joy",
                "versions": {"1.0.0": f"{BASE}/api/scripts/84/"},
                "latest_version": f"{BASE}/api/scripts/22338/",
            },
            "/api/scripts/84/": version_row(
                pk=84, script_id=77, name="No Greater Joy", version="1.0.0"
            ),
        }
    )
    for requested in ("1", "1.0", "1.0.0"):
        assert (await api.resolve("No Greater Joy", requested)).version_pk == 84


@pytest.mark.asyncio
async def test_an_unknown_version_lists_the_ones_that_do_exist():
    api, _ = client(
        {
            "/api/scripts/": page([version_row(pk=22338, script_id=77, name="No Greater Joy")]),
            "/api/script_ids/77/": {
                "pk": 77,
                "name": "No Greater Joy",
                "versions": {"1.0.0": f"{BASE}/api/scripts/84/", "4.0.0": f"{BASE}/api/scripts/2/"},
                "latest_version": f"{BASE}/api/scripts/2/",
            },
        }
    )
    with pytest.raises(ScriptNotFound, match=re.escape("1.0.0, 4.0.0")):
        await api.resolve("No Greater Joy", "9.9.9")


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["../../etc/passwd", "1.0.0/../..", "9 9 9"])
async def test_a_version_that_is_not_a_version_never_reaches_a_url(version):
    api, session = client(
        {"/api/scripts/": page([version_row(pk=1, script_id=77, name="Two Words")])}
    )
    with pytest.raises(InvalidVersion):
        await api.resolve("Two Words", version)
    assert not any("script_ids" in request for request in session.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["", "online", "latest", "LATEST"])
async def test_the_version_modes_are_not_treated_as_version_numbers(version):
    """"online" and "latest" select a version rather than naming one.

    They are handled before anything is interpolated into a URL, so the guard above
    still holds: only something shaped like a version number ever reaches a path.
    """
    api, session = client(
        {"/api/scripts/": page([version_row(pk=1, script_id=77, name="Two Words")])}
    )
    assert (await api.resolve("Two Words", version)).script_id == 77
    assert not any("script_ids/77/" in request for request in session.requests)


@pytest.mark.asyncio
async def test_upstream_version_urls_are_rebound_to_the_configured_base_url():
    api, session = client(
        {
            "/api/script_ids/77/": {
                "pk": 77,
                "name": "Two Words",
                "versions": {},
                "latest_version": "https://evil.example/api/scripts/84/",
            },
            "/api/scripts/84/": version_row(pk=84, script_id=77, name="Two Words"),
        }
    )
    assert (await api.resolve("77")).version_pk == 84
    assert not any("evil" in request for request in session.requests)


@pytest.mark.asyncio
async def test_json_comes_from_the_inline_content_without_a_second_request():
    rows = [
        version_row(pk=1, script_id=77, name="Two Words", content=[{"id": "imp"}, {"id": "sailor"}])
    ]
    api, session = client({"/api/scripts/": page(rows)})
    script = await api.resolve("Two Words")
    payload = await api.fetch_script_json(script)
    assert json.loads(payload) == [{"id": "imp"}, {"id": "sailor"}]
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_json_falls_back_to_the_dedicated_endpoint_when_content_is_absent():
    script = ScriptVersion(version_pk=22755, script_id=13108, name="Two Words", version="1.0.0")
    api, _ = client({"/api/scripts/22755/json/": [{"id": "imp"}]})
    assert json.loads(await api.fetch_script_json(script)) == [{"id": "imp"}]


@pytest.mark.asyncio
async def test_pdf_download_returns_the_bytes():
    script = ScriptVersion(version_pk=1, script_id=77, name="Two Words", version="4.0.0")
    api, session = client({"/script/77/4.0.0/download_pdf": (200, PDF_BYTES)})
    assert await api.fetch_pdf(script) == PDF_BYTES
    assert session.requests == ["/script/77/4.0.0/download_pdf"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 500])
async def test_a_missing_pdf_is_reported_as_unavailable_not_as_an_outage(status):
    # botc-scripts answers 500 with an HTML error page when a version has no PDF.
    script = ScriptVersion(version_pk=1, script_id=77, name="Two Words", version="4.0.0")
    api, _ = client({"/script/77/4.0.0/download_pdf": (status, b"<h1>Server Error (500)</h1>")})
    with pytest.raises(PdfUnavailable):
        await api.fetch_pdf(script)


@pytest.mark.asyncio
async def test_a_body_without_the_pdf_magic_number_is_rejected():
    script = ScriptVersion(version_pk=1, script_id=77, name="Two Words", version="4.0.0")
    api, _ = client({"/script/77/4.0.0/download_pdf": (200, b"<html>login page</html>")})
    with pytest.raises(PdfUnavailable):
        await api.fetch_pdf(script)


@pytest.mark.asyncio
async def test_an_oversized_pdf_is_refused_rather_than_buffered():
    script = ScriptVersion(version_pk=1, script_id=77, name="Two Words", version="4.0.0")
    session = FakeSession({"/script/77/4.0.0/download_pdf": (200, b"%PDF-" + b"x" * 10_000)})
    api = BotcScriptsClient(session, base_url=BASE, max_pdf_bytes=1_000)
    with pytest.raises(PdfUnavailable, match="limit"):
        await api.fetch_pdf(script)


@pytest.mark.asyncio
async def test_autocomplete_search_uses_the_similarity_ranked_endpoint_and_honours_its_limit():
    rows = [version_row(pk=i, script_id=i, name=f"Sects {i}") for i in range(1, 11)]
    api, session = client({"/api/scripts/": page(rows)})

    found = await api.search("sect", limit=4)

    assert [s.script_id for s in found] == [1, 2, 3, 4]
    # ordering= would raise the trigram threshold past what a half-typed name clears.
    assert "ordering" not in session.requests[0]


@pytest.mark.asyncio
async def test_autocomplete_search_asks_nothing_of_the_server_for_an_empty_query():
    api, session = client({"/api/scripts/": page([])})
    assert await api.search("   ") == []
    assert session.requests == []


@pytest.mark.asyncio
async def test_a_non_json_body_is_an_upstream_error_not_a_crash():
    api, _ = client({"/api/scripts/": (200, b"<html>maintenance</html>")})
    with pytest.raises(UpstreamError):
        await api.resolve("anything")


@pytest.mark.asyncio
async def test_setting_a_custom_id_patches_the_slug_route_with_basic_auth():
    api, session = writing_client(
        {
            "PATCH /api/script_ids/13108/slug/": {
                "pk": 13108,
                "name": "Sects and Violets",
                "slug": "sects",
            }
        }
    )

    info = await api.set_slug(13108, "  SECTS  ")

    assert (info.script_id, info.name, info.slug) == (13108, "Sects and Violets", "sects")
    assert session.requests == ["PATCH /api/script_ids/13108/slug/"]
    # Normalised on the way out, so one custom id has exactly one stored spelling.
    assert session.payloads == [{"slug": "sects"}]
    assert session.headers["Authorization"] == AUTH


@pytest.mark.asyncio
async def test_clearing_a_custom_id_sends_an_explicit_null_rather_than_an_empty_body():
    # The fork treats a missing slug key as a 400 rather than a silent no-op.
    api, session = writing_client(
        {"PATCH /api/script_ids/13108/slug/": {"pk": 13108, "name": "Two Words", "slug": None}}
    )

    assert (await api.set_slug(13108, None)).slug is None
    assert session.payloads == [{"slug": None}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [
        "That slug is already used by another script.",
        "'13108' is a number, and numbers are reserved for script ids.",
        "'search' is reserved because it is part of a site URL. Choose another slug.",
    ],
)
async def test_a_refused_custom_id_is_reported_in_the_instances_own_words(refusal):
    # Reserved words and uniqueness depend on state this bot does not have, so the
    # instance's sentence is passed on rather than paraphrased into something vaguer.
    body = json.dumps({"slug": [refusal]}).encode()
    api, _ = writing_client({"PATCH /api/script_ids/13108/slug/": (400, body)})

    with pytest.raises(SlugRejected, match=re.escape(refusal)):
        await api.set_slug(13108, "sects")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail",
    [
        "Invalid username/password.",
        "You do not have permission to perform this action.",
        "Authentication credentials were not provided.",
    ],
)
async def test_refused_credentials_are_their_own_failure_not_an_outage(detail):
    # DRF answers 403 for all three, because SessionAuthentication sits first in its
    # defaults and sends no WWW-Authenticate header; only the detail tells them apart.
    body = json.dumps({"detail": detail}).encode()
    api, _ = writing_client({"PATCH /api/script_ids/1/slug/": (403, body)})

    with pytest.raises(NotPermitted, match=re.escape(detail)):
        await api.set_slug(1, "sects")


@pytest.mark.asyncio
async def test_a_write_without_credentials_never_leaves_the_bot():
    api, session = client({})

    assert not api.can_write
    with pytest.raises(WriteNotConfigured):
        await api.set_slug(1, "sects")
    assert session.requests == []


@pytest.mark.asyncio
async def test_a_write_to_a_script_that_is_no_longer_there_is_a_not_found():
    api, _ = writing_client({})  # Every route 404s.
    with pytest.raises(ScriptNotFound):
        await api.set_slug(99, "sects")


@pytest.mark.asyncio
async def test_an_unexpected_status_from_a_write_is_an_upstream_error():
    api, _ = writing_client({"PATCH /api/script_ids/1/slug/": (500, b"<h1>Server Error</h1>")})
    with pytest.raises(UpstreamError):
        await api.set_slug(1, "sects")


def test_version_parses_the_server_status():
    from botcbot.botcscripts import ScriptVersion

    row = {"pk": 1, "script_id": 2, "name": "Two Words", "version": "1.0.0", "status": "online"}
    assert ScriptVersion.from_api(row).status == "online"
    assert ScriptVersion.from_api(row).is_offline is False


def test_a_missing_status_is_not_treated_as_offline():
    from botcbot.botcscripts import ScriptVersion

    # The public site has no status field at all. Reading its absence as "offline"
    # would make the whole catalogue unservable there.
    row = {"pk": 1, "script_id": 2, "name": "Two Words", "version": "1.0.0"}
    version = ScriptVersion.from_api(row)
    assert version.status is None
    assert version.is_offline is False


def test_offline_is_only_the_explicit_value():
    from botcbot.botcscripts import ScriptVersion

    row = {"pk": 1, "script_id": 2, "name": "Two Words", "version": "1.0.0", "status": "offline"}
    assert ScriptVersion.from_api(row).is_offline is True


def test_online_only_search_looks_past_the_latest_version():
    """The deployed version is often not the newest one.

    The API returns only latest versions unless all_scripts is set, so without it an
    online older version is invisible to search even with status=online.
    """
    import inspect

    from botcbot.botcscripts import BotcScriptsClient

    source = inspect.getsource(BotcScriptsClient._search)
    assert '"status"' in source and '"online"' in source
    assert '"all_scripts"' in source


def test_resolution_prefers_the_online_version():
    import inspect

    from botcbot.botcscripts import BotcScriptsClient

    source = inspect.getsource(BotcScriptsClient._latest_from_detail)
    # The online version is tried before falling back to latest_version.
    assert source.index("_online_version_for") < source.index("latest_version")

    lookup = inspect.getsource(BotcScriptsClient._online_version_for)
    assert '"script"' in lookup and '"status": "online"' in lookup
    assert '"all_scripts": "true"' in lookup


def test_a_one_character_query_is_treated_as_a_custom_id():
    """The fork allows single-character ids, so the bot must recognise them.

    With a higher minimum here than the server's, an id like "x" is not recognised as
    one and falls through to a fuzzy name search — which answers with whatever script
    the trigram happens to like, rather than the one that id names.
    """
    from botcbot.slugs import MIN_SLUG_LENGTH, is_slug

    assert MIN_SLUG_LENGTH == 1
    assert is_slug("x") is True


def test_a_named_version_is_served_even_when_it_is_not_deployed():
    """Asking for a version by number is explicit, so it is not filtered.

    Serving only what is on the Minecraft server is a default for "just give me the
    script". Someone who names 1.2.0 has said which one they want, and refusing it for
    being undeployed would make the parameter useless.
    """
    import inspect

    from botcbot.botcscripts import BotcScriptsClient

    source = inspect.getsource(BotcScriptsClient.resolve)
    # The online check runs only in the online mode, not for latest or a named version.
    assert "online_wanted = not wanted or folded == \"online\"" in source
    assert "if online_wanted:" in source
    named = source.index("fetch_version")
    guard = source.index("_require_online")
    assert guard < named, "the online guard must not cover the named-version path"


def test_latest_is_selectable_explicitly():
    import inspect

    from botcbot.botcscripts import BotcScriptsClient

    source = inspect.getsource(BotcScriptsClient.resolve)
    assert '"latest"' in source
    # latest skips the online preference when resolving, so it really is the newest.
    assert "prefer_online=online_wanted" in source
