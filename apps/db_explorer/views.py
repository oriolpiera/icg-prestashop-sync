"""
Read-only views for exploring the ICG SQL Server database schema.

All views require staff access and are completely read-only.
"""

from __future__ import annotations

import logging
from datetime import datetime, time

from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import EmptyPage, Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.dateparse import parse_date, parse_datetime

from apps.db_explorer import db
from apps.db_explorer.sql import validate_table_name

logger = logging.getLogger(__name__)

PAGE_SIZE = 100

TABLE_FILTERS = {
    "ClientesWeb": [
        {"column": "Estado", "param": "estado", "label": "Estado", "kind": "text"},
        {
            "column": "FechaExportacion",
            "param": "fecha_exportacion",
            "label": "FechaExportacion",
            "kind": "datetime",
        },
        {
            "column": "FechaInsercion",
            "param": "fecha_insercion",
            "label": "FechaInsercion",
            "kind": "datetime",
        },
        {
            "column": "CodClienteWeb",
            "param": "codclienteweb",
            "label": "CODCLIENTEWEB",
            "kind": "text",
        },
    ],
    "FacturasWeb": [
        {"column": "Estado", "param": "estado", "label": "Estado", "kind": "text"},
        {
            "column": "FechaExportacion",
            "param": "fecha_exportacion",
            "label": "FechaExportacion",
            "kind": "datetime",
        },
        {
            "column": "FechaInsercion",
            "param": "fecha_insercion",
            "label": "FechaInsercion",
            "kind": "datetime",
        },
        {
            "column": "CodClienteWeb",
            "param": "codclienteweb",
            "label": "CODCLIENTEWEB",
            "kind": "text",
        },
        {
            "column": "CodArticulo",
            "param": "codarticulo",
            "label": "CODARTICULO",
            "kind": "text",
        },
        {
            "column": "FechaDocumento",
            "param": "fecha_documento",
            "label": "FECHADOCUMENTO",
            "kind": "datetime",
        },
    ],
}


def _parse_datetime_filter(raw_value: str, *, is_end: bool) -> datetime | None:
    value = raw_value.strip()
    if not value:
        return None

    parsed_datetime = parse_datetime(value)
    if parsed_datetime is not None:
        return parsed_datetime.replace(tzinfo=None)

    parsed_date = parse_date(value)
    if parsed_date is not None:
        boundary = time.max if is_end else time.min
        return datetime.combine(parsed_date, boundary)

    raise ValueError(f"Invalid datetime filter value: {raw_value}")


def _get_table_filters(
    request: HttpRequest, table_name: str
) -> tuple[list[db.FilterCondition], list[dict]]:
    definitions = TABLE_FILTERS.get(table_name, [])
    conditions: list[db.FilterCondition] = []
    states: list[dict] = []

    for field in definitions:
        state = {
            "column": field["column"],
            "param": field["param"],
            "label": field["label"],
            "kind": field["kind"],
            "value": request.GET.get(field["param"], "").strip(),
            "from": request.GET.get(f"{field['param']}_from", "").strip(),
            "to": request.GET.get(f"{field['param']}_to", "").strip(),
            "presence": request.GET.get(f"{field['param']}_presence", "any").strip() or "any",
        }

        if field["kind"] == "text":
            if state["value"]:
                conditions.append(
                    db.FilterCondition(column=field["column"], operator="eq", value=state["value"])
                )
        else:
            if state["presence"] == "set":
                conditions.append(
                    db.FilterCondition(column=field["column"], operator="is_not_null")
                )
            elif state["presence"] == "not_set":
                conditions.append(db.FilterCondition(column=field["column"], operator="is_null"))

            if state["from"]:
                conditions.append(
                    db.FilterCondition(
                        column=field["column"],
                        operator="gte",
                        value=_parse_datetime_filter(state["from"], is_end=False),
                    )
                )

            if state["to"]:
                conditions.append(
                    db.FilterCondition(
                        column=field["column"],
                        operator="lte",
                        value=_parse_datetime_filter(state["to"], is_end=True),
                    )
                )

        states.append(state)

    return conditions, states


