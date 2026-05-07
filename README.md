# icg-prestashop-sync

ICG Manager -> Prestashop catalog synchronization with a Django admin for operations.

## Goal

This repository will host the new integration between ICG Manager and Prestashop.

The main use cases are:
- synchronize products, prices and stock from ICG to Prestashop
- give shop staff a backoffice to inspect sync status and errors
- allow manual reprocessing of a specific product when data issues are fixed in ICG

## Planned stack

- Django 5
- PostgreSQL
- Redis
- Celery + Celery Beat
- pyodbc for Microsoft SQL Server access
- Prestashop Webservice API client

## Documentation

- `docs/architecture.md`: initial architecture, app boundaries and operational model

## First scope

The first iteration will focus only on:
- products
- prices
- stock
- operational backoffice in Django admin

Orders from Prestashop to ICG are intentionally out of scope for now.
