from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from os import cpu_count

from lxml import etree

from lokit.data.structure import Data
from lokit.parsers.tmx.extraction import TmxExtractor
from lokit.parsers.tmx.models import TmxParseMode
from lokit.parsers.tmx.xml_utils import clear_element, iterparse_safe, local_name


ParallelExtractItem = tuple[str, Data]
ParallelExtractBatch = list[ParallelExtractItem]


@dataclass(frozen=True, slots=True)
class TmxParallelOptions:
    workers: int = 0
    batch_units: int = 5000
    batch_bytes: int = 16 * 1024 * 1024
    max_pending_batches: int = 2

    def resolved_workers(self) -> int:
        if self.workers > 0:
            return self.workers
        available = cpu_count() or 1
        return max(1, min(available, 4))

    def validate(self) -> None:
        if self.batch_units < 1:
            raise ValueError("batch_units must be at least 1")
        if self.batch_bytes < 1024:
            raise ValueError("batch_bytes must be at least 1024")
        if self.max_pending_batches < 1:
            raise ValueError("max_pending_batches must be at least 1")
        if self.resolved_workers() < 1:
            raise ValueError("workers must resolve to at least 1")


@dataclass(frozen=True, slots=True)
class _SerializedTuBatch:
    sequence: int
    payloads: list[bytes]

    @property
    def total_bytes(self) -> int:
        return sum(len(payload) for payload in self.payloads)


def extract_tmx_parallel(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
    options: TmxParallelOptions | None = None,
) -> Iterator[ParallelExtractItem]:
    parallel_options = options or TmxParallelOptions()
    parallel_options.validate()

    extractor = TmxExtractor(
        filepath=filepath,
        source_language=source_language,
        target_language=target_language,
        domain=domain,
        parse_header=not (source_language and target_language),
        mode=mode,
    )

    pending: list[tuple[int, Future[ParallelExtractBatch]]] = []
    max_pending = parallel_options.max_pending_batches
    with ProcessPoolExecutor(max_workers=parallel_options.resolved_workers()) as pool:
        for batch in _serialized_tu_batches(filepath, parallel_options):
            pending.append(
                (
                    batch.sequence,
                    pool.submit(
                        _parse_serialized_tu_batch,
                        batch.payloads,
                        extractor.native_source,
                        extractor.native_target,
                        domain,
                        mode,
                    ),
                )
            )
            if len(pending) >= max_pending:
                yield from _resolve_next_batch(pending)

        while pending:
            yield from _resolve_next_batch(pending)


def _resolve_next_batch(
    pending: list[tuple[int, Future[ParallelExtractBatch]]],
) -> Iterator[ParallelExtractItem]:
    pending.sort(key=lambda item: item[0])
    _, future = pending.pop(0)
    yield from future.result()


def _serialized_tu_batches(
    filepath: str,
    options: TmxParallelOptions,
) -> Iterator[_SerializedTuBatch]:
    context = iterparse_safe(filepath, events=("end",))
    sequence = 0
    payloads: list[bytes] = []
    payload_bytes = 0

    for _, elem in context:
        if local_name(elem.tag) != "tu":
            continue

        payload = etree.tostring(elem, encoding="utf-8")
        payloads.append(payload)
        payload_bytes += len(payload)

        if len(payloads) >= options.batch_units or payload_bytes >= options.batch_bytes:
            yield _SerializedTuBatch(sequence=sequence, payloads=payloads)
            sequence += 1
            payloads = []
            payload_bytes = 0

        clear_element(elem)

    if payloads:
        yield _SerializedTuBatch(sequence=sequence, payloads=payloads)


def _parse_serialized_tu_batch(
    payloads: list[bytes],
    source_language: str,
    target_language: str,
    domain: str | None,
    mode: TmxParseMode,
) -> ParallelExtractBatch:
    extractor = TmxExtractor(
        filepath="",
        source_language=source_language,
        target_language=target_language,
        domain=domain,
        parse_header=False,
        mode=mode,
    )
    parsed: ParallelExtractBatch = []
    for payload in payloads:
        elem = etree.fromstring(payload)
        parsed.append(extractor.extract_element(elem))
    return parsed
