from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import queue
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Protocol, TypeVar, cast

from lokit.io.atomic import raise_if_cancelled
from lokit.office.errors import OfficeProtocolError, OfficeReinsertionError, OfficeTimeoutError, OfficeWorkerError
from lokit.office.models import OfficeExportResult, OfficeWarning
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
    from collections.abc import Callable, Iterable, Iterator

    from lokit.data.structure import Data

_T = TypeVar("_T")

_DIAGNOSTIC_TAIL_BYTES = 64 * 1024
_DIAGNOSTIC_READ_BYTES = 8 * 1024
_CANCELLATION_POLL_SECONDS = 0.05


class _ClosableIterator(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkerDiagnostics:
    stderr: str = ""
    returncode: int | None = None


class _BoundedByteTail:
    """Retain only the most recent diagnostic bytes without slowing writers."""

    def __init__(self, max_bytes: int = _DIAGNOSTIC_TAIL_BYTES) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        if not data:
            return
        if len(data) >= self._max_bytes:
            with self._lock:
                self._chunks.clear()
                self._chunks.append(data[-self._max_bytes :])
                self._size = self._max_bytes
            return
        with self._lock:
            self._chunks.append(data)
            self._size += len(data)
            excess = self._size - self._max_bytes
            while excess > 0:
                first = self._chunks.popleft()
                if len(first) <= excess:
                    self._size -= len(first)
                    excess -= len(first)
                    continue
                self._chunks.appendleft(first[excess:])
                self._size -= excess
                excess = 0

    def text(self) -> str:
        with self._lock:
            value = b"".join(self._chunks)
        return value.decode("utf-8", errors="replace")


class _StderrDrainer:
    """Continuously drain a worker's stderr while retaining a bounded tail."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._tail = _BoundedByteTail()
        self._reader_thread = threading.Thread(
            target=self._run,
            name="lokit-office-stderr",
            daemon=True,
        )
        self._reader_thread.start()

    def text(self, *, wait_for_eof: bool = False) -> str:
        if wait_for_eof:
            self._reader_thread.join(timeout=0.25)
        return self._tail.text()

    def finish(self) -> None:
        self._reader_thread.join(timeout=1.0)
        with contextlib.suppress(OSError):
            self._stream.close()
        if self._reader_thread.is_alive():
            self._reader_thread.join()

    def _run(self) -> None:
        try:
            while True:
                data = self._stream.read(_DIAGNOSTIC_READ_BYTES)
                if not data:
                    return
                self._tail.append(data)
        except (OSError, ValueError):
            return


class _TimedOperationExpired(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _OperationCall:
    operation: Callable[[], object]
    result: queue.Queue[tuple[bool, object]]


class _OperationRunner:
    def __init__(self) -> None:
        self._requests: queue.Queue[_OperationCall | None] = queue.Queue()
        self._closed = False
        # ``_thread`` becomes ``__thread`` in mypyc's generated C, which is a
        # compiler keyword on Clang/GCC and makes release wheels uncompilable.
        self._worker_thread = threading.Thread(target=self._run, name="lokit-office-io", daemon=True)
        self._worker_thread.start()

    def execute(
        self,
        operation: Callable[[], _T],
        timeout: float,
        cancellation: threading.Event | None = None,
    ) -> _T:
        if self._closed:
            raise OfficeWorkerError("Office worker I/O runner is closed")
        raise_if_cancelled(cancellation)
        result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
        self._requests.put(_OperationCall(operation, result))
        deadline = time.monotonic() + timeout
        while True:
            raise_if_cancelled(cancellation)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TimedOperationExpired
            try:
                succeeded, value = result.get(timeout=min(remaining, _CANCELLATION_POLL_SECONDS))
                break
            except queue.Empty:
                continue
        if succeeded:
            return cast("_T", value)
        if isinstance(value, BaseException):
            raise value
        raise RuntimeError("Office worker I/O failed without an exception")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._requests.put(None)
        self._worker_thread.join()

    def _run(self) -> None:
        while True:
            call = self._requests.get()
            if call is None:
                return
            try:
                value = call.operation()
            except BaseException as exc:
                call.result.put((False, exc))
            else:
                call.result.put((True, value))


@dataclass(slots=True)
class _WorkerSession:
    process: subprocess.Popen[bytes]
    runner: _OperationRunner
    stderr_drainer: _StderrDrainer
    identity: tuple[Path, str]
    owner_pid: int
    total_deadline: float
    startup_deadline: float
    idle_timeout_seconds: float

    def configure_request(self, options: OfficeImportOptions, started: float) -> None:
        self.total_deadline = started + options.timeout_seconds
        self.idle_timeout_seconds = options.idle_timeout_seconds

    def write_frame(
        self,
        frame: ProtocolFrame,
        max_frame_bytes: int,
        *,
        startup: bool = False,
        cancellation: threading.Event | None = None,
    ) -> None:
        self._execute(
            lambda: _write_frame(self.process, frame, max_frame_bytes),
            startup=startup,
            cancellation=cancellation,
        )

    def read_frame(
        self,
        max_frame_bytes: int,
        *,
        startup: bool = False,
        cancellation: threading.Event | None = None,
    ) -> ProtocolFrame:
        return self._execute(
            lambda: _read_frame(self.process, self.stderr_drainer, max_frame_bytes),
            startup=startup,
            cancellation=cancellation,
        )

    def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=1.0)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            _terminate_worker(self.process)
        finally:
            self.stderr_drainer.finish()
            self.runner.close()

    def terminate(self) -> None:
        try:
            _terminate_worker(self.process)
        finally:
            self.stderr_drainer.finish()
            self.runner.close()

    def detach_after_fork(self) -> None:
        # Do not join the inherited runner/drainer threads: after fork they no
        # longer exist, and their Python synchronization objects may have been
        # locked by the vanished threads.  Worker pipes are deliberately
        # unbuffered (see ``_start_worker``), so closing them cannot wait on a
        # BufferedIO lock inherited in the locked state.
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()

    def _execute(
        self,
        operation: Callable[[], _T],
        *,
        startup: bool,
        cancellation: threading.Event | None,
    ) -> _T:
        timeout, timeout_kind = self._timeout_window(startup=startup)
        try:
            return self.runner.execute(operation, timeout, cancellation)
        except _TimedOperationExpired as exc:
            self.terminate()
            raise OfficeTimeoutError(f"Office worker {timeout_kind} timeout") from exc

    def _timeout_window(self, *, startup: bool) -> tuple[float, str]:
        now = time.monotonic()
        total_remaining = self.total_deadline - now
        if startup:
            phase_remaining = self.startup_deadline - now
            phase_kind = "startup"
        else:
            phase_remaining = self.idle_timeout_seconds
            phase_kind = "idle"
        if total_remaining <= phase_remaining:
            timeout = total_remaining
            timeout_kind = "total"
        else:
            timeout = phase_remaining
            timeout_kind = phase_kind
        if timeout <= 0:
            self.terminate()
            raise OfficeTimeoutError(f"Office worker {timeout_kind} timeout")
        return timeout, timeout_kind


_WORKER_LOCK = threading.Lock()
_PERSISTENT_WORKER: _WorkerSession | None = None


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
        limit=_DIAGNOSTIC_READ_BYTES,
    )
    stdout_tail = _BoundedByteTail(1)
    stderr_tail = _BoundedByteTail()
    stdout_task = asyncio.create_task(_drain_async_stream(process.stdout, stdout_tail))
    stderr_task = asyncio.create_task(_drain_async_stream(process.stderr, stderr_tail))
    if process.stdin is not None:
        process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        raise OfficeTimeoutError("Office worker timed out") from None
    finally:
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    diagnostics = WorkerDiagnostics(
        stderr=stderr_tail.text(),
        returncode=process.returncode,
    )
    if process.returncode not in (0, None):
        raise OfficeWorkerError(f"Office worker exited with status {process.returncode}: {diagnostics.stderr}")
    return diagnostics


async def _drain_async_stream(
    stream: asyncio.StreamReader | None,
    tail: _BoundedByteTail,
) -> None:
    if stream is None:
        return
    try:
        while True:
            data = await stream.read(_DIAGNOSTIC_READ_BYTES)
            if not data:
                return
            tail.append(data)
    except (ConnectionError, OSError, ValueError):
        return


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
    with _worker_request(options) as session:
        session.write_frame(
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
            frame = session.read_frame(options.max_frame_bytes)
            _validate_request(frame, request_id)
            if frame.frame_type == FrameType.DOCUMENT_START:
                required = _required(frame.payload)
                fingerprint = str(required.get("source_fingerprint", ""))
            elif frame.frame_type == FrameType.UNIT:
                items.append(unit_payload_to_data(frame.payload))
            elif frame.frame_type == FrameType.WARNING:
                _warning_from_payload(frame.payload)
            elif frame.frame_type == FrameType.DONE:
                return fingerprint, items
            elif frame.frame_type == FrameType.ERROR:
                raise OfficeWorkerError(_error_message(frame.payload))
            else:
                raise OfficeProtocolError(f"Unexpected Office worker frame: {frame.frame_type}")


def extract_with_worker_iter(
    source_path: Path,
    file_format: str,
    source_locale: str,
    target_locale: str | None,
    options: OfficeImportOptions,
) -> Iterator[tuple[str, Data]]:
    request_id = uuid.uuid4()
    with _worker_request(options) as session:
        session.write_frame(
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
            frame = session.read_frame(options.max_frame_bytes)
            _validate_request(frame, request_id)
            if frame.frame_type == FrameType.DOCUMENT_START:
                continue
            if frame.frame_type == FrameType.UNIT:
                yield unit_payload_to_data(frame.payload)
            elif frame.frame_type == FrameType.WARNING:
                _warning_from_payload(frame.payload)
            elif frame.frame_type == FrameType.DONE:
                return
            elif frame.frame_type == FrameType.ERROR:
                raise OfficeWorkerError(_error_message(frame.payload))
            else:
                raise OfficeProtocolError(f"Unexpected Office worker frame: {frame.frame_type}")


def reinsert_with_worker(
    source_path: Path,
    output_path: Path,
    file_format: str,
    translations: Iterable[tuple[str, Data]] | dict[str, Data],
    target_locale: str | None,
    options: OfficeExportOptions,
    cancellation: threading.Event | None = None,
) -> OfficeExportResult:
    source_items = translations.items() if isinstance(translations, dict) else translations
    items = iter(source_items)
    try:
        _validate_translation_limits(options)
        return _reinsert_with_worker_items(
            source_path,
            output_path,
            file_format,
            items,
            target_locale,
            options,
            cancellation,
        )
    finally:
        _close_iterator(items)


def _reinsert_with_worker_items(
    source_path: Path,
    output_path: Path,
    file_format: str,
    items: Iterator[tuple[str, Data]],
    target_locale: str | None,
    options: OfficeExportOptions,
    cancellation: threading.Event | None,
) -> OfficeExportResult:
    request_id = uuid.uuid4()
    with _worker_request(options, cancellation) as session:
        session.write_frame(
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
            cancellation=cancellation,
        )
        bytes_seen = 0
        for units_seen, (unit_id, data) in enumerate(items, 1):
            raise_if_cancelled(cancellation)
            if units_seen > options.max_translation_units:
                raise OfficeReinsertionError(
                    f"Office translations exceed max_translation_units ({options.max_translation_units})"
                )
            target = _translation_text(data, target_locale)
            if len(target) > options.max_text_unit_chars:
                raise OfficeReinsertionError("Office translation exceeds max_text_unit_chars")
            bytes_seen += _translation_size(unit_id, data.source, target)
            if bytes_seen > options.max_translation_bytes:
                raise OfficeReinsertionError(
                    f"Office translations exceed max_translation_bytes ({options.max_translation_bytes})"
                )
            session.write_frame(
                ProtocolFrame(
                    FrameType.TRANSLATION_UNIT,
                    request_id,
                    data_to_unit_payload(unit_id, data, target_locale),
                ),
                options.max_frame_bytes,
                cancellation=cancellation,
            )
        raise_if_cancelled(cancellation)
        session.write_frame(
            ProtocolFrame(FrameType.TRANSLATION_END, request_id, {"required": {}}),
            options.max_frame_bytes,
            cancellation=cancellation,
        )
        result: OfficeExportResult | None = None
        warnings: list[OfficeWarning] = []
        while True:
            frame = session.read_frame(options.max_frame_bytes, cancellation=cancellation)
            _validate_request(frame, request_id)
            if frame.frame_type == FrameType.RESULT:
                required = _required(frame.payload)
                result = OfficeExportResult(
                    output_path=output_path,
                    units_written=_int_value(required.get("units_written", 0)),
                    source_fingerprint=str(required.get("source_fingerprint", "")),
                    output_bytes=_int_value(required.get("output_bytes", 0)),
                )
            elif frame.frame_type == FrameType.WARNING:
                warnings.append(_warning_from_payload(frame.payload))
            elif frame.frame_type == FrameType.DONE:
                if result is None:
                    raise OfficeProtocolError("Office worker completed reinsertion without a result frame")
                return OfficeExportResult(
                    output_path=result.output_path,
                    units_written=result.units_written,
                    warnings=tuple(warnings),
                    source_fingerprint=result.source_fingerprint,
                    output_bytes=result.output_bytes,
                )
            elif frame.frame_type == FrameType.ERROR:
                raise OfficeWorkerError(_error_message(frame.payload))
            else:
                raise OfficeProtocolError(f"Unexpected Office worker frame: {frame.frame_type}")


@contextlib.contextmanager
def _worker_request(
    options: OfficeImportOptions,
    cancellation: threading.Event | None = None,
) -> Iterator[_WorkerSession]:
    global _PERSISTENT_WORKER

    # Keep the exact lock instance for the lifetime of this context.  A fork
    # resets the module-level lock in the child; an inherited streaming
    # generator may still finalize later and must release the old lock it
    # actually acquired, not the child's replacement.
    worker_lock = _WORKER_LOCK
    if cancellation is None:
        worker_lock.acquire()
    else:
        raise_if_cancelled(cancellation)
        while not worker_lock.acquire(timeout=_CANCELLATION_POLL_SECONDS):
            raise_if_cancelled(cancellation)
        if cancellation.is_set():
            worker_lock.release()
            raise_if_cancelled(cancellation)
    session: _WorkerSession | None = None
    try:
        started = time.monotonic()
        identity = _worker_identity()
        session = _PERSISTENT_WORKER
        if session is not None and (
            session.owner_pid != os.getpid() or session.identity != identity or session.process.poll() is not None
        ):
            _discard_worker(session)
            session = None
        if session is None:
            session = _start_worker(options, started, identity, cancellation)
            _PERSISTENT_WORKER = session
        else:
            session.configure_request(options, started)
        yield session
    except BaseException:
        # Never signal a worker owned by the parent process.  The at-fork
        # handler has already detached this child's copies of its pipes.
        if session is not None and session.owner_pid == os.getpid():
            _discard_worker(session)
        raise
    finally:
        worker_lock.release()


def _worker_identity() -> tuple[Path, str]:
    info = load_runtime_info()
    return executable_path().resolve(), info.sha256


def _start_worker(
    options: OfficeImportOptions,
    started: float,
    identity: tuple[Path, str],
    cancellation: threading.Event | None,
) -> _WorkerSession:
    executable, expected_sha256 = identity
    validate_executable_digest(executable, expected_sha256)
    process = subprocess.Popen(
        [str(executable)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Buffered pipe locks can be inherited while held by the stderr
        # drainer, permanently deadlocking an at-fork child during cleanup.
        # Protocol I/O is framed already, so buffering offers no benefit; the
        # read/write helpers below explicitly handle legal short operations.
        bufsize=0,
    )
    if process.stderr is None:
        _terminate_worker(process)
        raise OfficeWorkerError("Office worker stderr is unavailable")
    stderr_stream = cast("BinaryIO", process.stderr)
    session = _WorkerSession(
        process=process,
        runner=_OperationRunner(),
        stderr_drainer=_StderrDrainer(stderr_stream),
        identity=identity,
        owner_pid=os.getpid(),
        total_deadline=started + options.timeout_seconds,
        startup_deadline=started + options.startup_timeout_seconds,
        idle_timeout_seconds=options.idle_timeout_seconds,
    )
    request_id = uuid.uuid4()
    try:
        session.write_frame(
            _hello_frame(request_id),
            options.max_frame_bytes,
            startup=True,
            cancellation=cancellation,
        )
        _expect_frame(
            session,
            FrameType.READY,
            request_id,
            options.max_frame_bytes,
            startup=True,
            cancellation=cancellation,
        )
    except BaseException:
        session.terminate()
        raise
    return session


def _discard_worker(session: _WorkerSession) -> None:
    global _PERSISTENT_WORKER

    if _PERSISTENT_WORKER is session:
        _PERSISTENT_WORKER = None
    session.terminate()


def _shutdown_worker() -> None:
    global _PERSISTENT_WORKER

    acquired = _WORKER_LOCK.acquire(timeout=1.0)
    if not acquired:
        return
    try:
        session = _PERSISTENT_WORKER
        _PERSISTENT_WORKER = None
        if session is not None:
            session.close()
    finally:
        _WORKER_LOCK.release()


def _persistent_worker_pid() -> int | None:
    with _WORKER_LOCK:
        session = _PERSISTENT_WORKER
        if session is None or session.owner_pid != os.getpid() or session.process.poll() is not None:
            return None
        return session.process.pid


def _reset_worker_after_fork() -> None:
    global _PERSISTENT_WORKER, _WORKER_LOCK

    session = _PERSISTENT_WORKER
    _PERSISTENT_WORKER = None
    _WORKER_LOCK = threading.Lock()
    if session is not None:
        session.detach_after_fork()


atexit.register(_shutdown_worker)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_worker_after_fork)


def _hello_frame(request_id: uuid.UUID) -> ProtocolFrame:
    return ProtocolFrame(
        FrameType.HELLO,
        request_id,
        {
            "required": {
                "client": "lokit-python",
                "client_version": "0.5.3",
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
    encoded = encode_frame(frame, max_frame_bytes)
    remaining = memoryview(encoded)
    while remaining:
        written = process.stdin.write(remaining)
        if written is None or written <= 0:
            raise OfficeWorkerError("Office worker stdin ended unexpectedly")
        remaining = remaining[written:]
    process.stdin.flush()


def _read_frame(
    process: subprocess.Popen[bytes],
    stderr_drainer: _StderrDrainer,
    max_frame_bytes: int,
) -> ProtocolFrame:
    if process.stdout is None:
        raise OfficeWorkerError("Office worker stdout is unavailable")
    header = _read_exact(process, stderr_drainer, 32)
    payload_length = int.from_bytes(header[28:32], "big")
    payload = _read_exact(process, stderr_drainer, payload_length)
    return decode_frame(header + payload, max_frame_bytes)


def _read_exact(
    process: subprocess.Popen[bytes],
    stderr_drainer: _StderrDrainer,
    length: int,
) -> bytes:
    if process.stdout is None:
        raise OfficeWorkerError("Office worker stdout is unavailable")
    remaining = length
    chunks: list[bytes] = []
    while remaining > 0:
        raw_data: object = process.stdout.read(remaining)
        if not isinstance(raw_data, bytes) or not raw_data:
            stderr = stderr_drainer.text(wait_for_eof=True)
            raise OfficeWorkerError(f"Office worker ended unexpectedly: {stderr}")
        chunks.append(raw_data)
        remaining -= len(raw_data)
    if len(chunks) == 1:
        return chunks[0]
    return b"".join(chunks)


def _expect_frame(
    session: _WorkerSession,
    frame_type: int,
    request_id: uuid.UUID,
    max_frame_bytes: int,
    *,
    startup: bool = False,
    cancellation: threading.Event | None = None,
) -> ProtocolFrame:
    frame = session.read_frame(max_frame_bytes, startup=startup, cancellation=cancellation)
    _validate_request(frame, request_id)
    if frame.frame_type != frame_type:
        raise OfficeProtocolError(f"Expected Office worker frame {frame_type}, got {frame.frame_type}")
    return frame


def _validate_request(frame: ProtocolFrame, request_id: uuid.UUID) -> None:
    if frame.request_id != request_id:
        raise OfficeProtocolError("Office worker request ID mismatch")


def _terminate_worker(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


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


def _warning_from_payload(payload: dict[str, object]) -> OfficeWarning:
    required = _required(payload)
    code = required.get("code")
    message = required.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        raise OfficeProtocolError("Office worker warning is missing code or message")
    optional = payload.get("optional")
    if optional is None:
        optional = {}
    if not isinstance(optional, dict):
        raise OfficeProtocolError("Office worker warning optional value is not an object")
    extensions_value = optional.get("extensions")
    if extensions_value is None:
        extensions: dict[str, str] = {}
    elif isinstance(extensions_value, dict):
        extensions = {}
        for key, value in extensions_value.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise OfficeProtocolError("Office worker warning extension is not a string pair")
            extensions[key] = value
    else:
        raise OfficeProtocolError("Office worker warning extensions value is not an object")
    return OfficeWarning(
        code=code,
        message=message,
        unit_id=_optional_string(optional, "unit_id"),
        part=_optional_string(optional, "part"),
        extensions=extensions,
    )


def _optional_string(values: dict[str, object], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise OfficeProtocolError(f"Office worker optional field is not a string: {key}")
    return value


def _options_payload(options: OfficeImportOptions) -> dict[str, object]:
    payload: dict[str, object] = {
        "max_frame_bytes": options.max_frame_bytes,
        "max_unit_bytes": options.max_unit_bytes,
        "max_zip_entries": options.max_zip_entries,
        "max_compressed_bytes": options.max_compressed_bytes,
        "max_uncompressed_bytes": options.max_uncompressed_bytes,
        "max_compression_ratio": options.max_compression_ratio,
        "max_text_unit_chars": options.max_text_unit_chars,
        "include_headers_footers": options.include_headers_footers,
        "include_comments": options.include_comments,
        "include_slides": options.include_slides,
        "include_speaker_notes": options.include_speaker_notes,
        "include_notes": options.include_notes,
        "include_slide_masters": options.include_slide_masters,
        "include_slide_layouts": options.include_slide_layouts,
        "include_notes_masters": options.include_notes_masters,
        "include_handout_masters": options.include_handout_masters,
        "include_master_layout_content": options.include_master_layout_content,
        "include_alt_text": options.include_alt_text,
        "include_charts": options.include_charts,
        "include_diagrams": options.include_diagrams,
        "include_document_metadata": options.include_document_metadata,
        "include_hidden_slides": options.include_hidden_slides,
    }
    if isinstance(options, OfficeExportOptions):
        payload["max_translation_units"] = options.max_translation_units
        payload["max_translation_bytes"] = options.max_translation_bytes
        payload["missing_translation_policy"] = options.missing_translation_policy.value
        payload["extra_translation_policy"] = options.extra_translation_policy.value
    return payload


def _validate_translation_limits(options: OfficeExportOptions) -> None:
    if options.max_text_unit_chars < 1:
        raise OfficeReinsertionError("max_text_unit_chars must be at least 1")
    if options.max_translation_units < 1:
        raise OfficeReinsertionError("max_translation_units must be at least 1")
    if options.max_translation_bytes < 1:
        raise OfficeReinsertionError("max_translation_bytes must be at least 1")


def _translation_text(data: Data, target_locale: str | None) -> str:
    target = data.target
    if target_locale and target_locale in data.targets:
        target = data.targets[target_locale].text
    return target or ""


def _translation_size(unit_id: str, source: str, target: str) -> int:
    return len(unit_id.encode("utf-8")) + len(source.encode("utf-8")) + len(target.encode("utf-8"))


def _close_iterator(items: Iterator[tuple[str, Data]]) -> None:
    candidate: object = items
    if hasattr(candidate, "close"):
        cast("_ClosableIterator", candidate).close()


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return 0
