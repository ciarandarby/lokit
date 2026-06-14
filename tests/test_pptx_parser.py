from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from test_office import PPTX_FIXTURE, _write_minimal_pptx

import lokit

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def pptx_fixture(tmp_path: Path) -> Path:
    if PPTX_FIXTURE.exists():
        return PPTX_FIXTURE
    path = tmp_path / "minimal.pptx"
    _write_minimal_pptx(path)
    return path


def test_pptx_parse_output_has_office_extensions(pptx_fixture: Path) -> None:
    doc = lokit.parse.pptx(pptx_fixture, source_locale="en", progress=False)

    assert doc.data
    for unit_id, data in doc.data.items():
        assert unit_id.startswith("pptx:")
        assert data.source
        assert data.extensions["office.format"] == "pptx"
        assert data.extensions["office.part"].startswith("ppt/")
        assert "office.source_fingerprint" in data.extensions


def test_pptx_stream(pptx_fixture: Path) -> None:
    streamed = lokit.stream.pptx(pptx_fixture, source_locale="en")

    assert list(streamed.items)


@pytest.mark.asyncio
async def test_pptx_async(pptx_fixture: Path) -> None:
    items = [item async for item in lokit.stream.async_.pptx(pptx_fixture, source_locale="en")]

    assert items
    assert items[0][0].startswith("pptx:")
