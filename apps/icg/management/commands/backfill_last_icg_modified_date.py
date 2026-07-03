from django.core.management.base import BaseCommand
from django.db.models import Max

from apps.catalog.models import Combination, Manufacturer, Price, Product, Stock
from apps.icg.importer import (
    refresh_combination_from_icg,
    refresh_price_from_icg,
    refresh_product_from_icg,
    refresh_stock_from_icg,
)


def _run_refresh_batch(queryset, refresh_fn) -> dict:
    stats = {"updated": 0, "skipped": 0, "failed": 0}
    for obj_id in queryset.values_list("pk", flat=True).iterator():
        try:
            result = refresh_fn(obj_id)
        except Exception:
            stats["failed"] += 1
            continue

        status = result.get("status")
        if status == "updated":
            stats["updated"] += 1
        elif status == "skipped":
            stats["skipped"] += 1
        else:
            stats["failed"] += 1
    return stats


def _backfill_manufacturers_from_products() -> dict:
    stats = {"updated": 0, "skipped": 0}
    queryset = Manufacturer.objects.filter(last_icg_modified_date__isnull=True).annotate(
        latest_product_modified=Max("products__last_icg_modified_date")
    )

    for manufacturer in queryset.iterator():
        if manufacturer.latest_product_modified is None:
            stats["skipped"] += 1
            continue
        manufacturer.last_icg_modified_date = manufacturer.latest_product_modified
        manufacturer.save(update_fields=["last_icg_modified_date", "updated_at"])
        stats["updated"] += 1

    return stats


class Command(BaseCommand):
    help = "Backfill last_icg_modified_date on catalog entities that still have it empty."

    def handle(self, *args, **options):
        product_stats = _run_refresh_batch(
            Product.objects.filter(last_icg_modified_date__isnull=True).order_by("pk"),
            refresh_product_from_icg,
        )
        combination_stats = _run_refresh_batch(
            Combination.objects.filter(last_icg_modified_date__isnull=True).order_by("pk"),
            refresh_combination_from_icg,
        )
        price_stats = _run_refresh_batch(
            Price.objects.filter(last_icg_modified_date__isnull=True).order_by("pk"),
            refresh_price_from_icg,
        )
        stock_stats = _run_refresh_batch(
            Stock.objects.filter(last_icg_modified_date__isnull=True).order_by("pk"),
            refresh_stock_from_icg,
        )
        manufacturer_stats = _backfill_manufacturers_from_products()
        summary = " ".join(
            [
                "Backfilled last_icg_modified_date:",
                (
                    "products("
                    f"updated={product_stats['updated']}, "
                    f"skipped={product_stats['skipped']}, "
                    f"failed={product_stats['failed']})"
                ),
                (
                    "combinations("
                    f"updated={combination_stats['updated']}, "
                    f"skipped={combination_stats['skipped']}, "
                    f"failed={combination_stats['failed']})"
                ),
                (
                    "prices("
                    f"updated={price_stats['updated']}, "
                    f"skipped={price_stats['skipped']}, "
                    f"failed={price_stats['failed']})"
                ),
                (
                    "stock("
                    f"updated={stock_stats['updated']}, "
                    f"skipped={stock_stats['skipped']}, "
                    f"failed={stock_stats['failed']})"
                ),
                (
                    "manufacturers("
                    f"updated={manufacturer_stats['updated']}, "
                    f"skipped={manufacturer_stats['skipped']})"
                ),
            ]
        )

        self.stdout.write(self.style.SUCCESS(summary))
