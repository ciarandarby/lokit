from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import lokit
from lokit.data.structure import (
    CodePart,
    Comment,
    Data,
    Meta,
    Origin,
    StreamingStructure,
    Tags,
    TargetData,
    TargetTags,
    TextPart,
    TranslationStatus,
)
from lokit.data.tag_types import TieData, TieType
from lokit.types import DEFAULT_DICT_FIELDS, DictField, StringMode


def _write_rich_tmx(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4" xmlns:v="urn:vendor">
  <header creationtool="test" creationtoolversion="1" segtype="sentence"
          o-tmf="test" adminlang="en-US" srclang="en-US" datatype="PlainText"/>
  <body>
    <tu tuid="rich" usagecount="9">
      <prop type="x-system">memoQ</prop>
      <prop type="x-domain">checkout</prop>
      <prop type="x-project">website</prop>
      <prop type="x-status">approved</prop>
      <tuv xml:lang="en-US"><seg>Hello <hi type="bold" vendor="yes"
          v:flavor="rich">world <ph x="1">&lt;br/&gt;</ph></hi>!</seg></tuv>
      <tuv xml:lang="fr-FR"><seg><bpt i="1" x="bold">&lt;b&gt;</bpt>Bonjour<ept i="1">&lt;/b&gt;</ept></seg></tuv>
      <tuv xml:lang="de-DE"><seg>Hallo</seg></tuv>
    </tu>
  </body>
</tmx>
""",
        encoding="utf-8",
    )


def _write_rich_xliff(path: Path) -> None:
    path.write_text(
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


def _write_duplicate_resource_xliff(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2">
  <file original="first/messages" source-language="en-US" target-language="fr-FR" datatype="plaintext">
    <body>
      <trans-unit id="same"><source>First</source><target state="translated">Premier</target></trans-unit>
    </body>
  </file>
  <file original="second/messages" source-language="en-US" target-language="de-DE" datatype="plaintext">
    <body>
      <trans-unit id="same">
        <source>Second <g id="1" ctype="bold">item</g></source>
        <target state="final">Zweite <g id="1" ctype="bold">Sache</g></target>
      </trans-unit>
    </body>
  </file>
</xliff>
""",
        encoding="utf-8",
    )


def _write_namespaced_xliff(path: Path) -> None:
    path.write_text(
        """<xliff version="1.2" xmlns:v="urn:vendor" xmlns:p="urn:shared" xmlns:q="urn:shared">
<file original="namespaced" source-language="en" target-language="fr"><body>
<trans-unit id="ns"><source>A<v:span q:kind="bold">B<v:x p:name="token"/>C</v:span>D</source>
<target>W<v:span q:kind="bold">X<v:x p:name="jeton"/>Y</v:span>Z</target></trans-unit>
</body></file></xliff>""",
        encoding="utf-8",
    )


def test_public_projection_types_and_default_fields() -> None:
    assert tuple(field.value for field in DEFAULT_DICT_FIELDS) == (
        "source_language",
        "target_language",
        "source",
        "target",
        "domain",
    )
    assert StringMode.RAW.value == "raw"
    assert DictField.TARGET_LOCALE.value == "target_locale"


