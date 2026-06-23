import json
from unittest.mock import Mock

import pytest

from apps.catalog.models import Category, CategoryType, Manufacturer, Product
from apps.prestashop.client import PrestashopClient, PrestashopError
from apps.prestashop.services import export_product, format_sync_error
from apps.sync.models import SyncJob, SyncJobStatus, SyncJobType
from apps.sync.tasks import export_products


@pytest.fixture(autouse=True)
def _clean_db():
    SyncJob.objects.all().delete()
    Product.objects.all().delete()
    Manufacturer.objects.all().delete()
    Category.objects.all().delete()


@pytest.fixture
def _default_category():
    return Category.objects.create(
        prestashop_id=2,
        name="Default",
        category_type=CategoryType.DEFAULT,
    )


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
        visible_web=overrides.pop("visible_web", True),
        discontinued=overrides.pop("discontinued", False),
        **overrides,
    )


def _response(payload: str, status_code: int = 200):
    response = Mock()
    response.status_code = status_code
    response.text = payload
    return response


@pytest.mark.django_db
class TestProductExport:
    def test_export_creates_and_maps_new_product(self, _default_category):
        product = _make_product()
        client = Mock()
        client.find_product_id_by_reference.return_value = None
        client.upsert_product.return_value = 77

        result = export_product(product.pk, client=client)

        product.refresh_from_db()
        assert result == {"product_id": product.pk, "prestashop_id": 77}
        assert product.prestashop_id == 77
        assert product.sync_required is False
        assert product.last_sync_error == ""
        client.upsert_product.assert_called_once_with(
            product,
            prestashop_id=None,
            tax_rules_group_id=None,
            category_default_id=2,
            category_ids=[2],
        )

    def test_export_reuses_existing_prestashop_product_by_reference(self, _default_category):
        product = _make_product(reference="REF-EXISTING")
        client = Mock()
        client.find_product_id_by_reference.return_value = 45
        client.upsert_product.return_value = 45

        export_product(product.pk, client=client)

        product.refresh_from_db()
        assert product.prestashop_id == 45
        client.upsert_product.assert_called_once_with(
            product,
            prestashop_id=45,
            tax_rules_group_id=None,
            category_default_id=2,
            category_ids=[2],
        )

    def test_export_updates_already_mapped_product(self, _default_category):
        product = _make_product()
        product.prestashop_id = 34
        product.save(update_fields=["prestashop_id"])
        client = Mock()
        client.upsert_product.return_value = 34

        export_product(product.pk, client=client)

        client.find_product_id_by_reference.assert_not_called()
        client.upsert_product.assert_called_once_with(
            product,
            prestashop_id=34,
            tax_rules_group_id=None,
            category_default_id=2,
            category_ids=[2],
        )

    def test_export_uses_race_safe_mapping_upsert(self, _default_category):
        product = _make_product()
        client = Mock()
        client.find_product_id_by_reference.return_value = None
        client.upsert_product.return_value = 77

        result = export_product(product.pk, client=client)

        assert result == {"product_id": product.pk, "prestashop_id": 77}
        product.refresh_from_db()
        assert product.prestashop_id == 77

    def test_export_requires_mapped_manufacturer(self, _default_category):
        manufacturer = Manufacturer.objects.create(icg_code="15000", name="Brand X")
        product = _make_product(manufacturer=manufacturer)
        client = Mock()

        with pytest.raises(PrestashopError):
            export_product(product.pk, client=client)

        product.refresh_from_db()
        payload = json.loads(product.last_sync_error)
        assert "must be exported before product sync" in payload["message"]
        client.find_product_id_by_reference.assert_not_called()

    def test_export_stores_structured_error(self, _default_category):
        product = _make_product()
        client = Mock()
        client.find_product_id_by_reference.return_value = None
        client.upsert_product.side_effect = PrestashopError(
            "Prestashop returned HTTP 500 for products.",
            status_code=500,
            body="<errors />",
        )

        with pytest.raises(PrestashopError):
            export_product(product.pk, client=client)

        product.refresh_from_db()
        payload = json.loads(product.last_sync_error)
        assert payload["status_code"] == 500
        assert product.sync_required is True

    def test_export_includes_product_categories(self, _default_category):
        extra = Category.objects.create(prestashop_id=300, name="Extra")
        product = _make_product()
        product.categories.add(extra)
        client = Mock()
        client.find_product_id_by_reference.return_value = None
        client.upsert_product.return_value = 77

        export_product(product.pk, client=client)

        client.upsert_product.assert_called_once_with(
            product,
            prestashop_id=None,
            tax_rules_group_id=None,
            category_default_id=2,
            category_ids=[300, 2],
        )

    def test_export_uses_product_default_category(self, _default_category):
        custom_default = Category.objects.create(prestashop_id=100, name="Custom Default")
        product = _make_product(category_default=custom_default)
        client = Mock()
        client.find_product_id_by_reference.return_value = None
        client.upsert_product.return_value = 77

        export_product(product.pk, client=client)

        client.upsert_product.assert_called_once_with(
            product,
            prestashop_id=None,
            tax_rules_group_id=None,
            category_default_id=100,
            category_ids=[100],
        )

    def test_export_raises_when_no_default_category(self):
        product = _make_product()
        client = Mock()

        with pytest.raises(PrestashopError, match="No default category configured"):
            export_product(product.pk, client=client)

        product.refresh_from_db()
        payload = json.loads(product.last_sync_error)
        assert "No default category" in payload["message"]

    def test_export_recovers_when_product_deleted_from_prestashop(self, _default_category):
        product = _make_product()
        product.prestashop_id = 999
        product.save(update_fields=["prestashop_id"])

        client = Mock()
        call_count = 0

        def upsert_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("prestashop_id") == 999:
                raise PrestashopError(
                    "Prestashop returned HTTP 404 for products.",
                    status_code=404,
                )
            return 77

        client.upsert_product.side_effect = upsert_side_effect
        client.find_product_id_by_reference.return_value = None

        result = export_product(product.pk, client=client)

        product.refresh_from_db()
        assert result == {"product_id": product.pk, "prestashop_id": 77}
        assert product.prestashop_id == 77
        assert product.sync_required is False
        assert call_count == 2

    def test_export_does_not_recover_on_non_404_error(self, _default_category):
        product = _make_product()
        product.prestashop_id = 999
        product.save(update_fields=["prestashop_id"])

        client = Mock()
        client.upsert_product.side_effect = PrestashopError(
            "Prestashop returned HTTP 500 for products.",
            status_code=500,
        )

        with pytest.raises(PrestashopError):
            export_product(product.pk, client=client)

        product.refresh_from_db()
        assert product.prestashop_id == 999
        assert product.sync_required is True


