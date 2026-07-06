import json
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from apps.catalog.models import (
    Combination,
    Manufacturer,
    Price,
    Product,
)
from apps.prestashop.client import PrestashopClient, PrestashopError
from apps.prestashop.services import export_discount, format_sync_error
from apps.sync.models import SyncJob, SyncJobStatus, SyncJobType
from apps.sync.tasks import export_discounts


@pytest.fixture(autouse=True)
def _clean_db():
    SyncJob.objects.all().delete()
    Price.objects.all().delete()
    Combination.objects.all().delete()
    Product.objects.all().delete()
    Manufacturer.objects.all().delete()


def _make_manufacturer(**overrides):
    return Manufacturer.objects.create(
        icg_code=overrides.pop("icg_code", "M-1001"),
        name=overrides.pop("name", "Manufacturer 1001"),
        prestashop_id=overrides.pop("prestashop_id", 10),
        **overrides,
    )


def _make_product(**overrides):
    manufacturer = overrides.pop("manufacturer", None) or _make_manufacturer()
    return Product.objects.create(
        icg_id=overrides.pop("icg_id", 1001),
        reference=overrides.pop("reference", "REF001"),
        name=overrides.pop("name", "Product One"),
        manufacturer=manufacturer,
        visible_web=overrides.pop("visible_web", True),
        discontinued=overrides.pop("discontinued", False),
        discount_percent=overrides.pop("discount_percent", 0),
        **overrides,
    )


def _make_product_prestashop_id(product, prestashop_product_id):
    product.prestashop_id = prestashop_product_id
    product.save(update_fields=["prestashop_id"])


# ─── Export discount service ─────────────────────────────────────


