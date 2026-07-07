from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.catalog.models import Combination, Manufacturer, Price, Product, Stock
from apps.icg.importer import (
    _escape,
    import_prices,
    import_products,
    import_stock,
    refresh_combination_from_icg,
    refresh_price_from_icg,
    refresh_product_from_icg,
    refresh_stock_from_icg,
)
from apps.sync.cursor_service import advance_cursor, get_or_create_cursor
from apps.sync.models import SyncCursorSource, SyncJob, SyncJobType


class _FakeRow:
    def __init__(self, *values):
        self._values = values

    def __getitem__(self, idx):
        return self._values[idx]

    def __len__(self):
        return len(self._values)


PRODUCT_ROWS = [
    _FakeRow(
        1001,
        "REF001",
        "M",
        "RED",
        "1234567890123",
        "",
        "Product One",
        1,
        21,
        93,
        "TALENS",
        datetime(2026, 1, 15, 10, 0, 0),
        "T",
        14000,
        "ARTECREATION",
        "F",
    ),
    _FakeRow(
        1001,
        "REF001",
        "L",
        "BLUE",
        "1234567890124",
        "",
        "Product One",
        1,
        21,
        93,
        "TALENS",
        datetime(2026, 1, 15, 10, 0, 0),
        "T",
        14000,
        "ARTECREATION",
        "F",
    ),
    _FakeRow(
        1002,
        "REF002",
        "",
        "",
        "9876543210987",
        "",
        "Product Two",
        1,
        10,
        45,
        "BRAND X",
        datetime(2026, 2, 1, 8, 30, 0),
        "F",
        15000,
        "BRAND X",
        "T",
    ),
]

PRICE_ROWS = [
    _FakeRow(
        1,
        1001,
        "M",
        "RED",
        121.00,
        10,
        108.90,
        12.10,
        21,
        100.00,
        90.00,
        10.00,
        datetime(2026, 1, 20, 12, 0, 0),
    ),
    _FakeRow(
        1,
        1002,
        "",
        "",
        60.50,
        0,
        60.50,
        0,
        10,
        55.00,
        55.00,
        0,
        datetime(2026, 2, 5, 14, 0, 0),
    ),
]

STOCK_ROWS = [
    _FakeRow(1001, "M", "RED", "01", "Main WH", 20, 0, 20, datetime(2026, 1, 25, 9, 0, 0)),
    _FakeRow(1001, "L", "BLUE", "01", "Main WH", 15, 2, 13, datetime(2026, 1, 25, 9, 0, 0)),
    _FakeRow(1002, "", "", "01", "Main WH", 5, 0, 5, datetime(2026, 2, 10, 11, 0, 0)),
    _FakeRow(1001, "M", "RED", "02", "Secondary WH", 3, 0, 3, datetime(2026, 1, 25, 9, 0, 0)),
]


@pytest.fixture(autouse=True)
def _clean_db():
    SyncJob.objects.all().delete()
    for model in [Stock, Price, Combination, Product, Manufacturer]:
        model.objects.all().delete()


def _make_manufacturer():
    return Manufacturer.objects.create(icg_code="14000", name="ARTECREATION")


def _make_product(manufacturer, icg_id=1001, reference="REF001", name="Product One"):
    return Product.objects.create(
        icg_id=icg_id,
        reference=reference,
        name=name,
        manufacturer=manufacturer,
    )


def _assert_local_naive_dt(value, expected_naive: datetime):
    assert timezone.localtime(value).replace(tzinfo=None) == expected_naive


@pytest.mark.django_db
class TestCursorService:
    def test_get_or_create_returns_cursor(self):
        cursor = get_or_create_cursor(SyncCursorSource.PRODUCTS)
        assert cursor.source == SyncCursorSource.PRODUCTS.value
        assert cursor.last_modified_at is None

    def test_cursor_is_unique_per_source(self):
        c1 = get_or_create_cursor(SyncCursorSource.PRODUCTS)
        c2 = get_or_create_cursor(SyncCursorSource.PRODUCTS)
        assert c1.pk == c2.pk

    def test_advance_cursor_updates_last_modified_at(self):
        get_or_create_cursor(SyncCursorSource.PRODUCTS)
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        advanced = advance_cursor(SyncCursorSource.PRODUCTS, now)
        assert advanced.last_modified_at == now


