from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

if TYPE_CHECKING:
    from pathlib import Path

    from lokit.database import TranslationMemory

import pytest

import lokit
from lokit.data.interchange_types import DictField, StringMode
from lokit.data.structure import (
    AdjacentContext,
    BaseStructure,
    CodePart,
    Comment,
    Data,
    Meta,
    Origin,
    Plural,
    PluralCategory,
    StreamingStructure,
    Tags,
    TargetData,
    TargetTags,
    TextPart,
    TranslationStatus,
)
from lokit.data.tag_types import TieData, TieType
from lokit.database import (
    CommentFetchRow,
    MatchRow,
    PartFetchRow,
    SerializedUnit,
    TagFetchRow,
    UnitFetchRow,
    UnitWithChildren,
    connect,
    deserialize_unit,
    iter_serialized_units,
    serialize_unit,
)
from lokit.db.connection import _connection_info, _resolve_password_factory, _sanitize_uri
from lokit.db.matching import rows_to_match_results
from lokit.db.operations import _deduplicate_batch, _iter_load_batches, _serialized_unit_bytes
from lokit.db.queries import MATCH_QUERY
from lokit.db.schema import partition_name_for_locale


class _CloseTrackingItems:
    def __init__(self, items: tuple[tuple[str, Data], ...]) -> None:
        self._items = iter(items)
        self.closed = False

    def __iter__(self) -> _CloseTrackingItems:
        return self

    def __next__(self) -> tuple[str, Data]:
        return next(self._items)

    def close(self) -> None:
        self.closed = True


def _original_text_unit(position: int = 0) -> Data:
    return Data(
        source="",
        target="Cible",
        status=TranslationStatus.TRANSLATED,
        tags=Tags(
            source_tag_map={
                "empty": TieData(
                    id="empty",
                    type=TieType.CUSTOM_STANDALONE,
                    position=position,
                    original_name="x",
                    original_text="",
                ),
                "absent": TieData(
                    id="absent",
                    type=TieType.CUSTOM_STANDALONE,
                    original_name="x",
                ),
                "payload": TieData(
                    id="payload",
                    type=TieType.CUSTOM_STANDALONE,
                    original_name="x",
                    original_text="payload",
                ),
            },
            source_parts=[CodePart("empty"), CodePart("absent"), CodePart("payload")],
        ),
    )


def _serialized_children(serialized: SerializedUnit) -> UnitWithChildren:
    row = serialized.unit
    return UnitWithChildren(
        unit=UnitFetchRow(
            id=row.id,
            unit_key=row.unit_key,
            source_text=row.source_text,
            target_text=row.target_text,
            source_locale=row.source_locale,
            target_locale=row.target_locale,
            status=row.status,
            previous_source=row.previous_source,
            next_source=row.next_source,
            usage_count=row.usage_count,
            plural_variant=row.plural_variant,
            plural_count=row.plural_count,
            plural_category=row.plural_category,
            extensions=row.extensions,
            project=row.project,
            domain=row.domain,
        ),
        tags=[
            TagFetchRow(
                unit_id=row.id,
                source_locale=tag.source_locale,
                tag_id=tag.tag_id,
                tag_type=tag.tag_type,
                position=tag.position,
                tag_order=tag.tag_order,
                attribute_data=tag.attribute_data,
                pair_id=tag.pair_id,
                original_name=tag.original_name,
                original_text=tag.original_text,
                attributes=tag.attributes,
                is_source=tag.is_source,
            )
            for tag in serialized.tags
        ],
        parts=[
            PartFetchRow(
                unit_id=row.id,
                source_locale=part.source_locale,
                is_source=part.is_source,
                position=part.position,
                part_type=part.part_type,
                value=part.value,
            )
            for part in serialized.parts
        ],
        comments=[
            CommentFetchRow(
                unit_id=row.id,
                source_locale=comment.source_locale,
                context=comment.context,
                timestamp=comment.timestamp,
                context_key=comment.context_key,
                system=comment.system,
                project=comment.project,
                creator_id=comment.creator_id,
                extensions=comment.extensions,
            )
            for comment in serialized.comments
        ],
    )


