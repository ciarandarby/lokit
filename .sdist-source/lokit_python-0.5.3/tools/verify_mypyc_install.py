from __future__ import annotations

import importlib
import importlib.machinery
from typing import TYPE_CHECKING

from lokit._compiled_manifest import EXPECTED_MYPYC_MODULES

if TYPE_CHECKING:
    from types import ModuleType


def _is_extension(module: ModuleType) -> bool:
    path = module.__file__
    return path is not None and any(path.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)


def main() -> None:
    missing = [name for name in EXPECTED_MYPYC_MODULES if not _is_extension(importlib.import_module(name))]
    if missing:
        raise RuntimeError(f"Runtime modules did not import as mypyc extensions: {missing}")
    print(f"verified {len(EXPECTED_MYPYC_MODULES)} mypyc runtime modules")


if __name__ == "__main__":
    main()
