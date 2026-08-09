from __future__ import annotations

from typing import Final, TypeAlias

from lokit.compat import StrEnum


class StringMode(StrEnum):
    """String representation used by interchange dictionary projections."""

    SANITIZED = "sanitized"
    RAW = "raw"


class DictField(StrEnum):
    """Fields available in an interchange dictionary projection."""

    SOURCE_LANGUAGE = "source_language"
    TARGET_LANGUAGE = "target_language"
    SOURCE = "source"
    TARGET = "target"
    DOMAIN = "domain"
    UNIT_ID = "unit_id"
    SOURCE_LOCALE = "source_locale"
    TARGET_LOCALE = "target_locale"
    STATUS = "status"
    RESOURCE = "resource"
    PROJECT = "project"


TranslationRow: TypeAlias = dict[str, str]

DEFAULT_DICT_FIELDS: Final[tuple[DictField, ...]] = (
    DictField.SOURCE_LANGUAGE,
    DictField.TARGET_LANGUAGE,
    DictField.SOURCE,
    DictField.TARGET,
    DictField.DOMAIN,
)
