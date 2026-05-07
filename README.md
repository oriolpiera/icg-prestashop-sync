# icg-prestashop-sync

ICG Manager -> Prestashop catalog synchronization with a Django admin for operations.

## Goal

This repository will host the new integration between ICG Manager and Prestashop.

The main use cases are:
- synchronize products, prices and stock from ICG to Prestashop
- give shop staff a backoffice to inspect sync status and errors
- allow manual reprocessing of a specific product when data issues are fixed in ICG

## Stack

- Django 5
- PostgreSQL
- Redis
- Celery + Celery Beat
- pyodbc for Microsoft SQL Server access
- Prestashop Webservice API client

## Bootstrap

1. Create a virtual environment.
2. Start PostgreSQL and Redis.
3. Install the project in editable mode with dev dependencies.
4. Copy `.env.example` to `.env` and adjust credentials.
5. Run migrations.
6. Create an admin user.

Example:

```bash
python3 -m venv .venv
source .venv/bin/activate
docker-compose up -d
pip install -e .[dev]
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

The project loads environment variables from `.env` automatically.

Optional worker processes:

```bash
celery -A config worker -l info
celery -A config beat -l info
```

## Documentation

- `docs/architecture.md`: initial architecture, app boundaries and operational model

## First scope

The first iteration will focus only on:
- products
- prices
- stock
- operational backoffice in Django admin

Orders from Prestashop to ICG are intentionally out of scope for now.

## Current structure

```text
config/          Django project, settings split, Celery wiring
apps/core/       Shared base models and common building blocks
apps/icg/        ICG access layer placeholders
apps/catalog/    Catalog and Prestashop mapping models
apps/sync/       Synchronization cursors, jobs and Celery tasks
apps/prestashop/ Prestashop client placeholders
apps/operations/ Django admin configuration for operations
tests/           Repository smoke tests
```
