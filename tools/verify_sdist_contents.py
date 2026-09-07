from __future__ import annotations

import json
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

BINARY_SUFFIXES = (
    ".a",
    ".com",
    ".dll",
    ".dylib",
    ".exe",
    ".lib",
    ".msi",
    ".o",
    ".obj",
    ".pyd",
    ".so",
)
BINARY_MAGICS = (
    b"\x7fELF",
    b"!<arch>\n",
    b"\xca\xfe\xba\xbe",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
)
FORBIDDEN_PARTS = frozenset(
    {
        ".sdist-smoke",
        ".sdist-source",
        "__pycache__",
        "build",
        "dist",
        "sdist-wheel",
        "target",
    }
)
REQUIRED_FILES = frozenset(
    {
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "native/interchange/Cargo.lock",
        "native/interchange/Cargo.toml",
        "native/interchange/build.rs",
        "native/interchange/src/lib.rs",
        "native/interchange/src/detection.rs",
        "native/interchange/src/archive_probe.rs",
        "native/interchange/src/identity.rs",
        "native/interchange/src/input.rs",
        "native/interchange/src/json.rs",
        "native/interchange/src/materialize.rs",
        "native/interchange/src/plural.rs",
        "native/interchange/src/rows.rs",
        "native/interchange/src/semantic.rs",
        "native/interchange/src/tabular.rs",
        "native/interchange/src/lokit.rs",
        "native/interchange/src/po.rs",
        "native/lokit-format/Cargo.lock",
        "native/lokit-format/Cargo.toml",
        "native/lokit-format/src/id_registry.rs",
        "native/lokit-format/src/lib.rs",
        "native/lokit-format/src/memory.rs",
        "pyproject.toml",
        "setup.py",
        "src/lokit/_interchange_rust.pyi",
        "src/lokit/licenses/archive-dependencies.txt",
        "tools/benchmark_format_detection.py",
        "tools/benchmark_byte_input.py",
        "src/lokit/data/dict_projection.py",
        "src/lokit/data/interchange_types.py",
        "src/lokit/database/schema.py",
        "src/lokit/database/serialization.py",
        "src/lokit/office/process.py",
        "src/lokit/office/runtime.py",
        "src/lokit_office_runtime/runtime.json",
        "tools/benchmark_database.py",
        "tools/benchmark_interchange_api.py",
        "tools/benchmark_lokit_format.py",
        "tools/smoke_test_wheel.py",
        "tools/stage_office_runtime.py",
        "tools/verify_mypyc_install.py",
        "tools/verify_native_install.py",
        "tools/verify_release_versions.py",
        "tools/verify_sdist_contents.py",
    }
)


def _relative_member(name: str) -> tuple[str, PurePosixPath]:
    if "\\" in name:
        raise RuntimeError(f"Unsafe source-distribution member: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Unsafe source-distribution member: {name!r}")
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if not parts:
        raise RuntimeError("Source distribution contains an empty member path")
    root = parts[0]
    relative = PurePosixPath(*parts[1:])
    return root, relative


