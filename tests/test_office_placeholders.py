from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING, Literal

import pytest
from test_office import _write_minimal_docx, _write_minimal_pptx

from lokit.data.tag_types import TieType
from lokit.office import (
    import_docx,
    import_docx_async,
    import_pptx,
    import_pptx_async,
    stream_docx,
    stream_pptx,
)
from lokit.placeholders import PlaceholderSyntax

if TYPE_CHECKING:
    from pathlib import Path

    from lokit.data.structure import BaseStructure, Data, StreamingStructure

OfficeFormat = Literal["docx", "pptx"]


@pytest.mark.parametrize("file_format", ["docx", "pptx"])
def test_office_materialized_import_projects_placeholders_by_default(
    tmp_path: Path,
    file_format: OfficeFormat,
) -> None:
    source = _placeholder_package(tmp_path, file_format, "Hello {customer}")

    document = _import_document(source, file_format)

    _assert_projected(_first_data(document))


@pytest.mark.parametrize("file_format", ["docx", "pptx"])
def test_office_stream_projects_placeholders_without_losing_native_items(
    tmp_path: Path,
    file_format: OfficeFormat,
) -> None:
    source = _placeholder_package(tmp_path, file_format, "Hello {customer}")

    with _stream_document(source, file_format) as document:
        assert getattr(document.items, "_lokit_native_office", False) is True
        _assert_projected(next(iter(document.items))[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("file_format", ["docx", "pptx"])
async def test_office_async_import_projects_placeholders_by_default(
    tmp_path: Path,
    file_format: OfficeFormat,
) -> None:
    source = _placeholder_package(tmp_path, file_format, "Hello {customer}")

    if file_format == "docx":
        items = [item async for item in import_docx_async(source)]
    else:
        items = [item async for item in import_pptx_async(source)]

    assert items
    _assert_projected(items[0][1])


@pytest.mark.parametrize("file_format", ["docx", "pptx"])
def test_office_placeholder_projection_can_be_disabled(
    tmp_path: Path,
    file_format: OfficeFormat,
) -> None:
    source = _placeholder_package(tmp_path, file_format, "Hello {customer}")

    if file_format == "docx":
        document = import_docx(
            source,
            progress=False,
            runtime_placeholders=False,
            inline_placeholders=False,
        )
    else:
        document = import_pptx(
            source,
            progress=False,
            runtime_placeholders=False,
            inline_placeholders=False,
        )

    data = _first_data(document)
    assert data.source == "Hello {customer}"
    assert data.tags is None


@pytest.mark.parametrize("file_format", ["docx", "pptx"])
def test_office_import_forwards_explicit_placeholder_syntaxes(
    tmp_path: Path,
    file_format: OfficeFormat,
) -> None:
    source = _placeholder_package(tmp_path, file_format, "Hello $customer")
    syntaxes = [PlaceholderSyntax.SHELL_PARAMETER]

    if file_format == "docx":
        document = import_docx(source, progress=False, placeholder_syntaxes=syntaxes)
    else:
        document = import_pptx(source, progress=False, placeholder_syntaxes=syntaxes)

    data = _first_data(document)
    assert data.source == "Hello {LOKIT_P1}"
    assert data.tags is not None
    tag = next(iter(data.tags.source_tag_map.values()))
    assert tag.original_text == "$customer"
    assert tag.attributes["lokit.placeholder.syntax"] == PlaceholderSyntax.SHELL_PARAMETER


def _placeholder_package(tmp_path: Path, file_format: OfficeFormat, text: str) -> Path:
    path = tmp_path / f"placeholder.{file_format}"
    if file_format == "docx":
        _write_minimal_docx(path)
        member = "word/document.xml"
        original = "Hello DOCX"
    else:
        _write_minimal_pptx(path)
        member = "ppt/slides/slide1.xml"
        original = "Hello PPTX"
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members[member] = members[member].replace(original.encode(), text.encode())
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return path


def _import_document(path: Path, file_format: OfficeFormat) -> BaseStructure:
    if file_format == "docx":
        return import_docx(path, progress=False)
    return import_pptx(path, progress=False)


def _stream_document(path: Path, file_format: OfficeFormat) -> StreamingStructure:
    if file_format == "docx":
        return stream_docx(path)
    return stream_pptx(path)


def _first_data(document: BaseStructure) -> Data:
    return next(iter(document.data.values()))


def _assert_projected(data: Data) -> None:
    assert data.source == "Hello {LOKIT_P1}"
    assert data.tags is not None
    tag = next(iter(data.tags.source_tag_map.values()))
    assert tag.type is TieType.PLACEHOLDER_STANDALONE
    assert tag.original_text == "{customer}"
    assert tag.attributes["lokit.placeholder.kind"] == "runtime"
