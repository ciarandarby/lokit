from __future__ import annotations

import stat
import zipfile
from pathlib import Path
from typing import IO, TYPE_CHECKING, cast

from lokit.data.structure import CodePart, Data, Meta, Tags, TextPart, TranslationStatus
from lokit.data.tag_types import TieData, TieType
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.projection import project_items
from lokit.parsers.tmx.xml_utils import clear_element, is_tag, iterparse_safe
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence

    from lxml.etree import _Element

    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]

IDML_NS = "http://ns.adobe.com/AdobeInDesign/idms/1.0/"
IDML_NSMAP: dict[str, str] = {"idPkg": IDML_NS}
_MAX_ZIP_ENTRIES = 100_000
_MAX_COMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
_MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
_MAX_STORY_BYTES = 512 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 1000.0
_MAX_MEMBER_NAME_BYTES = 4096


class _BoundedStoryReader:
    def __init__(self, stream: IO[bytes], limit: int, label: str) -> None:
        self._stream = stream
        self._limit = limit
        self._label = label
        self._bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        remaining_with_probe = self._limit - self._bytes_read + 1
        requested = remaining_with_probe if size < 0 else min(size, remaining_with_probe)
        data = self._stream.read(max(0, requested))
        self._bytes_read += len(data)
        if self._bytes_read > self._limit:
            raise ValueError(f"IDML Story exceeds its decompression limit: {self._label}")
        return data


