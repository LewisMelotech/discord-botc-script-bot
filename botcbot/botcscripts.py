"""Async client for a botc-scripts instance.

Deliberately free of any Discord import so it can be exercised on its own.

Endpoint behaviour this client works around, all of it verified against
https://www.botcscripts.com:

* ``/api/scripts/?search=`` is a Postgres trigram fuzzy match, not an exact or
  substring match. Without ``ordering`` the similarity threshold is 0, so
  literally anything matches; adding ``ordering`` raises it to 0.3. We use the
  tight set for candidates and compare names exactly ourselves.
* ``ordering`` also replaces the similarity ranking with whatever it sorts by, and
  every list response is capped at 50 rows, so an exactly-named script can fall off
  the end of the tight set. The unordered search stays similarity-ranked and puts an
  exact name first, so it is consulted before a name is declared ambiguous.
* Homebrew and hybrid scripts are excluded unless asked for explicitly.
* The read API exposes no ``pdf`` field at all, so a PDF can only be obtained by
  fetching ``/script/<id>/<version>/download_pdf``.
* That view has no error handling upstream: a missing PDF, an unknown script and
  an unknown version all return HTTP 500 with an HTML body, never a 404.

Custom ids (slugs) are the one part of this that upstream does not have. Every slug
route is treated as optional: an instance without the feature 404s, which this client
reads as "no such slug" and falls through, so the same code works against both.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

import aiohttp

from . import __version__
from .slugs import is_slug, normalise_slug

_LOGGER = logging.getLogger(__name__)

USER_AGENT: Final = (
    f"botc-discord-bot/{__version__} "
    f"(Discord slash-command bot; aiohttp/{aiohttp.__version__})"
)

MAX_SUGGESTIONS: Final = 8

_VERSION_RE: Final = re.compile(r"^\d+(?:\.\d+){0,2}$")
_VERSION_PK_RE: Final = re.compile(r"/api/scripts/(\d+)/")
_UNSAFE_FILENAME_RE: Final = re.compile(r"[^A-Za-z0-9._ -]+")


class BotcScriptsError(Exception):
    """Base class for every failure this client reports."""


class UpstreamError(BotcScriptsError):
    """The botc-scripts instance was unreachable, timed out or answered nonsense."""


class InvalidVersion(BotcScriptsError):
    """A user-supplied version string was not a dotted numeric version."""


class ScriptNotFound(BotcScriptsError):
    def __init__(
        self,
        query: str,
        suggestions: list[ScriptVersion] | None = None,
        *,
        message: str | None = None,
    ) -> None:
        super().__init__(message or f"No script matched {query!r}.")
        self.query = query
        self.suggestions = suggestions or []


class AmbiguousScript(BotcScriptsError):
    def __init__(self, query: str, candidates: list[ScriptVersion]) -> None:
        super().__init__(f"{len(candidates)} scripts matched {query!r}.")
        self.query = query
        self.candidates = candidates


class PdfUnavailable(BotcScriptsError):
    """No renderable PDF could be obtained for this version."""


class WriteNotConfigured(BotcScriptsError):
    """A write was attempted on a client that holds no API credentials."""


class NotPermitted(BotcScriptsError):
    """The instance rejected this bot's credentials, or they lack the write permission."""


class SlugRejected(BotcScriptsError):
    """A custom id was refused: bad shape, a reserved word, or already taken."""


@dataclass(frozen=True, slots=True)
class ScriptInfo:
    """One ``/api/script_ids/`` row: a script's identity, with no version attached."""

    script_id: int
    name: str
    slug: str | None = None

    @classmethod
    def from_api(cls, payload: Any) -> ScriptInfo:
        if not isinstance(payload, dict):
            raise UpstreamError("Expected a script object from the API.")
        try:
            script_id = int(payload["pk"])
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamError(f"Malformed script payload: {exc}") from exc
        return cls(
            script_id=script_id,
            name=_opt_str(payload.get("name")) or f"Script {script_id}",
            slug=_opt_str(payload.get("slug")),
        )


