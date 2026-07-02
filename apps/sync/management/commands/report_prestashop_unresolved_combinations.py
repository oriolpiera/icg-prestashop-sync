import json
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.models import Product
from apps.prestashop.client import PrestashopClient
from apps.sync.reconciliation import (
    classify_product_matches,
    group_role,
    resolve_prestashop_combination,
)


class Command(BaseCommand):
    help = (
        "Build a JSON report for PrestaShop combinations whose size/color cannot be "
        "resolved safely from existing attribute groups and values."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            required=True,
            help="Path to write the unresolved combination JSON report.",
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

        self.stdout.write("Collecting unresolved PrestaShop combinations...")

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

        unresolved: list[dict] = []
        by_group_name: dict[str, dict] = defaultdict(
            lambda: {
                "count": 0,
                "roles": set(),
                "product_references": set(),
                "samples": [],
            }
        )

        for ps_product in prestashop_products:
            product_match = product_match_by_ps_id[ps_product.product_id]
            django_product = None
            if len(product_match.django_product_ids) == 1:
                django_product = django_products_by_id[product_match.django_product_ids[0]]

            ps_combinations = client.list_combinations_for_product(ps_product.product_id)
            for ps_combination in ps_combinations:
                resolved = resolve_prestashop_combination(ps_combination, value_index)
                if (
                    not resolved.unresolved_value_ids
                    and resolved.resolved_size
                    and resolved.resolved_color
                ):
                    continue

                entry = {
                    "reference": ps_product.reference,
                    "prestashop_product_id": ps_product.product_id,
                    "prestashop_combination_id": ps_combination.combination_id,
                    "django_product_id": django_product.pk if django_product else None,
                    "django_product_match_status": product_match.status,
                    "manufacturer": django_product.manufacturer.name
                    if django_product and django_product.manufacturer
                    else None,
                    "resolved_size": resolved.resolved_size,
                    "resolved_color": resolved.resolved_color,
                    "unresolved_value_ids": resolved.unresolved_value_ids,
                    "resolved_values": resolved.resolved_values,
                }
                unresolved.append(entry)

                for value in resolved.resolved_values:
                    group_name = str(value["group_name"])
                    bucket = by_group_name[group_name]
                    bucket["count"] += 1
                    bucket["roles"].add(str(value["role"]))
                    bucket["product_references"].add(ps_product.reference)
                    if len(bucket["samples"]) < 5:
                        bucket["samples"].append(
                            {
                                "reference": ps_product.reference,
                                "prestashop_combination_id": ps_combination.combination_id,
                                "value_name": value["name"],
                                "role": value["role"],
                            }
                        )

                for value_id in resolved.unresolved_value_ids:
                    group_name = f"missing_value_id:{value_id}"
                    bucket = by_group_name[group_name]
                    bucket["count"] += 1
                    bucket["roles"].add("missing")
                    bucket["product_references"].add(ps_product.reference)
                    if len(bucket["samples"]) < 5:
                        bucket["samples"].append(
                            {
                                "reference": ps_product.reference,
                                "prestashop_combination_id": ps_combination.combination_id,
                                "value_name": "",
                                "role": "missing",
                            }
                        )

        summary_groups = []
        for group_name, bucket in by_group_name.items():
            summary_groups.append(
                {
                    "group_name": group_name,
                    "detected_group_role": group_role(group_name)
                    if not group_name.startswith("missing_value_id:")
                    else "missing",
                    "count": bucket["count"],
                    "roles": sorted(bucket["roles"]),
                    "product_reference_count": len(bucket["product_references"]),
                    "samples": bucket["samples"],
                }
            )

        summary_groups.sort(key=lambda item: (-item["count"], item["group_name"]))

        report = {
            "summary": {
                "unresolved_combination_count": len(unresolved),
                "group_bucket_count": len(summary_groups),
            },
            "groups": summary_groups,
            "unresolved_combinations": unresolved,
        }

        with open(output_path, "w", encoding="utf-8") as output_file:
            json.dump(report, output_file, indent=2, sort_keys=True)

        self.stdout.write(
            self.style.SUCCESS(f"Wrote unresolved combination report to {output_path}")
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Unresolved summary: "
                f"combinations={report['summary']['unresolved_combination_count']} "
                f"group_buckets={report['summary']['group_bucket_count']}"
            )
        )
