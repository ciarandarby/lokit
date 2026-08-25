from __future__ import annotations

import json
import zipfile
from io import BytesIO, StringIO
from typing import TYPE_CHECKING

import pytest

from lokit.format_detection import (
    LokitInputFormat,
    _is_lokit_json_stream,
    detect_format,
    detect_format_from_bytes,
)

if TYPE_CHECKING:
    from pathlib import Path


def _zip_payload(entries: dict[str, str]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


class _GuardedReader(StringIO):
    def __init__(self, value: str, maximum_read: int) -> None:
        super().__init__(value)
        self.maximum_read = maximum_read
        self.characters_read = 0

    def read(self, size: int | None = -1, /) -> str:
        if size is None or size < 0:
            raise AssertionError("JSON format probing must use bounded reads")
        result = super().read(size)
        self.characters_read += len(result)
        if self.characters_read > self.maximum_read:
            raise AssertionError("JSON format probing read past the structural prefix")
        return result


def test_pot_path_detects_as_gettext(tmp_path: Path) -> None:
    path = tmp_path / "messages.pot"
    path.write_text('msgid "Hello"\nmsgstr ""\n', encoding="utf-8")

    assert detect_format(path) is LokitInputFormat.PO


@pytest.mark.parametrize(
    "payload",
    [
        {"data": "Visible copy"},
        {"format_version": "Version label"},
        {"source_locale": "Source locale label", "data": {"message": "Visible copy"}},
    ],
)
def test_json_i18n_keys_do_not_mimic_lokit_model(tmp_path: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload).encode()
    path = tmp_path / "messages.json"
    path.write_bytes(encoded)

    assert detect_format(path) is LokitInputFormat.JSON_I18N
    assert detect_format_from_bytes(encoded) is LokitInputFormat.JSON_I18N


@pytest.mark.parametrize(
    "units",
    [
        {},
        {"greeting": {"source": "Hello", "target": "Bonjour"}},
    ],
)
def test_lokit_model_json_requires_its_envelope_and_unit_shape(
    tmp_path: Path,
    units: dict[str, object],
) -> None:
    payload = {
        "source_locale": "en",
        "target_locale": "fr",
        "data": units,
        "format_version": "0.1",
    }
    encoded = json.dumps(payload).encode()
    path = tmp_path / "document.json"
    path.write_bytes(encoded)

    assert detect_format(path) is LokitInputFormat.LOKIT_JSON
    assert detect_format_from_bytes(encoded) is LokitInputFormat.LOKIT_JSON


def test_large_lokit_json_probe_is_bounded_and_short_circuits() -> None:
    payload = '{"source_locale":"en","data":{"greeting":{"source":"Hello","target":"' + ("x" * 2_000_000) + '"}}}'
    reader = _GuardedReader(payload, maximum_read=32_768)

    assert _is_lokit_json_stream(reader)
    assert reader.characters_read < len(payload) // 10


def test_unknown_zip_bytes_are_not_assumed_to_be_xlsx() -> None:
    payload = _zip_payload({"notes.txt": "not an Office package"})

    with pytest.raises(ValueError, match="Could not detect input format"):
        detect_format_from_bytes(payload)


@pytest.mark.parametrize(
    "suffix",
    [".docm", ".dotm", ".pptm", ".potm", ".ppsm", ".sldm", ".xlsm", ".xltm", ".xlam"],
)
def test_macro_enabled_office_suffixes_are_rejected(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"document{suffix}"

    with pytest.raises(ValueError, match="Macro-enabled Office"):
        detect_format(path)


@pytest.mark.parametrize(
    ("part_name", "content_type"),
    [
        ("word/document.xml", "application/vnd.ms-word.document.macroEnabled.main+xml"),
        ("ppt/presentation.xml", "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"),
        ("xl/workbook.xml", "application/vnd.ms-excel.sheet.macroEnabled.main+xml"),
    ],
)
def test_macro_enabled_office_bytes_are_rejected(part_name: str, content_type: str) -> None:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Override PartName="/{part_name}" ContentType="{content_type}"/>'
        "</Types>"
    )
    payload = _zip_payload({"[Content_Types].xml": content_types, part_name: ""})

    with pytest.raises(ValueError, match="Macro-enabled Office"):
        detect_format_from_bytes(payload)


def test_vba_project_part_is_rejected_even_without_macro_content_type() -> None:
    payload = _zip_payload(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
            ),
            "xl/workbook.xml": "",
            "xl/vbaProject.bin": "macro payload",
        }
    )

    with pytest.raises(ValueError, match="Macro-enabled Office"):
        detect_format_from_bytes(payload)


def test_content_types_member_lookup_is_case_insensitive() -> None:
    payload = _zip_payload(
        {
            "[CONTENT_TYPES].XML": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/xl/workbook.xml" '
                'ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/>'
                "</Types>"
            ),
            "xl/workbook.xml": "",
        }
    )

    with pytest.raises(ValueError, match="Macro-enabled Office"):
        detect_format_from_bytes(payload)
