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


def _make_product(reference="REF001", prestashop_id=22):
    manufacturer = Manufacturer.objects.create(icg_code=f"{reference}-M", name="Talens")
    return Product.objects.create(
        icg_id=1000 + Product.objects.count(),
        reference=reference,
        name=f"Product {reference}",
        manufacturer=manufacturer,
        prestashop_id=prestashop_id,
    )


def _make_combination(product, size="M", color="Red", prestashop_id=None):
    return Combination.objects.create(
        product=product,
        icg_size=size,
        icg_color=color,
        prestashop_id=prestashop_id,
        sync_required=True,
        last_sync_error="boom",
    )


@pytest.mark.django_db
class TestReconcilePrestashopCombinationsCommand:
    def test_dry_run_reports_safe_match_without_writing(self):
        product = _make_product()
        combination = _make_combination(product)

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_combinations.PrestashopClient"
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
                PrestashopProductSummary(22, "REF001", "Product REF001", None)
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(55, 22, [101, 201], "")
            ]

            out = StringIO()
            call_command("reconcile_prestashop_combinations", stdout=out)

        combination.refresh_from_db()
        assert combination.prestashop_id is None
        assert "[DRY RUN]" in out.getvalue()
        assert "updated=1" in out.getvalue()

    def test_apply_writes_safe_match(self):
        product = _make_product()
        combination = _make_combination(product)

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_combinations.PrestashopClient"
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
                PrestashopProductSummary(22, "REF001", "Product REF001", None)
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(55, 22, [101, 201], "")
            ]

            out = StringIO()
            call_command("reconcile_prestashop_combinations", "--apply", stdout=out)

        combination.refresh_from_db()
        assert combination.prestashop_id == 55
        assert combination.sync_required is False
        assert combination.last_sync_error == ""
        assert "[APPLIED]" in out.getvalue()

    def test_reports_unresolved_when_group_role_cannot_be_inferred(self):
        product = _make_product()
        combination = _make_combination(product)

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_combinations.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [{"ps_id": 10, "name": "MysteryGroup"}]
            client.list_attribute_values.return_value = [{"ps_id": 101, "name": "ValueX"}]
            client.list_products.return_value = [
                PrestashopProductSummary(22, "REF001", "Product REF001", None)
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(55, 22, [101], "")
            ]

            out = StringIO()
            call_command("reconcile_prestashop_combinations", "--apply", stdout=out)

        combination.refresh_from_db()
        assert combination.prestashop_id is None
        assert "unresolved=1" in out.getvalue()

    def test_apply_matches_single_color_dimension_to_blank_size(self):
        product = _make_product(reference="0300393")
        combination = _make_combination(product, size="", color="RO225")

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_combinations.PrestashopClient"
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
            call_command("reconcile_prestashop_combinations", "--apply", stdout=out)

        combination.refresh_from_db()
        assert combination.prestashop_id == 55
        assert "safe=1" in out.getvalue()

    def test_apply_matches_single_size_dimension_to_blank_color(self):
        product = _make_product(reference="1460005")
        combination = _make_combination(product, size="A4", color="")

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_combinations.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 10, "name": "MOLESKINE_tallas"},
            ]
            client.list_attribute_values.return_value = [{"ps_id": 101, "name": "A4"}]
            client.list_products.return_value = [
                PrestashopProductSummary(22, "1460005", "Product 1460005", None)
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(56, 22, [101], "")
            ]

            out = StringIO()
            call_command("reconcile_prestashop_combinations", "--apply", stdout=out)

        combination.refresh_from_db()
        assert combination.prestashop_id == 56
        assert "safe=1" in out.getvalue()

    def test_single_dimension_fallback_stays_ambiguous_when_both_axes_match(self):
        product = _make_product(reference="1740010")
        _make_combination(product, size="", color="B")
        _make_combination(product, size="B", color="")

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_combinations.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 11, "name": "LAMY_colores"},
            ]
            client.list_attribute_values.return_value = [{"ps_id": 201, "name": "B"}]
            client.list_products.return_value = [
                PrestashopProductSummary(22, "1740010", "Product 1740010", None)
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(55, 22, [201], "")
            ]

            out = StringIO()
            call_command("reconcile_prestashop_combinations", stdout=out)

        assert "ambiguous=1" in out.getvalue()

    def test_skips_conflicting_existing_mapping(self):
        product = _make_product()
        combination = _make_combination(product, prestashop_id=99)

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_combinations.PrestashopClient"
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
                PrestashopProductSummary(22, "REF001", "Product REF001", None)
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(55, 22, [101, 201], "")
            ]

            out = StringIO()
            call_command("reconcile_prestashop_combinations", "--apply", stdout=out)

        combination.refresh_from_db()
        assert combination.prestashop_id == 99
        assert "Conflict for combination REF001/M/Red" in out.getvalue()
        assert "conflicts=1" in out.getvalue()

    def test_writes_conflict_report_json(self, tmp_path: Path):
        product = _make_product()
        _make_combination(product, prestashop_id=99)
        output_path = tmp_path / "combination-conflicts.json"

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_combinations.PrestashopClient"
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
                PrestashopProductSummary(22, "REF001", "Product REF001", None)
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(55, 22, [101, 201], "")
            ]

            out = StringIO()
            call_command(
                "reconcile_prestashop_combinations",
                "--apply",
                "--output-conflicts",
                str(output_path),
                stdout=out,
            )

        payload = json.loads(output_path.read_text())
        assert payload[0]["reference"] == "REF001"
        assert payload[0]["django_prestashop_id"] == 99
        assert payload[0]["matched_prestashop_id"] == 55
        assert "Wrote conflict report" in out.getvalue()
