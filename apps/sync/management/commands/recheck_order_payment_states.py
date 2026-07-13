from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.prestashop.client import PrestashopClient
from apps.sales.models import PrestashopOrder
from apps.sales.services import export_order_to_icg_from_mirror, refresh_order_from_prestashop


class Command(BaseCommand):
    help = (
        "Re-check mirrored orders that are not in the payment-accepted state. "
        "For each order, fetch the latest snapshot from Prestashop and export "
        "to ICG if the state has transitioned to payment accepted."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Only re-check orders created within this many days (default: 30)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be re-checked without making changes",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(days=days)

        candidates = (
            PrestashopOrder.objects.filter(
                date_add__gte=cutoff,
            )
            .exclude(
                current_state=settings.PRESTASHOP_ORDER_STATE_PAYMENT_ACCEPTED,
            )
            .order_by("date_add")
        )

        total = candidates.count()
        if total == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"No orders to re-check (non-payment-accepted, last {days} days)."
                )
            )
            return

        self.stdout.write(
            f"Found {total} orders to re-check "
            f"(non-payment-accepted state, created within {days} days)."
        )

        if dry_run:
            for order in candidates:
                self.stdout.write(
                    f"  Order #{order.prestashop_id} — "
                    f"current_state={order.current_state} "
                    f"date_add={order.date_add.isoformat()}"
                )
            return

        client = PrestashopClient()
        exported = 0
        unchanged = 0
        refreshed = 0
        failed = 0

        for order in candidates:
            try:
                refresh_order_from_prestashop(order.prestashop_id, client=client)
                refreshed += 1
            except Exception as exc:
                self.stderr.write(f"Failed to refresh order #{order.prestashop_id}: {exc}")
                failed += 1
                continue

            order.refresh_from_db()
            if order.current_state == settings.PRESTASHOP_ORDER_STATE_PAYMENT_ACCEPTED:
                try:
                    result = export_order_to_icg_from_mirror(order.prestashop_id)
                    exported += 1
                    self.stdout.write(
                        f"Exported order #{order.prestashop_id} to ICG: "
                        f"{result.get('inserted_rows', 0)} rows inserted."
                    )
                except Exception as exc:
                    self.stderr.write(f"Failed to export order #{order.prestashop_id}: {exc}")
                    failed += 1
            else:
                unchanged += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Re-check complete: {refreshed} refreshed, "
                f"{exported} exported, {unchanged} unchanged, {failed} failed."
            )
        )
