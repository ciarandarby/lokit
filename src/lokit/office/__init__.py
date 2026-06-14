from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.office.errors import (
    OfficeCancelledError,
    OfficePackageError,
    OfficeProtocolError,
    OfficeProtocolVersionError,
    OfficeReinsertionError,
    OfficeRuntimeUnavailable,
    OfficeTimeoutError,
    OfficeUnsupportedPackageError,
    OfficeValidationError,
    OfficeWorkerError,
)
from lokit.office.models import OfficeExportResult, OfficeRuntimeInfo, OfficeWarning
from lokit.office.options import (
    ExtraTranslationPolicy,
    MissingTranslationPolicy,
    OfficeExportOptions,
    OfficeImportOptions,
    OfficeValidationMode,
    TagMismatchPolicy,
)

if TYPE_CHECKING:
    from lokit.office.backend import (
        export_docx as export_docx,
    )
    from lokit.office.backend import (
        export_docx_async as export_docx_async,
    )
    from lokit.office.backend import (
        export_pptx as export_pptx,
    )
    from lokit.office.backend import (
        export_pptx_async as export_pptx_async,
    )
    from lokit.office.backend import (
        import_docx as import_docx,
    )
    from lokit.office.backend import (
        import_docx_async as import_docx_async,
    )
    from lokit.office.backend import (
        import_pptx as import_pptx,
    )
    from lokit.office.backend import (
        import_pptx_async as import_pptx_async,
    )
    from lokit.office.backend import (
        stream_docx as stream_docx,
    )
    from lokit.office.backend import (
        stream_pptx as stream_pptx,
    )

_BACKEND_EXPORTS = {
    "export_docx",
    "export_docx_async",
    "export_pptx",
    "export_pptx_async",
    "import_docx",
    "import_docx_async",
    "import_pptx",
    "import_pptx_async",
    "stream_docx",
    "stream_pptx",
}

__all__ = [
    "ExtraTranslationPolicy",
    "MissingTranslationPolicy",
    "OfficeCancelledError",
    "OfficeExportOptions",
    "OfficeExportResult",
    "OfficeImportOptions",
    "OfficePackageError",
    "OfficeProtocolError",
    "OfficeProtocolVersionError",
    "OfficeReinsertionError",
    "OfficeRuntimeInfo",
    "OfficeRuntimeUnavailable",
    "OfficeTimeoutError",
    "OfficeUnsupportedPackageError",
    "OfficeValidationError",
    "OfficeValidationMode",
    "OfficeWarning",
    "OfficeWorkerError",
    "TagMismatchPolicy",
    "export_docx",
    "export_docx_async",
    "export_pptx",
    "export_pptx_async",
    "import_docx",
    "import_docx_async",
    "import_pptx",
    "import_pptx_async",
    "stream_docx",
    "stream_pptx",
]


def __getattr__(name: str) -> object:
    if name in _BACKEND_EXPORTS:
        from lokit.office import backend

        return getattr(backend, name)
    raise AttributeError(name)
