from typing import Optional

from lxml import etree

from lokit.core.logger import logger
from lokit.parsers.tmx.header import TmxHeaderParser
from lokit.parsers.tmx.models import HeaderData
from lokit.parsers.tmx.xml_utils import (
    clear_element,
    element_children,
    iterparse_safe,
    local_name,
)


class TmxParser:
    def __init__(
        self,
        tmx_file_path: str,
        source_language: Optional[str] = None,
        target_language: Optional[str] = None,
        domain: Optional[str] = None,
        parse_header: bool = True,
    ) -> None:
        self.filepath: str = tmx_file_path
        self.domain: str = domain or ""

        self.native_source: str = source_language or ""
        self.native_target: str = target_language or ""

        self.source_language: Optional[str] = None
        self.source_locale: Optional[str] = None
        self.target_language: Optional[str] = None
        self.target_locale: Optional[str] = None

        self.export_origin: str = ""
        self.export_timestamp: str = ""
        self.extensions: dict[str, str] = {}

        self.header_parser: TmxHeaderParser = TmxHeaderParser()
        self._parse_header: bool = parse_header
        self._header_initialized: bool = not parse_header

        if not parse_header:
            self._validate_and_set_languages()
        self.native_source_base: str = self._base_lang(self.native_source)
        self.native_target_base: str = self._base_lang(self.native_target)
        self._lang_base_cache: dict[str, str] = {}

    def _initialize_from_file(self) -> None:
        if self._header_initialized:
            return
        context = iterparse_safe(self.filepath, events=("end",))

        for _, elem in context:
            elem_name = local_name(elem.tag)
            if elem_name == "header":
                self.initialize_from_header_element(elem)
                clear_element(elem)
                if self.native_source and self.native_target:
                    break
                continue

            if elem_name == "tu":
                self.initialize_from_tu_element(elem)
                clear_element(elem)
                if self.native_source and self.native_target:
                    break

    def initialize_from_header_element(self, element: etree._Element) -> None:
        if self._header_initialized:
            return
        header_data: HeaderData = self.header_parser.parse(element)
        self.export_origin = header_data.origin
        self.export_timestamp = header_data.timestamp
        self.extensions.update(header_data.extensions)

        if self.native_source:
            if header_data.srclang and not self._compare_base_lang(
                self.native_source, header_data.srclang
            ):
                logger.warning(
                    f"Provided source '{self.native_source}' mismatches header '{header_data.srclang}'"
                )
        else:
            self.native_source = header_data.srclang

        if self.native_target:
            if header_data.tgtlang and not self._compare_base_lang(
                self.native_target, header_data.tgtlang
            ):
                logger.warning(
                    f"Provided target '{self.native_target}' mismatches header '{header_data.tgtlang}'"
                )
        else:
            self.native_target = header_data.tgtlang

        if self.native_source and self.native_target:
            self._finalize_header_initialization()

    def initialize_from_tu_element(self, element: etree._Element) -> None:
        if self._header_initialized:
            return
        self._initialize_missing_languages_from_tu(element)
        if self.native_source and self.native_target:
            self._finalize_header_initialization()

    def _finalize_header_initialization(self) -> None:
        self._header_initialized = True
        self._validate_and_set_languages()
        self.native_source_base = self._base_lang(self.native_source)
        self.native_target_base = self._base_lang(self.native_target)
        self._lang_base_cache.clear()

    def _compare_base_lang(self, lang1: str, lang2: str) -> bool:
        if not lang1 or not lang2:
            return False
        return self._base_lang(lang1) == self._base_lang(lang2)

    def _base_lang(self, lang: str) -> str:
        if not lang:
            return ""
        normalized = lang.replace("_", "-")
        return normalized.split("-", 1)[0].lower()

    def _cached_base_lang(self, lang: str) -> str:
        cached = self._lang_base_cache.get(lang)
        if cached is not None:
            return cached
        base_lang = self._base_lang(lang)
        self._lang_base_cache[lang] = base_lang
        return base_lang

    def _initialize_missing_languages_from_tu(self, element: etree._Element) -> None:
        langs: list[str] = []
        for tuv in element_children(element, "tuv"):
            lang = self._get_xml_lang(tuv)
            if lang:
                langs.append(lang)

        if not self.native_source and langs:
            self.native_source = langs[0]

        if not self.native_target:
            self.native_target = next(
                (
                    lang
                    for lang in langs
                    if not self._compare_base_lang(lang, self.native_source)
                ),
                "",
            )

    def _validate_and_set_languages(self) -> None:
        if self.native_source:
            self.source_language, self.source_locale = self._parse_locale_string(
                self.native_source
            )
        if self.native_target:
            self.target_language, self.target_locale = self._parse_locale_string(
                self.native_target
            )

    def _parse_locale_string(self, lang_string: str) -> tuple[str, str]:
        if not lang_string:
            raise ValueError("Cannot parse empty language string")

        normalized: str = lang_string.replace("_", "-")
        parts: list[str] = normalized.split("-")

        lang_code: str = parts[0].lower()
        canonical_parts = [lang_code]
        if len(parts) > 1:
            canonical_parts.extend(
                self._canonicalize_subtag(part) for part in parts[1:]
            )

        return lang_code, "-".join(canonical_parts)

    def _canonicalize_subtag(self, subtag: str) -> str:
        if len(subtag) == 2 and subtag.isalpha():
            return subtag.upper()
        if len(subtag) == 4 and subtag.isalpha():
            return subtag.title()
        return subtag

    def _get_xml_lang(self, element: etree._Element) -> str:
        return (
            element.get("{http://www.w3.org/XML/1998/namespace}lang")
            or element.get("lang")
            or ""
        )
