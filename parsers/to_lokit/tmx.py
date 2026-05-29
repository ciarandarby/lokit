"""Original code for parsing tmx files, refactor to lokit in progress, here for context only"""

from typing import Optional

from lxml import etree


class TmxParser:
    def __init__(
        self,
        tmx_file_path: str,
        source_language: Optional[str] = None,
        target_language: Optional[str] = None,
        domain: Optional[str] = None,
    ):
        self.filepath: str = tmx_file_path
        self.domain: str = domain or ""

        self.native_source: str = source_language or ""
        self.native_target: str = target_language or ""

        self.source_language: str = ""
        self.source_locale: str = ""
        self.target_language: str = ""
        self.target_locale: str = ""

        self._initialize_languages()

    def _initialize_languages(self) -> None:
        if not self.native_source:
            self._extract_source_from_file()

        if not self.native_target:
            self._extract_target_from_file()

        self.source_language, self.source_locale = self._parse_locale_string(
            self.native_source
        )
        self.target_language, self.target_locale = self._parse_locale_string(
            self.native_target
        )

    def _parse_locale_string(self, lang_string: str) -> tuple[str, str]:
        if not lang_string:
            return "", ""

        normalized: str = lang_string.replace("_", "-")
        parts: list[str] = normalized.split("-")

        if len(parts) > 1:
            return parts[0], parts[1]

        return parts[0], ""

    def _extract_source_from_file(self) -> None:
        extracted_lang: str = ""
        context = etree.iterparse(self.filepath, events=("start",))

        for _, elem in context:
            if elem.tag.endswith("header"):
                val: Optional[str] = elem.attrib.get("srclang")
                if val and val != "*all*":
                    extracted_lang = val
                    elem.clear()
                    break

            if elem.tag.endswith("tuv"):
                val = elem.attrib.get(
                    "{http://www.w3.org/XML/1998/namespace}lang"
                ) or elem.attrib.get("lang")
                if val:
                    extracted_lang = val
                    elem.clear()
                    break

        self.native_source = extracted_lang

    def _extract_target_from_file(self) -> None:
        extracted_lang: str = ""
        context = etree.iterparse(self.filepath, events=("start",))

        for _, elem in context:
            if elem.tag.endswith("header"):
                val: Optional[str] = elem.attrib.get("tgtlang")
                if val and val != "*all*":
                    extracted_lang = val
                    elem.clear()
                    break

            if elem.tag.endswith("tuv") and self.native_source:
                val = elem.attrib.get(
                    "{http://www.w3.org/XML/1998/namespace}lang"
                ) or elem.attrib.get("lang")
                if val and val != self.native_source:
                    extracted_lang = val
                    elem.clear()
                    break

        self.native_target = extracted_lang
