import importlib
from types import ModuleType

from lokit.data.structure import (
    AdjacentContext,
    BaseStructure,
    CodePart,
    Comment,
    ConversionStats,
    Data,
    Meta,
    Origin,
    Plural,
    PluralCategory,
    SegmentPart,
    StreamingStructure,
    Tags,
    TextPart,
    TranslationStatus,
)
from lokit.data.tag_types import TieData, TieType
from lokit.exporters import (
    export_csv,
    export_csv_async,
    export_idml,
    export_idml_async,
    export_html,
    export_html_async,
    export_json_i18n,
    export_json_i18n_async,
    export_po,
    export_po_async,
    export_tmx,
    export_tmx_from_json,
    export_xliff,
    export_xliff_async,
    export_xliff_from_json,
    export_xliff_from_json_async,
    export_xlsx,
    export_xlsx_async,
)
from lokit.importers import (
    import_csv,
    import_csv_async,
    import_file,
    import_file_async,
    import_idml,
    import_idml_async,
    import_html,
    import_html_async,
    import_json_i18n,
    import_json_i18n_async,
    import_po,
    import_po_async,
    import_tmx,
    import_tmx_async,
    import_tmx_batches_async,
    import_tmx_parallel,
    process_tmx_async,
    stream_tmx,
    stream_tmx_parallel,
    convert_tmx_to_csv,
    convert_tmx_to_tmx,
    convert_tmx_to_xliff,
    import_xliff,
    import_xliff_async,
    import_xlsx,
    import_xlsx_async,
)
from lokit.io import load_lokit_json, load_lokit_json_bytes
from lokit.io.stream_json import LokitJsonContext
from lokit.logic import Lokit, MatchResult
from lokit.parsers.csv.extraction import CsvExtractor
from lokit.parsers.xlsx.extraction import XlsxExtractor
from lokit.parsers.html.extraction import HtmlExtractor
from lokit.parsers.po.extraction import PoExtractor
from lokit.parsers.json_i18n.extraction import JsonI18nExtractor
from lokit.parsers.idml.extraction import IdmlExtractor
from lokit.parsers.tmx.extraction import TmxExtractor
from lokit.parsers.tmx.models import TmxParseMode
from lokit.parsers.tmx.parallel import TmxParallelOptions
from lokit.parsers.xliff.extraction import XliffExtractor
from lokit import data as data
from lokit import exporters as exporters
from lokit import io as io
from lokit import parsers as parsers

__all__ = [
    "AdjacentContext",
    "BaseStructure",
    "CodePart",
    "Comment",
    "ConversionStats",
    "Data",
    "Meta",
    "Lokit",
    "LokitJsonContext",
    "MatchResult",
    "Origin",
    "Plural",
    "PluralCategory",
    "SegmentPart",
    "StreamingStructure",
    "Tags",
    "TextPart",
    "TieData",
    "TieType",
    "TmxExtractor",
    "TmxParseMode",
    "TmxParallelOptions",
    "TranslationStatus",
    "XliffExtractor",
    "CsvExtractor",
    "XlsxExtractor",
    "HtmlExtractor",
    "PoExtractor",
    "JsonI18nExtractor",
    "IdmlExtractor",
    "data",
    "db",
    "exporters",
    "export_csv",
    "export_csv_async",
    "export_idml",
    "export_idml_async",
    "export_html",
    "export_html_async",
    "export_json_i18n",
    "export_json_i18n_async",
    "export_po",
    "export_po_async",
    "export_tmx",
    "export_tmx_from_json",
    "export_xliff",
    "export_xliff_async",
    "export_xliff_from_json",
    "export_xliff_from_json_async",
    "export_xlsx",
    "export_xlsx_async",
    "import_csv",
    "import_csv_async",
    "import_file",
    "import_file_async",
    "import_idml",
    "import_idml_async",
    "import_html",
    "import_html_async",
    "import_json_i18n",
    "import_json_i18n_async",
    "import_po",
    "import_po_async",
    "import_tmx",
    "import_tmx_async",
    "import_tmx_batches_async",
    "import_tmx_parallel",
    "process_tmx_async",
    "stream_tmx",
    "stream_tmx_parallel",
    "convert_tmx_to_csv",
    "convert_tmx_to_tmx",
    "convert_tmx_to_xliff",
    "import_xliff",
    "import_xliff_async",
    "import_xlsx",
    "import_xlsx_async",
    "io",
    "load_lokit_json",
    "load_lokit_json_bytes",
    "parsers",
]


def __getattr__(name: str) -> ModuleType:
    if name == "db":
        return importlib.import_module("lokit.db")
    raise AttributeError(name)
