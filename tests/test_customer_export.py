from datetime import datetime
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from apps.icg.services import ClientesWebRow, ICGCatalogReader, ICGClientesWebWriter
from apps.prestashop.client import (
    PrestashopAddress,
    PrestashopCustomerSnapshot,
    PrestashopCustomerSummary,
)
from apps.sales.models import ExportStatus, PrestashopCustomer
from apps.sync.cursor_service import get_or_create_cursor
from apps.sync.customer_export import (
    export_customer_to_icg,
    map_prestashop_customer_id_to_icg_web_code,
    map_snapshot_to_clientes_web,
)
from apps.sync.models import (
    SyncCursor,
    SyncCursorSource,
    SyncError,
    SyncJob,
    SyncJobStatus,
    SyncJobType,
)
from apps.sync.tasks import export_new_customers_to_icg


@pytest.fixture(autouse=True)
def _clean_db(request):
    if request.node.get_closest_marker("django_db"):
        SyncError.objects.all().delete()
        SyncJob.objects.all().delete()
        SyncCursor.objects.all().delete()
        PrestashopCustomer.objects.all().delete()


def _aware(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0):
    return timezone.make_aware(datetime(year, month, day, hour, minute, second))


def _snapshot(*, address: PrestashopAddress | None = None) -> PrestashopCustomerSnapshot:
    return PrestashopCustomerSnapshot(
        customer_id=42,
        firstname="Oriol",
        lastname="Piera",
        email="oriol@example.com",
        date_add=_aware(2026, 6, 30, 10, 0, 0),
        address=address,
    )


def test_map_prestashop_customer_id_to_icg_web_code_prefixes_with_five():
    assert map_prestashop_customer_id_to_icg_web_code(25392) == 525392


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commit = Mock()
        self.close = Mock()

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


def test_map_snapshot_to_clientes_web_uses_nulls_without_address():
    row = map_snapshot_to_clientes_web(_snapshot(), exported_at=_aware(2026, 6, 30, 12, 0, 0))

    assert row.cod_cliente_web == 542
    assert row.nombre_cliente == "Oriol Piera"
    assert row.nombre_comercial == ""
    assert row.cif is None
    assert row.direccion is None
    assert row.telefono1 is None
    assert row.estado == 1
    assert row.fecha_exportacion == _aware(2026, 6, 30, 12, 0, 0)
    assert row.fecha_insercion is None


def test_map_snapshot_to_clientes_web_uses_address_fields():
    row = map_snapshot_to_clientes_web(
        _snapshot(
            address=PrestashopAddress(
                address1="Carrer Major 1",
                postcode="08001",
                city="Barcelona",
                state="Barcelona",
                country="Spain",
                phone="931000000",
                phone_mobile="600000000",
                dni="12345678A",
                vat_number=None,
            )
        ),
        exported_at=_aware(2026, 6, 30, 12, 0, 0),
    )

    assert row.cif == "12345678A"
    assert row.direccion == "Carrer Major 1"
    assert row.cp == "08001"
    assert row.poblacion == "Barcelona"
    assert row.provincia == "Barcelona"
    assert row.pais == "Spain"
    assert row.telefono1 == "931000000"
    assert row.telefono2 == "600000000"


def test_map_snapshot_to_clientes_web_prefers_mobile_as_primary_when_phone_missing():
    row = map_snapshot_to_clientes_web(
        _snapshot(
            address=PrestashopAddress(
                address1="Carrer Major 1",
                postcode="08001",
                city="Barcelona",
                state="Barcelona",
                country="Spain",
                phone=None,
                phone_mobile="600000000",
                dni="12345678A",
                vat_number=None,
            )
        ),
        exported_at=_aware(2026, 6, 30, 12, 0, 0),
    )

    assert row.telefono1 == "600000000"
    assert row.telefono2 is None


