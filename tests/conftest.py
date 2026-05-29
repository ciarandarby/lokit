from __future__ import annotations

import pytest

from lokit.data.structure import (
    BaseStructure,
    CodePart,
    Comment,
    Data,
    Plural,
    PluralCategory,
    Tags,
    TextPart,
    TranslationStatus,
)
from lokit.data.tag_types import TieData, TieType


@pytest.fixture
def sample_document() -> BaseStructure:
    data = {
        "unit1": Data(
            source="Hello world",
            target="Bonjour le monde",
            status=TranslationStatus.TRANSLATED,
            comments=[Comment(context="Standard greeting")],
        ),
        "unit2": Data(
            source="Singular source",
            target="Singular target",
            status=TranslationStatus.APPROVED,
        ),
        "unit3": Data(
            source="Untranslated source",
            target=None,
            status=TranslationStatus.NEW,
        ),
        "unit4": Data(
            source="I have {count} apple",
            target="J'ai {count} pomme",
            plural=Plural(variant="I have {count} apples"),
            status=TranslationStatus.TRANSLATED,
        ),
        "unit4[1]": Data(
            source="I have {count} apple",
            target="J'ai {count} pommes",
            plural=Plural(
                variant="I have {count} apples",
                category=PluralCategory.OTHER,
            ),
            status=TranslationStatus.TRANSLATED,
        ),
    }
    tag_map = {
        "t0": TieData(
            id="t0",
            type=TieType.B_OPEN,
            position=0,
            order=0,
            pair_id="pair0",
            original_name="b",
        ),
        "t1": TieData(
            id="t1",
            type=TieType.B_CLOSE,
            position=2,
            order=2,
            pair_id="pair0",
            original_name="b",
        ),
    }
    tags = Tags(
        source_tag_map=tag_map,
        target_tag_map=tag_map,
        source_parts=[CodePart("t0"), TextPart("Formatted"), CodePart("t1")],
        target_parts=[CodePart("t0"), TextPart("Formaté"), CodePart("t1")],
    )
    data["unit5"] = Data(
        source="Formatted",
        target="Formaté",
        tags=tags,
        status=TranslationStatus.TRANSLATED,
    )

    return BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        data=data,
        source_language="en",
        target_language="fr",
        export_origin="lokit-test",
    )
