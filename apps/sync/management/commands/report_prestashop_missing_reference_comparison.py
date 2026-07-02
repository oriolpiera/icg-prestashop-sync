import json

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.models import Product
from apps.prestashop.client import PrestashopClient
from apps.sync.reconciliation import (
    classify_product_matches,
    find_candidate_django_combinations,
    resolve_prestashop_combination,
)


def _variant_key(size: str, color: str) -> str:
    return f"{size} | {color}"


class Command(BaseCommand):
    help = (
        "Build a per-reference comparison report for missing PrestaShop combinations, "
        "showing resolved Prestashop variants versus existing Django variants."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            required=True,
            help="Path to write the per-reference comparison JSON report.",
        )
        parser.add_argument(
            "--limit-products",
            type=int,
            default=0,
            help="Optional maximum number of PrestaShop products to inspect (0 = all).",
        )

    def handle(self, *args, **options):
        output_path = options["output"]
        limit_products = options["limit_products"]

        if not output_path:
            raise CommandError("--output is required")

        client = PrestashopClient()
        self.stdout.write("Building missing-combination comparison by reference...")

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
        django_products = list(
            Product.objects.select_related("manufacturer").prefetch_related("combinations")
        )
        product_matches = classify_product_matches(prestashop_products, django_products)
        product_match_by_ps_id = {match.prestashop_product_id: match for match in product_matches}
        django_products_by_id = {product.pk: product for product in django_products}

        reference_report: list[dict] = []
        total_missing = 0

        for ps_product in prestashop_products:
            product_match = product_match_by_ps_id[ps_product.product_id]
            if product_match.status != "safe":
                continue

            django_product = django_products_by_id[product_match.django_product_ids[0]]
            django_variants = sorted(
                {
                    _variant_key(combination.icg_size, combination.icg_color): {
                        "django_combination_id": combination.pk,
                        "size": combination.icg_size,
                        "color": combination.icg_color,
                        "prestashop_id": combination.prestashop_id,
                    }
                    for combination in django_product.combinations.all()
                }.values(),
                key=lambda item: (item["size"], item["color"], item["django_combination_id"]),
            )

            prestashop_variants_map: dict[str, dict] = {}
            missing_variants: list[dict] = []
            unresolved_count = 0

            for ps_combination in client.list_combinations_for_product(ps_product.product_id):
                resolved = resolve_prestashop_combination(ps_combination, value_index)
                if resolved.unresolved_value_ids or (
                    not resolved.resolved_size and not resolved.resolved_color
                ):
                    unresolved_count += 1
                    continue

                variant_key = _variant_key(resolved.resolved_size, resolved.resolved_color)
                prestashop_variants_map[variant_key] = {
                    "prestashop_combination_id": ps_combination.combination_id,
                    "size": resolved.resolved_size,
                    "color": resolved.resolved_color,
                    "resolved_values": resolved.resolved_values,
                }

                django_matches = find_candidate_django_combinations(
                    django_product,
                    resolved_size=resolved.resolved_size,
                    resolved_color=resolved.resolved_color,
                )
                if django_matches:
                    continue

                missing_variants.append(
                    {
                        "prestashop_combination_id": ps_combination.combination_id,
                        "size": resolved.resolved_size,
                        "color": resolved.resolved_color,
                        "resolved_values": resolved.resolved_values,
                    }
                )

            if not missing_variants:
                continue

            total_missing += len(missing_variants)
            reference_report.append(
                {
                    "reference": ps_product.reference,
                    "manufacturer": django_product.manufacturer.name
                    if django_product.manufacturer
                    else None,
                    "django_product_id": django_product.pk,
                    "prestashop_product_id": ps_product.product_id,
                    "missing_count": len(missing_variants),
                    "django_variant_count": len(django_variants),
                    "prestashop_variant_count": len(prestashop_variants_map),
                    "prestashop_unresolved_variant_count": unresolved_count,
                    "django_variants": django_variants,
                    "prestashop_variants": sorted(
                        prestashop_variants_map.values(),
                        key=lambda item: (
                            item["size"],
                            item["color"],
                            item["prestashop_combination_id"],
                        ),
                    ),
                    "missing_variants": sorted(
                        missing_variants,
                        key=lambda item: (
                            item["size"],
                            item["color"],
                            item["prestashop_combination_id"],
                        ),
                    ),
                }
            )

        reference_report.sort(key=lambda item: (-item["missing_count"], item["reference"]))

        report = {
            "summary": {
                "reference_count": len(reference_report),
                "missing_combination_count": total_missing,
            },
            "references": reference_report,
        }

        with open(output_path, "w", encoding="utf-8") as output_file:
            json.dump(report, output_file, indent=2, sort_keys=True)

        self.stdout.write(
            self.style.SUCCESS(f"Wrote missing reference comparison report to {output_path}")
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Comparison summary: "
                f"references={report['summary']['reference_count']} "
                f"missing_combinations={report['summary']['missing_combination_count']}"
            )
        )
