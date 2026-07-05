from django.core.management.base import BaseCommand, CommandError

from apps.prestashop.client import PrestashopClient
from apps.sync.cursor_service import advance_cursor
from apps.sync.models import SyncCursorSource


class Command(BaseCommand):
    help = "Set the customer sync cursor to the latest customer currently present in Prestashop"

    def handle(self, *args, **options):
        client = PrestashopClient()
        latest = client.get_latest_customer_summary()
        if latest is None:
            raise CommandError("No Prestashop customers were returned; cursor not changed.")

        advance_cursor(SyncCursorSource.CUSTOMERS, latest.date_add, str(latest.customer_id))
        self.stdout.write(
            self.style.SUCCESS(
                "Customer cursor set to Prestashop customer "
                f"#{latest.customer_id} ({latest.date_add.isoformat()})."
            )
        )
