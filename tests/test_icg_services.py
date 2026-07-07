from unittest.mock import Mock, patch

from django.test import override_settings

from apps.icg.services import ICGCatalogReader


@override_settings(
    ICG_ODBC_CONNECTION_STRING="DRIVER=FreeTDS;SERVERNAME=legacy-sql-alias;DATABASE=legacy_database;UID=legacy_user;PWD=example-password;",
    ICG_MSSQL_SERVER="ignored-server",
    ICG_MSSQL_SERVERNAME="ignored-servername",
)
def test_mssql_reader_prefers_explicit_odbc_connection_string():
    reader = ICGCatalogReader()

    assert reader.build_connection_string() == (
        "DRIVER=FreeTDS;SERVERNAME=legacy-sql-alias;DATABASE=legacy_database;UID=legacy_user;PWD=example-password;"
    )


@override_settings(
    ICG_ODBC_CONNECTION_STRING="",
    ICG_MSSQL_DRIVER="FreeTDS",
    ICG_MSSQL_SERVER="",
    ICG_MSSQL_SERVERNAME="legacy-sql-alias",
    ICG_MSSQL_DATABASE="legacy_database",
    ICG_MSSQL_USER="legacy_user",
    ICG_MSSQL_PASSWORD="example-password",
)
def test_mssql_reader_supports_freetds_servername_connections():
    reader = ICGCatalogReader()

    assert reader.build_connection_string() == (
        "DRIVER=FreeTDS;"
        "SERVERNAME=legacy-sql-alias;"
        "DATABASE=legacy_database;"
        "UID=legacy_user;"
        "PWD=example-password;"
        "Login Timeout=10;"
    )


@override_settings(
    ICG_ODBC_CONNECTION_STRING="",
    ICG_MSSQL_DRIVER="ODBC Driver 17 for SQL Server",
    ICG_MSSQL_SERVER="db.example.internal",
    ICG_MSSQL_SERVERNAME="",
    ICG_MSSQL_DATABASE="legacy_database",
    ICG_MSSQL_USER="legacy_user",
    ICG_MSSQL_PASSWORD="example-password",
    ICG_MSSQL_TRUST_SERVER_CERTIFICATE=True,
)
def test_mssql_reader_keeps_microsoft_odbc_encryption_settings():
    reader = ICGCatalogReader()

    assert reader.build_connection_string() == (
        "DRIVER={ODBC Driver 17 for SQL Server};"
        "SERVER=db.example.internal;"
        "DATABASE=legacy_database;"
        "UID=legacy_user;"
        "PWD=example-password;"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
        "Login Timeout=10;"
    )


class _FakeConnection:
    def __init__(self):
        self.close = Mock()
        self.commit = Mock()

    def cursor(self):
        return Mock()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@override_settings(
    ICG_ODBC_CONNECTION_STRING="DRIVER=FreeTDS;SERVERNAME=x;DATABASE=x;UID=x;PWD=x;",
    ICG_MSSQL_SERVER="",
    ICG_MSSQL_SERVERNAME="x",
)
def test_connection_closes_on_success():
    reader = ICGCatalogReader()
    fake_conn = _FakeConnection()

    with patch.object(reader, "_connect", return_value=fake_conn):
        with reader._connection() as conn:
            conn.cursor().execute("SELECT 1")

    fake_conn.close.assert_called_once()


@override_settings(
    ICG_ODBC_CONNECTION_STRING="DRIVER=FreeTDS;SERVERNAME=x;DATABASE=x;UID=x;PWD=x;",
    ICG_MSSQL_SERVER="",
    ICG_MSSQL_SERVERNAME="x",
)
def test_connection_closes_on_exception():
    reader = ICGCatalogReader()
    fake_conn = _FakeConnection()

    with patch.object(reader, "_connect", return_value=fake_conn):
        try:
            with reader._connection() as conn:
                conn.cursor().execute("SELECT 1")
                raise RuntimeError("boom")
        except RuntimeError:
            pass

    fake_conn.close.assert_called_once()
