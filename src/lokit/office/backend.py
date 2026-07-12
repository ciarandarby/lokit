from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import posixpath
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lxml import etree
from tqdm import tqdm

from lokit.data.structure import AdjacentContext, BaseStructure, Data, Meta, StreamingStructure, TranslationStatus
from lokit.data.targets import select_target
from lokit.office.errors import (
    OfficePackageError,
    OfficeReinsertionError,
    OfficeUnsupportedPackageError,
    OfficeValidationError,
)
from lokit.office.models import DocumentSink, DocumentSource, OfficeExportResult, OfficeWarning
from lokit.office.options import (
    ExtraTranslationPolicy,
    MissingTranslationPolicy,
    OfficeExportOptions,
    OfficeImportOptions,
)
from lokit.office.process import extract_with_worker, extract_with_worker_iter, reinsert_with_worker, worker_available
from lokit.office.runtime import load_runtime_info
from lokit.parsers.async_bridge import AsyncExtractionBridge

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Iterator

    from lxml.etree import _Element

ExtractItem = tuple[str, Data]

CONTENT_TYPES = "[Content_Types].xml"
DOCX_MAIN_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
}
PPTX_MAIN_TYPES = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
}
WORD_PART_PREFIXES = (
    "word/document.xml",
    "word/header",
    "word/footer",
    "word/footnotes.xml",
    "word/endnotes.xml",
    "word/comments.xml",
)
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


@dataclass(frozen=True, slots=True)
class _SourceFile:
    path: Path
    fingerprint: str
    cleanup: Path | None = None


@dataclass(frozen=True, slots=True)
class _OfficeUnit:
    unit_id: str
    data: Data
    part: str
    paragraph_index: int


class OfficeBackend:
    def extract(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
    ) -> Iterator[ExtractItem]:
        opts = options or OfficeImportOptions()
        with _materialize_source(source, f".{file_format}", opts) as source_file:
            if _use_worker():
                items = extract_with_worker_iter(
                    source_file.path,
                    file_format,
                    source_locale,
                    target_locale,
                    opts,
                )
                yield from _with_adjacent_context_items(items, source_file.fingerprint)
                return
            units = _extract_units(source_file.path, file_format, source_file.fingerprint, opts)
            for unit_id, data in _with_adjacent_context(units):
                data.extensions.setdefault("office.source_fingerprint", source_file.fingerprint)
                yield unit_id, data

    def stream(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
        progress: bool = False,
    ) -> StreamingStructure:
        opts = options or OfficeImportOptions()
        source_path = str(source) if isinstance(source, (str, Path)) else ""
        items: Iterable[ExtractItem] = self.extract(source, file_format, source_locale, target_locale, opts)
        if progress:
            items = tqdm(items, desc=f"Parsing {file_format.upper()}", unit="units")
        return StreamingStructure(
            source_locale=source_locale,
            target_locale=target_locale,
            items=items,
            source_language=_base_language(source_locale),
            target_language=_base_language(target_locale),
            extensions=_document_extensions(file_format, "", source_path),
        )

    def extract_async(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
    ) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(
            lambda: self.extract(source, file_format, source_locale, target_locale, options)
        )

    def import_document(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
        progress: bool = True,
    ) -> BaseStructure:
        opts = options or OfficeImportOptions()
        with _materialize_source(source, f".{file_format}", opts) as source_file:
            if _use_worker():
                fingerprint, items = extract_with_worker(
                    source_file.path,
                    file_format,
                    source_locale,
                    target_locale,
                    opts,
                )
                data = {
                    unit_id: unit_data
                    for unit_id, unit_data in tqdm(
                        items,
                        desc=f"Parsing {file_format.upper()}",
                        unit="units",
                        disable=not progress,
                    )
                }
                source_fingerprint = fingerprint
            else:
                units = _extract_units(source_file.path, file_format, source_file.fingerprint, opts)
                data = {
                    unit_id: unit_data
                    for unit_id, unit_data in tqdm(
                        _with_adjacent_context(units),
                        desc=f"Parsing {file_format.upper()}",
                        unit="units",
                        disable=not progress,
                    )
                }
                source_fingerprint = source_file.fingerprint
            return BaseStructure(
                source_locale=source_locale,
                target_locale=target_locale,
                data=data,
                source_language=_base_language(source_locale),
                target_language=_base_language(target_locale),
                extensions=_document_extensions(file_format, source_fingerprint, str(source_file.path)),
            )

    def reinsert(
        self,
        document: BaseStructure | StreamingStructure,
        output: DocumentSink,
        file_format: str,
        source_document: DocumentSource | None = None,
        target_locale: str | None = None,
        options: OfficeExportOptions | None = None,
    ) -> OfficeExportResult:
        opts = options or OfficeExportOptions()
        source = source_document or _source_document_from_extensions(document)
        if source is None:
            raise OfficeReinsertionError(
                f"{file_format.upper()} export requires source_{file_format} or document.extensions['source_file']"
            )
        selected = _selected_document(document, target_locale)
        with _materialize_source(source, f".{file_format}", opts) as source_file:
            return _write_output(selected, output, file_format, source_file, opts, target_locale)


