import logging
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, timedelta
from typing import Any

from celery import shared_task
from django.db import models, transaction
from django.utils import timezone

from apps.catalog.models import Category, Combination, Manufacturer, Price, Product, Stock
from apps.icg.importer import import_prices as run_import_prices
from apps.icg.importer import import_products as run_import_products
from apps.icg.importer import import_stock as run_import_stock
from apps.icg.services import ICGClientesWebWriter, ICGFacturasWebWriter
from apps.prestashop.client import PrestashopClient
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
from apps.sales.models import PrestashopCustomer, PrestashopOrder
from apps.sales.services import (
    export_customer_to_icg_from_mirror,
    export_order_to_icg_from_mirror,
    refresh_customer_from_prestashop,
    refresh_order_from_prestashop,
)
from apps.sync.cursor_service import advance_cursor, get_or_create_cursor
from apps.sync.errors import classify_error
from apps.sync.locking import LockAcquisitionError, sync_lock
from apps.sync.models import (
    BACKOFF_SCHEDULE_SECONDS,
    MAX_SYNC_RETRIES,
    SyncCursorSource,
    SyncError,
    SyncErrorType,
    SyncJob,
    SyncJobStatus,
    SyncJobType,
)

logger = logging.getLogger(__name__)
STALE_RUNNING_JOB_TIMEOUT = timedelta(minutes=30)
ICG_SALES_EXPORT_LOCK_KEY = "icg_sales_export"
ICG_SALES_EXPORT_ENTITY_TYPES = {"prestashop_customer", "prestashop_order"}
ICG_SALES_EXPORT_JOB_TYPES = {SyncJobType.EXPORT_CUSTOMER, SyncJobType.EXPORT_ORDER}
ICG_SALES_EXPORT_LOCK_CONTENTION_KEY = "icg_sales_export_lock_contention_count"


def _stale_running_threshold():
    return timezone.now() - STALE_RUNNING_JOB_TIMEOUT


def _resolve_job_errors(job: SyncJob) -> None:
    job.errors.filter(resolved=False).update(resolved=True, updated_at=timezone.now())


@contextmanager
def _maybe_icg_sales_export_lock(
    *,
    entity_type: str | None = None,
    job_type: str | None = None,
):
    requires_lock = (
        entity_type in ICG_SALES_EXPORT_ENTITY_TYPES or job_type in ICG_SALES_EXPORT_JOB_TYPES
    )
    if not requires_lock:
        yield
        return

    with sync_lock(ICG_SALES_EXPORT_LOCK_KEY):
        yield


def _release_running_job_for_lock_contention(job: SyncJob) -> None:
    contention_count = int(job.payload.get(ICG_SALES_EXPORT_LOCK_CONTENTION_KEY, 0)) + 1
    delay_index = min(contention_count - 1, len(BACKOFF_SCHEDULE_SECONDS) - 1)
    delay = BACKOFF_SCHEDULE_SECONDS[delay_index]
    job.available_at = timezone.now() + timedelta(seconds=delay)
    job.status = SyncJobStatus.PENDING
    job.last_error = ""
    job.started_at = None
    job.finished_at = None
    job.payload = {**job.payload, ICG_SALES_EXPORT_LOCK_CONTENTION_KEY: contention_count}
    job.save(
        update_fields=[
            "available_at",
            "status",
            "last_error",
            "started_at",
            "finished_at",
            "payload",
            "updated_at",
        ]
    )


def _resolve_superseded_jobs(job: SyncJob, *, finished_at) -> None:
    superseded_jobs = (
        SyncJob.objects.filter(
            job_type=job.job_type,
            entity_type=job.entity_type,
            entity_key=job.entity_key,
        )
        .filter(
            models.Q(status__in=[SyncJobStatus.PENDING, SyncJobStatus.FAILED])
            | models.Q(
                status=SyncJobStatus.RUNNING,
                started_at__lt=_stale_running_threshold(),
            )
        )
        .exclude(pk=job.pk)
    )

    SyncError.objects.filter(job__in=superseded_jobs, resolved=False).update(
        resolved=True,
        updated_at=timezone.now(),
    )
    superseded_jobs.update(
        status=SyncJobStatus.SUCCEEDED,
        last_error="",
        finished_at=finished_at,
        updated_at=timezone.now(),
    )


