from __future__ import annotations

from collections.abc import Generator, Iterator
from typing import cast

from lokit.data.dict_projection import iter_structure_rows
from lokit.data.structure import Data, StreamingStructure
from lokit.parsers.projection import project_items
from lokit.types import DictField, StringMode, TagSyntax, UnsupportedTagPolicy


class _ClosableUnits(Iterator[tuple[str, Data]]):
    def __init__(self) -> None:
        self._index = 0
        self.closed = False

    def __next__(self) -> tuple[str, Data]:
        if self._index >= 2:
            raise StopIteration
        self._index += 1
        return str(self._index), Data(source=f"Value {self._index}")

    def close(self) -> None:
        self.closed = True


def test_projected_stream_closes_source_on_early_exit() -> None:
    source = _ClosableUnits()
    projected = project_items(
        source,
        include_tags=False,
        tag_syntax=TagSyntax.NATIVE,
        native_syntax=TagSyntax.HTML,
        unsupported_tags=UnsupportedTagPolicy.ERROR,
        runtime_placeholders=True,
        inline_placeholders=True,
    )

    assert next(projected)[0] == "1"
    cast("Generator[tuple[str, Data], None, None]", projected).close()

    assert source.closed


def test_dictionary_projection_closes_source_on_early_exit() -> None:
    source = _ClosableUnits()
    rows = iter_structure_rows(
        StreamingStructure(source_locale="en", target_locale=None, items=source),
        fields=(DictField.UNIT_ID, DictField.SOURCE),
        strings=StringMode.SANITIZED,
    )

    assert next(rows) == {"unit_id": "1", "source": "Value 1"}
    cast("Generator[dict[str, str], None, None]", rows).close()

    assert source.closed
