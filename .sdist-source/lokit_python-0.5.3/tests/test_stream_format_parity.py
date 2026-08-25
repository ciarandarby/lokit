from __future__ import annotations

import ast
import json
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from rustpy_xlsxwriter import FastExcel

import lokit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from lokit.data.structure import Data


async def _collect(items: AsyncIterator[tuple[str, Data]]) -> list[tuple[str, Data]]:
    return [item async for item in items]


def test_every_input_format_is_attached_to_each_public_read_surface() -> None:
    expected = {
        "csv",
        "docx",
        "html",
        "idml",
        "json_i18n",
        "lokit",
        "lokit_json",
        "po",
        "pptx",
        "tmx",
        "xliff",
        "xlsx",
    }
    surfaces = (
        lokit.parse,
        lokit.parse.async_,
        lokit.stream,
        lokit.stream.async_,
    )

    for surface in surfaces:
        assert expected <= set(surface.__all__)
        assert all(callable(getattr(surface, format_name)) for format_name in expected)


def _write_idml(path: Path) -> None:
    story = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Story xmlns:idPkg="http://ns.adobe.com/AdobeInDesign/idms/1.0/">'
        "<ParagraphStyleRange><CharacterStyleRange><Content>Hello IDML</Content>"
        "</CharacterStyleRange></ParagraphStyleRange></Story>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Stories/Story_u1.xml", story)


def _function_arguments(path: Path, function_name: str) -> str:
    return ast.dump(_function_node(path, function_name).args, include_attributes=False)


def _function_node(path: Path, function_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    module = ast.parse(path.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return node
    raise AssertionError(f"Function {function_name!r} not found in {path}")


def _positional_argument_names(path: Path, function_name: str) -> list[str]:
    arguments = _function_node(path, function_name).args
    return [argument.arg for argument in (*arguments.posonlyargs, *arguments.args)]


def _assert_forwards_keyword(path: Path, function_name: str, keyword: str) -> None:
    function = _function_node(path, function_name)
    assert any(
        item.arg == keyword and isinstance(item.value, ast.Name) and item.value.id == keyword
        for call in ast.walk(function)
        if isinstance(call, ast.Call)
        for item in call.keywords
    )


def test_new_stream_signatures_are_mirrored() -> None:
    source_root = Path(__file__).parents[1] / "src" / "lokit" / "stream"
    sync_path = source_root / "__init__.py"
    async_path = source_root / "async_.py"

    for function_name in ("csv", "xlsx", "html", "json_i18n", "idml"):
        assert _function_arguments(sync_path, function_name) == _function_arguments(async_path, function_name)


def test_tmx_parameter_order_is_uniform_across_public_surfaces() -> None:
    source_root = Path(__file__).parents[1] / "src" / "lokit"
    surfaces = (
        (source_root / "parse" / "__init__.py", "tmx"),
        (source_root / "parse" / "async_.py", "tmx"),
        (source_root / "stream" / "__init__.py", "tmx"),
        (source_root / "stream" / "async_.py", "tmx"),
        (source_root / "importers.py", "import_tmx"),
        (source_root / "importers.py", "import_tmx_async"),
        (source_root / "importers.py", "stream_tmx"),
    )
    expected = ["filepath", "source_language", "target_language", "domain", "mode"]

    for path, function_name in surfaces:
        assert _positional_argument_names(path, function_name) == expected


def test_po_mode_is_uniformly_keyword_only() -> None:
    source_root = Path(__file__).parents[1] / "src" / "lokit"
    surfaces = (
        (source_root / "parse" / "__init__.py", "po"),
        (source_root / "parse" / "async_.py", "po"),
        (source_root / "stream" / "__init__.py", "po"),
        (source_root / "stream" / "async_.py", "po"),
        (source_root / "importers.py", "import_po"),
        (source_root / "importers.py", "import_po_async"),
        (source_root / "importers.py", "stream_po"),
    )

    for path, function_name in surfaces:
        arguments = _function_node(path, function_name).args
        assert "mode" not in [argument.arg for argument in (*arguments.posonlyargs, *arguments.args)]
        assert "mode" in [argument.arg for argument in arguments.kwonlyargs]


def test_docx_options_match_pptx_and_are_forwarded() -> None:
    source_root = Path(__file__).parents[1] / "src" / "lokit"
    mirrored_pairs = (
        (source_root / "parse" / "__init__.py", "docx", "pptx"),
        (source_root / "parse" / "async_.py", "docx", "pptx"),
        (source_root / "stream" / "__init__.py", "docx", "pptx"),
        (source_root / "stream" / "async_.py", "docx", "pptx"),
        (source_root / "importers.py", "import_docx", "import_pptx"),
        (source_root / "importers.py", "stream_docx", "stream_pptx"),
        (source_root / "importers.py", "import_docx_async", "import_pptx_async"),
    )

    for path, docx_name, pptx_name in mirrored_pairs:
        assert _function_arguments(path, docx_name) == _function_arguments(path, pptx_name)
        _assert_forwards_keyword(path, docx_name, "options")


@pytest.mark.asyncio
async def test_new_stream_surfaces_yield_sync_async_parity(tmp_path: Path) -> None:
    csv_path = tmp_path / "messages.csv"
    csv_path.write_text("id,en,fr\ngreeting,Hello,Bonjour\n", encoding="utf-8")
    csv_document = lokit.stream.csv(str(csv_path))
    assert csv_document.source_locale == "en"
    assert csv_document.target_locale == "fr"
    assert list(csv_document.items) == await _collect(lokit.stream.async_.csv(str(csv_path)))

    xlsx_path = tmp_path / "messages.xlsx"
    FastExcel(str(xlsx_path), autofit=False).sheet(
        "Sheet1",
        [{"id": "greeting", "en": "Hello", "fr": "Bonjour"}],
    ).save()
    xlsx_document = lokit.stream.xlsx(str(xlsx_path))
    assert xlsx_document.source_locale == "en"
    assert xlsx_document.target_locale == "fr"
    assert list(xlsx_document.items) == await _collect(lokit.stream.async_.xlsx(str(xlsx_path)))

    html_path = tmp_path / "index.html"
    html_path.write_text('<!doctype html><html lang="en"><body><p>Hello HTML</p></body></html>', encoding="utf-8")
    html_document = lokit.stream.html(str(html_path))
    assert html_document.source_locale == "en"
    assert list(html_document.items) == await _collect(lokit.stream.async_.html(str(html_path)))

    json_path = tmp_path / "messages.json"
    json_path.write_text(
        json.dumps({"en": {"greeting": "Hello"}, "fr": {"greeting": "Bonjour"}}),
        encoding="utf-8",
    )
    json_document = lokit.stream.json_i18n(str(json_path), source_locale="en")
    assert json_document.source_locale == "en"
    assert json_document.target_locales == ("fr",)
    assert list(json_document.items) == await _collect(
        lokit.stream.async_.json_i18n(str(json_path), source_locale="en")
    )

    idml_path = tmp_path / "document.idml"
    _write_idml(idml_path)
    idml_document = lokit.stream.idml(str(idml_path), source_locale="en", target_locale="fr")
    assert idml_document.source_language == "en"
    assert idml_document.target_language == "fr"
    assert list(idml_document.items) == await _collect(
        lokit.stream.async_.idml(str(idml_path), source_locale="en", target_locale="fr")
    )
