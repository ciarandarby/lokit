from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).parents[1]
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.+-]*)?$")


def _section_value(path: Path, section: str, key: str) -> str:
    current_section = ""
    matches: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            continue
        if current_section != section or not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator and name.strip() == key:
            matches.append(value.strip())
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {key!r} value in [{section}] of {path}, found {len(matches)}")
    return matches[0]


def _quoted_value(raw: str, label: str) -> str:
    if len(raw) < 2 or raw[0] not in {'"', "'"} or raw[-1] != raw[0]:
        raise RuntimeError(f"{label} must be a quoted string")
    value = raw[1:-1]
    if not value:
        raise RuntimeError(f"{label} must not be empty")
    return value


def _toml_string(path: Path, section: str, key: str) -> str:
    return _quoted_value(_section_value(path, section, key), f"{path}:{section}.{key}")


def _toml_integer(path: Path, section: str, key: str) -> int:
    raw = _section_value(path, section, key)
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{path}:{section}.{key} must be an integer") from exc


def _lock_package_version(path: Path, package_name: str) -> str:
    matches: list[str] = []
    for block in path.read_text(encoding="utf-8").split("[[package]]")[1:]:
        name = ""
        version = ""
        for raw_line in block.splitlines():
            line = raw_line.strip()
            key, separator, raw_value = line.partition("=")
            if not separator:
                continue
            if key.strip() == "name":
                name = _quoted_value(raw_value.strip(), f"{path}:package.name")
            elif key.strip() == "version":
                version = _quoted_value(raw_value.strip(), f"{path}:package.version")
        if name == package_name:
            if not version:
                raise RuntimeError(f"Package {package_name!r} in {path} has no version")
            matches.append(version)
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {package_name!r} package in {path}, found {len(matches)}")
    return matches[0]


def _json_object(path: Path) -> dict[object, object]:
    parsed: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return cast("dict[object, object]", parsed)


def _json_string(payload: dict[object, object], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label} must be a non-empty string")
    return value


def _json_integer(payload: dict[object, object], key: str, label: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"{label} must be an integer")
    return value


