import json
from decimal import Decimal
from unittest.mock import Mock

import pytest

from apps.catalog.models import (
    AttributeGroup,
    AttributeValue,
    Category,
    CategoryType,
    Combination,
    Manufacturer,
    Price,
    Product,
    TaxRuleMapping,
)
from apps.prestashop.client import PrestashopClient, PrestashopError
from apps.prestashop.services import (
    export_combination,
    export_price,
    export_product,
    format_sync_error,
    resolve_tax_rules_group,
)
from apps.sync.models import SyncJob, SyncJobStatus, SyncJobType
from apps.sync.tasks import export_prices


@pytest.fixture(autouse=True)
def _clean_db():
    SyncJob.objects.all().delete()
    Price.objects.all().delete()
    AttributeValue.objects.all().delete()
    AttributeGroup.objects.all().delete()
    Combination.objects.all().delete()
    Product.objects.all().delete()
    Manufacturer.objects.all().delete()
    TaxRuleMapping.objects.all().delete()
    Category.objects.all().delete()


@pytest.fixture
def _default_category():
    return Category.objects.create(
        prestashop_id=2,
        name="Default",
        category_type=CategoryType.DEFAULT,
    )


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


def _make_price(combination, amount_ex_vat=90.00, vat_rate=21):
    return Price.objects.create(
        combination=combination,
        amount_ex_vat=Decimal(str(amount_ex_vat)),
        vat_rate=Decimal(str(vat_rate)),
    )


def _make_tax_mapping(vat_rate=21, ps_group_id=1, label="IVA 21%"):
    return TaxRuleMapping.objects.create(
        vat_rate=Decimal(str(vat_rate)),
        prestashop_tax_rules_group_id=ps_group_id,
        label=label,
    )


def _make_product_prestashop_id(product, prestashop_product_id):
    product.prestashop_id = prestashop_product_id
    product.save(update_fields=["prestashop_id"])


def _response(payload: str, status_code: int = 200):
    response = Mock()
    response.status_code = status_code
    response.text = payload
    return response


# ─── Tax rule resolution ────────────────────────────────────────────


@pytest.mark.django_db
class TestResolveTaxRulesGroup:
    def test_resolves_mapped_vat_rate(self):
        _make_tax_mapping(vat_rate=21, ps_group_id=1)
        result = resolve_tax_rules_group(21)
        assert result == 1

    def test_resolves_mapped_vat_rate_as_string(self):
        _make_tax_mapping(vat_rate=10, ps_group_id=2)
        result = resolve_tax_rules_group("10")
        assert result == 2

    def test_falls_back_to_default_setting(self, settings):
        settings.PRESTASHOP_DEFAULT_TAX_RULES_GROUP_ID = 99
        result = resolve_tax_rules_group(16.5)
        assert result == 99

    def test_raises_for_unsupported_vat_without_default(self, settings):
        settings.PRESTASHOP_DEFAULT_TAX_RULES_GROUP_ID = None
        with pytest.raises(PrestashopError, match="Unsupported VAT rate"):
            resolve_tax_rules_group(16.5)

    def test_error_message_includes_admin_hint(self, settings):
        settings.PRESTASHOP_DEFAULT_TAX_RULES_GROUP_ID = None
        with pytest.raises(PrestashopError, match="Django admin"):
            resolve_tax_rules_group(7.5)


# ─── Price export service ───────────────────────────────────────────


