import json
from unittest.mock import Mock

import pytest

from apps.catalog.models import (
    AttributeGroup,
    AttributeValue,
    Combination,
    Manufacturer,
    Product,
)
from apps.prestashop.attribute_groups import expected_local_attribute_group_name
from apps.prestashop.client import PrestashopClient, PrestashopError
from apps.prestashop.services import (
    ensure_attribute_group,
    ensure_attribute_value,
    export_combination,
    format_sync_error,
)
from apps.sync.models import SyncJob, SyncJobStatus, SyncJobType
from apps.sync.tasks import export_combinations


@pytest.fixture(autouse=True)
def _clean_db():
    SyncJob.objects.all().delete()
    AttributeValue.objects.all().delete()
    AttributeGroup.objects.all().delete()
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


def _make_product_prestashop_id(product, prestashop_product_id):
    product.prestashop_id = prestashop_product_id
    product.save(update_fields=["prestashop_id"])


def _response(payload: str, status_code: int = 200):
    response = Mock()
    response.status_code = status_code
    response.text = payload
    return response


def _blank_combination_xml():
    return (
        "<prestashop><combination>"
        "<id_product></id_product>"
        "<ean13></ean13>"
        "<active></active>"
        "<price></price>"
        "<minimal_quantity></minimal_quantity>"
        "<associations><product_option_values></product_option_values></associations>"
        "</combination></prestashop>"
    )


def _existing_combination_xml(psid=55):
    return (
        f"<prestashop><combination>"
        f"<id>{psid}</id>"
        "<id_product>22</id_product>"
        "<ean13>1234567890123</ean13>"
        "<active>1</active>"
        "<price>0</price>"
        "<minimal_quantity>1</minimal_quantity>"
        "<associations><product_option_values>"
        "<product_option_value><id>1</id></product_option_value>"
        "<product_option_value><id>2</id></product_option_value>"
        "</product_option_values></associations>"
        "</combination></prestashop>"
    )


# ─── Attribute Group / Value helpers ────────────────────────────────


