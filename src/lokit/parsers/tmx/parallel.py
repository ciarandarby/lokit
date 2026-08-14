from __future__ import annotations

from dataclasses import dataclass
from os import cpu_count
from typing import TYPE_CHECKING

from lokit.data.structure import Data
from lokit.parsers.tmx.extraction import TmxExtractor
from lokit.parsers.tmx.models import TmxParseMode

if TYPE_CHECKING:
    from collections.abc import Iterator

ParallelExtractItem = tuple[str, Data]


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


def extract_tmx_parallel(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
    options: TmxParallelOptions | None = None,
    selected_target: bool = True,
) -> Iterator[ParallelExtractItem]:
    parallel_options = options or TmxParallelOptions()
    parallel_options.validate()
    extractor = TmxExtractor(
        filepath=filepath,
        source_language=source_language,
        target_language=target_language if selected_target else None,
        domain=domain,
        parse_header=not (source_language and target_language),
        mode=mode,
    )
    yield from extractor.extract()