@pytest.mark.django_db
class TestPriceExport:
    def test_export_price_syncs_product_and_combination(self, _default_category):
        _make_tax_mapping(vat_rate=21, ps_group_id=1)
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        combination = _make_combination(product=product)
        price = _make_price(combination, amount_ex_vat=90.00, vat_rate=21)

        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color",
            prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        client = Mock()
        client.upsert_product.return_value = 22
        client.upsert_combination.return_value = 55

        result = export_price(price.pk, client=client)

        assert result["price_id"] == price.pk
        assert result["product_prestashop_id"] == 22
        assert result["combination_prestashop_id"] == 55
        client.upsert_product.assert_called_once_with(
            product,
            prestashop_id=22,
            tax_rules_group_id=1,
            category_default_id=2,
            category_ids=[2],
        )
        client.upsert_combination.assert_called_once()
        call_kwargs = client.upsert_combination.call_args
        assert call_kwargs.kwargs["price"] == "90.00"

        price.refresh_from_db()
        assert price.sync_required is False
        assert price.last_sync_error == ""

    def test_export_price_passes_vat_to_product(self, _default_category):
        _make_tax_mapping(vat_rate=10, ps_group_id=2)
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        combination = _make_combination(product=product)
        price = _make_price(combination, amount_ex_vat=55.00, vat_rate=10)

        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color",
            prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        client = Mock()
        client.upsert_product.return_value = 22
        client.upsert_combination.return_value = 55

        export_price(price.pk, client=client)

        client.upsert_product.assert_called_once_with(
            product,
            prestashop_id=22,
            tax_rules_group_id=2,
            category_default_id=2,
            category_ids=[2],
        )

    def test_export_price_stores_structured_error(self, _default_category):
        _make_tax_mapping(vat_rate=21, ps_group_id=1)
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        combination = _make_combination(product=product)
        price = _make_price(combination)

        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color",
            prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        client = Mock()
        client.upsert_product.side_effect = PrestashopError(
            "Prestashop returned HTTP 500 for products.",
            status_code=500,
            body="<errors />",
        )

        with pytest.raises(PrestashopError):
            export_price(price.pk, client=client)

        price.refresh_from_db()
        payload = json.loads(price.last_sync_error)
        assert payload["status_code"] == 500
        assert price.sync_required is True

    def test_export_price_fails_on_unsupported_vat(self, settings):
        settings.PRESTASHOP_DEFAULT_TAX_RULES_GROUP_ID = None
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        combination = _make_combination(product=product)
        price = _make_price(combination, vat_rate=16.5)

        client = Mock()

        with pytest.raises(PrestashopError, match="Unsupported VAT rate"):
            export_price(price.pk, client=client)

        price.refresh_from_db()
        payload = json.loads(price.last_sync_error)
        assert "Unsupported VAT rate" in payload["message"]
        assert price.sync_required is True


# ─── Combination price passthrough ──────────────────────────────────


@pytest.mark.django_db
class TestCombinationPricePassthrough:
    def test_combination_export_passes_price_to_client(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        combination = _make_combination(product=product)
        _make_price(combination, amount_ex_vat=90.00, vat_rate=21)

        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color",
            prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        client = Mock()
        client.upsert_combination.return_value = 55

        export_combination(combination.pk, client=client)

        call_kwargs = client.upsert_combination.call_args
        assert call_kwargs.kwargs["price"] == "90.00"

    def test_combination_export_defaults_to_zero_when_no_price(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        combination = _make_combination(product=product)

        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color",
            prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        client = Mock()
        client.upsert_combination.return_value = 55

        export_combination(combination.pk, client=client)

        call_kwargs = client.upsert_combination.call_args
        assert call_kwargs.kwargs["price"] == "0"


# ─── Product tax_rules_group passthrough ────────────────────────────


@pytest.mark.django_db
class TestProductTaxRulesGroup:
    def test_product_export_sets_tax_rules_group(self, _default_category):
        _make_tax_mapping(vat_rate=21, ps_group_id=1)
        product = _make_product()

        client = Mock()
        client.find_product_id_by_reference.return_value = None
        client.upsert_product.return_value = 77

        export_product(product.pk, client=client, tax_rules_group_id=1)

        client.upsert_product.assert_called_once_with(
            product,
            prestashop_id=None,
            tax_rules_group_id=1,
            category_default_id=2,
            category_ids=[2],
        )

    def test_product_export_without_tax_rules_group(self, _default_category):
        product = _make_product()

        client = Mock()
        client.find_product_id_by_reference.return_value = None
        client.upsert_product.return_value = 77

        export_product(product.pk, client=client)

        client.upsert_product.assert_called_once_with(
            product,
            prestashop_id=None,
            tax_rules_group_id=None,
            category_default_id=2,
            category_ids=[2],
        )


# ─── PrestaShop client tax rules group search ──────────────────────


@pytest.mark.django_db
class TestPrestashopClientTaxRulesGroup:
    def test_find_tax_rules_group_by_name(self, settings):
        response = _response(
            "<prestashop><tax_rules_groups>"
            "<tax_rules_group id='1' />"
            "</tax_rules_groups></prestashop>"
        )
        session = Mock()
        session.request.return_value = response
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)
        group_id = client.find_tax_rules_group_id_by_name("IVA 21%")

        assert group_id == 1
        assert session.request.call_args.kwargs["params"] == {
            "filter[name]": "[IVA 21%]",
            "limit": "1",
        }

    def test_find_tax_rules_group_returns_none_when_not_found(self, settings):
        response = _response("<prestashop><tax_rules_groups></tax_rules_groups></prestashop>")
        session = Mock()
        session.request.return_value = response
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)
        group_id = client.find_tax_rules_group_id_by_name("Nonexistent")

        assert group_id is None

    def test_find_tax_rules_group_rejects_reserved_characters(self, settings):
        session = Mock()
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)

        with pytest.raises(PrestashopError, match="Unsupported tax rules group name characters"):
            client.find_tax_rules_group_id_by_name("IVA|21%")

        session.request.assert_not_called()


