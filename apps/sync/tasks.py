import logging
from collections.abc import Callable
from datetime import UTC
from typing import Any

from celery import shared_task
from django.db import models
from django.utils import timezone

from apps.catalog.models import Category, Combination, Manufacturer, Price, Product, Stock
from apps.icg.importer import import_prices as run_import_prices
from apps.icg.importer import import_products as run_import_products
from apps.icg.importer import import_stock as run_import_stock
from apps.prestashop.services import (
    export_category,
    export_combination,
    export_discount,
    export_manufacturer,
    export_price,
    export_product,
    export_stock,
    format_sync_error,
)
from apps.sync.errors import classify_error
from apps.sync.locking import LockAcquisitionError, sync_lock
from apps.sync.models import (
    MAX_SYNC_RETRIES,
    SyncError,
    SyncErrorType,
    SyncJob,
    SyncJobStatus,
    SyncJobType,
)

logger = logging.getLogger(__name__)


def _record_sync_error(
    job: SyncJob,
    exc: Exception,
    *,
    error_type: str | None = None,
) -> str:
    if error_type is None:
        error_type = classify_error(exc)
    error_message = format_sync_error(exc)
    SyncError.objects.create(
        job=job,
        entity_type=job.entity_type,
        entity_key=job.entity_key,
        error_type=error_type,
        message=str(exc),
        details=job.payload,
    )
    job.status = SyncJobStatus.FAILED
    job.last_error = error_message
    job.finished_at = timezone.now().astimezone(UTC)

    if error_type == SyncErrorType.TRANSIENT and job.attempts < MAX_SYNC_RETRIES:
        job.schedule_retry()
    else:
        job.save(
            update_fields=[
                "status",
                "last_error",
                "finished_at",
                "updated_at",
            ]
        )

    return error_message


def _run_export_batch(
    task_name: str,
    queryset: models.QuerySet,
    job_type: str,
    entity_type: str,
    entity_key_fn: Callable[..., str],
    export_fn: Callable[..., dict[str, Any]],
    payload_fn: Callable[..., dict[str, Any]],
    lock_key: str,
) -> dict:
    logger.info("Celery task: %s", task_name)
    processed = 0
    failed = 0

    try:
        with sync_lock(lock_key):
            for entity in queryset:
                key = entity_key_fn(entity)
                job = SyncJob.objects.create(
                    job_type=job_type,
                    entity_type=entity_type,
                    entity_key=key,
                    status=SyncJobStatus.RUNNING,
                    attempts=1,
                    started_at=timezone.now(),
                    payload=payload_fn(entity),
                )

                try:
                    result = export_fn(entity.pk)
                except Exception as exc:
                    failed += 1
                    _record_sync_error(job, exc)
                else:
                    processed += 1
                    job.status = SyncJobStatus.SUCCEEDED
                    job.payload = {**job.payload, **result}
                    job.finished_at = timezone.now().astimezone(UTC)
                    job.save(
                        update_fields=[
                            "status",
                            "payload",
                            "finished_at",
                            "updated_at",
                        ]
                    )
    except LockAcquisitionError:
        logger.warning("Skipping %s: lock already held", task_name)
        return {"status": "skipped", "reason": "lock_held"}

    return {
        "status": "success",
        "processed": processed,
        "failed": failed,
    }


@shared_task
def import_products() -> dict:
    logger.info("Celery task: import_products")
    try:
        result = run_import_products()
        logger.info("import_products completed: %s", result)
        return result
    except Exception:
        logger.exception("import_products failed")
        return {"status": "error", "detail": "See worker logs for details."}


@shared_task
def import_prices() -> dict:
    logger.info("Celery task: import_prices")
    try:
        result = run_import_prices()
        logger.info("import_prices completed: %s", result)
        return result
    except Exception:
        logger.exception("import_prices failed")
        return {"status": "error", "detail": "See worker logs for details."}


@shared_task
def import_stock() -> dict:
    logger.info("Celery task: import_stock")
    try:
        result = run_import_stock()
        logger.info("import_stock completed: %s", result)
        return result
    except Exception:
        logger.exception("import_stock failed")
        return {"status": "error", "detail": "See worker logs for details."}


