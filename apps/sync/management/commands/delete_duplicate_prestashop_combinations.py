from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.models import Product
from apps.prestashop.client import PrestashopClient
from apps.sync.management.commands.repair_single_axis_reference import _build_value_index
from apps.sync.reconciliation import resolve_prestashop_combination


def _variant_key(size: str, color: str) -> str:
    return f"{size}|{color}"


class Command(BaseCommand):
    help = (
        "Delete duplicate PrestaShop combinations for one reference, preserving the "
        "Prestashop IDs already mapped in Django whenever possible."
    )

    def add_arguments(self, parser):
        parser.add_argument("reference", help="Product reference to clean")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the duplicate Prestashop combinations.",
        )

    def handle(self, *args, **options):
        reference = options["reference"].strip()
        apply = options["apply"]
        if not reference:
            raise CommandError("reference is required")

        product = Product.objects.filter(reference=reference).first()
        if product is None:
            raise CommandError(f"No Django product found for reference {reference}")
        if product.prestashop_id is None:
            raise CommandError(f"Product {reference} has no prestashop_id in Django")

        client = PrestashopClient()
        value_index = _build_value_index(client)
        ps_combinations = client.list_combinations_for_product(product.prestashop_id)
        django_ps_ids = set(
            product.combinations.filter(prestashop_id__isnull=False).values_list(
                "prestashop_id", flat=True
            )
        )

        by_variant: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
        for ps_combination in ps_combinations:
            resolved = resolve_prestashop_combination(ps_combination, value_index)
            by_variant[_variant_key(resolved.resolved_size, resolved.resolved_color)].append(
                (
                    ps_combination.combination_id,
                    resolved.resolved_size,
                    resolved.resolved_color,
                )
            )

        delete_ids: list[int] = []
        duplicate_groups = 0
        preserved_ids: list[int] = []

        for _variant, entries in by_variant.items():
            if len(entries) < 2:
                continue
            duplicate_groups += 1

            mapped_entries = [entry for entry in entries if entry[0] in django_ps_ids]
            if len(mapped_entries) > 1:
                self.stdout.write(
                    self.style.WARNING(
                        "Skipping variant "
                        f"{entries[0][1]!r}/{entries[0][2]!r}: multiple Django-mapped "
                        f"Prestashop IDs {sorted(entry[0] for entry in mapped_entries)}"
                    )
                )
                continue

            if len(mapped_entries) == 1:
                keep_id = mapped_entries[0][0]
            else:
                keep_id = min(entry[0] for entry in entries)

            preserved_ids.append(keep_id)
            for combination_id, _size, _color in entries:
                if combination_id != keep_id:
                    delete_ids.append(combination_id)

        if apply:
            for combination_id in sorted(delete_ids):
                client.delete_combination(combination_id)

        mode = "APPLIED" if apply else "DRY RUN"
        self.stdout.write(
            self.style.SUCCESS(
                f"[{mode}] Duplicate cleanup {reference}: duplicate_groups={duplicate_groups} "
                f"keep={len(set(preserved_ids))} delete={len(delete_ids)}"
            )
        )
