from __future__ import annotations

import asyncio
import json
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass, is_dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

from lokit.format_detection import LokitInputFormat, detect_format, detect_format_from_bytes
from lokit.io import load_lokit_json, load_lokit_json_bytes
from lokit.io.atomic import atomic_output_path
from lokit.io.stream_json import LokitJsonContext, write_lokit_json_stream

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from lokit.data.structure import BaseStructure, Data, RegenProxy, TargetData

LokitT = TypeVar("LokitT", bound="Lokit")


@dataclass(slots=True)
class MatchResult:
    unit_id: str
    score: float
    kind: str
    source_equal: bool
    tags_equal: bool
    previous_equal: bool
    next_equal: bool


class Lokit:
    def __init__(self, document: BaseStructure) -> None:
        """Initialize an instance of the main Lokit class"""
        self.document = document
        self._ids: list[str] = list(document.data)
        self._positions: dict[str, int] = {unit_id: index for index, unit_id in enumerate(self._ids)}
        self._source_index: dict[str, list[str]] = defaultdict(list)
        self._normalized_sources: dict[str, str] = {}
        self._token_index: dict[str, set[str]] = defaultdict(set)
        for unit_id, unit in document.data.items():
            normalized_source = _normalize_text(unit.source)
            self._normalized_sources[unit_id] = normalized_source
            self._source_index[normalized_source].append(unit_id)
            for token in _tokens(normalized_source):
                self._token_index[token].add(unit_id)

    @classmethod
    def parse(cls: type[LokitT], filepath: str | Path) -> LokitT:
        """
        Classmethod:
            Imports and parses a file into a Lokit instance.
            Auto-detected filetype included for both ingestion and export.

        Intakes:
            Filepath of string or Path type.
        """
        from lokit import parse

        path = Path(filepath)
        input_format = detect_format(path)
        if input_format == LokitInputFormat.TMX:
            return cls(parse.tmx(str(path)))
        if input_format == LokitInputFormat.XLIFF:
            return cls(parse.xliff(str(path)))
        if input_format == LokitInputFormat.CSV:
            return cls(parse.csv(str(path)))
        if input_format == LokitInputFormat.XLSX:
            return cls(parse.xlsx(str(path)))
        if input_format == LokitInputFormat.HTML:
            return cls(parse.html(str(path)))
        if input_format == LokitInputFormat.PO:
            return cls(parse.po(str(path)))
        if input_format == LokitInputFormat.JSON_I18N:
            return cls(parse.json_i18n(str(path)))
        if input_format == LokitInputFormat.IDML:
            return cls(parse.idml(str(path)))
        if input_format == LokitInputFormat.DOCX:
            return cls(parse.docx(path))
        if input_format == LokitInputFormat.PPTX:
            return cls(parse.pptx(path))
        return cls(load_lokit_json(path))

    @classmethod
    def parse_bytes(cls: type[LokitT], data: bytes) -> LokitT:
        """
        Classmethod:
            Parses a byte-stream or payload into a Lokit instance.
        Intakes:
            data of type bytes
        """
        input_format = detect_format_from_bytes(data)
        if input_format == LokitInputFormat.LOKIT_JSON:
            return cls(load_lokit_json_bytes(data))
        suffix_map = {
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
        with tempfile.NamedTemporaryFile(suffix=suffix) as temp:
            temp.write(data)
            temp.flush()
            return cls.parse(temp.name)

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
    ) -> Path:
        """
        Writes a Lokit instance to a JSON file synchronously.

        Intakes:
            filepath (string, Path),
            output (string, Path),
            context Optional[LokitJsonContext | String]
        """
        return asyncio.run(cls.to_json_async(filepath, output, context))

    @classmethod
    async def to_json_async(
        cls,
        filepath: str | Path,
        output: str | Path,
        context: Iterable[LokitJsonContext | str] | None = None,
    ) -> Path:
        """
        Writes a Lokit instance to a JSON file asynchronously.

        Intakes:
            filepath (string, Path),
            output (string, Path),
            context Optional[LokitJsonContext | String]
        """
        return await write_lokit_json_stream(filepath, output, context)

    def output(self, filepath: str | Path) -> None:
        """
        Exports a Lokit structure to a filepath intaking a string or Path type.\n
        Auto-detection of filetypes included.
        """
        from lokit.parse import write

        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        suffix = path.suffix.lower()
        if suffix == ".tmx":
            write.tmx(self.document, path)
        elif suffix in (".xlf", ".xliff"):
            write.xliff(self.document, path)
        elif suffix == ".csv":
            write.csv(self.document, path)
        elif suffix == ".xlsx":
            write.xlsx(self.document, path)
        elif suffix in (".html", ".htm"):
            source_html = self.document.extensions.get("source_file") or self.document.extensions.get("source_html")
            write.html(self.document, path, source_html)
        elif suffix == ".po":
            write.po(self.document, path)
        elif suffix == ".json":
            if self.document.extensions.get("input_format") == "json_i18n":
                write.json_i18n(self.document, path)
            else:
                self._write_document_json(path)
        elif suffix == ".idml":
            source_idml = self.document.extensions.get("source_file") or self.document.extensions.get("source_idml")
            if not source_idml:
                raise ValueError(
                    "Original IDML file path not found in document extensions. Cannot export IDML without source IDML."
                )
            write.idml(self.document, path, source_idml)
        elif suffix == ".docx":
            source_docx = self.document.extensions.get("source_file") or self.document.extensions.get("source_docx")
            write.docx(self.document, path, source_docx=source_docx)
        elif suffix == ".pptx":
            source_pptx = self.document.extensions.get("source_file") or self.document.extensions.get("source_pptx")
            write.pptx(self.document, path, source_pptx=source_pptx)
        else:
            self._write_document_json(path)

    @property
    def regen(self) -> RegenProxy:
        return self.document.regen

    def _write_document_json(self, path: Path) -> None:
        with atomic_output_path(path, "w") as f:
            json.dump(asdict(self.document), f, ensure_ascii=False, indent=2, default=str)
            f.write("\n")

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
        return list(self._ids)

    def previous(self, unit_id: str) -> tuple[str, Data] | None:
        """
        Returns the previous (unit_id (string), Data) pair relitive to a given unit ID (string)
        """
        index = self._positions.get(unit_id)
        if index is None or index == 0:
            return None
        prev_id = self._ids[index - 1]
        return prev_id, self.document.data[prev_id]

    def next(self, unit_id: str) -> tuple[str, Data] | None:
        """
        Returns the next (unit_id (string), Data) pair relitive to a given unit ID (string)
        """
        index = self._positions.get(unit_id)
        if index is None or index + 1 >= len(self._ids):
            return None
        next_id = self._ids[index + 1]
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
        normalized = _normalize_text(source)
        exact_ids = self._source_index.get(normalized, [])
        exact_results = [
            self._match_against_unit(source, unit_id, require_context=False, require_tags=False)
            for unit_id in exact_ids
        ]
        if len(exact_results) >= limit:
            return exact_results[:limit]

        candidates: list[MatchResult] = exact_results
        exact_set = set(exact_ids)
        candidate_ids = self._candidate_ids(normalized, exact_set, max(limit * 200, 1000))
        for unit_id in candidate_ids:
            if unit_id in exact_set:
                continue
            score = SequenceMatcher(None, normalized, self._normalized_sources[unit_id]).ratio()
            if score >= threshold:
                candidates.append(
                    MatchResult(
                        unit_id=unit_id,
                        score=score,
                        kind="fuzzy",
                        source_equal=False,
                        tags_equal=False,
                        previous_equal=False,
                        next_equal=False,
                    )
                )

        candidates.sort(key=lambda item: item.score, reverse=True)
        return candidates[:limit]

    def _candidate_ids(
        self,
        normalized_source: str,
        exclude: set[str],
        max_candidates: int,
    ) -> list[str]:
        scores: dict[str, int] = {}
        for token in _tokens(normalized_source):
            for unit_id in self._token_index.get(token, ()):
                if unit_id in exclude:
                    continue
                scores[unit_id] = scores.get(unit_id, 0) + 1
        if not scores:
            return [unit_id for unit_id in self._ids if unit_id not in exclude][:max_candidates]
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
    ) -> MatchResult:
        unit = self.document.data[unit_id]
        normalized_source = _normalize_text(source)
        unit_normalized = self._normalized_sources[unit_id]
        source_equal = normalized_source == unit_normalized
        score = SequenceMatcher(None, normalized_source, unit_normalized).ratio()
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
        return MatchResult(
            unit_id=unit_id,
            score=1.0 if is_ice else score,
            kind="ice" if is_ice else ("exact" if source_equal else "fuzzy"),
            source_equal=source_equal,
            tags_equal=tags_equal,
            previous_equal=previous_equal,
            next_equal=next_equal,
        )


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


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
