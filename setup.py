import glob
from pathlib import Path, PurePosixPath, PureWindowsPath

from setuptools import setup
from setuptools.command.build_ext import build_ext


def _build_path_replacements(src_files):

    replacements = {}
    for src_file in src_files:
        posix_form = PurePosixPath(src_file).as_posix()
        windows_form = str(PureWindowsPath(src_file))
        if windows_form != posix_form:
            replacements[windows_form] = posix_form
    return replacements


def _normalize_generated_c_file(path, replacements):
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


def _normalize_all_generated_c_files(replacements):
    """Normalize all generated C files in the build directory."""
    build_dir = Path("build")
    if build_dir.exists():
        for path in build_dir.rglob("*.c"):
            _normalize_generated_c_file(path, replacements)


def _normalize_ext_c_files(ext, replacements):
    """Normalize C files listed as sources for an extension module."""
    for source in ext.sources:
        _normalize_generated_c_file(Path(source), replacements)


class BuildExt(build_ext):
    def build_extensions(self):
        self._normalize_before_compile()
        _normalize_all_generated_c_files(_path_replacements)
        super().build_extensions()

    def build_extension(self, ext):
        _normalize_ext_c_files(ext, _path_replacements)
        super().build_extension(ext)

    def _normalize_before_compile(self):
        original_compile = self.compiler.compile

        def compile_with_normalized_sources(sources, *args, **kwargs):
            for source in sources:
                _normalize_generated_c_file(Path(source), _path_replacements)
            return original_compile(sources, *args, **kwargs)

        self.compiler.compile = compile_with_normalized_sources


try:
    from mypyc.build import mypycify

    src_files = glob.glob("src/lokit/**/*.py", recursive=True)
    src_files = [f.replace("\\", "/") for f in src_files]
    src_files = [
        f
        for f in src_files
        if "importers.py" not in f
        and f != "src/lokit/__init__.py"
        and f != "src/lokit/db/__init__.py"
        and "/documents/" not in f
        and "_accelerators.py" not in f
        and "db/connection.py" not in f
        and "db/operations.py" not in f
    ]

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

setup(
    cmdclass={"build_ext": BuildExt},
    ext_modules=ext_modules,
)
