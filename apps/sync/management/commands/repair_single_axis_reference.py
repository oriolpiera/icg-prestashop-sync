from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalog.models import Combination, Product
from apps.catalog.variants import PLACEHOLDER_VARIANT_VALUES, is_placeholder_variant_axis
from apps.prestashop.client import PrestashopClient
from apps.sync.reconciliation import resolve_prestashop_combination


def _build_value_index(client: PrestashopClient) -> dict[int, dict[str, str | int]]:
    groups = client.list_attribute_groups()
    group_index = {
        int(group["ps_id"]): str(group["name"])
        for group in groups
        if isinstance(group.get("ps_id"), int)
    }

    value_index: dict[int, dict[str, str | int]] = {}
    for group_id, group_name in group_index.items():
        for value in client.list_attribute_values(group_id):
            value_id = value.get("ps_id")
            if not isinstance(value_id, int):
                continue
            value_index[value_id] = {
                "name": str(value.get("name") or ""),
                "group_name": group_name,
                "group_prestashop_id": group_id,
            }
    return value_index


class Command(BaseCommand):
    help = (
        "Repair one reference whose Django combinations should map to PrestaShop "
        "single-axis legacy combinations (for example color-only COPIC variants)."
    )

    def add_arguments(self, parser):
        parser.add_argument("reference", help="Product reference to repair")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist remapped prestashop_id values into Django.",
        )
        parser.add_argument(
            "--delete-obsolete",
            action="store_true",
            help="Delete superseded placeholder-created PrestaShop combinations after remapping.",
        )

    def handle(self, *args, **options):
        reference = options["reference"].strip()
        apply = options["apply"]
        delete_obsolete = options["delete_obsolete"]

        product = Product.objects.filter(reference=reference).first()
        if product is None:
            raise CommandError(f"No Django product found for reference {reference}")
        if product.prestashop_id is None:
            raise CommandError(f"Product {reference} has no prestashop_id in Django")

        client = PrestashopClient()
        value_index = _build_value_index(client)
        ps_combinations = client.list_combinations_for_product(product.prestashop_id)

        color_only_targets: dict[str, list[int]] = defaultdict(list)
        placeholder_candidates: set[int] = set()

        for ps_combination in ps_combinations:
            resolved = resolve_prestashop_combination(ps_combination, value_index)
            raw_names = [str(item["name"]).strip() for item in resolved.resolved_values]
            placeholder_names = [name for name in raw_names if name in PLACEHOLDER_VARIANT_VALUES]
            real_names = [name for name in raw_names if name not in PLACEHOLDER_VARIANT_VALUES]

            if (
                resolved.resolved_color
                and not resolved.resolved_size
                and len(real_names) == 1
                and not placeholder_names
            ):
                color_only_targets[resolved.resolved_color].append(ps_combination.combination_id)

            if placeholder_names and resolved.resolved_color:
                placeholder_candidates.add(ps_combination.combination_id)

        django_combinations = list(product.combinations.all().order_by("pk"))
        remaps: list[tuple[Combination, int, int | None]] = []
        ambiguous = 0
        missing = 0

        for combination in django_combinations:
            if not is_placeholder_variant_axis(combination.icg_size):
                continue
            if is_placeholder_variant_axis(combination.icg_color):
                continue

            target_ids = color_only_targets.get(combination.icg_color, [])
            if len(target_ids) == 1:
                remaps.append((combination, target_ids[0], combination.prestashop_id))
            elif len(target_ids) == 0:
                missing += 1
            else:
                ambiguous += 1

        remap_by_source_pk = {
            combination.pk: (combination, new_ps_id, old_ps_id)
            for combination, new_ps_id, old_ps_id in remaps
        }
        target_counts: dict[int, int] = defaultdict(int)
        for _combination, new_ps_id, _old_ps_id in remaps:
            target_counts[new_ps_id] += 1

        holders_by_ps_id = {
            combination.prestashop_id: combination
            for combination in django_combinations
            if combination.prestashop_id is not None
        }

        applicable_remaps: list[tuple[Combination, int, int | None]] = []
        target_in_use_conflicts = 0
        duplicate_target_conflicts = 0
        for combination, new_ps_id, old_ps_id in remaps:
            if target_counts[new_ps_id] > 1:
                duplicate_target_conflicts += 1
                continue

            holder = holders_by_ps_id.get(new_ps_id)
            if holder is None or holder.pk == combination.pk:
                applicable_remaps.append((combination, new_ps_id, old_ps_id))
                continue

            if holder.pk in remap_by_source_pk:
                applicable_remaps.append((combination, new_ps_id, old_ps_id))
                continue

            target_in_use_conflicts += 1

        obsolete_ids_to_delete = {
            old_ps_id
            for _, new_ps_id, old_ps_id in applicable_remaps
            if old_ps_id and old_ps_id != new_ps_id and old_ps_id in placeholder_candidates
        }

        if apply:
            with transaction.atomic():
                combos_to_clear = [
                    combination.pk
                    for combination, new_ps_id, old_ps_id in applicable_remaps
                    if old_ps_id is not None and old_ps_id != new_ps_id
                ]
                if combos_to_clear:
                    Combination.objects.filter(pk__in=combos_to_clear).update(prestashop_id=None)

                for combination, new_ps_id, _old_ps_id in applicable_remaps:
                    combination.prestashop_id = new_ps_id
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

            if delete_obsolete:
                still_referenced_ids = set(
                    Combination.objects.filter(
                        product=product, prestashop_id__isnull=False
                    ).values_list("prestashop_id", flat=True)
                )
                for obsolete_id in sorted(obsolete_ids_to_delete - still_referenced_ids):
                    client.delete_combination(obsolete_id)

        mode = "APPLIED" if apply else "DRY RUN"
        deleted = 0
        if apply and delete_obsolete:
            still_referenced_ids = set(
                Combination.objects.filter(
                    product=product, prestashop_id__isnull=False
                ).values_list("prestashop_id", flat=True)
            )
            deleted = len(obsolete_ids_to_delete - still_referenced_ids)

        self.stdout.write(
            self.style.SUCCESS(
                f"[{mode}] Repair {reference}: remaps={len(applicable_remaps)} missing_targets={missing} ambiguous_targets={ambiguous} duplicate_target_conflicts={duplicate_target_conflicts} target_in_use_conflicts={target_in_use_conflicts}"  # noqa:E501
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Placeholder candidates={len(placeholder_candidates)} obsolete_delete_candidates={len(obsolete_ids_to_delete)} deleted={deleted}"  # noqa:E501
            )
        )
