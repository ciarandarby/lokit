from __future__ import annotations

import asyncio
import hashlib
import json
import time
import tracemalloc
import zipfile
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from lokit.exporters.idml import export_idml
from lokit.exporters.po import export_po
from lokit.importers import import_idml, import_po, import_po_async

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from lokit.data.structure import Data

TESTS_ROOT = Path(__file__).resolve().parent
VENDOR_ROOT = TESTS_ROOT / "fixtures" / "vendor"
MANIFEST_PATH = TESTS_ROOT / "fixtures" / "vendor_manifest.json"
MAX_PO_IMPORT_SECONDS = 5.0
MAX_IDML_IMPORT_SECONDS = 5.0
MAX_PO_PEAK_BYTES = 16 * 1024 * 1024
MAX_IDML_PEAK_BYTES = 64 * 1024 * 1024
PO_METADATA_EXTENSION = "po_metadata_json"


class VendorEntry(TypedDict):
    name: str
    kind: str
    source_url: str
    download_url: str
    commit_sha: str
    license: str
    license_path: str
    original_path: str
    local_path: str
    sha256: str
    reason: str


def test_vendor_fixture_manifest_hashes_and_licenses() -> None:
    entries = _vendor_entries()
    license_paths = {entry["local_path"] for entry in entries if entry["kind"] == "license"}

    for entry in entries:
        local_path = _entry_path(entry)
        assert local_path.exists(), f"Missing vendor fixture: {entry['local_path']}"
        assert _sha256(local_path) == entry["sha256"], f"Vendor fixture hash changed: {entry['local_path']}"
        assert entry["commit_sha"] in entry["download_url"], f"Vendor download is not commit-pinned: {entry['name']}"
        assert entry["source_url"].startswith("https://github.com/"), entry["source_url"]
        if entry["kind"] != "license":
            assert entry["license_path"] in license_paths, f"Missing license notice for {entry['name']}"
            assert (VENDOR_ROOT / entry["license_path"]).exists(), f"Missing license file for {entry['name']}"


def test_vendor_po_fixtures_import_export_reimport(tmp_path: Path) -> None:
    for entry in _verified_entries_by_kind("po"):
        path = _entry_path(entry)
        locale = _po_locale(entry)
        document = import_po(str(path), source_locale="en", target_locale=locale, progress=False)
        metadata = _po_metadata(document.extensions)

        assert document.target_locale == locale
        assert document.data
        assert any(unit.plural is not None for unit in document.data.values()) or entry["name"].startswith(
            "astro-gettext"
        )
        if entry["name"].startswith("gettext-po-samples-"):
            assert metadata["Language"] == locale
            assert "Plural-Forms" in metadata

        output = tmp_path / path.name
        export_po(document, output)
        reparsed = import_po(str(output), source_locale="en", target_locale=locale, progress=False)
        reparsed_metadata = _po_metadata(reparsed.extensions)

        assert len(reparsed.data) == len(document.data)
        if entry["name"].startswith("gettext-po-samples-"):
            assert reparsed_metadata["Language"] == locale
            assert reparsed_metadata["Plural-Forms"] == metadata["Plural-Forms"]
        _assert_reparsed_po_payload(entry, document.data.values(), reparsed.data.values())


def test_vendor_idml_fixtures_import_export_reimport(tmp_path: Path) -> None:
    for entry in _verified_entries_by_kind("idml"):
        path = _entry_path(entry)
        original_names = _zip_names(path)
        document = import_idml(str(path), source_locale="und", target_locale="x-test", progress=False)
        stories = {unit.extensions.get("story", "") for unit in document.data.values()}
        first_id, first_unit = next(iter(document.data.items()))
        translated = first_unit.source + "\nLokit fixture translation"
        first_unit.target = translated

        output = tmp_path / path.name
        export_idml(document, output, source_idml=path)
        exported_names = _zip_names(output)
        duplicate_names = [name for name, count in Counter(exported_names).items() if count > 1]
        reparsed = import_idml(str(output), source_locale="x-test", progress=False)

        assert document.extensions["source_file"] == str(path)
        assert document.extensions["source_idml"] == str(path)
        assert "" not in stories
        assert len(stories) >= 1
        if entry["name"].startswith("DTF_Proofs-"):
            assert len(stories) > 1
            assert any(_contains_non_latin(unit.source) for unit in document.data.values())
        assert set(exported_names) == set(original_names)
        assert duplicate_names == []
        assert reparsed.data[first_id].source == translated


def test_vendor_po_async_import_memory_smoke() -> None:
    entry = _verified_entry("gettext-po-samples-ar")
    path = _entry_path(entry)
    expected_count = len(import_po(str(path), source_locale="en", target_locale="ar", progress=False).data)

    tracemalloc.start()
    started = time.perf_counter()
    count = asyncio.run(_count_po_async(path, "ar"))
    seconds = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert count == expected_count
    assert seconds < MAX_PO_IMPORT_SECONDS
    assert peak < MAX_PO_PEAK_BYTES


