import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from django.conf import settings

logger = logging.getLogger(__name__)


class ICGSettings(Protocol):
    ICG_MSSQL_SERVER: str
    ICG_MSSQL_DATABASE: str
    ICG_MSSQL_USER: str
    ICG_MSSQL_PASSWORD: str
    ICG_MSSQL_DRIVER: str


@dataclass(slots=True)
class ICGConnectionSettings:
    server: str
    database: str
    user: str
    password: str
    driver: str


class ICGCatalogReader:
    def connection_settings(self) -> ICGConnectionSettings:
        typed_settings = cast(ICGSettings, settings)
        return ICGConnectionSettings(
            server=typed_settings.ICG_MSSQL_SERVER,
            database=typed_settings.ICG_MSSQL_DATABASE,
            user=typed_settings.ICG_MSSQL_USER,
            password=typed_settings.ICG_MSSQL_PASSWORD,
            driver=typed_settings.ICG_MSSQL_DRIVER,
        )

    def _connect(self):
        import pyodbc

        cs = self.connection_settings()
        conn_str = (
            f"DRIVER={{{cs.driver}}};"
            f"SERVER={cs.server};"
            f"DATABASE={cs.database};"
            f"UID={cs.user};"
            f"PWD={cs.password};"
        )
        logger.info("Connecting to ICG MSSQL on %s/%s", cs.server, cs.database)
        return pyodbc.connect(conn_str)

    def fetch_products_after(
        self, cursor_at: datetime | None = None, last_source_key: str = "", limit: int = 0
    ) -> tuple[list, bool]:
        with self._connect() as conn:
            db_cursor = conn.cursor()
            if cursor_at is not None and last_source_key:
                db_cursor.execute(
                    "SELECT * FROM view_imp_articles "
                    "WHERE (Fecha_Modificado > ?) "
                    "OR (Fecha_Modificado = ? AND CAST(CODARTICULO AS INT) > CAST(? AS INT)) "
                    "ORDER BY Fecha_Modificado ASC, CODARTICULO ASC",
                    (cursor_at, cursor_at, last_source_key),
                )
            elif cursor_at is not None:
                db_cursor.execute(
                    "SELECT * FROM view_imp_articles WHERE Fecha_Modificado >= ? "
                    "ORDER BY Fecha_Modificado ASC, CODARTICULO ASC",
                    cursor_at,
                )
            else:
                db_cursor.execute(
                    "SELECT * FROM view_imp_articles ORDER BY Fecha_Modificado ASC, CODARTICULO ASC"
                )
            rows = db_cursor.fetchmany(limit) if limit else db_cursor.fetchall()
            has_more = len(rows) == limit if limit else False
            return rows, has_more

    def fetch_prices_after(
        self, cursor_at: datetime | None = None, last_source_key: str = "", limit: int = 0
    ) -> tuple[list, bool]:
        with self._connect() as conn:
            db_cursor = conn.cursor()
            if cursor_at is not None and last_source_key:
                db_cursor.execute(
                    "SELECT * FROM view_imp_preus "
                    "WHERE (Fecha_modificado > ?) "
                    "OR (Fecha_modificado = ? AND CAST(Codarticulo AS INT) > CAST(? AS INT)) "
                    "ORDER BY Fecha_modificado ASC, Codarticulo ASC",
                    (cursor_at, cursor_at, last_source_key),
                )
            elif cursor_at is not None:
                db_cursor.execute(
                    "SELECT * FROM view_imp_preus WHERE Fecha_modificado >= ? "
                    "ORDER BY Fecha_modificado ASC, Codarticulo ASC",
                    cursor_at,
                )
            else:
                db_cursor.execute(
                    "SELECT * FROM view_imp_preus ORDER BY Fecha_modificado ASC, Codarticulo ASC"
                )
            rows = db_cursor.fetchmany(limit) if limit else db_cursor.fetchall()
            has_more = len(rows) == limit if limit else False
            return rows, has_more

    def fetch_stock_after(
        self, cursor_at: datetime | None = None, last_source_key: str = "", limit: int = 0
    ) -> tuple[list, bool]:
        with self._connect() as conn:
            db_cursor = conn.cursor()
            if cursor_at is not None and last_source_key:
                db_cursor.execute(
                    "SELECT * FROM view_imp_stocks "
                    "WHERE (Fecha_Modificado > ?) "
                    "OR (Fecha_Modificado = ? AND CAST(Codarticulo AS INT) > CAST(? AS INT)) "
                    "ORDER BY Fecha_Modificado ASC, Codarticulo ASC",
                    (cursor_at, cursor_at, last_source_key),
                )
            elif cursor_at is not None:
                db_cursor.execute(
                    "SELECT * FROM view_imp_stocks WHERE Fecha_Modificado >= ? "
                    "ORDER BY Fecha_Modificado ASC, Codarticulo ASC",
                    cursor_at,
                )
            else:
                db_cursor.execute(
                    "SELECT * FROM view_imp_stocks ORDER BY Fecha_Modificado ASC, Codarticulo ASC"
                )
            rows = db_cursor.fetchmany(limit) if limit else db_cursor.fetchall()
            has_more = len(rows) == limit if limit else False
            return rows, has_more
