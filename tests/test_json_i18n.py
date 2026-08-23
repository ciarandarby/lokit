from __future__ import annotations

import json
from typing import TYPE_CHECKING, TextIO, cast

import pytest

from lokit.data.structure import BaseStructure, Data, StreamingStructure, TargetData, TranslationStatus
from lokit.exporters.json_i18n import export_json_i18n, export_json_i18n_async
from lokit.importers import import_json_i18n, import_json_i18n_async, stream_json_i18n

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
        assert size >= 0, "JSON i18n streaming must never use an unbounded read"
        self._read_sizes.append(size)
        return self._stream.read(size)


def test_json_i18n_flat_roundtrip(tmp_path: Path) -> None:
    source_file = tmp_path / "en.json"
    target_file = tmp_path / "fr.json"

    source_data = {
        "greeting": "Hello",
        "nested.key": "This is nested key",
    }
    target_data = {
        "greeting": "Bonjour",
        "nested.key": "C'est une clé imbriquée",
    }

    source_file.write_text(json.dumps(source_data), encoding="utf-8")
    target_file.write_text(json.dumps(target_data), encoding="utf-8")
    imported = import_json_i18n(
        str(source_file),
        source_locale="en",
        target_locale="fr",
        target_filepath=str(target_file),
    )

    assert imported.source_locale == "en"
    assert imported.target_locale == "fr"

    assert "greeting" in imported.data
    assert imported.data["greeting"].source == "Hello"
    assert imported.data["greeting"].target == "Bonjour"
    assert imported.data["greeting"].status == TranslationStatus.TRANSLATED

    assert "nested.key" in imported.data
    assert imported.data["nested.key"].source == "This is nested key"
    assert imported.data["nested.key"].target == "C'est une clé imbriquée"

    exported_flat = tmp_path / "fr_flat.json"
    export_json_i18n(imported, exported_flat, nested=False)

    with exported_flat.open("r", encoding="utf-8") as f:
        exported_data = json.load(f)

    assert exported_data == target_data


def test_json_i18n_nested_roundtrip(tmp_path: Path) -> None:
    source_file = tmp_path / "en_nested.json"
    target_file = tmp_path / "fr_nested.json"

    source_data = {
        "common": {
            "greeting": "Hello",
            "button": {
                "save": "Save",
            },
        }
    }
    target_data = {
        "common": {
            "greeting": "Bonjour",
            "button": {
                "save": "Sauvegarder",
            },
        }
    }

    source_file.write_text(json.dumps(source_data), encoding="utf-8")
    target_file.write_text(json.dumps(target_data), encoding="utf-8")
    imported = import_json_i18n(
        str(source_file),
        source_locale="en",
        target_locale="fr",
        target_filepath=str(target_file),
    )

    assert "common.greeting" in imported.data
    assert imported.data["common.greeting"].source == "Hello"
    assert imported.data["common.greeting"].target == "Bonjour"

    assert "common.button.save" in imported.data
    assert imported.data["common.button.save"].source == "Save"
    assert imported.data["common.button.save"].target == "Sauvegarder"
    exported_nested = tmp_path / "fr_nested_exported.json"
    export_json_i18n(imported, exported_nested, nested=True)

    with exported_nested.open("r", encoding="utf-8") as f:
        exported_data = json.load(f)

    assert exported_data == target_data


def test_json_i18n_import_multiple_target_files(tmp_path: Path) -> None:
    source_file = tmp_path / "en.json"
    fr_file = tmp_path / "fr.json"
    de_file = tmp_path / "de.json"
    source_file.write_text(json.dumps({"greeting": "Hello"}), encoding="utf-8")
    fr_file.write_text(json.dumps({"greeting": "Bonjour"}), encoding="utf-8")
    de_file.write_text(json.dumps({"greeting": "Hallo"}), encoding="utf-8")

    imported = import_json_i18n(
        str(source_file),
        source_locale="en",
        target_filepaths={"fr": str(fr_file), "de": str(de_file)},
        progress=False,
    )

    assert imported.target_locale is None
    assert imported.target_locales == ("fr", "de")
    assert imported.data["greeting"].target is None
    assert imported.data["greeting"].targets["fr"].text == "Bonjour"
    assert imported.data["greeting"].targets["de"].text == "Hallo"