def _assert_original_text_semantics(data: Data) -> None:
    assert data.tags is not None
    assert data.tags.source_tag_map["empty"].original_text == ""
    assert data.tags.source_tag_map["absent"].original_text is None
    assert data.tags.source_tag_map["payload"].original_text == "payload"
    projected = BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        data={"tags": data},
        extensions={"input_format": "xliff"},
    ).to_dict(fields=(DictField.SOURCE,), strings=StringMode.RAW)
    assert projected == [{"source": "<x></x><x/><x>payload</x>"}]


def test_db_serialization_preserves_tag_mapping_keys() -> None:
    data = Data(
        source="beforeafter",
        tags=Tags(
            source_tag_map={
                "part-reference": TieData(
                    id="native-payload-id",
                    type=TieType.CUSTOM_STANDALONE,
                    original_text="<x/>",
                )
            },
            source_parts=[
                TextPart("before"),
                CodePart("part-reference"),
                TextPart("after"),
            ],
        ),
    )

    serialized = serialize_unit("mapped", data, "en", "fr")
    assert [row.tag_id for row in serialized.tags] == ["part-reference"]

    _, restored = deserialize_unit(_serialized_children(serialized))
    assert restored.tags is not None
    assert data.tags is not None
    assert list(restored.tags.source_tag_map) == ["part-reference"]
    assert restored.tags.source_tag_map["part-reference"].id == "part-reference"
    assert restored.tags.source_parts == data.tags.source_parts


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
            usage_count=serialized.unit.usage_count + 3,
            plural_variant=serialized.unit.plural_variant,
            plural_count=serialized.unit.plural_count,
            plural_category=serialized.unit.plural_category,
            extensions=serialized.unit.extensions,
            project=serialized.unit.project,
            domain=serialized.unit.domain,
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
    assert restored.meta.usage_count == 10
    assert restored.meta.extensions == {"change_id": "editor-1"}
    assert restored.comments[0].origin is not None
    assert restored.comments[0].origin.project == "checkout"
    assert restored.previous_context is not None
    assert restored.previous_context.source == "Previous source"
    assert restored.previous_context.extensions == {"kind": "ui"}
    assert restored.next_context is not None
    assert restored.next_context.unit_id == "after"
    assert restored.extensions["project"] == "checkout"
    assert restored.extensions["domain"] == "web"


def test_public_db_serialization_distinguishes_empty_and_absent_original_text() -> None:
    serialized = serialize_unit("original-text", _original_text_unit(), "en-US", "fr-FR")

    assert [tag.original_text for tag in serialized.tags] == ["", "", "payload"]
    unit_key, restored = deserialize_unit(_serialized_children(serialized))

    assert unit_key == "original-text"
    _assert_original_text_semantics(restored)


def test_iter_serialized_units_closes_custom_stream_items() -> None:
    items = _CloseTrackingItems((("original-text", _original_text_unit()),))
    document = StreamingStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        items=items,
    )
    serialized = iter_serialized_units(document)

    next(serialized)
    serialized.close()

    assert items.closed


def test_db_serialization_expands_multitarget_document() -> None:
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        target_locales=("fr", "de"),
        data={
            "hello": Data(
                source="Hello",
                meta=Meta(usage_count=9),
                tags=Tags(
                    source_parts=[TextPart("Hello")],
                    target_parts=[TextPart("Legacy target")],
                ),
                targets={
                    "fr": TargetData(
                        text="Bonjour",
                        status=TranslationStatus.TRANSLATED,
                        tags=TargetTags(parts=[TextPart("Bonjour")]),
                    ),
                    "de": TargetData(text="Hallo", status=TranslationStatus.REVIEWED),
                },
            )
        },
    )

    serialized = list(iter_serialized_units(document))

    assert [item.unit.target_locale for item in serialized] == ["fr", "de"]
    assert [item.unit.target_text for item in serialized] == ["Bonjour", "Hallo"]
    assert [item.unit.status for item in serialized] == ["translated", "reviewed"]
    assert [item.unit.usage_count for item in serialized] == [9, 9]
    assert [row.value for row in serialized[1].parts if row.is_source] == ["Hello"]
    assert [row.value for row in serialized[0].parts if not row.is_source] == ["Bonjour"]
    assert [row.value for row in serialized[1].parts if not row.is_source] == []


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


