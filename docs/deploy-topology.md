# Deploy topology contract

This repository stays public and contains the application code plus the public deployment contract.
The concrete VPS deployment artifacts live in the separate private infrastructure repository `icg-prestashop-sync-infra`.

## Quick path

1. Keep local development in this repository using the existing root `docker-compose.yml`.
2. Keep real VPS deployment artifacts in the private infrastructure repository.
3. Treat this document as the public contract that the private repo must implement.

## Repository split

| Area | Lives here | Lives in `icg-prestashop-sync-infra` |
| --- | --- | --- |
| Django application code | Yes | No |
| Local development compose | Yes | No |
| Public deployment topology contract | Yes | No |
| Traefik runtime config | No | Yes |
| VPS compose stacks for `proxy`, `prod`, `test` | No | Yes |
| Real host paths, backup jobs, cron, restore scripts | No | Yes |
| Secrets and environment values | No | Yes |

## Public ingress contract

The target VPS topology uses one shared ingress layer and two isolated application environments.

### Shared ingress

- Traefik is the only service allowed to bind public ports `80` and `443`.
- Traefik must use the file provider.
- Traefik must not depend on Docker provider discovery or Docker labels.
- `prod` and `test` may share only the public `proxy` network.

### Public hostnames

| Hostname | Target environment | Target service role |
| --- | --- | --- |
| `shop.pierabellesarts.cat` | `prod` | Prestashop storefront |
| `sync.pierabellesarts.cat` | `prod` | Django admin and sync operations |
| `db.pierabellesarts.cat` | `prod` | Adminer |
| `test.shop.pierabellesarts.cat` | `test` | Prestashop test storefront |
| `test.sync.pierabellesarts.cat` | `test` | Django test admin and sync operations |

`db.pierabellesarts.cat` is production-only. There is no public test Adminer hostname.

## Environment isolation contract

The private infrastructure repository must keep `prod` and `test` isolated from each other.

| Concern | Rule |
| --- | --- |
| Public ingress | Shared `proxy` network only |
| Internal app/data traffic | Separate backend network per environment |
| PostgreSQL state | Separate per environment |
| Redis state | Separate per environment |
| MariaDB state | Separate per environment |
| Prestashop filesystem state | Separate per environment |
| Runtime env files | Separate per environment |

The `test` environment is intentionally lightweight. It does not need production-fidelity assets or styling, but it must remain useful for deployment and integration validation.

## Django-facing requirements

The private deployment must provide environment values compatible with the existing application settings.

- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `DATABASE_*`
- `CELERY_BROKER_URL`
- `CELERY_RESULT_BACKEND`
- `PRESTASHOP_BASE_URL`
- `PRESTASHOP_HOST`

`config/settings/production.py` already expects proxy-aware HTTPS forwarding through `SECURE_PROXY_SSL_HEADER`.

## Local vs VPS compose

The root `docker-compose.yml` in this repository is for local development only.
It is not the source of truth for the VPS topology.

That means:

- do not add real VPS Traefik or host-specific deploy state to the root compose file
- do not assume the local dev compose reflects the production stack
- keep public docs here and concrete VPS implementation in the private infra repo

## Suggested private repo structure

This repository does not own the private repo, but this shape is the expected target:

```text
icg-prestashop-sync-infra/
  README.md
  compose/
    proxy/
      docker-compose.yml
      traefik.yml
      dynamic/
        routes.yml
    prod/
      docker-compose.yml
      .env.example
    test/
      docker-compose.yml
      .env.example
  docs/
    deploy.md
    backup-restore.md
    rollback.md
  scripts/
    deploy-proxy.sh
    deploy-prod.sh
    deploy-test.sh
```

## Related issues

- `#86`: shared Traefik separation and public ingress contract
- `#87`: isolated public `test` stack behind the shared ingress
- `#88`: isolated `prod` stack with production-only Adminer
