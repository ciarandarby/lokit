from __future__ import annotations

import asyncio
import importlib
import importlib.machinery
import zipfile
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
from lokit.office.process import worker_available
from lokit.office.runtime import executable_path, load_runtime_info, validate_executable_digest
from lokit.types import DictField, StringMode


def _is_extension(module: ModuleType) -> bool:
    path = module.__file__
    return path is not None and any(path.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)


def _write_office_smoke(path: Path) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/ppt/presentation.xml"
   ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slides/slide1.xml"
   ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
</Types>
"""
    presentation = """<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
</p:presentation>
"""
    relationships = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
   Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
   Target="slides/slide1.xml"/>
</Relationships>
"""
    slide = """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody>
    <a:p><a:r><a:t>Office wheel smoke</a:t></a:r></a:p>
  </p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>
"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/_rels/presentation.xml.rels", relationships)
        archive.writestr("ppt/slides/slide1.xml", slide)


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
        office_source_path = directory / "smoke.pptx"
        office_output_path = directory / "translated.pptx"
        export_tmx(document, tmx_path)
        export_xliff(document, xliff_path)
        export_lokit(document, lokit_path)
        _write_office_smoke(office_source_path)
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
        plain_inline_rows = lokit.parse.to_dict(
            rich_xliff_path,
            fields=inline_fields,
            runtime_placeholders=False,
            inline_placeholders=False,
        )
        raw_inline_rows = lokit.parse.to_dict(
            rich_xliff_path,
            fields=inline_fields,
            strings=StringMode.RAW,
        )
        if sanitized_inline_rows != [
            {
                "source": "Save {LOKIT_P1}now {LOKIT_P2}{LOKIT_P3}",
                "target": "Enregistrer {LOKIT_P1}maintenant{LOKIT_P2}",
            }
        ]:
            raise RuntimeError("The installed wheel did not project inline XLIFF placeholders")
        if plain_inline_rows != [
            {
                "source": "Save now ",
                "target": "Enregistrer maintenant",
            }
        ]:
            raise RuntimeError("The installed wheel did not disable inline XLIFF placeholders")
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

        if runtime_info.rid:
            if not worker_available():
                raise RuntimeError(f"The {runtime_info.rid} wheel did not contain its Office worker")
            worker_path = executable_path()
            validate_executable_digest(worker_path, runtime_info.sha256)
        office_document = lokit.parse.pptx(office_source_path, source_locale="en-US", progress=False)
        if [unit.source for unit in office_document.data.values()] != ["Office wheel smoke"]:
            raise RuntimeError("The installed wheel did not parse its Office smoke presentation")
        next(iter(office_document.data.values())).target = "Office wheel translated"
        office_document.export.pptx(office_output_path)
        with zipfile.ZipFile(office_output_path) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("The installed wheel produced a corrupt Office package")
        reparsed_office = lokit.parse.pptx(office_output_path, source_locale="fr-FR", progress=False)
        if [unit.source for unit in reparsed_office.data.values()] != ["Office wheel translated"]:
            raise RuntimeError("The installed wheel did not reinsert its Office translation")

    print("verified installed-wheel Office, interchange, raw tags, projection, split, and SQL serialization APIs")


if __name__ == "__main__":
    main()
