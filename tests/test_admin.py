import json
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.contrib import admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django.utils import timezone

from apps.catalog.models import (
    Category,
    Combination,
    Manufacturer,
    Price,
    Product,
    Stock,
)
from apps.operations.admin import (
    FailedSyncFilter,
    ProductAdmin,
    StuckJobFilter,
    _sync_error_display,
    mark_for_resync,
    retry_entity_sync,
    retry_jobs,
)
from apps.sync.models import SyncJob, SyncJobStatus, SyncJobType
from apps.sync.tasks import retry_entity


@pytest.fixture(autouse=True)
def _clean_db():
    SyncJob.objects.all().delete()
    Product.objects.all().delete()
    Manufacturer.objects.all().delete()
    Category.objects.all().delete()


def _make_product(**overrides):
    icg_id = overrides.pop("icg_id", 1001)
    manufacturer = overrides.pop(
        "manufacturer",
        Manufacturer.objects.create(
            icg_code=f"M-{icg_id}",
            name=f"Manufacturer {icg_id}",
            prestashop_id=icg_id + 10,
        ),
    )
    return Product.objects.create(
        icg_id=icg_id,
        reference=overrides.pop("reference", "REF001"),
        name=overrides.pop("name", "Product One"),
        manufacturer=manufacturer,
        **overrides,
    )


def _make_combination(product, **overrides):
    return Combination.objects.create(
        product=product,
        icg_size=overrides.pop("icg_size", "M"),
        icg_color=overrides.pop("icg_color", "Red"),
        ean13=overrides.pop("ean13", "1234567890123"),
        active=overrides.pop("active", True),
    )


def _make_price(combination, **overrides):
    return Price.objects.create(
        combination=combination,
        amount_ex_vat=overrides.pop("amount_ex_vat", "29.99"),
        vat_rate=overrides.pop("vat_rate", "21.00"),
        currency=overrides.pop("currency", "EUR"),
    )


def _make_stock(combination, **overrides):
    return Stock.objects.create(
        combination=combination,
        warehouse_code=overrides.pop("warehouse_code", "WH01"),
        quantity=overrides.pop("quantity", 10),
    )


def _make_job(**overrides):
    return SyncJob.objects.create(
        job_type=overrides.pop("job_type", SyncJobType.EXPORT_PRODUCT),
        entity_type=overrides.pop("entity_type", "product"),
        entity_key=overrides.pop("entity_key", "REF001"),
        status=overrides.pop("status", SyncJobStatus.FAILED),
        last_error=overrides.pop("last_error", '{"message": "boom", "status_code": 500}'),
        started_at=overrides.pop("started_at", timezone.now()),
        **overrides,
    )


def _request_with_messages():
    factory = RequestFactory()
    request = factory.get("/admin/")
    request.session = "session"
    request._messages = FallbackStorage(request)
    return request


# --- retry_entity task ---


@pytest.mark.django_db
class TestRetryEntityTask:
    def test_retry_dispatches_to_correct_export(self):
        product = _make_product()
        product.sync_required = True
        product.save(update_fields=["sync_required"])

        mock_export = Mock(return_value={"product_id": product.pk, "prestashop_id": 42})
        with patch.dict(
            "apps.sync.tasks._EXPORT_DISPATCH",
            {"product": (SyncJobType.EXPORT_PRODUCT, mock_export)},
        ):
            result = retry_entity("product", product.pk, product.reference)

        assert result["status"] == "succeeded"
        assert result["entity_type"] == "product"
        mock_export.assert_called_once_with(product.pk)

    def test_retry_records_sync_job_on_success(self):
        product = _make_product()

        mock_export = Mock(return_value={"product_id": product.pk, "prestashop_id": 42})
        with patch.dict(
            "apps.sync.tasks._EXPORT_DISPATCH",
            {"product": (SyncJobType.EXPORT_PRODUCT, mock_export)},
        ):
            retry_entity("product", product.pk, product.reference)

        job = SyncJob.objects.get(job_type=SyncJobType.EXPORT_PRODUCT)
        assert job.status == SyncJobStatus.SUCCEEDED
        assert job.entity_type == "product"
        assert job.entity_key == product.reference

    def test_retry_records_failed_job_on_exception(self):
        product = _make_product()

        mock_export = Mock(side_effect=Exception("API down"))
        with patch.dict(
            "apps.sync.tasks._EXPORT_DISPATCH",
            {"product": (SyncJobType.EXPORT_PRODUCT, mock_export)},
        ):
            result = retry_entity("product", product.pk, product.reference)

        assert result["status"] == "failed"
        job = SyncJob.objects.get(job_type=SyncJobType.EXPORT_PRODUCT)
        assert job.status == SyncJobStatus.FAILED
        assert "API down" in job.last_error

    def test_retry_rejects_unknown_entity_type(self):
        result = retry_entity("unknown", 999)
        assert result["status"] == "error"
        assert "Unknown entity_type" in result["detail"]


