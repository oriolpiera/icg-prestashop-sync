from datetime import UTC, datetime
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.catalog.models import Combination, Manufacturer, Price, Product, Stock


@pytest.mark.django_db
class TestBackfillLastICGModifiedDateCommand:
    def test_backfill_refreshes_only_null_rows_and_derives_manufacturer_dates(self):
        manufacturer = Manufacturer.objects.create(icg_code="M-1", name="Maker")
        other_manufacturer = Manufacturer.objects.create(
            icg_code="M-2",
            name="Other Maker",
            last_icg_modified_date=datetime(2026, 1, 10, 10, 0, 0, tzinfo=UTC),
        )

        product = Product.objects.create(
            icg_id=1001,
            reference="REF001",
            name="Product One",
            manufacturer=manufacturer,
        )
        other_product = Product.objects.create(
            icg_id=1002,
            reference="REF002",
            name="Product Two",
            manufacturer=other_manufacturer,
            last_icg_modified_date=datetime(2026, 1, 9, 9, 0, 0, tzinfo=UTC),
        )

        combination = Combination.objects.create(product=product, icg_size="M", icg_color="RED")
        other_combination = Combination.objects.create(
            product=other_product,
            icg_size="L",
            icg_color="BLUE",
            last_icg_modified_date=datetime(2026, 1, 9, 9, 0, 0, tzinfo=UTC),
        )

        price = Price.objects.create(
            combination=combination,
            amount_ex_vat="10.00",
            vat_rate="21.00",
        )
        other_price = Price.objects.create(
            combination=other_combination,
            amount_ex_vat="12.00",
            vat_rate="21.00",
            last_icg_modified_date=datetime(2026, 1, 9, 9, 0, 0, tzinfo=UTC),
        )

        stock = Stock.objects.create(combination=combination, warehouse_code="01", quantity=3)
        other_stock = Stock.objects.create(
            combination=other_combination,
            warehouse_code="01",
            quantity=5,
            last_icg_modified_date=datetime(2026, 1, 9, 9, 0, 0, tzinfo=UTC),
        )

        expected_date = datetime(2026, 2, 1, 10, 0, 0, tzinfo=UTC)

        def _refresh_product(product_id):
            obj = Product.objects.get(pk=product_id)
            obj.last_icg_modified_date = expected_date
            obj.sync_required = False
            obj.save(update_fields=["last_icg_modified_date", "sync_required", "updated_at"])
            return {"status": "updated", "processed": 1, "skipped": 0}

        def _refresh_combination(combination_id):
            obj = Combination.objects.get(pk=combination_id)
            obj.last_icg_modified_date = expected_date
            obj.sync_required = False
            obj.save(update_fields=["last_icg_modified_date", "sync_required", "updated_at"])
            return {"status": "updated", "processed": 1, "skipped": 0}

        def _refresh_price(price_id):
            obj = Price.objects.get(pk=price_id)
            obj.last_icg_modified_date = expected_date
            obj.sync_required = False
            obj.save(update_fields=["last_icg_modified_date", "sync_required", "updated_at"])
            return {"status": "updated", "processed": 1, "skipped": 0}

        def _refresh_stock(stock_id):
            obj = Stock.objects.get(pk=stock_id)
            obj.last_icg_modified_date = expected_date
            obj.sync_required = False
            obj.save(update_fields=["last_icg_modified_date", "sync_required", "updated_at"])
            return {"status": "updated", "processed": 1, "skipped": 0}

        with (
            patch(
                "apps.icg.management.commands.backfill_last_icg_modified_date.refresh_product_from_icg",
                side_effect=_refresh_product,
            ) as refresh_product,
            patch(
                "apps.icg.management.commands.backfill_last_icg_modified_date.refresh_combination_from_icg",
                side_effect=_refresh_combination,
            ) as refresh_combination,
            patch(
                "apps.icg.management.commands.backfill_last_icg_modified_date.refresh_price_from_icg",
                side_effect=_refresh_price,
            ) as refresh_price,
            patch(
                "apps.icg.management.commands.backfill_last_icg_modified_date.refresh_stock_from_icg",
                side_effect=_refresh_stock,
            ) as refresh_stock,
        ):
            out = StringIO()
            call_command("backfill_last_icg_modified_date", stdout=out)

        refresh_product.assert_called_once_with(product.pk)
        refresh_combination.assert_called_once_with(combination.pk)
        refresh_price.assert_called_once_with(price.pk)
        refresh_stock.assert_called_once_with(stock.pk)

        product.refresh_from_db()
        combination.refresh_from_db()
        price.refresh_from_db()
        stock.refresh_from_db()
        manufacturer.refresh_from_db()
        other_manufacturer.refresh_from_db()
        other_product.refresh_from_db()
        other_price.refresh_from_db()
        other_stock.refresh_from_db()

        assert product.last_icg_modified_date == expected_date
        assert combination.last_icg_modified_date == expected_date
        assert price.last_icg_modified_date == expected_date
        assert stock.last_icg_modified_date == expected_date
        assert manufacturer.last_icg_modified_date == expected_date
        assert other_manufacturer.last_icg_modified_date == datetime(
            2026, 1, 10, 10, 0, 0, tzinfo=UTC
        )
        assert other_product.last_icg_modified_date == datetime(2026, 1, 9, 9, 0, 0, tzinfo=UTC)
        assert other_price.last_icg_modified_date == datetime(2026, 1, 9, 9, 0, 0, tzinfo=UTC)
        assert other_stock.last_icg_modified_date == datetime(2026, 1, 9, 9, 0, 0, tzinfo=UTC)
        assert "Backfilled last_icg_modified_date:" in out.getvalue()

    def test_backfill_counts_skipped_and_failed_refreshes(self):
        manufacturer = Manufacturer.objects.create(icg_code="M-1", name="Maker")
        product = Product.objects.create(
            icg_id=1001,
            reference="REF001",
            name="Product One",
            manufacturer=manufacturer,
        )
        combination = Combination.objects.create(product=product, icg_size="M", icg_color="RED")
        Price.objects.create(combination=combination, amount_ex_vat="10.00", vat_rate="21.00")
        Stock.objects.create(combination=combination, warehouse_code="01", quantity=3)

        with (
            patch(
                "apps.icg.management.commands.backfill_last_icg_modified_date.refresh_product_from_icg",
                return_value={"status": "skipped", "processed": 0, "skipped": 1},
            ),
            patch(
                "apps.icg.management.commands.backfill_last_icg_modified_date.refresh_combination_from_icg",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "apps.icg.management.commands.backfill_last_icg_modified_date.refresh_price_from_icg",
                return_value={"status": "updated", "processed": 1, "skipped": 0},
            ),
            patch(
                "apps.icg.management.commands.backfill_last_icg_modified_date.refresh_stock_from_icg",
                return_value={"status": "updated", "processed": 1, "skipped": 0},
            ),
        ):
            out = StringIO()
            call_command("backfill_last_icg_modified_date", stdout=out)

        output = out.getvalue()
        assert "products(updated=0, skipped=1, failed=0)" in output
        assert "combinations(updated=0, skipped=0, failed=1)" in output

    def test_backfill_logs_refresh_failures_with_context(self):
        manufacturer = Manufacturer.objects.create(icg_code="M-1", name="Maker")
        product = Product.objects.create(
            icg_id=1001,
            reference="REF001",
            name="Product One",
            manufacturer=manufacturer,
        )

        with (
            patch(
                "apps.icg.management.commands.backfill_last_icg_modified_date.refresh_product_from_icg",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "apps.icg.management.commands.backfill_last_icg_modified_date.logger"
            ) as mock_logger,
        ):
            call_command("backfill_last_icg_modified_date", stdout=StringIO())

        mock_logger.exception.assert_called_once_with(
            "Failed to backfill last_icg_modified_date for %s pk=%s",
            "product",
            product.pk,
        )
