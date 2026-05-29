from __future__ import annotations

import asyncio
import zipfile
from dataclasses import dataclass
from typing import AsyncIterator, Iterator, Optional

from lxml import etree
from lxml.etree import _Element

from lokit.data.structure import CodePart, Data, Meta, Tags, TextPart, TranslationStatus
from lokit.data.tag_types import TieData, TieType

ExtractItem = tuple[str, Data]

IDML_NS = "http://ns.adobe.com/AdobeInDesign/idms/1.0/"
IDML_NSMAP: dict[str, str] = {"idPkg": IDML_NS}


@dataclass(slots=True)
class _AsyncResult:
    item: Optional[ExtractItem] = None
    error: Optional[BaseException] = None
    done: bool = False


class _AsyncIdmlExtraction:
    def __init__(self, extractor: IdmlExtractor) -> None:
        self._extractor = extractor
        self._queue: asyncio.Queue[_AsyncResult] = asyncio.Queue()
        self._producer: asyncio.Task[None] | None = None

    def __aiter__(self) -> _AsyncIdmlExtraction:
        return self

    async def __anext__(self) -> ExtractItem:
        if self._producer is None:
            self._start()
        result = await self._queue.get()
        if result.done:
            await self._finish()
            raise StopAsyncIteration
        if result.error is not None:
            await self._finish()
            raise result.error
        if result.item is None:
            await self._finish()
            raise StopAsyncIteration
        return result.item

    def _start(self) -> None:
        loop = asyncio.get_running_loop()

        def produce() -> None:
            try:
                for item in self._extractor.extract():
                    loop.call_soon_threadsafe(
                        self._queue.put_nowait,
                        _AsyncResult(item=item),
                    )
            except BaseException as exc:
                loop.call_soon_threadsafe(
                    self._queue.put_nowait,
                    _AsyncResult(error=exc),
                )
            finally:
                loop.call_soon_threadsafe(
                    self._queue.put_nowait,
                    _AsyncResult(done=True),
                )

        self._producer = asyncio.create_task(asyncio.to_thread(produce))

    async def _finish(self) -> None:
        if self._producer is not None:
            await self._producer


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
        self.extensions: dict[str, str] = {"input_format": "idml"}

    def extract(self) -> Iterator[ExtractItem]:
        if self.source_locale and self.source_language is None:
            self.source_language = self._base_language(self.source_locale)
        if self.target_locale and self.target_language is None:
            self.target_language = self._base_language(self.target_locale)

        with zipfile.ZipFile(self.filepath, "r") as zf:
            story_files = sorted(
                name for name in zf.namelist()
                if name.startswith("Stories/Story_") and name.endswith(".xml")
            )
            for story_file in story_files:
                story_name = _story_name(story_file)
                with zf.open(story_file) as stream:
                    tree = etree.parse(stream)
                    root = tree.getroot()
                    yield from self._extract_story(root, story_name, story_file)

    def extract_async(self) -> AsyncIterator[ExtractItem]:
        return _AsyncIdmlExtraction(self)

    def _extract_story(
        self,
        root: _Element,
        story_name: str,
        story_file: str,
    ) -> Iterator[ExtractItem]:
        paragraph_index = 0
        for psr in root.iter():
            if _local_name(psr.tag) != "ParagraphStyleRange":
                continue

            result = self._extract_paragraph(psr, story_name, story_file, paragraph_index)
            if result is not None:
                yield result
                paragraph_index += 1

    def _extract_paragraph(
        self,
        psr: _Element,
        story_name: str,
        story_file: str,
        paragraph_index: int,
    ) -> ExtractItem | None:
        char_ranges: list[_Element] = [
            el for el in psr
            if _local_name(el.tag) == "CharacterStyleRange"
        ]

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

        return self._extract_styled_paragraph(
            char_ranges, story_name, story_file, paragraph_index
        )

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


def _local_name(tag: str | bytes) -> str:
    name = tag if isinstance(tag, str) else tag.decode("utf-8")
    if "}" in name:
        return name.split("}", 1)[1]
    return name


def _story_name(story_file: str) -> str:
    name = story_file
    if name.startswith("Stories/"):
        name = name[len("Stories/"):]
    if name.endswith(".xml"):
        name = name[: -len(".xml")]
    return name


def _collect_content_text(element: _Element) -> str:
    parts: list[str] = []
    for child in element.iter():
        if _local_name(child.tag) == "Content" and child.text:
            parts.append(child.text)
        if _local_name(child.tag) == "Br":
            parts.append("\n")
    return "".join(parts)
