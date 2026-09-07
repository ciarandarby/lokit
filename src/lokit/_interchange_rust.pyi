from collections.abc import Sequence
from typing import TypeAlias

from lokit.data.structure import BaseStructure, Data, Plural, SegmentPart
from lokit.data.tag_types import TieData
from lokit.tabular import ResolvedTabularLayout

NativeRecord: TypeAlias = tuple[
    bool,
    str,
    str,
    str | None,
    list[tuple[str, str]],
    str,
    dict[str, str],
    bytes | None,
]
LokitRecord: TypeAlias = tuple[str, Data]
PlaceholderOccurrence: TypeAlias = tuple[
    str,
    str,
    str,
    str,
    int,
    int,
    int | None,
    int | None,
    str,
]
PlaceholderProjection: TypeAlias = tuple[str, dict[str, TieData], list[SegmentPart], str]

class CsvReader:
    def __init__(self, path: str) -> None: ...
    @property
    def first_row(self) -> list[str] | None: ...
    @property
    def closed(self) -> bool: ...
    def configure(self, layout: ResolvedTabularLayout) -> None: ...
    def read_batch(
        self,
        target_locale: str | None = ...,
        runtime_placeholders: bool = ...,
        inline_placeholders: bool = ...,
        syntaxes: list[str] | None = ...,
        batch_size: int = ...,
    ) -> list[LokitRecord]: ...
    def read_target_batch(self, batch_size: int = ...) -> list[dict[str, LokitRecord]]: ...
    def close(self) -> None: ...

def json_object_roots(path: str) -> list[str]: ...

class JsonReader:
    def __init__(
        self,
        path: str,
        source_root: str | None = ...,
        targets: list[tuple[str, str, str | None]] = ...,
        selected_target: str | None = ...,
    ) -> None: ...
    @property
    def closed(self) -> bool: ...
    def read_batch(
        self,
        runtime_placeholders: bool = ...,
        inline_placeholders: bool = ...,
        syntaxes: list[str] | None = ...,
        batch_size: int = ...,
    ) -> list[LokitRecord]: ...
    def close(self) -> None: ...

class Reader:
    def read_row_batch(
        self,
        fields: list[str],
        source_language: str = "",
        target_language: str = "",
        domain: str = "",
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        syntaxes: list[str] | None = None,
        batch_size: int = 256,
    ) -> list[dict[str, str]]: ...
    def __init__(
        self,
        path: str,
        format_name: str,
        source_language: str | None = ...,
        target_language: str | None = ...,
        mode: str = ...,
    ) -> None: ...
    @property
    def version(self) -> str: ...
    @property
    def source_locale(self) -> str | None: ...
    @property
    def target_locale(self) -> str | None: ...
    @property
    def source_language(self) -> str | None: ...
    @property
    def target_language(self) -> str | None: ...
    @property
    def target_locales(self) -> list[str]: ...
    @property
    def target_languages(self) -> list[str]: ...
    @property
    def export_origin(self) -> str: ...
    @property
    def export_timestamp(self) -> str: ...
    @property
    def extensions(self) -> dict[str, str]: ...
    @property
    def closed(self) -> bool: ...
    def read_batch(self, batch_size: int = ...) -> list[NativeRecord]: ...
    def read_data_batch(
        self,
        batch_size: int = ...,
        runtime_placeholders: bool = ...,
        inline_placeholders: bool = ...,
        syntaxes: list[str] | None = ...,
        domain: str | None = ...,
    ) -> list[LokitRecord]: ...
    def close(self) -> None: ...

class PoReader:
    def __init__(
        self,
        path: str,
        source_locale: str | None = ...,
        target_locale: str | None = ...,
        mode: str = ...,
    ) -> None: ...
    @property
    def source_locale(self) -> str: ...
    @property
    def target_locale(self) -> str | None: ...
    @property
    def source_language(self) -> str | None: ...
    @property
    def target_language(self) -> str | None: ...
    @property
    def target_locales(self) -> list[str]: ...
    @property
    def target_languages(self) -> list[str]: ...
    @property
    def export_origin(self) -> str: ...
    @property
    def export_timestamp(self) -> str: ...
    @property
    def extensions(self) -> dict[str, str]: ...
    @property
    def closed(self) -> bool: ...
    def read_batch(self, batch_size: int = ...) -> list[LokitRecord]: ...
    def close(self) -> None: ...

class LokitReader:
    def __init__(self, path: str) -> None: ...
    @property
    def source_locale(self) -> str: ...
    @property
    def target_locale(self) -> str | None: ...
    @property
    def target_locales(self) -> list[str]: ...
    @property
    def format_version(self) -> str: ...
    @property
    def export_origin(self) -> str: ...
    @property
    def export_timestamp(self) -> str: ...
    @property
    def source_language(self) -> str | None: ...
    @property
    def target_language(self) -> str | None: ...
    @property
    def target_languages(self) -> list[str]: ...
    @property
    def extensions(self) -> dict[str, str]: ...
    @property
    def closed(self) -> bool: ...
    def read_batch(self, batch_size: int = ...) -> list[LokitRecord]: ...
    def read_target_batch(
        self,
        locale: str,
        legacy_locale: str | None = ...,
        include_missing: bool = ...,
        batch_size: int = ...,
    ) -> list[LokitRecord]: ...
    def close(self) -> None: ...

