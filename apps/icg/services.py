import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast

from django.conf import settings
from django.utils import timezone

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


@dataclass(slots=True)
class ClientesWebRow:
    cod_cliente_web: int
    nombre_cliente: str | None
    nombre_comercial: str | None
    cif: str | None
    direccion: str | None
    cp: str | None
    poblacion: str | None
    provincia: str | None
    pais: str | None
    telefono1: str | None
    telefono2: str | None
    fax: str | None
    email: str | None
    estado: int
    fecha_exportacion: datetime
    fecha_insercion: datetime | None


@dataclass(slots=True)
class FacturasWebRow:
    tipo_documento: int
    num_documento: int
    num_lin: int
    cod_cliente: int | None
    cod_cliente_web: int | None
    cod_articulo: int
    talla: str
    color: str
    descripcion: str | None
    unidades_total: int
    precio_iva: Decimal
    dto: Decimal | None
    total: Decimal
    fecha_documento: datetime
    estado: int
    forma_de_pago: int
    total_iva: Decimal
    tipo_iva: int
    fecha_exportacion: datetime
    fecha_insercion: datetime | None
    num_documento_mng: int | None
    total_lin: int
    cod_barras: str | None


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
        driver_part = (
            f"DRIVER={cs.driver};" if "freetds" in driver_name else f"DRIVER={{{cs.driver}}};"
        )  # noqa: E501
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
        logger.info("ICG connect start target=%s/%s", target, cs.database)
        t0 = time.monotonic()
        try:
            conn = pyodbc.connect(conn_str)
            logger.info(
                "ICG connect OK target=%s/%s elapsed=%.3fs",
                target,
                cs.database,
                time.monotonic() - t0,
            )
            return conn
        except pyodbc.Error:
            logger.exception(
                "ICG connect FAILED target=%s/%s elapsed=%.3fs",
                target,
                cs.database,
                time.monotonic() - t0,
            )
            raise

    @contextmanager
    def _connection(self):
        """Context manager that guarantees explicit close after use.

        pyodbc's ``Connection.__exit__`` commits but does **not** close the
        connection.  This wrapper ensures ``close()`` is called on every exit
        path so the TCP session is released immediately.
        """
        conn = self._connect()
        try:
            yield conn
        finally:
            logger.info("Closing ICG MSSQL connection")
            conn.close()

    def _set_query_timeout(self, db_cursor) -> None:
        try:
            db_cursor.timeout = self.connection_settings().query_timeout
        except AttributeError:
            logger.debug("ODBC cursor does not support timeout attribute; continuing without it")

    def _normalize_cursor_for_mssql(self, cursor_at: datetime | None) -> datetime | None:
        if cursor_at is None:
            return None
        if timezone.is_aware(cursor_at):
            return cursor_at.astimezone(timezone.get_current_timezone()).replace(tzinfo=None)
        return cursor_at

    def _fetch_rows(self, query: str, params: tuple = ()) -> list:
        with self._connection() as conn:
            db_cursor = conn.cursor()
            self._set_query_timeout(db_cursor)
            t0 = time.monotonic()
            db_cursor.execute(query, params)
            elapsed_exec = time.monotonic() - t0
            t1 = time.monotonic()
            rows = db_cursor.fetchall()
            elapsed_fetch = time.monotonic() - t1
            logger.info(
                "ICG query exec=%.3fs fetch=%.3fs rows=%d",
                elapsed_exec,
                elapsed_fetch,
                len(rows),
            )
            return rows

    def fetch_products_after(
        self, cursor_at: datetime | None = None, last_source_key: str = "", limit: int = 0
    ) -> tuple[list, bool]:
        cursor_at = self._normalize_cursor_for_mssql(cursor_at)
        with self._connection() as conn:
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
                    "SELECT * FROM view_imp_articles WHERE Fecha_Modificado > ? "
                    "ORDER BY Fecha_Modificado ASC, CAST(CODARTICULO AS INT) ASC",
                    cursor_at,
                )
            else:
                db_cursor.execute(
                    "SELECT * FROM view_imp_articles "
                    "ORDER BY Fecha_Modificado ASC, CAST(CODARTICULO AS INT) ASC"
                )
            t0 = time.monotonic()
            rows = db_cursor.fetchmany(limit) if limit else db_cursor.fetchall()
            elapsed = time.monotonic() - t0
            has_more = len(rows) == limit if limit else False
            logger.info(
                "ICG fetch products elapsed=%.3fs rows=%d has_more=%s",
                elapsed,
                len(rows),
                has_more,
            )
            return rows, has_more

    def fetch_product_rows(self, icg_id: int) -> list:
        return self._fetch_rows(
            "SELECT * FROM view_imp_articles "
            "WHERE CAST(CODARTICULO AS INT) = CAST(? AS INT) "
            "ORDER BY Fecha_Modificado ASC, CAST(CODARTICULO AS INT) ASC",
            (icg_id,),
        )

    def fetch_combination_rows(self, icg_id: int, icg_size: str, icg_color: str) -> list:
        return self._fetch_rows(
            "SELECT * FROM view_imp_articles "
            "WHERE CAST(CODARTICULO AS INT) = CAST(? AS INT) "
            "AND RTRIM(LTRIM(Talla)) = ? "
            "AND RTRIM(LTRIM(Color)) = ? "
            "ORDER BY Fecha_Modificado ASC, CAST(CODARTICULO AS INT) ASC",
            (icg_id, icg_size, icg_color),
        )

    def fetch_product_rows_by_reference(self, reference: str) -> list:
        with self._connection() as conn:
            db_cursor = conn.cursor()
            self._set_query_timeout(db_cursor)
            t0 = time.monotonic()
            db_cursor.execute(
                "SELECT * FROM view_imp_articles WHERE Referencia = ? "
                "ORDER BY Fecha_Modificado ASC, CAST(CODARTICULO AS INT) ASC",
                reference,
            )
            elapsed_exec = time.monotonic() - t0
            t1 = time.monotonic()
            rows = db_cursor.fetchall()
            elapsed_fetch = time.monotonic() - t1
            logger.info(
                "ICG fetch by ref exec=%.3fs fetch=%.3fs rows=%d",
                elapsed_exec,
                elapsed_fetch,
                len(rows),
            )
            return rows

    def fetch_prices_after(
        self, cursor_at: datetime | None = None, last_source_key: str = "", limit: int = 0
    ) -> tuple[list, bool]:
        cursor_at = self._normalize_cursor_for_mssql(cursor_at)
        with self._connection() as conn:
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
                    "SELECT * FROM view_imp_preus WHERE Fecha_modificado > ? "
                    "ORDER BY Fecha_modificado ASC, CAST(Codarticulo AS INT) ASC",
                    cursor_at,
                )
            else:
                db_cursor.execute(
                    "SELECT * FROM view_imp_preus "
                    "ORDER BY Fecha_modificado ASC, CAST(Codarticulo AS INT) ASC"
                )
            t0 = time.monotonic()
            rows = db_cursor.fetchmany(limit) if limit else db_cursor.fetchall()
            elapsed = time.monotonic() - t0
            has_more = len(rows) == limit if limit else False
            logger.info(
                "ICG fetch prices elapsed=%.3fs rows=%d has_more=%s",
                elapsed,
                len(rows),
                has_more,
            )
            return rows, has_more

    def fetch_price_rows(self, icg_id: int, icg_size: str, icg_color: str) -> list:
        return self._fetch_rows(
            "SELECT * FROM view_imp_preus "
            "WHERE CAST(Codarticulo AS INT) = CAST(? AS INT) "
            "AND RTRIM(LTRIM(Talla)) = ? "
            "AND RTRIM(LTRIM(Color)) = ? "
            "ORDER BY Fecha_modificado ASC, CAST(Codarticulo AS INT) ASC",
            (icg_id, icg_size, icg_color),
        )

    def fetch_price_rows_for_combination(self, icg_id: int, talla: str, color: str) -> list:
        with self._connection() as conn:
            db_cursor = conn.cursor()
            self._set_query_timeout(db_cursor)
            t0 = time.monotonic()
            db_cursor.execute(
                "SELECT * FROM view_imp_preus WHERE Codarticulo = ? AND Talla = ? AND Color = ?",
                icg_id,
                talla,
                color,
            )
            elapsed_exec = time.monotonic() - t0
            t1 = time.monotonic()
            rows = db_cursor.fetchall()
            elapsed_fetch = time.monotonic() - t1
            logger.info(
                "ICG fetch price combo exec=%.3fs fetch=%.3fs rows=%d",
                elapsed_exec,
                elapsed_fetch,
                len(rows),
            )
            return rows

    def fetch_stock_after(
        self, cursor_at: datetime | None = None, last_source_key: str = "", limit: int = 0
    ) -> tuple[list, bool]:
        cursor_at = self._normalize_cursor_for_mssql(cursor_at)
        with self._connection() as conn:
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
                    "SELECT * FROM view_imp_stocks WHERE Fecha_Modificado > ? "
                    "ORDER BY Fecha_Modificado ASC, CAST(Codarticulo AS INT) ASC",
                    cursor_at,
                )
            else:
                db_cursor.execute(
                    "SELECT * FROM view_imp_stocks "
                    "ORDER BY Fecha_Modificado ASC, CAST(Codarticulo AS INT) ASC"
                )
            t0 = time.monotonic()
            rows = db_cursor.fetchmany(limit) if limit else db_cursor.fetchall()
            elapsed = time.monotonic() - t0
            has_more = len(rows) == limit if limit else False
            logger.info(
                "ICG fetch stock elapsed=%.3fs rows=%d has_more=%s",
                elapsed,
                len(rows),
                has_more,
            )
            return rows, has_more

    def fetch_stock_rows(self, icg_id: int, icg_size: str, icg_color: str) -> list:
        return self._fetch_rows(
            "SELECT * FROM view_imp_stocks "
            "WHERE CAST(Codarticulo AS INT) = CAST(? AS INT) "
            "AND RTRIM(LTRIM(Talla)) = ? "
            "AND RTRIM(LTRIM(Color)) = ? "
            "ORDER BY Fecha_Modificado ASC, CAST(Codarticulo AS INT) ASC",
            (icg_id, icg_size, icg_color),
        )

    def fetch_stock_rows_for_combination(
        self,
        icg_id: int,
        talla: str,
        color: str,
        *,
        warehouse_code: str = "01",
    ) -> list:
        with self._connection() as conn:
            db_cursor = conn.cursor()
            self._set_query_timeout(db_cursor)
            t0 = time.monotonic()
            db_cursor.execute(
                "SELECT * FROM view_imp_stocks "
                "WHERE Codalmacen = ? AND Codarticulo = ? AND Talla = ? AND Color = ?",
                warehouse_code,
                icg_id,
                talla,
                color,
            )
            elapsed_exec = time.monotonic() - t0
            t1 = time.monotonic()
            rows = db_cursor.fetchall()
            elapsed_fetch = time.monotonic() - t1
            logger.info(
                "ICG fetch stock combo exec=%.3fs fetch=%.3fs rows=%d",
                elapsed_exec,
                elapsed_fetch,
                len(rows),
            )
            return rows


