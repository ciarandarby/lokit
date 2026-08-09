from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Protocol, cast

from lokit.data.structure import (
    AdjacentContext,
    BaseStructure,
    CodePart,
    Comment,
    Data,
    Meta,
    Origin,
    Plural,
    StreamingStructure,
    Tags,
    TargetData,
    TargetTags,
    TextPart,
    TranslationStatus,
)
from lokit.data.tag_types import TieData
from lokit.io.atomic import raise_if_cancelled

if TYPE_CHECKING:
    import threading
    from types import TracebackType

    from lokit.data.structure import SegmentPart


_MAX_SPLIT_TARGET_LOCALES = 256
_MAX_DIRECT_SPLIT_TARGET_LOCALES = 32
ExtractItem = tuple[str, Data]


class _ClosableIterator(Protocol):
    def close(self) -> None: ...


class _SplitLifetime:
    def __init__(self) -> None:
        self.active = False


def has_targets(data: Data) -> bool:
    return bool(data.targets) or data.target is not None


def get_target(data: Data, locale: str) -> TargetData | None:
    return data.targets.get(locale)


def selected_target(data: Data, locale: str | None) -> TargetData | None:
    if locale is not None:
        found = data.targets.get(locale)
        if found is not None:
            return found
        return None
    if len(data.targets) == 1:
        return next(iter(data.targets.values()))
    if data.target is not None:
        return TargetData(text=data.target, status=data.status)
    return None


def target_text(data: Data, locale: str | None) -> str | None:
    target = selected_target(data, locale)
    if target is not None:
        return target.text
    return data.target


def target_status(data: Data, locale: str | None) -> TranslationStatus:
    target = selected_target(data, locale)
    if target is not None:
        return target.status
    return data.status


def split_targets(
    document: BaseStructure,
    target_locales: tuple[str, ...] = (),
    *,
    include_missing: bool = True,
) -> dict[str, BaseStructure]:
    _validate_requested_locales(target_locales)
    locales = _unique_locales(target_locales or _document_target_locales(document))
    _validate_locale_count(locales)
    return {locale: _select_target(document, locale, include_missing=include_missing) for locale in locales}


def select_target(document: BaseStructure, locale: str) -> BaseStructure:
    return _select_target(document, locale, include_missing=True)


def _select_target(
    document: BaseStructure,
    locale: str,
    *,
    include_missing: bool,
) -> BaseStructure:
    data: dict[str, Data] = {}
    legacy_locale = _legacy_target_locale(document.target_locale, document.target_locales)
    for unit_id, unit in document.data.items():
        selected = unit.targets.get(locale)
        has_legacy = unit.target is not None and legacy_locale == locale
        if selected is None and not has_legacy and not include_missing:
            continue
        data[unit_id] = _clone_for_target(unit, selected, keep_legacy=has_legacy)

    target_language = _base_language(locale)
    return BaseStructure(
        source_locale=document.source_locale,
        target_locale=locale,
        data=data,
        target_locales=(locale,),
        format_version=document.format_version,
        export_origin=document.export_origin,
        export_timestamp=document.export_timestamp,
        source_language=document.source_language,
        target_language=target_language,
        target_languages=(target_language,),
        extensions=document.extensions.copy(),
    )


