from __future__ import annotations

import os
import shutil
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from lokit.data.structure import Data
from lokit.office import import_docx
from lokit.office.errors import OfficePackageError, OfficeTimeoutError, OfficeUnsupportedPackageError, OfficeWorkerError
from lokit.office.options import OfficeExportOptions, OfficeImportOptions
from lokit.office.process import _shutdown_worker, extract_with_worker, reinsert_with_worker

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def shutdown_worker() -> Iterator[None]:
    yield
    _shutdown_worker()


@pytest.fixture
def fake_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if os.name == "nt":
        pytest.skip("Fake executable worker test requires a POSIX shebang")
    path = tmp_path / "fake-office-worker"
    path.write_text(
        f"""#!{sys.executable}
import json
import os
import struct
import sys
import time

HEADER = struct.Struct(\">4sHHHH16sI\")


def read_exact(length):
    data = bytearray()
    while len(data) < length:
        chunk = sys.stdin.buffer.read(length - len(data))
        if not chunk:
            raise SystemExit(2)
        data.extend(chunk)
    return bytes(data)


def read_frame():
    header = read_exact(HEADER.size)
    magic, major, minor, frame_type, flags, request_id, payload_length = HEADER.unpack(header)
    payload = json.loads(read_exact(payload_length).decode(\"utf-8\"))
    return frame_type, request_id, payload


def write_frame(frame_type, request_id, payload):
    encoded = json.dumps(payload, separators=(\",\", \":\")).encode(\"utf-8\")
    sys.stdout.buffer.write(HEADER.pack(b\"LOK1\", 1, 0, frame_type, 1, request_id, len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


pid_path = os.environ.get(\"LOKIT_FAKE_PID_PATH\")
if pid_path:
    with open(pid_path, \"w\", encoding=\"utf-8\") as stream:
        stream.write(str(os.getpid()))

mode = os.environ[\"LOKIT_FAKE_WORKER_MODE\"]
if mode == \"stderr-flood\" or mode == \"stderr-flood-exit\":
    for _ in range(512):
        sys.stderr.buffer.write(b\"diagnostic-noise-\" + b\"x\" * 4079)
    sys.stderr.buffer.write(b\"stderr-tail-marker\")
    sys.stderr.buffer.flush()
    if mode == \"stderr-flood-exit\":
        raise SystemExit(17)
if mode == \"startup-stall\":
    time.sleep(30)
    raise SystemExit(0)

_, hello_id, _ = read_frame()
write_frame(0x0002, hello_id, {{\"required\": {{\"protocol_major\": 1, \"protocol_minor\": 0}}}})
hello_count_path = os.environ.get(\"LOKIT_FAKE_HELLO_COUNT_PATH\")
if hello_count_path:
    with open(hello_count_path, \"a\", encoding=\"utf-8\") as stream:
        stream.write(\"hello\\n\")

while True:
    request_type, request_id, request = read_frame()
    request_ids_path = os.environ.get(\"LOKIT_FAKE_REQUEST_IDS_PATH\")
    if request_ids_path:
        with open(request_ids_path, \"a\", encoding=\"utf-8\") as stream:
            stream.write(request_id.hex() + \"\\n\")

    if mode in {{\"idle-stall\", \"total-stall\"}}:
        time.sleep(30)
        raise SystemExit(0)

    if request_type == 0x0004:
        while read_frame()[0] != 0x0006:
            pass
        write_frame(
            0x0012,
            request_id,
            {{
                \"required\": {{\"code\": \"office.test_warning\", \"message\": \"test warning\"}},
                \"optional\": {{
                    \"unit_id\": \"docx:body:p/0\",
                    \"part\": \"word/document.xml\",
                    \"extensions\": {{\"detail\": \"fake\"}},
                }},
            }},
        )
        write_frame(
            0x0014,
            request_id,
            {{
                \"required\": {{
                    \"units_written\": 1,
                    \"source_fingerprint\": \"sha256:worker\",
                    \"output_bytes\": 17,
                }}
            }},
        )
        write_frame(0x0016, request_id, {{\"required\": {{\"units\": 1}}}})
    else:
        write_frame(
            0x0010,
            request_id,
            {{\"required\": {{\"format\": \"docx\", \"source_fingerprint\": \"sha256:worker\"}}}},
        )
        write_frame(0x0012, request_id, {{\"required\": {{\"code\": \"office.test\", \"message\": \"warning\"}}}})
        write_frame(0x0016, request_id, {{\"required\": {{\"units\": 0}}}})
    if mode == \"exit-after-done\":
        raise SystemExit(0)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    monkeypatch.setenv("LOKIT_OFFICE_WORKER", str(path))
    return path


@pytest.mark.parametrize(
    ("mode", "options", "message"),
    [
        (
            "startup-stall",
            OfficeImportOptions(timeout_seconds=2.0, startup_timeout_seconds=0.25, idle_timeout_seconds=1.0),
            "startup timeout",
        ),
        (
            "idle-stall",
            OfficeImportOptions(timeout_seconds=2.0, startup_timeout_seconds=1.0, idle_timeout_seconds=0.1),
            "idle timeout",
        ),
        (
            "total-stall",
            OfficeImportOptions(timeout_seconds=0.5, startup_timeout_seconds=1.0, idle_timeout_seconds=1.0),
            "total timeout",
        ),
    ],
)
def test_worker_deadlines_terminate_stalled_process(
    fake_worker: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    options: OfficeImportOptions,
    message: str,
) -> None:
    del fake_worker
    pid_path = tmp_path / "worker.pid"
    monkeypatch.setenv("LOKIT_FAKE_WORKER_MODE", mode)
    monkeypatch.setenv("LOKIT_FAKE_PID_PATH", str(pid_path))

    started = time.monotonic()
    with pytest.raises(OfficeTimeoutError, match=message):
        extract_with_worker(tmp_path / "source.docx", "docx", "en", None, options)

    assert time.monotonic() - started < 2.0
    if pid_path.exists():
        pid = int(pid_path.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_worker_continuously_drains_large_stderr_without_deadlock(
    fake_worker: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_worker
    monkeypatch.setenv("LOKIT_FAKE_WORKER_MODE", "stderr-flood")

    fingerprint, units = extract_with_worker(
        tmp_path / "source.docx",
        "docx",
        "en",
        None,
        OfficeImportOptions(timeout_seconds=2.0, startup_timeout_seconds=1.0),
    )

    assert fingerprint == "sha256:worker"
    assert units == []


def test_worker_failure_retains_only_bounded_stderr_tail(
    fake_worker: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_worker
    monkeypatch.setenv("LOKIT_FAKE_WORKER_MODE", "stderr-flood-exit")

    with pytest.raises(OfficeWorkerError) as caught:
        extract_with_worker(
            tmp_path / "source.docx",
            "docx",
            "en",
            None,
            OfficeImportOptions(timeout_seconds=2.0, startup_timeout_seconds=1.0),
        )

    message = str(caught.value)
    assert "stderr-tail-marker" in message
    assert len(message.encode("utf-8")) < 70_000


def test_worker_warning_and_actual_result_are_decoded(
    fake_worker: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_worker
    monkeypatch.setenv("LOKIT_FAKE_WORKER_MODE", "result")

    result = reinsert_with_worker(
        source_path=tmp_path / "source.docx",
        output_path=tmp_path / "output.docx",
        file_format="docx",
        translations={
            "docx:body:p/0": Data(source="source", target="target"),
            "docx:body:p/1": Data(source="unused", target="unused"),
        },
        target_locale="fr",
        options=OfficeExportOptions(),
    )

    assert result.units_written == 1
    assert result.source_fingerprint == "sha256:worker"
    assert result.output_bytes == 17
    assert result.warnings[0].code == "office.test_warning"
    assert result.warnings[0].unit_id == "docx:body:p/0"
    assert result.warnings[0].part == "word/document.xml"
    assert result.warnings[0].extensions == {"detail": "fake"}


def test_extract_accepts_worker_warning_frames(
    fake_worker: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_worker
    monkeypatch.setenv("LOKIT_FAKE_WORKER_MODE", "result")

    fingerprint, units = extract_with_worker(
        tmp_path / "source.docx",
        "docx",
        "en",
        None,
        OfficeImportOptions(),
    )

    assert fingerprint == "sha256:worker"
    assert units == []


def test_worker_session_reuses_handshake_and_distinct_request_ids(
    fake_worker: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_worker
    hello_count = tmp_path / "hello-count"
    request_ids = tmp_path / "request-ids"
    pid_path = tmp_path / "worker.pid"
    monkeypatch.setenv("LOKIT_FAKE_WORKER_MODE", "result")
    monkeypatch.setenv("LOKIT_FAKE_HELLO_COUNT_PATH", str(hello_count))
    monkeypatch.setenv("LOKIT_FAKE_REQUEST_IDS_PATH", str(request_ids))
    monkeypatch.setenv("LOKIT_FAKE_PID_PATH", str(pid_path))

    for _ in range(2):
        fingerprint, units = extract_with_worker(
            tmp_path / "source.docx",
            "docx",
            "en",
            None,
            OfficeImportOptions(),
        )
        assert fingerprint == "sha256:worker"
        assert units == []

    assert hello_count.read_text(encoding="utf-8").splitlines() == ["hello"]
    ids = request_ids.read_text(encoding="utf-8").splitlines()
    assert len(ids) == 2
    assert len(set(ids)) == 2
    pid = int(pid_path.read_text(encoding="utf-8"))
    _shutdown_worker()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_worker_session_restarts_after_worker_exit(
    fake_worker: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_worker
    hello_count = tmp_path / "hello-count"
    monkeypatch.setenv("LOKIT_FAKE_WORKER_MODE", "exit-after-done")
    monkeypatch.setenv("LOKIT_FAKE_HELLO_COUNT_PATH", str(hello_count))

    extract_with_worker(tmp_path / "source.docx", "docx", "en", None, OfficeImportOptions())
    time.sleep(0.05)
    extract_with_worker(tmp_path / "source.docx", "docx", "en", None, OfficeImportOptions())

    assert hello_count.read_text(encoding="utf-8").splitlines() == ["hello", "hello"]


def test_worker_session_serializes_concurrent_requests(
    fake_worker: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_worker
    hello_count = tmp_path / "hello-count"
    monkeypatch.setenv("LOKIT_FAKE_WORKER_MODE", "result")
    monkeypatch.setenv("LOKIT_FAKE_HELLO_COUNT_PATH", str(hello_count))

    def extract(index: int) -> str:
        fingerprint, units = extract_with_worker(
            tmp_path / f"source-{index}.docx",
            "docx",
            "en",
            None,
            OfficeImportOptions(),
        )
        assert units == []
        return fingerprint

    with ThreadPoolExecutor(max_workers=4) as executor:
        fingerprints = list(executor.map(extract, range(12)))

    assert fingerprints == ["sha256:worker"] * 12
    assert hello_count.read_text(encoding="utf-8").splitlines() == ["hello"]


def test_worker_session_is_reinitialized_after_fork(
    fake_worker: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(os, "fork"):
        pytest.skip("Fork is unavailable")
    del fake_worker
    hello_count = tmp_path / "hello-count"
    monkeypatch.setenv("LOKIT_FAKE_WORKER_MODE", "result")
    monkeypatch.setenv("LOKIT_FAKE_HELLO_COUNT_PATH", str(hello_count))
    extract_with_worker(tmp_path / "parent.docx", "docx", "en", None, OfficeImportOptions())

    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        try:
            fingerprint, units = extract_with_worker(
                tmp_path / "child.docx",
                "docx",
                "en",
                None,
                OfficeImportOptions(),
            )
            outcome = b"ok" if fingerprint == "sha256:worker" and not units else b"invalid"
        except BaseException as exc:
            outcome = repr(exc).encode("utf-8", errors="replace")
        _shutdown_worker()
        os.write(write_fd, outcome)
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    outcome = os.read(read_fd, 4096)
    os.close(read_fd)
    _, status = os.waitpid(child_pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert outcome == b"ok"

    extract_with_worker(tmp_path / "parent-again.docx", "docx", "en", None, OfficeImportOptions())
    assert hello_count.read_text(encoding="utf-8").splitlines() == ["hello", "hello"]


def test_python_backend_rejects_encrypted_and_oversized_xml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    _write_minimal_docx(source)
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")

    with pytest.raises(OfficePackageError, match="max_unit_bytes"):
        import_docx(source, options=OfficeImportOptions(max_unit_bytes=128), progress=False)

    _mark_zip_encrypted(source)
    with pytest.raises(OfficeUnsupportedPackageError, match="Encrypted Office packages"):
        import_docx(source, progress=False)


def test_dotnet_worker_enforces_bounded_xml_reads_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _debug_worker_path()
    if not worker.is_file() or not _configure_debug_dotnet(monkeypatch):
        pytest.skip("Office worker has not been built")
    source = tmp_path / "bounded.docx"
    _write_minimal_docx(source)
    monkeypatch.setenv("LOKIT_OFFICE_WORKER", str(worker))

    with pytest.raises(OfficeWorkerError, match="max_unit_bytes"):
        extract_with_worker(
            source,
            "docx",
            "en",
            None,
            OfficeImportOptions(max_unit_bytes=128),
        )


def test_dotnet_worker_rejects_encrypted_zip_entries_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _debug_worker_path()
    if not worker.is_file() or not _configure_debug_dotnet(monkeypatch):
        pytest.skip("Office worker has not been built")
    source = tmp_path / "encrypted.docx"
    _write_minimal_docx(source)
    _mark_zip_encrypted(source)
    monkeypatch.setenv("LOKIT_OFFICE_WORKER", str(worker))

    with pytest.raises(OfficeWorkerError, match="Encrypted Office packages"):
        extract_with_worker(source, "docx", "en", None, OfficeImportOptions())


def test_dotnet_worker_reports_actual_units_and_warnings_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _debug_worker_path()
    if not worker.is_file() or not _configure_debug_dotnet(monkeypatch):
        pytest.skip("Office worker has not been built")
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"
    _write_minimal_docx(source)
    monkeypatch.setenv("LOKIT_OFFICE_WORKER", str(worker))

    result = reinsert_with_worker(
        source_path=source,
        output_path=output,
        file_format="docx",
        translations={
            "docx:body:p/0": Data(source="source", target="translated"),
            "docx:body:p/404": Data(source="extra", target="extra"),
        },
        target_locale="fr",
        options=OfficeExportOptions(),
    )

    assert result.units_written == 1
    assert result.output_bytes == output.stat().st_size
    assert result.source_fingerprint.startswith("sha256:")
    assert [warning.code for warning in result.warnings] == ["office.extra_translation"]
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert b"translated" in archive.read("word/document.xml")


def _debug_worker_path() -> Path:
    name = "Lokit.Office.Worker.exe" if os.name == "nt" else "Lokit.Office.Worker"
    return Path("src/office/Lokit.Office.Worker/bin/Debug/net10.0") / name


def _configure_debug_dotnet(monkeypatch: pytest.MonkeyPatch) -> bool:
    executable = shutil.which("dotnet")
    if executable is None:
        return False
    resolved = Path(executable).resolve()
    for candidate in (resolved.parent, resolved.parent.parent / "libexec"):
        if (candidate / "dotnet").is_file() and (candidate / "host").is_dir():
            monkeypatch.setenv("DOTNET_ROOT", str(candidate))
            return True
    return False


def _write_minimal_docx(path: Path) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/word/document.xml"
   ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    document = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>source</w:t></w:r></w:p></w:body>
</w:document>
"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)


def _mark_zip_encrypted(path: Path) -> None:
    data = bytearray(path.read_bytes())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = 0
        while True:
            position = data.find(signature, position)
            if position < 0:
                break
            offset = position + flag_offset
            flags = int.from_bytes(data[offset : offset + 2], "little") | 0x1
            data[offset : offset + 2] = flags.to_bytes(2, "little")
            position += len(signature)
    path.write_bytes(data)
