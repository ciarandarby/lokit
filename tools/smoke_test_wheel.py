from __future__ import annotations

import asyncio
import importlib
import importlib.machinery
from importlib.metadata import version as distribution_version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    from lokit.types import TranslationRow

import lokit
from lokit._interchange_rust import LokitReader, Reader
from lokit.data.structure import BaseStructure, Data
from lokit.database import database_schema_statements, iter_serialized_units
from lokit.exporters.lokit import export_lokit
from lokit.exporters.tmx import export_tmx
from lokit.exporters.xliff import export_xliff
from lokit.importers import import_lokit, import_tmx, import_xliff
from lokit.logic import Lokit
from lokit.office.runtime import load_runtime_info
from lokit.types import DictField, StringMode


def _is_extension(module: ModuleType) -> bool:
    path = module.__file__
    return path is not None and any(path.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)


async def _async_projection(path: Path) -> list[TranslationRow]:
    return await lokit.parse.async_.to_dict(path)


def main() -> None:
    native_module = importlib.import_module("lokit._interchange_rust")
    if not _is_extension(native_module):
        raise RuntimeError("The installed wheel did not contain the native interchange extension")
    package_version = distribution_version("lokit-python")
    runtime_info = load_runtime_info()
    if runtime_info.worker_version != package_version:
        raise RuntimeError(
            f"The installed office runtime version {runtime_info.worker_version} "
            f"does not match package {package_version}"
        )

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
        rich_xliff_path = directory / "rich.xliff"
        lokit_path = directory / "smoke.lokit"
        export_tmx(document, tmx_path)
        export_xliff(document, xliff_path)
        export_lokit(document, lokit_path)
        rich_xliff_path.write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2">
  <file original="app/messages" source-language="en-US" target-language="fr-FR" datatype="plaintext">
    <body>
      <trans-unit id="save">
        <source>Save <g id="1" ctype="bold" vendor="yes">now <x id="2" equiv-text="?"/></g></source>
        <target state="translated">Enregistrer <g id="1" ctype="bold">maintenant</g></target>
      </trans-unit>
    </body>
  </file>
</xliff>
""",
            encoding="utf-8",
        )

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
        expected_row = {
            "source_language": "en",
            "target_language": "fr",
            "source": " Source & smoke ",
            "target": " Cible & test ",
            "domain": "",
        }
        materialized_rows = lokit.parse.to_dict(tmx_path)
        streamed_rows = list(lokit.stream.to_dict(tmx_path))
        async_rows = asyncio.run(_async_projection(tmx_path))
        if materialized_rows != [expected_row] or streamed_rows != materialized_rows or async_rows != materialized_rows:
            raise RuntimeError("The installed wheel produced inconsistent dictionary projections")
        inline_fields = (DictField.SOURCE, DictField.TARGET)
        sanitized_inline_rows = lokit.parse.to_dict(rich_xliff_path, fields=inline_fields)
        raw_inline_rows = lokit.parse.to_dict(
            rich_xliff_path,
            fields=inline_fields,
            strings=StringMode.RAW,
        )
        if sanitized_inline_rows != [
            {
                "source": "Save now ",
                "target": "Enregistrer maintenant",
            }
        ]:
            raise RuntimeError("The installed wheel did not sanitize inline XLIFF strings")
        if raw_inline_rows != [
            {
                "source": 'Save <g id="1" ctype="bold" vendor="yes">now <x id="2" equiv-text="?"/></g>',
                "target": 'Enregistrer <g id="1" ctype="bold">maintenant</g>',
            }
        ]:
            raise RuntimeError("The installed wheel did not preserve raw inline XLIFF tags")
        if (
            list(
                lokit.stream.to_dict(
                    rich_xliff_path,
                    fields=inline_fields,
                    strings=StringMode.RAW,
                )
            )
            != raw_inline_rows
        ):
            raise RuntimeError("The installed wheel produced inconsistent streaming raw inline tags")

        split_documents = tmx_document.split_targets()
        if (
            tuple(split_documents) != ("fr-FR",)
            or split_documents["fr-FR"].data["wheel-smoke"].target != " Cible & test "
        ):
            raise RuntimeError("The installed wheel did not split a materialized target document")
        with lokit.stream.lokit(str(lokit_path)).split_targets(("fr-FR",)) as streaming_splits:
            split_units = list(streaming_splits["fr-FR"].items)
        if len(split_units) != 1 or split_units[0][1].target != " Cible & test ":
            raise RuntimeError("The installed wheel did not split a streaming target document")

        serialized = list(iter_serialized_units(tmx_document, project="wheel", domain="smoke"))
        if len(serialized) != 1 or serialized[0].unit.project != "wheel" or serialized[0].unit.domain != "smoke":
            raise RuntimeError("The installed wheel did not expose working database serializers")
        schema = database_schema_statements(partitioned=False, include_extensions=False)
        if not schema or not all(statement.endswith(";") for statement in schema):
            raise RuntimeError("The installed wheel did not expose valid database schema statements")
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

    print("verified installed-wheel runtime, interchange, raw tags, projection, split, and SQL serialization APIs")


if __name__ == "__main__":
    main()