@pytest.mark.django_db
class TestProductExportTask:
    def test_task_exports_pending_products_and_tracks_jobs(self, monkeypatch):
        first = _make_product(icg_id=1001, reference="REF001")
        second = _make_product(icg_id=1002, reference="REF002")

        def fake_export(product_id: int):
            product = Product.objects.get(pk=product_id)
            product.prestashop_id = product.pk + 100
            product.sync_required = False
            product.last_sync_error = ""
            product.last_synced_at = product.updated_at
            product.save()
            return {"product_id": product_id, "prestashop_id": product.pk + 100}

        monkeypatch.setattr("apps.sync.tasks.export_product", fake_export)

        result = export_products()

        assert result == {"status": "success", "processed": 2, "failed": 0}
        assert SyncJob.objects.filter(job_type=SyncJobType.EXPORT_PRODUCT).count() == 2
        assert SyncJob.objects.filter(status=SyncJobStatus.SUCCEEDED).count() == 2
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.sync_required is False
        assert second.sync_required is False

    def test_task_marks_job_failed_when_export_raises(self, monkeypatch):
        product = _make_product(reference="REF-FAIL")

        def fake_export(product_id: int):
            product = Product.objects.get(pk=product_id)
            product.last_sync_error = format_sync_error(PrestashopError("boom", status_code=503))
            product.save(update_fields=["last_sync_error", "updated_at"])
            raise PrestashopError("boom", status_code=503)

        monkeypatch.setattr("apps.sync.tasks.export_product", fake_export)

        result = export_products()

        product.refresh_from_db()
        job = SyncJob.objects.get(job_type=SyncJobType.EXPORT_PRODUCT)
        assert result == {"status": "success", "processed": 0, "failed": 1}
        assert job.status == SyncJobStatus.PENDING
        assert job.attempts == 2
        assert json.loads(product.last_sync_error)["status_code"] == 503


