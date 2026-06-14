from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from test_office import DOCX_FIXTURE, _write_minimal_docx

import lokit

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def docx_fixture(tmp_path: Path) -> Path:
    if DOCX_FIXTURE.exists():
        return DOCX_FIXTURE
    path = tmp_path / "minimal.docx"
    _write_minimal_docx(path)
    return path


def test_docx_parse_output_has_office_extensions(docx_fixture: Path) -> None:
    doc = lokit.parse.docx(docx_fixture, source_locale="en", progress=False)

    assert doc.data
    for unit_id, data in doc.data.items():
        assert unit_id.startswith("docx:")
        assert data.source
        assert data.extensions["office.format"] == "docx"
        assert data.extensions["office.part"].startswith("word/")
        assert "office.source_fingerprint" in data.extensions


def test_docx_stream_and_bytes(docx_fixture: Path) -> None:
    streamed = lokit.stream.docx(docx_fixture, source_locale="en")
    from_bytes = lokit.parse.docx(docx_fixture.read_bytes(), source_locale="en", progress=False)

    assert list(streamed.items)
    assert from_bytes.data


@pytest.mark.asyncio
async def test_docx_async(docx_fixture: Path) -> None:
    items = [item async for item in lokit.stream.async_.docx(docx_fixture, source_locale="en")]

    assert items
    assert items[0][0].startswith("docx:")
