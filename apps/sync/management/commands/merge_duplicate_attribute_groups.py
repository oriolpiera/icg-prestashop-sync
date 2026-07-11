"""merge_duplicate_attribute_groups -- merge PrestaShop attribute groups that share the same name.

Attribute groups with the same name (e.g. ``0931027_color``) sometimes accumulate when
products are re-synced.  Each group holds a subset of the values that should belong to
a single group.  This command:

1. Detects remote (PrestaShop-side) groups whose default-language name is duplicated.
2. Resolves a *canonical* group per name (prefers Django-known, then more values,
   then lower PS ID).
3. Moves every attribute value from the orphan group(s) into the canonical group.
4. Updates any Django ``AttributeValue`` records that pointed to orphan PS IDs.
5. Deletes the now-empty orphan groups from PrestaShop.

``--dry-run`` (the default) prints what *would* happen without making changes.
``--apply`` executes the merge and deletion.

Combinations are **not** broken because they reference attribute *value* PS IDs,
not group PS IDs — moving a value preserves its ID.
"""

import logging
from collections import defaultdict

from django.core.management.base import BaseCommand

from apps.catalog.models import AttributeGroup, AttributeValue
from apps.prestashop.client import PrestashopClient, PrestashopError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Execute the merge. Default is dry-run only.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        client = PrestashopClient()

        self.stdout.write("Listing remote attribute groups from PrestaShop...")
        all_groups = client.list_attribute_groups()

        by_name: dict[str, list[dict]] = defaultdict(list)
        for g in all_groups:
            name = str(g.get("name", ""))
            if name:
                by_name[name].append(g)

        duplicate_names = {n: gs for n, gs in by_name.items() if len(gs) > 1}

        if not duplicate_names:
            self.stdout.write(self.style.SUCCESS("No duplicate attribute groups found."))
            return

        self.stdout.write(f"Found {len(duplicate_names)} duplicate group name(s):")
        for name in sorted(duplicate_names):
            ps_ids = sorted(g["ps_id"] for g in duplicate_names[name])
            self.stdout.write(f"  {name}: PS IDs {ps_ids}")

        total_merged = 0
        total_deleted_groups = 0

        for name, groups in sorted(duplicate_names.items()):
            self.stdout.write(f"\n{self.style.NOTICE('---- ' + name + ' ----')}")

            ps_ids = [g["ps_id"] for g in groups]

            django_ags = AttributeGroup.objects.filter(prestashop_id__in=ps_ids)
            django_ps_ids = set(django_ags.values_list("prestashop_id", flat=True))

            # ---- resolve canonical group ----
            canonical_ps_id: int | None = None
            reason = ""

            # 1. Prefer the group Django knows
            if django_ps_ids:
                django_ag = AttributeGroup.objects.filter(
                    prestashop_id__in=list(django_ps_ids)
                ).first()
                if django_ag is not None:
                    canonical_ps_id = django_ag.prestashop_id
                    reason = "Django tracked"

            # 2. Otherwise: group with more values → lower PS ID tie-break
            if canonical_ps_id is None:
                value_counts: list[tuple[int, int]] = []
                for g in groups:
                    try:
                        count = len(client.list_attribute_values(g["ps_id"]))
                        value_counts.append((g["ps_id"], count))
                    except PrestashopError as exc:
                        self.stdout.write(
                            self.style.WARNING(
                                f"  Cannot list values for PS {g['ps_id']}: {exc} — skipping."
                            )
                        )
                if value_counts:
                    value_counts.sort(key=lambda x: (-x[1], x[0]))
                    canonical_ps_id = value_counts[0][0]
                    reason = "most values"

            orphan_ps_ids = [pid for pid in ps_ids if pid != canonical_ps_id]

            self.stdout.write(f"  Canonical: PS {canonical_ps_id} ({reason})")
            self.stdout.write(f"  Orphans  : {orphan_ps_ids}")

            # ---- move values from orphans into canonical ----
            for orphan_id in orphan_ps_ids:
                try:
                    orphan_values = client.list_attribute_values(orphan_id)
                except PrestashopError as exc:
                    self.stdout.write(
                        self.style.ERROR(f"  Cannot list values for orphan PS {orphan_id}: {exc}")
                    )
                    continue
                self.stdout.write(f"  Moving {len(orphan_values)} value(s) from PS {orphan_id}:")

                try:
                    canonical_values = client.list_attribute_values(canonical_ps_id)
                except PrestashopError as exc:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  Cannot list canonical values for PS {canonical_ps_id}: {exc}"
                        )
                    )
                    continue
                canonical_names = {str(v.get("name", "")) for v in canonical_values}

                for ov in orphan_values:
                    value_name = str(ov.get("name", ""))
                    value_ps_id = int(ov["ps_id"])

                    if value_name in canonical_names:
                        # Duplicate value — the canonical already has it.
                        # Delete the orphan copy.
                        self.stdout.write(
                            self.style.WARNING(
                                f"    Value '{value_name}' (PS {value_ps_id}): "
                                f"already exists in canonical group — will delete."
                            )
                        )
                        if apply:
                            try:
                                client.delete_attribute_value(value_ps_id)
                            except PrestashopError as exc:
                                self.stdout.write(
                                    self.style.ERROR(
                                        f"    Failed to delete PS {value_ps_id}: {exc}"
                                    )
                                )
                                continue
                            continue
                    else:
                        self.stdout.write(
                            f"    Value '{value_name}' (PS {value_ps_id}): "
                            f"move to PS {canonical_ps_id}"
                        )
                        if apply:
                            try:
                                client.move_attribute_value_to_group(value_ps_id, canonical_ps_id)
                                total_merged += 1
                                canonical_names.add(value_name)
                            except PrestashopError as exc:
                                self.stdout.write(
                                    self.style.ERROR(f"    Failed to move PS {value_ps_id}: {exc}")
                                )
                                continue

                    # Update Django AttributeValue if it pointed to this orphan value
                    if apply:
                        django_av = AttributeValue.objects.filter(prestashop_id=value_ps_id).first()
                        if django_av is not None:
                            canonical_ag = AttributeGroup.objects.filter(
                                prestashop_id=canonical_ps_id
                            ).first()
                            if canonical_ag is not None:
                                django_av.attribute_group = canonical_ag
                                django_av.save(update_fields=["attribute_group", "updated_at"])

                # Delete the now-empty orphan group
                if apply:
                    try:
                        remaining = client.list_attribute_values(orphan_id)
                        if not remaining:
                            client.delete_attribute_group(orphan_id)
                            self.stdout.write(f"  Deleted orphan group PS {orphan_id}")
                            total_deleted_groups += 1
                        else:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"  Orphan group PS {orphan_id} still has "
                                    f"{len(remaining)} value(s) — skipping deletion."
                                )
                            )
                    except PrestashopError as exc:
                        self.stdout.write(
                            self.style.ERROR(f"  Failed to delete group PS {orphan_id}: {exc}")
                        )

        if apply:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[APPLIED] Merged {total_merged} value(s), "
                    f"deleted {total_deleted_groups} group(s)."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS("[DRY RUN] Re-run with --apply to execute the merge.")
            )