@dataclass(frozen=True, slots=True)
class ScriptVersion:
    """One row of ``/api/scripts/``.

    ``version_pk`` addresses the API detail routes; ``script_id`` is what the
    website's ``/script/<id>/...`` download URLs take. They are different numbers.

    ``slug`` is the script's custom id where the instance has the feature and the
    script has been given one, and ``None`` everywhere else — including against the
    public site, which never sends the field.
    """

    version_pk: int
    script_id: int
    name: str
    version: str
    script_type: str | None = None
    author: str | None = None
    score: int | None = None
    content: list[Any] | None = None
    slug: str | None = None

    @classmethod
    def from_api(cls, payload: Any) -> ScriptVersion:
        if not isinstance(payload, dict):
            raise UpstreamError("Expected a script version object from the API.")
        try:
            version_pk = int(payload["pk"])
            script_id = int(payload["script_id"])
            name = str(payload["name"])
            version = str(payload["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamError(f"Malformed script version payload: {exc}") from exc

        content = payload.get("content")
        return cls(
            version_pk=version_pk,
            script_id=script_id,
            name=name,
            version=version,
            script_type=_opt_str(payload.get("script_type")),
            author=_opt_str(payload.get("author")),
            score=payload.get("score") if isinstance(payload.get("score"), int) else None,
            content=content if isinstance(content, list) else None,
            slug=_opt_str(payload.get("slug")),
        )

    @property
    def label(self) -> str:
        return f"{self.name} v{self.version}"

    @property
    def reference(self) -> str:
        """The shortest id that resolves back to exactly this script.

        The slug when there is one, and the numeric id otherwise. Used wherever the
        bot hands an id back to a human to re-use.
        """
        return self.slug or str(self.script_id)

    @property
    def json_filename(self) -> str:
        stem = safe_filename(self.name) or "script"
        return f"{stem}_{self.version.replace('.', '_')}.json"

    def web_url(self, base_url: str) -> str:
        # The fork routes /script/<slug>/<version> alongside the numeric form, so the
        # link can carry the canonical id. Only the two page routes take a slug —
        # download_pdf and the rest stay numeric, and are built from script_id.
        return f"{base_url.rstrip('/')}/script/{quote(self.reference)}/{quote(self.version)}"


def basic_auth(user: str, password: str) -> str:
    """An ``Authorization`` header value for HTTP Basic, as the fork's write API wants.

    ``aiohttp.BasicAuth`` would do the same job, but it is deprecated and goes away in
    aiohttp 4, so the header is built directly. Raises :class:`ValueError` if the
    username contains a colon, which Basic auth cannot represent.
    """
    return aiohttp.encode_basic_auth(user, password)


class BotcScriptsClient:
    """Read-only unless given ``auth``. The caller owns the :class:`aiohttp.ClientSession`.

    ``auth`` is an ``Authorization`` header value — see :func:`basic_auth` — for a user
    holding the fork's ``scripts.api_write_permission``. Without it the only write
    method, :meth:`set_slug`, raises :class:`WriteNotConfigured` rather than sending
    anything.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        max_pdf_bytes: int = 60 * 1024 * 1024,
        auth: str | None = None,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._max_pdf_bytes = max_pdf_bytes
        self._auth = auth

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def can_write(self) -> bool:
        return self._auth is not None

    async def resolve(self, query: str, version: str | None = None) -> ScriptVersion:
        """Turn a name, custom id or numeric id (plus optional version) into one version.

        Raises :class:`ScriptNotFound`, :class:`AmbiguousScript`,
        :class:`InvalidVersion` or :class:`UpstreamError`.
        """
        cleaned = query.strip()
        if not cleaned:
            raise ScriptNotFound(query)

        latest = await self._resolve_script(cleaned)
        if version is None or not version.strip():
            return latest
        return await self.fetch_version(latest.script_id, version.strip())

    async def search(self, query: str, *, limit: int = 25) -> list[ScriptVersion]:
        """Similarity-ranked name matches, for autocomplete suggestions.

        Uses the unordered search deliberately: ``ordering`` raises the trigram
        threshold to 0.3, which a half-typed name almost never clears, and replaces the
        similarity ranking that makes the head of this list worth showing.
        """
        cleaned = query.strip()
        if not cleaned or limit <= 0:
            return []
        return (await self._search(cleaned, ordering=None))[:limit]

    async def fetch_version(self, script_id: int, version: str) -> ScriptVersion:
        """Fetch one specific version of a known script."""
        if not _VERSION_RE.match(version):
            raise InvalidVersion(
                f"{version!r} is not a version number. Use a form like `1.0.0`."
            )

        detail = await self._get_json(f"/api/script_ids/{int(script_id)}/", {"format": "json"})
        if detail is None:
            raise ScriptNotFound(str(script_id))
        if not isinstance(detail, dict):
            raise UpstreamError(f"{self._base_url} did not return a script object.")

        versions = detail.get("versions")
        if not isinstance(versions, dict) or not versions:
            raise ScriptNotFound(str(script_id))

        wanted = _normalise_version(version)
        for candidate, url in versions.items():
            if _normalise_version(str(candidate)) == wanted:
                return await self._fetch_version_by_url(str(url))

        name = _opt_str(detail.get("name")) or str(script_id)
        available = ", ".join(sorted((str(v) for v in versions), key=_normalise_version))
        raise ScriptNotFound(
            f"{name} v{version}",
            message=f"**{name}** has no version `{version}`. Available: {available}",
        )

    async def fetch_script_json(self, script: ScriptVersion) -> bytes:
        """Return the script's character JSON, pretty-printed, as bytes."""
        content = script.content
        if content is None:
            content = await self._get_json(
                f"/api/scripts/{script.version_pk}/json/", {"format": "json"}
            )
        if not isinstance(content, list):
            raise UpstreamError("The API did not return a script JSON list.")
        return json.dumps(content, indent=2, ensure_ascii=False).encode("utf-8")

    async def fetch_pdf(self, script: ScriptVersion) -> bytes:
        """Download the uploaded PDF, raising :class:`PdfUnavailable` if there isn't one."""
        url = (
            f"{self._base_url}/script/{script.script_id}"
            f"/{quote(script.version, safe='.')}/download_pdf"
        )
        try:
            async with self._session.get(url, headers={"User-Agent": USER_AGENT}) as resp:
                if resp.status != 200:
                    # botc-scripts raises an unhandled exception rather than 404ing when a
                    # version has no PDF, so a 500 here is indistinguishable from "missing".
                    if resp.status in (404, 500):
                        raise PdfUnavailable("no PDF has been uploaded for this version")
                    raise UpstreamError(
                        f"{self._base_url} returned HTTP {resp.status} for the PDF."
                    )

                declared = resp.content_length
                if declared is not None and declared > self._max_pdf_bytes:
                    raise PdfUnavailable(
                        f"the PDF is {human_bytes(declared)}, over this bot's "
                        f"{human_bytes(self._max_pdf_bytes)} limit"
                    )

                buffer = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buffer += chunk
                    if len(buffer) > self._max_pdf_bytes:
                        raise PdfUnavailable(
                            f"the PDF is over this bot's "
                            f"{human_bytes(self._max_pdf_bytes)} limit"
                        )
        except TimeoutError as exc:
            raise UpstreamError(f"Timed out downloading the PDF from {self._base_url}.") from exc
        except aiohttp.ClientError as exc:
            raise UpstreamError(f"Could not download the PDF: {exc}") from exc

        data = bytes(buffer)
        if not data.startswith(b"%PDF-"):
            raise PdfUnavailable("the upstream response was not a PDF")
        return data

    async def _resolve_script(self, query: str) -> ScriptVersion:
        # isascii() matters: str.isdigit() is also true for superscripts, which int()
        # cannot parse at all, and for other digit systems, which parse to a number the
        # user never typed. Both must go to the name search instead.
        if query.isascii() and query.isdigit():
            found = await self._latest_for_script_id(int(query))
            if found is not None:
                return found
            # Fall through: a script may legitimately be *named* something numeric.

        # Custom ids are exact, case-insensitive, and cannot reach the branch above:
        # is_slug() is false for everything int() accepts, so the two are disjoint by
        # construction rather than by ordering. On an instance without the feature
        # this 404s and costs one request before the name search below.
        folded = normalise_slug(query)
        if is_slug(folded):
            found = await self._latest_for_slug(folded)
            if found is not None:
                return found
            # Fall through: a script may legitimately be *named* like a custom id.

        return await self._resolve_by_name(query)

    async def _latest_for_script_id(self, script_id: int) -> ScriptVersion | None:
        return await self._latest_from_detail(
            await self._get_json(f"/api/script_ids/{script_id}/", {"format": "json"})
        )

    async def _latest_for_slug(self, slug: str) -> ScriptVersion | None:
        return await self._latest_from_detail(
            await self._get_json(f"/api/script_ids/slug/{quote(slug)}/", {"format": "json"})
        )

    async def _latest_from_detail(self, detail: Any | None) -> ScriptVersion | None:
        """Follow a script detail body to its latest version. ``None`` means 404."""
        if detail is None:
            return None
        if not isinstance(detail, dict):
            raise UpstreamError(f"{self._base_url} did not return a script object.")
        latest = detail.get("latest_version")
        if not isinstance(latest, str) or not latest:
            return None
        return await self._fetch_version_by_url(latest)

    async def set_slug(self, script_id: int, slug: str | None) -> ScriptInfo:
        """Set a script's custom id, or clear it with ``None``. Needs ``auth``.

        Raises :class:`SlugRejected` (the instance refused the id), :class:`NotPermitted`
        (bad or unprivileged credentials), :class:`ScriptNotFound`,
        :class:`WriteNotConfigured` or :class:`UpstreamError`.
        """
        payload = await self._write_json(
            "PATCH",
            f"/api/script_ids/{int(script_id)}/slug/",
            {"slug": normalise_slug(slug) if slug is not None else None},
        )
        return ScriptInfo.from_api(payload)

    async def _resolve_by_name(self, query: str) -> ScriptVersion:
        # ordering= raises the trigram threshold to 0.3, which is what makes this a
        # usable candidate set rather than a third of the site.
        candidates = await self._search(query, ordering="-score")

        folded = query.casefold()
        exact = [c for c in candidates if c.name.casefold() == folded]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise AmbiguousScript(query, exact[:MAX_SUGGESTIONS])

        # ordering= sorts by score rather than by similarity, and one page is 50 rows,
        # so a script named exactly what was asked for can sit past the end of the tight
        # set: "Brew Troubling" is row 67 of 170 for its own name. The unordered search
        # is the similarity-ranked one and puts an exact name first, so ask it before
        # concluding the name is ambiguous or missing.
        loose = await self._search(query, ordering=None)
        loose_exact = [c for c in loose if c.name.casefold() == folded]
        if len(loose_exact) == 1:
            return loose_exact[0]
        if len(loose_exact) > 1:
            raise AmbiguousScript(query, loose_exact[:MAX_SUGGESTIONS])

        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            substring = [c for c in candidates if folded in c.name.casefold()]
            if len(substring) == 1:
                return substring[0]
            raise AmbiguousScript(query, (substring or candidates)[:MAX_SUGGESTIONS])

        # Nothing cleared the 0.3 threshold, but the loose search ranks by similarity,
        # so its head makes a reasonable "did you mean".
        raise ScriptNotFound(query, loose[:MAX_SUGGESTIONS])

    async def _search(self, query: str, *, ordering: str | None) -> list[ScriptVersion]:
        params: dict[str, str] = {
            "format": "json",
            "search": query,
            "include_homebrew": "true",
            "include_hybrid": "true",
        }
        if ordering:
            params["ordering"] = ordering

        payload = await self._get_json("/api/scripts/", params)
        if payload is None:
            raise UpstreamError(f"{self._base_url} has no /api/scripts/ endpoint.")
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise UpstreamError("The search endpoint did not return a results list.")
        return [ScriptVersion.from_api(row) for row in results]

    async def _fetch_version_by_url(self, url: str) -> ScriptVersion:
        # Upstream hands back absolute URLs built from its own SITE_URL. Reduce them to
        # the version pk and rebuild against our configured base so a misconfigured or
        # hostile instance cannot redirect this bot at another host.
        match = _VERSION_PK_RE.search(url)
        if not match:
            raise UpstreamError(f"Could not read a version id out of {url!r}.")
        payload = await self._get_json(f"/api/scripts/{int(match.group(1))}/", {"format": "json"})
        if payload is None:
            raise UpstreamError(f"Version {match.group(1)} vanished from the API.")
        return ScriptVersion.from_api(payload)

    async def _get_json(self, path: str, params: dict[str, str]) -> Any | None:
        """GET a JSON endpoint. Returns ``None`` on 404, raises on anything else bad."""
        url = f"{self._base_url}{path}"
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        try:
            async with self._session.get(url, params=params, headers=headers) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    raise UpstreamError(f"{url} returned HTTP {resp.status}.")
                body = await resp.read()
        except TimeoutError as exc:
            raise UpstreamError(f"Timed out talking to {self._base_url}.") from exc
        except aiohttp.ClientError as exc:
            raise UpstreamError(f"Could not reach {self._base_url}: {exc}") from exc

        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _LOGGER.debug("Non-JSON body from %s: %r", url, body[:200])
            raise UpstreamError(f"{url} did not return JSON.") from exc

    async def _write_json(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        """Send an authenticated write, translating each refusal into its own exception."""
        if self._auth is None:
            raise WriteNotConfigured("This bot has no API credentials for this instance.")

        url = f"{self._base_url}{path}"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self._auth,
        }
        try:
            async with self._session.request(
                method,
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
            ) as resp:
                status = resp.status
                body = await resp.read()
        except TimeoutError as exc:
            raise UpstreamError(f"Timed out talking to {self._base_url}.") from exc
        except aiohttp.ClientError as exc:
            raise UpstreamError(f"Could not reach {self._base_url}: {exc}") from exc

        try:
            parsed = json.loads(body) if body else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            _LOGGER.debug("Non-JSON body from %s: %r", url, body[:200])
            parsed = None

        if status == 200:
            if parsed is None:
                raise UpstreamError(f"{url} did not return JSON.")
            return parsed
        if status == 400:
            # The instance owns the rules this bot cannot check — reserved words and
            # uniqueness — and words the refusal itself, so pass its sentence along
            # rather than paraphrasing it into something vaguer.
            raise SlugRejected(_api_errors(parsed) or "the instance would not accept it")
        if status in (401, 403):
            # DRF answers 403 rather than 401 for missing and wrong credentials alike,
            # because SessionAuthentication sits first in its defaults and sends no
            # WWW-Authenticate header. Only the detail string tells the two apart.
            raise NotPermitted(_api_errors(parsed) or "the credentials were refused")
        if status == 404:
            raise ScriptNotFound(path, message="That script is no longer on the instance.")
        raise UpstreamError(f"{url} returned HTTP {status}.")


def _api_errors(payload: Any) -> str | None:
    """Flatten a DRF error body into one sentence.

    Both shapes it uses are handled: ``{"detail": "..."}`` for a refused request, and
    ``{"<field>": ["...", ...]}`` for a refused value. The only field this bot ever
    writes is the slug, so the field names are dropped and the messages kept.
    """
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None

    detail = payload.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()

    messages: list[str] = []
    for value in payload.values():
        for item in value if isinstance(value, list) else [value]:
            text = str(item).strip()
            if text:
                messages.append(text)
    return " ".join(messages) or None


def safe_filename(name: str, *, max_length: int = 80) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("", name).strip().replace(" ", "_")
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("._-")
    return cleaned[:max_length]


def _normalise_version(version: str) -> tuple[int, int, int]:
    """``1``, ``1.0`` and ``1.0.0`` all address the same version upstream."""
    parts = [*version.strip().split("."), "0", "0", "0"][:3]
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return (-1, -1, -1)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{count} B"
