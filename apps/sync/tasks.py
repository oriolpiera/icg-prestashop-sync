import logging
from datetime import UTC

from celery import shared_task
from django.utils import timezone

from apps.catalog.models import Combination, Manufacturer, Product
from apps.icg.importer import import_prices as run_import_prices
from apps.icg.importer import import_products as run_import_products
from apps.icg.importer import import_stock as run_import_stock
from apps.prestashop.services import (
    export_combination,
    export_manufacturer,
    export_product,
    format_sync_error,
)
from apps.sync.models import SyncJob, SyncJobStatus, SyncJobType

logger = logging.getLogger(__name__)


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
    logger.info("Celery task: export_manufacturers")
    processed = 0
    failed = 0

    for manufacturer in Manufacturer.objects.filter(sync_required=True).order_by("pk"):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_MANUFACTURER,
            entity_type="manufacturer",
            entity_key=manufacturer.icg_code,
            status=SyncJobStatus.RUNNING,
            attempts=1,
            started_at=timezone.now(),
            payload={"manufacturer_id": manufacturer.pk, "icg_code": manufacturer.icg_code},
        )

        try:
            result = export_manufacturer(manufacturer.pk)
        except Exception as exc:
            failed += 1
            error = format_sync_error(exc)
            job.status = SyncJobStatus.FAILED
            job.last_error = error
        else:
            processed += 1
            job.status = SyncJobStatus.SUCCEEDED
            job.payload = {**job.payload, **result}

        job.finished_at = timezone.now().astimezone(UTC)
        job.save(update_fields=["status", "payload", "last_error", "finished_at", "updated_at"])

    return {
        "status": "success",
        "processed": processed,
        "failed": failed,
    }


@shared_task
def export_products() -> dict:
    logger.info("Celery task: export_products")
    processed = 0
    failed = 0

    for product in Product.objects.filter(sync_required=True).order_by("pk"):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key=product.reference,
            status=SyncJobStatus.RUNNING,
            attempts=1,
            started_at=timezone.now(),
            payload={
                "product_id": product.pk,
                "icg_id": product.icg_id,
                "reference": product.reference,
            },
        )

        try:
            result = export_product(product.pk)
        except Exception as exc:
            failed += 1
            error = format_sync_error(exc)
            job.status = SyncJobStatus.FAILED
            job.last_error = error
        else:
            processed += 1
            job.status = SyncJobStatus.SUCCEEDED
            job.payload = {**job.payload, **result}

        job.finished_at = timezone.now().astimezone(UTC)
        job.save(update_fields=["status", "payload", "last_error", "finished_at", "updated_at"])

    return {
        "status": "success",
        "processed": processed,
        "failed": failed,
    }


@shared_task
def export_combinations() -> dict:
    logger.info("Celery task: export_combinations")
    processed = 0
    failed = 0

    for combination in Combination.objects.filter(sync_required=True).order_by("pk"):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_COMBINATION,
            entity_type="combination",
            entity_key=f"{combination.product.reference}/{combination.icg_size}/{combination.icg_color}",
            status=SyncJobStatus.RUNNING,
            attempts=1,
            started_at=timezone.now(),
            payload={
                "combination_id": combination.pk,
                "product_reference": combination.product.reference,
                "icg_size": combination.icg_size,
                "icg_color": combination.icg_color,
            },
        )

        try:
            result = export_combination(combination.pk)
        except Exception as exc:
            failed += 1
            error = format_sync_error(exc)
            job.status = SyncJobStatus.FAILED
            job.last_error = error
        else:
            processed += 1
            job.status = SyncJobStatus.SUCCEEDED
            job.payload = {**job.payload, **result}

        job.finished_at = timezone.now().astimezone(UTC)
        job.save(update_fields=["status", "payload", "last_error", "finished_at", "updated_at"])

    return {
        "status": "success",
        "processed": processed,
        "failed": failed,
    }
