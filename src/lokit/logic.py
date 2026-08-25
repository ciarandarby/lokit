from __future__ import annotations

import asyncio
import json
import tempfile
from collections import defaultdict
from dataclasses import asdict, fields, is_dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

from lokit.export_projection import prepare_export_data
from lokit.format_detection import LokitInputFormat, detect_format_from_bytes
from lokit.io.atomic import atomic_output_path
from lokit.io.stream_json import LokitJsonContext, write_lokit_json_stream
from lokit.placeholders import CanonicalPlaceholderText, canonicalize, reform
from lokit.types import TagSyntax, UnsupportedTagPolicy
from lokit.types.match import MatchResult as MatchResult

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    from lokit.data.structure import BaseStructure, Data, RegenProxy, TargetData
    from lokit.placeholders import PlaceholderSyntax

LokitT = TypeVar("LokitT", bound="Lokit")


class Lokit:
    def __init__(self, document: BaseStructure) -> None:
        """Initialize an instance of the main Lokit class"""
        self.document = document
        # Navigation and matching indexes can dwarf the document for large TMs.
        # Build them only when their APIs are used instead of on every parse.
        self._ids: list[str] | None = None
        self._positions: dict[str, int] | None = None
        self._source_index: dict[str, list[str]] | None = None
        self._normalized_sources: dict[str, str] | None = None
        self._token_index: dict[str, set[str]] | None = None
        self._match_source_snapshot: list[str] | None = None

    @classmethod
    def parse(
        cls: type[LokitT],
        filepath: str | Path,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> LokitT:
        """
        Classmethod:
            Imports and parses a file into a Lokit instance.
            Auto-detected filetype included for both ingestion and export.

        Intakes:
            Filepath of string or Path type.
        """
        from lokit import parse

        return cls(
            parse.file(
                str(filepath),
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            )
        )

    @classmethod
    def parse_bytes(
        cls: type[LokitT],
        data: bytes,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> LokitT:
        """
        Classmethod:
            Parses a byte-stream or payload into a Lokit instance.
        Intakes:
            data of type bytes
        """
        input_format = detect_format_from_bytes(data)
        suffix_map = {
            LokitInputFormat.LOKIT: ".lokit",
            LokitInputFormat.LOKIT_JSON: ".json",
            LokitInputFormat.TMX: ".tmx",
            LokitInputFormat.XLIFF: ".xliff",
            LokitInputFormat.CSV: ".csv",
            LokitInputFormat.XLSX: ".xlsx",
            LokitInputFormat.DOCX: ".docx",
            LokitInputFormat.PPTX: ".pptx",
            LokitInputFormat.HTML: ".html",
            LokitInputFormat.PO: ".po",
            LokitInputFormat.JSON_I18N: ".json",
            LokitInputFormat.IDML: ".idml",
        }
        suffix = suffix_map.get(input_format, ".json")
        # Reopening an active NamedTemporaryFile is not portable to Windows.
        with tempfile.TemporaryDirectory(prefix="lokit-parse-bytes-") as temporary_directory:
            path = Path(temporary_directory) / f"payload{suffix}"
            path.write_bytes(data)
            return cls.parse(
                path,
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            )

    @classmethod
    def from_document(cls: type[LokitT], document: BaseStructure) -> LokitT:
        """
        Classmethod:
            Directly creates Lokit instance from existing Lokit structure.
        Intakes:
            document of Lokit instance structure.
        """
        return cls(document)

    @classmethod
    def to_json(
        cls,
        filepath: str | Path,
        output: str | Path,
        context: Iterable[LokitJsonContext | str] | None = None,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> Path:
        """Compatibility alias for :meth:`to_jsonl`."""
        return cls.to_jsonl(
            filepath,
            output,
            context,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )

    @classmethod
    def to_jsonl(
        cls,
        filepath: str | Path,
        output: str | Path,
        context: Iterable[LokitJsonContext | str] | None = None,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> Path:
        """Stream a supported input file to newline-delimited JSON."""
        return asyncio.run(
            cls.to_jsonl_async(
                filepath,
                output,
                context,
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            )
        )

    @classmethod
    async def to_json_async(
        cls,
        filepath: str | Path,
        output: str | Path,
        context: Iterable[LokitJsonContext | str] | None = None,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> Path:
        """Compatibility alias for :meth:`to_jsonl_async`."""
        return await cls.to_jsonl_async(
            filepath,
            output,
            context,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )

    @classmethod
    async def to_jsonl_async(
        cls,
        filepath: str | Path,
        output: str | Path,
        context: Iterable[LokitJsonContext | str] | None = None,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> Path:
        """Asynchronously stream a supported input file to JSON Lines."""
        return await write_lokit_json_stream(
            filepath,
            output,
            context,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )

    def output(
        self,
        filepath: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        """
        Exports a Lokit structure to a filepath intaking a string or Path type.\n
        Auto-detection of filetypes included.
        """
        from lokit.parse import write

        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        suffix = path.suffix.lower()
        if suffix == ".lokit":
            write.lokit(self.document, path, resolve_placeholders=resolve_placeholders)
        elif suffix == ".tmx":
            write.tmx(self.document, path, resolve_placeholders=resolve_placeholders)
        elif suffix in (".xlf", ".xliff"):
            write.xliff(self.document, path, resolve_placeholders=resolve_placeholders)
        elif suffix == ".csv":
            write.csv(self.document, path, resolve_placeholders=resolve_placeholders)
        elif suffix == ".xlsx":
            write.xlsx(self.document, path, resolve_placeholders=resolve_placeholders)
        elif suffix in (".html", ".htm"):
            source_html = self.document.extensions.get("source_file") or self.document.extensions.get("source_html")
            write.html(
                self.document,
                path,
                source_html,
                resolve_placeholders=resolve_placeholders,
            )
        elif suffix in (".po", ".pot"):
            write.po(self.document, path, resolve_placeholders=resolve_placeholders)
        elif suffix == ".json":
            if self.document.extensions.get("input_format") == "json_i18n":
                write.json_i18n(
                    self.document,
                    path,
                    resolve_placeholders=resolve_placeholders,
                )
            else:
                self._write_document_json(
                    path,
                    resolve_placeholders=resolve_placeholders,
                )
        elif suffix == ".idml":
            source_idml = self.document.extensions.get("source_file") or self.document.extensions.get("source_idml")
            if not source_idml:
                raise ValueError(
                    "Original IDML file path not found in document extensions. Cannot export IDML without source IDML."
                )
            write.idml(
                self.document,
                path,
                source_idml,
                resolve_placeholders=resolve_placeholders,
            )
        elif suffix == ".docx":
            source_docx = self.document.extensions.get("source_file") or self.document.extensions.get("source_docx")
            write.docx(
                self.document,
                path,
                source_docx=source_docx,
                resolve_placeholders=resolve_placeholders,
            )
        elif suffix == ".pptx":
            source_pptx = self.document.extensions.get("source_file") or self.document.extensions.get("source_pptx")
            write.pptx(
                self.document,
                path,
                source_pptx=source_pptx,
                resolve_placeholders=resolve_placeholders,
            )
        else:
            self._write_document_json(
                path,
                resolve_placeholders=resolve_placeholders,
            )

    @property
    def regen(self) -> RegenProxy:
        return self.document.regen

    def _write_document_json(
        self,
        path: Path,
        *,
        resolve_placeholders: bool,
    ) -> None:
        encoder = json.JSONEncoder(ensure_ascii=False, indent=2, default=str)
        metadata = {
            field.name: getattr(self.document, field.name) for field in fields(self.document) if field.name != "data"
        }
        with atomic_output_path(path, "w", encoding="utf-8", newline="\n") as output:
            output.write("{\n")
            for index, (key, value) in enumerate(metadata.items()):
                if index:
                    output.write(",\n")
                output.write(f"  {json.dumps(key, ensure_ascii=False)}: ")
                output.write("".join(encoder.iterencode(value)).replace("\n", "\n  "))
            output.write(',\n  "data": {')
            for index, (unit_id, data) in enumerate(self.document.data.items()):
                output.write("," if index else "")
                output.write(f"\n    {json.dumps(unit_id, ensure_ascii=False)}: ")
                prepared = prepare_export_data(
                    data,
                    resolve_placeholders=resolve_placeholders,
                )
                output.write("".join(encoder.iterencode(asdict(prepared))).replace("\n", "\n    "))
            output.write("\n  }\n}\n")

    def unit(self, unit_id: str) -> Data:
        """
        Obtains translation data from a unit ID in string type
        """
        return self.document.data[unit_id]

    def target(self, unit_id: str, locale: str) -> TargetData | None:
        """
        Obtains target translation data via input of unit ID (string) and locale (string)
        """
        return self.document.data[unit_id].targets.get(locale)

    def targets(self, unit_id: str) -> dict[str, TargetData]:
        """
        Obtains a a copy of all target translations in a dictionary from a unit ID (string)
        """
        return self.document.data[unit_id].targets.copy()

    def all(self) -> Iterator[tuple[str, Data]]:
        """
        Returns an iterator that yields the unit_id (string) and translation data (Data) pairs for all units
        """
        yield from self.document.data.items()

    def ids(self) -> list[str]:
        """
        Returns a list of all unit IDs in a Lokit document
        """
        return list(self._ordered_ids())

    def previous(self, unit_id: str) -> tuple[str, Data] | None:
        """
        Returns the previous (unit_id (string), Data) pair relitive to a given unit ID (string)
        """
        positions = self._unit_positions()
        ids = self._ordered_ids()
        index = positions.get(unit_id)
        if index is None or index == 0:
            return None
        prev_id = ids[index - 1]
        return prev_id, self.document.data[prev_id]

    def next(self, unit_id: str) -> tuple[str, Data] | None:
        """
        Returns the next (unit_id (string), Data) pair relitive to a given unit ID (string)
        """
        positions = self._unit_positions()
        ids = self._ordered_ids()
        index = positions.get(unit_id)
        if index is None or index + 1 >= len(ids):
            return None
        next_id = ids[index + 1]
        return next_id, self.document.data[next_id]

    def plurals(self) -> Iterator[tuple[str, Data]]:
        """
        Iterates over and yields unit paris containing plural forms
        """
        for unit_id, unit in self.document.data.items():
            if unit.plural is not None:
                yield unit_id, unit

    def filter(
        self,
        predicate: Callable[[str, Data], bool],
    ) -> list[str]:
        """
        Filters and returns a list of unit IDs that match a bool predicate function
        """
        return [unit_id for unit_id, unit in self.document.data.items() if predicate(unit_id, unit)]

    def where(self, key_path: str, value: object) -> list[str]:
        """
        Queries and filters unit IDs by checking equality of the path/value
        """
        expected = str(value)
        return [
            unit_id
            for unit_id, unit in self.document.data.items()
            if expected in _values_at_path(unit, key_path.split("."))
        ]

    def fuzzy_find(
        self,
        source: str,
        limit: int = 10,
        threshold: float = 0.0,
    ) -> list[MatchResult]:
        """
        Matches a string to translation units returning pre-ranked 'MatchResult' objects.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError("threshold must be between 0.0 and 1.0")

        source_index, normalized_sources, _ = self._match_indexes()
        query_match = _canonical_match_text(source)
        normalized = query_match.text
        exact_ids = source_index.get(normalized, [])
        exact_fallback_limit = max(limit * 4, limit + 16)
        exact_results: list[MatchResult] = []
        safe_exact_results = 0
        for unit_id in exact_ids:
            result = self._match_against_unit(
                source,
                unit_id,
                require_context=False,
                require_tags=False,
                query_match=query_match,
            )
            if result.can_apply:
                exact_results.append(result)
                safe_exact_results += 1
                if safe_exact_results >= limit:
                    return [item for item in exact_results if item.can_apply][:limit]
            elif len(exact_results) < exact_fallback_limit:
                # Keep the fallback result set bounded, but continue examining
                # exact-source candidates until enough safely reformable
                # translations are found. Capping the scan itself can hide a
                # valid translation behind target-only placeholder variants.
                exact_results.append(result)
        exact_results.sort(key=lambda item: (item.can_apply, item.score), reverse=True)

        candidates: list[MatchResult] = exact_results
        candidate_ids = self._candidate_ids(
            normalized,
            set(),
            max(limit * 200, 1000),
            exclude_source=normalized,
        )
        for unit_id in candidate_ids:
            score = SequenceMatcher(None, normalized, normalized_sources[unit_id]).ratio()
            if score >= threshold:
                candidates.append(
                    self._match_against_unit(
                        source,
                        unit_id,
                        require_context=False,
                        require_tags=False,
                        query_match=query_match,
                        score=score,
                    )
                )

        candidates.sort(key=lambda item: (item.can_apply, item.score), reverse=True)
        return candidates[:limit]

    def _candidate_ids(
        self,
        normalized_source: str,
        exclude: set[str],
        max_candidates: int,
        exclude_source: str | None = None,
    ) -> list[str]:
        _, normalized_sources, token_index = self._match_indexes()
        scores: dict[str, int] = {}
        for token in _tokens(normalized_source):
            for unit_id in token_index.get(token, ()):
                if unit_id in exclude:
                    continue
                if exclude_source is not None and normalized_sources[unit_id] == exclude_source:
                    continue
                scores[unit_id] = scores.get(unit_id, 0) + 1
        if not scores:
            candidates: list[str] = []
            for unit_id in self.document.data:
                if unit_id in exclude:
                    continue
                if exclude_source is not None and normalized_sources[unit_id] == exclude_source:
                    continue
                candidates.append(unit_id)
                if len(candidates) >= max_candidates:
                    break
            return candidates
        return [
            unit_id for unit_id, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:max_candidates]
        ]

    def match(
        self,
        source: str,
        target_unit_id: str,
        previous_source: str | None = None,
        next_source: str | None = None,
        tag_signature: tuple[tuple[str, str | None], ...] | None = None,
        require_context: bool = False,
        require_tags: bool = False,
    ) -> MatchResult:
        """
        Performs ICE (In-context-exact), exact or fuzzy matching against a specific translation unit.
        """
        return self._match_against_unit(
            source,
            target_unit_id,
            previous_source=previous_source,
            next_source=next_source,
            tag_signature=tag_signature,
            require_context=require_context,
            require_tags=require_tags,
        )

    def _match_against_unit(
        self,
        source: str,
        unit_id: str,
        previous_source: str | None = None,
        next_source: str | None = None,
        tag_signature: tuple[tuple[str, str | None], ...] | None = None,
        require_context: bool = False,
        require_tags: bool = False,
        query_match: CanonicalPlaceholderText | None = None,
        score: float | None = None,
    ) -> MatchResult:
        unit = self.document.data[unit_id]
        canonical_source = query_match or _canonical_match_text(source)
        unit_normalized = (
            _canonical_match_text(unit.source).text
            if self._normalized_sources is None
            else self._normalized_sources[unit_id]
        )
        source_equal = canonical_source.text == unit_normalized
        match_score = (
            score if score is not None else SequenceMatcher(None, canonical_source.text, unit_normalized).ratio()
        )
        tags_equal = (not require_tags) or (tag_signature is not None and tag_signature == _tags_signature(unit))
        previous_equal = (not require_context) or (
            previous_source is not None
            and _normalize_text(previous_source) == _normalize_text(_context_text(unit.previous_context) or "")
        )
        next_equal = (not require_context) or (
            next_source is not None
            and _normalize_text(next_source) == _normalize_text(_context_text(unit.next_context) or "")
        )
        checked_ice_context = require_context or require_tags
        is_ice = checked_ice_context and source_equal and tags_equal and previous_equal and next_equal
        translation, placeholders_reformed = _reformed_translation(unit.source, unit.target, source)
        return MatchResult(
            unit_id=unit_id,
            score=1.0 if is_ice else match_score,
            kind="ice" if is_ice else ("exact" if source_equal else "fuzzy"),
            source_equal=source_equal,
            tags_equal=tags_equal,
            previous_equal=previous_equal,
            next_equal=next_equal,
            translation=translation,
            placeholders_reformed=placeholders_reformed,
            can_apply=translation is not None and tags_equal,
        )

    def _ordered_ids(self) -> list[str]:
        if self._ids is not None and self._ordered_ids_are_current():
            return self._ids
        self._invalidate_match_indexes()
        self._ids = list(self.document.data)
        self._positions = None
        return self._ids

    def _unit_positions(self) -> dict[str, int]:
        ids = self._ordered_ids()
        if self._positions is None:
            self._positions = {unit_id: index for index, unit_id in enumerate(ids)}
        return self._positions

    def _match_indexes(
        self,
    ) -> tuple[dict[str, list[str]], dict[str, str], dict[str, set[str]]]:
        if (
            self._source_index is not None
            and self._normalized_sources is not None
            and self._token_index is not None
            and self._match_source_snapshot is not None
            and self._ids is not None
            and self._match_indexes_are_current()
        ):
            return self._source_index, self._normalized_sources, self._token_index

        source_index: dict[str, list[str]] = defaultdict(list)
        normalized_sources: dict[str, str] = {}
        token_index: dict[str, set[str]] = defaultdict(set)
        indexed_ids: list[str] = []
        source_snapshot: list[str] = []
        for unit_id, unit in self.document.data.items():
            canonical = _canonical_match_text(unit.source)
            normalized_source = canonical.text
            indexed_ids.append(unit_id)
            source_snapshot.append(unit.source)
            normalized_sources[unit_id] = normalized_source
            source_index[normalized_source].append(unit_id)
            for token in _tokens(normalized_source):
                token_index[token].add(unit_id)
        self._ids = indexed_ids
        self._positions = None
        self._source_index = source_index
        self._normalized_sources = normalized_sources
        self._token_index = token_index
        self._match_source_snapshot = source_snapshot
        return source_index, normalized_sources, token_index

    def _ordered_ids_are_current(self) -> bool:
        if self._ids is None or len(self._ids) != len(self.document.data):
            return False
        return all(cached == current for cached, current in zip(self._ids, self.document.data, strict=True))

    def _match_indexes_are_current(self) -> bool:
        if (
            self._ids is None
            or self._match_source_snapshot is None
            or len(self._ids) != len(self.document.data)
            or len(self._match_source_snapshot) != len(self._ids)
        ):
            return False
        for index, (unit_id, unit) in enumerate(self.document.data.items()):
            if unit_id != self._ids[index] or unit.source != self._match_source_snapshot[index]:
                return False
        return True

    def _invalidate_match_indexes(self) -> None:
        self._source_index = None
        self._normalized_sources = None
        self._token_index = None
        self._match_source_snapshot = None


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _canonical_match_text(value: str) -> CanonicalPlaceholderText:
    canonical = canonicalize(value)
    return CanonicalPlaceholderText(
        _normalize_canonical_text(canonical.text),
        canonical.signature,
    )


def _normalize_canonical_text(value: str) -> str:
    # U+001F delimits native canonical slots, but Python considers it
    # whitespace.  Protect every delimiter while normalizing literal text so
    # case-sensitive placeholder grammar is scanned before case folding.
    separator = "\x1f"
    marker_index = 0
    marker = "\ufdd0LOKIT0\ufdef"
    folded_value = value.casefold()
    while marker.casefold() in folded_value:
        marker_index += 1
        marker = f"\ufdd0LOKIT{marker_index}\ufdef"
    protected = value.replace(separator, marker)
    return _normalize_text(protected).replace(marker.casefold(), separator)


def _reformed_translation(
    candidate_source: str,
    candidate_target: str | None,
    query_source: str,
) -> tuple[str | None, bool]:
    if candidate_target is None:
        return None, False
    try:
        result = reform(candidate_source, candidate_target, query_source)
    except ValueError:
        # An incompatible source graph, target-only placeholder, or bounded
        # scanner failure must never produce an automatically applicable target.
        return None, False
    return result.text, result.changed


def _tokens(value: str) -> set[str]:
    return {token for token in value.split() if token}


def _values_at_path(root: object, path: list[str]) -> list[str]:
    if not path:
        return [str(root)] if root is not None else []
    head = path[0]
    tail = path[1:]

    if isinstance(root, list):
        values: list[str] = []
        for item in root:
            values.extend(_values_at_path(item, path))
        return values

    if isinstance(root, dict):
        if head not in root:
            return []
        return _values_at_path(root[head], tail)

    if is_dataclass(root):
        if not hasattr(root, head):
            return []
        return _values_at_path(getattr(root, head), tail)

    return []


def _tags_signature(unit: Data) -> tuple[tuple[str, str | None], ...]:
    if unit.tags is None:
        return ()
    ordered = sorted(unit.tags.source_tag_map.values(), key=lambda item: item.order)
    return tuple((tag.type.value, tag.pair_id) for tag in ordered)


def _context_text(context: object) -> str | None:
    if context is None:
        return None
    source = getattr(context, "source", None)
    return cast("str | None", source)