class LokitWriter:
    def __init__(
        self,
        path: str,
        source_locale: str,
        target_locale: str | None,
        target_locales: tuple[str, ...],
        format_version: str,
        export_origin: str,
        export_timestamp: str,
        source_language: str | None,
        target_language: str | None,
        target_languages: tuple[str, ...],
        extensions: dict[str, str],
    ) -> None: ...
    @property
    def closed(self) -> bool: ...
    def write(self, unit_id: str, data: Data) -> None: ...
    def write_batch(self, items: list[tuple[str, Data]], resolve_placeholders: bool = True) -> None: ...
    def close(self) -> None: ...
    def abort(self) -> None: ...

def backend_version() -> str: ...
def detect_placeholders(
    text: str,
    syntaxes: Sequence[str] | None = ...,
    gettext_flags: Sequence[str] | None = ...,
    auto_detect: bool = ...,
    max_input_bytes: int | None = ...,
    max_occurrences: int | None = ...,
    max_placeholder_bytes: int | None = ...,
    max_nesting: int | None = ...,
) -> list[PlaceholderOccurrence]: ...
def project_placeholders(
    text: str,
    syntaxes: Sequence[str] | None = ...,
    gettext_flags: Sequence[str] | None = ...,
    auto_detect: bool = ...,
    max_input_bytes: int | None = ...,
    max_occurrences: int | None = ...,
    max_placeholder_bytes: int | None = ...,
    max_nesting: int | None = ...,
) -> PlaceholderProjection: ...
def project_data_placeholders(
    data: Data,
    runtime_placeholders: bool = ...,
    inline_placeholders: bool = ...,
    project_targets: bool = ...,
    syntaxes: Sequence[str] | None = ...,
    gettext_flags: Sequence[str] | None = ...,
    auto_detect: bool = ...,
    max_input_bytes: int | None = ...,
    max_occurrences: int | None = ...,
    max_placeholder_bytes: int | None = ...,
    max_nesting: int | None = ...,
) -> Data: ...
def resolve_data_placeholders(data: Data) -> Data: ...
def literalize_data_placeholders(data: Data) -> Data: ...
def canonicalize_placeholders(
    text: str,
    syntaxes: Sequence[str] | None = ...,
    gettext_flags: Sequence[str] | None = ...,
    auto_detect: bool = ...,
    max_input_bytes: int | None = ...,
    max_occurrences: int | None = ...,
    max_placeholder_bytes: int | None = ...,
    max_nesting: int | None = ...,
) -> tuple[str, str]: ...
def reform_placeholders(
    candidate_source: str,
    candidate_target: str,
    query_source: str,
    syntaxes: Sequence[str] | None = ...,
    gettext_flags: Sequence[str] | None = ...,
    auto_detect: bool = ...,
    max_input_bytes: int | None = ...,
    max_occurrences: int | None = ...,
    max_placeholder_bytes: int | None = ...,
    max_nesting: int | None = ...,
) -> tuple[str, bool, str]: ...
def materialize_interchange(
    path: str,
    format_name: str,
    source_language: str | None = ...,
    target_language: str | None = ...,
    domain: str | None = ...,
    mode: str = ...,
    runtime_placeholders: bool = ...,
    inline_placeholders: bool = ...,
    syntaxes: list[str] | None = ...,
) -> BaseStructure | None: ...
def materialize_interchange_bytes(
    data: bytes,
    format_name: str,
    source_language: str | None = ...,
    target_language: str | None = ...,
    domain: str | None = ...,
    mode: str = ...,
    runtime_placeholders: bool = ...,
    inline_placeholders: bool = ...,
    syntaxes: list[str] | None = ...,
) -> BaseStructure: ...
def materialize_po(
    path: str,
    source_locale: str | None = ...,
    target_locale: str | None = ...,
    mode: str = ...,
) -> BaseStructure: ...
def convert_interchange(
    source_path: str,
    target_path: str,
    input_format: str,
    output_format: str,
    source_language: str | None = ...,
    target_language: str | None = ...,
    mode: str = ...,
    copy_if_same: bool = ...,
) -> int | None: ...
def export_base_interchange(document: object, target_path: str, output_format: str) -> int | None: ...
def export_stream_interchange(document: object, target_path: str, output_format: str) -> int | None: ...
def export_base_po(document: object, target_path: str, mode: str = ...) -> int | None: ...
def export_base_po_interchange(document: object, target_path: str, output_format: str) -> int | None: ...

class IdentityRegistry:
    def __init__(self) -> None: ...
    def tmx(self, raw: str) -> str: ...
    def xliff(self, raw: str, parent: str, resource: int, v2: bool) -> str: ...
    def resolve(self, preferred: str) -> str: ...
    def resolve_tabular(self, row: Sequence[str], index: int, id_column: int, format_label: str) -> str: ...
    def resolve_path(self, path: Sequence[str]) -> str: ...

def structural_json_path(path: Sequence[str]) -> str: ...
def xliff_plural(extensions: dict[str, str]) -> Plural | None: ...
