from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from lokit.data.structure import BaseStructure, Data
from lokit.exporters.tmx import export_tmx
from lokit.exporters.xliff import export_xliff
from lokit.importers import import_tmx, import_xliff


def main() -> None:
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
