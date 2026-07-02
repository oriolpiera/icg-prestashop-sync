import json

from django.core.management.base import BaseCommand

from apps.catalog.models import Combination, Product
from apps.prestashop.client import PrestashopClient
from apps.sync.reconciliation import (
    classify_product_matches,
    resolve_prestashop_combination,
)


class Command(BaseCommand):
    help = (
        "Write back safe Prestashop combination mappings into Django. "
        "Default mode is dry-run; use --apply to persist safe matches only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist safe combination Prestashop IDs into Django.",
        )
        parser.add_argument(
            "--limit-products",
            type=int,
            default=0,
            help="Optional maximum number of Prestashop products to inspect (0 = all).",
        )
        parser.add_argument(
            "--output-conflicts",
            help="Optional path to write combination conflicts as JSON.",
        )

    def handle(self, *args, **options):
        client = PrestashopClient()
        apply = options["apply"]
        limit_products = options["limit_products"]
        output_conflicts = options.get("output_conflicts")

        groups = client.list_attribute_groups()
        group_index = {
            int(group["ps_id"]): str(group["name"])
            for group in groups
            if isinstance(group.get("ps_id"), int)
        }
        value_index: dict[int, dict[str, str | int]] = {}
        for group_id, group_name in group_index.items():
            values = client.list_attribute_values(group_id)
            for value in values:
                value_id = value.get("ps_id")
                if not isinstance(value_id, int):
                    continue
                value_index[value_id] = {
                    "name": str(value.get("name") or ""),
                    "group_name": group_name,
                    "group_prestashop_id": group_id,
                }

        prestashop_products = client.list_products(limit=limit_products)
        django_products = list(Product.objects.all())
        product_matches = classify_product_matches(prestashop_products, django_products)
        product_match_by_ps_id = {match.prestashop_product_id: match for match in product_matches}
        django_products_by_id = {product.pk: product for product in django_products}

        safe = 0
        missing = 0
        ambiguous = 0
        unresolved = 0
        updated = 0
        skipped_existing = 0
        skipped_conflict = 0
        conflicts: list[dict[str, str | int]] = []

        for ps_product in prestashop_products:
            product_match = product_match_by_ps_id[ps_product.product_id]
            if product_match.status != "safe":
                continue

            django_product = django_products_by_id[product_match.django_product_ids[0]]
            if django_product.prestashop_id != ps_product.product_id:
                unresolved += 1
                self.stdout.write(
                    self.style.WARNING(
                        "Skipping product reference "
                        f"{django_product.reference}: Django product is not mapped to "
                        f"Prestashop product #{ps_product.product_id}."
                    )
                )
                continue

            ps_combinations = client.list_combinations_for_product(ps_product.product_id)
            for ps_combination in ps_combinations:
                resolved = resolve_prestashop_combination(ps_combination, value_index)
                resolved_size = resolved.resolved_size
                resolved_color = resolved.resolved_color
                unresolved_value_ids = resolved.unresolved_value_ids

                if unresolved_value_ids or not resolved_size or not resolved_color:
                    unresolved += 1
                    continue

                django_matches = list(
                    Combination.objects.filter(
                        product=django_product,
                        icg_size=resolved_size,
                        icg_color=resolved_color,
                    )
                )
                if len(django_matches) == 0:
                    missing += 1
                    continue
                if len(django_matches) > 1:
                    ambiguous += 1
                    continue

                safe += 1
                combination = django_matches[0]
                if combination.prestashop_id == ps_combination.combination_id:
                    skipped_existing += 1
                    continue

                if (
                    combination.prestashop_id is not None
                    and combination.prestashop_id != ps_combination.combination_id
                ):
                    skipped_conflict += 1
                    conflicts.append(
                        {
                            "reference": django_product.reference,
                            "icg_size": resolved_size,
                            "icg_color": resolved_color,
                            "django_combination_id": combination.pk,
                            "django_prestashop_id": combination.prestashop_id,
                            "matched_prestashop_id": ps_combination.combination_id,
                            "prestashop_product_id": ps_product.product_id,
                        }
                    )
                    self.stdout.write(
                        self.style.WARNING(
                            "Conflict for combination "
                            f"{django_product.reference}/{resolved_size}/{resolved_color}: "
                            f"Django has PS #{combination.prestashop_id}, safe match points to "
                            f"PS #{ps_combination.combination_id}. Skipping."
                        )
                    )
                    continue

                if apply:
                    combination.prestashop_id = ps_combination.combination_id
                    combination.sync_required = False
                    combination.last_sync_error = ""
                    combination.save(
                        update_fields=[
                            "prestashop_id",
                            "sync_required",
                            "last_sync_error",
                            "updated_at",
                        ]
                    )
                updated += 1

        if output_conflicts:
            with open(output_conflicts, "w", encoding="utf-8") as output_file:
                json.dump(conflicts, output_file, indent=2, sort_keys=True)
            self.stdout.write(self.style.SUCCESS(f"Wrote conflict report to {output_conflicts}"))

        mode = "APPLIED" if apply else "DRY RUN"
        self.stdout.write(
            self.style.SUCCESS(
                f"[{mode}] Combination reconciliation: "
                f"safe={safe} missing={missing} ambiguous={ambiguous} unresolved={unresolved}"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Write-back results: "
                f"updated={updated} already_mapped={skipped_existing} conflicts={skipped_conflict}"
            )
        )
