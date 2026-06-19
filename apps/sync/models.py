from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class SyncCursorSource(models.TextChoices):
    PRODUCTS = "products", "Products"
    PRICES = "prices", "Prices"
    STOCK = "stock", "Stock"


class SyncJobType(models.TextChoices):
    IMPORT_PRODUCTS = "import_products", "Import products"
    IMPORT_PRICES = "import_prices", "Import prices"
    IMPORT_STOCK = "import_stock", "Import stock"
    EXPORT_MANUFACTURER = "export_manufacturer", "Export manufacturer"
    EXPORT_PRODUCT = "export_product", "Export product"
    EXPORT_PRICE = "export_price", "Export price"
    EXPORT_STOCK = "export_stock", "Export stock"


class SyncJobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


class SyncCursor(TimeStampedModel):
    source = models.CharField(max_length=32, choices=SyncCursorSource.choices, unique=True)
    last_modified_at = models.DateTimeField(blank=True, null=True)
    last_source_key = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ["source"]

    def __str__(self) -> str:
        return self.source


class SyncJob(TimeStampedModel):
    job_type = models.CharField(max_length=32, choices=SyncJobType.choices)
    entity_type = models.CharField(max_length=32)
    entity_key = models.CharField(max_length=128)
    status = models.CharField(
        max_length=16,
        choices=SyncJobStatus.choices,
        default=SyncJobStatus.PENDING,
    )
    payload = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["status", "available_at", "created_at"]

    def __str__(self) -> str:
        return f"{self.job_type} [{self.entity_type}:{self.entity_key}]"
