from __future__ import annotations

import contextlib
import os
import tempfile
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal, TextIO, cast, overload

if TYPE_CHECKING:
    from collections.abc import Iterator


@overload
def atomic_output_path(
    path: Path,
    mode: Literal[
        "w",
        "wt",
        "w+",
        "wt+",
        "a",
        "at",
        "a+",
        "at+",
        "x",
        "xt",
        "x+",
        "xt+",
    ],
) -> AbstractContextManager[TextIO]: ...


@overload
def atomic_output_path(
    path: Path,
    mode: Literal[
        "wb",
        "w+b",
        "wb+",
        "ab",
        "a+b",
        "ab+",
        "xb",
        "x+b",
        "xb+",
    ] = "wb",
) -> AbstractContextManager[BinaryIO]: ...


@overload
def atomic_output_path(
    path: Path,
    mode: str,
) -> AbstractContextManager[BinaryIO | TextIO]: ...


def atomic_output_path(
    path: Path,
    mode: str = "wb",
) -> AbstractContextManager[BinaryIO | TextIO]:
    return _atomic_output_path(path, mode)


@contextmanager
def _atomic_output_path(path: Path, mode: str) -> Iterator[BinaryIO | TextIO]:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode=mode,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            yield cast("BinaryIO | TextIO", tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if directory_flag is not None:
            dir_fd = os.open(path.parent, directory_flag)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
        raise
