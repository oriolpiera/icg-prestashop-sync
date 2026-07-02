from django.core.management.base import BaseCommand, CommandError

from apps.icg.importer import _persist_price_row, _persist_product_row, _persist_stock_row
from apps.icg.services import ICGCatalogReader


class Command(BaseCommand):
    help = (
        "Rebuild one product reference from ICG into Django by replaying product, price, and "
        "stock rows for all combinations of that reference."
    )

    def add_arguments(self, parser):
        parser.add_argument("reference", help="ICG reference to rebuild from source data")

    def handle(self, *args, **options):
        reference = options["reference"].strip()
        if not reference:
            raise CommandError("reference is required")

        reader = ICGCatalogReader()
        product_rows = reader.fetch_product_rows_by_reference(reference)
        if not product_rows:
            raise CommandError(f"No ICG product rows found for reference {reference}")

        product_processed = 0
        price_processed = 0
        stock_processed = 0
        missing_prices = 0
        missing_stocks = 0

        for row in product_rows:
            _persist_product_row(row)
            product_processed += 1

            icg_id = int(row[0])
            talla = str(row[2])
            color = str(row[3])

            price_rows = reader.fetch_price_rows_for_combination(icg_id, talla, color)
            if price_rows:
                for price_row in price_rows:
                    _persist_price_row(price_row)
                    price_processed += 1
            else:
                missing_prices += 1

            stock_rows = reader.fetch_stock_rows_for_combination(icg_id, talla, color)
            if stock_rows:
                for stock_row in stock_rows:
                    _persist_stock_row(stock_row)
                    stock_processed += 1
            else:
                missing_stocks += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Rebuilt reference {reference}: "
                f"product_rows={product_processed} "
                f"price_rows={price_processed} "
                f"stock_rows={stock_processed}"
            )
        )
        if missing_prices or missing_stocks:
            self.stdout.write(
                self.style.WARNING(
                    "Missing source rows: "
                    f"price_combinations_without_rows={missing_prices} "
                    f"stock_combinations_without_rows={missing_stocks}"
                )
            )