class ICGClientesWebWriter:
    def __init__(self, reader: ICGCatalogReader | None = None) -> None:
        self.reader = reader or ICGCatalogReader()

    def customer_exists(self, cod_cliente_web: int) -> bool:
        with self.reader._connection() as conn:
            cursor = conn.cursor()
            self.reader._set_query_timeout(cursor)
            cursor.execute(
                "SELECT TOP 1 1 FROM ClientesWeb WHERE CodClienteWeb = ?",
                (cod_cliente_web,),
            )
            return cursor.fetchone() is not None

    def insert_customer(self, row: ClientesWebRow) -> bool:
        with self.reader._connection() as conn:
            cursor = conn.cursor()
            self.reader._set_query_timeout(cursor)
            t0 = time.monotonic()
            cursor.execute(
                "SELECT TOP 1 1 FROM ClientesWeb WHERE CodClienteWeb = ?",
                (row.cod_cliente_web,),
            )
            if cursor.fetchone() is not None:
                logger.debug(
                    "ICG customer already exists cod_cliente_web=%d elapsed=%.3fs",
                    row.cod_cliente_web,
                    time.monotonic() - t0,
                )
                return False

            t1 = time.monotonic()
            cursor.execute(
                "INSERT INTO ClientesWeb ("
                "CodClienteWeb, NombreCliente, NombreComercial, CIF, Direccion, CP, "
                "Poblacion, Provincia, Pais, Telefono1, Telefono2, FAX, Email, Estado, "
                "FechaExportacion, FechaInsercion"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.cod_cliente_web,
                    row.nombre_cliente,
                    row.nombre_comercial,
                    row.cif,
                    row.direccion,
                    row.cp,
                    row.poblacion,
                    row.provincia,
                    row.pais,
                    row.telefono1,
                    row.telefono2,
                    row.fax,
                    row.email,
                    row.estado,
                    self._as_sql_datetime(row.fecha_exportacion),
                    self._as_sql_datetime(row.fecha_insercion),
                ),
            )
            conn.commit()
            elapsed = time.monotonic() - t0
            logger.info(
                "ICG insert customer OK cod_cliente_web=%d "
                "elapsed=%.3fs (check=%.3fs insert=%.3fs)",
                row.cod_cliente_web,
                elapsed,
                t1 - t0,
                time.monotonic() - t1,
            )
            return True

    def _as_sql_datetime(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if timezone.is_aware(value):
            return timezone.make_naive(value, timezone.get_current_timezone())
        return value


class ICGFacturasWebWriter:
    def __init__(self, reader: ICGCatalogReader | None = None) -> None:
        self.reader = reader or ICGCatalogReader()

    def insert_order_rows(self, rows: list[FacturasWebRow]) -> int:
        if not rows:
            return 0

        inserted = 0

        t_start = time.monotonic()
        with self.reader._connection() as conn:
            cursor = conn.cursor()
            self.reader._set_query_timeout(cursor)

            for row in rows:
                cursor.execute(
                    "SELECT TOP 1 1 FROM FacturasWeb "
                    "WHERE TipoDocumento = ? AND NumDocumento = ? AND NumLin = ?",
                    (row.tipo_documento, row.num_documento, row.num_lin),
                )
                if cursor.fetchone() is not None:
                    continue

                cursor.execute(
                    "INSERT INTO FacturasWeb ("
                    "TipoDocumento, NumDocumento, NumLin, CodCliente, CodClienteWeb, "
                    "CodArticulo, Talla, Color, Descripcion, UnidadesTotal, PrecioIVA, "
                    "Dto, Total, FechaDocumento, Estado, FormaDePago, TotalIVA, TipoIVA, "
                    "FechaExportacion, FechaInsercion, NumDocumentoMNG, TotalLin, CODBARRAS"
                    ") VALUES ("
                    "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
                    ")",
                    (
                        row.tipo_documento,
                        row.num_documento,
                        row.num_lin,
                        row.cod_cliente,
                        row.cod_cliente_web,
                        row.cod_articulo,
                        row.talla,
                        row.color,
                        row.descripcion,
                        row.unidades_total,
                        row.precio_iva,
                        row.dto,
                        row.total,
                        self._as_sql_datetime(row.fecha_documento),
                        row.estado,
                        row.forma_de_pago,
                        row.total_iva,
                        row.tipo_iva,
                        self._as_sql_datetime(row.fecha_exportacion),
                        self._as_sql_datetime(row.fecha_insercion),
                        row.num_documento_mng,
                        row.total_lin,
                        row.cod_barras,
                    ),
                )
                inserted += 1

            if inserted:
                conn.commit()
        elapsed = time.monotonic() - t_start
        logger.info(
            "ICG insert orders total=%d inserted=%d elapsed=%.3fs",
            len(rows),
            inserted,
            elapsed,
        )
        return inserted

    def _as_sql_datetime(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if timezone.is_aware(value):
            return timezone.make_naive(value, timezone.get_current_timezone())
        return value
