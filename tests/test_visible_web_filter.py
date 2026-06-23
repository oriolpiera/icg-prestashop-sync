from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from apps.catalog.models import Combination, Price, Product, Stock
from apps.prestashop.services import export_product
from apps.sync.models import SyncJob
from apps.sync.tasks import (
    export_combinations,
    export_discounts,
    export_prices,
    export_products,
    export_stocks,
)


@pytest.fixture(autouse=True)
def _clean_db():
    SyncJob.objects.all().delete()
    Stock.objects.all().delete()
    Price.objects.all().delete()
    Combination.objects.all().delete()
    Product.objects.all().delete()


def _make_product(**overrides):
    return Product.objects.create(
        icg_id=overrides.pop("icg_id", 1001),
        reference=overrides.pop("reference", "REF001"),
        name=overrides.pop("name", "Product One"),
        visible_web=overrides.pop("visible_web", True),
        discontinued=overrides.pop("discontinued", False),
        discount_percent=overrides.pop("discount_percent", 0),
        **overrides,
    )


def _make_combination(product, **overrides):
    return Combination.objects.create(
        product=product,
        icg_size=overrides.pop("icg_size", "M"),
        icg_color=overrides.pop("icg_color", "Black"),
        ean13=overrides.pop("ean13", "1234567890123"),
        active=overrides.pop("active", True),
        **overrides,
    )


@pytest.mark.django_db
class TestVisibleWebFilter:
    def test_export_products_skips_invisible_without_prestashop_id(self):
        product = _make_product(visible_web=False, sync_required=True)

        result = export_products()

        assert result == {"status": "success", "processed": 0, "failed": 0}
        assert SyncJob.objects.count() == 0
        product.refresh_from_db()
        assert product.sync_required is False

    def test_export_products_includes_invisible_with_prestashop_id(self):
        product = _make_product(visible_web=False, sync_required=True)
        product.prestashop_id = 22
        product.save(update_fields=["prestashop_id"])

        mock_export = Mock(return_value={"product_id": product.pk, "prestashop_id": 22})
        with patch("apps.sync.tasks.export_product", mock_export):
            result = export_products()

        assert result == {"status": "success", "processed": 1, "failed": 0}

    def test_export_products_includes_visible(self):
        product = _make_product(visible_web=True, sync_required=True)

        mock_export = Mock(return_value={"product_id": product.pk, "prestashop_id": 22})
        with patch("apps.sync.tasks.export_product", mock_export):
            result = export_products()

        assert result == {"status": "success", "processed": 1, "failed": 0}

    def test_export_combinations_skips_invisible_product_without_prestashop_id(self):
        product = _make_product(visible_web=False)
        combination = _make_combination(product, sync_required=True)

        result = export_combinations()

        assert result == {"status": "success", "processed": 0, "failed": 0}
        assert SyncJob.objects.count() == 0
        combination.refresh_from_db()
        assert combination.sync_required is False

    def test_export_combinations_includes_invisible_product_with_prestashop_id(self):
        product = _make_product(visible_web=False)
        product.prestashop_id = 22
        product.save(update_fields=["prestashop_id"])
        combination = _make_combination(product, sync_required=True)

        mock_export = Mock(
            return_value={
                "combination_id": combination.pk,
                "prestashop_combination_id": 33,
            }
        )
        with patch("apps.sync.tasks.export_combination", mock_export):
            result = export_combinations()

        assert result == {"status": "success", "processed": 1, "failed": 0}

    def test_export_prices_skips_invisible_product_without_prestashop_id(self):
        product = _make_product(visible_web=False)
        combination = _make_combination(product)
        price = Price.objects.create(
            combination=combination,
            amount_ex_vat=Decimal("10.00"),
            vat_rate=Decimal("21.00"),
            sync_required=True,
        )

        result = export_prices()

        assert result == {"status": "success", "processed": 0, "failed": 0}
        assert SyncJob.objects.count() == 0
        price.refresh_from_db()
        assert price.sync_required is False

    def test_export_stocks_skips_invisible_product_without_prestashop_id(self):
        product = _make_product(visible_web=False)
        combination = _make_combination(product)
        stock = Stock.objects.create(
            combination=combination,
            quantity=10,
            sync_required=True,
        )

        result = export_stocks()

        assert result == {"status": "success", "processed": 0, "failed": 0}
        assert SyncJob.objects.count() == 0
        stock.refresh_from_db()
        assert stock.sync_required is False

    def test_export_discounts_skips_invisible_without_prestashop_id(self):
        product = _make_product(
            visible_web=False,
            discount_percent=Decimal("30"),
            discount_sync_required=True,
        )

        result = export_discounts()

        assert result == {"status": "success", "processed": 0, "failed": 0}
        assert SyncJob.objects.count() == 0
        product.refresh_from_db()
        assert product.discount_sync_required is False

    def test_export_discounts_skips_invisible_with_prestashop_id(self):
        product = _make_product(
            visible_web=False,
            discount_percent=Decimal("30"),
            discount_sync_required=True,
        )
        product.prestashop_id = 22
        product.save(update_fields=["prestashop_id"])

        result = export_discounts()

        assert result == {"status": "success", "processed": 0, "failed": 0}
        assert SyncJob.objects.count() == 0
        product.refresh_from_db()
        assert product.discount_sync_required is True


@pytest.mark.django_db
class TestExportProductVisibleWebGuard:
    def test_export_product_skips_invisible_without_prestashop_id(self):
        product = _make_product(visible_web=False, sync_required=True)

        result = export_product(product.pk)

        assert result["prestashop_id"] is None
        product.refresh_from_db()
        assert product.sync_required is False
