from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from lokit.data.structure import Data
from lokit.types import TagSyntax, UnsupportedTagPolicy, render_segment, segment_from_legacy

if TYPE_CHECKING:
    from collections.abc import Iterator


ExtractItem = tuple[str, Data]


def project_items(
    items: Iterator[ExtractItem],
    *,
    include_tags: bool,
    tag_syntax: TagSyntax,
    native_syntax: TagSyntax,
    unsupported_tags: UnsupportedTagPolicy,
) -> Iterator[ExtractItem]:
    if not include_tags:
        yield from items
        return
    for unit_id, data in items:
        yield unit_id, project_data(
            data,
            tag_syntax=tag_syntax,
            native_syntax=native_syntax,
            unsupported_tags=unsupported_tags,
        )


def project_data(
    data: Data,
    *,
    tag_syntax: TagSyntax,
    native_syntax: TagSyntax,
    unsupported_tags: UnsupportedTagPolicy,
) -> Data:
    projected = deepcopy(data)
    tags = projected.tags
    if tags is not None:
        source_segment = segment_from_legacy(
            projected.source,
            tags.source_parts,
            tags.source_tag_map,
            syntax=native_syntax,
        )
        projected.source = render_segment(
            source_segment,
            tag_syntax,
            native_syntax=native_syntax,
            unsupported_tags=unsupported_tags,
        )
        if projected.target is not None:
            target_segment = segment_from_legacy(
                projected.target,
                tags.target_parts,
                tags.target_tag_map,
                syntax=native_syntax,
            )
            projected.target = render_segment(
                target_segment,
                tag_syntax,
                native_syntax=native_syntax,
                unsupported_tags=unsupported_tags,
            )
    for target in projected.targets.values():
        if target.text is None or target.tags is None:
            continue
        target_segment = segment_from_legacy(
            target.text,
            target.tags.parts,
            target.tags.tag_map,
            syntax=native_syntax,
        )
        target.text = render_segment(
            target_segment,
            tag_syntax,
            native_syntax=native_syntax,
            unsupported_tags=unsupported_tags,
        )
    return projected
