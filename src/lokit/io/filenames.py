from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


MAX_PORTABLE_FILENAME_BYTES = 255
MAX_PORTABLE_FILENAME_UTF16_UNITS = 255
MAX_LOCALE_OUTPUTS = 256

UNSAFE_LOCALE = "unsafe"
RESERVED_LOCALE = "reserved"
FILENAME_TOO_LONG = "too_long"
FILENAME_COLLISION = "collision"
TOO_MANY_OUTPUTS = "too_many"

_WINDOWS_FORBIDDEN_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_STEMS = frozenset(
    {
        "aux",
        "clock$",
        "com1",
        "com2",
        "com3",
        "com4",
        "com5",
        "com6",
        "com7",
        "com8",
        "com9",
        "com¹",
        "com²",
        "com³",
        "con",
        "conin$",
        "conout$",
        "lpt1",
        "lpt2",
        "lpt3",
        "lpt4",
        "lpt5",
        "lpt6",
        "lpt7",
        "lpt8",
        "lpt9",
        "lpt¹",
        "lpt²",
        "lpt³",
        "nul",
        "prn",
    }
)


class LocaleFilenameError(ValueError):
    """A target locale cannot be represented by a portable output name."""

    reason: str
    locale: str | None
    filename: str | None

    def __init__(
        self,
        reason: str,
        *,
        locale: str | None = None,
        filename: str | None = None,
    ) -> None:
        self.reason = reason
        self.locale = locale
        self.filename = filename
        super().__init__(reason)


def locale_output_names(
    locales: Iterable[str],
    *,
    prefix: str = "",
    suffix: str,
) -> tuple[tuple[str, str], ...]:
    """Build bounded, cross-platform-safe filenames for target locales.

    Exact duplicates within the bounded request are ignored. Distinct
    spellings that would map to the same case-insensitive, canonically
    normalized filename are rejected. Returned filenames use NFC so their
    spelling is stable across platforms.
    """

    affix_characters = len(prefix) + len(suffix)
    if affix_characters > MAX_PORTABLE_FILENAME_UTF16_UNITS:
        raise LocaleFilenameError(FILENAME_TOO_LONG, filename=f"{prefix}{suffix}")
    outputs: list[tuple[str, str]] = []
    seen_locales: set[str] = set()
    collision_keys: set[str] = set()
    inputs_seen = 0
    for locale in locales:
        inputs_seen += 1
        if inputs_seen > MAX_LOCALE_OUTPUTS:
            raise LocaleFilenameError(TOO_MANY_OUTPUTS)
        if len(locale) + affix_characters > MAX_PORTABLE_FILENAME_UTF16_UNITS:
            raise LocaleFilenameError(FILENAME_TOO_LONG, locale=locale)
        if locale in seen_locales:
            continue
        seen_locales.add(locale)

        normalized_locale = _portable_locale(locale)
        filename = f"{prefix}{normalized_locale}{suffix}"
        _validate_filename_size(filename, locale)
        collision_key = _collision_key(filename)
        if collision_key in collision_keys:
            raise LocaleFilenameError(
                FILENAME_COLLISION,
                locale=locale,
                filename=filename,
            )
        collision_keys.add(collision_key)
        outputs.append((locale, filename))
    return tuple(outputs)


def _portable_locale(locale: str) -> str:
    if not locale:
        raise LocaleFilenameError(UNSAFE_LOCALE, locale=locale)
    normalized = unicodedata.normalize("NFC", locale)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LocaleFilenameError(UNSAFE_LOCALE, locale=locale) from exc
    if not normalized or normalized in {".", ".."} or normalized.startswith(".") or normalized.endswith((" ", ".")):
        raise LocaleFilenameError(UNSAFE_LOCALE, locale=locale)
    for character in normalized:
        if character in _WINDOWS_FORBIDDEN_CHARACTERS or unicodedata.category(character)[0] in {"C", "Z"}:
            raise LocaleFilenameError(UNSAFE_LOCALE, locale=locale)

    reserved_stem = normalized.split(".", 1)[0].rstrip(" .").casefold()
    if reserved_stem in _WINDOWS_RESERVED_STEMS:
        raise LocaleFilenameError(RESERVED_LOCALE, locale=locale)
    return normalized


def _validate_filename_size(filename: str, locale: str) -> None:
    try:
        utf8_lengths = (
            len(filename.encode("utf-8")),
            len(unicodedata.normalize("NFD", filename).encode("utf-8")),
        )
        utf16_units = len(filename.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise LocaleFilenameError(UNSAFE_LOCALE, locale=locale, filename=filename) from exc
    if max(utf8_lengths) > MAX_PORTABLE_FILENAME_BYTES or utf16_units > MAX_PORTABLE_FILENAME_UTF16_UNITS:
        raise LocaleFilenameError(FILENAME_TOO_LONG, locale=locale, filename=filename)


def _collision_key(filename: str) -> str:
    return unicodedata.normalize("NFC", filename.casefold())
