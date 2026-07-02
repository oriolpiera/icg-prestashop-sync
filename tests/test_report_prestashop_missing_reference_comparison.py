import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.catalog.models import Combination, Manufacturer, Product
from apps.prestashop.client import PrestashopCombinationSummary, PrestashopProductSummary


@pytest.fixture(autouse=True)
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
        prestashop_id=22,
    )


@pytest.mark.django_db
class TestReportPrestashopMissingReferenceComparison:
    def test_writes_per_reference_comparison_report(self, tmp_path: Path):
        product = _make_product(reference="0300393", manufacturer_name="W&N")
        Combination.objects.create(product=product, icg_size="", icg_color="B00")
        output_path = tmp_path / "missing-reference-comparison.json"

        with patch(
            "apps.sync.management.commands.report_prestashop_missing_reference_comparison.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 11, "name": "WINSOR & NEWTON_colores"},
            ]
            client.list_attribute_values.return_value = [
                {"ps_id": 201, "name": "RO225"},
                {"ps_id": 202, "name": "B00"},
            ]
            client.list_products.return_value = [
                PrestashopProductSummary(22, "0300393", "Product 0300393", None)
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(55, 22, [201], ""),
                PrestashopCombinationSummary(56, 22, [202], ""),
            ]

            out = StringIO()
            call_command(
                "report_prestashop_missing_reference_comparison",
                "--output",
                str(output_path),
                stdout=out,
            )

        payload = json.loads(output_path.read_text())
        assert payload["summary"]["reference_count"] == 1
        assert payload["summary"]["missing_combination_count"] == 1
        reference = payload["references"][0]
        assert reference["reference"] == "0300393"
        assert reference["missing_count"] == 1
        assert reference["django_variant_count"] == 1
        assert reference["prestashop_variant_count"] == 2
        assert reference["missing_variants"][0]["color"] == "RO225"
        assert reference["django_variants"][0]["color"] == "B00"
        assert "Wrote missing reference comparison report" in out.getvalue()
