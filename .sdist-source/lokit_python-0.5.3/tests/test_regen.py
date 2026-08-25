from __future__ import annotations

import asyncio
import json
import threading
import time
import tracemalloc
import zipfile
from typing import TYPE_CHECKING, TextIO, cast

import pytest
from lxml import etree

from lokit import Lokit
from lokit.data.structure import Data, StreamingStructure, TargetData
from lokit.exporters import regen as regen_exporter
from lokit.importers import import_csv, import_json_i18n, import_po, import_tmx, import_xliff, import_xlsx
from lokit.parse.write import regen
from lokit.parsers.tmx.xml_utils import find_child

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import TracebackType


class _BoundedTextReader:
    def __init__(self, stream: TextIO, read_sizes: list[int]) -> None:
        self._stream = stream
        self._read_sizes = read_sizes

    def __enter__(self) -> _BoundedTextReader:
        self._stream.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stream.__exit__(exc_type, exc_value, traceback)

    def read(self, size: int = -1) -> str:
        assert size >= 0, "JSON i18n regeneration must never use an unbounded read"
        self._read_sizes.append(size)
        return self._stream.read(size)


def test_regen_proxy_csv_preserves_original_columns(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "target.csv"
    source.write_text("id,en,fr,de,note\none,Hello,Bonjour,Hallo,keep\n", encoding="utf-8")
    document = import_csv(str(source), progress=False)
    document.data["one"].targets["fr"].text = "Salut"
    document.data["one"].targets["de"].text = "Guten Tag"

    document.regen.csv(source, output)

    assert output.read_text(encoding="utf-8") == "id,en,fr,de,note\none,Hello,Salut,Guten Tag,keep\n"


@pytest.mark.asyncio
async def test_regen_csv_async(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "target.csv"
    source.write_text("id,en,fr\none,Hello,Bonjour\n", encoding="utf-8")
    document = import_csv(str(source), progress=False)
    document.data["one"].target = "Salut"

    await regen.csv_async(document, source, output)

    assert output.read_text(encoding="utf-8") == "id,en,fr\none,Hello,Salut\n"


def test_lokit_regen_xliff_updates_targets_from_original(tmp_path: Path) -> None:
    source = tmp_path / "source.xliff"
    output = tmp_path / "target.xliff"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="messages" source-language="en" target-language="fr" datatype="plaintext">
    <body>
      <trans-unit id="one"><source>Hello</source><target>Bonjour</target><note>Keep me</note></trans-unit>
    </body>
  </file>
</xliff>
""",
        encoding="utf-8",
    )
    document = import_xliff(str(source), progress=False)
    document.data["one"].target = "Salut"

    Lokit(document).regen.xliff(source, output)
    regenerated = import_xliff(str(output), progress=False)

    assert regenerated.data["one"].target == "Salut"
    assert regenerated.data["one"].comments[0].context == "Keep me"


def test_regen_tmx_updates_each_target_locale(tmp_path: Path) -> None:
    source = tmp_path / "source.tmx"
    output = tmp_path / "target.tmx"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4">
  <header creationtool="test" segtype="sentence" adminlang="en" srclang="en" datatype="text"/>
  <body>
    <tu tuid="one">
      <tuv xml:lang="en"><seg>Hello</seg></tuv>
      <tuv xml:lang="fr"><seg>Bonjour</seg></tuv>
      <tuv xml:lang="de"><seg>Hallo</seg></tuv>
    </tu>
  </body>
</tmx>
""",
        encoding="utf-8",
    )
    document = import_tmx(str(source), source_language="en", progress=False)
    document.data["one"].targets["fr"].text = "Salut"
    document.data["one"].targets["de"].text = "Guten Tag"

    document.regen.tmx(source, output)
    regenerated = import_tmx(str(output), source_language="en", progress=False)

    assert regenerated.data["one"].targets["fr"].text == "Salut"
    assert regenerated.data["one"].targets["de"].text == "Guten Tag"


def test_regen_xliff_streams_nested_group_units_with_bounded_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    units = 2_000
    source = tmp_path / "large.xliff"
    output = tmp_path / "large.regen.xliff"
    body = "".join(
        f'<trans-unit id="u{index}"><source>Source {index}</source><target>Old {index}</target></trans-unit>'
        for index in range(units)
    )
    source.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE xliff SYSTEM "xliff-core-1.2-strict.dtd">\n'
        '<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" xmlns:vendor="urn:vendor" version="1.2">'
        '<file original="messages" source-language="en" target-language="fr" datatype="plaintext">'
        f'<body><group id="nested" vendor:flag="keep">{body}</group></body>'
        "</file></xliff>",
        encoding="utf-8",
    )

    def items() -> Iterator[tuple[str, Data]]:
        for index in range(units):
            yield f"u{index}", Data(source=f"Source {index}", target=f"New {index}")

    document = StreamingStructure(source_locale="en", target_locale="fr", items=items())

    def reject_whole_tree_parse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("regeneration must not call etree.parse")

    monkeypatch.setattr(etree, "parse", reject_whole_tree_parse)
    tracemalloc.start()
    regen_exporter.regen_xliff(document, source, output)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert peak < 8_000_000
    rendered = output.read_text(encoding="utf-8")
    assert '<!DOCTYPE xliff SYSTEM "xliff-core-1.2-strict.dtd">' in rendered
    assert 'vendor:flag="keep"' in rendered
    assert rendered.count('xmlns="urn:oasis:names:tc:xliff:document:1.2"') == 1
    assert rendered.count('xmlns:vendor="urn:vendor"') == 1
    assert rendered.count("<trans-unit") == units
    assert "<target>New 0</target>" in rendered
    assert f"<target>New {units - 1}</target>" in rendered
    assert rendered.index('id="u0"') < rendered.index(f'id="u{units - 1}"')


