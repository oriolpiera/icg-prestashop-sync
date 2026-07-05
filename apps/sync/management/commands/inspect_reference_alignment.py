import json

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.models import Product
from apps.prestashop.client import PrestashopClient
from apps.sync.management.commands.repair_single_axis_reference import _build_value_index
from apps.sync.reconciliation import (
    find_candidate_django_combinations,
    resolve_prestashop_combination,
)


class Command(BaseCommand):
    help = (
        "Inspect one product reference across Django and PrestaShop, showing variant-level "
        "alignment, conflicts, missing variants, and unresolved cases."
    )

    def add_arguments(self, parser):
        parser.add_argument("reference", help="Product reference to inspect")
        parser.add_argument(
            "--output-json",
            help="Optional path to write the full inspection payload as JSON.",
        )

    def handle(self, *args, **options):
        reference = options["reference"].strip()
        output_json = options.get("output_json")
        if not reference:
            raise CommandError("reference is required")

        product = Product.objects.filter(reference=reference).select_related("manufacturer").first()
        if product is None:
            raise CommandError(f"No Django product found for reference {reference}")
        if product.prestashop_id is None:
            raise CommandError(f"Product {reference} has no prestashop_id in Django")

        client = PrestashopClient()
        value_index = _build_value_index(client)
        ps_combinations = client.list_combinations_for_product(product.prestashop_id)
        django_combinations = list(
            product.combinations.all().order_by("icg_size", "icg_color", "pk")
        )

        payload = {
            "reference": reference,
            "product": {
                "django_product_id": product.pk,
                "prestashop_product_id": product.prestashop_id,
                "name": product.name,
                "manufacturer": product.manufacturer.name if product.manufacturer else None,
                "visible_web": product.visible_web,
                "discontinued": product.discontinued,
            },
            "django_combinations": [],
            "prestashop_combinations": [],
            "summary": {
                "django_combination_count": len(django_combinations),
                "prestashop_combination_count": len(ps_combinations),
                "conflict_count": 0,
                "missing_count": 0,
                "matched_count": 0,
                "unresolved_count": 0,
            },
        }

        for combination in django_combinations:
            payload["django_combinations"].append(
                {
                    "django_combination_id": combination.pk,
                    "prestashop_id": combination.prestashop_id,
                    "icg_size": combination.icg_size,
                    "icg_color": combination.icg_color,
                    "active": combination.active,
                }
            )

        for ps_combination in ps_combinations:
            resolved = resolve_prestashop_combination(ps_combination, value_index)
            candidate_matches = find_candidate_django_combinations(
                product,
                resolved_size=resolved.resolved_size,
                resolved_color=resolved.resolved_color,
            )
            candidate_ids = [candidate.pk for candidate in candidate_matches]
            status = "matched"
            if resolved.unresolved_value_ids or (
                not resolved.resolved_size and not resolved.resolved_color
            ):
                status = "unresolved"
                payload["summary"]["unresolved_count"] += 1
            elif not candidate_matches:
                status = "missing"
                payload["summary"]["missing_count"] += 1
            elif len(candidate_matches) > 1:
                status = "conflict"
                payload["summary"]["conflict_count"] += 1
            else:
                candidate = candidate_matches[0]
                if candidate.prestashop_id not in {None, ps_combination.combination_id}:
                    status = "conflict"
                    payload["summary"]["conflict_count"] += 1
                else:
                    payload["summary"]["matched_count"] += 1

            payload["prestashop_combinations"].append(
                {
                    "prestashop_combination_id": ps_combination.combination_id,
                    "ean13": ps_combination.ean13,
                    "resolved_size": resolved.resolved_size,
                    "resolved_color": resolved.resolved_color,
                    "resolved_values": resolved.resolved_values,
                    "unresolved_value_ids": resolved.unresolved_value_ids,
                    "candidate_django_combination_ids": candidate_ids,
                    "status": status,
                }
            )

        if output_json:
            with open(output_json, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            self.stdout.write(self.style.SUCCESS(f"Wrote inspection report to {output_json}"))

        summary = payload["summary"]
        self.stdout.write(
            self.style.SUCCESS(
                f"Reference {reference}: django={summary['django_combination_count']} prestashop={summary['prestashop_combination_count']} matched={summary['matched_count']} missing={summary['missing_count']} conflict={summary['conflict_count']} unresolved={summary['unresolved_count']}"  # noqa: E501
            )
        )
        for item in payload["prestashop_combinations"]:
            if item["status"] == "matched":
                continue
            self.stdout.write(
                f"{item['status'].upper()} ps#{item['prestashop_combination_id']} "
                f"size={item['resolved_size']!r} color={item['resolved_color']!r} "
                f"candidates={item['candidate_django_combination_ids']}"
            )
