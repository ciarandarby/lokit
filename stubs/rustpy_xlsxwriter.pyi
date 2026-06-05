from collections.abc import Iterable
from os import PathLike
from typing import BinaryIO, TypeVar

_FastExcelT = TypeVar("_FastExcelT", bound="FastExcel")

class FastExcel:
    def __init__(
        self,
        target: str | PathLike[str] | BinaryIO,
        *,
        password: str | None = None,
        autofit: bool = True,
    ) -> None: ...
    def sheet(
        self: _FastExcelT,
        name: str,
        data: Iterable[dict[str, str]],
        *,
        column_width: float | None = None,
        column_widths: dict[str, float] | list[float] | None = None,
        column_formats: object | None = None,
        header_format: object | None = None,
    ) -> _FastExcelT: ...
    def save(self) -> None: ...
