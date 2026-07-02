import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.catalog.models import Combination, Manufacturer, Product
from apps.prestashop.client import (
    PrestashopCombinationSummary,
    PrestashopProductSummary,
    PrestashopSpecificPriceSummary,
)
from apps.sync.management.commands.inventory_prestashop_catalog import (
    _group_role,
    _group_scope,
)


@pytest.fixture
def _clean_db():
    Combination.objects.all().delete()
    Product.objects.all().delete()
    Manufacturer.objects.all().delete()


def _make_product(reference="REF001", manufacturer_name="Talens"):
    manufacturer = Manufacturer.objects.create(
        icg_code=f"{reference}-M",
        name=manufacturer_name,
    )
    return Product.objects.create(
        icg_id=1000 + Product.objects.count(),
        reference=reference,
        name=f"Product {reference}",
        manufacturer=manufacturer,
    )


class TestInventoryPrestashopCatalogHelpers:
    def test_group_role_detects_size_and_color_patterns(self):
        assert _group_role("Size") == "size"
        assert _group_role("123_talla") == "size"
        assert _group_role("REF001_color") == "color"
        assert _group_role("Unknown") == "unknown"

    def test_group_scope_classifies_supported_patterns(self):
        assert _group_scope("Size", product_count=3, manufacturer_count=2) == "global"
        assert (
            _group_scope("REF001_color", product_count=1, manufacturer_count=1) == "product-scoped"
        )
        assert (
            _group_scope("TALENS_talla", product_count=3, manufacturer_count=1)
            == "manufacturer-scoped"
        )
        assert (
            _group_scope("legacy_group", product_count=3, manufacturer_count=2)
            == "legacy/anomalous"
        )


@pytest.mark.django_db
class TestInventoryPrestashopCatalogCommand:
    def test_builds_inventory_report_and_summary(self, tmp_path: Path, _clean_db):
        product = _make_product(reference="REF001", manufacturer_name="Talens")
        Combination.objects.create(product=product, icg_size="M", icg_color="Red")

        output_path = tmp_path / "inventory.json"

        with patch(
            "apps.sync.management.commands.inventory_prestashop_catalog.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 10, "name": "Size"},
                {"ps_id": 11, "name": "TALENS_color"},
            ]
            client.list_attribute_values.side_effect = [
                [{"ps_id": 101, "name": "M"}],
                [{"ps_id": 201, "name": "Red"}],
            ]
            client.list_products.return_value = [
                PrestashopProductSummary(
                    product_id=22,
                    reference="REF001",
                    name="Product REF001",
                    manufacturer_id=7,
                )
            ]
            client.list_specific_prices_by_product.return_value = [
                PrestashopSpecificPriceSummary(
                    specific_price_id=301,
                    product_id=22,
                    reduction=0.2,
                    reduction_type="percentage",
                )
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(
                    combination_id=55,
                    product_id=22,
                    attribute_value_ids=[101, 201],
                    ean13="1234567890123",
                )
            ]

            out = StringIO()
            call_command(
                "inventory_prestashop_catalog",
                "--output",
                str(output_path),
                stdout=out,
            )

        payload = json.loads(output_path.read_text())
        assert payload["summary"]["prestashop_products"] == 1
        assert payload["summary"]["prestashop_combinations"] == 1
        assert payload["summary"]["product_matches_safe"] == 1
        assert payload["summary"]["combination_matches_safe"] == 1
        assert payload["summary"]["products_with_product_level_specific_prices"] == 1
        assert payload["products"][0]["combinations"][0]["resolved_size"] == "M"
        assert payload["products"][0]["combinations"][0]["resolved_color"] == "Red"
        assert payload["attribute_groups"][0]["scope"] in {
            "global",
            "manufacturer-scoped",
        }
        assert "Inventory complete" in out.getvalue()

    def test_reports_unresolved_combination_when_attribute_role_cannot_be_inferred(self, _clean_db):
        _make_product(reference="REF001")

        with patch(
            "apps.sync.management.commands.inventory_prestashop_catalog.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 10, "name": "MysteryGroup"},
            ]
            client.list_attribute_values.return_value = [{"ps_id": 101, "name": "ValueX"}]
            client.list_products.return_value = [
                PrestashopProductSummary(
                    product_id=22,
                    reference="REF001",
                    name="Product REF001",
                    manufacturer_id=None,
                )
            ]
            client.list_specific_prices_by_product.return_value = []
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(
                    combination_id=55,
                    product_id=22,
                    attribute_value_ids=[101],
                    ean13="",
                )
            ]

            out = StringIO()
            call_command("inventory_prestashop_catalog", stdout=out)

        assert "unresolved=1" in out.getvalue()
