from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.catalog.models import AttributeGroup, AttributeValue
from apps.prestashop.client import PrestashopClient


def _expected_group_name(group: AttributeGroup) -> str:
    if group.icg_type == "color":
        return f"{group.product.reference}_color"
    return "Size"


def _reserve_temporary_ids(model, desired_ids: dict[int, int]) -> None:
    if not desired_ids:
        return

    max_current = (
        model.objects.order_by("-prestashop_id").values_list("prestashop_id", flat=True).first()
    )
    max_target = max(desired_ids.values(), default=0)
    temp_id = max(max_current or 0, max_target) + 1

    for pk in desired_ids:
        model.objects.filter(pk=pk).update(prestashop_id=temp_id)
        temp_id += 1


def _apply_prestashop_id_remap(model, desired_ids: dict[int, int]) -> None:
    if not desired_ids:
        return

    target_counts = Counter(desired_ids.values())
    duplicates = [target_id for target_id, count in target_counts.items() if count > 1]
    if duplicates:
        raise ValueError(f"Duplicate target Prestashop IDs requested: {sorted(duplicates)}")

    with transaction.atomic():
        _reserve_temporary_ids(model, desired_ids)
        for pk, target_id in desired_ids.items():
            model.objects.filter(pk=pk).update(prestashop_id=target_id)


class Command(BaseCommand):
    help = (
        "Reconcile local AttributeGroup/AttributeValue Prestashop IDs by matching "
        "their expected names against the current Prestashop catalog. Default mode "
        "is dry-run; use --apply to persist fixes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the reconciled Prestashop IDs into Django.",
        )
        parser.add_argument(
            "--prune-missing-local",
            action="store_true",
            help=(
                "Delete local AttributeGroup/AttributeValue rows whose expected Prestashop "
                "group or value no longer exists remotely."
            ),
        )

    def handle(self, *args, **options):
        client = PrestashopClient()
        apply = options["apply"]
        prune_missing_local = options["prune_missing_local"]

        remote_groups = client.list_attribute_groups()
        remote_group_ids_by_name = {
            str(group["name"]): int(group["ps_id"])
            for group in remote_groups
            if isinstance(group.get("ps_id"), int)
        }
        groups = list(AttributeGroup.objects.select_related("product").all())
        needed_group_names = {_expected_group_name(group) for group in groups}
        remote_values_by_group_and_name: dict[tuple[int, str], int] = {}
        for group_name in needed_group_names:
            group_ps_id = remote_group_ids_by_name.get(group_name)
            if group_ps_id is None:
                continue
            for value in client.list_attribute_values(group_ps_id):
                value_ps_id = value.get("ps_id")
                if not isinstance(value_ps_id, int):
                    continue
                remote_values_by_group_and_name[(group_ps_id, str(value.get("name") or ""))] = (
                    value_ps_id
                )

        group_updates: dict[int, int] = {}
        missing_group_pks: list[int] = []
        missing_groups: list[str] = []
        group_conflicts: list[str] = []

        for group in groups:
            expected_name = _expected_group_name(group)
            target_id = remote_group_ids_by_name.get(expected_name)
            if target_id is None:
                missing_group_pks.append(group.pk)
                missing_groups.append(expected_name)
                continue
            if target_id != group.prestashop_id:
                group_updates[group.pk] = target_id

        target_to_group_pk = {target_id: pk for pk, target_id in group_updates.items()}
        for group in groups:
            target_id = group_updates.get(group.pk, group.prestashop_id)
            owner_pk = target_to_group_pk.get(target_id)
            if owner_pk is not None and owner_pk != group.pk:
                continue
            occupied = next(
                (candidate for candidate in groups if candidate.prestashop_id == target_id), None
            )
            if (
                occupied is not None
                and occupied.pk != group.pk
                and occupied.pk not in group_updates
            ):
                group_conflicts.append(
                    f"Group '{_expected_group_name(group)}' wants PS #{target_id}, "
                    f"but local group pk={occupied.pk} is fixed there."
                )

        value_updates: dict[int, int] = {}
        missing_value_pks: list[int] = []
        missing_values: list[str] = []
        value_conflicts: list[str] = []

        values = list(
            AttributeValue.objects.select_related("attribute_group", "attribute_group__product")
        )
        for value in values:
            target_group_id = group_updates.get(
                value.attribute_group_id, value.attribute_group.prestashop_id
            )
            target_id = remote_values_by_group_and_name.get((target_group_id, value.icg_value))
            if target_id is None:
                missing_value_pks.append(value.pk)
                missing_values.append(
                    f"{_expected_group_name(value.attribute_group)}::{value.icg_value}"
                )
                continue
            if target_id != value.prestashop_id:
                value_updates[value.pk] = target_id

        target_to_value_pk = {target_id: pk for pk, target_id in value_updates.items()}
        for value in values:
            target_id = value_updates.get(value.pk, value.prestashop_id)
            owner_pk = target_to_value_pk.get(target_id)
            if owner_pk is not None and owner_pk != value.pk:
                continue
            occupied = next(
                (candidate for candidate in values if candidate.prestashop_id == target_id), None
            )
            if (
                occupied is not None
                and occupied.pk != value.pk
                and occupied.pk not in value_updates
            ):
                value_conflicts.append(
                    "Attribute value "
                    f"'{_expected_group_name(value.attribute_group)}::"
                    f"{value.icg_value}' wants PS #{target_id}, "
                    f"but local value pk={occupied.pk} is fixed there."
                )

        mode = "APPLIED" if apply else "DRY RUN"

        self.stdout.write(
            self.style.SUCCESS(
                f"[{mode}] Planned attribute mapping fixes: "
                f"groups={len(group_updates)} values={len(value_updates)}"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Missing remote matches: groups={len(missing_groups)} values={len(missing_values)}"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Blocking conflicts: groups={len(group_conflicts)} values={len(value_conflicts)}"
            )
        )

        for message in group_conflicts[:20]:
            self.stdout.write(self.style.WARNING(message))
        for message in value_conflicts[:20]:
            self.stdout.write(self.style.WARNING(message))
        for message in missing_groups[:20]:
            self.stdout.write(self.style.WARNING(f"Missing remote group: {message}"))
        for message in missing_values[:20]:
            self.stdout.write(self.style.WARNING(f"Missing remote value: {message}"))

        if not apply:
            return

        if group_conflicts or value_conflicts:
            self.stdout.write(
                self.style.ERROR("Aborting apply because blocking mapping conflicts remain.")
            )
            return

        _apply_prestashop_id_remap(AttributeGroup, group_updates)
        _apply_prestashop_id_remap(AttributeValue, value_updates)

        pruned_groups = 0
        pruned_values = 0
        if prune_missing_local:
            missing_value_qs = AttributeValue.objects.filter(pk__in=missing_value_pks)
            missing_group_qs = AttributeGroup.objects.filter(pk__in=missing_group_pks)
            pruned_values = missing_value_qs.count()
            pruned_groups = missing_group_qs.count()
            missing_value_qs.delete()
            missing_group_qs.delete()

        self.stdout.write(
            self.style.SUCCESS(
                "Attribute mapping reconciliation applied. "
                f"pruned_groups={pruned_groups} pruned_values={pruned_values}"
            )
        )
