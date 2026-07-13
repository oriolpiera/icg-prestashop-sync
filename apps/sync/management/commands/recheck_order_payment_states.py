from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.prestashop.client import PrestashopClient
from apps.sales.models import ExportStatus, PrestashopOrder
from apps.sales.services import export_order_to_icg_from_mirror, refresh_order_from_prestashop
from apps.sync.locking import LockAcquisitionError, sync_lock
from apps.sync.tasks import ICG_SALES_EXPORT_LOCK_KEY


class Command(BaseCommand):
    help = (
        "Re-check mirrored orders that may need exporting to ICG: "
        "orders not in the payment-accepted state (may have transitioned), "
        "and payment-accepted orders that were never successfully exported "
        "(e.g. stuck because list/detail API state disagreed)."
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
        payment_accepted = settings.PRESTASHOP_ORDER_STATE_PAYMENT_ACCEPTED

        candidates = (
            PrestashopOrder.objects.filter(date_add__gte=cutoff)
            .exclude(current_state=0)
            .filter(
                Q(current_state=payment_accepted, export_status=ExportStatus.NEVER)
                | Q(current_state=payment_accepted, export_status=ExportStatus.FAILED)
                | (
                    ~Q(current_state=payment_accepted)
                    & Q(export_status__in=[ExportStatus.NEVER, ExportStatus.FAILED])
                ),
            )
            .order_by("date_add")
        )

        total = candidates.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS(f"No orders to re-check (last {days} days)."))
            return

        self.stdout.write(f"Found {total} orders to re-check (created within {days} days).")

        if dry_run:
            for order in candidates:
                self.stdout.write(
                    f"  Order #{order.prestashop_id} — "
                    f"current_state={order.current_state} "
                    f"export_status={order.export_status} "
                    f"date_add={order.date_add.isoformat()}"
                )
            return

        client = PrestashopClient()
        exported = 0
        unchanged = 0
        refreshed = 0
        failed = 0

        try:
            with sync_lock(ICG_SALES_EXPORT_LOCK_KEY):
                for order in candidates:
                    try:
                        refresh_order_from_prestashop(order.prestashop_id, client=client)
                        refreshed += 1
                    except Exception as exc:
                        self.stderr.write(f"Failed to refresh order #{order.prestashop_id}: {exc}")
                        failed += 1
                        continue

                    order.refresh_from_db()
                    if order.current_state == payment_accepted:
                        try:
                            result = export_order_to_icg_from_mirror(order.prestashop_id)
                            exported += 1
                            self.stdout.write(
                                f"Exported order #{order.prestashop_id} to ICG: "
                                f"{result.get('inserted_rows', 0)} rows inserted."
                            )
                        except Exception as exc:
                            self.stderr.write(
                                f"Failed to export order #{order.prestashop_id}: {exc}"
                            )
                            failed += 1
                    else:
                        unchanged += 1
        except LockAcquisitionError:
            self.stderr.write("Re-check skipped: ICG sales export lock is already held.")
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Re-check complete: {refreshed} refreshed, "
                f"{exported} exported, {unchanged} unchanged, {failed} failed."
            )
        )