_BACKEND = OfficeBackend()


def import_docx(
    source: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    progress: bool = True,
) -> BaseStructure:
    """Parses DOCX into a structured BaseStructure."""
    return _BACKEND.import_document(source, "docx", source_locale, target_locale, options, progress)


def stream_docx(
    source: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    progress: bool = False,
) -> StreamingStructure:
    """Asynchronously streams docx translation units."""
    return _BACKEND.stream(source, "docx", source_locale, target_locale, options, progress)


def import_docx_async(
    source: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
) -> AsyncIterator[ExtractItem]:
    """Async generator streaming docx translation units."""
    return _BACKEND.extract_async(source, "docx", source_locale, target_locale, options)


def export_docx(
    document: BaseStructure | StreamingStructure,
    output: DocumentSink,
    *,
    source_docx: DocumentSource | None = None,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
) -> OfficeExportResult:
    """Reinserts translated units back into a DOCX file."""
    return _BACKEND.reinsert(document, output, "docx", source_docx, target_locale, options)


async def export_docx_async(
    document: BaseStructure | StreamingStructure,
    output: DocumentSink,
    *,
    source_docx: DocumentSource | None = None,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
) -> OfficeExportResult:
    """Async version of export docx using thread pool."""
    return await asyncio.to_thread(
        export_docx,
        document,
        output,
        source_docx=source_docx,
        target_locale=target_locale,
        options=options,
    )


def import_pptx(
    source: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    progress: bool = True,
) -> BaseStructure:
    """Parses PPTX into a structured BaseStructure."""
    return _BACKEND.import_document(source, "pptx", source_locale, target_locale, options, progress)


def stream_pptx(
    source: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    progress: bool = False,
) -> StreamingStructure:
    """Asynchronously streams pptx translation units."""
    return _BACKEND.stream(source, "pptx", source_locale, target_locale, options, progress)


def import_pptx_async(
    source: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
) -> AsyncIterator[ExtractItem]:
    """Async generator streaming pptx translation units."""
    return _BACKEND.extract_async(source, "pptx", source_locale, target_locale, options)


def export_pptx(
    document: BaseStructure | StreamingStructure,
    output: DocumentSink,
    *,
    source_pptx: DocumentSource | None = None,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
) -> OfficeExportResult:
    """Reinserts translated units back to a PPTX file."""
    return _BACKEND.reinsert(document, output, "pptx", source_pptx, target_locale, options)


async def export_pptx_async(
    document: BaseStructure | StreamingStructure,
    output: DocumentSink,
    *,
    source_pptx: DocumentSource | None = None,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
) -> OfficeExportResult:
    """Async version of export pptx using thread pool."""
    return await asyncio.to_thread(
        export_pptx,
        document,
        output,
        source_pptx=source_pptx,
        target_locale=target_locale,
        options=options,
    )