@pytest.mark.django_db
class TestEnsureAttributeGroup:
    def test_creates_group_when_not_in_db(self):
        client = _make_mock_client()
        client.find_attribute_group_id_by_name.return_value = None
        client.create_attribute_group.return_value = 10

        ps_id = ensure_attribute_group("size", client=client)

        assert ps_id == 10
        ag = AttributeGroup.objects.get(icg_type="size", product__isnull=True)
        assert ag.prestashop_id == 10
        assert ag.name == "Size"
        client.create_attribute_group.assert_called_once_with("Size", is_color_group=False)

    def test_reuses_existing_db_group(self):
        AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=42)
        client = _make_mock_client()

        ps_id = ensure_attribute_group("size", client=client)

        assert ps_id == 42
        client.find_attribute_group_id_by_name.assert_not_called()
        client.create_attribute_group.assert_not_called()

    def test_reuses_existing_ps_group_not_in_db(self):
        client = _make_mock_client()
        client.find_attribute_group_id_by_name.return_value = 99

        ps_id = ensure_attribute_group("size", client=client)

        assert ps_id == 99
        client.create_attribute_group.assert_not_called()
        ag = AttributeGroup.objects.get(icg_type="size", product__isnull=True)
        assert ag.prestashop_id == 99

    def test_color_group_requires_product(self):
        client = _make_mock_client()

        with pytest.raises(PrestashopError, match="require a product"):
            ensure_attribute_group("color", client=client)

    def test_color_group_creates_per_product(self):
        product = _make_product()
        _make_product_prestashop_id(product, 2945)
        client = _make_mock_client()
        client.list_attribute_groups.return_value = []
        client.find_attribute_group_id_by_name.return_value = None
        client.create_attribute_group.return_value = 20

        ps_id = ensure_attribute_group("color", client=client, product=product)

        assert ps_id == 20
        ag = AttributeGroup.objects.get(icg_type="color", product=product)
        assert ag.name == "2945_color"
        client.create_attribute_group.assert_called_once_with("2945_color", is_color_group=True)

    def test_color_group_reuses_existing_db_group(self):
        product = _make_product()
        AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color", prestashop_id=30, product=product
        )
        client = _make_mock_client()

        ps_id = ensure_attribute_group("color", client=client, product=product)

        assert ps_id == 30
        client.find_attribute_group_id_by_name.assert_not_called()

    def test_color_group_prefers_existing_prestashop_product_id_group(self):
        product = _make_product(reference="2830001")
        _make_product_prestashop_id(product, 2945)
        client = _make_mock_client()
        client.list_attribute_groups.return_value = [
            {"ps_id": 77, "name": "2945_color"},
            {"ps_id": 78, "name": "2830001_color"},
        ]

        ps_id = ensure_attribute_group("color", client=client, product=product)

        assert ps_id == 77
        ag = AttributeGroup.objects.get(icg_type="color", product=product)
        assert ag.prestashop_id == 77
        assert ag.name == "2945_color"
        client.create_attribute_group.assert_not_called()

    def test_size_group_prefers_existing_product_specific_remote_group(self):
        product = _make_product(reference="2830001")
        _make_product_prestashop_id(product, 2945)
        client = _make_mock_client()
        client.list_attribute_groups.return_value = [
            {"ps_id": 10, "name": "Size"},
            {"ps_id": 11, "name": "2945_talla"},
        ]

        ps_id = ensure_attribute_group("size", client=client, product=product)

        assert ps_id == 11
        ag = AttributeGroup.objects.get(icg_type="size", product=product)
        assert ag.prestashop_id == 11
        assert ag.name == "2945_talla"
        client.create_attribute_group.assert_not_called()

    def test_size_group_falls_back_to_global_size_group_when_no_specific_remote_exists(self):
        product = _make_product(reference="2830001")
        _make_product_prestashop_id(product, 2945)
        AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=42)
        client = _make_mock_client()
        client.list_attribute_groups.return_value = [{"ps_id": 42, "name": "Size"}]

        ps_id = ensure_attribute_group("size", client=client, product=product)

        assert ps_id == 42
        assert AttributeGroup.objects.filter(icg_type="size", product=product).exists() is False
        client.create_attribute_group.assert_not_called()

    def test_expected_local_color_group_name_prefers_prestashop_id(self):
        product = _make_product(reference="0110026")
        _make_product_prestashop_id(product, 2574)

        name = expected_local_attribute_group_name("color", product)

        assert name == "2574_color"

    def test_expected_local_color_group_name_falls_back_to_reference(self):
        product = _make_product(reference="0110026")

        name = expected_local_attribute_group_name("color", product)

        assert name == "0110026_color"

    def test_color_group_revalidates_cached_mismatched_name(self):
        product = _make_product(reference="0110026")
        _make_product_prestashop_id(product, 2574)
        stale_ag = AttributeGroup.objects.create(
            icg_type="color",
            name="0110026_color",
            prestashop_id=30,
            product=product,
        )
        AttributeValue.objects.create(
            attribute_group=stale_ag,
            icg_value="Azul",
            name="Azul",
            prestashop_id=999,
        )
        client = _make_mock_client()
        client.list_attribute_groups.return_value = [
            {"ps_id": 77, "name": "2574_color"},
            {"ps_id": 78, "name": "0110026_color"},
        ]

        ps_id = ensure_attribute_group("color", client=client, product=product)

        assert ps_id == 77
        ag = AttributeGroup.objects.get(icg_type="color", product=product)
        assert ag.prestashop_id == 77
        assert ag.name == "2574_color"
        assert not AttributeValue.objects.filter(attribute_group=ag).exists()

    def test_color_group_keeps_cached_when_preferred_remote_not_found(self):
        product = _make_product(reference="0110026")
        _make_product_prestashop_id(product, 2574)
        AttributeGroup.objects.create(
            icg_type="color",
            name="0110026_color",
            prestashop_id=30,
            product=product,
        )
        client = _make_mock_client()
        client.list_attribute_groups.return_value = [
            {"ps_id": 78, "name": "0110026_color"},
        ]

        ps_id = ensure_attribute_group("color", client=client, product=product)

        assert ps_id == 30
        ag = AttributeGroup.objects.get(icg_type="color", product=product)
        assert ag.prestashop_id == 30
        assert ag.name == "0110026_color"

    def test_color_group_skips_revalidation_when_name_already_matches(self):
        product = _make_product(reference="0110026")
        _make_product_prestashop_id(product, 2574)
        AttributeGroup.objects.create(
            icg_type="color",
            name="2574_color",
            prestashop_id=77,
            product=product,
        )
        client = _make_mock_client()

        ps_id = ensure_attribute_group("color", client=client, product=product)

        assert ps_id == 77
        client.list_attribute_groups.assert_not_called()

    def test_color_group_keeps_cached_when_preferred_ps_id_conflicts(self):
        mfr_a = _make_manufacturer(icg_code="M-2001", prestashop_id=201)
        product_a = _make_product(reference="REF_A", icg_id=2001, manufacturer=mfr_a)
        _make_product_prestashop_id(product_a, 100)
        mfr_b = _make_manufacturer(icg_code="M-2002", prestashop_id=202)
        product_b = _make_product(reference="REF_B", icg_id=2002, manufacturer=mfr_b)
        _make_product_prestashop_id(product_b, 200)
        AttributeGroup.objects.create(
            icg_type="color",
            name="100_color",
            prestashop_id=77,
            product=product_a,
        )
        AttributeGroup.objects.create(
            icg_type="color",
            name="REF_B_color",
            prestashop_id=30,
            product=product_b,
        )
        client = _make_mock_client()
        client.list_attribute_groups.return_value = [
            {"ps_id": 77, "name": "200_color"},
            {"ps_id": 78, "name": "REF_B_color"},
        ]

        ps_id = ensure_attribute_group("color", client=client, product=product_b)

        assert ps_id == 30
        ag = AttributeGroup.objects.get(icg_type="color", product=product_b)
        assert ag.prestashop_id == 30
        assert ag.name == "REF_B_color"

    def test_size_group_never_triggers_color_revalidation(self):
        product = _make_product(reference="REF001")
        _make_product_prestashop_id(product, 2574)
        AttributeGroup.objects.create(
            icg_type="size",
            name="2574_talla",
            prestashop_id=50,
            product=product,
        )
        client = _make_mock_client()

        ps_id = ensure_attribute_group("size", client=client, product=product)

        assert ps_id == 50
        client.list_attribute_groups.assert_not_called()