def _mark_job_succeeded(job: SyncJob, result: dict[str, Any]) -> None:
    with transaction.atomic():
        finished_at = timezone.now().astimezone(UTC)
        job.status = SyncJobStatus.SUCCEEDED
        job.payload = {**job.payload, **result}
        job.last_error = ""
        job.finished_at = finished_at
        job.save(
            update_fields=[
                "status",
                "payload",
                "last_error",
                "finished_at",
                "updated_at",
            ]
        )
        _resolve_job_errors(job)
        _resolve_superseded_jobs(job, finished_at=finished_at)


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


def _record_lock_contention(job: SyncJob) -> None:
    message = f"ICG sales export lock '{ICG_SALES_EXPORT_LOCK_KEY}' is currently held"
    SyncError.objects.create(
        job=job,
        entity_type=job.entity_type,
        entity_key=job.entity_key,
        error_type=SyncErrorType.TRANSIENT,
        message=message,
        details=job.payload,
    )
    job.last_error = message
    job.save(update_fields=["last_error", "updated_at"])


def _has_open_job_conflict(job_type: str, entity_type: str, entity_key: str) -> bool:
    return (
        SyncJob.objects.filter(
            job_type=job_type,
            entity_type=entity_type,
            entity_key=entity_key,
        )
        .filter(
            models.Q(status=SyncJobStatus.PENDING)
            | models.Q(
                status=SyncJobStatus.RUNNING,
                started_at__gte=_stale_running_threshold(),
            )
        )
        .exists()
    )


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
                if _has_open_job_conflict(job_type, entity_type, key):
                    continue

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
                    _mark_job_succeeded(job, result)
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
def export_manufacturers(limit: int = 10) -> dict:
    return _run_export_batch(
        task_name="export_manufacturers",
        queryset=Manufacturer.objects.filter(sync_required=True).order_by("updated_at", "pk")[
            :limit
        ],
        job_type=SyncJobType.EXPORT_MANUFACTURER,
        entity_type="manufacturer",
        entity_key_fn=lambda m: m.icg_code,
        export_fn=export_manufacturer,
        payload_fn=lambda m: {"entity_id": m.pk, "manufacturer_id": m.pk, "icg_code": m.icg_code},
        lock_key="export_manufacturers",
    )