@pytest.mark.django_db
class TestExportDiscount:
    def test_nonzero_discount_creates_specific_price(self):
        product = _make_product(discount_percent=Decimal("20"))
        _make_product_prestashop_id(product, 22)

        client = Mock()
        client.list_all_specific_price_ids_by_product.return_value = []
        client.upsert_specific_price.return_value = 500

        result = export_discount(product.pk, client=client)

        assert result["discount_percent"] == "20.00"
        assert result["prestashop_specific_price_id"] == 500
        client.upsert_specific_price.assert_called_once_with(22, Decimal("20"), prestashop_id=None)
        client.delete_specific_price.assert_not_called()

        product.refresh_from_db()
        assert product.prestashop_specific_price_id == 500

    def test_nonzero_discount_recreates_single_specific_price_after_cleanup(self):
        product = _make_product(discount_percent=Decimal("15"))
        _make_product_prestashop_id(product, 22)
        product.prestashop_specific_price_id = 500
        product.save(update_fields=["prestashop_specific_price_id"])

        client = Mock()
        client.list_all_specific_price_ids_by_product.return_value = [500, 501, 777]
        client.upsert_specific_price.return_value = 900

        result = export_discount(product.pk, client=client)

        assert client.delete_specific_price.call_args_list == [
            ((500,), {}),
            ((501,), {}),
            ((777,), {}),
        ]
        client.upsert_specific_price.assert_called_once_with(22, Decimal("15"), prestashop_id=None)
        assert result["prestashop_specific_price_id"] == 900

        product.refresh_from_db()
        assert product.prestashop_specific_price_id == 900

    def test_zero_discount_deletes_all_specific_prices_for_product(self):
        product = _make_product(discount_percent=Decimal("0"))
        _make_product_prestashop_id(product, 22)
        product.prestashop_specific_price_id = 500
        product.save(update_fields=["prestashop_specific_price_id"])

        client = Mock()
        client.list_all_specific_price_ids_by_product.return_value = [500, 501, 777]

        result = export_discount(product.pk, client=client)

        assert client.delete_specific_price.call_args_list == [
            ((500,), {}),
            ((501,), {}),
            ((777,), {}),
        ]
        client.upsert_specific_price.assert_not_called()
        assert result["prestashop_specific_price_id"] is None

        product.refresh_from_db()
        assert product.prestashop_specific_price_id is None

    def test_zero_discount_with_no_existing_does_nothing(self):
        product = _make_product(discount_percent=Decimal("0"))
        _make_product_prestashop_id(product, 22)

        client = Mock()
        client.list_all_specific_price_ids_by_product.return_value = []

        result = export_discount(product.pk, client=client)

        client.upsert_specific_price.assert_not_called()
        client.delete_specific_price.assert_not_called()
        assert result["prestashop_specific_price_id"] is None

    def test_delete_persists_id_before_final_save(self):
        product = _make_product(discount_percent=Decimal("0"))
        _make_product_prestashop_id(product, 22)
        product.prestashop_specific_price_id = 500
        product.save(update_fields=["prestashop_specific_price_id"])

        client = Mock()
        client.list_all_specific_price_ids_by_product.return_value = [500]

        call_count = 0
        original_save = Product.save

        def failing_save(self, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("DB write failed")
            return original_save(self, *args, **kwargs)

        with patch.object(Product, "save", failing_save):
            with pytest.raises(Exception, match="DB write failed"):
                export_discount(product.pk, client=client)

        product.refresh_from_db()
        assert product.prestashop_specific_price_id is None

    def test_fails_without_product_mapping(self):
        product = _make_product(discount_percent=Decimal("10"))

        client = Mock()

        with pytest.raises(PrestashopError, match="must be exported before discount"):
            export_discount(product.pk, client=client)

        product.refresh_from_db()
        assert product.sync_required is True

    def test_stores_structured_error_on_failure(self):
        product = _make_product(discount_percent=Decimal("10"))
        _make_product_prestashop_id(product, 22)

        client = Mock()
        client.list_all_specific_price_ids_by_product.return_value = []
        client.upsert_specific_price.side_effect = PrestashopError(
            "Prestashop returned HTTP 500 for specific_prices.",
            status_code=500,
            body="<errors />",
        )

        with pytest.raises(PrestashopError):
            export_discount(product.pk, client=client)

        product.refresh_from_db()
        payload = json.loads(product.last_sync_error)
        assert payload["status_code"] == 500
        assert product.sync_required is True


# ─── PrestashopClient specific_prices methods ────────────────────


@pytest.mark.django_db
class TestPrestashopClientSpecificPrices:
    def _response(self, payload: str, status_code: int = 200):
        response = Mock()
        response.status_code = status_code
        response.text = payload
        return response

    def test_find_specific_price_by_product(self, settings):
        response = self._response(
            "<prestashop><specific_prices>"
            "<specific_price id='42' />"
            "</specific_prices></prestashop>"
        )
        session = Mock()
        session.request.return_value = response
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)
        sp_id = client.find_specific_price_by_product(22)

        assert sp_id == 42

    def test_find_specific_price_returns_none_when_not_found(self, settings):
        response = self._response("<prestashop><specific_prices></specific_prices></prestashop>")
        session = Mock()
        session.request.return_value = response
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)
        sp_id = client.find_specific_price_by_product(22)

        assert sp_id is None

    def test_list_all_specific_price_ids_by_product(self, settings):
        response = self._response(
            "<prestashop><specific_prices>"
            "<specific_price><id>42</id><id_product_attribute>0</id_product_attribute></specific_price>"
            "<specific_price><id>43</id><id_product_attribute>55</id_product_attribute></specific_price>"
            "</specific_prices></prestashop>"
        )
        session = Mock()
        session.request.return_value = response
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)
        sp_ids = client.list_all_specific_price_ids_by_product(22)

        assert sp_ids == [42, 43]

    def test_delete_specific_price(self, settings):
        response = self._response("")
        session = Mock()
        session.request.return_value = response
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)
        client.delete_specific_price(42)

        call_args = session.request.call_args
        assert call_args.args[0] == "DELETE"
        assert "/api/specific_prices/42" in call_args.args[1]

    def test_upsert_specific_price_creates(self, settings):
        blank_response = self._response(
            "<prestashop><specific_price>"
            "<id_product></id_product>"
            "<id_product_attribute></id_product_attribute>"
            "<id_shop></id_shop>"
            "<id_cart></id_cart>"
            "<id_currency></id_currency>"
            "<id_country></id_country>"
            "<id_group></id_group>"
            "<id_customer></id_customer>"
            "<price></price>"
            "<reduction_tax></reduction_tax>"
            "<from></from>"
            "<to></to>"
            "<reduction></reduction>"
            "<reduction_type></reduction_type>"
            "<from_quantity></from_quantity>"
            "</specific_price></prestashop>"
        )
        create_response = self._response(
            "<prestashop><specific_price><id>99</id></specific_price></prestashop>"
        )
        session = Mock()
        session.request.side_effect = [blank_response, create_response]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)
        sp_id = client.upsert_specific_price(22, Decimal("30"))

        assert sp_id == 99
        post_call = session.request.call_args_list[1]
        payload = post_call.args[2] if len(post_call.args) > 2 else post_call.kwargs.get("data")
        assert "<id_product>22</id_product>" in payload
        assert "<reduction>0.3</reduction>" in payload
        assert "<reduction_type>percentage</reduction_type>" in payload
        assert "<id_product_attribute>0</id_product_attribute>" in payload

    def test_upsert_specific_price_updates(self, settings):
        existing_response = self._response(
            "<prestashop><specific_price>"
            "<id_product>22</id_product>"
            "<id_product_attribute>0</id_product_attribute>"
            "<reduction>0.2</reduction>"
            "<reduction_type>percentage</reduction_type>"
            "</specific_price></prestashop>"
        )
        session = Mock()
        session.request.return_value = existing_response
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)
        sp_id = client.upsert_specific_price(22, Decimal("10"), prestashop_id=42)

        assert sp_id == 42
        put_call = session.request.call_args_list[1]
        assert put_call.args[0] == "PUT"
        assert "/api/specific_prices/42" in put_call.args[1]


