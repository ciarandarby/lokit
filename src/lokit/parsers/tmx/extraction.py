from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Iterator, Optional
from uuid import uuid4

from lxml.etree import _Element

from lokit.data.structure import Data, SegmentPart, Tags
from lokit.data.tag_types import TieData
from lokit.parsers.tmx.base import TmxParser
from lokit.parsers.tmx.props import TmxProps
from lokit.parsers.tmx.tags import TmxTagParser
from lokit.parsers.tmx.xml_utils import (
    clear_element,
    element_children,
    find_child,
    iterparse_safe,
    local_name,
)

ExtractItem = tuple[str, Data]


@dataclass(slots=True)
class _AsyncExtractionResult:
    item: Optional[ExtractItem] = None
    error: Optional[BaseException] = None
    done: bool = False


class AsyncTmxExtraction:
    def __init__(self, extractor: TmxExtractor) -> None:
        self._extractor = extractor
        self._queue: asyncio.Queue[_AsyncExtractionResult] = asyncio.Queue()
        self._producer: asyncio.Task[None] | None = None

    def __aiter__(self) -> AsyncTmxExtraction:
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
                        _AsyncExtractionResult(item=item),
                    )
            except BaseException as exc:
                loop.call_soon_threadsafe(
                    self._queue.put_nowait,
                    _AsyncExtractionResult(error=exc),
                )
            finally:
                loop.call_soon_threadsafe(
                    self._queue.put_nowait,
                    _AsyncExtractionResult(done=True),
                )

        self._producer = asyncio.create_task(asyncio.to_thread(produce))

    async def _finish(self) -> None:
        if self._producer is not None:
            await self._producer


class TmxExtractor(TmxParser):
    def __init__(
        self,
        filepath: str,
        source_language: Optional[str] = None,
        target_language: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> None:
        super().__init__(
            tmx_file_path=filepath,
            source_language=source_language,
            target_language=target_language,
            domain=domain,
        )
        self.tag_parser: TmxTagParser = TmxTagParser()
        self.prop_parser: TmxProps = TmxProps()
        self.namespace: str = "{http://www.w3.org/XML/1998/namespace}"

    def extract(self) -> Iterator[tuple[str, Data]]:
        with open(self.filepath, "rb") as stream:
            context = iterparse_safe(stream, events=("end",))

            for _, elem in context:
                if local_name(elem.tag) != "tu":
                    continue

                unit_id: str = elem.attrib.get("tuid") or str(uuid4())

                meta = self.prop_parser.parse_meta(elem)
                comments = self.prop_parser.parse_comments(elem)
                prev_ctx, next_ctx = self.prop_parser.parse_adjacent_context(elem)
                status = self.prop_parser.parse_status(elem)
                extensions = self.prop_parser.parse_extensions(elem)

                source_text: str = ""
                target_text: str = ""
                source_tags: dict[str, TieData] = {}
                target_tags: dict[str, TieData] = {}
                source_parts: list[SegmentPart] = []
                target_parts: list[SegmentPart] = []

                for tuv in element_children(elem, "tuv"):
                    lang: str = (
                        tuv.get(f"{self.namespace}lang") or tuv.get("lang") or ""
                    )
                    seg: _Element | None = find_child(tuv, "seg")

                    if seg is not None:
                        text, tags, parts = self.tag_parser.parse(seg)

                        if self._compare_base_lang(lang, self.native_source):
                            source_text = text
                            source_tags = tags
                            source_parts = parts
                        else:
                            target_text = text
                            target_tags = tags
                            target_parts = parts

                tags_obj = Tags(
                    source_tag_map=source_tags,
                    target_tag_map=target_tags,
                    source_parts=source_parts,
                    target_parts=target_parts,
                )

                data_obj = Data(
                    source=source_text,
                    target=target_text if target_text else None,
                    plural=None,
                    tags=tags_obj if (source_tags or target_tags) else None,
                    meta=meta,
                    status=status,
                    comments=comments,
                    previous_context=prev_ctx,
                    next_context=next_ctx,
                    extensions=extensions,
                )

                yield unit_id, data_obj

                clear_element(elem)

    def extract_async(self) -> AsyncIterator[ExtractItem]:
        return AsyncTmxExtraction(self)