@pytest.mark.django_db
class TestProductImport:
    def test_import_creates_manufacturer_product_combination_and_jobs(self):
        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_products_after.return_value = (PRODUCT_ROWS, False)
            result = import_products()

        assert result["status"] == "success"
        assert result["processed"] == 3
        assert result["skipped"] == 0

        assert Manufacturer.objects.count() == 2
        assert Product.objects.count() == 2
        assert Combination.objects.count() == 3

        prod1 = Product.objects.get(icg_id=1001)
        assert prod1.reference == "REF001"
        assert prod1.name == "Product One"
        assert prod1.visible_web is True
        assert prod1.discontinued is False
        _assert_local_naive_dt(prod1.last_icg_modified_date, datetime(2026, 1, 15, 10, 0, 0))

        manufacturer = Manufacturer.objects.get(icg_code="14000")
        _assert_local_naive_dt(manufacturer.last_icg_modified_date, datetime(2026, 1, 15, 10, 0, 0))

        combination = Combination.objects.get(product__icg_id=1001, icg_size="M", icg_color="RED")
        _assert_local_naive_dt(combination.last_icg_modified_date, datetime(2026, 1, 15, 10, 0, 0))

        prod2 = Product.objects.get(icg_id=1002)
        assert prod2.reference == "REF002"
        assert prod2.visible_web is False
        assert prod2.discontinued is True

        assert SyncJob.objects.filter(job_type=SyncJobType.IMPORT_PRODUCTS).count() == 3

    def test_import_is_idempotent(self):
        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_products_after.return_value = (PRODUCT_ROWS, False)
            import_products()

        product_count = Product.objects.count()
        job_count = SyncJob.objects.count()

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_products_after.return_value = ([], False)
            import_products()

        assert Product.objects.count() == product_count
        assert SyncJob.objects.count() == job_count

    def test_cursor_advances_after_successful_import(self):
        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_products_after.return_value = (PRODUCT_ROWS, False)
            import_products()

        cursor = get_or_create_cursor(SyncCursorSource.PRODUCTS)
        assert cursor.last_modified_at is not None

    def test_cursor_not_advanced_on_hard_failure(self):
        def failing_fn(row):
            raise RuntimeError("DB write failed")

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_products_after.return_value = (PRODUCT_ROWS, False)

        with patch("apps.icg.importer._persist_product_row", side_effect=failing_fn):
            result = import_products()

        assert result["processed"] == 0
        cursor = get_or_create_cursor(SyncCursorSource.PRODUCTS)
        assert cursor.last_modified_at is None

    def test_discontinued_product_combination_not_active(self):
        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_products_after.return_value = (PRODUCT_ROWS, False)
            import_products()

        comb = Combination.objects.get(product__icg_id=1002)
        assert comb.active is False

    def test_sync_required_flag_on_new_records(self):
        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_products_after.return_value = (PRODUCT_ROWS, False)
            import_products()

        for p in Product.objects.all():
            assert p.sync_required is True
        for c in Combination.objects.all():
            assert c.sync_required is True

    def test_import_normalizes_multi_value_ean13_to_first_token(self):
        row = _FakeRow(
            14362,
            "0930788",
            "COLORES",
            "12",
            "8712079454364 09360325024 8712079454333 0936032501",
            "",
            "Product Three",
            1,
            21,
            93,
            "TALENS",
            datetime(2026, 3, 1, 10, 0, 0),
            "T",
            14000,
            "ARTECREATION",
            "F",
        )

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_products_after.return_value = ([row], False)
            result = import_products()

        assert result["processed"] == 1
        combination = Combination.objects.get(product__icg_id=14362)
        assert combination.ean13 == "8712079454364"

    def test_import_allows_duplicate_references_for_different_icg_ids(self):
        duplicate_rows = [
            _FakeRow(
                11006,
                "0090496",
                "",
                "",
                "1234567890123",
                "",
                "Tablero Inclinable",
                1,
                21,
                93,
                "TALENS",
                datetime(2026, 3, 1, 10, 0, 0),
                "T",
                14000,
                "ARTECREATION",
                "F",
            ),
            _FakeRow(
                9650,
                "0090496",
                "",
                "",
                "1234567890456",
                "",
                "Pintar x Números Acrílico Intermedio",
                1,
                21,
                93,
                "TALENS",
                datetime(2026, 3, 1, 10, 5, 0),
                "T",
                14000,
                "ARTECREATION",
                "F",
            ),
        ]

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_products_after.return_value = (duplicate_rows, False)
            result = import_products()

        assert result["processed"] == 2
        assert Product.objects.filter(reference="0090496").count() == 2
        assert Product.objects.filter(icg_id__in=[11006, 9650]).count() == 2

    def test_multiple_batches_process_all_rows(self):
        def fetch_side_effect(cursor_at=None, last_source_key="", limit=5000):
            if cursor_at is None:
                return (PRODUCT_ROWS[:2], True)
            return (PRODUCT_ROWS[2:], False)

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_products_after.side_effect = fetch_side_effect
            result = import_products()

        assert result["processed"] == 3
        assert Product.objects.count() == 2
        assert Combination.objects.count() == 3

    def test_import_updates_last_icg_modified_date_without_new_sync_job(self):
        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_products_after.return_value = (PRODUCT_ROWS[:1], False)
            import_products()

        product = Product.objects.get(icg_id=1001)
        combination = Combination.objects.get(product=product, icg_size="M", icg_color="RED")
        manufacturer = Manufacturer.objects.get(icg_code="14000")
        product.sync_required = False
        combination.sync_required = False
        manufacturer.sync_required = False
        product.save(update_fields=["sync_required", "updated_at"])
        combination.save(update_fields=["sync_required", "updated_at"])
        manufacturer.save(update_fields=["sync_required", "updated_at"])
        initial_job_count = SyncJob.objects.count()

        updated_row = _FakeRow(
            1001,
            "REF001",
            "M",
            "RED",
            "1234567890123",
            "",
            "Product One",
            1,
            21,
            93,
            "TALENS",
            datetime(2026, 1, 16, 10, 0, 0),
            "T",
            14000,
            "ARTECREATION",
            "F",
        )

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_products_after.return_value = ([updated_row], False)
            import_products()

        product.refresh_from_db()
        combination.refresh_from_db()
        manufacturer.refresh_from_db()

        expected = datetime(2026, 1, 16, 10, 0, 0)
        _assert_local_naive_dt(product.last_icg_modified_date, expected)
        _assert_local_naive_dt(combination.last_icg_modified_date, expected)
        _assert_local_naive_dt(manufacturer.last_icg_modified_date, expected)
        assert product.sync_required is False
        assert combination.sync_required is False
        assert manufacturer.sync_required is False
        assert SyncJob.objects.count() == initial_job_count