def _nested_json_object(payload: dict[object, object], key: str, label: str) -> dict[object, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return cast("dict[object, object]", value)


def _source_match(path: Path, pattern: re.Pattern[str], label: str) -> str:
    matches = [match.group(1) for match in pattern.finditer(path.read_text(encoding="utf-8"))]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {label} in {path}, found {len(matches)}")
    return matches[0]


def _source_integer(path: Path, pattern: re.Pattern[str], label: str) -> int:
    return int(_source_match(path, pattern, label))


def _require_version(value: str, label: str) -> str:
    if VERSION_PATTERN.fullmatch(value) is None:
        raise RuntimeError(f"{label} has invalid release version {value!r}")
    return value


def _require_equal(label: str, values: Sequence[tuple[str, str]]) -> str:
    if not values:
        raise RuntimeError(f"{label} has no values to verify")
    expected = values[0][1]
    mismatches = [f"{name}={value}" for name, value in values if value != expected]
    if mismatches:
        rendered = ", ".join(f"{name}={value}" for name, value in values)
        raise RuntimeError(f"{label} versions differ: {rendered}")
    return expected


def _verify_release_cohort(*, sdist: bool) -> str:
    runtime_path = ROOT / "src/lokit_office_runtime/runtime.json"
    runtime = _json_object(runtime_path)
    values = [
        ("python project", _toml_string(ROOT / "pyproject.toml", "project", "version")),
        ("native interchange", _toml_string(ROOT / "native/interchange/Cargo.toml", "package", "version")),
        ("native interchange lock", _lock_package_version(ROOT / "native/interchange/Cargo.lock", "lokit-interchange")),
        ("office runtime", _json_string(runtime, "worker_version", f"{runtime_path}:worker_version")),
        (
            "office Python client",
            _source_match(
                ROOT / "src/lokit/office/process.py",
                re.compile(r'"client_version"\s*:\s*"([^"]+)"'),
                "office client version",
            ),
        ),
    ]
    if not sdist:
        values.extend(
            (
                ("uv lock", _lock_package_version(ROOT / "uv.lock", "lokit-python")),
                (
                    "office C# worker",
                    _source_match(
                        ROOT / "src/office/Lokit.Office.Worker/Program.cs",
                        re.compile(r'\["worker_version"\]\s*=\s*"([^"]+)"'),
                        "office worker version",
                    ),
                ),
            )
        )
    for name, value in values:
        _require_version(value, name)
    return _require_equal("Python/native/office release cohort", values)


def _verify_lsp_cohort() -> str:
    vscode_package_path = ROOT / "tools/lokit-lsp/editors/vscode/package.json"
    vscode_lock_path = ROOT / "tools/lokit-lsp/editors/vscode/package-lock.json"
    vscode_package = _json_object(vscode_package_path)
    vscode_lock = _json_object(vscode_lock_path)
    lock_packages = _nested_json_object(vscode_lock, "packages", f"{vscode_lock_path}:packages")
    lock_root = _nested_json_object(lock_packages, "", f"{vscode_lock_path}:packages['']")
    values = (
        ("LSP", _toml_string(ROOT / "tools/lokit-lsp/Cargo.toml", "package", "version")),
        ("LSP lock", _lock_package_version(ROOT / "tools/lokit-lsp/Cargo.lock", "lokit-lsp")),
        ("VS Code package", _json_string(vscode_package, "version", f"{vscode_package_path}:version")),
        ("VS Code lock", _json_string(vscode_lock, "version", f"{vscode_lock_path}:version")),
        ("VS Code lock root", _json_string(lock_root, "version", f"{vscode_lock_path}:packages[''].version")),
        ("Zed crate", _toml_string(ROOT / "tools/lokit-lsp/editors/zed/Cargo.toml", "package", "version")),
        ("Zed lock", _lock_package_version(ROOT / "tools/lokit-lsp/editors/zed/Cargo.lock", "lokit-zed")),
        ("Zed extension", _toml_string(ROOT / "tools/lokit-lsp/editors/zed/extension.toml", "", "version")),
    )
    for name, value in values:
        _require_version(value, name)
    return _require_equal("LSP/editor release cohort", values)


def _verify_core_version(*, sdist: bool) -> str:
    values = [
        ("lokit-format", _toml_string(ROOT / "native/lokit-format/Cargo.toml", "package", "version")),
        ("lokit-format lock", _lock_package_version(ROOT / "native/lokit-format/Cargo.lock", "lokit-format")),
        ("interchange lock dependency", _lock_package_version(ROOT / "native/interchange/Cargo.lock", "lokit-format")),
    ]
    if not sdist:
        values.append(
            ("LSP lock dependency", _lock_package_version(ROOT / "tools/lokit-lsp/Cargo.lock", "lokit-format"))
        )
    for name, value in values:
        _require_version(value, name)
    return _require_equal("independent lokit-format cohort", values)


def _verify_protocol_version(*, sdist: bool) -> str:
    runtime_path = ROOT / "src/lokit_office_runtime/runtime.json"
    runtime = _json_object(runtime_path)
    python_runtime_path = ROOT / "src/lokit/office/runtime.py"
    python_major = _source_integer(
        python_runtime_path,
        re.compile(r"^PROTOCOL_MAJOR\s*=\s*([0-9]+)\s*$", re.MULTILINE),
        "Python protocol major",
    )
    python_minor = _source_integer(
        python_runtime_path,
        re.compile(r"^PROTOCOL_MINOR\s*=\s*([0-9]+)\s*$", re.MULTILINE),
        "Python protocol minor",
    )
    values = [
        (
            "office runtime metadata",
            f"{_json_integer(runtime, 'protocol_major', f'{runtime_path}:protocol_major')}."
            f"{_json_integer(runtime, 'protocol_minor', f'{runtime_path}:protocol_minor')}",
        ),
        ("office Python protocol", f"{python_major}.{python_minor}"),
    ]
    if not sdist:
        csharp_protocol_path = ROOT / "src/office/Lokit.Office.Core/Protocol/ProtocolFrame.cs"
        csharp_major = _source_integer(
            csharp_protocol_path,
            re.compile(r"ProtocolMajor\s*=\s*([0-9]+)\s*;"),
            "C# protocol major",
        )
        csharp_minor = _source_integer(
            csharp_protocol_path,
            re.compile(r"ProtocolMinor\s*=\s*([0-9]+)\s*;"),
            "C# protocol minor",
        )
        values.append(("office C# protocol", f"{csharp_major}.{csharp_minor}"))
    return _require_equal("independent office protocol cohort", values)


def _format_schema_version() -> int:
    return _source_integer(
        ROOT / "native/lokit-format/src/lib.rs",
        re.compile(r"pub const SCHEMA_VERSION:\s*u32\s*=\s*([0-9]+)\s*;"),
        "Lokit file schema version",
    )


def _parse_arguments(arguments: Sequence[str]) -> tuple[bool, str]:
    sdist = False
    expected_release = ""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--sdist":
            if sdist:
                raise SystemExit("--sdist may be specified only once")
            sdist = True
            index += 1
            continue
        if argument == "--expected-release":
            if expected_release or index + 1 >= len(arguments):
                raise SystemExit("--expected-release requires exactly one value")
            expected_release = arguments[index + 1]
            if expected_release.startswith("v"):
                expected_release = expected_release[1:]
            _require_version(expected_release, "expected release")
            index += 2
            continue
        raise SystemExit("usage: verify_release_versions.py [--sdist] [--expected-release VERSION_OR_TAG]")
    return sdist, expected_release


def main(arguments: Sequence[str]) -> None:
    sdist, expected_release = _parse_arguments(arguments)
    release_version = _verify_release_cohort(sdist=sdist)
    core_version = _verify_core_version(sdist=sdist)
    protocol_version = _verify_protocol_version(sdist=sdist)
    format_schema = _format_schema_version()
    if expected_release and expected_release != release_version:
        raise RuntimeError(f"Expected release {expected_release}, but the source cohort is {release_version}")
    print(f"verified Python/native/office release cohort {release_version}")
    print(f"verified independent lokit-format release cohort {core_version}")
    print(f"verified independent office protocol cohort {protocol_version}")
    if sdist:
        print(f"verified shipped source-distribution schema version: lokit={format_schema}")
        return
    lsp_version = _verify_lsp_cohort()
    zed_schema = _toml_integer(ROOT / "tools/lokit-lsp/editors/zed/extension.toml", "", "schema_version")
    print(f"verified independent LSP/editor release cohort {lsp_version}")
    print(f"verified independent schema versions: lokit={format_schema}, zed-manifest={zed_schema}")


if __name__ == "__main__":
    main(tuple(sys.argv[1:]))
