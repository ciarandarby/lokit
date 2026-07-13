from __future__ import annotations

import importlib
import importlib.machinery
from importlib.metadata import version
from typing import TYPE_CHECKING, Final

import lokit._interchange_rust as native_interchange

if TYPE_CHECKING:
    from types import ModuleType

MODULE_NAME: Final = "lokit._interchange_rust"


def _is_extension(module: ModuleType) -> bool:
    path = module.__file__
    return path is not None and any(path.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)


def main() -> None:
    module = importlib.import_module(MODULE_NAME)
    if not _is_extension(module):
        raise RuntimeError(f"{MODULE_NAME} did not import as a native extension: {module.__file__}")
    package_version = version("lokit-python")
    native_version = native_interchange.backend_version()
    if native_version != package_version:
        raise RuntimeError(f"Native interchange version {native_version} does not match package {package_version}")
    print(f"verified native interchange extension {native_version}: {module.__file__}")


if __name__ == "__main__":
    main()
