from __future__ import annotations

import json
from io import TextIOBase
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from lokit import parse, stream
from lokit.data.structure import BaseStructure, StreamingStructure
from lokit.io import legacy_json_stream
from lokit.io.json import load_lokit_json
from lokit.io.legacy_json_stream import stream_lokit_json

if TYPE_CHECKING:
    from types import TracebackType
    from typing import TextIO


def _materialize(document: StreamingStructure) -> BaseStructure:
    return BaseStructure(
        source_locale=document.source_locale,
        target_locale=document.target_locale,
        data=dict(document.items),
        target_locales=document.target_locales,
        format_version=document.format_version,
        export_origin=document.export_origin,
        export_timestamp=document.export_timestamp,
        source_language=document.source_language,
        target_language=document.target_language,
        target_languages=document.target_languages,
        extensions=document.extensions,
    )


def test_legacy_json_stream_matches_materialized_loader_with_trailing_metadata(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    payload = {
        "ignored": {"nested": [True, None, {"value": "ignored"}]},
        "data": {
            "unit": {
                "source": 'Hello\n"world" 🌍',
                "target": "Bonjour",
                "targets": {
                    "de": {
                        "text": "Hallo",
                        "status": "approved",
                        "plural": {"variant": "Hallos", "count": "2", "category": "other"},
                        "extensions": {"quality": 99},
                    }
                },
                "plural": {"variant": "Hellos", "count": 2, "category": "other"},
                "tags": {
                    "source_tag_map": {
                        "code": {
                            "id": "code",
                            "type": "custom.standalone",
                            "original_text": "<x/>",
                        }
                    },
                    "source_parts": [{"value": "Hello"}, {"ref": "code"}],
                },
                "meta": {"usage_count": "4", "extensions": {"owner": "docs"}},
                "status": "translated",
                "comments": [
                    {
                        "context": "menu",
                        "origin": {"system": "cms", "extensions": {"job": 7}},
                    }
                ],
                "previous_context": {"unit_id": "before", "source": "Before"},
                "next_context": {"unit_id": "after", "target": "Après"},
                "extensions": {"priority": 1},
            }
        },
        # Metadata deliberately follows data; JSON object order is not semantic.
        "source_locale": "en",
        "target_locale": "fr",
        "target_locales": ["fr", "de"],
        "format_version": "0.1",
        "export_origin": "test-suite",
        "export_timestamp": "2026-08-23T12:00:00Z",
        "source_language": "English",
        "target_language": "French",
        "target_languages": ["French", "German"],
        "extensions": {"domain": "website", "numeric": 3},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    expected = load_lokit_json(path)
    streamed = stream_lokit_json(str(path))

    assert _materialize(streamed) == expected
    assert list(streamed.items) == []


def test_legacy_json_is_available_on_all_public_parse_and_stream_surfaces(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps(
            {
                "source_locale": "en",
                "target_locale": "fr",
                "data": {
                    "first": {"source": "Hello", "target": "Bonjour"},
                    "second": {"source": "World", "target": "Monde"},
                },
            }
        ),
        encoding="utf-8",
    )
    expected = load_lokit_json(path)

    assert parse.lokit_json(str(path), progress=False) == expected
    assert parse.file(str(path)) == expected
    assert _materialize(stream.lokit_json(str(path))) == expected
    assert _materialize(stream.file(str(path))) == expected


@pytest.mark.asyncio
async def test_legacy_json_async_parse_and_stream_surfaces(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps(
            {
                "source_locale": "en",
                "target_locale": None,
                "data": {
                    "first": {"source": "Hello"},
                    "second": {"source": "World"},
                },
            }
        ),
        encoding="utf-8",
    )
    expected = list(load_lokit_json(path).data.items())

    assert [item async for item in parse.async_.lokit_json(str(path))] == expected
    assert [item async for item in parse.async_.file(str(path))] == expected
    assert [item async for item in stream.async_.lokit_json(str(path))] == expected
    assert [item async for item in stream.async_.file(str(path))] == expected


class _GuardedTextReader(TextIOBase):
    def __init__(self, wrapped: TextIO, requests: list[int]) -> None:
        self._wrapped = wrapped
        self._requests = requests

    def read(self, size: int | None = -1) -> str:
        assert size is not None
        assert 0 < size <= legacy_json_stream._READ_CHUNK_CHARS
        self._requests.append(size)
        return self._wrapped.read(size)

    def __enter__(self) -> _GuardedTextReader:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._wrapped.close()


def test_legacy_json_stream_uses_bounded_reads_for_large_one_shot_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "large.json"
    unit_count = 25_000
    with path.open("w", encoding="utf-8") as stream:
        stream.write('{"data":{')
        for index in range(unit_count):
            if index:
                stream.write(",")
            stream.write(json.dumps(f"unit-{index}"))
            stream.write(":")
            stream.write(json.dumps({"source": f"Source {index} " + "x" * 80}))
        stream.write('},"source_locale":"en","target_locale":null}')

    original_open = Path.open
    read_requests: list[int] = []

    def guarded_open(
        opened_path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> TextIO:
        wrapped = original_open(
            opened_path,
            mode,
            buffering,
            encoding,
            errors,
            newline,
        )
        return cast("TextIO", _GuardedTextReader(cast("TextIO", wrapped), read_requests))

    monkeypatch.setattr(Path, "open", guarded_open)

    document = stream_lokit_json(str(path))
    assert sum(1 for _ in document.items) == unit_count
    assert list(document.items) == []
    assert len(read_requests) > 2
    assert set(read_requests) == {legacy_json_stream._READ_CHUNK_CHARS}


def test_legacy_json_stream_rejects_oversized_unit_with_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert legacy_json_stream._MAX_CAPTURE_BYTES == 64 * 1024 * 1024
    monkeypatch.setattr(legacy_json_stream, "_MAX_CAPTURE_BYTES", 512)
    path = tmp_path / "oversized.json"
    path.write_text(
        json.dumps(
            {
                "source_locale": "en",
                "target_locale": None,
                "data": {"too-large": {"source": "x" * 2_000}},
            }
        ),
        encoding="utf-8",
    )

    document = stream_lokit_json(str(path))
    with pytest.raises(json.JSONDecodeError, match=r"512-byte safety limit at character offset \d+"):
        next(iter(document.items))


def test_legacy_json_stream_handles_escapes_across_read_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "boundaries.json"
    payload = {
        "data": {
            'unit"\\': {
                "source": 'quoted: "hello"; slash: \\; unicode: \u2603',
                "target": "ligne\nsuivante",
            }
        },
        "source_locale": "en",
        "target_locale": "fr",
    }
    path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    expected = load_lokit_json(path)

    for chunk_size in range(1, 17):
        monkeypatch.setattr(legacy_json_stream, "_READ_CHUNK_CHARS", chunk_size)
        assert _materialize(stream_lokit_json(path)) == expected


def test_legacy_json_stream_preserves_last_top_level_member_semantics(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-members.json"
    path.write_text(
        """{
  "source_locale": "discarded",
  "data": {"discarded": {"source": "First"}},
  "source_locale": "en",
  "data": {"retained": {"source": "Second"}}
}
""",
        encoding="utf-8",
    )

    assert _materialize(stream_lokit_json(path)) == load_lokit_json(path)


@pytest.mark.parametrize("invalid_data", ("[]", '"units"', "null", "1.5", "false"))
def test_legacy_json_stream_preserves_invalid_data_type_errors(tmp_path: Path, invalid_data: str) -> None:
    path = tmp_path / "invalid-data.json"
    path.write_text(
        '{"source_locale":"en","data":' + invalid_data + "}",
        encoding="utf-8",
    )

    with pytest.raises(TypeError) as expected:
        load_lokit_json(path)
    with pytest.raises(TypeError, match=str(expected.value)):
        stream_lokit_json(path)


@pytest.mark.parametrize(
    "malformed_value",
    (
        "[1,]",
        '{"nested":true,}',
        '"bad\\qescape"',
        "01",
    ),
)
def test_legacy_json_stream_strictly_validates_discarded_values(
    tmp_path: Path,
    malformed_value: str,
) -> None:
    path = tmp_path / "malformed.json"
    path.write_text(
        '{"source_locale":"en","ignored":' + malformed_value + ',"data":{}}',
        encoding="utf-8",
    )

    with pytest.raises(json.JSONDecodeError):
        stream_lokit_json(str(path))
