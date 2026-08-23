from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from itertools import chain
from os import cpu_count
from typing import TYPE_CHECKING, Protocol, cast

from lokit.data.structure import CodePart, Data, TextPart
from lokit.parsers.projection import project_items
from lokit.parsers.tmx.extraction import TmxExtractor
from lokit.parsers.tmx.models import TmxParseMode
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from lokit.data.structure import SegmentPart, Tags, TargetTags
    from lokit.data.tag_types import TieData
    from lokit.placeholders import PlaceholderSyntax

ParallelExtractItem = tuple[str, Data]
_MAX_WORKERS = 64
_THREAD_NAME_PREFIX = "lokit-tmx-project"
_AUTO_SAMPLE_UNITS = 16
_AUTO_MIN_WORK_PER_UNIT = 1024
_TAG_WORK_WEIGHT = 32


class _ClosableIterator(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _ProjectionOptions:
    include_tags: bool
    tag_syntax: TagSyntax
    unsupported_tags: UnsupportedTagPolicy
    runtime_placeholders: bool
    inline_placeholders: bool
    placeholder_syntaxes: tuple[PlaceholderSyntax | str, ...] | None


@dataclass(frozen=True, slots=True)
class TmxParallelOptions:
    """Bounds for ordered projection concurrency.

    ``batch_bytes`` is an allocation-free estimate of retained Python input
    payload, and a single oversized unit is always admitted on its own.
    ``max_pending_batches`` bounds all submitted batches, including running
    work and completed results waiting for their deterministic output turn.
    Automatic worker selection samples a small prefix and remains serial when
    projection is too light to recover thread and batching overhead.
    """

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
        if self.workers < 0:
            raise ValueError("workers must be zero (automatic) or positive")
        if self.workers > _MAX_WORKERS:
            raise ValueError(f"workers must not exceed {_MAX_WORKERS}")
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
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> Iterator[ParallelExtractItem]:
    """Stream TMX units with bounded, ordered parallel post-processing.

    The native reader and stateful TMX conversion are intentionally serial;
    only independent tag/placeholder projection is dispatched to threads.
    """

    parallel_options = options or TmxParallelOptions()
    parallel_options.validate()
    projection_options = _ProjectionOptions(
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=(tuple(placeholder_syntaxes) if placeholder_syntaxes is not None else None),
    )
    extractor = TmxExtractor(
        filepath=filepath,
        source_language=source_language,
        target_language=target_language if selected_target else None,
        domain=domain,
        parse_header=not (source_language and target_language),
        mode=mode,
    )
    # Native reading and TMX semantic conversion remain ordered on the caller
    # thread.  TmxExtractor carries cross-unit plural and locale state, so
    # moving that stage into workers would silently change valid TMX results.
    # Independent tag/placeholder projection is the expensive Rust-backed
    # stage and can safely run on distinct Data objects in parallel.
    raw_items = extractor._extract()
    pending: deque[Future[list[ParallelExtractItem]]] = deque()
    executor: ThreadPoolExecutor | None = None
    try:
        workers = parallel_options.resolved_workers()
        if workers == 1 or parallel_options.max_pending_batches == 1:
            yield from _project_stream(raw_items, projection_options)
            return

        sampled = _sample_items(raw_items, parallel_options)
        ordered_items = chain(sampled, raw_items)
        # Explicit worker counts honor the caller's request.  Automatic mode
        # avoids the measured regression that threading causes for ordinary
        # short units while still selecting concurrency for projection-heavy
        # placeholder/tag payloads.
        if parallel_options.workers == 0 and not _sample_warrants_parallelism(sampled, projection_options):
            yield from _project_stream(ordered_items, projection_options)
            return

        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=_THREAD_NAME_PREFIX)
        for batch in _iter_batches(ordered_items, parallel_options):
            pending.append(executor.submit(_project_batch, batch, projection_options))
            if len(pending) >= parallel_options.max_pending_batches:
                yield from pending.popleft().result()
        while pending:
            yield from pending.popleft().result()
    finally:
        try:
            _close_iterator(raw_items)
        finally:
            try:
                extractor.close()
            finally:
                for future in pending:
                    future.cancel()
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=True)


def _sample_items(
    items: Iterator[ParallelExtractItem],
    options: TmxParallelOptions,
) -> list[ParallelExtractItem]:
    sample: list[ParallelExtractItem] = []
    retained_bytes = 0
    sample_limit = min(_AUTO_SAMPLE_UNITS, options.batch_units)
    while len(sample) < sample_limit and retained_bytes < options.batch_bytes:
        try:
            item = next(items)
        except StopIteration:
            break
        sample.append(item)
        retained_bytes += _estimated_item_bytes(item)
    return sample


def _sample_warrants_parallelism(
    sample: list[ParallelExtractItem],
    options: _ProjectionOptions,
) -> bool:
    if not sample:
        return False
    work = sum(_projection_work(item, options) for item in sample)
    return work >= _AUTO_MIN_WORK_PER_UNIT * len(sample)


