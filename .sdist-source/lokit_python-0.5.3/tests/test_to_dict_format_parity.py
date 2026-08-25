from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from test_lokit_all_formats import _write_minimal_idml
from test_office import _write_minimal_docx, _write_minimal_pptx

import lokit
from lokit.data.structure import BaseStructure, Data
from lokit.format_detection import LokitInputFormat, detect_format
from lokit.types import DictField

if TYPE_CHECKING:
    from pathlib import Path


def _document() -> BaseStructure:
    return BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        data={"unit": Data(source="Source", target="Cible")},
    )


def _write_streaming_input(path: Path, input_format: LokitInputFormat) -> None:
    document = _document()
    if input_format is LokitInputFormat.LOKIT:
        lokit.write.lokit(document, path)
    elif input_format is LokitInputFormat.LOKIT_JSON:
        lokit.Lokit.from_document(document).output(path)
    elif input_format is LokitInputFormat.TMX:
        lokit.write.tmx(document, path)
    elif input_format is LokitInputFormat.XLIFF:
        lokit.write.xliff(document, path)
    elif input_format is LokitInputFormat.CSV:
        lokit.write.csv(document, path)
    elif input_format is LokitInputFormat.XLSX:
        lokit.write.xlsx(document, path)
    elif input_format is LokitInputFormat.DOCX:
        _write_minimal_docx(path)
    elif input_format is LokitInputFormat.PPTX:
        _write_minimal_pptx(path)
    elif input_format is LokitInputFormat.HTML:
        path.write_text("<!doctype html><html><body><p>Source</p></body></html>", encoding="utf-8")
    elif input_format is LokitInputFormat.PO:
        path.write_text('msgid "Source"\nmsgstr "Cible"\n', encoding="utf-8")
    elif input_format is LokitInputFormat.JSON_I18N:
        path.write_text(json.dumps({"unit": "Source"}), encoding="utf-8")
    elif input_format is LokitInputFormat.IDML:
        _write_minimal_idml(path)
    else:
        raise AssertionError(f"unhandled test input format: {input_format.value}")


@pytest.mark.parametrize(
    ("input_format", "suffix", "expected_source"),
    [
        (LokitInputFormat.LOKIT, ".lokit", "Source"),
        (LokitInputFormat.LOKIT_JSON, ".json", "Source"),
        (LokitInputFormat.TMX, ".tmx", "Source"),
        (LokitInputFormat.XLIFF, ".xliff", "Source"),
        (LokitInputFormat.CSV, ".csv", "Source"),
        (LokitInputFormat.XLSX, ".xlsx", "Source"),
        (LokitInputFormat.DOCX, ".docx", "Hello DOCX"),
        (LokitInputFormat.PPTX, ".pptx", "Hello PPTX"),
        (LokitInputFormat.HTML, ".html", "Source"),
        (LokitInputFormat.PO, ".po", "Source"),
        (LokitInputFormat.JSON_I18N, ".json", "Source"),
        (LokitInputFormat.IDML, ".idml", "Hello IDML"),
    ],
)
def test_to_dict_dispatches_every_streaming_input_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    input_format: LokitInputFormat,
    suffix: str,
    expected_source: str,
) -> None:
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    path = tmp_path / f"{input_format.value}{suffix}"
    _write_streaming_input(path, input_format)

    assert detect_format(path) is input_format
    rows = list(
        lokit.stream.to_dict(
            path,
            source_language="en-US",
            fields=(DictField.SOURCE, DictField.TARGET),
        )
    )

    assert rows
    assert expected_source in {row["source"] for row in rows}
    assert all(set(row) == {"source", "target"} for row in rows)


def test_lokit_output_accepts_pot_suffix(tmp_path: Path) -> None:
    output = tmp_path / "messages.pot"
    instance = lokit.Lokit.from_document(BaseStructure("en", None, {"hello": Data(source="Hello")}))

    instance.output(output)

    assert output.exists()
    assert detect_format(output) is LokitInputFormat.PO
    assert 'msgid "Hello"' in output.read_text(encoding="utf-8")
