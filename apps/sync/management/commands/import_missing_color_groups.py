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
        parser.add_argument(
            "--resolve-conflicts",
            action="store_true",
            help=(
                "Attempt to resolve prestashop_id conflicts by releasing the "
                "ID from stale holder groups before remapping."
            ),
        )

    def _lookup_holder(self, remote_ps_id: int, exclude_pk: int):
        return (
            AttributeGroup.objects.select_related("product")
            .filter(prestashop_id=remote_ps_id)
            .exclude(pk=exclude_pk)
            .first()
        )

    def handle(self, *args, **options):
        client = PrestashopClient()
        apply = options["apply"]
        resolve = options.get("resolve_conflicts", False)

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
        resolved = 0

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
                            old_ps_id = existing.prestashop_id
                            existing.prestashop_id = remote_ps_id
                            existing.name = expected_name
                            existing.save(update_fields=["prestashop_id", "name", "updated_at"])
                            if old_ps_id != remote_ps_id:
                                existing.values.all().delete()
                    except IntegrityError:
                        existing.refresh_from_db(fields=["prestashop_id", "name"])
                        stale_ps_id = existing.prestashop_id
                        holder = self._lookup_holder(remote_ps_id, existing.pk)
                        holder_info = ""
                        if holder is not None:
                            ref = holder.product.reference if holder.product else "no-product"
                            holder_info = (
                                f"held by pk={holder.pk} " f"(product={ref}, name={holder.name}"
                            )
                            holder_is_stale = (
                                holder.product
                                and holder.product.prestashop_id
                                and holder.name != f"{holder.product.prestashop_id}_color"
                            )
                            holder_info += ", STALE)" if holder_is_stale else ", correct)"
                        else:
                            holder_info = "holder not found (ghost id)"

                        if resolve and holder is not None and holder_is_stale:
                            try:
                                with transaction.atomic():
                                    max_ps = (
                                        AttributeGroup.objects.order_by("-prestashop_id")
                                        .values_list("prestashop_id", flat=True)
                                        .first()
                                    )
                                    temp_id = (max_ps or 0) + 1
                                    holder.prestashop_id = temp_id
                                    holder.save(update_fields=["prestashop_id", "updated_at"])
                                    existing.prestashop_id = remote_ps_id
                                    existing.name = expected_name
                                    existing.save(
                                        update_fields=["prestashop_id", "name", "updated_at"]
                                    )
                                    holder.prestashop_id = stale_ps_id
                                    holder.save(update_fields=["prestashop_id", "updated_at"])
                                resolved += 1
                                holder_ref = holder.product.reference if holder.product else "?"
                                self.stdout.write(
                                    self.style.SUCCESS(
                                        f"Resolved: swapped {product.reference} "
                                        f"(→ PS #{remote_ps_id}) with "
                                        f"{holder_ref} (→ PS #{stale_ps_id})"
                                    )
                                )
                                updated += 1
                                continue
                            except IntegrityError:
                                pass

                        conflicts += 1
                        self.stderr.write(
                            self.style.WARNING(
                                f"Conflict: cannot remap "
                                f"{product.reference} (pk={existing.pk}, "
                                f"name={existing.name}) to PS #{remote_ps_id} "
                                f"({expected_name}) — {holder_info}"
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
                    holder = self._lookup_holder(remote_ps_id, exclude_pk=-1)
                    holder_info = ""
                    if holder is not None:
                        ref = holder.product.reference if holder.product else "no-product"
                        holder_info = (
                            f"held by pk={holder.pk} " f"(product={ref}, name={holder.name})"
                        )
                    else:
                        holder_info = "holder not found (ghost id)"
                    conflicts += 1
                    self.stderr.write(
                        self.style.WARNING(
                            f"Conflict: cannot create "
                            f"{expected_name} with PS #{remote_ps_id} — "
                            f"{holder_info}"
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
        if resolved:
            self.stdout.write(f"  Resolved (swapped stale pairs): {resolved}")
