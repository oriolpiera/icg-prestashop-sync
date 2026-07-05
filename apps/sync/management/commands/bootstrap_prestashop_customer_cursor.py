from django.core.management.base import BaseCommand, CommandError

from apps.prestashop.client import PrestashopClient
from apps.sync.cursor_service import advance_cursor, get_or_create_cursor
from apps.sync.models import SyncCursorSource


class Command(BaseCommand):
    help = "Set the customer sync cursor to the latest customer currently present in Prestashop"

    def add_arguments(self, parser):
        parser.add_argument(
            "--customer-id",
            type=int,
            help="Set the cursor to a specific Prestashop customer ID instead of the latest one.",
        )
        parser.add_argument(
            "--allow-rewind",
            action="store_true",
            help="Allow moving the cursor backwards to an older Prestashop customer.",
        )

    def handle(self, *args, **options):
        client = PrestashopClient()
        customer_id = options.get("customer_id")
        if customer_id is not None:
            snapshot = client.get_customer_snapshot(customer_id)
            target_id = snapshot.customer_id
            target_date = snapshot.date_add
        else:
            latest = client.get_latest_customer_summary()
            if latest is None:
                raise CommandError("No Prestashop customers were returned; cursor not changed.")
            target_id = latest.customer_id
            target_date = latest.date_add

        cursor = get_or_create_cursor(SyncCursorSource.CUSTOMERS)
        previous_key = cursor.last_source_key or "0"
        previous_date = cursor.last_modified_at
        if previous_date is not None:
            current_position = (previous_date, int(previous_key))
            target_position = (target_date, target_id)
            if target_position < current_position and not options.get("allow_rewind", False):
                raise CommandError(
                    "Refusing to rewind the customer cursor without --allow-rewind. "
                    f"Current cursor is #{previous_key} ({previous_date.isoformat()}) and "
                    f"requested cursor is #{target_id} ({target_date.isoformat()})."
                )

        advance_cursor(SyncCursorSource.CUSTOMERS, target_date, str(target_id))
        self.stdout.write(
            self.style.SUCCESS(
                "Customer cursor set to Prestashop customer "
                f"#{target_id} ({target_date.isoformat()})."
            )
        )
        if previous_date is not None:
            self.stdout.write(f"Previous cursor was #{previous_key} ({previous_date.isoformat()}).")
