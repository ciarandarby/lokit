from __future__ import annotations

from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

if TYPE_CHECKING:
    from pathlib import Path

    from lokit.db.operations import TranslationMemory

import pytest

import lokit
from lokit.data.structure import (
    AdjacentContext,
    BaseStructure,
    Comment,
    Data,
    Meta,
    Origin,
    Plural,
    PluralCategory,
    TargetData,
    TranslationStatus,
)
from lokit.db.connection import _connection_info, _resolve_password_factory, _sanitize_uri
from lokit.db.matching import rows_to_match_results
from lokit.db.models import (
    CommentFetchRow,
    MatchRow,
    PartFetchRow,
    TagFetchRow,
    UnitFetchRow,
    UnitWithChildren,
)
from lokit.db.operations import _deduplicate_batch, _iter_serialized_document
from lokit.db.queries import MATCH_QUERY
from lokit.db.schema import partition_name_for_locale
from lokit.db.serialization import deserialize_unit, serialize_unit


def test_db_serialization_roundtrip_preserves_nested_data(
    sample_document: BaseStructure,
) -> None:
    unit = sample_document.data["unit5"]
    unit.meta = Meta(
        usage_count=7,
        last_used="20260606T120000Z",
        created="20260601T120000Z",
        updated="20260602T120000Z",
        extensions={"change_id": "editor-1"},
    )
    unit.comments = [
        Comment(
            context="Needs review",
            timestamp="20260603T120000Z",
            origin=Origin(system="tmx", project="checkout", creator_id="ciaran"),
            context_key="checkout.button",
            extensions={"severity": "low"},
        )
    ]
    unit.previous_context = AdjacentContext(
        unit_id="before",
        source="Previous source",
        target="Texte précédent",
        extensions={"kind": "ui"},
    )
    unit.next_context = AdjacentContext(unit_id="after", source="Next source")

    serialized = serialize_unit(
        "unit5",
        unit,
        sample_document.source_locale,
        sample_document.target_locale or "",
        "checkout",
        "web",
    )
    db_id = serialized.unit.id
    children = UnitWithChildren(
        unit=UnitFetchRow(
            id=db_id,
            unit_key=serialized.unit.unit_key,
            source_text=serialized.unit.source_text,
            target_text=serialized.unit.target_text,
            source_locale=serialized.unit.source_locale,
            target_locale=serialized.unit.target_locale,
            status=serialized.unit.status,
            previous_source=serialized.unit.previous_source,
            next_source=serialized.unit.next_source,
            usage_count=serialized.unit.usage_count,
            plural_variant=serialized.unit.plural_variant,
            plural_count=serialized.unit.plural_count,
            plural_category=serialized.unit.plural_category,
            extensions=serialized.unit.extensions,
        ),
        tags=[
            TagFetchRow(
                unit_id=db_id,
                source_locale=row.source_locale,
                tag_id=row.tag_id,
                tag_type=row.tag_type,
                position=row.position,
                tag_order=row.tag_order,
                attribute_data=row.attribute_data,
                pair_id=row.pair_id,
                original_name=row.original_name,
                original_text=row.original_text,
                attributes=row.attributes,
                is_source=row.is_source,
            )
            for row in serialized.tags
        ],
        parts=[
            PartFetchRow(
                unit_id=db_id,
                source_locale=row.source_locale,
                is_source=row.is_source,
                position=row.position,
                part_type=row.part_type,
                value=row.value,
            )
            for row in serialized.parts
        ],
        comments=[
            CommentFetchRow(
                unit_id=db_id,
                source_locale=row.source_locale,
                context=row.context,
                timestamp=row.timestamp,
                context_key=row.context_key,
                system=row.system,
                project=row.project,
                creator_id=row.creator_id,
                extensions=row.extensions,
            )
            for row in serialized.comments
        ],
    )

    unit_key, restored = deserialize_unit(children)

    assert unit_key == "unit5"
    assert restored.source == unit.source
    assert restored.target == unit.target
    assert restored.status == unit.status
    assert unit.tags is not None
    assert restored.tags is not None
    assert restored.tags.source_parts == unit.tags.source_parts
    assert restored.meta.usage_count == 7
    assert restored.meta.extensions == {"change_id": "editor-1"}
    assert restored.comments[0].origin is not None
    assert restored.comments[0].origin.project == "checkout"
    assert restored.previous_context is not None
    assert restored.previous_context.source == "Previous source"
    assert restored.previous_context.extensions == {"kind": "ui"}
    assert restored.next_context is not None
    assert restored.next_context.unit_id == "after"


