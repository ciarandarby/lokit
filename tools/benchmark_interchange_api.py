from __future__ import annotations

import argparse
import asyncio
import contextlib
import gc
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from dataclasses import dataclass
from html import escape as html_escape
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast

import lokit.parse as lokit_parse
import lokit.stream as lokit_stream
from lokit._interchange_rust import backend_version
from lokit.data.structure import (
    BaseStructure,
    CodePart,
    Data,
    StreamingStructure,
    Tags,
    TargetData,
    TargetTags,
    TextPart,
    TranslationStatus,
)
from lokit.data.tag_types import TieData, TieType
from lokit.exporters.lokit import export_lokit
from lokit.stream import async_ as lokit_async_stream
from lokit.types import DEFAULT_DICT_FIELDS, StringMode, TranslationRow

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence

FormatName = Literal["tmx", "xliff", "lokit"]
WorkloadKind = Literal["sync_stream", "async_stream", "materialized", "base_split", "stream_split"]
RowOrder = Literal["unit_major", "locale_major"]
CorpusRole = Literal["locale_scaling", "bounded_length"]
MemoryContract = Literal["bounded_stream", "spooled_bounded_stream", "materialized"]


class _ResourceUsage(Protocol):
    ru_maxrss: int


class _ResourceApi(Protocol):
    RUSAGE_SELF: int

    def getrusage(self, who: int) -> _ResourceUsage: ...


FORMATS: tuple[FormatName, ...] = ("tmx", "xliff", "lokit")
TARGET_LOCALES: tuple[str, ...] = (
    "fr-FR",
    "de-DE",
    "es-ES",
    "it-IT",
    "pt-BR",
    "nl-NL",
    "pl-PL",
    "sv-SE",
    "da-DK",
    "fi-FI",
    "cs-CZ",
    "ro-RO",
    "hu-HU",
    "tr-TR",
    "uk-UA",
    "ar-SA",
    "he-IL",
    "hi-IN",
    "ja-JP",
    "ko-KR",
    "vi-VN",
    "id-ID",
    "th-TH",
    "el-GR",
    "bg-BG",
    "hr-HR",
    "sk-SK",
    "sl-SI",
    "et-EE",
    "lv-LV",
    "lt-LT",
    "ga-IE",
)
SOURCE_LOCALE = "en-US"
DOMAIN = "benchmark"
CHECKSUM_SEPARATOR = "\x1f"
CHECKSUM_ROW_TERMINATOR = b"\x1e"
DEFAULT_UNITS = 2_500
DEFAULT_REPEATS = 5
DEFAULT_WARMUPS = 1
DEFAULT_LOCALE_COUNTS = (1, 2, 5, 20)
DEFAULT_MEMORY_SCALE = 8


@dataclass(frozen=True, slots=True)
class Arguments:
    corpus_dir: Path | None
    formats: tuple[FormatName, ...]
    locale_counts: tuple[int, ...]
    memory_scale: int
    output: Path | None
    repeats: int
    skip_allocations: bool
    skip_rss: bool
    units: int
    warmups: int


@dataclass(frozen=True, slots=True)
class CorpusKey:
    format_name: FormatName
    locale_count: int
    role: CorpusRole
    units: int


@dataclass(frozen=True, slots=True)
class CorpusFile:
    bytes: int
    key: CorpusKey
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class WorkloadSpec:
    format_name: FormatName
    input_units: int
    kind: WorkloadKind
    name: str
    path: Path
    row_order: RowOrder
    strings: StringMode
    target_locales: tuple[str, ...]
    target_filter: str


@dataclass(frozen=True, slots=True)
class Summary:
    checksum: str
    rows: int
    witness: int


@dataclass(frozen=True, slots=True)
class RunResult:
    summary: Summary
    time_to_first_seconds: float


@dataclass(frozen=True, slots=True)
class RssMeasurement:
    baseline_bytes: int
    method: str
    peak_bytes: int
    peak_delta_bytes: int
    supported: bool


class CorpusResult(TypedDict):
    bytes: int
    format: str
    locale_count: int
    path: str
    role: str
    sha256: str
    units: int


class WorkloadResult(TypedDict):
    checksum: str
    durations_seconds: list[float]
    format: str
    input_bytes: int
    input_units: int
    locale_count: int
    memory_assertion_scope: str
    memory_contract: str
    median_seconds: float
    mib_per_second: float
    min_seconds: float
    name: str
    p95_seconds: float
    peak_python_allocated_bytes: int
    peak_rss_baseline_bytes: int
    peak_rss_bytes: int
    peak_rss_delta_bytes: int
    rows: int
    rows_per_second: float
    rss_measurement: str
    rss_supported: bool
    source_units_per_second: float
    strings: str
    target_filter: str
    time_to_first_median_seconds: float
    time_to_first_samples_seconds: list[float]
    witness: int
    workload: str


class LocaleScalingResult(TypedDict):
    format: str
    locale_count: int
    median_seconds: float
    peak_python_allocated_bytes: int
    peak_rss_delta_bytes: int
    rows: int
    rows_per_second: float


class BoundedMemoryResult(TypedDict):
    allocation_assertion_evaluated: bool
    allocation_growth_limit: float
    allocation_growth_passed: bool
    format: str
    input_byte_growth_ratio: float
    large_input_bytes: int
    large_input_units: int
    large_peak_python_allocated_bytes: int
    large_peak_rss_delta_bytes: int
    large_rows: int
    python_allocation_growth_ratio: float
    row_growth_ratio: float
    rss_delta_growth_ratio: float
    rss_supported: bool
    small_input_bytes: int
    small_input_units: int
    small_peak_python_allocated_bytes: int
    small_peak_rss_delta_bytes: int
    small_rows: int


