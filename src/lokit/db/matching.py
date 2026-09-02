from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.placeholders import CanonicalPlaceholderText, canonicalize, reform
from lokit.types.match import MatchResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lokit.data.structure import Data, Tags
    from lokit.db.models import MatchRow

TagSignature = tuple[tuple[str, str | None], ...]
PLACEHOLDER_INDEX_VERSION = 1


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def canonical_match_text(value: str) -> CanonicalPlaceholderText:
    """Return the normalized, placeholder-name-independent TM index value."""
    canonical = canonicalize(value)
    return CanonicalPlaceholderText(
        _normalize_canonical_text(canonical.text),
        canonical.signature,
    )


def _normalize_canonical_text(value: str) -> str:
    separator = "\x1f"
    marker_index = 0
    marker = "\ufdd0LOKIT0\ufdef"
    folded_value = value.casefold()
    while marker.casefold() in folded_value:
        marker_index += 1
        marker = f"\ufdd0LOKIT{marker_index}\ufdef"
    protected = value.replace(separator, marker)
    return normalize_text(protected).replace(marker.casefold(), separator)


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
    rows: Sequence[MatchRow],
    source: str,
    previous_source: str,
    next_source: str,
    require_context: bool,
    require_tags: bool,
    source_tag_signature: TagSignature,
    candidate_tag_signatures: dict[str, TagSignature],
) -> list[MatchResult]:
    canonical_source = canonical_match_text(source)
    normalized_previous = normalize_text(previous_source)
    normalized_next = normalize_text(next_source)
    results: list[MatchResult] = []

    for row in rows:
        candidate_canonical = (
            CanonicalPlaceholderText(row.source_match_text, row.placeholder_signature)
            if row.source_match_text and row.placeholder_signature
            else canonical_match_text(row.source_text)
        )
        source_equal = candidate_canonical == canonical_source
        previous_equal = (not require_context) or (normalize_text(row.previous_source) == normalized_previous)
        next_equal = (not require_context) or (normalize_text(row.next_source) == normalized_next)
        tags_equal = (not require_tags) or (candidate_tag_signatures.get(row.id, ()) == source_tag_signature)
        is_ice = (require_context or require_tags) and source_equal and previous_equal and next_equal and tags_equal
        kind = "ice" if is_ice else ("exact" if source_equal else "fuzzy")
        translation, placeholders_reformed = _reformed_translation(
            row.source_text,
            row.target_text,
            source,
        )
        results.append(
            MatchResult(
                unit_id=row.unit_key,
                score=1.0 if is_ice else row.score,
                kind=kind,
                source_equal=source_equal,
                tags_equal=tags_equal,
                previous_equal=previous_equal,
                next_equal=next_equal,
                translation=translation,
                placeholders_reformed=placeholders_reformed,
                can_apply=translation is not None and tags_equal,
            )
        )

    results.sort(key=lambda item: (item.can_apply, item.score), reverse=True)
    return results


def _reformed_translation(
    candidate_source: str,
    candidate_target: str,
    query_source: str,
) -> tuple[str | None, bool]:
    try:
        result = reform(candidate_source, candidate_target, query_source)
    except ValueError:
        return None, False
    return result.text, result.changed