# ─── Client upsert_product with tax_rules_group_id ─────────────────


@pytest.mark.django_db
class TestPrestashopClientProductTaxRules:
    def test_upsert_product_sets_tax_rules_group(self, settings):
        product = _make_product()
        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><product>"
                "<id_category_default></id_category_default>"
                "<id_manufacturer></id_manufacturer>"
                "<id_tax_rules_group></id_tax_rules_group>"
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

        client.upsert_product(product, tax_rules_group_id=1)

        post_call = session.request.call_args_list[1]
        payload = post_call.kwargs["data"]
        assert "<id_tax_rules_group>1</id_tax_rules_group>" in payload

    def test_upsert_product_omits_tax_rules_group_when_none(self, settings):
        product = _make_product()
        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><product>"
                "<id_category_default></id_category_default>"
                "<id_manufacturer></id_manufacturer>"
                "<id_tax_rules_group></id_tax_rules_group>"
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

        client.upsert_product(product)

        post_call = session.request.call_args_list[1]
        payload = post_call.kwargs["data"]
        # When tax_rules_group_id is None, the field is not explicitly set,
        # so it stays as whatever the blank schema provided (empty).
        assert (
            "<id_tax_rules_group />" in payload
            or "<id_tax_rules_group></id_tax_rules_group>" in payload
        )


# ─── Price export Celery task ───────────────────────────────────────


@pytest.mark.django_db
class TestPriceExportTask:
    def test_task_exports_pending_prices(self, monkeypatch):
        _make_tax_mapping(vat_rate=21, ps_group_id=1)
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        combination = _make_combination(product=product)
        price = _make_price(combination, amount_ex_vat=90.00, vat_rate=21)

        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color",
            prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        def fake_export_price(price_id):
            p = Price.objects.get(pk=price_id)
            p.sync_required = False
            p.last_sync_error = ""
            p.last_synced_at = p.updated_at
            p.save()
            return {
                "price_id": price_id,
                "product_prestashop_id": 22,
                "combination_prestashop_id": 55,
            }

        monkeypatch.setattr("apps.sync.tasks.export_price", fake_export_price)

        result = export_prices()

        assert result == {"status": "success", "processed": 1, "failed": 0}
        assert SyncJob.objects.filter(job_type=SyncJobType.EXPORT_PRICE).count() == 1
        assert SyncJob.objects.filter(status=SyncJobStatus.SUCCEEDED).count() == 1
        price.refresh_from_db()
        assert price.sync_required is False

    def test_task_marks_job_failed_when_export_raises(self, monkeypatch):
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        combination = _make_combination(product=product)
        price = _make_price(combination)

        def fake_export_price(price_id):
            p = Price.objects.get(pk=price_id)
            p.last_sync_error = format_sync_error(PrestashopError("boom", status_code=503))
            p.save(update_fields=["last_sync_error", "updated_at"])
            raise PrestashopError("boom", status_code=503)

        monkeypatch.setattr("apps.sync.tasks.export_price", fake_export_price)

        result = export_prices()

        price.refresh_from_db()
        job = SyncJob.objects.get(job_type=SyncJobType.EXPORT_PRICE)
        assert result == {"status": "success", "processed": 0, "failed": 1}
        assert job.status == SyncJobStatus.PENDING
        assert job.attempts == 2
        assert json.loads(price.last_sync_error)["status_code"] == 503

    def test_task_skips_prices_without_sync_required(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        combination = _make_combination(product=product)
        price = _make_price(combination)
        price.sync_required = False
        price.save(update_fields=["sync_required"])

        result = export_prices()

        assert result == {"status": "success", "processed": 0, "failed": 0}
        assert SyncJob.objects.count() == 0