class ConfigurationResult(TypedDict):
    formats: list[str]
    locale_counts: list[int]
    memory_scale: int
    repeats: int
    skip_allocations: bool
    skip_rss: bool
    units: int
    warmups: int


class EnvironmentResult(TypedDict):
    backend_origin: str
    backend_sha256: str
    backend_version: str
    benchmark_sha256: str
    cpu: str
    executable: str
    logical_cpus: int
    machine: str
    native_interchange_disabled: bool
    package_version: str
    platform: str
    python: str
    python_implementation: str
    repository_dirty: bool
    repository_revision: str


class MethodologyResult(TypedDict):
    checksum: str
    generation: str
    memory_contracts: str
    python_allocations: str
    rss: str
    timing: str


class EvidenceResult(TypedDict):
    bounded_memory: BoundedMemoryResult
    locale_scaling: list[LocaleScalingResult]


class SuiteResult(TypedDict):
    configuration: ConfigurationResult
    corpora: list[CorpusResult]
    environment: EnvironmentResult
    evidence: EvidenceResult
    generated_at_utc: str
    methodology: MethodologyResult
    results: list[WorkloadResult]
    schema_version: int


class WorkerResult(TypedDict):
    checksum: str
    peak_rss_baseline_bytes: int
    peak_rss_bytes: int
    peak_rss_delta_bytes: int
    rows: int
    rss_measurement: str
    rss_supported: bool
    time_to_first_seconds: float
    witness: int


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _memory_scale(value: str) -> int:
    parsed = int(value)
    if parsed < 2:
        raise argparse.ArgumentTypeError("memory scale must be at least 2")
    return parsed


def _parse_formats(value: str) -> tuple[FormatName, ...]:
    selected: list[FormatName] = []
    for raw_format in value.split(","):
        format_name = raw_format.strip().lower()
        if format_name not in FORMATS:
            raise argparse.ArgumentTypeError(f"unsupported format: {format_name}")
        if format_name in selected:
            raise argparse.ArgumentTypeError(f"duplicate format: {format_name}")
        selected.append(format_name)
    if not selected:
        raise argparse.ArgumentTypeError("at least one format is required")
    return tuple(selected)


def _parse_locale_counts(value: str) -> tuple[int, ...]:
    counts: list[int] = []
    for raw_count in value.split(","):
        try:
            count = int(raw_count.strip())
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"invalid locale count: {raw_count}") from error
        if count < 1 or count > len(TARGET_LOCALES):
            raise argparse.ArgumentTypeError(f"locale counts must be between 1 and {len(TARGET_LOCALES)}")
        if count in counts:
            raise argparse.ArgumentTypeError(f"duplicate locale count: {count}")
        counts.append(count)
    if not counts:
        raise argparse.ArgumentTypeError("at least one locale count is required")
    return tuple(sorted(counts))


def _arguments(argv: Sequence[str]) -> Arguments:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark flat interchange projections and target splitting with deterministic multilingual inputs."
        )
    )
    parser.add_argument(
        "--units",
        type=_positive,
        default=DEFAULT_UNITS,
        help=f"source units per locale-scaling corpus (default: {DEFAULT_UNITS})",
    )
    parser.add_argument(
        "--repeats",
        type=_positive,
        default=DEFAULT_REPEATS,
        help=f"checksum-validated timing samples (default: {DEFAULT_REPEATS})",
    )
    parser.add_argument(
        "--warmups",
        type=_positive,
        default=DEFAULT_WARMUPS,
        help=f"untimed warm-up executions (default: {DEFAULT_WARMUPS})",
    )
    parser.add_argument(
        "--memory-scale",
        type=_memory_scale,
        default=DEFAULT_MEMORY_SCALE,
        help=f"input-length multiplier for the bounded-memory gate (default: {DEFAULT_MEMORY_SCALE})",
    )
    parser.add_argument(
        "--locale-counts",
        type=_parse_locale_counts,
        default=DEFAULT_LOCALE_COUNTS,
        help="comma-separated locale widths (default: 1,2,5,20; maximum: 32)",
    )
    parser.add_argument(
        "--formats",
        type=_parse_formats,
        default=FORMATS,
        help="comma-separated formats selected from tmx,xliff,lokit (default: all)",
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        help="retain deterministic inputs in this directory instead of a temporary directory",
    )
    parser.add_argument("--output", type=Path, help="atomically write the JSON report to this path")
    parser.add_argument(
        "--skip-allocations",
        action="store_true",
        help="skip tracemalloc passes and the bounded-allocation gate (quick diagnostics only)",
    )
    parser.add_argument(
        "--skip-rss",
        action="store_true",
        help="skip isolated-process RSS passes (quick diagnostics only)",
    )
    namespace = parser.parse_args(argv)
    corpus_dir = namespace.corpus_dir
    if corpus_dir is not None and not isinstance(corpus_dir, Path):
        raise TypeError("--corpus-dir must resolve to a path")
    output = namespace.output
    if output is not None and not isinstance(output, Path):
        raise TypeError("--output must resolve to a path")
    formats = namespace.formats
    if not isinstance(formats, tuple):
        raise TypeError("--formats must resolve to a tuple")
    locale_counts = namespace.locale_counts
    if not isinstance(locale_counts, tuple):
        raise TypeError("--locale-counts must resolve to a tuple")
    return Arguments(
        corpus_dir=corpus_dir,
        formats=cast("tuple[FormatName, ...]", formats),
        locale_counts=cast("tuple[int, ...]", locale_counts),
        memory_scale=int(namespace.memory_scale),
        output=output,
        repeats=int(namespace.repeats),
        skip_allocations=bool(namespace.skip_allocations),
        skip_rss=bool(namespace.skip_rss),
        units=int(namespace.units),
        warmups=int(namespace.warmups),
    )