class StreamingTargetSplit:
    """One-pass, bounded-memory target split backed by temporary Lokit files."""

    def __init__(
        self,
        document: StreamingStructure,
        target_locales: tuple[str, ...] = (),
        *,
        include_missing: bool = True,
        _cancellation: threading.Event | None = None,
    ) -> None:
        self._document = document
        _validate_requested_locales(target_locales)
        self._requested_locales = _unique_locales(target_locales)
        self._include_missing = include_missing
        self._cancellation = _cancellation
        self._temporary_directory: TemporaryDirectory[str] | None = None
        self._documents: dict[str, StreamingStructure] = {}
        self._source_document: StreamingStructure | None = None
        self._active_iterators: set[_ClosableIterator] = set()
        self._lifetime = _SplitLifetime()
        self._consumed = False
        _validate_locale_count(self._requested_locales)

    def __enter__(self) -> dict[str, StreamingStructure]:
        if self._temporary_directory is not None:
            raise RuntimeError("streaming target split is already open")
        if self._consumed:
            raise RuntimeError("streaming target split cannot be reused")
        self._consumed = True
        temporary_directory = TemporaryDirectory(prefix="lokit-target-split-")
        self._temporary_directory = temporary_directory
        self._lifetime.active = True
        try:
            self._documents = self._spool(Path(temporary_directory.name))
        except BaseException:
            self.close()
            raise
        return self._documents

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._lifetime.active = False
        for iterator in tuple(self._active_iterators):
            iterator.close()
        self._active_iterators.clear()
        self._documents = {}
        self._source_document = None
        temporary_directory = self._temporary_directory
        self._temporary_directory = None
        if temporary_directory is not None:
            temporary_directory.cleanup()

    def _spool(self, directory: Path) -> dict[str, StreamingStructure]:
        from lokit.exporters.lokit import export_lokit

        if self._requested_locales and len(self._requested_locales) <= _MAX_DIRECT_SPLIT_TARGET_LOCALES:
            return self._spool_requested(directory)
        discovered = list(self._requested_locales)
        _extend_unique(discovered, self._document.target_locales)
        if self._document.target_locale is not None:
            _extend_unique(discovered, (self._document.target_locale,))
        master_path = directory / "source.lokit"
        master = _streaming_metadata(
            self._document,
            self._document.target_locale,
            _discovering_items(self._document.items, discovered, self._cancellation),
            self._document.target_locales,
        )
        raise_if_cancelled(self._cancellation)
        export_lokit(master, master_path)
        raise_if_cancelled(self._cancellation)
        self._source_document = _streaming_metadata(
            self._document,
            self._document.target_locale,
            _LazyLokitItems(master_path, self._active_iterators, self._lifetime),
            self._document.target_locales,
        )
        locales = self._requested_locales or tuple(discovered)
        _validate_locale_count(locales)
        legacy_locale = _legacy_target_locale(
            self._document.target_locale,
            self._document.target_locales,
        )
        documents: dict[str, StreamingStructure] = {}
        for index, locale in enumerate(locales):
            output_path = directory / f"target-{index}.lokit"
            projected = _streaming_metadata(
                self._document,
                locale,
                _NativeTargetItems(
                    master_path,
                    locale,
                    legacy_locale,
                    self._include_missing,
                    self._cancellation,
                ),
                (locale,),
            )
            raise_if_cancelled(self._cancellation)
            export_lokit(projected, output_path)
            raise_if_cancelled(self._cancellation)
            documents[locale] = _streaming_metadata(
                self._document,
                locale,
                _LazyLokitItems(output_path, self._active_iterators, self._lifetime),
                (locale,),
            )
        return documents

    def _source_stream(self) -> StreamingStructure:
        source_document = self._source_document
        if source_document is None:
            raise RuntimeError("streaming target split has no spooled source")
        return source_document

    def _spool_requested(self, directory: Path) -> dict[str, StreamingStructure]:
        from lokit._interchange_rust import LokitWriter

        writers: list[tuple[str, Path, LokitWriter]] = []
        legacy_locale = _legacy_target_locale(
            self._document.target_locale,
            self._document.target_locales,
        )
        items = iter(self._document.items)
        try:
            for index, locale in enumerate(self._requested_locales):
                output_path = directory / f"target-{index}.lokit"
                metadata = _streaming_metadata(self._document, locale, (), (locale,))
                writer = LokitWriter(
                    str(output_path),
                    metadata.source_locale,
                    metadata.target_locale,
                    metadata.target_locales,
                    metadata.format_version,
                    metadata.export_origin,
                    metadata.export_timestamp,
                    metadata.source_language,
                    metadata.target_language,
                    metadata.target_languages,
                    metadata.extensions,
                )
                writers.append((locale, output_path, writer))
            for unit_id, unit in items:
                raise_if_cancelled(self._cancellation)
                for locale, _, writer in writers:
                    selected = unit.targets.get(locale)
                    has_legacy = unit.target is not None and legacy_locale == locale
                    if selected is None and not has_legacy and not self._include_missing:
                        continue
                    writer.write(
                        unit_id,
                        _clone_for_target(unit, selected, keep_legacy=has_legacy),
                    )
            for _, _, writer in writers:
                raise_if_cancelled(self._cancellation)
                writer.close()
            raise_if_cancelled(self._cancellation)
        except BaseException:
            for _, _, writer in writers:
                with suppress(BaseException):
                    writer.abort()
            raise
        finally:
            _close_iterator(items)
        return {
            locale: _streaming_metadata(
                self._document,
                locale,
                _LazyLokitItems(output_path, self._active_iterators, self._lifetime),
                (locale,),
            )
            for locale, output_path, _ in writers
        }


