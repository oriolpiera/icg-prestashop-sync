import json
from unittest.mock import Mock

import pytest

from apps.catalog.models import (
    Combination,
    Manufacturer,
    PrestashopMapping,
    Product,
    Stock,
)
from apps.prestashop.client import PrestashopClient, PrestashopError
from apps.prestashop.services import export_stock, format_sync_error
from apps.sync.models import SyncJob, SyncJobStatus, SyncJobType
from apps.sync.tasks import export_stocks


@pytest.fixture(autouse=True)
def _clean_db():
    SyncJob.objects.all().delete()
    PrestashopMapping.objects.all().delete()
    Stock.objects.all().delete()
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
        **overrides,
    )


def _make_combination(**overrides):
    product = overrides.pop("product", None) or _make_product()
    return Combination.objects.create(
        product=product,
        icg_size=overrides.pop("icg_size", "M"),
        icg_color=overrides.pop("icg_color", "Red"),
        ean13=overrides.pop("ean13", "1234567890123"),
        active=overrides.pop("active", True),
        **overrides,
    )


def _make_stock(**overrides):
    combination = overrides.pop("combination", None) or _make_combination()
    return Stock.objects.create(
        combination=combination,
        warehouse_code=overrides.pop("warehouse_code", "01"),
        quantity=overrides.pop("quantity", 10),
        **overrides,
    )


def _make_product_mapping(product, prestashop_product_id):
    return PrestashopMapping.objects.create(
        product=product,
        prestashop_product_id=prestashop_product_id,
    )


def _make_combination_mapping(combination, prestashop_combination_id):
    return PrestashopMapping.objects.create(
        combination=combination,
        prestashop_combination_id=prestashop_combination_id,
    )


def _response(payload: str, status_code: int = 200):
    response = Mock()
    response.status_code = status_code
    response.text = payload
    return response


def _combination_xml(ps_comb_id=55, ps_product_id=22, stock_available_id=33):
    return (
        f"<prestashop><combination>"
        f"<id>{ps_comb_id}</id>"
        f"<id_product>{ps_product_id}</id_product>"
        "<ean13>1234567890123</ean13>"
        "<active>1</active>"
        "<price>0</price>"
        "<minimal_quantity>1</minimal_quantity>"
        "<associations>"
        "<product_option_values>"
        "<product_option_value><id>100</id></product_option_value>"
        "</product_option_values>"
        "<stock_availables>"
        f"<stock_available><id>{stock_available_id}</id></stock_available>"
        "</stock_availables>"
        "</associations>"
        "</combination></prestashop>"
    )


def _stock_available_xml(sa_id=33, quantity=5, depends_on_stock=0, out_of_stock=2):
    return (
        f"<prestashop><stock_available>"
        f"<id>{sa_id}</id>"
        "<id_product>22</id_product>"
        "<id_product_attribute>55</id_product_attribute>"
        f"<depends_on_stock>{depends_on_stock}</depends_on_stock>"
        f"<out_of_stock>{out_of_stock}</out_of_stock>"
        f"<quantity>{quantity}</quantity>"
        "<id_shop>1</id_shop>"
        "<id_shop_group>1</id_shop_group>"
        "</stock_available></prestashop>"
    )


# ─── Stock export service ───────────────────────────────────────────


