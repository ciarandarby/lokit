from __future__ import annotations

import json
from pathlib import Path

import pytest

import lokit
from lokit.parsers.csv.extraction import CsvExtractor


def test_single_import_structured_parse_and_export(tmp_path: Path) -> None:
    csv_file = tmp_path / "translations.csv"
    csv_file.write_text(
        "id,source,target,status,comment\n"
        "unit1,Hello,Bonjour,translated,Greeting\n",
        encoding="utf-8",
    )
    output_file = tmp_path / "translations_out.csv"

    document = lokit.parsers.read.csv(
        str(csv_file),
        source_locale="en-US",
        target_locale="fr-FR",
    )
    lokit.exporters.write.csv(document, output_file)

    assert document.data["unit1"].target == "Bonjour"
    assert output_file.exists()
    assert lokit.parsers.extractors.csv is CsvExtractor


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

    document = lokit.parsers.stream.xliff(str(xliff_file))
    items = list(document.items)

    assert document.source_locale == "en-US"
    assert document.target_locale == "fr-FR"
    assert items[0][0] == "hello"
    assert items[0][1].targets["fr-FR"].text == "Bonjour"
    assert lokit.stream_xliff(str(xliff_file)).source_language == "en"


def test_lokit_target_accessors() -> None:
    document = lokit.BaseStructure(
        source_locale="en",
        target_locale=None,
        target_locales=("fr",),
        data={
            "hello": lokit.Data(
                source="Hello",
                targets={"fr": lokit.TargetData(text="Bonjour")},
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
        async for item in lokit.parsers.async_.csv(
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

    output = await lokit.parsers.stream.json(
        csv_file,
        output_dir,
        context=[lokit.LokitJsonContext.SOURCE],
    )

    assert output == output_dir / "translations.jsonl"
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "id": "unit1",
        "source": "Hello",
    }
