from __future__ import annotations

import argparse
import asyncio
import contextlib
import gc
import hashlib
import importlib.metadata
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict

from lokit.data.structure import Data, StreamingStructure, TranslationStatus
from lokit.database import TranslationMemory, connect, database_schema_statements

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence

SOURCE_LOCALE = "en-US"
TARGET_LOCALE = "fr-FR"
PROJECT = "lokit-database-benchmark"
DOMAIN = "performance"
DATABASE_NAME = "lokit_test"
URI_ENVIRONMENT_VARIABLE = "LOKIT_TEST_PG_URI"
DEFAULT_UNITS = 6_000
DEFAULT_REPEATS = 3
DEFAULT_WARMUPS = 1
DEFAULT_BATCH_SIZE = 750
MAX_LOOKUPS = 64
CHECKSUM_SEPARATOR = b"\x1f"
CHECKSUM_RECORD_TERMINATOR = b"\x1e"


@dataclass(frozen=True, slots=True)
class Arguments:
    batch_size: int
    output: Path | None
    repeats: int
    units: int
    uri: str
    warmups: int


@dataclass(frozen=True, slots=True)
class DatabaseIdentity:
    database: str
    endpoint: str
    schema: str
    server_version: str
    server_version_number: int


@dataclass(frozen=True, slots=True)
class Checksum:
    digest: str
    units: int
    witness: int


@dataclass(frozen=True, slots=True)
class SampleMeasurement:
    exact_batch_seconds: float
    exact_checksum: str
    exact_latency_seconds: tuple[float, ...]
    load_api_seconds: float
    load_seconds: float
    materialized: Checksum
    materialized_seconds: float
    row_count: int
    streaming: Checksum
    streaming_seconds: float


class ConfigurationResult(TypedDict):
    batch_size: int
    exact_lookups_per_sample: int
    repeats: int
    source_locale: str
    target_locale: str
    units: int
    uri_environment_variable: str
    warmups: int


class DatabaseResult(TypedDict):
    database: str
    endpoint: str
    partitioned: bool
    reset_policy: str
    schema: str
    server_version: str
    server_version_number: int


class EnvironmentResult(TypedDict):
    benchmark_sha256: str
    cpu: str
    executable: str
    logical_cpus: int
    machine: str
    package_version: str
    platform: str
    python: str
    python_implementation: str
    repository_dirty: bool
    repository_revision: str


class ThroughputResult(TypedDict):
    api_durations_seconds: list[float]
    checksum: str
    durations_seconds: list[float]
    maximum_seconds: float
    median_seconds: float
    memory_contract: str
    minimum_seconds: float
    name: str
    operations_per_sample: int
    p95_seconds: float
    throughput_per_second: float
    unit: str
    validation: str


class ExactMatchResult(TypedDict):
    batch_durations_seconds: list[float]
    checksum: str
    latency_samples_seconds: list[float]
    maximum_latency_seconds: float
    median_latency_seconds: float
    name: str
    p95_latency_seconds: float
    p99_latency_seconds: float
    queries_per_sample: int
    queries_per_second: float
    validation: str


class ResultsResult(TypedDict):
    exact_match: ExactMatchResult
    load: ThroughputResult
    materialized_retrieval: ThroughputResult
    streaming_retrieval: ThroughputResult


class ValidationResult(TypedDict):
    expected_checksum: str
    expected_units: int
    expected_witness: int
    measured_samples: int
    policy: str


class MethodologyResult(TypedDict):
    exact_match: str
    load: str
    materialized_retrieval: str
    reset: str
    streaming_retrieval: str
    timing: str


class SuiteResult(TypedDict):
    configuration: ConfigurationResult
    database: DatabaseResult
    environment: EnvironmentResult
    generated_at_utc: str
    methodology: MethodologyResult
    results: ResultsResult
    schema_version: int
    validation: ValidationResult


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _non_negative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return parsed


