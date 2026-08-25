from __future__ import annotations

import json
import struct
import uuid
from dataclasses import dataclass
from typing import Final

from lokit.data.structure import Data, Tags, TargetTags, TextPart, TranslationStatus
from lokit.office.errors import OfficeProtocolError
from lokit.office.runtime import PROTOCOL_MAJOR, PROTOCOL_MINOR

MAGIC = b"LOK1"
HEADER = struct.Struct(">4sHHHH16sI")


class FrameType:
    HELLO: Final[int] = 0x0001
    READY: Final[int] = 0x0002
    EXTRACT_REQUEST: Final[int] = 0x0003
    REINSERT_REQUEST: Final[int] = 0x0004
    TRANSLATION_UNIT: Final[int] = 0x0005
    TRANSLATION_END: Final[int] = 0x0006
    CANCEL: Final[int] = 0x0007
    DOCUMENT_START: Final[int] = 0x0010
    UNIT: Final[int] = 0x0011
    WARNING: Final[int] = 0x0012
    PROGRESS: Final[int] = 0x0013
    RESULT: Final[int] = 0x0014
    ERROR: Final[int] = 0x0015
    DONE: Final[int] = 0x0016


@dataclass(frozen=True, slots=True)
class ProtocolFrame:
    frame_type: int
    request_id: uuid.UUID
    payload: dict[str, object]
    required: bool = True
    major: int = PROTOCOL_MAJOR
    minor: int = PROTOCOL_MINOR


def encode_frame(frame: ProtocolFrame, max_frame_bytes: int = 16 * 1024 * 1024) -> bytes:
    payload = json.dumps(frame.payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > max_frame_bytes:
        raise OfficeProtocolError("Office protocol frame exceeds max_frame_bytes")
    flags = 1 if frame.required else 0
    return (
        HEADER.pack(
            MAGIC,
            frame.major,
            frame.minor,
            frame.frame_type,
            flags,
            frame.request_id.bytes,
            len(payload),
        )
        + payload
    )


def decode_frame(data: bytes, max_frame_bytes: int = 16 * 1024 * 1024) -> ProtocolFrame:
    if len(data) < HEADER.size:
        raise OfficeProtocolError("Office protocol frame is truncated")
    magic, major, minor, frame_type, flags, request_id, payload_len = HEADER.unpack(data[: HEADER.size])
    if magic != MAGIC:
        raise OfficeProtocolError("Invalid Office protocol frame magic")
    if major != PROTOCOL_MAJOR:
        raise OfficeProtocolError(f"Unsupported Office protocol major version: {major}")
    if payload_len > max_frame_bytes:
        raise OfficeProtocolError("Office protocol frame exceeds max_frame_bytes")
    payload_data = data[HEADER.size :]
    if len(payload_data) != payload_len:
        raise OfficeProtocolError("Office protocol payload length mismatch")
    payload = json.loads(payload_data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise OfficeProtocolError("Office protocol payload must be an object")
    return ProtocolFrame(
        frame_type=frame_type,
        request_id=uuid.UUID(bytes=request_id),
        payload=payload,
        required=bool(flags & 1),
        major=major,
        minor=minor,
    )


def data_to_unit_payload(unit_id: str, data: Data, target_locale: str | None = None) -> dict[str, object]:
    target = data.target
    if target_locale and target_locale in data.targets:
        target = data.targets[target_locale].text
    return {
        "required": {
            "unit_id": unit_id,
            "source": data.source,
            "target": target or "",
        },
        "optional": {
            "target_locale": target_locale,
            "extensions": data.extensions,
        },
    }


def unit_payload_to_data(payload: dict[str, object]) -> tuple[str, Data]:
    required = payload.get("required")
    optional = payload.get("optional", {})
    if not isinstance(required, dict):
        raise OfficeProtocolError("Office unit payload is missing required fields")
    if not isinstance(optional, dict):
        optional = {}
    unit_id = str(required.get("unit_id", ""))
    source = str(required.get("source", ""))
    target_value = required.get("target")
    status_value = str(required.get("status", TranslationStatus.UNKNOWN.value))
    extensions: dict[str, str] = {}
    raw_extensions = optional.get("extensions")
    if isinstance(raw_extensions, dict):
        extensions = {str(key): str(value) for key, value in raw_extensions.items()}
    tags = Tags(source_parts=[TextPart(source)]) if source else None
    return unit_id, Data(
        source=source,
        target=str(target_value) if target_value is not None else None,
        tags=tags,
        status=TranslationStatus(status_value),
        extensions=extensions,
    )


def target_tags_from_data(data: Data, target_locale: str | None) -> TargetTags | None:
    if target_locale and target_locale in data.targets:
        return data.targets[target_locale].tags
    if data.tags and data.tags.target_parts:
        return TargetTags(data.tags.target_tag_map, data.tags.target_parts)
    return None
