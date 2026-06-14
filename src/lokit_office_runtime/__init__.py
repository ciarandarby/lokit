from __future__ import annotations

from importlib import resources
from pathlib import Path


def executable_path() -> Path:
    candidate = resources.files(__name__) / "bin" / _executable_name()
    with resources.as_file(candidate) as path:
        return path


def _executable_name() -> str:
    import os

    return "lokit-office.exe" if os.name == "nt" else "lokit-office"
