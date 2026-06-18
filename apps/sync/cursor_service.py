from datetime import datetime

from apps.sync.models import SyncCursor, SyncCursorSource


def get_or_create_cursor(source: SyncCursorSource) -> SyncCursor:
    return SyncCursor.objects.get_or_create(source=source.value)[0]


def advance_cursor(source: SyncCursorSource, last_modified_at: datetime) -> SyncCursor:
    cursor, _ = SyncCursor.objects.get_or_create(source=source.value)
    cursor.last_modified_at = last_modified_at
    cursor.save(update_fields=["last_modified_at", "updated_at"])
    return cursor
