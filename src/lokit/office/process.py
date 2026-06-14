from __future__ import annotations

import asyncio
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lokit.office.errors import OfficeProtocolError, OfficeTimeoutError, OfficeWorkerError
from lokit.office.models import OfficeExportResult
from lokit.office.options import OfficeExportOptions, OfficeImportOptions
from lokit.office.protocol import (
    FrameType,
    ProtocolFrame,
    data_to_unit_payload,
    decode_frame,
    encode_frame,
    unit_payload_to_data,
)
from lokit.office.runtime import executable_path, load_runtime_info, validate_executable_digest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from lokit.data.structure import Data


@dataclass(frozen=True, slots=True)
class WorkerDiagnostics:
    stderr: str = ""
    returncode: int | None = None


async def run_worker_command(args: tuple[str, ...], timeout_seconds: float) -> WorkerDiagnostics:
    info = load_runtime_info()
    executable = executable_path()
    validate_executable_digest(executable, info.sha256)
    process = await asyncio.create_subprocess_exec(
        str(executable),
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        raise OfficeTimeoutError("Office worker timed out") from exc
    diagnostics = WorkerDiagnostics(
        stderr=stderr.decode("utf-8", errors="replace"),
        returncode=process.returncode,
    )
    if process.returncode not in (0, None):
        raise OfficeWorkerError(f"Office worker exited with status {process.returncode}: {diagnostics.stderr}")
    return diagnostics


def worker_available() -> bool:
    try:
        path = executable_path()
    except Exception:
        return False
    return Path(path).exists()


def extract_with_worker(
    source_path: Path,
    file_format: str,
    source_locale: str,
    target_locale: str | None,
    options: OfficeImportOptions,
) -> tuple[str, list[tuple[str, Data]]]:
    request_id = uuid.uuid4()
    process = _start_worker(options.timeout_seconds)
    try:
        _write_frame(process, _hello_frame(request_id), options.max_frame_bytes)
        _expect_frame(process, FrameType.READY, request_id, options.max_frame_bytes)
        _write_frame(
            process,
            ProtocolFrame(
                FrameType.EXTRACT_REQUEST,
                request_id,
                {
                    "required": {
                        "format": file_format,
                        "source_path": str(source_path),
                        "source_locale": source_locale,
                        "target_locale": target_locale,
                        "options": _options_payload(options),
                    }
                },
            ),
            options.max_frame_bytes,
        )
        fingerprint = ""
        items: list[tuple[str, Data]] = []
        while True:
            frame = _read_frame(process, options.max_frame_bytes)
            _validate_request(frame, request_id)
            if frame.frame_type == FrameType.DOCUMENT_START:
                required = _required(frame.payload)
                fingerprint = str(required.get("source_fingerprint", ""))
            elif frame.frame_type == FrameType.UNIT:
                items.append(unit_payload_to_data(frame.payload))
            elif frame.frame_type == FrameType.WARNING:
                continue
            elif frame.frame_type == FrameType.DONE:
                _finish_worker(process)
                return fingerprint, items
            elif frame.frame_type == FrameType.ERROR:
                raise OfficeWorkerError(_error_message(frame.payload))
            else:
                raise OfficeProtocolError(f"Unexpected Office worker frame: {frame.frame_type}")
    except BaseException:
        _terminate_worker(process)
        raise


def extract_with_worker_iter(
    source_path: Path,
    file_format: str,
    source_locale: str,
    target_locale: str | None,
    options: OfficeImportOptions,
) -> Iterator[tuple[str, Data]]:
    request_id = uuid.uuid4()
    process = _start_worker(options.timeout_seconds)
    try:
        _write_frame(process, _hello_frame(request_id), options.max_frame_bytes)
        _expect_frame(process, FrameType.READY, request_id, options.max_frame_bytes)
        _write_frame(
            process,
            ProtocolFrame(
                FrameType.EXTRACT_REQUEST,
                request_id,
                {
                    "required": {
                        "format": file_format,
                        "source_path": str(source_path),
                        "source_locale": source_locale,
                        "target_locale": target_locale,
                        "options": _options_payload(options),
                    }
                },
            ),
            options.max_frame_bytes,
        )
        while True:
            frame = _read_frame(process, options.max_frame_bytes)
            _validate_request(frame, request_id)
            if frame.frame_type == FrameType.DOCUMENT_START:
                continue
            if frame.frame_type == FrameType.UNIT:
                yield unit_payload_to_data(frame.payload)
            elif frame.frame_type == FrameType.WARNING:
                continue
            elif frame.frame_type == FrameType.DONE:
                _finish_worker(process)
                return
            elif frame.frame_type == FrameType.ERROR:
                raise OfficeWorkerError(_error_message(frame.payload))
            else:
                raise OfficeProtocolError(f"Unexpected Office worker frame: {frame.frame_type}")
    except BaseException:
        _terminate_worker(process)
        raise


def reinsert_with_worker(
    source_path: Path,
    output_path: Path,
    file_format: str,
    translations: dict[str, Data],
    target_locale: str | None,
    options: OfficeExportOptions,
) -> OfficeExportResult:
    request_id = uuid.uuid4()
    process = _start_worker(options.timeout_seconds)
    try:
        _write_frame(process, _hello_frame(request_id), options.max_frame_bytes)
        _expect_frame(process, FrameType.READY, request_id, options.max_frame_bytes)
        _write_frame(
            process,
            ProtocolFrame(
                FrameType.REINSERT_REQUEST,
                request_id,
                {
                    "required": {
                        "format": file_format,
                        "source_path": str(source_path),
                        "output_path": str(output_path),
                        "target_locale": target_locale,
                        "options": _options_payload(options),
                    }
                },
            ),
            options.max_frame_bytes,
        )
        for unit_id, data in translations.items():
            _write_frame(
                process,
                ProtocolFrame(
                    FrameType.TRANSLATION_UNIT,
                    request_id,
                    data_to_unit_payload(unit_id, data, target_locale),
                ),
                options.max_frame_bytes,
            )
        _write_frame(
            process,
            ProtocolFrame(FrameType.TRANSLATION_END, request_id, {"required": {}}),
            options.max_frame_bytes,
        )
        result = OfficeExportResult(output_path, 0)
        while True:
            frame = _read_frame(process, options.max_frame_bytes)
            _validate_request(frame, request_id)
            if frame.frame_type == FrameType.RESULT:
                required = _required(frame.payload)
                result = OfficeExportResult(
                    output_path=output_path,
                    units_written=_int_value(required.get("units_written", 0)),
                    warnings=(),
                    source_fingerprint=str(required.get("source_fingerprint", "")),
                    output_bytes=_int_value(required.get("output_bytes", 0)),
                )
            elif frame.frame_type == FrameType.WARNING:
                continue
            elif frame.frame_type == FrameType.DONE:
                _finish_worker(process)
                return result
            elif frame.frame_type == FrameType.ERROR:
                raise OfficeWorkerError(_error_message(frame.payload))
            else:
                raise OfficeProtocolError(f"Unexpected Office worker frame: {frame.frame_type}")
    except BaseException:
        _terminate_worker(process)
        raise


def _start_worker(timeout_seconds: float) -> subprocess.Popen[bytes]:
    info = load_runtime_info()
    executable = executable_path()
    validate_executable_digest(executable, info.sha256)
    return subprocess.Popen(
        [str(executable)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _hello_frame(request_id: uuid.UUID) -> ProtocolFrame:
    return ProtocolFrame(
        FrameType.HELLO,
        request_id,
        {
            "required": {
                "client": "lokit-python",
                "client_version": "0.3.1",
                "protocol_major": 1,
                "protocol_minor": 0,
            },
            "optional": {"debug": False},
        },
    )


def _write_frame(
    process: subprocess.Popen[bytes],
    frame: ProtocolFrame,
    max_frame_bytes: int,
) -> None:
    if process.stdin is None:
        raise OfficeWorkerError("Office worker stdin is unavailable")
    process.stdin.write(encode_frame(frame, max_frame_bytes))
    process.stdin.flush()


def _read_frame(
    process: subprocess.Popen[bytes],
    max_frame_bytes: int,
) -> ProtocolFrame:
    if process.stdout is None:
        raise OfficeWorkerError("Office worker stdout is unavailable")
    header = _read_exact(process, 32)
    payload_length = int.from_bytes(header[28:32], "big")
    payload = _read_exact(process, payload_length)
    return decode_frame(header + payload, max_frame_bytes)


def _read_exact(process: subprocess.Popen[bytes], length: int) -> bytes:
    if process.stdout is None:
        raise OfficeWorkerError("Office worker stdout is unavailable")
    data = process.stdout.read(length)
    if data is None or len(data) != length:
        stderr = _stderr_text(process)
        raise OfficeWorkerError(f"Office worker ended unexpectedly: {stderr}")
    return data


def _expect_frame(
    process: subprocess.Popen[bytes],
    frame_type: int,
    request_id: uuid.UUID,
    max_frame_bytes: int,
) -> ProtocolFrame:
    frame = _read_frame(process, max_frame_bytes)
    _validate_request(frame, request_id)
    if frame.frame_type != frame_type:
        raise OfficeProtocolError(f"Expected Office worker frame {frame_type}, got {frame.frame_type}")
    return frame


def _validate_request(frame: ProtocolFrame, request_id: uuid.UUID) -> None:
    if frame.request_id != request_id:
        raise OfficeProtocolError("Office worker request ID mismatch")


def _finish_worker(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        process.stdin.close()
    returncode = process.wait(timeout=5)
    if returncode != 0:
        raise OfficeWorkerError(f"Office worker exited with status {returncode}: {_stderr_text(process)}")


def _terminate_worker(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _stderr_text(process: subprocess.Popen[bytes]) -> str:
    if process.stderr is None:
        return ""
    try:
        return process.stderr.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _required(payload: dict[str, object]) -> dict[str, object]:
    value = payload.get("required")
    if not isinstance(value, dict):
        raise OfficeProtocolError("Office worker frame is missing required object")
    return value


def _error_message(payload: dict[str, object]) -> str:
    required = payload.get("required")
    if isinstance(required, dict):
        message = required.get("message")
        if isinstance(message, str):
            return message
    return "Office worker error"


def _options_payload(options: OfficeImportOptions) -> dict[str, object]:
    payload: dict[str, object] = {
        "max_frame_bytes": options.max_frame_bytes,
        "max_unit_bytes": options.max_unit_bytes,
        "max_zip_entries": options.max_zip_entries,
        "max_compressed_bytes": options.max_compressed_bytes,
        "max_uncompressed_bytes": options.max_uncompressed_bytes,
        "max_compression_ratio": options.max_compression_ratio,
        "include_headers_footers": options.include_headers_footers,
        "include_comments": options.include_comments,
        "include_notes": options.include_notes,
        "include_master_layout_content": options.include_master_layout_content,
    }
    if isinstance(options, OfficeExportOptions):
        payload["missing_translation_policy"] = options.missing_translation_policy.value
        payload["extra_translation_policy"] = options.extra_translation_policy.value
    return payload


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return 0