def _arguments(argv: Sequence[str]) -> Arguments:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark Lokit's public PostgreSQL ingestion, exact matching, streaming retrieval, "
            "and separately labelled materialized retrieval APIs."
        )
    )
    parser.add_argument(
        "--uri",
        default=os.environ.get(URI_ENVIRONMENT_VARIABLE, ""),
        help=f"PostgreSQL URI (default: ${URI_ENVIRONMENT_VARIABLE})",
    )
    parser.add_argument(
        "--units",
        type=_positive,
        default=DEFAULT_UNITS,
        help=f"deterministic translation units per sample (default: {DEFAULT_UNITS})",
    )
    parser.add_argument(
        "--repeats",
        type=_positive,
        default=DEFAULT_REPEATS,
        help=f"checksum-validated measured samples (default: {DEFAULT_REPEATS})",
    )
    parser.add_argument(
        "--warmups",
        type=_non_negative,
        default=DEFAULT_WARMUPS,
        help=f"untimed warm-up samples (default: {DEFAULT_WARMUPS})",
    )
    parser.add_argument(
        "--batch-size",
        type=_positive,
        default=DEFAULT_BATCH_SIZE,
        help=f"bounded database load and retrieval batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument("--output", type=Path, help="atomically write the JSON report to this path")
    namespace = parser.parse_args(argv)
    uri = str(namespace.uri).strip()
    if not uri:
        parser.error(f"--uri is required when ${URI_ENVIRONMENT_VARIABLE} is unset")
    output = namespace.output
    if output is not None and not isinstance(output, Path):
        raise TypeError("--output must resolve to a path")
    return Arguments(
        batch_size=int(namespace.batch_size),
        output=output,
        repeats=int(namespace.repeats),
        units=int(namespace.units),
        uri=uri,
        warmups=int(namespace.warmups),
    )


def _unit_key(index: int) -> str:
    return f"benchmark-unit-{index:08d}"


def _token(index: int) -> int:
    return (index * 2_654_435_761) % 1_000_003


def _source(index: int) -> str:
    return f"Benchmark source {index:08d}: token {_token(index):06d}."


def _target(index: int) -> str:
    return f"Référence cible {index:08d} : jeton {_token(index):06d}."


def _records(units: int) -> Iterator[tuple[str, Data]]:
    for index in range(units):
        yield (
            _unit_key(index),
            Data(
                source=_source(index),
                target=_target(index),
                status=TranslationStatus.TRANSLATED,
                extensions={"benchmark_index": str(index)},
            ),
        )


def _document(units: int) -> StreamingStructure:
    return StreamingStructure(
        source_locale=SOURCE_LOCALE,
        target_locale=TARGET_LOCALE,
        items=_records(units),
        source_language="en",
        target_language="fr",
        extensions={"benchmark": "database"},
    )


def _checksum_values(values: Iterator[tuple[str, ...]]) -> Checksum:
    digest = hashlib.sha256()
    units = 0
    witness = 0
    for row in values:
        for position, value in enumerate(row, start=1):
            encoded = value.encode("utf-8")
            digest.update(encoded)
            digest.update(CHECKSUM_SEPARATOR)
            witness += position * len(encoded)
        digest.update(CHECKSUM_RECORD_TERMINATOR)
        units += 1
    return Checksum(digest=digest.hexdigest(), units=units, witness=witness)


def _expected_values(units: int) -> Iterator[tuple[str, ...]]:
    for index in range(units):
        yield (
            _unit_key(index),
            _source(index),
            _target(index),
            TranslationStatus.TRANSLATED.value,
            str(index),
            PROJECT,
            DOMAIN,
        )


def _data_values(items: Iterator[tuple[str, Data]]) -> Iterator[tuple[str, ...]]:
    for unit_key, data in items:
        target = data.target
        if target is None:
            raise RuntimeError(f"retrieved unit {unit_key!r} has no target")
        yield (
            unit_key,
            data.source,
            target,
            data.status.value,
            data.extensions.get("benchmark_index", ""),
            data.extensions.get("project", ""),
            data.extensions.get("domain", ""),
        )


async def _stream_values(stream: AsyncIterator[tuple[str, Data]]) -> AsyncIterator[tuple[str, ...]]:
    async for unit_key, data in stream:
        target = data.target
        if target is None:
            raise RuntimeError(f"retrieved unit {unit_key!r} has no target")
        yield (
            unit_key,
            data.source,
            target,
            data.status.value,
            data.extensions.get("benchmark_index", ""),
            data.extensions.get("project", ""),
            data.extensions.get("domain", ""),
        )


async def _checksum_async(values: AsyncIterator[tuple[str, ...]]) -> Checksum:
    digest = hashlib.sha256()
    units = 0
    witness = 0
    async for row in values:
        for position, value in enumerate(row, start=1):
            encoded = value.encode("utf-8")
            digest.update(encoded)
            digest.update(CHECKSUM_SEPARATOR)
            witness += position * len(encoded)
        digest.update(CHECKSUM_RECORD_TERMINATOR)
        units += 1
    return Checksum(digest=digest.hexdigest(), units=units, witness=witness)


def _required_row(row: tuple[object, ...] | None, operation: str) -> tuple[object, ...]:
    if row is None:
        raise RuntimeError(f"PostgreSQL returned no row for {operation}")
    return row


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"PostgreSQL field {field!r} was not text")
    return value


