from datetime import datetime
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.prestashop.client import PrestashopCustomerSummary, PrestashopOrderSummary
from apps.sync.models import SyncCursor, SyncCursorSource


def _aware(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0):
    return timezone.make_aware(datetime(year, month, day, hour, minute, second))


@pytest.mark.django_db
class TestBootstrapPrestashopCustomerCursor:
    def test_sets_customer_cursor_from_latest_prestashop_summary(self):
        out = StringIO()
        with patch(
            "apps.sync.management.commands.bootstrap_prestashop_customer_cursor.PrestashopClient"
        ) as client_factory:
            client_factory.return_value.get_latest_customer_summary.return_value = (
                PrestashopCustomerSummary(
                    42,
                    "Ada",
                    "Lovelace",
                    "ada@example.com",
                    _aware(2026, 7, 5, 12),
                )
            )

            call_command("bootstrap_prestashop_customer_cursor", stdout=out)

        cursor = SyncCursor.objects.get(source=SyncCursorSource.CUSTOMERS)
        assert cursor.last_source_key == "42"
        assert cursor.last_modified_at == _aware(2026, 7, 5, 12)
        assert "Customer cursor set" in out.getvalue()

    def test_raises_when_no_customer_is_returned(self):
        with patch(
            "apps.sync.management.commands.bootstrap_prestashop_customer_cursor.PrestashopClient"
        ) as client_factory:
            client_factory.return_value.get_latest_customer_summary.return_value = None

            with pytest.raises(CommandError, match="No Prestashop customers"):
                call_command("bootstrap_prestashop_customer_cursor", stdout=StringIO())

    def test_sets_customer_cursor_from_explicit_customer_id(self):
        out = StringIO()
        with patch(
            "apps.sync.management.commands.bootstrap_prestashop_customer_cursor.PrestashopClient"
        ) as client_factory:
            client_factory.return_value.get_customer_snapshot.return_value = type(
                "CustomerSnapshot",
                (),
                {"customer_id": 55, "date_add": _aware(2026, 7, 5, 15)},
            )()

            call_command(
                "bootstrap_prestashop_customer_cursor",
                "--customer-id",
                "55",
                stdout=out,
            )

        cursor = SyncCursor.objects.get(source=SyncCursorSource.CUSTOMERS)
        assert cursor.last_source_key == "55"
        assert cursor.last_modified_at == _aware(2026, 7, 5, 15)
        assert "#55" in out.getvalue()


@pytest.mark.django_db
class TestBootstrapPrestashopOrderCursor:
    def test_sets_order_cursor_from_latest_prestashop_summary(self):
        out = StringIO()
        with patch(
            "apps.sync.management.commands.bootstrap_prestashop_order_cursor.PrestashopClient"
        ) as client_factory:
            client_factory.return_value.get_latest_order_summary.return_value = (
                PrestashopOrderSummary(
                    77,
                    42,
                    "Redsys Card",
                    _aware(2026, 7, 5, 13),
                )
            )

            call_command("bootstrap_prestashop_order_cursor", stdout=out)

        cursor = SyncCursor.objects.get(source=SyncCursorSource.ORDERS)
        assert cursor.last_source_key == "77"
        assert cursor.last_modified_at == _aware(2026, 7, 5, 13)
        assert "Order cursor set" in out.getvalue()

    def test_raises_when_no_order_is_returned(self):
        with patch(
            "apps.sync.management.commands.bootstrap_prestashop_order_cursor.PrestashopClient"
        ) as client_factory:
            client_factory.return_value.get_latest_order_summary.return_value = None

            with pytest.raises(CommandError, match="No Prestashop orders"):
                call_command("bootstrap_prestashop_order_cursor", stdout=StringIO())

    def test_sets_order_cursor_from_explicit_order_id(self):
        out = StringIO()
        with patch(
            "apps.sync.management.commands.bootstrap_prestashop_order_cursor.PrestashopClient"
        ) as client_factory:
            client_factory.return_value.get_order_snapshot.return_value = type(
                "OrderSnapshot",
                (),
                {"order_id": 88, "date_add": _aware(2026, 7, 5, 16)},
            )()

            call_command(
                "bootstrap_prestashop_order_cursor",
                "--order-id",
                "88",
                stdout=out,
            )

        cursor = SyncCursor.objects.get(source=SyncCursorSource.ORDERS)
        assert cursor.last_source_key == "88"
        assert cursor.last_modified_at == _aware(2026, 7, 5, 16)
        assert "#88" in out.getvalue()