def test_db_serialization_expands_multitarget_document() -> None:
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        target_locales=("fr", "de"),
        data={
            "hello": Data(
                source="Hello",
                targets={
                    "fr": TargetData(text="Bonjour", status=TranslationStatus.TRANSLATED),
                    "de": TargetData(text="Hallo", status=TranslationStatus.REVIEWED),
                },
            )
        },
    )

    serialized = list(_iter_serialized_document(document, "", ""))

    assert [item.unit.target_locale for item in serialized] == ["fr", "de"]
    assert [item.unit.target_text for item in serialized] == ["Bonjour", "Hallo"]
    assert [item.unit.status for item in serialized] == ["translated", "reviewed"]


def test_db_serialization_preserves_pluralization() -> None:
    source = Data(
        source="I have {count} apple",
        target="J'ai {count} pommes",
        plural=Plural(
            variant="I have {count} apples",
            count=2,
            category=PluralCategory.OTHER,
            extensions={"rule": "n != 1"},
        ),
        status=TranslationStatus.TRANSLATED,
    )

    serialized = serialize_unit("plural", source, "en-US", "fr-FR")
    restored_key, restored = deserialize_unit(
        UnitWithChildren(
            unit=UnitFetchRow(
                id=serialized.unit.id,
                unit_key="plural",
                source_text=serialized.unit.source_text,
                target_text=serialized.unit.target_text,
                source_locale="en-US",
                target_locale="fr-FR",
                status=serialized.unit.status,
                previous_source="",
                next_source="",
                usage_count=0,
                plural_variant=serialized.unit.plural_variant,
                plural_count=serialized.unit.plural_count,
                plural_category=serialized.unit.plural_category,
                extensions=serialized.unit.extensions,
            ),
            tags=[],
            parts=[],
            comments=[],
        )
    )

    assert restored_key == "plural"
    assert restored.plural is not None
    assert restored.plural.variant == "I have {count} apples"
    assert restored.plural.count == 2
    assert restored.plural.category == PluralCategory.OTHER
    assert restored.plural.extensions == {"rule": "n != 1"}


def test_db_matching_downgrades_ice_when_tags_differ() -> None:
    rows = [
        MatchRow(
            id="candidate",
            unit_key="unit",
            source_text="Submit",
            target_text="Envoyer",
            status="translated",
            previous_source="Email",
            next_source="Cancel",
            score=1.0,
            kind="ice",
        )
    ]

    results = rows_to_match_results(
        rows,
        source="Submit",
        previous_source="Email",
        next_source="Cancel",
        require_context=True,
        require_tags=True,
        source_tag_signature=(("b.open", "p0"),),
        candidate_tag_signatures={"candidate": (("i.open", "p0"),)},
    )

    assert results[0].kind == "exact"
    assert results[0].source_equal
    assert results[0].previous_equal
    assert results[0].next_equal
    assert not results[0].tags_equal


def test_db_query_uses_parameter_placeholders() -> None:
    assert "%s" in MATCH_QUERY
    assert "{source" not in MATCH_QUERY
    assert "format(" not in MATCH_QUERY


def test_db_partition_name_is_safe_and_stable() -> None:
    first = partition_name_for_locale("en-US")
    second = partition_name_for_locale("en-US")

    assert first == second
    assert first.startswith("tu_en_us_")
    assert "-" not in first


def test_db_connection_accepts_plain_password() -> None:
    factory = _resolve_password_factory("secret", None)

    assert factory is not None
    assert factory() == "secret"
    assert "password=secret" in _connection_info(
        "postgresql://user@localhost/db",
        factory,
        False,
    )
    with pytest.raises(ValueError, match="either password or password_factory"):
        _resolve_password_factory("secret", lambda: "dynamic")


def test_db_connection_sanitizes_passwords_for_logging() -> None:
    uri = _sanitize_uri("postgresql://user:secret@localhost:5432/db")
    keyword_info = _sanitize_uri("host=localhost port=5432 dbname=db user=user password=secret")

    assert "secret" not in uri
    assert "password=***" in uri
    assert "secret" not in keyword_info
    assert "password=***" in keyword_info
    assert _sanitize_uri("not a valid connection string") == "<invalid connection info>"


def test_db_load_batch_deduplicates_equivalent_units(
    sample_document: BaseStructure,
) -> None:
    duplicate = Data(
        source=sample_document.data["unit1"].source,
        target=sample_document.data["unit1"].target,
        previous_context=AdjacentContext(source="Before"),
        next_context=AdjacentContext(source="After"),
    )
    sample_document.data["unit1"].previous_context = AdjacentContext(source="Before")
    sample_document.data["unit1"].next_context = AdjacentContext(source="After")
    batch = [
        serialize_unit("unit1", sample_document.data["unit1"], "en-US", "fr-FR"),
        serialize_unit("duplicate", duplicate, "en-US", "fr-FR"),
    ]

    assert len(_deduplicate_batch(batch)) == 1


