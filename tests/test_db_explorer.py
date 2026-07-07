from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse

from apps.db_explorer import db as db_module
from apps.db_explorer.db import FilterCondition, TableData, get_table_data


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _CursorStub:
    def __init__(self, *, fetchall_results, fetchone_results):
        self.fetchall_results = list(fetchall_results)
        self.fetchone_results = list(fetchone_results)
        self.execute_calls = []
        self.timeout = None

    def execute(self, sql, params=None):
        self.execute_calls.append((sql, params))

    def fetchall(self):
        return self.fetchall_results.pop(0)

    def fetchone(self):
        return self.fetchone_results.pop(0)


def test_get_table_data_builds_structured_filters_and_sort_sql():
    cursor = _CursorStub(
        fetchall_results=[
            [
                SimpleNamespace(COLUMN_NAME="Estado"),
                SimpleNamespace(COLUMN_NAME="FechaExportacion"),
                SimpleNamespace(COLUMN_NAME="CodClienteWeb"),
            ],
            [(1, None, 5001, 2), (1, None, 5000, 3)],
        ],
        fetchone_results=[SimpleNamespace(row_count=2)],
    )

    with patch.object(db_module.reader, "_connect", return_value=_FakeConnection(cursor)):
        data = get_table_data(
            "ClientesWeb",
            page=2,
            page_size=2,
            filters=[
                FilterCondition(column="Estado", operator="eq", value="1"),
                FilterCondition(column="FechaExportacion", operator="is_null"),
            ],
            sort_column="Estado",
            sort_desc=True,
        )

    assert data == TableData(
        columns=["Estado", "FechaExportacion", "CodClienteWeb"],
        rows=[(1, None, 5001), (1, None, 5000)],
        total_rows=2,
        page=2,
        page_size=2,
        total_pages=1,
    )
    assert cursor.execute_calls[1] == (
        "SELECT COUNT(*) AS row_count FROM [dbo].[ClientesWeb] "
        "WHERE [Estado] = ? AND [FechaExportacion] IS NULL",
        ("1",),
    )
    assert cursor.execute_calls[2] == (
        "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (ORDER BY [Estado] DESC) AS _rn"
        " FROM [dbo].[ClientesWeb] WHERE [Estado] = ? AND [FechaExportacion] IS NULL)"
        " AS paged WHERE _rn BETWEEN ? AND ?",
        ("1", 3, 4),
    )


def test_get_table_data_rejects_invalid_sort_column():
    cursor = _CursorStub(
        fetchall_results=[[SimpleNamespace(COLUMN_NAME="Estado")]],
        fetchone_results=[],
    )

    with patch.object(db_module.reader, "_connect", return_value=_FakeConnection(cursor)):
        with pytest.raises(ValueError, match="Invalid sort column"):
            get_table_data("ClientesWeb", sort_column="FechaExportacion")


def test_get_table_data_uses_resolved_schema_for_unfiltered_count():
    cursor = _CursorStub(
        fetchall_results=[
            [SimpleNamespace(COLUMN_NAME="Estado")],
            [(1, 1)],
        ],
        fetchone_results=[SimpleNamespace(row_count=1)],
    )

    with patch.object(db_module.reader, "_connect", return_value=_FakeConnection(cursor)):
        get_table_data("ClientesWeb", schema="ventas")

    assert cursor.execute_calls[1] == (
        "SELECT COUNT(*) AS row_count FROM [ventas].[ClientesWeb]",
        None,
    )


TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_table_detail_uses_structured_filters_and_sort_for_facturasweb(client):
    staff_user = User.objects.create_user(username="staff", password="secret", is_staff=True)
    client.force_login(staff_user)

    with (
        patch(
            "apps.db_explorer.views.db.get_table_schema_by_name",
            return_value={"schema": "dbo", "name": "FacturasWeb"},
        ),
        patch(
            "apps.db_explorer.views.db.get_table_schema",
            return_value={"columns": [], "indexes": [], "foreign_keys": [], "row_count": 1},
        ),
        patch("apps.db_explorer.views.db.get_tables", return_value=[]),
        patch(
            "apps.db_explorer.views.db.get_table_data",
            return_value=TableData(
                columns=["Estado", "FechaDocumento"], rows=[("1", None)], total_rows=1
            ),
        ) as get_table_data_mock,
    ):
        response = client.get(
            reverse("db_explorer:table_detail", args=["FacturasWeb"]),
            {
                "estado": "1",
                "codclienteweb": "5001",
                "fecha_documento_presence": "not_set",
                "fecha_exportacion_from": "2026-07-01",
                "sort": "FechaDocumento",
                "direction": "desc",
            },
        )

    assert response.status_code == 200
    kwargs = get_table_data_mock.call_args.kwargs
    assert kwargs["sort_column"] == "FechaDocumento"
    assert kwargs["sort_desc"] is True
    assert kwargs["filter_column"] is None
    assert kwargs["filter_value"] is None
    assert kwargs["filters"] == [
        FilterCondition(column="Estado", operator="eq", value="1"),
        FilterCondition(
            column="FechaExportacion", operator="gte", value=kwargs["filters"][1].value
        ),
        FilterCondition(column="CodClienteWeb", operator="eq", value="5001"),
        FilterCondition(column="FechaDocumento", operator="is_null"),
    ]
    assert response.context["current_sort"] == "FechaDocumento"
    assert response.context["pagination_query"] == (
        "estado=1&codclienteweb=5001&fecha_documento_presence=not_set&"
        "fecha_exportacion_from=2026-07-01&sort=FechaDocumento&direction=desc"
    )


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_table_detail_keeps_generic_filter_for_other_tables(client):
    staff_user = User.objects.create_user(username="staff2", password="secret", is_staff=True)
    client.force_login(staff_user)

    with (
        patch(
            "apps.db_explorer.views.db.get_table_schema_by_name",
            return_value={"schema": "dbo", "name": "OtherTable"},
        ),
        patch(
            "apps.db_explorer.views.db.get_table_schema",
            return_value={"columns": [], "indexes": [], "foreign_keys": [], "row_count": 0},
        ),
        patch("apps.db_explorer.views.db.get_tables", return_value=[]),
        patch(
            "apps.db_explorer.views.db.get_table_data",
            return_value=TableData(columns=["Code"], rows=[], total_rows=0),
        ) as get_table_data_mock,
    ):
        response = client.get(
            reverse("db_explorer:table_detail", args=["OtherTable"]),
            {"filter_column": "Code", "filter_value": "ABC"},
        )

    assert response.status_code == 200
    kwargs = get_table_data_mock.call_args.kwargs
    assert kwargs["filter_column"] == "Code"
    assert kwargs["filter_value"] == "ABC"
    assert kwargs["filters"] == []
    assert response.context["has_structured_filters"] is False


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_table_detail_returns_404_for_invalid_structured_date(client):
    staff_user = User.objects.create_user(username="staff3", password="secret", is_staff=True)
    client.force_login(staff_user)

    with (
        patch(
            "apps.db_explorer.views.db.get_table_schema_by_name",
            return_value={"schema": "dbo", "name": "ClientesWeb"},
        ),
        patch(
            "apps.db_explorer.views.db.get_table_schema",
            return_value={"columns": [], "indexes": [], "foreign_keys": [], "row_count": 0},
        ),
    ):
        response = client.get(
            reverse("db_explorer:table_detail", args=["ClientesWeb"]),
            {"fecha_exportacion_from": "not-a-date"},
        )

    assert response.status_code == 404


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_table_detail_hides_clear_link_without_active_structured_filters(client):
    staff_user = User.objects.create_user(username="staff4", password="secret", is_staff=True)
    client.force_login(staff_user)

    with (
        patch(
            "apps.db_explorer.views.db.get_table_schema_by_name",
            return_value={"schema": "dbo", "name": "ClientesWeb"},
        ),
        patch(
            "apps.db_explorer.views.db.get_table_schema",
            return_value={"columns": [], "indexes": [], "foreign_keys": [], "row_count": 0},
        ),
        patch("apps.db_explorer.views.db.get_tables", return_value=[]),
        patch(
            "apps.db_explorer.views.db.get_table_data",
            return_value=TableData(columns=["Estado"], rows=[], total_rows=0),
        ),
    ):
        response = client.get(reverse("db_explorer:table_detail", args=["ClientesWeb"]))

    assert response.status_code == 200
    assert response.context["has_active_filters"] is False
    content = response.content.decode()
    assert "Clear" not in content
    assert "Table is empty." in content
