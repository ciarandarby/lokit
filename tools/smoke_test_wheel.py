from __future__ import annotations

import importlib
import importlib.machinery
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

from lokit._interchange_rust import Reader
from lokit.data.structure import BaseStructure, Data
from lokit.exporters.tmx import export_tmx
from lokit.exporters.xliff import export_xliff
from lokit.importers import import_tmx, import_xliff


def _is_extension(module: ModuleType) -> bool:
    path = module.__file__
    return path is not None and any(path.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)


def main() -> None:
    native_module = importlib.import_module("lokit._interchange_rust")
    if not _is_extension(native_module):
        raise RuntimeError("The installed wheel did not contain the native interchange extension")

    document = BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        data={
            "wheel-smoke": Data(
                source=" Source & smoke ",
                target=" Cible & test ",
            )
        },
    )
    with TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        tmx_path = directory / "smoke.tmx"
        xliff_path = directory / "smoke.xliff"
        export_tmx(document, tmx_path)
        export_xliff(document, xliff_path)

        native_tmx = Reader(str(tmx_path), "tmx", "en-US", "fr-FR")
        try:
            native_tmx_batch = native_tmx.read_batch(1)
        finally:
            native_tmx.close()
        if not native_tmx_batch or native_tmx_batch[0][1:4] != ("wheel-smoke", " Source & smoke ", " Cible & test "):
            raise RuntimeError("The installed native extension did not parse the TMX smoke unit")

        native_xliff = Reader(str(xliff_path), "xliff")
        try:
            native_xliff_batch = native_xliff.read_batch(1)
        finally:
            native_xliff.close()
        if (
            not native_xliff_batch
            or native_xliff_batch[0][1:4] != ("wheel-smoke", " Source & smoke ", None)
            or native_xliff_batch[0][4] != [("fr-FR", " Cible & test ")]
        ):
            raise RuntimeError("The installed native extension did not parse the XLIFF smoke unit")

        tmx_document = import_tmx(str(tmx_path), "en-US", "fr-FR", progress=False)
        xliff_document = import_xliff(str(xliff_path), progress=False)
        if tmx_document.data["wheel-smoke"].source != " Source & smoke ":
            raise RuntimeError("The installed wheel did not round-trip TMX source content")
        if xliff_document.data["wheel-smoke"].target != " Cible & test ":
            raise RuntimeError("The installed wheel did not round-trip XLIFF target content")
        if not tmx_path.read_bytes().endswith(b"</tmx>\n"):
            raise RuntimeError("The installed wheel did not produce formatted TMX output")
        if not xliff_path.read_bytes().endswith(b"</xliff>\n"):
            raise RuntimeError("The installed wheel did not produce formatted XLIFF output")

    print("verified installed-wheel TMX and XLIFF round trips")


if __name__ == "__main__":
    main()
