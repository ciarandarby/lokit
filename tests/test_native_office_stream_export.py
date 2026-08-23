from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NoReturn

import pytest
from test_office import DOCX_FIXTURE, PPTX_FIXTURE, _write_minimal_docx, _write_minimal_pptx

import lokit
from lokit import _interchange_rust
from lokit.data.structure import AdjacentContext, Data, StreamingStructure, Tags, TextPart
from lokit.office.native import attach_native_office_items

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(slots=True)
class _NativeCallTrace:
    attempts: int = 0
    results: list[int | None] = field(default_factory=list)


@pytest.fixture
def docx_fixture(tmp_path: Path) -> Path:
    if DOCX_FIXTURE.exists():
        return DOCX_FIXTURE
    path = tmp_path / "minimal.docx"
    _write_minimal_docx(path)
    return path


@pytest.fixture
def pptx_fixture(tmp_path: Path) -> Path:
    if PPTX_FIXTURE.exists():
        return PPTX_FIXTURE
    path = tmp_path / "minimal.pptx"
    _write_minimal_pptx(path)
    return path


def _trace_native_stream_export(monkeypatch: pytest.MonkeyPatch) -> _NativeCallTrace:
    original = _interchange_rust.export_stream_interchange
    trace = _NativeCallTrace()

    def tracking_export(document: object, target_path: str, output_format: str) -> int | None:
        trace.attempts += 1
        result = original(document, target_path, output_format)
        trace.results.append(result)
        return result

    monkeypatch.setattr(_interchange_rust, "export_stream_interchange", tracking_export)
    return trace


def _trace_native_base_export(monkeypatch: pytest.MonkeyPatch) -> _NativeCallTrace:
    original = _interchange_rust.export_base_interchange
    trace = _NativeCallTrace()

    def tracking_export(document: object, target_path: str, output_format: str) -> int | None:
        trace.attempts += 1
        result = original(document, target_path, output_format)
        trace.results.append(result)
        return result

    monkeypatch.setattr(_interchange_rust, "export_base_interchange", tracking_export)
    return trace


def _forbid_streaming_target_split(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("native Office export must not construct StreamingTargetSplit")


def test_pristine_docx_to_source_only_xliff_uses_native_export(
    docx_fixture: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = lokit.parse.docx(docx_fixture, source_locale="en-US", progress=False)
    output = tmp_path / "document.xliff"
    trace = _trace_native_stream_export(monkeypatch)
    monkeypatch.setattr("lokit.exporters.xliff.StreamingTargetSplit", _forbid_streaming_target_split)
    document = lokit.stream.docx(docx_fixture, source_locale="en-US")

    document.export.xliff(output)

    reparsed = lokit.parse.xliff(str(output), progress=False)
    assert trace.attempts == 1
    assert trace.results == [len(expected.data)]
    assert reparsed.target_locale is None
    assert [(unit_id, data.source, data.status) for unit_id, data in reparsed.data.items()] == [
        (unit_id, data.source, data.status) for unit_id, data in expected.data.items()
    ]
    assert list(document.items) == []


def test_pristine_pptx_to_source_only_xliff_uses_native_export(
    pptx_fixture: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = lokit.parse.pptx(pptx_fixture, source_locale="en-US", progress=False)
    output = tmp_path / "presentation.xliff"
    trace = _trace_native_stream_export(monkeypatch)
    monkeypatch.setattr("lokit.exporters.xliff.StreamingTargetSplit", _forbid_streaming_target_split)
    document = lokit.stream.pptx(pptx_fixture, source_locale="en-US")

    document.export.xliff(output)

    reparsed = lokit.parse.xliff(str(output), progress=False)
    assert trace.attempts == 1
    assert trace.results == [len(expected.data)]
    assert reparsed.target_locale is None
    assert [(unit_id, data.source, data.status) for unit_id, data in reparsed.data.items()] == [
        (unit_id, data.source, data.status) for unit_id, data in expected.data.items()
    ]


def test_partially_consumed_office_stream_falls_back_without_data_loss(
    docx_fixture: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "remainder.xliff"
    document = lokit.stream.docx(docx_fixture, source_locale="en-US")
    items = iter(document.items)
    first_id, _first_data = next(items)
    trace = _trace_native_stream_export(monkeypatch)

    document.export.xliff(output)

    reparsed = lokit.parse.xliff(str(output), progress=False)
    assert trace.attempts == 0
    assert reparsed.data
    assert first_id not in reparsed.data


def test_office_metadata_mutation_and_resource_grouping_bypass_native_export(
    pptx_fixture: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutated_output = tmp_path / "mutated.xliff"
    grouped_output = tmp_path / "grouped.xliff"
    trace = _trace_native_stream_export(monkeypatch)
    mutated = lokit.stream.pptx(pptx_fixture, source_locale="en-US")
    mutated.source_locale = "en-GB"

    mutated.export.xliff(mutated_output)
    lokit.stream.pptx(pptx_fixture, source_locale="en-US").export.xliff(
        grouped_output,
        group_by_resource=True,
    )

    assert trace.attempts == 0
    assert lokit.parse.xliff(str(mutated_output), progress=False).source_locale == "en-GB"
    assert lokit.parse.xliff(str(grouped_output), progress=False).data


def test_materialized_office_source_only_xliff_accepts_plain_tags_and_context(
    docx_fixture: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "materialized.xliff"
    document = lokit.parse.docx(docx_fixture, source_locale="en-US", progress=False)
    expected = [(unit_id, data.source, data.status) for unit_id, data in document.data.items()]
    for data in document.data.values():
        data.tags = Tags(source_parts=[TextPart(data.source)])
    trace = _trace_native_base_export(monkeypatch)

    document.export.xliff(output)

    reparsed = lokit.parse.xliff(str(output), progress=False)
    assert trace.attempts == 1
    assert trace.results == [len(expected)]
    assert [(unit_id, data.source, data.status) for unit_id, data in reparsed.data.items()] == expected


def test_late_native_office_failure_preserves_existing_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "existing.xliff"
    output.write_bytes(b"existing output\n")
    document = StreamingStructure(
        source_locale="en-US",
        target_locale=None,
        items=iter(
            (
                (
                    "first",
                    Data(
                        source="First",
                        next_context=AdjacentContext(unit_id="invalid", source="Invalid"),
                        extensions={"input_format": "docx"},
                    ),
                ),
                ("invalid", Data(source="Invalid\x01source", extensions={"input_format": "docx"})),
            )
        ),
        extensions={"input_format": "docx"},
    )
    attach_native_office_items(document)

    with pytest.raises(ValueError, match=r"XML 1\.0"):
        document.export.xliff(output)

    assert output.read_bytes() == b"existing output\n"
    assert list(tmp_path.glob(".existing.xliff.*.tmp")) == []
