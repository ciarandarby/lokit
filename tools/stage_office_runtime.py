from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).parents[1]
SUPPORTED_RIDS = frozenset({"linux-x64", "osx-arm64", "osx-x64", "win-x64"})


class RuntimeMetadata(TypedDict):
    worker_version: str
    protocol_major: int
    protocol_minor: int
    rid: str
    build_commit: str
    openxml_sdk_version: str
    sha256: str


def _single_match(path: Path, pattern: re.Pattern[str], label: str) -> str:
    matches = cast("list[str]", pattern.findall(path.read_text(encoding="utf-8")))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {label} in {path}, found {len(matches)}")
    return matches[0]


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_runtime(binary: Path, rid: str, build_commit: str, package: Path) -> Path:
    if rid not in SUPPORTED_RIDS:
        raise ValueError(f"Unsupported Office runtime identifier: {rid}")
    if not binary.is_file():
        raise FileNotFoundError(f"Office worker binary does not exist: {binary}")

    worker_version = _single_match(
        ROOT / "pyproject.toml",
        re.compile(r'^version = "([^"]+)"$', re.MULTILINE),
        "project version",
    )
    openxml_version = _single_match(
        ROOT / "src/office/Directory.Packages.props",
        re.compile(r'PackageVersion Include="DocumentFormat\.OpenXml" Version="([^"]+)"'),
        "Open XML SDK version",
    )
    executable_name = "lokit-office.exe" if rid == "win-x64" else "lokit-office"
    opposite_name = "lokit-office" if executable_name.endswith(".exe") else "lokit-office.exe"
    bin_directory = package / "bin"
    bin_directory.mkdir(parents=True, exist_ok=True)
    destination = bin_directory / executable_name
    temporary_binary = bin_directory / f".{executable_name}.tmp"
    shutil.copyfile(binary, temporary_binary)
    if rid != "win-x64":
        temporary_binary.chmod(temporary_binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    temporary_binary.replace(destination)
    (bin_directory / opposite_name).unlink(missing_ok=True)

    metadata: RuntimeMetadata = {
        "worker_version": worker_version,
        "protocol_major": 1,
        "protocol_minor": 0,
        "rid": rid,
        "build_commit": build_commit,
        "openxml_sdk_version": openxml_version,
        "sha256": _digest(destination),
    }
    metadata_path = package / "runtime.json"
    temporary_metadata = package / ".runtime.json.tmp"
    temporary_metadata.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    temporary_metadata.replace(metadata_path)
    return destination


def main(arguments: Sequence[str]) -> None:
    if len(arguments) not in (2, 3, 4):
        raise SystemExit("usage: stage_office_runtime.py BINARY RID [BUILD_COMMIT [PACKAGE]]")
    build_commit = arguments[2] if len(arguments) >= 3 else ""
    package = Path(arguments[3]) if len(arguments) == 4 else ROOT / "src/lokit_office_runtime"
    destination = stage_runtime(
        Path(arguments[0]),
        arguments[1],
        build_commit,
        package,
    )
    print(destination)


if __name__ == "__main__":
    main(tuple(sys.argv[1:]))
