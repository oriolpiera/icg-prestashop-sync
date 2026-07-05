from datetime import datetime
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from apps.catalog.models import Combination, Product
from apps.icg.services import FacturasWebRow, ICGCatalogReader, ICGFacturasWebWriter
from apps.prestashop.client import (
    PrestashopError,
    PrestashopOrderDiscountLine,
    PrestashopOrderLine,
    PrestashopOrderSnapshot,
    PrestashopOrderSummary,
)
from apps.sales.models import ExportStatus, PrestashopCustomer, PrestashopOrder
from apps.sync.cursor_service import get_or_create_cursor
from apps.sync.models import (
    SyncCursor,
    SyncCursorSource,
    SyncError,
    SyncJob,
    SyncJobStatus,
    SyncJobType,
)
from apps.sync.order_export import export_order_to_icg, map_snapshot_to_facturas_web
from apps.sync.tasks import export_new_orders_to_icg


@pytest.fixture(autouse=True)
def _clean_db(request):
    if request.node.get_closest_marker("django_db"):
        SyncError.objects.all().delete()
        SyncJob.objects.all().delete()
        SyncCursor.objects.all().delete()
        Combination.objects.all().delete()
        Product.objects.all().delete()
        PrestashopOrder.objects.all().delete()
        PrestashopCustomer.objects.all().delete()


def _aware(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0):
    return timezone.make_aware(datetime(year, month, day, hour, minute, second))


def _snapshot(*, payment: str = "Redsys Card") -> PrestashopOrderSnapshot:
    return PrestashopOrderSnapshot(
        order_id=42,
        customer_id=7,
        payment=payment,
        date_add=_aware(2026, 6, 30, 10, 0, 0),
        total_paid_tax_incl=Decimal("100.00"),
        total_shipping_tax_incl=Decimal("12.10"),
        total_shipping_tax_excl=Decimal("10.00"),
        lines=[
            PrestashopOrderLine(
                product_id=101,
                combination_id=202,
                description="Blue mug",
                quantity=2,
                unit_price_tax_incl=Decimal("24.20"),
                total_price_tax_incl=Decimal("48.40"),
                vat_rate=Decimal("21.00"),
            )
        ],
        discounts=[
            PrestashopOrderDiscountLine(
                description="Summer promo",
                amount_tax_incl=Decimal("6.05"),
                amount_tax_excl=Decimal("5.00"),
                vat_rate=Decimal("21.00"),
            ),
            PrestashopOrderDiscountLine(
                description="Loyalty",
                amount_tax_incl=Decimal("11.00"),
                amount_tax_excl=Decimal("10.00"),
                vat_rate=Decimal("10.00"),
            ),
        ],
    )


@pytest.fixture
def _catalog_mapping(db):
    product = Product.objects.create(
        icg_id=5001,
        prestashop_id=101,
        reference="MUG-001",
        name="Blue mug",
    )
    return Combination.objects.create(
        product=product,
        prestashop_id=202,
        icg_size="UNI",
        icg_color="BLUE",
        ean13="1234567890123",
    )


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commit = Mock()

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _CursorStub:
    def __init__(self, fetchone_results):
        self.fetchone_results = list(fetchone_results)
        self.execute_calls = []
        self.timeout = None

    def execute(self, sql, params=None):
        self.execute_calls.append((sql, params))

    def fetchone(self):
        if self.fetchone_results:
            return self.fetchone_results.pop(0)
        return None


@pytest.mark.django_db
def test_map_snapshot_to_facturas_web_builds_product_shipping_and_discount_lines(_catalog_mapping):
    rows = map_snapshot_to_facturas_web(_snapshot(), exported_at=_aware(2026, 6, 30, 12, 0, 0))

    assert len(rows) == 4
    assert all(row.total == Decimal("100.00") for row in rows)
    assert all(row.total_lin == 4 for row in rows)

    product_row = rows[0]
    assert product_row.cod_cliente_web == 57
    assert product_row.cod_articulo == 5001
    assert product_row.talla == "UNI"
    assert product_row.color == "BLUE"
    assert product_row.cod_barras == "1234567890123"
    assert product_row.forma_de_pago == 2
    assert product_row.tipo_iva == 1

    shipping_row = rows[1]
    assert shipping_row.cod_cliente_web == 57
    assert shipping_row.cod_articulo == 14873
    assert shipping_row.talla == "."
    assert shipping_row.color == "."
    assert shipping_row.unidades_total == 1
    assert shipping_row.total_iva == Decimal("12.10")
    assert shipping_row.tipo_iva == 1

    discount_row = rows[2]
    assert discount_row.cod_cliente_web == 57
    assert discount_row.cod_articulo == 14089
    assert discount_row.unidades_total == -1
    assert discount_row.precio_iva == Decimal("6.05")
    assert discount_row.total_iva == Decimal("-6.05")
    assert discount_row.tipo_iva == 1

    second_discount_row = rows[3]
    assert second_discount_row.tipo_iva == 2


