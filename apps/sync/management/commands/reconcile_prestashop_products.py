from django.core.management.base import BaseCommand

from apps.catalog.models import Product
from apps.prestashop.client import PrestashopClient
from apps.sync.reconciliation import classify_product_matches


class Command(BaseCommand):
    help = (
        "Write back safe Prestashop product mappings into Django. "
        "Default mode is dry-run; use --apply to persist safe matches only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist safe product Prestashop IDs into Django.",
        )
        parser.add_argument(
            "--limit-products",
            type=int,
            default=0,
            help="Optional maximum number of Prestashop products to inspect (0 = all).",
        )

    def handle(self, *args, **options):
        client = PrestashopClient()
        apply = options["apply"]
        limit_products = options["limit_products"]

        prestashop_products = client.list_products(limit=limit_products)
        django_products = list(Product.objects.all())
        django_products_by_id = {product.pk: product for product in django_products}

        matches = classify_product_matches(prestashop_products, django_products)

        safe = 0
        missing = 0
        ambiguous = 0
        updated = 0
        skipped_existing = 0
        skipped_conflict = 0

        for match in matches:
            if match.status == "missing":
                missing += 1
                continue
            if match.status == "ambiguous":
                ambiguous += 1
                continue

            safe += 1
            product = django_products_by_id[match.django_product_ids[0]]
            if product.prestashop_id == match.prestashop_product_id:
                skipped_existing += 1
                continue

            if (
                product.prestashop_id is not None
                and product.prestashop_id != match.prestashop_product_id
            ):
                skipped_conflict += 1
                self.stdout.write(
                    self.style.WARNING(
                        "Conflict for reference "
                        f"{product.reference}: Django has PS #{product.prestashop_id}, "
                        f"safe match points to PS #{match.prestashop_product_id}. Skipping."
                    )
                )
                continue

            if apply:
                product.prestashop_id = match.prestashop_product_id
                product.sync_required = False
                product.last_sync_error = ""
                product.save(
                    update_fields=[
                        "prestashop_id",
                        "sync_required",
                        "last_sync_error",
                        "updated_at",
                    ]
                )
            updated += 1

        mode = "APPLIED" if apply else "DRY RUN"
        self.stdout.write(
            self.style.SUCCESS(
                f"[{mode}] Product reconciliation:\
                        safe={safe} missing={missing} ambiguous={ambiguous}"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Write-back results: "
                f"updated={updated} already_mapped={skipped_existing} conflicts={skipped_conflict}"
            )
        )
