import glob
from pathlib import Path, PureWindowsPath

from setuptools import setup
from setuptools.command.build_ext import build_ext


class BuildExt(build_ext):
    def build_extensions(self):
        self._normalize_before_compile()
        _normalize_all_generated_c_paths()
        super().build_extensions()

    def build_extension(self, ext):
        _normalize_generated_c_paths(ext)
        super().build_extension(ext)

    def _normalize_before_compile(self):
        original_compile = self.compiler.compile

        def compile_with_normalized_sources(sources, *args, **kwargs):
            for source in sources:
                _normalize_generated_c_path(Path(source))
            return original_compile(sources, *args, **kwargs)

        self.compiler.compile = compile_with_normalized_sources


def _normalize_all_generated_c_paths():
    for path in Path("build").rglob("*.c"):
        _normalize_generated_c_path(path)


def _normalize_generated_c_paths(ext):
    for source in ext.sources:
        _normalize_generated_c_path(Path(source))


def _normalize_generated_c_path(path):
    if path.suffix != ".c" or not path.exists():
        return

    contents = path.read_text(encoding="utf-8")
    normalized = _normalize_lokit_py_paths(contents)
    if normalized != contents:
        path.write_text(normalized, encoding="utf-8")


def _normalize_lokit_py_paths(contents):
    result = []
    index = 0
    while index < len(contents):
        quote_index = contents.find('"', index)
        if quote_index == -1:
            result.append(contents[index:])
            break

        result.append(contents[index:quote_index + 1])
        end_quote = contents.find('"', quote_index + 1)
        if end_quote == -1:
            result.append(contents[quote_index + 1:])
            break

        quoted = contents[quote_index + 1:end_quote]
        quoted = _normalize_lokit_py_path(quoted)
        result.append(quoted)
        result.append('"')
        index = end_quote + 1

    return "".join(result)


def _normalize_lokit_py_path(path):
    windows_path = path.replace("\\\\", "\\")
    if "src\\lokit\\" in windows_path:
        return PureWindowsPath(windows_path).as_posix()
    return path

try:
    from mypyc.build import mypycify

    src_files = glob.glob("src/lokit/**/*.py", recursive=True)
    src_files = [f.replace("\\", "/") for f in src_files if "importers.py" not in f]

    ext_modules = mypycify(
        src_files,
        opt_level="3",
        debug_level="0",
    )
    _normalize_all_generated_c_paths()
except ImportError:
    ext_modules = []

setup(
    cmdclass={"build_ext": BuildExt},
    ext_modules=ext_modules,
)
