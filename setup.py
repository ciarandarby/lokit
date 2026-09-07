import glob
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, cast

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext
from setuptools.command.sdist import sdist

if TYPE_CHECKING:
    from setuptools._distutils.extension import Extension as DistutilsExtension


def _build_path_replacements(src_files: Sequence[str]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for src_file in src_files:
        posix_form = PurePosixPath(src_file).as_posix()
        windows_form = str(PureWindowsPath(src_file))
        if windows_form != posix_form:
            replacements[windows_form] = posix_form
    return replacements


def _normalize_generated_c_file(path: Path, replacements: Mapping[str, str]) -> None:
    """Replace Windows backslash paths with POSIX paths in a generated C file.

    Performs direct string replacement of known source file paths,
    avoiding any C source parsing. This is robust against escaped
    quotes and other C syntax that broke the previous quote-based parser.
    """
    if path.suffix != ".c" or not path.exists():
        return

    contents = path.read_text(encoding="utf-8")
    normalized = contents
    for windows_path, posix_path in replacements.items():
        normalized = normalized.replace(windows_path, posix_path)
    if normalized != contents:
        path.write_text(normalized, encoding="utf-8")


def _normalize_all_generated_c_files(replacements: Mapping[str, str]) -> None:
    """Normalize all generated C files in the build directory."""
    build_dir = Path("build")
    if build_dir.exists():
        for path in build_dir.rglob("*.c"):
            _normalize_generated_c_file(path, replacements)


def _normalize_ext_c_files(ext: Extension, replacements: Mapping[str, str]) -> None:
    """Normalize C files listed as sources for an extension module."""
    for source in ext.sources:
        _normalize_generated_c_file(Path(source), replacements)


def _remove_stale_compiled_artifacts(build_lib: Path) -> None:
    """Remove extension binaries left by an earlier, incompatible build.

    setuptools reuses ``build/lib.*`` between local wheel builds.  mypyc may
    change both its generated native module names and the set of compiled
    Python modules, so copying that directory without pruning can produce a
    wheel containing obsolete extensions.
    """
    if not build_lib.exists():
        return
    for pattern in ("*.so", "*.pyd"):
        for path in build_lib.rglob(pattern):
            path.unlink()


class BuildExt(build_ext):
    def build_extensions(self) -> None:
        _remove_stale_compiled_artifacts(Path(self.build_lib))
        _normalize_all_generated_c_files(_path_replacements)
        super().build_extensions()

    def build_extension(self, ext: Extension) -> None:
        _normalize_ext_c_files(ext, _path_replacements)
        super().build_extension(ext)


class SourceDistribution(sdist):
    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        super().make_release_tree(base_dir, files)
        path = Path(base_dir) / "src/lokit_office_runtime/runtime.json"
        value: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Office runtime metadata must be a JSON object")
        metadata = cast("dict[str, object]", value)
        for key in ("rid", "build_commit", "sha256"):
            metadata[key] = ""
        path.unlink()
        path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


_path_replacements: dict[str, str]
ext_modules: list[Extension]
_disable_mypyc = os.environ.get("LOKIT_NO_MYPYC", "").lower() in ("1", "true", "yes") or os.environ.get(
    "NO_MYPYC", ""
).lower() in ("1", "true", "yes")

if _disable_mypyc:
    _path_replacements = {}
    ext_modules = []
else:
    try:
        from mypyc.build import mypycify
    except ImportError:
        _path_replacements = {}
        ext_modules = []
    else:
        src_files = sorted(
            [
                *glob.glob("src/lokit/**/*.py", recursive=True),
                *glob.glob("src/lokit_office_runtime/**/*.py", recursive=True),
            ]
        )
        src_files = [f.replace("\\", "/") for f in src_files]
        bootstrap_modules = {"src/lokit/__init__.py", "src/lokit/db/__init__.py"}
        src_files = [path for path in src_files if path not in bootstrap_modules]

        _path_replacements = _build_path_replacements(src_files)

        ext_modules = mypycify(
            src_files,
            opt_level="3",
            debug_level="0",
        )
        _normalize_all_generated_c_files(_path_replacements)

distribution_extensions = cast("Sequence[DistutilsExtension]", ext_modules)

setup(
    cmdclass={"build_ext": BuildExt, "sdist": SourceDistribution},
    ext_modules=distribution_extensions,
)
