from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from xml.etree import ElementTree

from lxml import etree

from lokit.data.structure import (
    Comment,
    Data,
    Meta,
    Plural,
    SegmentPart,
    Tags,
    TargetData,
    TargetTags,
    TranslationStatus,
)
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.interchange import iter_native_records, open_native_reader
from lokit.parsers.projection import project_items
from lokit.parsers.tmx.xml_utils import (
    element_children,
    find_child,
    local_name,
)
from lokit.parsers.xliff.tags import XliffTagParser
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence

    from lxml.etree import _Element

    from lokit.data.tag_types import TieData
    from lokit.parsers.interchange import NativeReader, NativeRecord
    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]
_ASYNC_BATCH_SIZE = 64
_NATIVE_UNIT_NOTE_PREFIX = "__lokit_native_xliff_unit_note."
_NATIVE_STATUSES = {status.value: status for status in TranslationStatus}


@dataclass(slots=True)
class XliffFileContext:
    index: int
    original: str
    source_locale: str
    target_locale: str | None
    data_type: str
    tool_name: str | None = None
    tool_version: str | None = None


class XliffExtractor:
    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        self.version = "1.2"
        self.source_locale: str | None = None
        self.target_locale: str | None = None
        self.source_language: str | None = None
        self.target_language: str | None = None
        self.target_locales: tuple[str, ...] = ()
        self.target_languages: tuple[str, ...] = ()
        self.export_origin = ""
        self.export_timestamp = ""
        self.extensions: dict[str, str] = {"input_format": "xliff"}
        self.tag_parser = XliffTagParser()
        self._initialized = False
        self._native_reader: NativeReader | None = None
        self._gettext_plural_roots: dict[str, tuple[str, Plural, dict[str, str]]] = {}

    def extract(
        self,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> Iterator[ExtractItem]:
        self._initialize_from_file()
        return project_items(
            self._extract(),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=self._native_syntax(),
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )

    def _extract(self) -> Iterator[ExtractItem]:
        native_reader = self._ensure_native_reader()
        self._sync_native_metadata(native_reader)
        try:
            for record in iter_native_records(native_reader):
                yield self._native_record(record)
        finally:
            self._sync_native_metadata(native_reader)

    def _initialize_from_file(self) -> None:
        if self._initialized:
            return
        native_reader = self._ensure_native_reader()
        self._sync_native_metadata(native_reader)
        self._read_po_header_extensions()
        self._initialized = True

    def _ensure_native_reader(self) -> NativeReader:
        reader = self._native_reader
        if reader is not None and not reader.closed:
            return reader
        reader = open_native_reader(self.filepath, "xliff")
        self._native_reader = reader
        return reader

    def close(self) -> None:
        reader = self._native_reader
        if reader is not None and not reader.closed:
            reader.close()

    def _sync_native_metadata(self, reader: NativeReader) -> None:
        self.version = reader.version
        self.extensions["xliff_version"] = reader.version
        self.source_locale = reader.source_locale
        self.target_locale = reader.target_locale
        self.source_language = reader.source_language
        self.target_language = reader.target_language
        self.target_locales = tuple(reader.target_locales)
        self.target_languages = tuple(reader.target_languages)
        self.export_origin = reader.export_origin
        self.export_timestamp = reader.export_timestamp
        self.extensions.update(reader.extensions)

    def _read_po_header_extensions(self) -> None:
        aliases = {
            "x-po-metadata-json": "po_metadata_json",
            "x-po-header-translator-comments": "po_header_translator_comments",
            "x-po-header-extracted-comments": "po_header_extracted_comments",
            "x-po-header-flags": "po_header_flags",
            "x-po-header-previous": "po_header_previous",
        }
        with open(self.filepath, "rb") as source:
            for _, element in ElementTree.iterparse(source, events=("end",)):
                name = element.tag.rsplit("}", maxsplit=1)[-1]
                if name == "header":
                    for descendant in element.iter():
                        if descendant.tag.rsplit("}", maxsplit=1)[-1] != "prop":
                            continue
                        key = aliases.get(descendant.attrib.get("prop-type", ""))
                        if key is not None:
                            self.extensions[key] = descendant.text or ""
                    return
                if name in ("trans-unit", "unit", "segment"):
                    return

    def _native_record(self, record: NativeRecord) -> ExtractItem:
        is_complex, unit_id, source, target, raw_targets, raw_status, extensions, fragment = record
        comments = self._native_unit_comments(extensions)
        if is_complex:
            if fragment is None:
                raise ValueError("Native XLIFF parser returned a complex unit without XML")
            parser = etree.XMLParser(no_network=True, resolve_entities=False)
            element = etree.fromstring(fragment, parser)
            expected_name = "trans-unit" if self.version.startswith("1") else "segment"
            if local_name(element.tag) != expected_name:
                unit_element = next(
                    (child for child in element if local_name(child.tag) == expected_name),
                    None,
                )
                if unit_element is None:
                    raise ValueError("Native XLIFF parser returned an invalid unit fragment")
                element = unit_element
            file_context = XliffFileContext(
                index=int(extensions.get("resource_index", "0")),
                original=extensions.get("resource", ""),
                source_locale=self.source_locale or "",
                target_locale=raw_targets[0][0] if raw_targets else self.target_locale,
                data_type=extensions.get("data_type", ""),
            )
            if self.version.startswith("1"):
                _, data = self._parse_unit(element, file_context)
            else:
                _, data = self._parse_v2_segment(element, file_context)
                data.comments.extend(comments)
                data.extensions.update(extensions)
            return unit_id, data

        status = self._native_status(raw_status)
        targets = {
            locale: TargetData(
                text=text,
                status=status,
            )
            for locale, text in raw_targets
        }
        return unit_id, Data(
            source=source,
            target=target,
            targets=targets,
            plural=self._native_gettext_plural(unit_id, source, extensions),
            meta=Meta(),
            status=status,
            comments=comments,
            extensions=extensions,
        )

    def _native_unit_comments(self, extensions: dict[str, str]) -> list[Comment]:
        keys = sorted(
            (key for key in extensions if key.startswith(_NATIVE_UNIT_NOTE_PREFIX)),
            key=lambda key: int(key.removeprefix(_NATIVE_UNIT_NOTE_PREFIX)),
        )
        return [Comment(context=extensions.pop(key)) for key in keys]

    def _native_status(self, value: str) -> TranslationStatus:
        return _NATIVE_STATUSES.get(value, TranslationStatus.UNKNOWN)

    def extract_async(
        self,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(
            lambda: self.extract(
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            ),
            batch_size=_ASYNC_BATCH_SIZE,
        )

    def _file_context(self, element: _Element, index: int) -> XliffFileContext:
        original = element.attrib.get("original") or element.attrib.get("id") or ""
        source_locale = element.attrib.get("source-language") or self.source_locale or ""
        target_locale = element.attrib.get("target-language") or self.target_locale
        data_type = element.attrib.get("datatype", "")
        return XliffFileContext(
            index=index,
            original=original,
            source_locale=source_locale,
            target_locale=target_locale,
            data_type=data_type,
        )

    def _set_document_languages(self, context: XliffFileContext) -> None:
        if self.source_locale is None and context.source_locale:
            self.source_locale = context.source_locale
            self.source_language = self._base_language(context.source_locale)
        if self.target_locale is None and context.target_locale:
            self.target_locale = context.target_locale
            self.target_language = self._base_language(context.target_locale)
        if context.target_locale and context.target_locale not in self.target_locales:
            self.target_locales = (*self.target_locales, context.target_locale)
            self.target_languages = (*self.target_languages, self._base_language(context.target_locale))
        if len(self.target_locales) > 1:
            self.target_locale = None
            self.target_language = None

    def _parse_unit(
        self,
        element: _Element,
        file_context: XliffFileContext,
    ) -> ExtractItem:
        source = find_child(element, "source")
        target = find_child(element, "target")
        source_text, source_tags, source_parts = self._parse_segment(source)
        target_text, target_tags, target_parts = self._parse_segment(target)
        status = self._status(target)
        unit_id = element.attrib.get("id", "")
        stable_id = unit_id or f"{file_context.index}"
        tags: Tags | None = None
        if source_tags or target_tags:
            tags = Tags(
                source_tag_map=source_tags,
                target_tag_map=target_tags,
                source_parts=source_parts,
                target_parts=target_parts,
            )
        targets: dict[str, TargetData] = {}
        if file_context.target_locale is not None and target is not None:
            targets[file_context.target_locale] = TargetData(
                text=target_text,
                status=status,
                tags=TargetTags(tag_map=target_tags, parts=target_parts) if target_tags or target_parts else None,
            )
        extensions = self._extensions(element, file_context, unit_id)
        if self._has_fuzzy_flag(extensions):
            status = TranslationStatus.DRAFT
        plural = self._gettext_plural(element, extensions)
        data = Data(
            source=source_text,
            target=None if targets else (target_text if target is not None else None),
            targets=targets,
            plural=plural,
            tags=tags,
            meta=Meta(),
            status=status,
            comments=self._comments(element),
            extensions=extensions,
        )
        return stable_id, data

    def _parse_v2_segment(
        self,
        element: _Element,
        file_context: XliffFileContext,
    ) -> ExtractItem:
        source = find_child(element, "source")
        target = find_child(element, "target")
        source_text, source_tags, source_parts = self._parse_segment(source)
        target_text, target_tags, target_parts = self._parse_segment(target)
        status = self._v2_status(element)
        unit = element.getparent()
        while unit is not None and local_name(unit.tag) != "unit":
            unit = unit.getparent()
        unit_id = unit.attrib.get("id", "") if unit is not None else ""
        segment_id = element.attrib.get("id", "")
        stable_id = unit_id
        if segment_id:
            stable_id = f"{unit_id}:{segment_id}" if unit_id else segment_id
        if not stable_id:
            stable_id = f"{file_context.index}"
        tags: Tags | None = None
        if source_tags or target_tags:
            tags = Tags(
                source_tag_map=source_tags,
                target_tag_map=target_tags,
                source_parts=source_parts,
                target_parts=target_parts,
            )
        targets: dict[str, TargetData] = {}
        if file_context.target_locale is not None and target is not None:
            targets[file_context.target_locale] = TargetData(
                text=target_text,
                status=status,
                tags=TargetTags(tag_map=target_tags, parts=target_parts) if target_tags or target_parts else None,
            )
        extensions = self._extensions(element, file_context, unit_id)
        if segment_id:
            extensions["segment_id"] = segment_id
        extensions["xliff_version"] = self.version
        return stable_id, Data(
            source=source_text,
            target=None if targets else (target_text if target is not None else None),
            targets=targets,
            tags=tags,
            meta=Meta(),
            status=status,
            comments=self._comments(unit if unit is not None else element),
            extensions=extensions,
        )

    def _v2_status(self, element: _Element) -> TranslationStatus:
        state = (element.attrib.get("state") or "initial").lower()
        return {
            "final": TranslationStatus.APPROVED,
            "reviewed": TranslationStatus.REVIEWED,
            "translated": TranslationStatus.TRANSLATED,
            "initial": TranslationStatus.NEW,
        }.get(state, TranslationStatus.UNKNOWN)

    def _parse_segment(self, element: _Element | None) -> tuple[str, dict[str, TieData], list[SegmentPart]]:
        if element is None:
            return "", {}, []
        return self.tag_parser.parse_fast(element)

    def _status(self, target: _Element | None) -> TranslationStatus:
        if target is None:
            return TranslationStatus.NEW
        state = (target.attrib.get("state") or "").lower()
        if state in ("final", "signed-off"):
            return TranslationStatus.APPROVED
        if state in ("translated", "needs-review-translation"):
            return TranslationStatus.TRANSLATED
        if state in ("needs-review-adaptation", "needs-review-l10n"):
            return TranslationStatus.REVIEWED
        if state in ("new", "needs-translation"):
            return TranslationStatus.NEW
        return TranslationStatus.UNKNOWN

    def _comments(self, element: _Element) -> list[Comment]:
        comments: list[Comment] = []
        for child in element_children(element, "note"):
            kind = "translator" if child.attrib.get("from") == "po-translator" else "extracted"
            comments.append(
                Comment(
                    context=(child.text or "").strip(),
                    extensions={"po_comment_kind": kind},
                )
            )
        parent = element.getparent()
        if (
            parent is not None
            and local_name(parent.tag) == "group"
            and parent.attrib.get("restype") == "x-gettext-plurals"
            and element.attrib.get("id", "").endswith("[0]")
        ):
            for child in parent:
                if local_name(child.tag) == "context-group":
                    for context in element_children(child, "context"):
                        if context.text and context.attrib.get("context-type") == "x-po-autocomment":
                            comments.append(
                                Comment(
                                    context=context.text.strip(),
                                    extensions={"po_comment_kind": "extracted"},
                                )
                            )
                elif local_name(child.tag) == "note" and child.text:
                    comments.append(
                        Comment(
                            context=child.text.strip(),
                            extensions={"po_comment_kind": "extracted"},
                        )
                    )
        return comments

    def _extensions(
        self,
        element: _Element,
        file_context: XliffFileContext,
        unit_id: str,
    ) -> dict[str, str]:
        extensions = {
            "resource": file_context.original,
            "resource_index": str(file_context.index),
            "unit_id": unit_id,
        }
        if file_context.data_type:
            extensions["data_type"] = file_context.data_type
        xml_space = element.attrib.get("{http://www.w3.org/XML/1998/namespace}space")
        if xml_space:
            extensions["space"] = xml_space
        restype = element.attrib.get("restype")
        if restype:
            extensions["xliff_restype"] = restype
        context_keys = {
            "x-po-msgid": "po_msgid",
            "x-po-msgctxt": "po_msgctxt",
            "x-po-msgid-plural": "po_msgid_plural",
            "x-po-plural-index": "gettext_index",
            "x-po-entry-index": "po_entry_index",
            "x-po-flags": "flags",
            "x-po-references": "references",
            "x-po-previous": "po_previous",
        }
        for descendant in element.iter():
            if local_name(descendant.tag) != "context" or descendant.text is None:
                continue
            key = context_keys.get(descendant.attrib.get("context-type", ""))
            if key is not None:
                if key == "po_previous" and key in extensions:
                    extensions[key] = f"{extensions[key]}\n{descendant.text}"
                else:
                    extensions[key] = descendant.text
        return extensions

    def _gettext_plural(self, element: _Element, extensions: dict[str, str]) -> Plural | None:
        parent = element.getparent()
        grouped = (
            parent is not None
            and local_name(parent.tag) == "group"
            and parent.attrib.get("restype") == "x-gettext-plurals"
        )
        unit_id = element.attrib.get("id", "")
        suffix_match = unit_id.rsplit("[", maxsplit=1)
        suffix = suffix_match[-1] if len(suffix_match) == 2 else ""
        indexed = suffix.endswith("]") and suffix[:-1].isdigit()
        if not grouped and not indexed:
            return None
        index = suffix[:-1] if suffix.endswith("]") and suffix[:-1].isdigit() else "0"
        base_id = parent.attrib.get("id", unit_id) if grouped and parent is not None else suffix_match[0]
        source = find_child(element, "source")
        source_value = (
            "".join(value.decode("utf-8") if isinstance(value, bytes) else value for value in source.itertext())
            if source is not None
            else ""
        )
        root = self._gettext_plural_roots.get(base_id)
        if index == "0" or root is None:
            msgid = extensions.get("po_msgid", source_value)
            variant = extensions.get("po_msgid_plural", "" if index == "0" else source_value)
            plural = Plural(variant=variant, extensions={"gettext_index": index})
            self._gettext_plural_roots[base_id] = (msgid, plural, extensions)
        else:
            msgid, root_plural, root_extensions = root
            variant = extensions.get("po_msgid_plural", source_value)
            if not root_plural.variant:
                root_plural.variant = variant
                root_extensions["po_msgid_plural"] = variant
            plural = Plural(variant=variant, extensions={"gettext_index": index})
        extensions.setdefault("po_msgid", msgid)
        if variant:
            extensions.setdefault("po_msgid_plural", variant)
        extensions.setdefault("gettext_index", index)
        extensions.setdefault("po_entry_index", base_id)
        return plural

    def _native_gettext_plural(
        self,
        unit_id: str,
        source: str,
        extensions: dict[str, str],
    ) -> Plural | None:
        suffix_match = unit_id.rsplit("[", maxsplit=1)
        if len(suffix_match) != 2 or not suffix_match[1].endswith("]"):
            return None
        index = suffix_match[1][:-1]
        if not index.isdigit():
            return None
        base_id = suffix_match[0]
        root = self._gettext_plural_roots.get(base_id)
        if index == "0" or root is None:
            msgid = source
            variant = "" if index == "0" else source
            plural = Plural(variant=variant, extensions={"gettext_index": index})
            self._gettext_plural_roots[base_id] = (msgid, plural, extensions)
        else:
            msgid, root_plural, root_extensions = root
            variant = source
            if not root_plural.variant:
                root_plural.variant = variant
                root_extensions["po_msgid_plural"] = variant
            plural = Plural(variant=variant, extensions={"gettext_index": index})
        extensions.setdefault("po_msgid", msgid)
        if variant:
            extensions.setdefault("po_msgid_plural", variant)
        extensions.setdefault("gettext_index", index)
        extensions.setdefault("po_entry_index", base_id)
        return plural

    def _has_fuzzy_flag(self, extensions: dict[str, str]) -> bool:
        return any(flag.strip() == "fuzzy" for flag in extensions.get("flags", "").split(","))

    def _base_language(self, locale: str) -> str:
        return locale.replace("_", "-").split("-")[0].lower()

    def _set_root_languages(self, element: _Element) -> None:
        source_locale = element.attrib.get("srcLang")
        target_locale = element.attrib.get("trgLang")
        if source_locale:
            self.source_locale = source_locale
            self.source_language = self._base_language(source_locale)
        if target_locale:
            self.target_locale = target_locale
            self.target_language = self._base_language(target_locale)
            self.target_locales = (target_locale,)
            self.target_languages = (self._base_language(target_locale),)

    def _native_syntax(self) -> TagSyntax:
        if self.version.startswith("2.1"):
            return TagSyntax.XLIFF_21
        if self.version.startswith("2"):
            return TagSyntax.XLIFF_20
        return TagSyntax.XLIFF_12