@pytest.mark.django_db
class TestPriceImport:
    def test_import_skips_when_product_does_not_exist(self):
        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_prices_after.return_value = (PRICE_ROWS, False)
            result = import_prices()

        assert result["skipped"] == 2
        assert result["processed"] == 0

    def test_import_creates_prices_and_jobs(self):
        man = _make_manufacturer()
        prod = _make_product(man)
        comb = Combination.objects.create(product=prod, icg_size="M", icg_color="RED")

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_prices_after.return_value = (PRICE_ROWS[:1], False)
            result = import_prices()

        assert result["processed"] == 1
        assert Price.objects.count() == 1
        price = Price.objects.get(combination=comb)
        assert price.amount_ex_vat == 90.00
        assert price.vat_rate == 21
        _assert_local_naive_dt(price.last_icg_modified_date, datetime(2026, 1, 20, 12, 0, 0))
        assert SyncJob.objects.filter(job_type=SyncJobType.IMPORT_PRICES).count() == 1

    def test_price_update_changes_existing_record(self):
        man = _make_manufacturer()
        prod = _make_product(man)
        comb = Combination.objects.create(product=prod, icg_size="M", icg_color="RED")
        Price.objects.create(combination=comb, amount_ex_vat=99.99, vat_rate=21)

        updated_row = _FakeRow(
            1,
            1001,
            "M",
            "RED",
            121.00,
            10,
            108.90,
            12.10,
            21,
            100.00,
            85.00,
            10.00,
            datetime(2026, 1, 20, 12, 0, 0),
        )

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_prices_after.return_value = ([updated_row], False)
            import_prices()

        price = Price.objects.get(combination=comb)
        assert price.amount_ex_vat == 85.00

    def test_price_import_updates_last_icg_modified_date_without_new_sync_job(self):
        man = _make_manufacturer()
        prod = _make_product(man)
        comb = Combination.objects.create(product=prod, icg_size="M", icg_color="RED")

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_prices_after.return_value = (PRICE_ROWS[:1], False)
            import_prices()

        price = Price.objects.get(combination=comb)
        price.sync_required = False
        price.save(update_fields=["sync_required", "updated_at"])
        initial_job_count = SyncJob.objects.count()

        updated_row = _FakeRow(
            1,
            1001,
            "M",
            "RED",
            121.00,
            10,
            108.90,
            12.10,
            21,
            100.00,
            90.00,
            10.00,
            datetime(2026, 1, 21, 12, 0, 0),
        )

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_prices_after.return_value = ([updated_row], False)
            import_prices()

        price.refresh_from_db()
        _assert_local_naive_dt(price.last_icg_modified_date, datetime(2026, 1, 21, 12, 0, 0))
        assert price.sync_required is False
        assert SyncJob.objects.count() == initial_job_count

    def test_price_skipped_when_combination_not_found(self):
        man = _make_manufacturer()
        _make_product(man)

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_prices_after.return_value = (PRICE_ROWS[:1], False)
            result = import_prices()

        assert result["skipped"] >= 1
        assert Price.objects.count() == 0

    def test_discount_last_row_wins(self):
        man = _make_manufacturer()
        prod = _make_product(man)
        Combination.objects.create(product=prod, icg_size="M", icg_color="RED")
        Combination.objects.create(product=prod, icg_size="L", icg_color="BLUE")

        row_low = _FakeRow(
            1,
            1001,
            "M",
            "RED",
            121.00,
            5,
            114.95,
            6.05,
            21,
            100.00,
            95.00,
            5.00,
            datetime(2026, 1, 20, 12, 0, 0),
        )
        row_high = _FakeRow(
            1,
            1001,
            "L",
            "BLUE",
            121.00,
            15,
            102.85,
            18.15,
            21,
            100.00,
            85.00,
            15.00,
            datetime(2026, 1, 20, 12, 0, 0),
        )

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_prices_after.return_value = ([row_low, row_high], False)
            import_prices()

        prod.refresh_from_db()
        assert prod.discount_percent == Decimal("15")