def _projection_work(item: ParallelExtractItem, options: _ProjectionOptions) -> int:
    _, data = item
    work = 0
    if options.runtime_placeholders or options.inline_placeholders or options.include_tags:
        work += len(data.source)
        work += len(data.target) if data.target is not None else 0
        work += sum(len(target.text) for target in data.targets.values() if target.text is not None)
    if options.inline_placeholders or options.include_tags:
        if data.tags is not None:
            work += _TAG_WORK_WEIGHT * (
                len(data.tags.source_tag_map)
                + len(data.tags.target_tag_map)
                + len(data.tags.source_parts)
                + len(data.tags.target_parts)
            )
        for target in data.targets.values():
            if target.tags is not None:
                work += _TAG_WORK_WEIGHT * (len(target.tags.tag_map) + len(target.tags.parts))
    return work


def _iter_batches(
    items: Iterator[ParallelExtractItem],
    options: TmxParallelOptions,
) -> Iterator[list[ParallelExtractItem]]:
    batch: list[ParallelExtractItem] = []
    retained_bytes = 0
    for item in items:
        item_bytes = _estimated_item_bytes(item)
        if batch and (len(batch) >= options.batch_units or retained_bytes + item_bytes > options.batch_bytes):
            yield batch
            batch = []
            retained_bytes = 0
        batch.append(item)
        retained_bytes += item_bytes
        if len(batch) >= options.batch_units or retained_bytes >= options.batch_bytes:
            yield batch
            batch = []
            retained_bytes = 0
    if batch:
        yield batch


def _project_batch(
    batch: list[ParallelExtractItem],
    options: _ProjectionOptions,
) -> list[ParallelExtractItem]:
    return list(_project_stream(iter(batch), options))


def _project_stream(
    items: Iterator[ParallelExtractItem],
    options: _ProjectionOptions,
) -> Iterator[ParallelExtractItem]:
    return project_items(
        items,
        include_tags=options.include_tags,
        tag_syntax=options.tag_syntax,
        native_syntax=TagSyntax.TMX_14,
        unsupported_tags=options.unsupported_tags,
        runtime_placeholders=options.runtime_placeholders,
        inline_placeholders=options.inline_placeholders,
        placeholder_syntaxes=options.placeholder_syntaxes,
    )


def _estimated_item_bytes(item: ParallelExtractItem) -> int:
    unit_id, data = item
    size = data.__sizeof__() + _string_bytes(unit_id, data.source, data.target)
    size += data.extensions.__sizeof__() + _mapping_string_bytes(data.extensions)
    size += data.targets.__sizeof__()
    for locale, target in data.targets.items():
        size += target.__sizeof__() + _string_bytes(locale, target.text)
        size += target.extensions.__sizeof__() + _mapping_string_bytes(target.extensions)
        if target.tags is not None:
            size += _target_tags_bytes(target.tags)
    if data.tags is not None:
        size += _tags_bytes(data.tags)
    if data.plural is not None:
        size += data.plural.__sizeof__() + _string_bytes(data.plural.variant)
        size += data.plural.extensions.__sizeof__() + _mapping_string_bytes(data.plural.extensions)
    size += data.comments.__sizeof__()
    for comment in data.comments:
        size += comment.__sizeof__() + _string_bytes(comment.context, comment.timestamp, comment.context_key)
        size += comment.extensions.__sizeof__() + _mapping_string_bytes(comment.extensions)
    for context in (data.previous_context, data.next_context):
        if context is None:
            continue
        size += context.__sizeof__() + _string_bytes(context.unit_id, context.source, context.target)
        size += context.extensions.__sizeof__() + _mapping_string_bytes(context.extensions)
    return max(1, size)


def _tags_bytes(tags: Tags) -> int:
    return (
        tags.__sizeof__()
        + _tie_map_bytes(tags.source_tag_map)
        + _tie_map_bytes(tags.target_tag_map)
        + _parts_bytes(tags.source_parts)
        + _parts_bytes(tags.target_parts)
    )


def _target_tags_bytes(tags: TargetTags) -> int:
    return tags.__sizeof__() + _tie_map_bytes(tags.tag_map) + _parts_bytes(tags.parts)


def _tie_map_bytes(tag_map: dict[str, TieData]) -> int:
    size = tag_map.__sizeof__()
    for key, tag in tag_map.items():
        size += tag.__sizeof__()
        size += _string_bytes(
            key,
            tag.id,
            tag.attribute_data,
            tag.pair_id,
            tag.original_name,
            tag.original_text,
        )
        size += tag.attributes.__sizeof__() + _mapping_string_bytes(tag.attributes)
    return size


def _parts_bytes(parts: list[SegmentPart]) -> int:
    size = parts.__sizeof__()
    for part in parts:
        size += part.__sizeof__()
        if isinstance(part, TextPart):
            size += _string_bytes(part.value)
        elif isinstance(part, CodePart):
            size += _string_bytes(part.ref)
    return size


def _mapping_string_bytes(values: dict[str, str]) -> int:
    return sum(_string_bytes(key, value) for key, value in values.items())


def _string_bytes(*values: str | None) -> int:
    # str.__sizeof__ is allocation-free and captures CPython's compact ASCII
    # versus wider Unicode storage.  The batch limit is therefore a retained
    # memory estimate, not a promise about serialized UTF-8 byte length.
    return sum(value.__sizeof__() for value in values if value is not None)


def _close_iterator(items: Iterator[ParallelExtractItem]) -> None:
    candidate: object = items
    if hasattr(candidate, "close"):
        cast("_ClosableIterator", candidate).close()