@pytest.mark.django_db
class TestEnsureAttributeValue:
    def test_creates_value_when_not_in_db(self):
        ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        client = _make_mock_client()
        client.find_attribute_value_id.return_value = None
        client.create_attribute_value.return_value = 100

        ps_id = ensure_attribute_value(10, "M", client=client)

        assert ps_id == 100
        av = AttributeValue.objects.get(attribute_group=ag, icg_value="M")
        assert av.prestashop_id == 100

    def test_reuses_existing_db_value(self):
        ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        AttributeValue.objects.create(attribute_group=ag, icg_value="M", name="M", prestashop_id=55)
        client = _make_mock_client()

        ps_id = ensure_attribute_value(10, "M", client=client)

        assert ps_id == 55
        client.find_attribute_value_id.assert_not_called()

    def test_reuses_existing_ps_value_not_in_db(self):
        ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        client = _make_mock_client()
        client.find_attribute_value_id.return_value = 77

        ps_id = ensure_attribute_value(10, "L", client=client)

        assert ps_id == 77
        client.create_attribute_value.assert_not_called()
        av = AttributeValue.objects.get(attribute_group=ag, icg_value="L")
        assert av.prestashop_id == 77

    def test_idempotent_consecutive_calls_only_create_once(self):
        ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        client = _make_mock_client()
        client.find_attribute_value_id.return_value = None
        client.create_attribute_value.return_value = 55

        ps_id1 = ensure_attribute_value(10, "M", client=client)
        ps_id2 = ensure_attribute_value(10, "M", client=client)

        assert ps_id1 == 55
        assert ps_id2 == 55
        assert client.create_attribute_value.call_count == 1
        av = AttributeValue.objects.get(attribute_group=ag, icg_value="M")
        assert av.prestashop_id == 55

    def test_idempotent_creation(self):
        """Two sequential calls with the same params should only create one PS entry.
        Tests idempotency after the first worker has released the lock."""
        ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        client_a = Mock()
        client_a.find_attribute_value_id.return_value = None
        client_a.create_attribute_value.return_value = 100

        client_b = Mock()
        client_b.find_attribute_value_id.return_value = None

        ps_id_a = ensure_attribute_value(10, "XL", client=client_a)

        client_b.create_attribute_value.return_value = 101
        ps_id_b = ensure_attribute_value(10, "XL", client=client_b)

        assert ps_id_a == 100
        assert ps_id_b == 100
        assert client_a.create_attribute_value.call_count == 1
        assert client_b.create_attribute_value.call_count == 0
        av = AttributeValue.objects.get(attribute_group=ag, icg_value="XL")
        assert av.prestashop_id == 100


