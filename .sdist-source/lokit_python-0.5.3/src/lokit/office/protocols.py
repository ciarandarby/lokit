from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from lokit.data.structure import BaseStructure, Data, StreamingStructure
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence

    from lokit.office.models import DocumentSink, DocumentSource, OfficeExportResult
    from lokit.office.options import OfficeExportOptions, OfficeImportOptions
    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]


class OfficeBackendProtocol(Protocol):
    def extract(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> Iterator[ExtractItem]: ...

    def stream(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
        progress: bool = False,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> StreamingStructure: ...

    def extract_async(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> AsyncIterator[ExtractItem]: ...

    def import_document(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
        progress: bool = True,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> BaseStructure: ...

    def reinsert(
        self,
        document: BaseStructure | StreamingStructure,
        output: DocumentSink,
        file_format: str,
        source_document: DocumentSource | None = None,
        target_locale: str | None = None,
        options: OfficeExportOptions | None = None,
        *,
        resolve_placeholders: bool = True,
    ) -> OfficeExportResult: ...
