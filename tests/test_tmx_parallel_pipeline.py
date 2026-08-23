from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, cast

import pytest

from lokit.data.structure import Data
from lokit.parsers.tmx import parallel
from lokit.parsers.tmx.parallel import TmxParallelOptions, extract_tmx_parallel
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Sequence

    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]


class _FakeExtractor:
    def __init__(self, items: list[ExtractItem]) -> None:
        self._items = items
        self.produced = 0
        self.raw_closed = threading.Event()
        self.closed = threading.Event()

    def _extract(self) -> Iterator[ExtractItem]:
        try:
            for item in self._items:
                self.produced += 1
                yield item
        finally:
            self.raw_closed.set()

    def close(self) -> None:
        self.closed.set()


def test_parallel_projection_is_concurrent_ordered_and_forwards_options(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = _install_fake_extractor(monkeypatch, 8)
    lock = threading.Lock()
    active = 0
    peak = 0
    worker_names: set[str] = set()
    calls: list[tuple[bool, bool, tuple[PlaceholderSyntax | str, ...] | None]] = []

    def fake_project_items(
        items: Iterator[ExtractItem],
        *,
        include_tags: bool,
        tag_syntax: TagSyntax,
        native_syntax: TagSyntax,
        unsupported_tags: UnsupportedTagPolicy,
        runtime_placeholders: bool,
        inline_placeholders: bool,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None,
    ) -> Iterator[ExtractItem]:
        del include_tags, tag_syntax, native_syntax, unsupported_tags
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            worker_names.add(threading.current_thread().name)
            calls.append(
                (
                    runtime_placeholders,
                    inline_placeholders,
                    tuple(placeholder_syntaxes) if placeholder_syntaxes is not None else None,
                )
            )
        time.sleep(0.02)
        try:
            yield from items
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(parallel, "project_items", fake_project_items)

    items = list(
        extract_tmx_parallel(
            "unused.tmx",
            options=TmxParallelOptions(
                workers=4,
                batch_units=1,
                batch_bytes=1024 * 1024,
                max_pending_batches=4,
            ),
            include_tags=True,
            tag_syntax=TagSyntax.HTML,
            unsupported_tags=UnsupportedTagPolicy.PLACEHOLDER,
            runtime_placeholders=False,
            inline_placeholders=True,
            placeholder_syntaxes=("python-brace",),
        )
    )

    assert [unit_id for unit_id, _ in items] == [f"u{index}" for index in range(8)]
    assert peak >= 2
    assert worker_names
    assert all(name.startswith("lokit-tmx-project") for name in worker_names)
    assert calls == [(False, True, ("python-brace",))] * 8
    assert extractor.produced == 8
    assert extractor.raw_closed.is_set()
    assert extractor.closed.is_set()


def test_parallel_pipeline_applies_backpressure_at_pending_batch_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = _install_fake_extractor(monkeypatch, 12)
    release = threading.Event()
    both_started = threading.Event()
    lock = threading.Lock()
    started = 0

    def blocking_project_items(
        items: Iterator[ExtractItem],
        *,
        include_tags: bool,
        tag_syntax: TagSyntax,
        native_syntax: TagSyntax,
        unsupported_tags: UnsupportedTagPolicy,
        runtime_placeholders: bool,
        inline_placeholders: bool,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None,
    ) -> Iterator[ExtractItem]:
        del (
            include_tags,
            tag_syntax,
            native_syntax,
            unsupported_tags,
            runtime_placeholders,
            inline_placeholders,
            placeholder_syntaxes,
        )
        nonlocal started
        with lock:
            started += 1
            if started == 2:
                both_started.set()
        assert release.wait(timeout=5)
        yield from items

    monkeypatch.setattr(parallel, "project_items", blocking_project_items)
    results: list[ExtractItem] = []
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            results.extend(
                extract_tmx_parallel(
                    "unused.tmx",
                    options=TmxParallelOptions(
                        workers=2,
                        batch_units=2,
                        batch_bytes=1024 * 1024,
                        max_pending_batches=2,
                    ),
                )
            )
        except BaseException as error:
            errors.append(error)

    consumer = threading.Thread(target=consume, name="tmx-test-consumer")
    consumer.start()
    assert both_started.wait(timeout=5)
    assert extractor.produced == 4
    assert consumer.is_alive()

    release.set()
    consumer.join(timeout=5)
    assert not consumer.is_alive()
    assert not errors
    assert len(results) == 12
    assert extractor.closed.is_set()


def test_automatic_workers_keep_light_units_serial_with_bounded_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = _install_fake_extractor(monkeypatch, 100)
    projector_threads: set[str] = set()

    def passthrough_project_items(
        items: Iterator[ExtractItem],
        *,
        include_tags: bool,
        tag_syntax: TagSyntax,
        native_syntax: TagSyntax,
        unsupported_tags: UnsupportedTagPolicy,
        runtime_placeholders: bool,
        inline_placeholders: bool,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None,
    ) -> Iterator[ExtractItem]:
        del (
            include_tags,
            tag_syntax,
            native_syntax,
            unsupported_tags,
            runtime_placeholders,
            inline_placeholders,
            placeholder_syntaxes,
        )
        projector_threads.add(threading.current_thread().name)
        yield from items

    monkeypatch.setattr(parallel, "project_items", passthrough_project_items)
    monkeypatch.setattr(parallel, "cpu_count", lambda: 4)
    iterator = cast(
        "Generator[ExtractItem, None, None]",
        extract_tmx_parallel(
            "unused.tmx",
            options=TmxParallelOptions(
                workers=0,
                batch_units=5000,
                batch_bytes=16 * 1024 * 1024,
                max_pending_batches=2,
            ),
        ),
    )

    assert next(iterator)[0] == "u0"
    assert extractor.produced == 16
    assert projector_threads == {threading.current_thread().name}
    iterator.close()


def test_automatic_workers_parallelize_heavy_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = _FakeExtractor([(f"u{index}", Data(source="x" * 2048)) for index in range(8)])

    def factory(**kwargs: object) -> _FakeExtractor:
        del kwargs
        return extractor

    monkeypatch.setattr(parallel, "TmxExtractor", factory)
    monkeypatch.setattr(parallel, "cpu_count", lambda: 4)
    lock = threading.Lock()
    active = 0
    peak = 0

    def slow_project_items(
        items: Iterator[ExtractItem],
        *,
        include_tags: bool,
        tag_syntax: TagSyntax,
        native_syntax: TagSyntax,
        unsupported_tags: UnsupportedTagPolicy,
        runtime_placeholders: bool,
        inline_placeholders: bool,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None,
    ) -> Iterator[ExtractItem]:
        del (
            include_tags,
            tag_syntax,
            native_syntax,
            unsupported_tags,
            runtime_placeholders,
            inline_placeholders,
            placeholder_syntaxes,
        )
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        try:
            yield from items
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(parallel, "project_items", slow_project_items)

    items = list(
        extract_tmx_parallel(
            "unused.tmx",
            options=TmxParallelOptions(
                workers=0,
                batch_units=1,
                batch_bytes=1024 * 1024,
                max_pending_batches=2,
            ),
        )
    )

    assert len(items) == 8
    assert peak >= 2


@pytest.mark.parametrize(("workers", "max_pending_batches"), [(1, 4), (4, 1)])
def test_effectively_serial_configuration_stays_unit_streaming(
    monkeypatch: pytest.MonkeyPatch,
    workers: int,
    max_pending_batches: int,
) -> None:
    extractor = _install_fake_extractor(monkeypatch, 10)

    def passthrough_project_items(
        items: Iterator[ExtractItem],
        *,
        include_tags: bool,
        tag_syntax: TagSyntax,
        native_syntax: TagSyntax,
        unsupported_tags: UnsupportedTagPolicy,
        runtime_placeholders: bool,
        inline_placeholders: bool,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None,
    ) -> Iterator[ExtractItem]:
        del (
            include_tags,
            tag_syntax,
            native_syntax,
            unsupported_tags,
            runtime_placeholders,
            inline_placeholders,
            placeholder_syntaxes,
        )
        yield from items

    monkeypatch.setattr(parallel, "project_items", passthrough_project_items)
    iterator = cast(
        "Generator[ExtractItem, None, None]",
        extract_tmx_parallel(
            "unused.tmx",
            options=TmxParallelOptions(
                workers=workers,
                batch_units=5000,
                batch_bytes=16 * 1024 * 1024,
                max_pending_batches=max_pending_batches,
            ),
        ),
    )

    assert next(iterator)[0] == "u0"
    assert extractor.produced == 1
    iterator.close()
    assert extractor.closed.is_set()


def test_parallel_generator_close_releases_reader_then_joins_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = _install_fake_extractor(monkeypatch, 4)
    second_started = threading.Event()
    release_second = threading.Event()

    def blocking_second_project_items(
        items: Iterator[ExtractItem],
        *,
        include_tags: bool,
        tag_syntax: TagSyntax,
        native_syntax: TagSyntax,
        unsupported_tags: UnsupportedTagPolicy,
        runtime_placeholders: bool,
        inline_placeholders: bool,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None,
    ) -> Iterator[ExtractItem]:
        del (
            include_tags,
            tag_syntax,
            native_syntax,
            unsupported_tags,
            runtime_placeholders,
            inline_placeholders,
            placeholder_syntaxes,
        )
        batch = list(items)
        if batch[0][0] == "u1":
            second_started.set()
            assert release_second.wait(timeout=5)
        yield from batch

    monkeypatch.setattr(parallel, "project_items", blocking_second_project_items)
    iterator = cast(
        "Generator[ExtractItem, None, None]",
        extract_tmx_parallel(
            "unused.tmx",
            options=TmxParallelOptions(
                workers=2,
                batch_units=1,
                batch_bytes=1024 * 1024,
                max_pending_batches=2,
            ),
        ),
    )
    assert next(iterator)[0] == "u0"
    assert second_started.wait(timeout=5)

    closed = threading.Event()

    def close_iterator() -> None:
        iterator.close()
        closed.set()

    closer = threading.Thread(target=close_iterator, name="tmx-test-closer")
    closer.start()
    assert extractor.raw_closed.wait(timeout=5)
    assert extractor.closed.wait(timeout=5)
    assert not closed.is_set()

    release_second.set()
    closer.join(timeout=5)
    assert closed.is_set()
    assert not any(thread.name.startswith("lokit-tmx-project") for thread in threading.enumerate())


def test_parallel_worker_error_closes_reader_and_cancels_remaining_work(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = _install_fake_extractor(monkeypatch, 4)

    def failing_project_items(
        items: Iterator[ExtractItem],
        *,
        include_tags: bool,
        tag_syntax: TagSyntax,
        native_syntax: TagSyntax,
        unsupported_tags: UnsupportedTagPolicy,
        runtime_placeholders: bool,
        inline_placeholders: bool,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None,
    ) -> Iterator[ExtractItem]:
        del (
            include_tags,
            tag_syntax,
            native_syntax,
            unsupported_tags,
            runtime_placeholders,
            inline_placeholders,
            placeholder_syntaxes,
        )
        batch = list(items)
        if batch[0][0] == "u1":
            raise RuntimeError("projection failed")
        yield from batch

    monkeypatch.setattr(parallel, "project_items", failing_project_items)

    with pytest.raises(RuntimeError, match="projection failed"):
        list(
            extract_tmx_parallel(
                "unused.tmx",
                options=TmxParallelOptions(
                    workers=2,
                    batch_units=1,
                    batch_bytes=1024 * 1024,
                    max_pending_batches=2,
                ),
            )
        )

    assert extractor.raw_closed.is_set()
    assert extractor.closed.is_set()
    assert not any(thread.name.startswith("lokit-tmx-project") for thread in threading.enumerate())


@pytest.mark.parametrize("workers", [-1, 65])
def test_parallel_options_reject_unsafe_worker_counts(workers: int) -> None:
    with pytest.raises(ValueError, match="workers"):
        TmxParallelOptions(workers=workers).validate()


def _install_fake_extractor(monkeypatch: pytest.MonkeyPatch, unit_count: int) -> _FakeExtractor:
    extractor = _FakeExtractor([(f"u{index}", Data(source=f"source {index}")) for index in range(unit_count)])

    def factory(**kwargs: object) -> _FakeExtractor:
        del kwargs
        return extractor

    monkeypatch.setattr(parallel, "TmxExtractor", factory)
    return extractor
