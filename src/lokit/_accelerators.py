from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from typing import Final, Protocol, cast

StatusClassifier = Callable[[str], int]


class _NumbaModule(Protocol):
    def njit(self, *, cache: bool = False) -> Callable[[StatusClassifier], StatusClassifier]: ...

_NUMBA_ENV: Final = "LOKIT_ENABLE_NUMBA"


def numba_enabled() -> bool:
    value = os.environ.get(_NUMBA_ENV, "")
    return value.lower() in {"1", "true", "yes", "on"}


def _compile_status_classifier() -> StatusClassifier | None:
    if not numba_enabled():
        return None
    try:
        numba = cast(_NumbaModule, importlib.import_module("numba"))
    except ImportError:
        return None

    return numba.njit(cache=True)(_status_code_python)


def _status_code_python(value: str) -> int:
    if value in ("approved", "signed-off", "final"):
        return 1
    if value in ("reviewed", "review"):
        return 2
    if value in ("translated", "complete"):
        return 3
    if value == "new":
        return 4
    if value in ("draft", "notapproved", "not-approved", "unapproved"):
        return 5
    if value == "rejected":
        return 6
    return 0


STATUS_CODE: Final[StatusClassifier] = (
    _compile_status_classifier() or _status_code_python
)
