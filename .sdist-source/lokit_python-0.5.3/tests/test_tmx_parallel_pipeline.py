from __future__ import annotations

import threading
from typing import TYPE_CHECKING, cast

import pytest

from lokit.data.structure import Data
from lokit.parsers.tmx import parallel
from lokit.parsers.tmx.parallel import TmxParallelOptions, extract_tmx_parallel
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path

ExtractItem = tuple[str, Data]


def _write_tmx(path: Path, unit_count: int, *, payload_size: int = 0) -> None:
    padding = "x" * payload_size
    body = "".join(
        (
            f'<tu tuid="u{index}">'
            '<tuv xml:lang="en"><seg>Hello '
            f'<bpt i="1">&lt;b&gt;</bpt>{index}<ept i="1">&lt;/b&gt;</ept> '
            f"{{name}}{padding}</seg></tuv>"
            '<tuv xml:lang="fr"><seg>Bonjour '
            f'<bpt i="1">&lt;b&gt;</bpt>{index}<ept i="1">&lt;/b&gt;</ept> '
            f"{{name}}{padding}</seg></tuv>"
            "</tu>"
        )
        for index in range(unit_count)
    )
    path.write_text(
        '<tmx version="1.4"><header srclang="en"/><body>' + body + "</body></tmx>",
        encoding="utf-8",
    )


def test_parallel_projection_is_ordered_and_honors_projection_options(tmp_path: Path) -> None:
    source = tmp_path / "projection.tmx"
    _write_tmx(source, 8)
    options = TmxParallelOptions(
        workers=4,
        batch_units=1,
        batch_bytes=1024 * 1024,
        max_pending_batches=4,
    )

    projected = list(
        extract_tmx_parallel(
            str(source),
            "en",
            "fr",
            options=options,
            placeholder_syntaxes=("python-brace",),
        )
    )
    unprojected = list(
        extract_tmx_parallel(
            str(source),
            "en",
            "fr",
            options=options,
            runtime_placeholders=False,
            inline_placeholders=False,
        )
    )
    rendered = list(
        extract_tmx_parallel(
            str(source),
            "en",
            "fr",
            options=options,
            include_tags=True,
            tag_syntax=TagSyntax.HTML,
            unsupported_tags=UnsupportedTagPolicy.PLACEHOLDER,
            runtime_placeholders=False,
            inline_placeholders=False,
        )
    )

    expected_ids = [f"u{index}" for index in range(8)]
    assert [unit_id for unit_id, _ in projected] == expected_ids
    assert [unit_id for unit_id, _ in unprojected] == expected_ids
    assert [unit_id for unit_id, _ in rendered] == expected_ids
    assert "{LOKIT_P" in projected[0][1].source
    assert projected[0][1].target is not None
    assert "{LOKIT_P" in projected[0][1].target
    assert unprojected[0][1].source == "Hello 0 {name}"
    assert unprojected[0][1].target == "Bonjour 0 {name}"
    assert rendered[0][1].source == "Hello <b>0</b> {name}"
    assert rendered[0][1].target == "Bonjour <b>0</b> {name}"


def test_parallel_batching_is_lazy_and_bounded_by_units_and_bytes() -> None:
    produced: list[int] = []

    def tracked_items() -> Iterator[ExtractItem]:
        for index in range(12):
            produced.append(index)
            yield f"u{index}", Data(source=f"source {index}")

    options = TmxParallelOptions(
        workers=2,
        batch_units=2,
        batch_bytes=1024 * 1024,
        max_pending_batches=2,
    )
    batches = parallel._iter_batches(tracked_items(), options)

    assert [unit_id for unit_id, _ in next(batches)] == ["u0", "u1"]
    assert produced == [0, 1]
    remaining = list(batches)
    assert all(len(batch) <= 2 for batch in remaining)
    assert produced == list(range(12))

    oversized = iter(
        (
            ("large-1", Data(source="x" * 4096)),
            ("large-2", Data(source="y" * 4096)),
        )
    )
    byte_bounded = list(
        parallel._iter_batches(
            oversized,
            TmxParallelOptions(
                workers=2,
                batch_units=100,
                batch_bytes=1024,
                max_pending_batches=2,
            ),
        )
    )
    assert [[unit_id for unit_id, _ in batch] for batch in byte_bounded] == [["large-1"], ["large-2"]]


