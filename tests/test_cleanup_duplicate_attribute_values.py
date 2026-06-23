from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.catalog.models import AttributeGroup, AttributeValue


@pytest.fixture(autouse=True)
def _clean_db():
    AttributeValue.objects.all().delete()
    AttributeGroup.objects.all().delete()


_FAKE_VALUES = [
    {"ps_id": 50, "name": "S"},
    {"ps_id": 51, "name": "M"},
    {"ps_id": 52, "name": "M"},
    {"ps_id": 53, "name": "L"},
]


@pytest.mark.django_db
class TestCleanupDuplicateAttributeValues:
    def test_no_duplicates(self):
        ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        AttributeValue.objects.create(attribute_group=ag, icg_value="M", name="M", prestashop_id=51)

        with patch(
            "apps.sync.management.commands.cleanup_duplicate_attribute_values.PrestashopClient"
        ) as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.list_attribute_values.return_value = [
                {"ps_id": 51, "name": "M"},
            ]

            out = StringIO()
            call_command("cleanup_duplicate_attribute_values", stdout=out)

            assert "No duplicate attribute values found" in out.getvalue()
            mock_client.delete_attribute_value.assert_not_called()

    def test_detects_duplicates_dry_run(self):
        ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        AttributeValue.objects.create(attribute_group=ag, icg_value="M", name="M", prestashop_id=51)

        with patch(
            "apps.sync.management.commands.cleanup_duplicate_attribute_values.PrestashopClient"
        ) as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.list_attribute_values.return_value = _FAKE_VALUES

            out = StringIO()
            call_command("cleanup_duplicate_attribute_values", stdout=out)

            assert "M" in out.getvalue()
            assert "keep 51" in out.getvalue()
            assert "delete [52]" in out.getvalue()
            assert "DRY RUN" in out.getvalue()
            mock_client.delete_attribute_value.assert_not_called()

    def test_applies_and_deletes_duplicates(self):
        ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        AttributeValue.objects.create(attribute_group=ag, icg_value="M", name="M", prestashop_id=51)

        with patch(
            "apps.sync.management.commands.cleanup_duplicate_attribute_values.PrestashopClient"
        ) as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.list_attribute_values.return_value = _FAKE_VALUES

            out = StringIO()
            call_command("cleanup_duplicate_attribute_values", "--apply", stdout=out)

            assert "APPLIED" in out.getvalue()
            mock_client.delete_attribute_value.assert_called_once_with(52)

            av = AttributeValue.objects.get(attribute_group=ag, icg_value="M")
            assert av.prestashop_id == 51

    def test_keeps_lowest_id_when_no_django_match(self):
        AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)

        with patch(
            "apps.sync.management.commands.cleanup_duplicate_attribute_values.PrestashopClient"
        ) as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.list_attribute_values.return_value = _FAKE_VALUES

            out = StringIO()
            call_command("cleanup_duplicate_attribute_values", "--apply", stdout=out)

            assert "No Django record for 'M'" in out.getvalue()
            mock_client.delete_attribute_value.assert_called_once_with(52)

    def test_updates_django_when_ps_id_dangling(self):
        ag = AttributeGroup.objects.create(icg_type="size", name="Size", prestashop_id=10)
        AttributeValue.objects.create(attribute_group=ag, icg_value="M", name="M", prestashop_id=99)

        with patch(
            "apps.sync.management.commands.cleanup_duplicate_attribute_values.PrestashopClient"
        ) as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.list_attribute_values.return_value = _FAKE_VALUES

            out = StringIO()
            call_command("cleanup_duplicate_attribute_values", "--apply", stdout=out)

            assert "Django points to PS ID 99" in out.getvalue()
            av = AttributeValue.objects.get(attribute_group=ag, icg_value="M")
            assert av.prestashop_id == 51

    def test_no_group_in_django(self):
        out = StringIO()
        call_command("cleanup_duplicate_attribute_values", stdout=out)
        assert "No global size attribute group found" in out.getvalue()