@pytest.mark.django_db
class TestStockImport:
    def test_import_skips_non_primary_warehouse(self):
        man = _make_manufacturer()
        prod = _make_product(man)
        Combination.objects.create(product=prod, icg_size="M", icg_color="RED")

        rows = [
            _FakeRow(1001, "M", "RED", "02", "Other WH", 3, 0, 3, datetime(2026, 1, 25, 9, 0, 0)),
        ]

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_stock_after.return_value = (rows, False)
            result = import_stock()

        assert result["skipped"] == 1
        assert Stock.objects.count() == 0

    def test_import_creates_stock_and_jobs(self):
        man = _make_manufacturer()
        prod = _make_product(man)
        comb = Combination.objects.create(product=prod, icg_size="M", icg_color="RED")

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_stock_after.return_value = (STOCK_ROWS[:1], False)
            result = import_stock()

        assert result["processed"] == 1
        stock = Stock.objects.get(combination=comb)
        assert stock.quantity == 20
        assert stock.warehouse_code == "01"
        _assert_local_naive_dt(stock.last_icg_modified_date, datetime(2026, 1, 25, 9, 0, 0))
        assert SyncJob.objects.filter(job_type=SyncJobType.IMPORT_STOCK).count() == 1

    def test_import_negative_quantity_becomes_zero(self):
        man = _make_manufacturer()
        prod = _make_product(man)
        Combination.objects.create(product=prod, icg_size="M", icg_color="RED")

        negative = _FakeRow(
            1001,
            "M",
            "RED",
            "01",
            "Main",
            0,
            0,
            -5,
            datetime(2026, 1, 25, 9, 0, 0),
        )

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_stock_after.return_value = ([negative], False)
            import_stock()

        stock = Stock.objects.get(combination__product__icg_id=1001)
        assert stock.quantity == 0

    def test_stock_import_updates_last_icg_modified_date_without_new_sync_job(self):
        man = _make_manufacturer()
        prod = _make_product(man)
        comb = Combination.objects.create(product=prod, icg_size="M", icg_color="RED")

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_stock_after.return_value = (STOCK_ROWS[:1], False)
            import_stock()

        stock = Stock.objects.get(combination=comb)
        stock.sync_required = False
        stock.save(update_fields=["sync_required", "updated_at"])
        initial_job_count = SyncJob.objects.count()

        updated_row = _FakeRow(
            1001,
            "M",
            "RED",
            "01",
            "Main WH",
            20,
            0,
            20,
            datetime(2026, 1, 26, 9, 0, 0),
        )

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_stock_after.return_value = ([updated_row], False)
            import_stock()

        stock.refresh_from_db()
        _assert_local_naive_dt(stock.last_icg_modified_date, datetime(2026, 1, 26, 9, 0, 0))
        assert stock.sync_required is False
        assert SyncJob.objects.count() == initial_job_count

    def test_import_skips_non_existing_product(self):
        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_stock_after.return_value = (STOCK_ROWS, False)
            result = import_stock()

        assert result["skipped"] == 4
        assert Stock.objects.count() == 0

    def test_cross_imports_independent(self):
        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_stock_after.return_value = (STOCK_ROWS, False)
            result = import_stock()

        assert result["skipped"] == 4
        assert Price.objects.count() == 0