@contextlib.contextmanager
def _materialize_source(
    source: DocumentSource,
    suffix: str,
    options: OfficeImportOptions,
) -> Iterator[_SourceFile]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        fingerprint = _sha256_file(path, options.max_compressed_bytes)
        yield _SourceFile(path=path, fingerprint=fingerprint)
        return

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            digest = hashlib.sha256()
            written = 0
            if isinstance(source, bytes):
                written = len(source)
                if written > options.max_compressed_bytes:
                    raise OfficePackageError("Office source exceeds max_compressed_bytes")
                digest.update(source)
                tmp.write(source)
            else:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > options.max_compressed_bytes:
                        raise OfficePackageError("Office source exceeds max_compressed_bytes")
                    digest.update(chunk)
                    tmp.write(chunk)
        yield _SourceFile(path=tmp_path, fingerprint=f"sha256:{digest.hexdigest()}", cleanup=tmp_path)
    finally:
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()


def _extract_units(
    path: Path,
    file_format: str,
    fingerprint: str,
    options: OfficeImportOptions,
) -> list[_OfficeUnit]:
    with zipfile.ZipFile(path, "r") as zf:
        names = _preflight_zip(zf, options)
        actual = _detect_ooxml_format(zf, names)
        if actual != file_format:
            raise OfficeUnsupportedPackageError(f"Expected {file_format.upper()} package, detected {actual.upper()}")
        parts = _docx_parts(names, options) if file_format == "docx" else _pptx_parts(zf, names, options)
        units: list[_OfficeUnit] = []
        for part in parts:
            if part not in names:
                continue
            with zf.open(part) as stream:
                xml = stream.read(options.max_unit_bytes + 1)
            if len(xml) > options.max_unit_bytes:
                raise OfficePackageError(f"Office XML part exceeds max_unit_bytes: {part}")
            root = _parse_xml(xml, part)
            if file_format == "docx":
                units.extend(_extract_docx_part(root, part, fingerprint, options))
            else:
                units.extend(_extract_pptx_part(root, part, fingerprint, options))
        return units


def _preflight_zip(zf: zipfile.ZipFile, options: OfficeImportOptions) -> set[str]:
    infos = zf.infolist()
    if len(infos) > options.max_zip_entries:
        raise OfficePackageError("Office package has too many ZIP entries")
    seen: set[str] = set()
    compressed = 0
    uncompressed = 0
    for info in infos:
        name = _normalize_part_name(info.filename)
        if not name or name != info.filename:
            raise OfficePackageError(f"Unsafe Office ZIP entry name: {info.filename!r}")
        if name in seen:
            raise OfficePackageError(f"Duplicate Office ZIP entry: {name}")
        seen.add(name)
        compressed += info.compress_size
        uncompressed += info.file_size
        if compressed > options.max_compressed_bytes:
            raise OfficePackageError("Office package exceeds max_compressed_bytes")
        if uncompressed > options.max_uncompressed_bytes:
            raise OfficePackageError("Office package exceeds max_uncompressed_bytes")
        if info.compress_size and info.file_size / info.compress_size > options.max_compression_ratio:
            raise OfficePackageError(f"Suspicious compression ratio in Office ZIP entry: {name}")
        if info.flag_bits & 0x1:
            raise OfficeUnsupportedPackageError("Encrypted Office packages are not supported")
    if CONTENT_TYPES not in seen:
        raise OfficePackageError("Office package is missing [Content_Types].xml")
    return seen


def _normalize_part_name(name: str) -> str:
    if name.startswith("/") or "\\" in name or "\x00" in name:
        return ""
    normalized = posixpath.normpath(name)
    if normalized in ("", ".") or normalized.startswith("../") or normalized == "..":
        return ""
    return normalized


