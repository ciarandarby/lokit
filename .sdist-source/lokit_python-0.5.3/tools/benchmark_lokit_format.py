from __future__ import annotations

import argparse
import asyncio
import gc
import json
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import lokit
from lokit._interchange_rust import backend_version
from lokit.data.structure import Data, StreamingStructure, TranslationStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

_FNV_OFFSET = 1_469_598_103_934_665_603
_FNV_PRIME = 1_099_511_628_211
_UINT64_MASK = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class Arguments:
    output: Path | None
    repeats: int
    units: int


class WorkloadResult(TypedDict):
    durations_seconds: list[float]
    median_seconds: float
    peak_python_bytes: int
    units_per_second: float


class BenchmarkResult(TypedDict):
    backend_version: str
    file_bytes: int
    machine: str
    platform: str
    python: str
    repeats: int
    schema_version: int
    semantic_checksum: str
    units: int
    workloads: dict[str, WorkloadResult]


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _arguments() -> Arguments:
    parser = argparse.ArgumentParser(description="Benchmark native .lokit streaming and materialized APIs.")
    parser.add_argument("--units", type=_positive, default=50_000)
    parser.add_argument("--repeats", type=_positive, default=5)
    parser.add_argument("--output", type=Path)
    namespace = parser.parse_args()
    output_value = namespace.output
    if output_value is not None and not isinstance(output_value, Path):
        raise TypeError("--output must resolve to a path")
    return Arguments(output=output_value, repeats=int(namespace.repeats), units=int(namespace.units))


def _update_checksum(checksum: int, value: str) -> int:
    for byte in value.encode("utf-8"):
        checksum ^= byte
        checksum = (checksum * _FNV_PRIME) & _UINT64_MASK
    checksum ^= 0x1F
    return (checksum * _FNV_PRIME) & _UINT64_MASK


def _records(units: int) -> Iterator[tuple[str, Data]]:
    for index in range(units):
        suffix = index % 97
        yield (
            f"unit_{index:08d}",
            Data(
                source=f"Source message {index:08d}: café & tea <{suffix:02d}>.",
                target=f"Traduction {index:08d} : café & thé <{suffix:02d}>.",
                status=TranslationStatus.TRANSLATED,
            ),
        )


def _consume(items: Iterator[tuple[str, Data]]) -> int:
    checksum = _FNV_OFFSET
    units = 0
    for unit_id, data in items:
        checksum = _update_checksum(checksum, unit_id)
        checksum = _update_checksum(checksum, data.source)
        checksum = _update_checksum(checksum, data.target or "")
        units += 1
    return checksum ^ units


async def _consume_async(items: AsyncIterator[tuple[str, Data]]) -> int:
    checksum = _FNV_OFFSET
    units = 0
    async for unit_id, data in items:
        checksum = _update_checksum(checksum, unit_id)
        checksum = _update_checksum(checksum, data.source)
        checksum = _update_checksum(checksum, data.target or "")
        units += 1
    return checksum ^ units


def _streaming_document(units: int) -> StreamingStructure:
    return StreamingStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        items=_records(units),
        target_locales=("fr-FR",),
        source_language="en",
        target_language="fr",
        target_languages=("fr",),
    )


def _measure(
    operation: Callable[[], int],
    expected: int,
    units: int,
    repeats: int,
    verifier: Callable[[], int] | None = None,
) -> WorkloadResult:
    warm_up_checksum = operation()
    if verifier is not None:
        warm_up_checksum = verifier()
    if warm_up_checksum != expected:
        raise RuntimeError("benchmark workload failed its semantic checksum gate during warm-up")
    durations: list[float] = []
    for _ in range(repeats):
        gc.collect()
        started = time.perf_counter()
        checksum = operation()
        duration = time.perf_counter() - started
        if verifier is not None:
            checksum = verifier()
        if checksum != expected:
            raise RuntimeError("benchmark workload failed its semantic checksum gate")
        durations.append(duration)

    gc.collect()
    tracemalloc.start()
    try:
        checksum = operation()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if verifier is not None:
        checksum = verifier()
    if checksum != expected:
        raise RuntimeError("benchmark memory workload failed its semantic checksum gate")

    median = float(statistics.median(durations))
    return WorkloadResult(
        durations_seconds=durations,
        median_seconds=median,
        peak_python_bytes=peak,
        units_per_second=units / median,
    )


def _run(arguments: Arguments) -> BenchmarkResult:
    expected = _consume(_records(arguments.units))
    with tempfile.TemporaryDirectory(prefix="lokit-format-benchmark-") as temporary_directory:
        directory = Path(temporary_directory)
        path = directory / "messages.lokit"
        async_path = directory / "messages-async.lokit"

        def sync_export() -> int:
            lokit.export.lokit(_streaming_document(arguments.units), path)
            return expected

        async def async_export_operation() -> int:
            await lokit.export.async_.lokit(_streaming_document(arguments.units), async_path)
            return expected

        def async_export() -> int:
            return asyncio.run(async_export_operation())

        def stream_parse() -> int:
            return _consume(iter(lokit.stream.lokit(str(path)).items))

        def materialized_parse() -> int:
            return _consume(iter(lokit.parse.lokit(str(path), progress=False).data.items()))

        def verify_async_export() -> int:
            return _consume(iter(lokit.parse.lokit(str(async_path), progress=False).data.items()))

        def async_stream_parse() -> int:
            return asyncio.run(_consume_async(lokit.stream.async_.lokit(str(path))))

        sync_export()
        workloads = {
            "sync_export": _measure(
                sync_export,
                expected,
                arguments.units,
                arguments.repeats,
                stream_parse,
            ),
            "sync_stream_parse": _measure(stream_parse, expected, arguments.units, arguments.repeats),
            "sync_materialized_parse": _measure(materialized_parse, expected, arguments.units, arguments.repeats),
            "async_export": _measure(
                async_export,
                expected,
                arguments.units,
                arguments.repeats,
                verify_async_export,
            ),
            "async_stream_parse": _measure(async_stream_parse, expected, arguments.units, arguments.repeats),
        }
        return BenchmarkResult(
            backend_version=backend_version(),
            file_bytes=path.stat().st_size,
            machine=platform.machine(),
            platform=platform.platform(),
            python=sys.version.split()[0],
            repeats=arguments.repeats,
            schema_version=1,
            semantic_checksum=str(expected),
            units=arguments.units,
            workloads=workloads,
        )


def main() -> None:
    arguments = _arguments()
    result = _run(arguments)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")


if __name__ == "__main__":
    main()
