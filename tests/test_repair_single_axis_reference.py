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


def _make_product(reference="0090837", prestashop_id=2090):
    manufacturer = Manufacturer.objects.create(icg_code=f"{reference}-M", name="COPIC")
    return Product.objects.create(
        icg_id=1000 + Product.objects.count(),
        reference=reference,
        name=f"Product {reference}",
        manufacturer=manufacturer,
        prestashop_id=prestashop_id,
    )


@pytest.mark.django_db
class TestRepairSingleAxisReference:
    def test_remaps_placeholder_size_combinations_to_color_only_targets(self):
        product = _make_product()
        combination = Combination.objects.create(
            product=product,
            icg_size="***",
            icg_color="B00",
            prestashop_id=9001,
            sync_required=True,
            last_sync_error="boom",
        )

        with patch(
            "apps.sync.management.commands.repair_single_axis_reference.PrestashopClient"
        ) as client_cls:
            client = client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 11, "name": "COPIC_colores"},
                {"ps_id": 12, "name": "Size"},
            ]
            client.list_attribute_values.side_effect = [
                [{"ps_id": 201, "name": "B00"}],
                [{"ps_id": 301, "name": "***"}],
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(55, 2090, [201], ""),
                PrestashopCombinationSummary(9001, 2090, [301, 201], ""),
            ]

            out = StringIO()
            call_command("repair_single_axis_reference", "0090837", "--apply", stdout=out)

        combination.refresh_from_db()
        assert combination.prestashop_id == 55
        assert combination.sync_required is False
        assert combination.last_sync_error == ""
        assert "remaps=1" in out.getvalue()

    def test_can_delete_obsolete_placeholder_combinations_after_remap(self):
        product = _make_product()
        Combination.objects.create(
            product=product,
            icg_size="***",
            icg_color="B00",
            prestashop_id=9001,
        )

        with patch(
            "apps.sync.management.commands.repair_single_axis_reference.PrestashopClient"
        ) as client_cls:
            client = client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 11, "name": "COPIC_colores"},
                {"ps_id": 12, "name": "Size"},
            ]
            client.list_attribute_values.side_effect = [
                [{"ps_id": 201, "name": "B00"}],
                [{"ps_id": 301, "name": "***"}],
            ]
            client.list_combinations_for_product.return_value = [
                PrestashopCombinationSummary(55, 2090, [201], ""),
                PrestashopCombinationSummary(9001, 2090, [301, 201], ""),
            ]

            out = StringIO()
            call_command(
                "repair_single_axis_reference",
                "0090837",
                "--apply",
                "--delete-obsolete",
                stdout=out,
            )

        client.delete_combination.assert_called_once_with(9001)
        assert "obsolete_delete_candidates=1" in out.getvalue()
