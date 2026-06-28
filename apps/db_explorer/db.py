"""
Read-only helpers for exploring the ICG SQL Server database.

Reuses the existing ICGCatalogReader connection infrastructure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from apps.db_explorer.sql import (
    QUERY_COLUMNS,
    QUERY_FOREIGN_KEYS,
    QUERY_INDEXES_2008,
    QUERY_TABLE_FOREIGN_KEYS,
    QUERY_TABLES,
    query_table_row_count,
    validate_table_name,
)
from apps.icg.services import ICGCatalogReader

logger = logging.getLogger(__name__)

reader = ICGCatalogReader()


def _safe_ident(name: str) -> str:
    """Strip anything that is not alphanumeric or underscore.

    This is a safety net; callers should validate_table_name first.
    """
    return re.sub(r"[^A-Za-z0-9_]", "", name)


def _set_query_timeout(cursor) -> None:
    try:
        cursor.timeout = reader.connection_settings().query_timeout
    except AttributeError:
        pass


@dataclass
class TableInfo:
    schema: str
    name: str
    table_type: str
    column_count: int
    row_count: int | None = None


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    max_length: int | None
    numeric_precision: int | None
    numeric_scale: int | None
    is_nullable: bool
    default: str | None
    ordinal_position: int
    is_primary_key: bool


@dataclass
class IndexInfo:
    name: str
    is_unique: bool
    is_primary_key: bool
    type_desc: str
    columns: str


@dataclass
class ForeignKeyInfo:
    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str


@dataclass
class TableData:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    total_rows: int = 0
    page: int = 1
    page_size: int = 100
    total_pages: int = 1


def get_tables() -> list[TableInfo]:
    """Return all tables and views in the database."""
    with reader._connect() as conn:
        cursor = conn.cursor()
        _set_query_timeout(cursor)
        cursor.execute(QUERY_TABLES)
        tables = [
            TableInfo(
                schema=row.TABLE_SCHEMA,
                name=row.TABLE_NAME,
                table_type=row.TABLE_TYPE,
                column_count=row.column_count,
            )
            for row in cursor.fetchall()
        ]

    # Fetch row counts per table
    for t in tables:
        if t.table_type == "BASE TABLE":
            try:
                t.row_count = _get_row_count(t.name)
            except Exception:
                logger.warning("Could not get row count for %s", t.name)

    return tables


def _get_row_count(table_name: str) -> int:
    with reader._connect() as conn:
        cursor = conn.cursor()
        _set_query_timeout(cursor)
        cursor.execute(query_table_row_count(table_name))
        row = cursor.fetchone()
        return row.row_count if row else 0


def get_columns(table_name: str, schema: str = "dbo") -> list[ColumnInfo]:
    """Return column metadata for a table."""
    with reader._connect() as conn:
        cursor = conn.cursor()
        _set_query_timeout(cursor)
        cursor.execute(QUERY_COLUMNS, (schema, table_name, schema, table_name))
        return [
            ColumnInfo(
                name=row.COLUMN_NAME,
                data_type=row.DATA_TYPE,
                max_length=row.CHARACTER_MAXIMUM_LENGTH,
                numeric_precision=row.NUMERIC_PRECISION,
                numeric_scale=row.NUMERIC_SCALE,
                is_nullable=row.IS_NULLABLE == "YES",
                default=row.COLUMN_DEFAULT,
                ordinal_position=row.ORDINAL_POSITION,
                is_primary_key=bool(row.is_primary_key),
            )
            for row in cursor.fetchall()
        ]


def get_indexes(table_name: str) -> list[IndexInfo]:
    """Return index metadata for a table (SQL Server 2008 compatible)."""
    with reader._connect() as conn:
        cursor = conn.cursor()
        _set_query_timeout(cursor)
        cursor.execute(QUERY_INDEXES_2008, (table_name,))
        return [
            IndexInfo(
                name=row.index_name,
                is_unique=row.is_unique,
                is_primary_key=row.is_primary_key,
                type_desc=row.type_desc,
                columns=row.columns or "",
            )
            for row in cursor.fetchall()
        ]


def get_foreign_keys_for_table(table_name: str) -> list[ForeignKeyInfo]:
    """Return foreign keys where this table is the child."""
    with reader._connect() as conn:
        cursor = conn.cursor()
        _set_query_timeout(cursor)
        cursor.execute(QUERY_TABLE_FOREIGN_KEYS, (table_name,))
        return [
            ForeignKeyInfo(
                name=row.fk_name,
                from_table=table_name,
                from_column=row.from_column,
                to_table=row.to_table,
                to_column=row.to_column,
            )
            for row in cursor.fetchall()
        ]


def get_all_foreign_keys() -> list[ForeignKeyInfo]:
    """Return all foreign key relationships in the database."""
    with reader._connect() as conn:
        cursor = conn.cursor()
        _set_query_timeout(cursor)
        cursor.execute(QUERY_FOREIGN_KEYS)
        return [
            ForeignKeyInfo(
                name=row.fk_name,
                from_table=row.from_table,
                from_column=row.from_column,
                to_table=row.to_table,
                to_column=row.to_column,
            )
            for row in cursor.fetchall()
        ]


def get_table_schema(table_name: str, schema: str = "dbo") -> dict:
    """Return full schema info for a single table."""
    columns = get_columns(table_name, schema)
    indexes = get_indexes(table_name)
    fks = get_foreign_keys_for_table(table_name)
    row_count = _get_row_count(table_name)
    return {
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": fks,
        "row_count": row_count,
    }


def get_table_data(
    table_name: str,
    page: int = 1,
    page_size: int = 100,
    filter_column: str | None = None,
    filter_value: str | None = None,
) -> TableData:
    """Fetch paginated data from a table.

    When filter_column and filter_value are provided, a parameterised
    WHERE clause filters on that column using '=' comparison.
    """
    offset = (page - 1) * page_size

    with reader._connect() as conn:
        cursor = conn.cursor()
        _set_query_timeout(cursor)

        # Get columns first
        cursor.execute(QUERY_COLUMNS, ("dbo", table_name, "dbo", table_name))
        col_info = cursor.fetchall()
        columns = [c.COLUMN_NAME for c in col_info]

        # Count total rows (with optional filter)
        if filter_column and filter_value is not None:
            # Validate filter_column against known columns
            if filter_column not in columns:
                raise ValueError(f"Invalid filter column: {filter_column}")
            safe_table = _safe_ident(table_name)
            safe_col = _safe_ident(filter_column)
            count_sql = (
                f"SELECT COUNT(*) AS row_count FROM [dbo].[{safe_table}] WHERE [{safe_col}] = ?"
            )
            cursor.execute(count_sql, (filter_value,))
        else:
            cursor.execute(query_table_row_count(table_name))
        total_rows = cursor.fetchone().row_count

        # Fetch page of data using ROW_NUMBER() (SQL Server 2008 compatible)
        # OFFSET/FETCH NEXT requires SQL Server 2012+.
        quoted = f"[dbo].[{_safe_ident(table_name)}]"
        rn_start = offset + 1
        rn_end = offset + page_size
        if filter_column and filter_value is not None:
            safe_col = _safe_ident(filter_column)
            data_sql = (
                f"SELECT * FROM ("
                f"SELECT *, ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS _rn"
                f" FROM {quoted} WHERE [{safe_col}] = ?"
                f") AS paged WHERE _rn BETWEEN ? AND ?"
            )
            cursor.execute(data_sql, (filter_value, rn_start, rn_end))
        else:
            data_sql = (
                f"SELECT * FROM ("
                f"SELECT *, ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS _rn"
                f" FROM {quoted}"
                f") AS paged WHERE _rn BETWEEN ? AND ?"
            )
            cursor.execute(data_sql, (rn_start, rn_end))

        rows = [tuple(row[:-1]) for row in cursor.fetchall()]

    total_pages = max(1, -(-total_rows // page_size))  # ceil division

    return TableData(
        columns=columns,
        rows=rows,
        total_rows=total_rows,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


def get_table_schema_by_name(table_name: str) -> dict | None:
    """Look up a table's schema by name from metadata, return dict or None."""
    if not validate_table_name(table_name):
        return None
    with reader._connect() as conn:
        cursor = conn.cursor()
        _set_query_timeout(cursor)
        cursor.execute(
            "SELECT TABLE_SCHEMA FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_NAME = ? AND TABLE_TYPE = 'BASE TABLE'",
            (table_name,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        schema = row.TABLE_SCHEMA
    return {"schema": schema, "name": table_name}


def get_table_data_with_schema(
    table_name: str,
    page: int = 1,
    page_size: int = 100,
    filter_column: str | None = None,
    filter_value: str | None = None,
) -> tuple[dict, TableData]:
    """Convenience wrapper: validate + fetch schema + data."""
    info = get_table_schema_by_name(table_name)
    if info is None:
        raise ValueError(f"Table '{table_name}' not found or name is invalid")
    schema_info = get_table_schema(table_name, info["schema"])
    data = get_table_data(table_name, page, page_size, filter_column, filter_value)
    return schema_info, data
