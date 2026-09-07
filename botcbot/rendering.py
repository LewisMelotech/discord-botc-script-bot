"""Blocking PDF rasterisation. Call it through :func:`asyncio.to_thread`, never inline.

pypdfium2 is used rather than PyMuPDF because PyMuPDF is AGPL-3.0, whose network
clause is awkward for a bot other people interact with remotely. pypdfium2 is
BSD-3-Clause/Apache-2.0 and ships PDFium in the wheel, so there are no system
packages to install either.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Final

import pypdfium2 as pdfium
from PIL import Image

MIN_DPI: Final = 50
_DOWNSCALE_FACTOR: Final = 0.7
_JPEG_QUALITIES: Final = (85, 70, 55)

# scale=1 renders at 72 DPI in PDFium.
_POINTS_PER_INCH: Final = 72

# A page's dimensions come out of the PDF and have nothing to do with its file size, so
# the download-size limit does not bound the raster: at 150 DPI the spec's largest legal
# MediaBox (200x200in) is 900 Mpx, i.e. 3.6 GB for PDFium's buffer plus as much again for
# the Pillow copy. Cap the pixels so peak memory follows this constant instead. 40 Mpx is
# ~160 MB a buffer and well clear of any real page: A4 is 2.2 Mpx at 150 DPI and 15.5 Mpx
# even at the highest configurable 400.
MAX_RENDER_PIXELS: Final = 40_000_000


class RenderError(Exception):
    """The PDF could not be turned into images. The message is shown to the user."""


@dataclass(frozen=True, slots=True)
class RenderedPage:
    filename: str
    data: bytes


@dataclass(frozen=True, slots=True)
class RenderResult:
    pages: tuple[RenderedPage, ...]
    total_pages: int
    size_limited: bool

    @property
    def rendered_pages(self) -> int:
        return len(self.pages)

    @property
    def omitted_pages(self) -> int:
        return max(0, self.total_pages - self.rendered_pages)

    @property
    def total_bytes(self) -> int:
        return sum(len(page.data) for page in self.pages)


def rasterise(
    pdf_bytes: bytes,
    *,
    dpi: int = 150,
    max_pages: int = 10,
    per_file_budget: int,
    total_budget: int,
) -> RenderResult:
    """BLOCKING. Render the first ``max_pages`` pages as PNG (or JPEG if too big)."""
    try:
        document = pdfium.PdfDocument(pdf_bytes)
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise RenderError("the PDF is password-protected") from exc
        raise RenderError("the PDF could not be read") from exc

    try:
        total_pages = len(document)
        if total_pages == 0:
            raise RenderError("the PDF has no pages")

        pages: list[RenderedPage] = []
        used = 0
        size_limited = False

        for index in range(min(total_pages, max_pages)):
            budget = min(per_file_budget, total_budget - used)
            if budget <= 0:
                size_limited = True
                break

            try:
                encoded, extension = _render_page(document, index, dpi=dpi, budget=budget)
            except pdfium.PdfiumError as exc:
                raise RenderError(f"page {index + 1} of the PDF could not be rendered") from exc
            except MemoryError as exc:
                # pypdfium2 allocates the bitmap with ctypes, which raises MemoryError
                # rather than PdfiumError when the page is too big to hold.
                raise RenderError(f"page {index + 1} of the PDF is too large to render") from exc

            if encoded is None:
                if pages:
                    size_limited = True
                    break
                raise RenderError(
                    "the first page cannot be compressed under Discord's upload limit"
                )

            pages.append(RenderedPage(f"page_{index + 1:03d}.{extension}", encoded))
            used += len(encoded)

        if not pages:
            raise RenderError("no pages could be rendered under Discord's upload limit")

        return RenderResult(tuple(pages), total_pages, size_limited)
    finally:
        document.close()


def _render_page(
    document: pdfium.PdfDocument, index: int, *, dpi: int, budget: int
) -> tuple[bytes | None, str]:
    """Render one page, stepping the DPI down until it fits ``budget``."""
    page = document[index]
    try:
        # The requested DPI is only an upper bound: a page with an enormous MediaBox is
        # rendered at whatever scale keeps it under MAX_RENDER_PIXELS.
        max_scale = _pixel_capped_scale(*page.get_size())
        current_dpi = min(float(max(MIN_DPI, dpi)), max_scale * _POINTS_PER_INCH)
        while True:
            bitmap = page.render(scale=min(current_dpi / _POINTS_PER_INCH, max_scale))
            try:
                image = bitmap.to_pil()
            finally:
                bitmap.close()

            try:
                encoded, extension = _encode(image, budget)
            finally:
                image.close()

            if encoded is not None:
                return encoded, extension
            if current_dpi <= MIN_DPI:
                return None, ""
            current_dpi = max(MIN_DPI, int(current_dpi * _DOWNSCALE_FACTOR))
    finally:
        page.close()


def _pixel_capped_scale(width_pt: float, height_pt: float) -> float:
    """The largest render scale that keeps this page under ``MAX_RENDER_PIXELS``."""
    area = width_pt * height_pt
    if area <= 0:
        return float("inf")
    return (MAX_RENDER_PIXELS / area) ** 0.5


def _encode(image: Image.Image, budget: int) -> tuple[bytes | None, str]:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    png = buffer.getvalue()
    if len(png) <= budget:
        return png, "png"

    rgb = image.convert("RGB") if image.mode != "RGB" else image
    try:
        for quality in _JPEG_QUALITIES:
            buffer = io.BytesIO()
            rgb.save(buffer, format="JPEG", quality=quality, optimize=True)
            jpeg = buffer.getvalue()
            if len(jpeg) <= budget:
                return jpeg, "jpg"
    finally:
        if rgb is not image:
            rgb.close()
    return None, ""
