import json
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.prestashop.client import PrestashopClient
from apps.sync.reconciliation import group_role


class Command(BaseCommand):
    help = (
        "Build a report of PrestaShop products with many combinations and more than "
        "one color attribute group."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--min-combinations",
            type=int,
            default=50,
            help="Minimum number of combinations required to include a product.",
        )
        parser.add_argument(
            "--min-color-groups",
            type=int,
            default=2,
            help="Minimum number of distinct color groups required to include a product.",
        )
        parser.add_argument(
            "--limit-products",
            type=int,
            default=0,
            help="Optional maximum number of PrestaShop products to inspect (0 = all).",
        )
        parser.add_argument(
            "--output",
            help="Optional path to write the full JSON report.",
        )

    def handle(self, *args, **options):
        min_combinations = options["min_combinations"]
        min_color_groups = options["min_color_groups"]
        limit_products = options["limit_products"]
        output = options.get("output")

        client = PrestashopClient()

        self.stdout.write("Collecting PrestaShop products with multiple color groups...")

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

        products = client.list_products(limit=limit_products)
        matches: list[dict[str, object]] = []

        for product in products:
            combinations = client.list_combinations_for_product(product.product_id)
            combination_count = len(combinations)
            if combination_count <= min_combinations:
                continue

            color_groups: set[str] = set()
            all_groups: set[str] = set()
            unresolved_value_ids: set[int] = set()

            for combination in combinations:
                for value_id in combination.attribute_value_ids:
                    value_data = value_index.get(value_id)
                    if value_data is None:
                        unresolved_value_ids.add(value_id)
                        continue

                    group_name = str(value_data["group_name"])
                    all_groups.add(group_name)
                    if group_role(group_name) == "color":
                        color_groups.add(group_name)

            if len(color_groups) < min_color_groups:
                continue

            matches.append(
                {
                    "prestashop_product_id": product.product_id,
                    "reference": product.reference,
                    "name": product.name,
                    "combination_count": combination_count,
                    "color_group_count": len(color_groups),
                    "color_groups": sorted(color_groups),
                    "attribute_groups": sorted(all_groups),
                    "unresolved_value_ids": sorted(unresolved_value_ids),
                }
            )

        matches.sort(
            key=lambda item: (
                -int(item["combination_count"]),
                -int(item["color_group_count"]),
                int(item["prestashop_product_id"]),
            )
        )

        report = {
            "summary": {
                "product_count": len(matches),
                "min_combinations": min_combinations,
                "min_color_groups": min_color_groups,
            },
            "products": matches,
        }

        if output:
            output_path = Path(output)
            output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Wrote JSON report to {output_path}"))

        self.stdout.write(
            self.style.SUCCESS(
                "Matching products: "
                f"{report['summary']['product_count']} "
                f"(min_combinations>{min_combinations}, min_color_groups>={min_color_groups})"
            )
        )

        for item in matches:
            self.stdout.write(
                f"#{item['prestashop_product_id']} {item['reference']} | "
                f"combinations={item['combination_count']} | "
                f"color_groups={item['color_group_count']} | "
                f"{', '.join(item['color_groups'])}"
            )