def test_map_snapshot_to_clientes_web_trims_values_to_schema_lengths():
    row = map_snapshot_to_clientes_web(
        PrestashopCustomerSnapshot(
            customer_id=42,
            firstname="A" * 200,
            lastname="B" * 200,
            email=("x" * 300) + "@example.com",
            date_add=_aware(2026, 6, 30, 10, 0, 0),
            address=PrestashopAddress(
                address1="D" * 300,
                postcode="123456789",
                city="P" * 120,
                state="S" * 120,
                country="C" * 120,
                phone="1" * 20,
                phone_mobile="2" * 20,
                dni="Z" * 20,
                vat_number=None,
            ),
        ),
        exported_at=_aware(2026, 6, 30, 12, 0, 0),
    )

    assert len(row.nombre_cliente) == 255
    assert len(row.cif) == 12
    assert len(row.direccion) == 255
    assert len(row.cp) == 8
    assert len(row.poblacion) == 100
    assert len(row.provincia) == 100
    assert len(row.pais) == 100
    assert len(row.telefono1) == 15
    assert len(row.telefono2) == 15
    assert len(row.email) == 255


def test_writer_insert_customer_inserts_when_missing():
    cursor = _CursorStub([None])
    connection = _FakeConnection(cursor)
    reader = ICGCatalogReader()
    writer = ICGClientesWebWriter(reader=reader)
    row = ClientesWebRow(
        cod_cliente_web=42,
        nombre_cliente="Oriol Piera",
        nombre_comercial="Oriol Piera",
        cif=None,
        direccion=None,
        cp=None,
        poblacion=None,
        provincia=None,
        pais=None,
        telefono1=None,
        telefono2=None,
        fax=None,
        email="oriol@example.com",
        estado=1,
        fecha_exportacion=_aware(2026, 6, 30, 12, 0, 0),
        fecha_insercion=_aware(2026, 6, 30, 10, 0, 0),
    )

    with patch.object(reader, "_connection", return_value=connection):
        inserted = writer.insert_customer(row)

    assert inserted is True
    assert len(cursor.execute_calls) == 2
    assert "INSERT INTO ClientesWeb" in cursor.execute_calls[1][0]
    connection.commit.assert_called_once()


def test_writer_insert_customer_skips_duplicate():
    cursor = _CursorStub([object()])
    connection = _FakeConnection(cursor)
    reader = ICGCatalogReader()
    writer = ICGClientesWebWriter(reader=reader)
    row = ClientesWebRow(
        cod_cliente_web=42,
        nombre_cliente="Oriol Piera",
        nombre_comercial="Oriol Piera",
        cif=None,
        direccion=None,
        cp=None,
        poblacion=None,
        provincia=None,
        pais=None,
        telefono1=None,
        telefono2=None,
        fax=None,
        email="oriol@example.com",
        estado=1,
        fecha_exportacion=_aware(2026, 6, 30, 12, 0, 0),
        fecha_insercion=_aware(2026, 6, 30, 10, 0, 0),
    )

    with patch.object(reader, "_connection", return_value=connection):
        inserted = writer.insert_customer(row)

    assert inserted is False
    assert len(cursor.execute_calls) == 1
    connection.commit.assert_not_called()