def _detect_ooxml_format(zf: zipfile.ZipFile, names: set[str]) -> str:
    with zf.open(CONTENT_TYPES) as stream:
        root = _parse_xml(stream.read(2 * 1024 * 1024), CONTENT_TYPES)
    content_types: dict[str, str] = {}
    for child in root:
        if _local_name(child.tag) != "Override":
            continue
        part_name = (child.get("PartName") or "").lstrip("/")
        content_type = child.get("ContentType") or ""
        content_types[part_name] = content_type
    if any(content_type in DOCX_MAIN_TYPES for content_type in content_types.values()) or "word/document.xml" in names:
        return "docx"
    if (
        any(content_type in PPTX_MAIN_TYPES for content_type in content_types.values())
        or "ppt/presentation.xml" in names
    ):
        return "pptx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if any(name.startswith("Stories/") for name in names):
        return "idml"
    raise OfficeUnsupportedPackageError("Unsupported OOXML package type")


def _docx_parts(names: set[str], options: OfficeImportOptions) -> list[str]:
    parts = ["word/document.xml"]
    if options.include_headers_footers:
        parts.extend(
            sorted(name for name in names if name.startswith(("word/header", "word/footer")) and name.endswith(".xml"))
        )
    if options.include_comments and "word/comments.xml" in names:
        parts.append("word/comments.xml")
    for name in ("word/footnotes.xml", "word/endnotes.xml"):
        if name in names:
            parts.append(name)
    return parts


def _pptx_parts(zf: zipfile.ZipFile, names: set[str], options: OfficeImportOptions) -> list[str]:
    slides = _presentation_slide_parts(zf, names)
    parts = (
        slides
        if slides
        else sorted(name for name in names if name.startswith("ppt/slides/slide") and name.endswith(".xml"))
    )
    if options.include_notes:
        parts.extend(
            sorted(name for name in names if name.startswith("ppt/notesSlides/notesSlide") and name.endswith(".xml"))
        )
    if options.include_master_layout_content:
        parts.extend(
            sorted(
                name
                for name in names
                if name.startswith(("ppt/slideLayouts/", "ppt/slideMasters/")) and name.endswith(".xml")
            )
        )
    return parts


def _presentation_slide_parts(zf: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "ppt/presentation.xml" not in names or "ppt/_rels/presentation.xml.rels" not in names:
        return []
    rels = _read_relationships(zf, "ppt/_rels/presentation.xml.rels")
    with zf.open("ppt/presentation.xml") as stream:
        root = _parse_xml(stream.read(4 * 1024 * 1024), "ppt/presentation.xml")
    parts: list[str] = []
    for slide_id in root.iter(f"{{{PRESENTATION_NS}}}sldId"):
        rel_id = slide_id.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        if rel_id is None:
            continue
        target = rels.get(rel_id, "")
        part = _resolve_relationship_target("ppt/presentation.xml", target)
        if part in names:
            parts.append(part)
    return parts


def _read_relationships(zf: zipfile.ZipFile, part: str) -> dict[str, str]:
    with zf.open(part) as stream:
        root = _parse_xml(stream.read(2 * 1024 * 1024), part)
    relationships: dict[str, str] = {}
    for child in root.iter(f"{{{REL_NS}}}Relationship"):
        rel_id = child.get("Id")
        target = child.get("Target")
        mode = child.get("TargetMode")
        if rel_id and target and mode != "External":
            relationships[rel_id] = target
    return relationships


def _resolve_relationship_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        resolved = target.lstrip("/")
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))
    if resolved.startswith("../") or resolved == "..":
        raise OfficePackageError(f"Relationship target escapes package: {target}")
    return resolved


def _extract_docx_part(
    root: _Element,
    part: str,
    fingerprint: str,
    options: OfficeImportOptions,
) -> list[_OfficeUnit]:
    container = _docx_container(part)
    units: list[_OfficeUnit] = []
    paragraph_index = 0
    for paragraph in root.iter(f"{{{WORD_NS}}}p"):
        text = _docx_paragraph_text(paragraph)
        if not text.strip():
            paragraph_index += 1
            continue
        if len(text) > options.max_text_unit_chars:
            raise OfficePackageError("DOCX text unit exceeds max_text_unit_chars")
        unit_id = f"docx:{container}:p/{paragraph_index}"
        units.append(
            _OfficeUnit(
                unit_id,
                _office_data(text, "docx", part, container, fingerprint),
                part,
                paragraph_index,
            )
        )
        paragraph_index += 1
    if options.include_alt_text:
        units.extend(_extract_alt_text(root, "docx", part, container, fingerprint, len(units)))
    return units


