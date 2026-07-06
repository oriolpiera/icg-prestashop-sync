from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.catalog.models import AttributeGroup, AttributeValue, Manufacturer, Product


@pytest.fixture(autouse=True)
def _clean_db():
    AttributeValue.objects.all().delete()
    AttributeGroup.objects.all().delete()
    Product.objects.all().delete()
    Manufacturer.objects.all().delete()


def _make_product(reference="REF001"):
    manufacturer = Manufacturer.objects.create(icg_code=f"{reference}-M", name="Talens")
    return Product.objects.create(
        icg_id=1000 + Product.objects.count(),
        reference=reference,
        name=f"Product {reference}",
        manufacturer=manufacturer,
    )


@pytest.mark.django_db
class TestReconcilePrestashopAttributeMappingsCommand:
    def test_dry_run_reports_stale_group_and_value_without_writing(self):
        product = _make_product("1160080")
        size_group = AttributeGroup.objects.create(
            icg_type="size", name="Size", prestashop_id=19099
        )
        AttributeGroup.objects.create(
            icg_type="color",
            name="1160080_color",
            prestashop_id=19150,
            product=product,
        )
        size_value = AttributeValue.objects.create(
            attribute_group=size_group,
            icg_value="A4",
            name="A4",
            prestashop_id=79215,
        )

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_attribute_mappings.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 10, "name": "Size"},
                {"ps_id": 19099, "name": "1160080_color"},
            ]
            client.list_attribute_values.side_effect = lambda group_ps_id: {
                10: [{"ps_id": 101, "name": "A4"}],
                19099: [{"ps_id": 79215, "name": "A4"}],
            }[group_ps_id]

            out = StringIO()
            call_command("reconcile_prestashop_attribute_mappings", stdout=out)

        size_group.refresh_from_db()
        size_value.refresh_from_db()
        assert size_group.prestashop_id == 19099
        assert size_value.prestashop_id == 79215
        assert "[DRY RUN] Planned attribute mapping fixes: groups=2 values=1" in out.getvalue()

    def test_apply_remaps_stale_group_and_value(self):
        product = _make_product("1160080")
        size_group = AttributeGroup.objects.create(
            icg_type="size", name="Size", prestashop_id=19099
        )
        AttributeGroup.objects.create(
            icg_type="color",
            name="1160080_color",
            prestashop_id=19150,
            product=product,
        )
        size_value = AttributeValue.objects.create(
            attribute_group=size_group,
            icg_value="A4",
            name="A4",
            prestashop_id=79215,
        )

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_attribute_mappings.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 10, "name": "Size"},
                {"ps_id": 19099, "name": "1160080_color"},
            ]
            client.list_attribute_values.side_effect = lambda group_ps_id: {
                10: [{"ps_id": 101, "name": "A4"}],
                19099: [{"ps_id": 79215, "name": "A4"}],
            }[group_ps_id]

            out = StringIO()
            call_command("reconcile_prestashop_attribute_mappings", "--apply", stdout=out)

        size_group.refresh_from_db()
        size_value.refresh_from_db()
        assert size_group.prestashop_id == 10
        assert size_value.prestashop_id == 101
        assert "Attribute mapping reconciliation applied." in out.getvalue()

    def test_apply_handles_swapped_group_ids(self):
        product = _make_product("1160080")
        size_group = AttributeGroup.objects.create(
            icg_type="size", name="Size", prestashop_id=19099
        )
        color_group = AttributeGroup.objects.create(
            icg_type="color",
            name="1160080_color",
            prestashop_id=10,
            product=product,
        )

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_attribute_mappings.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [
                {"ps_id": 10, "name": "Size"},
                {"ps_id": 19099, "name": "1160080_color"},
            ]
            client.list_attribute_values.side_effect = [[], []]

            out = StringIO()
            call_command("reconcile_prestashop_attribute_mappings", "--apply", stdout=out)

        size_group.refresh_from_db()
        color_group.refresh_from_db()
        assert size_group.prestashop_id == 10
        assert color_group.prestashop_id == 19099
        assert "groups=2 values=0" in out.getvalue()

    def test_apply_can_prune_missing_local_values(self):
        product = _make_product("0190028")
        color_group = AttributeGroup.objects.create(
            icg_type="color",
            name="0190028_color",
            prestashop_id=20000,
            product=product,
        )
        stale_value = AttributeValue.objects.create(
            attribute_group=color_group,
            icg_value="1",
            name="1",
            prestashop_id=30000,
        )

        with patch(
            "apps.sync.management.commands.reconcile_prestashop_attribute_mappings.PrestashopClient"
        ) as mock_client_cls:
            client = mock_client_cls.return_value
            client.list_attribute_groups.return_value = [{"ps_id": 20000, "name": "0190028_color"}]
            client.list_attribute_values.return_value = []

            out = StringIO()
            call_command(
                "reconcile_prestashop_attribute_mappings",
                "--apply",
                "--prune-missing-local",
                stdout=out,
            )

        assert AttributeValue.objects.filter(pk=stale_value.pk).exists() is False
        assert AttributeGroup.objects.filter(pk=color_group.pk).exists() is True
        assert "pruned_groups=0 pruned_values=1" in out.getvalue()