@shared_task
def export_manufacturers() -> dict:
    return _run_export_batch(
        task_name="export_manufacturers",
        queryset=Manufacturer.objects.filter(sync_required=True).order_by("pk"),
        job_type=SyncJobType.EXPORT_MANUFACTURER,
        entity_type="manufacturer",
        entity_key_fn=lambda m: m.icg_code,
        export_fn=export_manufacturer,
        payload_fn=lambda m: {"entity_id": m.pk, "manufacturer_id": m.pk, "icg_code": m.icg_code},
        lock_key="export_manufacturers",
    )


@shared_task
def export_categories() -> dict:
    return _run_export_batch(
        task_name="export_categories",
        queryset=Category.objects.filter(
            models.Q(sync_required=True, active=True)
            | models.Q(active=False, prestashop_id__isnull=False)
        ).order_by("position", "pk"),
        job_type=SyncJobType.EXPORT_CATEGORY,
        entity_type="category",
        entity_key_fn=lambda c: c.name,
        export_fn=export_category,
        payload_fn=lambda c: {
            "entity_id": c.pk,
            "category_id": c.pk,
            "prestashop_id": c.prestashop_id,
            "name": c.name,
        },
        lock_key="export_categories",
    )


@shared_task
def export_products() -> dict:
    return _run_export_batch(
        task_name="export_products",
        queryset=Product.objects.filter(sync_required=True).order_by("pk"),
        job_type=SyncJobType.EXPORT_PRODUCT,
        entity_type="product",
        entity_key_fn=lambda p: p.reference,
        export_fn=export_product,
        payload_fn=lambda p: {
            "entity_id": p.pk,
            "product_id": p.pk,
            "icg_id": p.icg_id,
            "reference": p.reference,
        },
        lock_key="export_products",
    )


@shared_task
def export_combinations() -> dict:
    return _run_export_batch(
        task_name="export_combinations",
        queryset=Combination.objects.select_related("product")
        .filter(sync_required=True)
        .order_by("pk"),
        job_type=SyncJobType.EXPORT_COMBINATION,
        entity_type="combination",
        entity_key_fn=lambda c: f"{c.product.reference}/{c.icg_size}/{c.icg_color}",
        export_fn=export_combination,
        payload_fn=lambda c: {
            "entity_id": c.pk,
            "combination_id": c.pk,
            "product_reference": c.product.reference,
            "icg_size": c.icg_size,
            "icg_color": c.icg_color,
        },
        lock_key="export_combinations",
    )


@shared_task
def export_prices() -> dict:
    return _run_export_batch(
        task_name="export_prices",
        queryset=Price.objects.select_related("combination__product")
        .filter(sync_required=True)
        .order_by("pk"),
        job_type=SyncJobType.EXPORT_PRICE,
        entity_type="price",
        entity_key_fn=lambda p: (
            f"{p.combination.product.reference}/{p.combination.icg_size}/"
            f"{p.combination.icg_color}"
        ),
        export_fn=export_price,
        payload_fn=lambda p: {
            "entity_id": p.pk,
            "price_id": p.pk,
            "combination_id": p.combination_id,
            "product_reference": p.combination.product.reference,
            "amount_ex_vat": str(p.amount_ex_vat),
            "vat_rate": str(p.vat_rate),
        },
        lock_key="export_prices",
    )


@shared_task
def export_stocks() -> dict:
    return _run_export_batch(
        task_name="export_stocks",
        queryset=Stock.objects.select_related("combination__product")
        .filter(sync_required=True)
        .order_by("pk"),
        job_type=SyncJobType.EXPORT_STOCK,
        entity_type="stock",
        entity_key_fn=lambda s: (
            f"{s.combination.product.reference}/{s.combination.icg_size}/"
            f"{s.combination.icg_color}"
        ),
        export_fn=export_stock,
        payload_fn=lambda s: {
            "entity_id": s.pk,
            "stock_id": s.pk,
            "combination_id": s.combination_id,
            "product_reference": s.combination.product.reference,
        },
        lock_key="export_stocks",
    )


@shared_task
def export_discounts() -> dict:
    return _run_export_batch(
        task_name="export_discounts",
        queryset=Product.objects.filter(discount_sync_required=True).order_by("pk"),
        job_type=SyncJobType.EXPORT_DISCOUNT,
        entity_type="discount",
        entity_key_fn=lambda p: p.reference,
        export_fn=export_discount,
        payload_fn=lambda p: {
            "entity_id": p.pk,
            "product_id": p.pk,
            "reference": p.reference,
            "discount_percent": str(p.discount_percent),
        },
        lock_key="export_discounts",
    )


