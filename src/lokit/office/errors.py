class OfficeError(Exception):
    """Base exception for Office document adapter failures."""


class OfficeRuntimeUnavailable(OfficeError):
    """Raised when a required external Office runtime cannot be located."""


class OfficeProtocolError(OfficeError):
    """Raised when the Python/worker protocol is malformed."""


class OfficeProtocolVersionError(OfficeProtocolError):
    """Raised when worker and client protocol versions are incompatible."""


class OfficeWorkerError(OfficeError):
    """Raised when the Office worker reports a fatal error."""


class OfficeTimeoutError(OfficeError):
    """Raised when an Office operation exceeds its configured timeout."""


class OfficeCancelledError(OfficeError):
    """Raised when an Office operation is cancelled."""


class OfficePackageError(OfficeError):
    """Raised when an Office package is malformed or unsafe."""


class OfficeUnsupportedPackageError(OfficePackageError):
    """Raised when the package type or feature is not supported."""


class OfficeValidationError(OfficeError):
    """Raised when package validation fails."""


class OfficeReinsertionError(OfficeError):
    """Raised when translated content cannot be reinserted safely."""
