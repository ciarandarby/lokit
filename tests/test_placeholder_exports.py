from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from lxml import etree

from lokit import parse
from lokit.data.structure import BaseStructure, Data
from lokit.exporters import export_json_i18n, export_tmx, export_xliff
from lokit.placeholders import PlaceholderSyntax, literalize_data, project_data, resolve_data

if TYPE_CHECKING:
    from pathlib import Path


def _projected_document() -> BaseStructure:
    projected = project_data(
        Data(source="Hello {name}", target="Bonjour {name}"),
        runtime_placeholders=True,
        inline_placeholders=False,
        syntaxes=(PlaceholderSyntax.PYTHON_BRACE,),
    )
    return BaseStructure(
        source_locale="en",
        target_locale="fr",
        data={"greeting": projected},
    )


def test_rust_export_modes_are_reversible_and_non_mutating() -> None:
    document = _projected_document()
    projected = document.data["greeting"]

    resolved = resolve_data(projected)
    literal = literalize_data(projected)

    assert resolved.source == "Hello {name}"
    assert resolved.target == "Bonjour {name}"
    assert literal.source == "Hello {LOKIT_P1}"
    assert literal.target == "Bonjour {LOKIT_P1}"
    assert projected.source == "Hello {LOKIT_P1}"
    assert projected.target == "Bonjour {LOKIT_P1}"


def test_json_export_restores_or_literalizes_runtime_placeholders(tmp_path: Path) -> None:
    document = _projected_document()
    resolved_path = tmp_path / "resolved.json"
    literal_path = tmp_path / "literal.json"

    export_json_i18n(document, resolved_path, nested=False)
    export_json_i18n(
        document,
        literal_path,
        nested=False,
        resolve_placeholders=False,
    )

    assert json.loads(resolved_path.read_text(encoding="utf-8")) == {
        "greeting": "Bonjour {name}",
    }
    assert json.loads(literal_path.read_text(encoding="utf-8")) == {
        "greeting": "Bonjour {LOKIT_P1}",
    }


def test_xml_exports_do_not_serialize_runtime_markers_as_inline_codes(tmp_path: Path) -> None:
    document = _projected_document()
    resolved_tmx = tmp_path / "resolved.tmx"
    literal_tmx = tmp_path / "literal.tmx"
    resolved_xliff = tmp_path / "resolved.xliff"
    literal_xliff = tmp_path / "literal.xliff"

    export_tmx(document, resolved_tmx)
    export_tmx(document, literal_tmx, resolve_placeholders=False)
    export_xliff(document, resolved_xliff)
    export_xliff(document, literal_xliff, resolve_placeholders=False)

    resolved_tmx_root = etree.parse(str(resolved_tmx)).getroot()
    literal_tmx_root = etree.parse(str(literal_tmx)).getroot()
    assert resolved_tmx_root.xpath("string(.//tuv[@xml:lang='fr']/seg)") == "Bonjour {name}"
    assert literal_tmx_root.xpath("string(.//tuv[@xml:lang='fr']/seg)") == "Bonjour {LOKIT_P1}"
    assert not literal_tmx_root.xpath(".//tuv[@xml:lang='fr']/seg/*")

    resolved_xliff_root = etree.parse(str(resolved_xliff)).getroot()
    literal_xliff_root = etree.parse(str(literal_xliff)).getroot()
    namespace = {"x": "urn:oasis:names:tc:xliff:document:1.2"}
    assert resolved_xliff_root.xpath("string(.//x:target)", namespaces=namespace) == "Bonjour {name}"
    assert literal_xliff_root.xpath("string(.//x:target)", namespaces=namespace) == "Bonjour {LOKIT_P1}"
    assert not literal_xliff_root.xpath(".//x:target/*", namespaces=namespace)