def test_json_i18n_import_multilingual_root(tmp_path: Path) -> None:
    multilingual_file = tmp_path / "messages.json"
    multilingual_file.write_text(
        json.dumps(
            {
                "en": {"greeting": "Hello"},
                "fr": {"greeting": "Bonjour"},
                "de": {"greeting": "Hallo"},
            }
        ),
        encoding="utf-8",
    )

    imported = import_json_i18n(str(multilingual_file), source_locale="en", progress=False)

    assert imported.source_locale == "en"
    assert imported.target_locales == ("fr", "de")
    assert imported.data["greeting"].targets["fr"].text == "Bonjour"
    assert imported.data["greeting"].targets["de"].text == "Hallo"


def test_json_i18n_export_multitarget_directory(tmp_path: Path) -> None:
    source_file = tmp_path / "en.json"
    fr_file = tmp_path / "fr.json"
    de_file = tmp_path / "de.json"
    output_dir = tmp_path / "targets"
    source_file.write_text(json.dumps({"greeting": "Hello"}), encoding="utf-8")
    fr_file.write_text(json.dumps({"greeting": "Bonjour"}), encoding="utf-8")
    de_file.write_text(json.dumps({"greeting": "Hallo"}), encoding="utf-8")

    imported = import_json_i18n(
        str(source_file),
        source_locale="en",
        target_filepaths={"fr": str(fr_file), "de": str(de_file)},
        progress=False,
    )
    export_json_i18n(imported, output_dir)

    assert json.loads((output_dir / "fr.json").read_text(encoding="utf-8")) == {"greeting": "Bonjour"}
    assert json.loads((output_dir / "de.json").read_text(encoding="utf-8")) == {"greeting": "Hallo"}


def test_json_i18n_export_multitarget_consumes_stream_once(tmp_path: Path) -> None:
    yielded: list[str] = []

    def items() -> Iterator[tuple[str, Data]]:
        units = (
            (
                "greeting",
                Data(
                    source="Hello",
                    targets={
                        "fr": TargetData(text="Bonjour"),
                        "de": TargetData(text="Hallo"),
                    },
                    extensions={"json_path": '["common", "greeting"]'},
                ),
            ),
            (
                "farewell",
                Data(
                    source="Goodbye",
                    targets={
                        "fr": TargetData(text="Au revoir"),
                        "de": TargetData(text="Auf Wiedersehen"),
                    },
                    extensions={"json_path": '["common", "farewell"]'},
                ),
            ),
        )
        for unit_id, unit in units:
            yielded.append(unit_id)
            yield unit_id, unit

    document = StreamingStructure(
        source_locale="en",
        target_locale=None,
        items=items(),
        target_locales=("fr", "de"),
    )
    output_dir = tmp_path / "streamed-targets"
    export_json_i18n(document, output_dir, nested=True)

    assert yielded == ["greeting", "farewell"]
    assert json.loads((output_dir / "fr.json").read_text(encoding="utf-8")) == {
        "common": {"greeting": "Bonjour", "farewell": "Au revoir"}
    }
    assert json.loads((output_dir / "de.json").read_text(encoding="utf-8")) == {
        "common": {"greeting": "Hallo", "farewell": "Auf Wiedersehen"}
    }


def test_json_i18n_multitarget_rejects_unsafe_locale_before_consuming(tmp_path: Path) -> None:
    consumed = False

    def items() -> Iterator[tuple[str, Data]]:
        nonlocal consumed
        consumed = True
        yield "message", Data(source="Message")

    document = StreamingStructure(
        source_locale="en",
        target_locale=None,
        items=items(),
        target_locales=("fr", "../outside"),
    )

    with pytest.raises(ValueError, match="Unsafe target locale"):
        export_json_i18n(document, tmp_path / "output")

    assert not consumed
    assert not (tmp_path / "outside.json").exists()