def _docx_container(part: str) -> str:
    if part == "word/document.xml":
        return "body"
    if part.startswith("word/header"):
        return f"header/{Path(part).stem.removeprefix('header') or 'default'}"
    if part.startswith("word/footer"):
        return f"footer/{Path(part).stem.removeprefix('footer') or 'default'}"
    if part == "word/comments.xml":
        return "comment"
    return Path(part).stem


def _docx_paragraph_text(paragraph: _Element) -> str:
    parts: list[str] = []
    for element in paragraph.iter():
        local = _local_name(element.tag)
        if local == "t" and element.text:
            parts.append(element.text)
        elif local == "tab":
            parts.append("\t")
        elif local in ("br", "cr"):
            parts.append("\n")
    return "".join(parts)


def _extract_pptx_part(
    root: _Element,
    part: str,
    fingerprint: str,
    options: OfficeImportOptions,
) -> list[_OfficeUnit]:
    container = _pptx_container(part)
    slide_number = _slide_number(part)
    units: list[_OfficeUnit] = []
    paragraph_index = 0
    for paragraph in root.iter(f"{{{DRAWING_NS}}}p"):
        text = _pptx_paragraph_text(paragraph)
        if not text.strip():
            paragraph_index += 1
            continue
        if len(text) > options.max_text_unit_chars:
            raise OfficePackageError("PPTX text unit exceeds max_text_unit_chars")
        unit_id = f"pptx:{container}:p/{paragraph_index}"
        data = _office_data(text, "pptx", part, container, fingerprint)
        if slide_number:
            data.extensions["office.slide_number"] = slide_number
        units.append(_OfficeUnit(unit_id, data, part, paragraph_index))
        paragraph_index += 1
    if options.include_alt_text:
        units.extend(_extract_alt_text(root, "pptx", part, container, fingerprint, len(units)))
    return units


def _pptx_container(part: str) -> str:
    if part.startswith("ppt/slides/slide"):
        return f"slide/{_slide_number(part)}"
    if part.startswith("ppt/notesSlides/notesSlide"):
        return f"slide/{_slide_number(part)}:notes"
    if part.startswith("ppt/slideLayouts/"):
        return f"layout/{Path(part).stem}"
    if part.startswith("ppt/slideMasters/"):
        return f"master/{Path(part).stem}"
    return Path(part).stem


def _slide_number(part: str) -> str:
    stem = Path(part).stem
    digits = "".join(ch for ch in stem if ch.isdigit())
    return digits


def _pptx_paragraph_text(paragraph: _Element) -> str:
    parts: list[str] = []
    for element in paragraph.iter():
        local = _local_name(element.tag)
        if local == "t" and element.text:
            parts.append(element.text)
        elif local == "br":
            parts.append("\n")
    return "".join(parts)


def _extract_alt_text(
    root: _Element,
    file_format: str,
    part: str,
    container: str,
    fingerprint: str,
    offset: int,
) -> list[_OfficeUnit]:
    units: list[_OfficeUnit] = []
    for index, element in enumerate(root.iter()):
        descr = element.get("descr")
        title = element.get("title")
        text = descr or title
        if not text or not text.strip():
            continue
        unit_id = f"{file_format}:{container}:alt/{offset + index}"
        data = _office_data(text, file_format, part, container, fingerprint)
        data.extensions["office.alt_text"] = "true"
        units.append(_OfficeUnit(unit_id, data, part, -1))
    return units