@pytest.mark.asyncio
async def test_db_load_match_reconstruct_and_deduplicate(
    tm: TranslationMemory,
    sample_document: BaseStructure,
) -> None:
    memory = tm
    sample_document.data["unit1"].previous_context = AdjacentContext(source="Before hello")
    sample_document.data["unit1"].next_context = AdjacentContext(source="After hello")

    stats = await memory.load(sample_document, batch_size=2)
    second_stats = await memory.load(sample_document, batch_size=2)
    exact = await memory.match(
        source="Hello world",
        source_locale="en-US",
        target_locale="fr-FR",
        limit=3,
    )
    ice = await memory.match(
        source="Hello world",
        source_locale="en-US",
        target_locale="fr-FR",
        previous_source="Before hello",
        next_source="After hello",
        limit=1,
    )
    fuzzy = await memory.match(
        source="Hello worlds",
        source_locale="en-US",
        target_locale="fr-FR",
        threshold=0.3,
        limit=1,
    )
    restored = await memory.to_document(source_locale="en-US", target_locale="fr-FR")
    streamed_ids = [
        unit_id
        async for unit_id, _ in memory.stream(
            source_locale="en-US",
            target_locale="fr-FR",
            batch_size=2,
        )
    ]

    assert stats.units_read == len(sample_document.data)
    assert stats.units_written == len(sample_document.data)
    assert second_stats.units_written == len(sample_document.data)
    assert exact[0].kind == "exact"
    assert ice[0].kind == "ice"
    assert fuzzy[0].unit_id == "unit1"
    assert restored.data["unit5"].tags is not None
    assert restored.data["unit4[1]"].plural is not None
    assert restored.data["unit4[1]"].plural.category == PluralCategory.OTHER
    assert streamed_ids == sorted(sample_document.data)


@pytest.mark.asyncio
async def test_db_large_tmx_streaming_ingestion_and_matching(
    tm: TranslationMemory,
    tmp_path: Path,
) -> None:
    memory = tm
    tmx_path = tmp_path / "large.tmx"
    _write_large_tmx(tmx_path, 6000)

    stream = lokit.stream.tmx(str(tmx_path))
    stats = await memory.load(stream, batch_size=750)
    tagged = await memory.unit(
        "unit-tagged",
        source_locale="en-US",
        target_locale="fr-FR",
    )
    exact = await memory.match(
        source="Large source 5999",
        source_locale="en-US",
        target_locale="fr-FR",
        limit=1,
    )
    context = await memory.match(
        source="Large source 42",
        source_locale="en-US",
        target_locale="fr-FR",
        previous_source="Large source 41",
        next_source="Large source 43",
        limit=1,
    )

    assert stats.units_read == 6001
    assert tagged.tags is not None
    assert tagged.tags.source_tag_map
    assert tagged.meta.usage_count == 9
    assert tagged.comments[0].context == "Tagged metadata"
    assert exact[0].unit_id == "unit5999"
    assert context[0].kind == "ice"


def _write_large_tmx(path: Path, count: int) -> None:
    with path.open("w", encoding="utf-8") as stream:
        stream.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        stream.write('<tmx version="1.4">\n')
        stream.write(
            '<header creationtool="lokit-test" segtype="sentence" '
            'o-tmf="lokit" adminlang="en-US" srclang="en-US" datatype="text"/>\n'
        )
        stream.write("<body>\n")
        for index in range(count):
            previous_source = f"Large source {index - 1}" if index else ""
            next_source = f"Large source {index + 1}" if index + 1 < count else ""
            stream.write(
                f'<tu tuid="unit{index}">'
                f'<prop type="x-previous-source-text">{escape(previous_source)}</prop>'
                f'<prop type="x-next-source-text">{escape(next_source)}</prop>'
                f'<tuv xml:lang="en-US"><seg>Large source {index}</seg></tuv>'
                f'<tuv xml:lang="fr-FR"><seg>Grande source {index}</seg></tuv>'
                "</tu>\n"
            )
        stream.write(
            '<tu tuid="unit-tagged" creationdate="20260601T120000Z" '
            'changedate="20260602T120000Z" usagecount="9">'
            '<prop type="x-status">translated</prop>'
            "<note>Tagged metadata</note>"
            '<tuv xml:lang="en-US"><seg>'
            'Tagged <bpt i="1">&lt;b&gt;</bpt>source<ept i="1">&lt;/b&gt;</ept>'
            "</seg></tuv>"
            '<tuv xml:lang="fr-FR"><seg>'
            'Source <bpt i="1">&lt;b&gt;</bpt>balisée<ept i="1">&lt;/b&gt;</ept>'
            "</seg></tuv>"
            "</tu>\n"
        )
        stream.write("</body></tmx>\n")
