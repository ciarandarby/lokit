from __future__ import annotations

from dataclasses import dataclass

from lokit.parsers.csv.extraction import CsvExtractor
from lokit.parsers.html.extraction import HtmlExtractor
from lokit.parsers.idml.extraction import IdmlExtractor
from lokit.parsers.json_i18n.extraction import JsonI18nExtractor
from lokit.parsers.po.extraction import PoExtractor
from lokit.parsers.tmx.extraction import TmxExtractor
from lokit.parsers.tmx.models import TmxParseMode
from lokit.parsers.tmx.parallel import TmxParallelOptions
from lokit.parsers.xliff.extraction import XliffExtractor
from lokit.parsers.xlsx.extraction import XlsxExtractor


@dataclass(frozen=True)
class ExtractorNamespace:
    csv: type[CsvExtractor] = CsvExtractor
    html: type[HtmlExtractor] = HtmlExtractor
    idml: type[IdmlExtractor] = IdmlExtractor
    json: type[JsonI18nExtractor] = JsonI18nExtractor
    json_i18n: type[JsonI18nExtractor] = JsonI18nExtractor
    po: type[PoExtractor] = PoExtractor
    tmx: type[TmxExtractor] = TmxExtractor
    xliff: type[XliffExtractor] = XliffExtractor
    xlsx: type[XlsxExtractor] = XlsxExtractor


extractors = ExtractorNamespace()

__all__ = [
    "CsvExtractor",
    "ExtractorNamespace",
    "HtmlExtractor",
    "IdmlExtractor",
    "JsonI18nExtractor",
    "PoExtractor",
    "TmxExtractor",
    "TmxParallelOptions",
    "TmxParseMode",
    "XliffExtractor",
    "XlsxExtractor",
    "extractors",
]
