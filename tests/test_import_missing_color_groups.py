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


_product_counter = 0


def _make_product(**overrides):
    global _product_counter
    _product_counter += 1
    manufacturer = Manufacturer.objects.create(
        icg_code=overrides.pop("icg_code", f"M-{_product_counter}"),
        name=overrides.pop("manufacturer_name", f"Manufacturer {_product_counter}"),
    )
    return Product.objects.create(
        icg_id=overrides.pop("icg_id", 1000 + _product_counter),
        reference=overrides.pop("reference", f"REF{_product_counter:03d}"),
        name=overrides.pop("name", f"Product {_product_counter}"),
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

    def test_updates_stale_reference_named_group(self):
        product = _make_product(reference="0110026", prestashop_id=2574)
        AttributeGroup.objects.create(
            icg_type="color",
            name="0110026_color",
            prestashop_id=78,
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

        assert "Updated (stale name): 1" in out.getvalue()
        ag = AttributeGroup.objects.get(icg_type="color", product=product)
        assert ag.prestashop_id == 77
        assert ag.name == "2574_color"

    def test_dry_run_reports_stale_but_does_not_update(self):
        product = _make_product(reference="0110026", prestashop_id=2574)
        AttributeGroup.objects.create(
            icg_type="color",
            name="0110026_color",
            prestashop_id=78,
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
            call_command("import_missing_color_groups", stdout=out)

        assert "Updated (stale name): 1" in out.getvalue()
        ag = AttributeGroup.objects.get(icg_type="color", product=product)
        assert ag.prestashop_id == 78
        assert ag.name == "0110026_color"

    def test_skips_when_local_name_already_matches(self):
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

        assert "Already correct: 1" in out.getvalue()
        ag = AttributeGroup.objects.get(icg_type="color", product=product)
        assert ag.prestashop_id == 77

    def test_reports_not_found_when_remote_group_missing(self):
        _make_product(reference="0110026", prestashop_id=2574)

        with patch(
            "apps.sync.management.commands.import_missing_color_groups.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = []

            out = StringIO()
            call_command("import_missing_color_groups", "--apply", stdout=out)

        assert "Not found remotely: 1" in out.getvalue()

    def test_reports_not_found_for_stale_group_without_remote_preferred(self):
        product = _make_product(reference="0110026", prestashop_id=2574)
        AttributeGroup.objects.create(
            icg_type="color",
            name="0110026_color",
            prestashop_id=78,
            product=product,
        )

        with patch(
            "apps.sync.management.commands.import_missing_color_groups.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 78, "name": "0110026_color"},
            ]

            out = StringIO()
            call_command("import_missing_color_groups", "--apply", stdout=out)

        assert "Not found remotely: 1" in out.getvalue()
        ag = AttributeGroup.objects.get(icg_type="color", product=product)
        assert ag.prestashop_id == 78

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
            for label in (
                "Created: 0",
                "Updated (stale name): 0",
                "Already correct: 0",
                "Not found remotely: 0",
            )
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

        assert "Already correct: 1" in out.getvalue()
        assert AttributeGroup.objects.filter(icg_type="color", product=product).count() == 1

    def test_handles_prestashop_id_conflict_gracefully(self):
        product_a = _make_product(reference="REF_A", prestashop_id=100, icg_id=2001)
        product_b = _make_product(reference="REF_B", prestashop_id=200, icg_id=2002)
        AttributeGroup.objects.create(
            icg_type="color",
            name="100_color",
            prestashop_id=77,
            product=product_a,
        )

        with patch(
            "apps.sync.management.commands.import_missing_color_groups.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 77, "name": "100_color"},
                {"ps_id": 77, "name": "200_color"},
            ]

            out = StringIO()
            call_command("import_missing_color_groups", "--apply", stdout=out)

        assert "Already correct: 1" in out.getvalue()
        assert "Conflicts: 1" in out.getvalue()
        assert not AttributeGroup.objects.filter(icg_type="color", product=product_b).exists()

    def test_handles_stale_update_with_prestashop_id_conflict(self):
        product_a = _make_product(reference="REF_A", prestashop_id=100, icg_id=2001)
        product_b = _make_product(reference="REF_B", prestashop_id=200, icg_id=2002)
        AttributeGroup.objects.create(
            icg_type="color",
            name="100_color",
            prestashop_id=77,
            product=product_a,
        )
        AttributeGroup.objects.create(
            icg_type="color",
            name="REF_B_color",
            prestashop_id=78,
            product=product_b,
        )

        with patch(
            "apps.sync.management.commands.import_missing_color_groups.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 77, "name": "100_color"},
                {"ps_id": 77, "name": "200_color"},
            ]

            out = StringIO()
            call_command("import_missing_color_groups", "--apply", stdout=out)

        assert "Already correct: 1" in out.getvalue()
        assert "Conflicts: 1" in out.getvalue()
        ag_b = AttributeGroup.objects.get(icg_type="color", product=product_b)
        assert ag_b.name == "REF_B_color"
        assert ag_b.prestashop_id == 78

    def test_updates_stale_and_creates_missing_in_same_run(self):
        product_a = _make_product(reference="REF_STALE", prestashop_id=100, icg_id=2001)
        product_b = _make_product(reference="REF_NEW", prestashop_id=200, icg_id=2002)
        AttributeGroup.objects.create(
            icg_type="color",
            name="REF_STALE_color",
            prestashop_id=78,
            product=product_a,
        )

        with patch(
            "apps.sync.management.commands.import_missing_color_groups.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 77, "name": "100_color"},
                {"ps_id": 88, "name": "200_color"},
            ]

            out = StringIO()
            call_command("import_missing_color_groups", "--apply", stdout=out)

        assert "Updated (stale name): 1" in out.getvalue()
        assert "Created: 1" in out.getvalue()
        ag_a = AttributeGroup.objects.get(icg_type="color", product=product_a)
        assert ag_a.prestashop_id == 77
        assert ag_a.name == "100_color"
        ag_b = AttributeGroup.objects.get(icg_type="color", product=product_b)
        assert ag_b.prestashop_id == 88
        assert ag_b.name == "200_color"

    def test_resolve_conflicts_swaps_stale_pairs(self):
        product_a = _make_product(reference="REF_A", prestashop_id=100, icg_id=2001)
        product_b = _make_product(reference="REF_B", prestashop_id=200, icg_id=2002)
        AttributeGroup.objects.create(
            icg_type="color",
            name="REF_A_color",
            prestashop_id=77,
            product=product_a,
        )
        AttributeGroup.objects.create(
            icg_type="color",
            name="REF_B_color",
            prestashop_id=88,
            product=product_b,
        )

        with patch(
            "apps.sync.management.commands.import_missing_color_groups.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 77, "name": "200_color"},
                {"ps_id": 88, "name": "100_color"},
            ]

            out = StringIO()
            call_command(
                "import_missing_color_groups",
                "--apply",
                "--resolve-conflicts",
                stdout=out,
            )

        assert "Resolved (swapped stale pairs): 1" in out.getvalue()
        assert "Updated (stale name): 2" in out.getvalue()
        assert "Conflicts: 0" in out.getvalue()
        ag_a = AttributeGroup.objects.get(icg_type="color", product=product_a)
        assert ag_a.prestashop_id == 88
        assert ag_a.name == "100_color"
        ag_b = AttributeGroup.objects.get(icg_type="color", product=product_b)
        assert ag_b.prestashop_id == 77
        assert ag_b.name == "200_color"
