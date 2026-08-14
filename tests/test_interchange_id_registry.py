from __future__ import annotations

from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING, Protocol, cast

import lokit

if TYPE_CHECKING:
    from collections.abc import Iterator

    from lokit.data.structure import Data


class _Closable(Protocol):
    def close(self) -> None: ...


def test_native_id_registry_remains_exact_beyond_the_previous_spill_threshold(tmp_path: Path) -> None:
    source = tmp_path / "large-ids.tmx"
    unit_count = 70_000
    with source.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write('<tmx version="1.4"><header srclang="en"/><body>\n')
        for index in range(unit_count):
            stream.write(f'<tu tuid="unit-{index}"><tuv xml:lang="en"><seg>{index}</seg></tuv></tu>\n')
        stream.write("</body></tmx>\n")

    items = lokit.stream.tmx(str(source)).items
    first_id = ""
    last_id = ""
    count = 0
    for unit_id, _ in items:
        if count == 0:
            first_id = unit_id
        last_id = unit_id
        count += 1

    assert (count, first_id, last_id) == (unit_count, "unit-0", f"unit-{unit_count - 1}")


def test_native_repeated_ids_skip_only_explicit_collisions(tmp_path: Path) -> None:
    source = tmp_path / "repeated-ids.tmx"
    ids = ["alpha", *(f"alpha#{suffix}" for suffix in range(2, 1_001)), *("alpha" for _ in range(5_000))]
    with source.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write('<tmx version="1.4"><header srclang="en"/><body>\n')
        for index, unit_id in enumerate(ids):
            stream.write(f'<tu tuid="{unit_id}"><tuv xml:lang="en"><seg>{index}</seg></tuv></tu>\n')
        stream.write("</body></tmx>\n")

    resolved = [unit_id for unit_id, _ in lokit.stream.tmx(str(source)).items]

    assert resolved[:3] == ["alpha", "alpha#2", "alpha#3"]
    assert resolved[1_000] == "alpha#1001"
    assert resolved[-1] == "alpha#6000"


def test_native_reader_close_creates_no_id_spill_directory(tmp_path: Path) -> None:
    source = tmp_path / "early-close.tmx"
    source.write_text(
        """<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="same"><tuv xml:lang="en"><seg>one</seg></tuv></tu>
<tu tuid="same#2"><tuv xml:lang="en"><seg>two</seg></tuv></tu>
<tu tuid="same"><tuv xml:lang="en"><seg>three</seg></tuv></tu>
</body></tmx>""",
        encoding="utf-8",
    )
    temporary_root = Path(gettempdir())
    before = set(temporary_root.glob("lokit-unit-ids-*"))
    iterator = cast("Iterator[tuple[str, Data]]", lokit.stream.tmx(str(source)).items)

    assert next(iterator)[0] == "same"
    cast("_Closable", iterator).close()

    assert set(temporary_root.glob("lokit-unit-ids-*")) == before
