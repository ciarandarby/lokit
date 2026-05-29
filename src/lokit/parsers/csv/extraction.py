from __future__ import annotations

import csv
from pathlib import Path
from typing import AsyncIterator, Iterator

from lokit.data.structure import Comment, Data, TranslationStatus
from lokit.parsers.async_bridge import AsyncExtractionBridge

ExtractItem = tuple[str, Data]

_KNOWN_COLUMNS = frozenset({"id", "source", "target", "status", "comment"})


def _parse_base_lang(locale: str) -> str:
    return locale.replace("_", "-").split("-")[0].lower()


def _parse_status(value: str) -> TranslationStatus:
    normalized = value.strip().lower()
    try:
        return TranslationStatus(normalized)
    except ValueError:
        return TranslationStatus.UNKNOWN


def _infer_locales_from_filename(filepath: str) -> tuple[str, str | None]:
    stem = Path(filepath).stem
    if "-" in stem:
        parts = stem.split("-")
        if len(parts) == 2:
            return parts[0], parts[1]
        if len(parts) == 4:
            return f"{parts[0]}-{parts[1]}", f"{parts[2]}-{parts[3]}"
    if "_" in stem:
        parts = stem.split("_")
        if len(parts) == 2:
            return parts[0], parts[1]
        if len(parts) == 4:
            return f"{parts[0]}_{parts[1]}", f"{parts[2]}_{parts[3]}"
    return "", None


class CsvExtractor:
    def __init__(
        self,
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
    ) -> None:
        self.filepath: str = filepath

        if source_locale:
            self.source_locale: str = source_locale
            self.target_locale: str | None = target_locale
        else:
            inferred_source, inferred_target = _infer_locales_from_filename(filepath)
            self.source_locale = inferred_source
            self.target_locale = target_locale or inferred_target

        self.source_language: str | None = (
            _parse_base_lang(self.source_locale) if self.source_locale else None
        )
        self.target_language: str | None = (
            _parse_base_lang(self.target_locale) if self.target_locale else None
        )

        self.export_origin: str = ""
        self.export_timestamp: str = ""
        self.extensions: dict[str, str] = {"input_format": "csv"}

    def extract(self) -> Iterator[ExtractItem]:
        with open(self.filepath, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            fieldnames: list[str] = list(reader.fieldnames or [])
            has_id = "id" in fieldnames
            extra_columns = [c for c in fieldnames if c not in _KNOWN_COLUMNS]

            for index, row in enumerate(reader):
                unit_id = row["id"] if has_id and row.get("id") else f"csv:{index}"
                source = row.get("source", "")
                target = row.get("target") or None
                status = _parse_status(row["status"]) if row.get("status") else TranslationStatus.UNKNOWN

                comments: list[Comment] = []
                comment_text = row.get("comment", "").strip()
                if comment_text:
                    comments.append(Comment(context=comment_text))

                extensions: dict[str, str] = {
                    col: row[col] for col in extra_columns if row.get(col)
                }

                yield unit_id, Data(
                    source=source,
                    target=target,
                    status=status,
                    comments=comments,
                    extensions=extensions,
                )

    def extract_async(self) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(self.extract)
