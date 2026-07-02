from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.catalog.models import Manufacturer, Product
from apps.prestashop.client import PrestashopProductSummary


@pytest.fixture(autouse=True)
def _clean_db():
    Product.objects.all().delete()
    Manufacturer.objects.all().delete()


def _make_product(reference="REF001", prestashop_id=None):
    manufacturer = Manufacturer.objects.create(icg_code=f"{reference}-M", name="Brand")
    return Product.objects.create(
        icg_id=1000 + Product.objects.count(),
        reference=reference,
        name=f"Product {reference}",
        manufacturer=manufacturer,
        prestashop_id=prestashop_id,
        sync_required=True,
        last_sync_error="boom",
    )


@pytest.mark.django_db
class TestReconcilePrestashopProductsCommand:
    def test_dry_run_reports_safe_matches_without_writing(self):
        product = _make_product(reference="REF001")

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_products.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_products.return_value = [
                PrestashopProductSummary(
                    product_id=22,
                    reference="REF001",
                    name="Product REF001",
                    manufacturer_id=None,
                )
            ]

            out = StringIO()
            call_command("reconcile_prestashop_products", stdout=out)

        product.refresh_from_db()
        assert product.prestashop_id is None
        assert "[DRY RUN]" in out.getvalue()
        assert "updated=1" in out.getvalue()

    def test_apply_writes_safe_match(self):
        product = _make_product(reference="REF001")

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_products.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_products.return_value = [
                PrestashopProductSummary(
                    product_id=22,
                    reference="REF001",
                    name="Product REF001",
                    manufacturer_id=None,
                )
            ]

            out = StringIO()
            call_command("reconcile_prestashop_products", "--apply", stdout=out)

        product.refresh_from_db()
        assert product.prestashop_id == 22
        assert product.sync_required is False
        assert product.last_sync_error == ""
        assert "[APPLIED]" in out.getvalue()

    def test_apply_skips_conflicting_existing_mapping(self):
        product = _make_product(reference="REF001", prestashop_id=99)

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_products.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_products.return_value = [
                PrestashopProductSummary(
                    product_id=22,
                    reference="REF001",
                    name="Product REF001",
                    manufacturer_id=None,
                )
            ]

            out = StringIO()
            call_command("reconcile_prestashop_products", "--apply", stdout=out)

        product.refresh_from_db()
        assert product.prestashop_id == 99
        assert "Conflict for reference REF001" in out.getvalue()
        assert "conflicts=1" in out.getvalue()

    def test_duplicate_prestashop_reference_is_ambiguous(self):
        product = _make_product(reference="REF001")

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_products.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_products.return_value = [
                PrestashopProductSummary(
                    product_id=22,
                    reference="REF001",
                    name="First",
                    manufacturer_id=None,
                ),
                PrestashopProductSummary(
                    product_id=23,
                    reference="REF001",
                    name="Second",
                    manufacturer_id=None,
                ),
            ]

            out = StringIO()
            call_command("reconcile_prestashop_products", "--apply", stdout=out)

        product.refresh_from_db()
        assert product.prestashop_id is None
        assert "ambiguous=2" in out.getvalue()
