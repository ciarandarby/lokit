from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE_ROOT = ROOT / "src"
OUTPUT = SOURCE_ROOT / "lokit" / "_compiled_manifest.py"


def _module_name(path: Path) -> str:
    relative = path.relative_to(SOURCE_ROOT).with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def main() -> None:
    sources = sorted(
        (*SOURCE_ROOT.joinpath("lokit").rglob("*.py"), *SOURCE_ROOT.joinpath("lokit_office_runtime").rglob("*.py"))
    )
    bootstrap_modules = {SOURCE_ROOT / "lokit/__init__.py", SOURCE_ROOT / "lokit/db/__init__.py"}
    modules = tuple(sorted({_module_name(path) for path in sources if path not in bootstrap_modules}))
    rendered = "\n".join(f'    "{module}",' for module in modules)
    OUTPUT.write_text(
        '"""Generated list of runtime modules that must be mypyc extensions."""\n\n'
        "EXPECTED_MYPYC_MODULES: tuple[str, ...] = (\n"
        f"{rendered}\n"
        ")\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
