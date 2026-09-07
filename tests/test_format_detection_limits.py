from __future__ import annotations

import struct
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


def test_zip_probe_rejects_duplicate_content_type_members() -> None:
    payload = _zip_payload({"[Content_Types].xml": b"<Types/>", "[CONTENT_TYPES].XML": b"<Types/>"})
    with pytest.raises(ValueError, match=r"duplicate \[Content_Types\]\.xml"):
        format_detection.detect_format_from_bytes(payload)


def test_zip_probe_checks_actual_directory_count() -> None:
    payload = bytearray(_zip_payload({"word/document.xml": b"", "other": b""}))
    end = payload.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", payload, end + 8, 1, 1)
    with pytest.raises(ValueError, match="Could not detect input format"):
        format_detection.detect_format_from_bytes(bytes(payload))


def test_zip_probe_bounds_directory_allocation_before_indexing() -> None:
    payload = bytearray(_zip_payload({"word/document.xml": b""}))
    end = payload.rfind(b"PK\x05\x06")
    struct.pack_into("<I", payload, end + 12, 64 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="central directory exceeds"):
        format_detection.detect_format_from_bytes(bytes(payload))


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_zip_probe_detects_all_supported_content_type_compressions(compression: int) -> None:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", b"wordprocessingml.document.main+xml")
    assert format_detection.detect_format_from_bytes(output.getvalue()) is format_detection.LokitInputFormat.DOCX


def test_zip_probe_detects_zip64_local_entry() -> None:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive, archive.open("[Content_Types].xml", "w", force_zip64=True) as entry:
        entry.write(b"wordprocessingml.document.main+xml")
    assert format_detection.detect_format_from_bytes(output.getvalue()) is format_detection.LokitInputFormat.DOCX


def test_zip_probe_accepts_exact_non_zip64_entry_boundary() -> None:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", allowZip64=False) as archive:
        archive.writestr("word/document.xml", b"")
        for index in range(65534):
            archive.writestr(str(index), b"")
    assert format_detection.detect_format_from_bytes(output.getvalue()) is format_detection.LokitInputFormat.DOCX