def _file_prefix(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    stream = archive.extractfile(member)
    if stream is None:
        raise RuntimeError(f"Could not inspect source-distribution member: {member.name!r}")
    with stream:
        return stream.read(4096)


def _is_compiled_artifact(relative: PurePosixPath, prefix: bytes) -> bool:
    name = relative.name.lower()
    return (
        name.endswith(BINARY_SUFFIXES)
        or ".so." in name
        or _has_pe_header(prefix)
        or any(prefix.startswith(magic) for magic in BINARY_MAGICS)
    )


def _has_pe_header(prefix: bytes) -> bool:
    if len(prefix) < 64 or not prefix.startswith(b"MZ"):
        return False
    header_offset = int.from_bytes(prefix[60:64], byteorder="little")
    return header_offset <= len(prefix) - 4 and prefix[header_offset : header_offset + 4] == b"PE\x00\x00"


def _is_executable_script(prefix: bytes) -> bool:
    first_line = prefix.partition(b"\n")[0]
    return first_line.startswith(b"#!") and b"\x00" not in first_line


def _safe_output_path(destination: Path, member_name: str) -> Path:
    candidate = destination.joinpath(*PurePosixPath(member_name).parts)
    resolved_destination = destination.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate != resolved_destination and resolved_destination not in resolved_candidate.parents:
        raise RuntimeError(f"Unsafe extraction destination for source-distribution member: {member_name!r}")
    return candidate


def _extract_sdist(archive: tarfile.TarFile, members: Sequence[tarfile.TarInfo], destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise RuntimeError(f"Extraction destination already exists: {destination}")
    destination.mkdir(parents=True)
    for member in members:
        output = _safe_output_path(destination, member.name)
        if member.isdir():
            output.mkdir(parents=True, exist_ok=True)
            output.chmod(0o755)
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        stream = archive.extractfile(member)
        if stream is None:
            raise RuntimeError(f"Could not extract source-distribution member: {member.name!r}")
        with stream, output.open("xb") as target:
            shutil.copyfileobj(stream, target)
        output.chmod(0o755 if member.mode & 0o111 else 0o644)


def verify_sdist(path: Path, extract_to: Path | None = None) -> None:
    if not path.is_file():
        raise RuntimeError(f"Source distribution does not exist: {path}")
    roots: set[str] = set()
    entries: set[str] = set()
    files: set[str] = set()
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            root, relative = _relative_member(member.name)
            roots.add(root)
            if not relative.parts:
                if not member.isdir():
                    raise RuntimeError("The source-distribution root entry must be a directory")
                continue
            relative_name = relative.as_posix()
            if relative_name in entries:
                raise RuntimeError(f"Duplicate source-distribution member: {relative_name}")
            entries.add(relative_name)
            if not member.isfile() and not member.isdir():
                raise RuntimeError(
                    f"Source distribution must contain only regular files and directories: {relative_name}"
                )
            if member.isfile():
                files.add(relative_name)
                prefix = _file_prefix(archive, member)
                if relative_name == "src/lokit_office_runtime/runtime.json":
                    metadata: object = json.loads(prefix)
                    if not isinstance(metadata, dict) or any(
                        metadata.get(key) for key in ("rid", "build_commit", "sha256")
                    ):
                        raise RuntimeError("Source distribution contains platform-specific Office runtime metadata")
                if _is_compiled_artifact(relative, prefix):
                    raise RuntimeError(f"Compiled artifact in source distribution: {relative_name}")
                if member.mode & 0o111 and not _is_executable_script(prefix):
                    raise RuntimeError(f"Unexpected executable file in source distribution: {relative_name}")
            if relative_name == "tools/lokit-lsp" or relative_name.startswith("tools/lokit-lsp/"):
                raise RuntimeError("The standalone language server must not ship in the Python source distribution")
            if any(part in FORBIDDEN_PARTS for part in relative.parts):
                raise RuntimeError(f"Forbidden generated directory in source distribution: {relative_name}")
        if len(roots) != 1:
            raise RuntimeError(f"Source distribution must have one top-level directory, found {sorted(roots)}")
        missing = sorted(REQUIRED_FILES.difference(files))
        if missing:
            raise RuntimeError(f"Source distribution is missing required files: {missing}")
        if extract_to is not None:
            _extract_sdist(archive, members, extract_to)
    print(f"verified source distribution with {len(files)} files and no generated binaries: {path}")
    if extract_to is not None:
        print(f"safely extracted source distribution to: {extract_to}")


def main(arguments: Sequence[str]) -> None:
    if len(arguments) == 1:
        verify_sdist(Path(arguments[0]))
        return
    if len(arguments) == 3 and arguments[1] == "--extract-to":
        verify_sdist(Path(arguments[0]), Path(arguments[2]))
        return
    raise SystemExit("usage: verify_sdist_contents.py SDIST.tar.gz [--extract-to DIRECTORY]")


if __name__ == "__main__":
    main(tuple(sys.argv[1:]))