def _required_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"PostgreSQL field {field!r} was not an integer")
    return value


def _endpoint(uri: str) -> str:
    values = conninfo_to_dict(uri)
    host = values.get("host", "local-socket")
    port = values.get("port", "default")
    user = values.get("user", "default")
    return f"host={host};port={port};user={user}"


async def _prepare_database(uri: str) -> DatabaseIdentity:
    connection: AsyncConnection[tuple[object, ...]] = await AsyncConnection.connect(uri, autocommit=True)
    async with connection, connection.cursor() as cursor:
        await cursor.execute(
            "SELECT current_database(), current_schema(), current_setting('server_version'), "
            "current_setting('server_version_num')::integer"
        )
        row = _required_row(await cursor.fetchone(), "database identity")
        database = _required_text(row[0], "current_database")
        schema = _required_text(row[1], "current_schema")
        server_version = _required_text(row[2], "server_version")
        server_version_number = _required_int(row[3], "server_version_num")
        if database != DATABASE_NAME:
            raise RuntimeError(
                f"refusing to reset database {database!r}; this benchmark is restricted to {DATABASE_NAME!r}"
            )
        if schema != "public":
            raise RuntimeError(f"refusing to reset non-public schema {schema!r}")
        await cursor.execute("DROP TABLE IF EXISTS public.unit_comments CASCADE")
        await cursor.execute("DROP TABLE IF EXISTS public.segment_parts CASCADE")
        await cursor.execute("DROP TABLE IF EXISTS public.unit_tags CASCADE")
        await cursor.execute("DROP TABLE IF EXISTS public.translation_units CASCADE")
        await cursor.execute("DROP TABLE IF EXISTS public._lokit_meta CASCADE")
        for statement in database_schema_statements(partitioned=True):
            await cursor.execute(statement)
    return DatabaseIdentity(
        database=database,
        endpoint=_endpoint(uri),
        schema=schema,
        server_version=server_version,
        server_version_number=server_version_number,
    )


async def _truncate_lokit_tables(uri: str) -> None:
    connection: AsyncConnection[tuple[object, ...]] = await AsyncConnection.connect(uri, autocommit=True)
    async with connection, connection.cursor() as cursor:
        await cursor.execute("SELECT current_database()")
        row = _required_row(await cursor.fetchone(), "truncate safety check")
        database = _required_text(row[0], "current_database")
        if database != DATABASE_NAME:
            raise RuntimeError(f"refusing to truncate database {database!r}")
        await cursor.execute(
            "TRUNCATE TABLE public.unit_comments, public.segment_parts, public.unit_tags, "
            "public.translation_units RESTART IDENTITY CASCADE"
        )
        await cursor.execute("SELECT count(*) FROM public.translation_units")
        count_row = _required_row(await cursor.fetchone(), "post-truncate count")
        if _required_int(count_row[0], "translation_units count") != 0:
            raise RuntimeError("database reset left translation rows behind")


async def _database_row_count(uri: str) -> int:
    connection: AsyncConnection[tuple[object, ...]] = await AsyncConnection.connect(uri, autocommit=True)
    async with connection, connection.cursor() as cursor:
        await cursor.execute("SELECT count(*) FROM public.translation_units")
        row = _required_row(await cursor.fetchone(), "translation unit count")
        return _required_int(row[0], "translation_units count")


