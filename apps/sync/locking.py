from __future__ import annotations

import logging
import socket
from collections.abc import Generator
from contextlib import contextmanager
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.sync.models import SyncLock

logger = logging.getLogger(__name__)

LOCK_TIMEOUT_MINUTES = 5


class LockAcquisitionError(Exception):
    pass


@contextmanager
def sync_lock(
    lock_key: str,
    timeout_minutes: int = LOCK_TIMEOUT_MINUTES,
) -> Generator[SyncLock, None, None]:
    lock = _acquire_lock(lock_key, timeout_minutes)
    if lock is None:
        raise LockAcquisitionError(f"Cannot acquire lock '{lock_key}': another worker holds it.")
    try:
        yield lock
    finally:
        SyncLock.objects.filter(lock_key=lock_key).delete()
        logger.debug("Released lock '%s'", lock_key)


def _acquire_lock(lock_key: str, timeout_minutes: int) -> SyncLock | None:
    with transaction.atomic():
        lock = (
            SyncLock.objects.select_for_update(skip_locked=True).filter(lock_key=lock_key).first()
        )

        if lock is not None:
            if lock.locked_at < timezone.now() - timedelta(minutes=timeout_minutes):
                previous_owner = lock.locked_by
                lock.locked_by = _owner_id()
                lock.locked_at = timezone.now()
                lock.save(update_fields=["locked_by", "locked_at", "updated_at"])
                logger.info(
                    "Acquired stale lock '%s' from %s",
                    lock_key,
                    previous_owner,
                )
                return lock
            return None

        try:
            return SyncLock.objects.create(
                lock_key=lock_key,
                locked_by=_owner_id(),
                locked_at=timezone.now(),
            )
        except IntegrityError:
            return None


def _owner_id() -> str:
    return f"{socket.gethostname()}-{__import__('os').getpid()}"
