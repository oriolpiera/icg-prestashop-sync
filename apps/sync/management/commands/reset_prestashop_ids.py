from django.core.management.base import BaseCommand

from apps.catalog.models import Category, Combination, Manufacturer, Product

ENTITY_MODELS = {
    "product": Product,
    "combination": Combination,
    "category": Category,
    "manufacturer": Manufacturer,
}


class Command(BaseCommand):
    help = (
        "Reset prestashop_id for all entities of a given type, "
        "marking them for re-export on the next sync run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "entity_type",
            choices=list(ENTITY_MODELS),
            help="Entity type whose prestashop_id values should be cleared.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be changed without writing to the database.",
        )

    def handle(self, *args, **options):
        entity_type = options["entity_type"]
        dry_run = options["dry_run"]

        model = ENTITY_MODELS[entity_type]
        qs = model.objects.filter(prestashop_id__isnull=False)
        count = qs.count()

        if count == 0:
            self.stdout.write(
                self.style.WARNING(
                    f"No {entity_type} records with prestashop_id set."
                )
            )
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN: {count} {entity_type}(s) would have "
                    "prestashop_id cleared."
                )
            )
            return

        updated = qs.update(prestashop_id=None, sync_required=True)
        self.stdout.write(
            self.style.SUCCESS(
                f"Cleared prestashop_id for {updated} {entity_type}(s)."
            )
        )
