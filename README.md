# icg-prestashop-sync

[![Quality](https://github.com/oriolpiera/icg-prestashop-sync/actions/workflows/quality.yml/badge.svg)](https://github.com/oriolpiera/icg-prestashop-sync/actions/workflows/quality.yml)
[![codecov](https://codecov.io/gh/oriolpiera/icg-prestashop-sync/graph/badge.svg)](https://codecov.io/gh/oriolpiera/icg-prestashop-sync)

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

## Tmux launcher

If you use `tmux`, the repository includes `bin/dev-tmux` to open the whole development setup in one session.

It creates these windows:
- `services`: `docker-compose up`
- `web`: `migrate` and `runserver`
- `worker`: Celery worker
- `beat`: Celery beat

Usage:

```bash
bin/dev-tmux
```

Optional custom session name:

```bash
bin/dev-tmux my-session
```

Requirements:
- `tmux` installed on the machine
- `.venv` already created with `pip install -e .[dev]`
- `.env` present in the repository root

## Documentation

- `docs/architecture.md`: initial architecture, app boundaries and operational model
- `AGENTS.md`: repository conventions for contributors and AI agents
- `openspec/README.md`: git-tracked planning artifacts and when to use Engram, OpenSpec, or hybrid mode

## Local quality workflow

Install the local hooks once after `pip install -e .[dev]`:

```bash
pre-commit install
```

Run the default quality pass manually:

```bash
pre-commit run --all-files
ruff check .
ruff format .
python manage.py check --settings=config.settings.test
python -m pytest
```

Run the current typing baseline:

```bash
.venv/bin/mypy
```

Generate a local coverage baseline:

```bash
python -m pytest --cov --cov-report=term-missing --cov-report=xml
```

Current policy:
- publish the coverage baseline first
- do not enforce a minimum threshold yet
- use the XML report in CI as an artifact for later tracking

Recommended rule:
- use `pre-commit run --all-files` before opening a PR
- use `ruff format .` when you want repo-wide formatting
- use `ruff check .` when you want the raw lint output without hook wrapping
- use `.venv/bin/mypy` for the current service/config typing baseline

Current typing policy:
- checker: `mypy`
- baseline scope: `apps/icg/services.py`, `apps/prestashop/client.py`, and `config/settings/`
- CI integration is a clear next step, but not part of this issue

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
