# AGENTS.md

This file is the repository-level source of truth for how humans and AI agents should work in `icg-prestashop-sync`.

## Project purpose

This repository implements the new ICG Manager -> Prestashop synchronization service.

Current phase focus:
- products
- prices
- stock
- Django admin operations for support staff

Out of scope for now:
- Prestashop -> ICG order flows
- custom frontend outside Django admin
- bidirectional catalog editing

When in doubt, follow `README.md` for bootstrap details and `docs/architecture.md` for domain boundaries.

## Architecture guardrails

- `apps/icg/`: read and normalize source data from ICG / MSSQL.
- `apps/catalog/`: internal catalog representation and Prestashop mappings.
- `apps/sync/`: cursors, jobs, retries, Celery task orchestration.
- `apps/prestashop/`: Prestashop API integration only.
- `apps/operations/`: Django admin for visibility and manual actions.
- `apps/core/`: shared primitives and cross-cutting concerns.

Do not blur these responsibilities just to move faster.

## Working agreements

1. Start from a GitHub issue when the change is not trivial.
2. Keep branches short-lived and named after the work, for example `feat/7-agents-md` or `fix/catalog-sync-cursor`.
3. Use conventional commits only.
4. Keep pull requests focused and easy to review.
5. Never commit secrets, `.env`, local database files, or machine-specific state.
6. Treat `celerybeat-schedule`, Redis dump files, and similar runtime artifacts as disposable local state.

## Local development commands

### Quick path

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

Optional worker processes:

```bash
celery -A config worker -l info
celery -A config beat -l info
```

Tmux workflow:

```bash
bin/dev-tmux
```

That launcher expects:
- `tmux` installed
- `.venv` already created
- `.env` present at repo root

## Preferred quality checks

Run these before asking for review:

```bash
pre-commit run --all-files
python -m pytest
python -m pytest --cov --cov-report=term-missing --cov-report=xml
.venv/bin/mypy
ruff check .
ruff format .
python manage.py check --settings=config.settings.test
```

If you touch models, settings, Celery wiring, or startup behavior, also verify the affected command path directly.

Install hooks once per clone:

```bash
pre-commit install
```

## Testing expectations

- Put automated coverage in `tests/`.
- Prefer small, behavior-oriented tests over broad fragile integration tests.
- For bug fixes, add or update a regression test when the scenario can be reproduced.
- Coverage baseline is informative for now; there is no enforced minimum threshold yet.
- Typing baseline is intentionally narrow for now: service seams plus configuration modules.
- Do not skip quality checks just because the change is documentation-heavy; at minimum, verify the changed docs still match the repo.

## AI-agent instructions

- Read the smallest amount of code needed before changing anything.
- Prefer existing project patterns over inventing new structure.
- Keep changes aligned with the current stack: Django, PostgreSQL, Redis, Celery, `pyodbc`, Prestashop Webservice integration.
- Do not introduce frontend-heavy solutions when Django admin already covers the operational need.
- Do not move source-of-truth editing into Django admin; catalog corrections belong in ICG.
- If a change affects sync semantics, preserve cursor-based syncing and idempotent retries.
- Update documentation when repo conventions or workflows change.

## Review checklist

- [ ] The change stays inside the correct app boundary.
- [ ] Local commands in docs still match the repo.
- [ ] Tests and checks relevant to the change were run.
- [ ] No secrets or runtime artifacts were added.
- [ ] Any new workflow or convention is documented here if it should persist.

## Reference files

- `README.md` - bootstrap and local run flow
- `docs/architecture.md` - architecture boundaries and operational model
- `docker-compose.yml` - local PostgreSQL and Redis services
- `bin/dev-tmux` - preferred tmux-based development session
