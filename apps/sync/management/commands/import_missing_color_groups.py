from django.core.management.base import BaseCommand

from apps.catalog.models import AttributeGroup, Product
from apps.prestashop.client import PrestashopClient


class Command(BaseCommand):
    help = (
        "Import missing color attribute groups from Prestashop for products "
        "that already have a prestashop_id. Creates local AttributeGroup "
        "records for remote groups named {prestashop_id}_color that do not "
        "yet exist in Django."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Create the missing AttributeGroup records. Without this flag, dry-run.",
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
        already_exists = 0
        not_found_remote = 0

        for product in products.iterator():
            expected_name = f"{product.prestashop_id}_color"

            if AttributeGroup.objects.filter(icg_type="color", product=product).exists():
                already_exists += 1
                continue

            remote_ps_id = remote_by_name.get(expected_name)
            if remote_ps_id is None:
                not_found_remote += 1
                continue

            if apply:
                AttributeGroup.objects.create(
                    icg_type="color",
                    name=expected_name,
                    prestashop_id=remote_ps_id,
                    product=product,
                )
            created += 1

        mode = "APPLIED" if apply else "DRY RUN"
        self.stdout.write(self.style.SUCCESS(f"[{mode}] Import missing color groups"))
        self.stdout.write(f"  Created: {created}")
        self.stdout.write(f"  Already exists (local): {already_exists}")
        self.stdout.write(f"  Not found remotely: {not_found_remote}")
