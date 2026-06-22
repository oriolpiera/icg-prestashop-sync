import logging
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from django.conf import settings

logger = logging.getLogger(__name__)


class ICGSettings:
    ICG_ODBC_CONNECTION_STRING: str
    ICG_MSSQL_SERVER: str
    ICG_MSSQL_SERVERNAME: str
    ICG_MSSQL_DATABASE: str
    ICG_MSSQL_USER: str
    ICG_MSSQL_PASSWORD: str
    ICG_MSSQL_DRIVER: str
    ICG_MSSQL_LOGIN_TIMEOUT: int
    ICG_MSSQL_QUERY_TIMEOUT: int
    ICG_MSSQL_TRUST_SERVER_CERTIFICATE: bool


@dataclass(slots=True)
class ICGConnectionSettings:
    odbc_connection_string: str
    server: str
    servername: str
    database: str
    user: str
    password: str
    driver: str
    login_timeout: int
    query_timeout: int
    trust_server_certificate: bool


class ICGCatalogReader:
    def connection_settings(self) -> ICGConnectionSettings:
        typed_settings = cast(ICGSettings, settings)
        return ICGConnectionSettings(
            odbc_connection_string=typed_settings.ICG_ODBC_CONNECTION_STRING,
            server=typed_settings.ICG_MSSQL_SERVER,
            servername=typed_settings.ICG_MSSQL_SERVERNAME,
            database=typed_settings.ICG_MSSQL_DATABASE,
            user=typed_settings.ICG_MSSQL_USER,
            password=typed_settings.ICG_MSSQL_PASSWORD,
            driver=typed_settings.ICG_MSSQL_DRIVER,
            login_timeout=typed_settings.ICG_MSSQL_LOGIN_TIMEOUT,
            query_timeout=typed_settings.ICG_MSSQL_QUERY_TIMEOUT,
            trust_server_certificate=typed_settings.ICG_MSSQL_TRUST_SERVER_CERTIFICATE,
        )

    def build_connection_string(self) -> str:
        cs = self.connection_settings()
        if cs.odbc_connection_string:
            return cs.odbc_connection_string

        driver_name = cs.driver.lower()
        server_part = f"SERVERNAME={cs.servername};" if cs.servername else f"SERVER={cs.server};"
        trust_part = (
            f"TrustServerCertificate={'yes' if cs.trust_server_certificate else 'no'};"
            if "freetds" not in driver_name
            else ""
        )
        encrypt_part = "Encrypt=yes;" if "freetds" not in driver_name else ""
        driver_part = f"DRIVER={cs.driver};" if "freetds" in driver_name else f"DRIVER={{{cs.driver}}};"
        return (
            f"{driver_part}"
            f"{server_part}"
            f"DATABASE={cs.database};"
            f"UID={cs.user};"
            f"PWD={cs.password};"
            f"{encrypt_part}"
            f"{trust_part}"
            f"Login Timeout={cs.login_timeout};"
        )

    def _connect(self):
        import pyodbc

        cs = self.connection_settings()
        conn_str = self.build_connection_string()
        target = cs.servername or cs.server
        logger.info("Connecting to ICG MSSQL on %s/%s", target, cs.database)
        try:
            return pyodbc.connect(conn_str)
        except pyodbc.Error:
            logger.exception("Failed to connect to ICG MSSQL on %s/%s", target, cs.database)
            raise

    def _set_query_timeout(self, db_cursor) -> None:
        try:
            db_cursor.timeout = self.connection_settings().query_timeout
        except AttributeError:
            logger.debug("ODBC cursor does not support timeout attribute; continuing without it")

    def fetch_products_after(
        self, cursor_at: datetime | None = None, last_source_key: str = "", limit: int = 0
    ) -> tuple[list, bool]:
        with self._connect() as conn:
            db_cursor = conn.cursor()
            self._set_query_timeout(db_cursor)
            if cursor_at is not None and last_source_key:
                db_cursor.execute(
                    "SELECT * FROM view_imp_articles "
                    "WHERE (Fecha_Modificado > ?) "
                    "OR (Fecha_Modificado = ? AND CAST(CODARTICULO AS INT) > CAST(? AS INT)) "
                    "ORDER BY Fecha_Modificado ASC, CAST(CODARTICULO AS INT) ASC",
                    (cursor_at, cursor_at, last_source_key),
                )
            elif cursor_at is not None:
                db_cursor.execute(
                    "SELECT * FROM view_imp_articles WHERE Fecha_Modificado >= ? "
                    "ORDER BY Fecha_Modificado ASC, CAST(CODARTICULO AS INT) ASC",
                    cursor_at,
                )
            else:
                db_cursor.execute(
                    "SELECT * FROM view_imp_articles "
                    "ORDER BY Fecha_Modificado ASC, CAST(CODARTICULO AS INT) ASC"
                )
            rows = db_cursor.fetchmany(limit) if limit else db_cursor.fetchall()
            has_more = len(rows) == limit if limit else False
            return rows, has_more

    def fetch_prices_after(
        self, cursor_at: datetime | None = None, last_source_key: str = "", limit: int = 0
    ) -> tuple[list, bool]:
        with self._connect() as conn:
            db_cursor = conn.cursor()
            self._set_query_timeout(db_cursor)
            if cursor_at is not None and last_source_key:
                db_cursor.execute(
                    "SELECT * FROM view_imp_preus "
                    "WHERE (Fecha_modificado > ?) "
                    "OR (Fecha_modificado = ? AND CAST(Codarticulo AS INT) > CAST(? AS INT)) "
                    "ORDER BY Fecha_modificado ASC, CAST(Codarticulo AS INT) ASC",
                    (cursor_at, cursor_at, last_source_key),
                )
            elif cursor_at is not None:
                db_cursor.execute(
                    "SELECT * FROM view_imp_preus WHERE Fecha_modificado >= ? "
                    "ORDER BY Fecha_modificado ASC, CAST(Codarticulo AS INT) ASC",
                    cursor_at,
                )
            else:
                db_cursor.execute(
                    "SELECT * FROM view_imp_preus "
                    "ORDER BY Fecha_modificado ASC, CAST(Codarticulo AS INT) ASC"
                )
            rows = db_cursor.fetchmany(limit) if limit else db_cursor.fetchall()
            has_more = len(rows) == limit if limit else False
            return rows, has_more

    def fetch_stock_after(
        self, cursor_at: datetime | None = None, last_source_key: str = "", limit: int = 0
    ) -> tuple[list, bool]:
        with self._connect() as conn:
            db_cursor = conn.cursor()
            self._set_query_timeout(db_cursor)
            if cursor_at is not None and last_source_key:
                db_cursor.execute(
                    "SELECT * FROM view_imp_stocks "
                    "WHERE (Fecha_Modificado > ?) "
                    "OR (Fecha_Modificado = ? AND CAST(Codarticulo AS INT) > CAST(? AS INT)) "
                    "ORDER BY Fecha_Modificado ASC, CAST(Codarticulo AS INT) ASC",
                    (cursor_at, cursor_at, last_source_key),
                )
            elif cursor_at is not None:
                db_cursor.execute(
                    "SELECT * FROM view_imp_stocks WHERE Fecha_Modificado >= ? "
                    "ORDER BY Fecha_Modificado ASC, CAST(Codarticulo AS INT) ASC",
                    cursor_at,
                )
            else:
                db_cursor.execute(
                    "SELECT * FROM view_imp_stocks "
                    "ORDER BY Fecha_Modificado ASC, CAST(Codarticulo AS INT) ASC"
                )
            rows = db_cursor.fetchmany(limit) if limit else db_cursor.fetchall()
            has_more = len(rows) == limit if limit else False
            return rows, has_more
