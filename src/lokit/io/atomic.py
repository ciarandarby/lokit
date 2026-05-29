from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


@contextmanager
def atomic_output_path(path: Path, mode: str = "wb") -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode=mode,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        with tmp:
            yield tmp
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
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise
