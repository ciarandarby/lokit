from __future__ import annotations

from dataclasses import dataclass

from lokit.compat import StrEnum


class OfficeValidationMode(StrEnum):
    OFF = "off"
    BASIC = "basic"
    OPENXML = "openxml"


class MissingTranslationPolicy(StrEnum):
    PRESERVE = "preserve"
    WARN = "warn"
    ERROR = "error"


class ExtraTranslationPolicy(StrEnum):
    WARN = "warn"
    ERROR = "error"


class TagMismatchPolicy(StrEnum):
    PRESERVE = "preserve"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class OfficeImportOptions:
    timeout_seconds: float = 120.0
    startup_timeout_seconds: float = 10.0
    idle_timeout_seconds: float = 30.0
    max_frame_bytes: int = 16 * 1024 * 1024
    max_unit_bytes: int = 8 * 1024 * 1024
    max_zip_entries: int = 10_000
    max_compressed_bytes: int = 1 * 1024 * 1024 * 1024
    max_uncompressed_bytes: int = 4 * 1024 * 1024 * 1024
    max_compression_ratio: float = 100.0
    max_text_unit_chars: int = 1_000_000
    include_headers_footers: bool = True
    include_comments: bool = True
    include_notes: bool = True
    include_master_layout_content: bool = False
    include_alt_text: bool = True
    include_charts: bool = True
    include_hidden_slides: bool = True
    validation_mode: OfficeValidationMode = OfficeValidationMode.BASIC


@dataclass(frozen=True, slots=True)
class OfficeExportOptions(OfficeImportOptions):
    missing_translation_policy: MissingTranslationPolicy = MissingTranslationPolicy.PRESERVE
    extra_translation_policy: ExtraTranslationPolicy = ExtraTranslationPolicy.WARN
    tag_mismatch_policy: TagMismatchPolicy = TagMismatchPolicy.ERROR
