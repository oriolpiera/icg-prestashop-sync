import json
from collections import defaultdict

from django.core.management.base import BaseCommand

from apps.catalog.models import Combination, Product
from apps.prestashop.client import PrestashopClient


def _group_role(group_name: str) -> str:
    lower = group_name.strip().lower()
    if lower in {"size", "talla"} or lower.endswith(("_size", "_talla")):
        return "size"
    if "color" in lower:
        return "color"
    return "unknown"


def _group_scope(group_name: str, product_count: int, manufacturer_count: int) -> str:
    role = _group_role(group_name)
    lower = group_name.strip().lower()

    if role == "size" and lower in {"size", "talla"}:
        return "global"
    if product_count <= 1:
        return "product-scoped"
    if manufacturer_count == 1 and lower.endswith(("_size", "_talla", "_color")):
        return "manufacturer-scoped"
    return "legacy/anomalous"


class Command(BaseCommand):
    help = (
        "Read-only Prestashop catalog inventory for phase-1 reconciliation. "
        "Builds a report of products, combinations, attribute groups/values, and discounts."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit-products",
            type=int,
            default=0,
            help="Optional maximum number of Prestashop products to inspect (0 = all).",
        )
        parser.add_argument(
            "--output",
            help="Optional path to write the full machine-readable JSON report.",
        )

    def handle(self, *args, **options):
        client = PrestashopClient()
        limit_products = options["limit_products"]
        output_path = options.get("output")

        self.stdout.write("Building read-only Prestashop inventory for reconciliation...")

        groups = client.list_attribute_groups()
        group_index = {
            int(group["ps_id"]): {
                "prestashop_id": int(group["ps_id"]),
                "name": str(group["name"]),
            }
            for group in groups
            if isinstance(group.get("ps_id"), int)
        }

        value_index: dict[int, dict] = {}
        for group_id, group_data in group_index.items():
            values = client.list_attribute_values(group_id)
            for value in values:
                value_id = value.get("ps_id")
                if not isinstance(value_id, int):
                    continue
                value_index[value_id] = {
                    "prestashop_id": value_id,
                    "name": str(value.get("name") or ""),
                    "group_prestashop_id": group_id,
                    "group_name": group_data["name"],
                }

        django_products = defaultdict(list)
        for product in Product.objects.select_related("manufacturer").all():
            django_products[product.reference].append(product)

        report = {
            "summary": {
                "prestashop_products": 0,
                "prestashop_combinations": 0,
                "attribute_groups": len(group_index),
                "attribute_values": len(value_index),
                "product_matches_safe": 0,
                "product_matches_missing": 0,
                "product_matches_ambiguous": 0,
                "combination_matches_safe": 0,
                "combination_matches_missing": 0,
                "combination_matches_ambiguous": 0,
                "combination_matches_unresolved": 0,
                "products_with_product_level_specific_prices": 0,
            },
            "products": [],
            "attribute_groups": [],
            "ambiguous_products": [],
            "ambiguous_combinations": [],
            "unresolved_combinations": [],
        }

        group_usage: dict[int, dict] = {
            group_id: {
                "product_ids": set(),
                "product_references": set(),
                "manufacturers": set(),
                "roles": set(),
                "combination_ids": set(),
            }
            for group_id in group_index
        }

        prestashop_products = client.list_products(limit=limit_products)
        report["summary"]["prestashop_products"] = len(prestashop_products)

        for ps_product in prestashop_products:
            matched_products = django_products.get(ps_product.reference, [])
            matched_ids = [product.pk for product in matched_products]

            if len(matched_products) == 1:
                product_match = "safe"
                report["summary"]["product_matches_safe"] += 1
            elif len(matched_products) == 0:
                product_match = "missing"
                report["summary"]["product_matches_missing"] += 1
            else:
                product_match = "ambiguous"
                report["summary"]["product_matches_ambiguous"] += 1
                report["ambiguous_products"].append(
                    {
                        "reference": ps_product.reference,
                        "prestashop_product_id": ps_product.product_id,
                        "django_product_ids": matched_ids,
                    }
                )

            specific_prices = client.list_specific_prices_by_product(ps_product.product_id)
            if specific_prices:
                report["summary"]["products_with_product_level_specific_prices"] += 1

            product_entry = {
                "prestashop_product_id": ps_product.product_id,
                "reference": ps_product.reference,
                "name": ps_product.name,
                "product_match": product_match,
                "django_product_ids": matched_ids,
                "specific_prices": [
                    {
                        "prestashop_specific_price_id": sp.specific_price_id,
                        "reduction": str(sp.reduction),
                        "reduction_type": sp.reduction_type,
                    }
                    for sp in specific_prices
                ],
                "combinations": [],
            }

            ps_combinations = client.list_combinations_for_product(ps_product.product_id)
            report["summary"]["prestashop_combinations"] += len(ps_combinations)

            for ps_combination in ps_combinations:
                resolved_values = []
                resolved_size = ""
                resolved_color = ""
                unresolved_value_ids = []

                for value_id in ps_combination.attribute_value_ids:
                    value_data = value_index.get(value_id)
                    if value_data is None:
                        unresolved_value_ids.append(value_id)
                        continue

                    role = _group_role(value_data["group_name"])
                    resolved_values.append(
                        {
                            "prestashop_value_id": value_id,
                            "name": value_data["name"],
                            "group_prestashop_id": value_data["group_prestashop_id"],
                            "group_name": value_data["group_name"],
                            "role": role,
                        }
                    )

                    usage = group_usage[value_data["group_prestashop_id"]]
                    usage["product_ids"].add(ps_product.product_id)
                    usage["product_references"].add(ps_product.reference)
                    usage["roles"].add(role)
                    usage["combination_ids"].add(ps_combination.combination_id)

                    if len(matched_products) == 1 and matched_products[0].manufacturer:
                        usage["manufacturers"].add(matched_products[0].manufacturer.name)

                    if role == "size" and not resolved_size:
                        resolved_size = value_data["name"]
                    elif role == "color" and not resolved_color:
                        resolved_color = value_data["name"]

                if unresolved_value_ids or not resolved_size or not resolved_color:
                    combination_match = "unresolved"
                    report["summary"]["combination_matches_unresolved"] += 1
                    report["unresolved_combinations"].append(
                        {
                            "prestashop_product_id": ps_product.product_id,
                            "prestashop_combination_id": ps_combination.combination_id,
                            "reference": ps_product.reference,
                            "resolved_size": resolved_size,
                            "resolved_color": resolved_color,
                            "unresolved_value_ids": unresolved_value_ids,
                            "resolved_values": resolved_values,
                        }
                    )
                    matched_combination_ids = []
                elif len(matched_products) != 1:
                    combination_match = "ambiguous"
                    report["summary"]["combination_matches_ambiguous"] += 1
                    matched_combination_ids = []
                    report["ambiguous_combinations"].append(
                        {
                            "prestashop_product_id": ps_product.product_id,
                            "prestashop_combination_id": ps_combination.combination_id,
                            "reference": ps_product.reference,
                            "reason": "product_match_not_unique",
                            "resolved_size": resolved_size,
                            "resolved_color": resolved_color,
                        }
                    )
                else:
                    django_matches = list(
                        Combination.objects.filter(
                            product=matched_products[0],
                            icg_size=resolved_size,
                            icg_color=resolved_color,
                        )
                    )
                    matched_combination_ids = [combination.pk for combination in django_matches]
                    if len(django_matches) == 1:
                        combination_match = "safe"
                        report["summary"]["combination_matches_safe"] += 1
                    elif len(django_matches) == 0:
                        combination_match = "missing"
                        report["summary"]["combination_matches_missing"] += 1
                    else:
                        combination_match = "ambiguous"
                        report["summary"]["combination_matches_ambiguous"] += 1
                        report["ambiguous_combinations"].append(
                            {
                                "prestashop_product_id": ps_product.product_id,
                                "prestashop_combination_id": ps_combination.combination_id,
                                "reference": ps_product.reference,
                                "reason": "multiple_django_combinations",
                                "resolved_size": resolved_size,
                                "resolved_color": resolved_color,
                                "django_combination_ids": matched_combination_ids,
                            }
                        )

                product_entry["combinations"].append(
                    {
                        "prestashop_combination_id": ps_combination.combination_id,
                        "ean13": ps_combination.ean13,
                        "resolved_size": resolved_size,
                        "resolved_color": resolved_color,
                        "resolved_values": resolved_values,
                        "unresolved_value_ids": unresolved_value_ids,
                        "combination_match": combination_match,
                        "django_combination_ids": matched_combination_ids,
                    }
                )

            report["products"].append(product_entry)

        for group_id, usage in group_usage.items():
            group_data = group_index[group_id]
            report["attribute_groups"].append(
                {
                    "prestashop_id": group_id,
                    "name": group_data["name"],
                    "role": (
                        next(iter(usage["roles"])) if len(usage["roles"]) == 1 else "mixed/unknown"
                    ),
                    "scope": _group_scope(
                        group_data["name"],
                        len(usage["product_ids"]),
                        len(usage["manufacturers"]),
                    ),
                    "product_count": len(usage["product_ids"]),
                    "products": sorted(usage["product_references"]),
                    "manufacturers": sorted(usage["manufacturers"]),
                    "combination_count": len(usage["combination_ids"]),
                }
            )

        report["attribute_groups"].sort(key=lambda item: (item["scope"], item["name"]))

        if output_path:
            with open(output_path, "w", encoding="utf-8") as output_file:
                json.dump(report, output_file, indent=2, sort_keys=True)
            self.stdout.write(self.style.SUCCESS(f"Wrote JSON report to {output_path}"))

        summary = report["summary"]
        self.stdout.write(
            self.style.SUCCESS(
                "Inventory complete: "
                f"products={summary['prestashop_products']} "
                f"combinations={summary['prestashop_combinations']} "
                f"groups={summary['attribute_groups']} "
                f"values={summary['attribute_values']}"
            )
        )
        self.stdout.write(
            "Product matches: "
            f"safe={summary['product_matches_safe']} "
            f"missing={summary['product_matches_missing']} "
            f"ambiguous={summary['product_matches_ambiguous']}"
        )
        self.stdout.write(
            "Combination matches: "
            f"safe={summary['combination_matches_safe']} "
            f"missing={summary['combination_matches_missing']} "
            f"ambiguous={summary['combination_matches_ambiguous']} "
            f"unresolved={summary['combination_matches_unresolved']}"
        )
        self.stdout.write(
            "Product-level discounts detected: "
            f"{summary['products_with_product_level_specific_prices']}"
        )
