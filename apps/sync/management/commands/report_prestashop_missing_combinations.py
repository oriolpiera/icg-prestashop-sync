import json
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.models import Product
from apps.prestashop.client import PrestashopClient
from apps.sync.reconciliation import (
    classify_product_matches,
    find_candidate_django_combinations,
    group_role,
    resolve_prestashop_combination,
)


class Command(BaseCommand):
    help = (
        "Build a JSON report for PrestaShop combinations that resolve cleanly but have "
        "no matching Django combination."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            required=True,
            help="Path to write the missing combination JSON report.",
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

        self.stdout.write("Collecting missing PrestaShop combinations...")

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
        django_products = list(Product.objects.select_related("manufacturer").all())
        product_matches = classify_product_matches(prestashop_products, django_products)
        product_match_by_ps_id = {match.prestashop_product_id: match for match in product_matches}
        django_products_by_id = {product.pk: product for product in django_products}

        missing: list[dict] = []
        by_reference: dict[str, dict] = defaultdict(
            lambda: {
                "count": 0,
                "manufacturers": set(),
                "products": set(),
                "samples": [],
            }
        )
        by_group_name: dict[str, dict] = defaultdict(
            lambda: {
                "count": 0,
                "roles": set(),
                "products": set(),
                "samples": [],
            }
        )

        for ps_product in prestashop_products:
            product_match = product_match_by_ps_id[ps_product.product_id]
            if product_match.status != "safe":
                continue

            django_product = django_products_by_id[product_match.django_product_ids[0]]
            ps_combinations = client.list_combinations_for_product(ps_product.product_id)

            for ps_combination in ps_combinations:
                resolved = resolve_prestashop_combination(ps_combination, value_index)
                if resolved.unresolved_value_ids or (
                    not resolved.resolved_size and not resolved.resolved_color
                ):
                    continue

                django_matches = find_candidate_django_combinations(
                    django_product,
                    resolved_size=resolved.resolved_size,
                    resolved_color=resolved.resolved_color,
                )
                if django_matches:
                    continue

                entry = {
                    "reference": ps_product.reference,
                    "prestashop_product_id": ps_product.product_id,
                    "prestashop_combination_id": ps_combination.combination_id,
                    "django_product_id": django_product.pk,
                    "manufacturer": django_product.manufacturer.name
                    if django_product.manufacturer
                    else None,
                    "resolved_size": resolved.resolved_size,
                    "resolved_color": resolved.resolved_color,
                    "resolved_values": resolved.resolved_values,
                }
                missing.append(entry)

                ref_bucket = by_reference[ps_product.reference]
                ref_bucket["count"] += 1
                ref_bucket["products"].add(ps_product.reference)
                if django_product.manufacturer:
                    ref_bucket["manufacturers"].add(django_product.manufacturer.name)
                if len(ref_bucket["samples"]) < 5:
                    ref_bucket["samples"].append(
                        {
                            "prestashop_combination_id": ps_combination.combination_id,
                            "resolved_size": resolved.resolved_size,
                            "resolved_color": resolved.resolved_color,
                        }
                    )

                for value in resolved.resolved_values:
                    group_name = str(value["group_name"])
                    group_bucket = by_group_name[group_name]
                    group_bucket["count"] += 1
                    group_bucket["roles"].add(str(value["role"]))
                    group_bucket["products"].add(ps_product.reference)
                    if len(group_bucket["samples"]) < 5:
                        group_bucket["samples"].append(
                            {
                                "reference": ps_product.reference,
                                "prestashop_combination_id": ps_combination.combination_id,
                                "value_name": value["name"],
                                "role": value["role"],
                            }
                        )

        report = {
            "summary": {
                "missing_combination_count": len(missing),
                "reference_bucket_count": len(by_reference),
                "group_bucket_count": len(by_group_name),
            },
            "references": [
                {
                    "reference": reference,
                    "count": bucket["count"],
                    "manufacturers": sorted(bucket["manufacturers"]),
                    "samples": bucket["samples"],
                }
                for reference, bucket in sorted(
                    by_reference.items(), key=lambda item: (-item[1]["count"], item[0])
                )
            ],
            "groups": [
                {
                    "group_name": group_name,
                    "detected_group_role": group_role(group_name),
                    "count": bucket["count"],
                    "roles": sorted(bucket["roles"]),
                    "product_reference_count": len(bucket["products"]),
                    "samples": bucket["samples"],
                }
                for group_name, bucket in sorted(
                    by_group_name.items(), key=lambda item: (-item[1]["count"], item[0])
                )
            ],
            "missing_combinations": missing,
        }

        with open(output_path, "w", encoding="utf-8") as output_file:
            json.dump(report, output_file, indent=2, sort_keys=True)

        self.stdout.write(self.style.SUCCESS(f"Wrote missing combination report to {output_path}"))
        self.stdout.write(
            self.style.SUCCESS(
                "Missing summary: "
                f"combinations={report['summary']['missing_combination_count']} "
                f"references={report['summary']['reference_bucket_count']} "
                f"groups={report['summary']['group_bucket_count']}"
            )
        )
