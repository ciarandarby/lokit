from __future__ import annotations

import pytest
from psycopg_pool import AsyncConnectionPool

import lokit
import lokit.database as database
import lokit.db as legacy_database
import lokit.office as office
from lokit.data.targets import StreamingTargetSplit
from lokit.db.connection import Connection, WriterReaderPool
from lokit.io.stream_json import LokitJsonContext
from lokit.logic import MatchResult as LegacyMatchResult
from lokit.parsers.tmx.models import TmxParseMode
from lokit.parsers.tmx.parallel import TmxParallelOptions


def test_configuration_and_lifecycle_types_have_canonical_exports() -> None:
    assert lokit.parse.TmxParseMode is TmxParseMode
    assert lokit.parse.TmxParallelOptions is TmxParallelOptions
    assert lokit.stream.LokitJsonContext is LokitJsonContext
    assert lokit.types.StreamingTargetSplit is StreamingTargetSplit
    assert {"TmxParallelOptions", "TmxParseMode", "files"} <= set(lokit.parse.__all__)
    assert "LokitJsonContext" in lokit.stream.__all__
    assert "StreamingTargetSplit" in lokit.types.__all__


def test_returned_tag_and_match_types_are_public() -> None:
    expected = {
        "CodePart",
        "MatchResult",
        "Tags",
        "TargetTags",
        "TextPart",
        "TieData",
        "TieType",
    }

    assert expected <= set(lokit.types.__all__)
    code = lokit.types.TieData(id="break", type=lokit.types.TieType.BR)
    tags = lokit.types.Tags(
        source_tag_map={code.id: code},
        source_parts=[lokit.types.TextPart("Hello"), lokit.types.CodePart(code.id)],
    )
    match = lokit.types.MatchResult("unit-1", 1.0, "exact", True, True, True, True)
    part = tags.source_parts[1]

    assert isinstance(part, lokit.types.CodePart)
    assert part.ref == "break"
    assert match.unit_id == "unit-1"
    assert lokit.types.MatchResult is LegacyMatchResult


def test_advanced_office_types_are_public_without_expanding_the_root() -> None:
    assert {"DocumentSink", "DocumentSource", "OfficeError"} <= set(office.__all__)
    assert issubclass(office.OfficeRuntimeUnavailable, office.OfficeError)
    assert issubclass(office.OfficePackageError, office.OfficeError)
    assert "office" not in lokit.__all__
    assert "office" not in dir(lokit)


@pytest.mark.asyncio
async def test_database_stream_has_a_public_closeable_context_manager() -> None:
    pool: AsyncConnectionPool[Connection] = object.__new__(AsyncConnectionPool)
    memory = database.TranslationMemory(WriterReaderPool(writer=pool, reader=pool), pipeline=False)
    stream = memory.stream(
        source_locale="en",
        target_locale="fr",
        include_tags=True,
        batch_size=1,
    )

    assert database.TranslationMemoryStream is legacy_database.TranslationMemoryStream
    assert isinstance(stream, database.TranslationMemoryStream)
    assert stream.__aiter__() is stream
    async with stream as active:
        assert active is stream

    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