# ─── Export discount Celery task ─────────────────────────────────


@pytest.mark.django_db
class TestExportDiscountTask:
    def test_task_exports_pending_discounts(self, monkeypatch):
        product = _make_product(discount_percent=Decimal("20"), discount_sync_required=True)
        _make_product_prestashop_id(product, 22)

        def fake_export_discount(product_id):
            p = Product.objects.get(pk=product_id)
            p.prestashop_specific_price_id = 500
            p.last_sync_error = ""
            p.last_synced_at = p.updated_at
            p.save()
            return {
                "product_id": product_id,
                "prestashop_product_id": 22,
                "prestashop_specific_price_id": 500,
                "discount_percent": str(Decimal("20")),
            }

        monkeypatch.setattr("apps.sync.tasks.export_discount", fake_export_discount)

        result = export_discounts()

        assert result == {"status": "success", "processed": 1, "failed": 0}
        assert SyncJob.objects.filter(job_type=SyncJobType.EXPORT_DISCOUNT).count() == 1
        assert SyncJob.objects.filter(status=SyncJobStatus.SUCCEEDED).count() == 1

    def test_task_marks_job_failed_when_export_raises(self, monkeypatch):
        product = _make_product(discount_percent=Decimal("20"), discount_sync_required=True)
        _make_product_prestashop_id(product, 22)

        def fake_export_discount(product_id):
            p = Product.objects.get(pk=product_id)
            p.last_sync_error = format_sync_error(PrestashopError("boom", status_code=503))
            p.save(update_fields=["last_sync_error", "updated_at"])
            raise PrestashopError("boom", status_code=503)

        monkeypatch.setattr("apps.sync.tasks.export_discount", fake_export_discount)

        result = export_discounts()

        product.refresh_from_db()
        job = SyncJob.objects.get(job_type=SyncJobType.EXPORT_DISCOUNT)
        assert result == {"status": "success", "processed": 0, "failed": 1}
        assert job.status == SyncJobStatus.PENDING
        assert job.attempts == 2
        assert json.loads(product.last_sync_error)["status_code"] == 503

    def test_task_skips_products_without_discount_and_no_specific_price(self):
        product = _make_product(discount_percent=Decimal("0"))
        _make_product_prestashop_id(product, 22)

        result = export_discounts()

        assert result == {"status": "success", "processed": 0, "failed": 0}
        assert SyncJob.objects.count() == 0

    def test_task_includes_products_with_existing_specific_price(self):
        product = _make_product(discount_percent=Decimal("0"), discount_sync_required=True)
        _make_product_prestashop_id(product, 22)
        product.prestashop_specific_price_id = 500
        product.save(update_fields=["prestashop_specific_price_id"])

        def fake_export_discount(product_id):
            p = Product.objects.get(pk=product_id)
            p.prestashop_specific_price_id = None
            p.last_sync_error = ""
            p.last_synced_at = p.updated_at
            p.save()
            return {
                "product_id": product_id,
                "prestashop_product_id": 22,
                "prestashop_specific_price_id": None,
                "discount_percent": str(Decimal("0")),
            }

        import apps.sync.tasks as tasks_mod

        original = tasks_mod.export_discount
        tasks_mod.export_discount = fake_export_discount
        try:
            result = export_discounts()
        finally:
            tasks_mod.export_discount = original

        assert result["processed"] == 1
