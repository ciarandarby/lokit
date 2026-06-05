from collections.abc import Iterator
from pathlib import Path
from typing import TypeVar

_CalamineWorkbookT = TypeVar("_CalamineWorkbookT", bound="CalamineWorkbook")

class CalamineSheet:
    name: str
    start: tuple[int, int]
    end: tuple[int, int]
    width: int
    height: int
    total_width: int
    total_height: int
    def iter_rows(self) -> Iterator[list[object]]: ...
    def to_python(
        self,
        skip_empty_area: bool = True,
        nrows: int | None = None,
    ) -> list[list[object]]: ...

class CalamineWorkbook:
    sheet_names: list[str]
    @classmethod
    def from_path(cls: type[_CalamineWorkbookT], path: str | Path) -> _CalamineWorkbookT: ...
    def get_sheet_by_name(self, name: str) -> CalamineSheet: ...
