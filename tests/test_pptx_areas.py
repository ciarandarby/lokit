from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree

import pytest
from test_office_worker_process import _configure_worker

import lokit
from lokit.importers import import_pptx
from lokit.office import OfficeExportOptions, OfficeImportOptions

if TYPE_CHECKING:
    from lokit.data.structure import BaseStructure


@pytest.fixture
def complete_pptx(tmp_path: Path) -> Path:
    path = tmp_path / "complete.pptx"
    _write_complete_pptx(path)
    return path


def test_pptx_defaults_extract_every_area(complete_pptx: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    document = lokit.parse.pptx(complete_pptx, source_locale="en", progress=False)
    sources = _sources_by_area(document)

    assert sources["slides"] == {"Slide text", "Slide line one\nSlide line two", "Hidden slide text"}
    assert sources["speaker_notes"] == {"Speaker note"}
    assert sources["slide_masters"] == {"Master text"}
    assert sources["slide_layouts"] == {"Layout text"}
    assert sources["notes_masters"] == {"Notes master text"}
    assert sources["handout_masters"] == {"Handout master text"}
    assert sources["comments"] == {"Comment text"}
    assert sources["charts"] == {"Chart title"}
    assert sources["diagrams"] == {"Diagram text", " "}
    assert sources["document_metadata"] == {"Deck title", "Deck subject", "Creator", "Custom value"}
    assert sources["alt_text"] == {"Picture title", "Picture description"}
    assert "Generated slide number" not in {data.source for data in document.data.values()}
    assert "Unused layout text" not in {data.source for data in document.data.values()}


@pytest.mark.parametrize(
    ("options", "area"),
    [
        (OfficeImportOptions(include_slides=False), "slides"),
        (OfficeImportOptions(include_speaker_notes=False), "speaker_notes"),
        (OfficeImportOptions(include_slide_masters=False), "slide_masters"),
        (OfficeImportOptions(include_slide_layouts=False), "slide_layouts"),
        (OfficeImportOptions(include_notes_masters=False), "notes_masters"),
        (OfficeImportOptions(include_handout_masters=False), "handout_masters"),
        (OfficeImportOptions(include_comments=False), "comments"),
        (OfficeImportOptions(include_charts=False), "charts"),
        (OfficeImportOptions(include_diagrams=False), "diagrams"),
        (OfficeImportOptions(include_document_metadata=False), "document_metadata"),
        (OfficeImportOptions(include_alt_text=False), "alt_text"),
    ],
)
def test_pptx_areas_can_be_disabled_independently(
    complete_pptx: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: OfficeImportOptions,
    area: str,
) -> None:
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    document = lokit.parse.pptx(complete_pptx, options=options, progress=False)

    assert area not in _sources_by_area(document)


def test_pptx_legacy_area_switches_remain_effective(
    complete_pptx: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    options = OfficeImportOptions(include_notes=False, include_master_layout_content=False)
    document = import_pptx(complete_pptx, options=options, progress=False)
    areas = _sources_by_area(document)

    assert "speaker_notes" not in areas
    assert "slide_masters" not in areas
    assert "slide_layouts" not in areas


def test_pptx_hidden_slides_can_be_disabled(complete_pptx: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    document = lokit.stream.pptx(
        complete_pptx,
        options=OfficeImportOptions(include_hidden_slides=False),
    )

    assert {data.source for _, data in document.items if data.extensions.get("office.area") == "slides"} == {
        "Slide text",
        "Slide line one\nSlide line two",
    }


@pytest.mark.asyncio
async def test_pptx_async_entry_points_propagate_options(
    complete_pptx: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    options = OfficeImportOptions(include_speaker_notes=False)
    parsed = [item async for item in lokit.parse.async_.pptx(complete_pptx, options=options)]
    streamed = [item async for item in lokit.stream.async_.pptx(complete_pptx, options=options)]

    assert all(data.extensions.get("office.area") != "speaker_notes" for _, data in parsed)
    assert [(unit_id, data.source) for unit_id, data in parsed] == [
        (unit_id, data.source) for unit_id, data in streamed
    ]


def test_pptx_default_roundtrip_preserves_every_area(
    complete_pptx: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    document = lokit.parse.pptx(complete_pptx, target_locale="fr", progress=False)
    expected: dict[str, str] = {}
    for unit_id, data in document.data.items():
        translated = f"translated:{data.source}"
        data.target = translated
        expected[unit_id] = translated

    output = tmp_path / "translated.pptx"
    document.export.pptx(output, options=OfficeExportOptions())
    reparsed = lokit.parse.pptx(output, source_locale="fr", progress=False)

    assert {unit_id: data.source for unit_id, data in reparsed.data.items()} == expected
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            if name.endswith((".xml", ".rels")):
                ElementTree.fromstring(archive.read(name))


def test_pptx_picture_name_remains_structural_metadata(
    complete_pptx: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    document = lokit.parse.pptx(complete_pptx, target_locale="fr", progress=False)
    alt_text = {
        data.source: data for data in document.data.values() if data.extensions.get("office.area") == "alt_text"
    }
    assert set(alt_text) == {"Picture title", "Picture description"}
    alt_text["Picture title"].target = "Titre de l'image"
    alt_text["Picture description"].target = "Description de l'image"

    output = tmp_path / "translated-alt-text.pptx"
    document.export.pptx(output, options=OfficeExportOptions())

    with zipfile.ZipFile(output) as archive:
        root = ElementTree.fromstring(archive.read("ppt/slides/slide1.xml"))
    picture = next(element for element in root.iter() if element.tag.endswith("}cNvPr"))
    assert picture.attrib["name"] == "Picture 1"
    assert picture.attrib["title"] == "Titre de l'image"
    assert picture.attrib["descr"] == "Description de l'image"


def test_pptx_export_leaves_disabled_area_unchanged(
    complete_pptx: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    document = lokit.parse.pptx(complete_pptx, target_locale="fr", progress=False)
    for data in document.data.values():
        data.target = f"translated:{data.source}"

    output = tmp_path / "without-notes.pptx"
    document.export.pptx(output, options=OfficeExportOptions(include_speaker_notes=False))

    with zipfile.ZipFile(complete_pptx) as source, zipfile.ZipFile(output) as translated:
        assert translated.read("ppt/notesSlides/notesSlide1.xml") == source.read("ppt/notesSlides/notesSlide1.xml")


@pytest.mark.asyncio
async def test_pptx_async_export_propagates_options(
    complete_pptx: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    document = lokit.parse.pptx(complete_pptx, target_locale="fr", progress=False)
    for data in document.data.values():
        data.target = f"translated:{data.source}"

    output = tmp_path / "async-without-notes.pptx"
    await lokit.export.async_.pptx(
        document,
        output,
        complete_pptx,
        options=OfficeExportOptions(include_speaker_notes=False),
    )

    with zipfile.ZipFile(complete_pptx) as source, zipfile.ZipFile(output) as translated:
        assert translated.read("ppt/notesSlides/notesSlide1.xml") == source.read("ppt/notesSlides/notesSlide1.xml")
        assert translated.testzip() is None


def test_pptx_worker_matches_python_when_available(
    complete_pptx: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _debug_worker_path()
    if not worker.is_file() or not _configure_debug_dotnet(monkeypatch):
        pytest.skip("Office worker has not been built")
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    python_document = lokit.parse.pptx(complete_pptx, progress=False)
    monkeypatch.delenv("LOKIT_OFFICE_BACKEND")
    _configure_worker(worker, monkeypatch, tmp_path)
    worker_document = lokit.parse.pptx(complete_pptx, progress=False)

    assert {
        unit_id: (data.source, data.extensions.get("office.area")) for unit_id, data in worker_document.data.items()
    } == {unit_id: (data.source, data.extensions.get("office.area")) for unit_id, data in python_document.data.items()}

    options = OfficeImportOptions(
        include_slides=False,
        include_speaker_notes=False,
        include_slide_masters=False,
        include_slide_layouts=False,
        include_notes_masters=False,
        include_handout_masters=False,
        include_comments=False,
        include_charts=False,
        include_diagrams=False,
        include_document_metadata=False,
        include_alt_text=False,
    )
    assert not lokit.parse.pptx(complete_pptx, options=options, progress=False).data

    expected: dict[str, str] = {}
    for unit_id, data in worker_document.data.items():
        translated = f"worker:{data.source}"
        data.target = translated
        expected[unit_id] = translated
    output = tmp_path / "worker-roundtrip.pptx"
    worker_document.export.pptx(output, options=OfficeExportOptions())
    reparsed = lokit.parse.pptx(output, progress=False)

    assert {unit_id: data.source for unit_id, data in reparsed.data.items()} == expected


def test_pptx_worker_excludes_hidden_slide_related_content_when_disabled(
    complete_pptx: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _debug_worker_path()
    if not worker.is_file() or not _configure_debug_dotnet(monkeypatch):
        pytest.skip("Office worker has not been built")
    _add_hidden_slide_related_content(complete_pptx)
    options = OfficeImportOptions(include_hidden_slides=False)

    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    python_document = lokit.parse.pptx(complete_pptx, options=options, progress=False)
    monkeypatch.delenv("LOKIT_OFFICE_BACKEND")
    _configure_worker(worker, monkeypatch, complete_pptx.parent)
    worker_document = lokit.parse.pptx(complete_pptx, options=options, progress=False)

    python_units = {
        unit_id: (data.source, data.extensions.get("office.area")) for unit_id, data in python_document.data.items()
    }
    worker_units = {
        unit_id: (data.source, data.extensions.get("office.area")) for unit_id, data in worker_document.data.items()
    }
    assert worker_units == python_units
    hidden_only_text = {
        "Hidden-only chart",
        "Hidden-only comment",
        "Hidden-only diagram",
        "Hidden-only note",
    }
    assert hidden_only_text.isdisjoint(data.source for data in worker_document.data.values())
    visible_related_text = {"Chart title", "Comment text", "Diagram text", "Speaker note"}
    assert visible_related_text.issubset(data.source for data in worker_document.data.values())


def _sources_by_area(document: BaseStructure) -> dict[str, set[str]]:
    areas: dict[str, set[str]] = {}
    for data in document.data.values():
        area = data.extensions.get("office.area", "")
        areas.setdefault(area, set()).add(data.source)
    return areas


def _debug_worker_path() -> Path:
    name = "Lokit.Office.Worker.exe" if os.name == "nt" else "Lokit.Office.Worker"
    return Path("src/office/Lokit.Office.Worker/bin/Debug/net10.0") / name


def _configure_debug_dotnet(monkeypatch: pytest.MonkeyPatch) -> bool:
    executable = shutil.which("dotnet")
    if executable is None:
        return False
    resolved = Path(executable).resolve()
    candidates = (resolved.parent, resolved.parent.parent / "libexec")
    for candidate in candidates:
        if (candidate / "dotnet").is_file() and (candidate / "host").is_dir():
            monkeypatch.setenv("DOTNET_ROOT", str(candidate))
            return True
    return False


def _write_complete_pptx(path: Path) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/ppt/presentation.xml"
   ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
</Types>
"""
    presentation = """<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:sldIdLst><p:sldId id="256" r:id="rId1"/><p:sldId id="257" r:id="rId2"/></p:sldIdLst>
</p:presentation>"""
    presentation_rels = _relationships(
        ("rId1", "slide", "slides/slide1.xml"),
        ("rId2", "slide", "slides/slide2.xml"),
    )
    slide = """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree>
    <p:sp><p:txBody><a:p><a:r><a:t>Slide text</a:t></a:r></a:p></p:txBody></p:sp>
    <p:sp><p:txBody><a:p><a:r><a:t>Slide line one</a:t></a:r><a:br/>
      <a:r><a:t>Slide line two</a:t></a:r></a:p></p:txBody></p:sp>
    <p:pic><p:nvPicPr>
      <p:cNvPr id="2" name="Picture 1" title="Picture title" descr="Picture description"/>
    </p:nvPicPr></p:pic>
  </p:spTree></p:cSld>
</p:sld>"""
    hidden_slide = """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" show="0">
  <p:cSld><p:spTree><p:sp><p:txBody>
    <a:p><a:r><a:t>Hidden slide text</a:t></a:r></a:p>
  </p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>"""
    slide_rels = _relationships(
        ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml"),
        ("rId2", "notesSlide", "../notesSlides/notesSlide1.xml"),
        ("rId3", "chart", "../charts/chart1.xml"),
        ("rId4", "diagramData", "../diagrams/data1.xml"),
        ("rId5", "comments", "../comments/comment1.xml"),
    )
    notes = """<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <a:p><a:r><a:t>Speaker note</a:t></a:r></a:p>
  <a:p><a:fld type="slidenum"><a:t>Generated slide number</a:t></a:fld></a:p>
</p:notes>"""
    layout = _drawing_xml("p:sldLayout", "Layout text")
    unused_layout = _drawing_xml("p:sldLayout", "Unused layout text")
    master = _drawing_xml("p:sldMaster", "Master text")
    notes_master = _drawing_xml("p:notesMaster", "Notes master text")
    handout_master = _drawing_xml("p:handoutMaster", "Handout master text")
    layout_rels = _relationships(("rId1", "slideMaster", "../slideMasters/slideMaster1.xml"))
    notes_rels = _relationships(("rId1", "notesMaster", "../notesMasters/notesMaster1.xml"))
    chart = """<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <c:chart><c:title><c:tx><c:rich><a:p><a:r><a:t>Chart title</a:t></a:r></a:p></c:rich></c:tx></c:title></c:chart>
</c:chartSpace>"""
    diagram = """<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <dgm:pt><dgm:t><a:p><a:r><a:t>Diagram text</a:t></a:r></a:p></dgm:t></dgm:pt>
  <dgm:pt><dgm:t><a:p><a:r><a:t> </a:t></a:r></a:p></dgm:t></dgm:pt>
</dgm:dataModel>"""
    comments = """<p:cmLst xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cm><p:text>Comment text</p:text></p:cm>
</p:cmLst>"""
    core = """<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>Deck title</dc:title>
  <dc:subject>Deck subject</dc:subject>
  <dc:creator>Creator</dc:creator>
  <cp:revision>1</cp:revision>
</cp:coreProperties>"""
    custom = """<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <property name="Customer"><vt:lpwstr>Custom value</vt:lpwstr></property>
</Properties>"""

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in {
            "[Content_Types].xml": content_types,
            "ppt/presentation.xml": presentation,
            "ppt/_rels/presentation.xml.rels": presentation_rels,
            "ppt/slides/slide1.xml": slide,
            "ppt/slides/slide2.xml": hidden_slide,
            "ppt/slides/_rels/slide1.xml.rels": slide_rels,
            "ppt/notesSlides/notesSlide1.xml": notes,
            "ppt/notesSlides/_rels/notesSlide1.xml.rels": notes_rels,
            "ppt/slideLayouts/slideLayout1.xml": layout,
            "ppt/slideLayouts/slideLayout2.xml": unused_layout,
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels": layout_rels,
            "ppt/slideMasters/slideMaster1.xml": master,
            "ppt/notesMasters/notesMaster1.xml": notes_master,
            "ppt/handoutMasters/handoutMaster1.xml": handout_master,
            "ppt/charts/chart1.xml": chart,
            "ppt/diagrams/data1.xml": diagram,
            "ppt/comments/comment1.xml": comments,
            "docProps/core.xml": core,
            "docProps/custom.xml": custom,
        }.items():
            archive.writestr(name, data)


def _add_hidden_slide_related_content(path: Path) -> None:
    relationships = _relationships(
        ("rId1", "notesSlide", "../notesSlides/notesSlide2.xml"),
        ("rId2", "chart", "../charts/chart2.xml"),
        ("rId3", "diagramData", "../diagrams/data2.xml"),
        ("rId4", "comments", "../comments/comment2.xml"),
    )
    notes = _drawing_xml("p:notes", "Hidden-only note")
    chart = (
        '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<a:p><a:r><a:t>Hidden-only chart</a:t></a:r></a:p>"
        "</c:chartSpace>"
    )
    diagram = (
        '<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<a:p><a:r><a:t>Hidden-only diagram</a:t></a:r></a:p>"
        "</dgm:dataModel>"
    )
    comments = (
        '<p:cmLst xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        "<p:cm><p:text>Hidden-only comment</p:text></p:cm>"
        "</p:cmLst>"
    )
    with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ppt/slides/_rels/slide2.xml.rels", relationships)
        archive.writestr("ppt/notesSlides/notesSlide2.xml", notes)
        archive.writestr("ppt/charts/chart2.xml", chart)
        archive.writestr("ppt/diagrams/data2.xml", diagram)
        archive.writestr("ppt/comments/comment2.xml", comments)


def _relationships(*relationships: tuple[str, str, str]) -> str:
    entries = "".join(
        (
            f'<Relationship Id="{relationship_id}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            f'{relationship_type}" Target="{target}"/>'
        )
        for relationship_id, relationship_type, target in relationships
    )
    return (
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{entries}</Relationships>'
    )


def _drawing_xml(root: str, text: str) -> str:
    return (
        f'<{root} xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
        f"</{root}>"
    )
