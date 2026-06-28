"""
SQL Server 2008 compatible schema introspection queries.

All queries use INFORMATION_SCHEMA and system catalog views
compatible with SQL Server 2008+. Parameterised where possible;
table names are validated against metadata before use.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def validate_table_name(name: str) -> bool:
    """Return True if the identifier is a safe, unquoted SQL name."""
    return bool(_TABLE_NAME_RE.match(name))


def _quote(name: str) -> str:
    """Double-quote an identifier for safe interpolation in raw SQL.

    Only call after validate_table_name() confirms the name is safe.
    """
    return f"[{name}]"


# ---------------------------------------------------------------------------
# Schema queries
# ---------------------------------------------------------------------------

QUERY_TABLES = """
SELECT
    t.TABLE_SCHEMA,
    t.TABLE_NAME,
    t.TABLE_TYPE,
    (
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.COLUMNS c
        WHERE c.TABLE_SCHEMA = t.TABLE_SCHEMA
          AND c.TABLE_NAME   = t.TABLE_NAME
    ) AS column_count
FROM INFORMATION_SCHEMA.TABLES t
WHERE t.TABLE_TYPE IN ('BASE TABLE', 'VIEW')
ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME
"""

QUERY_TABLE_ROW_COUNT = """
SELECT
    COUNT(*) AS row_count
FROM {quoted_table}
"""


def query_table_row_count(table: str) -> str:
    return QUERY_TABLE_ROW_COUNT.format(quoted_table=_quote(table))


QUERY_COLUMNS = """
SELECT
    c.COLUMN_NAME,
    c.DATA_TYPE,
    c.CHARACTER_MAXIMUM_LENGTH,
    c.NUMERIC_PRECISION,
    c.NUMERIC_SCALE,
    c.IS_NULLABLE,
    c.COLUMN_DEFAULT,
    c.ORDINAL_POSITION,
    CASE
        WHEN pk.COLUMN_NAME IS NOT NULL THEN 1
        ELSE 0
    END AS is_primary_key
FROM INFORMATION_SCHEMA.COLUMNS c
LEFT JOIN (
    SELECT ku.COLUMN_NAME
    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
    JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku
        ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
       AND tc.TABLE_SCHEMA   = ku.TABLE_SCHEMA
       AND tc.TABLE_NAME     = ku.TABLE_NAME
    WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
      AND tc.TABLE_SCHEMA = ?
      AND tc.TABLE_NAME   = ?
) pk ON pk.COLUMN_NAME = c.COLUMN_NAME
WHERE c.TABLE_SCHEMA = ?
  AND c.TABLE_NAME   = ?
ORDER BY c.ORDINAL_POSITION
"""

# SQL Server 2008 does not have STRING_AGG; fall back to FOR XML PATH.
QUERY_INDEXES_2008 = """
SELECT
    i.name AS index_name,
    i.is_unique,
    i.is_primary_key,
    i.type_desc,
    STUFF((
        SELECT ', ' + col2.name
        FROM sys.index_columns ic2
        JOIN sys.columns col2
            ON ic2.object_id = col2.object_id
           AND ic2.column_id = col2.column_id
        WHERE ic2.object_id = i.object_id
          AND ic2.index_id  = i.index_id
        ORDER BY ic2.key_ordinal
        FOR XML PATH('')
    ), 1, 2, '') AS columns
FROM sys.indexes i
JOIN sys.tables t
    ON i.object_id = t.object_id
WHERE t.name = ?
  AND i.name IS NOT NULL
ORDER BY i.is_primary_key DESC, i.name
"""

QUERY_FOREIGN_KEYS = """
SELECT
    fk.name AS fk_name,
    tp.name AS from_table,
    cp.name AS from_column,
    tr.name AS to_table,
    cr.name AS to_column
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc
    ON fk.object_id = fkc.constraint_object_id
JOIN sys.tables tp
    ON fkc.parent_object_id = tp.object_id
JOIN sys.columns cp
    ON fkc.parent_object_id = cp.object_id
   AND fkc.parent_column_id = cp.column_id
JOIN sys.tables tr
    ON fkc.referenced_object_id = tr.object_id
JOIN sys.columns cr
    ON fkc.referenced_object_id = cr.object_id
   AND fkc.referenced_column_id = cr.column_id
ORDER BY tp.name, fk.name
"""

QUERY_TABLE_FOREIGN_KEYS = """
SELECT
    fk.name AS fk_name,
    cp.name AS from_column,
    tr.name AS to_table,
    cr.name AS to_column
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc
    ON fk.object_id = fkc.constraint_object_id
JOIN sys.tables tp
    ON fkc.parent_object_id = tp.object_id
JOIN sys.columns cp
    ON fkc.parent_object_id = cp.object_id
   AND fkc.parent_column_id = cp.column_id
JOIN sys.tables tr
    ON fkc.referenced_object_id = tr.object_id
JOIN sys.columns cr
    ON fkc.referenced_object_id = cr.object_id
   AND fkc.referenced_column_id = cr.column_id
WHERE tp.name = ?
ORDER BY fk.name
"""
