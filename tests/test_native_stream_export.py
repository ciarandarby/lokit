from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from xml.etree import ElementTree

import pytest

import lokit
from lokit import _interchange_rust
from lokit.data.structure import BaseStructure, CodePart, Data, TargetData, TranslationStatus

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(slots=True)
class _NativeCallTrace:
    attempts: int = 0
    results: list[int | None] = field(default_factory=list)


def _trace_native_conversion(monkeypatch: pytest.MonkeyPatch) -> _NativeCallTrace:
    original = _interchange_rust.convert_interchange
    trace = _NativeCallTrace()

    def tracking_conversion(
        source_path: str,
        target_path: str,
        input_format: str,
        output_format: str,
        source_language: str | None = None,
        target_language: str | None = None,
        mode: str = "full",
        copy_if_same: bool = False,
    ) -> int | None:
        trace.attempts += 1
        result = original(
            source_path,
            target_path,
            input_format,
            output_format,
            source_language,
            target_language,
            mode,
            copy_if_same,
        )
        trace.results.append(result)
        return result

    monkeypatch.setattr(_interchange_rust, "convert_interchange", tracking_conversion)
    return trace


def _trace_native_base_export(monkeypatch: pytest.MonkeyPatch) -> _NativeCallTrace:
    original = _interchange_rust.export_base_interchange
    trace = _NativeCallTrace()

    def tracking_export(
        document: object,
        target_path: str,
        output_format: str,
    ) -> int | None:
        trace.attempts += 1
        result = original(document, target_path, output_format)
        trace.results.append(result)
        return result

    monkeypatch.setattr(_interchange_rust, "export_base_interchange", tracking_export)
    return trace


def _write_simple_tmx(path: Path, *, units: int = 3) -> None:
    body = "".join(
        (
            f'<tu tuid="unit-{index}"><tuv xml:lang="en-US"><seg>Source {index} &amp; tea</seg></tuv>'
            f'<tuv xml:lang="fr-FR"><seg>Cible {index} &amp; café</seg></tuv></tu>'
        )
        for index in range(units)
    )
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header srclang="en-US"/><body>{body}</body></tmx>',
        encoding="utf-8",
    )


def _write_simple_xliff(path: Path, *, data_type: str = "plaintext") -> None:
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="messages" source-language="en-US" target-language="fr-FR" datatype="{data_type}">
    <body>
      <trans-unit id="one"><source>One &amp; tea</source><target state="translated">Un &amp; café</target></trans-unit>
      <trans-unit id="two"><source>Two &lt; three</source><target state="final">Deux &lt; trois</target></trans-unit>
    </body>
  </file>
</xliff>
""",
        encoding="utf-8",
    )


def _write_unlocalized_xliff(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="messages" source-language="en-US" datatype="plaintext">
    <body>
      <trans-unit id="orphan"><source>Hello</source><target state="translated">Bonjour</target></trans-unit>
    </body>
  </file>
</xliff>
""",
        encoding="utf-8",
    )


def _xliff_unit_snapshot(path: Path) -> dict[str, tuple[bool, str | None, str | None]]:
    root = ElementTree.parse(path).getroot()
    namespace = "urn:oasis:names:tc:xliff:document:1.2"
    snapshot: dict[str, tuple[bool, str | None, str | None]] = {}
    for unit in root.iter(f"{{{namespace}}}trans-unit"):
        target = unit.find(f"{{{namespace}}}target")
        snapshot[unit.attrib["id"]] = (
            target is not None,
            target.text if target is not None else None,
            target.attrib.get("state") if target is not None else None,
        )
    return snapshot


def _xliff_file_data_type(path: Path) -> str:
    root = ElementTree.parse(path).getroot()
    file_element = root.find("{urn:oasis:names:tc:xliff:document:1.2}file")
    assert file_element is not None
    return file_element.attrib["datatype"]