# ─── Combination export service ─────────────────────────────────────


def _make_mock_client(**overrides):
    client = Mock(**overrides)
    client.list_combinations_for_product.return_value = []
    return client


@pytest.mark.django_db
class TestCombinationExport:
    def test_export_creates_and_maps_new_combination(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)

        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color", prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        combination = _make_combination(
            product=product, icg_size="M", icg_color="Red", ean13="9788478290222"
        )

        client = _make_mock_client()
        client.upsert_combination.return_value = 55

        result = export_combination(combination.pk, client=client)

        assert result == {"combination_id": combination.pk, "prestashop_combination_id": 55}
        combination.refresh_from_db()
        assert combination.prestashop_id == 55
        assert combination.sync_required is False
        assert combination.last_sync_error == ""
        client.upsert_combination.assert_called_once_with(
            22,
            "9788478290222",
            True,
            [100, 200],
            prestashop_id=None,
            price="0",
        )

    def test_export_sends_empty_ean13_for_non_digit_ean(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)

        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color", prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        combination = _make_combination(product=product, icg_size="M", icg_color="Red", ean13="***")

        client = _make_mock_client()
        client.upsert_combination.return_value = 55

        result = export_combination(combination.pk, client=client)

        assert result == {"combination_id": combination.pk, "prestashop_combination_id": 55}
        combination.refresh_from_db()
        assert combination.prestashop_id == 55
        assert combination.sync_required is False
        client.upsert_combination.assert_called_once_with(
            22,
            "",
            True,
            [100, 200],
            prestashop_id=None,
            price="0",
        )

    def test_export_ignores_placeholder_size_attribute(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)

        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color", prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        combination = _make_combination(product=product, icg_size="***", icg_color="Red")

        client = _make_mock_client()
        client.upsert_combination.return_value = 55

        result = export_combination(combination.pk, client=client)

        assert result == {"combination_id": combination.pk, "prestashop_combination_id": 55}
        client.upsert_combination.assert_called_once_with(
            22,
            "",
            True,
            [200],
            prestashop_id=None,
            price="0",
        )

    def test_export_preserves_existing_placeholder_structure_when_mapped(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)

        AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=50, product=None)

        combination = _make_combination(product=product, icg_size="***", icg_color="***")
        combination.prestashop_id = 55
        combination.save(update_fields=["prestashop_id"])

        client = _make_mock_client()
        client.find_attribute_value_id.return_value = 300
        client.upsert_combination.return_value = 55

        result = export_combination(combination.pk, client=client)

        assert result == {"combination_id": combination.pk, "prestashop_combination_id": 55}
        client.upsert_combination.assert_called_once_with(
            22,
            "",
            True,
            [300],
            prestashop_id=55,
            price="0",
        )
        combination.refresh_from_db()
        assert combination.sync_required is False

    def test_export_creates_asterisk_size_combination_when_both_axes_are_placeholders(
        self,
    ):
        product = _make_product()
        _make_product_prestashop_id(product, 22)

        size_ag = AttributeGroup.objects.create(
            icg_type="size", name="Size", prestashop_id=50, product=None
        )

        combination = _make_combination(product=product, icg_size="***", icg_color="***")

        client = _make_mock_client()
        client.find_attribute_value_id.return_value = None
        client.create_attribute_value.return_value = 300
        client.upsert_combination.return_value = 100

        result = export_combination(combination.pk, client=client)

        assert result["prestashop_combination_id"] == 100
        client.upsert_combination.assert_called_once_with(
            22,
            "",
            True,
            [300],
            prestashop_id=None,
            price="0",
        )
        combination.refresh_from_db()
        assert combination.prestashop_id == 100
        assert combination.sync_required is False

        unique_av = AttributeValue.objects.filter(attribute_group=size_ag, icg_value="***").first()
        assert unique_av is not None
        assert unique_av.prestashop_id == 300

    def test_export_cleans_single_placeholder_axis_even_when_mapped(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)

        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color", prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        combination = _make_combination(product=product, icg_size="***", icg_color="Red")
        combination.prestashop_id = 55
        combination.save(update_fields=["prestashop_id"])

        client = _make_mock_client()
        client.upsert_combination.return_value = 55

        result = export_combination(combination.pk, client=client)

        assert result == {"combination_id": combination.pk, "prestashop_combination_id": 55}
        client.upsert_combination.assert_called_once_with(
            22,
            "",
            True,
            [200],
            prestashop_id=55,
            price="0",
        )

    def test_export_updates_existing_mapped_combination(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)

        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color", prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        combination = _make_combination(product=product, ean13="9788478290222")
        combination.prestashop_id = 88
        combination.save(update_fields=["prestashop_id"])

        client = _make_mock_client()
        client.upsert_combination.return_value = 88

        export_combination(combination.pk, client=client)

        client.upsert_combination.assert_called_once_with(
            22,
            "9788478290222",
            True,
            [100, 200],
            prestashop_id=88,
            price="0",
        )

    def test_export_creates_attribute_groups_and_values(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        combination = _make_combination(product=product)

        client = _make_mock_client()
        client.find_attribute_group_id_by_name.return_value = None
        client.create_attribute_group.side_effect = [10, 11]
        client.find_attribute_value_id.return_value = None
        client.create_attribute_value.side_effect = [100, 200]
        client.upsert_combination.return_value = 55

        export_combination(combination.pk, client=client)

        assert AttributeGroup.objects.count() == 2
        assert AttributeValue.objects.count() == 2
        assert client.create_attribute_group.call_count == 2
        assert client.create_attribute_value.call_count == 2

        size_ag = AttributeGroup.objects.get(icg_type="size", product__isnull=True)
        color_ag = AttributeGroup.objects.get(icg_type="color", product=product)
        assert size_ag.name == "Size"
        assert color_ag.name == "22_color"

    def test_export_requires_product_mapping(self):
        product = _make_product()
        combination = _make_combination(product=product)
        client = _make_mock_client()

        with pytest.raises(PrestashopError, match="must be exported before"):
            export_combination(combination.pk, client=client)

        combination.refresh_from_db()
        payload = json.loads(combination.last_sync_error)
        assert "must be exported before" in payload["message"]
        assert combination.sync_required is True

    def test_export_deactivates_inactive_combination(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        combination = _make_combination(product=product, active=False)
        combination.prestashop_id = 77
        combination.save(update_fields=["prestashop_id"])

        client = _make_mock_client()

        export_combination(combination.pk, client=client)

        client.deactivate_combination.assert_called_once_with(77)
        client.upsert_combination.assert_not_called()
        combination.refresh_from_db()
        assert combination.sync_required is False

    def test_export_inactive_combination_without_product_mapping_succeeds(self):
        product = _make_product()
        combination = _make_combination(product=product, active=False)

        client = _make_mock_client()

        result = export_combination(combination.pk, client=client)

        assert result == {"combination_id": combination.pk, "prestashop_combination_id": 0}
        combination.refresh_from_db()
        assert combination.sync_required is False
        assert combination.last_sync_error == ""
        client.deactivate_combination.assert_not_called()
        client.upsert_combination.assert_not_called()

    def test_export_stores_structured_error(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color", prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )
        combination = _make_combination(product=product)

        client = _make_mock_client()
        client.upsert_combination.side_effect = PrestashopError(
            "Prestashop returned HTTP 500 for combinations.",
            status_code=500,
            body="<errors />",
        )

        with pytest.raises(PrestashopError):
            export_combination(combination.pk, client=client)

        combination.refresh_from_db()
        payload = json.loads(combination.last_sync_error)
        assert payload["status_code"] == 500
        assert combination.sync_required is True

    def test_export_recovers_when_combination_deleted_from_prestashop(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)

        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color", prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        combination = _make_combination(product=product, icg_size="M", icg_color="Red")
        combination.prestashop_id = 88
        combination.save(update_fields=["prestashop_id"])

        client = _make_mock_client()
        call_count = 0

        def upsert_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("prestashop_id") == 88:
                raise PrestashopError(
                    "Prestashop returned HTTP 404 for combinations.",
                    status_code=404,
                )
            return 55

        client.upsert_combination.side_effect = upsert_side_effect

        result = export_combination(combination.pk, client=client)

        combination.refresh_from_db()
        assert result == {"combination_id": combination.pk, "prestashop_combination_id": 55}
        assert combination.prestashop_id == 55
        assert combination.sync_required is False
        assert call_count == 2

    def test_export_does_not_recover_combination_on_non_404_error(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)

        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color", prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        combination = _make_combination(product=product, icg_size="M", icg_color="Red")
        combination.prestashop_id = 88
        combination.save(update_fields=["prestashop_id"])

        client = _make_mock_client()
        client.upsert_combination.side_effect = PrestashopError(
            "Prestashop returned HTTP 500 for combinations.",
            status_code=500,
        )

        with pytest.raises(PrestashopError):
            export_combination(combination.pk, client=client)

        combination.refresh_from_db()
        assert combination.prestashop_id == 88
        assert combination.sync_required is True

    def test_export_maps_to_existing_combination_by_attribute_ids(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)

        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color", prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        combination = _make_combination(
            product=product, icg_size="M", icg_color="Red", ean13="9788478290222"
        )
        assert combination.prestashop_id is None

        existing_ps_combination = Mock()
        existing_ps_combination.combination_id = 77
        existing_ps_combination.attribute_value_ids = [100, 200]

        client = _make_mock_client()
        client.list_combinations_for_product.return_value = [existing_ps_combination]
        client.upsert_combination.return_value = 77

        result = export_combination(combination.pk, client=client)

        assert result == {"combination_id": combination.pk, "prestashop_combination_id": 77}
        combination.refresh_from_db()
        assert combination.prestashop_id == 77
        assert combination.sync_required is False
        client.upsert_combination.assert_called_once_with(
            22,
            "9788478290222",
            True,
            [100, 200],
            prestashop_id=77,
            price="0",
        )

    def test_export_creates_new_when_no_existing_match_by_attribute_ids(self):
        product = _make_product()
        _make_product_prestashop_id(product, 22)

        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color", prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )

        combination = _make_combination(
            product=product, icg_size="M", icg_color="Red", ean13="9788478290222"
        )

        different_combination = Mock()
        different_combination.combination_id = 77
        different_combination.attribute_value_ids = [300, 400]

        client = _make_mock_client()
        client.list_combinations_for_product.return_value = [different_combination]
        client.upsert_combination.return_value = 99

        result = export_combination(combination.pk, client=client)

        assert result == {"combination_id": combination.pk, "prestashop_combination_id": 99}
        combination.refresh_from_db()
        assert combination.prestashop_id == 99
        assert combination.sync_required is False
        client.upsert_combination.assert_called_once_with(
            22,
            "9788478290222",
            True,
            [100, 200],
            prestashop_id=None,
            price="0",
        )


# ─── Combination export task ────────────────────────────────────────


@pytest.mark.django_db
class TestCombinationExportTask:
    def test_task_exports_pending_combinations(self, monkeypatch):
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        size_ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        AttributeValue.objects.create(
            attribute_group=size_ag, icg_value="M", name="M", prestashop_id=100
        )
        color_ag = AttributeGroup.objects.create(
            icg_type="color", name=f"{product.reference}_color", prestashop_id=11, product=product
        )
        AttributeValue.objects.create(
            attribute_group=color_ag, icg_value="Red", name="Red", prestashop_id=200
        )
        _make_combination(product=product, icg_size="M", icg_color="Red", ean13="111")
        _make_combination(product=product, icg_size="M", icg_color="Blue", ean13="222")

        def fake_export(combination_id, client=None):
            c = Combination.objects.get(pk=combination_id)
            c.prestashop_id = c.pk + 100
            c.sync_required = False
            c.last_sync_error = ""
            c.last_synced_at = c.updated_at
            c.save()
            return {"combination_id": combination_id, "prestashop_combination_id": c.pk + 100}

        monkeypatch.setattr("apps.sync.tasks.export_combination", fake_export)

        result = export_combinations()

        assert result == {"status": "success", "processed": 2, "failed": 0}
        assert SyncJob.objects.filter(job_type=SyncJobType.EXPORT_COMBINATION).count() == 2
        assert SyncJob.objects.filter(status=SyncJobStatus.SUCCEEDED).count() == 2

    def test_task_marks_job_failed_when_export_raises(self, monkeypatch):
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        combination = _make_combination(product=product)

        def fake_export(combination_id, client=None):
            c = Combination.objects.get(pk=combination_id)
            c.last_sync_error = format_sync_error(PrestashopError("boom", status_code=503))
            c.save(update_fields=["last_sync_error", "updated_at"])
            raise PrestashopError("boom", status_code=503)

        monkeypatch.setattr("apps.sync.tasks.export_combination", fake_export)

        result = export_combinations()

        combination.refresh_from_db()
        job = SyncJob.objects.get(job_type=SyncJobType.EXPORT_COMBINATION)
        assert result == {"status": "success", "processed": 0, "failed": 1}
        assert job.status == SyncJobStatus.PENDING
        assert job.attempts == 2
        assert json.loads(combination.last_sync_error)["status_code"] == 503

    def test_failed_combination_moves_behind_older_pending_rows(self, monkeypatch):
        product = _make_product()
        _make_product_prestashop_id(product, 22)
        first = _make_combination(product=product, icg_size="M", icg_color="Red")
        second = _make_combination(product=product, icg_size="L", icg_color="Blue")

        def fake_export(combination_id, client=None):
            combination = Combination.objects.get(pk=combination_id)
            combination.last_sync_error = format_sync_error(
                PrestashopError("boom", status_code=503)
            )
            combination.save(update_fields=["last_sync_error", "updated_at"])
            raise PrestashopError("boom", status_code=503)

        monkeypatch.setattr("apps.sync.tasks.export_combination", fake_export)

        first_result = export_combinations(limit=1)
        second_result = export_combinations(limit=1)

        assert first_result == {"status": "success", "processed": 0, "failed": 1}
        assert second_result == {"status": "success", "processed": 0, "failed": 1}
        assert list(
            SyncJob.objects.order_by("created_at").values_list("entity_key", flat=True)
        ) == [
            f"{first.product.reference}/{first.icg_size}/{first.icg_color}",
            f"{second.product.reference}/{second.icg_size}/{second.icg_color}",
        ]


# ─── PrestaShopClient combination methods ──────────────────────────


@pytest.mark.django_db
class TestPrestashopClientCombinationExport:
    def test_upsert_combination_creates_new(self, settings):
        session = Mock()
        session.request.side_effect = [
            _response(_blank_combination_xml()),
            _response("<prestashop><combination><id>55</id></combination></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        client = PrestashopClient(session=session)
        comb_id = client.upsert_combination(22, "1234567890123", True, [100, 200])

        assert comb_id == 55
        post_call = session.request.call_args_list[1]
        payload = post_call.kwargs["data"]
        assert "<id_product>22</id_product>" in payload
        assert "<ean13>1234567890123</ean13>" in payload
        assert "<active>1</active>" in payload
        assert "<id>100</id>" in payload
        assert "<id>200</id>" in payload

    def test_upsert_combination_updates_existing(self, settings):
        session = Mock()
        session.request.side_effect = [
            _response(_existing_combination_xml(55)),
            _response("<prestashop><combination><id>55</id></combination></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        client = PrestashopClient(session=session)
        comb_id = client.upsert_combination(22, "9999999999999", True, [300, 400], prestashop_id=55)

        assert comb_id == 55
        put_call = session.request.call_args_list[1]
        assert put_call.args[0] == "PUT"
        payload = put_call.kwargs["data"]
        assert "<id>300</id>" in payload
        assert "<id>400</id>" in payload

    def test_deactivate_combination(self, settings):
        session = Mock()
        session.request.side_effect = [
            _response(_existing_combination_xml(55)),
            _response("<prestashop><combination><id>55</id></combination></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        client = PrestashopClient(session=session)
        client.deactivate_combination(55)

        put_call = session.request.call_args_list[1]
        assert put_call.args[0] == "PUT"
        payload = put_call.kwargs["data"]
        assert "<active>0</active>" in payload
        from xml.etree import ElementTree

        root = ElementTree.fromstring(payload)
        comb_active = root.find("./combination/active")
        assert comb_active is not None
        assert comb_active.text == "0"
        root_active = root.find("./active")
        assert root_active is None

    def test_find_attribute_group_uses_exact_match_filter(self, settings):
        response = _response(
            "<prestashop><product_options><product_option id='10' /></product_options></prestashop>"
        )
        session = Mock()
        session.request.return_value = response
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)
        group_id = client.find_attribute_group_id_by_name("Size")

        assert group_id == 10
        assert session.request.call_args.kwargs["params"] == {
            "filter[name]": "[Size]",
            "limit": "1",
        }

    def test_create_attribute_group(self, settings):
        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><product_option>"
                "<id></id>"
                "<is_color_group></is_color_group>"
                "<group_type></group_type>"
                "<position></position>"
                "<name><language id='1'></language></name>"
                "<public_name><language id='1'></language></public_name>"
                "</product_option></prestashop>"
            ),
            _response("<prestashop><product_option><id>10</id></product_option></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        client = PrestashopClient(session=session)
        group_id = client.create_attribute_group("Size")

        assert group_id == 10
        post_call = session.request.call_args_list[1]
        payload = post_call.kwargs["data"]
        assert "<is_color_group>0</is_color_group>" in payload
        assert "<group_type>select</group_type>" in payload
        assert "<position>1</position>" in payload

    def test_create_attribute_group_as_color_group(self, settings):
        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><product_option>"
                "<id></id>"
                "<is_color_group></is_color_group>"
                "<group_type></group_type>"
                "<position></position>"
                "<name><language id='1'></language></name>"
                "<public_name><language id='1'></language></public_name>"
                "</product_option></prestashop>"
            ),
            _response("<prestashop><product_option><id>20</id></product_option></prestashop>"),
        ]
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        client = PrestashopClient(session=session)
        group_id = client.create_attribute_group("REF001_color", is_color_group=True)

        assert group_id == 20
        post_call = session.request.call_args_list[1]
        payload = post_call.kwargs["data"]
        assert "<is_color_group>1</is_color_group>" in payload
        assert "<group_type>color</group_type>" in payload
