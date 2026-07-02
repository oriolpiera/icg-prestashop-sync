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
class TestReportPrestashopUnresolvedCombinations:
    def test_writes_grouped_unresolved_report(self, tmp_path: Path):
        _make_product()
        output_path = tmp_path / "unresolved.json"

        with patch(
            "apps.sync.management.commands.report_prestashop_unresolved_combinations.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 10, "name": "MysteryGroup"},
                {"ps_id": 11, "name": "TALENS_color"},
            ]
            client.list_attribute_values.side_effect = [
                [{"ps_id": 101, "name": "ValueX"}],
                [{"ps_id": 201, "name": "Red"}],
            ]
            client.list_products.return_value = [
                PrestashopProductSummary(22, "REF001", "Product REF001", None)
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(55, 22, [101, 201], "")
            ]

            out = StringIO()
            call_command(
                "report_prestashop_unresolved_combinations",
                "--output",
                str(output_path),
                stdout=out,
            )

        payload = json.loads(output_path.read_text())
        assert payload["summary"]["unresolved_combination_count"] == 1
        assert payload["groups"][0]["group_name"] == "MysteryGroup"
        assert payload["unresolved_combinations"][0]["reference"] == "REF001"
        assert payload["unresolved_combinations"][0]["resolved_color"] == "Red"
        assert payload["unresolved_combinations"][0]["resolved_size"] == ""
        assert "Wrote unresolved combination report" in out.getvalue()
