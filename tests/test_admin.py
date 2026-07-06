import json
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.contrib import admin
from django.contrib.auth.models import User
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
    LastICGModifiedDatePresenceFilter,
    ProductAdmin,
    StuckJobFilter,
    _sync_error_display,
    export_sales_to_icg,
    mark_for_resync,
    refresh_sales_from_prestashop,
    retry_entity_sync,
    retry_jobs,
    set_sales_sync_cursor,
    update_from_icg,
)
from apps.operations.sites import admin_site
from apps.sales.models import PrestashopCustomer, PrestashopOrder
from apps.sync.models import SyncCursor, SyncCursorSource, SyncJob, SyncJobStatus, SyncJobType
from apps.sync.tasks import retry_entity


@pytest.fixture(autouse=True)
def _clean_db():
    SyncJob.objects.all().delete()
    PrestashopOrder.objects.all().delete()
    PrestashopCustomer.objects.all().delete()
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


def _make_sales_customer(**overrides):
    return PrestashopCustomer.objects.create(
        prestashop_id=overrides.pop("prestashop_id", 42),
        firstname=overrides.pop("firstname", "Ada"),
        lastname=overrides.pop("lastname", "Lovelace"),
        email=overrides.pop("email", "ada@example.com"),
        date_add=overrides.pop("date_add", timezone.now()),
        last_snapshot_at=overrides.pop("last_snapshot_at", timezone.now()),
        **overrides,
    )