def test_automatic_worker_sampling_selects_only_projection_heavy_units() -> None:
    batch_options = TmxParallelOptions(
        workers=0,
        batch_units=5000,
        batch_bytes=16 * 1024 * 1024,
        max_pending_batches=2,
    )
    projection_options = parallel._ProjectionOptions(
        include_tags=False,
        tag_syntax=TagSyntax.NATIVE,
        unsupported_tags=UnsupportedTagPolicy.ERROR,
        runtime_placeholders=True,
        inline_placeholders=True,
        placeholder_syntaxes=None,
    )
    light = parallel._sample_items(
        iter((f"light-{index}", Data(source="short")) for index in range(100)),
        batch_options,
    )
    heavy = parallel._sample_items(
        iter((f"heavy-{index}", Data(source="x" * 2048)) for index in range(100)),
        batch_options,
    )

    assert len(light) == len(heavy) == 16
    assert not parallel._sample_warrants_parallelism(light, projection_options)
    assert parallel._sample_warrants_parallelism(heavy, projection_options)


@pytest.mark.parametrize(("workers", "max_pending_batches"), [(1, 4), (4, 1)])
def test_effectively_serial_configuration_remains_streaming(
    tmp_path: Path,
    workers: int,
    max_pending_batches: int,
) -> None:
    source = tmp_path / f"serial-{workers}-{max_pending_batches}.tmx"
    _write_tmx(source, 100)
    iterator = cast(
        "Generator[ExtractItem, None, None]",
        extract_tmx_parallel(
            str(source),
            "en",
            "fr",
            options=TmxParallelOptions(
                workers=workers,
                batch_units=5000,
                batch_bytes=16 * 1024 * 1024,
                max_pending_batches=max_pending_batches,
            ),
            runtime_placeholders=False,
            inline_placeholders=False,
        ),
    )

    unit_id, data = next(iterator)
    assert unit_id == "u0"
    assert data.source == "Hello 0 {name}"
    assert data.target == "Bonjour 0 {name}"
    assert data.tags is not None
    iterator.close()
    assert not any(thread.name.startswith("lokit-tmx-project") for thread in threading.enumerate())


def test_parallel_generator_close_joins_projection_workers(tmp_path: Path) -> None:
    source = tmp_path / "close.tmx"
    _write_tmx(source, 64, payload_size=4096)
    iterator = cast(
        "Generator[ExtractItem, None, None]",
        extract_tmx_parallel(
            str(source),
            "en",
            "fr",
            options=TmxParallelOptions(
                workers=2,
                batch_units=1,
                batch_bytes=1024 * 1024,
                max_pending_batches=2,
            ),
        ),
    )

    assert next(iterator)[0] == "u0"
    iterator.close()
    assert not any(thread.name.startswith("lokit-tmx-project") for thread in threading.enumerate())


def test_parallel_worker_error_joins_remaining_workers(tmp_path: Path) -> None:
    source = tmp_path / "worker-error.tmx"
    _write_tmx(source, 8)

    with pytest.raises(ValueError, match="unknown placeholder syntax"):
        list(
            extract_tmx_parallel(
                str(source),
                "en",
                "fr",
                options=TmxParallelOptions(
                    workers=2,
                    batch_units=1,
                    batch_bytes=1024 * 1024,
                    max_pending_batches=2,
                ),
                placeholder_syntaxes=("invalid",),
            )
        )

    assert not any(thread.name.startswith("lokit-tmx-project") for thread in threading.enumerate())


@pytest.mark.parametrize("workers", [-1, 65])
def test_parallel_options_reject_unsafe_worker_counts(workers: int) -> None:
    with pytest.raises(ValueError, match="workers"):
        TmxParallelOptions(workers=workers).validate()