def _preflight_package(path: Path, archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    if path.stat().st_size > _MAX_COMPRESSED_BYTES:
        raise ValueError("IDML package exceeds its compressed size limit")
    infos = archive.infolist()
    if len(infos) > _MAX_ZIP_ENTRIES:
        raise ValueError(f"IDML package has more than {_MAX_ZIP_ENTRIES} ZIP entries")

    names: set[str] = set()
    stories: list[zipfile.ZipInfo] = []
    compressed_bytes = 0
    uncompressed_bytes = 0
    for info in infos:
        _validate_member_name(info.filename)
        if info.filename in names:
            raise ValueError(f"duplicate IDML ZIP entry: {info.filename}")
        names.add(info.filename)
        if info.flag_bits & 0x1:
            raise ValueError("encrypted IDML ZIP entries are not supported")
        if (info.external_attr >> 16) & 0o170000 == stat.S_IFLNK:
            raise ValueError(f"symbolic-link IDML ZIP entry is not supported: {info.filename}")
        if info.file_size > _MAX_MEMBER_BYTES:
            raise ValueError(f"IDML ZIP entry exceeds its size limit: {info.filename}")
        if _is_story(info.filename):
            if info.file_size > _MAX_STORY_BYTES:
                raise ValueError(f"IDML Story exceeds its decompression limit: {info.filename}")
            stories.append(info)
        compressed_bytes += info.compress_size
        uncompressed_bytes += info.file_size
        if compressed_bytes > _MAX_COMPRESSED_BYTES:
            raise ValueError("IDML package exceeds its compressed size limit")
        if uncompressed_bytes > _MAX_UNCOMPRESSED_BYTES:
            raise ValueError("IDML package exceeds its decompression limit")
        if info.file_size and (info.compress_size == 0 or info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO):
            raise ValueError(f"suspicious compression ratio in IDML ZIP entry: {info.filename}")
    return tuple(sorted(stories, key=lambda info: info.filename))


def _validate_member_name(name: str) -> None:
    encoded_size = len(name.encode("utf-8"))
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    if (
        not trimmed
        or encoded_size > _MAX_MEMBER_NAME_BYTES
        or name.startswith("/")
        or "\\" in name
        or "\0" in name
        or (len(name) >= 2 and name[1] == ":")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"unsafe IDML ZIP entry: {name!r}")


def _is_story(name: str) -> bool:
    return name.startswith("Stories/Story_") and name.endswith(".xml")


class IdmlExtractor:
    def __init__(
        self,
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
    ) -> None:
        self.filepath = filepath
        self.source_locale = source_locale
        self.target_locale = target_locale
        self.source_language: str | None = None
        self.target_language: str | None = None
        self.export_origin = ""
        self.export_timestamp = ""
        self.extensions: dict[str, str] = {
            "input_format": "idml",
            "source_file": filepath,
            "source_idml": filepath,
        }

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
        return project_items(
            self._extract(),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.IDML,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )

    def _extract(self) -> Iterator[ExtractItem]:
        if self.source_locale and self.source_language is None:
            self.source_language = self._base_language(self.source_locale)
        if self.target_locale and self.target_language is None:
            self.target_language = self._base_language(self.target_locale)

        path = Path(self.filepath)
        with zipfile.ZipFile(path, "r") as zf:
            story_infos = _preflight_package(path, zf)
            for info in story_infos:
                story_name = _story_name(info.filename)
                with zf.open(info) as stream:
                    bounded = _BoundedStoryReader(stream, min(info.file_size, _MAX_STORY_BYTES), info.filename)
                    context = iterparse_safe(
                        cast("IO[bytes]", bounded),
                        events=("end",),
                        tag="{*}ParagraphStyleRange",
                    )
                    paragraph_index = 0
                    for processed_paragraphs, (_, paragraph) in enumerate(context, start=1):
                        result = self._extract_paragraph(
                            paragraph,
                            story_name,
                            info.filename,
                            paragraph_index,
                        )
                        if result is not None:
                            yield result
                            paragraph_index += 1
                        if processed_paragraphs % 256 == 0:
                            clear_element(paragraph)
                        else:
                            paragraph.clear()

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
            )
        )

    def _extract_paragraph(
        self,
        psr: _Element,
        story_name: str,
        story_file: str,
        paragraph_index: int,
    ) -> ExtractItem | None:
        char_ranges: list[_Element] = [el for el in psr if is_tag(el, "CharacterStyleRange")]

        if not char_ranges:
            return None

        if len(char_ranges) == 1:
            text = _collect_content_text(char_ranges[0])
            if not text.strip():
                return None
            unit_id = f"{story_name}:p{paragraph_index}"
            return unit_id, Data(
                source=text.strip(),
                meta=Meta(),
                status=TranslationStatus.UNKNOWN,
                extensions={"story": story_file, "input_format": "idml"},
            )

        return self._extract_styled_paragraph(char_ranges, story_name, story_file, paragraph_index)

    def _extract_styled_paragraph(
        self,
        char_ranges: list[_Element],
        story_name: str,
        story_file: str,
        paragraph_index: int,
    ) -> ExtractItem | None:
        parts: list[TextPart | CodePart] = []
        tag_map: dict[str, TieData] = {}
        full_text_parts: list[str] = []
        tag_order = 0
        pair_counter = 0

        for csr in char_ranges:
            style = csr.get("AppliedCharacterStyle") or ""
            text = _collect_content_text(csr)

            if not text:
                continue

            if style and style != "CharacterStyle/$ID/[No character style]":
                pair_id = f"pair{pair_counter}"
                pair_counter += 1

                open_id = f"t{tag_order}"
                tag_map[open_id] = TieData(
                    id=open_id,
                    type=TieType.CUSTOM_OPEN,
                    attributes={"style": style},
                    position=tag_order,
                    order=tag_order,
                    pair_id=pair_id,
                    original_name="CharacterStyleRange",
                )
                parts.append(CodePart(ref=open_id))
                tag_order += 1

                parts.append(TextPart(value=text))
                full_text_parts.append(text)

                close_id = f"t{tag_order}"
                tag_map[close_id] = TieData(
                    id=close_id,
                    type=TieType.CUSTOM_CLOSE,
                    position=tag_order,
                    order=tag_order,
                    pair_id=pair_id,
                    original_name="CharacterStyleRange",
                )
                parts.append(CodePart(ref=close_id))
                tag_order += 1
            else:
                parts.append(TextPart(value=text))
                full_text_parts.append(text)

        full_text = "".join(full_text_parts)
        if not full_text.strip():
            return None

        unit_id = f"{story_name}:p{paragraph_index}"
        tags = Tags(
            source_tag_map=tag_map,
            target_tag_map={},
            source_parts=parts,
            target_parts=[],
        )
        return unit_id, Data(
            source=full_text.strip(),
            tags=tags if tag_map else None,
            meta=Meta(),
            status=TranslationStatus.UNKNOWN,
            extensions={"story": story_file, "input_format": "idml"},
        )

    def _base_language(self, locale: str) -> str:
        return locale.replace("_", "-").split("-")[0].lower()


def _story_name(story_file: str) -> str:
    name = story_file
    if name.startswith("Stories/"):
        name = name[len("Stories/") :]
    if name.endswith(".xml"):
        name = name[: -len(".xml")]
    return name


def _collect_content_text(element: _Element) -> str:
    parts: list[str] = []
    for child in element.iter("{*}Content", "{*}Br"):
        if is_tag(child, "Content") and child.text:
            parts.append(child.text)
        elif is_tag(child, "Br"):
            parts.append("\n")
    return "".join(parts)