def test_db_load_batches_obey_row_and_byte_limits() -> None:
    units = [
        serialize_unit(f"unit-{index}", Data(source=f"Source {index}", target=f"Target {index}"), "en", "fr")
        for index in range(3)
    ]
    first_two_bytes = _serialized_unit_bytes(units[0]) + _serialized_unit_bytes(units[1])

    byte_batches = list(
        _iter_load_batches(
            units,
            batch_size=10,
            max_batch_bytes=first_two_bytes - 1,
        )
    )
    row_batches = list(
        _iter_load_batches(
            units,
            batch_size=2,
            max_batch_bytes=first_two_bytes * 2,
        )
    )

    assert [[item.unit.unit_key for item in batch] for batch in byte_batches] == [
        ["unit-0"],
        ["unit-1"],
        ["unit-2"],
    ]
    assert [len(batch) for batch in row_batches] == [2, 1]
    assert all(sum(_serialized_unit_bytes(item) for item in batch) <= first_two_bytes - 1 for batch in byte_batches)


def test_db_load_byte_budget_counts_utf8_and_child_payloads() -> None:
    plain = serialize_unit("plain", Data(source="Cafe", target="Cafe"), "en", "fr")
    rich = serialize_unit(
        "rich",
        Data(
            source="Café",
            target="Café",
            comments=[Comment(context="Révision", extensions={"note": "très important"})],
            tags=Tags(
                source_tag_map={
                    "break": TieData(
                        id="break",
                        type=TieType.BR,
                        original_text="<br/>",
                        attributes={"title": "saut de ligne"},
                    )
                },
                source_parts=[TextPart("Café"), CodePart("break")],
            ),
        ),
        "en",
        "fr",
    )

    assert _serialized_unit_bytes(rich) > _serialized_unit_bytes(plain)


def test_db_load_rejects_a_unit_larger_than_the_byte_limit() -> None:
    unit = serialize_unit("oversized", Data(source="Large source", target="Large target"), "en", "fr")
    required_bytes = _serialized_unit_bytes(unit)

    with pytest.raises(ValueError, match=r"unit 'oversized'.*max_batch_bytes"):
        list(
            _iter_load_batches(
                [unit],
                batch_size=1,
                max_batch_bytes=required_bytes - 1,
            )
        )


@pytest.mark.asyncio
async def test_db_load_applies_the_byte_limit_through_copy(tm: TranslationMemory) -> None:
    data = {
        f"bounded-{index}": Data(source=f"Bounded source {index}", target=f"Bounded target {index}")
        for index in range(3)
    }
    probe_units = [serialize_unit(key, unit, "en-US", "fr-FR") for key, unit in data.items()]
    single_unit_limit = max(_serialized_unit_bytes(item) for item in probe_units)

    stats = await tm.load(
        BaseStructure(source_locale="en-US", target_locale="fr-FR", data=data),
        batch_size=100,
        max_batch_bytes=single_unit_limit,
        progress=False,
    )
    restored = await tm.to_document(source_locale="en-US", target_locale="fr-FR")

    assert stats.units_read == 3
    assert stats.units_written == 3
    assert set(restored.data) == set(data)


@pytest.mark.asyncio
async def test_db_load_roundtrip_preserves_empty_original_text(
    tm: TranslationMemory,
) -> None:
    document = BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        data={"original-text": _original_text_unit()},
    )

    await tm.load(document, progress=False)
    restored = await tm.unit(
        "original-text",
        source_locale="en-US",
        target_locale="fr-FR",
    )

    _assert_original_text_semantics(restored)


@pytest.mark.asyncio
async def test_db_load_closes_stream_items_after_copy_failure(
    tm: TranslationMemory,
) -> None:
    from psycopg.errors import NumericValueOutOfRange

    items = _CloseTrackingItems((("invalid", _original_text_unit(position=2**40)),))
    document = StreamingStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        items=items,
    )

    with pytest.raises(NumericValueOutOfRange):
        await tm.load(document, batch_size=1, progress=False)

    assert items.closed


@pytest.mark.asyncio
async def test_db_load_match_reconstruct_and_deduplicate(
    tm: TranslationMemory,
    sample_document: BaseStructure,
) -> None:
    memory = tm
    sample_document.data["unit1"].previous_context = AdjacentContext(source="Before hello")
    sample_document.data["unit1"].next_context = AdjacentContext(source="After hello")

    stats = await memory.load(sample_document, batch_size=2, project="checkout", domain="web")
    second_stats = await memory.load(sample_document, batch_size=2, project="checkout", domain="web")
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
    assert restored.data["unit1"].extensions["project"] == "checkout"
    assert restored.data["unit1"].extensions["domain"] == "web"
    assert streamed_ids == sorted(sample_document.data)


