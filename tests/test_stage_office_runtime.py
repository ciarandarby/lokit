from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
from pathlib import Path
from typing import cast

SCRIPT = Path(__file__).parents[1] / "tools/stage_office_runtime.py"


def _stage(binary: Path, rid: str, build_commit: str, package: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, str(SCRIPT), str(binary), rid, build_commit, str(package)),
        check=False,
        capture_output=True,
        text=True,
    )


def test_stage_runtime_writes_atomic_platform_metadata(tmp_path: Path) -> None:
    binary = tmp_path / "worker"
    binary.write_bytes(b"native-office-worker")
    package = tmp_path / "runtime"

    result = _stage(binary, "linux-x64", "abc123", package)

    metadata_value: object = json.loads((package / "runtime.json").read_text(encoding="utf-8"))
    assert isinstance(metadata_value, dict)
    metadata = cast("dict[object, object]", metadata_value)
    destination = package / "bin/lokit-office"
    assert result.returncode == 0, result.stderr
    assert destination.read_bytes() == binary.read_bytes()
    assert destination.stat().st_mode & stat.S_IXUSR
    assert metadata["worker_version"] == "0.5.3"
    assert metadata["rid"] == "linux-x64"
    assert metadata["build_commit"] == "abc123"
    assert metadata["openxml_sdk_version"] == "3.5.1"
    assert metadata["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()


def test_stage_runtime_replaces_opposite_platform_binary(tmp_path: Path) -> None:
    binary = tmp_path / "worker.exe"
    binary.write_bytes(b"windows-office-worker")
    package = tmp_path / "runtime"
    bin_directory = package / "bin"
    bin_directory.mkdir(parents=True)
    (bin_directory / "lokit-office").write_bytes(b"stale")

    result = _stage(binary, "win-x64", "", package)

    destination = bin_directory / "lokit-office.exe"
    assert result.returncode == 0, result.stderr
    assert destination.read_bytes() == binary.read_bytes()
    assert not (bin_directory / "lokit-office").exists()


def test_stage_runtime_rejects_unknown_rid(tmp_path: Path) -> None:
    binary = tmp_path / "worker"
    binary.write_bytes(b"worker")

    result = _stage(binary, "linux-arm64", "", tmp_path / "runtime")

    assert result.returncode == 1
    assert "Unsupported Office runtime identifier" in result.stderr
