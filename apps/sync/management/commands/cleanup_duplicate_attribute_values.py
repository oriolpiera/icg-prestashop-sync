from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.models import AttributeGroup, AttributeValue
from apps.prestashop.client import PrestashopClient


class Command(BaseCommand):
    help = (
        "Detect and remove duplicate attribute values on the PrestaShop side. "
        "Values with the same default-language name in the same group are "
        "duplicates. Only the one matching Django (or the oldest) is kept."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete duplicates from PrestaShop. Default is dry-run only.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Acknowledge that deleted PS IDs may be referenced by live "
                "product combinations. Required together with --apply."
            ),
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        force = options["force"]

        if apply and not force:
            raise CommandError(
                "Use --force to confirm you understand: deleted PS IDs may still "
                "be referenced by product combinations on the PrestaShop side. "
                "Run with --force --apply to proceed, or run without --apply for a dry run."
            )

        client = PrestashopClient()

        size_group = AttributeGroup.objects.filter(icg_type="size", product__isnull=True).first()
        if size_group is None:
            self.stdout.write(self.style.WARNING("No global size attribute group found in Django."))
            return

        group_ps_id = size_group.prestashop_id
        self.stdout.write(f"Checking attribute group '{size_group.name}' (PS ID {group_ps_id})...")

        values = client.list_attribute_values(group_ps_id)

        by_name: dict[str, list[dict]] = defaultdict(list)
        for v in values:
            name = str(v.get("name", ""))
            if not name:
                continue
            by_name[name].append({"ps_id": v["ps_id"]})

        total_duplicates = 0
        total_deleted = 0

        for name, entries in sorted(by_name.items()):
            if len(entries) < 2:
                continue

            ps_ids = [e["ps_id"] for e in entries]

            django_av = AttributeValue.objects.filter(
                attribute_group=size_group, icg_value=name
            ).first()

            if django_av and django_av.prestashop_id in ps_ids:
                keep_id = django_av.prestashop_id
            else:
                keep_id = min(ps_ids)

            to_delete = [pid for pid in ps_ids if pid != keep_id]

            if apply:
                still_in_use = (
                    AttributeValue.objects.filter(prestashop_id__in=to_delete)
                    .exclude(pk=django_av.pk if django_av else None)
                    .count()
                )
                if still_in_use > 0:
                    self.stdout.write(
                        self.style.WARNING(
                            f"    Skipping '{name}' — {still_in_use} Django AttributeValue "
                            f"record(s) point to a to-be-deleted PS ID. "
                            "This should not happen; investigate manually."
                        )
                    )
                    continue

            total_duplicates += len(entries) - 1

            self.stdout.write(f"  '{name}': PS IDs {ps_ids} → keep {keep_id}, delete {to_delete}")

            if not apply:
                self.stdout.write(
                    self.style.WARNING(
                        "    WARNING: these PS IDs may be referenced by product "
                        "combinations. Use --force --apply to confirm deletion."
                    )
                )

            if django_av and django_av.prestashop_id not in ps_ids:
                self.stdout.write(
                    self.style.WARNING(
                        f"    Django points to PS ID {django_av.prestashop_id} "
                        f"which no longer exists in PrestaShop. Updating to {keep_id}."
                    )
                )
                if apply:
                    django_av.prestashop_id = keep_id
                    django_av.save(update_fields=["prestashop_id", "updated_at"])

            if not django_av:
                self.stdout.write(
                    f"    No Django record for '{name}' — will keep oldest PS ID {keep_id}."
                )

            if apply:
                for pid in to_delete:
                    try:
                        client.delete_attribute_value(pid)
                        self.stdout.write(f"    Deleted PS ID {pid}")
                        total_deleted += 1
                    except Exception as exc:
                        self.stdout.write(
                            self.style.ERROR(f"    Failed to delete PS ID {pid}: {exc}")
                        )
            else:
                total_deleted += len(to_delete)

        if total_duplicates == 0:
            self.stdout.write(self.style.SUCCESS("No duplicate attribute values found."))
            return

        mode = "DRY RUN" if not apply else "APPLIED"
        if not apply:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{mode}] Found {total_duplicates} duplicate(s), "
                    f"{total_deleted} would be deleted."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{mode}] Found {total_duplicates} duplicate(s), " f"deleted {total_deleted}."
                )
            )
