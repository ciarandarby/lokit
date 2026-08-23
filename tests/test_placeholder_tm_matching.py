from __future__ import annotations

import pytest

from lokit.data.structure import BaseStructure, Data
from lokit.db.matching import canonical_match_text, rows_to_match_results
from lokit.db.models import MatchRow
from lokit.logic import Lokit


def _memory(source: str, target: str | None) -> Lokit:
    return Lokit(
        BaseStructure(
            source_locale="en",
            target_locale="fr",
            data={"candidate": Data(source=source, target=target)},
        )
    )


def test_local_exact_match_reforms_renamed_placeholder() -> None:
    result = _memory("Hello {name}", "Bonjour {name}").match(
        "Hello {customer}",
        "candidate",
    )

    assert result.kind == "exact"
    assert result.source_equal
    assert result.translation == "Bonjour {customer}"
    assert result.placeholders_reformed
    assert result.can_apply


def test_local_exact_match_normalizes_literal_case_and_spacing_after_scanning() -> None:
    result = _memory("HELLO   {Name}", "Bonjour {Name}").match(
        "hello {customer}",
        "candidate",
    )

    assert result.kind == "exact"
    assert result.translation == "Bonjour {customer}"
    assert result.can_apply


def test_local_fuzzy_match_reforms_placeholder() -> None:
    result = _memory(
        "Welcome {name} to our store",
        "Bienvenue {name} dans notre magasin",
    ).match("Welcome {customer} to this store", "candidate")

    assert result.kind == "fuzzy"
    assert 0.0 < result.score < 1.0
    assert result.translation == "Bienvenue {customer} dans notre magasin"
    assert result.placeholders_reformed
    assert result.can_apply


def test_local_match_reforms_reordered_and_repeated_target_placeholders() -> None:
    result = _memory(
        "From {origin} to {destination}",
        "De {destination} à {origin}; {origin}",
    ).match("From {start} to {end}", "candidate")

    assert result.kind == "exact"
    assert result.translation == "De {end} à {start}; {start}"
    assert result.placeholders_reformed
    assert result.can_apply


def test_local_match_rejects_incompatible_repeat_graph() -> None:
    result = _memory("{first} {second}", "{first} {second}").match(
        "{value} {value}",
        "candidate",
    )

    assert result.kind == "fuzzy"
    assert not result.source_equal
    assert result.translation is None
    assert not result.placeholders_reformed
    assert not result.can_apply


def test_local_match_rejects_target_only_placeholder() -> None:
    result = _memory("Hello {name}", "Bonjour {unexpected}").match(
        "Hello {customer}",
        "candidate",
    )

    assert result.kind == "exact"
    assert result.source_equal
    assert result.translation is None
    assert not result.can_apply


def test_local_fuzzy_find_returns_safe_reformed_translation() -> None:
    result = _memory(
        "Download {count} files now",
        "Téléchargez {count} fichiers maintenant",
    ).fuzzy_find("Download {total} files today", limit=1, threshold=0.2)[0]

    assert result.kind == "fuzzy"
    assert result.translation == "Téléchargez {total} fichiers maintenant"
    assert result.can_apply


def test_local_fuzzy_find_does_not_reform_every_duplicate_exact_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def reformed_translation(
        candidate_source: str,
        candidate_target: str | None,
        query_source: str,
    ) -> tuple[str | None, bool]:
        nonlocal calls
        calls += 1
        return candidate_target, False

    monkeypatch.setattr("lokit.logic._reformed_translation", reformed_translation)
    memory = Lokit(
        BaseStructure(
            source_locale="en",
            target_locale="fr",
            data={f"candidate-{index}": Data(source="Hello {name}", target="Bonjour {name}") for index in range(200)},
        )
    )

    result = memory.fuzzy_find("Hello {customer}", limit=1)[0]

    assert result.kind == "exact"
    assert calls == 1


def test_local_fuzzy_find_validates_bounds() -> None:
    memory = _memory("Hello", "Bonjour")

    with pytest.raises(ValueError, match="limit"):
        memory.fuzzy_find("Hello", limit=0)
    with pytest.raises(ValueError, match="threshold"):
        memory.fuzzy_find("Hello", threshold=1.1)


def test_db_result_conversion_reforms_placeholder_names() -> None:
    candidate = canonical_match_text("From {origin} to {destination}")
    rows = [
        MatchRow(
            id="database-id",
            unit_key="candidate",
            source_text="From {origin} to {destination}",
            target_text="De {destination} à {origin}; {origin}",
            status="translated",
            previous_source="",
            next_source="",
            score=1.0,
            kind="exact",
            source_match_text=candidate.text,
            placeholder_signature=candidate.signature,
        )
    ]

    result = rows_to_match_results(
        rows,
        source="From {start} to {end}",
        previous_source="",
        next_source="",
        require_context=False,
        require_tags=False,
        source_tag_signature=(),
        candidate_tag_signatures={},
    )[0]

    assert result.kind == "exact"
    assert result.translation == "De {end} à {start}; {start}"
    assert result.placeholders_reformed
    assert result.can_apply


def test_db_result_conversion_blocks_incompatible_placeholders() -> None:
    candidate = canonical_match_text("{first} {second}")
    rows = [
        MatchRow(
            id="database-id",
            unit_key="candidate",
            source_text="{first} {second}",
            target_text="{first} {second}",
            status="translated",
            previous_source="",
            next_source="",
            score=0.8,
            kind="fuzzy",
            source_match_text=candidate.text,
            placeholder_signature=candidate.signature,
        )
    ]

    result = rows_to_match_results(
        rows,
        source="{value} {value}",
        previous_source="",
        next_source="",
        require_context=False,
        require_tags=False,
        source_tag_signature=(),
        candidate_tag_signatures={},
    )[0]

    assert result.kind == "fuzzy"
    assert result.translation is None
    assert not result.can_apply


def test_db_result_conversion_prioritizes_placeholder_safe_candidate() -> None:
    unsafe_canonical = canonical_match_text("Hello {name}")
    safe_canonical = canonical_match_text("Hello {name}")
    rows = [
        MatchRow(
            id="unsafe-id",
            unit_key="unsafe",
            source_text="Hello {name}",
            target_text="Bonjour {unexpected}",
            status="translated",
            previous_source="",
            next_source="",
            score=1.0,
            kind="exact",
            source_match_text=unsafe_canonical.text,
            placeholder_signature=unsafe_canonical.signature,
        ),
        MatchRow(
            id="safe-id",
            unit_key="safe",
            source_text="Hello {name}",
            target_text="Bonjour {name}",
            status="translated",
            previous_source="",
            next_source="",
            score=1.0,
            kind="exact",
            source_match_text=safe_canonical.text,
            placeholder_signature=safe_canonical.signature,
        ),
    ]

    results = rows_to_match_results(
        rows,
        source="Hello {customer}",
        previous_source="",
        next_source="",
        require_context=False,
        require_tags=False,
        source_tag_signature=(),
        candidate_tag_signatures={},
    )

    assert results[0].unit_id == "safe"
    assert results[0].translation == "Bonjour {customer}"
    assert results[0].can_apply
    assert not results[1].can_apply