@pytest.mark.django_db
def test_map_snapshot_to_facturas_web_rejects_unsupported_payment(_catalog_mapping):
    with pytest.raises(PrestashopError, match="Unsupported Prestashop payment method"):
        map_snapshot_to_facturas_web(
            _snapshot(payment="Cash on delivery"),
            exported_at=_aware(2026, 6, 30, 12, 0, 0),
        )


def test_writer_insert_order_rows_inserts_when_missing():
    cursor = _CursorStub([None, None])
    connection = _FakeConnection(cursor)
    reader = ICGCatalogReader()
    writer = ICGFacturasWebWriter(reader=reader)
    rows = [
        FacturasWebRow(
            tipo_documento=13,
            num_documento=42,
            num_lin=1,
            cod_cliente=None,
            cod_cliente_web=7,
            cod_articulo=5001,
            talla="UNI",
            color="BLUE",
            descripcion="Blue mug",
            unidades_total=2,
            precio_iva=Decimal("24.20"),
            dto=None,
            total=Decimal("100.00"),
            fecha_documento=_aware(2026, 6, 30, 10, 0, 0),
            estado=1,
            forma_de_pago=2,
            total_iva=Decimal("48.40"),
            tipo_iva=1,
            fecha_exportacion=_aware(2026, 6, 30, 12, 0, 0),
            fecha_insercion=None,
            num_documento_mng=None,
            total_lin=2,
            cod_barras="1234567890123",
        )
    ]

    with patch.object(reader, "_connect", return_value=connection):
        inserted = writer.insert_order_rows(rows)

    assert inserted == 1
    assert len(cursor.execute_calls) == 2
    assert "INSERT INTO FacturasWeb" in cursor.execute_calls[1][0]
    connection.commit.assert_called_once()


def test_writer_insert_order_rows_skips_duplicates():
    cursor = _CursorStub([object()])
    connection = _FakeConnection(cursor)
    reader = ICGCatalogReader()
    writer = ICGFacturasWebWriter(reader=reader)
    rows = [
        FacturasWebRow(
            tipo_documento=13,
            num_documento=42,
            num_lin=1,
            cod_cliente=None,
            cod_cliente_web=7,
            cod_articulo=5001,
            talla="UNI",
            color="BLUE",
            descripcion="Blue mug",
            unidades_total=2,
            precio_iva=Decimal("24.20"),
            dto=None,
            total=Decimal("100.00"),
            fecha_documento=_aware(2026, 6, 30, 10, 0, 0),
            estado=1,
            forma_de_pago=2,
            total_iva=Decimal("48.40"),
            tipo_iva=1,
            fecha_exportacion=_aware(2026, 6, 30, 12, 0, 0),
            fecha_insercion=None,
            num_documento_mng=None,
            total_lin=1,
            cod_barras="1234567890123",
        )
    ]

    with patch.object(reader, "_connect", return_value=connection):
        inserted = writer.insert_order_rows(rows)

    assert inserted == 0
    assert len(cursor.execute_calls) == 1
    connection.commit.assert_not_called()