@pytest.mark.django_db
class TestStockExport:
    def test_export_updates_prestashop_stock(self):
        product = _make_product()
        _make_product_mapping(product, 22)
        combination = _make_combination(product=product)
        _make_combination_mapping(combination, 55)
        stock = _make_stock(combination=combination, quantity=42)

        client = Mock()

        result = export_stock(stock.pk, client=client)

        assert result == {"stock_id": stock.pk, "prestashop_combination_id": 55, "quantity": 42}
        client.upsert_stock.assert_called_once_with(55, 42)
        stock.refresh_from_db()
        assert stock.sync_required is False
        assert stock.last_sync_error == ""

    def test_export_requires_combination_mapping(self):
        product = _make_product()
        combination = _make_combination(product=product)
        stock = _make_stock(combination=combination)

        client = Mock()

        with pytest.raises(PrestashopError, match="must be exported before"):
            export_stock(stock.pk, client=client)

        stock.refresh_from_db()
        payload = json.loads(stock.last_sync_error)
        assert "must be exported before" in payload["message"]
        assert stock.sync_required is True

    def test_export_combination_mapping_without_prestashop_id(self):
        product = _make_product()
        combination = _make_combination(product=product)
        PrestashopMapping.objects.create(combination=combination, prestashop_combination_id=None)
        stock = _make_stock(combination=combination)

        client = Mock()

        with pytest.raises(PrestashopError, match="must be exported before"):
            export_stock(stock.pk, client=client)

        stock.refresh_from_db()
        assert stock.sync_required is True

    def test_export_stores_structured_error(self):
        product = _make_product()
        _make_product_mapping(product, 22)
        combination = _make_combination(product=product)
        _make_combination_mapping(combination, 55)
        stock = _make_stock(combination=combination)

        client = Mock()
        client.upsert_stock.side_effect = PrestashopError(
            "Prestashop returned HTTP 500 for stock_availables.",
            status_code=500,
            body="<errors />",
        )

        with pytest.raises(PrestashopError):
            export_stock(stock.pk, client=client)

        stock.refresh_from_db()
        payload = json.loads(stock.last_sync_error)
        assert payload["status_code"] == 500
        assert stock.sync_required is True

    def test_export_zero_quantity(self):
        product = _make_product()
        _make_product_mapping(product, 22)
        combination = _make_combination(product=product)
        _make_combination_mapping(combination, 55)
        stock = _make_stock(combination=combination, quantity=0)

        client = Mock()

        export_stock(stock.pk, client=client)

        client.upsert_stock.assert_called_once_with(55, 0)

    def test_export_is_idempotent(self):
        product = _make_product()
        _make_product_mapping(product, 22)
        combination = _make_combination(product=product)
        _make_combination_mapping(combination, 55)
        stock = _make_stock(combination=combination, quantity=25)

        client = Mock()

        export_stock(stock.pk, client=client)
        stock.refresh_from_db()
        assert stock.sync_required is False

        stock.sync_required = True
        stock.save(update_fields=["sync_required"])

        export_stock(stock.pk, client=client)
        assert client.upsert_stock.call_count == 2
        client.upsert_stock.assert_called_with(55, 25)


# ─── Stock export task ───────────────────────────────────────────────


@pytest.mark.django_db
class TestStockExportTask:
    def test_task_exports_pending_stocks(self, monkeypatch):
        product = _make_product()
        _make_product_mapping(product, 22)
        comb1 = _make_combination(product=product, icg_size="M", icg_color="Red")
        comb2 = _make_combination(product=product, icg_size="L", icg_color="Blue")
        _make_combination_mapping(comb1, 55)
        _make_combination_mapping(comb2, 56)
        _make_stock(combination=comb1, quantity=10)
        _make_stock(combination=comb2, quantity=20)

        def fake_export(stock_id):
            s = Stock.objects.get(pk=stock_id)
            s.sync_required = False
            s.last_sync_error = ""
            s.last_synced_at = s.updated_at
            s.save()
            return {"stock_id": stock_id, "prestashop_combination_id": 55, "quantity": s.quantity}

        monkeypatch.setattr("apps.sync.tasks.export_stock", fake_export)

        result = export_stocks()

        assert result == {"status": "success", "processed": 2, "failed": 0}
        assert SyncJob.objects.filter(job_type=SyncJobType.EXPORT_STOCK).count() == 2
        assert SyncJob.objects.filter(status=SyncJobStatus.SUCCEEDED).count() == 2

    def test_task_marks_job_failed_when_export_raises(self, monkeypatch):
        product = _make_product()
        _make_product_mapping(product, 22)
        combination = _make_combination(product=product)
        _make_combination_mapping(combination, 55)
        stock = _make_stock(combination=combination)

        def fake_export(stock_id):
            s = Stock.objects.get(pk=stock_id)
            s.last_sync_error = format_sync_error(PrestashopError("boom", status_code=503))
            s.save(update_fields=["last_sync_error", "updated_at"])
            raise PrestashopError("boom", status_code=503)

        monkeypatch.setattr("apps.sync.tasks.export_stock", fake_export)

        result = export_stocks()

        stock.refresh_from_db()
        job = SyncJob.objects.get(job_type=SyncJobType.EXPORT_STOCK)
        assert result == {"status": "success", "processed": 0, "failed": 1}
        assert job.status == SyncJobStatus.PENDING
        assert job.attempts == 2
        assert json.loads(stock.last_sync_error)["status_code"] == 503


