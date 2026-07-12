from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

from lokit.data.structure import CodePart, Data, Meta, Tags, TextPart, TranslationStatus
from lokit.data.tag_types import TieData, TieType
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.projection import project_items
from lokit.parsers.tmx.xml_utils import clear_element, is_tag, iterparse_safe
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from lxml.etree import _Element

ExtractItem = tuple[str, Data]

IDML_NS = "http://ns.adobe.com/AdobeInDesign/idms/1.0/"
IDML_NSMAP: dict[str, str] = {"idPkg": IDML_NS}


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
    ) -> Iterator[ExtractItem]:
        return project_items(
            self._extract(),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.IDML,
            unsupported_tags=unsupported_tags,
        )

    def _extract(self) -> Iterator[ExtractItem]:
        if self.source_locale and self.source_language is None:
            self.source_language = self._base_language(self.source_locale)
        if self.target_locale and self.target_language is None:
            self.target_language = self._base_language(self.target_locale)

        with zipfile.ZipFile(self.filepath, "r") as zf:
            story_files = sorted(
                name for name in zf.namelist() if name.startswith("Stories/Story_") and name.endswith(".xml")
            )
            for story_file in story_files:
                story_name = _story_name(story_file)
                with zf.open(story_file) as stream:
                    context = iterparse_safe(
                        stream,
                        events=("end",),
                        tag="{*}ParagraphStyleRange",
                    )
                    paragraph_index = 0
                    for processed_paragraphs, (_, paragraph) in enumerate(context, start=1):
                        result = self._extract_paragraph(
                            paragraph,
                            story_name,
                            story_file,
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
    ) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(
            lambda: self.extract(
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
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
