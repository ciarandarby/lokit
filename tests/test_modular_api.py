from __future__ import annotations

from typing import TYPE_CHECKING

import lokit
from lokit.data.structure import StreamingStructure

if TYPE_CHECKING:
    from pathlib import Path


def _write_tmx(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4">
  <header creationtool="test" creationtoolversion="1" segtype="sentence"
          o-tmf="test" adminlang="en-US" srclang="en-US" datatype="PlainText"/>
  <body>
    <tu tuid="hello">
      <tuv xml:lang="en-US"><seg>Hello</seg></tuv>
      <tuv xml:lang="fr-FR"><seg>Bonjour</seg></tuv>
    </tu>
  </body>
</tmx>
""",
        encoding="utf-8",
    )


def test_parse_module_routes_to_importers(tmp_path: Path) -> None:
    csv_file = tmp_path / "translations.csv"
    csv_file.write_text("id,source,target\nunit1,Hello,Bonjour\n", encoding="utf-8")

    doc = lokit.parse.csv(str(csv_file), source_locale="en", target_locale="fr", progress=False)

    assert doc.data["unit1"].source == "Hello"
    assert doc.data["unit1"].target == "Bonjour"


def test_stream_module_returns_streaming_structure(tmp_path: Path) -> None:
    tmx_file = tmp_path / "translations.tmx"
    _write_tmx(tmx_file)

    doc = lokit.stream.tmx(str(tmx_file))

    assert isinstance(doc, StreamingStructure)
    assert list(doc.items)


def test_parse_write_module_exports(tmp_path: Path) -> None:
    csv_file = tmp_path / "translations.csv"
    output_file = tmp_path / "translations_out.csv"
    csv_file.write_text("id,source,target\nunit1,Hello,Bonjour\n", encoding="utf-8")

    doc = lokit.parse.csv(str(csv_file), source_locale="en", target_locale="fr", progress=False)
    lokit.parse.write.csv(doc, output_file)

    assert output_file.exists()


def test_fluent_export(tmp_path: Path) -> None:
    csv_file = tmp_path / "translations.csv"
    output_file = tmp_path / "translations_out.csv"
    csv_file.write_text("id,source,target\nunit1,Hello,Bonjour\n", encoding="utf-8")

    doc = lokit.parse.csv(str(csv_file), source_locale="en", target_locale="fr", progress=False)
    doc.export.csv(output_file)

    assert output_file.exists()


def test_database_module_lazy_loads() -> None:
    assert hasattr(lokit.database, "connect")
    assert hasattr(lokit.database, "TranslationMemory")


def test_quick_parse_tmx_to_csv(tmp_path: Path) -> None:
    tmx_file = tmp_path / "translations.tmx"
    output_file = tmp_path / "out.csv"
    _write_tmx(tmx_file)

    stats = lokit.convert.tmx_to_csv(str(tmx_file), str(output_file))

    assert stats.units_read > 0
    assert output_file.exists()
