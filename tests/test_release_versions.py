from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "tools" / "verify_release_versions.py"


def _verify(expected_release: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, str(SCRIPT), "--expected-release", expected_release),
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "value",
    (
        "0.5.2",
        "v0.5.2",
    ),
)
def test_accept_release_tag_for_current_version(value: str) -> None:
    result = _verify(value)

    assert result.returncode == 0, result.stderr
    assert "verified Python/native/office release cohort 0.5.2" in result.stdout


@pytest.mark.parametrize("value", ("v0.4", "v0.5", "v0.5.0","v0.5.1"))
def test_reject_mismatched_release_tag(value: str) -> None:
    result = _verify(value)

    assert result.returncode == 1
    assert "but the source cohort is 0.5.2" in result.stderr


@pytest.mark.parametrize("value", ("", "v", "0", "v0", "0.5.0.0", "release-0.5"))
def test_reject_invalid_expected_release(value: str) -> None:
    result = _verify(value)

    assert result.returncode == 1
    assert "invalid release version" in result.stderr