@pytest.mark.asyncio
async def test_db_multilingual_document_retrieves_all_and_filtered_targets(
    tm: TranslationMemory,
) -> None:
    document = BaseStructure(
        source_locale="en-US",
        target_locale=None,
        target_locales=("fr-FR", "de-DE"),
        data={
            "hello": Data(
                source="Hello",
                tags=Tags(source_parts=[TextPart("Hello")]),
                previous_context=AdjacentContext(source="Before hello"),
                next_context=AdjacentContext(source="After hello"),
                targets={
                    "fr-FR": TargetData(
                        text="Bonjour",
                        status=TranslationStatus.TRANSLATED,
                        tags=TargetTags(parts=[TextPart("Bonjour")]),
                    ),
                    "de-DE": TargetData(
                        text="Hallo",
                        status=TranslationStatus.REVIEWED,
                        tags=TargetTags(parts=[TextPart("Hallo")]),
                    ),
                },
            )
        },
    )

    await tm.load(document, progress=False)
    restored = await tm.to_multilingual_document(source_locale="en-US")
    filtered = await tm.to_multilingual_document(
        source_locale="en-US",
        target_locales=("de-DE",),
    )

    assert set(restored.target_locales) == {"de-DE", "fr-FR"}
    assert restored.data["hello"].targets["fr-FR"].text == "Bonjour"
    assert restored.data["hello"].targets["de-DE"].text == "Hallo"
    assert restored.data["hello"].tags is not None
    assert restored.data["hello"].tags.source_parts == [TextPart("Hello")]
    assert restored.data["hello"].targets["fr-FR"].tags is not None
    assert restored.data["hello"].targets["fr-FR"].tags.parts == [TextPart("Bonjour")]
    assert restored.data["hello"].previous_context is not None
    assert restored.data["hello"].previous_context.source == "Before hello"
    assert restored.data["hello"].next_context is not None
    assert restored.data["hello"].next_context.source == "After hello"
    assert filtered.target_locales == ("de-DE",)
    assert set(filtered.data["hello"].targets) == {"de-DE"}


@pytest.mark.asyncio
async def test_db_multilingual_document_does_not_merge_reused_unit_keys(
    tm: TranslationMemory,
) -> None:
    await tm.load(
        BaseStructure(
            source_locale="en-US",
            target_locale="fr-FR",
            data={
                "same": Data(
                    source="First",
                    target="Premier",
                    previous_context=AdjacentContext(source="Before first"),
                )
            },
        ),
        progress=False,
    )
    await tm.load(
        BaseStructure(
            source_locale="en-US",
            target_locale="de-DE",
            data={
                "same": Data(
                    source="Second",
                    target="Zweite",
                    previous_context=AdjacentContext(source="Before second"),
                )
            },
        ),
        progress=False,
    )

    restored = await tm.to_multilingual_document(source_locale="en-US")

    assert list(restored.data) == ["same", "same#2"]
    first = restored.data["same"]
    second = restored.data["same#2"]
    assert first.source == "First"
    assert first.previous_context is not None
    assert first.previous_context.source == "Before first"
    assert set(first.targets) == {"fr-FR"}
    assert first.targets["fr-FR"].text == "Premier"
    assert second.source == "Second"
    assert second.previous_context is not None
    assert second.previous_context.source == "Before second"
    assert set(second.targets) == {"de-DE"}
    assert second.targets["de-DE"].text == "Zweite"


@pytest.mark.asyncio
async def test_db_stream_completes_with_single_connection_pool(
    tm: TranslationMemory,
    pg_uri: str | None,
    sample_document: BaseStructure,
) -> None:
    assert pg_uri is not None
    await tm.load(sample_document, progress=False)
    memory = await connect(pg_uri, pool_size=1, min_size=1, pipeline=False)

    async def consume() -> list[str]:
        return [
            unit_id
            async for unit_id, _ in memory.stream(
                source_locale="en-US",
                target_locale="fr-FR",
                batch_size=2,
            )
        ]

    async with memory:
        unit_ids = await asyncio.wait_for(consume(), timeout=5.0)

    assert unit_ids == sorted(sample_document.data)


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
