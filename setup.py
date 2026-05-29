import glob
import os
from pathlib import Path

from setuptools import setup
from setuptools.command.build_ext import build_ext


class BuildExt(build_ext):
    def build_extension(self, ext):
        if os.name == "nt":
            self._normalize_generated_c_paths(ext)
        super().build_extension(ext)

    def _normalize_generated_c_paths(self, ext):
        for source in ext.sources:
            path = Path(source)
            if path.suffix != ".c" or not path.exists():
                continue

            contents = path.read_text(encoding="utf-8")
            normalized = self._normalize_lokit_py_paths(contents)
            if normalized != contents:
                path.write_text(normalized, encoding="utf-8")

    def _normalize_lokit_py_paths(self, contents):
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
            if "\\src\\lokit\\" in quoted or quoted.startswith("src\\lokit\\"):
                quoted = quoted.replace("\\", "/")
            result.append(quoted)
            result.append('"')
            index = end_quote + 1

        return "".join(result)

try:
    from mypyc.build import mypycify

    src_files = glob.glob("src/lokit/**/*.py", recursive=True)
    src_files = [f.replace("\\", "/") for f in src_files if "importers.py" not in f]

    ext_modules = mypycify(
        src_files,
        opt_level="3",
        debug_level="0",
    )
except ImportError:
    ext_modules = []

setup(
    cmdclass={"build_ext": BuildExt},
    ext_modules=ext_modules,
)