# ─── PrestaShopClient stock methods ─────────────────────────────────


@pytest.mark.django_db
class TestPrestashopClientStockExport:
    def test_upsert_stock_fetches_and_mutates_quantity(self, settings):
        session = Mock()
        session.request.side_effect = [
            _response(_combination_xml(55, 22, 33)),
            _response(_stock_available_xml(33, quantity=5)),
            _response("<prestashop><stock_available><id>33</id></stock_available></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        client = PrestashopClient(session=session)
        client.upsert_stock(55, 42)

        assert session.request.call_count == 3

        get_comb = session.request.call_args_list[0]
        assert get_comb.args[0] == "GET"
        assert "/combinations/55" in get_comb.args[1]

        get_sa = session.request.call_args_list[1]
        assert get_sa.args[0] == "GET"
        assert "/stock_availables/33" in get_sa.args[1]

        put_call = session.request.call_args_list[2]
        assert put_call.args[0] == "PUT"
        assert "/stock_availables/33" in put_call.args[1]
        payload = put_call.kwargs["data"]
        assert "<quantity>42</quantity>" in payload

    def test_upsert_stock_preserves_existing_fields(self, settings):
        session = Mock()
        session.request.side_effect = [
            _response(_combination_xml(55, 22, 33)),
            _response(_stock_available_xml(33, quantity=5, depends_on_stock=1, out_of_stock=3)),
            _response("<prestashop><stock_available><id>33</id></stock_available></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        client = PrestashopClient(session=session)
        client.upsert_stock(55, 42)

        put_call = session.request.call_args_list[2]
        payload = put_call.kwargs["data"]
        assert "<depends_on_stock>1</depends_on_stock>" in payload
        assert "<out_of_stock>3</out_of_stock>" in payload
        assert "<id_product>22</id_product>" in payload
        assert "<id_product_attribute>55</id_product_attribute>" in payload
        assert "<quantity>42</quantity>" in payload

    def test_upsert_stock_fails_without_stock_available(self, settings):
        combination_without_stock = (
            "<prestashop><combination>"
            "<id>55</id>"
            "<id_product>22</id_product>"
            "<associations>"
            "<stock_availables></stock_availables>"
            "</associations>"
            "</combination></prestashop>"
        )
        session = Mock()
        session.request.return_value = _response(combination_without_stock)
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        client = PrestashopClient(session=session)

        with pytest.raises(PrestashopError, match="no stock_available association"):
            client.upsert_stock(55, 42)

    def test_upsert_stock_falls_back_to_stock_available_lookup(self, settings):
        combination_without_stock = (
            "<prestashop><combination>"
            "<id>55</id>"
            "<id_product>22</id_product>"
            "<associations>"
            "<stock_availables></stock_availables>"
            "</associations>"
            "</combination></prestashop>"
        )
        stock_available_search = (
            "<prestashop><stock_availables>"
            "<stock_available><id>64</id><id_product>22</id_product>"
            "<id_product_attribute>55</id_product_attribute><quantity>0</quantity></stock_available>"
            "</stock_availables></prestashop>"
        )
        session = Mock()
        session.request.side_effect = [
            _response(combination_without_stock),
            _response(stock_available_search),
            _response(_stock_available_xml(64, quantity=5)),
            _response("<prestashop><stock_available><id>64</id></stock_available></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        client = PrestashopClient(session=session)
        client.upsert_stock(55, 42)

        assert session.request.call_count == 4
        search_call = session.request.call_args_list[1]
        assert search_call.kwargs["params"] == {
            "filter[id_product_attribute]": "55",
            "limit": "1",
        }
        put_call = session.request.call_args_list[3]
        assert "/stock_availables/64" in put_call.args[1]
        assert "<quantity>42</quantity>" in put_call.kwargs["data"]

    def test_upsert_stock_fails_without_combination_node(self, settings):
        session = Mock()
        session.request.return_value = _response("<prestashop></prestashop>")
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        client = PrestashopClient(session=session)

        with pytest.raises(PrestashopError, match="did not include a combination node"):
            client.upsert_stock(55, 42)