def _office_data(
    text: str,
    file_format: str,
    part: str,
    container: str,
    fingerprint: str,
) -> Data:
    return Data(
        source=text,
        meta=Meta(),
        status=TranslationStatus.UNKNOWN,
        extensions={
            "input_format": file_format,
            "office.format": file_format,
            "office.part": part,
            "office.container": container,
            "office.source_fingerprint": fingerprint,
        },
    )


def _with_adjacent_context(units: list[_OfficeUnit]) -> Iterator[ExtractItem]:
    for index, unit in enumerate(units):
        if index > 0 and units[index - 1].part == unit.part:
            previous = units[index - 1]
            unit.data.previous_context = AdjacentContext(previous.unit_id, previous.data.source)
        if index + 1 < len(units) and units[index + 1].part == unit.part:
            next_unit = units[index + 1]
            unit.data.next_context = AdjacentContext(next_unit.unit_id, next_unit.data.source)
        yield unit.unit_id, unit.data


def _with_adjacent_context_items(
    items: Iterator[ExtractItem],
    fingerprint: str,
) -> Iterator[ExtractItem]:
    previous: ExtractItem | None = None
    current: ExtractItem | None = next(items, None)
    while current is not None:
        next_item = next(items, None)
        unit_id, data = current
        data.extensions.setdefault("office.source_fingerprint", fingerprint)
        if previous is not None and _office_part(previous[1]) == _office_part(data):
            data.previous_context = AdjacentContext(previous[0], previous[1].source)
        if next_item is not None and _office_part(next_item[1]) == _office_part(data):
            data.next_context = AdjacentContext(next_item[0], next_item[1].source)
        yield unit_id, data
        previous = current
        current = next_item


def _office_part(data: Data) -> str:
    return data.extensions.get("office.part", "")


def _write_output(
    document: BaseStructure | StreamingStructure,
    output: DocumentSink,
    file_format: str,
    source_file: _SourceFile,
    options: OfficeExportOptions,
    target_locale: str | None,
) -> OfficeExportResult:
    output_path = Path(output) if isinstance(output, (str, Path)) else None
    if output_path is not None and document.target_locale is None and document.target_locales:
        if output_path.suffix:
            raise OfficeReinsertionError(
                f"{file_format.upper()} export needs a selected target locale for a single output path"
            )
        output_path.mkdir(parents=True, exist_ok=True)
        units_written = 0
        warnings: list[OfficeWarning] = []
        for locale in document.target_locales:
            result = _write_output(
                select_target(_as_base_structure(document), locale),
                output_path / f"{locale}.{file_format}",
                file_format,
                source_file,
                options,
                locale,
            )
            units_written += result.units_written
            warnings.extend(result.warnings)
        return OfficeExportResult(output_path, units_written, tuple(warnings), source_file.fingerprint)

    tmp_path = _temporary_output_path(output_path, f".{file_format}")
    try:
        translation_data = _translation_data_for(document, target_locale)
        translations = _plain_translations(translation_data, target_locale)
        if _use_worker():
            worker_result = reinsert_with_worker(
                source_file.path,
                tmp_path,
                file_format,
                translation_data,
                target_locale,
                options,
            )
            warnings = list(worker_result.warnings)
        else:
            warnings = _rewrite_package(source_file.path, tmp_path, file_format, translations, options)
        _validate_written_package(tmp_path, file_format, options)
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, output_path)
            final_path = output_path
        else:
            assert hasattr(output, "write")
            with tmp_path.open("rb") as stream:
                shutil.copyfileobj(stream, output)
            final_path = None
        output_bytes = output_path.stat().st_size if output_path is not None else tmp_path.stat().st_size
        return OfficeExportResult(
            output_path=final_path,
            units_written=len(translation_data),
            warnings=tuple(warnings),
            source_fingerprint=source_file.fingerprint,
            output_bytes=output_bytes,
        )
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise
    finally:
        if output_path is None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()


def _temporary_output_path(output_path: Path | None, suffix: str) -> Path:
    directory = output_path.parent if output_path is not None else None
    name = output_path.name if output_path is not None else "office-output"
    with tempfile.NamedTemporaryFile(
        dir=directory,
        prefix=f".{name}.",
        suffix=f"{suffix}.tmp",
        delete=False,
    ) as tmp:
        path = Path(tmp.name)
    return path


