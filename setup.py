import glob

from setuptools import setup

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
    ext_modules=ext_modules,
)
