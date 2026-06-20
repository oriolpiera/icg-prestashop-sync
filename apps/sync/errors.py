import logging

import requests

from apps.prestashop.client import PrestashopError
from apps.sync.models import SyncErrorType

logger = logging.getLogger(__name__)


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

    return SyncErrorType.PERMANENT