def test_regen_tmx_streams_many_units_and_keeps_atomic_output_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    units = 2_000
    source = tmp_path / "large.tmx"
    output = tmp_path / "large.regen.tmx"
    body = "".join(
        f'<tu tuid="u{index}"><tuv xml:lang="en"><seg>Source {index}</seg></tuv>'
        f'<tuv xml:lang="fr"><seg>Old {index}</seg></tuv></tu>'
        for index in range(units)
    )
    source.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE tmx SYSTEM "tmx14.dtd">\n'
        '<tmx version="1.4"><header srclang="en" adminlang="en" datatype="text" segtype="sentence"/>'
        f"<body>{body}</body></tmx>",
        encoding="utf-8",
    )

    def items() -> Iterator[tuple[str, Data]]:
        for index in range(units):
            yield f"u{index}", Data(source=f"Source {index}", target=f"New {index}")

    document = StreamingStructure(source_locale="en", target_locale="fr", items=items())

    def reject_whole_tree_parse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("regeneration must not call etree.parse")

    monkeypatch.setattr(etree, "parse", reject_whole_tree_parse)
    tracemalloc.start()
    regen_exporter.regen_tmx(document, source, output)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert peak < 8_000_000
    assert output.read_bytes().startswith(b"<?xml version=")
    last_target = ""
    for _, element in etree.iterparse(str(output), events=("end",), tag="{*}tu"):
        if element.attrib.get("tuid") == f"u{units - 1}":
            target_tuv = next(
                child for child in element if child.attrib.get(f"{{{regen_exporter.XML_NS}}}lang") == "fr"
            )
            segment = find_child(target_tuv, "seg")
            last_target = segment.text if segment is not None and segment.text is not None else ""
        element.clear()
    assert last_target == f"New {units - 1}"

    def failing_items() -> Iterator[tuple[str, Data]]:
        yield "u0", Data(source="Source 0", target="Changed")
        raise RuntimeError("translation stream failed")

    output.write_text("existing output", encoding="utf-8")
    failing_document = StreamingStructure(source_locale="en", target_locale="fr", items=failing_items())
    with pytest.raises(RuntimeError, match="translation stream failed"):
        regen_exporter.regen_tmx(failing_document, source, output)
    assert output.read_text(encoding="utf-8") == "existing output"


def test_regen_po_preserves_comments_and_updates_msgstr(tmp_path: Path) -> None:
    source = tmp_path / "source.po"
    output = tmp_path / "target.po"
    source.write_text(
        'msgid ""\nmsgstr ""\n"Language: fr\\n"\n\n#. Greeting\nmsgid "Hello"\nmsgstr "Bonjour"\n',
        encoding="utf-8",
    )
    document = import_po(str(source), source_locale="en", target_locale="fr", progress=False)
    document.data["Hello"].target = "Salut"

    regen.po(document, source, output)

    text = output.read_text(encoding="utf-8")
    assert "#. Greeting" in text
    assert 'msgstr "Salut"' in text


def test_regen_json_i18n_reuses_source_shape(tmp_path: Path) -> None:
    source = tmp_path / "en.json"
    target = tmp_path / "fr.json"
    output = tmp_path / "regen.fr.json"
    source.write_text(json.dumps({"common": {"hello": "Hello"}}), encoding="utf-8")
    target.write_text(json.dumps({"common": {"hello": "Bonjour"}}), encoding="utf-8")
    document = import_json_i18n(
        str(source),
        source_locale="en",
        target_locale="fr",
        target_filepath=str(target),
        progress=False,
    )
    document.data["common.hello"].target = "Salut"

    regen.json_i18n(document, source, output)

    assert json.loads(output.read_text(encoding="utf-8")) == {"common": {"hello": "Salut"}}