class _LazyLokitItems(Iterable[ExtractItem]):
    def __init__(
        self,
        path: Path,
        active: set[_ClosableIterator],
        lifetime: _SplitLifetime,
    ) -> None:
        self._path = path
        self._active = active
        self._lifetime = lifetime

    def __iter__(self) -> Iterator[ExtractItem]:
        return self._iter_items()

    def _iter_items(self) -> Iterator[ExtractItem]:
        from lokit.parsers.lokit.extraction import LokitExtractor

        if not self._lifetime.active:
            raise FileNotFoundError(self._path)
        iterator = LokitExtractor(str(self._path), eager=False).extract()
        candidate: object = iterator
        if not hasattr(candidate, "close"):
            raise RuntimeError("Lokit extraction iterator is not closable")
        closable = cast("_ClosableIterator", candidate)
        self._active.add(closable)
        try:
            while self._lifetime.active:
                try:
                    item = next(iterator)
                except StopIteration:
                    return
                yield item
        finally:
            closable.close()
            self._active.discard(closable)


class _NativeTargetItems(Iterable[ExtractItem]):
    def __init__(
        self,
        path: Path,
        locale: str,
        legacy_locale: str | None,
        include_missing: bool,
        cancellation: threading.Event | None,
    ) -> None:
        self._path = path
        self._locale = locale
        self._legacy_locale = legacy_locale
        self._include_missing = include_missing
        self._cancellation = cancellation

    def __iter__(self) -> Iterator[ExtractItem]:
        return self._iter_items()

    def _iter_items(self) -> Iterator[ExtractItem]:
        from lokit._interchange_rust import LokitReader

        reader = LokitReader(str(self._path))
        try:
            while True:
                raise_if_cancelled(self._cancellation)
                batch = reader.read_target_batch(
                    self._locale,
                    self._legacy_locale,
                    self._include_missing,
                    256,
                )
                if not batch:
                    return
                yield from batch
        finally:
            reader.close()


def _discovering_items(
    items: Iterable[ExtractItem],
    locales: list[str],
    cancellation: threading.Event | None,
) -> Iterator[ExtractItem]:
    iterator = iter(items)
    try:
        for item in iterator:
            raise_if_cancelled(cancellation)
            _extend_unique(locales, item[1].targets)
            _validate_locale_count(tuple(locales))
            yield item
    finally:
        _close_iterator(iterator)


def _close_iterator(items: Iterator[ExtractItem]) -> None:
    candidate: object = items
    if hasattr(candidate, "close"):
        cast("_ClosableIterator", candidate).close()


def _streaming_metadata(
    document: StreamingStructure,
    target_locale: str | None,
    items: Iterable[ExtractItem],
    target_locales: tuple[str, ...],
) -> StreamingStructure:
    target_language = _base_language(target_locale) if target_locale is not None else None
    target_languages = (target_language,) if target_language is not None else ()
    return StreamingStructure(
        source_locale=document.source_locale,
        target_locale=target_locale,
        items=items,
        target_locales=target_locales,
        format_version=document.format_version,
        export_origin=document.export_origin,
        export_timestamp=document.export_timestamp,
        source_language=document.source_language,
        target_language=target_language,
        target_languages=target_languages,
        extensions=document.extensions.copy(),
    )


def _clone_for_target(
    unit: Data,
    selected: TargetData | None,
    *,
    keep_legacy: bool,
) -> Data:
    cloned = Data(
        source=unit.source,
        target=unit.target if keep_legacy else None,
        targets={},
        plural=_clone_plural(unit.plural),
        tags=_clone_tags(unit.tags),
        meta=_clone_meta(unit.meta),
        status=unit.status,
        comments=_clone_comments(unit.comments),
        previous_context=_clone_context(unit.previous_context),
        next_context=_clone_context(unit.next_context),
        extensions=unit.extensions.copy(),
    )
    if selected is None:
        if not keep_legacy:
            _clear_target_tags(cloned)
        return cloned
    cloned.target = selected.text
    if selected.status is not TranslationStatus.UNKNOWN:
        cloned.status = selected.status
    if selected.plural is not None:
        cloned.plural = _clone_plural(selected.plural)
    cloned.meta = _merge_meta(cloned.meta, selected.meta)
    if selected.comments:
        cloned.comments = _clone_comments(selected.comments)
    cloned.extensions.update(selected.extensions)
    _set_target_tags(cloned, selected.tags)
    return cloned


def _clone_plural(value: Plural | None) -> Plural | None:
    if value is None:
        return None
    return Plural(
        variant=value.variant,
        count=value.count,
        category=value.category,
        extensions=value.extensions.copy(),
    )


def _clone_meta(value: Meta) -> Meta:
    return Meta(
        usage_count=value.usage_count,
        last_used=value.last_used,
        first_used=value.first_used,
        created=value.created,
        updated=value.updated,
        max_length=value.max_length,
        min_length=value.min_length,
        extensions=value.extensions.copy(),
    )


