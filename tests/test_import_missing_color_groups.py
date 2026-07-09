from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.catalog.models import AttributeGroup, Manufacturer, Product


@pytest.fixture(autouse=True)
def _clean_db():
    AttributeGroup.objects.all().delete()
    Product.objects.all().delete()
    Manufacturer.objects.all().delete()


def _make_product(**overrides):
    manufacturer = Manufacturer.objects.create(
        icg_code=overrides.pop("icg_code", "M-1001"),
        name=overrides.pop("manufacturer_name", "Manufacturer 1001"),
    )
    return Product.objects.create(
        icg_id=overrides.pop("icg_id", 1001),
        reference=overrides.pop("reference", "REF001"),
        name=overrides.pop("name", "Product One"),
        manufacturer=manufacturer,
        prestashop_id=overrides.pop("prestashop_id", None),
        visible_web=True,
        discontinued=False,
    )


@pytest.mark.django_db
class TestImportMissingColorGroupsCommand:
    def test_dry_run_reports_missing_groups_without_creating(self):
        product = _make_product(reference="0110026", prestashop_id=2574)

        with patch(
            "apps.sync.management.commands.import_missing_color_groups.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 77, "name": "2574_color"},
                {"ps_id": 78, "name": "0110026_color"},
            ]

            out = StringIO()
            call_command("import_missing_color_groups", stdout=out)

        assert "[DRY RUN] Import missing color groups" in out.getvalue()
        assert "Created: 1" in out.getvalue()
        assert not AttributeGroup.objects.filter(icg_type="color", product=product).exists()

    def test_apply_creates_missing_groups(self):
        product = _make_product(reference="0110026", prestashop_id=2574)

        with patch(
            "apps.sync.management.commands.import_missing_color_groups.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 77, "name": "2574_color"},
            ]

            out = StringIO()
            call_command("import_missing_color_groups", "--apply", stdout=out)

        assert "[APPLIED] Import missing color groups" in out.getvalue()
        assert "Created: 1" in out.getvalue()
        ag = AttributeGroup.objects.get(icg_type="color", product=product)
        assert ag.prestashop_id == 77
        assert ag.name == "2574_color"

    def test_skips_when_local_record_already_exists(self):
        product = _make_product(reference="0110026", prestashop_id=2574)
        AttributeGroup.objects.create(
            icg_type="color",
            name="2574_color",
            prestashop_id=77,
            product=product,
        )

        with patch(
            "apps.sync.management.commands.import_missing_color_groups.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 77, "name": "2574_color"},
            ]

            out = StringIO()
            call_command("import_missing_color_groups", "--apply", stdout=out)

        assert "Created: 0" in out.getvalue()
        assert "Already exists (local): 1" in out.getvalue()

    def test_reports_not_found_when_remote_group_missing(self):
        _make_product(reference="0110026", prestashop_id=2574)

        with patch(
            "apps.sync.management.commands.import_missing_color_groups.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = []

            out = StringIO()
            call_command("import_missing_color_groups", "--apply", stdout=out)

        assert "Created: 0" in out.getvalue()
        assert "Not found remotely: 1" in out.getvalue()

    def test_skips_products_without_prestashop_id(self):
        _make_product(reference="0110026", prestashop_id=None)

        with patch(
            "apps.sync.management.commands.import_missing_color_groups.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 77, "name": "None_color"},
            ]

            out = StringIO()
            call_command("import_missing_color_groups", "--apply", stdout=out)

        assert all(
            label in out.getvalue()
            for label in ("Created: 0", "Already exists (local): 0", "Not found remotely: 0")
        )

    def test_idempotent_second_run(self):
        product = _make_product(reference="0110026", prestashop_id=2574)

        with patch(
            "apps.sync.management.commands.import_missing_color_groups.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 77, "name": "2574_color"},
            ]

            call_command("import_missing_color_groups", "--apply", stdout=StringIO())

        assert AttributeGroup.objects.filter(icg_type="color", product=product).exists()

        with patch(
            "apps.sync.management.commands.import_missing_color_groups.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 77, "name": "2574_color"},
            ]

            out = StringIO()
            call_command("import_missing_color_groups", "--apply", stdout=out)

        assert "Created: 0" in out.getvalue()
        assert "Already exists (local): 1" in out.getvalue()
        assert AttributeGroup.objects.filter(icg_type="color", product=product).count() == 1
