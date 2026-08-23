from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).parents[1]
PROJECT = ROOT / "src/office/Lokit.Office.Worker/Lokit.Office.Worker.csproj"
STAGE_SCRIPT = ROOT / "tools/stage_office_runtime.py"
SUPPORTED_RIDS = frozenset({"linux-x64", "osx-arm64", "osx-x64", "win-x64"})


def _run(command: Sequence[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def build_runtime(rid: str, build_commit: str, native_library_path: str) -> Path:
    if rid not in SUPPORTED_RIDS:
        raise ValueError(f"Unsupported Office runtime identifier: {rid}")
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        raise RuntimeError("The .NET SDK is required to build the Office runtime")
    _run(
        (
            dotnet,
            "restore",
            str(PROJECT),
            "--locked-mode",
            "-p:PublishAot=true",
        )
    )
    with TemporaryDirectory(prefix="lokit-office-") as temporary_directory:
        output = Path(temporary_directory)
        command = [
            dotnet,
            "publish",
            str(PROJECT),
            "--configuration",
            "Release",
            "--runtime",
            rid,
            "--self-contained",
            "true",
            "--no-restore",
            "-p:PublishAot=true",
            "-p:StripSymbols=true",
            "-p:DebugType=None",
            "-p:DebugSymbols=false",
            "--output",
            str(output),
        ]
        if native_library_path:
            command.extend(
                (
                    f"-p:NativeOpenSslLibraryPath={native_library_path}",
                    f"-p:NativeBrotliLibraryPath={native_library_path}",
                )
            )
        _run(command)
        binary_name = "Lokit.Office.Worker.exe" if rid == "win-x64" else "Lokit.Office.Worker"
        _run(
            (
                sys.executable,
                str(STAGE_SCRIPT),
                str(output / binary_name),
                rid,
                build_commit,
            )
        )
    return ROOT / "src/lokit_office_runtime/bin" / ("lokit-office.exe" if rid == "win-x64" else "lokit-office")


def main(arguments: Sequence[str]) -> None:
    if len(arguments) not in (1, 2, 3):
        raise SystemExit("usage: build_office_runtime.py RID [BUILD_COMMIT [NATIVE_LIBRARY_PATH]]")
    build_commit = arguments[1] if len(arguments) >= 2 else ""
    native_library_path = arguments[2] if len(arguments) == 3 else ""
    print(build_runtime(arguments[0], build_commit, native_library_path))


if __name__ == "__main__":
    main(tuple(sys.argv[1:]))