def test_html_inline_markers_default_to_native_tags_with_literal_opt_out(tmp_path: Path) -> None:
    source = tmp_path / "source.html"
    source.write_text("<html><body><p>Hello <strong>world</strong>.</p></body></html>", encoding="utf-8")
    document = parse.html(str(source), source_locale="en", progress=False)
    resolved_path = tmp_path / "resolved.html"
    literal_path = tmp_path / "literal.html"

    document.export.html(resolved_path)
    document.export.html(literal_path, resolve_placeholders=False)

    resolved = resolved_path.read_text(encoding="utf-8")
    literal = literal_path.read_text(encoding="utf-8")
    assert "<strong>world</strong>" in resolved
    assert "{LOKIT_P1}" in literal
    assert "{LOKIT_P2}" in literal
    assert "<strong>world</strong>" not in literal


def test_html_mt_target_string_rebinds_and_reorders_complete_inline_pairs(tmp_path: Path) -> None:
    source = tmp_path / "source.html"
    output = tmp_path / "translated.html"
    source.write_text(
        "<html><body><p><strong>First</strong> and <em>second</em>.</p></body></html>",
        encoding="utf-8",
    )
    document = parse.html(str(source), source_locale="en", target_locale="fr", progress=False)
    unit = document.data["html:p:0"]
    assert unit.source == "{LOKIT_P1}First{LOKIT_P2} and {LOKIT_P3}second{LOKIT_P4}."
    unit.target = "{LOKIT_P3}deuxième{LOKIT_P4} puis {LOKIT_P1}premier{LOKIT_P2}."

    document.export.html(output)

    translated = output.read_text(encoding="utf-8")
    assert "<em>deuxième</em> puis <strong>premier</strong>." in translated
    assert "{LOKIT_" not in translated


def test_html_mt_target_string_restores_runtime_and_inline_placeholders(tmp_path: Path) -> None:
    source = tmp_path / "runtime-source.html"
    output = tmp_path / "runtime-translated.html"
    source.write_text(
        "<html><body><p>Hello {name}, <strong>{count}</strong>.</p></body></html>",
        encoding="utf-8",
    )
    document = parse.html(str(source), source_locale="en", target_locale="fr", progress=False)
    unit = document.data["html:p:0"]
    assert unit.source == "Hello {LOKIT_P1}, {LOKIT_P2}{LOKIT_P3}{LOKIT_P4}."
    unit.target = "Bonjour {LOKIT_P1}, {LOKIT_P2}{LOKIT_P3}{LOKIT_P4}."

    document.export.html(output)

    translated = output.read_text(encoding="utf-8")
    assert "Bonjour {name}, <strong>{count}</strong>." in translated
    assert "{LOKIT_" not in translated


@pytest.mark.parametrize(
    ("target", "message"),
    [
        pytest.param(
            "Bonjour {LOKIT_P99}monde{LOKIT_P2}.",
            "unknown projected placeholder token",
            id="unknown",
        ),
        pytest.param(
            "Bonjour {LOKIT_Px}monde{LOKIT_P2}.",
            "invalid projected placeholder token",
            id="malformed",
        ),
        pytest.param(
            "Bonjour {LOKIT_P1}monde.",
            "missing projected placeholder token",
            id="missing",
        ),
        pytest.param(
            "Bonjour {LOKIT_P1}{LOKIT_P1}monde{LOKIT_P2}.",
            "repeats projected placeholder token",
            id="duplicate",
        ),
        pytest.param(
            "Bonjour {LOKIT_P2}monde{LOKIT_P1}.",
            "invalid order",
            id="invalid-pair-order",
        ),
    ],
)
def test_html_mt_target_string_rejects_corrupt_marker_graph_atomically(
    tmp_path: Path,
    target: str,
    message: str,
) -> None:
    source = tmp_path / "source.html"
    output = tmp_path / "translated.html"
    source.write_text(
        "<html><body><p>Hello <strong>world</strong>.</p></body></html>",
        encoding="utf-8",
    )
    output.write_text("existing output", encoding="utf-8")
    document = parse.html(str(source), source_locale="en", target_locale="fr", progress=False)
    document.data["html:p:0"].target = target

    with pytest.raises(ValueError, match=message):
        document.export.html(output)

    assert output.read_text(encoding="utf-8") == "existing output"
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))
