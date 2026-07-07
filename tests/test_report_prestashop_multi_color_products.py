import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.prestashop.client import PrestashopCombinationSummary, PrestashopProductSummary


@pytest.mark.django_db
class TestReportPrestashopMultiColorProducts:
    def test_reports_only_products_with_many_combinations_and_multiple_color_groups(
        self, tmp_path: Path
    ):
        output_path = tmp_path / "multi-color-products.json"

        with patch(
            "apps.sync.management.commands.report_prestashop_multi_color_products.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 10, "name": "Size"},
                {"ps_id": 11, "name": "REF001_color"},
                {"ps_id": 12, "name": "legacy_color"},
                {"ps_id": 13, "name": "single_color"},
            ]
            client.list_attribute_values.side_effect = [
                [
                    {"ps_id": 101, "name": "S"},
                    {"ps_id": 102, "name": "M"},
                ],
                [
                    {"ps_id": 201, "name": "Red"},
                ],
                [
                    {"ps_id": 202, "name": "Blue"},
                ],
                [
                    {"ps_id": 203, "name": "Green"},
                ],
            ]
            client.list_products.return_value = [
                PrestashopProductSummary(22, "REF001", "Product REF001", None),
                PrestashopProductSummary(23, "REF002", "Product REF002", None),
            ]
            client.list_combinations_for_product.side_effect = [
                [
                    PrestashopCombinationSummary(i, 22, [101 if i % 2 else 102, 201, 202], "")
                    for i in range(1, 53)
                ],
                [
                    PrestashopCombinationSummary(i, 23, [101 if i % 2 else 102, 203], "")
                    for i in range(100, 152)
                ],
            ]

            out = StringIO()
            call_command(
                "report_prestashop_multi_color_products",
                "--output",
                str(output_path),
                stdout=out,
            )

        payload = json.loads(output_path.read_text())
        assert payload["summary"]["product_count"] == 1
        assert payload["products"][0]["prestashop_product_id"] == 22
        assert payload["products"][0]["combination_count"] == 52
        assert payload["products"][0]["color_group_count"] == 2
        assert payload["products"][0]["color_groups"] == ["REF001_color", "legacy_color"]
        assert "Matching products: 1" in out.getvalue()
