from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from lokit.data.structure import BaseStructure, Data, StreamingStructure
from lokit.placeholders import literalize_data, resolve_data

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


Structure = BaseStructure | StreamingStructure


class _Closable(Protocol):
    def close(self) -> None: ...


def prepare_export_document(
    document: Structure,
    *,
    resolve_placeholders: bool,
) -> StreamingStructure:
    """Create a bounded, non-mutating export view of ``document``.

    Exact runtime spellings and native inline-code positions are restored by
    default. When resolution is disabled, projected markers become ordinary
    text so format writers cannot accidentally reinterpret their backing tags.
    """
    source_items: Iterable[tuple[str, Data]] = (
        document.data.items() if isinstance(document, BaseStructure) else document.items
    )
    return StreamingStructure(
        source_locale=document.source_locale,
        target_locale=document.target_locale,
        items=_prepare_items(source_items, resolve_placeholders=resolve_placeholders),
        target_locales=document.target_locales,
        format_version=document.format_version,
        export_origin=document.export_origin,
        export_timestamp=document.export_timestamp,
        source_language=document.source_language,
        target_language=document.target_language,
        target_languages=document.target_languages,
        extensions=document.extensions.copy(),
    )


def _prepare_items(
    source_items: Iterable[tuple[str, Data]],
    *,
    resolve_placeholders: bool,
) -> Iterator[tuple[str, Data]]:
    items = iter(source_items)
    try:
        for unit_id, data in items:
            yield (
                unit_id,
                prepare_export_data(
                    data,
                    resolve_placeholders=resolve_placeholders,
                ),
            )
    finally:
        candidate: object = items
        if hasattr(candidate, "close"):
            cast("_Closable", candidate).close()


def prepare_export_data(
    data: Data,
    *,
    resolve_placeholders: bool,
) -> Data:
    """Prepare one unit for exact or literal-marker export."""
    if data.tags is None and not any(target.tags is not None for target in data.targets.values()):
        return data
    transform = resolve_data if resolve_placeholders else literalize_data
    return transform(data)


__all__ = ["prepare_export_data", "prepare_export_document"]