def _build_query_string(request: HttpRequest, **updates: str | int | None) -> str:
    params = request.GET.copy()
    for key, value in updates.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = str(value)
    return params.urlencode()


def _build_column_headers(
    request: HttpRequest,
    columns: list[str],
    sortable_columns: set[str],
    current_sort: str | None,
    current_direction: str,
) -> list[dict]:
    headers = []
    for column in columns:
        is_sortable = column in sortable_columns
        is_current = current_sort == column
        next_direction = "desc" if is_current and current_direction == "asc" else "asc"
        indicator = ""
        if is_current:
            indicator = "▼" if current_direction == "desc" else "▲"
        headers.append(
            {
                "name": column,
                "is_sortable": is_sortable,
                "is_current": is_current,
                "indicator": indicator,
                "query_string": _build_query_string(
                    request,
                    sort=column if is_sortable else None,
                    direction=next_direction if is_sortable else None,
                    page=None,
                )
                if is_sortable
                else "",
            }
        )
    return headers


@staff_member_required
def table_list(request: HttpRequest) -> HttpResponse:
    """Show all tables/views in the ICG database."""
    tables = db.get_tables()
    return render(request, "db_explorer/table_list.html", {"tables": tables, "all_tables": tables})


@staff_member_required
def table_detail(request: HttpRequest, table_name: str) -> HttpResponse:
    """Show schema + paginated data for a single table."""
    if not validate_table_name(table_name):
        raise Http404(f"Invalid table name: {table_name}")

    info = db.get_table_schema_by_name(table_name)
    if info is None:
        raise Http404(f"Table '{table_name}' not found")

    schema_info = db.get_table_schema(table_name, info["schema"])

    # Pagination
    page = request.GET.get("page", 1)
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1

    # Filtering
    filter_column = request.GET.get("filter_column", "").strip() or None
    filter_value = request.GET.get("filter_value", "").strip() or None
    structured_filters, table_filter_states = _get_table_filters(request, table_name)

    # If filter_column is set but filter_value is empty, clear the filter
    if filter_column and not filter_value:
        filter_column = filter_value = None

    if structured_filters:
        filter_column = filter_value = None

    sortable_columns = {field["column"] for field in TABLE_FILTERS.get(table_name, [])}
    sort_column = request.GET.get("sort", "").strip() or None
    sort_direction = request.GET.get("direction", "asc").strip().lower()
    if sort_direction not in {"asc", "desc"}:
        sort_direction = "asc"
    if sort_column and sort_column not in sortable_columns:
        sort_column = None

    try:
        data = db.get_table_data(
            table_name,
            schema=info["schema"],
            page=page,
            page_size=PAGE_SIZE,
            filter_column=filter_column,
            filter_value=filter_value,
            filters=structured_filters,
            sort_column=sort_column,
            sort_desc=sort_direction == "desc",
        )
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    # Build paginator
    paginator = Paginator(range(data.total_rows, 0, -1), PAGE_SIZE)
    try:
        page_obj = paginator.page(page)
    except EmptyPage:
        page = paginator.num_pages
        page_obj = paginator.page(page)
    data.page = page

    # All table names for sidebar
    all_tables = db.get_tables()

    column_headers = _build_column_headers(
        request,
        data.columns,
        sortable_columns,
        sort_column,
        sort_direction,
    )

    return render(
        request,
        "db_explorer/table_detail.html",
        {
            "table_name": table_name,
            "schema_info": schema_info,
            "data": data,
            "page_obj": page_obj,
            "all_tables": all_tables,
            "filter_column": filter_column or "",
            "filter_value": filter_value or "",
            "columns": schema_info["columns"],
            "table_filters": table_filter_states,
            "has_structured_filters": bool(TABLE_FILTERS.get(table_name)),
            "column_headers": column_headers,
            "current_sort": sort_column or "",
            "current_direction": sort_direction,
            "pagination_query": _build_query_string(request, page=None),
        },
    )


@staff_member_required
def relationships(request: HttpRequest) -> HttpResponse:
    """Show all foreign key relationships."""
    fks = db.get_all_foreign_keys()
    all_tables = db.get_tables()
    return render(
        request,
        "db_explorer/relationships.html",
        {"foreign_keys": fks, "all_tables": all_tables},
    )