@pytest.mark.django_db
class TestTargetedRefresh:
    def test_refresh_product_from_icg_updates_product_and_combinations(self):
        man = _make_manufacturer()
        product = _make_product(man, name="Old Name")
        Combination.objects.create(product=product, icg_size="M", icg_color="RED", ean13="old-ean")

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_product_rows.return_value = PRODUCT_ROWS[:2]
            result = refresh_product_from_icg(product.pk)

        assert result["status"] == "updated"
        product.refresh_from_db()
        assert product.name == "Product One"
        assert Combination.objects.filter(product=product).count() == 2

    def test_refresh_combination_from_icg_reuses_product_persistence(self):
        man = _make_manufacturer()
        product = _make_product(man)
        combination = Combination.objects.create(
            product=product,
            icg_size="M",
            icg_color="RED",
            ean13="old-ean",
        )

        updated_row = _FakeRow(
            1001,
            "REF001",
            "M",
            "RED",
            "9999999999999",
            "",
            "Product One",
            1,
            21,
            93,
            "TALENS",
            datetime(2026, 1, 15, 10, 0, 0),
            "T",
            14000,
            "ARTECREATION",
            "F",
        )

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_combination_rows.return_value = [updated_row]
            result = refresh_combination_from_icg(combination.pk)

        assert result["status"] == "updated"
        combination.refresh_from_db()
        assert combination.ean13 == "9999999999999"

    def test_refresh_price_from_icg_updates_price(self):
        man = _make_manufacturer()
        product = _make_product(man)
        combination = Combination.objects.create(product=product, icg_size="M", icg_color="RED")
        price = Price.objects.create(combination=combination, amount_ex_vat=99.99, vat_rate=21)

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_price_rows.return_value = PRICE_ROWS[:1]
            result = refresh_price_from_icg(price.pk)

        assert result["status"] == "updated"
        price.refresh_from_db()
        assert price.amount_ex_vat == 90.00

    def test_refresh_stock_from_icg_updates_stock(self):
        man = _make_manufacturer()
        product = _make_product(man)
        combination = Combination.objects.create(product=product, icg_size="M", icg_color="RED")
        stock = Stock.objects.create(combination=combination, warehouse_code="01", quantity=5)

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_stock_rows.return_value = STOCK_ROWS[:1]
            result = refresh_stock_from_icg(stock.pk)

        assert result["status"] == "updated"
        stock.refresh_from_db()
        assert stock.quantity == 20

    def test_refresh_returns_skipped_when_icg_row_not_found(self):
        man = _make_manufacturer()
        product = _make_product(man)

        with patch("apps.icg.importer.ICGCatalogReader") as mock_reader_factory:
            instance = mock_reader_factory.return_value
            instance.fetch_product_rows.return_value = []
            result = refresh_product_from_icg(product.pk)

        assert result == {"status": "skipped", "reason": "not_found", "processed": 0, "skipped": 0}


