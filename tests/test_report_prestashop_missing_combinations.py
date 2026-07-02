import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.catalog.models import Manufacturer, Product
from apps.prestashop.client import PrestashopCombinationSummary, PrestashopProductSummary


@pytest.fixture(autouse=True)
def _clean_db():
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
class TestReportPrestashopMissingCombinations:
    def test_writes_grouped_missing_report(self, tmp_path: Path):
        _make_product(reference="0300393", manufacturer_name="W&N")
        output_path = tmp_path / "missing.json"

        with patch(
            "apps.sync.management.commands.report_prestashop_missing_combinations.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 11, "name": "WINSOR & NEWTON_colores"},
            ]
            client.list_attribute_values.return_value = [{"ps_id": 201, "name": "RO225"}]
            client.list_products.return_value = [
                PrestashopProductSummary(22, "0300393", "Product 0300393", None)
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(55, 22, [201], "")
            ]

            out = StringIO()
            call_command(
                "report_prestashop_missing_combinations",
                "--output",
                str(output_path),
                stdout=out,
            )

        payload = json.loads(output_path.read_text())
        assert payload["summary"]["missing_combination_count"] == 1
        assert payload["references"][0]["reference"] == "0300393"
        assert payload["groups"][0]["group_name"] == "WINSOR & NEWTON_colores"
        assert payload["missing_combinations"][0]["resolved_color"] == "RO225"
        assert payload["missing_combinations"][0]["resolved_size"] == ""
        assert "Wrote missing combination report" in out.getvalue()
