from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import lokit
from lokit.io.stream_json import LokitJsonContext
from lokit.parsers.csv.extraction import CsvExtractor

if TYPE_CHECKING:
    from pathlib import Path


def test_root_completion_surface_is_minimal_and_uniform() -> None:
    expected = ["Lokit", "async_", "convert", "database", "parse", "stream", "types", "write"]

    assert lokit.__all__ == expected
    assert dir(lokit) == expected
    assert callable(lokit.Lokit)
    assert callable(lokit.parse.csv)
    assert callable(lokit.async_.parse.csv)
    assert callable(lokit.write.csv)
    assert callable(lokit.async_.write.csv)


def test_single_import_structured_parse_and_export(tmp_path: Path) -> None:
    csv_file = tmp_path / "translations.csv"
    csv_file.write_text(
        "id,source,target,status,comment\nunit1,Hello,Bonjour,translated,Greeting\n",
        encoding="utf-8",
    )
    output_file = tmp_path / "translations_out.csv"

    document = lokit.parse.csv(
        str(csv_file),
        source_locale="en-US",
        target_locale="fr-FR",
    )
    lokit.parse.write.csv(document, output_file)

    assert document.data["unit1"].target == "Bonjour"
    assert output_file.exists()
    assert CsvExtractor.__name__ == "CsvExtractor"


def test_stream_xliff_exposes_lazy_document(tmp_path: Path) -> None:
    xliff_file = tmp_path / "translations.xliff"
    xliff_file.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2">
  <file original="app" source-language="en-US" target-language="fr-FR" datatype="plaintext">
    <body>
      <trans-unit id="hello">
        <source>Hello</source>
        <target state="translated">Bonjour</target>
      </trans-unit>
    </body>
  </file>
</xliff>
""",
        encoding="utf-8",
    )

    document = lokit.stream.xliff(str(xliff_file))
    items = list(document.items)

    assert document.source_locale == "en-US"
    assert document.target_locale == "fr-FR"
    assert items[0][0] == "hello"
    assert items[0][1].targets["fr-FR"].text == "Bonjour"
    assert lokit.stream.xliff(str(xliff_file)).source_language == "en"


def test_lokit_target_accessors() -> None:
    document = lokit.types.BaseStructure(
        source_locale="en",
        target_locale=None,
        target_locales=("fr",),
        data={
            "hello": lokit.types.Data(
                source="Hello",
                targets={"fr": lokit.types.TargetData(text="Bonjour")},
            )
        },
    )
    wrapper = lokit.Lokit.from_document(document)
    target = wrapper.target("hello", "fr")

    assert target is not None
    assert target.text == "Bonjour"
    assert wrapper.targets("hello")["fr"].text == "Bonjour"


@pytest.mark.asyncio
async def test_single_import_structured_async_parse(tmp_path: Path) -> None:
    csv_file = tmp_path / "translations.csv"
    csv_file.write_text("id,source,target\nunit1,Hello,Bonjour\n", encoding="utf-8")

    items = [
        item
        async for item in lokit.parse.async_.csv(
            str(csv_file),
            source_locale="en-US",
            target_locale="fr-FR",
        )
    ]

    assert items[0][0] == "unit1"
    assert items[0][1].source == "Hello"


@pytest.mark.asyncio
async def test_single_import_structured_stream_json(tmp_path: Path) -> None:
    csv_file = tmp_path / "translations.csv"
    output_dir = tmp_path / "json"
    csv_file.write_text("id,source,target\nunit1,Hello,Bonjour\n", encoding="utf-8")

    output = await lokit.stream.async_.json(
        csv_file,
        output_dir,
        context=[LokitJsonContext.SOURCE],
    )

    assert output == output_dir / "translations.jsonl"
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "id": "unit1",
        "source": "Hello",
    }