def _worker_arguments(argv: Sequence[str]) -> WorkloadSpec:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--format", choices=FORMATS, required=True)
    parser.add_argument(
        "--kind",
        choices=("sync_stream", "async_stream", "materialized", "base_split", "stream_split"),
        required=True,
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--units", type=_positive, required=True)
    parser.add_argument("--locales", required=True)
    parser.add_argument("--strings", choices=("sanitized", "raw"), required=True)
    parser.add_argument("--target-filter", default="")
    parser.add_argument("--row-order", choices=("unit_major", "locale_major"), required=True)
    namespace = parser.parse_args(argv)
    path = namespace.path
    if not isinstance(path, Path):
        raise TypeError("worker path must resolve to a path")
    locales = tuple(locale for locale in str(namespace.locales).split(",") if locale)
    if not locales:
        raise ValueError("worker target locales must not be empty")
    return WorkloadSpec(
        format_name=cast("FormatName", str(namespace.format)),
        input_units=int(namespace.units),
        kind=cast("WorkloadKind", str(namespace.kind)),
        name=str(namespace.name),
        path=path,
        row_order=cast("RowOrder", str(namespace.row_order)),
        strings=StringMode(str(namespace.strings)),
        target_locales=locales,
        target_filter=str(namespace.target_filter),
    )


def _source_prefix(index: int) -> str:
    return f"Source {index:08d}: café & tea <{index % 97:02d}> "


def _target_prefix(index: int, locale: str) -> str:
    return f"{locale} target {index:08d}: café & tea <{index % 97:02d}> "


def _source_text(index: int) -> str:
    return f"{_source_prefix(index)}message."


def _target_text(index: int, locale: str) -> str:
    return f"{_target_prefix(index, locale)}message."


def _raw_text(prefix: str, tag_name: str) -> str:
    return f'{html_escape(prefix, quote=False)}<{tag_name} type="bold" x="1">message</{tag_name}>.'


def _xml_text(prefix: str, tag_name: str) -> str:
    return _raw_text(prefix, tag_name)


def _write_tmx(path: Path, units: int, target_locales: tuple[str, ...]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<tmx version="1.4">\n'
            '  <header creationtool="lokit-interchange-benchmark" creationtoolversion="1" '
            'segtype="sentence" o-tmf="benchmark" adminlang="en-US" srclang="en-US" '
            'datatype="PlainText"/>\n'
            "  <body>\n"
        )
        for index in range(units):
            output.write(
                f'    <tu tuid="unit_{index:08d}"><prop type="x-domain">{DOMAIN}</prop>'
                f'<tuv xml:lang="{SOURCE_LOCALE}"><seg>{_xml_text(_source_prefix(index), "hi")}</seg></tuv>'
            )
            for locale in target_locales:
                output.write(
                    f'<tuv xml:lang="{locale}"><seg>{_xml_text(_target_prefix(index, locale), "hi")}</seg></tuv>'
                )
            output.write("</tu>\n")
        output.write("  </body>\n</tmx>\n")


def _write_xliff(path: Path, units: int, target_locales: tuple[str, ...]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">\n'
        )
        for locale in target_locales:
            output.write(
                f'  <file original="messages/{locale}" source-language="{SOURCE_LOCALE}" '
                f'target-language="{locale}" datatype="plaintext"><body>\n'
            )
            for index in range(units):
                output.write(
                    f'    <trans-unit id="unit_{index:08d}">'
                    f"<source>{_xml_text(_source_prefix(index), 'g')}</source>"
                    f'<target state="translated">{_xml_text(_target_prefix(index, locale), "g")}</target>'
                    "</trans-unit>\n"
                )
            output.write("  </body></file>\n")
        output.write("</xliff>\n")


def _source_tags(index: int) -> Tags:
    return Tags(
        source_tag_map={
            "source-open": TieData(
                id="source-open",
                type=TieType.CUSTOM_OPEN,
                attributes={"type": "bold", "x": "1"},
                pair_id="source-pair",
                original_name="hi",
            ),
            "source-close": TieData(
                id="source-close",
                type=TieType.CUSTOM_CLOSE,
                pair_id="source-pair",
                original_name="hi",
            ),
        },
        source_parts=[
            TextPart(_source_prefix(index)),
            CodePart("source-open"),
            TextPart("message"),
            CodePart("source-close"),
            TextPart("."),
        ],
    )


def _target_tags(index: int, locale: str) -> TargetTags:
    return TargetTags(
        tag_map={
            "target-open": TieData(
                id="target-open",
                type=TieType.CUSTOM_OPEN,
                attributes={"type": "bold", "x": "1"},
                pair_id="target-pair",
                original_name="hi",
            ),
            "target-close": TieData(
                id="target-close",
                type=TieType.CUSTOM_CLOSE,
                pair_id="target-pair",
                original_name="hi",
            ),
        },
        parts=[
            TextPart(_target_prefix(index, locale)),
            CodePart("target-open"),
            TextPart("message"),
            CodePart("target-close"),
            TextPart("."),
        ],
    )


def _lokit_records(units: int, target_locales: tuple[str, ...]) -> Iterator[tuple[str, Data]]:
    for index in range(units):
        targets: dict[str, TargetData] = {}
        for locale in target_locales:
            targets[locale] = TargetData(
                text=_target_text(index, locale),
                status=TranslationStatus.TRANSLATED,
                tags=_target_tags(index, locale),
            )
        yield (
            f"unit_{index:08d}",
            Data(
                source=_source_text(index),
                targets=targets,
                tags=_source_tags(index),
                status=TranslationStatus.TRANSLATED,
                extensions={"domain": DOMAIN},
            ),
        )


def _write_lokit(path: Path, units: int, target_locales: tuple[str, ...]) -> None:
    document = StreamingStructure(
        source_locale=SOURCE_LOCALE,
        target_locale=None,
        items=_lokit_records(units, target_locales),
        target_locales=target_locales,
        source_language="en",
        target_languages=tuple(_base_language(locale) for locale in target_locales),
        extensions={"input_format": "tmx"},
    )
    export_lokit(document, path)


def _write_corpus(path: Path, format_name: FormatName, units: int, target_locales: tuple[str, ...]) -> None:
    if format_name == "tmx":
        _write_tmx(path, units, target_locales)
    elif format_name == "xliff":
        _write_xliff(path, units, target_locales)
    else:
        _write_lokit(path, units, target_locales)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _suffix(format_name: FormatName) -> str:
    if format_name == "xliff":
        return ".xliff"
    return f".{format_name}"


def _generate_corpora(directory: Path, arguments: Arguments) -> dict[CorpusKey, CorpusFile]:
    directory.mkdir(parents=True, exist_ok=True)
    corpora: dict[CorpusKey, CorpusFile] = {}
    for format_name in arguments.formats:
        for locale_count in arguments.locale_counts:
            key = CorpusKey(
                format_name=format_name,
                locale_count=locale_count,
                role="locale_scaling",
                units=arguments.units,
            )
            path = directory / f"{format_name}-u{arguments.units}-l{locale_count:02d}{_suffix(format_name)}"
            _write_corpus(path, format_name, arguments.units, TARGET_LOCALES[:locale_count])
            corpora[key] = CorpusFile(bytes=path.stat().st_size, key=key, path=path, sha256=_sha256(path))

    bounded_format = arguments.formats[0]
    bounded_locales = arguments.locale_counts[0]
    bounded_units = arguments.units * arguments.memory_scale
    bounded_key = CorpusKey(
        format_name=bounded_format,
        locale_count=bounded_locales,
        role="bounded_length",
        units=bounded_units,
    )
    bounded_path = directory / (
        f"{bounded_format}-u{bounded_units}-l{bounded_locales:02d}-bounded{_suffix(bounded_format)}"
    )
    _write_corpus(bounded_path, bounded_format, bounded_units, TARGET_LOCALES[:bounded_locales])
    corpora[bounded_key] = CorpusFile(
        bytes=bounded_path.stat().st_size,
        key=bounded_key,
        path=bounded_path,
        sha256=_sha256(bounded_path),
    )
    return corpora


def _projection_order(format_name: FormatName) -> RowOrder:
    return "locale_major" if format_name == "xliff" else "unit_major"


def _workloads(arguments: Arguments, corpora: Mapping[CorpusKey, CorpusFile]) -> list[WorkloadSpec]:
    specs: list[WorkloadSpec] = []
    for format_name in arguments.formats:
        for locale_count in arguments.locale_counts:
            corpus = corpora[CorpusKey(format_name, locale_count, "locale_scaling", arguments.units)]
            specs.append(
                WorkloadSpec(
                    format_name=format_name,
                    input_units=arguments.units,
                    kind="sync_stream",
                    name=f"{format_name}.sync_stream.sanitized.locales_{locale_count}",
                    path=corpus.path,
                    row_order=_projection_order(format_name),
                    strings=StringMode.SANITIZED,
                    target_locales=TARGET_LOCALES[:locale_count],
                    target_filter="",
                )
            )

        max_locale_count = arguments.locale_counts[-1]
        max_locales = TARGET_LOCALES[:max_locale_count]
        corpus = corpora[CorpusKey(format_name, max_locale_count, "locale_scaling", arguments.units)]
        first_target = max_locales[0]
        core_specs: tuple[tuple[str, WorkloadKind, StringMode, str, RowOrder], ...] = (
            (
                "sync_stream.raw.first_target",
                "sync_stream",
                StringMode.RAW,
                first_target,
                _projection_order(format_name),
            ),
            (
                "async_stream.sanitized.first_target",
                "async_stream",
                StringMode.SANITIZED,
                first_target,
                _projection_order(format_name),
            ),
            (
                "async_stream.raw.first_target",
                "async_stream",
                StringMode.RAW,
                first_target,
                _projection_order(format_name),
            ),
            (
                "materialized.sanitized.first_target",
                "materialized",
                StringMode.SANITIZED,
                first_target,
                _projection_order(format_name),
            ),
            (
                "materialized.raw.first_target",
                "materialized",
                StringMode.RAW,
                first_target,
                _projection_order(format_name),
            ),
            ("base_split.sanitized", "base_split", StringMode.SANITIZED, "", "locale_major"),
            ("stream_split.sanitized", "stream_split", StringMode.SANITIZED, "", "locale_major"),
        )
        for suffix, kind, strings, target_filter, row_order in core_specs:
            specs.append(
                WorkloadSpec(
                    format_name=format_name,
                    input_units=arguments.units,
                    kind=kind,
                    name=f"{format_name}.{suffix}",
                    path=corpus.path,
                    row_order=row_order,
                    strings=strings,
                    target_locales=max_locales,
                    target_filter=target_filter,
                )
            )

    bounded_format = arguments.formats[0]
    bounded_locale_count = arguments.locale_counts[0]
    bounded_units = arguments.units * arguments.memory_scale
    bounded_corpus = corpora[CorpusKey(bounded_format, bounded_locale_count, "bounded_length", bounded_units)]
    specs.append(
        WorkloadSpec(
            format_name=bounded_format,
            input_units=bounded_units,
            kind="sync_stream",
            name=f"{bounded_format}.sync_stream.sanitized.bounded_length",
            path=bounded_corpus.path,
            row_order=_projection_order(bounded_format),
            strings=StringMode.SANITIZED,
            target_locales=TARGET_LOCALES[:bounded_locale_count],
            target_filter="",
        )
    )
    return specs


def _base_language(locale: str) -> str:
    return locale.replace("_", "-").split("-", 1)[0].lower()


def _tag_name(format_name: FormatName) -> str:
    return "g" if format_name == "xliff" else "hi"


def _expected_values(spec: WorkloadSpec, index: int, locale: str) -> tuple[str, ...]:
    if spec.strings is StringMode.RAW:
        tag_name = _tag_name(spec.format_name)
        source = _raw_text(_source_prefix(index), tag_name)
        target = _raw_text(_target_prefix(index, locale), tag_name)
    else:
        source = _source_text(index)
        target = _target_text(index, locale)
    return ("en", _base_language(locale), source, target, DOMAIN)


def _expected_rows(spec: WorkloadSpec) -> Iterator[tuple[str, ...]]:
    locales = (
        tuple(locale for locale in spec.target_locales if locale == spec.target_filter)
        if spec.target_filter
        else spec.target_locales
    )
    if spec.row_order == "locale_major":
        for locale in locales:
            for index in range(spec.input_units):
                yield _expected_values(spec, index, locale)
    else:
        for index in range(spec.input_units):
            for locale in locales:
                yield _expected_values(spec, index, locale)


def _summarize_values(rows: Iterable[tuple[str, ...]]) -> Summary:
    digest = hashlib.sha256()
    count = 0
    witness = 0
    for values in rows:
        encoded = CHECKSUM_SEPARATOR.join(values).encode("utf-8")
        digest.update(encoded)
        digest.update(CHECKSUM_ROW_TERMINATOR)
        witness += sum((index + 1) * len(value) for index, value in enumerate(values))
        count += 1
    return Summary(checksum=digest.hexdigest(), rows=count, witness=witness)


def _row_values(row: TranslationRow) -> tuple[str, ...]:
    expected_names = tuple(field.value for field in DEFAULT_DICT_FIELDS)
    if tuple(row) != expected_names:
        raise RuntimeError(f"projection fields differ from the default contract: {tuple(row)!r}")
    return tuple(row[name] for name in expected_names)


def _summarize_rows(rows: Iterable[TranslationRow]) -> Summary:
    return _summarize_values(_row_values(row) for row in rows)


async def _summarize_rows_async(rows: AsyncIterator[TranslationRow]) -> Summary:
    digest = hashlib.sha256()
    count = 0
    witness = 0
    async for row in rows:
        values = _row_values(row)
        digest.update(CHECKSUM_SEPARATOR.join(values).encode("utf-8"))
        digest.update(CHECKSUM_ROW_TERMINATOR)
        witness += sum((index + 1) * len(value) for index, value in enumerate(values))
        count += 1
    return Summary(checksum=digest.hexdigest(), rows=count, witness=witness)


def _projection_arguments(spec: WorkloadSpec) -> dict[str, str | StringMode]:
    return {
        "source_language": SOURCE_LOCALE,
        "target_language": spec.target_filter,
        "domain": DOMAIN,
        "strings": spec.strings,
    }


def _run_sync_stream(spec: WorkloadSpec) -> RunResult:
    started = time.perf_counter()
    rows = iter(lokit_stream.to_dict(spec.path, **_projection_arguments(spec)))
    try:
        first = next(rows)
    except StopIteration as error:
        raise RuntimeError("projection unexpectedly produced no rows") from error
    time_to_first = time.perf_counter() - started

    def all_rows() -> Iterator[TranslationRow]:
        yield first
        yield from rows

    return RunResult(summary=_summarize_rows(all_rows()), time_to_first_seconds=time_to_first)


async def _run_async_stream_inner(spec: WorkloadSpec) -> RunResult:
    started = time.perf_counter()
    rows = lokit_async_stream.to_dict(spec.path, **_projection_arguments(spec))
    try:
        first = await anext(rows)
    except StopAsyncIteration as error:
        raise RuntimeError("asynchronous projection unexpectedly produced no rows") from error
    time_to_first = time.perf_counter() - started

    async def all_rows() -> AsyncIterator[TranslationRow]:
        yield first
        async for row in rows:
            yield row

    return RunResult(summary=await _summarize_rows_async(all_rows()), time_to_first_seconds=time_to_first)


def _run_async_stream(spec: WorkloadSpec) -> RunResult:
    return asyncio.run(_run_async_stream_inner(spec))


def _run_materialized(spec: WorkloadSpec) -> RunResult:
    started = time.perf_counter()
    rows = lokit_parse.to_dict(spec.path, **_projection_arguments(spec))
    if not rows:
        raise RuntimeError("materialized projection unexpectedly produced no rows")
    time_to_first = time.perf_counter() - started
    return RunResult(summary=_summarize_rows(rows), time_to_first_seconds=time_to_first)


def _parse_base(spec: WorkloadSpec) -> BaseStructure:
    path = str(spec.path)
    if spec.format_name == "tmx":
        return lokit_parse.tmx(path, SOURCE_LOCALE, progress=False)
    if spec.format_name == "xliff":
        return lokit_parse.xliff(path, progress=False)
    return lokit_parse.lokit(path, progress=False)


def _open_stream(spec: WorkloadSpec) -> StreamingStructure:
    path = str(spec.path)
    if spec.format_name == "tmx":
        return lokit_stream.tmx(path, SOURCE_LOCALE)
    if spec.format_name == "xliff":
        return lokit_stream.xliff(path)
    return lokit_stream.lokit(path)


def _base_split_rows(
    splits: Mapping[str, BaseStructure],
    target_locales: tuple[str, ...],
    strings: StringMode,
) -> Iterator[TranslationRow]:
    for locale in target_locales:
        yield from splits[locale].to_dict(domain=DOMAIN, strings=strings)


def _stream_split_rows(
    splits: Mapping[str, StreamingStructure],
    target_locales: tuple[str, ...],
    strings: StringMode,
) -> Iterator[TranslationRow]:
    for locale in target_locales:
        yield from splits[locale].to_dict(domain=DOMAIN, strings=strings)


def _summarize_with_first(rows: Iterator[TranslationRow], started: float) -> RunResult:
    try:
        first = next(rows)
    except StopIteration as error:
        raise RuntimeError("split projection unexpectedly produced no rows") from error
    time_to_first = time.perf_counter() - started

    def all_rows() -> Iterator[TranslationRow]:
        yield first
        yield from rows

    return RunResult(summary=_summarize_rows(all_rows()), time_to_first_seconds=time_to_first)


def _run_base_split(spec: WorkloadSpec) -> RunResult:
    started = time.perf_counter()
    source = _parse_base(spec)
    splits = source.split_targets(spec.target_locales, include_missing=False)
    rows = _base_split_rows(splits, spec.target_locales, spec.strings)
    return _summarize_with_first(rows, started)


def _run_stream_split(spec: WorkloadSpec) -> RunResult:
    started = time.perf_counter()
    source = _open_stream(spec)
    with source.split_targets(spec.target_locales, include_missing=False) as splits:
        rows = _stream_split_rows(splits, spec.target_locales, spec.strings)
        return _summarize_with_first(rows, started)


def _run_once(spec: WorkloadSpec) -> RunResult:
    if spec.kind == "sync_stream":
        return _run_sync_stream(spec)
    if spec.kind == "async_stream":
        return _run_async_stream(spec)
    if spec.kind == "materialized":
        return _run_materialized(spec)
    if spec.kind == "base_split":
        return _run_base_split(spec)
    return _run_stream_split(spec)


def _verify(result: RunResult, expected: Summary, workload: str) -> None:
    if result.summary != expected:
        raise RuntimeError(f"{workload} failed its semantic gate: expected {expected!r}, observed {result.summary!r}")
    if result.time_to_first_seconds < 0.0:
        raise RuntimeError(f"{workload} reported a negative time to first row")


def _peak_rss_bytes() -> tuple[int, str, bool]:
    if os.name == "nt":
        return (0, "unavailable on Windows without an external sampler", False)

    resource_api = cast("_ResourceApi", importlib.import_module("resource"))
    raw_peak = resource_api.getrusage(resource_api.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        return (raw_peak, "resource.ru_maxrss in an isolated subprocess (native bytes)", True)
    return (raw_peak * 1024, "resource.ru_maxrss in an isolated subprocess (KiB converted to bytes)", True)


def _worker(spec: WorkloadSpec) -> None:
    gc.collect()
    baseline, method, supported = _peak_rss_bytes()
    result = _run_once(spec)
    peak, method, supported = _peak_rss_bytes()
    encoded: WorkerResult = {
        "checksum": result.summary.checksum,
        "peak_rss_baseline_bytes": baseline,
        "peak_rss_bytes": peak,
        "peak_rss_delta_bytes": max(0, peak - baseline),
        "rows": result.summary.rows,
        "rss_measurement": method,
        "rss_supported": supported,
        "time_to_first_seconds": result.time_to_first_seconds,
        "witness": result.summary.witness,
    }
    print(json.dumps(encoded, ensure_ascii=False, sort_keys=True))


def _worker_command(spec: WorkloadSpec) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "_worker",
        "--format",
        spec.format_name,
        "--kind",
        spec.kind,
        "--name",
        spec.name,
        "--path",
        str(spec.path),
        "--units",
        str(spec.input_units),
        "--locales",
        ",".join(spec.target_locales),
        "--strings",
        spec.strings.value,
        "--target-filter",
        spec.target_filter,
        "--row-order",
        spec.row_order,
    ]


def _json_object(output: str) -> dict[str, object]:
    try:
        decoded = cast("object", json.loads(output))
    except json.JSONDecodeError as error:
        raise RuntimeError("RSS worker did not emit valid JSON") from error
    if not isinstance(decoded, dict):
        raise RuntimeError("RSS worker result must be a JSON object")
    return cast("dict[str, object]", decoded)


def _json_int(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"RSS worker field {key!r} must be an integer")
    return value


def _json_string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise RuntimeError(f"RSS worker field {key!r} must be a string")
    return value


def _json_bool(data: Mapping[str, object], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise RuntimeError(f"RSS worker field {key!r} must be a boolean")
    return value


def _measure_rss(spec: WorkloadSpec, expected: Summary) -> RssMeasurement:
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    try:
        completed = subprocess.run(
            _worker_command(spec),
            check=True,
            capture_output=True,
            env=environment,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "no diagnostic output"
        raise RuntimeError(f"RSS worker failed for {spec.name}:\n{detail}") from error
    data = _json_object(completed.stdout.strip())
    observed = Summary(
        checksum=_json_string(data, "checksum"),
        rows=_json_int(data, "rows"),
        witness=_json_int(data, "witness"),
    )
    if observed != expected:
        raise RuntimeError(f"RSS worker failed the semantic gate for {spec.name}")
    return RssMeasurement(
        baseline_bytes=_json_int(data, "peak_rss_baseline_bytes"),
        method=_json_string(data, "rss_measurement"),
        peak_bytes=_json_int(data, "peak_rss_bytes"),
        peak_delta_bytes=_json_int(data, "peak_rss_delta_bytes"),
        supported=_json_bool(data, "rss_supported"),
    )


def _zero_rss() -> RssMeasurement:
    return RssMeasurement(
        baseline_bytes=0,
        method="disabled by --skip-rss",
        peak_bytes=0,
        peak_delta_bytes=0,
        supported=False,
    )


def _memory_contract(kind: WorkloadKind) -> MemoryContract:
    if kind in {"materialized", "base_split"}:
        return "materialized"
    if kind == "stream_split":
        return "spooled_bounded_stream"
    return "bounded_stream"


def _memory_assertion_scope(contract: MemoryContract) -> str:
    if contract == "bounded_stream":
        return "bounded input-length growth gate for the designated sync-stream pair"
    if contract == "spooled_bounded_stream":
        return "observational: bounded RAM with intentional temporary-disk spooling"
    return "observational: materialization is intentionally proportional to returned rows"


def _percentile_95(samples: Sequence[float]) -> float:
    ordered = sorted(samples)
    index = max(0, (95 * len(ordered) + 99) // 100 - 1)
    return ordered[index]


def _measure(
    spec: WorkloadSpec,
    expected: Summary,
    corpus_bytes: int,
    arguments: Arguments,
) -> WorkloadResult:
    for _ in range(arguments.warmups):
        _verify(_run_once(spec), expected, spec.name)

    durations: list[float] = []
    time_to_first_samples: list[float] = []
    for _ in range(arguments.repeats):
        gc.collect()
        started = time.perf_counter()
        observed = _run_once(spec)
        duration = time.perf_counter() - started
        _verify(observed, expected, spec.name)
        durations.append(duration)
        time_to_first_samples.append(observed.time_to_first_seconds)

    peak_python = 0
    if not arguments.skip_allocations:
        gc.collect()
        tracemalloc.start()
        try:
            observed = _run_once(spec)
            _, peak_python = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        _verify(observed, expected, spec.name)

    rss = _zero_rss() if arguments.skip_rss else _measure_rss(spec, expected)
    median = float(statistics.median(durations))
    memory_contract = _memory_contract(spec.kind)
    return WorkloadResult(
        checksum=expected.checksum,
        durations_seconds=durations,
        format=spec.format_name,
        input_bytes=corpus_bytes,
        input_units=spec.input_units,
        locale_count=len(spec.target_locales),
        memory_assertion_scope=_memory_assertion_scope(memory_contract),
        memory_contract=memory_contract,
        median_seconds=median,
        mib_per_second=corpus_bytes / (1024 * 1024) / median,
        min_seconds=min(durations),
        name=spec.name,
        p95_seconds=_percentile_95(durations),
        peak_python_allocated_bytes=peak_python,
        peak_rss_baseline_bytes=rss.baseline_bytes,
        peak_rss_bytes=rss.peak_bytes,
        peak_rss_delta_bytes=rss.peak_delta_bytes,
        rows=expected.rows,
        rows_per_second=expected.rows / median,
        rss_measurement=rss.method,
        rss_supported=rss.supported,
        source_units_per_second=spec.input_units / median,
        strings=spec.strings.value,
        target_filter=spec.target_filter,
        time_to_first_median_seconds=float(statistics.median(time_to_first_samples)),
        time_to_first_samples_seconds=time_to_first_samples,
        witness=expected.witness,
        workload=spec.kind,
    )


def _corpus_for_spec(corpora: Mapping[CorpusKey, CorpusFile], spec: WorkloadSpec) -> CorpusFile:
    for corpus in corpora.values():
        if corpus.path == spec.path:
            return corpus
    raise KeyError(f"no corpus metadata found for {spec.path}")


def _locale_scaling_evidence(results: Sequence[WorkloadResult]) -> list[LocaleScalingResult]:
    evidence: list[LocaleScalingResult] = []
    for result in results:
        if result["workload"] != "sync_stream" or result["strings"] != "sanitized":
            continue
        if result["name"].endswith("bounded_length"):
            continue
        evidence.append(
            LocaleScalingResult(
                format=result["format"],
                locale_count=result["locale_count"],
                median_seconds=result["median_seconds"],
                peak_python_allocated_bytes=result["peak_python_allocated_bytes"],
                peak_rss_delta_bytes=result["peak_rss_delta_bytes"],
                rows=result["rows"],
                rows_per_second=result["rows_per_second"],
            )
        )
    return evidence


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _bounded_memory_evidence(
    results: Sequence[WorkloadResult],
    arguments: Arguments,
) -> BoundedMemoryResult:
    format_name = arguments.formats[0]
    locale_count = arguments.locale_counts[0]
    small_name = f"{format_name}.sync_stream.sanitized.locales_{locale_count}"
    large_name = f"{format_name}.sync_stream.sanitized.bounded_length"
    small = next(result for result in results if result["name"] == small_name)
    large = next(result for result in results if result["name"] == large_name)
    row_growth = _ratio(large["rows"], small["rows"])
    allocation_growth = _ratio(large["peak_python_allocated_bytes"], small["peak_python_allocated_bytes"])
    allocation_limit = max(2.0, row_growth * 0.25)
    allocation_evaluated = not arguments.skip_allocations
    return BoundedMemoryResult(
        allocation_assertion_evaluated=allocation_evaluated,
        allocation_growth_limit=allocation_limit,
        allocation_growth_passed=not allocation_evaluated or allocation_growth <= allocation_limit,
        format=format_name,
        input_byte_growth_ratio=_ratio(large["input_bytes"], small["input_bytes"]),
        large_input_bytes=large["input_bytes"],
        large_input_units=large["input_units"],
        large_peak_python_allocated_bytes=large["peak_python_allocated_bytes"],
        large_peak_rss_delta_bytes=large["peak_rss_delta_bytes"],
        large_rows=large["rows"],
        python_allocation_growth_ratio=allocation_growth,
        row_growth_ratio=row_growth,
        rss_delta_growth_ratio=_ratio(large["peak_rss_delta_bytes"], small["peak_rss_delta_bytes"]),
        rss_supported=small["rss_supported"] and large["rss_supported"],
        small_input_bytes=small["input_bytes"],
        small_input_units=small["input_units"],
        small_peak_python_allocated_bytes=small["peak_python_allocated_bytes"],
        small_peak_rss_delta_bytes=small["peak_rss_delta_bytes"],
        small_rows=small["rows"],
    )


def _corpus_results(corpora: Mapping[CorpusKey, CorpusFile], directory: Path) -> list[CorpusResult]:
    results: list[CorpusResult] = []
    for corpus in corpora.values():
        results.append(
            CorpusResult(
                bytes=corpus.bytes,
                format=corpus.key.format_name,
                locale_count=corpus.key.locale_count,
                path=str(corpus.path.relative_to(directory)),
                role=corpus.key.role,
                sha256=corpus.sha256,
                units=corpus.key.units,
            )
        )
    return results


def _repository_state() -> tuple[str, bool]:
    root = Path(__file__).resolve().parents[1]
    try:
        revision = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ("git", "status", "--porcelain"),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ("unavailable", False)
    return (revision or "unavailable", bool(status.strip()))


def _native_interchange_disabled() -> bool:
    return os.environ.get("LOKIT_DISABLE_RUST_INTERCHANGE", "").strip().lower() in {"1", "true", "yes", "on"}


def _backend_identity() -> tuple[str, str]:
    specification = importlib.util.find_spec("lokit._interchange_rust")
    if specification is None or specification.origin is None:
        return ("unavailable", "unavailable")
    origin = Path(specification.origin)
    if not origin.is_file():
        return (str(origin), "unavailable")
    return (str(origin), _sha256(origin))


def _run_suite_in_directory(arguments: Arguments, directory: Path) -> SuiteResult:
    package_version = importlib.metadata.version("lokit-python")
    native_backend_version = backend_version()
    if package_version != native_backend_version:
        raise RuntimeError(
            f"package/native version mismatch: lokit-python={package_version}, backend={native_backend_version}"
        )
    backend_origin, backend_sha256 = _backend_identity()
    repository_revision, repository_dirty = _repository_state()
    corpora = _generate_corpora(directory, arguments)
    results: list[WorkloadResult] = []
    workloads = _workloads(arguments, corpora)
    for index, spec in enumerate(workloads, start=1):
        print(f"[{index}/{len(workloads)}] {spec.name}", file=sys.stderr, flush=True)
        expected = _summarize_values(_expected_rows(spec))
        corpus = _corpus_for_spec(corpora, spec)
        results.append(_measure(spec, expected, corpus.bytes, arguments))

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    bounded_memory = _bounded_memory_evidence(results, arguments)
    if not bounded_memory["allocation_growth_passed"]:
        observed = bounded_memory["python_allocation_growth_ratio"]
        limit = bounded_memory["allocation_growth_limit"]
        raise RuntimeError(f"bounded stream allocation growth {observed:.3f}x exceeded the {limit:.3f}x gate")
    return SuiteResult(
        configuration=ConfigurationResult(
            formats=list(arguments.formats),
            locale_counts=list(arguments.locale_counts),
            memory_scale=arguments.memory_scale,
            repeats=arguments.repeats,
            skip_allocations=arguments.skip_allocations,
            skip_rss=arguments.skip_rss,
            units=arguments.units,
            warmups=arguments.warmups,
        ),
        corpora=_corpus_results(corpora, directory),
        environment=EnvironmentResult(
            backend_origin=backend_origin,
            backend_sha256=backend_sha256,
            backend_version=native_backend_version,
            benchmark_sha256=_sha256(Path(__file__).resolve()),
            cpu=platform.processor() or "unknown",
            executable=sys.executable,
            logical_cpus=os.cpu_count() or 0,
            machine=platform.machine(),
            native_interchange_disabled=_native_interchange_disabled(),
            package_version=package_version,
            platform=platform.platform(),
            python=sys.version.split()[0],
            python_implementation=platform.python_implementation(),
            repository_dirty=repository_dirty,
            repository_revision=repository_revision,
        ),
        evidence=EvidenceResult(
            bounded_memory=bounded_memory,
            locale_scaling=_locale_scaling_evidence(results),
        ),
        generated_at_utc=generated_at,
        methodology=MethodologyResult(
            checksum=(
                "SHA-256 over ordered UTF-8 default projection fields separated by 0x1f; rows terminated by 0x1e"
            ),
            generation="Deterministic incremental writes; at most one source unit and its targets are resident",
            memory_contracts=(
                "sync/async projections are bounded streams; streaming split is bounded via temporary .lokit spools; "
                "parse.to_dict and Base split are intentionally materialized and excluded from the bounded-growth gate"
            ),
            python_allocations="tracemalloc peak from a dedicated, untimed, checksum-validated execution",
            rss=("Absolute and baseline-delta resource.ru_maxrss from one checksum-validated workload per subprocess"),
            timing=(
                "Warm-ups excluded; each sample includes projection/split construction, full consumption, and checksum"
            ),
        ),
        results=results,
        schema_version=1,
    )


def _run_suite(arguments: Arguments) -> SuiteResult:
    if arguments.corpus_dir is not None:
        return _run_suite_in_directory(arguments, arguments.corpus_dir)
    with tempfile.TemporaryDirectory(prefix="lokit-interchange-api-benchmark-") as temporary_directory:
        return _run_suite_in_directory(arguments, Path(temporary_directory))


def _write_output(result: SuiteResult, output: Path | None) -> None:
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as temporary:
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, output)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            with contextlib.suppress(OSError):
                temporary_path.unlink(missing_ok=True)
            raise
    print(encoded, end="")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "_worker":
        _worker(_worker_arguments(sys.argv[2:]))
        return
    arguments = _arguments(sys.argv[1:])
    _write_output(_run_suite(arguments), arguments.output)


if __name__ == "__main__":
    main()