def test_regen_json_i18n_streams_scalars_arrays_order_and_escaping(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    output = tmp_path / "output.json"
    source.write_text(
        '{"\\u006deta":1e+02,"common":{"hello":"Hello"},'
        '"array":["line\\n",false,null,{"x":"y"}],"escaped":"A\\u0020B"}',
        encoding="utf-8",
    )
    document = StreamingStructure(
        source_locale="en",
        target_locale="fr",
        items=iter(
            (
                (
                    "common.hello",
                    Data(
                        source="Hello",
                        target="Salut",
                        extensions={"json_path": '["common", "hello"]'},
                    ),
                ),
            )
        ),
    )

    regen_exporter.regen_json_i18n(document, source, output)

    rendered = output.read_text(encoding="utf-8")
    decoded = json.loads(rendered)
    assert list(decoded) == ["meta", "common", "array", "escaped"]
    assert decoded == {
        "meta": 100.0,
        "common": {"hello": "Salut"},
        "array": ["line\n", False, None, {"x": "y"}],
        "escaped": "A B",
    }
    assert '"\\u006deta"' in rendered
    assert "1e+02" in rendered
    assert '"line\\n"' in rendered
    assert '"A\\u0020B"' in rendered


def test_regen_json_i18n_multilingual_clone_consumes_stream_once(tmp_path: Path) -> None:
    source = tmp_path / "messages.json"
    output = tmp_path / "regenerated.json"
    source.write_text(
        json.dumps(
            {
                "metadata": {"version": 1},
                "en": {"greeting": "Hello", "source_only": ["Keep", 2, True]},
                "fr": {"greeting": "Bonjour", "fr_only": "Conserver"},
                "tail": None,
            }
        ),
        encoding="utf-8",
    )
    yielded: list[str] = []

    def items() -> Iterator[tuple[str, Data]]:
        yielded.append("greeting")
        yield (
            "greeting",
            Data(
                source="Hello",
                targets={
                    "fr": TargetData(text="Salut"),
                    "de": TargetData(text="Hallo"),
                },
                extensions={"json_path": '["greeting"]'},
            ),
        )

    document = StreamingStructure(
        source_locale="en",
        target_locale=None,
        items=items(),
        target_locales=("fr", "de"),
    )

    regen_exporter.regen_json_i18n(document, source, output)

    decoded = json.loads(output.read_text(encoding="utf-8"))
    assert yielded == ["greeting"]
    assert list(decoded) == ["metadata", "en", "fr", "tail", "de"]
    assert decoded["en"] == {"greeting": "Hello", "source_only": ["Keep", 2, True]}
    assert decoded["fr"] == {"greeting": "Salut", "fr_only": "Conserver"}
    assert decoded["de"] == {"greeting": "Hallo", "source_only": ["Keep", 2, True]}


def test_regen_json_i18n_matches_canonical_locale_root_spellings(tmp_path: Path) -> None:
    source = tmp_path / "messages.json"
    output = tmp_path / "regenerated.json"
    source.write_text(
        '{"en_US":{"greeting":"Hello"},"fr_FR":{"greeting":"Bonjour"}}',
        encoding="utf-8",
    )
    document = StreamingStructure(
        source_locale="en-US",
        target_locale=None,
        items=iter(
            (
                (
                    "greeting",
                    Data(
                        source="Hello",
                        targets={
                            "fr-FR": TargetData(text="Salut"),
                            "de-DE": TargetData(text="Hallo"),
                        },
                    ),
                ),
            )
        ),
        target_locales=("fr-FR", "de-DE"),
    )

    regen_exporter.regen_json_i18n(document, source, output)

    decoded = json.loads(output.read_text(encoding="utf-8"))
    assert list(decoded) == ["en_US", "fr_FR", "de-DE"]
    assert decoded["fr_FR"] == {"greeting": "Salut"}
    assert decoded["de-DE"] == {"greeting": "Hallo"}


def test_regen_json_i18n_has_bounded_memory_reads_and_atomic_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    units = 12_000
    source = tmp_path / "large.json"
    output = tmp_path / "large.regenerated.json"
    with source.open("w", encoding="utf-8") as source_stream:
        source_stream.write("{")
        for index in range(units):
            if index:
                source_stream.write(",")
            source_stream.write(json.dumps(f"message_{index}"))
            source_stream.write(":")
            source_stream.write(json.dumps(f"Old {index}"))
        source_stream.write("}")

    def items() -> Iterator[tuple[str, Data]]:
        for index in range(units):
            yield (
                f"message_{index}",
                Data(
                    source=f"Old {index}",
                    target=f"New {index}",
                    extensions={"json_path": f'["message_{index}"]'},
                ),
            )

    read_sizes: list[int] = []
    original_open = type(source).open

    def guarded_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> TextIO | _BoundedTextReader:
        stream = cast("TextIO", original_open(path, mode, buffering, encoding, errors, newline))
        if path == source and "r" in mode:
            return _BoundedTextReader(stream, read_sizes)
        return stream

    monkeypatch.setattr(type(source), "open", guarded_open)
    document = StreamingStructure(source_locale="en", target_locale="fr", items=items())
    tracemalloc.start()
    started = time.perf_counter()
    regen_exporter.regen_json_i18n(document, source, output)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert peak < 8_000_000
    assert elapsed < 15
    assert read_sizes and max(read_sizes) <= 64 * 1024
    decoded = json.loads(output.read_text(encoding="utf-8"))
    assert len(decoded) == units
    assert decoded["message_0"] == "New 0"
    assert decoded[f"message_{units - 1}"] == f"New {units - 1}"

    def failing_items() -> Iterator[tuple[str, Data]]:
        yield "message_0", Data(source="Old 0", target="Changed")
        raise RuntimeError("translation stream failed")

    output.write_text("existing output", encoding="utf-8")
    failing_document = StreamingStructure(source_locale="en", target_locale="fr", items=failing_items())
    with pytest.raises(RuntimeError, match="translation stream failed"):
        regen_exporter.regen_json_i18n(failing_document, source, output)
    assert output.read_text(encoding="utf-8") == "existing output"


def test_regen_json_i18n_limits_fail_before_consuming_stream(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    output = tmp_path / "output.json"
    source.write_text('{"message":"Hello"}', encoding="utf-8")
    consumed = False

    def items() -> Iterator[tuple[str, Data]]:
        nonlocal consumed
        consumed = True
        yield "message", Data(source="Hello", target="Bonjour")

    document = StreamingStructure(source_locale="en", target_locale="fr", items=items())

    with pytest.raises(ValueError, match="indentation"):
        regen_exporter.regen_json_i18n(document, source, output, indent=33)

    assert not consumed
    assert not output.exists()


@pytest.mark.asyncio
async def test_regen_json_i18n_async(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    output = tmp_path / "output.json"
    source.write_text('{"message":"Hello"}', encoding="utf-8")
    document = StreamingStructure(
        source_locale="en",
        target_locale="fr",
        items=iter((("message", Data(source="Hello", target="Bonjour")),)),
    )

    await regen.json_i18n_async(document, source, output)

    assert json.loads(output.read_text(encoding="utf-8")) == {"message": "Bonjour"}


@pytest.mark.asyncio
async def test_regen_json_i18n_async_cancellation_quiesces_worker(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    output = tmp_path / "output.json"
    source.write_text('{"message":"Hello"}', encoding="utf-8")
    output.write_text("existing output", encoding="utf-8")
    started = threading.Event()
    closed = threading.Event()

    def items() -> Iterator[tuple[str, Data]]:
        try:
            started.set()
            time.sleep(0.05)
            yield "message", Data(source="Hello", target="Bonjour")
        finally:
            closed.set()

    document = StreamingStructure(source_locale="en", target_locale="fr", items=items())
    task = asyncio.create_task(regen.json_i18n_async(document, source, output))
    assert await asyncio.to_thread(started.wait, 1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed.is_set()
    assert output.read_text(encoding="utf-8") == "existing output"


def test_regen_xlsx_rewrites_target_cells_in_original_package(tmp_path: Path) -> None:
    from rustpy_xlsxwriter import FastExcel

    source = tmp_path / "source.xlsx"
    output = tmp_path / "target.xlsx"
    FastExcel(str(source), autofit=False).sheet(
        "Sheet1",
        [{"id": "one", "en": "Hello", "fr": "Bonjour", "note": "keep"}],
    ).save()
    document = import_xlsx(str(source), progress=False)
    document.data["one"].target = "Salut"

    document.regen.xlsx(source, output)
    regenerated = import_xlsx(str(output), progress=False)

    assert regenerated.data["one"].target == "Salut"
    with zipfile.ZipFile(output, "r") as archive:
        assert "xl/workbook.xml" in archive.namelist()


def test_regen_csv_has_bounded_memory_and_runtime(tmp_path: Path) -> None:
    source = tmp_path / "large.csv"
    output = tmp_path / "large.out.csv"
    rows = ["id,en,fr"]
    rows.extend(f"u{i},Hello {i},Bonjour {i}" for i in range(1000))
    source.write_text("\n".join(rows) + "\n", encoding="utf-8")
    document = import_csv(str(source), progress=False)
    for unit_id, unit in document.data.items():
        unit.target = f"{unit_id} translated"

    tracemalloc.start()
    start = time.perf_counter()
    regen.csv(document, source, output)
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert "u999 translated" in output.read_text(encoding="utf-8")
    assert peak < 5_000_000
    assert elapsed < 5.0
