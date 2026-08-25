from __future__ import annotations

import hashlib
import json
from importlib import import_module, resources
from pathlib import Path

from lokit.office.errors import OfficeProtocolVersionError, OfficeRuntimeUnavailable
from lokit.office.models import OfficeRuntimeInfo

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
PYTHON_FALLBACK_RUNTIME = OfficeRuntimeInfo(
    worker_version="python-fallback",
    protocol_major=PROTOCOL_MAJOR,
    protocol_minor=PROTOCOL_MINOR,
)


def load_runtime_info() -> OfficeRuntimeInfo:
    try:
        runtime = import_module("lokit_office_runtime")
    except ModuleNotFoundError:
        return PYTHON_FALLBACK_RUNTIME

    metadata = _load_runtime_json(runtime.__name__)
    info = OfficeRuntimeInfo(
        worker_version=str(metadata.get("worker_version", "")),
        protocol_major=_metadata_int(metadata, "protocol_major"),
        protocol_minor=_metadata_int(metadata, "protocol_minor"),
        rid=str(metadata.get("rid", "")),
        build_commit=str(metadata.get("build_commit", "")),
        openxml_sdk_version=str(metadata.get("openxml_sdk_version", "")),
        sha256=str(metadata.get("sha256", "")),
    )
    if info.protocol_major != PROTOCOL_MAJOR:
        raise OfficeProtocolVersionError(
            f"Office runtime protocol {info.protocol_major}.{info.protocol_minor} "
            f"is incompatible with client protocol {PROTOCOL_MAJOR}.{PROTOCOL_MINOR}"
        )
    return info


def executable_path() -> Path:
    import os

    env_path = os.environ.get("LOKIT_OFFICE_WORKER")
    if env_path:
        path = Path(env_path)
        if not path.exists():
            raise OfficeRuntimeUnavailable(f"Office worker executable not found: {path}")
        return path

    try:
        runtime = import_module("lokit_office_runtime")
    except ModuleNotFoundError as exc:
        raise OfficeRuntimeUnavailable(
            "Install lokit-python[office] to use the external Office worker runtime."
        ) from exc

    if hasattr(runtime, "executable_path"):
        return Path(runtime.executable_path())

    candidate = resources.files(runtime.__name__) / "bin" / _executable_name()
    with resources.as_file(candidate) as path:
        if not path.exists():
            raise OfficeRuntimeUnavailable(f"Office worker executable not found: {path}")
        return path


def validate_executable_digest(path: Path, expected_sha256: str) -> None:
    if not expected_sha256:
        return
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest().lower() != expected_sha256.lower():
        raise OfficeRuntimeUnavailable("Office worker executable digest mismatch")


def _load_runtime_json(package: str) -> dict[str, object]:
    try:
        resource = resources.files(package) / "runtime.json"
        parsed = json.loads(resource.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OfficeRuntimeUnavailable("Office runtime metadata is missing") from exc
    if not isinstance(parsed, dict):
        raise OfficeRuntimeUnavailable("Office runtime metadata must be a JSON object")
    return {str(key): value for key, value in parsed.items()}


def _metadata_int(metadata: dict[str, object], key: str) -> int:
    value = metadata.get(key, 0)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return 0


def _executable_name() -> str:
    import os

    return "lokit-office.exe" if os.name == "nt" else "lokit-office"
