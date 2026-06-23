from io import StringIO

import pytest
from django.core.management import call_command

from django.core.management.base import CommandError

from apps.catalog.models import Category, Combination, Manufacturer, Product


@pytest.fixture(autouse=True)
def _clean_db():
    Product.objects.all().delete()
    Manufacturer.objects.all().delete()
    Category.objects.all().delete()


@pytest.mark.django_db
class TestResetPrestashopIds:
    def test_resets_product_prestashop_ids(self):
        m1 = Manufacturer.objects.create(icg_code="M-1001", name="Brand A", prestashop_id=10)
        m2 = Manufacturer.objects.create(icg_code="M-1002", name="Brand B", prestashop_id=11)
        p1 = Product.objects.create(
            icg_id=1001, reference="REF001", name="P1", manufacturer=m1,
            prestashop_id=10, sync_required=False,
        )
        p2 = Product.objects.create(
            icg_id=1002, reference="REF002", name="P2", manufacturer=m2,
            prestashop_id=20, sync_required=False,
        )
        p3 = Product.objects.create(
            icg_id=1003, reference="REF003", name="P3", manufacturer=m1,
            prestashop_id=None, sync_required=False,
        )

        out = StringIO()
        call_command("reset_prestashop_ids", "product", stdout=out)

        p1.refresh_from_db()
        p2.refresh_from_db()
        p3.refresh_from_db()

        assert p1.prestashop_id is None
        assert p1.sync_required is True
        assert p2.prestashop_id is None
        assert p2.sync_required is True
        assert p3.prestashop_id is None
        assert p3.sync_required is False
        assert "Cleared prestashop_id for 2 product(s)" in out.getvalue()

    def test_resets_combination_prestashop_ids(self):
        m = Manufacturer.objects.create(icg_code="M-1001", name="Brand", prestashop_id=10)
        product = Product.objects.create(
            icg_id=1001, reference="REF001", name="P1", manufacturer=m,
            prestashop_id=22, sync_required=False,
        )
        c1 = Combination.objects.create(
            product=product, icg_size="M", icg_color="Red",
            ean13="111", prestashop_id=55, sync_required=False,
        )
        c2 = Combination.objects.create(
            product=product, icg_size="L", icg_color="Blue",
            ean13="222", prestashop_id=None, sync_required=False,
        )

        out = StringIO()
        call_command("reset_prestashop_ids", "combination", stdout=out)

        c1.refresh_from_db()
        c2.refresh_from_db()

        assert c1.prestashop_id is None
        assert c1.sync_required is True
        assert c2.prestashop_id is None
        assert c2.sync_required is False
        assert "Cleared prestashop_id for 1 combination(s)" in out.getvalue()

    def test_resets_category_prestashop_ids(self):
        cat = Category.objects.create(prestashop_id=30, name="Cat A", sync_required=False)

        out = StringIO()
        call_command("reset_prestashop_ids", "category", stdout=out)

        cat.refresh_from_db()
        assert cat.prestashop_id is None
        assert cat.sync_required is True

    def test_resets_manufacturer_prestashop_ids(self):
        m = Manufacturer.objects.create(
            icg_code="M-2000", name="Brand", prestashop_id=44, sync_required=False,
        )

        out = StringIO()
        call_command("reset_prestashop_ids", "manufacturer", stdout=out)

        m.refresh_from_db()
        assert m.prestashop_id is None
        assert m.sync_required is True

    def test_dry_run_does_not_modify_database(self):
        m = Manufacturer.objects.create(icg_code="M-1001", name="Brand", prestashop_id=10)
        p = Product.objects.create(
            icg_id=1001, reference="REF001", name="P1", manufacturer=m,
            prestashop_id=10, sync_required=False,
        )

        out = StringIO()
        call_command("reset_prestashop_ids", "product", "--dry-run", stdout=out)

        p.refresh_from_db()
        assert p.prestashop_id == 10
        assert p.sync_required is False
        assert "DRY RUN" in out.getvalue()

    def test_warns_when_no_records_with_prestashop_id(self):
        m = Manufacturer.objects.create(icg_code="M-1001", name="Brand", prestashop_id=10)
        Product.objects.create(
            icg_id=1001, reference="REF001", name="P1", manufacturer=m,
            prestashop_id=None, sync_required=False,
        )

        out = StringIO()
        call_command("reset_prestashop_ids", "product", stdout=out)

        assert "No product records with prestashop_id set" in out.getvalue()

    def test_rejects_invalid_entity_type(self):
        with pytest.raises(CommandError):
            call_command("reset_prestashop_ids", "invalid_type")