@pytest.mark.django_db
class TestPrestashopClientProductExport:
    def test_find_product_uses_exact_match_filter(self, settings):
        response = _response("<prestashop><products><product id='22' /></products></prestashop>")
        session = Mock()
        session.request.return_value = response
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)

        product_id = client.find_product_id_by_reference("REF001")

        assert product_id == 22
        assert session.request.call_args.kwargs["params"] == {
            "filter[reference]": "[REF001]",
            "limit": "1",
        }

    def test_find_product_rejects_reserved_filter_characters(self, settings):
        session = Mock()
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)

        with pytest.raises(PrestashopError, match="Unsupported product reference characters"):
            client.find_product_id_by_reference("REF|001")

        session.request.assert_not_called()

    def test_upsert_product_creates_with_category(self, settings):
        product = _make_product(visible_web=False)
        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><product>"
                "<id_category_default></id_category_default>"
                "<id_manufacturer></id_manufacturer>"
                "<reference></reference>"
                "<price></price>"
                "<state></state>"
                "<active></active>"
                "<available_for_order></available_for_order>"
                "<show_price></show_price>"
                "<visibility></visibility>"
                "<minimal_quantity></minimal_quantity>"
                "<name><language id='1'></language></name>"
                "<link_rewrite><language id='1'></language></link_rewrite>"
                "<associations><categories></categories></associations>"
                "</product></prestashop>"
            ),
            _response("<prestashop><product><id>77</id></product></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1
        settings.PRESTASHOP_DEFAULT_CATEGORY_ID = 2

        client = PrestashopClient(session=session)

        product_id = client.upsert_product(
            product, category_default_id=251, category_ids=[251, 300]
        )

        assert product_id == 77
        post_call = session.request.call_args_list[1]
        payload = post_call.kwargs["data"]
        assert "<visibility>none</visibility>" in payload
        assert "<active>0</active>" in payload
        assert "<id_category_default>251</id_category_default>" in payload
        assert "position_in_category" not in payload
        assert "<position>" not in payload
        assert "<id>251</id>" in payload
        assert "<id>300</id>" in payload

    def test_upsert_product_disables_discontinued_product(self, settings):
        product = _make_product(discontinued=True)
        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><product>"
                "<id>44</id>"
                "<id_manufacturer>12</id_manufacturer>"
                "<reference>OLD</reference>"
                "<price>0</price>"
                "<state>1</state>"
                "<active>1</active>"
                "<available_for_order>1</available_for_order>"
                "<show_price>1</show_price>"
                "<visibility>both</visibility>"
                "<minimal_quantity>1</minimal_quantity>"
                "<name><language id='1'>Old name</language></name>"
                "<link_rewrite><language id='1'>old-name</language></link_rewrite>"
                "</product></prestashop>"
            ),
            _response("<prestashop><product><id>44</id></product></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1
        settings.PRESTASHOP_DEFAULT_CATEGORY_ID = 2

        client = PrestashopClient(session=session)

        product_id = client.upsert_product(
            product, prestashop_id=44, category_default_id=251, category_ids=[251]
        )

        assert product_id == 44
        put_call = session.request.call_args_list[1]
        payload = put_call.kwargs["data"]
        assert "<active>0</active>" in payload
        assert "<visibility>none</visibility>" in payload
        assert "<available_for_order>0</available_for_order>" in payload

    def test_upsert_product_only_updates_configured_language(self, settings):
        product = _make_product(name="Updated Product")
        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><product>"
                "<id>44</id>"
                "<id_manufacturer>12</id_manufacturer>"
                "<reference>OLD</reference>"
                "<price>0</price>"
                "<state>1</state>"
                "<active>1</active>"
                "<available_for_order>1</available_for_order>"
                "<show_price>1</show_price>"
                "<visibility>both</visibility>"
                "<minimal_quantity>1</minimal_quantity>"
                "<name><language id='1'>Old default</language>"
                "<language id='2'>Nom catala</language></name>"
                "<link_rewrite><language id='1'>old-default</language>"
                "<language id='2'>nom-catala</language></link_rewrite>"
                "</product></prestashop>"
            ),
            _response("<prestashop><product><id>44</id></product></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1
        settings.PRESTASHOP_DEFAULT_CATEGORY_ID = 2

        client = PrestashopClient(session=session)

        client.upsert_product(
            product, prestashop_id=44, category_default_id=251, category_ids=[251]
        )

        put_call = session.request.call_args_list[1]
        payload = put_call.kwargs["data"]
        assert '<language id="1">Updated Product</language>' in payload
        assert '<language id="2">Nom catala</language>' in payload
        assert '<language id="1">updated-product</language>' in payload
        assert '<language id="2">nom-catala</language>' in payload

    def test_upsert_product_fills_all_languages_on_create(self, settings):
        product = _make_product(name="Nou Producte")
        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><product>"
                "<id_category_default></id_category_default>"
                "<id_manufacturer></id_manufacturer>"
                "<reference></reference>"
                "<price></price>"
                "<state></state>"
                "<active></active>"
                "<available_for_order></available_for_order>"
                "<show_price></show_price>"
                "<visibility></visibility>"
                "<minimal_quantity></minimal_quantity>"
                "<name><language id='1'></language>"
                "<language id='2'></language></name>"
                "<link_rewrite><language id='1'></language>"
                "<language id='2'></language></link_rewrite>"
                "<associations><categories></categories></associations>"
                "</product></prestashop>"
            ),
            _response("<prestashop><product><id>88</id></product></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1
        settings.PRESTASHOP_DEFAULT_CATEGORY_ID = 2

        client = PrestashopClient(session=session)

        product_id = client.upsert_product(product, category_default_id=251, category_ids=[251])

        assert product_id == 88
        post_call = session.request.call_args_list[1]
        payload = post_call.kwargs["data"]
        assert '<language id="1">Nou Producte</language>' in payload
        assert '<language id="2">Nou Producte</language>' in payload
        assert '<language id="1">nou-producte</language>' in payload
        assert '<language id="2">nou-producte</language>' in payload

    def test_upsert_product_strips_manufacturer_name_on_create(self, settings):
        product = _make_product()
        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><product>"
                "<id_category_default></id_category_default>"
                "<id_manufacturer>12</id_manufacturer>"
                "<manufacturer_name>Acme</manufacturer_name>"
                "<reference></reference>"
                "<price></price>"
                "<state></state>"
                "<active></active>"
                "<available_for_order></available_for_order>"
                "<show_price></show_price>"
                "<visibility></visibility>"
                "<minimal_quantity></minimal_quantity>"
                "<name><language id='1'></language></name>"
                "<link_rewrite><language id='1'></language></link_rewrite>"
                "<associations><categories></categories></associations>"
                "</product></prestashop>"
            ),
            _response("<prestashop><product><id>77</id></product></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1
        settings.PRESTASHOP_DEFAULT_CATEGORY_ID = 2

        client = PrestashopClient(session=session)

        product_id = client.upsert_product(product, category_default_id=2, category_ids=[2])

        assert product_id == 77
        post_call = session.request.call_args_list[1]
        payload = post_call.kwargs["data"]
        assert "manufacturer_name" not in payload
        assert "<id_manufacturer>" in payload

    def test_upsert_product_strips_manufacturer_name_on_update(self, settings):
        product = _make_product()
        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><product>"
                "<id>44</id>"
                "<id_manufacturer>12</id_manufacturer>"
                "<manufacturer_name>Acme</manufacturer_name>"
                "<reference>OLD</reference>"
                "<price>0</price>"
                "<state>1</state>"
                "<active>1</active>"
                "<available_for_order>1</available_for_order>"
                "<show_price>1</show_price>"
                "<visibility>both</visibility>"
                "<minimal_quantity>1</minimal_quantity>"
                "<name><language id='1'>Old name</language></name>"
                "<link_rewrite><language id='1'>old-name</language></link_rewrite>"
                "</product></prestashop>"
            ),
            _response("<prestashop><product><id>44</id></product></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1
        settings.PRESTASHOP_DEFAULT_CATEGORY_ID = 2

        client = PrestashopClient(session=session)

        product_id = client.upsert_product(
            product, prestashop_id=44, category_default_id=2, category_ids=[2]
        )

        assert product_id == 44
        put_call = session.request.call_args_list[1]
        payload = put_call.kwargs["data"]
        assert "manufacturer_name" not in payload
        assert "<id_manufacturer>" in payload

    def test_upsert_product_strips_quantity_on_create(self, settings):
        product = _make_product()
        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><product>"
                "<id_category_default></id_category_default>"
                "<id_manufacturer></id_manufacturer>"
                "<manufacturer_name>Acme</manufacturer_name>"
                "<quantity>50</quantity>"
                "<reference></reference>"
                "<price></price>"
                "<state></state>"
                "<active></active>"
                "<available_for_order></available_for_order>"
                "<show_price></show_price>"
                "<visibility></visibility>"
                "<minimal_quantity></minimal_quantity>"
                "<name><language id='1'></language></name>"
                "<link_rewrite><language id='1'></language></link_rewrite>"
                "<associations><categories></categories></associations>"
                "</product></prestashop>"
            ),
            _response("<prestashop><product><id>77</id></product></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1
        settings.PRESTASHOP_DEFAULT_CATEGORY_ID = 2

        client = PrestashopClient(session=session)

        product_id = client.upsert_product(product, category_default_id=2, category_ids=[2])

        assert product_id == 77
        post_call = session.request.call_args_list[1]
        payload = post_call.kwargs["data"]
        assert "manufacturer_name" not in payload
        assert "<quantity>" not in payload

    def test_upsert_product_strips_quantity_on_update(self, settings):
        product = _make_product()
        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><product>"
                "<id>44</id>"
                "<id_manufacturer>12</id_manufacturer>"
                "<manufacturer_name>Acme</manufacturer_name>"
                "<reference>OLD</reference>"
                "<price>0</price>"
                "<state>1</state>"
                "<active>1</active>"
                "<available_for_order>1</available_for_order>"
                "<show_price>1</show_price>"
                "<visibility>both</visibility>"
                "<minimal_quantity>1</minimal_quantity>"
                "<name><language id='1'>Old name</language></name>"
                "<link_rewrite><language id='1'>old-name</language></link_rewrite>"
                "</product></prestashop>"
            ),
            _response("<prestashop><product><id>44</id></product></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1
        settings.PRESTASHOP_DEFAULT_CATEGORY_ID = 2

        client = PrestashopClient(session=session)

        product_id = client.upsert_product(
            product, prestashop_id=44, category_default_id=2, category_ids=[2]
        )

        assert product_id == 44
        put_call = session.request.call_args_list[1]
        payload = put_call.kwargs["data"]
        assert "manufacturer_name" not in payload
        assert "<quantity>" not in payload
