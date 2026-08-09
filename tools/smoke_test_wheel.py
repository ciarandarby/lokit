from __future__ import annotations

import importlib
import importlib.machinery
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

from lokit._interchange_rust import LokitReader, Reader
from lokit.data.structure import BaseStructure, Data
from lokit.exporters.lokit import export_lokit
from lokit.exporters.tmx import export_tmx
from lokit.exporters.xliff import export_xliff
from lokit.importers import import_lokit, import_tmx, import_xliff
from lokit.logic import Lokit


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
        lokit_path = directory / "smoke.lokit"
        export_tmx(document, tmx_path)
        export_xliff(document, xliff_path)
        export_lokit(document, lokit_path)

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

        native_lokit = LokitReader(str(lokit_path))
        try:
            native_lokit_batch = native_lokit.read_batch(1)
        finally:
            native_lokit.close()
        if native_lokit_batch != [("wheel-smoke", document.data["wheel-smoke"])]:
            raise RuntimeError("The installed native extension did not parse the Lokit smoke unit")

        lokit_document = import_lokit(str(lokit_path), progress=False)
        if lokit_document != document:
            raise RuntimeError("The installed wheel did not exactly round-trip a Lokit document")
        if Lokit.parse_bytes(lokit_path.read_bytes()).document != document:
            raise RuntimeError("The installed wheel did not parse Lokit bytes portably")
        lokit_payload = lokit_path.read_text(encoding="utf-8")
        if not lokit_payload.startswith("@lokit 1\n") or not lokit_payload.endswith("\n"):
            raise RuntimeError("The installed wheel did not produce canonical Lokit output")
        if "null" in lokit_payload:
            raise RuntimeError("The installed wheel emitted a forbidden null literal")

    print("verified installed-wheel TMX, XLIFF, and Lokit round trips")


if __name__ == "__main__":
    main()