def _translation_data_for(
    document: BaseStructure | StreamingStructure,
    target_locale: str | None,
) -> dict[str, Data]:
    items = document.data.items() if isinstance(document, BaseStructure) else document.items
    translations: dict[str, Data] = {}
    for unit_id, data in items:
        text = data.target
        if target_locale and target_locale in data.targets:
            text = data.targets[target_locale].text
        if text is not None:
            translations[unit_id] = data
    return translations


def _plain_translations(
    translations: dict[str, Data],
    target_locale: str | None,
) -> dict[str, str]:
    plain: dict[str, str] = {}
    for unit_id, data in translations.items():
        text = data.target
        if target_locale and target_locale in data.targets:
            text = data.targets[target_locale].text
        if text is not None:
            plain[unit_id] = text
    return plain


def _rewrite_package(
    source_path: Path,
    output_path: Path,
    file_format: str,
    translations: dict[str, str],
    options: OfficeExportOptions,
) -> list[OfficeWarning]:
    warnings: list[OfficeWarning] = []
    consumed: set[str] = set()
    with zipfile.ZipFile(source_path, "r") as zin:
        names = _preflight_zip(zin, options)
        actual = _detect_ooxml_format(zin, names)
        if actual != file_format:
            raise OfficeUnsupportedPackageError(f"Expected {file_format.upper()} package, detected {actual.upper()}")
        parts = _docx_parts(names, options) if file_format == "docx" else _pptx_parts(zin, names, options)
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename in parts:
                    data = _rewrite_xml_part(
                        data,
                        info.filename,
                        file_format,
                        translations,
                        consumed,
                        options,
                        warnings,
                    )
                zout.writestr(_copy_zip_info(info), data)
    extras = set(translations) - consumed
    if extras:
        message = f"{len(extras)} supplied translation unit(s) did not match source document"
        if options.extra_translation_policy == ExtraTranslationPolicy.ERROR:
            raise OfficeReinsertionError(message)
        warnings.append(OfficeWarning("office.extra_translation", message))
    return warnings


def _copy_zip_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    copied = zipfile.ZipInfo(info.filename, info.date_time)
    copied.comment = info.comment
    copied.extra = info.extra
    copied.internal_attr = info.internal_attr
    copied.external_attr = info.external_attr
    copied.create_system = info.create_system
    copied.compress_type = zipfile.ZIP_DEFLATED
    return copied


def _rewrite_xml_part(
    xml: bytes,
    part: str,
    file_format: str,
    translations: dict[str, str],
    consumed: set[str],
    options: OfficeExportOptions,
    warnings: list[OfficeWarning],
) -> bytes:
    root = _parse_xml(xml, part)
    container = _docx_container(part) if file_format == "docx" else _pptx_container(part)
    paragraph_tag = f"{{{WORD_NS}}}p" if file_format == "docx" else f"{{{DRAWING_NS}}}p"
    for paragraph_index, paragraph in enumerate(root.iter(paragraph_tag)):
        unit_id = f"{file_format}:{container}:p/{paragraph_index}"
        replacement = translations.get(unit_id)
        if replacement is not None:
            if file_format == "docx":
                _replace_docx_paragraph(paragraph, replacement)
            else:
                _replace_pptx_paragraph(paragraph, replacement)
            consumed.add(unit_id)
        elif options.missing_translation_policy == MissingTranslationPolicy.ERROR:
            source = _docx_paragraph_text(paragraph) if file_format == "docx" else _pptx_paragraph_text(paragraph)
            if source.strip():
                raise OfficeReinsertionError(f"Missing translation for {unit_id}")
        elif options.missing_translation_policy == MissingTranslationPolicy.WARN:
            source = _docx_paragraph_text(paragraph) if file_format == "docx" else _pptx_paragraph_text(paragraph)
            if source.strip():
                warnings.append(
                    OfficeWarning(
                        "office.missing_translation",
                        f"Missing translation for {unit_id}",
                        unit_id,
                        part,
                    )
                )
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def _replace_docx_paragraph(paragraph: _Element, text: str) -> None:
    text_nodes = [element for element in paragraph.iter(f"{{{WORD_NS}}}t")]
    if not text_nodes:
        run = etree.SubElement(paragraph, f"{{{WORD_NS}}}r")
        node = etree.SubElement(run, f"{{{WORD_NS}}}t")
        node.text = text
        node.set(XML_SPACE, "preserve")
        return
    text_nodes[0].text = text
    text_nodes[0].set(XML_SPACE, "preserve")
    for node in text_nodes[1:]:
        node.text = ""


