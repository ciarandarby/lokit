from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

import pytest

from lokit.importers import stream_idml

if TYPE_CHECKING:
    from pathlib import Path


def test_idml_import_rejects_suspicious_story_compression_ratio(tmp_path: Path) -> None:
    source = tmp_path / "bomb.idml"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Stories/Story_bomb.xml", b"x" * (2 * 1024 * 1024))

    with pytest.raises(ValueError, match="suspicious compression ratio"):
        list(stream_idml(str(source)).items)


def test_idml_import_rejects_unsafe_member_names_before_story_parsing(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.idml"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("../outside", b"not extracted")
        archive.writestr("Stories/Story_valid.xml", b"<Story/>")

    with pytest.raises(ValueError, match="unsafe IDML ZIP entry"):
        list(stream_idml(str(source)).items)
