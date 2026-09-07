from __future__ import annotations

import io

import pytest
from conftest import make_pdf
from PIL import Image

from botcbot.rendering import MIN_DPI, RenderError, rasterise

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8"


def test_a_short_pdf_renders_every_page_as_png():
    result = rasterise(make_pdf(3), dpi=150, per_file_budget=10**7, total_budget=10**8)
    assert (result.rendered_pages, result.total_pages, result.omitted_pages) == (3, 3, 0)
    assert not result.size_limited
    assert [p.filename for p in result.pages] == ["page_001.png", "page_002.png", "page_003.png"]
    assert all(p.data.startswith(PNG_MAGIC) for p in result.pages)


def test_pages_beyond_the_cap_are_reported_not_silently_dropped():
    result = rasterise(
        make_pdf(14), dpi=100, max_pages=10, per_file_budget=10**7, total_budget=10**8
    )
    assert (result.rendered_pages, result.total_pages, result.omitted_pages) == (10, 14, 4)
    assert not result.size_limited


def test_a_page_over_the_per_file_budget_falls_back_to_jpeg():
    result = rasterise(
        make_pdf(1, noisy=True), dpi=150, per_file_budget=250_000, total_budget=10**8
    )
    page = result.pages[0]
    assert page.filename.endswith(".jpg")
    assert page.data.startswith(JPEG_MAGIC)
    assert len(page.data) <= 250_000


def test_a_tight_budget_downscales_the_image_not_just_the_jpeg_quality():
    pdf = make_pdf(1, noisy=True)
    roomy = rasterise(pdf, dpi=200, per_file_budget=10**7, total_budget=10**8)
    tight = rasterise(pdf, dpi=200, per_file_budget=40_000, total_budget=10**8)

    assert len(tight.pages[0].data) <= 40_000
    full_size = Image.open(io.BytesIO(roomy.pages[0].data)).size
    shrunk_size = Image.open(io.BytesIO(tight.pages[0].data)).size
    assert shrunk_size[0] < full_size[0] and shrunk_size[1] < full_size[1]


def test_the_request_budget_stops_rendering_and_says_so():
    full = rasterise(make_pdf(6, noisy=True), dpi=150, per_file_budget=10**7, total_budget=10**8)
    half = full.total_bytes // 2
    result = rasterise(make_pdf(6, noisy=True), dpi=150, per_file_budget=10**7, total_budget=half)
    assert result.size_limited
    assert 0 < result.rendered_pages < 6
    assert result.total_bytes <= half


def test_min_dpi_is_the_floor_of_the_downscale_ladder():
    assert MIN_DPI == 50
    with pytest.raises(RenderError, match="first page"):
        rasterise(make_pdf(1, noisy=True), dpi=150, per_file_budget=200, total_budget=10**8)


@pytest.mark.parametrize(
    "payload", [b"", b"not a pdf", b"%PDF-1.4 truncated right here", b"\x00" * 512]
)
def test_unreadable_input_raises_a_user_facing_render_error(payload):
    with pytest.raises(RenderError):
        rasterise(payload, per_file_budget=10**7, total_budget=10**8)
