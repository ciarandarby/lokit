import glob
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, cast

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

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


try:
    from mypyc.build import mypycify

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
except ImportError:
    _path_replacements = {}
    ext_modules = []

distribution_extensions = cast("Sequence[DistutilsExtension]", ext_modules)

setup(
    cmdclass={"build_ext": BuildExt},
    ext_modules=distribution_extensions,
)