def test_vendor_idml_import_memory_smoke() -> None:
    entry = _verified_entry("DTF_Proofs-devanagari-marathi")
    path = _entry_path(entry)

    tracemalloc.start()
    started = time.perf_counter()
    document = import_idml(str(path), source_locale="und", progress=False)
    seconds = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(document.data) == 6
    assert len({unit.extensions.get("story", "") for unit in document.data.values()}) > 1
    assert seconds < MAX_IDML_IMPORT_SECONDS
    assert peak < MAX_IDML_PEAK_BYTES


async def _count_po_async(path: Path, locale: str) -> int:
    count = 0
    async for _unit_id, _unit in import_po_async(str(path), source_locale="en", target_locale=locale):
        count += 1
    return count


def _assert_reparsed_po_payload(
    entry: VendorEntry,
    original_units: Iterable[Data],
    reparsed_units: Iterable[Data],
) -> None:
    original_targets = {unit.target for unit in original_units if unit.target}
    reparsed_targets = {unit.target for unit in reparsed_units if unit.target}

    if entry["name"] == "astro-gettext-ja-component":
        assert "\u3053\u3093\u306b\u3061\u306f" in reparsed_targets
        assert original_targets <= reparsed_targets
        return
    if entry["name"] == "gettext-po-samples-ar":
        assert "%d zero" in reparsed_targets
        assert "%d other" in reparsed_targets
        return
    assert original_targets <= reparsed_targets


def _vendor_entries() -> list[VendorEntry]:
    raw: object = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AssertionError("Vendor manifest must be a JSON object")
    raw_mapping = cast("Mapping[str, object]", raw)
    raw_files = raw_mapping.get("files")
    if not isinstance(raw_files, list):
        raise AssertionError("Vendor manifest must contain a files array")

    entries: list[VendorEntry] = []
    for raw_entry in raw_files:
        if not isinstance(raw_entry, dict):
            raise AssertionError("Vendor manifest file entries must be objects")
        entry_mapping = cast("Mapping[str, object]", raw_entry)
        entries.append(
            {
                "name": _required_str(entry_mapping, "name"),
                "kind": _required_str(entry_mapping, "kind"),
                "source_url": _required_str(entry_mapping, "source_url"),
                "download_url": _required_str(entry_mapping, "download_url"),
                "commit_sha": _required_str(entry_mapping, "commit_sha"),
                "license": _required_str(entry_mapping, "license"),
                "license_path": _required_str(entry_mapping, "license_path"),
                "original_path": _required_str(entry_mapping, "original_path"),
                "local_path": _required_str(entry_mapping, "local_path"),
                "sha256": _required_str(entry_mapping, "sha256"),
                "reason": _required_str(entry_mapping, "reason"),
            }
        )
    return entries


def _verified_entries_by_kind(kind: str) -> list[VendorEntry]:
    return [_verify_entry(entry) for entry in _vendor_entries() if entry["kind"] == kind]


def _verified_entry(name: str) -> VendorEntry:
    for entry in _vendor_entries():
        if entry["name"] == name:
            return _verify_entry(entry)
    raise AssertionError(f"Vendor manifest is missing entry {name!r}")


def _verify_entry(entry: VendorEntry) -> VendorEntry:
    local_path = _entry_path(entry)
    assert local_path.exists(), f"Missing vendor fixture: {entry['local_path']}"
    assert _sha256(local_path) == entry["sha256"], f"Vendor fixture hash changed: {entry['local_path']}"
    return entry


def _required_str(entry: Mapping[str, object], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"Vendor manifest entry is missing non-empty {key!r}")
    return value


def _entry_path(entry: VendorEntry) -> Path:
    return VENDOR_ROOT / entry["local_path"]


def _po_locale(entry: VendorEntry) -> str:
    if entry["name"] == "astro-gettext-ja-component":
        return "ja"
    return Path(entry["original_path"]).stem.replace("_", "-")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path, "r") as archive:
        return archive.namelist()


def _po_metadata(extensions: Mapping[str, str]) -> dict[str, str]:
    raw_metadata = extensions.get(PO_METADATA_EXTENSION)
    if raw_metadata is None:
        return {}
    parsed: object = json.loads(raw_metadata)
    if not isinstance(parsed, dict):
        return {}

    metadata: dict[str, str] = {}
    for key, value in parsed.items():
        if isinstance(key, str) and isinstance(value, str):
            metadata[key] = value
    return metadata


def _contains_non_latin(text: str) -> bool:
    return any(ord(char) > 0x024F for char in text)
