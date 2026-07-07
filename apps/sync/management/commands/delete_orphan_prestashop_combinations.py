from django.core.management.base import BaseCommand

from apps.catalog.models import Product
from apps.prestashop.client import PrestashopClient
from apps.sync.reconciliation import (
    find_candidate_django_combinations,
    resolve_prestashop_combination,
)


class Command(BaseCommand):
    help = (
        "Delete Prestashop combinations that belong to active+visible_web products "
        "but have no matching combination in Django. "
        "Default mode is dry-run; use --apply to delete."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete combinations from Prestashop. Without this flag, only reports.",
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
        django_products = Product.objects.filter(visible_web=True, discontinued=False)
        django_products_by_reference: dict[str, Product] = {p.reference: p for p in django_products}

        deleted = 0
        missing = 0
        skipped_inactive_product = 0
        errors = 0

        for ps_product in prestashop_products:
            django_product = django_products_by_reference.get(ps_product.reference)
            if django_product is None:
                skipped_inactive_product += 1
                continue

            if django_product.prestashop_id != ps_product.product_id:
                skipped_inactive_product += 1
                continue

            ps_combinations = client.list_combinations_for_product(ps_product.product_id)
            for ps_combination in ps_combinations:
                resolved = resolve_prestashop_combination(ps_combination, value_index)
                resolved_size = resolved.resolved_size
                resolved_color = resolved.resolved_color

                if resolved.unresolved_value_ids or (not resolved_size and not resolved_color):
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping PS combination #{ps_combination.combination_id}: "
                            f"unresolved values {resolved.unresolved_value_ids} or no "
                            f"resolved size/color."
                        )
                    )
                    continue

                django_matches = find_candidate_django_combinations(
                    django_product,
                    resolved_size=resolved_size,
                    resolved_color=resolved_color,
                )

                if len(django_matches) == 0:
                    missing += 1
                    self.stdout.write(
                        f"  PS #{ps_combination.combination_id} "
                        f"({django_product.reference}/{resolved_size}/{resolved_color}): "
                        f"not found in Django"
                    )
                    if apply:
                        try:
                            client.delete_combination(ps_combination.combination_id)
                            deleted += 1
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"  Deleted PS combination #{ps_combination.combination_id}"
                                )
                            )
                        except Exception as exc:
                            errors += 1
                            self.stdout.write(
                                self.style.ERROR(
                                    f"  Failed to delete PS combination "
                                    f"#{ps_combination.combination_id}: {exc}"
                                )
                            )

        mode = "DELETED" if apply else "DRY RUN"
        self.stdout.write(
            self.style.SUCCESS(
                f"[{mode}] Orphan Prestashop combinations cleanup: "
                f"deleted={deleted} missing_from_django={missing} "
                f"skipped_inactive_product={skipped_inactive_product} errors={errors}"
            )
        )
