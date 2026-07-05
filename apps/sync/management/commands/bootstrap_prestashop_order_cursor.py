from django.core.management.base import BaseCommand, CommandError

from apps.prestashop.client import PrestashopClient
from apps.sync.cursor_service import advance_cursor
from apps.sync.models import SyncCursorSource


class Command(BaseCommand):
    help = "Set the order sync cursor to the latest order currently present in Prestashop"

    def handle(self, *args, **options):
        client = PrestashopClient()
        latest = client.get_latest_order_summary()
        if latest is None:
            raise CommandError("No Prestashop orders were returned; cursor not changed.")

        advance_cursor(SyncCursorSource.ORDERS, latest.date_add, str(latest.order_id))
        self.stdout.write(
            self.style.SUCCESS(
                "Order cursor set to Prestashop order "
                f"#{latest.order_id} ({latest.date_add.isoformat()})."
            )
        )
