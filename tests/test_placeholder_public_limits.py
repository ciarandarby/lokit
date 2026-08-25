from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from lokit.data.structure import Data
from lokit.placeholders import (
    PlaceholderSyntax,
    canonicalize,
    detect,
    project,
    project_data,
    reform,
)

if TYPE_CHECKING:
    from collections.abc import Callable


_PYTHON_BRACE = (PlaceholderSyntax.PYTHON_BRACE,)


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda: detect("{one}{two}", _PYTHON_BRACE, max_occurrences=1), id="detect"),
        pytest.param(lambda: project("{one}{two}", _PYTHON_BRACE, max_occurrences=1), id="project"),
        pytest.param(
            lambda: project_data(
                Data(source="{one}{two}"),
                syntaxes=_PYTHON_BRACE,
                max_occurrences=1,
            ),
            id="project-data",
        ),
        pytest.param(
            lambda: canonicalize("{one}{two}", _PYTHON_BRACE, max_occurrences=1),
            id="canonicalize",
        ),
        pytest.param(
            lambda: reform(
                "{one}{two}",
                "{one}{two}",
                "{first}{second}",
                _PYTHON_BRACE,
                max_occurrences=1,
            ),
            id="reform",
        ),
    ],
)
def test_public_placeholder_operations_enforce_occurrence_limit(
    operation: Callable[[], object],
) -> None:
    with pytest.raises(ValueError, match="placeholder occurrence limit 1 exceeded"):
        operation()


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda: project("{value}", _PYTHON_BRACE, max_input_bytes=1), id="project"),
        pytest.param(
            lambda: canonicalize("{value}", _PYTHON_BRACE, max_input_bytes=1),
            id="canonicalize",
        ),
        pytest.param(
            lambda: reform(
                "{value}",
                "{value}",
                "{renamed}",
                _PYTHON_BRACE,
                max_input_bytes=1,
            ),
            id="reform",
        ),
    ],
)
def test_public_placeholder_operations_enforce_input_limit(
    operation: Callable[[], object],
) -> None:
    with pytest.raises(ValueError, match="placeholder input is 7 bytes; limit is 1"):
        operation()


def test_public_placeholder_limits_reject_zero() -> None:
    with pytest.raises(ValueError, match="max_nesting must be greater than zero"):
        project("{value}", _PYTHON_BRACE, max_nesting=0)
