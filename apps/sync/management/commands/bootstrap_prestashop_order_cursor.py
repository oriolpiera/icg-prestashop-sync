from django.core.management.base import BaseCommand, CommandError

from apps.prestashop.client import PrestashopClient
from apps.sync.cursor_service import advance_cursor
from apps.sync.models import SyncCursorSource


class Command(BaseCommand):
    help = "Set the order sync cursor to the latest order currently present in Prestashop"

    def add_arguments(self, parser):
        parser.add_argument(
            "--order-id",
            type=int,
            help="Set the cursor to a specific Prestashop order ID instead of the latest one.",
        )

    def handle(self, *args, **options):
        client = PrestashopClient()
        order_id = options.get("order_id")
        if order_id is not None:
            snapshot = client.get_order_snapshot(order_id)
            target_id = snapshot.order_id
            target_date = snapshot.date_add
        else:
            latest = client.get_latest_order_summary()
            if latest is None:
                raise CommandError("No Prestashop orders were returned; cursor not changed.")
            target_id = latest.order_id
            target_date = latest.date_add

        advance_cursor(SyncCursorSource.ORDERS, target_date, str(target_id))
        self.stdout.write(
            self.style.SUCCESS(
                "Order cursor set to Prestashop order " f"#{target_id} ({target_date.isoformat()})."
            )
        )
