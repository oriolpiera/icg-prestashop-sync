# Architecture

## Purpose

This project replaces the legacy Django 1.x integration with a simpler and more robust service.

The source of truth for catalog data remains ICG Manager.
Prestashop acts as the ecommerce destination for product catalog, prices and stock.

## Operational goals

The system must support these day-to-day operations:
- see whether a product is synchronized or failing
- inspect the latest sync error
- retry a single product manually from Django admin
- retry batches when an external issue is fixed
- keep an audit trail of what was sent to Prestashop and what failed

## High-level architecture

The project is split into a small number of clear responsibilities:

1. `icg` reads incremental changes from Microsoft SQL Server views.
2. `catalog` maps ICG records into internal domain records.
3. `sync` queues and executes synchronization jobs.
4. `prestashop` sends validated payloads to Prestashop Webservice.
5. `operations` exposes status, errors and manual actions in Django admin.

## Proposed Django apps

### `core`

Shared settings, utilities, health checks and common base models.

### `icg`

Responsibilities:
- connect to Microsoft SQL Server through `pyodbc`
- read the provided ICG views for products, prices and stock
- normalize raw rows into internal DTOs or service objects

This app should not know anything about Django admin or Prestashop payload details.

### `catalog`

Responsibilities:
- store the local representation of products, combinations, prices and stock
- keep mapping data between ICG identifiers and Prestashop identifiers
- track whether a record needs synchronization

Likely models:
- `Product`
- `Combination`
- `Price`
- `Stock`
- `PrestashopMapping`

### `sync`

Responsibilities:
- store synchronization cursors for each MSSQL view
- create sync events or jobs
- track attempts, errors and timestamps
- expose retry services

Likely models:
- `SyncCursor`
- `SyncJob`
- `SyncError`

Celery tasks will live here or very close to it.

### `prestashop`

Responsibilities:
- wrap the Prestashop Webservice API
- build payloads from internal catalog models
- apply idempotent create or update logic
- raise structured errors when Prestashop rejects data

This app should be the only place that knows Prestashop API details.

### `operations`

Responsibilities:
- configure Django admin
- show sync state and latest errors
- provide admin actions such as `retry sync` or `refresh from ICG`
- keep the interface simple for shop staff

No custom frontend is planned initially. Django admin is enough.

## Data flow

### Incremental import from ICG

1. A scheduled task reads one MSSQL view.
2. The task uses a persisted cursor, not a fixed time window.
3. Changed rows are normalized and upserted into local catalog tables.
4. A sync job is created for each affected entity.

### Push to Prestashop

1. A worker picks pending sync jobs.
2. The worker loads the related catalog entity.
3. The worker builds the Prestashop payload.
4. The worker creates or updates the target entity in Prestashop.
5. The worker stores success or structured failure details.

## Important rules

### Source of truth

Catalog data must be corrected in ICG, not in Django.
Django admin is for visibility and controlled retries, not for product editing.

### Cursors over time windows

The legacy project relied on queries like "changes in the last hour".
This project must persist explicit sync cursors to avoid missed or duplicated updates.

### Idempotency

Retrying the same product twice must be safe.
The sync process should always be able to resume after temporary failures.

### Structured errors

Prestashop validation errors should be stored in a readable format so staff can understand what failed.
Examples:
- invalid character in product name
- missing category mapping
- broken manufacturer mapping

## Current folder direction

This is the shape created in the repository skeleton:

```text
icg-prestashop-sync/
  docs/
    architecture.md
  config/
    settings/
    urls.py
    celery.py
  apps/
    core/
    icg/
    catalog/
    sync/
    prestashop/
    operations/
  manage.py
```

The exact naming can still move a little, but the separation of responsibilities should stay.

## Out of scope for phase 1

These topics are intentionally deferred:
- custom frontend outside Django admin
- bidirectional catalog editing
- advanced business workflows inside Django