@pytest.mark.django_db
class TestOrderExportTask:
    def test_export_order_to_icg_returns_inserted_rows(self, _catalog_mapping):
        client = Mock()
        client.get_order_snapshot.return_value = _snapshot()
        writer = Mock()
        writer.insert_order_rows.return_value = 4

        result = export_order_to_icg(42, client=client, writer=writer)

        assert result == {"order_id": 42, "inserted_rows": 4}
        writer.insert_order_rows.assert_called_once()

    def test_task_exports_orders_and_advances_cursor(self):
        orders = [
            PrestashopOrderSummary(1, 7, "Redsys Card", _aware(2026, 6, 30, 9)),
            PrestashopOrderSummary(2, 8, "Bank transfer", _aware(2026, 6, 30, 10)),
        ]

        with (
            patch("apps.sync.tasks.PrestashopClient") as client_factory,
            patch("apps.sync.tasks.ICGFacturasWebWriter") as writer_factory,
            patch("apps.sync.tasks.refresh_order_from_prestashop") as refresh_mock,
            patch("apps.sync.tasks.export_order_to_icg_from_mirror") as export_mock,
        ):
            client_factory.return_value.list_orders_created_after.return_value = orders
            writer_factory.return_value = Mock()
            refresh_mock.return_value = Mock()
            export_mock.side_effect = [
                {"order_id": 1, "inserted_rows": 3},
                {"order_id": 2, "inserted_rows": 4},
            ]

            result = export_new_orders_to_icg(limit=100)

        assert result == {"status": "success", "processed": 2, "inserted_rows": 7, "failed": 0}
        cursor = get_or_create_cursor(SyncCursorSource.ORDERS)
        assert cursor.last_source_key == "2"
        assert cursor.last_modified_at == _aware(2026, 6, 30, 10)
        jobs = list(SyncJob.objects.order_by("entity_key"))
        assert [job.job_type for job in jobs] == [
            SyncJobType.EXPORT_ORDER,
            SyncJobType.EXPORT_ORDER,
        ]
        assert all(job.status == SyncJobStatus.SUCCEEDED for job in jobs)
        client_factory.return_value.list_orders_created_after.assert_called_once_with(
            None,
            0,
            limit=100,
        )

    def test_task_records_failure_and_still_advances_cursor(self):
        orders = [PrestashopOrderSummary(9, 7, "Redsys Card", _aware(2026, 6, 30, 9))]
        customer = PrestashopCustomer.objects.create(
            prestashop_id=7,
            firstname="Grace",
            lastname="Hopper",
            email="grace@example.com",
            date_add=_aware(2026, 6, 30, 8),
            last_snapshot_at=_aware(2026, 6, 30, 8),
            export_status=ExportStatus.NEVER,
        )
        PrestashopOrder.objects.create(
            prestashop_id=9,
            customer=customer,
            payment="Redsys Card",
            date_add=_aware(2026, 6, 30, 9),
            total_paid_tax_incl=Decimal("100.00"),
            total_shipping_tax_incl=Decimal("12.10"),
            total_shipping_tax_excl=Decimal("10.00"),
            last_snapshot_at=_aware(2026, 6, 30, 8),
            export_status=ExportStatus.NEVER,
        )

        with (
            patch("apps.sync.tasks.PrestashopClient") as client_factory,
            patch("apps.sync.tasks.ICGFacturasWebWriter") as writer_factory,
            patch("apps.sync.tasks.refresh_order_from_prestashop") as refresh_mock,
            patch(
                "apps.sync.tasks.export_order_to_icg_from_mirror",
                side_effect=Exception("payment mismatch"),
            ),
        ):
            client_factory.return_value.list_orders_created_after.return_value = orders
            writer_factory.return_value = Mock()
            refresh_mock.return_value = Mock()

            result = export_new_orders_to_icg(limit=100)

        assert result == {"status": "success", "processed": 0, "inserted_rows": 0, "failed": 1}
        cursor = get_or_create_cursor(SyncCursorSource.ORDERS)
        assert cursor.last_source_key == "9"
        job = SyncJob.objects.get(job_type=SyncJobType.EXPORT_ORDER)
        assert job.status == SyncJobStatus.FAILED
        assert "payment mismatch" in job.last_error

    def test_task_stops_without_advancing_cursor_when_refresh_fails_before_mirror_exists(self):
        orders = [
            PrestashopOrderSummary(9, 7, "Redsys Card", _aware(2026, 6, 30, 9)),
            PrestashopOrderSummary(10, 8, "Bank transfer", _aware(2026, 6, 30, 10)),
        ]

        with (
            patch("apps.sync.tasks.PrestashopClient") as client_factory,
            patch("apps.sync.tasks.ICGFacturasWebWriter") as writer_factory,
            patch(
                "apps.sync.tasks.refresh_order_from_prestashop",
                side_effect=Exception("prestashop timeout"),
            ),
            patch("apps.sync.tasks.export_order_to_icg_from_mirror") as export_mock,
        ):
            client_factory.return_value.list_orders_created_after.return_value = orders
            writer_factory.return_value = Mock()

            result = export_new_orders_to_icg(limit=100)

        assert result == {"status": "success", "processed": 0, "inserted_rows": 0, "failed": 1}
        cursor = get_or_create_cursor(SyncCursorSource.ORDERS)
        assert cursor.last_source_key == ""
        assert cursor.last_modified_at is None
        export_mock.assert_not_called()
        assert SyncJob.objects.count() == 1
