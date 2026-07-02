from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


@pytest.mark.django_db
class TestRebuildReferenceFromICG:
    def test_rebuild_reference_replays_product_price_and_stock_rows(self):
        product_rows = [
            [
                1001,
                "0090837",
                "",
                "B00",
                "ean",
                None,
                "Copic",
                None,
                None,
                None,
                None,
                None,
                "T",
                None,
                None,
                "F",
            ],
            [
                1001,
                "0090837",
                "",
                "B01",
                "ean",
                None,
                "Copic",
                None,
                None,
                None,
                None,
                None,
                "T",
                None,
                None,
                "F",
            ],
        ]
        price_row = [None, 1001, "", "B00", None, 0, None, None, 21, None, 10.5, None, None]
        stock_row = [1001, "", "B00", "01", None, None, None, 4, None]

        with (
            patch(
                "apps.icg.management.commands.rebuild_reference_from_icg.ICGCatalogReader"
            ) as reader_cls,
            patch(
                "apps.icg.management.commands.rebuild_reference_from_icg._persist_product_row"
            ) as persist_product,
            patch(
                "apps.icg.management.commands.rebuild_reference_from_icg._persist_price_row"
            ) as persist_price,
            patch(
                "apps.icg.management.commands.rebuild_reference_from_icg._persist_stock_row"
            ) as persist_stock,
        ):
            reader = reader_cls.return_value
            reader.fetch_product_rows_by_reference.return_value = product_rows
            reader.fetch_price_rows_for_combination.side_effect = [[price_row], []]
            reader.fetch_stock_rows_for_combination.side_effect = [[stock_row], []]

            out = StringIO()
            call_command("rebuild_reference_from_icg", "0090837", stdout=out)

        assert persist_product.call_count == 2
        persist_price.assert_called_once_with(price_row)
        persist_stock.assert_called_once_with(stock_row)
        assert (
            "Rebuilt reference 0090837: product_rows=2 price_rows=1 stock_rows=1" in out.getvalue()
        )
        assert (
            "Missing source rows: price_combinations_without_rows=1 "
            "stock_combinations_without_rows=1" in out.getvalue()
        )

    def test_rebuild_reference_fails_when_reference_has_no_rows(self):
        with patch(
            "apps.icg.management.commands.rebuild_reference_from_icg.ICGCatalogReader"
        ) as reader_cls:
            reader_cls.return_value.fetch_product_rows_by_reference.return_value = []

            with pytest.raises(CommandError, match="No ICG product rows found"):
                call_command("rebuild_reference_from_icg", "0090837", stdout=StringIO())