def _merge_meta(base: Meta, target: Meta) -> Meta:
    extensions = base.extensions.copy()
    extensions.update(target.extensions)
    return Meta(
        usage_count=target.usage_count if target.usage_count is not None else base.usage_count,
        last_used=target.last_used if target.last_used is not None else base.last_used,
        first_used=target.first_used if target.first_used is not None else base.first_used,
        created=target.created if target.created is not None else base.created,
        updated=target.updated if target.updated is not None else base.updated,
        max_length=target.max_length if target.max_length is not None else base.max_length,
        min_length=target.min_length if target.min_length is not None else base.min_length,
        extensions=extensions,
    )


def _clone_origin(value: Origin | None) -> Origin | None:
    if value is None:
        return None
    return Origin(
        system=value.system,
        project=value.project,
        creator_id=value.creator_id,
        extensions=value.extensions.copy(),
    )


def _clone_comments(values: list[Comment]) -> list[Comment]:
    return [
        Comment(
            context=value.context,
            timestamp=value.timestamp,
            origin=_clone_origin(value.origin),
            context_key=value.context_key,
            extensions=value.extensions.copy(),
        )
        for value in values
    ]


def _clone_context(value: AdjacentContext | None) -> AdjacentContext | None:
    if value is None:
        return None
    return AdjacentContext(
        unit_id=value.unit_id,
        source=value.source,
        target=value.target,
        extensions=value.extensions.copy(),
    )


def _clone_tags(value: Tags | None) -> Tags | None:
    if value is None:
        return None
    return Tags(
        source_tag_map=_clone_tag_map(value.source_tag_map),
        target_tag_map=_clone_tag_map(value.target_tag_map),
        source_parts=_clone_parts(value.source_parts),
        target_parts=_clone_parts(value.target_parts),
    )


def _clone_target_tags(value: TargetTags | None) -> TargetTags | None:
    if value is None:
        return None
    return TargetTags(
        tag_map=_clone_tag_map(value.tag_map),
        parts=_clone_parts(value.parts),
    )


def _clone_tag_map(values: dict[str, TieData]) -> dict[str, TieData]:
    return {
        key: TieData(
            id=value.id,
            type=value.type,
            attributes=value.attributes.copy(),
            attribute_data=value.attribute_data,
            position=value.position,
            order=value.order,
            pair_id=value.pair_id,
            original_name=value.original_name,
            original_text=value.original_text,
        )
        for key, value in values.items()
    }


def _clone_parts(values: list[SegmentPart]) -> list[SegmentPart]:
    parts: list[SegmentPart] = []
    for value in values:
        if isinstance(value, TextPart):
            parts.append(TextPart(value.value))
        else:
            parts.append(CodePart(value.ref))
    return parts


def _set_target_tags(data: Data, value: TargetTags | None) -> None:
    selected = _clone_target_tags(value)
    if data.tags is None:
        if selected is not None:
            data.tags = Tags(
                target_tag_map=selected.tag_map,
                target_parts=selected.parts,
            )
        return
    if selected is None:
        data.tags.target_tag_map = {}
        data.tags.target_parts = []
    else:
        data.tags.target_tag_map = selected.tag_map
        data.tags.target_parts = selected.parts


def _clear_target_tags(data: Data) -> None:
    if data.tags is not None:
        data.tags.target_tag_map = {}
        data.tags.target_parts = []


def _document_target_locales(document: BaseStructure) -> tuple[str, ...]:
    seen: list[str] = []
    _extend_unique(seen, document.target_locales)
    if document.target_locale is not None:
        _extend_unique(seen, (document.target_locale,))
    for unit in document.data.values():
        _extend_unique(seen, unit.targets)
    return tuple(seen)


def _legacy_target_locale(
    target_locale: str | None,
    target_locales: tuple[str, ...],
) -> str | None:
    if target_locale is not None:
        return target_locale
    unique = _unique_locales(target_locales)
    return unique[0] if len(unique) == 1 else None


def _unique_locales(values: Iterable[str]) -> tuple[str, ...]:
    unique: list[str] = []
    _extend_unique(unique, values)
    return tuple(unique)


def _extend_unique(target: list[str], values: Iterable[str]) -> None:
    for value in values:
        if value and value not in target:
            target.append(value)


def _validate_locale_count(locales: tuple[str, ...]) -> None:
    if len(locales) > _MAX_SPLIT_TARGET_LOCALES:
        raise ValueError(f"target split supports at most {_MAX_SPLIT_TARGET_LOCALES} locales")


def _validate_requested_locales(locales: tuple[str, ...]) -> None:
    if any(not locale.strip() for locale in locales):
        raise ValueError("target locales cannot contain an empty value")


def _base_language(locale: str) -> str:
    return locale.replace("_", "-").split("-")[0].lower()
