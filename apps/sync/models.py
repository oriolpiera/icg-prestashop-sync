from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel

MAX_SYNC_RETRIES = 2
BACKOFF_SCHEDULE_SECONDS = [300, 1800]


class SyncCursorSource(models.TextChoices):
    PRODUCTS = "products", "Products"
    PRICES = "prices", "Prices"
    STOCK = "stock", "Stock"
    CUSTOMERS = "customers", "Customers"


class SyncJobType(models.TextChoices):
    IMPORT_PRODUCTS = "import_products", "Import products"
    IMPORT_PRICES = "import_prices", "Import prices"
    IMPORT_STOCK = "import_stock", "Import stock"
    EXPORT_CUSTOMER = "export_customer", "Export customer"
    EXPORT_MANUFACTURER = "export_manufacturer", "Export manufacturer"
    EXPORT_CATEGORY = "export_category", "Export category"
    EXPORT_PRODUCT = "export_product", "Export product"
    EXPORT_COMBINATION = "export_combination", "Export combination"
    EXPORT_PRICE = "export_price", "Export price"
    EXPORT_STOCK = "export_stock", "Export stock"
    EXPORT_DISCOUNT = "export_discount", "Export discount"


class SyncJobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


class SyncErrorType(models.TextChoices):
    TRANSIENT = "transient", "Transient (retryable)"
    PERMANENT = "permanent", "Permanent"
    VALIDATION = "validation", "Validation"


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

    @property
    def is_retryable(self) -> bool:
        return (
            self.status == SyncJobStatus.PENDING
            and self.attempts < MAX_SYNC_RETRIES
            and self.error_type == SyncErrorType.TRANSIENT
        )

    @property
    def error_type(self) -> str | None:
        cache = getattr(self, "_prefetched_objects_cache", None)
        if cache and "errors" in cache:
            errors = cache["errors"]
            if errors:
                return max(errors, key=lambda e: e.created_at).error_type
            return None
        last = self.errors.order_by("-created_at").first()
        return last.error_type if last else None

    def schedule_retry(self) -> None:
        from datetime import timedelta

        self.attempts += 1
        delay_index = min(self.attempts - 2, len(BACKOFF_SCHEDULE_SECONDS) - 1)
        delay = BACKOFF_SCHEDULE_SECONDS[delay_index]
        self.available_at = timezone.now() + timedelta(seconds=delay)
        self.status = SyncJobStatus.PENDING
        self.last_error = ""
        self.save(
            update_fields=[
                "attempts",
                "available_at",
                "status",
                "last_error",
                "finished_at",
                "updated_at",
            ]
        )


class SyncError(TimeStampedModel):
    job = models.ForeignKey(SyncJob, on_delete=models.CASCADE, related_name="errors")
    entity_type = models.CharField(max_length=32)
    entity_key = models.CharField(max_length=128)
    error_type = models.CharField(
        max_length=16,
        choices=SyncErrorType.choices,
    )
    message = models.TextField()
    details = models.JSONField(default=dict, blank=True)
    resolved = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.error_type} on {self.entity_type}:{self.entity_key}"


class SyncLock(TimeStampedModel):
    lock_key = models.CharField(max_length=64, unique=True)
    locked_by = models.CharField(max_length=128, blank=True)
    locked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["lock_key"]

    def __str__(self) -> str:
        return f"Lock: {self.lock_key} ({self.locked_by})"
