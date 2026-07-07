import logging

import requests

try:
    import pyodbc
except ImportError:  # pragma: no cover - pyodbc is available in production, but keep import safe.
    pyodbc = None

from apps.prestashop.client import PrestashopError
from apps.sync.models import SyncErrorType

logger = logging.getLogger(__name__)

_TRANSIENT_PYODBC_SQLSTATE_PREFIXES = ("08", "HYT")
_TRANSIENT_PYODBC_MESSAGE_SNIPPETS = (
    "unable to connect",
    "adaptive server is unavailable",
    "login timeout expired",
    "communication link failure",
    "connection failed",
    "timeout expired",
    "exception set",
)


def classify_error(exc: Exception) -> str:
    if isinstance(exc, PrestashopError):
        if exc.status_code is not None:
            if exc.status_code == 429:
                return SyncErrorType.TRANSIENT
            if exc.status_code == 404:
                return SyncErrorType.VALIDATION
            if 400 <= exc.status_code < 500:
                return SyncErrorType.PERMANENT
            if exc.status_code >= 500:
                return SyncErrorType.TRANSIENT

    if isinstance(exc, requests.ConnectionError | requests.Timeout | OSError):
        return SyncErrorType.TRANSIENT

    if pyodbc is not None and isinstance(exc, pyodbc.Error):
        if _is_transient_pyodbc_error(exc):
            return SyncErrorType.TRANSIENT
        return SyncErrorType.PERMANENT

    return SyncErrorType.PERMANENT


def _is_transient_pyodbc_error(exc: Exception) -> bool:
    assert pyodbc is not None

    if isinstance(exc, pyodbc.OperationalError | pyodbc.InterfaceError):
        return True

    args = getattr(exc, "args", ())
    if args:
        first = args[0]
        if isinstance(first, str) and first.startswith(_TRANSIENT_PYODBC_SQLSTATE_PREFIXES):
            return True

    message = str(exc).lower()
    return any(snippet in message for snippet in _TRANSIENT_PYODBC_MESSAGE_SNIPPETS)