def _replace_pptx_paragraph(paragraph: _Element, text: str) -> None:
    text_nodes = [element for element in paragraph.iter(f"{{{DRAWING_NS}}}t")]
    if not text_nodes:
        run = etree.SubElement(paragraph, f"{{{DRAWING_NS}}}r")
        node = etree.SubElement(run, f"{{{DRAWING_NS}}}t")
        node.text = text
        return
    text_nodes[0].text = text
    for node in text_nodes[1:]:
        node.text = ""


def _validate_written_package(
    path: Path,
    file_format: str,
    options: OfficeExportOptions,
) -> None:
    if options.validation_mode.value == "off":
        return
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = _preflight_zip(zf, options)
            actual = _detect_ooxml_format(zf, names)
            if actual != file_format:
                raise OfficeValidationError(f"Output package is not {file_format.upper()}")
    except zipfile.BadZipFile as exc:
        raise OfficeValidationError("Output Office package is not a valid ZIP archive") from exc


def _selected_document(
    document: BaseStructure | StreamingStructure,
    target_locale: str | None,
) -> BaseStructure | StreamingStructure:
    if isinstance(document, BaseStructure) and target_locale and target_locale in document.target_locales:
        return select_target(document, target_locale)
    return document


def _as_base_structure(document: BaseStructure | StreamingStructure) -> BaseStructure:
    if isinstance(document, BaseStructure):
        return document
    return BaseStructure(
        source_locale=document.source_locale,
        target_locale=document.target_locale,
        data=dict(document.items),
        target_locales=document.target_locales,
        source_language=document.source_language,
        target_language=document.target_language,
        target_languages=document.target_languages,
        extensions=document.extensions,
    )


def _source_document_from_extensions(document: BaseStructure | StreamingStructure) -> str | None:
    return document.extensions.get("source_file")


def _document_extensions(file_format: str, fingerprint: str, source_file: str) -> dict[str, str]:
    runtime = load_runtime_info()
    extensions = {
        "input_format": file_format,
        "office.worker_version": runtime.worker_version,
        "office.protocol_version": f"{runtime.protocol_major}.{runtime.protocol_minor}",
    }
    if fingerprint:
        extensions["office.source_fingerprint"] = fingerprint
    if source_file:
        extensions["source_file"] = source_file
    if runtime.openxml_sdk_version:
        extensions["office.openxml_sdk_version"] = runtime.openxml_sdk_version
    return extensions


def _use_worker() -> bool:
    return os.environ.get("LOKIT_OFFICE_BACKEND", "").lower() != "python" and worker_available()


def _parse_xml(data: bytes, part: str) -> _Element:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=False)
    try:
        return etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise OfficePackageError(f"Malformed XML in Office part {part}") from exc


def _sha256_file(path: Path, limit: int) -> str:
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            read += len(chunk)
            if read > limit:
                raise OfficePackageError("Office source exceeds max_compressed_bytes")
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _base_language(locale: str | None) -> str | None:
    if not locale:
        return None
    return locale.replace("_", "-").split("-")[0].lower()


def _local_name(tag: str | bytes) -> str:
    name = tag if isinstance(tag, str) else tag.decode("utf-8")
    if "}" in name:
        return name.split("}", 1)[1]
    return name
