from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.catalog.models import Combination, Manufacturer, Product
from apps.prestashop.client import PrestashopCombinationSummary


@pytest.fixture(autouse=True)
def _clean_db():
    Combination.objects.all().delete()
    Product.objects.all().delete()
    Manufacturer.objects.all().delete()


def _make_product(reference="0931060", prestashop_id=9558):
    manufacturer = Manufacturer.objects.create(icg_code=f"{reference}-M", name="GOLDEN")
    return Product.objects.create(
        icg_id=1000 + Product.objects.count(),
        reference=reference,
        name=f"Product {reference}",
        manufacturer=manufacturer,
        prestashop_id=prestashop_id,
    )


@pytest.mark.django_db
class TestDeleteDuplicatePrestashopCombinations:
    def test_prefers_django_mapped_prestashop_id(self):
        product = _make_product()
        Combination.objects.create(
            product=product, icg_size="11ML", icg_color="580", prestashop_id=138555
        )

        with patch(
            "apps.sync.management.commands.delete_duplicate_prestashop_combinations.PrestashopClient"
        ) as client_cls:
            client = client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 10, "name": "Size"},
                {"ps_id": 11, "name": "GOLDEN_colores"},
            ]
            client.list_attribute_values.side_effect = [
                [{"ps_id": 101, "name": "11ML"}],
                [{"ps_id": 201, "name": "580"}],
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(138555, 9558, [101, 201], ""),
                PrestashopCombinationSummary(140555, 9558, [101, 201], ""),
            ]

            out = StringIO()
            call_command("delete_duplicate_prestashop_combinations", "0931060", stdout=out)

        client.delete_combination.assert_not_called()
        assert "delete=1" in out.getvalue()

    def test_apply_deletes_unmapped_duplicate(self):
        product = _make_product()
        Combination.objects.create(
            product=product, icg_size="11ML", icg_color="580", prestashop_id=138555
        )

        with patch(
            "apps.sync.management.commands.delete_duplicate_prestashop_combinations.PrestashopClient"
        ) as client_cls:
            client = client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 10, "name": "Size"},
                {"ps_id": 11, "name": "GOLDEN_colores"},
            ]
            client.list_attribute_values.side_effect = [
                [{"ps_id": 101, "name": "11ML"}],
                [{"ps_id": 201, "name": "580"}],
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(138555, 9558, [101, 201], ""),
                PrestashopCombinationSummary(140555, 9558, [101, 201], ""),
            ]

            out = StringIO()
            call_command(
                "delete_duplicate_prestashop_combinations",
                "0931060",
                "--apply",
                stdout=out,
            )

        client.delete_combination.assert_called_once_with(140555)
        assert "[APPLIED]" in out.getvalue()
