from __future__ import annotations

from lokit.data.structure import Data, Tags
from lokit.db.models import MatchRow
from lokit.logic import MatchResult


TagSignature = tuple[tuple[str, str | None], ...]


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def tags_signature(unit: Data) -> TagSignature:
    if unit.tags is None:
        return ()
    return tags_signature_from_tags(unit.tags)


def tags_signature_from_tags(tags: Tags) -> TagSignature:
    ordered = sorted(tags.source_tag_map.values(), key=lambda item: item.order)
    return tuple((tag.type.value, tag.pair_id) for tag in ordered)


def tag_rows_signature(rows: list[tuple[str, str]]) -> TagSignature:
    return tuple((tag_type, pair_id if pair_id else None) for tag_type, pair_id in rows)


def rows_to_match_results(
    rows: list[MatchRow],
    source: str,
    previous_source: str,
    next_source: str,
    require_context: bool,
    require_tags: bool,
    source_tag_signature: TagSignature,
    candidate_tag_signatures: dict[str, TagSignature],
) -> list[MatchResult]:
    normalized_source = normalize_text(source)
    normalized_previous = normalize_text(previous_source)
    normalized_next = normalize_text(next_source)
    results: list[MatchResult] = []

    for row in rows:
        source_equal = normalize_text(row.source_text) == normalized_source
        previous_equal = (not require_context) or (
            normalize_text(row.previous_source) == normalized_previous
        )
        next_equal = (not require_context) or (
            normalize_text(row.next_source) == normalized_next
        )
        tags_equal = (not require_tags) or (
            candidate_tag_signatures.get(row.id, ()) == source_tag_signature
        )
        is_ice = (
            row.kind == "ice"
            and source_equal
            and previous_equal
            and next_equal
            and tags_equal
        )
        kind = "ice" if is_ice else ("exact" if source_equal else "fuzzy")
        results.append(
            MatchResult(
                unit_id=row.unit_key,
                score=1.0 if is_ice else row.score,
                kind=kind,
                source_equal=source_equal,
                tags_equal=tags_equal,
                previous_equal=previous_equal,
                next_equal=next_equal,
            )
        )

    results.sort(key=lambda item: item.score, reverse=True)
    return results