async def _analyze_database(uri: str) -> None:
    connection: AsyncConnection[tuple[object, ...]] = await AsyncConnection.connect(uri, autocommit=True)
    async with connection, connection.cursor() as cursor:
        await cursor.execute("ANALYZE public.translation_units")


def _lookup_indices(units: int) -> tuple[int, ...]:
    count = min(units, MAX_LOOKUPS)
    if count == 1:
        return (0,)
    return tuple((position * (units - 1)) // (count - 1) for position in range(count))


def _lookup_checksum(indices: Sequence[int]) -> str:
    digest = hashlib.sha256()
    for index in indices:
        digest.update(_unit_key(index).encode("utf-8"))
        digest.update(CHECKSUM_RECORD_TERMINATOR)
    return digest.hexdigest()


async def _measure_exact_matches(
    memory: TranslationMemory,
    indices: Sequence[int],
) -> tuple[float, tuple[float, ...], str]:
    digest = hashlib.sha256()
    latencies: list[float] = []
    batch_started = time.perf_counter()
    for index in indices:
        started = time.perf_counter()
        results = await memory.match(
            source=_source(index),
            source_locale=SOURCE_LOCALE,
            target_locale=TARGET_LOCALE,
            previous_source="benchmark-query-context",
            limit=1,
        )
        latencies.append(time.perf_counter() - started)
        if len(results) != 1:
            raise RuntimeError(f"exact lookup for index {index} returned {len(results)} results")
        result = results[0]
        expected_key = _unit_key(index)
        if result.unit_id != expected_key or result.kind != "exact" or not result.source_equal:
            raise RuntimeError(
                f"exact lookup for {expected_key!r} returned "
                f"unit={result.unit_id!r}, kind={result.kind!r}, source_equal={result.source_equal!r}"
            )
        digest.update(result.unit_id.encode("utf-8"))
        digest.update(CHECKSUM_RECORD_TERMINATOR)
    return (time.perf_counter() - batch_started, tuple(latencies), digest.hexdigest())


def _verify_checksum(observed: Checksum, expected: Checksum, workload: str) -> None:
    if observed != expected:
        raise RuntimeError(f"{workload} checksum gate failed: expected {expected!r}, observed {observed!r}")


async def _run_sample(
    memory: TranslationMemory,
    arguments: Arguments,
    expected: Checksum,
    lookup_indices: tuple[int, ...],
    expected_lookup_checksum: str,
) -> SampleMeasurement:
    await _truncate_lokit_tables(arguments.uri)
    gc.collect()
    load_started = time.perf_counter()
    stats = await memory.load(
        _document(arguments.units),
        batch_size=arguments.batch_size,
        project=PROJECT,
        domain=DOMAIN,
        progress=False,
    )
    load_seconds = time.perf_counter() - load_started
    if stats.units_read != arguments.units or stats.units_written != arguments.units:
        raise RuntimeError(
            f"load count gate failed: expected {arguments.units}, read {stats.units_read}, wrote {stats.units_written}"
        )
    row_count = await _database_row_count(arguments.uri)
    if row_count != arguments.units:
        raise RuntimeError(f"database count gate failed: expected {arguments.units}, observed {row_count}")
    await _analyze_database(arguments.uri)

    exact_batch_seconds, exact_latency_seconds, exact_checksum = await _measure_exact_matches(
        memory,
        lookup_indices,
    )
    if exact_checksum != expected_lookup_checksum:
        raise RuntimeError(
            f"exact-match checksum gate failed: expected {expected_lookup_checksum}, observed {exact_checksum}"
        )

    stream_started = time.perf_counter()
    streaming = await _checksum_async(
        _stream_values(
            memory.stream(
                source_locale=SOURCE_LOCALE,
                target_locale=TARGET_LOCALE,
                include_tags=False,
                batch_size=arguments.batch_size,
            )
        )
    )
    streaming_seconds = time.perf_counter() - stream_started
    _verify_checksum(streaming, expected, "streaming retrieval")

    materialized_started = time.perf_counter()
    document = await memory.to_document(
        source_locale=SOURCE_LOCALE,
        target_locale=TARGET_LOCALE,
        include_tags=False,
    )
    materialized = _checksum_values(_data_values(iter(document.data.items())))
    materialized_seconds = time.perf_counter() - materialized_started
    _verify_checksum(materialized, expected, "materialized retrieval")
    return SampleMeasurement(
        exact_batch_seconds=exact_batch_seconds,
        exact_checksum=exact_checksum,
        exact_latency_seconds=exact_latency_seconds,
        load_api_seconds=stats.seconds,
        load_seconds=load_seconds,
        materialized=materialized,
        materialized_seconds=materialized_seconds,
        row_count=row_count,
        streaming=streaming,
        streaming_seconds=streaming_seconds,
    )


def _percentile(samples: Sequence[float], percentile: int) -> float:
    if not samples:
        raise ValueError("at least one sample is required")
    ordered = sorted(samples)
    index = max(0, (percentile * len(ordered) + 99) // 100 - 1)
    return ordered[index]


def _throughput_result(
    name: str,
    durations: Sequence[float],
    operations: int,
    checksum: str,
    memory_contract: str,
    validation: str,
    api_durations: Sequence[float] = (),
) -> ThroughputResult:
    median = float(statistics.median(durations))
    return ThroughputResult(
        api_durations_seconds=list(api_durations),
        checksum=checksum,
        durations_seconds=list(durations),
        maximum_seconds=max(durations),
        median_seconds=median,
        memory_contract=memory_contract,
        minimum_seconds=min(durations),
        name=name,
        operations_per_sample=operations,
        p95_seconds=_percentile(durations, 95),
        throughput_per_second=operations / median,
        unit="translation units",
        validation=validation,
    )


def _exact_match_result(
    samples: Sequence[SampleMeasurement],
    queries_per_sample: int,
    checksum: str,
) -> ExactMatchResult:
    batch_durations = [sample.exact_batch_seconds for sample in samples]
    latencies = [latency for sample in samples for latency in sample.exact_latency_seconds]
    median_batch = float(statistics.median(batch_durations))
    return ExactMatchResult(
        batch_durations_seconds=batch_durations,
        checksum=checksum,
        latency_samples_seconds=latencies,
        maximum_latency_seconds=max(latencies),
        median_latency_seconds=float(statistics.median(latencies)),
        name="exact_match",
        p95_latency_seconds=_percentile(latencies, 95),
        p99_latency_seconds=_percentile(latencies, 99),
        queries_per_sample=queries_per_sample,
        queries_per_second=queries_per_sample / median_batch,
        validation="every query returned exactly the deterministic expected unit as an exact source match",
    )


def _repository_state() -> tuple[str, bool]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ("unavailable", False)
    return (revision or "unavailable", bool(status.strip()))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _result(
    arguments: Arguments,
    identity: DatabaseIdentity,
    expected: Checksum,
    lookup_indices: tuple[int, ...],
    expected_lookup_checksum: str,
    samples: Sequence[SampleMeasurement],
) -> SuiteResult:
    revision, dirty = _repository_state()
    load_durations = [sample.load_seconds for sample in samples]
    load_api_durations = [sample.load_api_seconds for sample in samples]
    stream_durations = [sample.streaming_seconds for sample in samples]
    materialized_durations = [sample.materialized_seconds for sample in samples]
    return SuiteResult(
        configuration=ConfigurationResult(
            batch_size=arguments.batch_size,
            exact_lookups_per_sample=len(lookup_indices),
            repeats=arguments.repeats,
            source_locale=SOURCE_LOCALE,
            target_locale=TARGET_LOCALE,
            units=arguments.units,
            uri_environment_variable=URI_ENVIRONMENT_VARIABLE,
            warmups=arguments.warmups,
        ),
        database=DatabaseResult(
            database=identity.database,
            endpoint=identity.endpoint,
            partitioned=True,
            reset_policy=(
                "schema recreated once after database-name/schema safety gates; all Lokit data tables truncated "
                "with RESTART IDENTITY CASCADE before every warm-up and measured sample"
            ),
            schema=identity.schema,
            server_version=identity.server_version,
            server_version_number=identity.server_version_number,
        ),
        environment=EnvironmentResult(
            benchmark_sha256=_sha256(Path(__file__).resolve()),
            cpu=platform.processor() or "unknown",
            executable=sys.executable,
            logical_cpus=os.cpu_count() or 0,
            machine=platform.machine(),
            package_version=importlib.metadata.version("lokit-python"),
            platform=platform.platform(),
            python=sys.version.split()[0],
            python_implementation=platform.python_implementation(),
            repository_dirty=dirty,
            repository_revision=revision,
        ),
        generated_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        methodology=MethodologyResult(
            exact_match=(
                "up to 64 evenly distributed deterministic sources queried serially through "
                "TranslationMemory.match; each individual await is timed and semantically checked"
            ),
            load=(
                "TranslationMemory.load consumes a fresh one-shot StreamingStructure generator; only the current "
                "unit plus Lokit's configured bounded batch can be resident from the source"
            ),
            materialized_retrieval=(
                "separately labelled TranslationMemory.to_document materialization; timing includes construction "
                "and the same ordered checksum traversal, and RAM is proportional to returned units"
            ),
            reset=(
                "destructive operations are hard-gated to current_database()='lokit_test' and current_schema()='public'"
            ),
            streaming_retrieval=(
                "TranslationMemory.stream is exhausted with include_tags=False and a bounded server-cursor batch; "
                "timing includes ordered checksum consumption"
            ),
            timing=(
                "perf_counter wall time; warm-ups excluded; PostgreSQL ANALYZE and independent validation counts "
                "are outside timed workloads"
            ),
        ),
        results=ResultsResult(
            exact_match=_exact_match_result(samples, len(lookup_indices), expected_lookup_checksum),
            load=_throughput_result(
                "streaming_load",
                load_durations,
                arguments.units,
                expected.digest,
                "bounded StreamingStructure source and bounded load batch",
                "API read/write counts and independent PostgreSQL row counts equal the requested unit count",
                load_api_durations,
            ),
            materialized_retrieval=_throughput_result(
                "materialized_retrieval",
                materialized_durations,
                arguments.units,
                expected.digest,
                "materialized BaseStructure; resident memory grows with returned units",
                "count, ordered content, status, project, domain, and extension checksum",
            ),
            streaming_retrieval=_throughput_result(
                "streaming_retrieval",
                stream_durations,
                arguments.units,
                expected.digest,
                "bounded named server cursor and bounded deserialization batch",
                "count, ordered content, status, project, domain, and extension checksum",
            ),
        ),
        schema_version=1,
        validation=ValidationResult(
            expected_checksum=expected.digest,
            expected_units=expected.units,
            expected_witness=expected.witness,
            measured_samples=len(samples),
            policy=(
                "every timed sample must pass API counts, independent SQL counts, exact-match identity, and "
                "SHA-256 plus witness checks over all retrieved fields"
            ),
        ),
    )


async def _run_suite(arguments: Arguments) -> SuiteResult:
    identity = await _prepare_database(arguments.uri)
    expected = _checksum_values(_expected_values(arguments.units))
    lookup_indices = _lookup_indices(arguments.units)
    expected_lookup_checksum = _lookup_checksum(lookup_indices)
    memory = await connect(
        arguments.uri,
        pool_size=4,
        min_size=2,
        pipeline=False,
    )
    samples: list[SampleMeasurement] = []
    try:
        await memory.setup(partitioned=True)
        total = arguments.warmups + arguments.repeats
        for index in range(total):
            measured = index >= arguments.warmups
            label = "measured" if measured else "warm-up"
            ordinal = index - arguments.warmups + 1 if measured else index + 1
            count = arguments.repeats if measured else arguments.warmups
            print(f"[{label} {ordinal}/{count}] {arguments.units} units", file=sys.stderr, flush=True)
            sample = await _run_sample(
                memory,
                arguments,
                expected,
                lookup_indices,
                expected_lookup_checksum,
            )
            if measured:
                samples.append(sample)
    finally:
        await memory.close()
    if len(samples) != arguments.repeats:
        raise RuntimeError(f"expected {arguments.repeats} measured samples, observed {len(samples)}")
    return _result(
        arguments,
        identity,
        expected,
        lookup_indices,
        expected_lookup_checksum,
        samples,
    )


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
    arguments = _arguments(sys.argv[1:])
    _write_output(asyncio.run(_run_suite(arguments)), arguments.output)


if __name__ == "__main__":
    main()