def test_json_i18n_nested_export_preserves_later_assignment_semantics(tmp_path: Path) -> None:
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        data={
            "first": Data(source="discarded", extensions={"json_path": '["a"]'}),
            "second": Data(source="nested", extensions={"json_path": '["a", "b"]'}),
            "third": Data(source="discarded subtree", extensions={"json_path": '["c", "d"]'}),
            "fourth": Data(source="scalar wins", extensions={"json_path": '["c"]'}),
        },
    )
    output = tmp_path / "nested.json"

    export_json_i18n(document, output, nested=True)

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "a": {"b": "nested"},
        "c": "scalar wins",
    }


def test_json_i18n_export_chunked_string_escaping(tmp_path: Path) -> None:
    boundary_text = ("x" * (64 * 1024 - 1)) + '"\\\n\t' + ("é" * (64 * 1024 + 3))
    document = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data={"message": Data(source="fallback", target=boundary_text)},
    )
    output = tmp_path / "chunked.json"

    export_json_i18n(document, output, nested=False)

    assert json.loads(output.read_text(encoding="utf-8")) == {"message": boundary_text}


def test_json_i18n_stream_uses_only_bounded_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_file = tmp_path / "en.json"
    source_file.write_text(
        json.dumps({f"message_{index}": f"Translation {index}" for index in range(12_000)}),
        encoding="utf-8",
    )
    read_sizes: list[int] = []
    original_open = type(source_file).open

    def guarded_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> TextIO | _BoundedTextReader:
        stream = cast("TextIO", original_open(path, mode, buffering, encoding, errors, newline))
        if path == source_file and "r" in mode:
            return _BoundedTextReader(stream, read_sizes)
        return stream

    monkeypatch.setattr(type(source_file), "open", guarded_open)
    document = stream_json_i18n(str(source_file), source_locale="en")

    assert sum(1 for _item in document.items) == 12_000
    assert read_sizes
    assert max(read_sizes) <= 64 * 1024


def test_json_i18n_flat_and_nested_key_collision_export(tmp_path: Path) -> None:
    source_file = tmp_path / "en.json"
    target_file = tmp_path / "fr.json"
    exported = tmp_path / "fr_exported.json"
    source_file.write_text(
        json.dumps({"a.b": "Flat", "a": {"b": "Nested"}}),
        encoding="utf-8",
    )
    target_file.write_text(
        json.dumps({"a.b": "Plat", "a": {"b": "Imbrique"}}),
        encoding="utf-8",
    )

    imported = import_json_i18n(
        str(source_file),
        source_locale="en",
        target_locale="fr",
        target_filepath=str(target_file),
        progress=False,
    )
    export_json_i18n(imported, exported, nested=True)

    assert json.loads(exported.read_text(encoding="utf-8")) == {
        "a.b": "Plat",
        "a": {"b": "Imbrique"},
    }


@pytest.mark.asyncio
async def test_json_i18n_async(tmp_path: Path) -> None:
    source_file = tmp_path / "en.json"
    target_file = tmp_path / "fr.json"

    source_data = {"greeting": "Hello"}
    target_data = {"greeting": "Bonjour"}

    source_file.write_text(json.dumps(source_data), encoding="utf-8")
    target_file.write_text(json.dumps(target_data), encoding="utf-8")

    imported_units = {}
    async for unit_id, data in import_json_i18n_async(
        str(source_file),
        source_locale="en",
        target_locale="fr",
        target_filepath=str(target_file),
    ):
        imported_units[unit_id] = data
    imported = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data=imported_units,
        extensions={"input_format": "json_i18n"},
    )
    assert imported.data["greeting"].source == "Hello"
    assert imported.data["greeting"].target == "Bonjour"

    exported = tmp_path / "fr_exported.json"
    await export_json_i18n_async(imported, exported, nested=False)
    assert exported.exists()


@pytest.mark.asyncio
async def test_json_i18n_async_resumes_disk_index_across_prefetch_windows(tmp_path: Path) -> None:
    source_file = tmp_path / "en.json"
    source_file.write_text(
        json.dumps({f"message_{index}": f"Translation {index}" for index in range(1_000)}),
        encoding="utf-8",
    )

    count = 0
    async for _unit_id, _data in import_json_i18n_async(str(source_file), source_locale="en"):
        count += 1

    assert count == 1_000
