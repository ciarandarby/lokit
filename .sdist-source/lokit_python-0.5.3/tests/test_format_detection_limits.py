from __future__ import annotations

import zipfile
from io import BytesIO

import pytest

import lokit.format_detection as format_detection


def _zip_payload(entries: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_zip_byte_detection_rejects_excessive_entry_count() -> None:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(100_001):
            archive.writestr(str(index), b"")

    with pytest.raises(ValueError, match="100000-entry format-detection limit"):
        format_detection.detect_format_from_bytes(output.getvalue())


def test_zip_byte_detection_bounds_content_types_decompression() -> None:
    payload = _zip_payload(
        {
            "[Content_Types].xml": b"x" * ((2 * 1024 * 1024) + 1),
            "word/document.xml": b"",
        }
    )

    with pytest.raises(ValueError, match="Could not detect input format"):
        format_detection.detect_format_from_bytes(payload)
