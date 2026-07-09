import logging

from django.core.management.base import BaseCommand

from apps.catalog.models import Combination
from apps.icg.services import ICGCatalogReader

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Bulk refresh prices from ICG for visible_web products with active combinations"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be updated without making changes",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        self.stdout.write("Building combination lookup...")

        combos = Combination.objects.filter(
            product__visible_web=True,
            active=True,
            product__discontinued=False,
        ).select_related("product", "price")

        combo_map = {}
        visible_icg_ids = set()
        for combo in combos:
            key = (combo.product.icg_id, combo.icg_size, combo.icg_color)
            combo_map[key] = combo
            visible_icg_ids.add(combo.product.icg_id)

        self.stdout.write(
            f"Tracking {len(combo_map)} combinations across {len(visible_icg_ids)} ICG products"
        )

        self.stdout.write("Fetching ALL prices from ICG (this may take a minute)...")
        reader = ICGCatalogReader()

        try:
            rows = reader.fetch_all_price_rows()
        except Exception as e:
            logger.error(f"Error fetching prices from ICG: {e}")
            self.stderr.write(self.style.ERROR(f"Failed to fetch prices: {e}"))
            return

        self.stdout.write(f"Fetched {len(rows)} price rows from ICG")

        updated = 0
        skipped = 0
        total = 0

        for row in rows:
            total += 1
            icg_id = int(row[0])

            if icg_id not in visible_icg_ids:
                continue

            size = str(row[1]).strip()
            color = str(row[2]).strip()
            key = (icg_id, size, color)

            combo = combo_map.get(key)
            if not combo:
                skipped += 1
                continue

            price_obj = getattr(combo, "price", None)
            if not price_obj:
                skipped += 1
                continue

            new_vat = row[3]
            new_amount = row[4]

            if dry_run:
                old_amount = price_obj.amount_ex_vat
                if old_amount != new_amount or price_obj.vat_rate != new_vat:
                    self.stdout.write(
                        f"  Would update {icg_id}/{size}/{color}: amount "
                        f"{old_amount} -> {new_amount}, vat {price_obj.vat_rate} -> {new_vat}"
                    )
                    updated += 1
            else:
                changed = False
                if price_obj.amount_ex_vat != new_amount:
                    price_obj.amount_ex_vat = new_amount
                    changed = True
                if price_obj.vat_rate != new_vat:
                    price_obj.vat_rate = new_vat
                    changed = True
                if changed:
                    price_obj.sync_required = True
                    price_obj.save(
                        update_fields=["amount_ex_vat", "vat_rate", "sync_required", "updated_at"]
                    )
                    updated += 1

            if total % 10000 == 0:
                self.stdout.write(
                    f"  Progress: {total} rows processed, {updated} updated, {skipped} skipped"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {updated} updated, {skipped} skipped (of {total} total ICG rows)"
            )
        )
