from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Protocol

from lokit.data.structure import BaseStructure, Data, StreamingStructure
from lokit.documents.models import DocumentSink, DocumentSource, OfficeExportResult
from lokit.documents.options import OfficeExportOptions, OfficeImportOptions

ExtractItem = tuple[str, Data]


class OfficeBackendProtocol(Protocol):
    def extract(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
    ) -> Iterator[ExtractItem]: ...

    def stream(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
    ) -> StreamingStructure: ...

    async def extract_async(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
    ) -> AsyncIterator[ExtractItem]: ...

    def reinsert(
        self,
        document: BaseStructure | StreamingStructure,
        output: DocumentSink,
        file_format: str,
        source_document: DocumentSource | None = None,
        target_locale: str | None = None,
        options: OfficeExportOptions | None = None,
    ) -> OfficeExportResult: ...