# --- Admin actions ---


@pytest.mark.django_db
class TestAdminActions:
    def test_mark_for_resync(self):
        product = _make_product()
        product.sync_required = False
        product.last_sync_error = '{"message": "old error"}'
        product.save(update_fields=["sync_required", "last_sync_error"])

        mark_for_resync(None, None, Product.objects.filter(pk=product.pk))

        product.refresh_from_db()
        assert product.sync_required is True
        assert product.last_sync_error == ""

    def test_retry_entity_sync_dispatches_celery(self):
        product = _make_product()
        product.sync_required = True
        product.save(update_fields=["sync_required"])

        request = _request_with_messages()
        model_admin = admin.site._registry[Product]

        with patch("apps.sync.tasks.retry_entity") as mock_retry:
            mock_retry.delay.return_value = None
            retry_entity_sync(model_admin, request, Product.objects.filter(pk=product.pk))

        mock_retry.delay.assert_called_once_with("product", product.pk, product.reference)

    def test_retry_entity_sync_dispatches_for_combination(self):
        product = _make_product()
        comb = _make_combination(product)

        request = _request_with_messages()
        model_admin = admin.site._registry[Combination]

        with patch("apps.sync.tasks.retry_entity") as mock_retry:
            mock_retry.delay.return_value = None
            retry_entity_sync(
                model_admin,
                request,
                Combination.objects.filter(pk=comb.pk),
            )

        mock_retry.delay.assert_called_once_with(
            "combination", comb.pk, f"{product.reference}/M/Red"
        )

    def test_retry_jobs_resets_failed_to_pending(self):
        job = _make_job(status=SyncJobStatus.FAILED)

        request = _request_with_messages()
        model_admin = admin.site._registry[SyncJob]

        retry_jobs(model_admin, request, SyncJob.objects.filter(pk=job.pk))

        job.refresh_from_db()
        assert job.status == SyncJobStatus.PENDING
        assert job.last_error == ""


# --- Custom filters ---


@pytest.mark.django_db
class TestSyncFilters:
    def test_failed_sync_filter_yes(self):
        p1 = _make_product(icg_id=2001, reference="FAIL")
        p1.last_sync_error = '{"message": "boom"}'
        p1.save(update_fields=["last_sync_error"])

        p2 = _make_product(icg_id=2002, reference="OK")

        model_admin = ProductAdmin(Product, admin.site)
        request = RequestFactory().get("/admin/")

        f = FailedSyncFilter(request, {"has_error": ["yes"]}, Product, model_admin)
        result = f.queryset(request, Product.objects.all())

        assert p1 in result
        assert p2 not in result

    def test_failed_sync_filter_no(self):
        p1 = _make_product(icg_id=3001, reference="FAIL")
        p1.last_sync_error = '{"message": "boom"}'
        p1.save(update_fields=["last_sync_error"])

        p2 = _make_product(icg_id=3002, reference="OK")

        model_admin = ProductAdmin(Product, admin.site)
        request = RequestFactory().get("/admin/")

        f = FailedSyncFilter(request, {"has_error": ["no"]}, Product, model_admin)
        result = f.queryset(request, Product.objects.all())

        assert p2 in result
        assert p1 not in result

    def test_stuck_job_filter(self):
        recent_job = _make_job(
            entity_key="recent",
            status=SyncJobStatus.RUNNING,
            started_at=timezone.now() - timedelta(minutes=5),
        )
        stuck_job = _make_job(
            entity_key="stuck",
            status=SyncJobStatus.RUNNING,
            started_at=timezone.now() - timedelta(minutes=60),
        )

        request = RequestFactory().get("/admin/")

        stuck_admin = admin.site._registry[SyncJob]
        f = StuckJobFilter(request, {"is_stuck": ["yes"]}, SyncJob, stuck_admin)
        result = f.queryset(request, SyncJob.objects.all())

        assert stuck_job in result
        assert recent_job not in result


# --- Display helpers ---


@pytest.mark.django_db
class TestSyncErrorDisplay:
    def test_shows_message_from_json(self):
        product = _make_product()
        product.last_sync_error = '{"message": "HTTP 500 error", "status_code": 500}'
        product.save(update_fields=["last_sync_error"])

        assert _sync_error_display(product) == "HTTP 500 error"

    def test_returns_dash_when_empty(self):
        product = _make_product()
        assert _sync_error_display(product) == "-"

    def test_truncates_long_messages(self):
        product = _make_product()
        product.last_sync_error = json.dumps({"message": "x" * 200})
        product.save(update_fields=["last_sync_error"])

        result = _sync_error_display(product)
        assert len(result) == 80

    def test_handles_invalid_json(self):
        product = _make_product()
        product.last_sync_error = "not-json"
        product.save(update_fields=["last_sync_error"])

        assert _sync_error_display(product) == "not-json"
