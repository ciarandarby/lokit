from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from lokit.data.structure import Data
from lokit.parsers.id_registry import BoundedIdRegistry
from lokit.parsers.tmx.extraction import TmxExtractor, unique_tmx_extract_item

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


def test_bounded_id_registry_spills_exactly_and_cleans_up() -> None:
    registry = BoundedIdRegistry(memory_entry_limit=2, memory_byte_limit=1_024)
    assert registry.add("alpha")
    assert registry.add("alpha#2")
    assert not registry.add("alpha")
    assert registry.next_suffix("alpha") == 2
    assert registry.add("beta")
    assert registry.spilled
    assert registry.next_suffix("alpha") == 3

    for index in range(2_000):
        assert registry.add(f"unit-{index}")
    for index in reversed(range(2_000)):
        assert not registry.add(f"unit-{index}")
    assert registry.next_suffix("alpha") == 4
    assert registry.add("line\nfeed")
    assert not registry.add("line\nfeed")

    temporary_path = registry.temporary_path
    assert temporary_path is not None
    assert temporary_path.is_dir()
    registry.close()
    assert not temporary_path.exists()
    registry.close()


@pytest.mark.parametrize(
    ("entry_limit", "byte_limit"),
    ((-1, 1), (1, -1)),
)
def test_bounded_id_registry_rejects_negative_limits(entry_limit: int, byte_limit: int) -> None:
    with pytest.raises(ValueError):
        BoundedIdRegistry(
            memory_entry_limit=entry_limit,
            memory_byte_limit=byte_limit,
        )


def test_bounded_id_registry_checks_suffix_overflow() -> None:
    registry = BoundedIdRegistry(maximum_suffix=3)
    assert registry.add("alpha")
    assert registry.next_suffix("alpha") == 2

    with pytest.raises(OverflowError, match="suffix counter overflow"):
        registry.next_suffix("alpha")


def test_repeated_ids_skip_only_explicit_collisions_after_disk_spill() -> None:
    registry = BoundedIdRegistry(memory_entry_limit=2, memory_byte_limit=1_024)
    data = Data(source="source")
    inputs = [
        "alpha",
        *(f"alpha#{suffix}" for suffix in range(2, 1_001)),
        *("alpha" for _ in range(5_000)),
    ]
    try:
        resolved = [unique_tmx_extract_item((unit_id, data), registry)[0] for unit_id in inputs]
    finally:
        registry.close()

    assert resolved[:3] == ["alpha", "alpha#2", "alpha#3"]
    assert resolved[1_000] == "alpha#1001"
    assert resolved[-1] == "alpha#6000"


def test_python_tmx_fallback_closes_spill_when_stream_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "early-close.tmx"
    source.write_text(
        """<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="same"><tuv xml:lang="en"><seg>one</seg></tuv></tu>
<tu tuid="same#2"><tuv xml:lang="en"><seg>two</seg></tuv></tu>
<tu tuid="same"><tuv xml:lang="en"><seg>three</seg></tuv></tu>
</body></tmx>""",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOKIT_DISABLE_RUST_INTERCHANGE", "1")
    registry = BoundedIdRegistry(memory_entry_limit=1, memory_byte_limit=128)
    iterator = cast(
        "Generator[tuple[str, Data], None, None]",
        TmxExtractor(str(source))._extract_python(registry),
    )
    assert next(iterator)[0] == "same"
    assert next(iterator)[0] == "same#2"
    temporary_path = registry.temporary_path
    assert temporary_path is not None
    assert temporary_path.is_dir()

    iterator.close()

    assert not temporary_path.exists()