def _make_sales_order(customer, **overrides):
    return PrestashopOrder.objects.create(
        prestashop_id=overrides.pop("prestashop_id", 77),
        customer=customer,
        payment=overrides.pop("payment", "Redsys Card"),
        date_add=overrides.pop("date_add", timezone.now()),
        total_paid_tax_incl=overrides.pop("total_paid_tax_incl", "100.00"),
        total_shipping_tax_incl=overrides.pop("total_shipping_tax_incl", "12.10"),
        total_shipping_tax_excl=overrides.pop("total_shipping_tax_excl", "10.00"),
        last_snapshot_at=overrides.pop("last_snapshot_at", timezone.now()),
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

    def test_retry_skips_when_job_already_open(self):
        product = _make_product()
        _make_job(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key=product.reference,
            status=SyncJobStatus.PENDING,
            attempts=2,
        )

        mock_export = Mock(return_value={"product_id": product.pk, "prestashop_id": 42})
        with patch.dict(
            "apps.sync.tasks._EXPORT_DISPATCH",
            {"product": (SyncJobType.EXPORT_PRODUCT, mock_export)},
        ):
            result = retry_entity("product", product.pk, product.reference)

        assert result["status"] == "skipped"
        assert result["reason"] == "job_already_open"
        mock_export.assert_not_called()


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
        model_admin = admin_site._registry[Product]

        with patch("apps.sync.tasks.retry_entity") as mock_retry:
            mock_retry.delay.return_value = None
            retry_entity_sync(model_admin, request, Product.objects.filter(pk=product.pk))

        mock_retry.delay.assert_called_once_with("product", product.pk, product.reference)

    def test_retry_entity_sync_dispatches_for_combination(self):
        product = _make_product()
        comb = _make_combination(product)

        request = _request_with_messages()
        model_admin = admin_site._registry[Combination]

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
        job = _make_job(
            status=SyncJobStatus.FAILED,
            attempts=2,
            available_at=timezone.now() - timedelta(hours=1),
        )

        request = _request_with_messages()
        model_admin = admin_site._registry[SyncJob]

        retry_jobs(model_admin, request, SyncJob.objects.filter(pk=job.pk))

        job.refresh_from_db()
        assert job.status == SyncJobStatus.PENDING
        assert job.last_error == ""
        assert job.attempts == 0
        assert job.available_at <= timezone.now()

    def test_retry_jobs_warns_when_no_failed_selected(self):
        job = _make_job(status=SyncJobStatus.SUCCEEDED, last_error="")

        request = _request_with_messages()
        model_admin = admin_site._registry[SyncJob]

        retry_jobs(model_admin, request, SyncJob.objects.filter(pk=job.pk))

        storage = request._messages
        msgs = list(storage)
        assert len(msgs) == 1
        assert "No failed jobs" in str(msgs[0])

    def test_update_from_icg_dispatches_for_product(self):
        product = _make_product()

        request = _request_with_messages()
        model_admin = admin_site._registry[Product]

        with patch("apps.icg.importer.refresh_product_from_icg") as mock_refresh:
            mock_refresh.return_value = {"status": "updated", "processed": 1, "skipped": 0}
            update_from_icg(model_admin, request, Product.objects.filter(pk=product.pk))

        mock_refresh.assert_called_once_with(product.pk)

    def test_update_from_icg_dispatches_for_combination(self):
        product = _make_product()
        combination = _make_combination(product)

        request = _request_with_messages()
        model_admin = admin_site._registry[Combination]

        with patch("apps.icg.importer.refresh_combination_from_icg") as mock_refresh:
            mock_refresh.return_value = {"status": "updated", "processed": 1, "skipped": 0}
            update_from_icg(model_admin, request, Combination.objects.filter(pk=combination.pk))

        mock_refresh.assert_called_once_with(combination.pk)

    def test_update_from_icg_reports_updated_skipped_and_failed_counts(self):
        product_one = _make_product(icg_id=7001, reference="REF7001")
        product_two = _make_product(icg_id=7002, reference="REF7002")
        product_three = _make_product(icg_id=7003, reference="REF7003")

        request = _request_with_messages()
        model_admin = admin_site._registry[Product]

        with patch("apps.icg.importer.refresh_product_from_icg") as mock_refresh:
            mock_refresh.side_effect = [
                {"status": "updated", "processed": 2, "skipped": 0},
                {"status": "skipped", "processed": 0, "skipped": 1},
                RuntimeError("ICG timeout"),
            ]
            update_from_icg(
                model_admin,
                request,
                Product.objects.filter(
                    pk__in=[product_one.pk, product_two.pk, product_three.pk]
                ).order_by("pk"),
            )

        msgs = list(request._messages)
        assert len(msgs) == 1
        assert str(msgs[0]) == "Updated 1 record(s) from ICG. Skipped 1. Failed 1."

    def test_refresh_sales_from_prestashop_dispatches_customer_task(self):
        customer = _make_sales_customer()
        request = _request_with_messages()
        model_admin = admin_site._registry[PrestashopCustomer]

        with patch("apps.sync.tasks.refresh_prestashop_customer") as mock_refresh:
            mock_refresh.delay.return_value = None
            refresh_sales_from_prestashop(
                model_admin,
                request,
                PrestashopCustomer.objects.filter(pk=customer.pk),
            )

        mock_refresh.delay.assert_called_once_with(customer.prestashop_id)

    def test_export_sales_to_icg_dispatches_order_retry(self):
        customer = _make_sales_customer()
        order = _make_sales_order(customer)
        request = _request_with_messages()
        model_admin = admin_site._registry[PrestashopOrder]

        with patch("apps.sync.tasks.retry_entity") as mock_retry:
            mock_retry.delay.return_value = None
            export_sales_to_icg(
                model_admin,
                request,
                PrestashopOrder.objects.filter(pk=order.pk),
            )

        mock_retry.delay.assert_called_once_with(
            "prestashop_order", order.prestashop_id, str(order.prestashop_id)
        )

    def test_set_sales_sync_cursor_for_customer_uses_selected_record(self):
        first = _make_sales_customer(
            prestashop_id=40,
            date_add=timezone.now() - timedelta(days=2),
            last_snapshot_at=timezone.now(),
        )
        second = _make_sales_customer(
            prestashop_id=42,
            date_add=timezone.now() - timedelta(days=1),
            last_snapshot_at=timezone.now(),
        )
        request = _request_with_messages()
        model_admin = admin_site._registry[PrestashopCustomer]

        set_sales_sync_cursor(
            model_admin,
            request,
            PrestashopCustomer.objects.filter(pk__in=[first.pk, second.pk]),
        )

        cursor = SyncCursor.objects.get(source=SyncCursorSource.CUSTOMERS)
        assert cursor.last_source_key == "42"
        assert cursor.last_modified_at == second.date_add

    def test_set_sales_sync_cursor_for_order_uses_selected_record(self):
        customer = _make_sales_customer()
        first = _make_sales_order(
            customer,
            prestashop_id=70,
            date_add=timezone.now() - timedelta(days=2),
            last_snapshot_at=timezone.now(),
        )
        second = _make_sales_order(
            customer,
            prestashop_id=77,
            date_add=timezone.now() - timedelta(days=1),
            last_snapshot_at=timezone.now(),
        )
        request = _request_with_messages()
        model_admin = admin_site._registry[PrestashopOrder]

        set_sales_sync_cursor(
            model_admin,
            request,
            PrestashopOrder.objects.filter(pk__in=[first.pk, second.pk]),
        )

        cursor = SyncCursor.objects.get(source=SyncCursorSource.ORDERS)
        assert cursor.last_source_key == "77"
        assert cursor.last_modified_at == second.date_add


# --- Custom filters ---


@pytest.mark.django_db
class TestSyncFilters:
    def test_last_icg_modified_date_presence_filter_yes(self):
        dated = _make_product(icg_id=4001, reference="DATED", last_icg_modified_date=timezone.now())
        undated = _make_product(icg_id=4002, reference="UNDATED")

        model_admin = ProductAdmin(Product, admin.site)
        request = RequestFactory().get("/admin/")

        f = LastICGModifiedDatePresenceFilter(
            request, {"has_last_icg_modified_date": ["yes"]}, Product, model_admin
        )
        result = f.queryset(request, Product.objects.all())

        assert dated in result
        assert undated not in result

    def test_last_icg_modified_date_presence_filter_no(self):
        dated = _make_product(icg_id=4101, reference="DATED", last_icg_modified_date=timezone.now())
        undated = _make_product(icg_id=4102, reference="UNDATED")

        model_admin = ProductAdmin(Product, admin.site)
        request = RequestFactory().get("/admin/")

        f = LastICGModifiedDatePresenceFilter(
            request, {"has_last_icg_modified_date": ["no"]}, Product, model_admin
        )
        result = f.queryset(request, Product.objects.all())

        assert undated in result
        assert dated not in result

    def test_catalog_admins_expose_last_icg_modified_date_filters(self):
        registry = admin_site._registry
        expected = {
            Manufacturer,
            Product,
            Combination,
            Price,
            Stock,
        }

        for model in expected:
            list_filter = registry[model].list_filter
            assert "last_icg_modified_date" in list_filter
            assert LastICGModifiedDatePresenceFilter in list_filter

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

        stuck_admin = admin_site._registry[SyncJob]
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

    def test_handles_non_dict_json(self):
        product = _make_product()
        product.last_sync_error = "[]"
        product.save(update_fields=["last_sync_error"])

        assert _sync_error_display(product) == "[]"

    def test_handles_json_null(self):
        product = _make_product()
        product.last_sync_error = "null"
        product.save(update_fields=["last_sync_error"])

        assert _sync_error_display(product) == "null"


@pytest.mark.django_db
class TestDashboardIndex:
    def test_dashboard_context_has_entity_stats(self):
        man = Manufacturer.objects.create(icg_code="M1", name="Maker", prestashop_id=1)
        cat = Category.objects.create(name="Root")
        prod = Product.objects.create(
            icg_id=1, reference="REF01", name="P", manufacturer=man, category_default=cat
        )
        comb = Combination.objects.create(product=prod, icg_size="M", icg_color="R", active=True)
        Price.objects.create(combination=comb, amount_ex_vat="10.00")
        Stock.objects.create(combination=comb, warehouse_code="WH1", quantity=5)
        SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF01",
            status=SyncJobStatus.FAILED,
        )
        SyncCursor.objects.create(source="products", last_source_key="100")

        staff = User.objects.create_user("staff", password="x", is_staff=True)
        factory = RequestFactory()
        request = factory.get("/admin/")
        request.user = staff

        context = admin_site.index(request)
        extra = context.context_data

        stats = extra["entity_stats"]
        labels = {s["label"]: s for s in stats}
        assert labels["Products"]["total"] == 1
        assert labels["Products"]["never_synced"] == 1
        assert labels["Combinations"]["total"] == 1
        assert labels["Prices"]["total"] == 1
        assert labels["Stock"]["total"] == 1
        assert extra["totals"]["total"] == 6
        assert extra["cursors"].count() == 1

    def test_dashboard_context_totals_aggregate_correctly(self):
        man = Manufacturer.objects.create(icg_code="M2", name="Mkr")
        prod = Product.objects.create(icg_id=2, reference="REF02", name="Q", manufacturer=man)
        comb = Combination.objects.create(product=prod, icg_size="L", icg_color="B")
        Stock.objects.create(combination=comb, warehouse_code="WH1", quantity=0)

        staff = User.objects.create_user("staff2", password="x", is_staff=True)
        factory = RequestFactory()
        request = factory.get("/admin/")
        request.user = staff

        context = admin_site.index(request)
        extra = context.context_data

        stats = extra["entity_stats"]
        labels = {s["label"]: s for s in stats}
        assert labels["Products"]["total"] == 1
        assert labels["Combinations"]["total"] == 1
        assert labels["Stock"]["total"] == 1
        assert labels["Categories"]["total"] == 0
        assert extra["totals"]["total"] == 4