@shared_task
def export_categories(limit: int = 10) -> dict:
    return _run_export_batch(
        task_name="export_categories",
        queryset=Category.objects.filter(
            models.Q(sync_required=True, active=True)
            | models.Q(active=False, prestashop_id__isnull=False)
        ).order_by("position", "pk")[:limit],
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
def export_products(limit: int = 100) -> dict:
    Product.objects.filter(
        sync_required=True, discontinued=True, prestashop_id__isnull=True
    ).update(sync_required=False)

    Product.objects.filter(
        sync_required=True, visible_web=False, prestashop_id__isnull=True
    ).update(sync_required=False)

    return _run_export_batch(
        task_name="export_products",
        queryset=Product.objects.filter(sync_required=True)
        .filter(models.Q(discontinued=False) | models.Q(prestashop_id__isnull=False))
        .filter(models.Q(visible_web=True) | models.Q(prestashop_id__isnull=False))
        .order_by("updated_at", "pk")[:limit],
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
def export_combinations(limit: int = 1000) -> dict:
    Combination.objects.filter(
        sync_required=True,
        product__discontinued=True,
        product__prestashop_id__isnull=True,
    ).update(sync_required=False)

    Combination.objects.filter(
        sync_required=True,
        product__visible_web=False,
        product__prestashop_id__isnull=True,
    ).update(sync_required=False)

    return _run_export_batch(
        task_name="export_combinations",
        queryset=Combination.objects.select_related("product")
        .filter(sync_required=True)
        .filter(
            models.Q(product__discontinued=False) | models.Q(product__prestashop_id__isnull=False)
        )
        .filter(
            models.Q(product__visible_web=True) | models.Q(product__prestashop_id__isnull=False)
        )
        .order_by("updated_at", "pk")[:limit],
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
def export_prices(limit: int = 1000) -> dict:
    Price.objects.filter(
        sync_required=True,
        combination__active=False,
    ).update(sync_required=False)

    Price.objects.filter(
        sync_required=True,
        combination__product__discontinued=True,
        combination__product__prestashop_id__isnull=True,
    ).update(sync_required=False)

    Price.objects.filter(
        sync_required=True,
        combination__product__visible_web=False,
        combination__product__prestashop_id__isnull=True,
    ).update(sync_required=False)

    return _run_export_batch(
        task_name="export_prices",
        queryset=Price.objects.select_related("combination__product")
        .filter(sync_required=True, combination__active=True)
        .filter(
            models.Q(combination__product__discontinued=False)
            | models.Q(combination__product__prestashop_id__isnull=False)
        )
        .filter(
            models.Q(combination__product__visible_web=True)
            | models.Q(combination__product__prestashop_id__isnull=False)
        )
        .order_by("updated_at", "pk")[:limit],
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
def export_stocks(limit: int = 1000) -> dict:
    Stock.objects.filter(
        sync_required=True,
        combination__active=False,
    ).update(sync_required=False)

    Stock.objects.filter(
        sync_required=True,
        combination__product__discontinued=True,
        combination__product__prestashop_id__isnull=True,
    ).update(sync_required=False)

    Stock.objects.filter(
        sync_required=True,
        combination__product__visible_web=False,
        combination__product__prestashop_id__isnull=True,
    ).update(sync_required=False)

    return _run_export_batch(
        task_name="export_stocks",
        queryset=Stock.objects.select_related("combination__product")
        .filter(sync_required=True, combination__active=True)
        .filter(
            models.Q(combination__product__discontinued=False)
            | models.Q(combination__product__prestashop_id__isnull=False)
        )
        .filter(
            models.Q(combination__product__visible_web=True)
            | models.Q(combination__product__prestashop_id__isnull=False)
        )
        .order_by("updated_at", "pk")[:limit],
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
def export_discounts(limit: int = 1000) -> dict:
    Product.objects.filter(discount_sync_required=True, discontinued=True).update(
        discount_sync_required=False
    )

    Product.objects.filter(
        discount_sync_required=True, visible_web=False, prestashop_id__isnull=True
    ).update(discount_sync_required=False)

    return _run_export_batch(
        task_name="export_discounts",
        queryset=Product.objects.filter(
            discount_sync_required=True,
            discontinued=False,
            visible_web=True,
            prestashop_id__isnull=False,
        ).order_by("updated_at", "pk")[:limit],
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


@shared_task
def export_new_customers_to_icg(limit: int = 100) -> dict:
    logger.info("Celery task: export_new_customers_to_icg")
    cursor = get_or_create_cursor(SyncCursorSource.CUSTOMERS)
    last_customer_id = int(cursor.last_source_key or "0")
    client = PrestashopClient()
    writer = ICGClientesWebWriter()
    processed = 0
    inserted = 0
    failed = 0

    try:
        customers = client.list_customers_created_after(
            cursor.last_modified_at,
            last_customer_id,
            limit=limit,
        )
    except Exception:
        logger.exception("Failed to fetch new Prestashop customers")
        return {"status": "error", "detail": "See worker logs for details."}

    try:
        with sync_lock(ICG_SALES_EXPORT_LOCK_KEY):
            for customer in customers:
                job = SyncJob.objects.create(
                    job_type=SyncJobType.EXPORT_CUSTOMER,
                    entity_type="prestashop_customer",
                    entity_key=str(customer.customer_id),
                    status=SyncJobStatus.RUNNING,
                    attempts=1,
                    started_at=timezone.now(),
                    payload={
                        "entity_id": customer.customer_id,
                        "customer_id": customer.customer_id,
                        "email": customer.email,
                        "firstname": customer.firstname,
                        "lastname": customer.lastname,
                        "date_add": customer.date_add.isoformat(),
                    },
                )

                try:
                    refresh_customer_from_prestashop(customer.customer_id, client=client)
                    result = export_customer_to_icg_from_mirror(customer.customer_id, writer=writer)
                except Exception as exc:
                    failed += 1
                    _record_sync_error(job, exc)
                    if not PrestashopCustomer.objects.filter(
                        prestashop_id=customer.customer_id
                    ).exists():
                        logger.warning(
                            (
                                "Stopping customer export batch at Prestashop customer %s: "
                                "refresh failed before mirror creation"
                            ),
                            customer.customer_id,
                        )
                        break
                else:
                    processed += 1
                    if result.get("inserted"):
                        inserted += 1
                    _mark_job_succeeded(job, result)
                    advance_cursor(
                        SyncCursorSource.CUSTOMERS,
                        customer.date_add,
                        str(customer.customer_id),
                    )
                    continue

                advance_cursor(
                    SyncCursorSource.CUSTOMERS,
                    customer.date_add,
                    str(customer.customer_id),
                )
    except LockAcquisitionError:
        logger.warning("Skipping export_new_customers_to_icg: lock already held")
        return {"status": "skipped", "reason": "lock_held"}

    return {
        "status": "success",
        "processed": processed,
        "inserted": inserted,
        "failed": failed,
    }


@shared_task
def export_new_orders_to_icg(limit: int = 100) -> dict:
    logger.info("Celery task: export_new_orders_to_icg")
    cursor = get_or_create_cursor(SyncCursorSource.ORDERS)
    last_order_id = int(cursor.last_source_key or "0")
    client = PrestashopClient()
    writer = ICGFacturasWebWriter()
    processed = 0
    inserted_rows = 0
    failed = 0

    try:
        orders = client.list_orders_created_after(
            cursor.last_modified_at,
            last_order_id,
            limit=limit,
        )
    except Exception:
        logger.exception("Failed to fetch new Prestashop orders")
        return {"status": "error", "detail": "See worker logs for details."}

    try:
        with sync_lock(ICG_SALES_EXPORT_LOCK_KEY):
            for order in orders:
                job = SyncJob.objects.create(
                    job_type=SyncJobType.EXPORT_ORDER,
                    entity_type="prestashop_order",
                    entity_key=str(order.order_id),
                    status=SyncJobStatus.RUNNING,
                    attempts=1,
                    started_at=timezone.now(),
                    payload={
                        "entity_id": order.order_id,
                        "order_id": order.order_id,
                        "customer_id": order.customer_id,
                        "payment": order.payment,
                        "date_add": order.date_add.isoformat(),
                    },
                )

                try:
                    refresh_order_from_prestashop(order.order_id, client=client)
                    result = export_order_to_icg_from_mirror(order.order_id, writer=writer)
                except Exception as exc:
                    failed += 1
                    _record_sync_error(job, exc)
                    if not PrestashopOrder.objects.filter(prestashop_id=order.order_id).exists():
                        logger.warning(
                            (
                                "Stopping order export batch at Prestashop order %s: "
                                "refresh failed before mirror creation"
                            ),
                            order.order_id,
                        )
                        break
                else:
                    processed += 1
                    inserted_rows += int(result.get("inserted_rows", 0))
                    _mark_job_succeeded(job, result)
                    advance_cursor(
                        SyncCursorSource.ORDERS,
                        order.date_add,
                        str(order.order_id),
                    )
                    continue

                advance_cursor(
                    SyncCursorSource.ORDERS,
                    order.date_add,
                    str(order.order_id),
                )
    except LockAcquisitionError:
        logger.warning("Skipping export_new_orders_to_icg: lock already held")
        return {"status": "skipped", "reason": "lock_held"}

    return {
        "status": "success",
        "processed": processed,
        "inserted_rows": inserted_rows,
        "failed": failed,
    }


_EXPORT_DISPATCH = {
    "manufacturer": (SyncJobType.EXPORT_MANUFACTURER, export_manufacturer),
    "category": (SyncJobType.EXPORT_CATEGORY, export_category),
    "product": (SyncJobType.EXPORT_PRODUCT, export_product),
    "combination": (SyncJobType.EXPORT_COMBINATION, export_combination),
    "price": (SyncJobType.EXPORT_PRICE, export_price),
    "stock": (SyncJobType.EXPORT_STOCK, export_stock),
    "discount": (SyncJobType.EXPORT_DISCOUNT, export_discount),
    "prestashop_customer": (SyncJobType.EXPORT_CUSTOMER, export_customer_to_icg_from_mirror),
    "prestashop_order": (SyncJobType.EXPORT_ORDER, export_order_to_icg_from_mirror),
}


@shared_task
def retry_entity(entity_type: str, entity_id: int, entity_key: str = "") -> dict:
    entry = _EXPORT_DISPATCH.get(entity_type)
    if entry is None:
        return {"status": "error", "detail": f"Unknown entity_type: {entity_type}"}

    job_type, export_fn = entry
    resolved_entity_key = entity_key or str(entity_id)
    if _has_open_job_conflict(job_type, entity_type, resolved_entity_key):
        return {
            "status": "skipped",
            "reason": "job_already_open",
            "entity_type": entity_type,
            "entity_id": entity_id,
        }

    job = SyncJob.objects.create(
        job_type=job_type,
        entity_type=entity_type,
        entity_key=resolved_entity_key,
        status=SyncJobStatus.RUNNING,
        attempts=1,
        started_at=timezone.now(),
        payload={"entity_id": entity_id, "entity_type": entity_type},
    )

    try:
        with _maybe_icg_sales_export_lock(entity_type=entity_type, job_type=job_type):
            result = export_fn(entity_id)
    except LockAcquisitionError:
        _record_lock_contention(job)
        _release_running_job_for_lock_contention(job)
        return {
            "status": "skipped",
            "reason": "lock_held",
            "entity_type": entity_type,
            "entity_id": entity_id,
        }
    except Exception as exc:
        error = _record_sync_error(job, exc)
        return {
            "status": "failed",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "error": error,
        }

    _mark_job_succeeded(job, result)
    return {"status": "succeeded", "entity_type": entity_type, "entity_id": entity_id, **result}


_RETRYABLE_EXPORT_MAP = {
    SyncJobType.EXPORT_CUSTOMER: lambda entity_id: retry_customer_export_from_job(int(entity_id)),
    SyncJobType.EXPORT_ORDER: lambda entity_id: retry_order_export_from_job(int(entity_id)),
    SyncJobType.EXPORT_MANUFACTURER: export_manufacturer,
    SyncJobType.EXPORT_CATEGORY: export_category,
    SyncJobType.EXPORT_PRODUCT: export_product,
    SyncJobType.EXPORT_COMBINATION: export_combination,
    SyncJobType.EXPORT_PRICE: export_price,
    SyncJobType.EXPORT_STOCK: export_stock,
    SyncJobType.EXPORT_DISCOUNT: export_discount,
}


def retry_customer_export_from_job(entity_id: int) -> dict[str, int | bool]:
    if not PrestashopCustomer.objects.filter(prestashop_id=entity_id).exists():
        refresh_customer_from_prestashop(entity_id)
    return export_customer_to_icg_from_mirror(entity_id)


def retry_order_export_from_job(entity_id: int) -> dict[str, int]:
    if not PrestashopOrder.objects.filter(prestashop_id=entity_id).exists():
        refresh_order_from_prestashop(entity_id)
    return export_order_to_icg_from_mirror(entity_id)


@shared_task
def refresh_prestashop_customer(prestashop_customer_id: int) -> dict[str, int | str]:
    logger.info("Celery task: refresh_prestashop_customer")
    refresh_customer_from_prestashop(prestashop_customer_id)
    return {"status": "success", "customer_id": prestashop_customer_id}


@shared_task
def refresh_prestashop_order(prestashop_order_id: int) -> dict[str, int | str]:
    logger.info("Celery task: refresh_prestashop_order")
    refresh_order_from_prestashop(prestashop_order_id)
    return {"status": "success", "order_id": prestashop_order_id}


@shared_task
def retry_failed_jobs() -> dict:
    logger.info("Celery task: retry_failed_jobs")
    retried = 0
    skipped = 0

    try:
        with sync_lock("retry_failed_jobs"):
            retryable_jobs = (
                SyncJob.objects.filter(
                    status=SyncJobStatus.PENDING,
                    available_at__lte=timezone.now(),
                )
                .prefetch_related("errors")
                .order_by("available_at")
            )

            for job in retryable_jobs:
                latest_error_type = job.error_type
                if latest_error_type != SyncErrorType.TRANSIENT:
                    skipped += 1
                    continue

                if job.attempts > MAX_SYNC_RETRIES:
                    job.status = SyncJobStatus.FAILED
                    job.finished_at = timezone.now().astimezone(UTC)
                    job.save(update_fields=["status", "finished_at", "updated_at"])
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
                    with _maybe_icg_sales_export_lock(
                        entity_type=job.entity_type,
                        job_type=job.job_type,
                    ):
                        if ICG_SALES_EXPORT_LOCK_CONTENTION_KEY in job.payload:
                            payload = dict(job.payload)
                            payload.pop(ICG_SALES_EXPORT_LOCK_CONTENTION_KEY, None)
                            job.payload = payload
                            job.save(update_fields=["payload", "updated_at"])
                        result = export_fn(entity_id)
                except LockAcquisitionError:
                    _release_running_job_for_lock_contention(job)
                    skipped += 1
                except Exception as exc:
                    _record_sync_error(job, exc)
                else:
                    _mark_job_succeeded(job, result)
                    retried += 1

    except LockAcquisitionError:
        logger.warning("Skipping retry_failed_jobs: lock already held")
        return {"status": "skipped", "reason": "lock_held"}

    return {
        "status": "success",
        "retried": retried,
        "skipped": skipped,
    }