@pytest.mark.django_db
class TestEscape:
    def test_removes_braces_and_quotes(self):
        assert _escape("{foo}") == "foo"
        assert _escape("bar'baz'") == "barbaz"
        assert _escape("{hello'}") == "hello"

    def test_strips_whitespace(self):
        assert _escape("  14X21  ") == "14X21"
        assert _escape("ESPIRAL ") == "ESPIRAL"
        assert _escape("\tMEDIDA\n") == "MEDIDA"
        assert _escape("  ") == ""

    def test_strips_after_removing_braces(self):
        assert _escape("  {foo}  ") == "foo"
        assert _escape("'{bar}' ") == "bar"

    def test_clean_value_unchanged(self):
        assert _escape("14X21") == "14X21"
        assert _escape("ESPIRAL") == "ESPIRAL"
        assert _escape("") == ""


@pytest.mark.django_db
class TestCursorTimezoneNormalization:
    def test_normalize_cursor_converts_aware_utc_to_naive(self):
        from apps.icg.services import ICGCatalogReader

        reader = ICGCatalogReader()

        aware_utc = datetime(2026, 7, 7, 18, 1, 44, 570000, tzinfo=UTC)
        result = reader._normalize_cursor_for_mssql(aware_utc)

        assert result is not None
        assert result.tzinfo is None, "cursor should be naive for MSSQL"
        assert result == datetime(2026, 7, 7, 18, 1, 44, 570000)

    def test_normalize_cursor_converts_aware_madrid_to_naive_utc(self):
        from zoneinfo import ZoneInfo

        from apps.icg.services import ICGCatalogReader

        reader = ICGCatalogReader()

        madrid_tz = ZoneInfo("Europe/Madrid")
        aware_madrid = datetime(2026, 7, 7, 20, 1, 44, 570000, tzinfo=madrid_tz)
        result = reader._normalize_cursor_for_mssql(aware_madrid)

        assert result is not None
        assert result.tzinfo is None
        assert result == datetime(2026, 7, 7, 18, 1, 44, 570000)

    def test_normalize_cursor_preserves_naive(self):
        from apps.icg.services import ICGCatalogReader

        reader = ICGCatalogReader()

        naive = datetime(2026, 7, 7, 18, 1, 44, 570000)
        result = reader._normalize_cursor_for_mssql(naive)

        assert result is not None
        assert result.tzinfo is None
        assert result == naive

    def test_normalize_cursor_returns_none_for_none(self):
        from apps.icg.services import ICGCatalogReader

        reader = ICGCatalogReader()

        result = reader._normalize_cursor_for_mssql(None)
        assert result is None