_EXPORT_DISPATCH = {
    "manufacturer": (SyncJobType.EXPORT_MANUFACTURER, export_manufacturer),
    "category": (SyncJobType.EXPORT_CATEGORY, export_category),
    "product": (SyncJobType.EXPORT_PRODUCT, export_product),
    "combination": (SyncJobType.EXPORT_COMBINATION, export_combination),
    "price": (SyncJobType.EXPORT_PRICE, export_price),
    "stock": (SyncJobType.EXPORT_STOCK, export_stock),
    "discount": (SyncJobType.EXPORT_DISCOUNT, export_discount),
}


@shared_task
def retry_entity(entity_type: str, entity_id: int, entity_key: str = "") -> dict:
    entry = _EXPORT_DISPATCH.get(entity_type)
    if entry is None:
        return {"status": "error", "detail": f"Unknown entity_type: {entity_type}"}

    job_type, export_fn = entry
    job = SyncJob.objects.create(
        job_type=job_type,
        entity_type=entity_type,
        entity_key=entity_key or str(entity_id),
        status=SyncJobStatus.RUNNING,
        attempts=1,
        started_at=timezone.now(),
        payload={"entity_id": entity_id, "entity_type": entity_type},
    )

    try:
        result = export_fn(entity_id)
    except Exception as exc:
        error = _record_sync_error(job, exc)
        return {
            "status": "failed",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "error": error,
        }

    job.status = SyncJobStatus.SUCCEEDED
    job.payload = {**job.payload, **result}
    job.finished_at = timezone.now().astimezone(UTC)
    job.save(update_fields=["status", "payload", "finished_at", "updated_at"])
    return {"status": "succeeded", "entity_type": entity_type, "entity_id": entity_id, **result}


_RETRYABLE_EXPORT_MAP = {
    SyncJobType.EXPORT_MANUFACTURER: export_manufacturer,
    SyncJobType.EXPORT_CATEGORY: export_category,
    SyncJobType.EXPORT_PRODUCT: export_product,
    SyncJobType.EXPORT_COMBINATION: export_combination,
    SyncJobType.EXPORT_PRICE: export_price,
    SyncJobType.EXPORT_STOCK: export_stock,
    SyncJobType.EXPORT_DISCOUNT: export_discount,
}


@shared_task
def retry_failed_jobs() -> dict:
    logger.info("Celery task: retry_failed_jobs")
    retried = 0
    skipped = 0

    try:
        with sync_lock("retry_failed_jobs"):
            retryable_jobs = SyncJob.objects.filter(
                status=SyncJobStatus.FAILED,
                available_at__lte=timezone.now(),
            ).order_by("available_at")

            for job in retryable_jobs:
                latest_error_type = job.error_type
                if latest_error_type != SyncErrorType.TRANSIENT or job.attempts >= MAX_SYNC_RETRIES:
                    skipped += 1
                    continue

                export_fn = _RETRYABLE_EXPORT_MAP.get(job.job_type)
                if export_fn is None:
                    skipped += 1
                    continue

                entity_id = job.payload.get("entity_id")
                if entity_id is None:
                    skipped += 1
                    continue

                job.status = SyncJobStatus.RUNNING
                job.started_at = timezone.now()
                job.save(update_fields=["status", "started_at", "updated_at"])

                try:
                    result = export_fn(entity_id)
                except Exception as exc:
                    _record_sync_error(job, exc)
                else:
                    job.status = SyncJobStatus.SUCCEEDED
                    job.payload = {**job.payload, **result}
                    job.finished_at = timezone.now().astimezone(UTC)
                    job.save(
                        update_fields=[
                            "status",
                            "payload",
                            "finished_at",
                            "updated_at",
                        ]
                    )
                    retried += 1

    except LockAcquisitionError:
        logger.warning("Skipping retry_failed_jobs: lock already held")
        return {"status": "skipped", "reason": "lock_held"}

    return {
        "status": "success",
        "retried": retried,
        "skipped": skipped,
    }
