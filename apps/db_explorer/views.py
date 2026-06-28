"""
Read-only views for exploring the ICG SQL Server database schema.

All views require staff access and are completely read-only.
"""

from __future__ import annotations

import logging

from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import EmptyPage, Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render

from apps.db_explorer import db
from apps.db_explorer.sql import validate_table_name

logger = logging.getLogger(__name__)

PAGE_SIZE = 100


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

    # If filter_column is set but filter_value is empty, clear the filter
    if filter_column and not filter_value:
        filter_column = filter_value = None

    try:
        data = db.get_table_data(
            table_name,
            page=page,
            page_size=PAGE_SIZE,
            filter_column=filter_column,
            filter_value=filter_value,
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
