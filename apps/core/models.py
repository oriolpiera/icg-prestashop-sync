from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SyncTrackedModel(TimeStampedModel):
    sync_required = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(blank=True, null=True)
    last_sync_error = models.TextField(blank=True)

    class Meta:
        abstract = True
