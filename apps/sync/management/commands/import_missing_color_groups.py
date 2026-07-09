from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction

from apps.catalog.models import AttributeGroup, Product
from apps.prestashop.client import PrestashopClient


class Command(BaseCommand):
    help = (
        "Import missing color attribute groups from Prestashop for products "
        "that already have a prestashop_id. Creates or updates local "
        "AttributeGroup records for remote groups named {prestashop_id}_color "
        "that do not yet exist in Django with the preferred name."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Create or update the AttributeGroup records. Without this flag, dry-run.",
        )

    def handle(self, *args, **options):
        client = PrestashopClient()
        apply = options["apply"]

        remote_groups = client.list_attribute_groups()
        remote_by_name = {
            str(g["name"]): g["ps_id"] for g in remote_groups if isinstance(g.get("ps_id"), int)
        }

        products = Product.objects.filter(prestashop_id__isnull=False)

        created = 0
        updated = 0
        already_ok = 0
        not_found_remote = 0
        conflicts = 0

        for product in products.iterator():
            expected_name = f"{product.prestashop_id}_color"
            existing = AttributeGroup.objects.filter(icg_type="color", product=product).first()

            if existing is not None:
                if existing.name == expected_name:
                    already_ok += 1
                    continue
                remote_ps_id = remote_by_name.get(expected_name)
                if remote_ps_id is None:
                    not_found_remote += 1
                    continue
                if apply:
                    try:
                        with transaction.atomic():
                            existing.prestashop_id = remote_ps_id
                            existing.name = expected_name
                            existing.save(update_fields=["prestashop_id", "name", "updated_at"])
                    except IntegrityError:
                        conflicts += 1
                        self.stderr.write(
                            self.style.WARNING(
                                f"Conflict: cannot remap "
                                f"{product.reference} (pk={existing.pk}) to "
                                f"PS #{remote_ps_id} — unique constraint violation"
                            )
                        )
                        continue
                updated += 1
                continue

            remote_ps_id = remote_by_name.get(expected_name)
            if remote_ps_id is None:
                not_found_remote += 1
                continue

            if apply:
                try:
                    with transaction.atomic():
                        AttributeGroup.objects.create(
                            icg_type="color",
                            name=expected_name,
                            prestashop_id=remote_ps_id,
                            product=product,
                        )
                except IntegrityError:
                    conflicts += 1
                    self.stderr.write(
                        self.style.WARNING(
                            f"Conflict: cannot create "
                            f"{expected_name} with PS #{remote_ps_id} — "
                            f"id already claimed"
                        )
                    )
                    continue
            created += 1

        mode = "APPLIED" if apply else "DRY RUN"
        self.stdout.write(self.style.SUCCESS(f"[{mode}] Import missing color groups"))
        self.stdout.write(f"  Created: {created}")
        self.stdout.write(f"  Updated (stale name): {updated}")
        self.stdout.write(f"  Already correct: {already_ok}")
        self.stdout.write(f"  Not found remotely: {not_found_remote}")
        self.stdout.write(f"  Conflicts: {conflicts}")