def test_tmx_to_dict_is_lazy_flat_stable_and_preserves_native_raw_tags(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rich.tmx"
    _write_rich_tmx(path)

    projected = lokit.stream.to_dict(path)
    assert isinstance(projected, Iterator)
    sanitized = list(projected)
    assert [row["target_language"] for row in sanitized] == ["fr", "de"]
    assert list(sanitized[0]) == [
        "source_language",
        "target_language",
        "source",
        "target",
        "domain",
    ]
    assert sanitized[0] == {
        "source_language": "en",
        "target_language": "fr",
        "source": "Hello world !",
        "target": "Bonjour",
        "domain": "checkout",
    }

    fields = (
        DictField.UNIT_ID,
        DictField.SOURCE_LOCALE,
        DictField.TARGET_LOCALE,
        DictField.SOURCE,
        DictField.TARGET,
        DictField.DOMAIN,
        DictField.PROJECT,
        DictField.STATUS,
    )
    raw = lokit.parse.to_dict(path, target_language="fr-FR", fields=fields, strings=StringMode.RAW)
    assert raw == [
        {
            "unit_id": "rich",
            "source_locale": "en-US",
            "target_locale": "fr-FR",
            "source": ('Hello <hi type="bold" vendor="yes" v:flavor="rich">world <ph x="1">&lt;br/&gt;</ph></hi>!'),
            "target": ('<bpt i="1" x="bold">&lt;b&gt;</bpt>Bonjour<ept i="1">&lt;/b&gt;</ept>'),
            "domain": "checkout",
            "project": "website",
            "status": "approved",
        }
    ]
    assert lokit.parse.to_dict(path, domain="override")[0]["domain"] == "override"
    assert next(iter(lokit.stream.tmx(str(path)).items))[1].meta.usage_count == 9


def test_duplicate_and_generated_tmx_ids_are_collision_safe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate-ids.tmx"
    output = tmp_path / "split-fr.tmx"
    path.write_text(
        """<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="alpha"><tuv xml:lang="en"><seg>one</seg></tuv><tuv xml:lang="fr"><seg>un</seg></tuv></tu>
<tu tuid="alpha#2"><tuv xml:lang="en"><seg>two</seg></tuv><tuv xml:lang="fr"><seg>deux</seg></tuv></tu>
<tu tuid="alpha"><tuv xml:lang="en"><seg>three</seg></tuv><tuv xml:lang="fr"><seg>trois</seg></tuv></tu>
<tu><tuv xml:lang="en"><seg>four</seg></tuv><tuv xml:lang="fr"><seg>quatre</seg></tuv></tu>
<tu tuid="auto_0"><tuv xml:lang="en"><seg>five</seg></tuv><tuv xml:lang="fr"><seg>cinq</seg></tuv></tu>
</body></tmx>""",
        encoding="utf-8",
    )

    native = [(unit_id, data.source, data.extensions["unit_id"]) for unit_id, data in lokit.stream.tmx(str(path)).items]
    assert native == [
        ("alpha", "one", "alpha"),
        ("alpha#2", "two", "alpha#2"),
        ("alpha#3", "three", "alpha"),
        ("auto_0", "four", ""),
        ("auto_0#2", "five", "auto_0"),
    ]
    document = lokit.parse.tmx(str(path), progress=False)
    assert list(document.data) == [item[0] for item in native]
    with lokit.stream.tmx(str(path)).split_targets(include_missing=False) as streamed:
        assert [unit_id for unit_id, _ in streamed["fr"].items] == [item[0] for item in native]

    split = document.split_targets(("fr",), include_missing=False)["fr"]
    split.export.tmx(output)
    reparsed = lokit.parse.tmx(str(output), progress=False)
    assert list(reparsed.data) == [item[0] for item in native]
    assert [unit.extensions["unit_id"] for unit in reparsed.data.values()] == [
        "alpha",
        "alpha#2",
        "alpha",
        "",
        "auto_0",
    ]


def test_xliff_raw_and_sanitized_projection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rich.xliff"
    _write_rich_xliff(path)
    fields = (
        DictField.SOURCE,
        DictField.TARGET,
        DictField.RESOURCE,
        DictField.STATUS,
    )

    sanitized = lokit.parse.to_dict(path, fields=fields)
    raw = lokit.parse.to_dict(path, fields=fields, strings=StringMode.RAW)
    assert sanitized == [
        {
            "source": "Save now ",
            "target": "Enregistrer maintenant",
            "resource": "app/messages",
            "status": "translated",
        }
    ]
    assert raw == [
        {
            "source": ('Save <g id="1" ctype="bold" vendor="yes">now <x id="2" equiv-text="?"/></g>'),
            "target": 'Enregistrer <g id="1" ctype="bold">maintenant</g>',
            "resource": "app/messages",
            "status": "translated",
        }
    ]


def test_namespaced_raw_xliff_survives_native_and_lokit_round_trip(
    tmp_path: Path,
) -> None:
    path = tmp_path / "namespaced.xliff"
    lokit_path = tmp_path / "namespaced.lokit"
    _write_namespaced_xliff(path)
    fields = (DictField.SOURCE, DictField.TARGET)
    expected = [
        {
            "source": ('A<v:span q:kind="bold">B<v:x p:name="token"/>C</v:span>D'),
            "target": ('W<v:span q:kind="bold">X<v:x p:name="jeton"/>Y</v:span>Z'),
        }
    ]
    sanitized = [{"source": "ABCD", "target": "WXYZ"}]

    assert lokit.parse.to_dict(path, fields=fields, strings=StringMode.RAW) == expected
    assert lokit.parse.to_dict(path, fields=fields) == sanitized
    document = lokit.parse.xliff(str(path), progress=False)
    assert document.to_dict(fields=fields, strings=StringMode.RAW) == expected
    assert document.to_dict(fields=fields) == sanitized
    lokit.export.lokit(document, lokit_path)
    assert lokit.parse.to_dict(lokit_path, fields=fields, strings=StringMode.RAW) == expected
    assert lokit.parse.to_dict(lokit_path, fields=fields) == sanitized


@pytest.mark.asyncio
async def test_namespaced_nested_xliff_raw_and_sanitized_async_projection(tmp_path: Path) -> None:
    path = tmp_path / "namespaced-async.xliff"
    _write_namespaced_xliff(path)
    fields = (DictField.SOURCE, DictField.TARGET)
    raw = [
        {
            "source": ('A<v:span q:kind="bold">B<v:x p:name="token"/>C</v:span>D'),
            "target": ('W<v:span q:kind="bold">X<v:x p:name="jeton"/>Y</v:span>Z'),
        }
    ]
    sanitized = [{"source": "ABCD", "target": "WXYZ"}]

    assert await lokit.parse.async_.to_dict(path, fields=fields, strings=StringMode.RAW) == raw
    assert await lokit.parse.async_.to_dict(path, fields=fields) == sanitized
    assert [row async for row in lokit.stream.async_.to_dict(path, fields=fields, strings=StringMode.RAW)] == raw
    assert [row async for row in lokit.stream.async_.to_dict(path, fields=fields)] == sanitized


def test_duplicate_xliff_ids_are_unique_across_resources_and_survive_split_export(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate-resources.xliff"
    de_output = tmp_path / "de.xliff"
    _write_duplicate_resource_xliff(path)
    fields = (
        DictField.UNIT_ID,
        DictField.RESOURCE,
        DictField.TARGET_LOCALE,
        DictField.SOURCE,
        DictField.TARGET,
    )

    native_rows = lokit.parse.to_dict(path, fields=fields, strings=StringMode.RAW)
    assert native_rows == [
        {
            "unit_id": "same",
            "resource": "first/messages",
            "target_locale": "fr-FR",
            "source": "First",
            "target": "Premier",
        },
        {
            "unit_id": "1:same",
            "resource": "second/messages",
            "target_locale": "de-DE",
            "source": 'Second <g id="1" ctype="bold">item</g>',
            "target": 'Zweite <g id="1" ctype="bold">Sache</g>',
        },
    ]
    native_document = lokit.parse.xliff(str(path), progress=False)
    assert list(native_document.data) == ["same", "1:same"]
    assert native_document.data["same"].extensions["unit_id"] == "same"
    assert native_document.data["1:same"].extensions["unit_id"] == "same"
    with lokit.stream.xliff(str(path)).split_targets(include_missing=False) as streamed:
        assert [unit_id for unit_id, _ in streamed["fr-FR"].items] == ["same"]
        assert [unit_id for unit_id, _ in streamed["de-DE"].items] == ["1:same"]

    split = native_document.split_targets(include_missing=False)
    assert list(split["fr-FR"].data) == ["same"]
    assert list(split["de-DE"].data) == ["1:same"]
    assert split["de-DE"].data["1:same"].status is TranslationStatus.APPROVED
    assert split["de-DE"].data["1:same"].tags is not None
    split["de-DE"].export.xliff(de_output, group_by_resource=True)
    exported = lokit.parse.xliff(str(de_output), progress=False)
    assert list(exported.data) == ["same"]
    assert exported.data["same"].extensions["resource"] == "second/messages"
    assert exported.data["same"].target == "Zweite Sache"
    assert exported.data["same"].status is TranslationStatus.APPROVED
    assert lokit.parse.to_dict(
        de_output,
        fields=(DictField.SOURCE, DictField.TARGET),
        strings=StringMode.RAW,
    ) == [
        {
            "source": 'Second <bx id="c0" rid="p0"/>item<ex id="c1" rid="p0"/>',
            "target": 'Zweite <bx id="c0" rid="p0"/>Sache<ex id="c1" rid="p0"/>',
        }
    ]


def test_xliff_generated_ids_and_explicit_collisions_are_deterministic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "id-collisions.xliff"
    path.write_text(
        """<xliff version="1.2"><file source-language="en" target-language="fr"><body>
<trans-unit id="0"><source>explicit-zero</source><target>one</target></trans-unit>
<trans-unit><source>missing-one</source><target>two</target></trans-unit>
<trans-unit><source>missing-two</source><target>three</target></trans-unit>
<trans-unit id="0:0#2"><source>explicit-generated-shape</source><target>four</target></trans-unit>
<trans-unit id="0"><source>duplicate-zero</source><target>five</target></trans-unit>
</body></file></xliff>""",
        encoding="utf-8",
    )

    native = [
        (unit_id, data.source, data.extensions["unit_id"]) for unit_id, data in lokit.stream.xliff(str(path)).items
    ]
    assert native == [
        ("0", "explicit-zero", "0"),
        ("0:0", "missing-one", ""),
        ("0:0#2", "missing-two", ""),
        ("0:0:0#2", "explicit-generated-shape", "0:0#2"),
        ("0:0#3", "duplicate-zero", "0"),
    ]


def test_lokit_projection_round_trip_validation_and_source_only_rows(tmp_path: Path) -> None:
    tmx_path = tmp_path / "rich.tmx"
    lokit_path = tmp_path / "rich.lokit"
    source_only_path = tmp_path / "source-only.lokit"
    _write_rich_tmx(tmx_path)
    imported = lokit.parse.tmx(str(tmx_path), progress=False)
    lokit.export.lokit(imported, lokit_path)
    fields = (DictField.SOURCE, DictField.TARGET, DictField.TARGET_LOCALE)

    assert lokit.parse.to_dict(
        lokit_path,
        target_language="fr-FR",
        fields=fields,
        strings=StringMode.RAW,
    ) == lokit.parse.to_dict(
        tmx_path,
        target_language="fr-FR",
        fields=fields,
        strings=StringMode.RAW,
    )

    source_only = lokit.types.BaseStructure(
        source_locale="en-US",
        target_locale=None,
        data={"only": Data(source="Only source")},
    )
    lokit.export.lokit(source_only, source_only_path)
    expected = [
        {
            "source_language": "en",
            "target_language": "",
            "source": "Only source",
            "target": "",
            "domain": "",
        }
    ]
    assert lokit.parse.to_dict(source_only_path) == expected
    assert source_only.to_dict() == expected

    with pytest.raises(ValueError, match="duplicate dictionary field"):
        lokit.stream.to_dict(tmx_path, fields=(DictField.SOURCE, DictField.SOURCE))
    with pytest.raises(ValueError):
        lokit.stream.to_dict(tmx_path, fields=("not-a-field",))
    with pytest.raises(ValueError):
        lokit.stream.to_dict(tmx_path, strings="not-a-mode")


@pytest.mark.asyncio
async def test_sync_stream_and_async_dict_projections_are_equal(tmp_path: Path) -> None:
    path = tmp_path / "rich.tmx"
    _write_rich_tmx(path)

    streamed = list(lokit.stream.to_dict(path, target_language="de"))
    materialized = lokit.parse.to_dict(path, target_language="de")
    async_streamed = [row async for row in lokit.stream.async_.to_dict(path, target_language="de")]
    async_materialized = await lokit.parse.async_.to_dict(path, target_language="de")

    assert streamed == materialized == async_streamed == async_materialized
    assert streamed[0]["target"] == "Hallo"


def test_quick_parse_tmx_to_json_threads_language_selection(tmp_path: Path) -> None:
    path = tmp_path / "rich.tmx"
    output = tmp_path / "de.jsonl"
    _write_rich_tmx(path)

    result = lokit.convert.tmx_to_json(
        path,
        output,
        source_language="en-US",
        target_language="de-DE",
    )

    assert result == output
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert records == [{"id": "rich", "source": "Hello world !", "target": "Hallo"}]


@pytest.mark.asyncio
async def test_async_quick_parse_tmx_to_json_threads_language_selection(tmp_path: Path) -> None:
    path = tmp_path / "rich.tmx"
    output = tmp_path / "fr.jsonl"
    _write_rich_tmx(path)

    result = await lokit.async_.convert.tmx_to_json(
        path,
        output,
        source_language="en-US",
        target_language="fr-FR",
    )

    assert result == output
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert records == [{"id": "rich", "source": "Hello world !", "target": "Bonjour"}]


def test_base_split_targets_are_independent_and_can_omit_missing_units() -> None:
    source_code = TieData(
        id="source",
        type=TieType.CUSTOM_STANDALONE,
        attributes={"vendor": "source"},
        original_name="ph",
    )
    fr_code = TieData(
        id="fr",
        type=TieType.CUSTOM_STANDALONE,
        attributes={"vendor": "fr"},
        original_name="ph",
    )
    document = lokit.types.BaseStructure(
        source_locale="en-US",
        target_locale=None,
        target_locales=("fr-FR", "de-DE"),
        data={
            "translated": Data(
                source="Source",
                tags=Tags(
                    source_tag_map={"source": source_code},
                    source_parts=[TextPart("Source"), CodePart("source")],
                ),
                comments=[Comment(context="base", origin=Origin(project="base"))],
                targets={
                    "fr-FR": TargetData(
                        text="Français",
                        status=TranslationStatus.APPROVED,
                        tags=TargetTags(
                            tag_map={"fr": fr_code},
                            parts=[TextPart("Français"), CodePart("fr")],
                        ),
                        meta=Meta(usage_count=4),
                        comments=[Comment(context="fr", origin=Origin(project="fr"))],
                        extensions={"target": "fr"},
                    ),
                    "de-DE": TargetData(text="Deutsch"),
                },
            ),
            "missing-de": Data(
                source="Only French",
                targets={"fr-FR": TargetData(text="Seulement français")},
            ),
        },
    )

    split = document.split_targets()
    assert list(split) == ["fr-FR", "de-DE"]
    assert split["fr-FR"].data["translated"].target == "Français"
    assert split["fr-FR"].data["translated"].meta.usage_count == 4
    assert split["de-DE"].data["translated"].target == "Deutsch"
    assert split["de-DE"].data["missing-de"].target is None

    fr_unit = split["fr-FR"].data["translated"]
    assert fr_unit.tags is not None
    fr_unit.tags.source_tag_map["source"].attributes["vendor"] = "changed"
    assert document.data["translated"].tags is not None
    assert document.data["translated"].tags.source_tag_map["source"].attributes["vendor"] == "source"
    assert split["de-DE"].data["translated"].tags is not None
    assert split["de-DE"].data["translated"].tags.source_tag_map["source"].attributes["vendor"] == "source"

    without_missing = document.split_targets(("de-DE",), include_missing=False)
    assert list(without_missing["de-DE"].data) == ["translated"]
    with pytest.raises(ValueError, match="empty value"):
        document.split_targets(("",))
    with pytest.raises(ValueError, match="empty value"):
        StreamingStructure(
            source_locale="en",
            target_locale=None,
            items=(),
        ).split_targets(("",))


def test_streaming_split_spools_once_discovers_late_locales_and_cleans_up() -> None:
    yielded: list[str] = []

    def source_items() -> Iterator[tuple[str, Data]]:
        yielded.append("source")
        yield "source", Data(source="Source only")
        yielded.append("translated")
        yield (
            "translated",
            Data(
                source="Hello",
                targets={
                    "fr-FR": TargetData(text="Bonjour"),
                    "de-DE": TargetData(text="Hallo"),
                },
            ),
        )

    document = StreamingStructure(
        source_locale="en-US",
        target_locale=None,
        items=source_items(),
        target_locales=(),
        source_language="en",
    )
    context = document.split_targets()
    assert yielded == []

    with context as splits:
        assert yielded == ["source", "translated"]
        assert list(splits) == ["fr-FR", "de-DE"]
        assert splits["fr-FR"].target_locale == "fr-FR"
        assert splits["fr-FR"].target_locales == ("fr-FR",)
        assert [unit.target for _, unit in splits["fr-FR"].items] == [None, "Bonjour"]
        assert [unit.target for _, unit in splits["de-DE"].items] == [None, "Hallo"]
        saved = splits["fr-FR"]

    with pytest.raises(FileNotFoundError):
        list(saved.items)


def test_streaming_split_include_missing_false_and_xliff_stream_export(tmp_path: Path) -> None:
    source = StreamingStructure(
        source_locale="en",
        target_locale=None,
        items=iter(
            (
                ("missing", Data(source="Missing")),
                ("translated", Data(source="Hello", targets={"fr": TargetData(text="Bonjour")})),
            )
        ),
    )
    with source.split_targets(("fr",), include_missing=False) as split:
        assert [unit_id for unit_id, _ in split["fr"].items] == ["translated"]

    input_path = tmp_path / "input.xliff"
    output_path = tmp_path / "output.xliff"
    _write_rich_xliff(input_path)
    lokit.stream.xliff(str(input_path)).export.xliff(output_path)
    reparsed = lokit.parse.xliff(str(output_path), progress=False)
    assert reparsed.data["save"].target == "Enregistrer maintenant"


def test_explicit_streaming_split_is_one_pass_and_not_reusable() -> None:
    yielded: list[str] = []

    def source_items() -> Iterator[tuple[str, Data]]:
        yielded.append("one")
        yield (
            "one",
            Data(
                source="Hello",
                targets={
                    "fr": TargetData(text="Bonjour"),
                    "de": TargetData(text="Hallo"),
                },
            ),
        )

    context = StreamingStructure(
        source_locale="en",
        target_locale=None,
        items=source_items(),
    ).split_targets(("fr", "de"))
    with context as split:
        assert yielded == ["one"]
        temporary_directory = context._temporary_directory
        assert temporary_directory is not None
        directory = Path(temporary_directory.name)
        assert not (directory / "source.lokit").exists()
        assert [unit.target for _, unit in split["fr"].items] == ["Bonjour"]
        assert [unit.target for _, unit in split["de"].items] == ["Hallo"]

    assert not directory.exists()
    with pytest.raises(RuntimeError, match="cannot be reused"), context:
        pass


def test_streaming_split_cleans_up_when_source_iteration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[Path] = []

    def recording_temporary_directory(*, prefix: str) -> TemporaryDirectory[str]:
        temporary_directory = TemporaryDirectory(prefix=prefix)
        created.append(Path(temporary_directory.name))
        return temporary_directory

    def broken_items() -> Iterator[tuple[str, Data]]:
        yield "one", Data(source="Hello", targets={"fr": TargetData(text="Bonjour")})
        raise RuntimeError("broken input")

    monkeypatch.setattr(
        "lokit.data.targets.TemporaryDirectory",
        recording_temporary_directory,
    )
    context = StreamingStructure(
        source_locale="en",
        target_locale=None,
        items=broken_items(),
    ).split_targets(("fr",))

    with pytest.raises(RuntimeError, match="broken input"), context:
        pass

    assert len(created) == 1
    assert not created[0].exists()
    assert context._temporary_directory is None


def test_streaming_split_closes_partially_consumed_target_iterators() -> None:
    source = StreamingStructure(
        source_locale="en",
        target_locale=None,
        items=iter(
            (
                ("one", Data(source="One", targets={"fr": TargetData(text="Un")})),
                ("two", Data(source="Two", targets={"fr": TargetData(text="Deux")})),
            )
        ),
    )
    context = source.split_targets(("fr",))

    with context as split:
        iterator = iter(split["fr"].items)
        assert next(iterator)[0] == "one"
        temporary_directory = context._temporary_directory
        assert temporary_directory is not None
        directory = Path(temporary_directory.name)
        assert directory.exists()

    assert not directory.exists()
    with pytest.raises(StopIteration):
        next(iterator)
