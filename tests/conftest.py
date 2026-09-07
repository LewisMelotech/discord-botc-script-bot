"""Offline stand-ins: a fake aiohttp session, a fake Discord interaction, real PDFs."""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import discord
from PIL import Image, ImageDraw


class FakeResponse:
    def __init__(self, status: int, body: bytes, *, delay: float = 0.0) -> None:
        self.status = status
        self._body = body
        self._delay = delay

    async def __aenter__(self) -> FakeResponse:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def read(self) -> bytes:
        return self._body

    @property
    def content_length(self) -> int:
        return len(self._body)

    @property
    def content(self) -> FakeStream:
        return FakeStream(self._body)


class FakeStream:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int):
        for offset in range(0, len(self._body), size):
            yield self._body[offset : offset + size]


class FakeSession:
    """Routes are keyed by the request path; values are (status, body) or a payload.

    A write is keyed by ``"<METHOD> <path>"`` — ``"PATCH /api/script_ids/13108/slug/"`` —
    so one dict describes both halves and every key reads as what was asked for.
    """

    def __init__(self, routes: dict[str, Any], *, delay: float = 0.0) -> None:
        self.routes = routes
        self.requests: list[str] = []
        self.delay = delay
        # What the bot sent, and with which credentials, for the write tests to assert on.
        self.payloads: list[Any] = []
        self.headers: dict[str, Any] = {}

    def get(self, url: str, params: dict[str, str] | None = None, headers: Any = None):
        path = _path_of(url)
        self.requests.append(path + ("?" + urlencode(params) if params else ""))
        return self._respond(self.routes.get(path))

    def request(self, method: str, url: str, *, data: Any = None, headers: Any = None):
        path = _path_of(url)
        self.requests.append(f"{method} {path}")
        self.payloads.append(json.loads(data) if data else None)
        self.headers = dict(headers or {})
        return self._respond(self.routes.get(f"{method} {path}"))

    def _respond(self, route: Any) -> FakeResponse:
        if route is None:
            return FakeResponse(404, b"not found", delay=self.delay)
        if isinstance(route, tuple):
            status, body = route
            body = body if isinstance(body, bytes) else body.encode()
            return FakeResponse(status, body, delay=self.delay)
        return FakeResponse(200, json.dumps(route).encode(), delay=self.delay)


def _path_of(url: str) -> str:
    return "/" + url.split("://", 1)[-1].split("/", 1)[-1]


def version_row(
    *,
    pk: int,
    script_id: int,
    name: str,
    version: str = "1.0.0",
    author: str | None = "Someone",
    content: list[Any] | None = None,
    slug: str | None = None,
) -> dict[str, Any]:
    return {
        "pk": pk,
        "script_id": script_id,
        "name": name,
        "version": version,
        "script_type": "Full",
        "author": author,
        "content": content if content is not None else [{"id": "imp"}],
        "score": 0,
        # The fork sends the script's custom id on version rows too, as null when unset.
        # The public site omits the key entirely, which reads back the same way.
        "slug": slug,
    }


def script_detail(
    *, pk: int, name: str, version_pk: int, version: str = "1.0.0", slug: str | None = None
) -> dict[str, Any]:
    """One ``/api/script_ids/<pk>/`` body: a script's identity and its version list."""
    return {
        "pk": pk,
        "name": name,
        "slug": slug,
        "versions": {version: f"https://example.test/api/scripts/{version_pk}/"},
        "latest_version": f"https://example.test/api/scripts/{version_pk}/",
    }


def page(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"count": len(rows), "next": None, "previous": None, "results": rows}


def make_pdf(pages: int, *, noisy: bool = False) -> bytes:
    """A real multi-page PDF, built with Pillow so the tests need no fixture file."""
    images = []
    for index in range(pages):
        image = Image.new("RGB", (595, 842), "white")
        draw = ImageDraw.Draw(image)
        draw.text((40, 40), f"page {index + 1}", fill="black")
        if noisy:
            for x in range(0, 595, 3):
                for y in range(0, 842, 7):
                    draw.point((x, y), fill=((x * 7) % 256, (y * 13) % 256, (x + y) % 256))
        images.append(image)

    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:], resolution=72)
    return buffer.getvalue()


@dataclass
class SentMessage:
    """One message the bot delivered, and how it was delivered."""

    kind: str  # "edit" for the deferred placeholder, "followup", or "response"
    content: str | None
    ephemeral: bool
    files: list[tuple[str, bytes]] = field(default_factory=list)

    @property
    def filenames(self) -> list[str]:
        return [name for name, _ in self.files]


def _text(content: Any) -> str | None:
    return content if isinstance(content, str) else None


def _files(files: Any) -> list[tuple[str, bytes]]:
    return [(f.filename, f.fp.getvalue()) for f in (files or ())]


class FakeInteractionResponse:
    def __init__(self, interaction: FakeInteraction) -> None:
        self._interaction = interaction
        self.deferred: dict[str, bool] | None = None

    async def defer(self, *, thinking: bool = False, ephemeral: bool = False) -> None:
        self.deferred = {"thinking": thinking, "ephemeral": ephemeral}

    def is_done(self) -> bool:
        return self.deferred is not None or bool(self._interaction.sent)

    async def send_message(self, content: Any = None, *, ephemeral: bool = False, **_: Any):
        self._interaction.sent.append(
            SentMessage(kind="response", content=_text(content), ephemeral=ephemeral)
        )


class FakeFollowup:
    def __init__(self, interaction: FakeInteraction) -> None:
        self._interaction = interaction

    async def send(
        self, content: Any = None, *, files: Any = (), ephemeral: bool = False, **_: Any
    ):
        self._interaction.sent.append(
            SentMessage(
                kind="followup",
                content=_text(content),
                ephemeral=ephemeral,
                files=_files(files),
            )
        )


class FakeInteraction:
    """Enough of :class:`discord.Interaction` for the command layer to run offline."""

    def __init__(
        self,
        client: Any,
        *,
        command_name: str = "script",
        guild_id: int | None = None,
        filesize_limit: int = 10 * 1024 * 1024,
        expires_in: float = 900.0,
        interaction_type: discord.InteractionType = discord.InteractionType.application_command,
        permissions: discord.Permissions | None = None,
        user_id: int = 4242,
    ) -> None:
        self.client = client
        self.command = SimpleNamespace(name=command_name, qualified_name=command_name)
        self.guild_id = guild_id
        self.filesize_limit = filesize_limit
        self.type = interaction_type
        self.extras: dict[str, Any] = {}
        self.sent: list[SentMessage] = []
        self.expired = False
        self.expires_at = discord.utils.utcnow() + timedelta(seconds=expires_in)
        self.response = FakeInteractionResponse(self)
        self.followup = FakeFollowup(self)
        # A command's own checks read these three. Permissions default to none, so a test
        # of an administrators-only command has to opt in to being an administrator.
        self.permissions = discord.Permissions.none() if permissions is None else permissions
        self.user = SimpleNamespace(id=user_id)
        self.created_at = discord.utils.utcnow()

    def is_expired(self) -> bool:
        return self.expired

    async def edit_original_response(self, *, content: Any = None, attachments: Any = ()):
        # Discord fixes the placeholder's visibility at defer time and refuses to change
        # it afterwards, so record the deferred value rather than anything passed here.
        deferred = self.response.deferred or {}
        self.sent.append(
            SentMessage(
                kind="edit",
                content=_text(content),
                ephemeral=bool(deferred.get("ephemeral")),
                files=_files(attachments),
            )
        )