def _write_rich_tmx(path: Path) -> None:
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<tmx version="1.4"><header creationtool="test" creationtoolversion="1" srclang="en-US"/><body>'
        '<tu tuid="rich" usagecount="9"><prop type="x-status">approved</prop><note>Keep the emphasis.</note>'
        '<tuv xml:lang="en-US"><seg>Hello '
        '<bpt i="1" type="bold">&lt;b&gt;</bpt>world<ept i="1">&lt;/b&gt;</ept>.</seg></tuv>'
        '<tuv xml:lang="fr-FR"><seg>Bonjour '
        '<bpt i="1" type="bold">&lt;b&gt;</bpt>monde<ept i="1">&lt;/b&gt;</ept>.</seg></tuv>'
        "</tu></body></tmx>",
        encoding="utf-8",
    )


def _write_rich_xliff(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="messages" source-language="en-US" target-language="fr-FR" datatype="plaintext">
    <body>
      <trans-unit id="rich" xml:space="preserve">
        <source>Hello <g id="1" ctype="bold">world</g>.</source>
        <target state="final">Bonjour <g id="1" ctype="bold">monde</g>.</target>
        <note>Keep the emphasis.</note>
      </trans-unit>
    </body>
  </file>
</xliff>
""",
        encoding="utf-8",
    )


def test_pristine_tmx_to_xliff_uses_native_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.tmx"
    output = tmp_path / "output.xliff"
    _write_simple_tmx(source, units=2)
    trace = _trace_native_conversion(monkeypatch)
    document = lokit.stream.tmx(str(source), "en-US", "fr-FR")
    document.export.xliff(output)

    reparsed = lokit.parse.xliff(str(output), progress=False)
    assert trace.attempts == 1
    assert trace.results == [2]
    assert list(document.items) == []
    assert list(reparsed.data) == ["unit-0", "unit-1"]
    assert [(unit.source, unit.target) for unit in reparsed.data.values()] == [
        ("Source 0 & tea", "Cible 0 & café"),
        ("Source 1 & tea", "Cible 1 & café"),
    ]


def test_pristine_xliff_to_tmx_uses_native_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.xliff"
    output = tmp_path / "output.tmx"
    _write_simple_xliff(source)
    trace = _trace_native_conversion(monkeypatch)
    lokit.stream.xliff(str(source)).export.tmx(output)

    reparsed = lokit.parse.tmx(str(output), "en-US", "fr-FR", progress=False)
    assert trace.attempts == 1
    assert trace.results == [2]
    assert list(reparsed.data) == ["one", "two"]
    assert [(unit.source, unit.target, unit.status) for unit in reparsed.data.values()] == [
        ("One & tea", "Un & café", TranslationStatus.TRANSLATED),
        ("Two < three", "Deux < trois", TranslationStatus.APPROVED),
    ]


def test_pristine_xliff_to_xliff_native_export_preserves_data_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.xliff"
    output = tmp_path / "output.xliff"
    _write_simple_xliff(source, data_type="html")
    trace = _trace_native_conversion(monkeypatch)
    lokit.stream.xliff(str(source)).export.xliff(output)

    reparsed = lokit.parse.xliff(str(output), progress=False)
    assert trace.attempts == 1
    assert trace.results == [2]
    assert _xliff_file_data_type(output) == "html"
    assert {unit.extensions["data_type"] for unit in reparsed.data.values()} == {"html"}


def test_unlocalized_xliff_to_tmx_declines_native_and_drops_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "unlocalized.xliff"
    output = tmp_path / "source-only.tmx"
    _write_unlocalized_xliff(source)
    document = lokit.stream.xliff(str(source))
    trace = _trace_native_conversion(monkeypatch)
    document.export.tmx(output)

    reparsed = lokit.parse.tmx(str(output), "en-US", progress=False)
    assert trace.attempts == 1
    assert trace.results == [None]
    assert document.target_locale is None
    assert list(reparsed.data) == ["orphan"]
    assert reparsed.data["orphan"].source == "Hello"
    assert reparsed.data["orphan"].target is None
    assert reparsed.data["orphan"].targets == {}
    assert "Bonjour" not in output.read_text(encoding="utf-8")


def test_late_multitarget_xliff_fallback_refreshes_stream_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "multitarget.xliff"
    output = tmp_path / "roundtrip.xliff"
    french_units = "".join(
        f'<trans-unit id="fr-{index}"><source>Source {index}</source><target>Fr {index}</target></trans-unit>'
        for index in range(257)
    )
    source.write_text(
        f"""<xliff version="1.2">
<file original="fr" source-language="en" target-language="fr"><body>{french_units}</body></file>
<file original="de" source-language="en" target-language="de"><body>
<trans-unit id="de-unit"><source>German source</source><target>Deutsch</target></trans-unit>
</body></file></xliff>""",
        encoding="utf-8",
    )
    document = lokit.stream.xliff(str(source))
    initial_targets = (document.target_locale, document.target_locales)
    assert initial_targets == ("fr", ("fr",))
    trace = _trace_native_conversion(monkeypatch)

    document.export.xliff(output)

    reparsed = lokit.parse.xliff(str(output), progress=False)
    assert trace.attempts == 1
    assert trace.results == [None]
    assert document.target_locale is None
    assert document.target_locales == ("fr", "de")
    assert document.target_language is None
    assert document.target_languages == ("fr", "de")
    assert reparsed.target_locale is None
    assert reparsed.target_locales == ("fr", "de")
    assert reparsed.data["fr-0"].targets["fr"].text == "Fr 0"
    assert reparsed.data["de-unit"].targets["de"].text == "Deutsch"


def test_partially_consumed_stream_falls_back_and_exports_only_remainder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.tmx"
    output = tmp_path / "remainder.tmx"
    _write_simple_tmx(source)
    document = lokit.stream.tmx(str(source), "en-US", "fr-FR")
    items = iter(document.items)
    assert next(items)[0] == "unit-0"
    trace = _trace_native_conversion(monkeypatch)
    document.export.tmx(output)

    reparsed = lokit.parse.tmx(str(output), "en-US", "fr-FR", progress=False)
    assert trace.attempts == 0
    assert list(reparsed.data) == ["unit-1", "unit-2"]


def test_metadata_mutation_forces_python_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.xliff"
    output = tmp_path / "mutated.xliff"
    _write_simple_xliff(source)
    document = lokit.stream.xliff(str(source))
    document.source_locale = "en-GB"
    trace = _trace_native_conversion(monkeypatch)
    document.export.xliff(output)

    reparsed = lokit.parse.xliff(str(output), progress=False)
    assert trace.attempts == 0
    assert reparsed.source_locale == "en-GB"
    assert [(unit.source, unit.target) for unit in reparsed.data.values()] == [
        ("One & tea", "Un & café"),
        ("Two < three", "Deux < trois"),
    ]


def test_rich_tmx_fallback_preserves_tags_status_and_comment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "rich.tmx"
    output = tmp_path / "rich.xliff"
    _write_rich_tmx(source)
    trace = _trace_native_conversion(monkeypatch)
    lokit.stream.tmx(
        str(source),
        "en-US",
        "fr-FR",
        runtime_placeholders=False,
        inline_placeholders=False,
    ).export.xliff(output)

    unit = lokit.parse.xliff(
        str(output),
        progress=False,
        runtime_placeholders=False,
        inline_placeholders=False,
    ).data["rich"]
    assert trace.attempts == 1
    assert trace.results == [None]
    assert (unit.source, unit.target, unit.status) == ("Hello world.", "Bonjour monde.", TranslationStatus.APPROVED)
    assert [comment.context for comment in unit.comments] == ["Keep the emphasis."]
    assert unit.tags is not None
    assert any(isinstance(part, CodePart) for part in unit.tags.source_parts)
    assert any(isinstance(part, CodePart) for part in unit.tags.target_parts)


def test_rich_xliff_fallback_preserves_tags_status_and_comment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "rich.xliff"
    output = tmp_path / "rich.tmx"
    _write_rich_xliff(source)
    trace = _trace_native_conversion(monkeypatch)
    lokit.stream.xliff(
        str(source),
        runtime_placeholders=False,
        inline_placeholders=False,
    ).export.tmx(output)

    unit = lokit.parse.tmx(
        str(output),
        "en-US",
        "fr-FR",
        progress=False,
        runtime_placeholders=False,
        inline_placeholders=False,
    ).data["rich"]
    assert trace.attempts == 1
    assert trace.results == [None]
    assert (unit.source, unit.target, unit.status) == ("Hello world.", "Bonjour monde.", TranslationStatus.APPROVED)
    assert [comment.context for comment in unit.comments] == ["Keep the emphasis."]
    assert unit.tags is not None
    assert any(isinstance(part, CodePart) for part in unit.tags.source_parts)
    assert any(isinstance(part, CodePart) for part in unit.tags.target_parts)


def test_late_native_parse_failure_preserves_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "late-malformed.tmx"
    output = tmp_path / "existing.xliff"
    valid_units = "".join(
        (
            f'<tu tuid="unit-{index}"><tuv xml:lang="en-US"><seg>Source {index}</seg></tuv>'
            f'<tuv xml:lang="fr-FR"><seg>Cible {index}</seg></tuv></tu>'
        )
        for index in range(256)
    )
    source.write_text(
        '<tmx version="1.4"><header srclang="en-US"/><body>'
        f'{valid_units}<tu tuid="broken"><tuv xml:lang="en-US"><seg>unfinished',
        encoding="utf-8",
    )
    output.write_bytes(b"existing output\n")
    document = lokit.stream.tmx(str(source), "en-US", "fr-FR")
    trace = _trace_native_conversion(monkeypatch)

    with pytest.raises(ValueError, match="ended inside a translation unit"):
        document.export.xliff(output)

    assert trace.attempts == 1
    assert trace.results == []
    assert output.read_bytes() == b"existing output\n"
    assert list(tmp_path.glob(".existing.xliff.*.tmp")) == []


def test_materialized_tmx_to_xliff_uses_native_export_and_preserves_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.tmx"
    output = tmp_path / "edited.xliff"
    _write_simple_tmx(source, units=2)
    document = lokit.parse.tmx(str(source), "en-US", "fr-FR", progress=False)
    document.data["unit-0"].source = "Edited <source> & tea"
    document.data["unit-0"].target = "Cible éditée & café"
    document.data["unit-0"].status = TranslationStatus.REVIEWED
    trace = _trace_native_base_export(monkeypatch)
    document.export.xliff(output)

    reparsed = lokit.parse.xliff(str(output), progress=False)
    assert trace.attempts == 1
    assert trace.results == [2]
    assert list(reparsed.data) == ["unit-0", "unit-1"]
    assert (
        reparsed.data["unit-0"].source,
        reparsed.data["unit-0"].target,
        reparsed.data["unit-0"].status,
    ) == ("Edited <source> & tea", "Cible éditée & café", TranslationStatus.REVIEWED)


def test_materialized_xliff_native_export_preserves_data_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "html.xliff"
    document = BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        target_locales=("fr-FR",),
        data={
            "html": Data(
                source="<p>Hello</p>",
                target="<p>Bonjour</p>",
                extensions={"data_type": "html"},
            )
        },
    )
    trace = _trace_native_base_export(monkeypatch)
    document.export.xliff(output)

    assert trace.attempts == 1
    assert trace.results == [1]
    assert _xliff_file_data_type(output) == "html"


def test_materialized_xliff_to_tmx_uses_native_export_and_preserves_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.xliff"
    output = tmp_path / "edited.tmx"
    _write_simple_xliff(source)
    document = lokit.parse.xliff(str(source), progress=False)
    document.data["two"].source = "Edited two & more"
    document.data["two"].target = "Deux modifié & plus"
    document.data["two"].status = TranslationStatus.REVIEWED
    trace = _trace_native_base_export(monkeypatch)
    document.export.tmx(output)

    reparsed = lokit.parse.tmx(str(output), "en-US", "fr-FR", progress=False)
    assert trace.attempts == 1
    assert trace.results == [2]
    assert list(reparsed.data) == ["one", "two"]
    assert (
        reparsed.data["two"].source,
        reparsed.data["two"].target,
        reparsed.data["two"].status,
    ) == ("Edited two & more", "Deux modifié & plus", TranslationStatus.REVIEWED)


def test_materialized_xliff_target_selection_matches_python_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_output = tmp_path / "native.xliff"
    python_output = tmp_path / "python.xliff"
    document = BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        target_locales=("fr-FR",),
        data={
            "exact": Data(
                source="Exact source",
                target="Legacy exact",
                status=TranslationStatus.DRAFT,
                targets={
                    "fr-FR": TargetData(
                        text="Exact target",
                        status=TranslationStatus.APPROVED,
                    )
                },
            ),
            "exact-none": Data(
                source="Exact missing source",
                target="Legacy must disappear",
                status=TranslationStatus.TRANSLATED,
                targets={"fr-FR": TargetData(text=None)},
            ),
            "sole": Data(
                source="Sole source",
                targets={
                    "de-DE": TargetData(
                        text="Sole target",
                        status=TranslationStatus.REVIEWED,
                    )
                },
            ),
            "legacy": Data(
                source="Legacy source",
                target="Legacy target",
                status=TranslationStatus.TRANSLATED,
                targets={
                    "de-DE": TargetData(
                        text="Non-matching sole target",
                        status=TranslationStatus.APPROVED,
                    )
                },
            ),
        },
    )
    lokit.parse.write.xliff(document, python_output)
    trace = _trace_native_base_export(monkeypatch)
    document.export.xliff(native_output)

    expected = {
        "exact": (True, "Exact target", "final"),
        "exact-none": (False, None, None),
        "sole": (True, "Sole target", "needs-review-l10n"),
        "legacy": (True, "Legacy target", "translated"),
    }
    assert trace.attempts == 1
    assert trace.results == [4]
    assert _xliff_unit_snapshot(python_output) == expected
    assert _xliff_unit_snapshot(native_output) == expected


def test_materialized_tmx_keeps_data_status_over_target_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "status.tmx"
    document = BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        target_locales=("fr-FR",),
        data={
            "status": Data(
                source="Hello",
                status=TranslationStatus.REVIEWED,
                targets={
                    "fr-FR": TargetData(
                        text="Bonjour",
                        status=TranslationStatus.APPROVED,
                    )
                },
            )
        },
    )
    trace = _trace_native_base_export(monkeypatch)
    document.export.tmx(output)

    unit = lokit.parse.tmx(str(output), "en-US", "fr-FR", progress=False).data["status"]
    assert trace.attempts == 1
    assert trace.results == [1]
    assert (unit.source, unit.target, unit.status) == (
        "Hello",
        "Bonjour",
        TranslationStatus.REVIEWED,
    )


def test_materialized_xliff_preserves_empty_unit_id_and_omits_empty_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "empty-attributes.xliff"
    document = BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        target_locales=("fr-FR",),
        data={
            "fallback-id": Data(
                source="Hello",
                target="Bonjour",
                extensions={"unit_id": "", "space": ""},
            )
        },
    )
    trace = _trace_native_base_export(monkeypatch)
    document.export.xliff(output)

    root = ElementTree.parse(output).getroot()
    unit = root.find(".//{urn:oasis:names:tc:xliff:document:1.2}trans-unit")
    assert trace.attempts == 1
    assert trace.results == [1]
    assert unit is not None
    assert unit.attrib["id"] == "fallback-id"
    assert unit.attrib["{urn:lokit:provenance:1}original-unit-id"] == ""
    assert "{http://www.w3.org/XML/1998/namespace}space" not in unit.attrib


def test_materialized_rich_tmx_falls_back_and_preserves_unit_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "rich.tmx"
    output = tmp_path / "roundtrip.tmx"
    _write_rich_tmx(source)
    document = lokit.parse.tmx(
        str(source),
        "en-US",
        "fr-FR",
        progress=False,
    )
    original = document.data["rich"]
    trace = _trace_native_base_export(monkeypatch)
    document.export.tmx(output)

    unit = lokit.parse.tmx(
        str(output),
        "en-US",
        "fr-FR",
        progress=False,
    ).data["rich"]
    assert trace.attempts == 1
    assert trace.results == [None]
    assert (unit.source, unit.target, unit.status) == (original.source, original.target, original.status)
    assert unit.meta.usage_count == 9
    assert [comment.context for comment in unit.comments] == ["Keep the emphasis."]
    assert unit.tags is not None
    assert any(isinstance(part, CodePart) for part in unit.tags.source_parts)
    assert any(isinstance(part, CodePart) for part in unit.tags.target_parts)


def test_materialized_group_by_resource_bypasses_native_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "grouped.xliff"
    document = BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        target_locales=("fr-FR",),
        data={
            "one": Data(
                source="One",
                target="Un",
                extensions={"resource": "first", "unit_id": "one"},
            ),
            "two": Data(
                source="Two",
                target="Deux",
                extensions={"resource": "second", "unit_id": "two"},
            ),
        },
    )
    trace = _trace_native_base_export(monkeypatch)
    document.export.xliff(output, group_by_resource=True)

    reparsed = lokit.parse.xliff(str(output), progress=False)
    assert trace.attempts == 0
    assert [unit.extensions["resource"] for unit in reparsed.data.values()] == [
        "first",
        "second",
    ]
    assert [(unit.source, unit.target) for unit in reparsed.data.values()] == [
        ("One", "Un"),
        ("Two", "Deux"),
    ]


def test_materialized_native_export_failure_preserves_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "existing.tmx"
    output.write_bytes(b"existing output\n")
    document = BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        target_locales=("fr-FR",),
        data={
            "valid": Data(source="Valid", target="Valide"),
            "invalid": Data(source="\ud800", target="Invalide"),
        },
    )
    trace = _trace_native_base_export(monkeypatch)

    with pytest.raises(UnicodeError):
        document.export.tmx(output)

    assert trace.attempts == 1
    assert trace.results == []
    assert output.read_bytes() == b"existing output\n"
    assert list(tmp_path.glob(".existing.tmx.*.tmp")) == []


def test_materialized_illegal_xml_control_declines_native_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "existing-control.tmx"
    output.write_bytes(b"existing output\n")
    document = BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        target_locales=("fr-FR",),
        data={"invalid": Data(source="Invalid\x01source", target="Invalide")},
    )
    trace = _trace_native_base_export(monkeypatch)

    with pytest.raises(ValueError, match="XML compatible"):
        document.export.tmx(output)

    assert trace.attempts == 1
    assert trace.results == [None]
    assert output.read_bytes() == b"existing output\n"
    assert list(tmp_path.glob(".existing-control.tmx.*.tmp")) == []


def test_native_conversion_rejects_illegal_numeric_character_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "illegal-reference.tmx"
    output = tmp_path / "existing-reference.xliff"
    source.write_text(
        '<tmx version="1.4"><header srclang="en-US"/><body>'
        '<tu tuid="bad"><tuv xml:lang="en-US"><seg>Invalid &#1; source</seg></tuv>'
        '<tuv xml:lang="fr-FR"><seg>Invalide</seg></tuv></tu></body></tmx>',
        encoding="utf-8",
    )
    output.write_bytes(b"existing output\n")
    document = lokit.stream.tmx(str(source), "en-US", "fr-FR")
    trace = _trace_native_conversion(monkeypatch)

    with pytest.raises(ValueError, match=r"U\+0001.*not permitted in XML 1\.0"):
        document.export.xliff(output)

    assert trace.attempts == 1
    assert trace.results == []
    assert output.read_bytes() == b"existing output\n"
    assert list(tmp_path.glob(".existing-reference.xliff.*.tmp")) == []


def test_native_same_format_copy_validates_all_attribute_character_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "illegal-attribute-reference.tmx"
    output = tmp_path / "existing-copy.tmx"
    source.write_text(
        '<tmx version="1.4" unused="&#1;"><header srclang="en-US"/><body>'
        '<tu tuid="valid"><tuv xml:lang="en-US"><seg>Valid</seg></tuv>'
        '<tuv xml:lang="fr-FR"><seg>Valide</seg></tuv></tu></body></tmx>',
        encoding="utf-8",
    )
    output.write_bytes(b"existing output\n")
    trace = _trace_native_conversion(monkeypatch)

    with pytest.raises(ValueError, match=r"U\+0001.*not permitted in XML 1\.0"):
        document = lokit.stream.tmx(str(source), "en-US", "fr-FR", include_tags=True)
        document.export.tmx(output)

    assert trace.attempts == 0
    assert trace.results == []
    assert output.read_bytes() == b"existing output\n"
    assert list(tmp_path.glob(".existing-copy.tmx.*.tmp")) == []
