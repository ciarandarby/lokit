from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from lokit import Lokit
from lokit.data.structure import BaseStructure
from lokit.exporters.csv import export_csv
from lokit.importers import convert_tmx_to_csv, import_tmx, import_xliff
from lokit.io.atomic import atomic_output_path
from lokit.parsers.csv.extraction import CsvExtractor


def _write_tmx(path: Path, units: int = 3) -> None:
    body = "\n".join(
        f"""
        <tu tuid="u{i}">
          <prop type="x-status">translated</prop>
          <tuv xml:lang="en-US"><seg>Hello <bpt i="1" type="bold">&lt;b&gt;</bpt>{i}<ept i="1">&lt;/b&gt;</ept></seg></tuv>
          <tuv xml:lang="fr-FR"><seg>Bonjour <bpt i="1" type="bold">&lt;b&gt;</bpt>{i}<ept i="1">&lt;/b&gt;</ept></seg></tuv>
        </tu>
        """
        for i in range(units)
    )
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4">
  <header creationtool="test" segtype="sentence" adminlang="en-US" srclang="en-US" datatype="text"/>
  <body>{body}</body>
</tmx>
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_async_extractor_uses_bounded_bridge(tmp_path: Path) -> None:
    csv_file = tmp_path / "sample.csv"
    csv_file.write_text("id,source,target\n1,Hello,Bonjour\n2,Bye,Salut\n", encoding="utf-8")

    extraction = CsvExtractor(str(csv_file)).extract_async()
    assert getattr(extraction, "_queue").maxsize == 1000

    first = await anext(extraction)
    assert first[0] == "1"
    await cast(Any, extraction).aclose()


def test_atomic_csv_export_leaves_existing_file_on_failure(
    sample_document: BaseStructure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "translations.csv"
    output.write_text("original\n", encoding="utf-8")

    def fail_after_header(self: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("csv.DictWriter.writeheader", fail_after_header)
    with pytest.raises(OSError):
        export_csv(sample_document, output)

    assert output.read_text(encoding="utf-8") == "original\n"
    assert not list(tmp_path.glob(".translations.csv.*.tmp"))


def test_atomic_output_without_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "windows-safe.txt"
    monkeypatch.delattr("os.O_DIRECTORY", raising=False)

    with atomic_output_path(output, "w") as handle:
        handle.write("ok")

    assert output.read_text(encoding="utf-8") == "ok"


def test_misnamed_xml_dispatch_and_direct_import_validation(tmp_path: Path) -> None:
    misnamed = tmp_path / "memory.xliff"
    _write_tmx(misnamed, units=1)

    parsed = Lokit.parse(misnamed)
    assert parsed.unit("u0").source == "Hello 0"

    with pytest.raises(ValueError, match="Expected XLIFF XML root"):
        import_xliff(str(misnamed))


def test_streaming_tmx_conversion_reports_stats(tmp_path: Path) -> None:
    source = tmp_path / "source.tmx"
    target = tmp_path / "target.csv"
    _write_tmx(source, units=5)

    stats = convert_tmx_to_csv(str(source), str(target), source_language="en-US", target_language="fr-FR")

    assert stats.units_read == 5
    assert stats.units_written == 5
    assert stats.input_bytes > 0
    assert stats.output_bytes == target.stat().st_size
    assert "u4" in target.read_text(encoding="utf-8")


def test_tmx_inline_codes_roundtrip_original_markup(tmp_path: Path) -> None:
    source = tmp_path / "source.tmx"
    output = tmp_path / "output.tmx"
    _write_tmx(source, units=1)

    document = import_tmx(str(source), source_language="en-US", target_language="fr-FR")
    Lokit(document).output(output)
    exported = output.read_text(encoding="utf-8")

    assert '<bpt i="0" type="bold">&lt;b&gt;</bpt>' in exported
    reparsed = import_tmx(str(output), source_language="en-US", target_language="fr-FR")
    assert reparsed.data["u0"].source == "Hello 0"
