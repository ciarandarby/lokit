from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO


DocumentSource = str | Path | bytes | BinaryIO
DocumentSink = str | Path | BinaryIO


@dataclass(frozen=True, slots=True)
class OfficeWarning:
    code: str
    message: str
    unit_id: str | None = None
    part: str | None = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OfficeExportResult:
    output_path: Path | None
    units_written: int
    warnings: tuple[OfficeWarning, ...] = ()
    source_fingerprint: str = ""
    output_bytes: int = 0


@dataclass(frozen=True, slots=True)
class OfficeRuntimeInfo:
    worker_version: str
    protocol_major: int
    protocol_minor: int
    rid: str = ""
    build_commit: str = ""
    openxml_sdk_version: str = ""
    sha256: str = ""