@pytest.mark.django_db
class TestCustomerExportTask:
    def test_export_customer_to_icg_returns_insert_status(self):
        client = Mock()
        client.get_customer_snapshot.return_value = _snapshot()
        writer = Mock()
        writer.insert_customer.return_value = True

        result = export_customer_to_icg(42, client=client, writer=writer)

        assert result == {"customer_id": 42, "inserted": True}
        writer.insert_customer.assert_called_once()

    def test_task_exports_customers_and_advances_cursor(self):
        customers = [
            PrestashopCustomerSummary(
                1, "Ada", "Lovelace", "ada@example.com", _aware(2026, 6, 30, 9)
            ),
            PrestashopCustomerSummary(
                2, "Alan", "Turing", "alan@example.com", _aware(2026, 6, 30, 10)
            ),
        ]

        with (
            patch("apps.sync.tasks.PrestashopClient") as client_factory,
            patch("apps.sync.tasks.ICGClientesWebWriter") as writer_factory,
            patch("apps.sync.tasks.refresh_customer_from_prestashop") as refresh_mock,
            patch("apps.sync.tasks.export_customer_to_icg_from_mirror") as export_mock,
        ):
            client_factory.return_value.list_customers_created_after.return_value = customers
            writer_factory.return_value = Mock()
            refresh_mock.return_value = Mock()
            export_mock.side_effect = [
                {"customer_id": 1, "inserted": True},
                {"customer_id": 2, "inserted": False},
            ]

            result = export_new_customers_to_icg(limit=100)

        assert result == {"status": "success", "processed": 2, "inserted": 1, "failed": 0}
        cursor = get_or_create_cursor(SyncCursorSource.CUSTOMERS)
        assert cursor.last_source_key == "2"
        assert cursor.last_modified_at == _aware(2026, 6, 30, 10)
        jobs = list(SyncJob.objects.order_by("entity_key"))
        assert [job.job_type for job in jobs] == [
            SyncJobType.EXPORT_CUSTOMER,
            SyncJobType.EXPORT_CUSTOMER,
        ]
        assert all(job.status == SyncJobStatus.SUCCEEDED for job in jobs)
        client_factory.return_value.list_customers_created_after.assert_called_once_with(
            None,
            0,
            limit=100,
        )

    def test_task_records_failure_and_still_advances_cursor(self):
        customers = [
            PrestashopCustomerSummary(
                9, "Grace", "Hopper", "grace@example.com", _aware(2026, 6, 30, 9)
            ),
        ]
        PrestashopCustomer.objects.create(
            prestashop_id=9,
            firstname="Grace",
            lastname="Hopper",
            email="grace@example.com",
            date_add=_aware(2026, 6, 30, 9),
            last_snapshot_at=_aware(2026, 6, 30, 8),
            export_status=ExportStatus.NEVER,
        )

        with (
            patch("apps.sync.tasks.PrestashopClient") as client_factory,
            patch("apps.sync.tasks.ICGClientesWebWriter") as writer_factory,
            patch("apps.sync.tasks.refresh_customer_from_prestashop") as refresh_mock,
            patch(
                "apps.sync.tasks.export_customer_to_icg_from_mirror",
                side_effect=Exception("sql down"),
            ),
        ):
            client_factory.return_value.list_customers_created_after.return_value = customers
            writer_factory.return_value = Mock()
            refresh_mock.return_value = Mock()

            result = export_new_customers_to_icg(limit=100)

        assert result == {"status": "success", "processed": 0, "inserted": 0, "failed": 1}
        cursor = get_or_create_cursor(SyncCursorSource.CUSTOMERS)
        assert cursor.last_source_key == "9"
        job = SyncJob.objects.get(job_type=SyncJobType.EXPORT_CUSTOMER)
        assert job.status == SyncJobStatus.FAILED
        assert "sql down" in job.last_error

    def test_task_stops_without_advancing_cursor_when_refresh_fails_before_mirror_exists(self):
        customers = [
            PrestashopCustomerSummary(
                9, "Grace", "Hopper", "grace@example.com", _aware(2026, 6, 30, 9)
            ),
            PrestashopCustomerSummary(
                10, "Katherine", "Johnson", "kat@example.com", _aware(2026, 6, 30, 10)
            ),
        ]

        with (
            patch("apps.sync.tasks.PrestashopClient") as client_factory,
            patch("apps.sync.tasks.ICGClientesWebWriter") as writer_factory,
            patch(
                "apps.sync.tasks.refresh_customer_from_prestashop",
                side_effect=Exception("prestashop timeout"),
            ),
            patch("apps.sync.tasks.export_customer_to_icg_from_mirror") as export_mock,
        ):
            client_factory.return_value.list_customers_created_after.return_value = customers
            writer_factory.return_value = Mock()

            result = export_new_customers_to_icg(limit=100)

        assert result == {"status": "success", "processed": 0, "inserted": 0, "failed": 1}
        cursor = get_or_create_cursor(SyncCursorSource.CUSTOMERS)
        assert cursor.last_source_key == ""
        assert cursor.last_modified_at is None
        export_mock.assert_not_called()
        assert SyncJob.objects.count() == 1
