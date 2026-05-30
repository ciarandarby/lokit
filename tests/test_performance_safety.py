from __future__ import annotations

import json
from pathlib import Path

import pytest

from lokit import Lokit, LokitJsonContext, TmxParallelOptions, TmxParseMode
from lokit.data.structure import BaseStructure, TranslationStatus
from lokit.exporters.csv import export_csv
from lokit.importers import (
    convert_tmx_to_csv,
    import_tmx,
    import_tmx_batches_async,
    import_tmx_parallel,
    import_xliff,
)
from lokit.io.atomic import atomic_output_path
from lokit.parsers.async_bridge import AsyncExtractionBridge
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
    assert isinstance(extraction, AsyncExtractionBridge)
    assert getattr(extraction, "_queue").maxsize == 4

    first = await anext(extraction)
    assert first[0] == "1"
    await extraction.aclose()


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


def test_tmx_vendor_status_property_is_parsed_generically(tmp_path: Path) -> None:
    source = tmp_path / "source.tmx"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4">
  <header creationtool="test" segtype="sentence" adminlang="en-US" srclang="en-US" datatype="text"/>
  <body>
    <tu tuid="u1">
      <prop type="x-vendor-status">approved</prop>
      <tuv xml:lang="en-US"><seg>Hello</seg></tuv>
      <tuv xml:lang="fr-FR"><seg>Bonjour</seg></tuv>
    </tu>
  </body>
</tmx>
""",
        encoding="utf-8",
    )

    document = import_tmx(str(source), source_language="en-US", target_language="fr-FR")

    assert document.data["u1"].status == TranslationStatus.APPROVED
    assert "property.x_vendor_status" not in document.data["u1"].extensions


def test_tmx_text_mode_skips_metadata_but_keeps_text(tmp_path: Path) -> None:
    source = tmp_path / "source.tmx"
    _write_tmx(source, units=1)

    document = import_tmx(
        str(source),
        source_language="en-US",
        target_language="fr-FR",
        mode=TmxParseMode.TEXT,
    )

    unit = document.data["u0"]
    assert unit.source == "Hello 0"
    assert unit.target == "Bonjour 0"
    assert unit.status == TranslationStatus.UNKNOWN
    assert unit.comments == []


def test_lokit_to_json_streams_selected_context(tmp_path: Path) -> None:
    source = tmp_path / "source.tmx"
    output_dir = tmp_path / "json"
    _write_tmx(source, units=2)

    output = Lokit.to_json(
        source,
        output=output_dir,
        context=[LokitJsonContext.SOURCE, LokitJsonContext.STATUS],
    )

    lines = output.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert output == output_dir / "source.jsonl"
    assert len(lines) == 2
    assert first == {"id": "u0", "source": "Hello 0", "status": "translated"}


@pytest.mark.asyncio
async def test_tmx_batch_import_yields_batches(tmp_path: Path) -> None:
    source = tmp_path / "source.tmx"
    _write_tmx(source, units=3)

    batches = [
        batch
        async for batch in import_tmx_batches_async(
            str(source),
            source_language="en-US",
            target_language="fr-FR",
            batch_size=2,
        )
    ]

    assert [len(batch) for batch in batches] == [2, 1]
    assert batches[0][0][0] == "u0"


def test_tmx_parallel_import_preserves_order_with_bounded_batches(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.tmx"
    _write_tmx(source, units=6)

    document = import_tmx_parallel(
        str(source),
        source_language="en-US",
        target_language="fr-FR",
        options=TmxParallelOptions(
            workers=2,
            batch_units=2,
            batch_bytes=4096,
            max_pending_batches=1,
        ),
    )

    assert list(document.data) == ["u0", "u1", "u2", "u3", "u4", "u5"]
    assert document.data["u5"].target == "Bonjour 5"
